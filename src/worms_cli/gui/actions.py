"""worms-gui's actions: what the UI offers for what is marked (or under the cursor), each a `worms`
command (src/manage.py of this checkout, run by this interpreter), and the runner that runs them.
Every change goes through the CLI, with its own checks, its host lock and its run log; nothing here
changes the platform itself.

The UI's API, which stays as it is:

    entries = menu(tab, items, snap)
        tab     "vms" | "jobs" | "rounds" | "services" | "components" | "platform"
        items   the marked rows as the snapshot has them (dicts), else the one under the cursor
                ([] for "platform", which acts on the whole platform)
        ->      [Entry]: key (unique in its menu), label, prompt ("" or what to ask first: the command
                to run, a reason), unavailable ("" or why it does not apply to these items: shown,
                not chosen), scope ("items": it acts on them; "agent", "platform": on that, whatever
                is marked, so a prompt does not name the items)
    find(entries, key)             the entry of a key, or None: the tables' single keys (VMs `l` login,
                                   `!` run, `L` log; Jobs `c` cancel, `p` pause the agent, `P` resume)
    actions = entry.make(answer)   [Action], `answer` being what was typed for its prompt ("" without
                                   one); Unavailable (its text is for the status line) when it cannot
    Action                         title, argv, mode ("foreground" | "background"), confirm ("" or
                                   the question to ask first, naming the targets), destructive,
                                   targets [names], typed ("" or the word to type to confirm: "yes"
                                   for a destructive action on production, and for `vm kill`),
                                   note ("" or what else to say: what is left out, and why),
                                   session (a login: back at once when it lasted, no key to press),
                                   command ("worms vm stop dns", for the screen)

    runner = Runner(store, dry_run=False)      make it before Collectors.start(): its commands get the
                                               environment as it was, not the database's settings the
                                               database collector's Django puts into it
    runner.start(action) -> task id | None     a background action: a Task in the store (snap["tasks"]),
                                               its end in the status line; None with --dry-run
    runner.foreground(actions) -> exit code    on this terminal, one after the other, from the main
                                               thread: the UI calls curses.endwin() first and draws
                                               again after; it ends with "press a key to return" (not
                                               after a login session); SystemExit when the GUI got
                                               SIGHUP or SIGTERM meanwhile (HOLD: after the command)
    runner.cancel(task_id) -> bool             SIGINT to its process group (as Ctrl-C), SIGTERM 10 s later
    runner.pause(task_id), resume(task_id)     SIGSTOP, SIGCONT to its process group (state `paused`) -> bool;
                                               a paused task that holds the host's lock keeps it
    runner.busy() -> [task id]                 the tasks waiting, running or paused (ask before quitting)
    runner.shutdown(timeout=5)                 on quit: cancel what runs, wait for it a little

Modes. Foreground: what asks, takes long or is the operator's own terminal (login, run, log -f,
show, apply, vm reset, vm rebuild, vm kill, internet on|off); the CLI asks its own questions there,
so it gets no --yes (apply shows its plan first). Background: what is quick and asks nothing (vm
start|stop|restart, jobs cancel|pause|resume, round pause|resume|start|cancel, service restart),
with --yes where the command takes it, as the GUI has asked. The commands that take the host's lock
(manage.py's LOCKED) run one at a time: a second one waits ("waiting") instead of being refused.
Several targets: one command when the CLI takes several (`vm stop a b c`), else one each.
"""

from __future__ import annotations

import functools
import importlib.util
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

MANAGE = Path(__file__).resolve().parents[2] / "manage.py"
BASE_ENV = dict(os.environ)  # at import, before a collector's Django setup adds its settings
CANCEL_GRACE = 10.0  # s after SIGINT before SIGTERM
SESSION_LASTED = 5.0  # s: a login that ended sooner (with an error) waits for a key, so what it said can be read
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


class Hold:
    """SIGHUP and SIGTERM while a command runs on the terminal: app.py's handler only notes them here, and
    the GUI ends once the command has (Runner.foreground raises SystemExit then). Raised at once, they
    would SIGKILL the command (subprocess's wait does on an exception): a `worms apply` cut short leaves
    its tofu and ansible running without the host's lock, and its ssh-agent behind."""

    def __init__(self):
        self.active = False
        self.signum = None


HOLD = Hold()


class Unavailable(Exception):
    """An entry that does not apply (to these items, with this answer): its text says why."""


@dataclass
class Action:
    title: str
    argv: list[str]
    mode: str  # "foreground" | "background"
    confirm: str = ""  # the question; "" = it changes nothing, nothing to ask
    destructive: bool = False
    targets: list[str] = field(default_factory=list)
    typed: str = ""  # the word to type to confirm; "" = y/n
    note: str = ""
    session: bool = False  # a login: back without a key once it has lasted (the shell's last status says nothing)

    @property
    def command(self) -> str:
        return "worms " + shlex.join(self.argv[2:])


