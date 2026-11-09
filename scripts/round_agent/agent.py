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
    REPO_ROOT                 the agent's workspace (/opt/worms)
    DASHBOARD_HOST            where to read uploaded artifacts (ssh)
    DASHBOARD_MEDIA_ROOT      the dashboard's media directory

The service does not start while one it needs is missing or empty (REQUIRED_ENV),
or the WORMS key is not there: it says which, rather than fail a job later.
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
from round_agent.executor import DEFAULT_JOB_LOG_DIR, Executor

log = logging.getLogger("round-agent")

# What /etc/worms/round-agent.env must set (role round_agent writes all of it).
REQUIRED_ENV = (
    "REPO_ROOT", "DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_SSLROOTCERT",
    "DASHBOARD_HOST", "DASHBOARD_MEDIA_ROOT", "SCORING_HOST", "LOGGING_HOST", "SERVICE_HOSTS", "SSH_KEY",
    "TF_VAR_proxmox_api_url", "TF_VAR_proxmox_api_token", "TF_VAR_proxmox_instance", "TF_VAR_ssh_public_key",
    "TF_VAR_node_ssh_address", "TF_VAR_node_ssh_port", "TF_VAR_llm_access_ip",
)
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
        # The service VMs' management addresses, waited for after they are rolled back.
        "service_hosts": [host for host in env.get("SERVICE_HOSTS", "").split(",") if host],
        "apply_timeout": int(env.get("APPLY_TIMEOUT", "5400")),
    }


class Agent:
    def __init__(self, env=None, executor=None, connection=None):
        self.env = env or dict(os.environ)
        self.cfg = config(self.env)
        self.executor = executor or Executor(
            self.env.get("REPO_ROOT", os.getcwd()), log_dir=self.env.get("JOB_LOG_DIR", DEFAULT_JOB_LOG_DIR)
        )
        self._conn = connection
        self.running = True

    def conn(self):
        if self._conn is None or self._conn.closed:
            self._conn = connect(self.env)
            self._conn.autocommit = True
        return self._conn

    # -- claiming -------------------------------------------------------------

    def claim(self):
        """Claim the oldest queued job, a round's before a devbox's: a round's jobs are timed, and
        a team's test must not hold them up. Returns (id, uuid, kind, params) or None."""
        conn = self.conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE host_jobs SET state = 'running', started_at = now(), heartbeat_at = now() "
                "WHERE id = (SELECT id FROM host_jobs WHERE state = 'queued' "
                "ORDER BY kind LIKE 'devbox%%', id FOR UPDATE SKIP LOCKED LIMIT 1) "
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

    def superseded(self, job_id, kind):
        """A reconfigure job carries every team's keys, so a newer one queued
        behind it covers everything it would do."""
        if kind != "devbox_reconfigure":
            return False
        with self.conn().cursor() as cur:
            cur.execute("SELECT 1 FROM host_jobs WHERE kind = %s AND state = 'queued' AND id > %s LIMIT 1", (kind, job_id))
            return cur.fetchone() is not None

    def run_job(self, job_id, uuid, kind, params):
        if self.superseded(job_id, kind):
            self.finish(job_id, "done", {"superseded": True})
            log.info("job %s (%s) skipped: a newer one is queued", uuid, kind)
            return
        log.info("job %s (%s) starting", uuid, kind)
        stop = threading.Event()
        beat = threading.Thread(target=self._heartbeat_loop, args=(job_id, stop), daemon=True)
        beat.start()
        with self.executor.job_log(uuid, kind) as job_log:
            if job_log.path:
                log.info("job %s output: %s", uuid, job_log.path)
            try:
                clean = validation.validate(kind, params)
                result = handlers.HANDLERS[kind](self.executor, self.cfg, clean)
                self.finish(job_id, "done", result)
                job_log.note("job done")
                log.info("job %s done", uuid)
            except validation.Invalid as exc:
                self.finish(job_id, "failed", error=f"invalid: {exc}")
                job_log.note(f"job rejected: {exc}")
                log.warning("job %s rejected: %s", uuid, exc)
            except Exception as exc:  # noqa: BLE001
                self.finish(job_id, "failed", error=f"{type(exc).__name__}: {exc}")
                job_log.note(f"job failed: {type(exc).__name__}: {exc}")
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


def check_env(env) -> None:
    """Stop before the loop when the environment file lacks what the jobs need."""
    missing = [name for name in REQUIRED_ENV if not env.get(name)]
    missing += [path for path in (env.get("SSH_KEY"), env.get("TF_VAR_ssh_public_key")) if path and not os.path.exists(path)]
    if missing:
        raise SystemExit(
            f"round agent: not set or not there: {', '.join(missing)} (/etc/worms/round-agent.env). "
            "Run SetupRoundAgent.yml again from the controller (`worms build round-agent`), with the vault complete "
            "(`worms secrets`)"
        )


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    check_env(os.environ)
    agent = Agent()

    def stop(*_):
        agent.running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    agent.run_forever()


if __name__ == "__main__":
    main()
