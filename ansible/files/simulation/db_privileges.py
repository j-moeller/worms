#!/usr/bin/env python3
"""Checks a service's database role from its own VM (ansible/Simulate.yml),
with the credentials and TLS settings of its env file.

argv[1]: the env file (KEY=value), argv[2]: JSON list of SQL statements.
Every statement runs in its own transaction, which is rolled back.
Prints JSON {"connect": ..., "ssl": true|false, "<statement>": ok | denied | error: ...}.
"""
import json
import sys

env = {}
for line in open(sys.argv[1]):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        env[key] = value

result = {}
try:
    import psycopg2
    import psycopg2.errors

    conn = psycopg2.connect(
        host=env["DB_HOST"], port=int(env.get("DB_PORT", "5432")), dbname=env["DB_NAME"],
        user=env["DB_USER"], password=env["DB_PASSWORD"],
        sslmode=env.get("DB_SSLMODE", "verify-full"), sslrootcert=env.get("DB_SSLROOTCERT"),
        connect_timeout=10,
    )
    result["connect"] = "ok"
    with conn.cursor() as cur:
        cur.execute("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
        result["ssl"] = bool(cur.fetchone()[0])
    conn.rollback()
    for statement in json.loads(sys.argv[2]):
        try:
            with conn.cursor() as cur:
                cur.execute(statement)
            result[statement] = "ok"
        except psycopg2.errors.InsufficientPrivilege:
            result[statement] = "denied"
        except Exception as exc:  # noqa: BLE001
            result[statement] = f"error: {type(exc).__name__}: {str(exc).splitlines()[0]}"
        finally:
            conn.rollback()
    conn.close()
except Exception as exc:  # noqa: BLE001
    result.setdefault("connect", f"error: {type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}")
print(json.dumps(result))