@dataclass
class Entry:
    key: str
    label: str
    make: Callable[[str], list[Action]]
    prompt: str = ""
    unavailable: str = ""
    scope: str = "items"  # "agent" | "platform": it acts on that, not on the items


def worms(*words) -> list[str]:
    return [sys.executable, str(MANAGE), *[str(word) for word in words]]


def find(entries: list[Entry], key: str) -> Entry | None:
    return next((entry for entry in entries if entry.key == key), None)


def production(snap: dict) -> bool:
    """Whether to ask for the typed word: production, or a system not known yet (as if it were)."""
    return (snap.get("host") or {}).get("system") != "development"


def menu(tab: str, items: list[dict], snap: dict) -> list[Entry]:
    builders = {"vms": _vm_menu, "jobs": _job_menu, "rounds": _round_menu, "services": _service_menu,
                "components": _component_menu, "platform": _platform_menu}  # fmt: skip
    if tab not in builders:
        raise KeyError(tab)
    return builders[tab](list(items), snap)


def _entry(key, label, make, unavailable="", prompt="", scope="items") -> Entry:
    """An entry whose `make` is not called when it does not apply."""
    if unavailable:
        def refused(_answer=""):
            raise Unavailable(unavailable)

        return Entry(key, label, refused, prompt, unavailable, scope)
    return Entry(key, label, make, prompt, "", scope)


def _names(items) -> str:
    names = [str(item) for item in items]
    return ", ".join(names) if len(names) <= 6 else f"{', '.join(names[:6])} and {len(names) - 6} more"


def _typed(destructive: bool, snap: dict) -> str:
    return "yes" if destructive and production(snap) else ""


# -- VMs -------------------------------------------------------------------------------------------------------

TEAM_POOLS = ("sandbox", "devbox")
PLATFORM_POOLS = ("persistent", "ephemeral", "private_internet")


def _rooted(vm: dict) -> bool:
    """A team's VM or a gateway (the shared SSH gateway is in the persistent pool), or one in no
    platform pool yet: assumed rooted, as state.trusted says. A record without `trusted` is too."""
    return vm.get("pool") in TEAM_POOLS or not vm.get("trusted", False)


def _round_vm(vm: dict) -> bool:
    """A round's sandbox VM: its pool, or no pool of the platform's or the devboxes' (a sandbox VM is
    created and started before it joins its pool). The round agent's to start and stop."""
    return vm.get("pool") not in (*PLATFORM_POOLS, "devbox")


def _vm_menu(vms: list[dict], snap: dict) -> list[Entry]:
    from worms_cli import components

    none = "" if vms else "no VM marked or under the cursor"
    running = [vm for vm in vms if vm.get("status") in ("running", "paused")]
    platform = [vm for vm in running if not _rooted(vm)]
    built = {name for name, c in components.COMPONENTS.items() if c.kind == "vm"}
    services = {name for name, c in components.COMPONENTS.items() if c.service}
    devbox = any(vm.get("pool") == "devbox" and vm.get("team_slug") for vm in vms)
    on = f"on {_names(vm['name'] for vm in platform)}"
    rooted = [vm["name"] for vm in running if _rooted(vm)]
    # Nothing is run on a team's VM or a gateway from here: assumed rooted, what runs there is the team's to see.
    idle = none or ("" if platform else "only on a running platform VM: a team VM or a gateway is assumed rooted"
                    if rooted else "none of them runs")
    left = f" (not on {_names(rooted)}: a team VM or a gateway)" if rooted and platform else ""
    return [
        _entry("s", "start", lambda _: _power("start", vms, snap), none or _power_why("start", vms)),
        _entry("t", "stop (a clean shutdown, forced after 120 s)", lambda _: _power("stop", vms, snap),
               none or _power_why("stop", vms)),
        _entry("R", "restart (stop, then start)", lambda _: _power("restart", vms, snap), none or _power_why("restart", vms)),
        _entry("e", "reset: a team's devbox to its snapshot, the service VMs to `clean`", lambda _: _reset(vms, snap, services),
               none or ("" if devbox or any(vm["name"] in services for vm in vms) else "neither a team's devbox VM nor a service VM")),
        _entry("b", "rebuild: destroyed and built again (a team's devbox, a platform VM)", lambda _: _rebuild(vms, snap, built),
               none or ("" if devbox or any(vm["name"] in built for vm in vms) else "neither a team's devbox VM nor a component")),
        _entry("l", "log in (and come back here)", lambda _: _login(vms[0]), none or _login_why(vms)),
        _entry("!", "run a command (platform VMs)", lambda answer: _run(platform, answer, False, rooted), idle,
               prompt=f"command to run {on}{left}"),
        _entry("#", "run a command as root (platform VMs)", lambda answer: _run(platform, answer, True, rooted), idle,
               prompt=f"command to run as root {on}{left}"),
        _entry("L", "its log, followed (Ctrl-C ends it)", lambda _: _vm_logs(platform),
               none or ("" if platform else "only a running platform VM's: a team VM's or a gateway's journal is the team's")),
        _entry("d", "show everything about it (`worms show`)", lambda _: _shows(vms),
               none or ("" if any(not (_rooted(vm) and vm in running) for vm in vms) else
                        "`worms show` logs in to a running VM (ssh true): not from here for a team VM or a gateway")),
        _entry("K", "kill: hard-stop EVERY VM on the host (`worms vm kill`)", lambda _: _kill(), scope="platform"),
    ]  # fmt: skip


