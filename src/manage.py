#!/usr/bin/env python3
"""worms -- one CLI for the WORMS platform.

The single tool for building and operating the platform. It IS the build
pipeline (the top-level Makefile just calls `worms build` / `worms destroy`);
it also shows live status from the one source of truth -- the database VM and
the Proxmox API. Run it on your laptop with WORMS_SSH=root@<node> (OpenTofu and
Ansible run there, with the secrets of the vault), or on the Proxmox node.

    worms secrets [set <name>]        # the vault: enter the Proxmox tokens once
    worms node trust                  # once: the WORMS key on VMs that predate it
    worms build                       # full idempotent end-to-end setup (= make)
    worms build <component>           # one step (see `worms build --list`)
    worms status                      # components, internet, competition, rounds
    worms teams [delete]              # teams are created/edited on the dashboard
    worms devbox provision            # build the devboxes of new teams (host job)
    worms users / worms leaderboard
    worms admin reset-password        # one-time password for the dashboard admin
    worms admin create-staff EMAIL    # a tutor account (students: on the dashboard)
    worms simulate                    # end-to-end simulation (was make simulate)
    worms test                        # the offline test suite (was make test)
    worms debug <host>                # Ansible connection diagnostics
    worms reset-database              # DANGER: wipe + re-migrate + re-init
    worms destroy / destroy-all       # teardown
    worms emergency-stop              # freeze: hard-stop ALL vms (does NOT destroy)

Configuration (env or ~/.worms/config, no secrets): src/worms_cli/config.py;
secrets: src/worms_cli/secrets.py; docs/accounts.md §4. Run `worms <command>
--help` for details.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

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


def _internet_cell(state, stderr) -> list[str]:
    if state == "allowed":
        return ["internet access", output.status_cell("warn", "ALLOWED (deny before deploying a sandbox)")]
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
    builds no infrastructure; run this after adding teams."""
    _with_db(config)
    from django.core.management import call_command
    from django.core.management.base import CommandError

    try:
        call_command("provision_devboxes", dry_run=args.dry_run)
    except CommandError as exc:
        output.error(str(exc))
        return 1
    return 0


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

        if args.dry_run:
            print(output.color(f"(dry run) would create the staff account {args.email!r}", "yellow"))
            return 0
        created = accounts.create_account(args.email, args.first_name, args.last_name, staff=True)
        if created is None:
            output.error(f"an account {args.email!r} already exists")
            return 1
        print(f"staff: created {created.user.username!r}; one-time password below (shown once):", file=sys.stderr)
        print(created.password)
        return 0

    admin = User.objects.filter(username=args.username).first()
    if admin is not None and admin.has_usable_password() and not admin.must_change_password:
        if not build.confirm(f"Replace {args.username!r}'s password and end its sessions?", args.yes):
            return 1
    if args.dry_run:
        print(output.color(f"(dry run) would set a one-time password for {args.username!r}", "yellow"))
        return 0
    try:
        call_command("reset_admin_password", username=args.username)
    except CommandError as exc:
        output.error(str(exc))
        return 1
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
    set <name>`: enter one, prompted without echo or read from stdin."""
    import getpass

    from worms_cli.secrets import EXTERNAL, Vault, VaultError

    vault = Vault(config.repo_root)
    try:
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
    except (VaultError, KeyError) as exc:
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
    if args.dry_run:
        print(output.color("  (dry run: not executed)", "yellow"))
        return 0
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
        rows = [[name, help_text] for name, (_, help_text) in pipeline.COMPONENTS.items()]
        print(output.table(rows, headers=["component", "what"]))
        return 0
    component = args.component or "all"
    if component not in pipeline.COMPONENTS:
        output.error(f"unknown component {component!r}. Try `worms build --list`.")
        return 1
    method_name, _ = pipeline.COMPONENTS[component]
    pipe = pipeline.Pipeline(config, dry_run=args.dry_run, ansible_args=" ".join(args.make_args))
    try:
        getattr(pipe, method_name)()
    except build.BuildError as exc:
        output.error(str(exc))
        return 1
    return 0


def cmd_debug(config, args) -> int:
    pipe = pipeline.Pipeline(config, dry_run=args.dry_run)
    try:
        pipe.debug(args.host)
    except build.BuildError as exc:
        output.error(str(exc))
        return 1
    return 0


def cmd_test(config, args) -> int:
    """Run the offline test suite (was `make test`): a .venv-test venv +
    pytest. Runs on this machine (the repo), not over SSH."""
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
    if args.dry_run:
        print(output.color("(dry run) would run manage.py reset_database --yes", "yellow"))
        return 0
    from django.core.management import call_command

    call_command("reset_database", "--yes")
    return 0


def cmd_destroy(config, args, key: str) -> int:
    method_name, help_text = DESTRUCTIVE[key]
    if not build.confirm(f"{help_text}. Proceed?", args.yes):
        return 1
    pipe = pipeline.Pipeline(config, dry_run=args.dry_run)
    try:
        getattr(pipe, method_name)()
    except build.BuildError as exc:
        output.error(str(exc))
        return 1
    return 0


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="worms", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="path to a KEY=value config file (default ~/.worms/config)")
    parser.add_argument("--dry-run", action="store_true", help="print what would run, change nothing")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="components, internet, competition, rounds")

    teams_help = "list teams; delete one with its history (create/edit them on the dashboard: Staff → Teams)"
    p_teams = sub.add_parser("teams", help=teams_help, description=teams_help)
    team_sub = p_teams.add_subparsers(dest="team_action")
    p_delete = team_sub.add_parser("delete", help="delete a team with everything it owns (users unaffected)")
    p_delete.add_argument("name")
    p_delete.add_argument("--yes", action="store_true", help="skip confirmation")

    devbox_help = "the team devboxes: `provision` enqueues the build of new teams' devboxes for the round agent"
    p_devbox = sub.add_parser("devbox", help="the team devboxes", description=devbox_help)
    devbox_sub = p_devbox.add_subparsers(dest="devbox_action", required=True)
    devbox_sub.add_parser("provision", help="give new teams a devbox: enqueue the provision host job")

    sub.add_parser("users", help="all users: team, login status, one-time password")
    sub.add_parser("leaderboard", help="the production leaderboard, in the console")

    p_admin = sub.add_parser("admin", help="the dashboard admin account")
    admin_sub = p_admin.add_subparsers(dest="admin_action", required=True)
    p_reset_pw = admin_sub.add_parser("reset-password", help="new one-time password, printed once (created if missing)")
    p_reset_pw.add_argument("--username", default="admin")
    p_reset_pw.add_argument("--yes", action="store_true", help="skip confirmation")
    p_staff = admin_sub.add_parser("create-staff", help="a staff (tutor) account; one-time password printed once")
    p_staff.add_argument("email")
    p_staff.add_argument("--first-name", default="")
    p_staff.add_argument("--last-name", default="")

    p_secrets = sub.add_parser("secrets", help="the vault: what is set (never values); set the Proxmox tokens")
    secrets_sub = p_secrets.add_subparsers(dest="secrets_action")
    p_set = secrets_sub.add_parser("set", help="enter a secret (prompted, or from stdin)")
    p_set.add_argument("name", choices=["proxmox-token", "orchestrator-token", "topology-token", "ssh-key"])

    p_node = sub.add_parser("node", help="the Proxmox node")
    node_sub = p_node.add_subparsers(dest="node_action", required=True)
    node_sub.add_parser("trust", help="once: authorize the WORMS key on VMs that predate it (and for root on the node)")

    p_build = sub.add_parser("build", help="full pipeline, or one component (--list)")
    p_build.add_argument("component", nargs="?", help="component to build (default: all)")
    p_build.add_argument("--list", action="store_true", help="list components and exit")
    p_build.add_argument("make_args", nargs="*", help="extra args passed to ansible-playbook (e.g. --limit db -vvv)")

    sub.add_parser("simulate", help="end-to-end simulation against the deployed platform")

    p_test = sub.add_parser("test", help="run the offline test suite (.venv-test + pytest)")
    p_test.add_argument("pytest_args", nargs="*", help="extra args passed to pytest (e.g. -k shipper -x)")

    p_dbg = sub.add_parser("debug", help="Ansible connection diagnostics for a host/group")
    p_dbg.add_argument("host")

    p_reset = sub.add_parser("reset-database", help="DANGER: wipe + re-migrate + re-init the database")
    p_reset.add_argument("--yes", action="store_true")

    for key, (_, help_text) in DESTRUCTIVE.items():
        p = sub.add_parser(key, help=help_text)
        p.add_argument("--yes", action="store_true")

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = Config.load(args.config)

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
    if args.command == "admin":
        return cmd_admin(config, args)
    if args.command == "secrets":
        return cmd_secrets(config, args)
    if args.command == "node":
        return cmd_node(config, args)
    if args.command == "build":
        return cmd_build(config, args)
    if args.command == "simulate":
        return cmd_build(config, argparse.Namespace(component="simulate", list=False, make_args=[], dry_run=args.dry_run))
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
