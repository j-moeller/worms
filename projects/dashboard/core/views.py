import hashlib
import json
import os
import tempfile

from django.conf import settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.files import File
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import (
    ApplicationSetting,
    FlagCapture,
    FlagCaptureAttempt,
    Logging,
    LoggingAttempt,
    MalwareArtifact,
    Round,
    Team,
    Upload,
    User,
)

MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100 MiB
CHUNK_SIZE = 1024 * 1024  # 1 MiB


def render_with_debug(request, template, context=None, *args, **kwargs):
    if context is None:
        context = {}

    context["debug"] = (settings.DEBUG,)
    if settings.DEBUG:
        context["available_users"] = User.objects.select_related("team").order_by(
            "username"
        )
    return render(request, template, context, *args, **kwargs)


def leaderboard(request):
    try:
        settings = ApplicationSetting.objects.select_related("current_competition").get(
            key="current"
        )
    except ApplicationSetting.DoesNotExist:
        return HttpResponse("No competition selected")

    competition = settings.current_competition

    teams = list(
        Team.objects.filter(competition=competition)
        .annotate(points=Count("flag_captures"))
        .order_by("-points", "name")
    )

    captures = list(
        FlagCapture.objects.filter(round__competition=competition)
        .select_related(
            "team",
            "flag_capture_attempt",
        )
        .order_by("flag_capture_attempt__created_at")
    )

    chart_data = []

    points_by_team = {}

    for capture in captures:
        team_id = capture.team_id

        points_by_team[team_id] = points_by_team.get(team_id, 0) + 1

        chart_data.append(
            {
                "time": capture.flag_capture_attempt.created_at.isoformat(),
                "team": capture.team.name,
                "points": points_by_team[team_id],
            }
        )

    rounds = Round.objects.filter(competition=competition).order_by("round_start")

    round_data = [
        {
            "name": round.name,
            "start": round.round_start.isoformat(),
            "end": round.round_end.isoformat(),
        }
        for round in rounds
    ]

    return render_with_debug(
        request,
        "leaderboard.html",
        {
            "competition": competition,
            "teams": teams,
            "chart_data": chart_data,
            "round_data": round_data,
        },
    )


def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")

        user = authenticate(
            request,
            username=username,
            password=password,
        )

        if user is not None:
            login(request, user)
            return redirect("dashboard")

        return render_with_debug(
            request,
            "login.html",
            {
                "error": "Invalid username or password.",
            },
            status=401,
        )

    return render_with_debug(request, "login.html")


@login_required
def logout_view(request):
    logout(request)
    return redirect("login")


@login_required
def dashboard(request):
    context = {
        "user": request.user,
    }

    return render_with_debug(request, "dashboard.html", context)


@login_required
@require_POST
def debug_login_as(request, user_id):
    if not settings.DEBUG:
        return JsonResponse(
            {"error": "not available"},
            status=404,
        )

    user = get_object_or_404(User, pk=user_id)

    login(request, user)

    next_url = request.POST.get("next")

    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)

    return redirect("dashboard")


@login_required
def upload_get_method(request):
    user = request.user

    uploads = (
        Upload.objects.filter(user__team=user.team)
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

    return render_with_debug(
        request,
        "uploads.html",
        {
            "uploads": rows,
        },
    )


@login_required
def upload_post_method(request):
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
                chunk = request.read(CHUNK_SIZE)

                if not chunk:
                    break

                total_size += len(chunk)

                if total_size > MAX_UPLOAD_SIZE:
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
def upload(request):
    if request.method == "GET":
        return upload_get_method(request)
    elif request.method == "POST":
        return upload_post_method(request)
    else:
        return JsonResponse(
            {"error": "method not allowed"},
            status=405,
        )


@login_required
def flag_capture_attempts(request):
    team = request.user.team

    if team is None:
        return render_with_debug(
            request,
            "flag_capture_attempts.html",
            {"rows": []},
        )

    attempts = list(
        FlagCaptureAttempt.objects.filter(
            team_token_hash=team.access_token_hash
        ).order_by("-created_at", "-id")
    )

    attempt_ids = [attempt.id for attempt in attempts]

    captures = FlagCapture.objects.filter(
        flag_capture_attempt_id__in=attempt_ids,
        team_id=team.id,
        round__competition_id=team.competition_id,
        vm__round__competition_id=team.competition_id,
    ).select_related("vm", "round")

    capture_by_attempt = {
        capture.flag_capture_attempt_id: capture for capture in captures
    }

    rounds = list(
        Round.objects.filter(competition_id=team.competition_id).order_by(
            "-round_start", "-id"
        )
    )

    rows = []

    for round_obj in rounds:
        for attempt in attempts:
            if not (round_obj.round_start <= attempt.created_at < round_obj.round_end):
                continue

            capture = capture_by_attempt.get(attempt.id)

            if (
                capture is not None
                and capture.round_id == round_obj.id
                and capture.vm.round_id == round_obj.id
            ):
                vm_identifier = capture.vm.vm_identifier
                invalid = False
            else:
                vm_identifier = None
                invalid = True

            rows.append(
                {
                    "round": round_obj,
                    "attempt": attempt,
                    "vm_identifier": vm_identifier,
                    "invalid": invalid,
                }
            )

    return render_with_debug(
        request,
        "flag_capture_attempts.html",
        {"rows": rows},
    )


@login_required
def logs(request):
    team = request.user.team

    if team is None:
        return render(
            request,
            "logs.html",
            {"rounds": []},
        )

    events = (
        Logging.objects.filter(team=team)
        .select_related("round", "logging_attempt")
        .order_by("-created_at")
    )

    rounds = []

    for event in events:
        if not rounds or rounds[-1]["round"].pk != event.round.pk:
            rounds.append(
                {
                    "round": event.round,
                    "events": [],
                }
            )

        rounds[-1]["events"].append(event)

    return render_with_debug(
        request,
        "logs.html",
        {"rounds": rounds},
    )


@login_required
def logging_event_data(request, uuid):
    if request.user.team is None:
        return HttpResponse(status=404)

    event = get_object_or_404(
        Logging.objects.select_related("logging_attempt"),
        logging_attempt__uuid=uuid,
        team=request.user.team,
    )

    return JsonResponse(event.logging_attempt.log_data)
