"""worms-gui's screen (curses), htop-like: the host's meters and badges on top, seven tabs of tables,
a detail pane, the action menu and its dialogs, a status line and the keys at the bottom.

    1 VMs  2 Jobs  3 Rounds  4 Services  5 Components  6 Tasks  7 Log

It reads the store's snapshot (state.py's docstring has its fields) and never waits on I/O: the
collectors fill the store in their threads (collect.py), the commands run in theirs (actions.py), a
pane's read-only command (`worms show job 812`) in its own. It draws after a key, and once a second
with a new snapshot when the store has changed (its version): an idle screen costs next to nothing. A command that wants the
terminal (login, run, apply, ...) gets it: curses is left, the command runs, a key brings the screen
back as it was (the same tab, cursor, marks, filters and pane).

What it asks of its backend (app.py wires the live one, and a fake one for --demo):
    snapshot(), version              the store's snapshot, and its version (a new snapshot only when it moved)
    menu(tab, items, snap)           actions.menu: the Entries (key, label, prompt, unavailable, scope, make)
                                     for these records; entry.make(answer) -> [Action], or raises
                                     Unavailable (its text says why)
    start(action)                    a background action: a Task in the next snapshots (its id)
    foreground(actions) -> int       run them on the terminal, then wait for a key (curses is left)
    task_signal(task_id, what)       "cancel" | "pause" | "resume" a task: "" or why not
    read_argv(words), read_env()     a read-only `worms <words>` for a pane, and its environment
    refresh(), say(text, level), dry_run, watchers
An Action has title, argv, mode ("foreground" | "background"), confirm (the question, "" for none),
destructive, targets, typed (the word to type to run it), note, command. --dry-run is the backend's:
it says what it would run; the screen then stays as it is.
"""

from __future__ import annotations

import atexit
import curses
import locale
import os
import re
import shlex
import signal
import subprocess
import sys
import termios
import textwrap
import threading
import time
import tty
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable

TICK_MS = 250  # getch's timeout: how soon a second's redraw comes; nothing is drawn when nothing changed
SNAP_EVERY = 1.0  # s: a new snapshot at most this often (when the store changed), and a redraw with it
HINT_SECONDS = 8.0  # how long a message stays in the status line
FETCH_SETTLE = 0.7  # s the pane's record stays under the cursor before its command runs (not one per row passed)
FETCH_AGAIN = 45.0  # s: a pane's command is run again when its record is still moving (a queued job)
FETCHES_KEPT = 40  # the panes' readings kept (the newest)
STEAL_WARN = 20.0  # % of one core: a VM's steal shown in its activity (the host gives it too little time)

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-_]")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

KIND_SHORT = {"persistent": "infra", "ephemeral": "service", "private_internet": "private", "devbox": "devbox",
              "sandbox": "sandbox"}  # fmt: skip
KIND_ORDER = list(KIND_SHORT)  # worms_cli.vms.KINDS' order, as `worms status` lists the VMs
HOST_FIREWALL = ("proxmox-firewall", "worms-host-firewall", "worms-firewall-watchdog.timer", "worms-mirror")
AGENT_UNIT = "worms-round-agent"
WATCHDOG_EVERY = 15  # s: host_fw_watchdog_interval (ansible/roles/host/host_network/defaults/main.yml)
RUNNING_TASK = ("waiting", "running", "paused")


# -- text --------------------------------------------------------------------------------------------


