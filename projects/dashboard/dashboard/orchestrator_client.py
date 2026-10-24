"""Minimal client for the orchestrator REST API (dashboard -> orchestrator).

The dashboard never talks to Proxmox; privileged actions (sandbox VM power,
devbox reset) go through the orchestrator, authenticated with the per-caller
bearer token in settings.ORCHESTRATOR_API. The connection is TLS-verified
against the internal CA; set ORCHESTRATOR_URL to a name the certificate covers
(orchestrator.worms.internal) so verification succeeds.
"""

from __future__ import annotations

from django.conf import settings


class OrchestratorError(RuntimeError):
    pass


def _config() -> dict:
    api = getattr(settings, "ORCHESTRATOR_API", {}) or {}
    if not api.get("url") or not api.get("token"):
        raise OrchestratorError("orchestrator API is not configured (ORCHESTRATOR_URL / _API_TOKEN)")
    return api


def post(path: str, timeout: int = 15) -> dict:
    """POST to the orchestrator and return its JSON. Raises OrchestratorError
    on any transport or non-2xx error."""
    import requests

    api = _config()
    url = api["url"].rstrip("/") + path
    verify = api.get("ca_bundle") or True
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {api['token']}"},
            timeout=timeout,
            verify=verify,
        )
    except requests.RequestException as exc:  # pragma: no cover (network)
        raise OrchestratorError(f"orchestrator unreachable: {exc}") from exc
    if response.status_code >= 300:
        raise OrchestratorError(f"orchestrator returned {response.status_code}: {response.text[:200]}")
    try:
        return response.json()
    except ValueError:
        return {}
