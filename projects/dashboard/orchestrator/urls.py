from django.urls import path
from orchestrator import api

urlpatterns = [
    # Dashboard (token scope "dashboard"): reset a team's devbox (bump the
    # version + enqueue the rebuild), or re-push SSH keys (reconfigure, no
    # rebuild) after a key is added/removed or a member leaves.
    path("devbox/<uuid:team_uuid>/reset/", api.devbox_reset, name="devbox_reset"),
    path("devbox/<uuid:team_uuid>/reconfigure/", api.devbox_reconfigure, name="devbox_reconfigure"),
    # Dashboard (same scope): a gameplan's simulated attempt, for staff's Simulations page.
    path("gameplans/<str:filename>/simulation/", api.gameplan_simulation, name="gameplan_simulation"),
]
