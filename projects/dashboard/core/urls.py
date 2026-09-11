from django.urls import path
from django.conf import settings

from . import debug_views, views

urlpatterns = [
    path("", views.leaderboard, name="index"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("captures/", views.flag_capture_attempts, name="captures"),
    path("logs/", views.logs, name="logs"),
    path(
        "logging/events/<uuid:uuid>/",
        views.logging_event_data,
        name="logging_event_data",
    ),
    #
    path("upload/", views.upload, name="upload"),
    #
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    #
]

if settings.DEBUG:
    urlpatterns += [
        path("dev/seed_dummy_data", debug_views.seed_dummy_data),
        path(
            "debug/login-as/<int:user_id>/",
            views.debug_login_as,
            name="debug_login_as",
        ),
    ]
