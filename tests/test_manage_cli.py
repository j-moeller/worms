"""The `worms` management CLI (src/manage.py), DB-backed commands against a
real PostgreSQL with the real migrations and grants (tests/pg.py).

The infrastructure probes (Proxmox, SSH) are not exercised here -- they degrade
to `n/a`/`unknown` without configuration, which is covered by the unit check of
the unconfigured status output below.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from pg import DB_NAME, MIGRATOR, PASSWORD, REPO  # noqa: E402

MANAGE = REPO / "src" / "manage.py"


def worms(cluster, *args, check=True, python=False):
    """Run `worms <args>` with the CLI pointed at the test cluster as the
    migrator (owns the tables: can read all and manage teams); `python`: Python
    with <args> in that environment instead."""
    env = dict(
        os.environ,
        WORMS_DB_HOST="127.0.0.1",
        WORMS_DB_PORT=str(cluster.port),
        WORMS_DB_NAME=DB_NAME,
        WORMS_DB_USER=MIGRATOR,
        WORMS_DB_PASSWORD=PASSWORD,
        WORMS_DB_SSLMODE="disable",
        NO_COLOR="1",
        # Its run logs (~/.worms/runs) go with the test cluster, not into the operator's.
        HOME=str(cluster.dir),
    )
    proc = subprocess.run(
        [sys.executable, *([] if python else [str(MANAGE)]), *args],
        env=env, capture_output=True, text=True,
    )  # fmt: skip
    if check:
        assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc


def sql(cluster, query, params=None, role="postgres"):
    conn = cluster.connect(role, PASSWORD)
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall() if cur.description else None
    finally:
        conn.close()


@pytest.fixture
def initialised(database):
    """A database with the admin + default competition (manage.py init)."""
    env = dict(
        os.environ,
        PRODUCTION="1",
        DJANGO_SETTINGS_MODULE="config.settings.orchestrator",
        DJANGO_SECRET_KEY="t", DJANGO_ALLOWED_HOSTS="localhost",
        DB_MIGRATOR_USER=MIGRATOR, DB_MIGRATOR_PASSWORD=PASSWORD,
        PROXMOX_HOST="x", PROXMOX_USER="x", PROXMOX_TOKEN_NAME="x", PROXMOX_TOKEN_VALUE="x",
        **database.env("django_orchestrator"),
    )  # fmt: skip
    subprocess.run(
        [sys.executable, "manage.py", "init", "--competition", "WORMS CLI", "--skip-checks"],
        cwd=REPO / "projects" / "dashboard", env=env, capture_output=True, text=True, check=True,
    )
    return database


def test_help_runs_without_db():
    proc = subprocess.run([sys.executable, str(MANAGE), "--help"], capture_output=True, text=True)
    assert proc.returncode == 0 and "worms" in proc.stdout


def test_status_degrades_without_config(tmp_path):
    # A checkout without a vault (a developer's own checkout may have one).
    proc = subprocess.run(
        [sys.executable, str(MANAGE), "status"],
        capture_output=True, text=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("WORMS_")}
        | {"NO_COLOR": "1", "WORMS_REPO_ROOT": str(tmp_path), "HOME": str(tmp_path)},
    )
    assert proc.returncode == 0
    assert "no Proxmox API configured" in proc.stdout
    assert "no database configured" in proc.stdout


def add_team(cluster, name):
    """A team in the current competition (teams are created on the dashboard,
    not by the CLI any more: dashboard/views/staff_teams.py)."""
    sql(cluster, "INSERT INTO teams (uuid, name, competition_id, max_members, created_at, devbox_version) "
        "SELECT gen_random_uuid(), %s, current_competition_id, 2, now(), 1 FROM application_setting "
        "WHERE key = 'current'", (name,), role=MIGRATOR)


def test_teams_listing_and_delete(initialised):
    cluster = initialised
    add_team(cluster, "Alpha")
    add_team(cluster, "Beta")
    listing = worms(cluster, "team")
    assert "Alpha" in listing.stdout and "Beta" in listing.stdout

    worms(cluster, "team", "delete", "Beta", "--yes")
    assert sql(cluster, "SELECT count(*) FROM teams WHERE name = 'Beta'")[0][0] == 0


def test_teams_are_created_on_the_dashboard_not_here(initialised):
    proc = worms(initialised, "create-team", "Nope", check=False)
    assert proc.returncode != 0
    assert "managed on the dashboard" in worms(initialised, "--help").stdout


def cli(cluster, call):
    """One of the CLI's functions (src/manage.py) in a process of its own, against the test
    cluster as `worms` sees it: for what `worms apply devbox` does after it has asked the host
    about the devboxes' dependencies (there is no host here)."""
    code = (f"import sys; sys.path.insert(0, {str(MANAGE.parent)!r}); import manage; "
            f"from worms_cli.config import Config; config = Config.load(); sys.exit(manage.{call})")  # fmt: skip
    return worms(cluster, "-c", code, python=True)


