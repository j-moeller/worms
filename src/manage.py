#!/usr/bin/env python3
"""worms -- one CLI for the WORMS platform.

The single tool for building and operating the platform. It IS the build
pipeline (the top-level Makefile just calls `worms build` / `worms destroy`);
it also shows live status from the one source of truth -- the database VM and
the Proxmox API. Run it on your laptop with WORMS_SSH=root@<node> (OpenTofu and
Ansible run there, with the secrets of the vault), or on the Proxmox node.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from worms_cli import build, output, pipeline  # noqa: E402
from worms_cli.config import Config  # noqa: E402

# Disruptive pipeline operations, each a confirm-first subcommand. emergency-stop
# only freezes (stops) the VMs; terminate is the full teardown.
DESTRUCTIVE = {
    "emergency-stop": ("stop_all", "freeze: hard-stop ALL VMs (does NOT destroy them)"),
    "destroy": ("destroy", "destroy the VM stacks (keeps firewall/platform)"),
    "destroy-all": ("destroy_all", "destroy everything (VMs, firewall, platform)"),
    "terminate": ("terminate", "stop, unlock and destroy every VM and all stacks"),
}


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def _infra_status(config) -> list[list[str]]:
    from worms_cli.proxmox import Infra, ProxmoxUnavailable

    infra = Infra(config)
    rows: list[list[str]] = []

    if not infra.configured:
        reason = infra.error or "no Proxmox API configured (worms secrets set topology-token)"
        return [["infrastructure", output.status_cell("n/a", reason)]]

    try:
        enabled = infra.firewall_enabled()
        rows.append(["firewall", output.status_cell("active" if enabled else "down", "enabled" if enabled else "DISABLED")])
    except Exception as exc:  # noqa: BLE001
        return [["infrastructure", output.status_cell("error", f"Proxmox API error: {exc}")]]

    try:
        bridges = infra.bridges()
        rows.append(["bridges / network", output.status_cell("ok", f"{len(bridges)} Linux bridge(s)")])
    except Exception as exc:  # noqa: BLE001
        rows.append(["bridges / network", output.status_cell("error", str(exc))])

    try:
        vms = infra.vms()
    except Exception as exc:  # noqa: BLE001
        rows.append(["VMs", output.status_cell("error", str(exc))])
        return rows

    groups = {
        "infrastructure VMs": {"persistent", "ephemeral", ""},
        "private internet": {"private_internet"},
        "sandbox VMs": {"sandbox"},
        "devbox VMs": {"devbox"},
    }
    for label, pools in groups.items():
        members = [vm for vm in vms if vm.get("pool") in pools]
        if label == "infrastructure VMs":
            members = [vm for vm in vms if vm.get("pool") in {"persistent", "ephemeral"}]
        running = sum(1 for vm in members if vm.get("status") == "running")
        total = len(members)
        if total == 0:
            rows.append([label, output.status_cell("stopped", "none")])
        else:
            status = "running" if running == total else ("warn" if running else "stopped")
            rows.append([label, output.status_cell(status, f"{running}/{total} running")])
    return rows


INTERNET_FLAG = "/run/worms/internet-allowed"
PVE_CONFIG_DIR = "/etc/pve"  # exists only on a Proxmox node


def _internet_status(config) -> list[str]:
    """The node's internet flag: read directly on the node, over SSH (with the
    WORMS key) from a laptop (WORMS_SSH)."""
    if not config.ssh:
        if not os.path.isdir(PVE_CONFIG_DIR):
            return ["internet access", output.status_cell("unknown", "not on the Proxmox node: set WORMS_SSH to probe it")]
        state = "allowed" if os.path.exists(INTERNET_FLAG) else "denied"
        return _internet_cell(state, "")
    from worms_cli.session import SessionError, for_config

    try:
        state = "allowed" if for_config(config).internet_allowed() else "denied"
    except (SessionError, OSError, subprocess.SubprocessError) as exc:
        return _internet_cell("unknown", str(exc))
    return _internet_cell(state, "")


def _internet_notice(config) -> None:
    """Every command starts by saying whether the VMs have internet access. On
    stderr, because the stdout of some commands is their result (a one-time
    password), and it never stops a command: the node may be out of reach."""
    try:
        row = _internet_status(config)
    except Exception as exc:  # noqa: BLE001
        row = _internet_cell("unknown", str(exc))
    print(": ".join(row), file=sys.stderr)


def _internet_cell(state, stderr) -> list[str]:
    if state == "allowed":
        return ["internet access", output.status_cell("warn", "ALLOWED (close it with `worms build internet-deny`)")]
    if state == "denied":
        return ["internet access", output.status_cell("ok", "denied")]
    return ["internet access", output.status_cell("unknown", stderr.strip()[:60] or "could not probe")]


def _competition_status(config) -> list[list[str]]:
    from worms_cli import django_setup

    try:
        django_setup.setup(config)
    except django_setup.NotConfigured as exc:
        return [["competition", output.status_cell("n/a", str(exc))]]

    from worms_cli import queries

    overview = queries.competition_overview()
    rows: list[list[str]] = []
    comp = overview.get("competition")
    if comp is None:
        rows.append(["competition", output.status_cell("warn", "none set (run manage.py init)")])
        return rows

    rows.append(["competition", output.status_cell("ok", comp.name)])
    if overview["registration"]:
        rows.append(["phase", output.status_cell("active", "registration open")])
    elif overview["active_round"]:
        rows.append(["phase", output.status_cell("active", f"round in progress: {overview['active_round'].name}")])
    elif overview["next_round"]:
        rows.append(["phase", output.status_cell("warn", f"next round: {overview['next_round'].name}")])
    else:
        rows.append(["phase", output.status_cell("stopped", "no active or upcoming round")])
    rows.append(["rounds", output.status_cell("ok", str(overview["n_rounds"]))])
    rows.append(["teams", output.status_cell("ok", str(overview["n_teams"]))])

    try:
        runs = queries.run_overview()
        if runs["active"]:
            r = runs["active"]
            rows.append(["active run", output.status_cell("running", f"{r.team.name} ({r.state})")])
        if runs["by_state"]:
            summary = ", ".join(f"{k}:{v}" for k, v in sorted(runs["by_state"].items()))
            rows.append(["round runs", output.status_cell("ok", summary)])
        if runs["queued_jobs"]:
            rows.append(["host jobs", output.status_cell("warn", f"{runs['queued_jobs']} queued")])
    except Exception:  # noqa: BLE001  (round tables may be empty/new)
        pass
    return rows


def cmd_status(config, args) -> int:
    print(output.heading("infrastructure"))
    print(output.table(_infra_status(config) + [_internet_status(config)]))
    print()
    print(output.heading("competition (database)"))
    print(output.table(_competition_status(config)))
    return 0


# ---------------------------------------------------------------------------
# teams / users
# ---------------------------------------------------------------------------


def _with_db(config):
    from worms_cli import django_setup

    try:
        django_setup.setup(config)
    except django_setup.NotConfigured as exc:
        output.error(str(exc))
        raise SystemExit(2)


def cmd_teams(config, args) -> int:
    _with_db(config)
    from worms_cli import queries

    action = args.team_action
    if action == "delete":
        if not build.confirm(f"Delete team {args.name!r} (users are kept)?", getattr(args, "yes", False)):
            return 1
        print(queries.delete_team(args.name))
        from core import events

        events.team("deleted", actor=_console(), team_name=args.name)
        return 0

    rows = queries.team_rows()
    if not rows:
        print("No teams (or no current competition).")
        return 0
    table_rows = []
    for row in rows:
        members = ", ".join(
            m["username"]
            + ("" if m["logged_in"] else output.color(" (never logged in)", "grey"))
            + (output.color(" (one-time pw)", "yellow") if m["must_change_password"] else "")
            for m in row["members"]
        ) or output.color("(empty)", "grey")
        table_rows.append([row["name"], f"{row['n_members']}/{row['max_members']}", members])
    print(output.table(table_rows, headers=["Team", "Members", "Who"]))
    return 0


def cmd_devbox(config, args) -> int:
    """`worms devbox provision`: give every team of the current competition a
    devbox and enqueue the provision host job; the round agent on the node
    builds them (docs/devbox.md). Teams are created on the dashboard, which
    builds no infrastructure; run this after adding teams. `worms devbox
    reconfigure`: push what the service VMs and the gateway must know again."""
    if args.devbox_action == "reconfigure":
        return _request_devbox_reconfigure(config)
    _with_db(config)
    from django.core.management import call_command
    from django.core.management.base import CommandError

    try:
        call_command("provision_devboxes")
    except CommandError as exc:
        output.error(str(exc))
        return 1
    return 0


def _request_devbox_reconfigure(config) -> int:
    """Have the round agent push every team's SSH keys and the source->team map
    again, from the database. Never from the service VMs: one of them may have
    been changed by a worm, which is what a reset is there to undo."""
    _with_db(config)
    from core.models.competition import ApplicationSetting
    from orchestrator.rounds.devbox import request_reconfigure

    competition = ApplicationSetting.get_current_competition()
    job = request_reconfigure(competition) if competition else None
    if job is None:
        print("nothing to enqueue: no devbox is provisioned, or the same job is queued")
    else:
        print(f"enqueued devbox_reconfigure host job {job.uuid}: the round agent does the work")
    return 0


def _restore_devbox_map(config) -> int:
    """After the service VMs were rolled back or rebuilt: the map on scoring and
    logging is newer than their snapshot. Whatever goes wrong here must not read as
    a failed reset."""
    try:
        return _request_devbox_reconfigure(config)
    except (SystemExit, Exception) as exc:  # noqa: BLE001
        output.error(
            f"the services are reset, but the devbox map on scoring and logging is empty ({exc}): "
            "devbox submissions stay unattributed until `worms devbox reconfigure` has run"
        )
        return 1


def cmd_users(config, args) -> int:
    _with_db(config)
    from worms_cli import queries

    rows = []
    for user in queries.user_rows():
        flags = []
        if user["is_staff"]:
            flags.append(output.color("staff", "cyan"))
        if user["must_change_password"]:
            flags.append(output.color("one-time pw", "yellow"))
        rows.append(
            [
                user["username"],
                user["team"] or output.color("(no team)", "grey"),
                output.status_cell("ok", "yes") if user["logged_in"] else output.status_cell("stopped", "never"),
                " ".join(flags),
            ]
        )
    print(output.table(rows, headers=["User", "Team", "Logged in", "Flags"]))
    return 0


def _console() -> str:
    """Who ran a console command, for the event tables: `console:<user>@<host>`."""
    import getpass
    import socket

    return f"console:{getpass.getuser()}@{socket.gethostname()}"


def cmd_audit(config, args) -> int:
    """`worms audit`: the event tables as one timeline, newest first. Only the admin reads
    them: the dashboard's and the node's accounts may only append (docs/monitoring.md)."""
    _with_db(config)
    from worms_cli import queries

    rows = queries.event_rows(hours=args.hours, kind=args.kind, limit=args.limit)
    if not rows:
        print(f"No audit events in the last {args.hours} hour(s).")
        return 0
    print(output.table(rows, headers=["Time (UTC)", "Event", "Written by", "Actor", "Target", "Team", "Client", "Details"]))
    return 0


def cmd_admin(config, args) -> int:
    """`worms admin reset-password`: a new one-time password for the dashboard
    admin, printed once here (stdout), never stored (manage.py
    reset_admin_password). `worms admin create-staff EMAIL`: a staff (tutor)
    account with a one-time password -- the public dashboard manages student
    accounts but cannot create staff (docs/accounts.md)."""
    _with_db(config)
    from core.models.user import User
    from django.core.management import call_command
    from django.core.management.base import CommandError

    if args.admin_action == "create-staff":
        from core import accounts

        created = accounts.create_account(args.email, args.first_name, args.last_name, staff=True)
        if created is None:
            output.error(f"an account {args.email!r} already exists")
            return 1
        from core import events

        events.account("staff_created", actor=_console(), target=created.user)
        print(f"staff: created {created.user.username!r}; one-time password below (shown once):", file=sys.stderr)
        print(created.password)
        return 0

    admin = User.objects.filter(username=args.username).first()
    if admin is not None and admin.has_usable_password() and not admin.must_change_password:
        if not build.confirm(f"Replace {args.username!r}'s password and end its sessions?", args.yes):
            return 1
    try:
        call_command("reset_admin_password", username=args.username)
    except CommandError as exc:
        output.error(str(exc))
        return 1
    from core import events

    events.password("reset", actor=_console(), target=User.objects.filter(username=args.username).first())
    return 0


# ---------------------------------------------------------------------------
# leaderboard
# ---------------------------------------------------------------------------


def cmd_leaderboard(config, args) -> int:
    _with_db(config)
    from worms_cli import queries

    data = queries.leaderboard()
    if data["competition"] is None:
        print("No current competition.")
        return 0
    print(output.heading(f"leaderboard — {data['competition'].name}"))
    if data.get("current_round"):
        print(output.color(f"current round: {data['current_round'].name}", "dim"))
    rows = []
    for team in data["teams"]:
        place = "" if getattr(team, "skip_place", False) else f"{team.place:02d}"
        wins = ", ".join(getattr(team, "round_wins", [])) or ""
        rows.append([place, team.name, str(team.total_points), wins])
    print(output.table(rows, headers=["#", "Team", "Points", "Rounds won"]))
    return 0


# ---------------------------------------------------------------------------
# secrets / node
# ---------------------------------------------------------------------------


def cmd_secrets(config, args) -> int:
    """`worms secrets`: what the vault holds (never a value); `worms secrets
    set <name>`: enter one, prompted without echo or read from stdin; `worms
    secrets issue <name>`: create or rotate a Proxmox token and store it; `worms
    secrets check`: ask Proxmox whether the stored tokens are right."""
    import getpass

    from worms_cli.secrets import EXTERNAL, Vault, VaultError
    from worms_cli.session import SessionError, for_config
    from worms_cli.tokens import TOKENS, TokenError, check, issue

    vault = Vault(config.repo_root)
    try:
        if args.secrets_action == "issue":
            spec = TOKENS[args.name]
            vault.set(args.name, issue(spec, for_config(config).node_run))
            print(f"{args.name}: issued {spec.full_id} ({spec.role} on {spec.path}), stored in {vault.file}")
            print(output.color("Deliver it to its users with `worms build provision`.", "dim"))
            return 0
        if args.secrets_action == "check":
            vault.ensure()
            permissions = for_config(config).token_permissions()
            rows, bad = [], 0
            for name, spec in TOKENS.items():
                state = check(spec, vault.get(name), lambda secret, path, s=spec: permissions(s, secret, path))
                bad += not state.startswith("ok")
                rows.append([name, spec.full_id, spec.purpose, output.status_cell("ok" if state.startswith("ok") else "warn", state)])
            print(output.table(rows, headers=["secret", "token", "used by", "state"]))
            return 1 if bad else 0
        if args.secrets_action == "set":
            if sys.stdin.isatty():
                if args.name == "ssh-key":
                    output.error("pipe the private key in: worms secrets set ssh-key < key")
                    return 2
                value = getpass.getpass(f"{args.name} ({EXTERNAL[args.name][1]}): ")
            else:
                value = sys.stdin.read()
            vault.set(args.name, value)
            print(f"{args.name}: stored in {vault.file}")
            return 0
        vault.ensure()
        rows = [[name, what, output.status_cell("ok" if state == "set" else ("warn" if "MISSING" in state else "stopped"),
                                                state)] for name, what, state in vault.status()]  # fmt: skip
    except (VaultError, KeyError, TokenError, SessionError) as exc:
        output.error(str(exc))
        return 1
    print(output.table(rows, headers=["secret", "what", "state"]))
    print(output.color(f"vault: {vault.file} (key {vault.key_file}); set one with `worms secrets set <name>`", "dim"))
    return 0


def cmd_node(config, args) -> int:
    """`worms node trust`: authorize the WORMS key (vault) on the VMs that exist
    already (they accept root's old key on the node, which logs in to them), and
    for root on the node, with your own SSH login. Needed once for VMs that
    predate the WORMS key, and for the node only without an ssh-agent of your
    own (the node is otherwise reached with your keys)."""
    import shlex

    from worms_cli.secrets import SSH_PUBLIC_KEY
    from worms_cli.session import SessionError, for_config

    if not config.ssh:
        output.error("set WORMS_SSH=root@<node> first (~/.worms/config)")
        return 2
    session = for_config(config)
    try:
        key = session.values()[SSH_PUBLIC_KEY].strip()
    except SessionError as exc:
        output.error(str(exc))
        return 1
    add = f"grep -qxF {shlex.quote(key)} ~/.ssh/authorized_keys 2>/dev/null || echo {shlex.quote(key)} >> ~/.ssh/authorized_keys"
    inventory = config.repo_root / "ansible" / "inventory.generated.yml"
    ips = sorted(set(re.findall(r"ansible_host: (\d+\.\d+\.\d+\.\d+)", inventory.read_text()))) if inventory.exists() else []
    vm = "ssh -o BatchMode=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10"
    # The VMs the node knows of: ours, and its own (earlier) inventories.
    script = rf"""set -u
umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; {add}
echo "node: the WORMS key may log in as $(id -un)"
old=$(sed -n 's/^REPO_ROOT=//p' /etc/worms/round-agent.env 2>/dev/null)
known=$(cat "$old/ansible/inventory.generated.yml" /opt/worms/ansible/inventory.generated.yml 2>/dev/null \
  | sed -n 's/^ *ansible_host: \([0-9.]*\)$/\1/p')
for ip in $(printf '%s\n' {' '.join(ips)} $known | sort -u); do
  [ "$ip" = 127.0.0.1 ] && continue
  if {vm} debian@"$ip" {shlex.quote(add)} >/dev/null 2>&1; then echo "vm $ip: ok"; else echo "vm $ip: FAILED (not reachable with root's key)"; fi
done
"""
    print(output.color(f"$ ssh {config.ssh} ...  (your own SSH login)", "dim"))
    if subprocess.run(["ssh", config.ssh, script]).returncode != 0:
        output.error(f"could not add the key on {config.ssh}")
        return 1
    try:
        session.check_node()
    except SessionError as exc:
        output.error(str(exc))
        return 1
    print(output.color("login to the node with the agent's keys (as OpenTofu does): ok", "green"))
    return 0


# ---------------------------------------------------------------------------
# build / destructive
# ---------------------------------------------------------------------------


def cmd_build(config, args) -> int:
    if args.list:
        print(_components_overview())
        return 0
    component = args.component or "all"
    if component not in pipeline.COMPONENTS:
        output.error(f"unknown component {component!r}. Try `worms build --list`.")
        return 1
    method_name, _ = pipeline.COMPONENTS[component]
    pipe = pipeline.Pipeline(config, ansible_args=" ".join(args.make_args))
    code = 0
    try:
        getattr(pipe, method_name)()
    except build.BuildError as exc:
        output.error(str(exc))
        code = 1
    # Also after a failure: a rollback that happened is not undone by one.
    if pipe.services_rolled_back and pipe.devboxes_exist():
        code = _restore_devbox_map(config) or code
    return code


def cmd_debug(config, args) -> int:
    pipe = pipeline.Pipeline(config)
    try:
        pipe.debug(args.host)
    except build.BuildError as exc:
        output.error(str(exc))
        return 1
    return 0


def cmd_test(config, args) -> int:
    """Run the offline test suite: a .venv-test venv + pytest. Runs on this
    machine (the repo), not over SSH."""
    import subprocess

    repo = str(config.repo_root)
    venv = f"{repo}/.venv-test"
    pytest = f"{venv}/bin/pytest"
    if not os.path.exists(pytest):
        print(output.color("creating .venv-test ...", "dim"))
        subprocess.run([sys.executable, "-m", "venv", venv], check=True)
        subprocess.run([f"{venv}/bin/pip", "install", "-q", "-r", f"{repo}/tests/requirements.txt"], check=True)
    env = {**os.environ, "PATH": f"{venv}/bin:" + os.environ.get("PATH", "")}
    return subprocess.run([pytest, *args.pytest_args], cwd=repo, env=env).returncode


def cmd_reset_database(config, args) -> int:
    """Wipe the database to a clean slate and re-initialise it. DANGER.

    Runs through the CLI's own database connection (a privileged role), so it
    needs WORMS_DB_* but no SSH. It is intentionally not a Makefile target.
    """
    if not build.confirm("Wipe ALL dashboard data (teams, users, captures, logs) and re-init?", args.yes):
        return 1
    _with_db(config)
    from django.core.management import call_command

    call_command("reset_database", "--yes")
    return 0


def cmd_destroy(config, args, key: str) -> int:
    method_name, help_text = DESTRUCTIVE[key]
    if not build.confirm(f"{help_text}. Proceed?", args.yes):
        return 1
    pipe = pipeline.Pipeline(config)
    try:
        getattr(pipe, method_name)()
    except build.BuildError as exc:
        output.error(str(exc))
        return 1
    return 0


# ---------------------------------------------------------------------------
# help: every command under what it is for
# ---------------------------------------------------------------------------


class Section(NamedTuple):
    """One part of the help. `commands` are (as typed, one line); build_parser
    takes each command's help from here, so a new command has to say what it is
    for. `components` are `worms build` components that also get a line in
    `worms --help`; `more_components` appear only in `worms build --list`."""

    title: str
    about: str
    commands: tuple[tuple[str, str], ...] = ()
    components: tuple[str, ...] = ()
    more_components: tuple[str, ...] = ()


SECTIONS = (
    Section(
        "set up the system",
        "From an empty node to a running platform. `worms build` is safe to repeat; it opens the internet "
        "window, which is refused during a round and shuts running devboxes down while it is open. It keeps the "
        "snapshot of the service VMs that a reset returns to. Afterwards: `worms admin reset-password` for the "
        "first login.",
        commands=(
            ("secrets issue <name>", "create or rotate a Proxmox token and store it"),
            ("secrets set <name>", "store a secret by hand (prompted, or from stdin)"),
            ("node trust", "once: authorize the WORMS key on VMs that predate it (and for root on the node)"),
            ("build [<component>]", "everything, end to end (= make), or one component: `worms build --list`"),
        ),
        more_components=("all", "init", "secrets", "platform", "firewall", "host-network", "infrastructure",
                         "inventory", "configure", "provision", "offline", "snapshot-services", "internet-allow"),  # fmt: skip
    ),
    Section(
        "manage production",
        "The running competition, from the console. Teams, students and rounds are managed on the dashboard "
        "instead (/staff/), and a round starts by itself.",
        commands=(
            ("devbox provision", "give new teams a devbox: enqueue the build for the round agent"),
            ("devbox reconfigure", "enqueue a new push of the teams' SSH keys and the devbox map (a reset does this itself)"),
            ("admin reset-password", "one-time password for the dashboard admin, printed once (created if missing)"),
            ("admin create-staff <email>", "a staff (tutor) account; one-time password printed once"),
            ("teams delete <name>", "delete a team with everything it owns (its users stay)"),
        ),
        components=("round-reset-services", "rebuild-services", "internet-deny"),
    ),
    Section(
        "run by the round agent",
        "The agent runs these itself, for `worms devbox provision` and for rounds; `devbox` is the two devbox "
        "steps together.",
        more_components=("round-configure-sandbox", "devbox", "devbox-apply", "devbox-configure"),
    ),
    Section(
        "see the status",
        "These change nothing on the platform.",
        commands=(
            ("status", "components, internet, competition, rounds"),
            ("teams", "teams, their members, who has signed in (create and edit them on the dashboard: Staff → Teams)"),
            ("users", "all users: team, login status, one-time password"),
            ("leaderboard", "the production leaderboard, in the console"),
            ("audit", "the event tables: sign-ins, passwords, view-as, SSH gateway logins, ... (newest first)"),
            ("secrets", "the vault: what is set, never a value (creates the vault and missing generated secrets)"),
            ("secrets check", "ask Proxmox whether the stored tokens work, with exactly their rights"),
        ),
    ),
    Section(
        "debug a problem",
        "When a build or a VM misbehaves. Host jobs log to /var/log/worms/jobs/ on the node.",
        commands=(("debug <host>", "Ansible connection diagnostics for a host/group"),),
        components=("ping", "show-config"),
    ),
    Section(
        "develop and test",
        "For working on the code. `test` runs offline, anywhere; `simulate` runs on the deployed platform, "
        "where it creates a test team and removes it afterwards (docs/testing.md).",
        commands=(
            ("test [-- <pytest args>]", "run the offline test suite (.venv-test + pytest)"),
            ("simulate", pipeline.COMPONENTS["simulate"][1]),
        ),
        more_components=("simulate",),
    ),
    Section(
        "stop and tear down",
        "Each asks first, unless given --yes. `emergency-stop` is the only hard stop of a running round; it "
        "also stops the database and the dashboard.",
        commands=(
            ("emergency-stop", DESTRUCTIVE["emergency-stop"][1]),
            ("reset-database", "DANGER: wipe + re-migrate + re-init the database"),
            ("destroy", DESTRUCTIVE["destroy"][1]),
            ("destroy-all", DESTRUCTIVE["destroy-all"][1]),
            ("terminate", DESTRUCTIVE["terminate"][1]),
        ),
    ),
)

HELP_WIDTH = 79


def _command_name(usage: str) -> str:
    """`secrets issue <name>` -> `secrets issue`: the words that select the command."""
    return re.split(r" [<\[]", usage)[0]


def _command_help(command: str) -> str:
    for section in SECTIONS:
        for usage, text in section.commands:
            if _command_name(usage) == command:
                return text
    raise KeyError(f"`worms {command}` is under no section of SECTIONS")


def _add(subparsers, command: str, **kwargs) -> argparse.ArgumentParser:
    """Add `worms <command>`, with its line in SECTIONS as its help."""
    text = _command_help(command)
    return subparsers.add_parser(command.split()[-1], help=text, description=text, **kwargs)


def _format_sections(sections: list[tuple[Section, list[tuple[str, str]]]]) -> str:
    """Each section that has rows: title, what it is for, then two columns."""
    sections = [(section, rows) for section, rows in sections if rows]
    column = max(len(usage) for _, rows in sections for usage, _ in rows)
    parts = []
    for section, rows in sections:
        about = textwrap.fill(section.about, HELP_WIDTH, initial_indent="  ", subsequent_indent="  ")
        lines = [output.heading(section.title), about]
        lines += [
            textwrap.fill(
                text,
                HELP_WIDTH,
                initial_indent=f"  {usage:<{column}}  ",
                subsequent_indent=" " * (column + 4),
                break_long_words=False,
                break_on_hyphens=False,
            )
            for usage, text in rows
        ]
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _commands_overview() -> str:
    """The sections of `worms --help`."""
    return _format_sections([
        (section, [*section.commands, *((f"build {c}", pipeline.COMPONENTS[c][1]) for c in section.components)])
        for section in SECTIONS
    ])  # fmt: skip


def _components_overview() -> str:
    """The sections of `worms build --list`."""
    return _format_sections([
        (section, [(name, pipeline.COMPONENTS[name][1]) for name in (*section.components, *section.more_components)])
        for section in SECTIONS
    ])  # fmt: skip


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="worms",
        usage="worms [-h] [--config FILE] <command> ...",
        description=f"{__doc__}\n{_commands_overview()}",
        epilog="Configuration (env or ~/.worms/config, no secrets): src/worms_cli/config.py;\n"
        "secrets: src/worms_cli/secrets.py; docs/accounts.md §4. Run `worms <command>\n"
        "--help` for details, and see docs/management.md for how it fits together.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", metavar="FILE", help="path to a KEY=value config file (default ~/.worms/config)")
    # The sections above replace argparse's flat list of commands. An explicit
    # prog: older Pythons build it from the parser's custom usage otherwise.
    sub = parser.add_subparsers(
        dest="command", required=True, prog="worms", metavar="<command>", help=argparse.SUPPRESS
    )

    _add(sub, "status")

    p_teams = _add(sub, "teams")
    team_sub = p_teams.add_subparsers(dest="team_action")
    p_delete = _add(team_sub, "teams delete")
    p_delete.add_argument("name")
    p_delete.add_argument("--yes", action="store_true", help="skip confirmation")

    devbox_help = "the team devboxes: `provision` enqueues the build of new teams' devboxes for the round agent"
    p_devbox = sub.add_parser("devbox", description=devbox_help)
    devbox_sub = p_devbox.add_subparsers(dest="devbox_action", required=True)
    _add(devbox_sub, "devbox provision")
    _add(devbox_sub, "devbox reconfigure")

    _add(sub, "users")
    _add(sub, "leaderboard")
    p_audit = _add(sub, "audit")
    p_audit.add_argument("--hours", type=int, default=24, help="how far back (default 24)")
    p_audit.add_argument("--kind", default=None, help="only events starting with this: login, password, team.joined, ...")
    p_audit.add_argument("--limit", type=int, default=200, help="at most this many rows (default 200)")

    p_admin = sub.add_parser("admin", description="the dashboard admin account, and staff accounts")
    admin_sub = p_admin.add_subparsers(dest="admin_action", required=True)
    p_reset_pw = _add(admin_sub, "admin reset-password")
    p_reset_pw.add_argument("--username", default="admin")
    p_reset_pw.add_argument("--yes", action="store_true", help="skip confirmation")
    p_staff = _add(admin_sub, "admin create-staff")
    p_staff.add_argument("email")
    p_staff.add_argument("--first-name", default="")
    p_staff.add_argument("--last-name", default="")

    p_secrets = _add(sub, "secrets")
    secrets_sub = p_secrets.add_subparsers(dest="secrets_action")
    p_issue = _add(secrets_sub, "secrets issue")
    p_issue.add_argument("name", choices=["proxmox-token", "orchestrator-token", "topology-token"])
    _add(secrets_sub, "secrets check")
    p_set = _add(secrets_sub, "secrets set")
    p_set.add_argument("name", choices=["proxmox-token", "orchestrator-token", "topology-token", "ssh-key"])

    p_node = sub.add_parser("node", description="the Proxmox node")
    node_sub = p_node.add_subparsers(dest="node_action", required=True)
    _add(node_sub, "node trust")

    p_build = _add(sub, "build", epilog=_components_overview(), formatter_class=argparse.RawDescriptionHelpFormatter)
    p_build.add_argument("component", nargs="?", help="component to build (default: all)")
    p_build.add_argument("--list", action="store_true", help="list components and exit")
    p_build.add_argument(
        "make_args", nargs="*", help="extra args passed to ansible-playbook, after `--` (e.g. -- --limit db -vvv)"
    )

    _add(sub, "simulate")

    p_test = _add(sub, "test")
    p_test.add_argument(
        "pytest_args", nargs="*", help="extra args passed to pytest, after `--` (e.g. -- -k shipper -x)"
    )

    p_dbg = _add(sub, "debug")
    p_dbg.add_argument("host")

    p_reset = _add(sub, "reset-database")
    p_reset.add_argument("--yes", action="store_true")

    for key in DESTRUCTIVE:
        p = _add(sub, key)
        p.add_argument("--yes", action="store_true")

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = Config.load(args.config)
    _internet_notice(config)

    if args.command == "status":
        return cmd_status(config, args)
    if args.command == "teams":
        return cmd_teams(config, args)
    if args.command == "devbox":
        return cmd_devbox(config, args)
    if args.command == "users":
        return cmd_users(config, args)
    if args.command == "leaderboard":
        return cmd_leaderboard(config, args)
    if args.command == "audit":
        return cmd_audit(config, args)
    if args.command == "admin":
        return cmd_admin(config, args)
    if args.command == "secrets":
        return cmd_secrets(config, args)
    if args.command == "node":
        return cmd_node(config, args)
    if args.command == "build":
        return cmd_build(config, args)
    if args.command == "simulate":
        return cmd_build(config, argparse.Namespace(component="simulate", list=False, make_args=[]))
    if args.command == "test":
        return cmd_test(config, args)
    if args.command == "debug":
        return cmd_debug(config, args)
    if args.command == "reset-database":
        return cmd_reset_database(config, args)
    if args.command in DESTRUCTIVE:
        return cmd_destroy(config, args, args.command)
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