def _power_why(verb: str, vms: list[dict]) -> str:
    usable = [vm for vm in vms if not _round_vm(vm)]
    if not usable:
        return "a round's sandbox VM (or one in no pool yet): the round agent's to start and stop (`worms vm kill` stops every VM)"
    if verb == "start" and all(vm.get("status") == "running" for vm in usable):
        return "running already"
    if verb == "stop" and all(vm.get("status") == "stopped" for vm in usable):
        return "stopped already"
    return ""


def _power(verb: str, vms: list[dict], snap: dict) -> list[Action]:
    usable = [vm["name"] for vm in vms if not _round_vm(vm)]
    left = [vm["name"] for vm in vms if _round_vm(vm)]
    question = {"start": "Start", "stop": "Shut down (forced after 120 s)", "restart": "Restart (shut down, then start)"}[verb]
    team = [vm["name"] for vm in vms if vm.get("pool") == "devbox"]
    extra = f" {len(team)} of them a team's: the team loses what runs there." if team and verb != "start" else ""
    destructive = verb != "start"  # a restart stops it too
    note = f"not: {_names(left)} (a round's sandbox VM, or in no pool yet: the round agent's)" if left else ""
    return [Action(f"vm {verb} {_names(usable)}", worms("vm", verb, *usable), "background", f"{question} {_names(usable)}?{extra}",
                   destructive, usable, _typed(destructive, snap), note)]  # fmt: skip


def _teams_of(vms: list[dict], snap: dict) -> list[tuple[str, str]]:
    """(slug, label) of the teams whose devbox VMs these are. `worms vm reset|rebuild` is given the slug
    (t2), never the team's name: teams choose their names, and in the CLI `devboxes`, `services`, a
    component's or a group's name means something else (a team named `database` would rebuild the
    platform's database VM). Unavailable when a team's name is another's slug: the CLI reads names first."""
    from worms_cli import components

    taken = {"devboxes", "services", *components.COMPONENTS, *components.GROUPS}
    teams = snap.get("teams") or {}
    out = {}
    for vm in vms:
        slug = vm.get("team_slug") or ""
        if vm.get("pool") != "devbox" or not slug or slug in out:
            continue
        if not teams:
            raise Unavailable("the team map is not known yet (the database): a team is named by it")
        if slug in taken or not re.fullmatch(r"t\d+", slug):
            raise Unavailable(f"{slug}: not a team's slug the CLI takes as one")
        clash = [t.get("label") or s for s, t in teams.items() if s != slug and (t.get("name") or "").casefold() == slug.casefold()]
        if clash:
            raise Unavailable(f"the team {clash[0]} is named {slug!r}: `worms vm` would take it for that team")
        out[slug] = vm.get("team") or slug
    return list(out.items())


def _reset(vms: list[dict], snap: dict, services: set) -> list[Action]:
    teams = _teams_of(vms, snap)
    handles, labels = [slug for slug, _ in teams], [label for _, label in teams]
    left = [vm["name"] for vm in vms if vm.get("pool") != "devbox" and vm["name"] not in services]
    note = f"not: {_names(left)} (only a team's devbox and the service VMs are reset)" if left else ""
    actions = []
    if handles:  # the CLI resets teams or the service VMs, not both at once
        actions.append(Action(f"vm reset {_names(handles)}", worms("vm", "reset", *handles), "foreground",
                              f"Reset the devbox of {_names(labels)}: its four VMs back to their snapshot, its dev score "
                              "starts over?", True, labels, _typed(True, snap), note))  # fmt: skip
    if any(vm["name"] in services for vm in vms):
        actions.append(Action("vm reset services", worms("vm", "reset", "services"), "foreground",
                              "Return EVERY service VM (not only those marked) to its `clean` snapshot? What they hold "
                              "since is lost", True, ["services"], _typed(True, snap), note))  # fmt: skip
    return actions


