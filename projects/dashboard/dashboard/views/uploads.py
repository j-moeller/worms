"""Worm uploads.

A plain multipart form (works without JavaScript); the static enhance.js only
adds drag and drop and a progress bar to the very same request. An artifact is
identified by the SHA-256 of the file's content: the same zip uploaded twice is
stored once. An upload belongs to the uploader's team at the time.

The two views are separate. Live: the team's latest upload before a round starts
is its entry (orchestrator/rounds/planner.py). Dev: the upload resets the team's
devbox and then runs on VM A (orchestrator/rounds/devbox_runs.py), and never enters
a round; a newer one cancels it. Only the orchestrator acts on it: this view only
stores it. A team can download its own uploads again from the history.
"""

from contextlib import suppress
import hashlib
import logging
import os
import re
import uuid
import zipfile
from datetime import timedelta

from core import clock
from core.models.devbox import DevboxRun, DevboxVM
from core.locking import serialized_resource
from core.models.team import Team
from core.models.worms import MalwareArtifact, Upload
from dashboard.views.decorators import user_in_team_required
from dashboard.views.devbox import active_upload
from dashboard.views.util import render_with_base_info
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.storage import default_storage
from django.http import FileResponse, Http404, HttpRequest, HttpResponseNotAllowed
from django.shortcuts import redirect
from django.template.defaultfilters import filesizeformat
from django.urls import reverse
from django.views.decorators.http import require_GET

log = logging.getLogger("dashboard")

FIELD = "artifact"
ENTRY = "main.sh"
# The multipart envelope around the file (boundaries, headers, the CSRF field).
ENVELOPE = 64 * 1024
# What a team sees of a dev upload. A failure's reason is for staff: it can name internals.
RUN_STATUS = {
    DevboxRun.State.PENDING: "Waiting for a reset or a round to finish",
    DevboxRun.State.RESETTING: "Resetting your VMs",
    DevboxRun.State.DEPLOYING: "Copying it to VM A",
    DevboxRun.State.ARMED: "About to start",
    DevboxRun.State.RUNNING: "Running",
    DevboxRun.State.FINISHED: "Finished",
    DevboxRun.State.FAILED: "Could not be started. Upload it again, or ask an organiser",
    DevboxRun.State.REPLACED: "Replaced by a newer upload or a reset",
}


def _environment(request: HttpRequest) -> str:
    return "dev" if request.is_dev_environment else "live"


@login_required
@user_in_team_required
def upload(request: HttpRequest, team: Team):
    if request.method == "POST":
        return _receive(request, team)
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])
    return _history(request, team)


def _history(request: HttpRequest, team: Team):
    environment = _environment(request)
    uploads = (
        Upload.objects.filter(team=team, devbox=environment == "dev")
        .select_related("user", "artifact", "devbox_run")
        .prefetch_related("runs__round")
        .order_by("-created_at")
    )
    # The dev upload under way: a new one cancels it (the page says so before it is sent).
    running = active_upload(team) if environment == "dev" else None
    rows = []
    for upload in uploads:
        # The rounds this upload was run in (newest first).
        rounds = sorted(
            {run.round for run in upload.runs.all()},
            key=lambda round: (round.round_start is not None, round.round_start, round.pk),
            reverse=True,
        )
        run = getattr(upload, "devbox_run", None)
        rows.append(
            {
                "content_hash": upload.artifact.content_hash,
                "download": reverse("upload_download", args=[upload.artifact.content_hash]),
                "time_date": upload.created_at,
                "username": upload.user.username,
                "rounds": [round.name for round in rounds],
                "referenced": bool(rounds),
                "status": RUN_STATUS[run.state] if run else RUN_STATUS[DevboxRun.State.PENDING],
                "ends_at": run.ends_at if run and run.state == DevboxRun.State.RUNNING else None,
                "running": running is not None and upload.pk == running.pk,
            }
        )
    current = next((row for row in rows if row["running"]), None)
    return render_with_base_info(
        request,
        "uploads.html",
        {
            "uploads": rows,
            "running": current,
            "max_size": settings.MAX_UPLOAD_SIZE,
            "environment": environment,
            "devbox_ready": environment != "dev" or DevboxVM.objects.filter(devbox__team=team).exists(),
        },
    )


