from functools import wraps

from core.models.competition import Competition
from django.http import HttpRequest, HttpResponse


def require_competition(view_func):
    @wraps(view_func)
    def wrapper(request: HttpRequest, *args, **kwargs):
        if request.competition is None:
            return HttpResponse("No competition selected", status=500)

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
            return HttpResponse("Registration closed", status=400)

        return view_func(request, *args, competition=competition, **kwargs)

    return wrapper


def require_competition_registration_inactive(view_func):
    @require_competition
    @wraps(view_func)
    def wrapper(request: HttpRequest, competition: Competition, *args, **kwargs):
        if competition.is_registration_period():
            return HttpResponse("Registration still open", status=400)

        return view_func(request, *args, competition=competition, **kwargs)

    return wrapper


def user_in_team_required(view_func):
    @wraps(view_func)
    def wrapper(request: HttpRequest, *args, **kwargs):
        if request.team is None:
            return HttpResponse("User not assigned to team yet", status=400)

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
            return HttpResponse("User assigned to team already", status=400)

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
