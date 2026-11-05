from core.models.devbox import DevboxSSHKey
from core.models.team import AccessTokens
from dashboard.views.util import render_with_base_info
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest


@login_required
def profile(request: HttpRequest):
    """The user's account and their team. The former Access page is folded in
    here as the team's "Access tokens" section (reachable from the team name in
    the header), and the team's devbox SSH keys live here too."""
    team = request.team
    show_access_tokens = team is not None and not team.competition.is_registration_period()
    context = {
        "ssh_keys": list(DevboxSSHKey.objects.filter(team=team).order_by("-created_at")) if team is not None else [],
        "show_access_tokens": show_access_tokens,
        "access_tokens": AccessTokens.get_team_access_codes(team) if show_access_tokens else [],
        # Shown once, right after it is created (create_access_token stores it).
        "created_access_token": request.session.pop("created_access_token", None),
    }
    return render_with_base_info(request, "profile.html", context)
