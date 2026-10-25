"""Staff: user accounts (dashboard/views/staff.py has the overview).

Staff create student accounts one by one or from a CSV (core/accounts.py),
reset a forgotten password to a new one-time password, deactivate an account,
and put a user into a team. A one-time password is shown once, in the
response to the request that made it (never stored, never cached).

Staff accounts are not made here: the dashboard's database role cannot create
or promote one (setup_db_permissions); `worms admin create-staff` on the
console does. The admin manages existing staff accounts (password,
deactivation); nobody manages the admin here (`worms admin reset-password`).
"""

from __future__ import annotations

from core import accounts
from core.models.team import TeamMembership
from core.models.user import User
from dashboard.forms import UserCreateForm, UserEditForm, UserImportForm, UserTeamForm
from dashboard.membership import remove_member
from dashboard.views.decorators import staff_required
from dashboard.views.util import render_with_base_info
from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404, HttpRequest
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

FILTERS = {
    "all": Q(),
    "students": Q(is_staff=False, is_superuser=False),
    "staff": Q(is_staff=True) | Q(is_superuser=True),
    "no-team": Q(is_staff=False, is_superuser=False, is_active=True),
    "one-time": Q(must_change_password=True),
    "inactive": Q(is_active=False),
}


def _team_of(competition):
    """{user id -> team} in this competition."""
    if competition is None:
        return {}
    return {
        m.user_id: m.team
        for m in TeamMembership.objects.filter(team__competition=competition).select_related("team")
    }


def _credentials(request, created, title, problems=()):
    """The page that shows freshly made one-time passwords, once."""
    response = render_with_base_info(request, "staff/credentials.html", {
        "title": title,
        "created": created,
        "csv": "email,password\n" + "".join(f"{c.user.username},{c.password}\n" for c in created),
        "problems": list(problems),
    })  # fmt: skip
    response["Cache-Control"] = "no-store"
    return response


@never_cache
@staff_required
def users(request: HttpRequest):
    competition = request.competition
    create_form = UserCreateForm(competition=competition)
    if request.method == "POST":
        create_form = UserCreateForm(request.POST, competition=competition)
        if create_form.is_valid():
            data = create_form.cleaned_data
            with transaction.atomic():
                created = accounts.create_account(data["email"], data["first_name"], data["last_name"])
                if created is not None and data.get("team") is not None:
                    team = data["team"]
                    if team.users.count() >= team.max_members:
                        messages.error(request, f"{team.name} is full; {created.user.username} has no team yet.")
                    else:
                        TeamMembership.objects.create(user=created.user, team=team)
            if created is None:
                create_form.add_error("email", "An account with this email already exists.")
            else:
                return _credentials(request, [created], "Account created")

    selected = request.GET.get("show", "all")
    if selected not in FILTERS:
        selected = "all"
    query = (request.GET.get("q") or "").strip()[:100]
    team_of = _team_of(competition)
    listed = User.objects.filter(FILTERS[selected]).annotate(uploads_count=Count("uploads")).order_by("username")
    if query:
        listed = listed.filter(
            Q(username__icontains=query) | Q(first_name__icontains=query) | Q(last_name__icontains=query)
        )
    rows = []
    for user in listed[:500]:
        team = team_of.get(user.id)
        if selected == "no-team" and team is not None:
            continue
        rows.append({"user": user, "team": team, "manageable": accounts.can_manage(request.user, user)})
    return render_with_base_info(request, "staff/users.html", {
        "rows": rows,
        "create_form": create_form,
        "import_form": UserImportForm(),
        "filters": list(FILTERS),
        "selected": selected,
        "query": query,
        "total": User.objects.count(),
    })  # fmt: skip


@never_cache
@staff_required
@require_POST
def import_users(request: HttpRequest):
    form = UserImportForm(request.POST, request.FILES)
    if not form.is_valid():
        for error in form.non_field_errors():
            messages.error(request, error)
        return redirect("staff_users")
    try:
        parsed = accounts.parse_csv(form.cleaned_data["text"])
    except accounts.CsvError as exc:
        messages.error(request, str(exc))
        return redirect("staff_users")
    created, problems = accounts.import_rows(parsed.rows, competition=request.competition)
    return _credentials(request, created, "Accounts imported", [*parsed.problems, *problems])


def _managed_user(request, uuid):
    user = get_object_or_404(User, uuid=uuid)
    if not accounts.can_manage(request.user, user):
        raise Http404("not manageable")
    return user


@never_cache
@staff_required
def user_detail(request: HttpRequest, uuid):
    user = get_object_or_404(User, uuid=uuid)
    manageable = accounts.can_manage(request.user, user)
    competition = request.competition
    team = _team_of(competition).get(user.id)
    edit_form = UserEditForm(initial={"first_name": user.first_name, "last_name": user.last_name, "email": user.email})
    if request.method == "POST":
        if not manageable:
            raise Http404("not manageable")
        edit_form = UserEditForm(request.POST)
        if edit_form.is_valid():
            for key in ("first_name", "last_name", "email"):
                setattr(user, key, edit_form.cleaned_data[key])
            user.save(update_fields=["first_name", "last_name", "email"])
            messages.success(request, f"Saved {user.username}.")
            return redirect("staff_user", uuid=user.uuid)
    return render_with_base_info(request, "staff/user.html", {
        "subject": user,
        "manageable": manageable,
        "team": team,
        "edit_form": edit_form,
        "team_form": UserTeamForm(competition=competition, initial={"team": team}),
        "uploads": user.uploads.select_related("artifact").order_by("-created_at")[:10],
    })  # fmt: skip


@never_cache
@staff_required
@require_POST
def user_password(request: HttpRequest, uuid):
    user = _managed_user(request, uuid)
    password = accounts.reset_password(user)
    return _credentials(request, [accounts.Created(user, password)], "New one-time password")


@staff_required
@require_POST
def user_active(request: HttpRequest, uuid):
    """Deactivate (no login, sessions end) or reactivate an account. Accounts
    are never deleted here: their uploads and results stay attributable."""
    user = _managed_user(request, uuid)
    user.is_active = request.POST.get("active") == "1"
    user.save(update_fields=["is_active"])
    messages.success(request, f"{user.username} is now {'active' if user.is_active else 'deactivated'}.")
    return redirect("staff_user", uuid=user.uuid)


@staff_required
@require_POST
def user_team(request: HttpRequest, uuid):
    """Put the user into a team of the current competition (or none). Leaving
    the old team removes the user's devbox keys there, as leaving does."""
    user = _managed_user(request, uuid)
    competition = request.competition
    form = UserTeamForm(request.POST, competition=competition)
    if not form.is_valid():
        messages.error(request, "Choose a team of this competition.")
        return redirect("staff_user", uuid=user.uuid)
    new = form.cleaned_data["team"]
    old = _team_of(competition).get(user.id)
    if new is not None and old is not None and new.pk == old.pk:
        return redirect("staff_user", uuid=user.uuid)
    if new is not None and new.users.count() >= new.max_members:
        messages.error(request, f"{new.name} is full ({new.max_members} members); raise its limit first.")
        return redirect("staff_user", uuid=user.uuid)
    if old is not None:
        remove_member(old, user)
    if new is not None:
        TeamMembership.objects.create(user=user, team=new)
        messages.success(request, f"{user.username} is now in {new.name}.")
    else:
        messages.success(request, f"{user.username} has no team now.")
    return redirect("staff_user", uuid=user.uuid)
