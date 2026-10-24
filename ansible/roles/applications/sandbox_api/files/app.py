"""Sandbox-facing REST API: flag submissions (scoring) or log events (logging).

The only infrastructure service reachable from the (assumed compromised)
sandbox. Every request is hostile until proven otherwise, so the API is small
and strict:

- POST /submit only (nginx rejects everything else), JSON object only, size
  capped by nginx and by Flask (MAX_BODY_BYTES).
- No access token. Only one team runs at a time, so the round agent writes the
  running team's uuid into CURRENT_TEAM_FILE at deploy; the API records that
  uuid on every submission. A submission while no round is active (no team
  file) is refused (409). (Access tokens still exist for teams in general;
  they are just not used by the scoring/logging servers anymore.)
- A token-bucket rate limit keyed by the current team; past the limit a short
  ban (429). IP-based blocking is pointless here: sandbox-gw SNATs all sandbox
  traffic, so per-VM limits are enforced on the Proxmox host instead.
- Payload validation (scoring: printable flag <= 64 chars; logging: JSON object
  with bounded size, depth and item count).
- The database role may only INSERT into one table; statements are time-limited.

State (buckets, bans) is in memory, so gunicorn runs ONE worker process with
threads (see the systemd unit).
"""

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid

import psycopg2
from flask import Flask, jsonify, request
from psycopg2.extras import Json
from psycopg2.pool import ThreadedConnectionPool


def env_int(name, default):
    return int(os.environ.get(name, default))


KIND = os.environ["SANDBOX_API_KIND"]
if KIND not in ("scoring", "logging"):
    raise RuntimeError(f"SANDBOX_API_KIND must be scoring or logging, not {KIND!r}")

MAX_BODY_BYTES = env_int("MAX_BODY_BYTES", 4096)
MAX_FLAG_LENGTH = 64
LOG_MAX_BYTES = env_int("LOG_MAX_BYTES", 8192)
LOG_MAX_DEPTH = env_int("LOG_MAX_DEPTH", 8)
LOG_MAX_ITEMS = env_int("LOG_MAX_ITEMS", 256)

TEAM_RATE_PER_MINUTE = env_int("TEAM_RATE_PER_MINUTE", 30)
TEAM_BURST = env_int("TEAM_BURST", 10)
TEAM_BAN_AFTER = env_int("TEAM_BAN_AFTER", 100)  # rejected requests per minute
TEAM_BAN_SECONDS = env_int("TEAM_BAN_SECONDS", 300)

# The round agent writes the running team's uuid here at deploy; read per
# request (cached by mtime). No file / empty => no active round.
CURRENT_TEAM_FILE = os.environ.get("CURRENT_TEAM_FILE", "/etc/sandbox-api/current-team")
# Devbox attribution (docs/devbox.md): `<source-ip> <team-uuid>` per line,
# written when devboxes are provisioned. A request whose source address (the
# team's ipfilter-pinned devbox gateway) is listed is a DEVBOX submission for
# that team -- attributed by address, concurrent with and independent of the
# single running round (which uses CURRENT_TEAM_FILE). This is what keeps a
# team's devbox from interfering with the round, and one team from submitting
# as another.
DEVBOX_TEAMS_FILE = os.environ.get("DEVBOX_TEAMS_FILE", "/etc/sandbox-api/devbox-teams")
# nginx passes the real client (gateway) address in this header; only nginx can
# reach the app socket, so it is trusted.
CLIENT_IP_HEADER = os.environ.get("CLIENT_IP_HEADER", "X-Client-IP")
UUID_RE = re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")

# Every other column has a database default (processed, created_at); the
# logging attempt's uuid is generated here (no database default on SQLite).
INSERT = {
    "scoring": "INSERT INTO flag_capture_attempts (team_uuid, vm_flag_hash, devbox) VALUES (%s, %s, %s)",
    "logging": "INSERT INTO logging_attempts (uuid, team_uuid, log_data, devbox) VALUES (%s, %s, %s, %s)",
}[KIND]

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_BYTES
log = logging.getLogger("sandbox-api")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

_pool = None
_pool_lock = threading.Lock()


