"""The node's event tables: SSH logins through the devbox gateway
(ssh_gateway_logins), and the internet window and the firewall watchdog
(platform_events). docs/monitoring.md, "Event tables"; the dashboard writes its
own (projects/dashboard/core/events.py).

    worms-audit event internet_allowed [--data '{"by": "Internet.yml"}'] [--actor NAME]
    worms-audit collect-gateway

Both write as the node's events account (dbuser_events_node, EVENTS_DB_USER in
/etc/worms/events.env), used for nothing else: it may only INSERT into these two
tables, never read or change a row; the database fills the time and the writer
(setup_db_permissions). Writing is best effort: a failure is reported on stderr
and the exit code, and never blocks what called it.

collect-gateway (worms-audit-gateway.timer, every minute) reads the SSH
gateway's sshd journal over the management network from where it stopped last,
and records each accepted team login: when, which key (sshd's fingerprint,
which credential_events names too, with who registered it) and, when it can
tell, the client's public address. The gateway only sees the node (the node's DNAT
masquerades every team), so the address comes from the node's own log of new
connections to the published port (`worms-ssh-gw:` kernel log, worms.nft.j2),
matched by source port: masquerading keeps the port unless it is taken.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PLATFORM = "INSERT INTO platform_events (kind, actor, detail) VALUES (%s, %s, %s)"
LOGIN = (
    "INSERT INTO ssh_gateway_logins (logged_in_at, gateway_user, key_type, ssh_fingerprint, client_ip,"
    " gateway_peer_port) VALUES (%s, %s, %s, %s, %s, %s)"
)
# What `event` may record: the callers are Internet.yml and the firewall watchdog.
EVENT_KINDS = {"internet_allowed", "internet_denied", "watchdog_stopped"}
MAX_DATA = 2048

STATE_DIR = Path("/var/lib/worms")
CURSOR_FILE = STATE_DIR / "audit-gateway.cursor"
NODE_LOG_PREFIX = "worms-ssh-gw:"
# How far back, and forward, the node's connection log is searched for a login.
MATCH_BEFORE, MATCH_AFTER = 300, 5
BATCH = 2000  # journal entries per run at most; the next run goes on

ACCEPTED = re.compile(
    r"^Accepted publickey for (?P<user>[a-z_][a-z0-9_-]{0,31}) from (?P<peer>\d{1,3}(?:\.\d{1,3}){3}) "
    r"port (?P<port>\d{1,5}) ssh2: (?P<type>[A-Z0-9-]{2,32}) (?P<fingerprint>SHA256:[A-Za-z0-9+/]{43})$"
)
NODE_CONNECTION = re.compile(r"\bSRC=(?P<src>\d{1,3}(?:\.\d{1,3}){3}) .*\bSPT=(?P<port>\d{1,5}) .*\bDPT=\d+")
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR"]  # fmt: skip


def connect(env):
    """The database as the node's events account, never the round agent's role."""
    from round_agent.agent import connect as agent_connect

    if not env.get("EVENTS_DB_USER"):
        raise RuntimeError("no EVENTS_DB_USER (/etc/worms/events.env)")
    return agent_connect({**env, "DB_USER": env["EVENTS_DB_USER"], "DB_PASSWORD": env.get("EVENTS_DB_PASSWORD", "")})


def _data(data: dict) -> str:
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return text if len(text) <= MAX_DATA else json.dumps({"truncated": True, "start": text[: MAX_DATA - 100]})


def platform(conn, kind: str, detail: dict, actor: str | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(PLATFORM, (kind, actor, _data(detail)))


# --- the SSH gateway ------------------------------------------------------------------------


def parse_gateway(lines, user: str) -> tuple[list[dict], str | None]:
    """The accepted logins of `user` in journal JSON lines, and the last cursor."""
    logins, cursor = [], None
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        cursor = entry.get("__CURSOR", cursor)
        message = entry.get("MESSAGE")
        match = ACCEPTED.match(message) if isinstance(message, str) else None
        if match is None or match["user"] != user:
            continue
        try:
            at = int(entry["__REALTIME_TIMESTAMP"]) / 1e6
        except (KeyError, TypeError, ValueError):
            continue
        logins.append({"at": at, "user": match["user"], "port": int(match["port"]), "peer": match["peer"],
                       "key_type": match["type"], "fingerprint": match["fingerprint"]})  # fmt: skip
    return logins, cursor


def parse_node_log(lines) -> list[tuple[float, str, int]]:
    """(time, client address, client port) of each new connection to the published port."""
    seen = []
    for line in lines:
        try:
            entry = json.loads(line)
            message = entry["MESSAGE"]
            at = int(entry["__REALTIME_TIMESTAMP"]) / 1e6
        except (ValueError, KeyError, TypeError):
            continue
        if not isinstance(message, str) or NODE_LOG_PREFIX not in message:
            continue
        match = NODE_CONNECTION.search(message)
        if match:
            seen.append((at, match["src"], int(match["port"])))
    return seen


def client_of(login: dict, connections) -> str | None:
    """The client address whose connection reached the node just before this login, on the
    port the gateway saw (the masquerade keeps it). The latest such connection wins."""
    best = None
    for at, src, port in connections:
        if port == login["port"] and login["at"] - MATCH_BEFORE <= at <= login["at"] + MATCH_AFTER:
            if best is None or at > best[0]:
                best = (at, src)
    return best[1] if best else None


def _run(argv, timeout=60) -> str:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"{argv[0]} exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    return proc.stdout


def collect_gateway(env, conn, run=_run, cursor_file: Path = CURSOR_FILE) -> int:
    """Record the gateway's new team logins. Returns how many were recorded."""
    host = env.get("SSH_GATEWAY_HOST", "")
    if not host:
        return 0
    user = env.get("SSH_GATEWAY_USER", "team")
    cursor = cursor_file.read_text().strip() if cursor_file.exists() else ""
    remote = ["sudo", "journalctl", "-u", "ssh", "-o", "json", "--no-pager", "-n", str(BATCH)]
    if cursor:
        remote.append(f"--after-cursor={cursor}")
    ssh = ["ssh", *SSH_OPTS, "-i", env.get("SSH_KEY", "/etc/worms/ssh/worms"), f"debian@{host}", shlex.join(remote)]
    logins, last = parse_gateway(run(ssh).splitlines(), user)
    connections = []
    if logins:
        since = int(min(l["at"] for l in logins) - MATCH_BEFORE)
        until = int(max(l["at"] for l in logins) + MATCH_AFTER + 1)
        try:
            connections = parse_node_log(
                run(["journalctl", "-k", "-o", "json", "--no-pager", f"--since=@{since}", f"--until=@{until}"]).splitlines()
            )
        except (RuntimeError, OSError, subprocess.TimeoutExpired):
            connections = []
    for login in logins:
        with conn.cursor() as cur:
            cur.execute(LOGIN, (
                datetime.fromtimestamp(login["at"], timezone.utc), login["user"], login["key_type"],
                login["fingerprint"], client_of(login, connections), login["port"],
            ))  # fmt: skip
    conn.commit()
    if last:
        cursor_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = cursor_file.with_suffix(".tmp")
        tmp.write_text(last + "\n")
        tmp.replace(cursor_file)
    return len(logins)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="worms-audit", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="action", required=True)
    event = sub.add_parser("event", help="record one node event")
    event.add_argument("kind", choices=sorted(EVENT_KINDS))
    event.add_argument("--data", default="{}", help="a JSON object")
    event.add_argument("--actor", default=None, help="who caused it, if known (e.g. the operator)")
    sub.add_parser("collect-gateway", help="record the SSH gateway's new team logins")
    args = parser.parse_args(argv)

    env = dict(os.environ)
    try:
        conn = connect(env)
    except Exception as exc:  # noqa: BLE001 -- best effort: say why, never block the caller
        print(f"worms-audit: no database: {exc}", file=sys.stderr)
        return 1
    try:
        if args.action == "event":
            data = json.loads(args.data)
            if not isinstance(data, dict):
                raise ValueError("--data must be a JSON object")
            platform(conn, args.kind, data, actor=(args.actor or None) and args.actor[:200])
            conn.commit()
        else:
            collect_gateway(env, conn)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"worms-audit: {args.action} not recorded: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
