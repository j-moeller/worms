from functools import wraps

from core.models.competition import Competition
from django.http import HttpRequest


def refuse(request: HttpRequest, message: str, status: int):
    """A themed page explaining why the request was refused (same status
    codes as before; a page instead of bare text, so it reads like the rest of
    the site with or without JavaScript)."""
    from dashboard.views.util import render_with_base_info

    return render_with_base_info(request, "message.html", {"message": message}, status=status)


def require_competition(view_func):
    @wraps(view_func)
    def wrapper(request: HttpRequest, *args, **kwargs):
        if request.competition is None:
            return refuse(request, "No competition selected", 500)

        return view_func(
            request,
            *args,
            competition=request.competition,
            **kwargs,
        )

    return wrapper


def require_competition_registration_active(view_func):
    @require_competition
    @wraps(view_func)
    def wrapper(request: HttpRequest, competition: Competition, *args, **kwargs):
        if not competition.is_registration_period():
            return refuse(request, "Registration closed", 400)

        return view_func(request, *args, competition=competition, **kwargs)

    return wrapper


def require_competition_registration_inactive(view_func):
    @require_competition
    @wraps(view_func)
    def wrapper(request: HttpRequest, competition: Competition, *args, **kwargs):
        if competition.is_registration_period():
            return refuse(request, "Registration still open", 400)

        return view_func(request, *args, competition=competition, **kwargs)

    return wrapper


def user_in_team_required(view_func):
    @wraps(view_func)
    def wrapper(request: HttpRequest, *args, **kwargs):
        if request.team is None:
            return refuse(request, "User not assigned to team yet", 400)

        return view_func(
            request,
            *args,
            team=request.team,
            **kwargs,
        )

    return wrapper


def user_not_in_team_required(view_func):
    @wraps(view_func)
    def wrapper(request: HttpRequest, *args, **kwargs):
        if request.team is not None:
            return refuse(request, "User assigned to team already", 400)

        return view_func(
            request,
            *args,
            **kwargs,
        )

    return wrapper


def user_in_team_optional(view_func):
    @wraps(view_func)
    def wrapper(request: HttpRequest, *args, **kwargs):
        return view_func(
            request,
            *args,
            team=request.team,
            **kwargs,
        )

    return wrapper


def staff_required(view_func):
    """Staff pages (dashboard/views/staff*.py): signed-in staff only. Anyone
    else goes to the login (anonymous) or the home page (students), as the
    other staff views do."""
    from django.contrib.auth.decorators import login_required
    from django.shortcuts import redirect

    @login_required
    @wraps(view_func)
    def wrapper(request: HttpRequest, *args, **kwargs):
        if not request.user.is_staff:
            return redirect("index")
        return view_func(request, *args, **kwargs)

    return wrapper
