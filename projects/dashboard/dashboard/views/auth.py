from dashboard.views.navigation import safe_next
from dashboard.views.util import render_with_base_info
from django.contrib.auth import authenticate, login, logout
from django.http import HttpRequest
from django.shortcuts import redirect
from django.views.decorators.http import require_POST


def _next_or_profile(request: HttpRequest):
    """Back to the page that sent the user to the login (?next=, set by
    login_required), if it is a path on this site; else the profile."""
    return safe_next(request, default="profile")


def login_view(request: HttpRequest):
    if request.user.is_authenticated:
        return _next_or_profile(request)

    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")

        user = authenticate(
            request,
            username=username,
            password=password,
        )

        if user is not None:
            login(request, user)
            return _next_or_profile(request)

        return render_with_base_info(
            request,
            "login.html",
            {
                "error": "Invalid username or password.",
                "username": username,
            },
            status=401,
        )

    return render_with_base_info(request, "login.html")


@require_POST
def logout_view(request):
    """POST only: a GET (a link, an <img>, a crawler) must not end a session."""
    logout(request)
    return redirect("login")
