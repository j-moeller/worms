import hashlib
import os
import tempfile

from core.models.competition import Competition
from core.models.team import Team
from core.models.worms import MalwareArtifact, Upload
from core.views.decorators import user_in_team_required
from core.views.util import render_with_base_info
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.files import File
from django.core.files.storage import default_storage
from django.db import transaction
from django.http import HttpRequest, JsonResponse


@login_required
@user_in_team_required
def upload_get_method(request: HttpRequest, team: Team, *, competition: Competition):
    uploads = (
        Upload.objects.filter(
            user__team_memberships__team=team,
        )
        .select_related("user", "artifact")
        .prefetch_related(
            "worm_deployments__round",
        )
        .order_by("-created_at")
    )

    rows = []

    for upload in uploads:
        deployments = sorted(
            upload.worm_deployments.all(),
            key=lambda deployment: (
                deployment.round.round_start,
                deployment.round.pk,
            ),
            reverse=True,
        )

        rows.append(
            {
                "content_hash": upload.artifact.content_hash,
                "time_date": upload.created_at,
                "username": upload.user.username,
                "rounds": [deployment.round.name for deployment in deployments],
                "referenced": bool(deployments),
            }
        )

    return render_with_base_info(
        request,
        "uploads.html",
        {
            "uploads": rows,
        },
    )


@login_required
def upload_post_method(request: HttpRequest):
    user = request.user

    artifact_hash = hashlib.sha256()
    total_size = 0

    # Keep the incoming artifact outside the application's permanent
    # storage until we know its content hash.
    with tempfile.NamedTemporaryFile(
        mode="w+b",
        prefix="upload-",
        suffix=".bin",
        dir=getattr(settings, "FILE_UPLOAD_TEMP_DIR", None),
        delete=False,
    ) as temporary_file:
        temporary_path = temporary_file.name

        try:
            while True:
                chunk = request.read(settings.CHUNK_SIZE)

                if not chunk:
                    break

                total_size += len(chunk)

                if total_size > settings.MAX_UPLOAD_SIZE:
                    return JsonResponse(
                        {"error": "file too large"},
                        status=413,
                    )

                artifact_hash.update(chunk)
                temporary_file.write(chunk)

            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        finally:
            temporary_file.close()

    content_hash = artifact_hash.hexdigest()

    storage_key = (
        f"malware-artifacts/"
        f"{content_hash[:2]}/"
        f"{content_hash[2:4]}/"
        f"{content_hash}.zip"
    )

    try:
        with transaction.atomic():
            artifact = MalwareArtifact.objects.filter(
                content_hash=content_hash,
            ).first()

            if artifact is None:
                with open(temporary_path, "rb") as artifact_file:
                    saved_key = default_storage.save(
                        storage_key,
                        File(artifact_file),
                    )

                artifact = MalwareArtifact.objects.create(
                    content_hash=content_hash,
                    storage_key=saved_key,
                )

            upload = Upload.objects.create(
                artifact=artifact,
                user=user,
            )

    finally:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass

    return JsonResponse(
        {
            "upload_id": upload.pk,
            "artifact_id": artifact.pk,
            "content_hash": artifact.content_hash,
            "size": total_size,
        },
        status=201,
    )


@login_required
def upload(request: HttpRequest):
    if request.method == "GET":
        return upload_get_method(request)
    elif request.method == "POST":
        return upload_post_method(request)
    else:
        return JsonResponse(
            {"error": "method not allowed"},
            status=405,
        )
