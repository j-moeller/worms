#!/usr/bin/env python3
"""worms -- one CLI for the WORMS platform, one verb per task.

Builds and operates the platform: OpenTofu and Ansible run on this machine
(your laptop with WORMS_SSH=root@<node>, or the Proxmox node), the node's own
part over SSH. Status comes from the database VM and the Proxmox API.
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

INTERNET_FLAG = "/run/worms/internet-allowed"
PVE_CONFIG_DIR = "/etc/pve"  # exists only on a Proxmox node


# ---------------------------------------------------------------------------
# worms status [<topic>]
# ---------------------------------------------------------------------------


def _vm_rows(config) -> list[list[str]]:
    """Every VM: name, vmid, kind, state, uptime, memory (Proxmox API, the topology token)."""
    from worms_cli.proxmox import Infra
    from worms_cli.vms import KINDS

    infra = Infra(config)
    if not infra.configured:
        return [["(VMs)", "", "", output.status_cell("n/a", infra.error or "no Proxmox API configured (worms secrets issue topology-token)"), "", ""]]
    try:
        vms = infra.vms()
    except Exception as exc:  # noqa: BLE001
        return [["(VMs)", "", "", output.status_cell("error", f"Proxmox API: {exc}"), "", ""]]
    rows = []
    for vm in sorted(vms, key=lambda vm: (list(KINDS).index(vm["pool"]) if vm.get("pool") in KINDS else 9, vm["name"])):
        up = int(vm.get("uptime") or 0)
        state = vm.get("status", "?")
        rows.append([
            vm["name"], str(vm["vmid"]), KINDS.get(vm.get("pool") or "", vm.get("pool") or "—"),
            output.status_cell("running" if state == "running" else "stopped", state),
            f"{up // 86400} d {up % 86400 // 3600} h" if up >= 86400 else (f"{up // 3600} h {up % 3600 // 60} min" if up else "—"),
            f"{int(vm.get('mem') or 0) >> 20}/{int(vm.get('maxmem') or 0) >> 20} MB" if state == "running" else "—",
        ])  # fmt: skip
    return rows


def _platform_rows(config) -> list[list[str]]:
    from worms_cli.proxmox import Infra

    rows = [_system_status(config)]
    infra = Infra(config)
    if infra.configured:
        try:
            enabled = infra.firewall_enabled()
            rows.append(["firewall", output.status_cell("active" if enabled else "down", "enabled" if enabled else "DISABLED")])
        except Exception as exc:  # noqa: BLE001
            rows.append(["firewall", output.status_cell("error", f"Proxmox API: {exc}")])
    rows.append(_internet_status(config))
    return rows


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
        return ["internet access", output.status_cell("warn", "ALLOWED (close it with `worms internet disable`)")]
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


def _system_status(config) -> list[str]:
    """Which system this is: the switch in ansible/group_vars/all.yml, by the node's name."""
    from worms_cli.session import SessionError, for_config

    try:
        system = for_config(config).environment()
    except SessionError as exc:
        return ["system", output.status_cell("error", str(exc))]
    return ["system", output.status_cell("ok", f"{system['name']} ({system['node']}, {system['public_name']})")]


def cmd_status(config, args) -> int:
    topic = args.topic
    if topic is None:
        print(output.heading("platform"))
        print(output.table(_platform_rows(config)))
        print()
        print(output.heading("VMs"))
        print(output.table(_vm_rows(config), headers=["VM", "vmid", "kind", "state", "up", "memory"]))
        print()
        print(output.heading("competition (database)"))
        print(output.table(_competition_status(config)))
        return 0
    _with_db(config)
    from worms_cli import queries

    try:
        if topic == "teams":
            return _status_teams(queries)
        if topic == "users":
            return _status_users(queries)
        if topic == "leaderboard":
            return _status_leaderboard(queries)
        if topic == "audit":
            rows = queries.event_rows(hours=args.hours, kind=args.kind, limit=args.limit)
            if not rows:
                print(f"No audit events in the last {args.hours} hour(s).")
                return 0
            print(output.table(rows, headers=["Time (UTC)", "Event", "Written by", "Actor", "Target", "Team", "Client", "Details"]))
            return 0
        if topic == "rounds":
            print(output.table(queries.round_rows(), headers=["Round", "Start (UTC)", "End", "Gameplan", "Check", "Per team", "Attempts"]))
            return 0
        if topic == "jobs":
            print(output.table(queries.job_rows(args.limit if args.limit != 200 else 30),
                               headers=["#", "Job", "State", "Team", "Requested", "Finished", "Error"]))  # fmt: skip
            return 0
        if topic == "devboxes":
            print(output.table(queries.devbox_rows(), headers=["Team", "Devbox", "SSH keys (name, fingerprint)"]))
            return 0
        if topic == "gameplans":
            print(output.table(queries.gameplan_rows(config.repo_root), headers=["Gameplan", "Orchestrator + node", "This checkout"]))
            print(output.color("Why the deployed copy is invalid: journalctl -u orchestrator-conduct-rounds (orchestrator), "
                               "the gameplans job log on the node (/var/log/worms/jobs/).", "dim"))  # fmt: skip
            return 0
    except ValueError as exc:
        output.error(str(exc))
        return 1
    raise AssertionError(topic)


