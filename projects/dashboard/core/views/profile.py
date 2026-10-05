from core.views.util import render_with_base_info
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest


@login_required
def profile(request: HttpRequest):
    return render_with_base_info(request, "profile.html")
