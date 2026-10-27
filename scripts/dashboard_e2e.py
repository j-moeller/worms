#!/usr/bin/env python3
"""Run a loopback-only dashboard against disposable PostgreSQL.

Install tests/requirements.txt in a virtualenv first; PostgreSQL's initdb and
pg_ctl must be on PATH. This reuses the test suite's migrations and real
service grants. It never reads the vault or connects to Proxmox. Ctrl-C
stops the web servers and removes the disposable database.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
import pg  # noqa: E402


def run(*args, **kwargs):
    subprocess.run(args, check=True, **kwargs)


class ThreadedServer(socketserver.ThreadingMixIn, WSGIServer):
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--http-port", type=int, default=28180)
    parser.add_argument("--https-port", type=int, default=28181)
    parser.add_argument("--work-dir", type=Path)
    args = parser.parse_args()
    bindir = pg.pg_bindir()
    if bindir is None:
        parser.error("Put PostgreSQL server binaries (initdb, pg_ctl) on PATH")
    work = (args.work_dir or Path(tempfile.mkdtemp(prefix="worms-dashboard-e2e-"))).resolve()
    work.mkdir(parents=True, exist_ok=True)
    if (work / "session.json").exists():
        parser.error("Use a fresh work directory; session.json already exists")
    work.chmod(0o700)
    # Drop inherited live connection settings inside this process only.
    for key in list(os.environ):
        if key.startswith(("DB_", "WORMS_", "PROXMOX_", "ORCHESTRATOR_", "DJANGO_")):
            del os.environ[key]
    inventory, grants = work / "inventory.yml", work / "privileges.sql"
    run(sys.executable, str(REPO / "scripts/generate_inventory.py"),
        "--repo-root", str(work / "no-infrastructure-stacks"),
        "--platform-json", str(REPO / "tests/fixtures/platform-outputs.json"),
        "--output", str(inventory), "--quiet")
    run("ansible-playbook", "-i", str(inventory), str(REPO / "tests/ansible/render.yml"),
        "-e", json.dumps({"role_dir": str(REPO / "ansible/roles/database/setup_db_permissions"),
                           "template": "privileges.sql.j2", "dest": str(grants)}))
    cluster = pg.Cluster(bindir)
    servers = []
    running = []
    try:
        cluster.start()
        pg.setup(cluster, grants.read_text())
        env = {
            "PRODUCTION": "1", "DJANGO_SETTINGS_MODULE": "config.settings.dashboard",
            "DJANGO_SECRET_KEY": secrets.token_urlsafe(40),
            "DJANGO_ALLOWED_HOSTS": "localhost,127.0.0.1",
            "DJANGO_MEDIA_ROOT": str(work / "media"),
            "DJANGO_STATIC_ROOT": str(work / "static"),
            "ORCHESTRATOR_URL": "", "ORCHESTRATOR_API_TOKEN": "",
            **cluster.env("django_dashboard"),
        }
        password = secrets.token_urlsafe(18)
        seed_env = dict(os.environ, **env)
        seed_env.update(DB_USER="postgres", DB_PASSWORD="trust", E2E_ADMIN_PASSWORD=password)
        run(sys.executable, "-c", """
import os, django
django.setup()
from django.core.management import call_command
from core.models.user import User
call_command('init', competition='Local E2E Competition')
admin = User.objects.get(username='admin')
admin.set_password(os.environ['E2E_ADMIN_PASSWORD'])
admin.must_change_password = False
admin.save(update_fields=['password', 'must_change_password'])
""", cwd=REPO / "projects/dashboard", env=seed_env)
        os.environ.update(env)
        sys.path.insert(0, str(REPO / "projects/dashboard"))
        import django
        django.setup()
        from django.contrib.staticfiles.handlers import StaticFilesHandler
        from django.core.wsgi import get_wsgi_application
        app = StaticFilesHandler(get_wsgi_application())
        http = make_server("127.0.0.1", args.http_port, app,
                           server_class=ThreadedServer, handler_class=WSGIRequestHandler)
        servers.append(http)
        # Trust is scoped to clients that explicitly use this certificate.
        # Nothing is installed in the OS/browser certificate store.
        cert, key, config = work / "localhost.pem", work / "localhost-key.pem", work / "openssl.cnf"
        config.write_text("[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=v3\n"
                          "[dn]\nCN=localhost\n[v3]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n")
        run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
            "-config", str(config), "-keyout", str(key), "-out", str(cert),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        key.chmod(0o600)

        def https_app(environ, start_response):
            environ["wsgi.url_scheme"] = "https"
            return app(environ, start_response)

        https = make_server("127.0.0.1", args.https_port, https_app,
                            server_class=ThreadedServer, handler_class=WSGIRequestHandler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        https.socket = context.wrap_socket(https.socket, server_side=True)
        servers.append(https)
        session = {
            "http_url": f"http://localhost:{http.server_port}",
            "https_url": f"https://localhost:{https.server_port}",
            "certificate": str(cert), "database_port": cluster.port,
            "database_directory": str(cluster.dir), "username": "admin", "password": password,
            "work_directory": str(work), "media_root": str(work / "media"),
        }
        session_file = work / "session.json"
        session_file.write_text(json.dumps(session, indent=2) + "\n")
        session_file.chmod(0o600)
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            running.append(server)
        print(json.dumps(session, indent=2), flush=True)
        print("Local test instance ready. Database writes use django_dashboard; no conductor or node agent is running.", flush=True)
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        for server in servers:
            if server in running:
                server.shutdown()
            server.server_close()
        if (cluster.dir / "data").exists():
            cluster.stop()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    main()
