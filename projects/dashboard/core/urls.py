from django.conf import settings
from django.urls import path

from core.views.auth import login_view, logout_view
from core.views.access import access, create_access_token, revoke_access_token
from core.views.profile import profile
from core.views.debug import debug_login_as
from core.views.leaderboard import leaderboard
from core.views.logging import logging_event_data, logs
from core.views.scoring import flag_capture_attempts
from core.views.uploads import upload
from core.views.teams import team_registration, join_team, leave_team

urlpatterns = [
    path("", leaderboard, name="index"),
    path("captures/", flag_capture_attempts, name="captures"),
    path("logs/", logs, name="logs"),
    path(
        "logging/events/<uuid:uuid>/",
        logging_event_data,
        name="logging_event_data",
    ),
    #
    path("upload/", upload, name="upload"),
    #
    path("login/", login_view, name="login"),
    path("logout/", logout_view, name="logout"),
    #
    path("teams/", team_registration, name="team_registration"),
    path("join_team/<uuid:uuid>", join_team, name="join_team"),
    path("leave_team/<uuid:uuid>", leave_team, name="leave_team"),
    #
    path("profile/", profile, name="profile"),
    #
    path("access/", access, name="access"),
    path(
        "access/access-tokens/create/",
        create_access_token,
        name="create_access_token",
    ),
    path(
        "access/access-tokens/<int:token_id>/revoke/",
        revoke_access_token,
        name="revoke_access_token",
    ),
]

if settings.DEBUG:
    urlpatterns += [
        path(
            "debug/login-as/<uuid:uuid>/",
            debug_login_as,
            name="debug_login_as",
        ),
    ]