def _status_teams(queries) -> int:
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


def _status_users(queries) -> int:
    rows = []
    for user in queries.user_rows():
        flags = []
        if user["is_staff"]:
            flags.append(output.color("staff", "cyan"))
        if user["must_change_password"]:
            flags.append(output.color("one-time pw", "yellow"))
        rows.append([
            user["username"], user["team"] or output.color("(no team)", "grey"),
            output.status_cell("ok", "yes") if user["logged_in"] else output.status_cell("stopped", "never"), " ".join(flags),
        ])  # fmt: skip
    print(output.table(rows, headers=["User", "Team", "Logged in", "Flags"]))
    return 0


def _status_leaderboard(queries) -> int:
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
        rows.append([place, team.name, str(team.total_points), ", ".join(getattr(team, "round_wins", [])) or ""])
    print(output.table(rows, headers=["#", "Team", "Points", "Rounds won"]))
    return 0


def _with_db(config):
    from worms_cli import django_setup

    try:
        django_setup.setup(config)
    except django_setup.NotConfigured as exc:
        output.error(str(exc))
        raise SystemExit(2)


def _console() -> str:
    """Who ran a console command, for the event tables: `console:<user>@<host>`."""
    import getpass
    import socket

    return f"console:{getpass.getuser()}@{socket.gethostname()}"


# ---------------------------------------------------------------------------
# worms build [<component>...] [--rebuild]
# ---------------------------------------------------------------------------


def _exists(pipe, config):
    """components.plan's question: is this component built?"""
    from worms_cli import components

    cache = {}

    def vm_names():
        if "vms" not in cache:
            cache["vms"] = {vm["name"] for vm in pipe.runner.vms()}
        return cache["vms"]

    def exists(component) -> bool:
        if component.kind == "stack":
            return components.stack_built(config.repo_root, component.stack)
        if component.kind == "node":
            return pipe.runner.node_sh(f"systemctl is-active --quiet {component.node_service}", check=False, quiet=True) == 0
        if component.kind == "devbox":
            return True
        return component.name in vm_names()

    return exists


def _pending_migrations(pipe):
    def pending():
        try:
            return pipe.runner.pending_migrations()
        except build.BuildError as exc:
            return [f"unknown: {exc}"]

    return pending


def cmd_build(config, args) -> int:
    """`worms build [<component>...] [--rebuild]`. The first step that fails ends it, and nothing
    after it runs but closing the internet window and starting the devbox VMs again."""
    from worms_cli import components
    from worms_cli.session import SessionError

    pipe = pipeline.Pipeline(config, ansible_args=" ".join(args.ansible_args))
    code = 0
    try:
        if not args.components:
            if args.rebuild:
                output.error("--rebuild needs components: everything at once is `worms destroy`, then `worms build`")
                return 1
            pipe.build_all()
        else:
            try:
                requested = components.expand(args.components)
                plan = components.plan(requested, _exists(pipe, config), rebuild=args.rebuild,
                                       pending_migrations=_pending_migrations(pipe))  # fmt: skip
            except KeyError as exc:
                output.error(f"no component {exc.args[0]!r}: `worms build --help` lists them")
                return 1
            except ValueError as exc:
                output.error(str(exc))
                return 1
            print(output.heading("plan"))
            print(output.table(plan.table(), headers=["component", "what happens", "what it is"]))
            for note in plan.notes:
                print(output.color(f"note: {note}", "dim"))
            if args.plan:
                return 0
            asks = []
            if plan.added:
                asks.append(f"build {len(plan.added)} more component(s) it needs ({', '.join(plan.added)})")
            if plan.names("recreate"):
                asks.append(f"DESTROY and build again {', '.join(plan.names('recreate'))}"
                            + (": every record in the database is LOST" if "database" in plan.names("recreate") else ""))  # fmt: skip
            if asks and not build.confirm("This will " + "; ".join(asks) + ". Proceed?", args.yes):
                return 1
            infrastructure = components.Plan([step for step in plan.steps if components.COMPONENTS[step[0]].kind != "devbox"],
                                             plan.notes)  # fmt: skip
            if infrastructure.steps:
                pipe.build_components(infrastructure, packages=args.packages)
            if "devbox" in dict(plan.steps):
                code = _build_devboxes(config, rebuild=args.rebuild, teams=args.team)
    except (build.BuildError, SessionError, KeyboardInterrupt) as exc:
        output.error(f"build stopped: {exc}" if str(exc) else "build stopped: interrupted")
        code = 130 if isinstance(exc, KeyboardInterrupt) else 1
    # Also after a failure: a rollback that happened is not undone by one.
    if pipe.services_rolled_back and pipe.devboxes_exist():
        code = _restore_devbox_map(config) or code
    return code