def test_devbox_provision_enqueues_a_host_job(initialised):
    """Building devboxes is infrastructure work: the console enqueues it for
    the round agent (the dashboard only creates the team)."""
    cluster = initialised
    add_team(cluster, "DevTeam")
    cli(cluster, "_build_devboxes(config)")
    # A Devbox row with a stable index + source address, and a provision job.
    row = sql(cluster, "SELECT index, services_source_ip FROM devboxes d "
              "JOIN teams t ON t.id = d.team_id WHERE t.name = 'DevTeam'")
    assert row and row[0][0] is not None and row[0][1].startswith("10.2.0.")
    jobs = sql(cluster, "SELECT count(*) FROM host_jobs WHERE kind = 'devbox_provision' AND state = 'queued'")[0][0]
    assert jobs >= 1
    # The same team list again: the queued job already covers it.
    cli(cluster, "_build_devboxes(config)")
    assert sql(cluster, "SELECT count(*) FROM host_jobs WHERE kind = 'devbox_provision' AND state = 'queued'")[0][0] == jobs


def test_delete_team_keeps_users(initialised):
    cluster = initialised
    add_team(cluster, "WithMembers")
    # A user and a membership (as the table owner).
    sql(cluster, "INSERT INTO users (uuid, password, is_superuser, username, is_staff, is_active, "
        "date_joined, first_name, last_name, email, must_change_password, created_at) VALUES "
        "(gen_random_uuid(), 'x', false, 'stud@x', false, true, now(), '', '', 'stud@x', true, now())",
        role=MIGRATOR)
    sql(cluster, "INSERT INTO team_memberships (user_id, team_id, created_at) SELECT u.id, t.id, now() "
        "FROM users u, teams t WHERE u.username = 'stud@x' AND t.name = 'WithMembers'", role=MIGRATOR)

    worms(cluster, "team", "delete", "WithMembers", "--yes")
    assert sql(cluster, "SELECT count(*) FROM teams WHERE name = 'WithMembers'")[0][0] == 0
    # The user survives.
    assert sql(cluster, "SELECT count(*) FROM users WHERE username = 'stud@x'")[0][0] == 1


def test_status_and_leaderboard_with_db(initialised):
    cluster = initialised
    # (The DB is a shared session fixture, so the current competition name may
    # have been set by another test; assert structure, not a specific name.)
    add_team(cluster, "Solo")
    status = worms(cluster, "status", check=False)  # 1 when it found something (a failed job of another test)
    assert status.returncode in (0, 1), status.stderr
    assert "competition" in status.stdout
    assert "teams" in status.stdout

    board = worms(cluster, "show", "leaderboard")
    assert "leaderboard" in board.stdout
    assert "Solo" in board.stdout


def test_users_listing(initialised):
    cluster = initialised
    sql(cluster, "INSERT INTO users (uuid, password, is_superuser, username, is_staff, is_active, "
        "date_joined, first_name, last_name, email, must_change_password, created_at) VALUES "
        "(gen_random_uuid(), 'x', false, 'newbie@x', false, true, now(), '', '', 'newbie@x', true, now())",
        role=MIGRATOR)
    out = worms(cluster, "user").stdout
    assert "newbie@x" in out and "never" in out


def test_reset_database(initialised):
    cluster = initialised
    add_team(cluster, "Doomed")
    assert sql(cluster, "SELECT count(*) FROM teams")[0][0] >= 1
    worms(cluster, "db", "reset", "--yes")
    # Teams wiped; init recreated the admin + default competition.
    assert sql(cluster, "SELECT count(*) FROM teams")[0][0] == 0
    assert sql(cluster, "SELECT count(*) FROM users WHERE username = 'admin'")[0][0] == 1
    assert sql(cluster, "SELECT count(*) FROM competitions")[0][0] == 1


# --- configuration defaults (no database needed) ---------------------------------