def pool():
    """Lazily created, so the service starts even while the database is down."""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadedConnectionPool(
                minconn=1,
                maxconn=env_int("DB_POOL_SIZE", 4),
                host=os.environ["DB_HOST"],
                port=env_int("DB_PORT", 5432),
                dbname=os.environ["DB_NAME"],
                user=os.environ["DB_USER"],
                password=os.environ["DB_PASSWORD"],
                sslmode=os.environ.get("DB_SSLMODE", "verify-full"),
                sslrootcert=os.environ.get("DB_SSLROOTCERT", "/etc/ssl/certs/ca-certificates.crt"),
                connect_timeout=5,
                options="-c statement_timeout=2000",
                application_name=f"sandbox-api-{KIND}",
            )
        return _pool


def execute(query, params=()):
    conn = pool().getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall() if cur.description else None
    except psycopg2.OperationalError:
        pool().putconn(conn, close=True)
        conn = None
        raise
    finally:
        if conn is not None:
            pool().putconn(conn)


# ---------------------------------------------------------------------------
# Current team and rate limiting
# ---------------------------------------------------------------------------


class CurrentTeam:
    """The running team's uuid, from CURRENT_TEAM_FILE (the round agent writes
    it at deploy). Cached and only re-read when the file's mtime changes, so
    the hot path does no I/O once warm. Returns None when no round is active."""

    def __init__(self, path):
        self._path = path
        self._lock = threading.Lock()
        self._value = None
        self._mtime = None

    def get(self):
        try:
            mtime = os.stat(self._path).st_mtime
        except OSError:
            return None
        if mtime != self._mtime:
            with self._lock:
                try:
                    text = open(self._path).read().strip()
                except OSError:
                    return self._value
                self._value = text if UUID_RE.match(text) else None
                self._mtime = mtime
        return self._value


class DevboxTeams:
    """Map of devbox gateway source address -> team uuid, from
    DEVBOX_TEAMS_FILE (`<ip> <uuid>` per line). Cached by mtime; returns None
    for an address that is not a known devbox gateway."""

    def __init__(self, path):
        self._path = path
        self._lock = threading.Lock()
        self._map = {}
        self._mtime = None

    def team_for(self, source_ip):
        if not source_ip:
            return None
        try:
            mtime = os.stat(self._path).st_mtime
        except OSError:
            return None
        if mtime != self._mtime:
            with self._lock:
                mapping = {}
                try:
                    for line in open(self._path):
                        parts = line.split()
                        if len(parts) == 2 and UUID_RE.match(parts[1]):
                            mapping[parts[0]] = parts[1]
                except OSError:
                    return self._map.get(source_ip)
                self._map = mapping
                self._mtime = mtime
        return self._map.get(source_ip)


class TeamLimiter:
    """Token bucket per team, plus a temporary ban for teams that keep pushing."""

    def __init__(self):
        self._lock = threading.Lock()
        self._buckets = {}  # team -> (tokens, last refill)
        self._rejects = {}  # team -> (count, window start)
        self._banned_until = {}

    def allow(self, team):
        now = time.monotonic()
        with self._lock:
            if self._banned_until.get(team, 0) > now:
                return False, int(self._banned_until[team] - now)

            tokens, last = self._buckets.get(team, (TEAM_BURST, now))
            tokens = min(TEAM_BURST, tokens + (now - last) * TEAM_RATE_PER_MINUTE / 60)
            if tokens >= 1:
                self._buckets[team] = (tokens - 1, now)
                return True, 0
            self._buckets[team] = (tokens, now)

            count, start = self._rejects.get(team, (0, now))
            if now - start > 60:
                count, start = 0, now
            count += 1
            self._rejects[team] = (count, start)
            if count >= TEAM_BAN_AFTER:
                self._banned_until[team] = now + TEAM_BAN_SECONDS
                self._rejects.pop(team, None)
                log.warning("team %s banned for %ss (rate limit exceeded %s times)", team, TEAM_BAN_SECONDS, count)
                return False, TEAM_BAN_SECONDS
            return False, int(60 / max(TEAM_RATE_PER_MINUTE, 1)) + 1


class LimitedLog:
    """At most `per_minute` log lines, so a flood cannot fill the disk."""

    def __init__(self, per_minute=30):
        self._lock = threading.Lock()
        self._per_minute = per_minute
        self._count, self._start = 0, time.monotonic()

    def warning(self, message, *args):
        with self._lock:
            now = time.monotonic()
            if now - self._start > 60:
                if self._count > self._per_minute:
                    log.warning("suppressed %s rejected-request messages", self._count - self._per_minute)
                self._count, self._start = 0, now
            self._count += 1
            if self._count <= self._per_minute:
                log.warning(message, *args)