def _build_devboxes(config, rebuild=False, teams=()) -> int:
    """`worms build devbox`: a devbox for every team without one (the orchestrator enqueues the
    build; the round agent on the node builds them, docs/04-dev-environment.md). With --rebuild:
    each team's (or `teams`') devbox from nothing, through the orchestrator as a reset is."""
    _with_db(config)
    if not rebuild:
        from django.core.management import call_command
        from django.core.management.base import CommandError

        try:
            call_command("provision_devboxes")
        except CommandError as exc:
            output.error(str(exc))
            return 1
        return 0
    from core.models.devbox import Devbox
    from orchestrator.rounds.devbox import request_rebuild
    from worms_cli import queries

    try:
        chosen = [queries.team(name) for name in teams] or [box.team for box in Devbox.objects.select_related("team")]
    except ValueError as exc:
        output.error(str(exc))
        return 1
    for team in chosen:
        job, enqueued = request_rebuild(team, fresh=True)
        if job is None:
            print(f"{team.name}: no devbox to rebuild (`worms build devbox` builds it)")
        else:
            print(f"{team.name}: {'enqueued' if enqueued else 'already queued'} devbox_rebuild {job.uuid} (from nothing)")
    print(output.color("The round agent does the work: tail -F /var/log/worms/jobs/latest.log on the node.", "dim"))
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
            "devbox submissions stay unattributed until `worms devbox-reconfigure` has run"
        )
        return 1


def cmd_step(config, args) -> int:
    """`worms step <name>`: one pipeline step (pipeline.STEPS). The round agent runs these, never
    interactively; for troubleshooting too."""
    pipe = pipeline.Pipeline(config, ansible_args=" ".join(args.ansible_args))
    try:
        if args.name == "debug":
            if not args.host:
                output.error("worms step debug <host>: an inventory host or group")
                return 2
            pipe.debug(args.host)
        else:
            pipe.run_step(args.name)
    except build.BuildError as exc:
        output.error(f"step stopped: {exc}")
        return 1
    return 0


# ---------------------------------------------------------------------------
# login / show
# ---------------------------------------------------------------------------


def _vm_list(config) -> str:
    """The VMs there are now, for `worms login --help` and `worms show --help`."""
    from worms_cli.vms import Catalogue

    try:
        names = Catalogue(config).names()
    except Exception as exc:  # noqa: BLE001  (the node may be out of reach)
        return f"(the VMs cannot be listed now: {exc})"
    width = max(len(name) for name, _ in names)
    return "VMs there are now:\n" + "\n".join(f"  {name:<{width}}  {what}" for name, what in names)


def cmd_login(config, args) -> int:
    from worms_cli.vms import Catalogue

    try:
        return Catalogue(config).login(args.vm)
    except build.BuildError as exc:
        output.error(str(exc))
        return 1


def cmd_show(config, args) -> int:
    from worms_cli.vms import Catalogue

    if args.vm == "database" and args.table:
        return _show_table(config, args)
    try:
        print(output.heading(args.vm))
        print(output.table(Catalogue(config).details(args.vm)))
    except build.BuildError as exc:
        output.error(str(exc))
        return 1
    if args.vm == "database":
        _with_db(config)
        from worms_cli import database

        print()
        print(output.heading("tables (`worms show database <table>`)"))
        print(output.table(database.tables(), headers=["table", "rows (estimate)", "size"]))
    return 0


def _show_table(config, args) -> int:
    _with_db(config)
    from worms_cli import database

    try:
        if args.schema:
            columns, extra = database.schema(args.table)
            print(output.table(columns, headers=["column", "type", "null", "default"]))
            for line in extra:
                print(output.color(line, "dim"))
            return 0
        columns, rows = database.rows(args.table, args.limit)
    except KeyError as exc:
        output.error(exc.args[0])
        return 1
    print(output.table(rows, headers=columns))
    print(output.color(f"the newest {len(rows)} row(s) (--limit N; --schema for the columns)", "dim"))
    return 0


# ---------------------------------------------------------------------------
# internet / destroy / reset / emergency-stop
# ---------------------------------------------------------------------------


def cmd_internet(config, args) -> int:
    pipe = pipeline.Pipeline(config)
    try:
        pipe.internet_allow() if args.state == "enable" else pipe.internet_deny()
    except build.BuildError as exc:
        output.error(str(exc))
        return 1
    return 0


def cmd_destroy(config, args) -> int:
    if args.all:
        what, method = "Stop, unlock and destroy EVERY VM and every stack, the firewall and the platform too", "terminate"
    else:
        what, method = "Destroy the VM stacks, the database VM and its records too (the firewall and platform stay)", "destroy"
    if not build.confirm(f"{what}. Proceed?", args.yes):
        return 1
    try:
        getattr(pipeline.Pipeline(config), method)()
    except build.BuildError as exc:
        output.error(str(exc))
        return 1
    return 0


def cmd_reset(config, args) -> int:
    if args.what == "database":
        if not build.confirm("Wipe ALL dashboard data (teams, users, captures, logs) and re-init?", args.yes):
            return 1
        _with_db(config)
        from django.core.management import call_command

        call_command("reset_database", "--yes")
        return 0
    if args.what == "services":
        if not build.confirm("Return the service VMs (ephemeral, private internet) to their snapshot? What they hold "
                             "since is lost", args.yes):  # fmt: skip
            return 1
        pipe = pipeline.Pipeline(config)
        try:
            pipe.round_reset_services()
        except build.BuildError as exc:
            output.error(str(exc))
            return 1
        finally:
            if pipe.services_rolled_back and pipe.devboxes_exist():
                _restore_devbox_map(config)
        return 0
    return _reset_devboxes(config, args)