def _rebuild(vms: list[dict], snap: dict, built: set) -> list[Action]:
    teams = _teams_of(vms, snap)
    handles, labels = [slug for slug, _ in teams], [label for _, label in teams]
    platform = [vm["name"] for vm in vms if vm["name"] in built and vm.get("trusted")]
    left = [vm["name"] for vm in vms if vm["name"] not in platform and vm.get("pool") != "devbox"]
    note = f"not: {_names(left)} (neither a team's devbox nor a component)" if left else ""
    actions = []
    if handles:  # the CLI rebuilds teams or platform VMs, not both at once
        actions.append(Action(f"vm rebuild {_names(handles)}", worms("vm", "rebuild", *handles), "foreground",
                              f"Rebuild the devbox of {_names(labels)}: its VMs DESTROYED and built again from nothing, "
                              "what is on them is lost?", True, labels, _typed(True, snap), note))  # fmt: skip
    if platform:
        actions.append(Action(f"vm rebuild {_names(platform)}", worms("vm", "rebuild", *platform), "foreground",
                              f"Rebuild {_names(platform)}: DESTROYED and built again (`worms vm rebuild` shows its plan "
                              "and asks)?", True, platform, _typed(True, snap), note))  # fmt: skip
    return actions


def _login_why(vms: list[dict]) -> str:
    if len(vms) != 1:
        return "one VM at a time"
    return "" if vms[0].get("status") == "running" else f"{vms[0]['name']} is {vms[0].get('status')}: start it first"


def _login(vm: dict) -> list[Action]:
    confirm = f"{vm['name']} is a team VM or a gateway, assumed rooted: type nothing secret there. Log in?" if _rooted(vm) else ""
    return [Action(f"login {vm['name']}", worms("login", vm["name"]), "foreground", confirm, False, [vm["name"]], session=True)]


def _run(vms: list[dict], answer: str, root: bool, left=()) -> list[Action]:
    """On the platform VMs only (`vms`); `left`: the team VMs and gateways that were marked too."""
    command = answer.strip()
    if not command:
        raise Unavailable("no command: nothing run")
    names = [vm["name"] for vm in vms if not _rooted(vm)]
    if not names:
        raise Unavailable("only on a running platform VM: a team VM or a gateway is assumed rooted")
    argv = worms("run", *(["--root"] if root else []), *names, "--", command)  # one word after --: the VM's shell reads it
    note = f"not on {_names(left)}: a team VM or a gateway, assumed rooted (`l` logs in, with a warning)" if left else ""
    return [Action(f"run {_names(names)}: {command}", argv, "foreground", f"Run `{command}`{' as root' if root else ''} on "
                   f"{_names(names)}?", False, names, note=note)]  # fmt: skip


def _vm_logs(vms: list[dict]) -> list[Action]:
    return [Action(f"log {vm['name']}", worms("log", vm["name"], "-f"), "foreground", "", False, [vm["name"]]) for vm in vms]


def _shows(vms: list[dict]) -> list[Action]:
    """`worms show` asks a running VM over SSH whether it answers (`ssh true`): not a rooted one from here."""
    shown = [vm for vm in vms if not (_rooted(vm) and vm.get("status") in ("running", "paused"))]
    left = [vm["name"] for vm in vms if vm not in shown]
    note = f"not {_names(left)}: it would log in to a team VM or a gateway (ssh true)" if left else ""
    return [Action(f"show {vm['name']}", worms("show", vm["name"]), "foreground", "", False, [vm["name"]], note=note)
            for vm in shown]  # fmt: skip


def _kill() -> list[Action]:
    return [Action("vm kill", worms("vm", "kill"), "foreground", "Hard-stop EVERY VM on the host, the platform's own too "
                   "(nothing is destroyed; `worms vm start` them again one by one)?", True, ["every VM"], "yes")]  # fmt: skip


# -- jobs --------------------------------------------------------------------------------------------------------


def _job_menu(jobs: list[dict], snap: dict) -> list[Entry]:
    queued = [job for job in jobs if job.get("state") == "queued"]
    none = "" if jobs else "no job marked or under the cursor"
    paused = (snap.get("host") or {}).get("agent_paused", "")
    host_known = bool(((snap.get("sources") or {}).get("host") or {}).get("updated_at"))
    return [
        _entry("c", "cancel (queued ones: they fail, never run)", lambda _: _cancel(queued, jobs, snap),
               none or ("" if queued else "only a queued job is cancelled: a running one cannot be stopped from here "
                        "(`worms jobs cancel` takes queued ones)")),
        _entry("p", "pause the AGENT, not a job: the job under way finishes, no other starts", lambda answer: _pause(answer),
               prompt="why the agent is paused (shown to whoever looks; may be empty). Not the marked jobs: the round agent",
               scope="agent"),
        _entry("P", "resume the agent: it takes jobs again", lambda _: _resume(paused),
               "the agent is not paused" if host_known and not paused else "", scope="agent"),
        _entry("d", "show the job (its fields, why it waits, its log's end)", lambda _: _job_shows(jobs), none),
        _entry("L", "its log (followed while it runs)", lambda _: _job_logs(jobs), none),
    ]  # fmt: skip


