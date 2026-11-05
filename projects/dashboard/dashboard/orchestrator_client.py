"""Minimal client for the orchestrator REST API (dashboard -> orchestrator).

The dashboard never talks to Proxmox; its privileged actions (a devbox reset,
pushing a team's SSH keys) go through the orchestrator, authenticated with the
per-caller bearer token in settings.ORCHESTRATOR_API.

The infrastructure side has no DNS, so ORCHESTRATOR_URL holds an IP address. The
connection goes there, and the certificate is verified for ORCHESTRATOR_TLS_NAME
against the system's trust store, which holds the internal CA. Standard library
only: the dashboard VM installs packages only during a build's internet window.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
from urllib.parse import urlsplit

from django.conf import settings


class OrchestratorError(RuntimeError):
    pass


def _config() -> dict:
    api = getattr(settings, "ORCHESTRATOR_API", {}) or {}
    if not api.get("url") or not api.get("token"):
        raise OrchestratorError("orchestrator API is not configured (ORCHESTRATOR_URL / _API_TOKEN)")
    return api


class _Connection(http.client.HTTPSConnection):
    """Connects to the address it is given, and verifies the certificate for
    another name (http.client would verify it for the address)."""

    def __init__(self, host, port, tls_name, context, timeout):
        super().__init__(host, port, timeout=timeout, context=context)
        self._tls_name = tls_name
        self._tls_context = context

    def connect(self):
        sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock = self._tls_context.wrap_socket(sock, server_hostname=self._tls_name)


def post(path: str, timeout: int = 15) -> dict:
    """POST to the orchestrator and return its JSON. Raises OrchestratorError
    on any transport or non-2xx error."""
    api = _config()
    url = urlsplit(api["url"])
    tls_name = api.get("tls_name") or url.hostname
    context = ssl.create_default_context(cafile=api.get("ca_bundle") or None)
    connection = _Connection(url.hostname, url.port or 443, tls_name, context, timeout)
    try:
        connection.request(
            "POST",
            url.path.rstrip("/") + path,
            headers={"Authorization": f"Bearer {api['token']}", "Host": tls_name},
        )
        response = connection.getresponse()
        body = response.read()
    except (OSError, http.client.HTTPException) as exc:  # ssl.SSLError is an OSError
        raise OrchestratorError(f"orchestrator unreachable: {exc}") from exc
    finally:
        connection.close()
    if response.status >= 300:
        raise OrchestratorError(f"orchestrator returned {response.status}: {body[:200].decode(errors='replace')}")
    try:
        return json.loads(body)
    except ValueError:
        return {}