def _reset_devboxes(config, args) -> int:
    """Each team's devbox back to its snapshot, through the orchestrator (as the dashboard's reset):
    the version goes up, so its dev score starts over; earlier records are kept."""
    _with_db(config)
    from core import events
    from core.models.devbox import Devbox
    from orchestrator.rounds.devbox import request_rebuild
    from worms_cli import queries

    if not args.teams and not args.all_teams:
        output.error("which team's devbox? `worms reset devbox <team>...` or `--all-teams`")
        return 2
    try:
        teams = [box.team for box in Devbox.objects.select_related("team")] if args.all_teams else [queries.team(n) for n in args.teams]
    except ValueError as exc:
        output.error(str(exc))
        return 1
    if not build.confirm(f"Reset the devbox of {', '.join(t.name for t in teams) or 'no team'}: its VMs back to their "
                         "snapshot, its dev score starts over?", args.yes):  # fmt: skip
        return 1
    for team in teams:
        job, enqueued = request_rebuild(team)
        events.team("devbox_reset", actor=_console(), team=team)
        if job is None:
            print(f"{team.name}: no devbox (version bumped all the same)")
        else:
            print(f"{team.name}: {'enqueued' if enqueued else 'already queued'} devbox_rebuild {job.uuid}")
    return 0


def cmd_emergency_stop(config, args) -> int:
    if not build.confirm("Freeze: hard-stop ALL VMs (nothing is destroyed). Proceed?", args.yes):
        return 1
    try:
        pipeline.Pipeline(config).stop_all()
    except build.BuildError as exc:
        output.error(str(exc))
        return 1
    return 0


# ---------------------------------------------------------------------------
# test
# ---------------------------------------------------------------------------


def cmd_test(config, args) -> int:
    """The offline test suite (a .venv-test venv + pytest), here; with --live, the end-to-end
    simulation on the deployed platform (ansible/Simulate.yml)."""
    if args.live:
        pipe = pipeline.Pipeline(config, ansible_args=" ".join(args.extra))
        try:
            pipe.simulate()
        except build.BuildError as exc:
            output.error(str(exc))
            return 1
        return 0
    repo = str(config.repo_root)
    venv = f"{repo}/.venv-test"
    pytest = f"{venv}/bin/pytest"
    if not os.path.exists(pytest):
        print(output.color("creating .venv-test ...", "dim"))
        subprocess.run([sys.executable, "-m", "venv", venv], check=True)
        subprocess.run([f"{venv}/bin/pip", "install", "-q", "-r", f"{repo}/tests/requirements.txt"], check=True)
    env = {**os.environ, "PATH": f"{venv}/bin:" + os.environ.get("PATH", "")}
    return subprocess.run([pytest, *args.extra], cwd=repo, env=env).returncode


# ---------------------------------------------------------------------------
# accounts, teams, devbox keys
# ---------------------------------------------------------------------------


def cmd_admin_reset_password(config, args) -> int:
    """A new one-time password for the dashboard admin, printed once here (stdout), never stored
    (manage.py reset_admin_password)."""
    _with_db(config)
    from core.models.user import User
    from django.core.management import call_command
    from django.core.management.base import CommandError

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


def cmd_admin_create_staff(config, args) -> int:
    """A staff (tutor) account with a one-time password: the public dashboard manages student
    accounts but cannot create staff (docs/07-security.md)."""
    _with_db(config)
    from core import accounts, events

    created = accounts.create_account(args.email, args.first_name, args.last_name, staff=True)
    if created is None:
        output.error(f"an account {args.email!r} already exists")
        return 1
    events.account("staff_created", actor=_console(), target=created.user)
    print(f"staff: created {created.user.username!r}; one-time password below (shown once):", file=sys.stderr)
    print(created.password)
    return 0


def cmd_delete_team(config, args) -> int:
    _with_db(config)
    from worms_cli import queries

    if not build.confirm(f"Delete team {args.name!r} with everything it owns (its users are kept)?", args.yes):
        return 1
    try:
        print(queries.delete_team(args.name))
    except ValueError as exc:
        output.error(str(exc))
        return 1
    from core import events

    events.team("deleted", actor=_console(), team_name=args.name)
    return 0


def _public_key(value: str) -> str:
    """The key itself, a .pub file, or `-` for stdin."""
    if value == "-":
        return sys.stdin.read().strip()
    path = Path(value).expanduser()
    if " " not in value and path.is_file():
        return path.read_text().strip()
    return value.strip()


