"""Ansible's output for WORMS: every VM's whole log in a file of its own, and on the terminal only
what a person reads while ten VMs are configured at once (ansible.cfg: stdout_callback).

Under `worms` (WORMS_ANSIBLE_LOGS names a directory: src/worms_cli/runlog.py sets it next to the
run log), each host's tasks go to <directory>/<vm>.log, in the order they ran there: the time, how
long each task took, what it did; a change or a failure with its result as YAML (multi-line output
as lines, not one string full of \\n). The terminal gets the plays' headers, a failure in full
(VM, task, file:line, msg, rc, stdout, stderr) the moment it happens, and at the end one line per
VM: its counts, its time, its file. A line on stdout per task start and result,
`\\x1eworms-live progress <json>`, feeds the view of every VM's current task that `worms` draws
under the output (src/worms_cli/live.py); `worms` takes these lines out of the output.

Without that directory (the round agent's jobs, a run by hand), every result is a line of its own,
`[vm] ok|changed|failed: role : task (1.2s)`, a failure or a change that printed something with its
result as YAML: a job's log stays complete, and readable when VMs run in parallel.

A task with no_log shows nothing of its result (Ansible censors it): its line says so, with where
the task is (`-e hide_secrets=false` shows it, secrets included).
"""

from __future__ import annotations

import json
import os
import sys
import time

import yaml
from ansible.plugins.callback import CallbackBase

DOCUMENTATION = """
name: worms
type: stdout
short_description: one VM per log file, failures in full, a progress view for worms
description:
  - With WORMS_ANSIBLE_LOGS set, every host's results go to a file of its own in that directory,
    and the terminal gets play headers, failures and a summary (and progress lines for worms).
  - Without it, one line per host and task, failures (and changes that printed something) as YAML.
"""

MARK = "\x1eworms-live progress "
DOMAIN = ".worms.internal"
DROPPED = {"invocation", "stdout_lines", "stderr_lines", "_ansible_no_log", "_ansible_verbose_always",
           "_ansible_verbose_override", "_ansible_item_label", "ansible_loop_var", "changed", "failed", "skipped",
           "warnings", "deprecations", "exception", "diff", "_ansible_parsed", "_ansible_delegated_vars",
           "ansible_facts", "item", "results", "_ansible_ignore_errors", "_ansible_item_result", "server_side",
           "start", "end", "delta", "attempts"}  # fmt: skip
# Shown first, in this order: what a person looks for.
FIRST = ("msg", "cmd", "rc", "stdout", "stderr", "module_stdout", "module_stderr", "reason")
DONE_TASK = "worms: phase done"
INCLUDES = ("ansible.builtin.include_role", "include_role", "ansible.builtin.include_tasks", "include_tasks")  # the last task of a phase play (Site.yml): the host has finished it


class _Block(str):
    """A multi-line string, dumped as a YAML block."""


class _Dumper(yaml.SafeDumper):
    pass


_Dumper.add_representer(_Block, lambda dumper, value: dumper.represent_scalar("tag:yaml.org,2002:str", str(value), style="|"))


def _plain(value):
    """`value` with every multi-line string a block, every key a string (YAML can take it)."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, str):
        text = value.rstrip("\n")
        return _Block(text) if "\n" in text else text
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def as_yaml(result: dict, indent: int = 4) -> str:
    """A task's result for a person: the keys that matter first, the noise left out."""
    shown = {k: result[k] for k in FIRST if result.get(k) not in (None, "", [])}
    shown.update({k: v for k, v in result.items() if k not in shown and k not in FIRST and k not in DROPPED
                  and not k.startswith("_ansible")})  # fmt: skip
    if not shown:
        return ""
    text = yaml.dump(_plain(shown), Dumper=_Dumper, default_flow_style=False, allow_unicode=True, sort_keys=False, width=110)
    return "".join(" " * indent + line + "\n" for line in text.splitlines())