def width_of(text) -> int:
    """Terminal cells, not code points: a wide (CJK, emoji) character takes two, a combining one none."""
    text = str(text)
    if text.isascii():
        return len(text)
    return sum(0 if unicodedata.combining(ch) else 2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def cut(text, width: int) -> str:
    """The start of `text` that fits in `width` cells."""
    text = str(text)
    if text.isascii():
        return text[: max(0, width)]
    out, used = [], 0
    for ch in text:
        w = 0 if unicodedata.combining(ch) else 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if used + w > width:
            break
        out.append(ch)
        used += w
    return "".join(out)


def clean(text) -> str:
    """Someone's line as the screen can show it: no colour codes, no control characters; of a line that
    a progress meter rewrote with \\r, what a terminal would show last."""
    text = ANSI.sub("", str(text)).replace("\t", "    ").rstrip("\r")
    return CONTROL.sub("", text.rsplit("\r", 1)[-1])


def size(n) -> str:
    """Bytes, short: 512B, 1.5K, 24.1G."""
    if n is None:
        return "?"
    n = float(n)
    for unit in "BKMGT":
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" or n >= 100 else f"{n:.1f}{unit}"
        n /= 1024
    return "?"


def rate(n) -> str:
    """Bytes per second, short; None (no two readings yet) as `.`."""
    return "." if n is None else ("0" if n < 1 else size(n))


def span(seconds, long=False) -> str:
    """18d10h, 3h31m, 12m05s, 45s; long: 18 d 10 h, 3 h 31 min, 12 min, 45 s."""
    if seconds is None:
        return "?"
    s = int(max(0, seconds))
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    if long:
        return f"{d} d {h} h" if d else f"{h} h {m} min" if h else f"{m} min" if m else f"{s} s"
    return f"{d}d{h:02d}h" if d else f"{h}h{m:02d}m" if h else f"{m}m{s % 60:02d}s" if m else f"{s}s"


def when(epoch, now=None) -> str:
    """A time in UTC, as the CLI and the database say them: 18:03:12 today, 11-21 18:03 another day, "" for none.
    (Not local: Django's setup in the database collector sets this process's TZ to UTC anyway.)"""
    if not epoch:
        return ""
    now = now or time.time()
    utc, today = time.gmtime(epoch), time.gmtime(now)
    return time.strftime("%H:%M:%S" if utc[:3] == today[:3] else "%m-%d %H:%M", utc)


def command_line(action) -> str:
    """What an action runs, as the operator would type it: `worms vm stop alpha`."""
    shown = field_of(action, "command")
    if shown:
        return str(shown)
    argv = [str(a) for a in field_of(action, "argv") or []]
    if len(argv) >= 2 and argv[1].endswith("manage.py"):
        return "worms " + shlex.join(argv[2:])
    return shlex.join(argv)


def field_of(record, name, default=None):
    """A field of an Action or a Task: a dataclass or a dict."""
    value = record.get(name, default) if isinstance(record, dict) else getattr(record, name, default)
    return default if value is None else value


def pct(part, whole):
    return None if part is None or not whole else 100.0 * part / whole


# -- what each tab can do ----------------------------------------------------------------------------
# The menus are the backend's (actions.menu: an Entry per command, with its key); the screen only
# knows which of their keys work on a tab without the menu, and the Tasks tab's own verbs.

HOT_KEYS = {"vms": "l!L", "jobs": "cpPL", "rounds": "pPsc", "services": "L"}  # the contract's single keys (actions.find)
ONE_KEYS = {"vms": "lL", "jobs": "L", "services": "L"}  # the cursor's record, not the marked ones: one login, one log


@dataclass(frozen=True)
class TaskVerb:
    key: str
    name: str  # what backend.task_signal is asked
    label: str


TASK_VERBS = (
    TaskVerb("c", "cancel", "cancel (Ctrl-C to it, SIGTERM 10 s later)"),
    TaskVerb("p", "pause", "pause (SIGSTOP: its SSH connections may time out; one that holds the host's lock keeps it)"),
    TaskVerb("P", "resume", "resume (SIGCONT)"),
)


def find(entries, key):
    return next((entry for entry in entries or [] if field_of(entry, "key") == key), None)


# -- columns and tabs --------------------------------------------------------------------------------


@dataclass
class Col:
    key: str
    title: str
    width: int  # its width; a flex column's least
    text: Callable  # (record, ui) -> str
    sort: Callable | None = None  # (record, ui) -> a value, None last; default its text
    style: Callable | None = None  # (record, ui) -> a style name
    right: bool = False
    flex: bool = False  # takes the room that is left
    keep: int = 5  # the higher, the sooner it goes when the screen is narrow (0: never)
    auto: bool = False  # as wide as its widest cell, at most `width`
    desc: bool = False  # chosen to sort by, the biggest first (htop's CPU%, MEM%)
    want: int = 0  # a flex column: the width it takes from the columns kept less (a higher `keep`) before they stay


def fit_columns(columns: list[Col], width: int, items, ui) -> list[tuple[Col, int]]:
    """The columns that fit in `width`, at their widths: the least kept ones go first."""
    columns = [c for c in columns if c.width or c.auto]  # a width of 0: only an order to sort by
    widths = {}
    for col in columns:
        if col.auto:
            cells = [width_of(col.text(item, ui)) for item in items[:400]]
            widths[col.key] = max(len(col.title) + 2, min(col.width, max(cells, default=0)))
        else:
            widths[col.key] = col.width
    chosen = list(columns)

    def need(cols):
        return sum(widths[c.key] for c in cols) + len(cols) - 1

    while need(chosen) > width and len(chosen) > 1:
        droppable = [c for c in chosen if c.keep]
        if not droppable:
            break
        chosen.remove(max(droppable, key=lambda c: (c.keep, chosen.index(c))))
    flex = next((c for c in chosen if c.flex), None)
    while flex is not None and flex.want and widths[flex.key] + width - need(chosen) < flex.want:
        # The flex column (the VMs' activity) is worth more than the columns kept less: they go first.
        droppable = [c for c in chosen if c.keep > flex.keep]
        if not droppable:
            break
        chosen.remove(max(droppable, key=lambda c: (c.keep, chosen.index(c))))
    room = width - need(chosen)
    out = [(c, widths[c.key]) for c in chosen]
    for i, (c, w) in enumerate(out):
        if c.flex and room > 0:
            out[i] = (c, w + room)
            break
    return out


@dataclass
class View:
    """What the operator made of a tab: kept across refreshes, and across a command on the terminal."""

    sort: str
    reverse: bool = False
    cursor: Any = None  # the record's id under the cursor
    index: int = 0  # ... and its row, for when that record goes
    top: int = 0  # the first row shown
    marks: set = field(default_factory=set)
    text: str = ""  # the `/` filter
    quick: int = 0  # the `f` filter
    follow: bool = False  # the cursor stays on the last row (the Log tab)


class Tab:
    name = ""
    title = ""
    short = ""
    source = ""  # whose data (the snapshot's sources): dim when stale
    default_sort = ""
    default_reverse = False
    markable = True
    follow = False
    quick: tuple = (("all", lambda r, ui: True),)

    def items(self, snap) -> list[dict]:
        return []

    def ident(self, record):
        return record.get("id")

    def columns(self) -> list[Col]:
        return []

    def label(self, record) -> str:
        return str(record.get("name") or record.get("id"))

    def count(self, snap, items) -> str:
        return str(len(items))

    def attention(self, snap, items, ui) -> bool:
        return False

    def detail(self, record, ui) -> list:
        return [[(f"{k}: {v}", "normal")] for k, v in record.items()]

    def empty(self, ui) -> str:
        return "(nothing here)"

    def row_style(self, record, ui) -> str | None:
        return None


def kv(label, value, style="normal", width=12) -> list:
    """A pane's line: a label, then its value."""
    return [(f"{label:<{width - 1}} ", "label"), (str(value), style)]


def onoff(value, on="on", off="OFF") -> tuple[str, str]:
    return (on, "ok") if value is True else (off, "error") if value is False else ("?", "dim")


def stale(ui, source) -> bool:
    state = (ui.snap.get("sources") or {}).get(source)
    return bool(state and state.get("stale") and state.get("updated_at"))


class VmsTab(Tab):
    name, title, short, source, default_sort = "vms", "VMs", "VMs", "proxmox", "kind"
    quick = (
        ("all", lambda r, ui: True),
        ("platform", lambda r, ui: bool(r.get("trusted"))),
        ("team VMs", lambda r, ui: not r.get("trusted")),
        ("sandbox", lambda r, ui: r.get("pool") == "sandbox"),
        ("devbox", lambda r, ui: r.get("pool") == "devbox"),
        ("running", lambda r, ui: r.get("status") in ("running", "paused")),
        ("stopped", lambda r, ui: r.get("status") not in ("running", "paused")),
    )

    def items(self, snap):
        return list((snap.get("vms") or {}).values())

    def ident(self, record):
        # By name too: a vmid is given again after a destroy, and a mark must not move to the VM that gets it.
        return (record.get("vmid"), record.get("name"))

    def columns(self):
        running = ("running", "paused")
        return [
            Col("name", "VM", 24, lambda r, ui: r.get("name", ""), auto=True, keep=0),
            Col("vmid", "vmid", 5, lambda r, ui: str(r.get("vmid", "")), lambda r, ui: int(r.get("vmid") or 0), right=True, keep=7),
            Col("kind", "kind", 7, lambda r, ui: KIND_SHORT.get(r.get("pool") or "", r.get("pool") or "-"), self._kind_order, keep=4),
            Col("team", "team", 16, lambda r, ui: r.get("team") or "", auto=True, keep=6),
            Col("state", "state", 8, self._state, self._state_order, self._state_style, keep=0),
            Col("up", "up", 7, lambda r, ui: span(r.get("uptime")) if r.get("status") in running else "-",
                lambda r, ui: r.get("uptime") if r.get("status") in running else None, right=True, keep=7, desc=True),  # fmt: skip
            # A VM that does not run sorts last either way (None), not as the idlest.
            Col("cpu", "CPU%", 5, self._cpu, lambda r, ui: r.get("cpu_pct") if r.get("status") == "running" else None, self._cpu_style,
                right=True, keep=1, desc=True),  # fmt: skip
            Col("mem", "MEM", 10, self._mem, lambda r, ui: r.get("mem") if r.get("status") == "running" else None, right=True, keep=2,
                desc=True),  # fmt: skip
            Col("net", "NET in/out", 11, lambda r, ui: self._rates(r, "net_in_bps", "net_out_bps"),
                lambda r, ui: self._rate_sum(r, "net_in_bps", "net_out_bps"), right=True, keep=8, desc=True),  # fmt: skip
            Col("disk", "DISK r/w", 11, lambda r, ui: self._rates(r, "disk_read_bps", "disk_write_bps"),
                lambda r, ui: self._rate_sum(r, "disk_read_bps", "disk_write_bps"), right=True, keep=9, desc=True),  # fmt: skip
            Col("fw", "FW", 4, self._fw, lambda r, ui: {"OFF": 0, "?": 1, "on": 2}.get(self._fw(r, ui), 3), self._fw_style, keep=1),
            Col("clean", "clean", 5, self._clean, lambda r, ui: {False: 0, True: 1}.get(r.get("clean")), self._clean_style, keep=5),
            Col("fail", "fail", 4, self._failed, lambda r, ui: (r.get("watch") or {}).get("failed_units"),
                lambda r, ui: "error" if (r.get("watch") or {}).get("failed_units") else "dim", right=True, keep=4,
                desc=True),  # fmt: skip
            Col("activity", "activity", 20, self._activity, self._busy, self._activity_style, flex=True, keep=3, desc=True, want=36),
            Col("updated", "updated", 19, lambda r, ui: r.get("updated") or "", lambda r, ui: r.get("updated_at") or None,
                auto=True, keep=8, desc=True),  # fmt: skip
        ]

    @staticmethod
    def _rate_sum(r, one, other):
        if r.get("status") != "running" or (r.get(one) is None and r.get(other) is None):
            return None
        return (r.get(one) or 0) + (r.get(other) or 0)

    @staticmethod
    def _failed(r, ui):
        watch = r.get("watch") or {}
        return "" if watch.get("failed_units") in (None, 0) or watch.get("stale") else str(watch["failed_units"])

    @staticmethod
    def _busy(r, ui):
        """How busy it is by what its activity says: a watcher's load, else (a team VM) its CPU pressure."""
        watch = r.get("watch") or {}
        if watch.get("load1") is not None:
            return watch["load1"]
        return r.get("psi_cpu") if r.get("status") == "running" else None

    @staticmethod
    def _rates(r, one, other):
        if r.get("status") != "running":
            return "-"
        return "." if r.get(one) is None and r.get(other) is None else f"{rate(r.get(one))}/{rate(r.get(other))}"

    @staticmethod
    def _kind_order(r, ui):
        pool = r.get("pool") or ""
        return KIND_ORDER.index(pool) if pool in KIND_ORDER else len(KIND_ORDER)

    @staticmethod
    def _state(r, ui):
        status = r.get("status") or "?"
        if status == "stopped" and r.get("held"):
            return "held"
        if status == "stopped" and r.get("window_stopped"):
            return "window"
        if status == "stopped" and r.get("pool") not in ("sandbox", "devbox"):
            return "STOPPED"  # a platform VM that should run: in capitals, for a screen without colours
        if r.get("lock"):
            return f"{status[:4]}/{r['lock']}"
        return status

    @staticmethod
    def _state_order(r, ui):
        return {"running": 2, "paused": 1}.get(r.get("status"), 0)

    @staticmethod
    def _state_style(r, ui):
        status = r.get("status")
        if r.get("lock"):
            return "warn"
        if status == "running":
            return "ok"
        if status == "paused" or r.get("held") or r.get("window_stopped"):
            return "paused"
        return "error" if r.get("pool") not in ("sandbox", "devbox") else "dim"

    @staticmethod
    def _cpu(r, ui):
        return "-" if r.get("status") != "running" else ("?" if r.get("cpu_pct") is None else f"{r['cpu_pct']:.1f}")

    @staticmethod
    def _cpu_style(r, ui):
        cpu, cores = r.get("cpu_pct"), r.get("maxcpu") or 1
        return None if cpu is None else "error" if cpu >= 95 * cores else "warn" if cpu >= 75 * cores else None

    @staticmethod
    def _mem(r, ui):
        if r.get("status") not in ("running", "paused"):
            return f"-/{int(r.get('maxmem') or 0) >> 20}M"
        return f"{int(r.get('mem') or 0) >> 20}/{int(r.get('maxmem') or 0) >> 20}M"

    @staticmethod
    def _fw(r, ui):
        return (r.get("firewall") or {}).get("summary") or "?"

    def _fw_style(self, r, ui):
        if stale(ui, "host"):
            return "dim"  # the host's view of it is old: not shown as current
        return {"on": "ok", "OFF": "error"}.get(self._fw(r, ui), "dim")

    @staticmethod
    def _clean(r, ui):
        return {True: "yes", False: "NO"}.get(r.get("clean"), "")

    @staticmethod
    def _clean_style(r, ui):
        return "dim" if stale(ui, "host") else {True: "ok", False: "warn"}.get(r.get("clean"))

    @staticmethod
    def _activity(r, ui):
        task = r.get("task") or {}
        if task.get("active"):
            return f"Proxmox: {task.get('type', '?')} running"
        if r.get("status") not in ("running", "paused"):
            return ""
        if not r.get("trusted"):
            psi = r.get("psi_cpu")
            return "host view: " + (f"CPU pressure {psi:.0f}%" if psi is not None else "no pressure known yet")
        watch = r.get("watch")
        if not watch:
            return "no watcher" if ui.backend_watchers else "no watcher (--no-watchers)"
        if watch.get("error") and watch.get("load1") is None:
            return f"watcher: {watch['error']}"
        top = (watch.get("top") or [[None, ""]])[0]
        busiest = f" {ui.g['dot']} {top[0]:.0f}% {top[1]}" if top and top[0] is not None else ""
        steal = watch.get("steal_pct")
        stolen = f" {ui.g['dot']} steal {steal:.0f}%" if steal is not None and steal >= STEAL_WARN else ""
        load = watch.get("load1")
        return (f"load {load:.2f}" if load is not None else "load ?") + stolen + busiest

    @staticmethod
    def _activity_style(r, ui):
        task = r.get("task") or {}
        if task.get("active"):
            return "warn"
        watch = r.get("watch")
        if not r.get("trusted") or not watch:
            return "warn" if (r.get("psi_cpu") or 0) >= 20 and r.get("status") == "running" else "dim"
        if watch.get("error") or watch.get("stale"):
            return "dim" if watch.get("load1") is not None else "warn"
        if (watch.get("steal_pct") or 0) >= STEAL_WARN:
            return "error" if watch["steal_pct"] >= 50 * max(1, watch.get("cpus") or 1) else "warn"  # half its time stolen
        return None

    def label(self, record):
        return record.get("name") or str(record.get("vmid"))

    def attention(self, snap, items, ui):
        return any((r.get("status") == "stopped" and r.get("pool") not in ("sandbox", "devbox"))
                   or (r.get("firewall") or {}).get("summary") == "OFF" for r in items)  # fmt: skip

    def empty(self, ui):
        source = (ui.snap.get("sources") or {}).get("proxmox") or {}
        if source.get("error"):
            return f"(no VMs: the Proxmox API: {source['error']})"
        return "(no VMs yet: waiting for the Proxmox API)"

    def row_style(self, record, ui):
        return "dim" if stale(ui, "proxmox") else None

    def detail(self, r, ui):
        now = ui.snap.get("now") or time.time()
        running = r.get("status") in ("running", "paused")
        out = [[(f"{r.get('name')}", "bold"), (f"   vmid {r.get('vmid')} on {r.get('node') or '?'}   {r.get('kind')}", "normal"),
                (f"   pool {r.get('pool') or '-'}", "dim")]]  # fmt: skip
        if r.get("team"):
            handle = r.get("team_handle")
            out.append(kv("team", r["team"] + (f"   ({handle}/{r['role']})" if handle and r.get("role") else "")))
        state = r.get("status", "?") + (f", up {span(r.get('uptime'), long=True)}" if running else "")
        if r.get("qmpstatus") and r.get("qmpstatus") != r.get("status"):
            state += f" (QEMU: {r['qmpstatus']})"
        if r.get("lock"):
            state += f", locked: {r['lock']}"
        out.append(kv("state", state, self._state_style(r, ui)))
        if r.get("held"):
            out.append(kv("", "held shut down by a round: its release starts it (`worms vm start --held` once it is over)", "paused"))
        if r.get("window_stopped"):
            out.append(kv("", "stopped by the internet window: `worms internet off` starts it", "paused"))
        if running:
            cores = r.get("maxcpu") or "?"
            cpu = r.get("cpu_pct")
            out.append(kv("CPU", (f"{cpu:.1f}%" if cpu is not None else "?") + f" (100% = one core; {cores} cores)",
                          self._cpu_style(r, ui) or "normal"))  # fmt: skip
            used = pct(r.get("mem"), r.get("maxmem"))
            share = f" ({used:.0f}%)" if used is not None else ""
            out.append(kv("memory", f"{size(r.get('mem'))} of {size(r.get('maxmem'))}{share}"))
            out.append(kv("network", f"in {rate(r.get('net_in_bps'))}/s   out {rate(r.get('net_out_bps'))}/s"))
            out.append(kv("disk", f"read {rate(r.get('disk_read_bps'))}/s   write {rate(r.get('disk_write_bps'))}/s"))
            psi = [(name, r.get(f"psi_{name}")) for name in ("cpu", "io", "mem")]
            if any(value is not None for _, value in psi):  # Proxmox's view: a team VM's too, the one signal besides CPU, NET, DISK
                out.append(kv("pressure", "   ".join(f"{name} {'?' if value is None else f'{value:.1f}%'}" for name, value in psi)
                              + "   (some, avg10: the share of time it waited for it)",
                              "warn" if any((value or 0) >= 20 for _, value in psi) else "normal"))  # fmt: skip
        else:
            out.append(kv("memory", f"{size(r.get('maxmem'))} when it runs"))
        fw = r.get("firewall") or {}
        vm_text, vm_style = onoff(fw.get("vm"))
        rules = fw.get("rules")
        rules_text, rules_style = ("- (stopped: no tap device)", "dim") if rules is None and not running else onoff(rules)
        line = kv("firewall", self._fw(r, ui), self._fw_style(r, ui))
        line += [("   its option ", "dim"), (vm_text, vm_style), ("   the host's guest rules ", "dim"), (rules_text, rules_style)]
        nics = fw.get("nics")
        if nics:
            line += [(f"   NICs with firewall=1: {nics[0]} of {nics[1]}", "ok" if nics[0] == nics[1] else "error")]
        out.append(line)
        if nics and len(nics) > 2 and nics[2]:
            mirror = ((ui.snap.get("host") or {}).get("units") or {}).get("worms-mirror")
            out.append(kv("", f"and {nics[2]} on the mirror bridge: no firewall by design, receive-only by worms-mirror "
                              f"({mirror or '?'})", "ok" if mirror == "active" else "error"))  # fmt: skip
        if r.get("clean") is not None:
            clean_text = "clean: a reset returns to it" if r["clean"] else \
                "NO `clean` snapshot: a reset leaves it as it is (`worms vm rebuild services`)"  # fmt: skip
            out.append(kv("snapshot", clean_text, self._clean_style(r, ui)))
        out.append(kv("history", f"created {r.get('created') or '?'}   updated {r.get('updated') or '?'}"))
        task = r.get("task")
        if task:
            started = when(task.get("start"), now)
            text = f"{task.get('type', '?')} {task.get('status') or ''} by {task.get('user', '?')}"
            text += f" at {started}" if started else ""
            out.append(kv("Proxmox", text + (" (running now)" if task.get("active") else ""), "warn" if task.get("active") else "dim"))
        services = {s.get("name"): s for s in ui.snap.get("services") or []}
        for name in r.get("services") or []:
            service = services.get(name) or {}
            units = "  ".join(f"{unit} {state or '?'}" for unit, state in (service.get("units") or {}).items())
            out.append(kv("service", f"{name}: {units}", ServicesTab.state_style(service.get("state"))))
        out.append([])
        if not r.get("trusted"):
            out.append([("A team's VM or a gateway, assumed rooted: only the host's view of it counts. No watcher, nothing run on it.",
                         "dim")])  # fmt: skip
        elif not r.get("watch"):
            out.append([("no watcher: it is not running" if not running else "no watcher yet (or --no-watchers)", "dim")])
        else:
            watch = r["watch"]
            age = watch.get("age_s")
            seen = f"   (its last line {span(age)} ago{', STALE' if watch.get('stale') else ''})" if age is not None else ""
            out.append([("watcher", "bold"), (seen, "warn" if watch.get("stale") else "dim")])
            if watch.get("error"):
                out.append(kv("error", watch["error"], "error"))

            def num(value, fmt):
                return "?" if value is None else fmt.format(value)

            loads = " ".join(num(watch.get(k), "{:.2f}") for k in ("load1", "load5", "load15"))
            out.append(kv("load", loads))
            steal = watch.get("steal_pct")
            out.append(kv("CPU", num(watch.get("cpu_pct"), "{:.1f}%") + " (its own work)   steal " + num(steal, "{:.1f}%")
                          + ("   (time the host did not give it: the host is short of CPU)" if (steal or 0) >= STEAL_WARN else ""),
                          "warn" if (steal or 0) >= STEAL_WARN else "normal"))  # fmt: skip
            out.append(kv("memory", "used " + num(watch.get("mem_used_pct"), "{:.0f}%") + "   / used "
                          + num(watch.get("root_used_pct"), "{:.0f}%")))  # fmt: skip
            failed = watch.get("failed_units")
            out.append(kv("failed units", ("?" if failed is None else str(failed)) + "   (systemctl, every 30 s)",
                          "error" if failed else "ok"))  # fmt: skip
            for cpu, command in watch.get("top") or []:
                out.append(kv("  top", f"{num(cpu, '{:5.1f}%')}  {command}"))
            if not watch.get("top"):
                out.append(kv("  top", "nothing busy in the last 3 s (the watcher leaves itself out)", "dim"))
            for unit, state in (watch.get("units") or {}).items():
                out.append(kv("  unit", f"{unit} {state}", "ok" if state == "active" else "error"))
        out.append([])
        out.append([("m menu (the marked VMs, or this one)   l log in   ! run a command   L its log", "dim")])
        out.append([])
        if not r.get("trusted"):
            out.append([("`worms show` (d in the menu) is not run here for a team VM or a gateway: it asks the VM itself over SSH "
                         "whether it answers", "dim")])  # fmt: skip
            return out
        # Its snapshots, disks, networks and history on the host (`worms show`: read-only; `ssh true` for whether it answers).
        fetch = ui.fetch(("vm", r.get("vmid"), r.get("name")), ["show", r.get("name") or str(r.get("vmid"))])
        out.append([(f"worms show {r.get('name')}", "bold"), (fetch.note(ui.now), "dim")])
        out.extend([[(line, "normal")] for line in fetch.lines])
        return out


class JobsTab(Tab):
    name, title, short, source, default_sort, default_reverse = "jobs", "Jobs", "Jobs", "db", "id", True
    quick = (
        ("all", lambda r, ui: True),
        ("active", lambda r, ui: r.get("state") in ("queued", "running")),
        ("queued", lambda r, ui: r.get("state") == "queued"),
        ("running", lambda r, ui: r.get("state") == "running"),
        ("failed", lambda r, ui: r.get("state") == "failed"),
        ("done", lambda r, ui: r.get("state") == "done"),
    )

    def items(self, snap):
        return list(snap.get("jobs") or [])

    def label(self, record):
        return f"#{record.get('id')} {record.get('kind')}"

    def columns(self):
        return [
            Col("id", "#", 6, lambda r, ui: str(r.get("id", "")), lambda r, ui: r.get("id") or 0, right=True, keep=0),
            Col("kind", "job", 18, lambda r, ui: r.get("kind") or "", auto=True, keep=0),
            Col("state", "state", 7, lambda r, ui: r.get("state") or "", None, self._state_style, keep=0),
            Col("team", "team", 14, lambda r, ui: r.get("team") or "", auto=True, keep=4),
            Col("run", "run", 5, lambda r, ui: str(r.get("run") or ""), lambda r, ui: r.get("run"), right=True, keep=7),
            Col("requested", "requested", 11, lambda r, ui: when(r.get("requested_at"), ui.now), lambda r, ui: r.get("requested_at"),
                keep=5),  # fmt: skip
            Col("started", "started", 11, lambda r, ui: when(r.get("started_at"), ui.now), lambda r, ui: r.get("started_at"), keep=8),
            Col("took", "took", 7, self._took, self._took_seconds, right=True, keep=3),
            Col("note", "why it waits / error", 20, self._note, None, self._note_style, flex=True, keep=2),
        ]

    @staticmethod
    def _state_style(r, ui):
        state = r.get("state")
        if state == "queued" and ((ui.snap.get("host") or {}).get("agent_paused")):
            return "paused"
        return {"queued": "warn", "running": "ok", "failed": "error", "done": "dim"}.get(state)

    @staticmethod
    def _took_seconds(r, ui):
        start = r.get("started_at")
        if not start:
            return None
        return (r.get("finished_at") or ui.now) - start

    def _took(self, r, ui):
        seconds = self._took_seconds(r, ui)
        return "" if seconds is None else span(seconds)

    @staticmethod
    def _note(r, ui):
        if r.get("state") == "queued":
            return "; ".join(r.get("reasons") or []) or "next for the agent"
        if r.get("state") == "failed":
            return clean(r.get("error") or "failed")
        if r.get("state") == "running" and r.get("heartbeat_at"):
            return f"heartbeat {span(ui.now - r['heartbeat_at'])} ago"
        return clean(r.get("error") or "")

    @staticmethod
    def _note_style(r, ui):
        return {"failed": "error", "queued": "warn"}.get(r.get("state"), "dim")

    def count(self, snap, items):
        return f"{sum(1 for r in items if r.get('state') in ('queued', 'running'))}/{len(items)}"  # active/all, as Tasks

    def attention(self, snap, items, ui):
        day = (snap.get("now") or time.time()) - 86400
        return any(r.get("state") == "failed" and (r.get("finished_at") or 0) >= day for r in items)

    def empty(self, ui):
        source = (ui.snap.get("sources") or {}).get("db") or {}
        return f"(no jobs: the database: {source['error']})" if source.get("error") else "(no host jobs)"

    def row_style(self, record, ui):
        return "dim" if stale(ui, "db") else None

    def detail(self, r, ui):
        now = ui.now
        out = [[(f"#{r.get('id')} {r.get('kind')}", "bold"), ("   " + (r.get("state") or "?"), self._state_style(r, ui) or "normal")]]
        if r.get("team"):
            out.append(kv("team", r["team"]))
        if r.get("run"):
            out.append(kv("round run", str(r["run"])))
        for label, key in (("requested", "requested_at"), ("started", "started_at"), ("heartbeat", "heartbeat_at"),
                           ("finished", "finished_at")):  # fmt: skip
            if r.get(key):
                out.append(kv(label, f"{when(r[key], now)}   ({span(now - r[key], long=True)} ago)"))
        took = self._took_seconds(r, ui)
        if took is not None:
            out.append(kv("took", span(took, long=True) + (" so far" if not r.get("finished_at") else "")))
        for reason in r.get("reasons") or []:
            out.append(kv("waits", reason, "warn"))
        if r.get("error"):
            for i, line in enumerate(str(r["error"]).splitlines() or [""]):
                out.append(kv("error" if i == 0 else "", clean(line), "error"))
        if r.get("uuid"):
            out.append(kv("log", f"/var/log/worms/jobs/{r['uuid']}.log on the host   (L: follow it)", "dim"))
        out.append([])
        moving = r.get("state") in ("queued", "running")
        fetch = ui.fetch(("job", r.get("id")), ["show", "job", str(r.get("id"))], again=moving)
        out.append([("worms show job " + str(r.get("id")), "bold"), (fetch.note(ui.now), "dim")])
        out.extend([[(line, "normal")] for line in fetch.lines])
        return out


class RoundsTab(Tab):
    name, title, short, source, default_sort = "rounds", "Rounds", "Rounds", "db", "start"
    quick = (
        ("all", lambda r, ui: True),
        ("current", lambda r, ui: bool(r.get("current"))),
        ("upcoming", lambda r, ui: (r.get("start") or 0) > ui.now),
        ("past", lambda r, ui: (r.get("end") or 0) and r["end"] < ui.now),
    )

    def items(self, snap):
        return list(snap.get("rounds") or [])

    def label(self, record):
        return f"{record.get('name')} (#{record.get('id')})"

    def columns(self):
        return [
            Col("id", "#", 4, lambda r, ui: str(r.get("id", "")), lambda r, ui: r.get("id") or 0, right=True, keep=3),
            Col("name", "round", 24, lambda r, ui: r.get("name") or "", auto=True, keep=0),
            Col("start", "start", 11, lambda r, ui: when(r.get("start"), ui.now), lambda r, ui: r.get("start"), keep=0),
            Col("end", "end", 11, lambda r, ui: when(r.get("end"), ui.now), lambda r, ui: r.get("end"), keep=4),
            Col("when", "now", 15, self._when, lambda r, ui: r.get("start"), self._when_style, keep=1),
            Col("gameplan", "gameplan", 16, lambda r, ui: r.get("gameplan") or "", auto=True, keep=5),
            Col("check", "check", 9, lambda r, ui: r.get("gameplan_status") or "", None, self._check_style, keep=2),
            Col("per_team", "per team", 10, lambda r, ui: span(r.get("team_duration"), long=True) if r.get("team_duration") else "",
                lambda r, ui: r.get("team_duration"), right=True, keep=6),  # fmt: skip
            Col("hold", "hold", 14, self._hold, None, self._hold_style, keep=2),
            Col("runs", "runs", 20, self._runs, lambda r, ui: sum((r.get("runs") or {}).values()), flex=True, keep=3),
        ]

    @staticmethod
    def _when(r, ui):
        now, start, end = ui.now, r.get("start"), r.get("end")
        if r.get("current"):
            return f"ON, {span(end - now)} left" if end else "ON"
        if start and start > now:
            return f"in {span(start - now)}"
        return "over" if end and end < now else ""

    @staticmethod
    def _when_style(r, ui):
        return "ok" if r.get("current") else "dim" if (r.get("end") or 0) and r["end"] < ui.now else None

    @staticmethod
    def _check_style(r, ui):
        status = (r.get("gameplan_status") or "").lower()
        return "ok" if status in ("valid", "ok", "checked") else "error" if status in ("invalid", "failed", "error") else "warn"

    @staticmethod
    def _hold(r, ui):
        parts = (["ON HOLD"] if r.get("on_hold") else []) + (["devboxes off"] if r.get("devboxes_off") else [])
        if r.get("devbox_hold") and not r.get("devboxes_off"):
            parts.append(f"holds {r['devbox_hold']}" if not isinstance(r["devbox_hold"], bool) else "holds devboxes")
        return ", ".join(parts)

    @staticmethod
    def _hold_style(r, ui):
        return "paused" if r.get("on_hold") else "warn" if r.get("devboxes_off") else None

    @staticmethod
    def _runs(r, ui):
        return "  ".join(f"{state} {n}" for state, n in (r.get("runs") or {}).items() if n)

    def attention(self, snap, items, ui):
        return any(self._check_style(r, ui) == "error" and not ((r.get("end") or 0) and r["end"] < ui.now) for r in items)

    def row_style(self, record, ui):
        return "dim" if stale(ui, "db") else ("bold" if record.get("current") else None)

    def empty(self, ui):
        source = (ui.snap.get("sources") or {}).get("db") or {}
        return f"(no rounds: the database: {source['error']})" if source.get("error") else "(no rounds in this competition)"

    def detail(self, r, ui):
        now = ui.now
        out = [[(f"{r.get('name')}", "bold"), (f"   round #{r.get('id')}   ", "normal"),
                (self._when(r, ui), self._when_style(r, ui) or "normal")]]  # fmt: skip
        out.append(kv("start", f"{when(r.get('start'), now)}"))
        out.append(kv("end", f"{when(r.get('end'), now)}"))
        out.append(kv("gameplan", f"{r.get('gameplan') or '?'}   check: {r.get('gameplan_status') or '?'}", self._check_style(r, ui)))
        if r.get("team_duration"):
            out.append(kv("per team", span(r["team_duration"], long=True)))
        out.append(kv("hold", self._hold(r, ui) or "no"))
        out.append(kv("runs", self._runs(r, ui) or "none yet"))
        run = ui.snap.get("run")
        if run and (run.get("round_id") == r.get("id") or run.get("round") == r.get("name")):
            out.append([])
            out.append([("the attempt under way", "bold")])
            out.append(kv("team", run.get("team") or "?"))
            out.append(kv("state", f"{run.get('state')}   attempt {run.get('attempt')}", "ok"))
            if run.get("worm_start_at"):
                out.append(kv("worm start", when(run["worm_start_at"], now)))
            if run.get("ends_at"):
                out.append(kv("ends", f"{when(run['ends_at'], now)}   ({span(run['ends_at'] - now, long=True)} left)"))
        out.append([])
        out.append([("m menu: pause, resume, start now, cancel", "dim")])
        return out


class ServicesTab(Tab):
    name, title, short, source, default_sort = "services", "Services", "Svcs", "host", "order"
    quick = (
        ("all", lambda r, ui: True),
        ("host", lambda r, ui: r.get("where") == "host"),
        ("VMs", lambda r, ui: r.get("where") != "host"),
        ("not active", lambda r, ui: r.get("state") != "active"),
    )

    def items(self, snap):
        return [dict(s, order=i) for i, s in enumerate(snap.get("services") or [])]

    def ident(self, record):
        return record.get("name")

    @staticmethod
    def state_style(state):
        return {"active": "ok", "partial": "warn", "down": "error"}.get(state, "dim")

    def columns(self):
        return [
            Col("order", "", 0, lambda r, ui: "", lambda r, ui: r.get("order"), keep=9),
            Col("name", "service", 10, lambda r, ui: r.get("name") or "", auto=True, keep=0),
            Col("where", "runs on", 12, lambda r, ui: r.get("where") or "", auto=True, keep=2),
            Col("state", "state", 7, lambda r, ui: r.get("state") or "?", None, self._style, keep=0),
            Col("units", "units", 30, self._units, None, self._style, auto=True, keep=3),
            Col("restart", "restart", 7, lambda r, ui: "yes" if r.get("restartable") else "no", None,
                lambda r, ui: None if r.get("restartable") else "dim", keep=4),  # fmt: skip
            Col("about", "what its log says", 20, lambda r, ui: r.get("about") or "", flex=True, keep=5),
        ]

    def _style(self, r, ui):
        return self.state_style(r.get("state"))

    @staticmethod
    def _units(r, ui):
        return " ".join(unit + ("" if state == "active" else f"({state or '?'})") for unit, state in (r.get("units") or {}).items())

    def attention(self, snap, items, ui):
        return any(r.get("state") in ("down", "partial") for r in items)

    def row_style(self, record, ui):
        return "dim" if record.get("where") == "host" and stale(ui, "host") else None

    def detail(self, r, ui):
        out = [[(r.get("name") or "?", "bold"), (f"   on {r.get('where')}   ", "normal"),
                (r.get("state") or "?", self.state_style(r.get("state")))]]  # fmt: skip
        out.append(kv("about", r.get("about") or ""))
        for unit, state in (r.get("units") or {}).items():
            who = "the host" if r.get("where") == "host" else "its watcher"
            out.append(kv("unit", f"{unit}: {state or f'? (not known: {who})'}",
                          "ok" if state == "active" else "error" if state else "dim"))  # fmt: skip
        if r.get("restartable"):
            out.append(kv("restart", f"worms service restart {r.get('name')}   (m: the menu)"))
        else:
            out.append(kv("restart", f"not from here: {r.get('why_not') or 'not restartable'}", "dim"))
        out.append(kv("log", f"worms log {r.get('name')}   (L: follow it)"))
        return out


class ComponentsTab(Tab):
    name, title, short, source, default_sort = "components", "Components", "Comps", "host", "order"
    quick = (
        ("all", lambda r, ui: True),
        ("VMs", lambda r, ui: r.get("kind") == "vm"),
        ("stacks, host", lambda r, ui: r.get("kind") in ("stack", "host")),
        ("missing", lambda r, ui: r.get("exists") is False),
    )

    def items(self, snap):
        return [dict(c, order=i) for i, c in enumerate(snap.get("components") or [])]

    def ident(self, record):
        return record.get("name")

    def columns(self):
        return [
            Col("order", "", 0, lambda r, ui: "", lambda r, ui: r.get("order"), keep=9),
            Col("name", "component", 16, lambda r, ui: r.get("name") or "", auto=True, keep=0),
            Col("kind", "kind", 9, lambda r, ui: r.get("kind") or "", keep=3),
            Col("exists", "built", 5, lambda r, ui: {True: "yes", False: "NO"}.get(r.get("exists"), "?"), None,
                lambda r, ui: {True: "ok", False: "warn"}.get(r.get("exists"), "dim"), keep=1),  # fmt: skip
            Col("deployed", "last apply", 26, self._deployed, lambda r, ui: r.get("deployed_at") or None, auto=True, keep=4),
            Col("deps", "needs", 24, lambda r, ui: ", ".join(r.get("deps") or []), auto=True, keep=5),
            Col("about", "what it is", 20, lambda r, ui: r.get("about") or "", flex=True, keep=2),
        ]

    def row_style(self, record, ui):
        return "dim" if stale(ui, "host") else None

    @staticmethod
    def _deployed(r, ui):
        at = str(r.get("deployed_at") or "")
        return " ".join(part for part in (at[5:16].replace("T", " "), r.get("deployed_commit") or "") if part)

    def detail(self, r, ui):
        everyone = ui.snap.get("components") or []
        needed_by = [c.get("name") for c in everyone if r.get("name") in (c.get("deps") or [])]
        out = [[(r.get("name") or "?", "bold"), (f"   {r.get('kind')}", "normal")]]
        out.append(kv("what", r.get("about") or ""))
        out.append(kv("built", {True: "yes", False: "NO: an apply creates it"}.get(r.get("exists"), "? (not known yet)"),
                      {True: "ok", False: "warn"}.get(r.get("exists"), "dim")))  # fmt: skip
        out.append(kv("last apply", self._deployed(r, ui) or "no record on the host"))
        out.append(kv("needs", ", ".join(r.get("deps") or []) or "nothing"))
        out.append(kv("needed by", ", ".join(needed_by) or "nothing"))
        out.append([])
        out.append([("m menu: apply or plan the marked components (or this one), or what changed. An apply runs on the terminal.",
                     "dim")])  # fmt: skip
        return out


class TasksTab(Tab):
    name, title, short, source, default_sort, default_reverse = "tasks", "Tasks", "Tasks", "", "id", True
    quick = (
        ("all", lambda r, ui: True),
        ("running", lambda r, ui: r.get("state") in RUNNING_TASK),
        ("failed", lambda r, ui: r.get("state") in ("failed", "cancelled")),
        ("done", lambda r, ui: r.get("state") == "done"),
    )

    def items(self, snap):
        return list(snap.get("tasks") or [])

    def label(self, record):
        return f"#{record.get('id')} {record.get('title')}"

    @staticmethod
    def state_text(r, ui=None):
        state = r.get("state") or "?"
        return f"{state} {r['rc']}" if state == "failed" and r.get("rc") is not None else state

    @staticmethod
    def state_style(r, ui=None):
        return {"running": "ok", "waiting": "paused", "paused": "paused", "failed": "error", "cancelled": "warn",
                "done": "dim"}.get(r.get("state"))  # fmt: skip

    def columns(self):
        return [
            Col("id", "#", 4, lambda r, ui: str(r.get("id", "")), lambda r, ui: r.get("id") or 0, right=True, keep=0),
            Col("state", "state", 9, self.state_text, None, self.state_style, keep=0),
            Col("title", "task", 28, lambda r, ui: r.get("title") or "", auto=True, keep=0),
            Col("started", "started", 8, lambda r, ui: when(r.get("started"), ui.now), lambda r, ui: r.get("started"), keep=4),
            Col("took", "took", 7, lambda r, ui: span(self._took(r, ui)) if r.get("started") else "", self._took, right=True, keep=3),
            Col("last", "its last line", 20, lambda r, ui: clean((r.get("lines") or [""])[-1]), flex=True, keep=2),
        ]

    @staticmethod
    def _took(r, ui):
        return ((r.get("ended") or ui.now) - r["started"]) if r.get("started") else None

    def count(self, snap, items):
        return f"{sum(1 for r in items if r.get('state') in RUNNING_TASK)}/{len(items)}"  # running/all, as Jobs

    def attention(self, snap, items, ui):
        return any(r.get("state") == "failed" for r in items)

    def empty(self, ui):
        return "(no tasks: what worms-gui runs in the background is listed here: vm start/stop, jobs, rounds, restarts)"

    def detail(self, r, ui):
        now = ui.now
        out = [[(f"#{r.get('id')} {r.get('title')}", "bold"), ("   " + self.state_text(r), self.state_style(r) or "normal")]]
        out.append(kv("command", r.get("command") or command_line(r)))
        if r.get("targets"):
            out.append(kv("targets", ", ".join(map(str, r["targets"]))))
        if r.get("started"):
            out.append(kv("started", when(r["started"], now)))
        if r.get("ended"):
            out.append(kv("ended", f"{when(r['ended'], now)}   took {span(r['ended'] - (r.get('started') or r['ended']), long=True)}"
                          f"   exit {r.get('rc')}", "ok" if r.get("rc") == 0 else "error"))  # fmt: skip
        out.append([])
        out.extend([[(clean(line), "normal")] for line in r.get("lines") or []])
        if not r.get("lines"):
            out.append([("(no output yet)", "dim")])
        return out


class LogTab(Tab):
    name, title, short, source, default_sort, markable, follow = "log", "Log", "Log", "", "at", False, True
    quick = (
        ("all", lambda r, ui: True),
        ("warnings", lambda r, ui: r.get("level") in ("warn", "error")),
        ("errors", lambda r, ui: r.get("level") == "error"),
    )

    def items(self, snap):
        return [dict(entry, id=i) for i, entry in enumerate(snap.get("log") or [])]

    def ident(self, record):
        return (record.get("at"), record.get("text"))  # an index moves when the oldest entries go

    def label(self, record):
        return f"{when(record.get('at'))} {record.get('level') or ''}".strip()

    def columns(self):
        level = {"ok": "ok", "warn": "warn", "error": "error", "info": None}
        return [
            Col("at", "time", 8, lambda r, ui: time.strftime("%H:%M:%S", time.gmtime(r.get("at") or 0)),
                lambda r, ui: (r.get("at") or 0, r.get("id")), keep=0),  # fmt: skip
            Col("level", "level", 5, lambda r, ui: r.get("level") or "", None, lambda r, ui: level.get(r.get("level")), keep=3),
            Col("text", "message", 20, lambda r, ui: clean(" ".join(str(r.get("text") or "").split())), None,
                lambda r, ui: level.get(r.get("level")), flex=True, keep=0),  # fmt: skip
        ]

    def count(self, snap, items):
        return str(len(items))

    def attention(self, snap, items, ui):
        return any(r.get("level") == "error" and (r.get("at") or 0) > ui.now - 300 for r in items)

    def empty(self, ui):
        return "(nothing logged yet)"

    def detail(self, r, ui):
        out = [[(time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(r.get("at") or 0)), "bold"), (f"   {r.get('level')}", "normal")]]
        out.extend([[(clean(line), "normal")] for line in str(r.get("text") or "").splitlines()])
        return out


