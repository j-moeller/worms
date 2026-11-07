"""Serve the site favicon.

The dashboard is served entirely through Django (nginx proxies `location /`
straight to gunicorn; there is no `/static/` location and collectstatic is not
run), so the favicon is delivered by a normal URL route rather than the static
pipeline. The SVG is read once at import.
"""

from pathlib import Path

from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.views.decorators.cache import cache_control

_FAVICON = Path(__file__).resolve().parent.parent / "static" / "favicon.svg"
_SVG = _FAVICON.read_bytes()


@cache_control(max_age=86400, public=True)
def favicon_svg(request: HttpRequest) -> HttpResponse:
    return HttpResponse(_SVG, content_type="image/svg+xml")


def favicon_ico(request: HttpRequest) -> HttpResponse:
    # Browsers that honour the <link rel="icon"> in the page never ask for
    # this; it only catches the bare /favicon.ico probe, so point it at the SVG.
    return redirect("favicon", permanent=True)
