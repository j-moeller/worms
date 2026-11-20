"""The notifications from staff, as a signed-in user sees them: the bell in the header, next to
Upload and Progression (base.html), and the page it opens. A user sees the active notifications for
every team and those naming their own team (core/models/notification.py); staff write them on
/staff/notifications/ (staff_notifications.py).
"""

from __future__ import annotations

from core.models.notification import Notification
from dashboard.views.util import render_with_base_info
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest


def bell(request: HttpRequest) -> dict | None:
    """The header's bell (views/util.py, render_with_base_info): how many active notifications the
    user sees, and the most severe of them; None for an anonymous visitor."""
    if not request.user.is_authenticated:
        return None
    severities = [severity for _, severity in Notification.shown_to(getattr(request, "team", None)).values_list("id", "severity")]
    return {"count": len(severities), "severity": Notification.most_severe(severities)}


def ranked(notifications) -> list[Notification]:
    """Most severe first, then newest."""
    return sorted(notifications, key=lambda n: (-Notification.RANK.get(n.severity, 0), -n.created_at.timestamp(), -n.id))


@login_required
def notifications(request: HttpRequest):
    shown = ranked(Notification.shown_to(request.team).prefetch_related("teams"))
    return render_with_base_info(request, "notifications.html", {"notifications": shown})
