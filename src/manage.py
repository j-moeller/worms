#!/usr/bin/env python3
"""worms -- the WORMS platform's CLI: `worms <command> [<args>]`.

Look with status, show, log, check, trace; reach a VM with login, run, rsync;
change things with apply and a noun's verbs (`worms vm reset alpha`). A noun
alone lists its things: `worms vm`, `worms jobs`, `worms round`. OpenTofu and
Ansible run here: on the Proxmox host, or a laptop with WORMS_SSH=root@<host>.
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
PVE_CONFIG_DIR = "/etc/pve"  # exists only on a Proxmox host


# ---------------------------------------------------------------------------
# worms status [<topic>]
# ---------------------------------------------------------------------------


VM_HEADERS = ["VM", "vmid", "kind", "team", "state", "up", "memory", "created", "updated", "snapshot"]


def _list_vms(config) -> tuple[list | None, str]:
    """(every VM, from the Proxmox API with the topology token, "") or (None, why not as a cell)."""
    from worms_cli.proxmox import Infra

    infra = Infra(config)
    if not infra.configured:
        return None, output.status_cell("n/a", infra.error or "no Proxmox API configured (worms secret issue topology-token)")
    try:
        return infra.vms(), ""
    except Exception as exc:  # noqa: BLE001
        return None, output.status_cell("error", f"Proxmox API: {exc}")


def _vm_rows(config, listing=None, probe=None) -> list[list[str]]:
    """Every VM: name, vmid, kind, team, state, uptime, memory (Proxmox API, the topology token;
    the team from the team map, worms_cli/teams.py); when it was created and last updated, with
    the commit (the host's VM history, worms_cli/history.py, read by _host_probe)."""
    from worms_cli import history, teams
    from worms_cli.vms import KINDS

    import time

    from worms_cli import consistency

    vms, problem = listing or _list_vms(config)
    if vms is None:
        return [["(VMs)", "", "", "", problem, "", "", "", "", ""]]
    probe = probe or {}
    # A service VM's `clean` snapshot: what every reset and round build returns it to (its commit, its age).
    snapshots, now = consistency.parse_snapshots(probe.get("clean", "")), time.time()
    ledger = history.summary(history.parse(probe.get("ledger", "")))
    ctimes = {}
    for line in probe.get("meta", "").splitlines():  # /etc/pve/qemu-server/<vmid>.conf:meta: ...,ctime=<epoch>
        path, _, meta = line.partition(":meta:")
        vmid = path.rsplit("/", 1)[-1].removesuffix(".conf")
        if vmid.isdigit() and history.ctime(meta):
            ctimes[int(vmid)] = {"time": history.ctime(meta), "commit": "?"}

    def changes(vm):
        known = ledger.get(int(vm["vmid"]))
        if known and known["name"] != vm["name"]:
            known = None  # another VM had that vmid
        created = (known or {}).get("created") or ctimes.get(int(vm["vmid"]))
        return history.cell(created), history.cell((known or {}).get("updated"))
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
            *changes(vm), consistency.snapshot_cell(vm, snapshots, now),
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
    """The host's internet flag: read directly on the host, over SSH (with the
    WORMS key) from a laptop (WORMS_SSH)."""
    if not config.ssh:
        if not os.path.isdir(PVE_CONFIG_DIR):
            return ["internet access", output.status_cell("unknown", "not on the Proxmox host: set WORMS_SSH to probe it")]
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
    password), and it never stops a command: the host may be out of reach."""
    try:
        row = _internet_status(config)
    except Exception as exc:  # noqa: BLE001
        row = _internet_cell("unknown", str(exc))
    print(": ".join(row), file=sys.stderr)


def _internet_cell(state, stderr) -> list[str]:
    if state == "allowed":
        return ["internet access", output.status_cell("warn", "ALLOWED (close it with `worms internet off`)")]
    if state == "denied":
        return ["internet access", output.status_cell("ok", "denied")]
    return ["internet access", output.status_cell("unknown", stderr.strip()[:60] or "could not probe")]


def _competition_status(config) -> list[list[str]]:
    from worms_cli import django_setup

    try:
        django_setup.connected(config)  # once per run: the attention block and the team map asked too
    except django_setup.NotConfigured as exc:
        return [["competition", output.status_cell("n/a", str(exc))]]
    except Exception as exc:  # noqa: BLE001  (the database is down: said, not a traceback)
        return [["competition", output.status_cell("error", f"the database does not answer: {str(exc).strip()[:120]}")]]

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
    except Exception as exc:  # noqa: BLE001  (said, not a traceback: before 2026-11-26 it was swallowed)
        rows.append(["round runs", output.status_cell("error", f"could not be read: {str(exc).strip().splitlines()[0][:120] if str(exc).strip() else type(exc).__name__}")])
    return rows


def _system_status(config) -> list[str]:
    """Which system this is: the switch in ansible/group_vars/all.yml, by the host's name."""
    from worms_cli.session import SessionError, for_config

    try:
        system = for_config(config).environment()
    except SessionError as exc:
        return ["system", output.status_cell("error", str(exc))]
    return ["system", output.status_cell("ok", f"{system['name']} ({system['host']}, {system['public_name']})")]


HOST_PROBE = r"""
cat {held} 2>/dev/null; echo; echo '#--'
cat {window} 2>/dev/null; echo; echo '#--'
for f in /etc/pve/qemu-server/*.conf; do awk -v f="$f" '/^\[clean\]$/{{p=1;next}} /^\[/{{p=0}} p && /^#/ && d=="" {{d=substr($0,2)}} p && /^snaptime:/{{t=$2}} END{{if (t) print f" "t" "d}}' "$f"; done 2>/dev/null; echo '#--'
for unit in {units}; do printf '%s %s\n' "$unit" "$(systemctl is-active "$unit")"; done; echo '#--'
tail -n 20000 {ledger} 2>/dev/null; echo '#--'
grep -H '^meta:' /etc/pve/qemu-server/*.conf 2>/dev/null; echo '#--'
cat {stopped} 2>/dev/null; echo; echo '#--'
pid=$(sed -n 's/.*"pid": *\([0-9][0-9]*\).*/\1/p' {stopped} 2>/dev/null); [ -n "$pid" ] && tr '\0' ' ' < /proc/$pid/cmdline 2>/dev/null; echo; echo '#--'
cat {deployed} 2>/dev/null; echo; echo '#--'
sha256sum /var/lib/worms/devbox-teams 2>/dev/null | cut -c1-64; echo '#--'
cat /opt/worms/opentofu/stacks/sandbox/terraform.tfstate 2>/dev/null; echo; echo '#--'
cat /opt/worms/opentofu/stacks/devbox/terraform.tfstate 2>/dev/null; echo; echo '#--'
grep -c '^resource' /opt/worms/opentofu/stacks/sandbox/generated_vms.tf 2>/dev/null; echo '#--'
test -e /var/lib/worms/sandbox-build.json && echo yes; echo '#--'
pvesh get /cluster/backup --output-format json 2>/dev/null; echo; echo '#--'
for s in platform firewall; do printf '%s %s\n' $s "$(sed -n 's/^ "serial": \([0-9]*\).*/\1/p' /var/lib/worms/outputs/$s.json 2>/dev/null)"; done; echo '#--'
"""
PROBED = ("held", "window", "clean", "active", "ledger", "meta", "stopped", "session", "deployed", "map", "sandbox_state",
          "devbox_state", "generated", "build", "backup", "outputs")  # fmt: skip


def _host_probe(config) -> dict:
    """What `worms status` reads on the host, in one command: the records of held and
    window-stopped devboxes, which VMs have a `clean` snapshot, the host services' state, the VM
    history (its last 20000 lines) and the VMs' creation times. {} with "error" when it cannot."""
    import shlex

    from worms_cli import history, services
    from worms_cli.session import SessionError, for_config

    if not config.ssh and not os.path.isdir(PVE_CONFIG_DIR):
        return {"error": "not on it: set WORMS_SSH"}
    units = [unit for service in services.SERVICES.values() if service.where == "host" for unit in service.up_units]
    from worms_cli.changes import DEPLOYED
    from worms_cli.rounds import STOPPED_RECORD

    script = HOST_PROBE.format(held=power.HELD_DEVBOXES, window=power.WINDOW_STOPPED, units=" ".join(map(shlex.quote, units)),
                               ledger=shlex.quote(history.LEDGER), stopped=shlex.quote(STOPPED_RECORD),
                               deployed=shlex.quote(DEPLOYED))  # fmt: skip
    try:
        code, out, err = for_config(config).host_run(["sh", "-c", script])
    except (SessionError, OSError) as exc:
        return {"error": str(exc)[:60]}
    parts = out.split("#--\n")
    if code != 0 and len(parts) < len(PROBED):
        return {"error": err.strip()[:60] or f"exit {code}"}
    return dict(zip(PROBED, parts + [""] * len(PROBED)))


def _session_runs(probe) -> bool:
    """A `worms round start` runs on the host now (its record's pid, worms_cli/rounds.py)."""
    return "round start" in probe.get("session", "")


def _round_session_rows(probe, vms, service_states=None) -> list[list[str]]:
    """What a round session stopped (worms_cli/rounds.py STOPPED_RECORD): while the session runs,
    said; once it has ended, what it left stopped that still is, and what starts it again."""
    import json

    from worms_cli.rounds import START_AGAIN, STOPPED_RECORD

    try:
        record = json.loads(probe.get("stopped", "").strip() or "{}")
    except ValueError:
        return [["round session", output.status_cell("warn", f"its record is unreadable ({STOPPED_RECORD})")]]
    if not isinstance(record, dict) or not record:
        return []
    names = [name for name in record.get("stopped") or [] if name in START_AGAIN]
    who = f"the session of round {record.get('round')!r} (pid {record.get('pid')}, since {record.get('since', '?')})"
    if _session_runs(probe):
        what = ", ".join(START_AGAIN[name][0] for name in START_AGAIN if name in names) or "nothing yet"
        return [[ROUND_RUNS_LABEL, output.status_cell("running", f"{who} runs: it stopped {what}, and starts them again "
                                                      "at its end")]]  # fmt: skip
    orchestrator = (service_states or {}).get("orchestrator")
    if isinstance(orchestrator, dict) and all(state == "active" for state in orchestrator.values()):
        names = [name for name in names if name != "orchestrator"]
    if "worms-round-agent active" in probe.get("active", ""):
        names = [name for name in names if name != "agent"]
    if vms is not None and all(vm.get("status") == "running" for vm in vms if vm.get("pool") == "devbox"):
        names = [name for name in names if name != "devboxes"]
    if not names:
        return []
    return [["round session", output.status_cell("error", f"{who} has ended, and left stopped: " + "; ".join(
        f"{START_AGAIN[name][0]} (`{START_AGAIN[name][1]}`)" for name in START_AGAIN if name in names))]]  # fmt: skip


ROUND_RUNS_LABEL = "round in progress"  # said while a session runs: not a problem (cmd_status's exit)


def _service_rows(states, during_round=False) -> list[list[str]]:
    """The platform's services on their own VMs (services.Units.vm_states) that are not active: one
    row. During a round the orchestrator is stopped on purpose (its session's row says so)."""
    from worms_cli import services

    service_of = {(service.where, unit): name for name, service in services.SERVICES.items() for unit in service.up_units}
    down = []
    for where, found in states.items():
        if during_round and where == "orchestrator":
            continue
        if isinstance(found, str):
            down.append(f"{where}: {found}")
            continue
        for unit, state in found.items():
            if state != "active":
                name = service_of.get((where, unit), unit)
                down.append(f"{name} on {where} ({unit}: {state}; `worms log {name}`, `worms service start {name}`)")
    if not down:
        return []
    return [["platform services", output.status_cell("error", "not active: " + "; ".join(down))]]


RUNNING_APPS = ("dashboard", "orchestrator")  # the Django apps whose units record what they started with (T8)


