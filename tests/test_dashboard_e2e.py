"""A student's complete journey over real HTTPS, production middleware and PG grants.

Only run/VM records are fixture data (there is no Proxmox here). Staff setup,
account onboarding, team assignment, uploads, API ingestion, processing,
permissions and downloads go through their real application entry points.
"""

from datetime import datetime, timedelta, timezone
import html
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import zipfile

import pytest
import requests

from conftest import REPO
import pg


@pytest.fixture
def local_dashboard(tmp_path):
    if pg.pg_bindir() is None:
        pytest.skip("PostgreSQL server binaries are required")
    work = tmp_path / "dashboard"
    log_path = tmp_path / "dashboard.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [sys.executable, str(REPO / "scripts/dashboard_e2e.py"),
             "--work-dir", str(work), "--http-port", "0", "--https-port", "0"],
            cwd=REPO, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 60
            while not (work / "session.json").exists():
                assert process.poll() is None, log_path.read_text()
                assert time.monotonic() < deadline, log_path.read_text()
                time.sleep(0.1)
            yield json.loads((work / "session.json").read_text())
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    assert not Path(json.loads((work / "session.json").read_text())["database_directory"]).exists()


class Browser:
    def __init__(self, config):
        self.base = config["https_url"]
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.verify = config["certificate"]

    def get(self, path, **kwargs):
        return self.session.get(self.base + path, timeout=15, allow_redirects=False, **kwargs)

    def post(self, path, data=None, *, files=None, csrf=True, origin=None):
        headers = {"Origin": origin or self.base, "Referer": self.base + path}
        if csrf:
            headers["X-CSRFToken"] = self.session.cookies.get("csrftoken", "")
        return self.session.post(self.base + path, data=data, files=files, headers=headers,
                                 timeout=15, allow_redirects=False)

    def login(self, username, password):
        assert self.get("/login/").status_code == 200
        return self.post("/login/", {"username": username, "password": password})


def django_command(config, code, *, role="postgres"):
    # No inherited live settings; credentials below are disposable local roles.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("DB_", "WORMS_", "PROXMOX_", "ORCHESTRATOR_", "DJANGO_"))}
    env.update(
        PRODUCTION="1", DJANGO_SETTINGS_MODULE="config.settings.orchestrator",
        DJANGO_SECRET_KEY="local-fixture-only", DJANGO_ALLOWED_HOSTS="localhost",
        DB_HOST="127.0.0.1", DB_PORT=str(config["database_port"]), DB_NAME="mydb",
        DB_USER=role, DB_PASSWORD=pg.PASSWORD, DB_SSLMODE="disable",
        DB_MIGRATOR_USER=pg.MIGRATOR, DB_MIGRATOR_PASSWORD=pg.PASSWORD,
        PROXMOX_HOST="unused.invalid", PROXMOX_USER="unused", PROXMOX_TOKEN_NAME="unused",
        PROXMOX_TOKEN_VALUE="unused",
    )
    result = subprocess.run([sys.executable, "-c", "import django; django.setup()\n" + code],
                            cwd=REPO / "projects/dashboard", env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout) if result.stdout.strip() else None


def credentials(response):
    assert response.status_code == 200
    match = re.search(r'/staff/users/([0-9a-f-]+)/.*?class="secret">([^<]+)', response.text, re.S)
    assert match, response.text[-2000:]
    assert "no-store" in response.headers["Cache-Control"]
    return match[1], html.unescape(match[2])


def ingest(config, kind, team, payload):
    """Actual loopback HTTP to the Flask service, as its INSERT-only DB role."""
    code = '''
import importlib.util, os, threading
from pathlib import Path
import requests
from werkzeug.serving import make_server
work = Path(CONFIG['work_directory'])
current = work / ('current-team-' + KIND)
current.write_text(TEAM)
os.environ.update(SANDBOX_API_KIND=KIND, CURRENT_TEAM_FILE=str(current),
                  DEVBOX_TEAMS_FILE=str(work / 'no-devbox-map'), CLIENT_IP_HEADER='X-Client-IP',
                  SANDBOX_GW_IP='10.2.0.2')
spec = importlib.util.spec_from_file_location('local_sandbox_api', APP)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
server = make_server('127.0.0.1', 0, module.app)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    session = requests.Session()
    session.trust_env = False
    r = session.post(f'http://127.0.0.1:{server.server_port}/submit', json=PAYLOAD,
                     headers={'X-Client-IP': '10.2.0.2'}, timeout=10)
    assert r.status_code == 201, (r.status_code, r.text)
finally:
    server.shutdown()
    server.server_close()
'''
    constants = (f"CONFIG = {{'work_directory': {config['work_directory']!r}}}\nKIND = {kind!r}\nTEAM = {team!r}\nPAYLOAD = {payload!r}\n"
                 f"APP = {str(REPO / 'ansible/roles/applications/sandbox_api/files/app.py')!r}\n")
    django_command(config, constants + code, role=f"dbuser_{kind}")


