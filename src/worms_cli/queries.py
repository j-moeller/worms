"""Database-backed queries and mutations for the CLI, on top of the dashboard's
own models and scoring (imported after django_setup.setup()). The database is
the single source of truth; nothing here reimplements schema or scoring.
"""

from __future__ import annotations


# -- status ------------------------------------------------------------------


def competition_overview() -> dict:
    """Current competition, its rounds, the active/next round and team count."""
    from core import clock
    from core.models.competition import ApplicationSetting, Round
    from core.models.team import Team

    now = clock.now()
    competition = ApplicationSetting.get_current_competition()
    if competition is None:
        return {"competition": None}

    rounds = list(Round.objects.filter(competition=competition).order_by("round_start"))
    active = next((r for r in rounds if r.is_active(now)), None)
    upcoming = next((r for r in rounds if r.round_start and r.round_start > now), None)
    # is_registration_period() assumes both dates are set; a freshly created
    # default competition has none (no registration window yet).
    registration = (
        competition.registration_starts_at is not None
        and competition.registration_ends_at is not None
        and competition.is_registration_period()
    )
    return {
        "competition": competition,
        "now": now,
        "rounds": rounds,
        "n_rounds": len(rounds),
        "active_round": active,
        "next_round": upcoming,
        "n_teams": Team.objects.filter(competition=competition).count(),
        "registration": registration,
    }


def run_overview() -> dict:
    """Round-orchestration run states (docs/05-live-environment.md), if any."""
    from core.models.rounds import HostJob, RoundRun

    by_state: dict[str, int] = {}
    for run in RoundRun.objects.values_list("state", flat=True):
        by_state[run] = by_state.get(run, 0) + 1
    active = (
        RoundRun.objects.exclude(
            state__in=[RoundRun.State.FINISHED, RoundRun.State.FAILED, RoundRun.State.SKIPPED, RoundRun.State.PENDING]
        )
        .select_related("round", "team")
        .first()
    )
    return {
        "by_state": by_state,
        "active": active,
        "queued_jobs": HostJob.objects.filter(state=HostJob.State.QUEUED).count(),
    }


# -- teams --------------------------------------------------------------------


def team_rows() -> list[dict]:
    """Every team of the current competition with membership and the members'
    login status (have they ever logged in / changed their one-time password)."""
    from core.models.competition import ApplicationSetting
    from core.models.team import Team

    competition = ApplicationSetting.get_current_competition()
    if competition is None:
        return []

    rows = []
    teams = (
        Team.objects.filter(competition=competition)
        .prefetch_related("users")
        .order_by("name")
    )
    for team in teams:
        members = list(team.users.all())
        rows.append(
            {
                "name": team.name,
                "uuid": str(team.uuid),
                "n_members": len(members),
                "max_members": team.max_members,
                "members": [
                    {
                        "username": u.username,
                        "logged_in": u.last_login is not None,
                        "must_change_password": u.must_change_password,
                    }
                    for u in members
                ],
                "devbox_version": getattr(team, "devbox_version", None),
            }
        )
    return rows


def user_rows() -> list[dict]:
    """Every user with team assignment and login status."""
    from core.models.competition import ApplicationSetting
    from core.models.team import TeamMembership
    from core.models.user import User

    competition = ApplicationSetting.get_current_competition()
    team_of = {}
    if competition is not None:
        for membership in TeamMembership.objects.filter(
            team__competition=competition
        ).select_related("team"):
            team_of[membership.user_id] = membership.team.name

    rows = []
    for user in User.objects.order_by("username"):
        rows.append(
            {
                "username": user.username,
                "is_staff": user.is_staff,
                "logged_in": user.last_login is not None,
                "must_change_password": user.must_change_password,
                "team": team_of.get(user.id),
            }
        )
    return rows


def delete_team(name: str) -> str:
    """Delete a team. Memberships are removed (cascade); the user accounts
    themselves are untouched."""
    competition = _require_competition()
    team = _get_team(competition, name)
    n_members = team.users.count()
    team.delete()
    return f"deleted team {name!r} ({n_members} membership(s) removed; users kept)"


def _require_competition():
    from core.models.competition import ApplicationSetting

    competition = ApplicationSetting.get_current_competition()
    if competition is None:
        raise ValueError("no current competition (run `worms status` / `manage.py init`)")
    return competition


def _get_team(competition, name):
    from core.models.team import Team

    team = Team.objects.filter(competition=competition, name=name).first()
    if team is None:
        raise ValueError(f"no team named {name!r} in {competition.name!r}")
    return team


