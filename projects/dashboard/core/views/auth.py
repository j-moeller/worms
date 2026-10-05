from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest
from django.shortcuts import redirect

from django.shortcuts import redirect
from core.views.util import render_with_base_info


def login_view(request: HttpRequest):
    if request.user.is_authenticated:
        return redirect("profile")

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
            return redirect("profile")

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