def _running_commits(config, listed=None) -> dict:
    """{app: the commit its units started with}: /run/worms-django/running-<app> on its VM
    (django_app's ExecStartPost, T8), read over SSH. An app not answering is left out."""
    from worms_cli import services
    from worms_cli.vms import Catalogue

    try:
        units = services.Units(config, catalogue=Catalogue(config, vms=listed))
    except Exception:  # noqa: BLE001  (no catalogue: nothing to say)
        return {}
    found = {}
    for app in RUNNING_APPS:
        try:
            _code, out = units._sh(app, f"cat /run/worms-django/running-{app} 2>/dev/null || true", check=False)
        except Exception:  # noqa: BLE001  (unreachable: left out)
            continue
        if out.split():
            found[app] = out.split()[0]
    return found


def _version_lines(config, listing, probe, running) -> list[str]:
    """`worms status`'s first block: the one answer to "which commit does this system run?"
    (consistency.version_summary): deployed.json's record of each component and the workspace,
    each service VM's `clean` snapshot, what the Django apps run, next to this checkout."""
    import json

    from worms_cli import components, consistency

    if probe.get("error"):
        return "", [output.status_cell("unknown", f"the host could not be read ({probe['error']})")]
    try:
        deployed = json.loads(probe.get("deployed", "").strip() or "{}")
    except ValueError:
        deployed = {}
    expected = [name for name, c in components.COMPONENTS.items() if c.kind in ("vm", "host", "gameplans")] + ["workspace"]
    vms = listing[0] or []
    by_vmid = consistency.parse_snapshots(probe.get("clean", ""))
    snapshots = {vm["name"]: by_vmid.get(int(vm["vmid"]), {}).get("commit", "") for vm in vms
                 if vm.get("pool") in consistency.SERVICE_POOLS}  # fmt: skip
    state, lines = consistency.version_summary(build.Runner(config).commit(), deployed, expected, snapshots, running)
    return state, [output.status_cell("ok" if state == "on" else "warn", lines[0]), *lines[1:]]


def _service_states(config, listed=None) -> tuple[dict, str]:
    """(services.Units.vm_states' states, what could not be checked, said: "" for nothing).
    `listed`: the VMs listed already (_list_vms), not listed again."""
    try:
        from worms_cli import services
        from worms_cli.vms import Catalogue

        states, not_asked = services.Units(config, catalogue=Catalogue(config, vms=listed)).vm_states()
    except Exception as exc:  # noqa: BLE001  (no catalogue, no host: said, not fatal)
        return {}, f"the platform's services ({str(exc).strip()[:60]})"
    if not not_asked:
        return states, ""
    reasons = sorted(set(not_asked.values()))
    return states, f"the services on {', '.join(not_asked)} ({reasons[0]}{' …' if len(reasons) > 1 else ''})"


def _service_files(config, listed=None) -> dict:
    """{scoring|logging: {"map": the sha256 of its devbox map ("" without one), "team": its
    current-team}}, read over SSH; a VM not answering is left out."""
    from worms_cli import services
    from worms_cli.vms import Catalogue

    try:
        units = services.Units(config, catalogue=Catalogue(config, vms=listed))
    except Exception:  # noqa: BLE001
        return {}
    found = {}
    for vm in ("scoring", "logging"):
        try:
            code, out = units._sh(vm, "sha256sum /etc/sandbox-api/devbox-teams 2>/dev/null | cut -c1-64; echo '#--'; "
                                      "cat /etc/sandbox-api/current-team 2>/dev/null; true", check=False)  # fmt: skip
        except Exception:  # noqa: BLE001
            continue
        digest, _, team = out.partition("#--")
        if code == 0 and "#--" in out:
            found[vm] = {"map": digest.strip(), "team": team.strip()}
    return found


def _controller_states(config) -> dict:
    """{vmid: name} of the VMs this checkout's OpenTofu states hold (not the host's stacks: theirs
    are in /opt/worms, the host probe's)."""
    from worms_cli import consistency
    from worms_cli.session import HOST_STACKS

    found = {}
    stacks = Path(config.repo_root) / "opentofu" / "stacks"
    for state in stacks.glob("**/terraform.tfstate"):
        if state.parent.relative_to(stacks).as_posix() not in HOST_STACKS:
            try:
                found.update(consistency.state_vms(state.read_text()))
            except OSError:
                continue
    return found


def _drift(config) -> dict:
    """`worms status --deep`: {stack: `tofu plan -detailed-exitcode`'s exit} of each controller
    stack (0: as planned, 2: changes, else: no plan), read-only (-lock=false)."""
    from worms_cli.pipeline import INIT_STACKS
    from worms_cli.session import HOST_STACKS

    runner = build.Runner(config)
    return {stack: runner.sh("tofu plan -detailed-exitcode -lock=false -input=false -no-color >/dev/null 2>&1",
                             cwd=f"opentofu/stacks/{stack}", check=False, quiet=True, tofu=True)
            for stack in INIT_STACKS if stack not in HOST_STACKS}  # fmt: skip


def _consistency_rows(config, vms, probe, facts) -> list:
    """T11: each copy against its source (worms_cli/consistency.py), from what was read once."""
    import json

    from worms_cli import consistency

    rows = consistency.version_row(facts.get("version", "on"))
    held = facts.get("held") or {}
    rows += consistency.devbox_map_rows(probe.get("map", "").strip(), {vm: f["map"] for vm, f in held.items()})
    rows += consistency.current_team_rows({vm: f["team"] for vm, f in held.items()}, _session_runs(probe))
    if vms is not None:
        sandbox_state = consistency.state_vms(probe.get("sandbox_state", ""))
        managed = {**_controller_states(config), **sandbox_state, **consistency.state_vms(probe.get("devbox_state", ""))}
        generated = probe.get("generated", "").strip()
        rows += consistency.sandbox_rows({int(vm["vmid"]) for vm in vms if vm.get("pool") == "sandbox"}, set(sandbox_state),
                                         int(generated) if generated.isdigit() else 0, probe.get("build", "").strip() == "yes")  # fmt: skip
        rows += consistency.orphan_rows(vms, managed)
        try:
            jobs = json.loads(probe.get("backup", "").strip() or "[]")
        except ValueError:
            jobs = []
        rows += consistency.backup_rows(jobs, vms)
    published = dict(line.split(" ", 1) for line in probe.get("outputs", "").splitlines() if " " in line)
    controller = {}
    for stack in published:
        try:
            controller[stack] = json.loads((Path(config.repo_root) / "opentofu" / "stacks" / stack / "terraform.tfstate")
                                           .read_text()).get("serial")  # fmt: skip
        except (OSError, ValueError):
            continue
    rows += consistency.outputs_rows(controller, {stack: serial.strip() for stack, serial in published.items()})
    rows += consistency.drift_rows(facts.get("plans") or {})
    return rows


def _attention(config, listing, internet, probe, services_found=None, facts=None) -> list[list[str]]:
    """`worms status`'s first block: what needs a look. The internet window open; devbox VMs held
    by a round or stopped by the window; service VMs without a `clean` snapshot; migrations the
    database lacks; host jobs failed in the last 24 h; host services not active. The host's part
    from _host_probe, the database's when it answers."""
    import json

    from worms_cli.pipeline import SERVICE_POOLS

    rows, unchecked = [], []
    service_states, not_asked = services_found() if services_found else _service_states(config)
    if not_asked:
        unchecked.append(not_asked)
    if "ALLOWED" in internet[1]:
        rows.append(["internet window", output.status_cell("warn", "OPEN: no team VM may run; `worms internet off`")])
    vms, _ = listing
    held, window, clean, active = (probe.get(key, "") for key in ("held", "window", "clean", "active"))
    if probe.get("error"):
        unchecked.append(f"the host ({probe['error']})")
    else:
        for text, what, hint in ((held, "held by a round", "its release starts them (`worms vm start --held` once it is over)"),
                                 (window, "stopped by the internet window", "`worms internet off` starts them")):  # fmt: skip
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
                                                             "leaves them as they are (`worms vm rebuild services`)")])  # fmt: skip
        down = [line.split()[0] for line in active.splitlines() if line.split() and line.split()[-1] != "active"]
        if down:
            rows.append(["host services", output.status_cell("error", f"not active: {', '.join(down)} (`worms log agent`, "
                                                             "`worms log firewall`)")])  # fmt: skip
        rows += _round_session_rows(probe, vms, service_states)
        rows += [[row.name, output.status_cell(row.state, row.said())]
                 for row in _consistency_rows(config, vms, probe, facts or {})]  # fmt: skip
        if vms is not None:
            from worms_cli import consistency, history

            try:
                deployed = json.loads(probe.get("deployed", "").strip() or "{}")
            except ValueError:
                deployed = {}
            for row in consistency.snapshot_rows(vms, consistency.parse_snapshots(clean), deployed,
                                                 history.parse(probe.get("ledger", ""))):  # fmt: skip
                rows.append([row.name, output.status_cell(row.state, row.said())])
    rows += _service_rows(service_states, during_round=_session_runs(probe))
    try:
        from worms_cli import django_setup

        django_setup.connected(config)
        from datetime import timedelta

        from core import clock
        from core.models.rounds import HostJob, RoundRun
        from worms_cli import services

        # Marked running, run nowhere (services.left_behind: their session ended, the agent is
        # stopped, or no heartbeat): what they hold up waits until they are closed.
        agent_stopped = not probe.get("error") and "worms-round-agent active" not in probe.get("active", "")
        left = [(job, why) for job in HostJob.objects.filter(state=HostJob.State.RUNNING).order_by("id")
                if (why := services.left_behind(job, agent_stopped))]  # fmt: skip
        if left:
            rows.append(["host jobs", output.status_cell("error", f"{len(left)} marked running, but run nowhere: "
                                                         + "; ".join(f"#{job.id} {job.kind}: {why}" for job, why in left[:3])
                                                         + f" (`worms jobs cancel {' '.join(str(job.id) for job, _ in left)}`)")])  # fmt: skip
        # An attempt under way that no session runs (a SIGKILL): every dev upload waits for it.
        if not probe.get("error") and not _session_runs(probe):
            done = [RoundRun.State.FINISHED, RoundRun.State.FAILED, RoundRun.State.SKIPPED, RoundRun.State.PENDING]
            for run in RoundRun.objects.exclude(state__in=done).select_related("team", "round"):
                rows.append(["round run", output.status_cell("error", f"{run.team.name}'s attempt {run.attempt} in round "
                                                             f"{run.round.name!r} is {run.state}, but no round session runs: "
                                                             "every dev upload waits for it; `worms round teardown` closes it "
                                                             f"(or `worms round start {run.round.id} --resume`)")])  # fmt: skip

        failed = list(HostJob.objects.filter(state=HostJob.State.FAILED, finished_at__gte=clock.now() - timedelta(hours=24))
                      .order_by("-id").values_list("id", "kind")[:20])  # fmt: skip
        if failed:
            rows.append(["host jobs", output.status_cell("error", f"{len(failed)} failed in 24 h: "
                                                         + ", ".join(f"#{n} {kind}" for n, kind in failed[:5])
                                                         + f" (`worms log {failed[0][0]}`)")])  # fmt: skip
        pending = build.Runner(config).pending_migrations()
        if pending:
            rows.append(["migrations", output.status_cell("warn", f"the database lacks {len(pending)} of this checkout's "
                                                          "(`worms apply orchestrator`)")])  # fmt: skip
    except Exception as exc:  # noqa: BLE001  (not configured, not reachable: said, not fatal)
        unchecked.append(f"the database ({str(exc).splitlines()[0][:60] if str(exc) else type(exc).__name__})")
    if not rows:
        rows.append(["", output.status_cell("ok", "nothing found")])
    if unchecked:
        rows.append(["not checked", output.status_cell("unknown", "; ".join(unchecked))])
    return rows


def cmd_status(config, args) -> int:
    """`worms status`: what needs attention, the platform, every VM, the competition. Exits 1 when
    it found something that needs a look (the attention block: more than a round in progress, or
    what it could not check), else 0: an unconfigured checkout is not a problem."""
    from concurrent.futures import ThreadPoolExecutor

    # The platform's services and what its apps run are asked over SSH, a VM each (seconds):
    # meanwhile, the rest.
    listing = _list_vms(config)
    with ThreadPoolExecutor(max_workers=3) as pool:
        services_found = pool.submit(_service_states, config, listing[0])
        running = pool.submit(_running_commits, config, listing[0])
        held = pool.submit(_service_files, config, listing[0])
        internet, probe = _internet_status(config), _host_probe(config)
        state, version = _version_lines(config, listing, probe, running.result())
        facts = {"version": state, "held": held.result(), "plans": _drift(config) if getattr(args, "deep", False) else {}}
        attention = _attention(config, listing, internet, probe, services_found.result, facts)
    print(output.heading("version"))
    print("\n".join(version))
    print()
    print(output.heading("attention"))
    print(output.table(attention))
    print()
    print(output.heading("platform"))
    print(output.table(_platform_rows(config, internet)))
    print()
    print(output.heading("VMs"))
    print(output.table(_vm_rows(config, listing, probe), headers=VM_HEADERS))
    print()
    print(output.heading("competition (database)"))
    print(output.table(_competition_status(config)))
    return 0 if all(row[0] in ("", ROUND_RUNS_LABEL, "not checked") for row in attention) else 1


