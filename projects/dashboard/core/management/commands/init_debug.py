import hashlib
import random
import uuid
from datetime import timedelta

from core import clock
from core.models.competition import ApplicationSetting, Competition, Round
from core.models.logging import Logging, LoggingAttempt
from core.models.scoring import FlagCapture, FlagCaptureAttempt
from core.models.team import AccessTokens, Team, TeamMembership
from core.models.user import CompetitionParticipant, User
from core.models.vm import VirtualMachine
from core.models.worms import (
    MalwareArtifact,
    Upload,
    WormDeployment,
    WormDeploymentPlan,
)
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

VM_COUNT = 50


class Command(BaseCommand):
    help = "Initialize a deterministic CTF competition with dummy data."

    def add_arguments(self, parser):
        parser.add_argument(
            "--n-teams",
            type=int,
            default=5,
        )
        parser.add_argument(
            "--n-users",
            type=int,
            default=10,
        )
        parser.add_argument(
            "--n-rounds",
            type=int,
            default=10,
        )
        parser.add_argument(
            "--round-duration",
            type=int,
            default=120,
            help="Round duration in seconds.",
        )
        parser.add_argument(
            "--wait-between-rounds",
            type=int,
            default=60,
            help="Time between rounds in seconds.",
        )
        parser.add_argument(
            "--in-round",
            type=int,
            default=None,
            help="1-based round number when state is 'round'.",
        )
        parser.add_argument(
            "--state",
            choices=[
                "registration started",
                "registration ended",
                "round",
                "finished",
            ],
            required=True,
        )
        parser.add_argument(
            "--seed",
            type=int,
            default=1,
        )

    @transaction.atomic
    def handle(self, *args, **options):
        n_teams = options["n_teams"]
        n_users = options["n_users"]
        n_rounds = options["n_rounds"]
        round_duration = options["round_duration"]
        wait_between_rounds = options["wait_between_rounds"]
        in_round = options["in_round"]
        state = options["state"]
        seed = options["seed"]

        print("Validate")
        self._validate(
            n_teams=n_teams,
            n_users=n_users,
            n_rounds=n_rounds,
            round_duration=round_duration,
            wait_between_rounds=wait_between_rounds,
            in_round=in_round,
            state=state,
        )

        rng = random.Random(seed)
        now = clock.now()

        self._clear_database()

        print("Create competition")
        competition = self._create_competition(
            state=state,
            now=now,
            n_rounds=n_rounds,
            round_duration=round_duration,
            wait_between_rounds=wait_between_rounds,
            rng=rng,
        )

        print("Create rounds")
        rounds = self._create_rounds(
            competition=competition,
            state=state,
            now=now,
            n_rounds=n_rounds,
            round_duration=round_duration,
            wait_between_rounds=wait_between_rounds,
            in_round=in_round,
            rng=rng,
        )

        print("Create users")
        users = self._create_users(
            competition=competition,
            n_users=n_users,
            seed=seed,
            rng=rng,
        )

        print("Create teams")
        teams = self._create_teams(
            competition=competition,
            users=users,
            n_teams=n_teams,
            state=state,
            seed=seed,
            rng=rng,
        )

        print("Create application setting")
        ApplicationSetting.objects.create(
            key="current",
            current_competition=competition,
        )

        print("Create VMs")
        vms_by_round = self._create_vms(
            rounds=rounds,
            seed=seed,
            rng=rng,
        )

        if state in {"round", "finished"}:
            activity_rounds = self._get_activity_rounds(
                rounds=rounds,
                state=state,
                in_round=in_round,
            )

            access_token_hashes_by_team = self._create_access_tokens(
                teams=teams,
                seed=seed,
                rng=rng,
            )

            self._create_game_activity(
                teams=teams,
                users=users,
                rounds=activity_rounds,
                vms_by_round=vms_by_round,
                access_token_hashes_by_team=access_token_hashes_by_team,
                seed=seed,
                rng=rng,
                now=now,
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Initialized competition successfully "
                f"(state={state!r}, seed={seed})"
            )
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate(
        self,
        *,
        n_teams,
        n_users,
        n_rounds,
        round_duration,
        wait_between_rounds,
        in_round,
        state,
    ):
        if n_teams < 1:
            raise CommandError("--n-teams must be at least 1.")

        if n_users < 1:
            raise CommandError("--n-users must be at least 1.")

        if n_rounds < 1:
            raise CommandError("--n-rounds must be at least 1.")

        if n_users < n_teams:
            raise CommandError("There must be at least as many users as teams.")

        if n_users > n_teams * 2:
            raise CommandError(
                "There can be at most two users per team, so "
                "--n-users cannot exceed --n-teams * 2."
            )

        if round_duration < 1:
            raise CommandError("--round-duration must be greater than 0.")

        if wait_between_rounds < 0:
            raise CommandError("--wait-between-rounds cannot be negative.")

        if state == "round":
            if in_round is None:
                raise CommandError("--in-round is required when --state='round'.")

            if not 1 <= in_round <= n_rounds:
                raise CommandError(f"--in-round must be between 1 and {n_rounds}.")

        elif in_round is not None:
            raise CommandError("--in-round can only be used with --state='round'.")

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------

    def _clear_database(self):
        WormDeploymentPlan.objects.all().delete()
        Upload.objects.all().delete()
        MalwareArtifact.objects.all().delete()

        FlagCapture.objects.all().delete()
        FlagCaptureAttempt.objects.all().delete()

        Logging.objects.all().delete()
        LoggingAttempt.objects.all().delete()

        AccessTokens.objects.all().delete()
        TeamMembership.objects.all().delete()
        Team.objects.all().delete()

        VirtualMachine.objects.all().delete()
        Round.objects.all().delete()

        CompetitionParticipant.objects.all().delete()
        ApplicationSetting.objects.all().delete()

        Competition.objects.all().delete()
        User.objects.all().delete()

    # ------------------------------------------------------------------
    # Deterministic helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _hash(seed, *parts):
        value = ":".join(
            [
                str(seed),
                *(str(part) for part in parts),
            ]
        )

        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @classmethod
    def _uuid(cls, seed, *parts):
        return uuid.uuid5(
            uuid.NAMESPACE_URL,
            cls._hash(seed, *parts),
        )

    @classmethod
    def _token_hash(cls, seed, *parts):
        return cls._hash(
            seed,
            "token",
            *parts,
        )

    # ------------------------------------------------------------------
    # Competition
    # ------------------------------------------------------------------

    def _create_competition(
        self,
        *,
        state,
        now,
        n_rounds,
        round_duration,
        wait_between_rounds,
        rng,
    ):
        total_duration = (
            n_rounds * round_duration + max(0, n_rounds - 1) * wait_between_rounds
        )

        if state == "registration started":
            registration_starts_at = now - timedelta(
                seconds=rng.randint(
                    300,
                    7200,
                )
            )

            registration_ends_at = now + timedelta(
                seconds=rng.randint(
                    1800,
                    7200,
                )
            )

        elif state == "registration ended":
            first_round_start = now + timedelta(
                seconds=rng.randint(
                    60,
                    300,
                )
            )

            registration_ends_at = first_round_start - timedelta(
                seconds=rng.randint(
                    30,
                    120,
                )
            )

            registration_starts_at = registration_ends_at - timedelta(
                seconds=rng.randint(
                    3600,
                    86400,
                )
            )

        elif state == "round":
            registration_starts_at = now - timedelta(
                seconds=(
                    total_duration
                    + rng.randint(
                        3600,
                        86400,
                    )
                )
            )

            registration_ends_at = registration_starts_at + timedelta(
                seconds=rng.randint(
                    1800,
                    7200,
                )
            )

        else:
            registration_starts_at = now - timedelta(
                seconds=(
                    total_duration
                    + rng.randint(
                        86400,
                        172800,
                    )
                )
            )

            registration_ends_at = registration_starts_at + timedelta(
                seconds=rng.randint(
                    1800,
                    7200,
                )
            )

        return Competition.objects.create(
            name="Cyber Defense Competition",
            created_at=registration_starts_at,
            registration_starts_at=registration_starts_at,
            registration_ends_at=registration_ends_at,
        )

    # ------------------------------------------------------------------
    # Rounds
    # ------------------------------------------------------------------

    def _create_rounds(
        self,
        *,
        competition,
        state,
        now,
        n_rounds,
        round_duration,
        wait_between_rounds,
        in_round,
        rng,
    ):
        spacing = round_duration + wait_between_rounds

        if state == "registration started":
            first_round_start = now + timedelta(
                seconds=rng.randint(
                    3600,
                    7200,
                )
            )

        elif state == "registration ended":
            first_round_start = now + timedelta(
                seconds=rng.randint(
                    60,
                    300,
                )
            )

        elif state == "round":
            offset = (in_round - 1) * spacing

            elapsed = rng.randint(
                1,
                max(
                    1,
                    round_duration - 1,
                ),
            )

            first_round_start = (
                now - timedelta(seconds=offset) - timedelta(seconds=elapsed)
            )

        else:
            time_since_last_round = rng.randint(
                300,
                3600,
            )

            first_round_start = (
                now
                - timedelta(seconds=time_since_last_round)
                - timedelta(seconds=round_duration)
                - timedelta(seconds=(n_rounds - 1) * spacing)
            )

        rounds = []

        for index in range(n_rounds):
            round_start = first_round_start + timedelta(seconds=index * spacing)
            round_end = round_start + timedelta(seconds=round_duration)

            name = f"Round {index + 1}"

            round_obj = Round.objects.create(
                seed=rng.randint(
                    0,
                    2**63 - 1,
                ),
                round_start=round_start,
                round_end=round_end,
                name=name,
                competition=competition,
                created_at=competition.created_at,
            )

            print(
                f"Generated round ({name}): ",
                round_start,
                round_end,
                round_start <= now < round_end,
            )

            rounds.append(round_obj)

        return rounds

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    def _create_users(
        self,
        *,
        competition,
        n_users,
        seed,
        rng,
    ):
        first_names = [
            "Alex",
            "Sam",
            "Jordan",
            "Taylor",
            "Morgan",
            "Casey",
            "Riley",
            "Jamie",
            "Chris",
            "Robin",
            "Drew",
            "Avery",
            "Cameron",
            "Quinn",
            "Charlie",
            "Max",
            "Nico",
            "Maya",
            "Lena",
            "Jonas",
        ]

        last_names = [
            "Miller",
            "Smith",
            "Keller",
            "Fischer",
            "Brown",
            "Wilson",
            "Martin",
            "Schmidt",
            "Anderson",
            "Clark",
            "Walker",
            "Lewis",
            "Young",
            "Hall",
            "Wright",
            "Baker",
            "Murphy",
            "Taylor",
            "Evans",
            "Thomas",
        ]

        users = []

        for index in range(n_users):
            username = f"user{index + 1:04d}"

            user = User.objects.create_user(
                username=username,
                password="password",
                email=f"{username}@example.test",
                first_name=rng.choice(first_names),
                last_name=rng.choice(last_names),
            )

            user.uuid = self._uuid(
                seed,
                "user",
                index,
            )

            user.save(update_fields=["uuid"])

            CompetitionParticipant.objects.create(
                competition=competition,
                user=user,
            )

            users.append(user)

        root = User.objects.create_superuser(
            username="root",
            password="root",
            email="root@example.test",
        )

        root.uuid = self._uuid(
            seed,
            "root",
        )

        root.save(update_fields=["uuid"])

        return users

    # ------------------------------------------------------------------
    # Teams
    # ------------------------------------------------------------------

    def _create_teams(
        self,
        *,
        competition,
        users,
        n_teams,
        state,
        seed,
        rng,
    ):
        adjectives = [
            "Binary",
            "Null",
            "Kernel",
            "Packet",
            "Root",
            "Shadow",
            "Cipher",
            "Buffer",
            "Stack",
            "Daemon",
            "Socket",
            "Hex",
            "Vector",
            "Signal",
            "Bit",
            "Opcode",
        ]

        nouns = [
            "Foxes",
            "Ravens",
            "Wolves",
            "Owls",
            "Lynx",
            "Badgers",
            "Dragons",
            "Wasps",
            "Sharks",
            "Vipers",
            "Hawks",
            "Falcons",
        ]

        # There are only 16 * 12 = 192 possible adjective/noun
        # combinations. For larger competitions, the numeric suffix
        # guarantees uniqueness.
        combinations = [(adjective, noun) for adjective in adjectives for noun in nouns]

        rng.shuffle(combinations)

        teams = []

        for index in range(n_teams):
            adjective, noun = combinations[index % len(combinations)]

            cycle = index // len(combinations)

            if cycle == 0:
                team_name = f"{adjective} {noun}"
            else:
                team_name = f"{adjective} {noun} {cycle + 1}"

            team = Team.objects.create(
                uuid=self._uuid(
                    seed,
                    "team",
                    index,
                ),
                name=team_name,
                competition=competition,
                max_members=2,
            )

            # Stable latent strength. It controls behavior throughout
            # the competition without modifying the database schema.
            team._strength = rng.betavariate(
                2.0,
                2.0,
            )

            teams.append(team)

        if state in {
            "registration ended",
            "round",
            "finished",
        }:
            self._assign_users_to_teams(
                users=users,
                teams=teams,
                rng=rng,
            )

        return teams

    def _assign_users_to_teams(
        self,
        *,
        users,
        teams,
        rng,
    ):
        shuffled_users = list(users)
        rng.shuffle(shuffled_users)

        # Guarantee every team gets a member.
        for index, team in enumerate(teams):
            TeamMembership.objects.create(
                user=shuffled_users[index],
                team=team,
            )

        remaining_users = shuffled_users[len(teams) :]

        rng.shuffle(remaining_users)

        for user in remaining_users:
            eligible = [
                team for team in teams if team.memberships.count() < team.max_members
            ]

            if not eligible:
                raise CommandError("Could not assign all users to teams.")

            team = rng.choice(eligible)

            TeamMembership.objects.create(
                user=user,
                team=team,
            )

    # ------------------------------------------------------------------
    # Virtual machines
    # ------------------------------------------------------------------

    def _get_vm_rounds(
        self,
        *,
        rounds,
        state,
        in_round,
    ):
        if state == "registration started":
            return []

        if state == "registration ended":
            return rounds[:1]

        if state == "round":
            return rounds[:in_round]

        return rounds

    def _create_vms(
        self,
        *,
        rounds,
        seed,
        rng,
    ):
        services = [
            "web",
            "api",
            "db",
            "mail",
            "vpn",
            "git",
            "dns",
        ]

        vms_by_round = {}

        for round_index, round_obj in enumerate(
            rounds,
            start=1,
        ):
            vms = []

            used_identifiers = set()

            for vm_index in range(VM_COUNT):
                service = rng.choice(services)

                identifier = f"r{round_index}-" f"{service}-" f"{vm_index + 1}"

                while identifier in used_identifiers:
                    service = rng.choice(services)

                    identifier = f"r{round_index}-" f"{service}-" f"{vm_index + 1}"

                used_identifiers.add(identifier)

                third_octet = 10 + ((round_index * 7 + vm_index) % 200)

                fourth_octet = rng.randint(
                    2,
                    254,
                )

                vm = VirtualMachine.objects.create(
                    vm_identifier=identifier,
                    vm_maintenance_ip=(
                        f"10.{round_index}." f"{third_octet}." f"{fourth_octet}"
                    ),
                    ctf_flag_hash=self._hash(
                        seed,
                        "ctf-flag",
                        round_index,
                        vm_index,
                    ),
                    round=round_obj,
                )

                vms.append(vm)

            vms_by_round[round_obj.pk] = vms

        return vms_by_round

    # ------------------------------------------------------------------
    # Access tokens
    # ------------------------------------------------------------------

    def _create_access_tokens(
        self,
        *,
        teams,
        seed,
        rng,
    ):
        """
        Create access tokens and return the hashes of all active tokens
        grouped by team.

        The returned hashes are the exact values stored in
        AccessTokens.access_token_hash. Gameplay attempts use these
        values directly as their team_token_hash.

        This establishes the intended relationship:

            AccessTokens.access_token_hash
                    ==
            LoggingAttempt.team_token_hash

        and:

            AccessTokens.access_token_hash
                    ==
            FlagCaptureAttempt.team_token_hash
        """
        active_token_hashes_by_team = {}

        for team_index, team in enumerate(teams):
            token_count = rng.choices(
                [1, 2, 3],
                weights=[
                    70,
                    25,
                    5,
                ],
                k=1,
            )[0]

            active_hashes = []

            for token_index in range(token_count):
                # Guarantee at least one valid token per team.
                # The first token is always active.
                revoked = token_index != 0 and rng.random() < 0.08

                access_token_hash = self._token_hash(
                    seed,
                    "access",
                    team_index,
                    token_index,
                )

                AccessTokens.objects.create(
                    name=f"token-{token_index + 1}",
                    description=(
                        "Competition access token"
                        if not revoked
                        else "Revoked competition access token"
                    ),
                    access_token_hash=access_token_hash,
                    team=team,
                    revoked=revoked,
                    revoked_at=(clock.now() if revoked else None),
                )

                if not revoked:
                    active_hashes.append(access_token_hash)

            active_token_hashes_by_team[team.pk] = active_hashes

        return active_token_hashes_by_team

    # ------------------------------------------------------------------
    # Gameplay
    # ------------------------------------------------------------------

    def _get_activity_rounds(
        self,
        *,
        rounds,
        state,
        in_round,
    ):
        if state == "round":
            return rounds[:in_round]

        if state == "finished":
            return rounds

        return []

    def _create_game_activity(
        self,
        *,
        teams,
        users,
        rounds,
        vms_by_round,
        access_token_hashes_by_team,
        seed,
        rng,
        now,
    ):
        deployed_on_vms = set()

        for team in teams:
            strength = team._strength

            members = list(team.users.all())

            if not members:
                continue

            # Only use hashes belonging to non-revoked access tokens.
            #
            # These exact hashes are subsequently written into
            # LoggingAttempt.team_token_hash and
            # FlagCaptureAttempt.team_token_hash.
            access_token_hashes = access_token_hashes_by_team.get(
                team.pk,
                [],
            )

            if not access_token_hashes:
                continue

            deployed_vms = self._create_team_uploads(
                team=team,
                members=members,
                rounds=rounds,
                vms_by_round=vms_by_round,
                seed=seed,
                strength=strength,
                rng=rng,
                deployed_on_vms=deployed_on_vms,
            )

            for deployed_vm in deployed_vms:
                deployed_on_vms.add(deployed_vm)

            for round_obj in rounds:
                vms = vms_by_round.get(
                    round_obj.pk,
                    [],
                )

                if not vms:
                    continue

                self._create_logs_for_team_round(
                    team=team,
                    round_obj=round_obj,
                    strength=strength,
                    access_token_hashes=access_token_hashes,
                    seed=seed,
                    rng=rng,
                    now=now,
                )

                self._create_captures_for_team_round(
                    team=team,
                    round_obj=round_obj,
                    vms=vms,
                    strength=strength,
                    access_token_hashes=access_token_hashes,
                    seed=seed,
                    rng=rng,
                    now=now,
                )

    # ------------------------------------------------------------------
    # Uploads / worm deployments
    # ------------------------------------------------------------------

    def _create_team_uploads(
        self,
        *,
        team,
        members,
        rounds,
        vms_by_round,
        seed,
        strength,
        rng,
        deployed_on_vms,
    ):
        expected = 0.5 + 3.0 * strength

        upload_count = self._random_count(
            rng=rng,
            expected=expected,
            maximum=5,
        )

        deployed_rounds = set()
        new_deployment_vms = set()

        for upload_index in range(upload_count):
            content_hash = self._hash(
                seed,
                "artifact",
                team.pk,
                upload_index,
            )

            artifact = MalwareArtifact.objects.create(
                content_hash=content_hash,
                storage_key=(f"malware/" f"{content_hash[:2]}/" f"{content_hash}"),
            )

            user = rng.choice(members)

            upload = Upload.objects.create(
                artifact=artifact,
                user=user,
            )

            if not rounds:
                continue

            deployment_probability = 0.10 + 0.65 * strength

            if rng.random() >= deployment_probability:
                continue

            round_obj = rng.choice(rounds)

            if round_obj.pk in deployed_rounds:
                continue

            vms = vms_by_round.get(round_obj.pk)
            if vms is None:
                continue

            available_vms = list(
                set(vms) - set(deployed_on_vms) - set(new_deployment_vms)
            )

            vm = rng.choice(available_vms)

            worm_deployment_plan = WormDeploymentPlan.objects.create(
                team=team, upload=upload, virtual_machine=vm, round=vm.round
            )
            WormDeployment.objects.create(worm_deployment_plan=worm_deployment_plan)

            deployed_rounds.add(round_obj.pk)

        return new_deployment_vms

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _create_logs_for_team_round(
        self,
        *,
        team,
        round_obj,
        strength,
        access_token_hashes,
        seed,
        rng,
        now,
    ):
        expected = 1.0 + 5.0 * strength

        count = self._random_count(
            rng=rng,
            expected=expected,
            maximum=200,
        )

        events = [
            "ssh_login",
            "http_request",
            "dns_query",
            "process_start",
            "connection",
        ]

        for index in range(count):
            processed = True

            created_at = round_obj.round_start + timedelta(
                seconds=self._random_activity_offset(
                    rng=rng,
                    round_obj=round_obj,
                    now=now,
                )
            )

            log_data = {
                "event": rng.choice(events),
                "success": (rng.random() < (0.55 + 0.25 * strength)),
            }

            # IMPORTANT:
            # This hash MUST be one of the team's actual,
            # non-revoked AccessTokens.access_token_hash values.
            team_token_hash = rng.choice(access_token_hashes)

            attempt = LoggingAttempt.objects.create(
                uuid=self._uuid(
                    seed,
                    "logging-attempt",
                    team.pk,
                    round_obj.pk,
                    index,
                ),
                team_token_hash=team_token_hash,
                log_data=log_data,
                processed=processed,
                processed_at=(created_at if processed else None),
                created_at=created_at,
            )

            if processed:
                Logging.objects.create(
                    team=team,
                    round=round_obj,
                    logging_attempt=attempt,
                    created_at=created_at,
                )

    # ------------------------------------------------------------------
    # Flag captures
    # ------------------------------------------------------------------

    def _create_captures_for_team_round(
        self,
        *,
        team,
        round_obj,
        vms,
        strength,
        access_token_hashes,
        seed,
        rng,
        now,
    ):
        expected = 5 + 15 * strength

        count = self._random_count(
            rng=rng,
            expected=expected,
            maximum=VM_COUNT,
        )

        for index in range(count):
            vm = rng.choice(vms)

            processed = True

            created_at = round_obj.round_start + timedelta(
                seconds=self._random_activity_offset(
                    rng=rng,
                    round_obj=round_obj,
                    now=now,
                )
            )

            # IMPORTANT:
            # This hash MUST be one of the team's actual,
            # non-revoked AccessTokens.access_token_hash values.
            team_token_hash = rng.choice(access_token_hashes)

            attempt = FlagCaptureAttempt.objects.create(
                team_token_hash=team_token_hash,
                vm_flag_hash=vm.ctf_flag_hash,
                processed=processed,
                processed_at=(created_at if processed else None),
                created_at=created_at,
            )

            if not processed:
                continue

            success_probability = 0.15 + 0.70 * strength

            if rng.random() >= success_probability:
                continue

            FlagCapture.objects.create(
                team=team,
                round=round_obj,
                vm=vm,
                flag_capture_attempt=attempt,
                created_at=created_at,
            )

    # ------------------------------------------------------------------
    # Randomization
    # ------------------------------------------------------------------

    @staticmethod
    def _random_count(
        *,
        rng,
        expected,
        maximum,
    ):
        if maximum <= 0:
            return 0

        probability = max(
            0.0,
            min(
                1.0,
                expected / maximum,
            ),
        )

        count = 0

        for _ in range(maximum):
            if rng.random() < probability:
                count += 1

        return count

    @staticmethod
    def _random_activity_offset(
        *,
        rng,
        round_obj,
        now,
    ):
        duration = int((round_obj.round_end - round_obj.round_start).total_seconds())

        if duration <= 1:
            return 0

        # Completed round: activity can happen anywhere in it.
        if now >= round_obj.round_end:
            return rng.randint(
                0,
                duration - 1,
            )

        # Current round: activity cannot occur in the future.
        elapsed = int((now - round_obj.round_start).total_seconds())

        if elapsed <= 0:
            return 0

        return rng.randint(
            0,
            min(
                elapsed,
                duration - 1,
            ),
        )