def cmd_devbox_add_key(config, args) -> int:
    """Add an SSH key to a team's devbox, as a member does on the dashboard: the same checks
    (core/ssh_keys.py), then the orchestrator has the round agent push the keys again."""
    _with_db(config)
    from core import events, ssh_keys
    from core.models.devbox import Devbox, DevboxSSHKey
    from django.db import IntegrityError, transaction
    from worms_cli import queries

    key = _public_key(args.key)
    if not ssh_keys.valid(key):
        output.error("not a single-line OpenSSH public key (ssh-ed25519, ssh-rsa, ecdsa, sk-...) with a plain comment")
        return 1
    try:
        team = queries.team(args.team)
    except ValueError as exc:
        output.error(str(exc))
        return 1
    existing = DevboxSSHKey.objects.filter(key_fingerprint=DevboxSSHKey.fingerprint(key)).first()
    if existing is not None and existing.team_id != team.id:
        output.error(f"that key is registered by team {existing.team.name!r}: one key, one team")
        return 1
    if existing is None:
        if DevboxSSHKey.objects.filter(team=team).count() >= ssh_keys.MAX_KEYS_PER_TEAM:
            output.error(f"{team.name} has {ssh_keys.MAX_KEYS_PER_TEAM} keys already: remove one first")
            return 1
        name = args.name or (key.split(" ")[2] if len(key.split(" ")) == 3 else "")
        try:
            with transaction.atomic():
                DevboxSSHKey.objects.create(team=team, name=name[:200], public_key=key)
                events.credential("ssh_key_added", actor=_console(), team=team, name=name, public_key=key)
        except IntegrityError:
            output.error("that key is registered already")
            return 1
        print(f"{team.name}: key added")
    else:
        print(f"{team.name}: has that key already; pushing the keys again")
    if not Devbox.objects.filter(team=team).exists():
        print("It is installed when the team's devbox is built (`worms build devbox`).")
        return 0
    return _request_devbox_reconfigure(config)


def cmd_devbox_remove_key(config, args) -> int:
    _with_db(config)
    from core import events
    from core.models.devbox import Devbox, DevboxSSHKey
    from worms_cli import queries

    try:
        team = queries.team(args.team)
    except ValueError as exc:
        output.error(str(exc))
        return 1
    keys = DevboxSSHKey.objects.filter(team=team)
    matches = [k for k in keys if args.key in (k.name, k.key_fingerprint) or (len(args.key) >= 8 and k.key_fingerprint.startswith(args.key))]
    if len(matches) != 1:
        output.error(f"{len(matches)} of {team.name}'s keys match {args.key!r}: give its name or fingerprint "
                     "(`worms status devboxes`)")  # fmt: skip
        return 1
    key = matches[0]
    if not build.confirm(f"Remove {team.name}'s key {key.name or key.key_fingerprint[:16]!r} from its devbox?", args.yes):
        return 1
    key.delete()
    events.credential("ssh_key_removed", actor=_console(), team=team, name=key.name, public_key=key.public_key)
    print(f"{team.name}: key removed")
    return _request_devbox_reconfigure(config) if Devbox.objects.filter(team=team).exists() else 0


# ---------------------------------------------------------------------------
# secrets / trust-node
# ---------------------------------------------------------------------------


def cmd_secrets(config, args) -> int:
    """`worms secrets`: what the vault holds (never a value); `worms secrets
    set <name>`: enter one, prompted without echo or read from stdin; `worms
    secrets issue <name>`: create or rotate a Proxmox token and store it; `worms
    secrets check`: ask Proxmox whether the stored tokens are right."""
    import getpass

    from worms_cli.secrets import EXTERNAL, Vault, VaultError, vms_deployed
    from worms_cli.session import SessionError, for_config
    from worms_cli.tokens import TOKENS, TokenError, check, issue

    vault = Vault(config.repo_root)
    try:
        if args.secrets_action == "issue":
            spec = TOKENS[args.name]
            vault.set(args.name, issue(spec, for_config(config).node_run))
            print(f"{args.name}: issued {spec.full_id} ({spec.role} on {spec.path}), stored in {vault.file}")
            print(output.color("Deliver it to its users with `worms build`.", "dim"))
            return 0
        if args.secrets_action == "check":
            vault.ensure(new_ssh_key=not vms_deployed(config.repo_root))
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
        vault.ensure(new_ssh_key=not vms_deployed(config.repo_root))
        rows = [[name, what, output.status_cell("ok" if state == "set" else ("warn" if "MISSING" in state else "stopped"),
                                                state)] for name, what, state in vault.status()]  # fmt: skip
    except (VaultError, KeyError, TokenError, SessionError) as exc:
        output.error(str(exc))
        return 1
    print(output.table(rows, headers=["secret", "what", "state"]))
    print(output.color(f"vault: {vault.file} (key {vault.key_file}); set one with `worms secrets set <name>`", "dim"))
    return 0


def cmd_trust_node(config, args) -> int:
    """Let the WORMS key (vault) in, the one key the build logs in with: for root on the node,
    and on the VMs that exist already (they accept root's old key on the node, which logs in to
    them). Once, with your own login: over SSH from a laptop, right here on the node. It fails
    unless every VM took it."""
    import shlex

    from worms_cli.secrets import SSH_PUBLIC_KEY
    from worms_cli.session import SessionError, for_config

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
failed=0
for ip in $(printf '%s\n' {' '.join(ips)} $known | sort -u); do
  [ "$ip" = 127.0.0.1 ] && continue
  if {vm} debian@"$ip" {shlex.quote(add)} >/dev/null 2>&1; then echo "vm $ip: ok"
  else echo "vm $ip: FAILED (not reachable with root's key)"; failed=$((failed + 1)); fi