TABS = (VmsTab, JobsTab, RoundsTab, ServicesTab, ComponentsTab, TasksTab, LogTab)
TAB_NAMES = tuple(t.name for t in TABS)


# -- a pane's read-only command ----------------------------------------------------------------------


FETCHING: set = set()  # the panes' commands that run: ended at exit (q, a signal) with the GUI


@atexit.register
def _end_fetches() -> None:
    for fetch in list(FETCHING):
        fetch.stop(block=True)


class Fetch:
    """A read-only command's output for a pane (`worms show job 812`), read in its own thread. `stop()`
    ends it (Ctrl-C first: the CLI then stops its ssh-agent, which a SIGKILL would leave behind)."""

    def __init__(self, argv, env=None, start=True):
        self.argv, self.env = argv, dict(env or os.environ)
        self.lines: list[str] = []
        self.done = False
        self.started = False
        self.rc = None
        self.at = time.time()
        self.proc = None
        self.stopped = False
        if start:
            self.start()

    def start(self):
        self.started, self.at = True, time.time()
        threading.Thread(target=self._run, name="worms-gui fetch", daemon=True).start()

    def _run(self):
        try:
            self.proc = subprocess.Popen(self.argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True, env={**self.env, "NO_COLOR": "1"}, start_new_session=True)  # fmt: skip
            FETCHING.add(self)
            if self.stopped:
                self.stop()
            try:
                out, err = self.proc.communicate(timeout=90)
            except subprocess.TimeoutExpired:
                self.stop()
                out, err = self.proc.communicate()
            lines = out.splitlines()
            if self.proc.returncode:
                lines += [line for line in err.splitlines() if not line.startswith("internet access:")]
            if self.stopped:
                lines = lines or ["(ended: another record's pane, or the GUI quit)"]
            self.lines, self.rc = [clean(line) for line in lines], self.proc.returncode
        except (OSError, subprocess.SubprocessError) as exc:
            self.lines, self.rc = [f"could not run it: {exc}"], -1
        finally:
            FETCHING.discard(self)
        self.done = True

    def stop(self, block=False):
        """SIGINT to its process group (the CLI's atexit stops its agent), SIGTERM, SIGKILL: each after a second;
        in a thread of its own unless `block`."""
        self.stopped = True
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return

        def end():
            for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, signum)
                    proc.wait(timeout=1)
                    return
                except subprocess.TimeoutExpired:
                    continue
                except OSError:
                    return

        if block:
            end()
        else:
            threading.Thread(target=end, name="worms-gui fetch stop", daemon=True).start()

    def note(self, now) -> str:
        if not self.started:
            return "   (run once the cursor rests on it)"
        if not self.done:
            return f"   reading ({span(now - self.at)})"
        return f"   read {span(now - self.at)} ago" + (f", exit {self.rc}" if self.rc else "")