# -- the lists: a noun alone (`worms jobs`), or `worms show <list>` ---------------------------------


def _listing(config, show) -> int:
    """A list from the database: `show(queries)` (worms_cli/queries.py)."""
    _with_db(config)
    from worms_cli import queries

    try:
        return show(queries) or 0
    except ValueError as exc:
        output.error(str(exc))
        return 1


def list_vms(config, args) -> int:
    """`worms vm`: every VM, as `worms status` lists them."""
    print(output.table(_vm_rows(config, _list_vms(config), _host_probe(config)), headers=VM_HEADERS))
    return 0


def list_teams(config, args) -> int:
    """`worms team`: the teams and their members; every team's devbox and SSH keys."""
    def show(queries):
        _status_teams(queries)
        print()
        print(output.heading("devboxes"))
        print(output.table(queries.devbox_rows(), headers=["Team", "Devbox", "SSH keys (name, fingerprint)"]))

    return _listing(config, show)


def list_users(config, args) -> int:
    return _listing(config, _status_users)


def list_rounds(config, args) -> int:
    return _listing(config, lambda queries: print(output.table(
        queries.round_rows(), headers=["Round", "Start (UTC)", "End", "Gameplan", "Check", "Per team", "Attempts"])))


def list_jobs(config, args) -> int:
    return _listing(config, lambda queries: print(output.table(
        queries.job_rows(getattr(args, "limit", None) or 30), headers=["#", "Job", "State", "Team", "Requested", "Finished", "Error"])))


def list_services(config, args) -> int:
    """`worms service`: each service, where it runs, whether `worms service restart` restarts it."""
    from worms_cli import services

    rows = [[name, service.where, service.about, "yes" if service.restartable else "no"] for name, service in services.SERVICES.items()]
    print(output.table(rows, headers=["service", "runs on", "what its log says", "restart"]))
    print(output.color("its log: `worms log <service>`", "dim"))
    return 0


def list_keys(config, args) -> int:
    """`worms key`: every team's SSH keys, read on the SSH gateway and its devbox VMs (worms_cli/keys.py)."""
    _with_db(config)
    from worms_cli import keys

    try:
        return keys.status(config)
    except ValueError as exc:
        output.error(str(exc))
        return 1


def show_audit(config, args) -> int:
    def show(queries):
        rows = queries.event_rows(hours=args.hours, kind=args.kind, limit=args.limit or 200)
        if not rows:
            print(f"No audit events in the last {args.hours} hour(s).")
            return
        print(output.table(rows, headers=["Time (UTC)", "Event", "Written by", "Actor", "Target", "Team", "Client", "Details"]))

    return _listing(config, show)


def show_gameplans(config, args) -> int:
    def show(queries):
        print(output.table(queries.gameplan_rows(config.repo_root), headers=["Gameplan", "Orchestrator + host", "This checkout"]))
        print(output.color("Why the deployed copy is invalid: the newest gameplans step's log (`worms jobs --limit 50`, "
                           "then `worms log <#>`; a round's: `worms log round`).", "dim"))  # fmt: skip

    return _listing(config, show)


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
# worms apply [<component>...], worms vm rebuild <component>...
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
        if component.kind == "host":
            return pipe.runner.host_sh(f"systemctl is-active --quiet {component.host_service}", check=False, quiet=True) == 0
        if component.kind in ("devbox", "gameplans"):
            return True
        return component.name in vm_names()

    return exists


def _unfinished(pipe):
    """components.plan's other question: did the build that created this VM stop before its end?"""
    cache = {}

    def unfinished(component) -> bool:
        if "names" not in cache:
            cache["names"] = pipe.unfinished_vms()
        return component.name in cache["names"]

    return unfinished


def _pending_migrations(pipe):
    def pending():
        try:
            return pipe.runner.pending_migrations()
        except build.BuildError as exc:
            return [f"unknown: {exc}"]

    return pending


def cmd_apply(config, args) -> int:
    """`worms apply`: the platform up to this checkout. Alone: what changed since what runs
    (_apply_changed); with components: those (_apply_components), built if missing; --all: the
    whole build, in order, with packages (a new host). --plan: what it would do, changing nothing."""
    if args.all:
        if args.components:
            output.error("--all is every component: name none")
            return 2
        return _apply_all(config, args)
    if not args.components:
        return _apply_changed(config, args)
    return _apply_components(config, args, args.components)


def _apply_all(config, args) -> int:
    """`worms apply --all`: the full pipeline (pipeline.build_all). The first step that fails ends
    it, and nothing after it runs but closing the internet window and starting the devboxes again."""
    from worms_cli import components
    from worms_cli.session import SessionError

    if args.plan:
        return _apply_components(config, args, [n for n, c in components.COMPONENTS.items() if c.kind != "devbox"])
    if not build.confirm("Build and configure every component, packages too: the internet window opens and the devbox VMs "
                         "are off meanwhile?", args.yes):  # fmt: skip
        return 1
    pipe = _pipeline(config, args)
    code = 0
    try:
        pipe.build_all()
        _record_deployed(pipe, [n for n, c in components.COMPONENTS.items() if c.kind in ("vm", "host")])
    except (build.BuildError, SessionError, KeyboardInterrupt) as exc:
        output.error(f"build stopped: {exc}" if str(exc) else "build stopped: interrupted")
        code = 130 if isinstance(exc, KeyboardInterrupt) else 1
    return code or (1 if pipe.devbox_map_failed else 0)


def _pipeline(config, args):
    """The pipeline of an apply: the arguments after `--` for every playbook, and with --full every
    role again (Site.yml's stamps skip the unchanged ones otherwise)."""
    extra = " ".join(args.ansible_args) + (" -e worms_full=true" if getattr(args, "full", False) else "")
    return pipeline.Pipeline(config, ansible_args=extra.strip())


def _prepared(pipe, config, names, rebuild=False):
    """(the plan of these components, whether the agent restarts after it (and the gameplans are checked), the
    sandbox VMs there are). The orchestrator and the round agent bring the gameplans along (the
    host's copy and the orchestrator's, checked), unless a round is on; pending migrations bring
    the orchestrator (components.plan). KeyError, ValueError: no such component, no plan."""
    from worms_cli import components

    exists = _exists(pipe, config)
    names = components.expand(names)
    sandboxes = sorted(vm["name"] for vm in pipe.runner.vms() if vm.get("pool") == "sandbox")

    def plan_of(names):
        return components.plan(names, exists, rebuild=rebuild, pending_migrations=_pending_migrations(pipe),
                               unfinished=_unfinished(pipe))  # fmt: skip

    plan = plan_of(names)
    if {"orchestrator", "round-agent"} & set(plan.names()) and "gameplans" not in plan.names() and not sandboxes:
        plan = plan_of([*names, "gameplans"])
    return plan, bool({"orchestrator", "round-agent", "gameplans"} & set(plan.names())), sandboxes


def _refused_during_round(plan, sandboxes, args) -> bool:
    from worms_cli import components

    machines = [n for n in plan.names() if components.COMPONENTS[n].kind in ("vm", "host")]
    if sandboxes and machines and not args.during_round:
        output.error(f"refusing while a round's sandbox exists ({len(sandboxes)} sandbox VM(s)): it restarts the round "
                     "agent and the orchestrator; destroy the sandbox at the round's end, or --during-round if it must")  # fmt: skip
        return True
    return False


def _lists_checked(config, names, packages) -> bool:
    """Before a fill of the caches: their package lists checked (worms_cli/caches.py), so that a bad
    name fails before the window opens and the devboxes stop."""
    from worms_cli import caches

    kinds = [kind for kind, component in (("apt", "apt-cache"), ("pypi", "devpi")) if component in names]
    if not packages or not kinds:
        return True
    print(output.heading("the caches' package lists"), flush=True)
    try:
        failed = max(caches.check(config, kind) for kind in kinds)
    except build.BuildError as exc:
        output.error(str(exc))
        return False
    if failed:
        output.error(f"not filled: fix the list first ({', '.join(caches.LISTS[k][0] for k in kinds)}); nothing was opened")
        return False
    return True


def _restore_point(config, pipe, names) -> tuple[bool, str]:
    """(go on?, the dump): a dump of the database first, when the orchestrator (it migrates) or
    the database is among what changes and the database VM is there."""
    from worms_cli import components, database

    if not {"orchestrator", "database"} & set(names) or not _exists(pipe, config)(components.COMPONENTS["database"]):
        return True, ""
    print(output.heading("restore point: a dump of the database"), flush=True)
    try:
        path, size = database.backup(config)
    except (build.BuildError, OSError) as exc:
        output.error(f"no dump: {exc}")
        return build.confirm("Go on without one?", False), ""
    print(output.status_cell("ok", f"{size / 1e6:.1f} MB: {path}"), flush=True)
    return True, path


def _configure(config, args, pipe, plan, follow_up) -> int:
    """The plan carried out: its components (pipeline.build_components), the agent (once no host job
    runs) restarted and the host's gameplans checked when `follow_up`, the devboxes (the round agent's jobs,
    followed); what runs recorded (worms_cli/changes.py)."""
    from worms_cli import components, services

    infrastructure = components.Plan([step for step in plan.steps if components.COMPONENTS[step[0]].kind != "devbox"],
                                     plan.notes)  # fmt: skip
    if infrastructure.steps:
        pipe.build_components(infrastructure, packages=args.packages)
        _record_deployed(pipe, infrastructure.names())
    if follow_up:
        _with_db(config)
        services.wait_until_idle(getattr(args, "timeout", 900))
        services.Restarter(config).restart("agent")
        print(output.status_cell("ok", "agent: restarted, active"))
        from worms_cli import rounds

        rounds.check_gameplans(config)
    if infrastructure.steps:
        print(output.status_cell("ok", f"applied: {', '.join(infrastructure.names())}"))
    if "devbox" in dict(plan.steps):
        return _build_devboxes(config, wait=True)
    return 0


def _way_back(before: dict, dump: str) -> None:
    """The last lines of an apply: the commit the components ran before, and the dump."""
    dirty = sorted({c for c in before.values() if c.endswith("-dirty")})
    before = {name: commit.removesuffix("-dirty") for name, commit in before.items()}
    commits = sorted(set(before.values()))
    if dirty:
        print(output.color(f"(they ran {', '.join(dirty)}: uncommitted changes then, which the commit alone does not have)", "dim"))
    if len(commits) == 1:
        back = f"`git checkout {commits[0]}`, then `worms apply`"
    elif commits:
        by = {c: sorted(n for n, commit in before.items() if commit == c) for c in commits}
        back = "check out what they ran (" + "; ".join(f"{c}: {', '.join(n) if len(n) <= 3 else f'{len(n)} components'}"
                                                     for c, n in by.items()) + "), then `worms apply`"  # fmt: skip
    else:
        back = "what they ran before is not recorded"
    print(output.color(f"the way back: {back}" + (f"; the database as it was: `worms db restore {dump}`" if dump else ""), "dim"))


def _ran_before(pipe, names) -> dict:
    from worms_cli import changes

    try:
        recorded = {name: commit for name, (commit, _time) in changes.deployed(pipe.runner.session).items()}
    except Exception:  # noqa: BLE001  (the record is a convenience)
        return {}
    return {name: recorded[name] for name in names if recorded.get(name)}


