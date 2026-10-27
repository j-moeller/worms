"""Real Linux reverse proxy, worker processes, orchestrator TLS and recovery.

Run in tests/docker/Dockerfile; no agent or Proxmox operation runs. Functional
cases deliberately use a high sign-in rate; the final case restores and checks
production's 10/minute setting, independently of browser/test timing.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import http.client
import json
import os
from pathlib import Path
import shutil
import signal
import ssl
import subprocess
import sys
import time

import pytest
import requests

from conftest import REPO
from test_dashboard_e2e import Browser, credentials, django_command, exercise_student_journey, ingest
import pg

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux nginx/Gunicorn stack rehearsal")


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    if not all(shutil.which(tool) for tool in ("nginx", "gunicorn", "openssl")) or not pg.pg_bindir():
        pytest.skip("Linux nginx, Gunicorn, OpenSSL and PostgreSQL binaries required")
    root = tmp_path_factory.mktemp("linux-stack")
    work = root / "state"
    log_path = root / "stack.log"
    with log_path.open("w") as log:
        process = subprocess.Popen([sys.executable, str(REPO / "scripts/dashboard_linux.py"),
            "--work-dir", str(work), "--http-port", str(pg.free_port()),
            "--https-port", str(pg.free_port()), "--orchestrator-port", str(pg.free_port()),
            "--login-rate", "6000r/s"], cwd=REPO, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 180
            while not (work / "session.json").exists():
                assert process.poll() is None, log_path.read_text()
                assert time.monotonic() < deadline, log_path.read_text()
                time.sleep(0.1)
            config = json.loads((work / "session.json").read_text())
            probe = Browser(config)
            while True:
                try:
                    if probe.get("/login/").status_code == 200:
                        break
                except requests.ConnectionError:
                    pass
                assert time.monotonic() < deadline, log_path.read_text()
                time.sleep(0.1)
            exercise_student_journey(config)
            admin = Browser(config)
            assert admin.login(config["username"], config["password"]).status_code == 302
            uuids = django_command(config, "from core.models.user import User\nimport json\n"
                "print(json.dumps(dict(User.objects.values_list('username','uuid')),default=str))")
            _, otp = credentials(admin.post(f"/staff/users/{uuids['journey.alice@example.test']}/password/"))
            student = Browser(config)
            assert student.login("journey.alice@example.test", otp).status_code == 302
            student.get("/change-password/")
            password = "Linux-Stack-Student-7392!"
            assert student.post("/change-password/", {"new_password1": password, "new_password2": password}).status_code == 302
            config.update(student_password=password, admin_client=admin, student_client=student)
            yield config
        finally:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def test_proxy_headers_tls_static_files_and_body_limits(stack):
    client = stack["student_client"]
    assert client.get("/static/dashboard/js/topology.js").status_code == 200
    assert client.get("/profile/").headers["Strict-Transport-Security"].startswith("max-age=")
    assert client.get("/profile/", headers={"Host": "wrong.example"}).status_code == 400
    response = client.session.post(client.base + "/theme/", data={"theme": "lab-dark"},
        headers={"Origin": client.base, "X-CSRFToken": client.session.cookies.get("csrftoken"),
                 "X-Forwarded-Proto": "http"}, allow_redirects=False, timeout=15)
    assert response.status_code == 302  # nginx overrides the forged scheme
    assert "Secure" in response.headers["Set-Cookie"]
    assert client.post("/theme/", {"theme": "lab"}, origin="https://wrong.example").status_code == 403
    untrusted = requests.Session()
    untrusted.trust_env = False
    with pytest.raises(requests.exceptions.SSLError):
        untrusted.get(client.base + "/login/", timeout=10)
    context = ssl.create_default_context(cafile=stack["certificate"])
    port = int(client.base.rsplit(":", 1)[1])
    with closing(http.client.HTTPSConnection("localhost", port, context=context, timeout=10)) as raw:
        raw.request("POST", "/upload/", headers={"Content-Length": str(102 * 1024 * 1024)})
        assert raw.getresponse().status == 413  # reject without sending 102 MiB


def test_dashboard_can_reset_a_devbox_over_verified_internal_tls(stack):
    django_command(stack, "from core.models.team import Team\nfrom core.models.devbox import Devbox\n"
        "Devbox.objects.get_or_create(team=Team.objects.get(name='Journey Blue'),defaults={'index':1})")
    student = stack["student_client"]
    assert student.post("/devbox/reset/").status_code == 302
    page = student.get("/")
    assert "Could not reset your devbox" not in page.text
    jobs = django_command(stack, "from core.models.rounds import HostJob\nimport json\n"
        "print(json.dumps(HostJob.objects.filter(kind='devbox_rebuild').count()))")
    assert jobs == 1
    assert student.post("/devbox/reset/").status_code == 302
    assert "already in progress" in student.get("/").text


@pytest.mark.parametrize("engine", ["chromium", "firefox", "webkit"])
def test_real_browsers_read_logs_and_download_their_own_zip(stack, tmp_path, engine):
    driver = REPO / "tests/browser/dashboard_journey.js"
    output = Path(os.environ.get("WORMS_SCREENSHOTS", str(tmp_path))) / f"linux-stack-{engine}"
    result = subprocess.run(["node", str(driver)], cwd=REPO, input=json.dumps({
        "base": stack["https_url"], "student": "journey.alice@example.test", "password": stack["student_password"],
        "admin": stack["username"], "adminPassword": stack["password"], "output": str(output),
        "engine": engine, "fixtureTLS": True}), text=True, capture_output=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["errors"] == []


def test_simultaneous_first_logins_and_competing_team_joins(stack):
    admin = stack["admin_client"]
    assert admin.post("/staff/teams/", {"name": "Crowded team", "max_members": 3}).status_code == 302
    team = django_command(stack, "from core.models.team import Team\nimport json\n"
        "print(json.dumps(str(Team.objects.get(name='Crowded team').uuid)))")
    # Registration was closed by the round journey; open it for this cohort.
    django_command(stack, "from core.models.competition import ApplicationSetting\nfrom django.utils import timezone\n"
        "from datetime import timedelta\nc=ApplicationSetting.get_current_competition()\n"
        "c.registration_starts_at=timezone.now()-timedelta(days=1)\nc.registration_ends_at=timezone.now()+timedelta(days=1)\n"
        "c.save(update_fields=['registration_starts_at','registration_ends_at'])")
    response = admin.post("/staff/users/import/", {"csv_text": "email\n" + "\n".join(f"cohort{i}@example.test" for i in range(12))})
    assert response.status_code == 200
    import re
    one_time = re.findall(r'>(cohort\d+@example.test)</a></th><td class="secret">([^<]+)', response.text)
    assert len(one_time) == 12

    def onboard(item):
        email, otp = item
        student = Browser(stack)
        assert student.login(email, otp).status_code == 302
        assert student.get("/change-password/").status_code == 200
        password = "Concurrent-Student-7392!"
        assert student.post("/change-password/", {"new_password1": password, "new_password2": password}).status_code == 302
        assert student.post(f"/join_team/{team}").status_code == 302
        assert student.get("/profile/").status_code == 200
        return student

    with ThreadPoolExecutor(max_workers=12) as pool:
        students = list(pool.map(onboard, one_time))
    assert len(students) == 12
    counts = django_command(stack, "from core.models.team import Team\nimport json\n"
        "t=Team.objects.get(name='Crowded team')\nprint(json.dumps([t.users.count(),t.max_members]))")
    assert counts == [3, 3]
    # Close registration again for subsequent Live progression/recovery checks.
    django_command(stack, "from core.models.competition import ApplicationSetting\nfrom django.utils import timezone\n"
        "from datetime import timedelta\nc=ApplicationSetting.get_current_competition()\n"
        "c.registration_ends_at=timezone.now()-timedelta(minutes=1)\nc.save(update_fields=['registration_ends_at'])")


def children_of(parent):
    result = set()
    for status in Path("/proc").glob("[0-9]*/status"):
        try:
            fields = dict(line.split(":", 1) for line in status.read_text().splitlines() if ":" in line)
            if int(fields["PPid"]) == parent:
                result.add(int(status.parent.name))
        except (OSError, KeyError, ValueError):
            pass
    return result


def test_gunicorn_replaces_a_killed_worker_without_losing_the_session(stack):
    parent = stack["gunicorn_pid"]
    workers = children_of(parent)
    assert len(workers) == 3
    os.kill(next(iter(workers)), signal.SIGKILL)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        current = children_of(parent)
        if len(current) == 3 and current != workers:
            break
        time.sleep(0.1)
    assert len(current) == 3 and current != workers
    for _ in range(6):
        assert stack["student_client"].get("/profile/").status_code == 200


def test_database_restart_recovers_http_sessions_and_attempt_processing(stack):
    bindir = pg.pg_bindir()
    data = str(Path(stack["database_directory"]) / "data")
    command = [str(Path(bindir) / "pg_ctl"), "-D", data, "-w"]
    if os.geteuid() == 0:
        command = ["runuser", "-u", "nobody", "--", *command]
    subprocess.run([*command, "-m", "fast", "stop"], check=True, capture_output=True)
    try:
        unavailable = Browser(stack).get("/api/v1/rounds/")
        assert unavailable.status_code == 503
        time.sleep(1)  # make the real daemon observe at least one failed pass
    finally:
        options = (f"-p {stack['database_port']} -k {stack['database_directory']} "
                   "-c listen_addresses=127.0.0.1 -c fsync=off")
        subprocess.run([*command, "-l", str(Path(stack["database_directory"]) / "log"),
                        "-o", options, "start"], check=True, capture_output=True)
    assert stack["student_client"].get("/profile/").status_code == 200
    assert Browser(stack).get("/api/v1/rounds/").status_code == 401
    team = django_command(stack, "from core.models.team import Team\nfrom core.models.rounds import RoundRun\n"
        "from django.utils import timezone\nfrom datetime import timedelta\nimport json\n"
        "RoundRun.objects.update(ends_at=timezone.now()+timedelta(hours=1))\n"
        "print(json.dumps(str(Team.objects.get(name='Journey Blue').uuid)))")
    before = django_command(stack, "from core.models.logging import Logging\nimport json\nprint(json.dumps(Logging.objects.count()))")
    ingest(stack, "logging", team, {"log_data": {"event": "after database restart"}})
    deadline = time.monotonic() + 12
    count = before
    while time.monotonic() < deadline:
        count = django_command(stack, "from core.models.logging import Logging\nimport json\nprint(json.dumps(Logging.objects.count()))")
        if count == before + 1:
            break
        time.sleep(0.2)
    assert count == before + 1, "attempt processor did not reconnect after PostgreSQL restarted"


def test_production_sign_in_and_api_limits_are_enforced(stack):
    site = Path(stack["work_directory"]) / "site.conf"
    config = site.read_text()
    assert "rate=6000r/s" in config
    site.write_text(config.replace("rate=6000r/s", "rate=10r/m"))
    subprocess.run(["nginx", "-t", "-c", stack["nginx_config"]], check=True, capture_output=True)
    subprocess.run(["nginx", "-c", stack["nginx_config"], "-s", "reload"], check=True, capture_output=True)
    time.sleep(1)

    def sign_in(_):
        client = Browser(stack)
        client.get("/login/")
        return client.post("/login/", {"username": "does-not-exist", "password": "wrong"}).status_code

    with ThreadPoolExecutor(max_workers=12) as pool:
        statuses = list(pool.map(sign_in, range(18)))
    assert 429 in statuses and set(statuses) <= {401, 429}
    client = Browser(stack)
    with ThreadPoolExecutor(max_workers=12) as pool:
        statuses = list(pool.map(lambda _: client.get("/api/v1/rounds/").status_code, range(40)))
    assert 429 in statuses and set(statuses) <= {401, 429}