def exercise_student_journey(config):
    admin = Browser(config)
    assert admin.login(config["username"], config["password"]).status_code == 302
    assert admin.session.cookies.get("sessionid")
    assert all(cookie.secure for cookie in admin.session.cookies if cookie.name in ("sessionid", "csrftoken"))
    assert admin.post("/logout/", csrf=False).status_code == 403
    assert admin.post("/logout/", origin="https://other.invalid").status_code == 403
    assert admin.get("/debug/populate/").status_code == 404

    now = datetime.now(timezone.utc)
    stamp = lambda dt: dt.strftime("%Y-%m-%dT%H:%M")
    assert admin.post("/staff/", {"name": "Local E2E Competition",
        "registration_starts_at": stamp(now - timedelta(days=1)),
        "registration_ends_at": stamp(now + timedelta(days=1))}).status_code == 302
    teams = {}
    for name, capacity in (("Journey Red", 1), ("Journey Blue", 2)):
        r = admin.post("/staff/teams/", {"name": name, "max_members": capacity})
        assert r.status_code == 302
        teams[name] = r.headers["Location"].split("/")[-2]
    ids = django_command(config, "from core.models.team import Team\nimport json\n"
                         "print(json.dumps(dict(Team.objects.values_list('name', 'pk'))))")
    alice_uuid, otp = credentials(admin.post("/staff/users/", {"email": "journey.alice@example.test"}))
    credentials(admin.post("/staff/users/", {
        "email": "journey.bob@example.test", "team": ids["Journey Red"]}))
    long_email = "student@" + ".".join(["a" * 63, "b" * 63, "c" * 63]) + ".test"
    imported = admin.post("/staff/users/import/", {
        "csv_text": f"email,team\njourney.carol@example.test,Journey Blue\n{long_email},\n"})
    credentials(imported)
    assert "150" in imported.text and "skipped" in imported.text
    duplicate = admin.post("/staff/users/", {"email": "JOURNEY.ALICE@example.test"})
    assert duplicate.status_code == 200 and "already exists" in duplicate.text

    alice = Browser(config)
    assert alice.login("journey.alice@example.test", "wrong").status_code == 401
    assert alice.login("journey.alice@example.test", otp).status_code == 302
    for path in ("/profile/", "/teams/", "/upload/"):
        r = alice.get(path)
        assert r.status_code == 302 and r.headers["Location"] == "/change-password/"
    assert alice.get("/change-password/").status_code == 200
    bad = alice.post("/change-password/", {"new_password1": "123", "new_password2": "456"})
    assert bad.status_code == 200 and "match" in bad.text
    password = "Local-Journey-Student-7392!"
    assert alice.post("/change-password/", {"new_password1": password, "new_password2": password}).status_code == 302
    assert alice.get("/profile/").status_code == 200  # password rotation retained session
    assert alice.get("/staff/").status_code == 302
    full = alice.post(f"/join_team/{teams['Journey Red']}")
    assert full.status_code == 302
    assert "is full" in alice.get("/teams/").text
    assert alice.post(f"/join_team/{teams['Journey Blue']}").status_code == 302
    assert alice.post(f"/leave_team/{teams['Journey Blue']}").status_code == 302
    assert alice.post(f"/join_team/{teams['Journey Blue']}").status_code == 302
    # Staff moves use both interfaces, and keep the student in just one team.
    assert admin.post(f"/staff/users/{alice_uuid}/team/", {"team": ""}).status_code == 302
    alice_id = django_command(config, "from core.models.user import User\nimport json\n"
                             "print(json.dumps(User.objects.get(username='journey.alice@example.test').pk))")
    assert admin.post(f"/staff/teams/{teams['Journey Blue']}/members/", {"user": alice_id}).status_code == 302

    assert alice.get("/upload/").status_code == 200
    bad_zip = alice.post("/upload/", files={"artifact": ("broken.zip", b"not zip", "application/zip")})
    assert bad_zip.status_code == 302 and "not a .zip archive" in alice.get("/upload/").text
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("main.py", "print('local fixture; never executed')\n")
    data_zip = buffer.getvalue()
    for _ in range(2):
        assert alice.post("/upload/", files={"artifact": ("worm.zip", data_zip, "application/zip")}).status_code == 302
        assert "Uploaded worm.zip" in alice.get("/upload/").text
    assert len(list(Path(config["media_root"]).rglob("*.zip"))) == 1

    start = now + timedelta(days=1)
    data = {"environment": "live", "name": "Journey Round", "round_start": stamp(start),
        "round_end": stamp(start + timedelta(hours=8)), "seed": 123,
        "vms": 8, "networks": 4, "sites": 1, "max_interfaces": 3, "vm_ram": 512,
        "worm_start_delay": 5, "run_duration": 60, "initial_fraction": 0.5,
        "first_stop_after": 10, "stop_interval": 10, "min_alive": 2, "restart_after": ""}
    invalid = admin.post("/staff/rounds/", {**data, "round_end": stamp(start - timedelta(hours=1))})
    assert invalid.status_code == 200 and "end after" in invalid.text
    r = admin.post("/staff/rounds/", data)
    assert r.status_code == 302, r.text
    round_id = int(r.headers["Location"].split("/")[-2])
    assert admin.get(r.headers["Location"]).status_code == 200
    # Substitute a running sandbox: no host job or infrastructure operation.
    fixture = django_command(config, f'''
import hashlib, json
from datetime import timedelta
from django.utils import timezone
from core.models.competition import ApplicationSetting, Round
from core.models.rounds import RoundRun, RunEvent
from core.models.team import Team
from core.models.vm import VirtualMachine
now = timezone.now()
comp = ApplicationSetting.get_current_competition()
comp.registration_ends_at = now - timedelta(minutes=1)
comp.save(update_fields=['registration_ends_at'])
r = Round.objects.get(pk={round_id})
r.round_start, r.round_end = now - timedelta(minutes=5), now + timedelta(hours=1)
r.save(update_fields=['round_start', 'round_end'])
out = {{}}
for position, name in enumerate(['Journey Blue', 'Journey Red'], start=1):
    team = Team.objects.get(name=name)
    run = RoundRun.objects.create(round=r, team=team, position=position, seed=123, state='running',
        worm_start_at=now-timedelta(minutes=1), ends_at=now+timedelta(minutes=10))
    flag = 'FLAG{{local-only-' + name + '}}'
    vm = VirtualMachine.objects.create(round=r, run=run, vm_identifier='fixture-'+name,
        vm_maintenance_ip='192.0.2.10', ctf_flag_hash=hashlib.sha256(flag.encode()).hexdigest(),
        addresses=['10.64.0.10'], networks=[])
    RunEvent.objects.create(run=run, ts=now-timedelta(minutes=1), kind='worm_started')
    out[name] = {{'team': str(team.uuid), 'run': str(run.uuid), 'flag': flag}}
print(json.dumps(out))
''')
    blue, red = fixture["Journey Blue"], fixture["Journey Red"]
    payload = {"message": "local log ü 🐛", "nested": {"values": [1, True, None]},
               "html": "<img src=x onerror=alert(1)>"}
    ingest(config, "logging", blue["team"], {"log_data": payload})
    ingest(config, "logging", red["team"], {"log_data": {"private": "RED-ONLY-MARKER"}})
    ingest(config, "scoring", blue["team"], {"vm_flag": blue["flag"]})
    ingest(config, "scoring", red["team"], {"vm_flag": red["flag"]})
    if not config.get("attempt_processor"):
        django_command(config, "from django.core.management import call_command\ncall_command('process_attempts', once=True)",
                       role="django_orchestrator")
    else:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            count = django_command(config, "from core.models.logging import Logging\nimport json\nprint(json.dumps(Logging.objects.count()))")
            if count == 2:
                break
            time.sleep(0.2)
    counts = django_command(config, "from core.models.scoring import FlagCapture\n"
                            "from core.models.logging import Logging\nimport json\n"
                            "print(json.dumps([FlagCapture.objects.count(), Logging.objects.count()]))")
    assert counts == [2, 2]
    assert alice.get("/").status_code == 200
    assert alice.get("/progression/").status_code == 200
    doc = alice.get(f"/progression/rounds/{round_id}/")
    assert doc.status_code == 200, doc.text
    assert doc.json()["logs"]["count"] == 1
    refs = django_command(config, "from core.models.logging import LoggingAttempt\nimport json\n"
                          "print(json.dumps(dict(LoggingAttempt.objects.values_list('team_uuid', 'uuid')), default=str))")
    base = f"/progression/rounds/{round_id}"
    own = alice.get(f"{base}/logs/{refs[blue['team']]}/")
    assert own.status_code == 200 and own.json()["data"] == payload
    assert alice.get(f"{base}/logs/{refs[red['team']]}/").status_code == 404
    assert alice.get(f"/rounds/{red['run']}/progression/").status_code == 403
    for path in ("/staff/rounds/999999/", "/progression/rounds/999999/", f"{base}/logs/not-a-uuid/"):
        client = admin if path.startswith("/staff/") else alice
        assert client.get(path).status_code == 404
    download = alice.get(f"{base}/download/")
    assert download.status_code == 200 and download.headers["Content-Type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        assert json.loads(archive.read(f"logs/{refs[blue['team']]}.json"))["data"] == payload
        assert f"logs/{refs[red['team']]}.json" not in archive.namelist()
        assert "RED-ONLY-MARKER" not in "\n".join(archive.read(n).decode() for n in archive.namelist())
    assert alice.get(f"{base}/download/?logs=invalid").status_code == 400
    assert admin.get(f"/rounds/{blue['run']}/progression/").status_code == 200
    assert admin.get(f"/rounds/{blue['run']}/progression/download/").status_code == 200
    # Deactivation ends existing sessions; a reset invalidates the old password.
    assert admin.post(f"/staff/users/{alice_uuid}/active/", {"active": "0"}).status_code == 302
    assert alice.get("/profile/").status_code == 302
    assert admin.post(f"/staff/users/{alice_uuid}/active/", {"active": "1"}).status_code == 302
    _, reset = credentials(admin.post(f"/staff/users/{alice_uuid}/password/"))
    assert Browser(config).login("journey.alice@example.test", password).status_code == 401
    assert Browser(config).login("journey.alice@example.test", reset).status_code == 302


def test_student_journey_over_https(local_dashboard):
    exercise_student_journey(local_dashboard)


def test_replay_and_download_in_a_real_browser(local_dashboard, tmp_path):
    import shutil

    if not shutil.which("node") or subprocess.run(
        ["node", "-e", "require('playwright')"], cwd=REPO, capture_output=True,
    ).returncode:
        pytest.skip("Node.js Playwright is required for the browser rehearsal")
    config = local_dashboard
    exercise_student_journey(config)
    # Reset this synthetic account after the lifecycle's reset test, then
    # complete its password form over verified HTTPS for the browser login.
    admin = Browser(config)
    assert admin.login(config["username"], config["password"]).status_code == 302
    users = django_command(config, "from core.models.user import User\nimport json\n"
                           "print(json.dumps(dict(User.objects.values_list('username', 'uuid')), default=str))")
    _, otp = credentials(admin.post(f"/staff/users/{users['journey.alice@example.test']}/password/"))
    student = Browser(config)
    assert student.login("journey.alice@example.test", otp).status_code == 302
    assert student.get("/change-password/").status_code == 200
    password = "Local-Browser-Rehearsal-7392!"
    assert student.post("/change-password/", {"new_password1": password, "new_password2": password}).status_code == 302
    output = Path(os.environ.get("WORMS_SCREENSHOTS", str(tmp_path))) / "dashboard-journey"
    result = subprocess.run(
        ["node", str(REPO / "tests/browser/dashboard_journey.js")], cwd=REPO,
        input=json.dumps({"base": config["http_url"], "student": "journey.alice@example.test",
                          "password": password, "admin": config["username"],
                          "adminPassword": config["password"], "output": str(output)}),
        text=True, capture_output=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads(result.stdout)
    assert observed["errors"] == []
    with zipfile.ZipFile(observed["archive"]) as archive:
        logs = [json.loads(archive.read(name)) for name in archive.namelist() if name.startswith("logs/")]
        assert len(logs) == 1 and logs[0]["data"]["message"] == "local log ü 🐛"
