"""The round agent's main loop: pull host jobs from the database, run them,
write the results. Runs as root on the Proxmox host (systemd service,
ansible role host/round_agent).

The only new path into the host is this outbound connection to the database.
The agent claims one queued job at a time with FOR UPDATE SKIP LOCKED, so a
second instance (or a restart) never runs the same job twice. While a job
runs the agent heartbeats; the conductor fails a job whose heartbeat goes
stale. Every parameter is validated (validation.py) before a handler runs,
and a handler that raises fails only that job -- the loop keeps going.

Connection settings come from the environment (a root-only file, like the
other services):

    DB_HOST DB_PORT DB_NAME DB_USER DB_PASSWORD DB_SSLMODE DB_SSLROOTCERT
    REPO_ROOT                 the agent's workspace (default: cwd; /opt/worms)
    DASHBOARD_HOST            where to read uploaded artifacts (ssh)
    DASHBOARD_MEDIA_ROOT      the dashboard's media directory
"""

from __future__ import annotations

import json
import logging
import os
import select
import signal
import threading
import time

from round_agent import handlers, validation
from round_agent.executor import Executor

log = logging.getLogger("round-agent")

HEARTBEAT_SECONDS = 30
IDLE_POLL_SECONDS = 5


def connect(env):
    import psycopg2

    return psycopg2.connect(
        host=env["DB_HOST"],
        port=int(env.get("DB_PORT", "5432")),
        dbname=env["DB_NAME"],
        user=env["DB_USER"],
        password=env["DB_PASSWORD"],
        sslmode=env.get("DB_SSLMODE", "verify-full"),
        sslrootcert=env.get("DB_SSLROOTCERT", "/etc/ssl/certs/ca-certificates.crt"),
        connect_timeout=10,
        application_name="round-agent",
    )


def config(env):
    return {
        "dashboard_host": env.get("DASHBOARD_HOST", ""),
        "dashboard_media_root": env.get("DASHBOARD_MEDIA_ROOT", "/opt/dashboard/media"),
        # Scoring/logging management addresses: the agent writes the running
        # team's uuid to them at deploy (no access token anymore).
        "scoring_host": env.get("SCORING_HOST", ""),
        "logging_host": env.get("LOGGING_HOST", ""),
        "apply_timeout": int(env.get("APPLY_TIMEOUT", "5400")),
    }


class Agent:
    def __init__(self, env=None, executor=None, connection=None):
        self.env = env or dict(os.environ)
        self.cfg = config(self.env)
        self.executor = executor or Executor(self.env.get("REPO_ROOT", os.getcwd()))
        self._conn = connection
        self.running = True

    def conn(self):
        if self._conn is None or self._conn.closed:
            self._conn = connect(self.env)
            self._conn.autocommit = True
        return self._conn

    # -- claiming -------------------------------------------------------------

    def claim(self):
        """Claim the oldest queued job. Returns (id, uuid, kind, params) or None."""
        conn = self.conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE host_jobs SET state = 'running', started_at = now(), heartbeat_at = now() "
                "WHERE id = (SELECT id FROM host_jobs WHERE state = 'queued' "
                "ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1) "
                "RETURNING id, uuid, kind, params"
            )
            row = cur.fetchone()
        return row

    def heartbeat(self, job_id):
        with self.conn().cursor() as cur:
            cur.execute("UPDATE host_jobs SET heartbeat_at = now() WHERE id = %s", (job_id,))

    def finish(self, job_id, state, result=None, error=""):
        with self.conn().cursor() as cur:
            cur.execute(
                "UPDATE host_jobs SET state = %s, result = %s, error = %s, finished_at = now() WHERE id = %s",
                (state, json.dumps(result or {}), error[:4000], job_id),
            )

    # -- running one job ------------------------------------------------------

    def run_job(self, job_id, uuid, kind, params):
        log.info("job %s (%s) starting", uuid, kind)
        stop = threading.Event()
        beat = threading.Thread(target=self._heartbeat_loop, args=(job_id, stop), daemon=True)
        beat.start()
        try:
            clean = validation.validate(kind, params)
            result = handlers.HANDLERS[kind](self.executor, self.cfg, clean)
            self.finish(job_id, "done", result)
            log.info("job %s done", uuid)
        except validation.Invalid as exc:
            self.finish(job_id, "failed", error=f"invalid: {exc}")
            log.warning("job %s rejected: %s", uuid, exc)
        except Exception as exc:  # noqa: BLE001
            self.finish(job_id, "failed", error=f"{type(exc).__name__}: {exc}")
            log.exception("job %s failed", uuid)
        finally:
            stop.set()
            beat.join(timeout=5)

    def _heartbeat_loop(self, job_id, stop):
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                self.heartbeat(job_id)
            except Exception:  # noqa: BLE001
                log.warning("heartbeat failed for job %s", job_id)

    # -- the loop -------------------------------------------------------------

    def tick(self):
        """Claim and run one job. Returns True if a job ran."""
        row = self.claim()
        if row is None:
            return False
        self.run_job(*row)
        return True

    def run_forever(self):
        self._listen()
        log.info("round agent started")
        while self.running:
            try:
                while self.tick():
                    pass
            except Exception:  # noqa: BLE001  (connection hiccup: reconnect and retry)
                log.exception("agent loop error; reconnecting")
                self._conn = None
                time.sleep(IDLE_POLL_SECONDS)
                continue
            self._wait_for_work()

    def _listen(self):
        try:
            with self.conn().cursor() as cur:
                cur.execute("LISTEN host_jobs")
        except Exception:  # noqa: BLE001  (polling still works without NOTIFY)
            log.warning("LISTEN failed; polling only")

    def _wait_for_work(self):
        """Block until a NOTIFY arrives or the poll interval passes."""
        conn = self.conn()
        try:
            if select.select([conn], [], [], IDLE_POLL_SECONDS) != ([], [], []):
                conn.poll()
                conn.notifies.clear()
        except Exception:  # noqa: BLE001
            time.sleep(IDLE_POLL_SECONDS)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    agent = Agent()

    def stop(*_):
        agent.running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    agent.run_forever()


if __name__ == "__main__":
    main()
