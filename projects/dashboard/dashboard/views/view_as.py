"""View as: the admin sees the dashboard as another user does, read only (docs/accounts.md).

Only these two views change the state, and only the real, active admin can start it. It does
not sign anyone in: the session stays the admin's, so last_login, the user's own sessions and
their password are untouched. dashboard/middleware.py, ViewAsMiddleware, applies it and
refuses every write while it lasts.
"""

from __future__ import annotations

from core.models.user import User
from dashboard.middleware import VIEW_AS_SESSION_KEY, is_admin, view_as_log
from dashboard.views.decorators import refuse
from django.http import Http404, HttpRequest
from django.shortcuts import redirect
from django.views.decorators.http import require_POST


@require_POST
def start(request: HttpRequest, uuid):
    # While viewing, the middleware has already refused this: request.user is not the admin.
    if request.view_as is not None or not is_admin(request.user):
        return refuse(request, "Only the admin can view the dashboard as another user.", 403)
    # Not the admin itself, and nobody who could not sign in anyway.
    target = User.objects.filter(uuid=uuid, is_active=True, is_superuser=False).first()
    if target is None:
        raise Http404("No such user to view as.")
    # A new session key for the new state, as Django does at a login.
    request.session.cycle_key()
    request.session[VIEW_AS_SESSION_KEY] = {"by": request.user.pk, "user": target.pk}
    view_as_log.warning("view-as started: admin=%r viewed=%r", request.user.username, target.username)
    return redirect("index")


@require_POST
def stop(request: HttpRequest):
    target = request.view_as
    if target is None:
        return redirect("index")
    del request.session[VIEW_AS_SESSION_KEY]
    request.session.cycle_key()
    view_as_log.warning("view-as stopped: admin=%r viewed=%r", request.real_user.username, target.username)
    return redirect("staff_user", uuid=target.uuid)
