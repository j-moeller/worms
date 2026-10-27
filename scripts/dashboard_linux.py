#!/usr/bin/env python3
"""Linux-only rehearsal: actual nginx template -> Gunicorn -> disposable PG.

Run inside tests/docker/Dockerfile. Only fixture credentials are generated;
no vault, host agent or Proxmox is consulted. The bootstrap process owns the
PG lifecycle; Gunicorn serves every request through the real nginx template.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
import time

from dashboard_e2e import REPO, pg, run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--http-port", type=int, default=28190)
    parser.add_argument("--https-port", type=int, default=28191)
    parser.add_argument("--login-rate", default="10r/m")
    parser.add_argument("--orchestrator-port", type=int, default=28192)
    args = parser.parse_args()
    work = args.work_dir.resolve()
    work.mkdir(parents=True, mode=0o700)
    children, logs = [], []
    try:
        def start(name, command, **kwargs):
            log = (work / f"{name}.log").open("w")
            logs.append(log)
            child = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, **kwargs)
            children.append(child)
            return child

        bootstrap_dir = work / "bootstrap"
        bootstrap = start("bootstrap", [sys.executable, str(REPO / "scripts/dashboard_e2e.py"),
            "--work-dir", str(bootstrap_dir), "--http-port", "0", "--https-port", "0"])
        deadline = time.monotonic() + 90
        while not (bootstrap_dir / "session.json").exists():
            if bootstrap.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError((work / "bootstrap.log").read_text())
            time.sleep(0.1)
        session = json.loads((bootstrap_dir / "session.json").read_text())
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("DB_", "WORMS_", "PROXMOX_", "ORCHESTRATOR_", "DJANGO_"))}
        token = secrets.token_urlsafe(32)
        env.update(PRODUCTION="1", DJANGO_SETTINGS_MODULE="config.settings.dashboard",
            DJANGO_SECRET_KEY=secrets.token_urlsafe(40), DJANGO_ALLOWED_HOSTS="localhost,127.0.0.1",
            DJANGO_MEDIA_ROOT=session["media_root"], DJANGO_STATIC_ROOT=str(work / "static"),
            DB_HOST="127.0.0.1", DB_PORT=str(session["database_port"]), DB_NAME="mydb",
            DB_USER="django_dashboard", DB_PASSWORD="test-password", DB_SSLMODE="disable",
            ORCHESTRATOR_URL=f"https://localhost:{args.orchestrator_port}", ORCHESTRATOR_API_TOKEN=token,
            SSL_CERT_FILE=session["certificate"])
        project = REPO / "projects/dashboard"
        run(sys.executable, "manage.py", "collectstatic", "--noinput", env=env, cwd=project,
            stdout=subprocess.DEVNULL)
        dashboard_socket = work / "dashboard.sock"
        orchestrator_socket = work / "orchestrator.sock"
        gunicorn = start("gunicorn", ["gunicorn", "--bind", f"unix:{dashboard_socket}",
            "--workers", "3", "--access-logfile", "-", "--error-logfile", "-", "config.wsgi:application"],
            env=env, cwd=project)
        while not dashboard_socket.exists():
            if gunicorn.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError((work / "gunicorn.log").read_text())
            time.sleep(0.1)
        orchestrator_env = dict(env, DJANGO_SETTINGS_MODULE="config.settings.orchestrator",
            DB_USER="django_orchestrator", DB_MIGRATOR_USER="unused", DB_MIGRATOR_PASSWORD="unused",
            PROXMOX_HOST="unused.invalid", PROXMOX_USER="unused", PROXMOX_TOKEN_NAME="unused",
            PROXMOX_TOKEN_VALUE="unused", ORCHESTRATOR_DASHBOARD_TOKEN=token)
        start("orchestrator", ["gunicorn", "--bind", f"unix:{orchestrator_socket}", "--workers", "2",
            "--access-logfile", "-", "--error-logfile", "-", "config.wsgi:application"],
            env=orchestrator_env, cwd=project)
        start("attempt-processor", [sys.executable, "manage.py", "process_attempts", "--interval", "0.2"],
            env=orchestrator_env, cwd=project)
        site = work / "site.conf"
        run("ansible-playbook", "-i", str(bootstrap_dir / "inventory.yml"), str(REPO / "tests/ansible/render.yml"),
            "-e", json.dumps({"role_dir": str(REPO / "ansible/roles/web/nginx_tls_django"),
                "template": "worms-tls.conf.j2", "dest": str(site), "tls_dns_name": "localhost",
                "tls_cert_path": session["certificate"], "tls_key_path": str(bootstrap_dir / "localhost-key.pem"),
                "nginx_django_static_root": str(work / "static"), "nginx_team_api": True,
                "nginx_login_rate": args.login_rate, "nginx_upload_max_body_size": "101m"}))
        # Substitute only local paths/ports; retain the deployed proxy policy.
        config = site.read_text().replace("listen 80;", f"listen {args.http_port};")
        config = config.replace("listen [::]:80;", f"listen [::]:{args.http_port};")
        config = config.replace("listen 443 ssl;", f"listen {args.https_port} ssl;")
        config = config.replace("listen [::]:443 ssl;", f"listen [::]:{args.https_port} ssl;")
        config = config.replace("https://$host$request_uri", f"https://$host:{args.https_port}$request_uri")
        config = config.replace("/run/django/django.sock", str(dashboard_socket))
        site.write_text(config)
        orchestrator_site = work / "orchestrator-site.conf"
        run("ansible-playbook", "-i", str(bootstrap_dir / "inventory.yml"), str(REPO / "tests/ansible/render.yml"),
            "-e", json.dumps({"role_dir": str(REPO / "ansible/roles/web/nginx_tls_django"),
                "template": "worms-tls.conf.j2", "dest": str(orchestrator_site), "tls_dns_name": "localhost",
                "tls_cert_path": session["certificate"], "tls_key_path": str(bootstrap_dir / "localhost-key.pem"),
                "nginx_django_static_root": str(work / "static")}))
        orch_http_port = pg.free_port()
        orch_config = orchestrator_site.read_text().replace("listen 80;", f"listen {orch_http_port};")
        orch_config = orch_config.replace("listen [::]:80;", f"listen [::]:{orch_http_port};")
        orch_config = orch_config.replace("listen 443 ssl;", f"listen {args.orchestrator_port} ssl;")
        orch_config = orch_config.replace("listen [::]:443 ssl;", f"listen [::]:{args.orchestrator_port} ssl;")
        orch_config = orch_config.replace("/run/django/django.sock", str(orchestrator_socket))
        orchestrator_site.write_text(orch_config)
        nginx_config = work / "nginx.conf"
        user = "user root;\n" if os.geteuid() == 0 else ""
        nginx_config.write_text(f"{user}pid {work}/nginx.pid;\nerror_log {work}/nginx-error.log;\n"
            f"events {{ worker_connections 256; }}\nhttp {{ include /etc/nginx/mime.types;\n"
            f"client_body_temp_path {work}/body-temp;\nproxy_temp_path {work}/proxy-temp;\n"
            f"access_log {work}/nginx-access.log;\ninclude {site};\ninclude {orchestrator_site};\n}}\n")
        run("nginx", "-t", "-c", str(nginx_config))
        start("nginx", ["nginx", "-c", str(nginx_config), "-g", "daemon off;"])
        session.update(http_url=f"http://localhost:{args.http_port}",
            https_url=f"https://localhost:{args.https_port}", work_directory=str(work),
            stack="nginx + 3 Gunicorn workers + PostgreSQL", gunicorn_pid=gunicorn.pid,
            nginx_config=str(nginx_config), login_rate=args.login_rate, attempt_processor=True,
            orchestrator_url=f"https://localhost:{args.orchestrator_port}", orchestrator_token=token)
        (work / "session.json").write_text(json.dumps(session, indent=2) + "\n")
        (work / "session.json").chmod(0o600)
        print(json.dumps({k: session[k] for k in ("http_url", "https_url", "stack")}), flush=True)
        print(f"Credentials and private certificate: {work}/session.json", flush=True)
        while True:
            if any(child.poll() is not None for child in children):
                raise RuntimeError("A stack process exited; inspect logs in " + str(work))
            threading.Event().wait(1)
    except KeyboardInterrupt:
        pass
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        for log in logs:
            log.close()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    main()
