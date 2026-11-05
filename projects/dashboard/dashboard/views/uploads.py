"""Worm uploads.

A plain multipart form (works without JavaScript); the static enhance.js only
adds drag and drop and a progress bar to the very same request. An artifact is
identified by the SHA-256 of the file's content: the same zip uploaded twice is
stored once. An upload belongs to the uploader's team at the time; the team's
latest upload before a round starts is its entry (orchestrator/rounds/planner.py).
"""

from contextlib import suppress
import hashlib
import logging
import os
import uuid
import zipfile

from core.locking import serialized_resource
from core.models.team import Team
from core.models.worms import MalwareArtifact, Upload
from dashboard.views.decorators import user_in_team_required
from dashboard.views.util import render_with_base_info
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.storage import default_storage
from django.http import HttpRequest, HttpResponseNotAllowed
from django.shortcuts import redirect
from django.template.defaultfilters import filesizeformat

log = logging.getLogger("dashboard")

FIELD = "artifact"
ENTRY = "main.sh"
# The multipart envelope around the file (boundaries, headers, the CSRF field).
ENVELOPE = 64 * 1024


@login_required
@user_in_team_required
def upload(request: HttpRequest, team: Team):
    if request.method == "POST":
        return _receive(request, team)
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])
    return _history(request, team)


def _history(request: HttpRequest, team: Team):
    uploads = (
        Upload.objects.filter(team=team)
        .select_related("user", "artifact")
        .prefetch_related("runs__round")
        .order_by("-created_at")
    )
    rows = []
    for upload in uploads:
        # The rounds this upload was run in (newest first).
        rounds = sorted(
            {run.round for run in upload.runs.all()},
            key=lambda round: (round.round_start is not None, round.round_start, round.pk),
            reverse=True,
        )
        rows.append(
            {
                "content_hash": upload.artifact.content_hash,
                "time_date": upload.created_at,
                "username": upload.user.username,
                "rounds": [round.name for round in rounds],
                "referenced": bool(rounds),
            }
        )
    return render_with_base_info(
        request,
        "uploads.html",
        {"uploads": rows, "max_size": settings.MAX_UPLOAD_SIZE},
    )


def _check_zip(uploaded) -> str | None:
    """Why this file cannot run as a worm, or None. The round agent unpacks
    the zip and starts `bash main.sh` from its top level
    (docs/worm-contract.md)."""
    uploaded.seek(0)
    try:
        with zipfile.ZipFile(uploaded) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError, ValueError):
        return f"That is not a .zip archive. The worm is unpacked and its {ENTRY} started."
    if ENTRY not in names:
        return f"The zip has no {ENTRY} at its top level (not in a folder): that is the file that is started."
    return None


def _store_upload(uploaded, storage_key, digest):
    """Publish a complete file atomically; interrupted copies stay unreferenced.

    This application and the round agent share a local MEDIA_ROOT. Repair a
    missing/corrupt copy on re-upload, even when its database row still exists.
    """
    if default_storage.exists(storage_key):
        with default_storage.open(storage_key, "rb") as existing:
            stored = hashlib.sha256()
            for chunk in existing.chunks():
                stored.update(chunk)
            if stored.hexdigest() == digest:
                return
    staging = f"{storage_key}.pending-{uuid.uuid4().hex}"
    saved = staging
    try:
        uploaded.seek(0)
        saved = default_storage.save(staging, uploaded)
        os.replace(default_storage.path(saved), default_storage.path(storage_key))
    finally:
        with suppress(OSError):
            default_storage.delete(saved)


def _receive(request: HttpRequest, team: Team):
    limit = settings.MAX_UPLOAD_SIZE
    # Refuse an oversized body before reading it (nginx caps it, too).
    try:
        declared = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        declared = 0
    if declared > limit + ENVELOPE:
        messages.error(request, f"That file is larger than {filesizeformat(limit)}.")
        return redirect("upload")

    uploaded = request.FILES.get(FIELD)
    if uploaded is None or uploaded.size == 0:
        messages.error(request, "Choose a .zip file to upload.")
        return redirect("upload")
    if uploaded.size > limit:
        messages.error(request, f"That file is larger than {filesizeformat(limit)}.")
        return redirect("upload")
    problem = _check_zip(uploaded)
    if problem:
        messages.error(request, problem)
        return redirect("upload")

    digest = hashlib.sha256()
    uploaded.seek(0)
    for chunk in uploaded.chunks(settings.CHUNK_SIZE):
        digest.update(chunk)
    content_hash = digest.hexdigest()
    # Content-addressed: the key is fixed by the hash, and the round agent
    # accepts exactly this shape (scripts/round_agent/validation.py).
    storage_key = f"malware-artifacts/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}.zip"

    try:
        with serialized_resource("upload", content_hash):
            _store_upload(uploaded, storage_key, content_hash)
            artifact, _ = MalwareArtifact.objects.get_or_create(
                content_hash=content_hash, defaults={"storage_key": storage_key},
            )
            Upload.objects.create(artifact=artifact, user=request.user, team=team)
    except OSError:
        log.exception("Could not store uploaded artifact")
        messages.error(request, "Could not store your upload. Please try again or contact an organiser.")
        return redirect("upload")

    messages.success(
        request,
        f"Uploaded {uploaded.name} ({filesizeformat(uploaded.size)}), sha256 {content_hash[:16]}…. "
        "Your team's latest upload before a round starts is the one that runs.",
    )
    return redirect("upload")