def _apply_components(config, args, names, rebuild=False) -> int:
    """`worms apply <component>...` and `worms vm rebuild <component>...`: the plan, what it touches;
    created what is missing (after asking), configured what exists; a restore point first when the
    database may change."""
    from worms_cli import changes, components
    from worms_cli.session import SessionError

    pipe = _pipeline(config, args)
    code = 0
    try:
        try:
            plan, follow_up, sandboxes = _prepared(pipe, config, names, rebuild=rebuild)
        except KeyError as exc:
            output.error(f"no component {exc.args[0]!r}: `worms apply --help` lists them")
            return 1
        except ValueError as exc:
            output.error(str(exc))
            return 1
        _print_plan(plan, args.packages)
        if follow_up:
            print(output.color("  then: the round agent restarted once no host job runs, then the host's gameplans checked", "dim"))
        if args.plan:
            vms = [name for name, _ in plan.steps if components.COMPONENTS[name].kind == "vm"]
            if vms:
                print()
                print(output.heading("what each VM runs now (its last configuration's commit) and this checkout"))
                print(output.table(changes.deployed_commits(pipe, vms), headers=["VM", "configured from", "this checkout", ""]))
            print()
            print(output.heading("OpenTofu's own plan (nothing is applied)"))
            changes.tofu_plans(pipe, plan)
            return 0
        if _refused_during_round(plan, sandboxes, args) or not _lists_checked(config, plan.names(), args.packages):
            return 1
        if not _asked(plan, args.yes):
            return 1
        before = _ran_before(pipe, plan.names())
        go_on, dump = _restore_point(config, pipe, plan.names())
        if not go_on:
            return 1
        code = _configure(config, args, pipe, plan, follow_up)
        _way_back(before, dump)
    except (build.BuildError, SessionError, KeyboardInterrupt) as exc:
        output.error(f"stopped: {exc}" if str(exc) else "stopped: interrupted")
        code = 130 if isinstance(exc, KeyboardInterrupt) else 1
    return code or (1 if pipe.devbox_map_failed else 0)


def _asked(plan, yes) -> bool:
    """Agreed to what the plan builds besides what it configures (or nothing to ask)."""
    asks = []
    if plan.added:
        asks.append(f"build {len(plan.added)} more component(s) it needs ({', '.join(plan.added)})")
    if plan.names("recreate"):
        asks.append(f"DESTROY and build again {', '.join(plan.names('recreate'))}"
                    + (": every record in the database is LOST" if "database" in plan.names("recreate") else ""))  # fmt: skip
    return not asks or build.confirm("This will " + "; ".join(asks) + ". Proceed?", yes)


def _changed_report(config, pipe):
    """What changed since what runs (worms_cli/changes.py), said; (changes, stack plans, the
    directory of the saved plans)."""
    import tempfile

    from worms_cli import changes, components

    found, _bases, unclaimed = changes.analyse(config, pipe.runner.session)
    exists = _exists(pipe, config)
    print(output.heading(f"what changed since what runs, up to this checkout ({pipe.runner.commit()})"))
    rows = []
    for change in found:
        component = components.COMPONENTS[change.component]
        files = [w for w in change.why if "/" in w]
        reasons = [w for w in change.why if "/" not in w]
        text = "; ".join([*reasons, *(files[:3]), *([f"+{len(files) - 3} more"] if len(files) > 3 else [])])
        missing = component.kind in ("vm", "host") and not exists(component)
        rows.append([change.component, change.base or "?", text[:150], ", ".join(change.packages)[:60] or "—",
                     "not built: `worms apply " + change.component + "`" if missing else change.note])  # fmt: skip
    if rows:
        print(output.table(rows, headers=["component", "runs", "why", "packages", "note"]))
    else:
        print(output.status_cell("ok", "every component runs this checkout"))
    if unclaimed:
        print(output.color(f"changed, but no component is made from it (checked by nothing here): {', '.join(unclaimed[:8])}"
                           + (f" (+{len(unclaimed) - 8})" if len(unclaimed) > 8 else ""), "yellow"))  # fmt: skip
    print()
    print(output.heading("OpenTofu's own plan of each stack (saved: what is applied is what is shown)"))
    directory = tempfile.mkdtemp(prefix="worms-plans-")
    plans = []
    for stack in changes.stacks_to_plan():
        plan = changes.plan_stack(pipe, stack, directory)
        plans.append(plan)
        if plan.error:
            print(output.status_cell("error", f"{stack}: not planned: {plan.error}"))
        elif plan.empty:
            print(output.status_cell("ok", f"{stack}: no changes"))
        else:
            counts = f"{len(plan.updates)} in place, {len(plan.creates)} new, {len(plan.replaces)} REPLACED, {len(plan.deletes)} DESTROYED"
            print(output.status_cell("warn" if plan.safe else "error", f"{stack}: {counts}"
                                     + ("" if plan.safe else ": not applied here (a VM made anew, replaced or destroyed)")))  # fmt: skip
            for address in [*plan.replaces, *plan.deletes, *plan.creates, *plan.updates][:12]:
                print(output.color(f"    {address}", "dim"))
    return found, plans, directory


def _apply_changed(config, args) -> int:
    """`worms apply` alone: what changed since what runs. OpenTofu's in-place changes first,
    applied from the plans shown, then the components that changed are configured; packages only
    with --packages. A VM replaced or destroyed is never done here: `worms vm rebuild` does it."""
    import shutil

    from worms_cli import components
    from worms_cli.session import SessionError

    pipe = _pipeline(config, args)
    try:
        found, plans, directory = _changed_report(config, pipe)
    except build.BuildError as exc:
        output.error(str(exc))
        return 1
    code = 0
    try:
        exists = _exists(pipe, config)
        names = [c.component for c in found if components.COMPONENTS[c.component].kind in ("vm", "host", "gameplans")
                 and (components.COMPONENTS[c.component].kind == "gameplans" or exists(components.COMPONENTS[c.component]))]
        stacks = [plan for plan in plans if not plan.empty and plan.safe]
        left = [plan for plan in plans if not plan.empty and not plan.safe]
        print()
        if not names and not stacks:
            print(output.status_cell("ok", "nothing to apply: the platform runs this checkout"
                                     + (" (but see OpenTofu's changes above)" if left else "")))  # fmt: skip
            return 1 if left else 0
        print(output.heading("what `worms apply` does"))
        for plan in stacks:
            print(f"  OpenTofu: apply {plan.stack}'s saved plan ({len(plan.updates)} in place, {len(plan.creates)} new)")
        if names:
            print(f"  configure: {', '.join(names)}" + (" --packages (the internet window opens)" if args.packages else ""))
        for plan in left:
            print(output.color(f"  not here: {plan.stack} ({len(plan.replaces)} replaced, {len(plan.deletes)} destroyed): "
                               "`worms vm rebuild <vm>` or `worms apply <stack>` does it, by name", "yellow"))  # fmt: skip
        for change in found:
            if change.note:
                print(output.color(f"  {change.component}: {change.note}", "dim"))
        packaged = [c.component for c in found if c.packages and c.component in names]
        if packaged and not args.packages:
            output.error(f"the packages of {', '.join(packaged)} changed: with --packages (it opens the internet window; on "
                         "production the host needs egress), or they would be missing")  # fmt: skip
            return 1
        plan, follow_up, sandboxes = _prepared(pipe, config, names) if names else (None, False, [])
        if plan is not None:
            print()
            _print_plan(plan, args.packages)
        if args.plan:
            return 0
        if plan is not None and (_refused_during_round(plan, sandboxes, args)
                                 or not _lists_checked(config, plan.names(), args.packages)):  # fmt: skip
            return 1
        if not build.confirm("Apply these changes?", args.yes) or (plan is not None and not _asked(plan, args.yes)):
            return 1
        before = _ran_before(pipe, plan.names() if plan else [])
        go_on, dump = _restore_point(config, pipe, plan.names() if plan else [])
        if not go_on:
            return 1
        for stack in stacks:
            print(output.heading(f"OpenTofu: {stack.stack}"), flush=True)
            try:
                pipe.runner.tofu(stack.stack, "apply", "-input=false", stack.file)
                if stack.stack in pipeline.PUBLISHED_STACKS:  # what the host's stacks read (T2)
                    pipe.publish_outputs(stack.stack)
            except build.BuildError as exc:
                output.error(f"{stack.stack}: {exc}; nothing after it was applied")
                return 1
        if plan is not None:
            print(output.heading("configure"), flush=True)
            code = _configure(config, args, pipe, plan, follow_up)
        _way_back(before, dump)
        code = code or (1 if left else 0)
    except (build.BuildError, SessionError, KeyboardInterrupt) as exc:
        output.error(f"stopped: {exc}" if str(exc) else "stopped: interrupted")
        code = 130 if isinstance(exc, KeyboardInterrupt) else 1
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    return code or (1 if pipe.devbox_map_failed else 0)


def _print_plan(plan, packages: bool) -> None:
    """The plan of `worms apply <component>` or `worms apply`: each component, what happens to
    it and what that touches; then what the whole run touches (components.Plan.touches)."""
    rows, summary = plan.touches(packages)
    print(output.heading("plan"))
    print(output.table([[name, what, rows[name]] for name, what, _ in plan.table()], headers=["component", "what happens", "touches"]))
    for line in summary:
        print(output.color(f"  {line}", "dim"))
    for note in plan.notes:
        print(output.color(f"note: {note}", "dim"))


def _wait_for_jobs(jobs, config=None) -> int:
    """Follow these host jobs until each has ended: why one waits while it is queued, then its log,
    live (worms_cli/jobs.py). 1 if one failed, 130 if Ctrl-C (the jobs go on)."""
    from worms_cli import jobs as host_jobs
    from worms_cli.session import for_config

    return host_jobs.follow(for_config(config or Config.load()), jobs)


def _open_jobs(kind: str) -> list:
    from core.models.rounds import HostJob

    return list(HostJob.objects.filter(kind=kind, state__in=[HostJob.State.QUEUED, HostJob.State.RUNNING]).order_by("id"))


def _build_devboxes(config, rebuild=False, teams=(), wait=False) -> int:
    """`worms apply devbox`: a devbox for every team without one (the orchestrator enqueues the
    build; the round agent on the host builds them, docs/04-dev-environment.md). With --rebuild:
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
        return _wait_for_jobs(_open_jobs("devbox_provision"), config) if wait else 0
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
            print(f"{team.name}: no devbox to rebuild (`worms apply devbox` builds it)")
        else:
            print(f"{team.name}: {'enqueued' if enqueued else 'already queued'} devbox_rebuild #{job.id} {job.uuid} (from nothing)")
    if wait:
        return _wait_for_jobs(jobs, config)
    print(output.color("The round agent does the work: `worms jobs`, `worms log latest -f`.", "dim"))
    return 0


def _record_deployed(pipe, names) -> None:
    """These components run this checkout now (worms_cli/changes.py: what `worms apply` alone starts
    from). Best effort: a build never fails for its record."""
    from worms_cli import changes, components

    kept = [n for n in names if components.COMPONENTS[n].kind in ("vm", "host", "gameplans")]
    kept += ["workspace"] if "round-agent" in kept else []  # its configuration syncs the workspace (T7)
    if not kept:
        return
    try:
        changes.record_deployed(pipe.runner.session, kept, pipe.runner.commit())
    except Exception as exc:  # noqa: BLE001
        output.warn(f"the record of what runs ({changes.DEPLOYED}) did not get {', '.join(kept)}: {exc}")


def cmd_validate(config, args) -> int:
    """`worms check [<team>...]`: can each team work on its devbox, as it does: login
    through the SSH gateway, the flag, scoring, logging, apt, PyPI (worms_cli/validate.py)."""
    _with_db(config)
    from worms_cli import validate

    try:
        return validate.devbox(config, args.teams)
    except (ValueError, build.BuildError) as exc:
        output.error(str(exc))
        return 1


def cmd_devbox_sync_keys(config, args) -> int:
    """`worms key sync`: every team's SSH keys, from the database, copied over SSH to the SSH
    gateway and its devbox VMs; then where they are (worms_cli/keys.py)."""
    _with_db(config)
    from worms_cli import keys

    return keys.sync(config)


def cmd_step(config, args) -> int:
    """`worms step <name>`: one pipeline step (pipeline.STEPS). The round agent runs these, never
    interactively; for troubleshooting too."""
    pipe = _pipeline(config, args)
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
    except Exception as exc:  # noqa: BLE001  (the host may be out of reach)
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
            return logs.run_log(args.nth, args.lines, args.follow, args.vm)
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


def _show_vm(config, name: str) -> int:
    """`worms show <vm>|<team>`: a VM in detail, with its history; a team's devbox."""
    from worms_cli.vms import Catalogue

    catalogue = Catalogue(config)
    try:
        team = catalogue.find_team(name) if "/" not in name and not catalogue.knows(name) else None
        if team is not None:
            return _show_team(config, catalogue, team)
        print(output.heading(name))
        print(output.table(catalogue.details(name)))
        rows, note = catalogue.history_rows(name)
        print()
        print(output.heading("history, newest first"))
        if rows:
            print(output.table(rows, headers=["time (UTC)", "event", "commit", "by", "detail"]))
        print(output.color(note, "dim"))
    except build.BuildError as exc:
        output.error(str(exc))
        return 1
    return 0