done
[ "$failed" -eq 0 ] || {{ echo "$failed VM(s) do not accept the WORMS key: fix their SSH, then run this again" >&2; exit 1; }}
"""
    if config.ssh:
        print(output.color(f"$ ssh {config.ssh} ...  (your own SSH login)", "dim"))
        argv = ["ssh", config.ssh, script]
    else:
        print(output.color("$ (this node, as root) ...", "dim"))
        argv = ["sh", "-c", script]
    if subprocess.run(argv).returncode != 0:
        output.error("the WORMS key is not let in everywhere yet: see the lines above")
        return 1
    try:
        session.check_node()
    except SessionError as exc:
        output.error(str(exc))
        return 1
    print(output.color("root on the node accepts the WORMS key (as OpenTofu logs in): ok", "green"))
    return 0


# ---------------------------------------------------------------------------
# help: every command under what it is for
# ---------------------------------------------------------------------------


class Section(NamedTuple):
    """One part of `worms --help`: (as typed, what it does) per command. build_parser takes each
    command's help from here, so a new command has to say what it is for."""

    title: str
    about: str
    commands: tuple[tuple[str, str], ...]


STATUS_TOPICS = {
    "teams": "teams, their members, who has signed in",
    "users": "every user: team, login status, one-time password",
    "leaderboard": "the leaderboard, as the dashboard shows it",
    "audit": "the event tables: sign-ins, passwords, view-as, keys, SSH gateway logins (newest first)",
    "rounds": "the rounds: window, gameplan and its check, attempts by state",
    "gameplans": "the gameplans: valid on the orchestrator and the node? why not, by this checkout's check",
    "jobs": "the round agent's latest host jobs, and why one failed",
    "devboxes": "every team's devbox and SSH keys",
}

SECTIONS = (
    Section(
        "see",
        "These change nothing.",
        (
            ("status [<topic>]", "every VM and its state, the internet window, the competition; or one topic: "
                                 + ", ".join(STATUS_TOPICS)),  # fmt: skip
            ("show <vm> [<table>]", "one VM in detail: state, resources, networks, snapshots, login; "
                                    "`show database [<table>]`: its tables, a table's rows or --schema"),  # fmt: skip
            ("login <vm>", "a shell on a VM over its management SSH (`worms login --help` lists them)"),
        ),
    ),
    Section(
        "build",
        "`worms build` builds everything; `worms build <component>` one service, and what it needs first (it shows "
        "the plan and asks). Packages need the internet window: devbox VMs are shut down while it is open, and it is "
        "refused during a round.",
        (
            ("build [<component>...]", "build what is missing, configure what exists; --rebuild recreates (`worms build --help`); "
                                       "after `--`: arguments for ansible-playbook"),
            ("internet enable|disable", "open or close the internet window by hand (refused while a team VM runs)"),
        ),
    ),
    Section(
        "operate",
        "The running competition, from the console. Teams, students and rounds are managed on the dashboard "
        "(/staff/), and a round starts by itself.",
        (
            ("reset database", "DANGER: wipe the database, migrate and initialise it again"),
            ("reset services", "the service VMs (ephemeral, private internet) back to their snapshot"),
            ("reset devbox <team>...", "teams' devboxes back to their snapshot, through the orchestrator (--all-teams)"),
            ("devbox-add-key <team> <key>", "add an SSH key to a team's devbox (the orchestrator pushes it)"),
            ("devbox-remove-key <team> <key>", "remove one, by its name or fingerprint"),
            ("devbox-reconfigure", "push every team's SSH keys and the devbox map again (a reset does this itself)"),
            ("admin-reset-password", "one-time password for the dashboard admin, printed once (created if missing)"),
            ("admin-create-staff <email>", "a staff (tutor) account; one-time password printed once"),
            ("delete-team <name>", "delete a team with everything it owns (its users stay)"),
        ),
    ),
    Section(
        "set up",
        "Once per controller: the vault, the tokens, the key the build logs in with.",
        (
            ("secrets", "the vault: what is set, never a value (creates it and the missing generated secrets)"),
            ("secrets issue <name>", "create or rotate a Proxmox token and store it"),
            ("secrets set <name>", "store a secret by hand (prompted, or from stdin)"),
            ("secrets check", "ask Proxmox whether the stored tokens work, with exactly their rights"),
            ("trust-node", "let the WORMS key in, for root on the node and on VMs that predate it"),
        ),
    ),
    Section(
        "stop and tear down",
        "Each asks first, unless given --yes.",
        (
            ("emergency-stop", "freeze: hard-stop ALL VMs (destroys nothing); the only hard stop of a round"),
            ("destroy [--all]", "destroy the VM stacks (keeps firewall and platform); --all: everything"),
        ),
    ),
    Section(
        "develop",
        "For working on the code.",
        (
            ("test [--live]", "the offline test suite; --live: the simulation on the deployed platform; after `--`: arguments "
                             "for pytest (or ansible-playbook)"),
            ("step <name>", "one pipeline step (the round agent's, and for troubleshooting): "
                            + ", ".join([*pipeline.STEPS, "debug"])),  # fmt: skip
        ),
    ),
)