def _cli_module():
    import importlib.util

    sys.path.insert(0, str(REPO / "src"))
    spec = importlib.util.spec_from_file_location("worms_manage", MANAGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_internet_probe_runs_locally_on_the_host(tmp_path, monkeypatch):
    """Without WORMS_SSH the CLI is on the host (the build steps run locally
    too), so `status` reads the host flag directly."""
    cli = _cli_module()
    from worms_cli.config import Config

    config = Config(env={})
    monkeypatch.setattr(cli, "PVE_CONFIG_DIR", str(tmp_path / "missing"))
    assert "set WORMS_SSH" in cli._internet_status(config)[1]
    monkeypatch.setattr(cli, "PVE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "INTERNET_FLAG", str(tmp_path / "internet-allowed"))
    assert "denied" in cli._internet_status(config)[1]
    (tmp_path / "internet-allowed").touch()
    assert "ALLOWED" in cli._internet_status(config)[1]


def test_database_ca_defaults_to_the_internal_ca(tmp_path):
    sys.path.insert(0, str(REPO / "src"))
    from worms_cli.config import Config

    config = Config(env={"WORMS_REPO_ROOT": str(tmp_path), "WORMS_DB_HOST": "10.11.0.16"})
    assert config.db_env()["WORMS_DB_SSLROOTCERT"] == "/etc/ssl/certs/ca-certificates.crt"
    ca = tmp_path / "ansible" / "pki" / "worms" / "ca.crt"
    ca.parent.mkdir(parents=True)
    ca.write_text("x")
    assert config.db_env()["WORMS_DB_SSLROOTCERT"] == str(ca)
    assert config.db_env()["WORMS_DB_USER"] == "admin" and config.db_env()["WORMS_DB_NAME"] == "mydb"


def test_proxmox_url_keeps_its_port(monkeypatch):
    import types

    sys.path.insert(0, str(REPO / "src"))
    from worms_cli.config import Config
    from worms_cli.proxmox import Infra

    seen = {}
    fake = types.ModuleType("proxmoxer")
    fake.ProxmoxAPI = lambda host, **kw: seen.update(host=host, **kw) or object()
    monkeypatch.setitem(sys.modules, "proxmoxer", fake)
    for url, host, port in [("https://10.11.0.1:8006/", "10.11.0.1", 8006), ("https://pve.lab:9443", "pve.lab", 9443),
                            ("127.0.0.1", "127.0.0.1", 8006)]:
        seen.clear()
        # The host named: else it is read from this checkout's platform state, which a test has not.
        Infra(Config(env={"WORMS_PROXMOX_URL": url, "WORMS_PROXMOX_USER": "topology@pve", "WORMS_PROXMOX_NODE": "pve",
                          "WORMS_PROXMOX_TOKEN_NAME": "readonly", "WORMS_PROXMOX_TOKEN_VALUE": "x"})).api()
        assert (seen["host"], seen["port"]) == (host, port), url
        assert seen["user"] == "topology@pve" and seen["token_name"] == "readonly"


def test_worms_db_settings_win_over_a_service_env(initialised):
    """DB_* left in the shell (e.g. a sourced service env file) must not send
    the CLI to another database: WORMS_DB_* win."""
    import os as _os

    stale = dict(DB_HOST="192.0.2.1", DB_PORT="1", DB_NAME="elsewhere", DB_SSLMODE="require")
    old = {k: _os.environ.get(k) for k in stale}
    _os.environ.update(stale)
    try:
        out = worms(initialised, "status", check=False).stdout  # exit 1: it found something, not an error
        # Reached the test database (192.0.2.1:1 would not answer).
        assert "competition  ● WORMS" in out and "error" not in out.lower()
    finally:
        for k, v in old.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v



# --- admin one-time password ---------------------------------------------------


def admin_row(cluster):
    return sql(cluster, "SELECT password, must_change_password, is_superuser, is_staff FROM users "
               "WHERE username = 'admin'")  # fmt: skip


def check_password(password, encoded):
    """Django's default PBKDF2 hash, checked without configuring Django here."""
    import base64
    import hashlib
    import hmac

    algorithm, iterations, salt, digest = encoded.split("$")
    assert algorithm == "pbkdf2_sha256"
    computed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations))
    return hmac.compare_digest(base64.b64encode(computed).decode(), digest)


def test_init_leaves_the_admin_without_a_password(initialised):
    encoded, must_change, *_ = admin_row(initialised)[0]
    assert encoded.startswith("!")  # Django's unusable password: login disabled


def test_admin_reset_password_prints_a_one_time_password_once(initialised):
    proc = worms(initialised, "user", "password")
    password = proc.stdout.strip()
    assert len(password) == 20 and "\n" not in password
    assert "shown once" in proc.stderr and password not in proc.stderr
    encoded, must_change, is_superuser, is_staff = admin_row(initialised)[0]
    assert check_password(password, encoded) and password not in encoded
    assert must_change and is_superuser and is_staff

    # A changed (real) password is only replaced after confirmation.
    sql(initialised, "UPDATE users SET must_change_password = false WHERE username = 'admin'", role=MIGRATOR)
    refused = worms(initialised, "user", "password", check=False)
    assert refused.returncode != 0 and admin_row(initialised)[0][0] == encoded
    again = worms(initialised, "user", "password", "--yes").stdout.strip()
    assert again != password and check_password(again, admin_row(initialised)[0][0])


def test_admin_reset_password_creates_a_missing_admin(initialised):
    sql(initialised, "DELETE FROM users WHERE username = 'admin'", role=MIGRATOR)
    assert worms(initialised, "user", "password").stdout.strip()
    assert admin_row(initialised)[0][1:] == (True, True, True)


def test_admin_reset_password_never_promotes_a_student(initialised):
    sql(initialised, "INSERT INTO users (uuid, password, is_superuser, username, is_staff, is_active, "
        "date_joined, first_name, last_name, email, must_change_password, created_at) VALUES "
        "(gen_random_uuid(), 'x', false, 'stud@x', false, true, now(), '', '', 'stud@x', false, now())",
        role=MIGRATOR)  # fmt: skip
    proc = worms(initialised, "user", "password", "stud@x", "--yes", check=False)
    assert proc.returncode != 0 and "not an admin account" in proc.stderr
    assert sql(initialised, "SELECT is_superuser, password FROM users WHERE username = 'stud@x'") == [(False, "x")]


def test_admin_create_staff_prints_a_one_time_password(initialised):
    """Staff (tutor) accounts are made on the console: the dashboard's role
    cannot create one (setup_db_permissions)."""
    cluster = initialised
    proc = worms(cluster, "user", "add", "tutor@uni.example", "--first-name", "Tu")
    password = proc.stdout.strip()
    assert len(password) >= 10 and "\n" not in password
    row = sql(cluster, "SELECT is_staff, is_superuser, must_change_password, first_name FROM users "
              "WHERE username = 'tutor@uni.example'")
    assert row == [(True, False, True, "Tu")]
    again = worms(cluster, "user", "add", "tutor@uni.example", check=False)
    assert again.returncode != 0 and "already exists" in again.stderr


