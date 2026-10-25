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
from dashboard.views.navigation import set_environment, set_theme
from dashboard.views.profile import profile
from dashboard.views.teams import join_team, leave_team, team_registration
from dashboard.views.uploads import upload
from dashboard.views.todos import todos
from dashboard import api
from dashboard.views import progression
from dashboard.views.rounds import rounds
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
    # A team's rounds, replayed (docs/progression.md).
    path("progression/", progression.progression_page, name="progression"),
    path("progression/rounds/", progression.team_rounds, name="progression_rounds"),
    path("progression/rounds/<int:round_id>/", progression.team_document, name="progression_data"),
    path("progression/rounds/<int:round_id>/flows/", progression.team_flows, name="progression_flows"),
    path("progression/rounds/<int:round_id>/logs/<uuid:ref>/", progression.team_log, name="progression_log"),
    path("progression/rounds/<int:round_id>/download/", progression.team_download, name="progression_download"),
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
    # One run's progression, for staff (the TOPOLOGY player).
    path("rounds/<uuid:uuid>/progression/", progression.run_document, name="run_progression"),
    path("rounds/<uuid:uuid>/progression/flows/", progression.run_flows, name="run_progression_flows"),
    path("rounds/<uuid:uuid>/progression/logs/<uuid:ref>/", progression.run_log, name="run_progression_log"),
    path("rounds/<uuid:uuid>/progression/download/", progression.run_download, name="run_progression_download"),
    # Team API (bearer token; docs/team_api.md).
    path("api/v1/rounds/", api.rounds, name="api_rounds"),
    path("api/v1/rounds/<int:round_id>/scoring/", api.round_scoring, name="api_round_scoring"),
    path("api/v1/rounds/<int:round_id>/logging/", api.round_logging, name="api_round_logging"),
    path("api/v1/rounds/<int:round_id>/progression/", api.round_progression, name="api_round_progression"),
    path("api/v1/rounds/<int:round_id>/progression/flows/", api.round_progression_flows, name="api_round_progression_flows"),
    path(
        "api/v1/rounds/<int:round_id>/progression/logs/<uuid:ref>/",
        api.round_progression_log,
        name="api_round_progression_log",
    ),
    path("api/v1/rounds/<int:round_id>/progression.zip", api.round_progression_zip, name="api_round_progression_zip"),
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
