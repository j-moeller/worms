"""Live output of a long task, for `worms` (src/worms_cli/live.py).

Ansible shows what a task printed only once the task is over: a module runs on the host and hands
back its whole result at the end. So a long task writes its output to a file on its host as it
runs, and names that file:

    - name: Fetch the packages
      vars:
        worms_live_log: /var/log/worms/devpi-seed.log
      ansible.builtin.shell: |
        ... >> /var/log/worms/devpi-seed.log 2>&1

This callback tells `worms` when such a task starts and ends on each host, with a line of its own on
stdout, which `worms` takes out of the output (its run log, runlog.Tee) and answers by following the
file over SSH: the last lines, under the task, with how long it runs and how long since its last
line. A package install (the apt module) follows apt's own log, /var/log/apt/term.log, unasked.

Only when `worms` asks for it (WORMS_LIVE=1, set by its run log): anything else that runs
ansible-playbook (the round agent, a run by hand) gets nothing from it.
"""

from __future__ import annotations

import os
import re
import sys

from ansible.plugins.callback import CallbackBase

DOCUMENTATION = """
name: worms_live
type: aggregate
short_description: tells worms which file a running task writes, so that it can show it live
description:
  - With WORMS_LIVE=1, a line on stdout when a task that names worms_live_log (or installs packages
    with apt) starts and ends on a host. worms takes these lines out of the output.
"""

MARK = "\x1eworms-live"
SAFE = re.compile(r"[A-Za-z0-9._@:+-]+")
PATH = re.compile(r"/[A-Za-z0-9._/+-]+")
APT_ACTIONS = {"apt", "ansible.builtin.apt", "ansible.legacy.apt"}
APT_LOG = "/var/log/apt/term.log"


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "aggregate"
    CALLBACK_NAME = "worms_live"
    CALLBACK_NEEDS_ENABLED = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._on = os.environ.get("WORMS_LIVE") == "1"
        self._running: set[tuple[str, str]] = set()

    @staticmethod
    def _say(*words: str) -> None:
        try:
            sys.stdout.write(" ".join((MARK, *words)) + "\n")
            sys.stdout.flush()
        except Exception:  # noqa: BLE001  (never a reason for a task to fail)
            pass

    @staticmethod
    def _log(task) -> str | None:
        try:
            path = task.get_vars().get("worms_live_log")
        except Exception:  # noqa: BLE001
            path = None
        if not path and task.action in APT_ACTIONS:
            path = APT_LOG
        return path if isinstance(path, str) and PATH.fullmatch(path) else None

    def v2_runner_on_start(self, host, task):
        if not self._on:
            return
        path, name = self._log(task), host.get_name()
        if path and SAFE.fullmatch(name):
            self._running.add((name, task._uuid))
            self._say("start", name, task._uuid, path)

    def _end(self, result, *args, **kwargs):
        try:
            key = (result._host.get_name(), result._task._uuid)
        except AttributeError:
            return
        if key in self._running:
            self._running.discard(key)
            self._say("stop", *key)

    v2_runner_on_ok = _end
    v2_runner_on_failed = _end
    v2_runner_on_skipped = _end
    v2_runner_on_unreachable = _end

    def v2_playbook_on_stats(self, stats):
        for key in sorted(self._running):
            self._say("stop", *key)
        self._running.clear()
