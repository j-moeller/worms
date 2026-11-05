from core.models.devbox import DevboxSSHKey, DevboxVM
from core.models.team import AccessTokens, Team
from dashboard.views.devbox import captured_vm_ids
from dashboard.views.leaderboard import add_places, leaderboard_tabs, round_result, score_teams
from dashboard.views.util import get_current_round, get_flag_captures, get_rounds, get_teams, render_with_base_info
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest


def _dev_performance(team: Team) -> dict | None:
    """The devbox has no rounds, so only the overall count: tokens found at its version."""
    vms = list(DevboxVM.objects.filter(devbox__team=team).order_by("role", "vm_identifier"))
    if not vms:
        return None
    found = captured_vm_ids(team)
    return {
        "meta": f"version {team.devbox_version}",
        "value": len(found),
        "of": len(vms),
        "label": "tokens submitted at this version",
        "pips": [vm.id in found for vm in vms],
    }


def _own_row(result: dict, team: Team) -> dict:
    return next(row for row in result["rows"] if row["team"].id == team.id)


def _live_performance(request: HttpRequest, team: Team) -> dict | None:
    """The team's results overall, or in one started round (?round=<id>), in the leaderboard's
    own numbers. VM counts only: which VMs a team has not captured stays hidden."""
    competition, now = request.active_competition, request.time_now
    rounds = get_rounds(competition) if competition is not None else []
    started = [r for r in rounds if r.round_start is not None and r.round_start <= now]
    if not started:
        return None
    selected = next((r for r in started if str(r.id) == request.GET.get("round")), None)
    tabs = leaderboard_tabs(request, rounds, now, selected)
    tabs[0]["name"] = "Overall"

    if selected is not None:
        result = round_result(competition, selected, now)
        rows, label = [_own_row(result, team)], f"VMs captured in {selected.name}"
        place, entrants, points = rows[0]["place"], len(result["rows"]), rows[0]["points"]
    else:
        rows = [_own_row(round_result(competition, r, now), team) for r in started]
        label = f"VMs captured over {len(started)} round{'s' if len(started) != 1 else ''}"
        teams = get_teams(competition, request)
        score_teams(teams=teams, rounds=rounds, captures=get_flag_captures(competition), current_round=get_current_round(rounds, now))
        teams.sort(key=lambda t: (-t.total_points, t.name))
        add_places(teams)
        own = next(t for t in teams if t.id == team.id)
        place, entrants, points = own.place, len(teams), own.total_points

    captured = sum(row["captured"] for row in rows)
    size = sum(row["vms"] or 0 for row in rows)
    return {
        "tabs": tabs,
        "value": captured,
        "of": size,
        "label": label,
        "pips": [True] * captured + [False] * max(size - captured, 0),
        "facts": [{"name": "Place", "value": f"{place} of {entrants}"}, {"name": "Points", "value": points}],
    }


def _performance(request: HttpRequest, team: Team | None) -> dict | None:
    if team is None:
        return None
    return _dev_performance(team) if request.is_dev_environment else _live_performance(request, team)


@login_required
def profile(request: HttpRequest):
    """The user's account and their team. The former Access page is folded in
    here as the team's "Access tokens" section (reachable from the team name in
    the header), and the team's devbox SSH keys and a summary of its results live here too."""
    team = request.team
    show_access_tokens = team is not None and not team.competition.is_registration_period()
    context = {
        "performance": _performance(request, team),
        "ssh_keys": list(DevboxSSHKey.objects.filter(team=team).order_by("-created_at")) if team is not None else [],
        "show_access_tokens": show_access_tokens,
        "access_tokens": AccessTokens.get_team_access_codes(team) if show_access_tokens else [],
        # Shown once, right after it is created (create_access_token stores it).
        "created_access_token": request.session.pop("created_access_token", None),
    }
    return render_with_base_info(request, "profile.html", context)