# --- the help: every command under what it is for (no database needed) -------------


def run_cli(*args):
    proc = subprocess.run(
        [sys.executable, str(MANAGE), *args],
        capture_output=True, text=True, env=dict(os.environ, NO_COLOR="1", COLUMNS="80"),
    )  # fmt: skip
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def sections_of(text):
    """{title: its text} of the `── title` parts of a help output."""
    return {part.split("\n", 1)[0].strip(): part for part in text.split("── ")[1:]}


def commands_of(parser, path=()):
    """Every command that runs as typed: `teams` lists, `admin` alone is refused."""
    subs = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
    if path and not (subs and subs[0].required):
        yield " ".join(path), parser
    for sub in subs:
        for name, child in sub.choices.items():
            yield from commands_of(child, (*path, name))


def test_help_groups_the_commands_by_what_they_are_for():
    sections = sections_of(run_cli("--help"))
    assert list(sections) == ["look", "reach", "deploy", "machines", "operate", "data and accounts", "set up"]
    # Looking comes first, in this order; then reaching a VM.
    cli = _cli_module()
    looks = [usage.split()[0] for usage, _ in cli.SECTIONS[0].commands]
    assert list(dict.fromkeys(looks)) == ["status", "show", "log", "check", "trace"]
    assert "apply <component>..." in sections["deploy"] and "secret issue <name>" in sections["set up"]
    assert "key add <team> <key>" in sections["data and accounts"] and "user add <email>" in sections["data and accounts"]
    assert "vm reset services" in sections["machines"] and "jobs pause" in sections["operate"]
    # A noun's verbs are its own commands; alone, a noun lists (`worms jobs`).
    assert cli._command_name("vm reset <team>...|devboxes") == "vm reset" and cli._command_name("show job <n>") == "show"
    assert {usage.split()[0] for section in cli.SECTIONS for usage, _ in section.commands} >= set(cli.NOUNS)
    # One line a command, and no flat list of argparse beside it.
    assert "positional arguments" not in run_cli("--help")
    assert not [line for line in run_cli("--help").splitlines() if line.startswith("  ") and len(line) > 79]
    # The commands of the CLI before are gone: refused, not rewritten.
    for old in (["configure"], ["close", "window"], ["reset", "alpha"], ["logs"], ["release"]):
        proc = subprocess.run([sys.executable, str(MANAGE), *old], capture_output=True, text=True)
        assert proc.returncode == 2 and "invalid choice" in proc.stderr, old


def test_every_command_is_in_exactly_one_section(tmp_path, monkeypatch):
    cli = _cli_module()
    listed = {cli._command_name(usage) for section in cli.SECTIONS for usage, _ in section.commands} | set(cli.HIDDEN)
    assert sorted(listed) == sorted(name for name, _ in commands_of(cli.build_parser()))
    # Tab completion is generated from the same parsers: it offers every top-level command.
    script = run_cli("completion", "bash")
    offered = next(line for line in script.splitlines() if line.startswith("_WORMS_COMMANDS=")).split("=", 1)[1].strip("'").split()
    assert sorted(offered) == sorted({name.split()[0] for name, _ in commands_of(cli.build_parser())})
    # ... and never hands a word to `compgen -W`, which expands it: a team names itself, and a
    # name like $(...) would run as the operator. `__complete` offers safe words only.
    assert "compgen -W" not in script
    from worms_cli import completion, teams

    cache = tmp_path / "team-map.json"
    monkeypatch.setattr(completion, "TEAM_CACHE", str(cache))
    cache.write_text('{"teams": [{"slug": "t9", "name": "$(touch${IFS}x)"}, {"slug": "t3", "name": "alpha"},'
                     ' {"slug": "`id`", "name": "beta"}]}')  # fmt: skip
    words = completion.candidates("teams", str(tmp_path)) + completion.candidates("targets", str(tmp_path))
    assert {"t9", "alpha", "t9/a", "alpha/gw"} <= set(words)
    assert not [w for w in words if "$" in w or "`" in w or "beta" in w]
    monkeypatch.setattr(teams, "CACHE", str(cache))
    teams.write_cache({"t9": teams.Team("t9", "$(touch${IFS}x)"), "t3": teams.Team("t3", "alpha")})
    assert "$(" not in cache.read_text() and "alpha" in cache.read_text()


def test_build_help_lists_every_component_and_group_with_what_it_needs():
    from worms_cli import components

    sections = sections_of(run_cli("apply", "--help"))
    listed, groups = sections["components, in build order"], sections["groups"]
    for name, component in components.COMPONENTS.items():
        assert f"\n  {name} " in listed, name
        for dep in component.deps:
            assert dep in listed
    for name in components.GROUPS:
        assert f"\n  {name} " in groups, name


def test_every_command_explains_itself():
    cli = _cli_module()
    for name, parser in commands_of(cli.build_parser()):
        assert " ".join(parser.description.split()) == cli._command_help(name)
        assert parser.prog == f"worms {name}"


def test_help_fits_a_terminal():
    for args in (["--help"], ["apply", "--help"], ["show", "--help"]):
        too_long = [line for line in run_cli(*args).splitlines() if len(line) > 80]
        assert not too_long, (args, too_long)


