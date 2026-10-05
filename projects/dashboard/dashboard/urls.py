from dashboard.views.access import access, create_access_token, revoke_access_token
from dashboard.views.auth import login_view, logout_view
from dashboard.views.debug import debug_login_as
from dashboard.views.leaderboard import leaderboard
from dashboard.views.logging import logging_event_data, logs
from dashboard.views.profile import profile
from dashboard.views.scoring import flag_capture_attempts
from dashboard.views.teams import join_team, leave_team, team_registration
from dashboard.views.uploads import upload
from dashboard.views.todos import todos
from django.conf import settings
from django.contrib import admin
from django.urls import path

urlpatterns = [
    path("", leaderboard, name="index"),
    #
    path("admin/", admin.site.urls),
    #
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
    path("todos/", todos, name="todos"),
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
    from core import clock

    urlpatterns += [
        path(
            "debug/login-as/<uuid:uuid>/",
            debug_login_as,
            name="debug_login_as",
        ),
        path(
            "debug/minutes-plus-one",
            clock.minutes_plus_one,
            name="minutes-plus-one",
        ),
        path(
            "debug/minutes-minus-one",
            clock.minutes_minus_one,
            name="minutes-minus-one",
        ),
    ]
