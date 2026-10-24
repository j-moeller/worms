from dashboard.views.util import render_with_base_info
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme


def _next_or_profile(request: HttpRequest):
    """Back to the page that sent the user to the login (?next=, set by
    login_required), if it is a path on this site; else the profile."""
    target = request.POST.get("next") or request.GET.get("next") or ""
    if target and url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(target)
    return redirect("profile")


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
            },
            status=401,
        )

    return render_with_base_info(request, "login.html")


@login_required
def logout_view(request):
    logout(request)
    return redirect("index")