def _job_name(job: dict) -> str:
    return f"#{job['id']} {job.get('kind', '')}" + (f" ({job['team']})" if job.get("team") else "")


def _cancel(queued: list[dict], jobs: list[dict], snap: dict) -> list[Action]:
    left = [f"#{job['id']} ({job.get('state')})" for job in jobs if job not in queued]
    numbers = [str(job["id"]) for job in queued]
    note = f"not: {_names(left)}: only a queued job is cancelled" if left else ""
    return [Action(f"jobs cancel {_names(numbers)}", worms("jobs", "cancel", *numbers), "background",
                   f"Cancel {_names(_job_name(job) for job in queued)}? They fail (\"cancelled by the operator\") and never run",
                   True, [f"#{n}" for n in numbers], _typed(True, snap), note)]  # fmt: skip


def _pause(reason: str) -> list[Action]:
    reason = " ".join(reason.split())
    # --reason=...: one word, whatever it starts with (a reason "-x" would be an option of its own)
    return [Action("jobs pause", worms("jobs", "pause", *([f"--reason={reason}"] if reason else [])), "background",
                   "Pause the round agent (not a job)? The job under way finishes; no other starts until `resume`", False,
                   ["the round agent"])]  # fmt: skip


def _resume(paused: str) -> list[Action]:
    question = "Let the round agent take jobs again?" + (f" ({paused})" if paused else "")
    return [Action("jobs resume", worms("jobs", "resume"), "background", question, False, ["the round agent"])]


def _job_shows(jobs: list[dict]) -> list[Action]:
    return [Action(f"show job {job['id']}", worms("show", "job", job["id"]), "foreground", "", False, [f"#{job['id']}"])
            for job in jobs]  # fmt: skip


def _job_logs(jobs: list[dict]) -> list[Action]:
    follow = ("queued", "running")  # -f waits for a queued one's log
    return [Action(f"log {job['id']}", worms("log", job["id"], *(["-f"] if job.get("state") in follow else [])), "foreground", "",
                   False, [f"#{job['id']}"]) for job in jobs]  # fmt: skip


# -- rounds ------------------------------------------------------------------------------------------------------

ROUND_QUESTIONS = {
    "pause": "Hold round {}: none of its attempts starts until `resume`?",
    "resume": "Let round {} go on (its devboxes held again if it wants them off)?",
    "start": "Start round {} now (its start moved to now, its runs planned with the uploads made until now)?",
    "cancel": "Skip every attempt of round {} still to come (one under way ends as usual)?",
}


def _round_menu(rounds: list[dict], snap: dict) -> list[Entry]:
    none = "" if rounds else "no round marked or under the cursor"
    now = snap.get("now") or time.time()
    held = [r for r in rounds if r.get("on_hold")]
    going = [r for r in rounds if not r.get("on_hold")]
    to_come = [r for r in rounds if not r.get("start") or r["start"] > now]
    return [
        _entry("p", "pause: no attempt of it starts (one under way ends as usual)", lambda _: _round("pause", going, snap),
               none or ("" if going else "on hold already")),
        _entry("P", "resume", lambda _: _round("resume", held, snap), none or ("" if held else "not on hold")),
        _entry("s", "start now (a round still to come begins now)", lambda _: _round("start", to_come, snap),
               none or ("" if to_come else "it has started already")),
        _entry("c", "cancel: its attempts still to come are skipped", lambda _: _round("cancel", rounds, snap), none),
    ]  # fmt: skip


def _round(verb: str, rounds: list[dict], snap: dict) -> list[Action]:
    """One command each: `worms round <verb>` takes one round."""
    destructive = verb == "cancel"
    actions = []
    for r in rounds:
        label = f"{r['id']} {r.get('name', '')}".strip()
        argv = worms("round", verb, r["id"], *(["--yes"] if verb == "cancel" else []))
        question = ROUND_QUESTIONS[verb].format(repr(r.get("name") or r["id"]))
        actions.append(Action(f"round {verb} {label}", argv, "background", question, destructive, [label], _typed(destructive, snap)))
    return actions


# -- services ------------------------------------------------------------------------------------------------------


def _service_menu(services: list[dict], snap: dict) -> list[Entry]:
    none = "" if services else "no service marked or under the cursor"
    restartable = [s for s in services if s.get("restartable")]
    why_not = "; ".join(s["why_not"] for s in services if s.get("why_not")) or "not restarted from here"
    why = none or ("" if restartable else why_not)
    return [
        _entry("R", "restart (in a safe order: the agent once no host job runs)",
               lambda _: _restart(restartable, services, snap, False), why),
        _entry("D", "restart even during a round (--during-round: it can cost the running team its attempt)",
               lambda _: _restart(restartable, services, snap, True), why),
        _entry("L", "its log, followed (Ctrl-C ends it)", lambda _: [
            Action(f"log {s['name']}", worms("log", s["name"], "-f"), "foreground", "", False, [s["name"]]) for s in services], none),
    ]  # fmt: skip


