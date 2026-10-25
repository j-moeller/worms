import hashlib
import secrets
from typing import Optional

from core.models.competition import Competition
from core.models.team import AccessTokens, Team
from dashboard.views.decorators import (
    require_competition_registration_inactive,
    user_in_team_optional,
    user_in_team_required,
)
from dashboard.views.util import render_with_base_info
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST


def hash_access_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@login_required
@user_in_team_optional
def access(request: HttpRequest, team: Optional[Team]):
    if team is None or team.competition.is_registration_period():
        show_access_tokens = False
    else:
        show_access_tokens = True

    if show_access_tokens:
        access_tokens = AccessTokens.get_team_access_codes(team)
    else:
        access_tokens = []

    created_access_token = request.session.pop(
        "created_access_token",
        None,
    )

    context = {
        "access_tokens": access_tokens,
        "show_access_tokens": show_access_tokens,
        "created_access_token": created_access_token,
    }

    return render_with_base_info(request, "access.html", context)


@login_required
@require_POST
@user_in_team_required
@require_competition_registration_inactive
def create_access_token(request: HttpRequest, team: Team, *, competition: Competition):
    name = request.POST.get("name", "")
    description = request.POST.get("description", "")

    if not name.strip():
        messages.error(request, "Give the token a name.")
        return redirect("access")

    # Generate the raw token. It is never stored in the database.
    raw_token = secrets.token_urlsafe(32)
    token_hash = hash_access_token(raw_token)

    AccessTokens.objects.create(
        name=name,
        description=description,
        access_token_hash=token_hash,
        team=team,
        created_by=request.user,
    )

    # Store the raw token only temporarily so it can be displayed once.
    request.session["created_access_token"] = raw_token

    return redirect("access")


@login_required
@require_POST
@user_in_team_required
@require_competition_registration_inactive
def revoke_access_token(
    request: HttpRequest, team: Team, token_id: int, *, competition: Competition
):
    access_token = get_object_or_404(
        AccessTokens,
        id=token_id,
        team=team,
    )

    if not access_token.revoked:
        access_token.revoked = True
        access_token.revoked_at = request.time_now
        access_token.save(update_fields=["revoked", "revoked_at"])
        messages.success(request, f"Access token “{access_token.name}” revoked.")

    return redirect("access")
