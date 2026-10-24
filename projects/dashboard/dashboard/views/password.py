"""First-login password change for student accounts.

Student accounts are created with a one-time password and
`must_change_password = True` (core/management/commands/create_users.py). The
ForcePasswordChangeMiddleware (dashboard/middleware.py) redirects such a user
to this view for everything until they set their own password; on success the
flag is cleared and the session is kept alive.
"""

from dashboard.views.util import render_with_base_info
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import SetPasswordForm
from django.http import HttpRequest
from django.shortcuts import redirect


@login_required
def change_password(request: HttpRequest):
    user = request.user

    if request.method == "POST":
        form = SetPasswordForm(user, request.POST)
        if form.is_valid():
            form.save()
            # Keep the user logged in after the password hash changes.
            update_session_auth_hash(request, form.user)
            if user.must_change_password:
                user.must_change_password = False
                user.save(update_fields=["must_change_password"])
            return redirect("profile")
    else:
        form = SetPasswordForm(user)

    return render_with_base_info(
        request,
        "change_password.html",
        {
            "form": form,
            "forced": user.must_change_password,
        },
    )