# -- leaderboard --------------------------------------------------------------


def leaderboard() -> dict:
    """The production leaderboard, scored with the dashboard's own functions."""
    from core import clock
    from core.models.competition import ApplicationSetting, Round
    from core.models.scoring import FlagCapture
    from core.models.team import Team
    from dashboard.views.leaderboard import add_places, add_round_winners, score_teams
    from django.db.models import Count

    competition = ApplicationSetting.get_current_competition()
    if competition is None:
        return {"competition": None, "teams": []}

    now = clock.now()
    teams = list(
        Team.objects.filter(competition=competition)
        .annotate(flag_capture_count=Count("flag_captures"))
        .order_by("name")
    )
    rounds = list(Round.objects.filter(competition=competition).order_by("round_start"))
    captures = list(
        FlagCapture.objects.filter(round__competition=competition)
        .select_related("team", "flag_capture_attempt", "round")
        .order_by("flag_capture_attempt__created_at")
    )
    current_round = next((r for r in rounds if r.is_active(now)), None)

    score_teams(teams=teams, rounds=rounds, captures=captures, current_round=current_round)
    teams.sort(key=lambda t: (-t.total_points, t.name))
    add_places(teams)
    add_round_winners(teams, rounds, captures)

    return {"competition": competition, "current_round": current_round, "teams": teams}


def event_rows(hours: int = 24, kind: str | None = None, limit: int = 200) -> list[list[str]]:
    """The event tables (core/models/events.py) as one timeline, newest first, for `worms
    audit`. Each row is named `<table>.<kind>` (login.ok, password.reset, ...). An SSH
    gateway login is matched by its key's fingerprint to who registered the key."""
    import json
    from datetime import timedelta

    from core import clock
    from core.models import events as m

    since = clock.now() - timedelta(hours=max(1, hours))
    limit = max(1, min(limit, 5000))
    from core.events import ssh_fingerprint
    from core.models.devbox import DevboxSSHKey

    # Who registered each key: the keys registered now, then what credential_events
    # recorded (it also knows keys removed since).
    keys = {}
    for key in DevboxSSHKey.objects.select_related("created_by", "team"):
        fp = ssh_fingerprint(key.public_key)
        if fp:
            keys[fp] = (key.created_by.username if key.created_by else None, key.team.name)
    for added in m.CredentialEvent.objects.filter(kind="ssh_key_added").order_by("created_at"):
        if added.ssh_fingerprint:
            keys[added.ssh_fingerprint] = (added.actor_username, added.team_name)

    def row(e, name, actor="", target="", team="", details=None, at=None):
        at = at or e.created_at
        details = json.dumps(details or {}, ensure_ascii=False, sort_keys=True, default=str) if details else ""
        return (at, [
            at.strftime("%Y-%m-%d %H:%M:%S"), name, e.written_by, actor or "", target or "", team or "",
            getattr(e, "client_ip", None) or "", details if len(details) <= 100 else details[:97] + "...",
        ])  # fmt: skip

    def recent(model):
        return model.objects.filter(created_at__gte=since).order_by("-created_at", "-id")[:limit]

    rows = []
    for e in recent(m.LoginEvent):
        extra = {"not_recorded_before": e.not_recorded_before} if e.not_recorded_before else None
        rows.append(row(e, f"login.{e.outcome}", actor=e.username, details=extra))
    for e in recent(m.PasswordEvent):
        extra = {"one_time_password_replaced": e.one_time_password_replaced} if e.one_time_password_replaced is not None else None
        rows.append(row(e, f"password.{e.kind}", e.actor_username, f"{e.target_username} ({e.target_role})", details=extra))
    for e in recent(m.ImpersonationEvent):
        rows.append(row(e, f"view_as.{e.kind}", e.actor_username, e.target_username))
    for e in recent(m.AccountEvent):
        rows.append(row(e, f"account.{e.kind}", e.actor_username, e.target_username, details=e.detail))
    for e in recent(m.TeamEvent):
        rows.append(row(e, f"team.{e.kind}", e.actor_username, e.target_username, e.team_name))
    for e in recent(m.CredentialEvent):
        extra = {"name": e.name, **({"fingerprint": e.ssh_fingerprint} if e.ssh_fingerprint else {})}
        rows.append(row(e, f"credential.{e.kind}", e.actor_username, team=e.team_name, details=extra))
    for e in recent(m.CompetitionEvent):
        rows.append(row(e, f"competition.{e.kind}", e.actor_username, details={"round": e.round_id, **(e.detail or {})}))
    # A login is dated by the gateway's clock (the collector reads it up to a minute later).
    logins = m.SSHGatewayLogin.objects.filter(logged_in_at__gte=since).order_by("-logged_in_at", "-id")[:limit]
    for e in logins:
        owner, owner_team = keys.get(e.ssh_fingerprint, (None, None))
        rows.append(row(e, "ssh_gateway.login", f"key of {owner}" if owner else "unknown key", team=owner_team,
                        details={"fingerprint": e.ssh_fingerprint}, at=e.logged_in_at))  # fmt: skip
    for e in recent(m.PlatformEvent):
        rows.append(row(e, f"platform.{e.kind}", e.actor, details=e.detail))
    if kind:
        rows = [r for r in rows if r[1][1].startswith(kind)]
    rows.sort(key=lambda r: r[0], reverse=True)
    return [r[1] for r in rows[:limit]]