HELP_WIDTH = 79


def _command_name(usage: str) -> str:
    """`secrets issue <name>` -> `secrets issue`, `internet enable|disable` -> `internet`: the
    words that select the command, up to its first argument."""
    words = []
    for word in usage.split(" "):
        if word.startswith(("<", "[")) or "|" in word:
            break
        words.append(word)
    return " ".join(words)


def _command_help(command: str) -> str:
    for section in SECTIONS:
        for usage, text in section.commands:
            if _command_name(usage) == command:
                return text
    raise KeyError(f"`worms {command}` is under no section of SECTIONS")


def _add(subparsers, command: str, **kwargs) -> argparse.ArgumentParser:
    """Add `worms <command>`, with its line in SECTIONS as its help (wrapped here when the parser
    keeps its own layout, for its epilog)."""
    text = _command_help(command)
    description = textwrap.fill(text, HELP_WIDTH) if kwargs.get("formatter_class") is argparse.RawDescriptionHelpFormatter else text
    return subparsers.add_parser(command.split()[-1], help=text, description=description, **kwargs)


def _format_rows(rows: list[tuple[str, str]], column: int) -> list[str]:
    return [
        textwrap.fill(text, HELP_WIDTH, initial_indent=f"  {usage:<{column}}  ", subsequent_indent=" " * (column + 4),
                      break_long_words=False, break_on_hyphens=False)  # fmt: skip
        for usage, text in rows
    ]


def _commands_overview() -> str:
    """The sections of `worms --help`."""
    column = max(len(usage) for section in SECTIONS for usage, _ in section.commands)
    parts = []
    for section in SECTIONS:
        about = textwrap.fill(section.about, HELP_WIDTH, initial_indent="  ", subsequent_indent="  ")
        parts.append("\n".join([output.heading(section.title), about, *_format_rows(list(section.commands), column)]))
    return "\n\n".join(parts)


def _components_overview() -> str:
    """`worms build --help`: every component, what it needs, the groups."""
    from worms_cli import components

    column = max(len(name) for name in [*components.COMPONENTS, *components.GROUPS])
    rows = [(name, f"{c.help} (needs {', '.join(c.deps)})" if c.deps else c.help) for name, c in components.COMPONENTS.items()]
    groups = [(name, f"{about}: {', '.join(members)}") for name, (members, about) in components.GROUPS.items()]
    return "\n".join([
        output.heading("components, in build order"),
        *_format_rows(rows, column),
        "",
        output.heading("groups"),
        *_format_rows(groups, column),
        "",
        textwrap.fill("A component that exists is configured again (no internet window: packages are skipped, unless "
                      "--packages); one that is missing is created, and so is a missing dependency, after the plan is "
                      "shown and you agree. --rebuild destroys the component's VM and builds it again. Service VMs are "
                      "configured from their snapshot and snapshotted again, with the devboxes off meanwhile. A "
                      "dashboard whose code needs migrations brings the orchestrator (which migrates) and the "
                      "database (grants) along.", HELP_WIDTH),  # fmt: skip
    ])  # fmt: skip


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def _yes(parser) -> None:
    parser.add_argument("--yes", action="store_true", help="do not ask first")