def test_arguments_for_the_tools_go_after_a_double_dash():
    """What the help promises for `build` and `test`: and options before the `--` stay the command's."""
    cli = _cli_module()
    own, passed = cli._split(["apply", "dashboard", "--packages", "--", "--limit", "db"])
    args = cli.build_parser().parse_args(own)
    assert (args.components, args.packages, passed) == (["dashboard"], True, ["--limit", "db"])
    assert cli._split(["test", "--", "-k", "shipper"]) == (["test"], ["-k", "shipper"])


def test_a_command_is_still_required():
    proc = subprocess.run([sys.executable, str(MANAGE)], capture_output=True, text=True)
    assert proc.returncode == 2 and "usage: worms" in proc.stderr and "<command>" in proc.stderr


def test_there_is_no_dry_run():
    """It did not hold for every command: `teams delete` and `secrets issue` really ran."""
    proc = subprocess.run([sys.executable, str(MANAGE), "--dry-run", "status"], capture_output=True, text=True)
    assert proc.returncode == 2 and "--dry-run" in proc.stderr
    assert "dry-run" not in run_cli("--help")


# --- the internet notice: every command starts with it ------------------------------


def run_on(tmp_path, *args, **env):
    """The CLI in an empty checkout: it is on no host unless the test says so."""
    return subprocess.run(
        [sys.executable, str(MANAGE), *args], capture_output=True, text=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("WORMS_")}
        | {"NO_COLOR": "1", "WORMS_REPO_ROOT": str(tmp_path), "HOME": str(tmp_path), **env},
    )  # fmt: skip


def test_every_command_starts_with_the_internet_notice(tmp_path):
    for args in (["status"], ["secret"]):
        proc = run_on(tmp_path, *args)
        assert proc.returncode == 0, proc.stderr
        assert proc.stderr.splitlines()[0].startswith("internet access: "), args


def test_the_internet_notice_stays_off_stdout(tmp_path):
    """The stdout of `worms user password` is the password, and nothing else."""
    assert "internet access:" not in run_on(tmp_path, "secret").stdout


def test_help_is_not_a_command(tmp_path):
    assert run_on(tmp_path, "--help").stderr == ""