def _parts(result):
    """(host name, task, result dict) of a task's result: ansible-core 2.19 on has .host/.task/.result,
    older ones only ._host/._task/._result (a laptop's Ansible may be older than the node's)."""
    host = getattr(result, "host", None) or result._host
    task = getattr(result, "task", None) or result._task
    res = getattr(result, "result", None)
    return host.get_name(), task, res if res is not None else result._result


def short(host: str) -> str:
    return host.removesuffix(DOMAIN)


def _secs(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m{seconds % 60:02d}s"


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "stdout"
    CALLBACK_NAME = "worms"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dir = os.environ.get("WORMS_ANSIBLE_LOGS") or None
        self.progress = os.environ.get("WORMS_LIVE") == "1" and self.dir is not None
        self.files: dict[str, object] = {}
        self.started: dict[tuple[str, str], float] = {}
        self.host_started: dict[str, float] = {}
        self.host_seconds: dict[str, float] = {}
        self.host_last: dict[str, float] = {}
        self.roles: dict[str, list[int]] = {}  # host: [roles run, roles skipped as unchanged]
        self.failed_items: dict[tuple[str, str], list] = {}  # (host, task): its loop's failed items
        self.failures: list[tuple[str, str]] = []  # (host, task)
        self.run_started = time.monotonic()
        self.playbook = ""

    # -- where things go ------------------------------------------------------------------------

    def _out(self, text: str) -> None:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
        sys.stdout.flush()

    def _mark(self, **event) -> None:
        if self.progress:
            self._out(MARK + json.dumps(event, separators=(",", ":")))

    def _file(self, host: str):
        if self.dir is None:
            return None
        name = short(host)
        if name not in self.files:
            try:
                os.makedirs(self.dir, mode=0o700, exist_ok=True)
                fd = os.open(os.path.join(self.dir, f"{name}.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                self.files[name] = os.fdopen(fd, "a", buffering=1)
            except OSError:
                self.files[name] = None
        return self.files[name]

    def _log(self, host: str, text: str) -> None:
        handle = self._file(host)
        if handle is not None:
            try:
                handle.write(text if text.endswith("\n") else text + "\n")
            except OSError:
                pass

    def _path_of(self, name: str) -> str:
        return os.path.join(self.dir, f"{short(name)}.log") if self.dir else ""

    # -- one task's result ----------------------------------------------------------------------

    @staticmethod
    def _rescued(task) -> bool:
        """Inside a block with a rescue: the failure is the block's to handle, not the run's."""
        parent = getattr(task, "_parent", None)
        while parent is not None:
            if getattr(parent, "rescue", None):
                return True
            parent = getattr(parent, "_parent", None)
        return False

    @staticmethod
    def _gated(task) -> bool:
        """An include of Site.yml's tasks/role.yml: its gate's line says which role, and why."""
        return "tasks/role.yml" in (task.get_path() or "")

    @staticmethod
    def _task_name(task) -> str:
        return (task.get_name() or task.action or "?").strip()

    def _took(self, host: str, task) -> str:
        started = self.started.pop((host, task._uuid), None)
        return f" ({time.monotonic() - started:.1f}s)" if started else ""

    def _result(self, result, status: str) -> None:
        host, task, res = _parts(result)
        name, took = self._task_name(task), self._took(host, task)
        if name == DONE_TASK:
            self._mark(event="done", host=short(host))
            self.host_seconds[host] = time.monotonic() - self.host_started.get(host, self.run_started)
            return
        self.host_last[host] = time.monotonic()
        if task.action in INCLUDES and status in ("ok", "skipped"):
            if status == "ok" and task.action.endswith("include_role") and not self._gated(task):
                self._log(host, f"{time.strftime('%H:%M:%S')}  ▸ {(task.args or {}).get('name', '') or name}")
            return  # an include: its tasks say what it does; skipped, a role this VM does not get
        if task.action.endswith("worms_role_gate") and status == "ok":  # Site.yml: tasks/role.yml
            skipped = bool(res.get("skip"))
            if res.get("record"):  # not in a run of a part (--tags): there every gate says "it runs"
                counts = self.roles.setdefault(host, [0, 0])
                counts[1 if skipped else 0] += 1
            self._log(host, f"{time.strftime('%H:%M:%S')}  ▸ {(task.args or {}).get('role', '?')}: {res.get('why', '')}")
            self._mark(event="result", host=short(host), status="ok")
            return
        hidden = res.get("_ansible_no_log") or res.get("censored")
        detail = ""
        if status in ("failed", "unreachable"):
            detail = (f"    (output hidden: no_log; task at {task.get_path()}; -e hide_secrets=false shows it)\n" if hidden
                      else as_yaml(res))  # fmt: skip
            for label, item in self.failed_items.pop((host, task._uuid), [])[:5]:  # a loop: the items that failed
                detail += f"    item {label}:\n" + (as_yaml(item, indent=6) if not item.get("_ansible_no_log") else "      (hidden: no_log)\n")
        if status == "failed" and self._rescued(task):
            self._log(host, f"{time.strftime('%H:%M:%S')}  failed, rescued  {name}{took}\n{detail}")
            return
        elif status == "changed" and not hidden:
            detail = as_yaml({k: res[k] for k in ("msg", "stdout", "stderr") if res.get(k)})
        when = time.strftime("%H:%M:%S")
        line = f"{when}  {status:<11} {name}{took}"
        self._log(host, line + ("\n" + detail if detail else ""))
        self._mark(event="result", host=short(host), status=status)
        if status in ("failed", "unreachable"):
            self.failures.append((host, name))
            self._failure(host, task, detail)
        elif self.dir is None and status != "skipped":
            self._out(f"[{short(host)}] {status}: {name}{took}" + ("\n" + detail.rstrip("\n") if detail and status == "changed" else ""))

    def _failure(self, host: str, task, detail: str) -> None:
        path = task.get_path() or "?"
        where = path.split("/ansible/", 1)[-1]
        self._out(f"\n✗ {short(host)} · {self._task_name(task)} · {where}\n{detail.rstrip()}"
                  + (f"\n    its whole log: {self._path_of(host)}" if self.dir else "") + "\n")  # fmt: skip

    def v2_runner_on_start(self, host, task):
        name = host.get_name()
        self.started[(name, task._uuid)] = time.monotonic()
        self.host_started.setdefault(name, time.monotonic())
        role = task._role.get_name() if task._role else ""
        task_name = self._task_name(task)
        if task_name != DONE_TASK and task.action not in INCLUDES:
            self._mark(event="task", host=short(name), role=role, task=task_name.split(" : ", 1)[-1])

    def v2_runner_on_ok(self, result):
        _host, _task, res = _parts(result)
        self._handle_warnings(res)
        self._result(result, "changed" if res.get("changed") else "ok")

    def _exception(self, result) -> str:
        """Ansible's own account of a failure (its error, a module's traceback): into the VM's log
        only. The terminal's block says the same, shorter, with the task's file and line."""
        current = getattr(self, "_current_task_result", None)
        exception = getattr(current, "exception", None) if _parts(result)[2].pop("exception", None) else None
        if not exception:
            return ""
        messages, traces, event = [], [], getattr(exception, "event", None)
        while event is not None:  # ansible-core's ErrorSummary: a chain of events, each a message
            messages.append(str(getattr(event, "msg", "")).strip())
            if getattr(event, "formatted_traceback", None):
                traces.append(event.formatted_traceback)
            chain = getattr(event, "chain", None)
            event = getattr(chain, "event", None)
        text = " <<< ".join(m for m in messages if m) or str(exception)
        return (text + ("\n" + "\n".join(traces) if traces else ""))[:4000]

    def v2_runner_on_failed(self, result, ignore_errors=False):
        host, task, res = _parts(result)
        self._handle_warnings(res)
        exception = self._exception(result)
        if ignore_errors:
            self._log(host, f"{time.strftime('%H:%M:%S')}  failed, ignored  {self._task_name(task)}\n" + as_yaml(res))
            return
        self._result(result, "failed")
        if exception:
            self._log(host, "    ansible: " + exception.replace("\n", "\n    "))

    def v2_runner_on_skipped(self, result):
        self._result(result, "skipped")

    def v2_runner_on_unreachable(self, result):
        self._handle_warnings(_parts(result)[2])
        self._exception(result)
        self._result(result, "unreachable")

    def v2_runner_item_on_failed(self, result):
        host, task, res = _parts(result)
        self._log(host, f"    item failed: {self._get_item_label(res)}\n" + as_yaml(res, indent=6))
        self.failed_items.setdefault((host, task._uuid), []).append((self._get_item_label(res), dict(res)))

    def v2_runner_item_on_ok(self, result):
        host, _task, res = _parts(result)
        if res.get("changed"):
            self._log(host, f"    item changed: {self._get_item_label(res)}")

    def v2_runner_retry(self, result):
        host, task, res = _parts(result)
        left = res.get("retries", 0) - res.get("attempts", 0)
        self._log(host, f"{time.strftime('%H:%M:%S')}  retrying   {self._task_name(task)} ({left} left)")

    def v2_playbook_on_handler_task_start(self, task):
        pass

    # -- plays and the run ----------------------------------------------------------------------

    def v2_playbook_on_start(self, playbook):
        self.playbook = os.path.basename(playbook._file_name)

    def v2_playbook_on_play_start(self, play):
        name = play.get_name().strip()
        try:
            hosts = [h.get_name() for h in play.get_variable_manager()._inventory.get_hosts(play.hosts)] if play.hosts else []
        except Exception:  # noqa: BLE001  (the header without its hosts)
            hosts = []
        self._out(f"── {self.playbook}: {name}")
        self._mark(event="play", name=name, hosts=[short(h) for h in hosts])
        for host in hosts:
            self._log(host, f"\n── {self.playbook}: {name}  ({time.strftime('%Y-%m-%d %H:%M:%S')})")

    def v2_playbook_on_no_hosts_matched(self):
        pass

    def v2_playbook_on_no_hosts_remaining(self):
        self._out("   no VM left to run on: the failures above stopped it")

    def _busy(self, stats, host) -> bool:
        """It changed something, or ran a role (Site.yml): its own row in the summary."""
        return bool(stats.summarize(host)["changed"] or self.roles.get(host, [0, 0])[0])

    def v2_playbook_on_stats(self, stats):
        rows = []
        for host in sorted(stats.processed):
            s = stats.summarize(host)
            bad = s["failures"] or s["unreachable"]
            took = self.host_last.get(host, self.run_started) - self.host_started.get(host, self.run_started)
            ran, unchanged = self.roles.get(host, (0, 0))
            roles = f"roles {ran:>2} run {unchanged:>2} unchanged  " if ran or unchanged else ""
            rows.append((bad, f"  {'✗' if bad else '✓'} {short(host):<18} {roles}changed {s['changed']:>3}  "
                              f"failed {s['failures'] + s['unreachable']:>2}  {_secs(max(took, 0)):>6}"
                              + (f"  {self._path_of(host)}" if self.dir else "")))  # fmt: skip
        self._mark(event="end")
        self._out(f"── {self.playbook}: {len(rows)} VM(s) in {_secs(time.monotonic() - self.run_started)}")
        quiet = [host for host, (bad, row) in zip(sorted(stats.processed), rows) if not bad and not self._busy(stats, host)]
        for host, (bad, row) in zip(sorted(stats.processed), rows):
            if host not in quiet or len(quiet) <= 3:
                self._out(row)
        if len(quiet) > 3:
            self._out(f"  ✓ {len(quiet)} VMs, nothing changed: {', '.join(short(h) for h in quiet)}")
        if self.failures:
            self._out(f"   failed: {', '.join(f'{short(h)} ({t})' for h, t in self.failures[:6])}")
        for handle in self.files.values():
            if handle is not None:
                handle.close()
        self.files.clear()