def build_parser(vm_list: str = "") -> argparse.ArgumentParser:
    """`vm_list`: the VMs there are now, for `login --help` and `show --help` (main asks the node
    only when one of them is asked for)."""
    parser = argparse.ArgumentParser(
        prog="worms",
        usage="worms [-h] [--config FILE] <command> ...",
        description=f"{__doc__}\n{_commands_overview()}",
        epilog="Configuration (env or ~/.worms/config, no secrets): src/worms_cli/config.py;\n"
        "secrets: src/worms_cli/secrets.py, docs/07-security.md#secrets.\n"
        "Run `worms <command> --help` for details; docs/03-manage-py.md for the whole.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", metavar="FILE", help="path to a KEY=value config file (default ~/.worms/config)")
    sub = parser.add_subparsers(dest="command", required=True, prog="worms", metavar="<command>", help=argparse.SUPPRESS)
    raw = {"formatter_class": argparse.RawDescriptionHelpFormatter}

    p = _add(sub, "status", epilog="topics:\n" + "\n".join(_format_rows(list(STATUS_TOPICS.items()), 11)), **raw)
    p.add_argument("topic", nargs="?", choices=list(STATUS_TOPICS), metavar="topic")
    p.add_argument("--hours", type=int, default=24, help="audit: how far back (default 24)")
    p.add_argument("--kind", default=None, help="audit: only events starting with this: login, password, team.joined, ...")
    p.add_argument("--limit", type=int, default=200, help="audit, jobs: at most this many rows")

    p = _add(sub, "show", epilog=vm_list, **raw)
    p.add_argument("vm")
    p.add_argument("table", nargs="?", help="with `database`: a table's newest rows")
    p.add_argument("--schema", action="store_true", help="the table's columns, indexes and constraints instead")
    p.add_argument("--limit", type=int, default=20, help="rows to show (default 20)")

    p = _add(sub, "login", epilog=vm_list, **raw)
    p.add_argument("vm")

    p = _add(sub, "build", epilog=_components_overview(), **raw)
    p.add_argument("components", nargs="*", metavar="component", help="components or groups (default: everything)")
    p.add_argument("--rebuild", action="store_true", help="destroy the components' VMs and build them again")
    p.add_argument("--packages", action="store_true", help="install packages too (opens the internet window)")
    p.add_argument("--plan", action="store_true", help="show the plan, build nothing")
    p.add_argument("--team", action="append", default=[], help="devbox --rebuild: only this team's (repeatable)")
    _yes(p)

    p = _add(sub, "internet")
    p.add_argument("state", choices=["enable", "disable"])

    p = sub.add_parser("reset", help="reset a part to its clean state", description="Reset the database, the service "
                       "VMs or teams' devboxes to their clean state.")  # fmt: skip
    reset_sub = p.add_subparsers(dest="what", required=True, metavar="database|services|devbox")
    _yes(_add(reset_sub, "reset database"))
    _yes(_add(reset_sub, "reset services"))
    p_devbox = _add(reset_sub, "reset devbox")
    p_devbox.add_argument("teams", nargs="*", metavar="team")
    p_devbox.add_argument("--all-teams", action="store_true", help="every team's devbox")
    _yes(p_devbox)

    p = _add(sub, "devbox-add-key")
    p.add_argument("team")
    p.add_argument("key", help="the public key (quoted), a .pub file, or - for stdin")
    p.add_argument("--name", default="", help="a name for it (default: the key's comment)")
    p = _add(sub, "devbox-remove-key")
    p.add_argument("team")
    p.add_argument("key", help="the key's name, or its fingerprint (at least 8 characters of it)")
    _yes(p)
    _add(sub, "devbox-reconfigure")
    p = _add(sub, "admin-reset-password")
    p.add_argument("--username", default="admin")
    _yes(p)
    p = _add(sub, "admin-create-staff")
    p.add_argument("email")
    p.add_argument("--first-name", default="")
    p.add_argument("--last-name", default="")
    p = _add(sub, "delete-team")
    p.add_argument("name")
    _yes(p)

    p = _add(sub, "secrets")
    secrets_sub = p.add_subparsers(dest="secrets_action")
    p_issue = _add(secrets_sub, "secrets issue")
    p_issue.add_argument("name", choices=["proxmox-token", "orchestrator-token", "topology-token"])
    _add(secrets_sub, "secrets check")
    p_set = _add(secrets_sub, "secrets set")
    p_set.add_argument("name", choices=["proxmox-token", "orchestrator-token", "topology-token", "ssh-key"])
    _add(sub, "trust-node")

    _yes(_add(sub, "emergency-stop"))
    p = _add(sub, "destroy")
    p.add_argument("--all", action="store_true", help="also every VM outside the stacks, the firewall and the platform")
    _yes(p)

    p = _add(sub, "test")
    p.add_argument("--live", action="store_true", help="the end-to-end simulation on the deployed platform")
    p = _add(sub, "step")
    p.add_argument("name", choices=[*pipeline.STEPS, "debug"], metavar="name")
    p.add_argument("host", nargs="?", help="debug: an inventory host or group")
    return parser


HANDLERS = {
    "status": cmd_status, "show": cmd_show, "login": cmd_login, "build": cmd_build, "internet": cmd_internet,
    "reset": cmd_reset, "devbox-add-key": cmd_devbox_add_key, "devbox-remove-key": cmd_devbox_remove_key,
    "devbox-reconfigure": lambda config, args: _request_devbox_reconfigure(config),
    "admin-reset-password": cmd_admin_reset_password, "admin-create-staff": cmd_admin_create_staff,
    "delete-team": cmd_delete_team, "secrets": cmd_secrets, "trust-node": cmd_trust_node,
    "emergency-stop": cmd_emergency_stop, "destroy": cmd_destroy, "test": cmd_test, "step": cmd_step,
}  # fmt: skip


def _split(argv: list[str]) -> tuple[list[str], list[str]]:
    """The command's own arguments, and those after `--` (for ansible-playbook or pytest)."""
    if "--" in argv:
        at = argv.index("--")
        return argv[:at], argv[at + 1 :]
    return argv, []


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    vm_list = ""
    if len(argv) >= 2 and argv[0] in ("login", "show") and ("-h" in argv or "--help" in argv):
        vm_list = _vm_list(Config.load())
    parser = build_parser(vm_list)
    own, passed = _split(argv)
    args = parser.parse_args(own)
    args.ansible_args = args.extra = passed
    config = Config.load(args.config)
    _internet_notice(config)
    return HANDLERS[args.command](config, args)


if __name__ == "__main__":
    sys.exit(main())