def team(name: str):
    """The current competition's team of that name (ValueError if there is none)."""
    return _get_team(_require_competition(), name)


# -- rounds, host jobs, devboxes, gameplans (`worms status <topic>`) -----------


def _when(ts) -> str:
    return ts.strftime("%Y-%m-%d %H:%M") if ts else "—"


def round_rows() -> list[list[str]]:
    """The current competition's rounds: window, gameplan and its check, attempts by state."""
    from collections import Counter

    from core.models.competition import Round
    from core.models.rounds import RoundRun

    rows = []
    for rnd in Round.objects.filter(competition=_require_competition()).order_by("round_start", "id"):
        states = Counter(RoundRun.objects.filter(round=rnd).values_list("state", flat=True))
        rows.append([
            rnd.name, _when(rnd.round_start), _when(rnd.round_end), rnd.gameplan or "(none yet)",
            rnd.gameplan_status or "—", f"{rnd.team_duration // 60} min",
            ", ".join(f"{state}: {n}" for state, n in sorted(states.items())) or "not planned",
        ])  # fmt: skip
    return rows


def job_rows(limit: int = 30) -> list[list[str]]:
    """The newest host jobs (the round agent's work), newest first."""
    from core.models.rounds import HostJob

    rows = []
    for job in HostJob.objects.select_related("run__team").order_by("-id")[:limit]:
        who = job.run.team.name if job.run else (job.params or {}).get("team_uuid", "")[:8]
        rows.append([str(job.id), job.kind, job.state, who, _when(job.requested_at), _when(job.finished_at),
                     (job.error or "").replace("\n", " ")[:80]])  # fmt: skip
    return rows


def devbox_rows() -> list[list[str]]:
    """Every team: its devbox (index, version, VMs) and its SSH keys (name, fingerprint)."""
    from core.models.devbox import Devbox, DevboxSSHKey
    from core.models.team import Team

    rows = []
    for t in Team.objects.filter(competition=_require_competition()).order_by("name"):
        box = Devbox.objects.filter(team=t).first()
        keys = [f"{k.name or '(no name)'} {k.key_fingerprint[:16]}" for k in DevboxSSHKey.objects.filter(team=t).order_by("id")]
        devbox = f"t{t.id}: index {box.index}, version {t.devbox_version}, {box.vms.count()} VMs" if box else "none"
        rows.append([t.name, devbox, "; ".join(keys) or "no key"])
    return rows


def gameplan_rows(repo_root) -> list[list[str]]:
    """The gameplans: what the orchestrator and the node found (the `gameplans` table), and this
    checkout's own check of each file (src/gameplan, the dry run): why one is invalid."""
    import sys

    from core.models.rounds import GameplanFile

    sys.path.insert(0, str(repo_root / "src"))
    from gameplan import loader, runtime

    deployed = {g.filename: g for g in GameplanFile.objects.all()}
    names = sorted(set(deployed) | set(loader.available(repo_root / "gameplans")))
    rows = []
    for name in names:
        row = deployed.get(name)
        there = ("valid" if row.valid else "INVALID") + f" ({_when(row.checked_at)})" if row else "not deployed"
        try:
            problems = runtime.validate(loader.load(repo_root / "gameplans", name).cls) if name in loader.available(repo_root / "gameplans") else ["not in this checkout"]
        except Exception as exc:  # noqa: BLE001  (a file that does not load is a problem like any other)
            problems = [f"{type(exc).__name__}: {exc}"]
        rows.append([name, there, "ok" if not problems else "; ".join(problems)[:200]])
    return rows
