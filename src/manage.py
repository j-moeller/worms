#!/usr/bin/env python3
"""worms -- one CLI for the WORMS platform.

Replaces the growing set of Makefile incantations with a single, discoverable
tool you can run from your laptop with the same access tokens you use on the
server. Status information comes from the one source of truth -- the database
VM (and the Proxmox API for live infrastructure state) -- never from guesswork.
Building still goes through the Makefile (run locally on the Proxmox host, or
over SSH with WORMS_SSH); the CLI just drives it with a clear interface.

    worms status                      # components, internet, competition, rounds
    worms teams                       # teams and their members' login status
    worms teams create "Team Name"
    worms teams rename "Old" "New"
    worms teams delete "Team Name"    # users are not affected
    worms users                       # all users: team, login, must-change-pw
    worms leaderboard                 # the production leaderboard, in the console
    worms build                       # = make (idempotent, end to end)
    worms build <component>           # one component (see `worms build --list`)
    worms reset-database              # DANGER: wipe + re-migrate + re-init
    worms emergency-stop              # DANGER: stop and destroy ALL vms

Configuration (env or ~/.worms/config): see src/worms_cli/config.py and
docs/management.md. Run `worms <command> --help` for details.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from worms_cli import build, output  # noqa: E402
from worms_cli.config import Config  # noqa: E402


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def _infra_status(config) -> list[list[str]]:
    from worms_cli.proxmox import Infra, ProxmoxUnavailable

    infra = Infra(config)
    rows: list[list[str]] = []

    if not infra.configured:
        return [["infrastructure", output.status_cell("n/a", "no Proxmox API configured (WORMS_PROXMOX_*)")]]

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


def _internet_status(config) -> list[str]:
    """The host internet flag (/run/worms/internet-allowed) via SSH, if set up."""
    if not config.ssh:
        return ["internet access", output.status_cell("unknown", "set WORMS_SSH to probe the host flag")]
    import subprocess

    probe = subprocess.run(
        ["ssh", config.ssh, "test -e /run/worms/internet-allowed && echo allowed || echo denied"],
        capture_output=True, text=True, timeout=20,
    )  # fmt: skip
    state = probe.stdout.strip()
    if state == "allowed":
        return ["internet access", output.status_cell("warn", "ALLOWED (deny before deploying a sandbox)")]
    if state == "denied":
        return ["internet access", output.status_cell("ok", "denied")]
    return ["internet access", output.status_cell("unknown", probe.stderr.strip()[:60] or "could not probe")]


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
    if action == "create":
        print(queries.create_team(args.name))
        return 0
    if action == "rename":
        print(queries.rename_team(args.old, args.new))
        return 0
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
# build / destructive
# ---------------------------------------------------------------------------


def cmd_build(config, args) -> int:
    if args.list:
        rows = [[name, help_text] for name, (_, help_text) in build.COMPONENTS.items()]
        print(output.table(rows, headers=["component", "what"]))
        return 0
    runner = build.Runner(config, dry_run=args.dry_run)
    try:
        runner.component(args.component, extra=args.make_args)
    except build.BuildError as exc:
        output.error(str(exc))
        return 1
    return 0


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
    targets, help_text = build.DESTRUCTIVE[key]
    if not build.confirm(f"{help_text}. Proceed?", args.yes):
        return 1
    runner = build.Runner(config, dry_run=args.dry_run)
    try:
        runner.make(*targets)
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

    p_teams = sub.add_parser("teams", help="list teams; create/rename/delete")
    team_sub = p_teams.add_subparsers(dest="team_action")
    p_create = team_sub.add_parser("create", help="create a team in the current competition")
    p_create.add_argument("name")
    p_rename = team_sub.add_parser("rename", help="rename a team")
    p_rename.add_argument("old")
    p_rename.add_argument("new")
    p_delete = team_sub.add_parser("delete", help="delete a team (users unaffected)")
    p_delete.add_argument("name")
    p_delete.add_argument("--yes", action="store_true", help="skip confirmation")

    sub.add_parser("users", help="all users: team, login status, one-time password")
    sub.add_parser("leaderboard", help="the production leaderboard, in the console")

    p_build = sub.add_parser("build", help="run the build (make), all or one component")
    p_build.add_argument("component", nargs="?", help="component to build (default: all)")
    p_build.add_argument("--list", action="store_true", help="list components and exit")
    p_build.add_argument("make_args", nargs="*", help="extra args passed to make (e.g. ARGS=...)")

    p_reset = sub.add_parser("reset-database", help="DANGER: wipe + re-migrate + re-init the database")
    p_reset.add_argument("--yes", action="store_true")

    for key in build.DESTRUCTIVE:
        p = sub.add_parser(key, help="DANGER: " + build.DESTRUCTIVE[key][1])
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
    if args.command == "users":
        return cmd_users(config, args)
    if args.command == "leaderboard":
        return cmd_leaderboard(config, args)
    if args.command == "build":
        return cmd_build(config, args)
    if args.command == "reset-database":
        return cmd_reset_database(config, args)
    if args.command in build.DESTRUCTIVE:
        return cmd_destroy(config, args, args.command)
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
