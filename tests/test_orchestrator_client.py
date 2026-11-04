"""The dashboard's client for the orchestrator API, over real TLS.

The dashboard's page tests replace this client with a stub, so they could not
see that it crashed on the dashboard VM: it imported a package only the
orchestrator VM has, and could not have verified the certificate anyway. These
tests run it against a local HTTPS server, and check that each VM gets the
packages its code imports.
"""

from __future__ import annotations

import ast
import http.server
import json
import ssl
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest
import yaml

from conftest import REPO

PROJECT = REPO / "projects" / "dashboard"
sys.path.insert(0, str(PROJECT))

from dashboard import orchestrator_client  # noqa: E402

TLS_NAME = "orchestrator.worms.internal"


@pytest.fixture(scope="module")
def certificate(tmp_path_factory):
    path = tmp_path_factory.mktemp("tls")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "2",
         "-keyout", path / "key.pem", "-out", path / "cert.pem", "-subj", "/CN=test", "-addext", f"subjectAltName=DNS:{TLS_NAME}"],
        check=True, capture_output=True,
    )  # fmt: skip
    return path


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.server.requests.append((self.path, self.headers["Authorization"], self.headers["Host"]))
        body = json.dumps({"ok": True}).encode()
        self.send_response(self.server.status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def server(certificate):
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.requests, httpd.status = [], 200
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate / "cert.pem", certificate / "key.pem")
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def configure(monkeypatch, certificate):
    def _configure(port, **overrides):
        api = {
            "url": f"https://127.0.0.1:{port}",
            "tls_name": TLS_NAME,
            "token": "the-token",
            "ca_bundle": str(certificate / "cert.pem"),
            **overrides,
        }
        monkeypatch.setattr(orchestrator_client, "settings", SimpleNamespace(ORCHESTRATOR_API=api))

    return _configure


def test_connects_by_address_and_verifies_the_certificate_for_the_name(server, configure):
    configure(server.server_address[1])
    assert orchestrator_client.post("/devbox/x/reconfigure/") == {"ok": True}
    assert server.requests == [("/devbox/x/reconfigure/", "Bearer the-token", TLS_NAME)]


def test_refuses_a_certificate_for_another_name(server, configure):
    configure(server.server_address[1], tls_name="other.worms.internal")
    with pytest.raises(orchestrator_client.OrchestratorError, match="unreachable"):
        orchestrator_client.post("/x/")
    assert server.requests == []


def test_refuses_a_certificate_from_an_unknown_ca(server, configure):
    configure(server.server_address[1], ca_bundle="")
    with pytest.raises(orchestrator_client.OrchestratorError, match="unreachable"):
        orchestrator_client.post("/x/")
    assert server.requests == []


def test_reports_an_error_status(server, configure):
    server.status = 403
    configure(server.server_address[1])
    with pytest.raises(orchestrator_client.OrchestratorError, match="returned 403"):
        orchestrator_client.post("/x/")


def test_reports_an_orchestrator_that_is_down(configure):
    configure(1)  # nothing listens on port 1
    with pytest.raises(orchestrator_client.OrchestratorError, match="unreachable"):
        orchestrator_client.post("/x/", timeout=2)


def test_reports_a_missing_configuration(configure):
    configure(1, token="")
    with pytest.raises(orchestrator_client.OrchestratorError, match="not configured"):
        orchestrator_client.post("/x/")


def _third_party_imports(*directories):
    found = set()
    for directory in directories:
        for path in (PROJECT / directory).rglob("*.py"):
            if "migrations" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Import):
                    found |= {alias.name.split(".")[0] for alias in node.names}
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    found.add(node.module.split(".")[0])
    return found - set(sys.stdlib_module_names) - {"config", "core", "dashboard", "orchestrator"}


def test_each_vm_gets_the_packages_its_code_imports():
    """Tests run in one virtualenv that has everything, so they cannot see a VM
    missing a package. The VMs get only what the role lists."""
    role = yaml.safe_load((REPO / "ansible/roles/applications/django_app/defaults/main.yml").read_text())
    orchestrator = yaml.safe_load((REPO / "ansible/group_vars/orchestrator.yml").read_text())
    shared = set(role["pip_packages"])
    assert _third_party_imports("config", "core", "dashboard") <= shared
    assert _third_party_imports("orchestrator") <= shared | set(orchestrator["django_extra_pip_packages"])
