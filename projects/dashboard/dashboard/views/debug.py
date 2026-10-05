from core.models.user import User
from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST


@login_required
@require_POST
def debug_login_as(request: HttpRequest, uuid):
    if not settings.DEBUG:
        return JsonResponse(
            {"error": "not available"},
            status=404,
        )

    user = get_object_or_404(User, uuid=uuid)

    login(request, user)

    next_url = request.POST.get("next")

    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)

    return redirect("profile")
