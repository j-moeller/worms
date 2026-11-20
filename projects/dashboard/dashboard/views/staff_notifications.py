"""Staff: the notifications under the bell in the header (core/models/notification.py,
views/notifications.py). Staff write one for every team or for the teams it names, edit it, switch
it on and off, and delete it. Each change is recorded (competition_events: notification_created,
_updated, _activated, _deactivated, _deleted) with the dashboard's own grants on the two tables
(setup_db_permissions).
"""

from __future__ import annotations

from core import clock, events
from core.models.notification import Notification
from dashboard.forms import NotificationForm
from dashboard.views.decorators import staff_required
from dashboard.views.util import render_with_base_info
from django.contrib import messages
from django.db import transaction
from django.http import HttpRequest
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

FIELDS = ("severity", "message", "audience", "active")


def _detail(notification: Notification) -> dict:
    """What the record of a change says about the notification."""
    return {
        "uuid": str(notification.uuid), "severity": notification.severity, "active": notification.active,
        "audience": notification.audience, "teams": sorted(t.name for t in notification.teams.all()),
        "message": notification.message[:160],
    }  # fmt: skip


def _summary(notification: Notification) -> str:
    text = " ".join(notification.message.split())
    return text if len(text) <= 60 else text[:59] + "…"


@staff_required
def notifications(request: HttpRequest):
    form = NotificationForm(competition=request.competition)
    if request.method == "POST":
        form = NotificationForm(request.POST, competition=request.competition)
        if form.is_valid():
            data = form.cleaned_data
            with transaction.atomic():
                created = Notification.objects.create(created_by=request.user, **{name: data[name] for name in FIELDS})
                created.teams.set(data["teams"])
            events.competition("notification_created", request=request, detail=_detail(created))
            messages.success(request, f"{'Shown' if created.active else 'Saved, not shown'}: “{_summary(created)}”.")
            return redirect("staff_notifications")
    rows = Notification.objects.prefetch_related("teams").select_related("created_by").order_by("-active", "-created_at", "-id")
    return render_with_base_info(request, "staff/notifications.html", {"notifications": rows, "form": form})


@staff_required
def notification_detail(request: HttpRequest, uuid):
    notification = get_object_or_404(Notification, uuid=uuid)
    if request.method == "POST":
        form = NotificationForm(request.POST, competition=request.competition)
        if form.is_valid():
            data = form.cleaned_data
            with transaction.atomic():
                notification = Notification.objects.select_for_update().get(pk=notification.pk)
                for name in FIELDS:
                    setattr(notification, name, data[name])
                notification.updated_at = clock.now()
                notification.save(update_fields=[*FIELDS, "updated_at"])
                notification.teams.set(data["teams"])
            events.competition("notification_updated", request=request, detail=_detail(notification))
            messages.success(request, f"Saved: “{_summary(notification)}”.")
            return redirect("staff_notifications")
    else:
        form = NotificationForm(initial={**{name: getattr(notification, name) for name in FIELDS},
                                         "teams": notification.teams.all()}, competition=request.competition)  # fmt: skip
    # Teams it names that are not of the current competition: kept until it is saved here.
    elsewhere = notification.teams.exclude(competition=request.competition)
    return render_with_base_info(request, "staff/notification.html", {"notification": notification, "form": form,
                                                                      "elsewhere": elsewhere})  # fmt: skip


@staff_required
@require_POST
def notification_active(request: HttpRequest, uuid):
    notification = get_object_or_404(Notification, uuid=uuid)
    notification.active = request.POST.get("active") == "1"
    notification.updated_at = clock.now()
    notification.save(update_fields=["active", "updated_at"])
    events.competition("notification_activated" if notification.active else "notification_deactivated", request=request,
                       detail=_detail(notification))  # fmt: skip
    messages.success(request, f"{'Shown' if notification.active else 'Hidden'}: “{_summary(notification)}”.")
    return redirect("staff_notifications")


@staff_required
@require_POST
def notification_delete(request: HttpRequest, uuid):
    notification = get_object_or_404(Notification, uuid=uuid)
    detail, summary = _detail(notification), _summary(notification)
    notification.delete()  # its rows in notifications_teams first
    events.competition("notification_deleted", request=request, detail=detail)
    messages.success(request, f"Deleted: “{summary}”.")
    return redirect("staff_notifications")