def _show_team(config, catalogue, team) -> int:
    """`worms show <team>`: its devbox's four VMs (vmid, address, state) and its keys on each."""
    from worms_cli import keys

    print(output.heading(f"team {team.label}"))
    cells = keys.team_cells(config, team.slug, catalogue) if team.devbox else {}
    print(output.table(catalogue.team_rows(team, cells), headers=["target", "VM", "vmid", "address", "state", "team keys"]))
    if cells.get("keys"):
        print(output.color(f"{cells['keys']} key(s) in the database; `worms key`: every team's", "dim"))
    print(output.color(f"worms login {team.handle}/a · worms show {team.handle}/gw · worms vm reset {team.handle}", "dim"))
    return 0


def list_tables(config, args=None) -> int:
    """`worms db`: every table of the platform's database, with its rows."""
    _with_db(config)
    from worms_cli import database

    print(output.table(database.tables(), headers=["table", "rows (estimate)", "size"]))
    print(output.color("a table's newest rows: `worms show table <table>` (--limit N, --schema)", "dim"))
    return 0


def _show_table(config, args) -> int:
    """`worms show table <name>`: its newest rows, or its columns (--schema)."""
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
    """`worms internet`: whether the window is open. `worms internet on` is refused during a round
    (a sandbox VM exists). Running devbox VMs are stopped first, after asking: no team VM runs
    while the window is open; they are written down on the host (power.WINDOW_STOPPED). `off`
    closes it, switches DNS and the caches back to offline mode (`worms step offline`) and starts
    those devboxes again."""
    if args.state is None:
        print(": ".join(_internet_status(config)))
        return 0
    pipe = pipeline.Pipeline(config)
    try:
        if args.state == "on":
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
                    # host first, for `worms internet off` to start again.
                    for vm in pipe.running_devboxes():
                        recorded = [r for r in recorded if r["vmid"] != int(vm["vmid"])] + [{"vmid": int(vm["vmid"]), "name": vm["name"]}]
                        power.write_record(pipe.runner, power.WINDOW_STOPPED, recorded)
                        pipe.runner.host_sh(power.SHUTDOWN.format(vmid=int(vm["vmid"])))
                        stopped.append(vm)
                finally:
                    if stopped:  # also when a later one failed: these are off now
                        print(output.color(f"stopped {len(stopped)} devbox VM(s): {', '.join(vm['name'] for vm in stopped)}. "
                                           "`worms internet off` starts them again once the window is closed "
                                           f"(on the host: {pipeline.start_command(vm['vmid'] for vm in stopped)})", "dim"))  # fmt: skip
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
    """After the window closed: the devbox VMs `worms internet on` shut down, started again
    (those still there and stopped). Not while a round is on (a sandbox VM exists) or holds the
    devboxes (its record): the record may be stale, and a round with devboxes off must not get
    them back; it stays, and says so. The window is closed by then: a record that cannot be
    read is a warning, not a failed close; a VM that does not start is an error (exit 1)."""
    try:
        recorded = power.read_record(runner, power.WINDOW_STOPPED)
        if not recorded:
            return 0
        held = power.read_record(runner, power.HELD_DEVBOXES)
        sandboxes = sorted(vm["name"] for vm in runner.vms() if vm.get("pool") == "sandbox")
    except build.BuildError as exc:
        output.warn(f"the devboxes the window stopped are not started again: {exc}. Start them with `worms vm start`")
        return 0
    names = ", ".join(r["name"] for r in recorded)
    if held or sandboxes:
        why = f"a round holds the devboxes ({power.HELD_DEVBOXES})" if held else f"a round is on ({len(sandboxes)} sandbox VM(s))"
        output.warn(f"{why}: the devbox VM(s) the window stopped stay off and written down ({names}); "
                    "`worms internet off` again once it is over, or `worms vm start <vm>`")  # fmt: skip
        return 0
    vms = power.still_there(runner, recorded)
    failed = power.start(runner, vms)
    power.write_record(runner, power.WINDOW_STOPPED, [r for r in recorded if r["name"] in failed])
    started = [vm["name"] for vm in vms if vm["name"] not in failed]
    if started:
        print(output.status_cell("running", f"started again the devbox VM(s) the window stopped: {', '.join(started)}"))
    if failed:
        output.error(f"did not start again: {', '.join(failed)} (`worms vm start {failed[0]}`)")
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
            pipe.round_reset_services()  # the devbox map pushed again from the host's copy, at once
        except build.BuildError as exc:
            output.error(str(exc))
            return 1
        return 1 if pipe.devbox_map_failed else 0
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
        output.error("which team's devbox? `worms vm reset <team>...` or `worms vm reset devboxes`")
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
        job, enqueued = request_rebuild(team, fresh=fresh)  # fresh: as `worms vm rebuild devboxes`
        events.team("devbox_reset", actor=_console(), team=team)
        jobs.append(job)
        if job is None:
            print(f"{team.name}: no devbox (version bumped all the same)")
        else:
            print(f"{team.name}: {'enqueued' if enqueued else 'already queued'} devbox_rebuild #{job.id} {job.uuid}")
    if getattr(args, "detach", False):
        print(output.color("queued; follow them with `worms log latest -f`", "dim"))
        return 0
    return _wait_for_jobs(jobs, config)


ROUND_CRITICAL = ("agent", "processor", "scoring", "logging")  # a round's attempt needs them running


def cmd_restart(config, args) -> int:
    """`worms service restart <service>...`: in a safe order (services.SERVICES); the agent once no host
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
    """`worms db backup`: a pg_dump of the platform's database, here (database.backup)."""
    from worms_cli import database

    try:
        path, size = database.backup(config, args.output)
    except (build.BuildError, OSError) as exc:
        output.error(str(exc))
        return 1
    print(output.status_cell("ok", f"{size / 1e6:.1f} MB: {path}"))
    print(output.color(f"readable by you only (0600). Restore it: `worms db restore {path}`", "dim"))
    return 0


def cmd_power(config, args) -> int:
    """`worms vm start|stop|restart <target>...` (worms_cli/power.py): a restart is a stop, then a start."""
    held = getattr(args, "held", False)
    if not args.targets and not held:
        output.error(f"which VMs? `worms vm {args.action} <target>...`" + (" or --held" if args.action == "start" else ""))
        return 2
    try:
        machines = power.Power(config)
        if args.action == "start":
            return machines.start(args.targets, held=held)
        code = machines.stop(args.targets)
        return machines.start(args.targets) if args.action == "restart" and code == 0 else code
    except build.BuildError as exc:
        output.error(str(exc))
        return 1


def completion_slots() -> dict[str, list[str]]:
    """What tab completion offers for a command's positional arguments that its parser cannot
    say, slot by slot (the last one repeats; "" = files): from the CLI's own tables."""
    from worms_cli import components, remote, services

    built = " ".join([*components.COMPONENTS, *components.GROUPS])
    groups = " ".join(remote.groups())
    machines = f"@targets @teams devboxes {groups}"
    vms = " ".join(n for n, c in components.COMPONENTS.items() if c.kind == "vm")
    return {
        "show": [f"@targets @teams {' '.join(SHOW_TOPICS)} job table", ""],
        "log": [f"@targets @teams {' '.join(services.SERVICES)} run latest", ""],
        "check": ["@teams packages tokens"],
        "trace": ["@teams upload", ""],
        "login": ["@targets", ""],
        "run": [f"@targets {groups}"],
        "rsync": ["@rsync"],
        "apply": [built],
        "vm start": [machines],
        "vm stop": [machines],
        "vm restart": [machines],
        "vm reset": ["@teams devboxes services"],
        "vm rebuild": [f"@teams devboxes {vms} {' '.join(components.GROUPS)}"],
        "service restart": [" ".join(n for n, s in services.SERVICES.items() if s.restartable)],
        "team delete": ["@teams", ""],
        "key add": ["@teams", ""],
        "key remove": ["@teams", ""],
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
        print("It is installed when the team's devbox is built (`worms apply devbox`).")
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
                     "(`worms team`)")  # fmt: skip
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
# secrets / trust-host
# ---------------------------------------------------------------------------


def cmd_secrets(config, args) -> int:
    """`worms secret`: what the vault holds (never a value); `worms secret set <name>`:
    enter one, prompted without echo or read from stdin; `worms secret issue <name>`: create or
    rotate a Proxmox token and store it; `worms check tokens`: ask Proxmox whether the stored
    tokens are right."""
    import getpass

    from worms_cli.secrets import EXTERNAL, Vault, VaultError, vms_deployed
    from worms_cli.session import SessionError, for_config
    from worms_cli.tokens import TOKENS, TokenError, check, issue

    vault = Vault(config.repo_root)
    try:
        if args.secrets_action == "issue":
            spec = TOKENS[args.name]
            vault.set(args.name, issue(spec, for_config(config).host_run))
            print(f"{args.name}: issued {spec.full_id} ({spec.role} on {spec.path}), stored in {vault.file}")
            print(output.color("Deliver it to its users with `worms apply`.", "dim"))
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
                    output.error("pipe the private key in: worms secret set ssh-key < key")
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
    print(output.color(f"vault: {vault.file} (key {vault.key_file}); set one with `worms secret set <name>`", "dim"))
    return 0


def cmd_trust_host(config, args) -> int:
    """Let the WORMS key (vault) in, the one key the build logs in with: for root on the host,
    and on the VMs that exist already (they accept root's old key on the host, which logs in to
    them). Once, with your own login: over SSH from a laptop, right here on the host. It fails
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
    # The VMs the host knows of: ours, and its own (earlier) inventories.
    script = rf"""set -u
umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; {add}
echo "host: the WORMS key may log in as $(id -un)"
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
        print(output.color("$ (this host, as root) ...", "dim"))
        argv = ["sh", "-c", script]
    if subprocess.run(argv).returncode != 0:
        output.error("the WORMS key is not let in everywhere yet: see the lines above")
        return 1
    try:
        session.check_host()
    except SessionError as exc:
        output.error(str(exc))
        return 1
    print(output.color("root on the host accepts the WORMS key (as OpenTofu logs in): ok", "green"))
    return 0


# ---------------------------------------------------------------------------
# the commands, git's way: a few verbs to look and to reach a VM, and a noun for each kind of
# thing that changes (`worms vm reset alpha`, `worms jobs pause`); a noun alone lists its things
# ---------------------------------------------------------------------------


class Section(NamedTuple):
    """One part of `worms --help`: (as typed, what it does) per line."""

    title: str
    about: str
    commands: tuple[tuple[str, str], ...]


# `worms <noun> <verb> ...`; `worms <noun>` alone lists (`worms jobs`: the queue).
NOUNS = ("vm", "jobs", "round", "service", "db", "team", "key", "user", "secret")

# `worms show <topic>`: what has no noun of its own.
SHOW_TOPICS = {
    "leaderboard": "the leaderboard, as the dashboard shows it",
    "gameplans": "the gameplans: valid on the orchestrator and the host? why not",
    "audit": "the audit events: sign-ins, passwords, view-as, keys, SSH gateway logins",
}
# `worms show <list>` is the noun alone too.
LISTS = {"vms": "vm", "jobs": "jobs", "rounds": "round", "services": "service", "tables": "db", "teams": "team",
         "devboxes": "team", "keys": "key", "users": "user", "secrets": "secret"}  # fmt: skip

