from functools import wraps

from core.models.competition import ApplicationSetting, Competition
from core.models.team import TeamMembership
from core.models.user import User
from django.http import HttpRequest, HttpResponse


def get_current_competition():
    try:
        settings = ApplicationSetting.objects.select_related("current_competition").get(
            key="current"
        )
    except ApplicationSetting.DoesNotExist:
        return None

    return settings.current_competition


def get_users_current_team(user: User, competition: Competition):
    try:
        team_membership = TeamMembership.objects.get(
            user=user, team__competition=competition
        )
    except TeamMembership.DoesNotExist:
        return None

    return team_membership.team


def require_competition(view_func):
    @wraps(view_func)
    def wrapper(request: HttpRequest, *args, **kwargs):
        competition = get_current_competition()

        if competition is None:
            return HttpResponse("No competition selected", status=500)

        return view_func(
            request,
            *args,
            competition=competition,
            **kwargs,
        )

    return wrapper


def require_competition_registration_active(view_func):
    @wraps(view_func)
    def wrapper(request: HttpRequest, competition: Competition, *args, **kwargs):
        if not competition.registration_is_open:
            return HttpResponse("Registration closed", status=400)

        return view_func(request, *args, competition=competition, **kwargs)

    return wrapper


def require_competition_registration_inactive(view_func):
    @wraps(view_func)
    def wrapper(request: HttpRequest, competition: Competition, *args, **kwargs):
        if competition.registration_is_open:
            return HttpResponse("Registration still open", status=400)

        return view_func(request, *args, competition=competition, **kwargs)

    return wrapper


def user_in_team_required(view_func):
    @require_competition
    @wraps(view_func)
    def wrapper(request: HttpRequest, competition: Competition, *args, **kwargs):
        team = get_users_current_team(request.user, competition)

        if team is None:
            return HttpResponse("User not assigned to team yet", status=400)

        return view_func(
            request,
            *args,
            team=team,
            competition=competition,
            **kwargs,
        )

    return wrapper


def user_in_team_optional(view_func):
    @wraps(view_func)
    def wrapper(request: HttpRequest, *args, **kwargs):
        competition = get_current_competition()
        if competition is None or not request.user.is_authenticated:
            team = None
        else:
            team = get_users_current_team(request.user, competition)

        return view_func(
            request,
            *args,
            team=team,
            **kwargs,
        )

    return wrapper