# -- the screen's styles -----------------------------------------------------------------------------


class Paint:
    """The styles by name: colour pairs when the terminal has colours (and NO_COLOR is not set), else
    attributes only; the text itself says what matters (OFF, STOPPED, ALLOWED), never a colour alone."""

    def __init__(self, colours: bool):
        B, D, R, U = curses.A_BOLD, curses.A_DIM, curses.A_REVERSE, curses.A_UNDERLINE
        k, r, g, y, b, m, c, w = (curses.COLOR_BLACK, curses.COLOR_RED, curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_BLUE,
                                  curses.COLOR_MAGENTA, curses.COLOR_CYAN, curses.COLOR_WHITE)  # fmt: skip
        spec = {  # name: (foreground, background, attributes with colours, without)
            "normal": (-1, -1, 0, 0), "bold": (-1, -1, B, B), "dim": (-1, -1, D, D),
            "ok": (g, -1, 0, 0), "warn": (y, -1, 0, B), "error": (r, -1, B, B | U), "paused": (c, -1, 0, 0),
            "label": (c, -1, 0, 0), "title": (c, -1, B, B),
            "head": (k, g, 0, R), "head_sorted": (k, c, B, R | B | U),
            "cursor": (k, c, 0, R), "cursor_marked": (k, y, B, R | B), "marked": (y, -1, B, B | U),
            "tab": (-1, -1, 0, 0), "tab_on": (k, c, B, R | B), "fkey": (-1, -1, B, B), "fkey_label": (k, c, 0, R),
            "prod": (w, r, B, R | B), "dev": (k, m, 0, R), "dry": (k, y, B, R | B),
            "bar": (g, -1, 0, 0), "bar_warn": (y, -1, 0, B), "bar_error": (r, -1, B, B), "bar_cache": (b, -1, B, D),
            "frame": (c, -1, 0, 0), "frame_danger": (r, -1, B, B), "input": (-1, -1, U, U),
        }  # fmt: skip
        self.attrs = {}
        self.colours = colours
        pairs = {}
        if colours:
            try:
                curses.use_default_colors()
                default = -1
            except curses.error:
                default = None
        for name, (fg, bg, with_colour, without) in spec.items():
            if not colours:
                self.attrs[name] = without
                continue
            if default is None:  # no default colours: white on black
                fg, bg = (w if fg == -1 else fg), (k if bg == -1 else bg)
            if (fg, bg) == (-1, -1):
                self.attrs[name] = with_colour
                continue
            if (fg, bg) not in pairs:
                pairs[(fg, bg)] = len(pairs) + 1
                curses.init_pair(pairs[(fg, bg)], fg, bg)
            self.attrs[name] = curses.color_pair(pairs[(fg, bg)]) | with_colour

    def __getitem__(self, name):
        return self.attrs.get(name or "normal", 0)


GLYPHS_UTF8 = {"up": "▲", "down": "▼", "more": "…", "sep": "│", "dot": "·", "mark": "*", "bar": "|"}
GLYPHS_ASCII = {"up": "^", "down": "v", "more": "~", "sep": "|", "dot": "-", "mark": "*", "bar": "|"}
BOX_UTF8 = {"hline": "─", "vline": "│", "ulcorner": "┌", "urcorner": "┐", "llcorner": "└", "lrcorner": "┘"}
BOX_ASCII = {"hline": "-", "vline": "|", "ulcorner": "+", "urcorner": "+", "llcorner": "+", "lrcorner": "+"}


# -- input -------------------------------------------------------------------------------------------


def _wait_key():
    """One key on the terminal, outside curses (what was typed before it does not count)."""
    fd = sys.stdin.fileno()
    try:
        saved = termios.tcgetattr(fd)
    except termios.error:
        return
    try:
        tty.setcbreak(fd)
        termios.tcflush(fd, termios.TCIFLUSH)
        os.read(fd, 1)
    except (OSError, KeyboardInterrupt):
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


ENTER = ("\n", "\r", curses.KEY_ENTER)
BACKSPACE = ("\x7f", "\b", curses.KEY_BACKSPACE)
ESC = "\x1b"


class LineEdit:
    """One line of text being typed: the filter, a prompt's answer."""

    def __init__(self, text=""):
        self.text, self.pos = text, len(text)

    def key(self, k) -> str | None:
        """'enter', 'cancel', or None (the text changed, or nothing happened)."""
        if k in ENTER:
            return "enter"
        if k == ESC:
            return "cancel"
        if k in BACKSPACE:
            if self.pos:
                self.text, self.pos = self.text[: self.pos - 1] + self.text[self.pos :], self.pos - 1
        elif k == curses.KEY_DC or k == "\x04":
            self.text = self.text[: self.pos] + self.text[self.pos + 1 :]
        elif k == curses.KEY_LEFT:
            self.pos = max(0, self.pos - 1)
        elif k == curses.KEY_RIGHT:
            self.pos = min(len(self.text), self.pos + 1)
        elif k in (curses.KEY_HOME, "\x01"):
            self.pos = 0
        elif k in (curses.KEY_END, "\x05"):
            self.pos = len(self.text)
        elif k == "\x15":  # Ctrl-U
            self.text, self.pos = self.text[self.pos :], 0
        elif k == "\x0b":  # Ctrl-K
            self.text = self.text[: self.pos]
        elif k == "\x17":  # Ctrl-W: the word before the cursor
            head = self.text[: self.pos].rstrip()
            head = head[: head.rfind(" ") + 1] if " " in head else ""
            self.text, self.pos = head + self.text[self.pos :], len(head)
        elif isinstance(k, str) and len(k) == 1 and k.isprintable():
            self.text, self.pos = self.text[: self.pos] + k + self.text[self.pos :], self.pos + 1
        return None

    def draw(self, ui, y, x, width, style="input"):
        """The text from where the cursor is visible, the cursor as a reversed cell."""
        start = max(0, self.pos - width + 1)
        shown = self.text[start : start + width]
        ui.put(y, x, shown.ljust(width), style)
        cursor = self.pos - start
        ui.put(y, x + cursor, (self.text[self.pos] if self.pos < len(self.text) else " "), "cursor")


# -- dialogs -----------------------------------------------------------------------------------------


class Modal:
    danger = False

    def draw(self, ui):  # noqa: B027  (a dialog draws itself)
        pass

    def key(self, ui, k):  # noqa: B027
        pass


def wrap(text: str, width: int) -> list[str]:
    out = []
    for paragraph in str(text).splitlines() or [""]:
        out.extend(textwrap.wrap(paragraph, max(10, width), break_on_hyphens=False, drop_whitespace=True) or [""])
    return out


class Menu(Modal):
    """A list to choose from: arrows and Enter, or an entry's key (an entry's key wins over j and k)."""

    def __init__(self, title, entries, on_choose, note="", note_style="dim"):
        self.title, self.entries, self.on_choose, self.note = title, entries, on_choose, note  # entries: (key, label, why_not)
        self.note_style = note_style
        self.index = next((i for i, e in enumerate(entries) if not e[2]), 0)

    def draw(self, ui):
        width = min(ui.w - 4, max([len(self.title) + 6, min(len(self.note) + 6, 70)]
                                  + [len(label) + 10 + (len(why) + 5 if why else 0) for _, label, why in self.entries]))
        note = wrap(self.note, width - 4) if self.note else []
        # Each entry in as many lines as it needs (a narrow screen cuts no label): its key, then its text.
        rows = []
        for i, (key, label, why_not) in enumerate(self.entries):
            text = label + (f"   ({why_not})" if why_not else "")
            for n, part in enumerate(wrap(text, max(10, width - 9))):
                rows.append((i, (f" {key:>3}  " if n == 0 else " " * 6) + part))
        height = min(ui.h, len(rows) + len(note) + (1 if note else 0) + 2)
        y, x, inner = ui.frame(height, width, self.title, self.danger)
        row = y + 1
        for text in note[: max(0, height - 4)]:
            ui.put(row, x + 2, text, self.note_style, inner - 2)
            row += 1
        if note:
            row += 1
        room = max(1, y + height - 1 - row)
        lines = [n for n, (i, _) in enumerate(rows) if i == self.index]
        first = max(0, (lines[-1] if lines else 0) - room + 1)
        for i, text in rows[first : first + room]:
            style = "cursor" if i == self.index else ("dim" if self.entries[i][2] else "normal")
            ui.put(row, x + 1, ui.cell(text, inner), style, inner)
            row += 1

    def key(self, ui, k):
        chosen = next((i for i, (key, _label, _why) in enumerate(self.entries) if k == key), None)
        if chosen is not None:
            self._choose(ui, chosen)
        elif k in (ESC, "q", curses.KEY_F10):
            ui.close(self)
        elif k in (curses.KEY_UP, "k"):
            self.index = (self.index - 1) % len(self.entries)
        elif k in (curses.KEY_DOWN, "j"):
            self.index = (self.index + 1) % len(self.entries)
        elif k in ENTER:
            self._choose(ui, self.index)

    def _choose(self, ui, i):
        key, label, why_not = self.entries[i]
        if why_not:
            ui.hint(f"{label}: {why_not}", "warn")
            return
        ui.close(self)
        self.on_choose(i)


class Message(Modal):
    """Text to read: a key closes it; long text scrolls (the help)."""

    def __init__(self, title, lines, danger=False):
        self.title, self.lines, self.danger, self.top = title, lines, danger, 0

    def _body(self, ui, width):
        out = []
        for line in self.lines:
            if isinstance(line, tuple):
                text, style = line
            else:
                text, style = line, "normal"
            out.extend((part, style) for part in (wrap(text, width) if text else [""]))
        return out

    def draw(self, ui):
        texts = [line[0] if isinstance(line, tuple) else line for line in self.lines]
        width = min(ui.w - 4, max([len(self.title) + 6] + [len(text) + 4 for text in texts]))
        body = self._body(ui, width - 4)
        height = min(ui.h - 2, len(body) + 3)
        y, x, inner = ui.frame(height, width, self.title, self.danger)
        room = height - 3
        self.top = max(0, min(self.top, len(body) - room))
        for i, (text, style) in enumerate(body[self.top : self.top + room]):
            ui.put(y + 1 + i, x + 2, text, style, inner - 2)
        more = f" {self.top + 1}-{min(len(body), self.top + room)} of {len(body)}: arrows, PgUp/PgDn " if len(body) > room else ""
        ui.put(y + height - 2, x + 2, (more + " any other key closes").strip(), "dim", inner - 2)
        self.room = room

    def key(self, ui, k):
        room = getattr(self, "room", 10)
        moves = {curses.KEY_UP: -1, "k": -1, curses.KEY_DOWN: 1, "j": 1, curses.KEY_PPAGE: -room, curses.KEY_NPAGE: room,
                 " ": room, curses.KEY_HOME: -10**6, curses.KEY_END: 10**6}  # fmt: skip
        if k in moves:
            self.top = max(0, self.top + moves[k])
        elif k != curses.KEY_RESIZE:
            ui.close(self)


def fit_body(lines, width, room, tail=0) -> list:
    """A dialog's lines, wrapped, in `room` lines: the last `tail` (the targets, --dry-run, the PRODUCTION
    line: what must be read before it runs) whole, the start of the rest, `...` for what is left out."""
    def wrapped(part):
        return [(piece, style) for text, style in part for piece in (wrap(text, width) if text else [""])]

    head, end = wrapped(lines[: len(lines) - tail] if tail else lines), wrapped(lines[len(lines) - tail :] if tail else [])
    if len(head) + len(end) <= room:
        return head + end
    if len(end) >= room:
        return end[len(end) - room :]
    keep = max(0, room - len(end) - 1)
    return head[:keep] + [(f"... {len(head) - keep} line(s) more (a wider or taller terminal shows them)", "dim")] + end


class Confirm(Modal):
    """y does it, any other key does not."""

    def __init__(self, title, lines, on_yes, danger=False, yes="run it", no="not run", tail=0):
        self.title, self.lines, self.on_yes, self.danger, self.yes, self.no = title, lines, on_yes, danger, yes, no
        self.tail = tail

    def draw(self, ui):
        width = min(ui.w - 4, max([len(self.title) + 6, 44] + [len(text) + 4 for text, _ in self.lines]))
        body = fit_body(self.lines, width - 4, max(1, ui.h - 6), self.tail)
        y, x, inner = ui.frame(len(body) + 4, width, self.title, self.danger)
        for i, (text, style) in enumerate(body):
            ui.put(y + 1 + i, x + 2, text, style, inner - 2)
        ui.put(y + len(body) + 2, x + 2, f"y: {self.yes}    n, Esc: no", "bold", inner - 2)

    def key(self, ui, k):
        if k == curses.KEY_RESIZE:
            return
        ui.close(self)
        if k in ("y", "Y"):
            self.on_yes()
        else:
            ui.hint(self.no, "dim")