SECTIONS = (
    Section("look", "Change nothing. A VM: its name or <team>/a|b|c|gw; a team: its name or t<id>.", (
        ("status", "the platform at a glance, attention first"),
        ("show <vm>|<team>", "a VM in detail; a team's devbox and keys"),
        ("show job <n>", "a host job: why it waits, its log's end"),
        ("show table <name>", "a table's newest rows (--schema: columns)"),
        ("show leaderboard|gameplans|audit", "the scores; the gameplans' check; the events"),
        ("log [<what>] [-f]", "a service's, VM's, job's or team's log"),
        ("check [<team>...]", "is each team's devbox set up and working?"),
        ("check packages|tokens", "the caches' package lists; the API tokens"),
        ("trace <team>|upload <n>", "where a team's upload is, why it waits"),
    )),
    Section("reach", "Over the management SSH; what a team's VM prints is escaped.", (
        ("login <vm> [-- <command>]", "a shell on a VM, or a command there"),
        ("run <target>... -- <command>", "a command on several VMs, or a group"),
        ("rsync <source>... <target>", "files between here and a VM"),
    )),
    Section("deploy", "Each shows its plan and asks first; packages open the internet window.", (
        ("apply", "only what changed, up to this checkout"),
        ("apply <component>...", "these components, built if missing"),
        ("apply --all", "everything in build order: a new host"),
        ("destroy [--all]", "the VM stacks; --all: everything"),
        ("internet [on|off]", "the internet window, for packages"),
    )),
    Section("machines", "A target: a VM, a group, a team (its devbox) or `devboxes` (every team's).", (
        ("vm", "every VM and its state"),
        ("vm start <target>...", "start VMs (--held: those a round held off)"),
        ("vm stop <target>...", "shut VMs down, forced after 120 s"),
        ("vm restart <target>...", "shut down, then start again"),
        ("vm reset <team>...|devboxes", "a team's devbox back to its snapshot"),
        ("vm reset services", "the service VMs back to their snapshot"),
        ("vm rebuild <team>...|devboxes", "a team's devbox built again from nothing"),
        ("vm rebuild <vm>...", "platform VMs built again from nothing"),
        ("vm kill", "hard-stop every VM now: the emergency stop"),
    )),
    Section("operate", "The round agent's queue, the rounds, the services.", (
        ("jobs", "the round agent's host jobs"),
        ("jobs pause", "the agent takes no more jobs (its own ends)"),
        ("jobs resume", "the agent takes jobs again"),
        ("jobs cancel <n>...|--team <t>", "queued jobs, never run"),
        ("round", "the rounds and their attempts"),
        ("round start <r>", "run a round here, now, team by team (asks first)"),
        ("round start <r> --resume", "... every team that has not finished"),
        ("round publish <r>", "its results to the teams and the leaderboard"),
        ("round unpublish <r>", "... hidden from them again"),
        ("round teardown", "the round's sandbox, kept after a session, destroyed"),
        ("service", "the services and where they run"),
        ("service restart <name>...", "in a safe order (the agent once idle)"),
        ("service stop <name>...", "stopped (orchestrator: all of its services)"),
        ("service start <name>...", "... started again"),
    )),
    Section("data and accounts", "The console's part: teams and rounds are managed on the dashboard.", (
        ("db", "the database's tables"),
        ("db backup", "a dump of the database, here"),
        ("db restore <dump>", "DANGER: the database back to a dump"),
        ("db reset", "DANGER: an empty database, initialised"),
        ("db dev-reset", "DEV: an empty database with admin, alpha (TeamA), beta (TeamB); upload zips asked"),
        ("team", "the teams, their members and devboxes"),
        ("team delete <name>", "a team and what it owns (its users stay)"),
        ("key", "every team's SSH keys, as its VMs have them"),
        ("key add <team> <key>", "a team's SSH key, then onto its VMs"),
        ("key remove <team> <key>", "one of its keys, then off its VMs"),
        ("key sync", "every team's keys copied to its VMs"),
        ("user", "every user: team, signed in, password"),
        ("user add <email>", "a staff account; its password, shown once"),
        ("user password [<user>]", "a one-time password for admin (or <user>)"),
    )),
    Section("set up", "Once per controller.", (
        ("init", "the vault; the WORMS key on the host"),
        ("secret", "what the vault holds (never a value)"),
        ("secret set <name>", "store a secret by hand"),
        ("secret issue <name>", "create or rotate a Proxmox token"),
        ("completion bash", "tab completion for bash"),
        ("test [--live]", "the tests; --live: the simulation"),
    )),
)  # fmt: skip

# Not in the help: the round agent's (and for troubleshooting, docs/03-manage-py.md).
HIDDEN = {"step": "one pipeline step (the round agent's): " + ", ".join([*pipeline.STEPS, "debug"])}

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
host, the login user's (debian) elsewhere.

A devbox or sandbox VM is assumed rooted by its team. Downloaded from one, files get no
setuid bits, owner or device files, and symbolic links stay inert (--munge-links: a
backup goes back up as it came); what it prints is escaped. Options after -- can override
that. rsync has to be on the VM: a devbox has it (devbox_packages), the SSH gateway not."""


RUN_HELP = """\
  worms run dashboard -- systemctl is-active dashboard nginx
  worms run services -- df -h /           every service VM, a [<vm>] prefix a line
  worms run host -- qm list               the Proxmox host
  worms run alpha/a --root -- ls /root    a team's VM: only when named itself
  worms run dns -- 'journalctl -n 50 | grep named'    one word: the VM's shell runs it

Targets: host; a VM by its name, an alias or <team>/<a|b|c|gw>; a group:
infrastructure, services, private-internet (platform VMs only: a team's VM never
runs a command as part of a group). After --, several words are the command and
its arguments, each kept as it is (quoted for the VM's shell); one word is a
command line for the VM's shell (pipes, redirections). Never interactive
(BatchMode, no terminal); the WORMS key is never forwarded. What a devbox or
sandbox VM prints is escaped. The exit code is the worst of the VMs'; a stopped
VM is not started."""

LOG_HELP = """\
  worms log                 the names there are
  worms log processor -f    a service's log (`worms service` lists them), followed
  worms log round [3] [alpha]  a round's sessions (`worms round start`): round.log, an attempt's steps
  worms log round [3] summary  what failed in its newest session, why, and the file with the rest
                            (~/.worms/rounds/latest/summary.log: always the newest session's)
  worms log dns             a VM's journal (--since "1 hour ago")
  worms log 2003            a host job's log, by its number (`worms jobs`)
  worms log latest -f       the newest host job's, followed
  worms log alpha           everything about a team: its jobs, its uploads, what
                            scoring and logging refused from its devbox
  worms log run [N]         the N-th newest run of a changing command here
  worms log run [N] dns     that run's Ansible log of one VM (each VM has its own)"""


def _command_name(usage: str) -> str:
    """`vm reset <team>...` -> `vm reset`, `show job <n>` -> `show`: a noun and its verb, else the
    command's first word."""
    words = usage.split(" ")
    return " ".join(words[:2]) if words[0] in NOUNS and len(words) > 1 and words[1][:1].isalpha() and words[1].replace("-", "").isalpha() else words[0]


def _rows_of(command: str) -> list[tuple[str, str]]:
    return [(usage, text) for section in SECTIONS for usage, text in section.commands if _command_name(usage) == command]


def _command_help(command: str) -> str:
    """What `worms <command>` does: its lines in SECTIONS, joined (HIDDEN: its own)."""
    if command in HIDDEN:
        return HIDDEN[command]
    texts = [text for _usage, text in _rows_of(command)]
    if not texts:
        raise KeyError(f"`worms {command}` is under no section of SECTIONS")
    return "; ".join(texts)


def _format_rows(rows: list[tuple[str, str]], column: int) -> list[str]:
    """`  usage  text` lines; a usage longer than the column puts its text on the next line."""
    lines = []
    for usage, text in rows:
        if len(usage) > column:
            lines.append(f"  {usage}")
            usage = ""
        lines.append(textwrap.fill(text, HELP_WIDTH, initial_indent=f"  {usage:<{column}}  ", subsequent_indent=" " * (column + 4),
                                   break_long_words=False, break_on_hyphens=False))  # fmt: skip
    return lines


COLUMN = 29


def _commands_overview() -> str:
    """The sections of `worms --help`."""
    parts = []
    for section in SECTIONS:
        about = textwrap.fill(section.about, HELP_WIDTH, initial_indent="  ", subsequent_indent="  ")
        parts.append("\n".join([output.heading(section.title), about, *_format_rows(list(section.commands), COLUMN)]))
    return "\n\n".join(parts)


def _components_overview() -> str:
    """`worms apply --help`: every component, what it needs, the groups."""
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
        textwrap.fill("Alone, `worms apply` finds what changed: per component, the files of it this checkout changed since "
                      "its last apply (git) and its packages, and OpenTofu's in-place changes (a VM replaced or destroyed "
                      "is left to `worms vm rebuild`). A component that exists is configured again, offline unless "
                      "--packages; one that is missing is created, and so is a missing dependency, once you agree. When "
                      "the orchestrator (it migrates) or the database changes, a dump of the database comes first. The "
                      "orchestrator and the round agent bring the gameplans along, and restart the agent (once no host "
                      "job runs) after; then the host's gameplans are checked (the dashboard's list).", HELP_WIDTH),  # fmt: skip
    ])  # fmt: skip


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def _yes(parser) -> argparse.ArgumentParser:
    parser.add_argument("--yes", action="store_true", help="do not ask first")
    return parser


def _json(parser) -> argparse.ArgumentParser:
    parser.add_argument("--json", action="store_true", help="what it shows as one JSON document on stdout (sections: "
                        "heading, tables as rows keyed by their headers, lines), for scripts")
    return parser


def _add(subparsers, command: str, **kwargs) -> argparse.ArgumentParser:
    """`worms <command>` (`vm reset`: under `vm`), with its lines in SECTIONS as its help (wrapped
    here when the parser keeps its own layout, for its epilog)."""
    text = _command_help(command)
    raw = kwargs.get("formatter_class") is argparse.RawDescriptionHelpFormatter
    description = textwrap.fill(text, HELP_WIDTH, break_on_hyphens=False) if raw else text
    return subparsers.add_parser(command.split()[-1], help=argparse.SUPPRESS, description=description, **kwargs)


RAW = {"formatter_class": argparse.RawDescriptionHelpFormatter}


def _noun(sub, noun: str):
    """`worms <noun>`: alone, its list; its verbs as subcommands (dest `action`), listed in its help."""
    verbs = [(usage, text) for section in SECTIONS for usage, text in section.commands
             if usage.split(" ")[0] == noun and usage != noun]  # fmt: skip
    parser = _add(sub, noun, usage=f"worms {noun} [-h] [<verb> ...]",
                  epilog="\n".join([output.heading(f"worms {noun} <verb>"), *_format_rows(verbs, COLUMN)]), **RAW)
    return parser, parser.add_subparsers(dest="action", metavar="<verb>", prog=f"worms {noun}", help=argparse.SUPPRESS)


def _targets(parser, nargs="+") -> None:
    parser.add_argument("targets", nargs=nargs, metavar="target", help="a VM (its name, an alias, <team>/<role>), a group, "
                        "a team (its devbox VMs) or `devboxes`")  # fmt: skip