def test_the_internet_notice_says_what_it_found(tmp_path, monkeypatch, capsys):
    cli = _cli_module()
    from worms_cli.config import Config

    config = Config(env={})
    monkeypatch.setattr(cli, "PVE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "INTERNET_FLAG", str(tmp_path / "internet-allowed"))
    cli._internet_notice(config)
    assert "denied" in capsys.readouterr().err
    (tmp_path / "internet-allowed").touch()
    cli._internet_notice(config)
    err = capsys.readouterr().err
    assert "ALLOWED" in err and "worms internet off" in err


def test_the_internet_notice_never_stops_a_command(monkeypatch, capsys):
    cli = _cli_module()

    def broken(_config):
        raise RuntimeError("no route to host")

    monkeypatch.setattr(cli, "_internet_status", broken)
    cli._internet_notice(None)
    assert "no route to host" in capsys.readouterr().err


# --- a reset of the service VMs brings back the devbox map ---------------------------


def _fake_pipeline(run, map_failed=False):
    return lambda *_a, **_k: type("P", (), {
        "build_all": lambda self: run(self), "round_reset_services": lambda self: run(self),
        "devbox_map_failed": map_failed,
    })()  # fmt: skip


def _run(cli, command, config):
    """`worms apply --all` (everything) or `worms vm reset services`, confirmed."""
    if command == "build":
        args = argparse.Namespace(components=[], all=True, packages=False, plan=False, yes=True, ansible_args=[])
        return cli._apply_all(config, args)
    return cli.cmd_reset(config, argparse.Namespace(what="services", yes=True))


@pytest.mark.parametrize("command", ["reset services", "build"])
@pytest.mark.parametrize("map_failed", [False, True])
def test_a_rollback_whose_map_push_failed_exits_1(monkeypatch, tmp_path, command, map_failed):
    """The pipeline pushes the devbox map itself after a rollback of scoring or logging
    (worms_cli/pipeline.py, scripts/round_agent/devbox_map.py): no host job, no database. One
    that did not reach them leaves the reset done, and the command exits 1."""
    cli = _cli_module()
    from worms_cli import pipeline

    done = []
    monkeypatch.setattr(pipeline, "Pipeline", _fake_pipeline(lambda self: done.append(1), map_failed=map_failed))
    assert _run(cli, command, argparse.Namespace(repo_root=tmp_path)) == (1 if map_failed else 0)
    assert done == [1] and not hasattr(cli, "_request_devbox_reconfigure")


def _request_reconfigure(cluster):
    """What the dashboard asks for when a team's keys change (orchestrator/rounds/devbox.py): a
    reset of the services no longer does (the CLI pushes the host's map itself)."""
    code = (f"import sys; sys.path.insert(0, {str(MANAGE.parent)!r}); import manage; "
            "from worms_cli.config import Config; manage._with_db(Config.load()); "
            "from core.models.competition import ApplicationSetting; "
            "from orchestrator.rounds.devbox import request_reconfigure; "
            "job = request_reconfigure(ApplicationSetting.get_current_competition()); "
            "print('nothing to enqueue' if job is None else f'enqueued {job.uuid}')")  # fmt: skip
    return worms(cluster, "-c", code, python=True)


def test_devbox_reconfigure_enqueues_a_job_the_agent_accepts(initialised):
    """The agent validates a job's params; one it refuses changes nothing (keys, the map)."""
    cluster = initialised
    add_team(cluster, "Mapped")
    cli(cluster, "_build_devboxes(config)")
    _request_reconfigure(cluster)
    (params,), = sql(cluster, "SELECT params FROM host_jobs WHERE kind = 'devbox_reconfigure' AND state = 'queued'")
    sys.path.insert(0, str(REPO / "scripts"))
    from round_agent import validation

    assert validation.devbox_reconfigure(params)["teams"]
    # The same list again: the queued job already covers it.
    assert "nothing to enqueue" in _request_reconfigure(cluster).stdout


def test_devbox_reconfigure_without_devboxes_enqueues_nothing(initialised):
    assert "nothing to enqueue" in _request_reconfigure(initialised).stdout


# --- the service VMs' snapshots in `worms status` (worms_cli/consistency.py) -----------------


SNAPSHOT_VMS = [
    {"vmid": 111, "name": "scoring", "pool": "ephemeral", "status": "running"},
    {"vmid": 100, "name": "dns", "pool": "ephemeral", "status": "running"},
    {"vmid": 110, "name": "dashboard", "pool": "persistent", "status": "running"},
]
SNAPSHOT_LINES = ("/etc/pve/qemu-server/111.conf 1795362982 worms a3a66c9\n"
                  "/etc/pve/qemu-server/100.conf 1795362982 worms 9f1e2d0\n")  # fmt: skip


def _event(vmid, name, event, time):
    return {"time": time, "vmid": vmid, "name": name, "event": event, "commit": "a3a66c9", "by": "t", "detail": ""}


def test_a_service_vm_shows_its_snapshots_commit_and_age():
    sys.path.insert(0, str(REPO / "src"))
    from worms_cli import consistency

    snapshots = consistency.parse_snapshots(SNAPSHOT_LINES + "/etc/pve/qemu-server/119.conf 1795125786 worms devbox%3A x\n")
    assert snapshots[111] == {"time": 1795362982, "commit": "a3a66c9", "description": "worms a3a66c9"}
    assert snapshots[119]["commit"] == ""  # a devbox's, prose: no commit
    now = 1795362982 + 2 * 86400 + 60
    assert [consistency.snapshot_cell(vm, snapshots, now) for vm in SNAPSHOT_VMS] == ["a3a66c9 2 d", "9f1e2d0 2 d", ""]
    assert consistency.snapshot_cell({"vmid": 105, "name": "ntp", "pool": "private_internet"}, snapshots, now) == "—"


@pytest.mark.parametrize("deployed, events, rows", [
    ({"scoring": {"commit": "a3a66c9"}, "dns": {"commit": "9f1e2d0"}}, [], []),  # as deployed
    ({"scoring": {"commit": "a3a66c9-dirty"}}, [], []),  # -dirty is the same commit
    ({}, [], []),  # nothing recorded: unknown, not a finding
    ({"scoring": {"commit": "b6d380f"}, "dns": {"commit": "9f1e2d0"}}, [], ["snapshot behind deployed"]),
    ({}, [_event(111, "scoring", "configured", "2026-11-22T10:00:00Z")], []),  # before the snapshot
    ({}, [_event(111, "scoring", "configured", "2026-11-30T10:00:00Z")], ["configured after its snapshot"]),
    ({}, [_event(111, "scoring", "rolled_back", "2026-11-30T10:00:00Z")], []),  # a rollback is no configuration
    ({}, [_event(111, "other-vm", "packages", "2026-11-30T10:00:00Z")], []),  # another VM that had the vmid
    ({"dns": {"commit": "a3a66c9"}}, [_event(100, "dns", "packages", "2026-11-30T10:00:00Z")],
     ["snapshot behind deployed", "configured after its snapshot"]),
])  # fmt: skip
def test_the_snapshot_rows_appear_exactly_when_they_should(deployed, events, rows):
    sys.path.insert(0, str(REPO / "src"))
    from worms_cli import consistency

    found = consistency.snapshot_rows(SNAPSHOT_VMS, consistency.parse_snapshots(SNAPSHOT_LINES), deployed, events)
    assert [row.name for row in found] == rows
    assert all("worms apply <component>" in row.said() for row in found)


def test_a_login_onto_a_service_vm_says_its_changes_do_not_last(capsys):
    sys.path.insert(0, str(REPO / "src"))
    from worms_cli.vms import Catalogue

    catalogue = Catalogue.__new__(Catalogue)
    catalogue.service_notice([{"name": "dns", "pool": "ephemeral"}])
    assert "dns: changes made here are lost at the next reset or round build" in capsys.readouterr().err
    catalogue.service_notice([{"name": "dashboard", "pool": "persistent"}, None])
    assert capsys.readouterr().err == ""


# --- which commit this system runs: the version block (worms_cli/consistency.py) ---------------


def _version(checkout="a3a66c9", deployed=None, expected=("dns", "scoring", "workspace"), snapshots=None, running=None):
    sys.path.insert(0, str(REPO / "src"))
    from worms_cli import consistency

    deployed = {"dns": {"commit": "a3a66c9"}, "scoring": {"commit": "a3a66c9"}, "workspace": {"commit": "a3a66c9"}} \
        if deployed is None else deployed  # fmt: skip
    return consistency.version_summary(checkout, deployed, list(expected), snapshots or {"dns": "a3a66c9"},
                                       running or {"dashboard": "a3a66c9"})  # fmt: skip


def test_a_system_on_one_commit_says_so():
    assert _version() == ("on", ["system: on a3a66c9"])


def test_one_component_on_another_commit_makes_it_mixed():
    state, lines = _version(deployed={"dns": {"commit": "9f1e2d0"}, "scoring": {"commit": "a3a66c9"},
                                      "workspace": {"commit": "a3a66c9"}}, snapshots={"dns": "9f1e2d0"})  # fmt: skip
    assert state == "mixed" and lines[0] == "system: mixed"
    assert "  a3a66c9: scoring, workspace, dashboard (running)" in lines
    assert "  9f1e2d0: dns, dns (snapshot)" in lines and lines[-1] == "  checkout: a3a66c9"


def test_a_dirty_checkout_is_never_on_one_commit():
    state, lines = _version(checkout="a3a66c9-dirty")
    assert state == "mixed" and lines[-1] == "  checkout: a3a66c9-dirty"


def test_a_component_without_a_record_reads_unknown():
    state, lines = _version(deployed={"dns": {"commit": "a3a66c9"}, "workspace": {"commit": "a3a66c9"}})
    assert state == "mixed" and lines[-2] == "  unknown: scoring"


def test_a_service_vm_without_a_snapshot_adds_no_commit():
    # What runs there is its entry's; the attention block's `snapshots` row names it.
    assert _version(snapshots={"dns": "a3a66c9", "ntp": ""}) == ("on", ["system: on a3a66c9"])


def test_a_record_at_an_older_commit_with_nothing_changed_since_counts_as_on_the_checkout():
    sys.path.insert(0, str(REPO / "src"))
    from worms_cli import consistency

    deployed = {"dns": {"commit": "9f1e2d0"}, "scoring": {"commit": "a3a66c9"}, "workspace": {"commit": "a3a66c9"}}
    args = ("a3a66c9", deployed, ["dns", "scoring", "workspace"], {"dns": "9f1e2d0"}, {"dashboard": "1111111"})
    same = {("dns", "9f1e2d0"), ("dashboard", "1111111")}
    assert consistency.version_summary(*args, same) == ("on", ["system: on a3a66c9"])
    state, lines = consistency.version_summary(*args, same - {("dashboard", "1111111")})
    assert state == "mixed" and "  1111111: dashboard (running)" in lines


def test_same_as_checkout_counts_a_commit_only_when_nothing_of_its_component_changed(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace

    sys.path.insert(0, str(REPO / "src"))
    from worms_cli import changes

    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                              capture_output=True, text=True, check=True).stdout.strip()  # fmt: skip

    (tmp_path / "ansible/roles/dns").mkdir(parents=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "ansible/roles/dns/main.yml").write_text("a\n")
    (tmp_path / "src/cli.py").write_text("a\n")
    git("init", "-q")
    git("add", ".")
    git("commit", "-qm", "one")
    first = git("rev-parse", "--short=7", "HEAD")
    (tmp_path / "src/cli.py").write_text("b\n")
    git("commit", "-qam", "two")
    monkeypatch.setattr(changes, "_made_from", lambda here, name, component: (None, "", [], ["ansible/roles/dns/"]))
    records = [("dns", first), ("workspace", first), ("dns", "deadbee"), ("dns", f"{first}-dirty"), ("platform", first)]
    assert changes.same_as_checkout(SimpleNamespace(repo_root=tmp_path), records) == {("dns", first)}


# --- the consistency rows of `worms status` (worms_cli/consistency.py, T11) ---------------------


def _consistency():
    sys.path.insert(0, str(REPO / "src"))
    from worms_cli import consistency

    return consistency


def test_the_versions_row_only_when_the_system_is_mixed():
    c = _consistency()
    assert [r.name for r in c.version_row("mixed")] == ["versions"]
    assert c.version_row("on") == [] and c.version_row("") == []  # "": the host was not read


def test_the_devbox_map_row_when_a_holder_differs_from_the_host():
    c = _consistency()
    assert c.devbox_map_rows("aa", {"scoring": "aa", "logging": "aa"}) == []
    assert c.devbox_map_rows("", {"scoring": "bb"}) == []  # no map on the host: no devbox job has run
    [row] = c.devbox_map_rows("aa", {"scoring": "aa", "logging": ""})
    assert "logging hold another devbox map" in row.text and "worms step devbox-map" in row.repair


def test_the_current_team_row_while_no_round_runs():
    c = _consistency()
    teams = {"scoring": "11111111-2222-3333-4444-555555555555", "logging": ""}
    assert c.current_team_rows(teams, round_runs=True) == []
    assert c.current_team_rows({"scoring": "", "logging": ""}, round_runs=False) == []
    [row] = c.current_team_rows(teams, round_runs=False)
    assert row.text.startswith("scoring still name a round's team (11111111…)") and "worms round teardown" in row.repair


@pytest.mark.parametrize("pool, state, generated, unfinished, found", [
    (set(), set(), "", False, None),  # nothing: coherent
    ({130, 131}, {130, 131}, "round_one.py", False, None),  # a kept sandbox: coherent
    ({130, 131}, {130}, "round_one.py", False, "VMs in the pool no state holds (131)"),
    ({130}, {130, 131}, "round_one.py", False, "VMs its state holds that Proxmox lacks (131)"),
    (set(), set(), "round_one.py", False, "generated_vms.tf still defines round_one.py's sandbox with none built"),
    (set(), set(), "", True, "an unfinished build is recorded with no VM"),
])  # fmt: skip
def test_the_sandbox_row_when_pool_state_definition_and_record_disagree(pool, state, generated, unfinished, found):
    rows = _consistency().sandbox_rows(pool, state, generated, unfinished)
    assert (rows == []) if found is None else (found in rows[0].text and rows[0].repair == "`worms round teardown`")


def test_the_orphan_row_for_a_vm_no_state_holds():
    c = _consistency()
    vms = [{"vmid": 111, "name": "scoring", "pool": "ephemeral"}, {"vmid": 130, "name": "sandbox-a", "pool": "sandbox"},
           {"vmid": 900, "name": "someones-test", "pool": None}]  # fmt: skip
    assert c.orphan_rows(vms, {111: "scoring", 130: "sandbox-a"}) == []
    [row] = c.orphan_rows(vms, {111: "scoring"})
    assert row.text == "in no OpenTofu state: sandbox-a" and "worms round teardown" in row.repair  # not someones-test
    assert c.state_vms('{"resources": [{"type": "proxmox_virtual_environment_vm", "instances": '
                       '[{"attributes": {"vm_id": 130, "name": "sandbox-a"}}]}]}') == {130: "sandbox-a"}  # fmt: skip


def test_the_backup_row_when_a_job_names_a_vm_that_is_gone():
    c = _consistency()
    vms = [{"vmid": 107}, {"vmid": 110}]
    assert c.backup_rows([{"id": "worms-stateful", "pool": "persistent"}], vms) == []
    assert c.backup_rows([{"id": "worms-stateful", "vmid": "107,110"}], vms) == []
    [row] = c.backup_rows([{"id": "worms-stateful", "vmid": "107,108,110"}], vms)
    assert "worms-stateful backs up VM IDs that do not exist (108)" in row.text


def test_the_drift_row_when_a_plan_has_changes():
    c = _consistency()
    assert c.drift_rows({"platform": 0, "firewall": 0}) == []
    rows = c.drift_rows({"platform": 0, "firewall": 2, "private_internet": 1})
    assert [(r.name, r.state) for r in rows] == [("drift", "warn"), ("drift", "unknown")]
    assert "OpenTofu would change firewall" in rows[0].text and "private_internet" in rows[1].text


def test_the_host_outputs_row_when_what_the_host_reads_is_older_than_the_state():
    c = _consistency()
    assert c.outputs_rows({"platform": 7, "firewall": 42}, {"platform": "7", "firewall": "42"}) == []
    assert c.outputs_rows({}, {"platform": "", "firewall": ""}) == []  # no state here: nothing to compare
    [row] = c.outputs_rows({"platform": 7, "firewall": 43}, {"platform": "7", "firewall": "42"})
    assert "firewall (state serial 43, published 42)" in row.text and row.repair == "`worms step outputs`"
    [row] = c.outputs_rows({"platform": 7}, {"platform": ""})
    assert "published none" in row.text


# --- `worms round teardown`: the sandbox's orphans (worms_cli/rounds.py, T12) -----------------------


def test_teardown_offers_each_orphan_of_the_sandbox_and_destroys_it_only_when_asked(tmp_path):
    """A VM in the sandbox's pool that its state does not hold (an interrupted build's): listed,
    by name, and destroyed (stopped, `qm destroy --purge`) only after a yes."""
    import json
    from types import SimpleNamespace

    sys.path.insert(0, str(REPO / "src"))
    from worms_cli import rounds

    vms = [{"vmid": 130, "name": "sandbox-a", "pool": "sandbox"}, {"vmid": 131, "name": "sandbox-b", "pool": "sandbox"}]
    state = tmp_path / "terraform.tfstate"
    state.write_text(json.dumps({"resources": [{"type": "proxmox_virtual_environment_vm",
                                                "instances": [{"attributes": {"vm_id": 130, "name": "sandbox-a"}}]}]}))  # fmt: skip
    ran, notes = [], []
    jobs = SimpleNamespace(handlers=SimpleNamespace(snapshots=SimpleNamespace(pool_vms=lambda ex, pool: vms)),
                           executor=SimpleNamespace(run=lambda argv, **kw: ran.append(argv)))  # fmt: skip
    summary = SimpleNamespace(note=notes.append)
    asked = []
    assert rounds._destroy_orphans(jobs, False, summary, ask_each=lambda q: asked.append(q) or False, state_path=state) == []
    assert ran == [] and len(asked) == 1 and asked[0].startswith("sandbox-b (vmid 131) is in no OpenTofu state")
    assert rounds._destroy_orphans(jobs, False, summary, ask_each=lambda q: True, state_path=state) == ["sandbox-b"]
    assert ran == [["qm", "stop", "131"], ["qm", "destroy", "131", "--purge"]] and "sandbox-b" in notes[0]