class Prompt(Modal):
    """A line to type: Enter takes it, Esc does not. `word` set: only that word runs it (typed yes)."""

    def __init__(self, title, lines, label, on_done, word="", initial="", danger=False, tail=0):
        self.title, self.lines, self.label, self.on_done, self.word, self.danger = title, lines, label, on_done, word, danger
        self.edit = LineEdit(initial)
        self.tail = tail

    def draw(self, ui):
        width = min(ui.w - 4, max([len(self.title) + 6, 60] + [len(text) + 4 for text, _ in self.lines]))
        body = fit_body(self.lines, width - 4, max(0, ui.h - 8), self.tail)
        y, x, inner = ui.frame(len(body) + 5, width, self.title, self.danger)
        for i, (text, style) in enumerate(body):
            ui.put(y + 1 + i, x + 2, text, style, inner - 2)
        row = y + len(body) + 2
        ui.put(row, x + 2, self.label, "bold", inner - 2)
        start = x + 3 + len(self.label)
        self.edit.draw(ui, row, start, max(4, x + inner - start))
        ui.put(row + 1, x + 2, "Enter: " + ("run it" if self.word else "take it") + "    Esc: not", "dim", inner - 2)

    def key(self, ui, k):
        done = self.edit.key(k)
        if done == "cancel":
            ui.close(self)
            ui.hint("not run", "dim")
        elif done == "enter":
            if self.word and self.edit.text.strip() != self.word:
                ui.close(self)
                ui.hint(f"not run: only `{self.word}` runs it", "warn")
                return
            ui.close(self)
            self.on_done(self.edit.text)


# -- the screen --------------------------------------------------------------------------------------