def build_parser(vm_list: str = "") -> argparse.ArgumentParser:
    """`vm_list`: the VMs there are now, for `login`, `show`, `run` and `rsync --help` (main asks
    the host only when one of them is asked for)."""
    parser = argparse.ArgumentParser(
        prog="worms",
        usage="worms [-h] [--config FILE] <command> ...",
        description=f"{__doc__}\n{_commands_overview()}",
        epilog="Configuration: env or ~/.worms/config, no secrets (src/worms_cli/config.py).\n"
        "`worms <command> --help`: its details; docs/03-manage-py.md: the whole.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", metavar="FILE", help="path to a KEY=value config file (default ~/.worms/config)")
    sub = parser.add_subparsers(dest="command", required=True, prog="worms", metavar="<command>", help=argparse.SUPPRESS)
    vms = f"\n\n{vm_list}" if vm_list else ""

    # -- look
    _json(_add(sub, "status")).add_argument("--deep", action="store_true", help="also each controller stack's "
                                             "`tofu plan` (read-only): what OpenTofu would change (slow)")
    topics = "\n".join(_format_rows([*SHOW_TOPICS.items(), ("<list>", "a list's name (jobs, rounds, teams, keys, ...): as "
                                     "the noun alone (`worms jobs`)")], 12))  # fmt: skip
    p = _json(_add(sub, "show", epilog=topics + vms, **RAW))
    p.add_argument("what", nargs="+", metavar="what", help="a VM (its name, an alias or <team>/a|b|c|gw), a team (its "
                   "name or slug t<id>), `job <n>`, `table <name>`, or a topic (below)")  # fmt: skip
    p.add_argument("--schema", action="store_true", help="table: its columns, indexes and constraints instead")
    p.add_argument("--limit", type=int, default=None, help="rows to show (table: 20, audit: 200, jobs: 30)")
    p.add_argument("--hours", type=int, default=24, help="audit: how far back (default 24)")
    p.add_argument("--kind", default=None, help="audit: only events starting with this: login, password, team.joined, ...")
    p = _add(sub, "log", epilog=LOG_HELP, **RAW)
    p.add_argument("what", nargs="?", help="a service, a VM, a job's number, `latest`, a team, or `run [N] [<vm>]`")
    p.add_argument("more", nargs="*", metavar="N|vm", help="run: the N-th newest run log (default 1), and a VM: its own "
                   "Ansible log of that run")  # fmt: skip
    p.add_argument("-f", "--follow", action="store_true", help="keep showing new lines (Ctrl-C ends it)")
    p.add_argument("-n", "--lines", type=int, default=100, help="lines to show first (default 100)")
    p.add_argument("--since", help="journals only: from this time on (\"1 hour ago\", \"2026-11-19 18:00\")")
    p = _json(_add(sub, "check"))
    p.add_argument("what", nargs="*", metavar="team", help="teams (their names or slugs t<id>; default: every team with a "
                   "devbox), `packages` or `tokens`")  # fmt: skip
    p = _json(_add(sub, "trace"))
    p.add_argument("what", nargs="+", metavar="what", help="a team (its name or slug t<id>), or `upload <n>`")

    # -- reach
    p = _add(sub, "login", epilog=vm_list, **RAW)
    p.add_argument("vm", help="its name, an alias, or <team>/a|b|c|gw (alpha/gw, t2/a)")
    p = _add(sub, "run", epilog=RUN_HELP + vms, usage="worms run [-h] [--root] <target>... -- <command>", **RAW)
    p.add_argument("targets", nargs="+", metavar="target", help="host, a VM (its name, an alias, <team>/<role>), a group")
    p.add_argument("--root", action="store_true", help="run the command as root (sudo -n sh -c); the host's login is root")
    p = _add(sub, "rsync", epilog=RSYNC_HELP + vms, **RAW)
    p.add_argument("paths", nargs="+", metavar="path", help="sources, then the target: a local path or [<user>@]<vm>:[<path>]")
    p.add_argument("-n", "--dry-run", action="store_true", help="rsync's: list what would be sent, send nothing")
    p.add_argument("--delete", action="store_true", help="rsync's: delete what the source does not have on the target")
    p.add_argument("--preview", action="store_true", help="count what would be sent (files, sizes, bytes), send nothing")

    # -- deploy
    p = _yes(_add(sub, "apply", epilog=_components_overview(), **RAW))
    p.add_argument("components", nargs="*", metavar="component", help="components or groups (default: what changed)")
    p.add_argument("--all", action="store_true", help="every component, in build order, packages too")
    p.add_argument("--plan", action="store_true", help="what it would do and touch, changing nothing: the steps, what each "
                   "VM runs now, OpenTofu's own plan")  # fmt: skip
    p.add_argument("--packages", action="store_true", help="install packages too (opens the internet window)")
    p.add_argument("--full", action="store_true", help="every role on every VM, also those unchanged since their last "
                   "run there (skipped otherwise: their stamps, ansible/action_plugins/worms_role_gate.py)")  # fmt: skip
    p.add_argument("--during-round", action="store_true", help="even while a round is on (costs its attempt)")
    p.add_argument("--timeout", type=int, default=900, help="how long to wait for the agent's running host job (s)")
    p = _yes(_add(sub, "destroy"))
    p.add_argument("--all", action="store_true", help="also every VM outside the stacks, the firewall and the platform")
    p = _yes(_add(sub, "internet"))
    p.add_argument("state", nargs="?", choices=("on", "off"), metavar="on|off", help="on: open it (running devbox VMs are "
                   "shut down first); off: close it (they start again); neither: whether it is open")  # fmt: skip

    # -- machines
    p, verbs = _noun(sub, "vm")
    for verb in ("start", "stop", "restart"):
        _targets(v := _add(verbs, f"vm {verb}"), nargs="*" if verb == "start" else "+")
        if verb == "start":
            v.add_argument("--held", action="store_true", help="the devbox VMs a round holds shut down")
    for verb in ("reset", "rebuild"):
        v = _yes(_add(verbs, f"vm {verb}", epilog=_components_overview() if verb == "rebuild" else None, **RAW))
        v.add_argument("targets", nargs="+", metavar="target", help="teams (their names or slugs t<id>), `devboxes` (every "
                       "team's)" + (", or platform VMs and groups" if verb == "rebuild" else ", or `services`"))  # fmt: skip
        v.add_argument("--detach", action="store_true", help="queue the round agent's jobs and return (default: follow them, "
                       "their logs live)")  # fmt: skip
        if verb == "rebuild":
            v.add_argument("--packages", action="store_true", help="platform VMs: install packages too (the window opens anyway)")
            v.add_argument("--during-round", action="store_true", help=argparse.SUPPRESS)
    _yes(_add(verbs, "vm kill"))

    # -- operate
    p, verbs = _noun(sub, "jobs")
    p.add_argument("--limit", type=int, default=None, help="how many (default 30)")
    _add(verbs, "jobs pause").add_argument("--reason", default="", help="said to whoever looks (`worms show job <n>`)")
    _add(verbs, "jobs resume")
    v = _add(verbs, "jobs cancel")
    v.add_argument("numbers", nargs="*", type=int, metavar="n", help="the jobs' numbers (`worms jobs`)")
    v.add_argument("--team", help="every queued job of this team (its name or slug t<id>)")
    p, verbs = _noun(sub, "round")
    v = _yes(_add(verbs, "round start"))
    v.add_argument("round", help="its id (`worms round`) or its name")
    v.add_argument("--resume", action="store_true", help="every team that has not finished (a new attempt after a failed one)")
    v.add_argument("--team", action="append", default=[], metavar="TEAM", help="only this team (again: several)")
    v = _yes(_add(verbs, "round publish"))
    v.add_argument("round", help="its id (`worms round`) or its name")
    v.add_argument("--anyway", action="store_true", help="even though not every team finished without an error (asked)")
    _yes(_add(verbs, "round unpublish")).add_argument("round", help="its id (`worms round`) or its name")
    _yes(_add(verbs, "round teardown"))
    p, verbs = _noun(sub, "service")
    v = _add(verbs, "service restart")
    v.add_argument("services", nargs="+", metavar="name", help="agent, processor, api, topology, dashboard, scoring, "
                   "logging, monitor")  # fmt: skip
    v.add_argument("--timeout", type=int, default=900, help="agent: how long to wait for its running host job (s, default 900)")
    v.add_argument("--during-round", action="store_true", help="restart what a round needs even while one is on")
    for verb in ("stop", "start"):
        _add(verbs, f"service {verb}").add_argument("services", nargs="+", metavar="name",
                                                    help="orchestrator (api, processor, topology), agent, or any of `worms service`")  # fmt: skip

    # -- data and accounts
    p, verbs = _noun(sub, "db")
    _add(verbs, "db backup").add_argument("-o", "--output", metavar="FILE", help="the dump's file (default "
                                          "~/.worms/backups/<system>-<db>-<time>.dump)")  # fmt: skip
    _yes(_add(verbs, "db restore")).add_argument("file", metavar="dump", help="a dump `worms db backup` wrote (~/.worms/backups/)")
    _yes(_add(verbs, "db reset"))
    _add(verbs, "db dev-reset")  # no --yes: always asked
    p, verbs = _noun(sub, "team")
    _yes(_add(verbs, "team delete")).add_argument("name")
    p, verbs = _noun(sub, "key")
    v = _add(verbs, "key add")
    v.add_argument("team", help="its name or slug t<id>")
    v.add_argument("key", help="the public key (quoted), a .pub file, or - for stdin")
    v.add_argument("--name", default="", help="a name for it (default: the key's comment)")
    v = _yes(_add(verbs, "key remove"))
    v.add_argument("team", help="its name or slug t<id>")
    v.add_argument("key", help="the key's name, or its fingerprint (at least 8 characters of it)")
    _add(verbs, "key sync")
    p, verbs = _noun(sub, "user")
    v = _add(verbs, "user add")
    v.add_argument("email")
    v.add_argument("--first-name", default="")
    v.add_argument("--last-name", default="")
    v = _yes(_add(verbs, "user password"))
    v.add_argument("user", nargs="?", default="admin", help="its username (default: admin)")

    # -- set up
    _add(sub, "init")
    p, verbs = _noun(sub, "secret")
    _add(verbs, "secret set").add_argument("name", choices=["proxmox-token", "orchestrator-token", "topology-token", "ssh-key"])
    _add(verbs, "secret issue").add_argument("name", choices=["proxmox-token", "orchestrator-token", "topology-token"])
    _add(sub, "completion").add_argument("shell", choices=("bash",))
    _add(sub, "test").add_argument("--live", action="store_true", help="the end-to-end simulation on the deployed platform")
    p = _add(sub, "step")
    p.add_argument("name", choices=[*pipeline.STEPS, "debug"], metavar="name")
    p.add_argument("host", nargs="?", help="debug: an inventory host or group")
    return parser


# ---------------------------------------------------------------------------
# the commands' handlers: what each does with its words, on the implementations above
# ---------------------------------------------------------------------------


def _with(args, **values):
    """`args` with these values set (an implementation's own names)."""
    return argparse.Namespace(**{**vars(args), **values})


def _team_words(words) -> tuple[bool, list[str]]:
    """(every team, the teams named): `devboxes` stands for every team's devbox."""
    every = "devboxes" in words
    return every, [word for word in words if word != "devboxes"]


def _said(act) -> int:
    """`act()`, a BuildError or ValueError said: 1."""
    try:
        return act()
    except (build.BuildError, ValueError) as exc:
        output.error(str(exc))
        return 1


def verb_show(config, args) -> int:
    head, rest = args.what[0], args.what[1:]
    if head in LISTS and not rest:
        return HANDLERS[LISTS[head]](config, args)
    if head == "leaderboard":
        return _listing(config, _status_leaderboard)
    if head == "audit":
        return show_audit(config, args)
    if head == "gameplans":
        return show_gameplans(config, args)
    if head == "job":
        if len(rest) != 1 or not rest[0].isdigit():
            output.error("which job? `worms show job <n>` (`worms jobs` lists them)")
            return 2
        _with_db(config)
        from worms_cli import jobs as host_jobs
        from worms_cli.session import for_config

        return _said(lambda: host_jobs.show(for_config(config), int(rest[0])))
    if head == "table":
        if not rest:
            return list_tables(config)
        return _show_table(config, _with(args, table=rest[0], limit=args.limit or 20))
    if rest:
        output.error(f"`worms show {head}` takes nothing after it")
        return 2
    return _show_vm(config, head)


def verb_log(config, args) -> int:
    if args.what == "round":
        from worms_cli import rounds

        if len(args.more) > 2:
            output.error("`worms log round [<round>] [<team>|summary]`")
            return 2
        return _said(lambda: rounds.show_logs(*(args.more + [None, None])[:2], lines=args.lines, follow=args.follow))
    numbers = [word for word in args.more if word.isdigit()]
    words = [word for word in args.more if not word.isdigit()]
    if args.more and (args.what != "run" or len(numbers) > 1 or len(words) > 1):
        output.error("only `worms log run [N] [<vm>]` takes more than one word")
        return 2
    args = _with(args, nth=int(numbers[0]) if numbers else 1, vm=words[0] if words else None)
    what = "build" if args.what == "run" else args.what
    if what and not what.isdigit() and what not in ("build", "latest"):
        from worms_cli.services import SERVICES
        from worms_cli.vms import Catalogue

        if what not in SERVICES and not Catalogue(config).knows(what):
            _with_db(config)
            from worms_cli import logs, queries

            try:
                queries.team(what)
            except ValueError:
                pass  # no team either: cmd_log says what there is
            else:
                return logs.team(config, what, lines=min(args.lines, 40), since=args.since or "2 days ago")
    return cmd_log(config, _with(args, what=what))


