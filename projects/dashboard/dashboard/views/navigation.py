"""Small POST endpoints for page-wide settings, and the safe "back to where you
were" redirect they (and the login) share.

Both settings are plain form posts, so they work without JavaScript; the
static enhance.js only makes them faster (the theme switches instantly).
"""

from __future__ import annotations

from dashboard import themes
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST


def safe_next(request: HttpRequest, default: str = "index"):
    """A redirect to ?next= / POST next when it is a path on this site, else
    to `default` (a URL name)."""
    target = request.POST.get("next") or request.GET.get("next") or ""
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(target)
    return redirect(default)


@require_POST
def set_theme(request: HttpRequest):
    """Remember the chosen design in a cookie (no session: anonymous visitors
    can switch too, without a database write)."""
    response = safe_next(request)
    key = request.POST.get("theme", "")
    if key in themes.THEMES:
        response.set_cookie(
            themes.COOKIE, key, max_age=themes.COOKIE_MAX_AGE,
            secure=request.is_secure(), httponly=True, samesite="Lax",
        )  # fmt: skip
    return response


@login_required
@require_POST
def set_environment(request: HttpRequest):
    """Switch between the production (arena) and development (devbox) view."""
    request.session["is_dev_environment"] = request.POST.get("environment") == "dev"
    return redirect("index")