class UI:
    def __init__(self, scr, backend, tab="vms", ascii_only=False, colours=True):
        self.scr, self.backend = scr, backend
        self.tabs = [cls() for cls in TABS]
        self.columns = {tab.name: tab.columns() for tab in self.tabs}
        self.current = TAB_NAMES.index(tab) if tab in TAB_NAMES else 0
        self.views = {t.name: View(sort=t.default_sort, reverse=t.default_reverse, follow=t.follow) for t in self.tabs}
        self.g = GLYPHS_ASCII if ascii_only else GLYPHS_UTF8
        self.ascii = ascii_only
        self.paint = Paint(colours)
        self.modals: list[Modal] = []
        self.pane = False
        self.pane_top = 0
        self.pane_follow = False
        self.pane_for = None
        self.filter_edit: LineEdit | None = None
        self.message = None  # (text, style, at): a hint of the screen's own, beside the store's status
        self.fetches: OrderedDict = OrderedDict()  # the panes' readings by record, the newest last (FETCHES_KEPT)
        self.pane_key = None  # the record the pane's reading is for, and since when it is under the cursor
        self.pane_since = 0.0
        self.task_states: dict = {}
        self.snap: dict = {}
        self.snap_version = None  # the store's version the snapshot was taken at
        self.snap_at = 0.0
        self.snap_serial = 0  # goes up with each snapshot taken: what is computed from one is kept by it
        self.bar = (-1, [])  # (snap_serial, [(count, attention)] per tab): the tab bar, once per snapshot
        self.drawn_at = 0.0
        self.dirty = True  # a key or a resize: draw at once
        self.wake_at = None  # draw then (a pane's reading starts once its record rested)
        self.now = time.time()
        self.quit = False
        self.h, self.w = scr.getmaxyx()
        self.hits: dict = {}  # where the mouse may click: the tabs, the column heads, the rows
        self.backend_watchers = bool(getattr(backend, "watchers", True))
        self.failed = ""  # the last drawing error said

    # -- drawing primitives -------------------------------------------------------------------------

    def fit(self, text: str) -> str:
        """What the terminal's encoding can show: a non-UTF-8 one gets `?` for the rest."""
        if self.ascii:
            encoding = getattr(self.scr, "encoding", "ascii") or "ascii"
            return text.encode(encoding, "replace").decode(encoding)
        return text

    def put(self, y, x, text, style="normal", width=None, attr=0) -> int:
        """Text at (y, x), cut at the screen's edge (or `width`); the column after it."""
        if y < 0 or y >= self.h or x >= self.w or x < 0:
            return x
        room = self.w - x if width is None else min(width, self.w - x)
        if room <= 0:
            return x
        text = cut(self.fit(str(text)), room)
        try:
            self.scr.addstr(y, x, text, self.paint[style] | attr)
        except curses.error:
            pass  # the bottom right corner: written, and curses says it could not move past it
        return x + width_of(text)

    def cell(self, text: str, width: int, right=False) -> str:
        """`text` in `width` terminal cells (a team's name may have wide characters): cut with an ellipsis, padded."""
        text = self.fit(str(text))
        if width_of(text) > width:
            text = cut(text, max(0, width - 1)) + self.g["more"] if width > 1 else cut(text, width)
        pad = " " * max(0, width - width_of(text))
        return pad + text if right else text + pad

    def frame(self, height, width, title, danger=False) -> tuple[int, int, int]:
        """A dialog's box in the middle of the screen, its inside cleared: (top, left, inner width)."""
        height, width = min(height, self.h), min(width, self.w)
        y, x = max(0, (self.h - height) // 2), max(0, (self.w - width) // 2)
        style = "frame_danger" if danger else "frame"
        for row in range(y, y + height):
            self.put(row, x, " " * width)
        self.hline(y, x + 1, width - 2, style)
        self.hline(y + height - 1, x + 1, width - 2, style)
        for row in range(y + 1, y + height - 1):
            self.line(row, x, "vline", style)
            self.line(row, x + width - 1, "vline", style)
        for cy, cx, corner in ((y, x, "ulcorner"), (y, x + width - 1, "urcorner"), (y + height - 1, x, "llcorner"),
                               (y + height - 1, x + width - 1, "lrcorner")):  # fmt: skip
            self.line(cy, cx, corner, style)
        self.put(y, x + 2, f" {title} ", "frame_danger" if danger else "title", width - 4)
        return y, x, width - 2

    def line(self, y, x, part, style="frame"):
        """One cell of a line: box drawing characters, plain - | + where the encoding is not UTF-8. Not the
        terminal's alternate character set: several terminals (tmux under vt100, a C locale) show it as letters."""
        self.put(y, x, (BOX_ASCII if self.ascii else BOX_UTF8)[part], style)

    def hline(self, y, x, n, style="frame"):
        if n > 0:
            self.put(y, x, (BOX_ASCII if self.ascii else BOX_UTF8)["hline"] * n, style)

    def hint(self, text, style="normal"):
        self.message = (text, style, time.time())

    def open(self, modal):
        self.modals.append(modal)

    def close(self, modal):
        if modal in self.modals:
            self.modals.remove(modal)

    def fetch(self, key, words, again=False) -> Fetch:
        """A pane's read-only `worms <words>` (each is a whole CLI run: seconds of CPU, a database connection):
        one at a time, for the record the pane shows, once it has rested FETCH_SETTLE s under the cursor (not
        one per row passed); the one under way ends when the pane moves on. Run again after FETCH_AGAIN while
        its record moves (`again`: a queued or running job), never while it runs."""
        now = time.time()
        if key != self.pane_key:
            running = self.fetches.get(self.pane_key)
            if running is not None and running.started and not running.done:
                running.stop()
                del self.fetches[self.pane_key]  # an ended reading is no reading: run again when it is back
            self.pane_key, self.pane_since = key, now
        known = self.fetches.get(key)
        if known is None or known.stopped or (again and known.done and now - known.at > FETCH_AGAIN):
            env = self.backend.read_env() if hasattr(self.backend, "read_env") else None
            fresh = Fetch(self.backend.read_argv(words), env, start=False)
            if known is not None and known.lines and not known.stopped:
                fresh.lines = known.lines  # the last reading stays until the new one is there
            self.fetches[key] = known = fresh
        if not known.started:
            if now - self.pane_since >= FETCH_SETTLE:
                known.start()
            else:
                self.wake_at = self.pane_since + FETCH_SETTLE
        self.fetches.move_to_end(key)
        while len(self.fetches) > FETCHES_KEPT:
            _key, old = self.fetches.popitem(last=False)
            old.stop()
        return known

    def stop_fetches(self, block=True):
        for fetch in list(self.fetches.values()):
            fetch.stop(block=block)

    # -- the records --------------------------------------------------------------------------------

    @property
    def tab(self) -> Tab:
        return self.tabs[self.current]

    def shown(self, tab: Tab) -> tuple[list, list]:
        """(every record of the tab, those shown: filtered and sorted as the operator asked)."""
        view = self.views[tab.name]
        records = tab.items(self.snap)
        everyone = records
        _label, keep = tab.quick[view.quick % len(tab.quick)]
        records = [r for r in records if keep(r, self)]
        if view.text:
            needle = view.text.lower()
            columns = self.columns[tab.name]
            records = [r for r in records if any(needle in str(c.text(r, self)).lower() for c in columns)]
        column = next((c for c in self.columns[tab.name] if c.key == view.sort), None)
        if column is not None:
            records.sort(key=lambda r: str(tab.label(r)))
            value = column.sort or column.text
            known = [(value(r, self), r) for r in records]
            present = [pair for pair in known if pair[0] is not None and pair[0] != ""]
            missing = [r for v, r in known if v is None or v == ""]
            try:
                present.sort(key=lambda pair: pair[0], reverse=view.reverse)
            except TypeError:
                present.sort(key=lambda pair: str(pair[0]), reverse=view.reverse)
            records = [r for _, r in present] + missing  # what is not known last, either way
        return everyone, records

    def sync_cursor(self, tab, records):
        """The cursor on the same record as before a refresh; on the same row when it went."""
        view = self.views[tab.name]
        ids = [tab.ident(r) for r in records]
        if view.follow and records:
            view.index = len(records) - 1
        elif view.cursor in ids:
            view.index = ids.index(view.cursor)
        view.index = max(0, min(view.index, len(records) - 1))
        view.cursor = ids[view.index] if records else None

    def targets(self, tab, one=False) -> list[dict]:
        """The marked records, or the cursor's; `one`: the cursor's."""
        everyone, records = self.shown(tab)
        view = self.views[tab.name]
        cursor = next((r for r in records if tab.ident(r) == view.cursor), None)
        if one:
            return [cursor] if cursor else []
        # In the order shown (the commands name them so), then those the filters hide now: marked all the same.
        shown = {tab.ident(r) for r in records}
        marked = [r for r in records if tab.ident(r) in view.marks]
        marked += [r for r in everyone if tab.ident(r) in view.marks and tab.ident(r) not in shown]
        return marked or ([cursor] if cursor else [])

    def hidden(self, tab) -> list[dict]:
        """The marked records the filters hide now: still acted on, so said wherever they are."""
        everyone, records = self.shown(tab)
        view, shown = self.views[tab.name], {tab.ident(r) for r in records}
        return [r for r in everyone if tab.ident(r) in view.marks and tab.ident(r) not in shown]

    def _prune_marks(self):
        """A record gone takes its mark with it, on every tab (not only the one drawn): a VM rebuilt while
        another tab was shown leaves no mark for whichever VM gets its vmid."""
        for tab in self.tabs:
            view = self.views[tab.name]
            if view.marks:
                view.marks &= {tab.ident(r) for r in tab.items(self.snap)}

    # -- the loop ---------------------------------------------------------------------------------------

    def run(self) -> int:
        self.scr.timeout(TICK_MS)
        self.scr.keypad(True)
        self._mouse()
        try:
            while not self.quit:
                try:
                    self.step()
                except KeyboardInterrupt:
                    self.interrupted()
        finally:
            self.stop_fetches()  # a pane's `worms show` does not outlive the GUI
        return 0

    def interrupted(self):
        """Ctrl-C: in a dialog, a menu or the filter, as Esc (it closes, nothing runs); at the table, as q."""
        self.dirty = True
        if self.modals:
            self.modals.pop()
            self.hint("closed (Ctrl-C): nothing run", "dim")
        elif self.filter_edit is not None:
            self._filter_key(ESC)
        else:
            self.ask_quit()

    def step(self):
        """A new snapshot when the store changed (at most every SNAP_EVERY s), a drawing when something did
        (a key, a snapshot, the clock's second), one key or TICK_MS without one. A record in a shape the
        screen does not expect costs its frame, said once in the status line and the Log tab, not the program."""
        now = time.time()
        version = getattr(self.backend, "version", None)
        due = self.wake_at is not None and now >= self.wake_at
        second = int(now) != int(self.drawn_at)  # the clock moved on: one drawing a second when idle
        if second or self.dirty or due:
            # A new snapshot on the second (when the store changed, or every 5 s for the ages), or at once after an
            # action (snap_at 0): one composition and one drawing a second, however often the collectors write.
            moved = version is None or version != self.snap_version or now - self.snap_at >= 5
            if not self.snap or not self.snap_at or (second and moved and now - self.snap_at >= SNAP_EVERY / 2):
                try:
                    self.snap = self.backend.snapshot() or {}
                    self.snap_version, self.snap_at = version, now
                    self.snap_serial += 1
                    self._prune_marks()
                    self._watch_tasks()
                except Exception as exc:  # noqa: BLE001  (the store is ours: still, the screen stays up)
                    self.hint(f"the store: {exc}", "error")
            self.now = now
            self.dirty, self.drawn_at = False, now
            if due:
                self.wake_at = None
            self._draw()
        try:
            key = self.scr.get_wch()
        except curses.error:
            return
        self.dirty = True
        try:
            self.on_key(key)
        except Exception as exc:  # noqa: BLE001  (a key's handler failed: said; the screen and its state stay)
            self.backend.say(f"worms-gui: {key!r}: {type(exc).__name__}: {exc}", "error")

    def _draw(self):
        try:
            self.draw()
        except curses.error:
            pass  # a resize under way: the next frame has the new size
        except Exception as exc:  # noqa: BLE001
            said = f"worms-gui: drawing failed: {type(exc).__name__}: {exc}"
            if said != self.failed:
                self.failed = said
                self.backend.say(said, "error")
            self.scr.erase()
            self.put(0, 0, said[: self.w], "error")
            self.put(1, 0, "q quits; 1-7 another tab; the Log tab has it", "dim")
            self.scr.refresh()

    def _mouse(self):
        try:
            curses.mousemask(curses.BUTTON1_CLICKED | curses.BUTTON1_DOUBLE_CLICKED | curses.BUTTON4_PRESSED
                             | getattr(curses, "BUTTON5_PRESSED", 0))  # fmt: skip
        except curses.error:
            pass

    def _watch_tasks(self):
        """A task's end in the status line (red when it failed)."""
        states = {}
        for task in self.snap.get("tasks") or []:
            state, before = task.get("state"), self.task_states.get(task.get("id"))
            if before is not None and before != state and state in ("done", "failed", "cancelled"):
                rc = task.get("rc")
                self.hint(f"task #{task['id']} {task.get('title')}: {state}" + (f" (exit {rc})" if rc not in (None, 0) else ""),
                          "ok" if state == "done" else "error")  # fmt: skip
            states[task.get("id")] = state
        self.task_states = states  # the tasks the store still has: no other

    # -- keys ----------------------------------------------------------------------------------------

    def on_key(self, k):
        if k == curses.KEY_RESIZE:
            self.resize()
            return
        if self.modals:
            self.modals[-1].key(self, k)
            return
        if self.filter_edit is not None:
            self._filter_key(k)
            return
        if k == curses.KEY_MOUSE:
            self._click()
            return
        tab, view = self.tab, self.views[self.tab.name]
        everyone, records = self.shown(tab)
        self.sync_cursor(tab, records)
        hot = HOT_KEYS.get(tab.name, "") + ("cpP" if tab.name == "tasks" else "")
        pane_full = self.pane and self._pane_full()
        page = max(1, self.hits.get("rows", (0, 10, 0))[1] - self.hits.get("rows", (0, 10, 0))[0])
        if k in ("q", curses.KEY_F10):
            self.ask_quit()
        elif k in ("?", "h", curses.KEY_F1):
            self.open(Message("worms-gui: the keys", help_lines(self)))
        elif isinstance(k, str) and k in "1234567" and k:
            self.switch(int(k) - 1)
        elif k in ("\t", curses.KEY_RIGHT):
            self.switch(self.current + 1)
        elif k in (curses.KEY_BTAB, curses.KEY_LEFT):
            self.switch(self.current - 1)
        elif k in (curses.KEY_UP, "k", curses.KEY_DOWN, "j") and pane_full:
            self.pane_top = max(0, self.pane_top + (-1 if k in (curses.KEY_UP, "k") else 1))
            self.pane_follow = False
        elif k in (curses.KEY_UP, "k"):
            self.move(records, -1)
        elif k in (curses.KEY_DOWN, "j"):
            self.move(records, 1)
        elif k in (curses.KEY_PPAGE, curses.KEY_NPAGE) and self.pane:
            self.pane_top = max(0, self.pane_top + (-1 if k == curses.KEY_PPAGE else 1) * max(1, self.hits.get("pane", 10) - 1))
            self.pane_follow = False
        elif k == curses.KEY_PPAGE:
            self.move(records, -page)
        elif k == curses.KEY_NPAGE:
            self.move(records, page)
        elif k in (curses.KEY_HOME, "g"):
            self.move(records, -len(records) - 1)
        elif k in (curses.KEY_END, "G"):
            self.move(records, len(records) + 1)
        elif k == " " and tab.markable:
            if view.cursor is not None:
                view.marks.symmetric_difference_update({view.cursor})
                self.move(records, 1)
        elif k == "a" and tab.markable:
            view.marks.update(tab.ident(r) for r in records)
            hidden = len(self.hidden(tab))
            self.hint(f"{len(records)} shown marked; {len(view.marks)} in all" + (f", {hidden} of them hidden by the filters: "
                      "acted on too (u unmarks all)" if hidden else ""), "warn" if hidden else "normal")  # fmt: skip
        elif k == "u" and tab.markable:
            view.marks.clear()
            self.hint("nothing marked", "normal")
        elif k in ENTER:
            self.pane = not self.pane
            self.pane_top, self.pane_follow = 0, tab.name == "tasks"
        elif k == ESC:
            if self.pane:
                self.pane = False
            elif view.text or view.quick:
                view.text, view.quick = "", 0
                self.hint("filters cleared", "normal")
        elif k in ("m", curses.KEY_F2):
            self.action_menu(tab)
        elif k == "i":
            self.action_menu(tab, platform=True)
        elif k in ("/", curses.KEY_F3):
            self.filter_edit = LineEdit(view.text)
        elif k in ("f", curses.KEY_F4):
            view.quick = (view.quick + 1) % len(tab.quick)
        elif k == "F":
            view.quick = (view.quick - 1) % len(tab.quick)
        elif k in ("<", ">"):
            self.cycle_sort(tab, -1 if k == "<" else 1)
        elif k == curses.KEY_F6:
            self.sort_menu(tab)
        elif k == "I":
            view.reverse = not view.reverse
        elif k in ("r", curses.KEY_F5):
            refresh = getattr(self.backend, "refresh", None)
            if refresh:
                refresh()
            self.stop_fetches(block=False)
            self.fetches.clear()
            self.pane_key = None
            self.hint("refreshing", "normal")
        elif k == "\x0c":  # Ctrl-L
            self.scr.clear()
        elif isinstance(k, str) and k and k in hot:
            self.hot_key(tab, k)

    def _filter_key(self, k):
        view = self.views[self.tab.name]
        done = self.filter_edit.key(k)
        if done == "cancel":
            view.text = ""
            self.filter_edit = None
        elif done == "enter":
            self.filter_edit = None
        else:
            view.text = self.filter_edit.text

    def _click(self):
        try:
            _id, x, y, _z, state = curses.getmouse()
        except curses.error:
            return
        tab = self.tab
        _everyone, records = self.shown(tab)
        if state & curses.BUTTON4_PRESSED:
            self.move(records, -3)
            return
        if state & getattr(curses, "BUTTON5_PRESSED", 0):
            self.move(records, 3)
            return
        if y == self.hits.get("tabs_y"):
            for x0, x1, index in self.hits.get("tabs", []):
                if x0 <= x < x1:
                    self.switch(index)
            return
        head_y, heads = self.hits.get("head", (None, []))
        if y == head_y:  # a column's head: sorted by it (again: inverted), as htop
            for x0, x1, key in heads:
                if x0 <= x < x1:
                    self.sort_by(tab, key)
            return
        first, last, top = self.hits.get("rows", (0, 0, 0))
        if first <= y < last and top + y - first < len(records):
            view = self.views[tab.name]
            view.index = top + y - first
            view.cursor = tab.ident(records[view.index])
            view.follow = False
            if state & curses.BUTTON1_DOUBLE_CLICKED:
                self.pane, self.pane_top = True, 0

    def switch(self, index):
        self.current = index % len(self.tabs)
        self.pane = False
        self.filter_edit = None

    def move(self, records, delta):
        view = self.views[self.tab.name]
        if not records:
            return
        view.index = max(0, min(len(records) - 1, view.index + delta))
        view.cursor = self.tab.ident(records[view.index])
        view.follow = self.tab.follow and view.index == len(records) - 1
        self.pane_top = 0

    def cycle_sort(self, tab, step):
        view = self.views[tab.name]
        keys = [c.key for c in self.columns[tab.name] if c.title or c.key == tab.default_sort]
        at = keys.index(view.sort) if view.sort in keys else 0
        self.sort_by(tab, keys[(at + step) % len(keys)])

    def sort_by(self, tab, key):
        """Sort by a column: the one sorted by already is inverted; another starts in its own direction (the
        biggest first for CPU%, MEM, NET, ...: Col.desc; the tab's for its default column)."""
        view = self.views[tab.name]
        column = next((c for c in self.columns[tab.name] if c.key == key), None)
        if key == view.sort:
            view.reverse = not view.reverse
        else:
            view.sort = key
            view.reverse = tab.default_reverse if key == tab.default_sort else bool(column and column.desc)
        self.hint(f"sorted by {self._column_title(tab, view.sort)}" + (" (the biggest first)" if view.reverse else ""), "normal")

    def _column_title(self, tab, key):
        column = next((c for c in self.columns[tab.name] if c.key == key), None)
        return (column.title or "the default order") if column else key

    def sort_menu(self, tab):
        view = self.views[tab.name]
        columns = [c for c in self.columns[tab.name] if c.title or c.key == tab.default_sort]
        keys = [k for k in "abcdefghilmnoprstuvwxyz"]  # not j, k (the menu's own up and down) nor q (closes it)
        entries = [(keys[i], (c.title or "the default order") + ("   (now; again: inverted)" if c.key == view.sort else "")
                    + ("   (the biggest first)" if c.desc and c.key != view.sort else ""), "")
                   for i, c in enumerate(columns)]  # fmt: skip

        def choose(i):
            self.sort_by(tab, columns[i].key)

        menu = Menu(f"{tab.title}: sort by", entries, choose)
        menu.index = next((i for i, c in enumerate(columns) if c.key == view.sort), 0)
        self.open(menu)

    def resize(self):
        try:
            curses.update_lines_cols()
        except AttributeError:
            pass
        self.h, self.w = self.scr.getmaxyx()
        self.scr.clear()

    def ask_quit(self):
        running = [t for t in self.snap.get("tasks") or [] if t.get("state") in RUNNING_TASK]
        if not running:
            self.quit = True
            return
        lines = [(f"{len(running)} task(s) still run:", "bold")] + [(f"  #{t['id']} {t.get('title')}", "normal") for t in running[:8]]
        lines += [("", "normal"), ("They are cancelled when worms-gui ends (Ctrl-C to each, then SIGTERM: as `c` in the "
                                   "Tasks tab).", "warn")]  # fmt: skip

        def yes():
            self.quit = True

        self.open(Confirm("quit worms-gui?", lines, yes, danger=True, yes="quit", no="still here"))

    # -- actions -----------------------------------------------------------------------------------

    def _menu(self, name, items):
        """The backend's entries for these records, or a str: why there are none."""
        try:
            return self.backend.menu(name, items, self.snap)
        except Exception as exc:  # noqa: BLE001  (a bug in a menu: said, the screen stays up)
            return f"the {name} menu: {type(exc).__name__}: {exc}"

    def action_menu(self, tab, platform=False):
        """m: the menu for the marked records (or the cursor's); i: the platform's."""
        if tab.name == "tasks" and not platform:
            items = self.targets(tab)
            entries = [(v.key, v.label, "" if items else "no task under the cursor") for v in TASK_VERBS]
            self.open(Menu(f"tasks: {self._names(tab, items)}", entries, lambda i: self.task_verb(tab, TASK_VERBS[i])))
            return
        if tab.name == "log" and not platform:
            self.hint("the Log tab has no actions: i for the platform's (internet, the emergency stop)", "dim")
            return
        name = "platform" if platform else tab.name
        items = [] if platform else self.targets(tab)
        hidden = [] if platform else self.hidden(tab)
        entries = self._menu(name, items)
        if isinstance(entries, str):
            self.hint(entries, "error")
            return
        title = "the platform" if platform else f"{tab.title}: {self._names(tab, items)}"
        note = "" if platform else ("marked: " if self.views[tab.name].marks else "under the cursor: ") + self._names(tab, items, 400)
        if hidden:
            note += f". {len(hidden)} of them hidden by the filters, acted on all the same: {self._names(tab, hidden, 200)}"
        rows = [(field_of(e, "key", "?"), field_of(e, "label", ""), field_of(e, "unavailable", "")) for e in entries]
        menu = Menu(title, rows, lambda i: self.choose(tab, name, entries[i], items, hidden), note=note,
                    note_style="warn" if hidden else "dim")
        menu.danger = platform
        self.open(menu)

    def hot_key(self, tab, key):
        """A table's own key (VMs l login, ! run, L log; Jobs c, p, P): its menu entry, at once."""
        if tab.name == "tasks":
            verb = next(v for v in TASK_VERBS if v.key == key)
            self.task_verb(tab, verb)
            return
        items, hidden = self.targets(tab), self.hidden(tab)
        if key in ONE_KEYS.get(tab.name, ""):
            items, hidden = self.targets(tab, one=True), []
        entries = self._menu(tab.name, items)
        if isinstance(entries, str):
            self.hint(entries, "error")
            return
        entry = find(entries, key)
        if entry is None:
            self.hint(f"{key}: not a key of the {tab.title} tab (m: its menu)", "dim")
            return
        self.choose(tab, tab.name, entry, items, hidden)

    def _names(self, tab, items, room=60) -> str:
        names = [tab.label(r) for r in items]
        text = ", ".join(names)
        if len(text) > room:
            text = text[: room - 10].rsplit(", ", 1)[0] + f", ... ({len(names)})"
        return text or "nothing"

    def choose(self, tab, name, entry, items, hidden=()):
        """An entry chosen: its prompt first when it has one, then its actions, confirmed, run. An entry
        whose scope is not its items (pause the agent, the platform's) names none of them."""
        why = field_of(entry, "unavailable", "")
        label = field_of(entry, "label", "")
        if why:
            self.hint(f"{label}: {why}", "warn")
            return
        own = field_of(entry, "scope", "items") == "items"
        hidden = [tab.label(r) for r in hidden] if own else []
        prompt = field_of(entry, "prompt", "")
        if prompt:
            lines = ([(self._names(tab, items, 300), "normal"), ("", "normal")] if items and own else []) + [(prompt, "dim")]
            self.open(Prompt(label, lines, ">", lambda text: self._make(entry, text, hidden)))
            return
        self._make(entry, "", hidden)

    def _make(self, entry, answer, hidden=()):
        try:
            actions = entry.make(answer)
        except Exception as exc:  # noqa: BLE001  (actions.Unavailable: why not; anything else: a bug, said)
            unavailable = type(exc).__name__ == "Unavailable"
            self.hint(str(exc) if unavailable else f"{field_of(entry, 'label', '')}: {type(exc).__name__}: {exc}",
                      "warn" if unavailable else "error")  # fmt: skip
            return
        actions = [a for a in (actions if isinstance(actions, (list, tuple)) else [actions]) if a]
        if not actions:
            self.hint(f"{field_of(entry, 'label', '')}: nothing to do", "dim")
            return
        if any(field_of(a, "confirm", "") for a in actions):
            self.confirm(actions, hidden)
        else:
            self.execute(actions)

    def system(self) -> str:
        return (self.snap.get("host") or {}).get("system") or ""

    def _typed(self, action) -> str:
        """The word to type to run it: the action's own (actions.py says when); else `yes` for a destructive
        one on production, or on a system not known yet."""
        typed = field_of(action, "typed", None)
        if typed is not None:
            return typed
        return "yes" if field_of(action, "destructive", False) and self.system() != "development" else ""

    def confirm(self, actions, hidden=()):
        """What runs, its question, what is left out; y, or the typed word, runs it. What must be read before
        it runs (the targets, the hidden ones, --dry-run, PRODUCTION) comes last and is never cut off."""
        lines = []
        for action in actions[:6]:
            question = field_of(action, "confirm", "")
            if question and question is not True:
                lines.append((str(question), "bold"))
            lines.append((f"$ {command_line(action)}", "normal"))
            if field_of(action, "note", ""):
                lines.append((field_of(action, "note", ""), "warn"))
            terminal = field_of(action, "mode", "background") == "foreground"
            lines.append(("on the terminal (worms-gui comes back when it ends)" if terminal else "in the background (the Tasks tab)",
                          "dim"))  # fmt: skip
            lines.append(("", "normal"))
        if len(actions) > 6:
            lines.append((f"... and {len(actions) - 6} more", "dim"))
        head = len(lines)
        targets = list(dict.fromkeys(str(t) for a in actions for t in field_of(a, "targets", []) or []))
        if targets:
            shown = ", ".join(targets)
            if len(shown) > 400:
                shown = shown[:380].rsplit(", ", 1)[0] + f", ... ({len(targets)})"
            lines.append((f"{len(targets)} target(s): {shown}", "normal"))
        if hidden:
            lines.append((f"Given to it too, though the filters hide them now: {', '.join(hidden[:12])}"
                          + (f" and {len(hidden) - 12} more" if len(hidden) > 12 else ""), "warn"))
        if self.backend.dry_run:
            lines.append(("--dry-run: it is shown, not run", "warn"))
        destructive = any(field_of(a, "destructive", False) for a in actions)
        title = "run it?" if len(actions) == 1 else f"run these {len(actions)}?"
        word = next((self._typed(a) for a in actions if self._typed(a)), "")
        if word and not self.backend.dry_run:
            where = (self.system() or "a system not known yet").upper()
            lines.append(("", "normal"))
            lines.append((f"This is {where}: type `{word}` to run it.", "error"))
            self.open(Prompt(title, lines, f"type {word}:", lambda text: self.execute(actions), word=word, danger=True,
                             tail=len(lines) - head))
            return
        self.open(Confirm(title, lines, lambda: self.execute(actions), danger=destructive, tail=len(lines) - head))

    def execute(self, actions):
        """The background ones as tasks; the foreground ones on the terminal, one after the other. The next
        snapshot is taken at once: the task is in the Tasks tab for the next key."""
        self.snap_at = 0.0
        terminal = [a for a in actions if field_of(a, "mode", "background") == "foreground"]
        for action in [a for a in actions if field_of(a, "mode", "background") != "foreground"]:
            try:
                task = self.backend.start(action)
            except Exception as exc:  # noqa: BLE001
                self.backend.say(f"could not start `{command_line(action)}`: {exc}", "error")
                continue
            if task is not None:
                self.hint(f"started #{task}: $ {command_line(action)}   (6: the Tasks tab)", "ok")
        if terminal:
            self.foreground(terminal)

    def foreground(self, actions):
        """Leave curses, the backend runs them on the terminal and waits for a key; back as it was.
        With --dry-run they are only said: the screen stays."""
        if self.backend.dry_run:
            self.backend.foreground(actions)
            return
        curses.def_prog_mode()
        curses.endwin()
        rc = None
        try:
            rc = self.backend.foreground(actions)
        except Exception as exc:  # noqa: BLE001  (said on the terminal; the screen comes back)
            sys.stdout.write(f"\nworms-gui: could not run it: {exc}\n[press a key]")
            sys.stdout.flush()
            _wait_key()
        finally:
            curses.reset_prog_mode()
            try:
                curses.curs_set(0)
                self.scr.refresh()  # back in curses now, not at the next drawing: a SystemExit (HOLD) leaves through endwin
            except curses.error:
                pass
            self._mouse()
            self.resize()
        shown = "; ".join(command_line(a) for a in actions)
        interrupted = rc in (-signal.SIGINT, 130)  # Ctrl-C: how a followed log ends, not a failure
        ended = "done" if rc == 0 else "ended by Ctrl-C" if interrupted else f"exit {rc}" if rc is not None else "not run"
        self.hint(f"$ {shown}: {ended}", "ok" if rc == 0 else "normal" if interrupted else "error")

    def task_verb(self, tab, verb):
        tasks = [t for t in self.targets(tab) if t.get("state") in RUNNING_TASK]
        if not tasks:
            self.hint(f"{verb.name}: no running task marked or under the cursor", "warn")
            return

        def go():
            done = []
            self.snap_at = 0.0  # the tasks' new states in the next frame
            for task in tasks:
                why = self.backend.task_signal(task["id"], verb.name)
                if isinstance(why, str) and why:
                    self.hint(f"task #{task['id']}: {why}", "warn")
                elif why is False:
                    self.hint(f"task #{task['id']}: it does not run", "warn")
                else:
                    done.append(f"#{task['id']}")
            if done:
                self.hint(f"{verb.name}: {', '.join(done)}", "ok")

        if verb.name == "cancel":
            lines = [(f"#{t['id']} {t.get('title')}", "normal") for t in tasks[:10]] + [("", "normal"), (verb.label, "dim")]
            self.open(Confirm(f"cancel {len(tasks)} task(s)?", lines, go, danger=True, yes="cancel them", no="not cancelled"))
        elif verb.name == "pause" and any(t.get("locks") for t in tasks):
            locking = [t for t in tasks if t.get("locks")]
            lines = [(f"#{t['id']} {t.get('title')}", "normal") for t in tasks[:10]] + [("", "normal")]
            lines.append((f"{len(locking)} of them hold the host's lock (/run/worms/worms.lock) while they run: paused, they keep "
                          "it, and every other `worms` that changes the platform (apply, internet, vm ...) is refused or waits "
                          "until P resumes them. If worms-gui is killed meanwhile, they stay stopped with the lock.", "warn"))
            self.open(Confirm(f"pause {len(tasks)} task(s)?", lines, go, yes="pause them", no="not paused"))
        else:
            go()

    # -- drawing -----------------------------------------------------------------------------------------

    def draw(self):
        self.h, self.w = self.scr.getmaxyx()
        self.scr.erase()
        if self.h < 12 or self.w < 50:
            for i, text in enumerate(("worms-gui needs 50x12;", f"this is {self.w}x{self.h}.", "q quits")):
                self.put(i, 0, text, "warn")
            self.scr.refresh()
            return
        y = self.draw_header()
        self.draw_tabs(y)
        tab = self.tab
        everyone, records = self.shown(tab)
        self.sync_cursor(tab, records)
        self.draw_info(y + 1, tab, everyone, records)
        self.draw_body(y + 2, self.h - 2, tab, records)
        self.draw_status(self.h - 2)
        self.draw_keys(self.h - 1, tab)
        for modal in self.modals:
            modal.draw(self)
        self.scr.refresh()

    def meter(self, y, x, width, label, fraction, text, parts=None, old=False):
        """htop's meter: CPU[||||||||        23.4%]; parts: [(fraction, style)] stacked; `old`: the host's data is
        stale: dim, and the text says so."""
        x = self.put(y, x, label, "title")
        x = self.put(y, x, "[", "bold")
        room = max(0, width - len(label) - 2)
        if parts is None:
            style = "bar" if fraction is None or fraction < 0.75 else "bar_warn" if fraction < 0.9 else "bar_error"
            parts = [(fraction or 0, style)]
        cells = []
        for share, style in parts:
            cells += [("dim" if old else style)] * int(round(max(0.0, min(1.0, share)) * room))
        cells = cells[:room]
        text = (f"stale {text}" if old else text)[:room]
        start = room - len(text)
        for i in range(room):
            if i >= start:
                self.put(y, x + i, text[i - start], "bold" if fraction is not None and not old else "dim")
            elif i < len(cells):
                self.put(y, x + i, self.g["bar"], cells[i])
        self.put(y, x + room, "]", "bold")

    def draw_header(self) -> int:
        host = self.snap.get("host") or {}
        wide = self.w >= 110
        cpu = host.get("cpu_pct")
        total, available = host.get("mem_total"), host.get("mem_available")
        used = (total - available) if total and available is not None else None
        arc = host.get("arc_bytes") or 0
        mem_parts = None
        if used is not None:
            cache = min(arc, used)
            mem_parts = [((used - cache) / total, "bar"), (cache / total, "bar_cache")]
        mem_text = f"{size(used)}/{size(total)}" if used is not None else "?"
        some, full = host.get("psi_some_avg10"), host.get("psi_full_avg10")
        psi_text = f"some {some:.1f}% full {full:.1f}%" if some is not None and full is not None else "?"
        cpu_text = f"{cpu:.1f}%" if cpu is not None else "?"
        old = stale(self, "host")
        if wide:
            left = self.w * 45 // 100
            self.meter(0, 1, left - 2, "CPU", None if cpu is None else cpu / 100, cpu_text, old=old)
            self.meter(1, 1, left - 2, "Mem", None if used is None else used / total, mem_text, mem_parts, old=old)
            psi_style = None if some is None else ("bar_error" if (full or 0) >= 5 else "bar_warn" if some >= 10 else "bar")
            self.meter(2, 1, left - 2, "PSI", None if some is None else some / 100, psi_text,
                       None if some is None else [(some / 100, psi_style)], old=old)  # fmt: skip
            self.draw_host_lines(0, left + 1, self.w - left - 2, host)
            y = 3
        else:
            half = self.w // 2
            self.meter(0, 1, half - 2, "CPU", None if cpu is None else cpu / 100, cpu_text, old=old)
            self.meter(0, half, self.w - half - 1, "Mem", None if used is None else used / total, mem_text, mem_parts, old=old)
            self.draw_host_lines(1, 1, self.w - 2, host, compact=True)
            y = 2
        return self.draw_badges(y, 2 if self.h >= 24 else 1)

    def draw_host_lines(self, y, x, width, host, compact=False):
        now = self.now
        system = host.get("system") or ""
        name = host.get("name") or "?"
        x0 = x
        if system == "production":
            x = self.put(y, x, " PRODUCTION ", "prod")
        elif system == "development":
            x = self.put(y, x, " development ", "dev")
        else:
            x = self.put(y, x, " system ? ", "dim")
        x = self.put(y, x + 1, name, "bold")
        load = host.get("load")
        load_text = " ".join(f"{v:.2f}" for v in load) if load else "?"
        clock = time.strftime("%H:%M:%S UTC", time.gmtime(now))
        right = (" DRY RUN " if self.backend.dry_run else "") + " " + clock
        public = f" ({host.get('public_name')})" if host.get("public_name") else ""
        if public and not compact and x + len(public) < x0 + width - len(right):  # only whole, not under the clock
            x = self.put(y, x, public, "dim")
        if compact:
            # What fits before DRY RUN and the clock, whole: the load (its 1-minute value when the three do not
            # fit), then PSI, then the uptime; never drawn under them.
            end = x0 + width - len(right) - 1
            some = host.get("psi_some_avg10")
            up = span(host["uptime"]) if host.get("uptime") else ""
            psi = f"{some:.1f}%" if some is not None else ""
            parts = [("load ", load_text, "normal"), ("PSI ", psi, "warn" if (some or 0) >= 10 else "normal"), ("up ", up, "normal")]
            if load and x + 2 + len("load ") + len(load_text) > end:
                parts[0] = ("load ", f"{load[0]:.2f}", "normal")
            for label, value, style in parts:
                if not value or x + 2 + len(label) + len(value) > end:
                    break
                x = self.put(y, x + 2, label, "label")
                x = self.put(y, x, value, style)
        else:
            x = self.put(y + 1, x0, "Load average: ", "label")
            x = self.put(y + 1, x, load_text, "bold")
            if host.get("cpus"):
                x = self.put(y + 1, x, f"  ({host['cpus']} CPUs)", "dim")
            x = self.put(y + 1, x + 2, "Uptime: ", "label")
            x = self.put(y + 1, x, span(host.get("uptime"), long=True) if host.get("uptime") else "?", "bold")
            vms = list((self.snap.get("vms") or {}).values())
            counts = {}
            for vm in vms:
                counts[vm.get("status")] = counts.get(vm.get("status"), 0) + 1
            x = self.put(y + 2, x0, "VMs: ", "label")
            x = self.put(y + 2, x, str(len(vms)), "bold")
            first = True
            for status, style in (("running", "ok"), ("paused", "paused"), ("stopped", "dim")):
                if counts.get(status):
                    x = self.put(y + 2, x, ": " if first else ", ", "dim")
                    x = self.put(y + 2, x, f"{counts[status]} {status}", style)
                    first = False
            x = self.put(y + 2, x + 2, "avail ", "label")
            x = self.put(y + 2, x, size(host.get("mem_available")))
            if host.get("ksm_sharing_bytes") is not None:
                x = self.put(y + 2, x + 2, "KSM ", "label")
                x = self.put(y + 2, x, size(host["ksm_sharing_bytes"]))
            if host.get("arc_bytes") is not None:
                x = self.put(y + 2, x + 2, "ARC ", "label")
                self.put(y + 2, x, size(host["arc_bytes"]), "bar_cache")
        if self.backend.dry_run:
            self.put(y, x0 + width - len(right), " DRY RUN ", "dry")
        self.put(y, x0 + width - len(clock), clock, "bold")

    def badges(self, short=False) -> list[tuple[str, str]]:
        """The platform's state in a few words each, what `worms status` puts first; `short`: fewer words. What
        comes from the host while its source is stale is dim, after a badge that says how old it is."""
        out = self._host_badges(short)
        if stale(self, "host"):
            age = ((self.snap.get("sources") or {}).get("host") or {}).get("age_s")
            said = f"HOST DATA {span(age)} OLD" if not short else f"host {span(age)} old"
            out = [(said, "warn")] + [(text, "dim") for text, _ in out]
        return out + self._other_badges(short)

    def _host_badges(self, short) -> list[tuple[str, str]]:
        host = self.snap.get("host") or {}
        out = []
        internet = host.get("internet")
        out.append({"allowed": ("internet ALLOWED", "error"), "denied": ("internet denied", "ok")}.get(internet,
                                                                                                    ("internet ?", "dim")))
        cluster = host.get("cluster_firewall")
        word = "cl. fw" if short else "cluster fw"
        out.append({True: (f"{word} on", "ok"), False: (f"{word} OFF", "error")}.get(cluster, (f"{word} ?", "dim")))
        units = host.get("units") or {}
        known = [unit for unit in HOST_FIREWALL if units.get(unit)]
        down = [unit for unit in known if units[unit] != "active"]
        if down:
            out.append((f"host fw: {' '.join(down)} DOWN", "error"))
        elif known:
            out.append(("host fw on", "ok") if len(known) == len(HOST_FIREWALL) else ("host fw on (partly known)", "warn"))
        else:
            out.append(("host fw ?", "dim"))
        watchdog = host.get("watchdog") or {}
        age = watchdog.get("last_run_age_s")
        result = watchdog.get("result") or ""
        word = "wd" if short else "watchdog"
        if result == "success":
            late = age is not None and age > 4 * WATCHDOG_EVERY
            text = f"{word} ok" + (f" {span(age)}" + ("" if short else " ago") if age is not None else "")
            out.append((text, "warn" if late else "ok"))
        elif result:
            out.append((f"{word} FAILED ({result})", "error"))
        else:
            out.append((f"{word} ?", "dim"))
        agent = units.get(AGENT_UNIT)
        if host.get("agent_paused"):
            out.append(("agent PAUSED" + ("" if short else f": {host['agent_paused'][:40]}"), "paused"))
        elif agent == "active":
            out.append(("agent active", "ok"))
        elif agent:
            out.append((f"agent DOWN ({agent})", "error"))
        else:
            out.append(("agent ?", "dim"))
        held, window = host.get("held_devboxes") or [], host.get("window_stopped") or []
        if held:
            out.append((f"held {len(held)}" if short else f"devboxes held: {len(held)}", "warn"))
        if window:
            out.append((f"window-stopped {len(window)}", "warn"))
        if host.get("lock"):
            out.append((f"lock: {host['lock'][:30 if short else 50]}", "warn"))
        return out

    def _other_badges(self, short) -> list[tuple[str, str]]:
        now, out = self.now, []
        run = self.snap.get("run")
        current = next((r for r in self.snap.get("rounds") or [] if r.get("current")), None)
        if run:
            left = f", {span(run['ends_at'] - now)} left" if run.get("ends_at") else ""
            where = "" if short else f"{run.get('round') or ''} "
            out.append((f"run: {where}{run.get('team') or '?'} {run.get('state')} #{run.get('attempt')}{left}", "ok"))
        elif current:
            out.append((f"round: {current.get('name')}" + (" ON HOLD" if current.get("on_hold") else ""),
                        "paused" if current.get("on_hold") else "ok"))  # fmt: skip
        sources = self.snap.get("sources") or {}
        errors = [name for name, s in sources.items() if s.get("error")]
        waiting = [name for name, s in sources.items() if not s.get("updated_at") and not s.get("error")]
        old = [name for name, s in sources.items() if s.get("stale") and s.get("updated_at") and not s.get("error") and name != "host"]
        if errors:
            out.append((f"no answer: {', '.join(errors)}", "error"))
        if old:
            out.append((f"stale: {', '.join(old)}", "warn"))
        if waiting:
            out.append((f"waiting: {', '.join(waiting)}", "dim"))
        return out

    def draw_badges(self, y, rows) -> int:
        """The badges in `rows` lines, always as many (the table below does not jump); `~` when they do not fit."""
        x, used = 1, 1
        for text, style in self.badges(short=self.w < 110):
            width = len(text) + 2
            if x + width > self.w - 1 and x > 1:
                if used == rows:
                    self.put(y, min(x, self.w - 2), self.g["more"], "dim")
                    break
                y, x, used = y + 1, 1, used + 1
            self.put(y, x, "[", "dim")
            self.put(y, x + 1, text, style)
            self.put(y, x + 1 + len(text), "]", "dim")
            x += width + 1
        return y + 1 + (rows - used)

    def draw_tabs(self, y):
        x = 0
        self.hits["tabs"], self.hits["tabs_y"] = [], y
        short = self.w < 100
        if self.bar[0] != self.snap_serial:  # each tab's records counted once per snapshot, not at every drawing
            self.bar = (self.snap_serial, [(tab.count(self.snap, items), tab.attention(self.snap, items, self))
                                           for tab in self.tabs for items in [tab.items(self.snap)]])  # fmt: skip
        for i, tab in enumerate(self.tabs):
            count, attention = self.bar[1][i]
            start = x
            on = "tab_on" if i == self.current else "tab"
            x = self.put(y, x, f"{i + 1}{tab.short} {count}" if short else f" {i + 1} {tab.title} {count}", on)
            if attention:
                x = self.put(y, x, "!", "error")
            x = self.put(y, x, " ", on)
            self.hits["tabs"].append((start, x, i))
            if i < len(self.tabs) - 1 and not short:
                x = self.put(y, x, self.g["sep"], "dim")
        self.hline(y, x, self.w - x, "dim")

    def draw_info(self, y, tab, everyone, records):
        view = self.views[tab.name]
        parts = [(f" {tab.title}: {len(everyone)}", "bold")]
        if len(records) != len(everyone):
            parts.append((f", {len(records)} shown", "normal"))
        quick = tab.quick[view.quick % len(tab.quick)][0]
        if view.quick:
            parts.append((f"   f: {quick}", "warn"))
        if view.text:
            parts.append((f"   /{view.text}", "warn"))
        arrow = self.g["up"] if not view.reverse else self.g["down"]
        parts.append((f"   sort: {self._column_title(tab, view.sort)} {arrow}", "dim"))
        if view.marks:
            hidden = len(view.marks) - sum(1 for r in records if tab.ident(r) in view.marks)
            parts.append((f"   {len(view.marks)} marked", "marked"))
            if hidden:
                parts.append((f" ({hidden} hidden by the filters: acted on too)", "warn"))
        if tab.source:
            source = (self.snap.get("sources") or {}).get(tab.source) or {}
            if source.get("error"):
                parts.append((f"   {tab.source}: {source['error']}", "error"))
            elif source.get("updated_at"):
                parts.append((f"   {tab.source} {span(source.get('age_s'))} ago" + (" STALE" if source.get("stale") else ""),
                              "warn" if source.get("stale") else "dim"))  # fmt: skip
            else:
                parts.append((f"   waiting for {tab.source}", "dim"))
        x = 0
        for text, style in parts:
            x = self.put(y, x, text, style)
        position = self.hits.get("position") or ""  # the last frame's: the table is drawn after this line
        if position and x + len(position) + 2 < self.w:
            self.put(y, self.w - len(position) - 1, position, "dim")

    def _pane_full(self) -> bool:
        return (self.h - 2) - (self.hits.get("body_top", 0)) < 18

    def draw_body(self, top, bottom, tab, records):
        self.hits["body_top"] = top
        view = self.views[tab.name]
        cursor = records[view.index] if records else None
        if self.pane and cursor is not None:
            if self._pane_full():
                self.hits["rows"], self.hits["head"] = (0, 0, 0), (None, [])  # no table drawn: nothing to click there
                self.draw_pane(top, bottom, tab, cursor)
                return
            middle = top + max(5, (bottom - top) * 2 // 5)
            self.draw_table(top, middle, tab, records)
            self.draw_pane(middle, bottom, tab, cursor)
            return
        if self.pane and cursor is None:
            self.pane = False
        self.draw_table(top, bottom, tab, records)

    def draw_table(self, top, bottom, tab, records):
        view = self.views[tab.name]
        columns = fit_columns(self.columns[tab.name], self.w - 2, records, self)
        x = 0
        self.put(top, 0, " " * self.w, "head")
        x = self.put(top, x, "  ", "head")
        heads = []
        for col, width in columns:
            if not width:
                continue
            title = col.title
            sorted_here = col.key == view.sort
            if sorted_here:
                title = (title + (self.g["down"] if view.reverse else self.g["up"]))
            self.put(top, x, self.cell(title, width, col.right), "head_sorted" if sorted_here else "head")
            heads.append((x, x + width + 1, col.key))
            x += width + 1
        self.hits["head"] = (top, heads)
        rows = bottom - top - 1
        if rows <= 0:
            return
        if view.index < view.top:
            view.top = view.index
        elif view.index >= view.top + rows:
            view.top = view.index - rows + 1
        view.top = max(0, min(view.top, max(0, len(records) - rows)))
        self.hits["rows"] = (top + 1, bottom, view.top)
        if not records:
            filtered = (view.text or view.quick) and tab.items(self.snap)  # there are records: the filters hide them
            self.put(top + 2, 2, "(none shown: the filters hide them all; Esc clears them)" if filtered else tab.empty(self), "dim")
            return
        for i, record in enumerate(records[view.top : view.top + rows]):
            y = top + 1 + i
            index = view.top + i
            ident = tab.ident(record)
            marked = ident in view.marks
            on_cursor = index == view.index
            base = tab.row_style(record, self)
            if on_cursor:
                self.put(y, 0, " " * self.w, "cursor_marked" if marked else "cursor")
            x = self.put(y, 0, self.g["mark"] if marked else " ", "cursor_marked" if on_cursor and marked else "cursor" if on_cursor
                         else "marked")  # fmt: skip
            x += 1
            for col, width in columns:
                if not width:
                    continue
                text = self.cell(col.text(record, self), width, col.right)
                if on_cursor:
                    style = "cursor_marked" if marked else "cursor"
                elif base == "dim":
                    style = "dim"
                elif marked and col.key == "name":
                    style = "marked"
                else:
                    style = (col.style(record, self) if col.style else None) or base or "normal"
                self.put(y, x, text, style)
                x += width + 1
        last = min(len(records), view.top + rows)
        self.hits["position"] = f"rows {view.top + 1}-{last} of {len(records)}" if len(records) > rows else ""

    def draw_pane(self, top, bottom, tab, record):
        ident = (tab.name, tab.ident(record))
        if ident != self.pane_for:
            self.pane_for, self.pane_top = ident, 0
            self.pane_follow = tab.name == "tasks"
        self.hline(top, 0, self.w, "frame")
        title = f" {tab.label(record)} "
        self.put(top, 2, title, "title")
        keys = " Enter/Esc close   PgUp/PgDn scroll " + ("  arrows scroll " if self._pane_full() else "  arrows: the next one ")
        lines = []
        width = self.w - 2
        try:
            content = tab.detail(record, self)
        except Exception as exc:  # noqa: BLE001  (a record in a shape not foreseen: said, not a crash)
            content = [[(f"(cannot show it: {type(exc).__name__}: {exc})", "error")]]
        for line in content:
            lines.extend(self._wrap_segments(line, width))
        rows = bottom - top - 1
        self.hits["pane"] = rows
        if self.pane_follow:
            self.pane_top = max(0, len(lines) - rows)
        self.pane_top = max(0, min(self.pane_top, max(0, len(lines) - rows)))
        if self.pane_top >= max(0, len(lines) - rows) and tab.name == "tasks":
            self.pane_follow = True
        for i, segments in enumerate(lines[self.pane_top : self.pane_top + rows]):
            x = 1
            for text, style in segments:
                x = self.put(top + 1 + i, x, text, style or "normal", self.w - 1 - x)
        # On the pane's top rule, beside its keys: never over a line of it.
        more = f" {self.pane_top + 1}-{min(len(lines), self.pane_top + rows)}/{len(lines)} " if len(lines) > rows else ""
        self.put(top, max(len(title) + 4, self.w - len(keys) - len(more) - 2), more + keys, "dim")

    @staticmethod
    def _wrap_segments(segments, width) -> list:
        """One pane line as screen lines: cut at `width`, continued under its value (after its label)."""
        if not segments:
            return [[]]
        indent = len(segments[0][0]) if len(segments) > 1 and segments[0][1] == "label" else 2
        indent = indent if indent < width // 2 else 2
        out, line, room = [], [], width
        for text, style in segments:
            for word in re.findall(r"\s*\S+|\s+", str(text)):  # each word with the spaces before it
                if len(word) > room and len(line) > (1 if out else 0):
                    out.append(line)
                    line, room, word = [(" " * indent, "normal")], width - indent, word.lstrip()
                while len(word) > room:  # longer than a line: cut
                    line.append((word[:room], style))
                    out.append(line)
                    line, word = [(" " * indent, "normal")], word[room:]
                    room = width - indent
                line.append((word, style))
                room -= len(word)
        out.append(line)
        return out

    def draw_status(self, y):
        status = self.snap.get("status") or None
        candidates = []
        if status:
            level = status.get("level")
            candidates.append((status.get("text") or "", "normal" if level == "info" else level, status.get("at") or 0))
        if self.message:
            candidates.append(self.message)
        # a message for HINT_SECONDS, an error three times as long
        candidates = [c for c in candidates if self.now - c[2] < HINT_SECONDS * (3 if c[1] == "error" else 1)]
        if self.filter_edit is not None:
            x = self.put(y, 0, " filter: ", "bold")
            self.filter_edit.draw(self, y, x, max(10, self.w - x - 40))
            self.put(y, self.w - 38, "Enter: keep it   Esc: clear it", "dim")
            return
        if candidates:
            text, style, _at = max(candidates, key=lambda c: c[2])
            self.put(y, 0, " " + clean(text), style or "normal", self.w)
            return
        sources = self.snap.get("sources") or {}
        x = self.put(y, 0, " ", "normal")
        for name, s in sources.items():
            if s.get("error"):
                text, style = f"{name}: {s['error'][:60]}", "error"
            elif not s.get("updated_at"):
                text, style = f"{name} waiting", "dim"
            else:
                text, style = f"{name} {span(s.get('age_s'))}", "warn" if s.get("stale") else "dim"
            x = self.put(y, x, text, style)
            x = self.put(y, x, f" {self.g['dot']} ", "dim")
        watched = [vm for vm in (self.snap.get("vms") or {}).values() if vm.get("watch")]
        if watched:
            fresh = sum(1 for vm in watched if not vm["watch"].get("stale"))
            text = f"VMs watched {fresh}/{len(watched)}" + (f" ({len(watched) - fresh} stale)" if fresh < len(watched) else "")
            self.put(y, x, text, "dim" if fresh == len(watched) else "warn")

    def draw_keys(self, y, tab):
        """The keys of this tab, and those of every tab: what a narrow screen cannot show goes from the end
        (F3-F6 first: the tab's own keys and Enter matter more there)."""
        own = {"vms": [("l", "Login"), ("!", "Run"), ("L", "Log")],
               "jobs": [("c", "Cancel"), ("p", "PauseAgent"), ("P", "Resume"), ("L", "Log")],
               "rounds": [("p", "Pause"), ("P", "Resume"), ("s", "Start"), ("c", "Cancel")],
               "services": [("L", "Log")],
               "tasks": [("c", "Cancel"), ("p", "Pause"), ("P", "Resume")]}.get(tab.name, [])  # fmt: skip
        keys = [("F1", "Help"), ("F2", "Menu")] + ([("Spc", "Mark")] if tab.markable else []) + own
        keys += [("Ent", "Pane"), ("i", "Net"), ("F3", "Filter"), ("F4", "Quick"), ("F6", "Sort"), ("F5", "Refresh")]
        quit_room = len("F10Quit ") + 1
        x = 0
        for key, label in keys:
            if x + len(key) + len(label) + 1 > self.w - quit_room:
                break  # the rest goes; how to quit is always said
            x = self.put(y, x, key, "fkey")
            x = self.put(y, x, label.ljust(max(len(label), 6 if self.w >= 120 else len(label))) + " ", "fkey_label")
        x = self.put(y, x, "F10", "fkey")
        x = self.put(y, x, "Quit ", "fkey_label")
        self.put(y, x, " " * (self.w - x), "fkey_label")


def help_lines(ui) -> list:
    """The help: every key, the menus' keys as the menus have them (actions.menu: they cannot drift apart),
    then what the columns and the colours say."""
    H = "bold"
    lines = [
        ("Everywhere", H),
        ("1-7, Tab, Shift-Tab, Left/Right   the tabs: VMs, Jobs, Rounds, Services, Components, Tasks, Log", "normal"),
        ("Up/Down, j/k, PgUp/PgDn, Home/End   the cursor (g, G: the first, the last)", "normal"),
        ("Space   mark (and down)      a   mark all shown      u   unmark all   (marks stay across refreshes; marks the "
         "filters hide are acted on too, and the info line says how many)", "normal"),
        ("Enter   the detail pane of the cursor's record (again, or Esc: closed); PgUp/PgDn scroll it; its `worms show` "
         "runs once the cursor rests on a record", "normal"),
        ("m, F2   the action menu for the marked records, or the cursor's", "normal"),
        ("i       the platform's menu (below)", "normal"),
        ("/, F3   filter by text (as you type; Enter keeps it, Esc clears it)", "normal"),
        ("f, F4   the quick filters, in turn (F: back)        Esc   clears the filters", "normal"),
        ("<  >    sort by the column before / after      F6   choose the column      I   invert      a click on a column's "
         "head sorts by it; CPU%, MEM, NET, DISK, up, activity sort the biggest first", "normal"),
        ("r, F5   refresh now      Ctrl-L   redraw      ?, h, F1   this help", "normal"),
        ("q, F10, Ctrl-C   quit (asks while tasks run); in a menu, a dialog or the filter Ctrl-C is Esc: it closes, nothing runs",
         "normal"),
        ("mouse   click: a row, a tab, a column's head; double click: its pane; the wheel scrolls", "normal"),
    ]
    one = {"vms": "l and L: the cursor's VM; ! and #: the marked ones (platform VMs only)", "jobs": "single keys c, p, P, L",
           "rounds": "single keys p, P, s, c", "services": "single key L"}
    for name, title in (("vms", "VMs"), ("jobs", "Jobs (the round agent's host jobs)"), ("rounds", "Rounds"), ("services", "Services"),
                        ("components", "Components"), ("platform", "The platform (i, on every tab)")):  # fmt: skip
        lines += [("", "normal"), (title + (f"   ({one[name]})" if name in one else "   (m: its menu)"), H)]
        entries = ui._menu(name, [])
        for entry in entries if not isinstance(entries, str) else []:
            lines.append((f"{field_of(entry, 'key', '?'):>3}  {field_of(entry, 'label', '')}", "normal"))
    lines += [
        ("", "normal"),
        ("Tasks (what worms-gui runs in the background)", H),
        *[(f"{v.key:>3}  {v.label}", "normal") for v in TASK_VERBS],
        ("     the pane follows its output", "normal"),
        ("", "normal"),
        ("The VMs' columns", H),
        ("FW: on when the VM's firewall option is on, the host has its guest rules (a running VM) and every NIC has "
         "firewall=1; the monitor's capture NIC on the mirror bridge has none by design and counts while worms-mirror is "
         "active (only a platform VM's). OFF if any part is not; ? not known yet. The pane says which part; dim: the host's "
         "data is stale", "normal"),
        ("clean: a service VM has its `clean` snapshot (a reset returns to it). fail: its failed systemd units (its watcher)",
         "normal"),
        ("activity: a platform VM's watcher: load, steal (time the host did not give it) when it is high, the busiest process; "
         "a Proxmox task under way; a team VM or a gateway (assumed rooted, never logged in to by a watcher): the host's view, "
         "its CPU pressure", "normal"),
        ("state: STOPPED (capitals) is a platform VM that should run; held / window: a devbox stopped by a round or by the "
         "internet window", "normal"),
        ("quick filters: all, platform (trusted VMs), team VMs (and gateways), sandbox, devbox, running, stopped", "normal"),
        ("", "normal"),
        ("Commands", H),
        ("Every command is the CLI's own (`worms ...`, src/manage.py) with its checks, lock and run log. What changes something "
         "asks first, naming its targets. Typed `yes` (not y): on production (or a system not known yet), vm stop, restart, "
         "reset, rebuild, jobs cancel, round cancel, service restart --during-round, internet on and apply; vm kill always. "
         "login, run, log, show, apply, reset, rebuild, kill, status and internet run on the terminal: worms-gui comes back "
         "when they end (after a login at once, else after a key). The rest run in the background: the Tasks tab, their end "
         "in the status line.", "normal"),
        ("--dry-run: no action runs, each is only shown (status line, Log tab); the panes' `worms show` and the watchers "
         "still read.", "normal"),
        ("", "normal"),
        ("Colours", H),
        ("green ok, red an error or a VM down that should run, yellow a warning, cyan paused or held, dim stale or not known; "
         "without colours the words say it (OFF, STOPPED, ALLOWED, PAUSED)", "normal"),
        ("All times are UTC, as the CLI and the database give them.", "normal"),
    ]
    if ui.backend.dry_run:
        lines.insert(0, ("DRY RUN: no action runs", "warn"))
    return lines


# -- start -------------------------------------------------------------------------------------------


def terminal_problem() -> str:
    """Why curses cannot draw on this terminal ("" when it can): a dumb terminal places no cursor."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return "worms-gui needs a terminal (stdin and stdout); `worms status` prints the overview as text"
    try:
        curses.setupterm()
    except curses.error as exc:
        return f"worms-gui: TERM={os.environ.get('TERM', '')!r}: {exc}"
    if not curses.tigetstr("cup"):
        return (f"worms-gui: this terminal (TERM={os.environ.get('TERM', '')}) cannot place the cursor. In a terminal that "
                "can: TERM=xterm-256color (colours off: NO_COLOR=1); `worms status` prints the overview as text")  # fmt: skip
    return ""


def run(backend, tab="vms", ascii_only=None) -> int:
    """The screen until `q`. The locale's encoding decides UTF-8 or ASCII glyphs (`ascii_only`, when app.py
    has set the locale already); NO_COLOR, colours."""
    if ascii_only is None:
        try:
            locale.setlocale(locale.LC_ALL, "")
        except locale.Error:
            pass  # a locale not installed: C's
        # The C library's codeset, which curses writes in: Python's own UTF-8 mode says UTF-8 under LC_ALL=C too.
        codeset = (locale.nl_langinfo(locale.CODESET) or "").lower().replace("-", "").replace("_", "")
        ascii_only = codeset != "utf8"
    problem = terminal_problem()
    if problem:
        print(problem, file=sys.stderr)
        return 2
    os.environ.setdefault("ESCDELAY", "25")

    def main(scr):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        colours = curses.has_colors() and os.environ.get("NO_COLOR") is None
        if colours:
            curses.start_color()
        return UI(scr, backend, tab=tab, ascii_only=ascii_only, colours=colours).run()

    return curses.wrapper(main)
