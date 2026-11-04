"""What an administrator finds in the database, as JSON on stdout. Read-only, through the
`worms` CLI's connection. For the live journey (tests/live/test_devbox_journey.py).

A separate process on purpose: the offline suite points Django at a throwaway database,
and nothing in the live journey may share a process with that. Holds no token, flag or
password values, only counts, times and hashes.
"""

from __future__ import annotations

import ipaddress
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from worms_cli import django_setup  # noqa: E402
from worms_cli.config import Config  # noqa: E402

django_setup.setup(Config.load())

from django.db import connection, transaction  # noqa: E402


def rows(cursor, sql, *params):
    cursor.execute(sql, params)
    columns = [c.name for c in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def main():
    # Which networks count as a devbox's: argv[1] is a JSON list of CIDRs.
    devbox_networks = [ipaddress.ip_network(net) for net in json.loads(sys.argv[1])] if len(sys.argv) > 1 else []
    with transaction.atomic():
        cursor = connection.cursor()
        cursor.execute("SET TRANSACTION READ ONLY")
        view = {
            "users": [r["username"] for r in rows(cursor, "SELECT username FROM users ORDER BY 1")],
            "jobs": rows(cursor, "SELECT left(uuid::text, 8) AS id, kind, state, error, requested_at, started_at, finished_at FROM host_jobs ORDER BY requested_at"),
            "rounds": rows(cursor, "SELECT count(*) AS n FROM rounds")[0]["n"],
            "teams": {},
            "flows": rows(cursor, "SELECT count(*) AS n FROM network_flows")[0]["n"],
            "flows_touching_devboxes": 0,
            "zeek_heartbeat": rows(cursor, "SELECT max(ts) AS ts FROM network_events WHERE log = 'stats'")[0]["ts"],
            "now": rows(cursor, "SELECT now() AS now")[0]["now"],
        }
        for net in devbox_networks:
            view["flows_touching_devboxes"] += rows(
                cursor,
                "SELECT count(*) AS n FROM network_flows WHERE src_ip::inet <<= %s::inet OR dst_ip::inet <<= %s::inet",
                str(net),
                str(net),
            )[0]["n"]
        for team in rows(cursor, "SELECT t.id, t.name, t.devbox_version, d.index FROM teams t LEFT JOIN devboxes d ON d.team_id = t.id ORDER BY t.name"):
            count = lambda sql, *a: rows(cursor, sql, team["id"], *a)[0]["n"]  # noqa: E731
            view["teams"][team["name"]] = {
                "version": team["devbox_version"],
                "index": team["index"],
                "ssh_keys": count("SELECT count(*) AS n FROM devbox_ssh_keys WHERE team_id = %s"),
                "captures": {
                    str(r["devbox_version"]): r["n"]
                    for r in rows(cursor, "SELECT devbox_version, count(*) AS n FROM devbox_captures WHERE team_id = %s GROUP BY 1", team["id"])
                },
                "logs": {
                    str(r["devbox_version"]): r["n"]
                    for r in rows(cursor, "SELECT devbox_version, count(*) AS n FROM devbox_logs WHERE team_id = %s GROUP BY 1", team["id"])
                },
                "vms": rows(
                    cursor,
                    "SELECT v.vm_identifier AS name, v.role, v.ssh_fabric_ip, v.vm_maintenance_ip, v.addresses, v.proxmox_vm_id, v.ctf_flag_hash AS token_hash "
                    "FROM devbox_vms v JOIN devboxes d ON d.id = v.devbox_id WHERE d.team_id = %s ORDER BY v.role",
                    team["id"],
                ),
            }
        transaction.set_rollback(True)
    print(json.dumps(view, default=str))


if __name__ == "__main__":
    main()