def verb_check(config, args) -> int:
    if args.what == ["tokens"]:
        return cmd_secrets(config, _with(args, secrets_action="check"))
    if args.what == ["packages"]:
        from worms_cli import caches

        return _said(lambda: max(caches.check(config, kind) for kind in ("apt", "pypi")))
    _every, teams = _team_words(args.what)
    return cmd_validate(config, _with(args, teams=teams))


def verb_trace(config, args) -> int:
    _with_db(config)
    from worms_cli import trace
    from worms_cli.session import for_config

    return _said(lambda: trace.trace(for_config(config), args.what))


def _control(config, act) -> int:
    """`act(control)` with the database (worms_cli/control.py)."""
    _with_db(config)
    from worms_cli import control

    return _said(lambda: act(control))


def jobs_pause(config, args) -> int:
    from worms_cli import control
    from worms_cli.session import for_config

    return control.pause_jobs(for_config(config), args.reason)


def jobs_resume(config, args) -> int:
    from worms_cli import control
    from worms_cli.session import for_config

    return control.resume_jobs(for_config(config))


def jobs_cancel(config, args) -> int:
    if bool(args.numbers) == bool(args.team):
        output.error("which jobs? their numbers (`worms jobs cancel 2003 2004`), or --team <t>")
        return 2
    if args.team:
        from worms_cli import queries

        return _control(config, lambda control: control.cancel_jobs(team=queries.team(args.team)))
    def act(control):
        from worms_cli import services

        # The round agent stopped: a job marked running that no `worms round start` runs is left behind.
        agent_stopped = services.Units(config).stopped("agent")
        return control.cancel_jobs(numbers=args.numbers, agent_stopped=agent_stopped)

    return _control(config, act)


def round_start(config, args) -> int:
    """`worms round start <r> [--resume] [--team <t>]`: the round, run here (worms_cli/rounds.py)."""
    def act(control):
        from worms_cli import rounds

        if args.resume and args.team:
            raise build.BuildError("--resume runs every team that has not finished; --team the ones named: one of them")
        return rounds.start(config, args)

    return _control(config, act)


def round_publish(config, args) -> int:
    """`worms round publish|unpublish <r>` (worms_cli/rounds.py)."""
    def act(control):
        from worms_cli import rounds

        round_ = control.find_round(args.round)
        if args.action == "unpublish":
            return rounds.unpublish(round_, args.yes)
        return rounds.publish(round_, args.yes, args.anyway)

    return _control(config, act)


def round_teardown(config, args) -> int:
    """`worms round teardown`: the sandbox a session kept, destroyed (worms_cli/rounds.py)."""
    def act(control):
        from worms_cli import rounds

        return rounds.teardown(config, args.yes)

    return _control(config, act)


def service_power(config, args) -> int:
    """`worms service stop|start <name>...` (worms_cli/services.py, Units)."""
    from worms_cli import services

    try:
        services.expand(args.services)
    except KeyError as exc:
        output.error(f"no service {exc.args[0]!r}: {', '.join([*services.GROUPS, *services.SERVICES])}")
        return 2
    if "agent" in args.services:
        _with_db(config)
    units = services.Units(config)
    try:
        (units.stop if args.action == "stop" else units.start)(args.services)
    except build.BuildError as exc:
        output.error(str(exc))
        return 1
    return 0


def vm_reset(config, args) -> int:
    if args.targets == ["services"]:
        return cmd_reset(config, _with(args, what="services"))
    if "services" in args.targets:
        output.error("`worms vm reset services` resets the service VMs, all at once: name no team with it")
        return 2
    every, teams = _team_words(args.targets)
    return _reset_devboxes(config, _with(args, teams=teams, all_teams=every, fresh=False))


def vm_rebuild(config, args) -> int:
    from worms_cli import components

    every, teams = _team_words(args.targets)
    known = set(components.COMPONENTS) | set(components.GROUPS)
    if not every and all(word in known for word in teams):
        return _apply_components(config, _with(args, plan=False, during_round=getattr(args, "during_round", False)), teams,
                                 rebuild=True)  # fmt: skip
    if any(word in known for word in teams):
        output.error("rebuild either teams' devboxes or platform VMs, not both at once")
        return 2
    return _reset_devboxes(config, _with(args, teams=teams, all_teams=every, fresh=True))


def verb_init(config, args) -> int:
    """`worms init`: the vault (its generated secrets made when missing), then the WORMS key let
    in on the host; what is still to do is said (the Proxmox tokens)."""
    code = cmd_secrets(config, _with(args, secrets_action=None))
    return code or cmd_trust_host(config, args)


def db_dev_reset(config, args) -> int:
    """`worms db dev-reset` (worms_cli/dev_reset.py): a known start for the development system."""
    _with_db(config)
    from worms_cli import dev_reset

    return dev_reset.run(config, _console())


def db_restore(config, args) -> int:
    from worms_cli import database

    if not build.confirm(f"Replace the WHOLE database with {args.file}? Everything written since that dump is lost; the "
                         "dashboard, the orchestrator and the round agent stop meanwhile", args.yes):  # fmt: skip
        return 1
    try:
        return database.restore(config, args.file)
    except (build.BuildError, OSError) as exc:
        output.error(str(exc))
        return 1


HANDLERS = {
    "status": cmd_status, "show": verb_show, "log": verb_log, "check": verb_check, "trace": verb_trace,
    "login": cmd_login, "run": cmd_run, "rsync": cmd_rsync,
    "apply": cmd_apply, "destroy": cmd_destroy, "internet": cmd_internet,
    "vm": list_vms, "vm start": cmd_power, "vm stop": cmd_power, "vm restart": cmd_power, "vm reset": vm_reset,
    "vm rebuild": vm_rebuild, "vm kill": cmd_emergency_stop,
    "jobs": list_jobs, "jobs pause": jobs_pause, "jobs resume": jobs_resume, "jobs cancel": jobs_cancel,
    "round": list_rounds, "round start": round_start, "round publish": round_publish, "round unpublish": round_publish, "round teardown": round_teardown,
    "service": list_services, "service restart": cmd_restart, "service stop": service_power, "service start": service_power,
    "db": list_tables, "db backup": cmd_backup, "db restore": db_restore, "db dev-reset": db_dev_reset, "db reset": lambda config, args: cmd_reset(
        config, _with(args, what="database")),
    "team": list_teams, "team delete": cmd_delete_team,
    "key": list_keys, "key add": cmd_devbox_add_key, "key remove": cmd_devbox_remove_key, "key sync": cmd_devbox_sync_keys,
    "user": list_users, "user add": cmd_admin_create_staff,
    "user password": lambda config, args: cmd_admin_reset_password(config, _with(args, username=args.user)),
    "init": verb_init, "secret": lambda config, args: cmd_secrets(config, _with(args, secrets_action=None)),
    "secret set": lambda config, args: cmd_secrets(config, _with(args, secrets_action="set")),
    "secret issue": lambda config, args: cmd_secrets(config, _with(args, secrets_action="issue")),
    "completion": None,  # main() runs it before anything else
    "test": cmd_test, "step": cmd_step,
}  # fmt: skip


def _command(args) -> str:
    """The command as HANDLERS names it: `status`, `vm reset`."""
    return args.command + (f" {args.action}" if getattr(args, "action", None) else "")


def _split(argv: list[str]) -> tuple[list[str], list[str]]:
    """The command's own arguments, and those after `--` (for ansible-playbook or pytest)."""
    if "--" in argv:
        at = argv.index("--")
        return argv[:at], argv[at + 1 :]
    return argv, []


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    vm_list = ""
    if len(argv) >= 2 and argv[0] in ("login", "show", "rsync", "run") and ("-h" in argv or "--help" in argv):
        vm_list = _vm_list(Config.load())
    parser = build_parser(vm_list)
    own, passed = _split(argv)
    args = parser.parse_args(own)
    args.ansible_args = args.extra = passed
    # What the VM history says did it (worms_cli/history.py); the round agent sets its job's.
    os.environ.setdefault("WORMS_HISTORY_BY", "worms " + " ".join(own))
    config = Config.load(args.config)
    if args.command == "completion":  # read at every shell start: no notice, no host
        return cmd_completion(config, args)
    command = _command(args)
    if command in HOST_ONLY:
        from worms_cli import rounds

        refusal = rounds.local_on_the_host(config)
        if refusal:
            output.error(refusal)
            return 1
    tee = _run_log(config, args, command)
    lock = None
    code = 1
    captured = getattr(args, "json", False)
    if captured:  # --json: what it shows, gathered (worms_cli/output.py), printed as one document at the end
        import json

        output.capture_start()
        terminal, sys.stdout = sys.stdout, output.Sink()
    try:
        _internet_notice(config)
        lock = _lock(config, args, command, own)
        code = HANDLERS[command](config, args)
    except build.BuildError as exc:  # the lock refused
        output.error(str(exc))
        code = 1
    finally:
        if lock is not None:
            lock.release()
        if tee is not None:
            tee.stop()
        if captured:
            sys.stdout.flush()
            sys.stdout = terminal
            print(json.dumps(output.capture_result("worms " + " ".join(own), code), indent=2, ensure_ascii=False))
    if tee is not None and code:
        print(output.color(f"this run's whole output: {tee.path} (`worms log run`)", "dim"), file=sys.stderr)
    return code


# A round's own commands run on the Proxmox host itself, as there, whatever WORMS_SSH says (worms_cli/rounds.py).
HOST_ONLY = {"round start", "round teardown"}

# The commands that change something: their whole output goes to a run log too (worms_cli/runlog.py).
# Never one whose output is a secret (`user add`, `user password`: a one-time password) or a VM's
# (run, login).
# `round start` writes its own, in its session's folder (worms_cli/rounds.py).
LOGGED = {"apply", "destroy", "internet", "vm start", "vm stop", "vm restart", "vm reset", "vm rebuild", "vm kill",
          "jobs pause", "jobs resume", "jobs cancel", "round publish", "round unpublish", "round teardown",
          "service restart", "service stop", "service start", "db restore", "db reset", "db dev-reset", "team delete", "key add", "key remove", "key sync", "step"}

# The commands that change the platform: one at a time, by the host's lock (worms_cli/lock.py); a round's
# session holds it to its end. Never `vm kill`, `jobs ...`, `round publish`: they must work meanwhile.
LOCKED = {"apply", "destroy", "internet", "vm start", "vm stop", "vm restart", "vm reset", "vm rebuild", "service restart",
          "service stop", "service start", "round start", "round teardown",
          "db restore", "db reset", "db dev-reset", "key sync", "step"}


def _changes_nothing(args, command) -> bool:
    return getattr(args, "plan", False) or (command == "internet" and args.state is None)


def _lock(config, args, command, own):
    """The host's lock, when this command changes the platform (and is not the round agent's own run)."""
    from worms_cli import lock
    from worms_cli.session import HOST_WORKSPACE, for_config

    if command not in LOCKED or _changes_nothing(args, command) or Path(config.repo_root).resolve() == Path(HOST_WORKSPACE):
        return None
    return lock.Lock(for_config(config), " ".join(own)).acquire()


def _run_log(config, args, command):
    """Start this run's log, if it is a changing one; None otherwise, or when it cannot be (said once)."""
    from worms_cli import runlog
    from worms_cli.session import HOST_WORKSPACE

    if command not in LOGGED or _changes_nothing(args, command) or Path(config.repo_root).resolve() == Path(HOST_WORKSPACE):
        return None
    try:
        tee = runlog.Tee(runlog.name(command, args), config=config).start()
    except OSError as exc:
        output.warn(f"no run log for this run ({exc})")
        return None
    print(output.color(f"run log: {tee.path}", "dim"), file=sys.stderr, flush=True)
    return tee


if __name__ == "__main__":
    sys.exit(main())
