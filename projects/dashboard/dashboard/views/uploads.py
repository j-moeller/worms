"""Worm uploads.

A plain multipart form (works without JavaScript); the static enhance.js only
adds drag and drop and a progress bar to the very same request. An artifact is
identified by the SHA-256 of the file's content: the same zip uploaded twice is
stored once. The team's latest upload before a round starts is its entry
(orchestrator/rounds/planner.py).
"""

import hashlib
import zipfile

from core.models.team import Team
from core.models.worms import MalwareArtifact, Upload
from dashboard.views.decorators import user_in_team_required
from dashboard.views.util import render_with_base_info
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.storage import default_storage
from django.db import transaction
from django.http import HttpRequest, HttpResponseNotAllowed
from django.shortcuts import redirect
from django.template.defaultfilters import filesizeformat

FIELD = "artifact"
# The multipart envelope around the file (boundaries, headers, the CSRF field).
ENVELOPE = 64 * 1024


@login_required
@user_in_team_required
def upload(request: HttpRequest, team: Team):
    if request.method == "POST":
        return _receive(request)
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])
    return _history(request, team)


def _history(request: HttpRequest, team: Team):
    uploads = (
        Upload.objects.filter(user__team_memberships__team=team)
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


def _receive(request: HttpRequest):
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
    if not zipfile.is_zipfile(uploaded):
        messages.error(request, "That is not a .zip archive. The worm is unpacked and its main.py started.")
        return redirect("upload")

    digest = hashlib.sha256()
    uploaded.seek(0)
    for chunk in uploaded.chunks(settings.CHUNK_SIZE):
        digest.update(chunk)
    content_hash = digest.hexdigest()
    storage_key = f"malware-artifacts/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}.zip"

    with transaction.atomic():
        artifact = MalwareArtifact.objects.filter(content_hash=content_hash).first()
        if artifact is None:
            uploaded.seek(0)
            saved_key = default_storage.save(storage_key, uploaded)
            artifact = MalwareArtifact.objects.create(content_hash=content_hash, storage_key=saved_key)
        Upload.objects.create(artifact=artifact, user=request.user)

    messages.success(
        request,
        f"Uploaded {uploaded.name} ({filesizeformat(uploaded.size)}), sha256 {content_hash[:16]}…. "
        "Your team's latest upload before a round starts is the one that runs.",
    )
    return redirect("upload")
