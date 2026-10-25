"""The dashboard's visual themes.

Every page uses one markup and the shared component styles
(static/dashboard/css/base.css); a theme is a stylesheet on top of them
(static/dashboard/css/themes/<key>.css). The default comes from the
DASHBOARD_THEME setting; a visitor's own choice is kept in a cookie -- not in
the session, so switching costs no database write, also for anonymous
visitors.
"""

from __future__ import annotations

from django.conf import settings

THEMES = {
    "lab": "Light",
    "lab-dark": "Dark",
}
DEFAULT = "lab"
COOKIE = "worms_theme"
COOKIE_MAX_AGE = 365 * 24 * 3600


def default() -> str:
    key = getattr(settings, "DASHBOARD_THEME", DEFAULT)
    return key if key in THEMES else DEFAULT


def resolve(request) -> str:
    """The theme for this request: the visitor's valid choice, else the default."""
    choice = request.COOKIES.get(COOKIE, "")
    return choice if choice in THEMES else default()


def stylesheet(key: str) -> str:
    """Static path of a theme's stylesheet."""
    return f"dashboard/css/themes/{key}.css"