CURRENT_TEAM = CurrentTeam(CURRENT_TEAM_FILE)
DEVBOX_TEAMS = DevboxTeams(DEVBOX_TEAMS_FILE)
LIMITER = TeamLimiter()
REJECTS = LimitedLog()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def error(status, message, **headers):
    response = jsonify({"status": "error", "message": message})
    response.status_code = status
    for key, value in headers.items():
        response.headers[key.replace("_", "-")] = str(value)
    return response


def bounded_json(value, depth=0, budget=None):
    """True if `value` stays within the depth and item limits."""
    budget = budget if budget is not None else [LOG_MAX_ITEMS]
    if depth > LOG_MAX_DEPTH:
        return False
    if isinstance(value, dict):
        items = list(value.items())
    elif isinstance(value, list):
        items = list(enumerate(value))
    else:
        return True
    budget[0] -= len(items)
    if budget[0] < 0:
        return False
    return all(bounded_json(v, depth + 1, budget) for _, v in items)


def validate_payload(data):
    """Return (value to insert, None) or (None, error message)."""
    if KIND == "scoring":
        flag = data.get("vm_flag")
        if not isinstance(flag, str) or not 0 < len(flag) <= MAX_FLAG_LENGTH or not flag.isprintable():
            return None, f"vm_flag must be a printable string of 1-{MAX_FLAG_LENGTH} characters."
        return hashlib.sha256(flag.encode()).hexdigest(), None

    log_data = data.get("log_data")
    if not isinstance(log_data, dict) or not log_data:
        return None, "log_data must be a non-empty JSON object."
    if len(json.dumps(log_data, separators=(",", ":"))) > LOG_MAX_BYTES:
        return None, f"log_data exceeds {LOG_MAX_BYTES} bytes."
    if not bounded_json(log_data):
        return None, f"log_data exceeds depth {LOG_MAX_DEPTH} or {LOG_MAX_ITEMS} items."
    return Json(log_data), None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.post("/submit")
def submit():
    if not request.is_json:
        return error(415, "Content-Type must be application/json.")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return error(400, "Body must be a JSON object.")

    # A request from a known devbox gateway address is a devbox submission for
    # that team (concurrent, source-attributed); otherwise it belongs to the
    # single running round (current-team file). The two never mix.
    source_ip = request.headers.get(CLIENT_IP_HEADER, "").strip()
    team = DEVBOX_TEAMS.team_for(source_ip)
    is_devbox = team is not None
    if team is None:
        team = CURRENT_TEAM.get()
    if team is None:
        REJECTS.warning("rejected: no active round and not a devbox source")
        return error(409, "No active round.")

    # Rate-limit bucket keyed per team; devbox and round buckets are distinct
    # so one cannot exhaust the other.
    bucket = f"devbox:{team}" if is_devbox else team
    allowed, retry_after = LIMITER.allow(bucket)
    if not allowed:
        REJECTS.warning("rejected: %s rate limited", bucket)
        return error(429, "Rate limit exceeded.", Retry_After=retry_after)

    value, problem = validate_payload(data)
    if problem:
        return error(400, problem)

    try:
        if KIND == "scoring":
            params = (team, value, is_devbox)
        else:
            params = (str(uuid.uuid4()), team, value, is_devbox)
        execute(INSERT, params)
    except Exception:
        log.exception("insert failed")
        return error(503, "Temporarily unavailable.")

    return jsonify({"status": "success", "message": "Submission stored."}), 201


@app.errorhandler(400)
def bad_request(_):
    return error(400, "Bad request.")


@app.errorhandler(404)
def not_found(_):
    return error(404, "Not found.")


@app.errorhandler(405)
def method_not_allowed(_):
    return error(405, "Method not allowed.")


@app.errorhandler(413)
def too_large(_):
    return error(413, f"Request body exceeds {MAX_BODY_BYTES} bytes.")


@app.errorhandler(Exception)
def unexpected(e):
    log.exception("unhandled error")
    return error(500, "Internal server error.")
