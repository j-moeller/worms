"""The devbox -> team map: which devbox gateway address is which team's, so that scoring and
logging attribute a devbox's submissions (docs/04-dev-environment.md).

One source: the host's file PATH (root, 0600), written by the devbox jobs alone
(handlers._write_devbox_map: the devbox stack's output and the teams the job carries). One
way onto scoring and logging: `push`, here only, from that file. The devbox jobs push it after
writing it; every rollback of scoring or logging pushes it again, because their `clean`
snapshot is older than the map: the round agent's (handlers._reset_services) and the CLI's
(worms_cli/pipeline.py). Neither needs the database or a host job for it.
"""

from __future__ import annotations

from pathlib import Path

PATH = "/var/lib/worms/devbox-teams"
TARGET = "/etc/sandbox-api/devbox-teams"  # sandbox_api_devbox_teams_file (ansible role sandbox_api)
HOLDERS = ("scoring", "logging")  # the VMs that hold a copy
INSTALL = ["sudo", "install", "-m", "0640", "-o", "root", "-g", "www-data", "/dev/stdin", TARGET]


class Missing(RuntimeError):
    """No map on the host: no devbox job has run yet."""


def read(path=PATH) -> str:
    try:
        return Path(path).read_text()
    except FileNotFoundError:
        raise Missing(f"no devbox map on the host ({path}): no devbox job has run yet") from None


def push(run_ssh, hosts, path=PATH, text=None) -> None:
    """The host's map onto each of `hosts`: `run_ssh(host, argv, input=...)` runs argv there (the
    agent's `ex.ssh`, by address; the CLI's, by VM name). `text`: the file's content, read on
    the host by a caller that is not on it."""
    body = read(path) if text is None else text
    for host in hosts:
        run_ssh(host, INSTALL, input=body)