def _restart(restartable: list[dict], services: list[dict], snap: dict, during_round: bool) -> list[Action]:
    names = [s["name"] for s in restartable]
    left = [f"{s['name']} ({s.get('why_not') or 'not restartable'})" for s in services if s not in restartable]
    argv = worms("service", "restart", *names, *(["--during-round"] if during_round else []))
    question = f"Restart {_names(names)}" + (" even while a round is on" if during_round else "") + "?"
    return [Action(f"service restart {_names(names)}", argv, "background", question, during_round, names, _typed(during_round, snap),
                   f"not: {'; '.join(left)}" if left else "")]  # fmt: skip


# -- components ------------------------------------------------------------------------------------------------------


def _component_menu(items: list[dict], snap: dict) -> list[Entry]:
    names = [c["name"] for c in items]
    none = "" if names else "no component marked or under the cursor"
    return [
        _entry("a", "apply the marked ones", lambda _: _apply(names, snap), none),
        _entry("n", "plan the marked ones (--plan: changes nothing)", lambda _: _plan(names), none),
        _entry("A", "apply what changed", lambda _: _apply([], snap)),
        _entry("N", "plan what changed (--plan)", lambda _: _plan([])),
    ]


def _apply(names: list[str], snap: dict) -> list[Action]:
    if names:
        question = (f"Apply {_names(names)}: built if missing, configured again otherwise? `worms apply` shows its plan and asks "
                    "before it changes anything")  # fmt: skip
    else:
        question = "Apply what changed since what runs? `worms apply` shows what changed and its plan, and asks first"
    return [Action(f"apply {_names(names)}".strip(), worms("apply", *names), "foreground", question, True, names or ["what changed"],
                   _typed(True, snap))]  # fmt: skip


def _plan(names: list[str]) -> list[Action]:
    return [Action(f"apply --plan {_names(names)}".strip(), worms("apply", *names, "--plan"), "foreground", "", False,
                   names or ["what changed"])]  # fmt: skip


# -- the platform -------------------------------------------------------------------------------------------------------


def _platform_menu(_items: list[dict], snap: dict) -> list[Entry]:
    host = snap.get("host") or {}
    held = host.get("held_devboxes") or []
    entries = [
        _entry("o", "internet on: open the window (running devbox VMs are shut down first)", lambda _: _internet("on", snap),
               "open already" if host.get("internet") == "allowed" else ""),
        _entry("f", "internet off: close it (DNS and the caches offline again, the devboxes started again)",
               lambda _: _internet("off", snap)),
        _entry("h", "start the devbox VMs a round holds shut down (`vm start --held`)", lambda _: _held(held),
               "" if held else "no devbox VM is held"),
        _entry("K", "kill: hard-stop EVERY VM on the host (`worms vm kill`)", lambda _: _kill()),
        _entry("S", "`worms status` on the terminal", lambda _: [Action("status", worms("status"), "foreground", "", False, [])]),
    ]  # fmt: skip
    return [Entry(e.key, e.label, e.make, e.prompt, e.unavailable, "platform") for e in entries]


def _internet(state: str, snap: dict) -> list[Action]:
    if state == "on":
        question = ("Open the internet window? No team VM may run while it is open: the running devbox VMs are shut down first "
                    "(`worms internet off` starts them again)")  # fmt: skip
    else:
        question = "Close the internet window (DNS and the caches back to offline mode: a few minutes)?"
    destructive = state == "on"
    return [Action(f"internet {state}", worms("internet", state), "foreground", question, destructive, ["internet"],
                   _typed(destructive, snap))]  # fmt: skip


def _held(held: list[str]) -> list[Action]:
    return [Action("vm start --held", worms("vm", "start", "--held"), "background", f"Start the {len(held)} held devbox VM(s) by hand "
                   f"({_names(held)})? The CLI refuses while the round is on", False, held)]  # fmt: skip


