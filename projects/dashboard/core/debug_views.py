import hashlib
import random
import secrets
from datetime import datetime, timedelta

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .models import (
    Competition,
    FlagCapture,
    FlagCaptureAttempt,
    Logging,
    LoggingAttempt,
    MalwareArtifact,
    Round,
    Student,
    Team,
    Upload,
    User,
    VirtualMachine,
    WormDeployment,
    ApplicationSetting,
)

# -------------------------------------------------------------------------
# Deterministic randomness
# -------------------------------------------------------------------------

SEED = 20260910


def hash_value(value):
    return hashlib.sha256(value.encode()).hexdigest()


@require_http_methods(["GET", "POST"])
def seed_dummy_data(request):
    if not settings.DEBUG:
        return JsonResponse(
            {"error": ("Dummy data endpoint is only available " "in DEBUG mode.")},
            status=403,
        )

    if request.method == "GET":
        return render(request, "seed_dummy_data.html", {})

    # ---------------------------------------------------------------------
    # Delete existing dummy data
    #
    # Delete dependent/processed records before their attempts.
    # ---------------------------------------------------------------------

    ApplicationSetting.objects.all().delete()

    FlagCapture.objects.all().delete()
    Logging.objects.all().delete()
    WormDeployment.objects.all().delete()

    FlagCaptureAttempt.objects.all().delete()
    LoggingAttempt.objects.all().delete()

    Upload.objects.all().delete()
    MalwareArtifact.objects.all().delete()
    VirtualMachine.objects.all().delete()
    Student.objects.all().delete()
    User.objects.all().delete()
    Team.objects.all().delete()
    Round.objects.all().delete()
    Competition.objects.all().delete()

    user = User.objects.create_user(
        username="root",
        password="root",
        first_name="",
        last_name="",
        is_staff=True,
        is_active=True,
        team=None,
    )

    # ---------------------------------------------------------------------
    # Deterministic RNG
    #
    # random.Random is seeded, so all generated distributions/timestamps
    # are reproducible.
    #
    # secrets is intentionally still used for token/flag values. Those
    # values do not need to be deterministic for the dummy data.
    # ---------------------------------------------------------------------

    rng = random.Random(SEED)

    # ---------------------------------------------------------------------
    # Competition
    # ---------------------------------------------------------------------

    competition = Competition.objects.create(
        name="Dummy CTF 2026",
    )

    ApplicationSetting.objects.create(key="current", current_competition=competition)

    # ---------------------------------------------------------------------
    # Rounds
    #
    # Five weekly rounds.
    #
    # Monday 09:00 -> Friday 17:00
    # ---------------------------------------------------------------------

    round_start_base = timezone.make_aware(datetime(2026, 9, 7, 9, 0, 0))

    rounds = []

    for i in range(5):
        round_start = round_start_base + timedelta(days=i * 7)

        round_end = round_start + timedelta(
            days=4,
            hours=8,
        )

        round_obj = Round.objects.create(
            seed=1000 + i,
            round_start=round_start,
            round_end=round_end,
            name=f"Round {i + 1}",
            competition=competition,
        )

        rounds.append(round_obj)

    # ---------------------------------------------------------------------
    # Teams
    #
    # Use a skewed distribution so teams are not equally strong.
    #
    # The resulting skill multiplier is subsequently used for:
    #
    #   - number of uploads
    #   - number of flag attempts
    #   - flag success probability
    #   - number of logs
    #
    # Randomness is retained so the relationship is not perfectly linear.
    # ---------------------------------------------------------------------

    team_names = [
        "Team Alpha",
        "Team Bravo",
        "Team Charlie",
        "Team Delta",
        "Team Echo",
    ]

    raw_skills = [
        rng.gammavariate(
            alpha=5.0,
            beta=1.0,
        )
        for _ in team_names
    ]

    mean_skill = sum(raw_skills) / len(raw_skills)

    team_skills = [
        max(
            0.45,
            min(
                1.80,
                skill / mean_skill,
            ),
        )
        for skill in raw_skills
    ]

    teams = []
    team_skill = {}
    team_token_hash = {}

    for i, name in enumerate(team_names):
        raw_token = secrets.token_hex(32)
        token_hash = hash_value(raw_token)

        team = Team.objects.create(
            name=name,
            access_token_hash=token_hash,
            competition=competition,
        )

        teams.append(team)
        team_skill[team.id] = team_skills[i]
        team_token_hash[team.id] = token_hash

    # ---------------------------------------------------------------------
    # Users + Students
    # ---------------------------------------------------------------------

    students = [
        ("Alice", "Miller", "alice@miller.com", "M100001"),
        ("Bob", "Smith", "bob@smith.com", "M100002"),
        ("Charlie", "Johnson", "charlie@johnson.com", "M100003"),
        ("Diana", "Williams", "diana@williams.com", "M100004"),
        ("Eve", "Dallinger", "eve@dallinger.com", "M100005"),
    ]

    users = []
    user_by_team = {}

    for i, team in enumerate(teams):
        first_name, last_name, email, matriculation = students[i]

        user = User.objects.create(
            team=team,
            username=email,
            first_name=first_name,
            last_name=last_name,
            email=email,
            is_staff=False,
            is_active=True,
        )

        Student.objects.create(
            matriculation_number=matriculation,
            user=user,
        )

        users.append(user)
        user_by_team[team.id] = user

    # ---------------------------------------------------------------------
    # Virtual Machines
    #
    # Three to five VMs per round.
    # ---------------------------------------------------------------------

    virtual_machines = []
    vms_by_round = {}

    for round_obj in rounds:
        vm_count = rng.randint(3, 5)

        vms_by_round[round_obj.id] = []

        for i in range(vm_count):
            raw_flag = secrets.token_hex(16)

            vm = VirtualMachine.objects.create(
                vm_identifier=(f"vm-r{round_obj.id}-{i + 1}"),
                vm_maintenance_ip=(f"10.0.{round_obj.id}.{10 + i}"),
                ctf_flag_hash=hash_value(raw_flag),
                round=round_obj,
            )

            virtual_machines.append(vm)
            vms_by_round[round_obj.id].append(vm)

    # ---------------------------------------------------------------------
    # Malware artifacts
    # ---------------------------------------------------------------------

    artifact_count = rng.randint(8, 12)

    artifacts = []

    for _ in range(artifact_count):
        content = secrets.token_bytes(32)
        content_hash = hashlib.sha256(content).hexdigest()

        artifact = MalwareArtifact.objects.create(
            content_hash=content_hash,
            storage_key=(f"dummy/artifacts/{content_hash}"),
        )

        artifacts.append(artifact)

    # ---------------------------------------------------------------------
    # Worm uploads
    #
    # Every team gets at least one upload before Round 1.
    #
    # Better teams generally upload more often, but the distribution is
    # noisy.
    #
    # Upload timestamps span the entire competition.
    # ---------------------------------------------------------------------

    uploads = []

    competition_start = rounds[0].round_start
    competition_end = rounds[-1].round_end

    total_competition_seconds = int(
        (competition_end - competition_start).total_seconds()
    )

    for team in teams:
        skill = team_skill[team.id]

        # Better teams tend to upload more often.
        upload_count = max(
            1,
            int(
                round(
                    rng.gauss(
                        3.0 * skill,
                        0.8,
                    )
                )
            ),
        )

        upload_count = min(
            upload_count,
            8,
        )

        upload_times = []

        # -------------------------------------------------------------
        # Guaranteed initial upload before Round 1.
        # -------------------------------------------------------------

        first_upload_time = competition_start - timedelta(hours=rng.randint(2, 72))

        upload_times.append(first_upload_time)

        # -------------------------------------------------------------
        # Additional uploads throughout the competition.
        # -------------------------------------------------------------

        for _ in range(upload_count - 1):
            offset_seconds = rng.randint(
                0,
                total_competition_seconds,
            )

            upload_times.append(competition_start + timedelta(seconds=offset_seconds))

        upload_times.sort()

        for upload_time in upload_times:
            artifact = rng.choice(artifacts)

            # created_at can now be set directly.
            upload = Upload.objects.create(
                artifact=artifact,
                user=user_by_team[team.id],
                created_at=upload_time,
            )

            uploads.append(upload)

    # ---------------------------------------------------------------------
    # Flag Capture Attempts
    #
    # Every attempt is now stored in FlagCaptureAttempt.
    #
    # The attempt itself has no round FK, so the timestamp is what places
    # it in a particular round.
    #
    # For successful attempts, vm_flag_hash is the actual VM's flag hash.
    # For unsuccessful attempts, generate an incorrect hash.
    #
    # We keep the selected round and VM in memory so that, when the
    # attempt succeeds, the normalized FlagCapture record can reference
    # the same round/VM.
    # ---------------------------------------------------------------------

    flag_capture_attempts = []
    flag_captures = []

    for team in teams:
        skill = team_skill[team.id]

        for round_obj in rounds:
            round_duration = (
                round_obj.round_end - round_obj.round_start
            ).total_seconds()

            # Stronger teams generally make more attempts.
            expected_attempts = 6.0 * skill

            attempt_count = max(
                1,
                min(
                    12,
                    int(
                        round(
                            rng.gauss(
                                expected_attempts,
                                1.5,
                            )
                        )
                    ),
                ),
            )

            for attempt_number in range(attempt_count):
                # -----------------------------------------------------
                # Strictly inside:
                #
                # round_start < attempt_time < round_end
                # -----------------------------------------------------

                offset_seconds = rng.uniform(
                    1,
                    round_duration - 1,
                )

                attempt_time = round_obj.round_start + timedelta(seconds=offset_seconds)

                round_vms = vms_by_round[round_obj.id]

                vm = rng.choice(round_vms)

                # -----------------------------------------------------
                # Success probability depends on team skill.
                # -----------------------------------------------------

                capture_probability = min(
                    0.85,
                    max(
                        0.08,
                        0.40 * skill,
                    ),
                )

                successful = rng.random() <= capture_probability

                if successful:
                    # The submitted value is the correct flag.
                    vm_flag_hash = vm.ctf_flag_hash
                else:
                    # Generate an incorrect flag hash.
                    #
                    # It is overwhelmingly unlikely to collide with
                    # the actual VM flag.
                    vm_flag_hash = hash_value(
                        f"dummy-wrong-flag-"
                        f"{team.id}-"
                        f"{round_obj.id}-"
                        f"{attempt_number}-"
                        f"{secrets.token_hex(8)}"
                    )

                # -----------------------------------------------------
                # Raw attempt.
                # -----------------------------------------------------

                attempt = FlagCaptureAttempt.objects.create(
                    team_token_hash=team_token_hash[team.id],
                    vm_flag_hash=vm_flag_hash,
                    processed=successful,
                    processed_at=(attempt_time if successful else None),
                    created_at=attempt_time,
                )

                flag_capture_attempts.append(attempt)

                # -----------------------------------------------------
                # Only successful attempts become FlagCapture rows.
                # -----------------------------------------------------

                if successful:
                    capture = FlagCapture.objects.create(
                        team=team,
                        round=round_obj,
                        vm=vm,
                        flag_capture_attempt=attempt,
                        created_at=attempt_time,
                    )

                    flag_captures.append(capture)

    # ---------------------------------------------------------------------
    # Logging Attempts + Logging
    #
    # Logging attempts are now generated separately from Logging.
    #
    # The raw LoggingAttempt represents the incoming log event.
    # The Logging row represents the processed/accepted event.
    #
    # Since LoggingAttempt has no round FK, we include the round_id and
    # round name in log_data and ensure created_at falls inside that round.
    # ---------------------------------------------------------------------

    logging_attempts = []
    logging_records = []

    log_severities = [
        "info",
        "info",
        "info",
        "warning",
        "error",
    ]

    log_events = [
        "dummy_event",
        "connection",
        "scan",
        "malware_activity",
        "flag_attempt",
        "vm_access",
    ]

    log_sources = [
        "dummy-seeder",
        "vm-agent",
        "network-monitor",
        "ctf-service",
    ]

    for team in teams:
        skill = team_skill[team.id]

        # Generate activity separately for every round so that logging
        # also has a meaningful temporal distribution.
        for round_obj in rounds:
            round_duration = (
                round_obj.round_end - round_obj.round_start
            ).total_seconds()

            expected_logs = 12.0 * skill / len(rounds)

            log_count = max(
                1,
                int(
                    round(
                        rng.gauss(
                            expected_logs,
                            1.5,
                        )
                    )
                ),
            )

            for i in range(log_count):
                offset_seconds = rng.uniform(
                    1,
                    round_duration - 1,
                )

                log_time = round_obj.round_start + timedelta(seconds=offset_seconds)

                severity = rng.choice(log_severities)

                event = rng.choice(log_events)

                source = rng.choice(log_sources)

                log_data = {
                    "event": event,
                    "message": (f"Dummy log message {i + 1}"),
                    "severity": severity,
                    "source": source,
                    "counter": i + 1,
                    # LoggingAttempt has no round FK, so preserve the
                    # round context in the payload.
                    "round_id": round_obj.id,
                    "round_name": round_obj.name,
                }

                logging_attempt = LoggingAttempt.objects.create(
                    team_token_hash=team_token_hash[team.id],
                    log_data=log_data,
                    processed=True,
                    processed_at=log_time,
                    created_at=log_time,
                )

                logging_attempts.append(logging_attempt)

                logging_record = Logging.objects.create(
                    team=team,
                    round=round_obj,
                    logging_attempt=logging_attempt,
                    created_at=log_time,
                )

                logging_records.append(logging_record)

    # ---------------------------------------------------------------------
    # Worm Deployments
    #
    # For every round/team:
    #
    #     select the latest upload where
    #
    #         upload.created_at <= round.round_start
    #
    # This means a worm uploaded after the round starts cannot affect that
    # round.
    #
    # Because every team has an upload before Round 1, every team should
    # receive a deployment for every round.
    # ---------------------------------------------------------------------

    deployments = []

    for round_obj in rounds:
        for team in teams:
            latest_upload = (
                Upload.objects.filter(
                    user=user_by_team[team.id],
                    created_at__lte=round_obj.round_start,
                )
                .select_related("artifact")
                .order_by(
                    "-created_at",
                    "-pk",
                )
                .first()
            )

            if latest_upload is None:
                # Defensive guard. Normally impossible because every
                # team receives an upload before Round 1.
                continue

            deployment = WormDeployment.objects.create(
                round=round_obj,
                team=team,
                upload=latest_upload,
                created_at=round_obj.round_start,
            )

            deployments.append(deployment)

    # ---------------------------------------------------------------------
    # Response
    # ---------------------------------------------------------------------

    return redirect("/")

    return JsonResponse(
        {
            "success": True,
            "seed": SEED,
            "created": {
                "competitions": 1,
                "rounds": len(rounds),
                "teams": len(teams),
                "users": len(users),
                "students": len(students),
                "virtual_machines": len(virtual_machines),
                "malware_artifacts": len(artifacts),
                "uploads": len(uploads),
                "flag_capture_attempts": len(flag_capture_attempts),
                "flag_captures": len(flag_captures),
                "logging_attempts": len(logging_attempts),
                "logging": len(logging_records),
                "worm_deployments": len(deployments),
            },
            "team_skills": {
                team.name: round(
                    team_skill[team.id],
                    3,
                )
                for team in teams
            },
        }
    )
