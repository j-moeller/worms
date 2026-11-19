#!/usr/bin/env python3
"""worms -- one CLI for the WORMS platform, one verb per task.

Builds and operates the platform: OpenTofu and Ansible run on this machine
(your laptop with WORMS_SSH=root@<node>, or the Proxmox node), the node's own
part over SSH. Status comes from the database VM and the Proxmox API.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

if __name__ == "__main__" and sys.argv[1:2] == ["__complete"]:
    # A TAB (worms_cli/completion.py): it answers before the imports below, which would cost it a
    # quarter of a second, and reads local files only.
    from worms_cli.completion import complete_main

    sys.exit(complete_main(sys.argv[2:]))

import argparse  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402
import textwrap  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import NamedTuple  # noqa: E402

from worms_cli import build, output, pipeline, power  # noqa: E402
from worms_cli.config import Config  # noqa: E402

INTERNET_FLAG = "/run/worms/internet-allowed"
PVE_CONFIG_DIR = "/etc/pve"  # exists only on a Proxmox node


# ---------------------------------------------------------------------------
# worms status [<topic>]
# ---------------------------------------------------------------------------


VM_HEADERS = ["VM", "vmid", "kind", "team", "state", "up", "memory"]


def _list_vms(config) -> tuple[list | None, str]:
    """(every VM, from the Proxmox API with the topology token, "") or (None, why not as a cell)."""
    from worms_cli.proxmox import Infra

    infra = Infra(config)
    if not infra.configured:
        return None, output.status_cell("n/a", infra.error or "no Proxmox API configured (worms secrets issue topology-token)")
    try:
        return infra.vms(), ""
    except Exception as exc:  # noqa: BLE001
        return None, output.status_cell("error", f"Proxmox API: {exc}")


def _vm_rows(config, listing=None) -> list[list[str]]:
    """Every VM: name, vmid, kind, team, state, uptime, memory (Proxmox API, the topology token;
    the team from the team map, worms_cli/teams.py)."""
    from worms_cli import teams
    from worms_cli.vms import KINDS

    vms, problem = listing or _list_vms(config)
    if vms is None:
        return [["(VMs)", "", "", "", problem, "", ""]]
    names = [vm["name"] for vm in vms]
    known = teams.team_map(config, names) if any(teams.of_vm(name) for name in names) else {}

    def team(name):
        if name == teams.SHARED_GATEWAY:
            return "(every team)"
        found = teams.of_vm(name)
        return (known.get(found[0]) or teams.Team(found[0])).label if found else ""

    rows = []
    for vm in sorted(vms, key=lambda vm: (list(KINDS).index(vm["pool"]) if vm.get("pool") in KINDS else 9, vm["name"])):
        up = int(vm.get("uptime") or 0)
        state = vm.get("status", "?")
        rows.append([
            vm["name"], str(vm["vmid"]), KINDS.get(vm.get("pool") or "", vm.get("pool") or "—"), team(vm["name"]),
            output.status_cell("running" if state == "running" else "stopped", state),
            f"{up // 86400} d {up % 86400 // 3600} h" if up >= 86400 else (f"{up // 3600} h {up % 3600 // 60} min" if up else "—"),
            f"{int(vm.get('mem') or 0) >> 20}/{int(vm.get('maxmem') or 0) >> 20} MB" if state == "running" else "—",
        ])  # fmt: skip
    return rows


def _platform_rows(config, internet=None) -> list[list[str]]:
    from worms_cli.proxmox import Infra

    rows = [_system_status(config)]
    infra = Infra(config)
    if infra.configured:
        try:
            enabled = infra.firewall_enabled()
            rows.append(["firewall", output.status_cell("active" if enabled else "down", "enabled" if enabled else "DISABLED")])
        except Exception as exc:  # noqa: BLE001
            rows.append(["firewall", output.status_cell("error", f"Proxmox API: {exc}")])
    rows.append(internet or _internet_status(config))
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


NODE_PROBE = r"""
cat {held} 2>/dev/null; echo; echo '#--'
cat {window} 2>/dev/null; echo; echo '#--'
grep -lx '\[clean\]' /etc/pve/qemu-server/*.conf 2>/dev/null; echo '#--'
for unit in {units}; do printf '%s %s\n' "$unit" "$(systemctl is-active "$unit")"; done
"""


def _attention(config, listing, internet) -> list[list[str]]:
    """`worms status`'s first block: what needs a look. The internet window open; devbox VMs held
    by a round or stopped by the window; service VMs without a `clean` snapshot; migrations the
    database lacks; host jobs failed in the last 24 h; node services not active. One command on
    the node (the records, the VMs' configs, systemd) and the database, each when it answers."""
    import json
    import shlex

    from worms_cli import services
    from worms_cli.pipeline import SERVICE_POOLS

    rows, unchecked = [], []
    if "ALLOWED" in internet[1]:
        rows.append(["internet window", output.status_cell("warn", "OPEN: no team VM may run; `worms internet disable`")])
    vms, _ = listing
    units = [unit for service in services.SERVICES.values() if service.where == "node" for unit in service.up_units]
    if config.ssh or os.path.isdir(PVE_CONFIG_DIR):
        from worms_cli.session import SessionError, for_config

        script = NODE_PROBE.format(held=power.HELD_DEVBOXES, window=power.WINDOW_STOPPED, units=" ".join(map(shlex.quote, units)))
        try:
            code, out, err = for_config(config).node_run(["sh", "-c", script])
            held, window, clean, active = (out.split("#--\n") + ["", "", "", ""])[:4]
        except (SessionError, OSError) as exc:
            code, held, window, clean, active, err = 1, "", "", "", "", str(exc)
        if code != 0 and not active:
            unchecked.append(f"the node ({err.strip()[:60]})")
        else:
            for text, what, hint in ((held, "held by a round", "its release starts them (`worms power start --held` once it is over)"),
                                     (window, "stopped by the internet window", "`worms internet disable` starts them")):  # fmt: skip
                try:
                    listed = (json.loads(text) if text.strip() else {}).get("vms", [])
                except (ValueError, AttributeError):
                    listed = [{"name": "(unreadable record)"}]
                if listed:
                    rows.append(["devbox VMs", output.status_cell("warn", f"{len(listed)} {what}: {', '.join(vm['name'] for vm in listed[:6])}"
                                                                  + ("…" if len(listed) > 6 else "") + f"; {hint}")])  # fmt: skip
            if vms is not None:
                with_clean = {int(m) for m in re.findall(r"/(\d+)\.conf", clean)}
                lacking = sorted(vm["name"] for vm in vms if vm.get("pool") in SERVICE_POOLS and int(vm["vmid"]) not in with_clean)
                if lacking:
                    rows.append(["snapshots", output.status_cell("warn", f"no `clean` snapshot on {', '.join(lacking)}: a reset "
                                                                 "leaves them as they are (`worms build services --rebuild`)")])  # fmt: skip
            down = [line.split()[0] for line in active.splitlines() if line.split() and line.split()[-1] != "active"]
            if down:
                rows.append(["node services", output.status_cell("error", f"not active: {', '.join(down)} (`worms log agent`, "
                                                                 "`worms log firewall`)")])  # fmt: skip
    else:
        unchecked.append("the node (not on it: set WORMS_SSH)")
    try:
        from worms_cli import django_setup

        django_setup.setup(config)
        from datetime import timedelta

        from core import clock
        from core.models.rounds import HostJob

        failed = list(HostJob.objects.filter(state=HostJob.State.FAILED, finished_at__gte=clock.now() - timedelta(hours=24))
                      .order_by("-id").values_list("id", "kind")[:20])  # fmt: skip
        if failed:
            rows.append(["host jobs", output.status_cell("error", f"{len(failed)} failed in 24 h: "
                                                         + ", ".join(f"#{n} {kind}" for n, kind in failed[:5])
                                                         + f" (`worms log {failed[0][0]}`)")])  # fmt: skip
        pending = build.Runner(config).pending_migrations()
        if pending:
            rows.append(["migrations", output.status_cell("warn", f"the database lacks {len(pending)} of this checkout's "
                                                          "(`worms deploy orchestrator`)")])  # fmt: skip
    except Exception as exc:  # noqa: BLE001  (not configured, not reachable: said, not fatal)
        unchecked.append(f"the database ({str(exc).splitlines()[0][:60] if str(exc) else type(exc).__name__})")
    if not rows:
        rows.append(["", output.status_cell("ok", "nothing found")])
    if unchecked:
        rows.append(["not checked", output.status_cell("unknown", "; ".join(unchecked))])
    return rows


def cmd_status(config, args) -> int:
    topic = args.topic
    if topic is None:
        listing, internet = _list_vms(config), _internet_status(config)
        print(output.heading("attention"))
        print(output.table(_attention(config, listing, internet)))
        print()
        print(output.heading("platform"))
        print(output.table(_platform_rows(config, internet)))
        print()
        print(output.heading("VMs"))
        print(output.table(_vm_rows(config, listing), headers=VM_HEADERS))
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
            print(output.color("Why the deployed copy is invalid: `worms log conductor` (the orchestrator's check), "
                               "and the newest gameplans job's log (`worms status jobs`, then `worms log <#>`).", "dim"))  # fmt: skip
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
        if component.kind in ("devbox", "gameplans"):
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
            _print_plan(plan, args.packages)
            configured = [name for name, action in plan.steps
                          if action == "reconfigure" and components.COMPONENTS[name].kind in ("vm", "node")]  # fmt: skip
            if configured:
                print(output.color(f"note: {', '.join(configured)} exist(s): `worms deploy {' '.join(configured)}` configures "
                                   "them too, never runs OpenTofu, and restarts the agent and the conductor after", "dim"))  # fmt: skip
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
                code = _build_devboxes(config, rebuild=args.rebuild, teams=args.team, wait=getattr(args, "wait", False))
    except (build.BuildError, SessionError, KeyboardInterrupt) as exc:
        output.error(f"build stopped: {exc}" if str(exc) else "build stopped: interrupted")
        code = 130 if isinstance(exc, KeyboardInterrupt) else 1
    # Also after a failure: a rollback that happened is not undone by one.
    if pipe.services_rolled_back and pipe.devboxes_exist():
        code = _restore_devbox_map(config) or code
    return code


def _print_plan(plan, packages: bool) -> None:
    """The plan of `worms build <component>` or `worms deploy`: each component, what happens to
    it and what that touches; then what the whole run touches (components.Plan.touches)."""
    rows, summary = plan.touches(packages)
    print(output.heading("plan"))
    print(output.table([[name, what, rows[name]] for name, what, _ in plan.table()], headers=["component", "what happens", "touches"]))
    for line in summary:
        print(output.color(f"  {line}", "dim"))
    for note in plan.notes:
        print(output.color(f"note: {note}", "dim"))


def _wait_for_jobs(jobs) -> int:
    """--wait: until each of these host jobs has ended, saying each one's end as it comes. 1 if one
    failed, 130 if the wait was interrupted (the jobs go on)."""
    import time

    from core.models.rounds import HostJob
    from core.models.team import Team

    pending = {job.id for job in jobs if job is not None}
    if not pending:
        return 0
    print(output.color(f"waiting for {len(pending)} host job(s): {', '.join(f'#{n}' for n in sorted(pending))} "
                       "(Ctrl-C stops waiting, not the jobs)", "dim"), flush=True)  # fmt: skip
    failed = []
    try:
        while pending:
            for job in HostJob.objects.filter(id__in=pending).order_by("id"):
                if job.state not in (HostJob.State.DONE, HostJob.State.FAILED):
                    continue
                pending.discard(job.id)
                team = Team.objects.filter(uuid=(job.params or {}).get("team_uuid") or None).first()
                took = (job.finished_at - (job.started_at or job.requested_at)).total_seconds() if job.finished_at else 0
                ok = job.state == HostJob.State.DONE
                print(output.status_cell("ok" if ok else "error", f"#{job.id} {job.kind}{f' {team.name}' if team else ''}: "
                                         f"{job.state} after {took / 60:.0f} min") + (f": {job.error[:160]}" if job.error else ""),
                      flush=True)  # fmt: skip
                if not ok:
                    failed.append(job.id)
            if pending:
                time.sleep(10)
    except KeyboardInterrupt:
        output.error(f"stopped waiting; {len(pending)} job(s) go on (`worms status jobs`, `worms log latest -f`)")
        return 130
    if failed:
        output.error(f"{len(failed)} job(s) failed: `worms log {failed[0]}`")
        return 1
    return 0


def _open_jobs(kind: str) -> list:
    from core.models.rounds import HostJob

    return list(HostJob.objects.filter(kind=kind, state__in=[HostJob.State.QUEUED, HostJob.State.RUNNING]).order_by("id"))


def _build_devboxes(config, rebuild=False, teams=(), wait=False) -> int:
    """`worms build devbox`: a devbox for every team without one (the orchestrator enqueues the
    build; the round agent on the node builds them, docs/04-dev-environment.md). With --rebuild:
    each team's (or `teams`') devbox from nothing, through the orchestrator as a reset is. With
    `wait`: until those jobs have ended."""
    _with_db(config)
    if not rebuild:
        from django.core.management import call_command
        from django.core.management.base import CommandError

        try:
            call_command("provision_devboxes")
        except CommandError as exc:
            output.error(str(exc))
            return 1
        return _wait_for_jobs(_open_jobs("devbox_provision")) if wait else 0
    from core.models.devbox import Devbox
    from orchestrator.rounds.devbox import request_rebuild
    from worms_cli import queries

    try:
        chosen = [queries.team(name) for name in teams] or [box.team for box in Devbox.objects.select_related("team")]
    except ValueError as exc:
        output.error(str(exc))
        return 1
    jobs = []
    for team in chosen:
        job, enqueued = request_rebuild(team, fresh=True)
        jobs.append(job)
        if job is None:
            print(f"{team.name}: no devbox to rebuild (`worms build devbox` builds it)")
        else:
            print(f"{team.name}: {'enqueued' if enqueued else 'already queued'} devbox_rebuild #{job.id} {job.uuid} (from nothing)")
    if wait:
        return _wait_for_jobs(jobs)
    print(output.color("The round agent does the work: `worms log latest -f` (or --wait).", "dim"))
    return 0


def cmd_deploy(config, args) -> int:
    """`worms deploy [<component>...]`: configure what exists, as the operators did by hand: the
    configuration (never OpenTofu; no internet window unless --packages), then, when the
    orchestrator or the round agent was deployed, the gameplans, the round agent (once no host
    job runs) and the conductor, in that order. Alone: every component that exists, offline."""
    from worms_cli import components, services
    from worms_cli.session import SessionError

    pipe = pipeline.Pipeline(config, ansible_args=" ".join(args.ansible_args))
    code = 0
    try:
        exists = _exists(pipe, config)
        try:
            names = components.expand(args.components) if args.components else \
                [n for n, c in components.COMPONENTS.items() if c.kind in ("vm", "node") and exists(c)]  # fmt: skip
        except KeyError as exc:
            output.error(f"no component {exc.args[0]!r}: `worms build --help` lists them")
            return 1
        not_here = [n for n in names if components.COMPONENTS[n].kind in ("stack", "devbox")]
        if not_here:
            output.error(f"{', '.join(not_here)}: not deployed: an OpenTofu stack, or the round agent's devboxes "
                         f"(`worms build {' '.join(not_here)}`, `worms reset devbox <team>`)")  # fmt: skip
            return 1
        missing = [n for n in names if components.COMPONENTS[n].kind in ("vm", "node") and not exists(components.COMPONENTS[n])]
        if missing:
            output.error(f"{', '.join(missing)} not there: a deploy only configures what exists. `worms build {' '.join(missing)}` "
                         "creates it")  # fmt: skip
            return 1
        sandboxes = sorted(vm["name"] for vm in pipe.runner.vms() if vm.get("pool") == "sandbox")
        if sandboxes and not args.during_round:
            output.error(f"Refusing to deploy during a round ({len(sandboxes)} sandbox VM(s)): it restarts the conductor, "
                         "which costs the running team its attempt; --during-round if it must")  # fmt: skip
            return 1
        follow_up = bool({"orchestrator", "round-agent", "gameplans"} & set(names))
        if follow_up and "gameplans" not in names and not sandboxes:
            names.append("gameplans")  # the node's copy and the orchestrator's, checked: as `worms build gameplans`
        plan = components.plan(names, exists, pending_migrations=_pending_migrations(pipe))
        if "orchestrator" in plan.names() and "gameplans" not in plan.names() and not sandboxes:
            # Pending migrations brought the orchestrator in: the gameplans follow it, as above.
            plan = components.plan([*names, "gameplans"], exists, pending_migrations=_pending_migrations(pipe))
        new = plan.names("create", "recreate", "dependency")
        if new:
            output.error(f"{', '.join(new)} would be created (missing): a deploy never runs OpenTofu. `worms build {' '.join(new)}`")
            return 1
        follow_up = follow_up or "orchestrator" in plan.names()
        _print_plan(plan, args.packages)
        if follow_up:
            print(output.color("  then: the round agent restarted once no host job runs, then the conductor", "dim"))
        if args.plan:
            return 0
        pipe.build_components(plan, packages=args.packages)
        if follow_up:
            _with_db(config)
            restarter = services.Restarter(config)
            for name in ("agent", "conductor"):
                if name == "agent":
                    services.wait_until_idle(args.timeout)
                restarter.restart(name)
                print(output.status_cell("ok", f"{name}: restarted, active"))
        print(output.status_cell("ok", f"deployed: {', '.join(plan.names())}"))
    except (build.BuildError, SessionError, KeyboardInterrupt) as exc:
        output.error(f"deploy stopped: {exc}" if str(exc) else "deploy stopped: interrupted")
        code = 130 if isinstance(exc, KeyboardInterrupt) else 1
    if pipe.services_rolled_back and pipe.devboxes_exist():
        code = _restore_devbox_map(config) or code
    return code


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


def cmd_keys(config, args) -> int:
    """`worms keys [status|sync|add|remove]`: the teams' devbox SSH keys (worms_cli/keys.py). The
    old `worms status keys`, `devbox-sync-keys`, `devbox-add-key`, `devbox-remove-key` (ALIASES)."""
    action = args.keys_action or "status"
    if action == "status":
        _with_db(config)
        from worms_cli import keys

        try:
            return keys.status(config)
        except ValueError as exc:
            output.error(str(exc))
            return 1
    return {"sync": cmd_devbox_sync_keys, "add": cmd_devbox_add_key, "remove": cmd_devbox_remove_key}[action](config, args)


def cmd_devbox_sync_keys(config, args) -> int:
    """`worms devbox-sync-keys`: every team's SSH keys, from the database, copied over SSH to the SSH
    gateway and its devbox VMs; then where they are (worms_cli/keys.py)."""
    _with_db(config)
    from worms_cli import keys

    return keys.sync(config)


def _restore_devbox_map(config) -> int:
    """After the service VMs were rolled back or rebuilt: the map on scoring and
    logging is newer than their snapshot. Whatever goes wrong here must not read as
    a failed reset."""
    try:
        return _request_devbox_reconfigure(config)
    except (SystemExit, Exception) as exc:  # noqa: BLE001
        output.error(
            f"the services are reset, but the devbox map on scoring and logging is empty ({exc}): "
            "devbox submissions stay unattributed until the round agent writes it again: run `worms reset services` "
            "again once the database answers"
        )
        return 1


def cmd_step(config, args) -> int:
    """`worms step <name>`: one pipeline step (pipeline.STEPS). The round agent runs these, never
    interactively; for troubleshooting too."""
    pipe = pipeline.Pipeline(config, ansible_args=" ".join(args.ansible_args))
    try:
        pipeline.refuse_without_varfile(args.name, args.ansible_args)
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
    """The VMs there are now, for the --help of `worms login`, `worms show` and `worms rsync`."""
    from worms_cli.vms import Catalogue

    try:
        names = Catalogue(config).names()
    except Exception as exc:  # noqa: BLE001  (the node may be out of reach)
        return f"(the VMs cannot be listed now: {exc})"
    rows = [[name, team, what] for name, team, what in names]
    return ("VMs there are now (a team's also as <team>/a|b|c|gw; `worms show <team>`):\n"
            + "\n".join("  " + line for line in output.table(rows, headers=["name", "team", "what"]).splitlines()))  # fmt: skip


def cmd_login(config, args) -> int:
    """`worms login <vm>`: a shell; `worms login <vm> -- <command>`: that command there, as
    `worms run <vm> -- <command>` runs it (it used to be dropped silently)."""
    from worms_cli.vms import Catalogue

    if args.extra:
        return _run_on(config, [args.vm], args.extra, root=False)
    try:
        return Catalogue(config).login(args.vm)
    except build.BuildError as exc:
        output.error(str(exc))
        return 1


def cmd_run(config, args) -> int:
    """`worms run <target>... [--root] -- <command>` (worms_cli/remote.py)."""
    return _run_on(config, args.targets, args.extra, root=args.root)


def _run_on(config, targets, words, root) -> int:
    from worms_cli.remote import Remote

    try:
        return Remote(config).run(targets, words, root=root)
    except build.BuildError as exc:
        output.error(str(exc))
        return 1


def cmd_log(config, args) -> int:
    """`worms log [<job>|<name>|<vm>]`: one place for every log (worms_cli/logs.py)."""
    from worms_cli import logs

    what = args.what
    try:
        if what == "build":
            return logs.run_log(args.nth, args.lines, args.follow)
        if what == "latest":
            return logs.Logs(config).latest(args.lines, args.follow)
        if what is None or what.isdigit():
            _with_db(config)
            if what is None:
                logs.overview()
                return 0
            return logs.Logs(config).job(int(what), args.lines, args.follow)
        if what in logs.SERVICES:
            return logs.Logs(config).source(what, args.lines, args.follow, args.since)
        return logs.Logs(config).vm(what, args.lines, args.follow, args.since)
    except build.BuildError as exc:
        output.error(str(exc))
        return 1


def cmd_rsync(config, args) -> int:
    """`worms rsync <source>... <target>`: rsync's arguments, a VM by its login name
    (worms_cli/rsync.py); rsync's own options after `--`."""
    from worms_cli.rsync import Rsync

    options = [*(["--dry-run"] if args.dry_run else []), *(["--delete"] if args.delete else []), *args.extra]
    try:
        return Rsync(config).run(args.paths, options, preview=args.preview)
    except build.BuildError as exc:
        output.error(str(exc))
        return 1


def cmd_show(config, args) -> int:
    from worms_cli.vms import Catalogue

    if args.vm == "database" and args.table:
        return _show_table(config, args)
    catalogue = Catalogue(config)
    try:
        team = catalogue.find_team(args.vm) if "/" not in args.vm and not catalogue.knows(args.vm) else None
        if team is not None:
            return _show_team(config, catalogue, team)
        print(output.heading(args.vm))
        print(output.table(catalogue.details(args.vm)))
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


def _show_team(config, catalogue, team) -> int:
    """`worms show <team>`: its devbox's four VMs (vmid, address, state) and its keys on each."""
    from worms_cli import keys

    print(output.heading(f"team {team.label}"))
    cells = keys.team_cells(config, team.slug, catalogue) if team.devbox else {}
    print(output.table(catalogue.team_rows(team, cells), headers=["target", "VM", "vmid", "address", "state", "team keys"]))
    if cells.get("keys"):
        print(output.color(f"{cells['keys']} key(s) in the database; `worms status keys`: every team's", "dim"))
    print(output.color(f"worms login {team.handle}/a · worms show {team.handle}/gw · worms reset devbox {team.handle}", "dim"))
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
    """`worms internet enable` is refused during a round (a sandbox VM exists). Running devbox
    VMs are stopped first, after asking: no team VM runs while the window is open; they are
    written down on the node (power.WINDOW_STOPPED). `disable` closes it, switches DNS and the
    caches back to offline mode (`worms step offline`) and starts those devboxes again."""
    pipe = pipeline.Pipeline(config)
    try:
        if args.state == "enable":
            pipe.refuse_during_round()
            running = pipe.running_devboxes()
            if running:
                names = ", ".join(vm["name"] for vm in running)
                if not build.confirm(f"{len(running)} devbox VM(s) run ({names}). Stop them all and open the window?", args.yes):
                    output.error("the window stays closed: no team VM may run while it is open")
                    return 1
                stopped = []
                recorded = power.read_record(pipe.runner, power.WINDOW_STOPPED)
                try:
                    # Listed again: one started while the question was open goes too. Forced after
                    # 120 s: a team's VM may ignore the shutdown request. Each is written down on the
                    # node first, for `worms internet disable` to start again.
                    for vm in pipe.running_devboxes():
                        recorded = [r for r in recorded if r["vmid"] != int(vm["vmid"])] + [{"vmid": int(vm["vmid"]), "name": vm["name"]}]
                        power.write_record(pipe.runner, power.WINDOW_STOPPED, recorded)
                        pipe.runner.node_sh(power.SHUTDOWN.format(vmid=int(vm["vmid"])))
                        stopped.append(vm)
                finally:
                    if stopped:  # also when a later one failed: these are off now
                        print(output.color(f"stopped {len(stopped)} devbox VM(s): {', '.join(vm['name'] for vm in stopped)}. "
                                           "`worms internet disable` starts them again once the window is closed "
                                           f"(on the node: {pipeline.start_command(vm['vmid'] for vm in stopped)})", "dim"))  # fmt: skip
            pipe.internet_allow()
        else:
            pipe.internet_deny()
            print(output.color("switching DNS and the private internet's caches to offline mode (a few minutes)", "dim"), flush=True)
            code = 0
            try:
                pipe.offline_services()
            except build.BuildError as exc:
                output.error(f"the window is closed, but DNS and the caches may not be offline or not serve "
                             f"what they should: {exc}. Run `worms step offline` again once fixed")  # fmt: skip
                code = 1
            return _start_window_stopped(pipe.runner) or code
    except build.BuildError as exc:
        output.error(str(exc))
        return 1
    return 0


def _start_window_stopped(runner) -> int:
    """After the window closed: the devbox VMs `worms internet enable` shut down, started again
    (those still there and stopped, and not held by a round). Those that do not start stay
    written down."""
    recorded = power.read_record(runner, power.WINDOW_STOPPED)
    if not recorded:
        return 0
    held = {r["vmid"] for r in power.read_record(runner, power.HELD_DEVBOXES)}
    vms = [vm for vm in power.still_there(runner, recorded) if int(vm["vmid"]) not in held]
    failed = power.start(runner, vms)
    power.write_record(runner, power.WINDOW_STOPPED, [r for r in recorded if r["name"] in failed])
    started = [vm["name"] for vm in vms if vm["name"] not in failed]
    if started:
        print(output.status_cell("running", f"started again the devbox VM(s) the window stopped: {', '.join(started)}"))
    if held & {r["vmid"] for r in recorded}:
        print(output.color("held by a round, left off: its release starts them", "dim"))
    if failed:
        output.error(f"did not start again: {', '.join(failed)} (`worms power start {failed[0]}`)")
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
        if getattr(args, "wait", False):  # the map, written again by the round agent
            _with_db(config)
            return _wait_for_jobs(_open_jobs("devbox_reconfigure"))
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
    fresh = getattr(args, "fresh", False)
    what = "its VMs DESTROYED and built again from nothing (what is on them is lost)" if fresh else "its VMs back to their snapshot"
    if not build.confirm(f"Reset the devbox of {', '.join(t.name for t in teams) or 'no team'}: {what}, its dev score "
                         "starts over?", args.yes):  # fmt: skip
        return 1
    jobs = []
    for team in teams:
        job, enqueued = request_rebuild(team, fresh=fresh)  # fresh: as `worms build devbox --rebuild`
        events.team("devbox_reset", actor=_console(), team=team)
        jobs.append(job)
        if job is None:
            print(f"{team.name}: no devbox (version bumped all the same)")
        else:
            print(f"{team.name}: {'enqueued' if enqueued else 'already queued'} devbox_rebuild #{job.id} {job.uuid}")
    return _wait_for_jobs(jobs) if getattr(args, "wait", False) else 0


ROUND_CRITICAL = ("agent", "processor", "conductor", "scoring", "logging")  # a round's attempt needs them running


def cmd_restart(config, args) -> int:
    """`worms restart <service>...`: in a safe order (services.SERVICES); the agent once no host
    job runs. Refused for what a round needs while one is on, unless --during-round."""
    from worms_cli import services

    try:
        names = services.in_order(args.services)
    except KeyError as exc:
        output.error(f"no service {exc.args[0]!r}: {', '.join(n for n, s in services.SERVICES.items() if s.restartable)}")
        return 2
    except ValueError as exc:
        output.error(str(exc))
        return 1
    restarter = services.Restarter(config)
    try:
        critical = [name for name in names if name in ROUND_CRITICAL]
        sandboxes = [vm["name"] for vm in restarter.catalogue.vms() if vm.get("pool") == "sandbox"] if critical else []
        if sandboxes and not args.during_round:
            raise build.BuildError(f"Refusing to restart {', '.join(critical)} during a round ({len(sandboxes)} sandbox VM(s)): "
                                   "it can cost the running team its attempt; --during-round if it must")  # fmt: skip
        for name in names:
            if name == "agent":
                _with_db(config)
                services.wait_until_idle(args.timeout)
            restarter.restart(name)
            print(output.status_cell("ok", f"{name}: restarted, active"))
    except build.BuildError as exc:
        output.error(str(exc))
        return 1
    except KeyboardInterrupt:
        output.error("interrupted: what is listed above as restarted was restarted, nothing else")
        return 130
    return 0


def cmd_backup(config, args) -> int:
    """`worms backup database`: a pg_dump of the platform's database, here (database.backup)."""
    from worms_cli import database

    try:
        path, size = database.backup(config, args.output)
    except (build.BuildError, OSError) as exc:
        output.error(str(exc))
        return 1
    print(output.status_cell("ok", f"{size / 1e6:.1f} MB: {path}"))
    print(output.color(f"readable by you only (0600). Restore on the database VM: pg_restore -d <db> {Path(path).name}", "dim"))
    return 0


def cmd_power(config, args) -> int:
    """`worms power start|stop <target>...` (worms_cli/power.py)."""
    if args.held and args.state != "start":
        output.error("--held is for `worms power start --held`")
        return 2
    if not args.targets and not args.held:
        output.error(f"which VMs? `worms power {args.state} <target>...`" + (" or --held" if args.state == "start" else ""))
        return 2
    try:
        machines = power.Power(config)
        return machines.start(args.targets, held=args.held) if args.state == "start" else machines.stop(args.targets)
    except build.BuildError as exc:
        output.error(str(exc))
        return 1


def completion_slots() -> dict[str, list[str]]:
    """What tab completion offers for a command's positional arguments that its parser cannot
    say, slot by slot (the last one repeats; "" = files): from the CLI's own tables."""
    from worms_cli import components, remote, services

    built = " ".join([*components.COMPONENTS, *components.GROUPS])
    groups = " ".join(remote.groups())
    return {
        "show": ["@targets @teams database", ""],
        "login": ["@targets", ""],
        "run": [f"@targets {groups}"],
        "log": [f"@targets {' '.join(services.SERVICES)} build latest", ""],
        "rsync": ["@rsync"],
        "power": ["start stop", f"@targets {groups}"],
        "build": [built],
        "deploy": [built],
        "restart": [" ".join(n for n, s in services.SERVICES.items() if s.restartable)],
        "reset devbox": ["@teams"],
        "keys add": ["@teams", ""],
        "keys remove": ["@teams", ""],
        "delete-team": ["@teams", ""],
        "step": [" ".join([*pipeline.STEPS, "debug"]), ""],
    }


def cmd_completion(config, args) -> int:
    """`worms completion bash`: the bash completion script (worms_cli/completion.py)."""
    from worms_cli import completion

    print(completion.script(build_parser(), completion_slots(), [sys.executable, str(Path(__file__).resolve())]), end="")
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
    from worms_cli import keys

    return keys.sync(config)


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
    if not Devbox.objects.filter(team=team).exists():
        return 0
    from worms_cli import keys

    return keys.sync(config)


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
    "devboxes": "every team's devbox (t<id> and its VMs) and SSH keys",
}

SECTIONS = (
    Section(
        "day to day",
        "Look, reach, fix. status, show and log change nothing. A VM is its name or <team>/a|b|c|gw (alpha/gw, t2/a).",
        (
            ("status [<topic>]", "what needs attention, the internet window, every VM (team, state), the competition; "
                                 "or one topic: " + ", ".join(STATUS_TOPICS)),  # fmt: skip
            ("show <vm>|<team> [<table>]", "one VM in detail: state, resources, networks, snapshots, team, services, login; "
                                           "a team: its four devbox VMs, their states and its keys on each; "
                                           "`show database [<table>]`: its tables, a table's rows or --schema"),  # fmt: skip
            ("log [<job>|<name>|<vm>]", "a host job's log by its number (`worms status jobs`), a service's (agent, "
                                       "conductor, dashboard, scoring, audit, ...) or a VM's journal; `log latest`: the "
                                       "newest host job's; `log build [N]`: the newest (N-th) run log of a build, deploy, "
                                       "reset, ... here; -f follows. Alone: what there is"),
            ("run <target>... -- <command>", "a command on VMs without a login: a VM, <team>/<role>, node, or a group "
                                              "(infrastructure, services, private-internet: never a team's VM); "
                                              "one prefix per VM; --root: with sudo"),  # fmt: skip
            ("login <vm>", "a shell on a VM over its management SSH (`worms login --help` lists them); "
                           "`login <vm> -- <command>`: that command there, as `worms run`"),  # fmt: skip
            ("deploy [<component>...]", "configure what exists, never OpenTofu, no internet window unless --packages; then "
                                        "the gameplans, the agent (once idle) and the conductor; alone: every component "
                                        "offline; refused during a round"),  # fmt: skip
            ("reset database", "DANGER: wipe the database, migrate and initialise it again"),
            ("reset services", "the service VMs (ephemeral, private internet) back to their snapshot (--wait)"),
            ("reset devbox <team>...", "teams' devboxes back to their snapshot, through the orchestrator (--all-teams; "
                                       "--fresh: rebuilt from nothing; --wait)"),  # fmt: skip
            ("restart <service>...", "restart services in a safe order: the agent first and once no host job runs, "
                                     "then the conductor (`worms log` lists them); refused during a round for what "
                                     "it needs, unless --during-round"),  # fmt: skip
        ),
    ),
    Section(
        "teams and keys",
        "Teams, students and rounds are managed on the dashboard (/staff/), and a round starts by itself. These are "
        "the console's part.",
        (
            ("keys", "are every team's SSH keys on the SSH gateway and its devbox VMs? (reads them there; = keys status)"),
            ("keys status", "as `worms keys` alone: every team's SSH keys, read on the SSH gateway and its devbox VMs"),
            ("keys sync", "copy every team's SSH keys from the database to the SSH gateway and its devbox VMs, then show them"),
            ("keys add <team> <key>", "add an SSH key to a team's devbox, as on the dashboard, then copy the keys"),
            ("keys remove <team> <key>", "remove one, by its name or fingerprint, then copy the keys"),
            ("admin-reset-password", "one-time password for the dashboard admin, printed once (created if missing)"),
            ("admin-create-staff <email>", "a staff (tutor) account; one-time password printed once"),
            ("delete-team <name>", "delete a team with everything it owns (its users stay)"),
        ),
    ),
    Section(
        "build and machines",
        "`worms build` builds everything; `worms build <component>` one service, and what it needs first (it shows "
        "the plan, what it touches, and asks). Packages need the internet window: devbox VMs are shut down while it "
        "is open, and it is refused during a round.",
        (
            ("build [<component>...]", "build what is missing, configure what exists; --rebuild recreates (`worms build --help`); "
                                       "after `--`: arguments for ansible-playbook"),
            ("internet enable|disable", "open or close the internet window by hand (refused during a round; stops running "
                                        "devboxes, after asking, and starts them again on closing, which also switches "
                                        "DNS and the caches back to offline mode)"),  # fmt: skip
            ("power start|stop <target>...", "VMs on or off (qm on the node), by the names `worms run` takes; "
                                             "`start --held`: the devboxes a round holds, once it is over"),  # fmt: skip
            ("backup database", "a pg_dump -Fc of the platform's database, streamed to a file here (0600)"),
            ("rsync <source>... <target>", "rsync -avz between here and a VM, in rsync's syntax: `worms rsync dir/ "
                                           "alpha/a:` up, `worms rsync alpha/a: dir/` down (`worms rsync --help`)"),  # fmt: skip
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
            ("completion bash", "tab completion for bash: eval \"$(worms completion bash)\" in ~/.bashrc"),
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

# Commands that moved: the old words, as typed, -> the new ones. main() rewrites argv with it (and
# says so on stderr); tests/test_docs.py checks the docs' commands through it too.
ALIASES = {
    ("status", "keys"): ("keys", "status"),
    ("devbox-sync-keys",): ("keys", "sync"),
    ("devbox-add-key",): ("keys", "add"),
    ("devbox-remove-key",): ("keys", "remove"),
}


def _aliased(argv: list[str]) -> tuple[list[str], str]:
    """argv with an old command's words replaced by its new ones (ALIASES), and the note that says
    so ("" if none). Options before the command (--config FILE) stay where they are."""
    at = 0
    while at < len(argv) and argv[at].startswith("-") and argv[at] != "--":
        at += 2 if argv[at] == "--config" else 1
    for old, new in ALIASES.items():
        if tuple(argv[at:at + len(old)]) == old:
            note = f"`worms {' '.join(old)}` is now `worms {' '.join(new)}`"
            return [*argv[:at], *new, *argv[at + len(old):]], note
    return list(argv), ""

HELP_WIDTH = 79

RSYNC_HELP = """\
rsync's own syntax, with a VM's name (below) for the host:
  worms rsync team-1-backup/ devbox-t1-a:       the directory's contents into the home
  worms rsync devbox-t2-a: team-2-backup/       the home's contents into the directory
  worms rsync devbox-t2-a:notes :src/a.py .     two files from that VM (`:path`: the same VM)
  worms rsync alpha/a: team-alpha-backup/       a team's VM as <team>/<a|b|c|gw>: alpha's VM A
  worms rsync debian@dashboard:.bashrc .        as another user, from their home
  worms rsync ./a:b devbox-t1-a:                a local file with a ":" (after a "/": local)
  worms rsync dir/ devbox-t1-a:dir -- --exclude .cache --progress   rsync's options after --

A trailing "/" on a source copies its contents, not itself; `<vm>:` alone is the home.
The VM's rsync runs as <user> (sudo) from <user>'s home: root's on a devbox and on the
node, the login user's (debian) elsewhere.

A devbox or sandbox VM is assumed rooted by its team. Downloaded from one, files get no
setuid bits, owner or device files, and symbolic links stay inert (--munge-links: a
backup goes back up as it came); what it prints is escaped. Options after -- can override
that. rsync has to be on the VM: a devbox has it (devbox_packages), the SSH gateway not."""


RUN_HELP = """\
  worms run dashboard -- systemctl is-active dashboard nginx
  worms run services -- df -h /           every service VM, a [<vm>] prefix a line
  worms run node -- qm list               the Proxmox node
  worms run alpha/a --root -- ls /root    a team's VM: only when named itself

Targets: node; a VM by its name, an alias or <team>/<a|b|c|gw>; a group:
infrastructure, services, private-internet (platform VMs only: a team's VM never
runs a command as part of a group). The words after -- are joined with spaces
and run by the VM's shell, as ssh does: quote a pipe
(`worms run dns -- 'journalctl -n 50 | grep named'`). Never interactive
(BatchMode, no terminal); the WORMS key is never forwarded. What a devbox or
sandbox VM prints is escaped. The exit code is the worst of the VMs'; a stopped
VM is not started."""


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
                      "configured from their snapshot and snapshotted again, with the devboxes off meanwhile. While "
                      "the database lacks a migration of this checkout, the dashboard and the database bring the "
                      "orchestrator (which migrates) along, and the database (grants).", HELP_WIDTH),  # fmt: skip
    ])  # fmt: skip


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def _yes(parser) -> None:
    parser.add_argument("--yes", action="store_true", help="do not ask first")


def build_parser(vm_list: str = "") -> argparse.ArgumentParser:
    """`vm_list`: the VMs there are now, for `login`, `show` and `rsync --help` (main asks the node
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
    p.add_argument("vm", help="a VM (its name, an alias or <team>/a|b|c|gw), or a team (its name or slug t<id>)")
    p.add_argument("table", nargs="?", help="with `database`: a table's newest rows")
    p.add_argument("--schema", action="store_true", help="the table's columns, indexes and constraints instead")
    p.add_argument("--limit", type=int, default=20, help="rows to show (default 20)")

    p = _add(sub, "login", epilog=vm_list, **raw)
    p.add_argument("vm", help="its name, an alias, or <team>/a|b|c|gw (alpha/gw, t2/a)")

    p = _add(sub, "run", epilog=RUN_HELP + (f"\n\n{vm_list}" if vm_list else ""),
             usage="worms run [-h] [--root] <target>... -- <command>", **raw)  # fmt: skip
    p.add_argument("targets", nargs="+", metavar="target", help="node, a VM (its name, an alias, <team>/<role>), a group")
    p.add_argument("--root", action="store_true", help="run the command as root (sudo -n sh -c); the node's login is root")

    p = _add(sub, "log")
    p.add_argument("what", nargs="?", help="a host job's number, a log's name (`worms log` lists them), a VM's name; "
                   "`build [N]`: the newest (N-th newest) run log here; `latest`: the newest host job's")  # fmt: skip
    p.add_argument("nth", nargs="?", type=int, default=1, metavar="N", help="build: the N-th newest run log (default 1)")
    p.add_argument("-f", "--follow", action="store_true", help="keep showing new lines (Ctrl-C ends it)")
    p.add_argument("-n", "--lines", type=int, default=100, help="lines to show first (default 100)")
    p.add_argument("--since", help="journals only: from this time on (\"1 hour ago\", \"2026-11-19 18:00\")")

    p = _add(sub, "rsync", epilog=RSYNC_HELP + (f"\n\n{vm_list}" if vm_list else ""), **raw)
    p.add_argument("paths", nargs="+", metavar="path", help="sources, then the target: a local path or [<user>@]<vm>:[<path>]")
    p.add_argument("-n", "--dry-run", action="store_true", help="rsync's: list what would be sent, send nothing")
    p.add_argument("--delete", action="store_true", help="rsync's: delete what the source does not have on the target")
    p.add_argument("--preview", action="store_true", help="count what would be sent (files, sizes, bytes), send nothing")

    p = _add(sub, "build", epilog=_components_overview(), **raw)
    p.add_argument("components", nargs="*", metavar="component", help="components or groups (default: everything)")
    p.add_argument("--rebuild", action="store_true", help="destroy the components' VMs and build them again")
    p.add_argument("--packages", action="store_true", help="install packages too (opens the internet window)")
    p.add_argument("--plan", action="store_true", help="show the plan, build nothing")
    p.add_argument("--team", action="append", default=[], help="devbox --rebuild: only this team's (repeatable)")
    p.add_argument("--wait", action="store_true", help="devbox: wait for its host jobs, and say how each ended")
    _yes(p)

    p = _add(sub, "deploy", epilog=_components_overview(), **raw)
    p.add_argument("components", nargs="*", metavar="component", help="components or groups (default: every one that exists)")
    p.add_argument("--packages", action="store_true", help="install packages too (opens the internet window)")
    p.add_argument("--plan", action="store_true", help="show the plan, deploy nothing")
    p.add_argument("--during-round", action="store_true", help="deploy even while a round is on (costs its attempt)")
    p.add_argument("--timeout", type=int, default=900, help="how long to wait for the agent's running host job (s, default 900)")

    p = _add(sub, "internet")
    p.add_argument("state", choices=["enable", "disable"])
    _yes(p)

    p = sub.add_parser("reset", help="reset a part to its clean state", description="Reset the database, the service "
                       "VMs or teams' devboxes to their clean state.")  # fmt: skip
    reset_sub = p.add_subparsers(dest="what", required=True, metavar="database|services|devbox")
    _yes(_add(reset_sub, "reset database"))
    p_services = _add(reset_sub, "reset services")
    p_services.add_argument("--wait", action="store_true", help="wait for the round agent to write the devbox map again")
    _yes(p_services)
    p_devbox = _add(reset_sub, "reset devbox")
    p_devbox.add_argument("teams", nargs="*", metavar="team")
    p_devbox.add_argument("--all-teams", action="store_true", help="every team's devbox")
    p_devbox.add_argument("--fresh", action="store_true", help="rebuilt from nothing, fresh disks (= build devbox --rebuild)")
    p_devbox.add_argument("--wait", action="store_true", help="wait for its host jobs, and say how each ended")
    _yes(p_devbox)

    p = _add(sub, "restart")
    p.add_argument("services", nargs="+", metavar="service", help="agent, processor, api, conductor, dashboard, scoring, "
                   "logging, monitor")  # fmt: skip
    p.add_argument("--timeout", type=int, default=900, help="agent: how long to wait for its running host job (s, default 900)")
    p.add_argument("--during-round", action="store_true", help="restart what a round needs even while one is on")

    p = _add(sub, "power")
    p.add_argument("state", choices=["start", "stop"])
    p.add_argument("targets", nargs="*", metavar="target", help="a VM (its name, an alias, <team>/<role>) or a group")
    p.add_argument("--held", action="store_true", help="start: the devbox VMs a round holds shut down")

    p = sub.add_parser("backup", help="back a part up", description="Back up the platform's database.")
    backup_sub = p.add_subparsers(dest="what", required=True, metavar="database")
    p_backup = _add(backup_sub, "backup database")
    p_backup.add_argument("-o", "--output", metavar="FILE", help="the dump's file (default ~/.worms/backups/<system>-<db>-<time>.dump)")

    p = _add(sub, "keys")
    keys_sub = p.add_subparsers(dest="keys_action")
    _add(keys_sub, "keys status")
    _add(keys_sub, "keys sync")
    p_add = _add(keys_sub, "keys add")
    p_add.add_argument("team", help="its name or slug t<id>")
    p_add.add_argument("key", help="the public key (quoted), a .pub file, or - for stdin")
    p_add.add_argument("--name", default="", help="a name for it (default: the key's comment)")
    p_remove = _add(keys_sub, "keys remove")
    p_remove.add_argument("team", help="its name or slug t<id>")
    p_remove.add_argument("key", help="the key's name, or its fingerprint (at least 8 characters of it)")
    _yes(p_remove)
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
    p = sub.add_parser("completion", help="tab completion", description="Tab completion for the shell.")
    completion_sub = p.add_subparsers(dest="shell", required=True, metavar="bash")
    _add(completion_sub, "completion bash")

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
    "status": cmd_status, "show": cmd_show, "login": cmd_login, "run": cmd_run, "log": cmd_log, "rsync": cmd_rsync,
    "build": cmd_build, "deploy": cmd_deploy,
    "internet": cmd_internet, "restart": cmd_restart, "power": cmd_power, "backup": cmd_backup,
    "reset": cmd_reset, "keys": cmd_keys,
    "admin-reset-password": cmd_admin_reset_password, "admin-create-staff": cmd_admin_create_staff,
    "delete-team": cmd_delete_team, "secrets": cmd_secrets, "trust-node": cmd_trust_node, "completion": cmd_completion,
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
    argv, moved = _aliased(argv)
    if moved:
        print(output.color(f"note: {moved}", "dim"), file=sys.stderr)
    vm_list = ""
    if len(argv) >= 2 and argv[0] in ("login", "show", "rsync", "run") and ("-h" in argv or "--help" in argv):
        vm_list = _vm_list(Config.load())
    parser = build_parser(vm_list)
    own, passed = _split(argv)
    args = parser.parse_args(own)
    args.ansible_args = args.extra = passed
    config = Config.load(args.config)
    if args.command == "completion":  # read at every shell start: no notice, no node
        return cmd_completion(config, args)
    tee = _run_log(config, args)
    try:
        _internet_notice(config)
        code = HANDLERS[args.command](config, args)
    finally:
        if tee is not None:
            tee.stop()
    if tee is not None and code:
        print(output.color(f"this run's whole output: {tee.path} (`worms log build`)", "dim"), file=sys.stderr)
    return code


# The commands that change something: their whole output goes to a run log too (worms_cli/runlog.py).
# Never one whose output is a secret (admin-reset-password, admin-create-staff) or a VM's (run, login).
LOGGED = ("build", "deploy", "reset", "internet", "keys", "step", "restart", "power", "destroy", "emergency-stop")


def _run_log(config, args):
    """Start this run's log, if it is a changing one; None otherwise, or when it cannot be (said once)."""
    from worms_cli import runlog
    from worms_cli.session import NODE_WORKSPACE

    if args.command not in LOGGED or Path(config.repo_root).resolve() == Path(NODE_WORKSPACE):
        return None
    if args.command == "keys" and getattr(args, "keys_action", None) in (None, "status"):
        return None  # reads only
    try:
        tee = runlog.Tee(runlog.verb(args)).start()
    except OSError as exc:
        output.warn(f"no run log for this run ({exc})")
        return None
    print(output.color(f"run log: {tee.path}", "dim"), file=sys.stderr, flush=True)
    return tee


if __name__ == "__main__":
    sys.exit(main())
