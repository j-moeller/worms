from dashboard.views.access import create_access_token, revoke_access_token
from dashboard.views.auth import login_view, logout_view
from dashboard.views.password import change_password
from dashboard.views.debug import debug_login_as, populate
from dashboard.views.leaderboard import leaderboard
from dashboard.views.devbox import (
    home,
    devbox_add_ssh_key,
    devbox_delete_ssh_key,
    devbox_reset,
)
from dashboard.views.logging import logging_event_data, logs
from dashboard.views.navigation import set_environment, set_theme
from dashboard.views.profile import profile
from dashboard.views.scoring import flag_capture_attempts
from dashboard.views.teams import join_team, leave_team, team_registration
from dashboard.views.uploads import upload
from dashboard.views.todos import todos
from dashboard import api
from dashboard.views.rounds import rounds, round_timeline_data
from dashboard.views.topology import topology, topology_data, topology_runs
from django.conf import settings
from django.contrib import admin
from django.urls import path

urlpatterns = [
    # Dispatches to the leaderboard (production) or the devbox (development).
    path("", home, name="index"),
    #
    path("devbox/reset/", devbox_reset, name="devbox_reset"),
    path("devbox/ssh-keys/add/", devbox_add_ssh_key, name="devbox_add_ssh_key"),
    path("devbox/ssh-keys/<int:key_id>/delete/", devbox_delete_ssh_key, name="devbox_delete_ssh_key"),
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
    path("change-password/", change_password, name="change_password"),
    # Page-wide settings (plain form posts; enhance.js only speeds them up).
    path("theme/", set_theme, name="set_theme"),
    path("environment/", set_environment, name="set_environment"),
    #
    path("teams/", team_registration, name="team_registration"),
    path("join_team/<uuid:uuid>", join_team, name="join_team"),
    path("leave_team/<uuid:uuid>", leave_team, name="leave_team"),
    #
    path("profile/", profile, name="profile"),
    #
    path("todos/", todos, name="todos"),
    #
    path("rounds/", rounds, name="rounds"),
    path("rounds/<uuid:uuid>/timeline/", round_timeline_data, name="round_timeline_data"),
    # Team API (bearer token; docs/team_api.md).
    path("api/v1/rounds/", api.rounds, name="api_rounds"),
    path("api/v1/rounds/<int:round_id>/scoring/", api.round_scoring, name="api_round_scoring"),
    path("api/v1/rounds/<int:round_id>/logging/", api.round_logging, name="api_round_logging"),
    path("topology/", topology, name="topology"),
    path("topology/data/", topology_data, name="topology_data"),
    path("topology/runs/", topology_runs, name="topology_runs"),
    #
    # Team API tokens (shown in the profile; the standalone Access page is gone).
    path(
        "access-tokens/create/",
        create_access_token,
        name="create_access_token",
    ),
    path(
        "access-tokens/<int:token_id>/revoke/",
        revoke_access_token,
        name="revoke_access_token",
    ),
]

if settings.DEBUG:
    from core import clock

    urlpatterns += [
        path("debug/populate/", populate, name="debug_populate"),
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