CONTENT_HASH = re.compile(r"[0-9a-f]{64}")


@login_required
@require_GET
@user_in_team_required
def download(request: HttpRequest, team: Team, content_hash: str):
    """One of the team's own uploads, byte for byte. Only what the team uploaded itself: the
    same bytes in another team's upload are no way into that team's history."""
    upload = None
    if CONTENT_HASH.fullmatch(content_hash):
        upload = Upload.objects.filter(team=team, artifact__content_hash=content_hash).select_related("artifact").first()
    if upload is None:
        raise Http404("No such upload of your team.")
    try:
        stream = default_storage.open(upload.artifact.storage_key, "rb")
    except OSError:
        raise Http404("That upload is no longer stored.") from None
    response = FileResponse(stream, as_attachment=True, filename=f"worm-{content_hash[:12]}.zip", content_type="application/zip")
    response["Cache-Control"] = "private, no-store"
    return response


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


def _stored_bytes(team: Team) -> int:
    """What the team's uploads keep on the disk: each artifact once."""
    total = 0
    for key in MalwareArtifact.objects.filter(uploads__team=team).values_list("storage_key", flat=True).distinct():
        try:
            total += default_storage.size(key)
        except OSError:  # gone from the disk: it takes no space
            pass
    return total


def _over_quota(team: Team, size: int, content_hash: str) -> str | None:
    """Why this upload would put the team over its quota, or None."""
    since = clock.now() - timedelta(hours=1)
    if Upload.objects.filter(team=team, created_at__gte=since).count() >= settings.UPLOAD_TEAM_PER_HOUR:
        return f"Your team has made {settings.UPLOAD_TEAM_PER_HOUR} uploads in the last hour, the most allowed. Try again later."
    if MalwareArtifact.objects.filter(content_hash=content_hash, uploads__team=team).exists():
        return None  # already stored for this team: it adds nothing
    if _stored_bytes(team) + size > settings.UPLOAD_TEAM_QUOTA:
        return (
            f"Your team's uploads would take more than {filesizeformat(settings.UPLOAD_TEAM_QUOTA)}, "
            "the most stored per team. Ask an organiser."
        )
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

    # The page says which view it was in. Live and Dev share a session, so another tab can have
    # switched since, and an upload to the wrong one is either a round's entry or a reset.
    environment = _environment(request)
    if request.POST.get("environment") != environment:
        messages.error(
            request,
            f"You are in the {environment.capitalize()} view now, not the one that page was opened in, "
            "so nothing was uploaded. Check this page and upload again.",
        )
        return redirect("upload")
    dev = environment == "dev"
    if dev and not DevboxVM.objects.filter(devbox__team=team).exists():
        messages.error(request, "Your devbox has not been built yet, so there is nothing to run this on.")
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
    problem = _over_quota(team, uploaded.size, content_hash)
    if problem:
        messages.error(request, problem)
        return redirect("upload")
    # Content-addressed: the key is fixed by the hash, and the round agent
    # accepts exactly this shape (scripts/round_agent/validation.py).
    storage_key = f"malware-artifacts/{content_hash[:2]}/{content_hash[2:4]}/{content_hash}.zip"

    try:
        with serialized_resource("upload", content_hash):
            _store_upload(uploaded, storage_key, content_hash)
            artifact, _ = MalwareArtifact.objects.get_or_create(
                content_hash=content_hash, defaults={"storage_key": storage_key},
            )
            Upload.objects.create(artifact=artifact, user=request.user, team=team, devbox=dev)
    except OSError:
        log.exception("Could not store uploaded artifact")
        messages.error(request, "Could not store your upload. Please try again or contact an organiser.")
        return redirect("upload")

    messages.success(
        request,
        f"Uploaded {uploaded.name} ({filesizeformat(uploaded.size)}), sha256 {content_hash[:16]}…. "
        + (
            "Your devbox VMs are being reset, and then it runs on VM A."
            if dev
            else "Your team's latest upload before a round starts is the one that runs."
        ),
    )
    return redirect("upload")