# -- the runner -----------------------------------------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _cli():
    """manage.py itself, for its tables (LOCKED, NOUNS): what takes the host's lock is its word."""
    spec = importlib.util.spec_from_file_location("worms_manage", MANAGE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def locked(argv: list[str]) -> bool:
    """Whether the CLI takes the host's lock for this command (manage.py: LOCKED, _changes_nothing)."""
    words = [word for word in argv[2:] if not word.startswith("-")]
    if not words:
        return False
    cli = _cli()
    command = " ".join(words[:2]) if words[0] in cli.NOUNS and len(words) > 1 and words[1].isalpha() else words[0]
    if "--plan" in argv or (command == "internet" and len(words) == 1):
        return False
    return command in cli.LOCKED


def clean(text: str) -> str:
    """A line of a command's output for the screen: no colours, no control characters; of a line
    that rewrites itself (\\r), what it says last."""
    text = ANSI.sub("", text.rstrip("\n")).rsplit("\r", 1)[-1]
    return "".join(ch if ch.isprintable() else (" " if ch == "\t" else "?") for ch in text)


def ended(code) -> str:
    """How a command ended, in words: `exit 1`, `Ctrl-C`, `signal 15`."""
    if code is None:
        return "not started"
    if code in (-signal.SIGINT, 130):
        return "Ctrl-C"
    return f"signal {-code}" if code < 0 else f"exit {code}"


def _nothing(_signum, _frame):
    """SIGINT while a command runs on the terminal: the command's (it is in the same process group),
    not the GUI's. A handler, not SIG_IGN, which the command would inherit."""


class Runner:
    def __init__(self, store, dry_run: bool = False, env: dict | None = None):
        self.store, self.dry_run = store, dry_run
        self.env = dict(BASE_ENV if env is None else env)
        self.env["PYTHONUNBUFFERED"] = "1"  # its lines as they come
        self.host_lock = threading.Lock()  # one command at a time that takes the host's lock
        self.procs: dict[int, subprocess.Popen] = {}
        self.cancelled: set[int] = set()
        self._lock = threading.Lock()

    def start(self, action: Action) -> int | None:
        if action.mode != "background":
            raise ValueError(f"{action.command} runs on the terminal: Runner.foreground")
        if self.dry_run:
            self.store.say(f"dry run, not run: $ {action.command}", "warn")
            return None
        wait = locked(action.argv)  # here: manage.py is loaded once, by this thread
        task = self.store.add_task(action.title, action.argv, action.command, action.targets, locks=wait)
        threading.Thread(target=self._run, args=(task.id, action, wait), name=f"worms-gui-task-{task.id}", daemon=True).start()
        self.store.say(f"started #{task.id}: $ {action.command}")
        return task.id

    def _run(self, task_id: int, action: Action, wait: bool) -> None:
        if wait and not self.host_lock.acquire(blocking=False):
            self.store.task_line(task_id, "(waiting: another task of this GUI holds the host's lock)")
            self.host_lock.acquire()
        code = None
        try:
            if task_id in self.cancelled:
                return
            try:
                proc = subprocess.Popen(action.argv, env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, start_new_session=True)  # fmt: skip
            except OSError as exc:
                self.store.task_line(task_id, f"could not start it: {exc}")
                code = 127
                return
            with self._lock:
                self.procs[task_id] = proc
            if task_id in self.cancelled:  # cancelled as it started
                self._signal(proc, signal.SIGINT)
            self.store.task_state(task_id, "running", started=time.time(), pid=proc.pid)
            for raw in proc.stdout:
                self.store.task_line(task_id, clean(raw.decode(errors="replace")))
            code = proc.wait()
        finally:
            if wait:
                self.host_lock.release()
            with self._lock:
                self.procs.pop(task_id, None)
            self._ended(task_id, action, code)

    def _ended(self, task_id: int, action: Action, code) -> None:
        if task_id in self.cancelled and code != 0:  # one that ended well as the Ctrl-C came is done, not cancelled
            state, level, said = "cancelled", "warn", "cancelled"
        elif code == 0:
            state, level, said = "done", "ok", "done"
        else:
            state, level, said = "failed", "error", f"FAILED ({ended(code)})"
        self.store.task_state(task_id, state, ended=time.time(), rc=code)
        last = (self.store.task(task_id) or {}).get("lines") or [""]
        why = f" — {last[-1][:120]}" if state == "failed" and last[-1] else ""  # the CLI's error is its last line
        self.store.say(f"#{task_id} {said}: {action.command}{why}", level)

    def foreground(self, actions: list[Action]) -> int:
        if self.dry_run:
            for action in actions:
                self.store.say(f"dry run, not run: $ {action.command}", "warn")
            return 0
        return on_terminal(actions, lambda action: _attached(action.argv, self.env), self.store.log)

    def cancel(self, task_id: int) -> bool:
        """Ctrl-C to it (its process group: the CLI and what it started), SIGTERM CANCEL_GRACE s later.
        A task waiting for the lock does not start."""
        task = self.store.task(task_id)
        if task is None or task["state"] not in ("waiting", "running", "paused") or task["ended"] is not None:
            return False
        with self._lock:
            self.cancelled.add(task_id)
            proc = self.procs.get(task_id)
        if proc is None:
            self.store.say(f"#{task_id} cancelled before it started", "warn")
            return True
        if task["state"] == "paused":
            self.resume(task_id)  # a stopped process would not act on its Ctrl-C
        self._signal(proc, signal.SIGINT)
        timer = threading.Timer(CANCEL_GRACE, lambda: proc.poll() is None and self._signal(proc, signal.SIGTERM))
        timer.daemon = True
        timer.start()
        self.store.say(f"#{task_id}: Ctrl-C sent (SIGTERM in {CANCEL_GRACE:.0f} s if it goes on)", "warn")
        return True

    def pause(self, task_id: int) -> bool:
        """SIGSTOP to its process group: it stands still until resume (its SSH connections may time out
        meanwhile; one that holds the host's lock keeps it). Not a task that waits for the lock: it has no
        process yet. The check, the signal and the state together: a task that ends meanwhile stays ended."""
        with self._lock:
            proc = self._running(task_id, "running")
            if proc is None:
                return False
            self._signal(proc, signal.SIGSTOP)
            if not self.store.task_state(task_id, "paused"):
                self._signal(proc, signal.SIGCONT)
                return False
        self.store.say(f"#{task_id} paused (SIGSTOP): `P` in the Tasks tab resumes it", "warn")
        return True

    def resume(self, task_id: int) -> bool:
        with self._lock:
            proc = self._running(task_id, "paused")
            if proc is None:
                return False
            self._signal(proc, signal.SIGCONT)
            self.store.task_state(task_id, "running")
        self.store.say(f"#{task_id} resumed (SIGCONT)")
        return True

    def _running(self, task_id: int, state: str):
        """The task's process while it is in `state` and has not ended, else None (under self._lock)."""
        task = self.store.task(task_id)
        proc = self.procs.get(task_id)
        if task is None or task["state"] != state or task["ended"] is not None or proc is None or proc.poll() is not None:
            return None
        return proc

    def busy(self) -> list[int]:
        return self.store.busy()

    def shutdown(self, timeout: float = 5.0) -> None:
        for task_id in self.busy():
            self.cancel(task_id)
        deadline = time.monotonic() + timeout
        with self._lock:
            procs = list(self.procs.values())
        for proc in procs:
            try:
                proc.wait(max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                self._signal(proc, signal.SIGTERM)

    @staticmethod
    def _signal(proc, signum) -> None:
        try:
            os.killpg(proc.pid, signum)
        except OSError:
            pass


def on_terminal(actions: list, run: Callable, log: Callable) -> int:
    """`actions` on this terminal one after the other (`run(action)` -> exit code), then "press a key to
    return". Not after a login session that lasted (SESSION_LASTED): straight back. SIGHUP or SIGTERM
    meanwhile (HOLD): no further command, no key; SystemExit once the one under way has ended."""
    worst, started = 0, time.monotonic()
    HOLD.active, HOLD.signum = True, None
    try:
        for action in actions:
            if HOLD.signum is not None:
                break
            print(f"\n$ {action.command}", flush=True)
            log(f"on the terminal: $ {action.command}")
            code = run(action)
            interrupted = code in (-signal.SIGINT, 130)  # Ctrl-C: how a followed log ends
            log(f"{ended(code)}: {action.command}", "ok" if code == 0 else "info" if interrupted else "error")
            worst = code if code and not worst else worst
    finally:
        HOLD.active = False
    if HOLD.signum is not None:
        print(f"\nworms-gui: ended by signal {HOLD.signum}, once the command had", flush=True)
        raise SystemExit(128 + HOLD.signum)
    sessions = all(getattr(action, "session", False) for action in actions)
    if sessions and (worst == 0 or time.monotonic() - started >= SESSION_LASTED):
        return worst  # a login that lasted: the shell's last status says nothing, and the operator wants the screen back
    print(f"\n[{ended(worst)}] press a key to return to worms-gui", end="", flush=True)
    _key()
    return worst


def _attached(argv: list, env: dict) -> int:
    """`argv` on this terminal: its exit code. Ctrl-C is the command's (it is in this process group), not
    the GUI's; a SIGHUP or a SIGTERM to the GUI waits for it (HOLD)."""
    try:
        previous = signal.signal(signal.SIGINT, _nothing)
    except ValueError:  # not the main thread: Ctrl-C would end the GUI too
        previous = None
    try:
        try:
            proc = subprocess.Popen(argv, env=env)
        except OSError as exc:
            print(f"could not start it: {exc}", flush=True)
            return 127
        while True:
            try:
                return proc.wait()
            except KeyboardInterrupt:  # a Ctrl-C before the handler was in place: still the command's
                continue
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


def _key() -> None:
    """One key on the terminal (Enter where it is no terminal); Ctrl-C counts as one."""
    try:
        import termios
        import tty
    except ImportError:
        termios = None
    if termios is None or not sys.stdin.isatty():
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
        return
    fd = sys.stdin.fileno()
    try:
        saved = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            termios.tcflush(fd, termios.TCIFLUSH)  # not a key typed while the command ran
            os.read(fd, 1)
        except KeyboardInterrupt:  # Ctrl-C at "press a key" (right after ending a `log -f`): a key, not a quit
            pass
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    except (OSError, termios.error):
        pass
