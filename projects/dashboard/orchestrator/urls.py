from django.urls import path
from orchestrator import api

urlpatterns = [
    # Dashboard (token scope "dashboard"): reset a team's devbox (bump the
    # version + enqueue the rebuild).
    path("devbox/<uuid:team_uuid>/reset/", api.devbox_reset, name="devbox_reset"),
]
