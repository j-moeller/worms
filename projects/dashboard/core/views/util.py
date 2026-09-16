from django.conf import settings
from django.http import HttpRequest
from django.shortcuts import render

from core.models.user import User
from core.views.decorators import get_current_competition, get_users_current_team


def render_with_base_info(
    request: HttpRequest, template, context=None, *args, **kwargs
):
    if context is None:
        context = {}

    user = request.user
    if not user.is_authenticated:
        return render(request, template, context, *args, **kwargs)

    competition = get_current_competition()
    if user is None or competition is None:
        team = None
    else:
        team = get_users_current_team(user, competition)

    context["user"] = user
    context["user_team"] = team

    context["debug"] = settings.DEBUG

    if settings.DEBUG:
        context["available_users"] = User.objects.order_by("username")

    competition = get_current_competition()
    if competition is None:
        context["is_registration_period"] = False
    else:
        context["is_registration_period"] = competition.registration_is_open

    return render(request, template, context, *args, **kwargs)
