"""zeek-shipper: Zeek's JSON logs -> PostgreSQL (network_flows, network_events).

Runs on the monitor VM next to Zeek, as its own user: Zeek (which parses the
hostile traffic) never sees the database password, and this process never
sees a packet, only Zeek's log files (group-readable).

  conn.log                 -> network_flows (one row per connection)
  EVENT_LOGS (dns, http,   -> network_events (log name, ts, uid, record as
  ssl, ssh, ..., stats)       JSON, size-limited)

Delivery: at least once. A file position is saved (STATE_FILE) only after
the rows read up to it are committed; after a crash the rest is re-read.
network_flows.uid is unique and inserted with ON CONFLICT DO NOTHING, so a
connection is never stored twice; an event may be (rarely).

Zeek rotates its logs (Log::default_rotation_interval) by moving them into
ARCHIVE_DIR. Files are followed by inode, so a rotated file is read to its
end before the new one is started, also across restarts of this process.

Everything in the logs is attacker-controlled (addresses, HTTP headers, DNS
names, ...): values are type-checked and size-limited before they reach the
database, and the database role may only INSERT into the two tables. A flood
is throttled by per-second budgets: what is over budget stays in Zeek's log
files (on disk, kept for the archive retention) and is shipped later. The
backlog is reported in a `shipper` event every REPORT_SECONDS (a heartbeat).

Standard library + psycopg2 (Debian: python3-psycopg2).
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import logging
import math
import os
import signal
import time
from pathlib import Path

log = logging.getLogger("zeek-shipper")

FLOW_COLUMNS = (
    "uid", "community_id", "ts", "duration",
    "src_ip", "src_port", "dst_ip", "dst_port", "src_mac", "dst_mac",
    "transport", "service",
    "src_bytes", "dst_bytes", "src_pkts", "dst_pkts", "src_ip_bytes", "dst_ip_bytes",
    "missed_bytes", "conn_state", "history",
)  # fmt: skip
EVENT_COLUMNS = ("log", "ts", "uid", "data")

TRANSPORTS = {"tcp", "udp", "icmp", "unknown_transport"}
MAX_INT = 2**63 - 1
# Rows with a timestamp outside this window are garbage (or a forged pcap).
MIN_TS = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc).timestamp()
MAX_TS_AHEAD = 7 * 24 * 3600


class Invalid(ValueError):
    """A record that cannot be stored (counted, then skipped)."""


# ---------------------------------------------------------------------------
# Record parsing (pure functions, tested offline: tests/test_zeek_shipper.py)
# ---------------------------------------------------------------------------


def _timestamp(value, now=None):
    now = time.time() if now is None else now
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise Invalid(f"bad timestamp {value!r}")
    if not MIN_TS <= value <= now + MAX_TS_AHEAD:
        raise Invalid(f"timestamp out of range {value!r}")
    return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc)


def _text(value, limit, required=False):
    if value is None or value == "" or value == "-":
        if required:
            raise Invalid("missing text")
        return None
    if isinstance(value, list):  # e.g. service as a set in some versions
        value = ",".join(str(v) for v in value)
    if not isinstance(value, str):
        value = str(value)
    value = value.replace("\x00", "")
    return value[:limit]


def _int(value, low=0, high=MAX_INT):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Invalid(f"bad integer {value!r}")
    if isinstance(value, float):
        if not math.isfinite(value) or value != int(value):
            raise Invalid(f"bad integer {value!r}")
        value = int(value)
    if not low <= value <= high:
        raise Invalid(f"integer out of range {value!r}")
    return value


def _float(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise Invalid(f"bad duration {value!r}")
    return float(value)


def _ip(value):
    try:
        return str(ipaddress.ip_address(value))
    except (ValueError, TypeError):
        raise Invalid(f"bad address {value!r}") from None


def parse_conn(record: dict, now=None) -> tuple:
    """A conn.log record -> a network_flows row (FLOW_COLUMNS order)."""
    if not isinstance(record, dict):
        raise Invalid("not an object")
    transport = record.get("proto")
    if transport not in TRANSPORTS:
        transport = "unknown_transport"
    return (
        _text(record.get("uid"), 64, required=True),
        _text(record.get("community_id"), 64),
        _timestamp(record.get("ts"), now),
        _float(record.get("duration")),
        _ip(record.get("id.orig_h")),
        _int(record.get("id.orig_p"), 0, 65535),
        _ip(record.get("id.resp_h")),
        _int(record.get("id.resp_p"), 0, 65535),
        _text(record.get("orig_l2_addr"), 17),
        _text(record.get("resp_l2_addr"), 17),
        transport,
        _text(record.get("service"), 128),
        _int(record.get("orig_bytes")),
        _int(record.get("resp_bytes")),
        _int(record.get("orig_pkts")),
        _int(record.get("resp_pkts")),
        _int(record.get("orig_ip_bytes")),
        _int(record.get("resp_ip_bytes")),
        _int(record.get("missed_bytes")),
        _text(record.get("conn_state"), 16),
        _text(record.get("history"), 256),
    )


def _clean(value, string_limit, list_limit, depth=0):
    """JSON value with strings/lists cut to the limits and NUL removed
    (PostgreSQL jsonb rejects \\u0000)."""
    if depth > 6:
        return None
    if isinstance(value, str):
        return value.replace("\x00", "")[:string_limit]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {
            str(k).replace("\x00", "")[:64]: _clean(v, string_limit, list_limit, depth + 1)
            for k, v in list(value.items())[:list_limit]
        }
    if isinstance(value, list):
        return [_clean(v, string_limit, list_limit, depth + 1) for v in value[:list_limit]]
    return value


def shrink(record: dict, max_bytes: int) -> dict:
    """The record, made to fit into max_bytes of JSON."""
    for string_limit, list_limit in ((4096, 64), (512, 32), (128, 8), (32, 4)):
        data = _clean(record, string_limit, list_limit)
        if len(json.dumps(data, separators=(",", ":"))) <= max_bytes:
            return data
    return {"_truncated": True, "_fields": sorted(str(k)[:64] for k in record)[:32]}


def parse_event(name: str, record: dict, max_bytes: int, now=None) -> tuple:
    """A record of another Zeek log -> a network_events row."""
    if not isinstance(record, dict):
        raise Invalid("not an object")
    ts = record.get("ts")
    ts = _timestamp(ts, now) if ts is not None else dt.datetime.now(dt.timezone.utc)
    data = {k: v for k, v in record.items() if k not in ("ts", "uid")}
    return (name, ts, _text(record.get("uid"), 64), shrink(data, max_bytes))


# ---------------------------------------------------------------------------
# Following log files across rotation and restarts
# ---------------------------------------------------------------------------


class Follower:
    """Reads complete lines of LOG_DIR/<name>.log, following rotation.

    `position()` is what is saved after a commit: (device, inode, offset) of
    the file being read. `rewind()` goes back to the last committed position.
    """

    def __init__(self, name, log_dir, archive_dir, saved=None):
        self.name = name
        self.path = Path(log_dir) / f"{name}.log"
        self.archive_dir = Path(archive_dir)
        self.file = None
        self.ident = None  # (st_dev, st_ino) of self.file
        self.offset = 0  # bytes consumed (complete lines)
        self.committed = None  # (ident, offset) after the last commit
        if saved:
            self._resume(tuple(saved["ident"]), saved["offset"])

    # -- opening ----------------------------------------------------------

    def _open(self, path, offset=0):
        f = open(path, "rb")  # noqa: SIM115 (kept open while following)
        st = os.fstat(f.fileno())
        if offset > st.st_size:  # not the file we read before (inode reused)
            offset = 0
        f.seek(offset)
        if self.file:
            self.file.close()
        self.file, self.ident, self.offset = f, (st.st_dev, st.st_ino), offset

    def _resume(self, ident, offset):
        """Continue a file read before the restart, wherever it is now."""
        candidates = [self.path]
        if self.archive_dir.is_dir():
            candidates += sorted(self.archive_dir.glob(f"{self.name}.*"))
        candidates += sorted(self.path.parent.glob(f"{self.name}.*.log"))
        for path in candidates:
            try:
                st = path.stat()
            except FileNotFoundError:
                continue
            if (st.st_dev, st.st_ino) == ident:
                self._open(path, offset)
                self.committed = (self.ident, self.offset)
                return
        log.warning("%s: the file read before the restart is gone; starting with the current one", self.name)

    def _current_ident(self):
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return None
        return (st.st_dev, st.st_ino)

    # -- reading ----------------------------------------------------------

    def read(self, max_lines):
        """Up to max_lines complete lines (bytes, without the newline)."""
        lines = []
        while len(lines) < max_lines:
            if self.file is None:
                if self._current_ident() is None:
                    break
                self._open(self.path)
            line = self.file.readline()
            if line.endswith(b"\n"):
                self.offset += len(line)
                lines.append(line[:-1])
                continue
            # End of the file (or a line still being written).
            self.file.seek(self.offset)
            current = self._current_ident()
            if current is not None and current != self.ident:
                # Rotated: the old file is complete; when it is fully read,
                # continue with the new one.
                rest = self.file.read()
                if rest:
                    # A last line without newline: Zeek never leaves one,
                    # but do not lose it.
                    self.offset += len(rest)
                    lines.append(rest.rstrip(b"\n"))
                self._open(self.path)
                continue
            break
        return lines

    def backlog(self):
        """Bytes not read yet (the open file and, if rotated, the new one)."""
        total = 0
        if self.file is not None:
            total += max(0, os.fstat(self.file.fileno()).st_size - self.offset)
        current = self._current_ident()
        if current is not None and current != self.ident:
            total += self.path.stat().st_size
        return total

    def position(self):
        return None if self.ident is None else {"ident": list(self.ident), "offset": self.offset}

    def mark_committed(self):
        self.committed = (self.ident, self.offset)

    def rewind(self):
        """Back to the last committed position (after a failed insert)."""
        if self.committed is None:
            if self.file:
                self.file.close()
            self.file, self.ident, self.offset = None, None, 0
            return
        ident, offset = self.committed
        if ident == self.ident:
            self.file.seek(offset)
            self.offset = offset
        else:
            self._resume(ident, offset)


def load_state(path):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_state(path, followers):
    state = {f.name: f.position() for f in followers if f.position()}
    tmp = Path(f"{path}.tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


class Budget:
    """Token bucket: `rate` rows per second, at most `burst` at once."""

    def __init__(self, rate, burst):
        self.rate, self.burst = rate, burst
        self.tokens, self.last = burst, time.monotonic()

    def available(self):
        now = time.monotonic()
        self.tokens = min(self.burst, self.tokens + (now - self.last) * self.rate)
        self.last = now
        return int(self.tokens)

    def consume(self, n):
        self.tokens -= n


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


class Database:
    def __init__(self, env):
        self.env = env
        self.conn = None

    def connect(self):
        import psycopg2

        if self.conn is None or self.conn.closed:
            self.conn = psycopg2.connect(
                host=self.env["DB_HOST"],
                port=int(self.env.get("DB_PORT", "5432")),
                dbname=self.env["DB_NAME"],
                user=self.env["DB_USER"],
                password=self.env["DB_PASSWORD"],
                sslmode=self.env.get("DB_SSLMODE", "verify-full"),
                sslrootcert=self.env.get("DB_SSLROOTCERT", "/etc/ssl/certs/ca-certificates.crt"),
                connect_timeout=10,
                options="-c statement_timeout=30000",
                application_name="zeek-shipper",
            )
        return self.conn

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            finally:
                self.conn = None

    def insert(self, flows, events):
        """Insert both lists in one transaction. Returns the number of rows
        skipped because the database rejected them individually."""
        import psycopg2
        from psycopg2.extras import Json, execute_values

        flow_sql = (
            f"INSERT INTO network_flows ({', '.join(FLOW_COLUMNS)}) VALUES %s ON CONFLICT DO NOTHING"
        )
        event_sql = f"INSERT INTO network_events ({', '.join(EVENT_COLUMNS)}) VALUES %s"
        events = [(n, ts, uid, Json(data)) for n, ts, uid, data in events]

        conn = self.connect()
        try:
            with conn.cursor() as cur:
                if flows:
                    execute_values(cur, flow_sql, flows, page_size=1000)
                if events:
                    execute_values(cur, event_sql, events, page_size=1000)
            conn.commit()
            return 0
        except (psycopg2.DataError, psycopg2.IntegrityError):
            conn.rollback()
        # One bad row must not block the rest: row by row, each in a savepoint.
        skipped = 0
        with conn.cursor() as cur:
            for sql, rows in ((flow_sql, flows), (event_sql, events)):
                for row in rows:
                    cur.execute("SAVEPOINT row")
                    try:
                        execute_values(cur, sql, [row])
                        cur.execute("RELEASE SAVEPOINT row")
                    except (psycopg2.DataError, psycopg2.IntegrityError) as exc:
                        cur.execute("ROLLBACK TO SAVEPOINT row")
                        skipped += 1
                        log.warning("row rejected by the database: %s", str(exc).splitlines()[0])
        conn.commit()
        return skipped


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


class Shipper:
    def __init__(self, env, db=None):
        self.env = env
        self.log_dir = env.get("ZEEK_LOG_DIR", "/var/lib/zeek/logs")
        self.archive_dir = env.get("ZEEK_ARCHIVE_DIR", "/var/lib/zeek/archive")
        self.state_file = env.get("STATE_FILE", "/var/lib/zeek-shipper/state.json")
        self.batch = int(env.get("BATCH_SIZE", "2000"))
        self.poll = float(env.get("POLL_SECONDS", "1"))
        self.max_event_bytes = int(env.get("MAX_EVENT_BYTES", "4096"))
        self.report_every = float(env.get("REPORT_SECONDS", "60"))
        event_logs = [n.strip() for n in env.get("EVENT_LOGS", "").split(",") if n.strip()]

        state = load_state(self.state_file)
        self.followers = [
            Follower(name, self.log_dir, self.archive_dir, state.get(name))
            for name in ["conn", *event_logs]
        ]
        self.flow_budget = Budget(float(env.get("MAX_FLOWS_PER_SECOND", "2000")), float(env.get("FLOW_BURST", "20000")))
        self.event_budget = Budget(float(env.get("MAX_EVENTS_PER_SECOND", "1000")), float(env.get("EVENT_BURST", "10000")))
        self.db = db or Database(env)
        self.counts = {"flows": 0, "events": 0, "invalid": 0, "rejected": 0}
        self.last_report = time.monotonic()
        self.running = True

    def collect(self):
        flows, events = [], []
        for follower in self.followers:
            budget = self.flow_budget if follower.name == "conn" else self.event_budget
            lines = follower.read(min(self.batch, budget.available()))
            budget.consume(len(lines))
            for line in lines:
                try:
                    record = json.loads(line.decode("utf-8", errors="replace"))
                    if follower.name == "conn":
                        flows.append(parse_conn(record))
                    else:
                        events.append(parse_event(follower.name, record, self.max_event_bytes))
                except (Invalid, ValueError, RecursionError):
                    self.counts["invalid"] += 1
        return flows, events

    def report_event(self):
        """A `shipper` event every REPORT_SECONDS: heartbeat, counts since the
        last one, and the backlog (bytes of Zeek logs not shipped yet)."""
        now = time.monotonic()
        if now - self.last_report < self.report_every:
            return []
        self.last_report = now
        data = {
            **self.counts,
            "interval_seconds": self.report_every,
            "backlog_bytes": {f.name: f.backlog() for f in self.followers},
            "host": os.uname().nodename,
        }
        log.info("shipped %s", data)
        self.counts = dict.fromkeys(self.counts, 0)
        return [("shipper", dt.datetime.now(dt.timezone.utc), None, data)]

    def step(self):
        """One round: read, insert, save the positions. True if work was done."""
        flows, events = self.collect()
        events += self.report_event()
        if not flows and not events:
            for follower in self.followers:
                follower.mark_committed()
            return False
        try:
            self.counts["rejected"] += self.db.insert(flows, events)
        except Exception:
            for follower in self.followers:
                follower.rewind()
            self.db.close()
            raise
        for follower in self.followers:
            follower.mark_committed()
        save_state(self.state_file, self.followers)
        self.counts["flows"] += len(flows)
        self.counts["events"] += sum(1 for e in events if e[0] != "shipper")
        return True

    def run(self):
        backoff = 1
        while self.running:
            try:
                busy = self.step()
                backoff = 1
            except Exception as exc:  # database down, TLS, ...: retry
                log.warning("insert failed (%s); retrying in %ss", str(exc).splitlines()[0] if str(exc) else type(exc).__name__, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            if not busy:
                time.sleep(self.poll)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    shipper = Shipper(dict(os.environ))

    def stop(*_):
        shipper.running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("following %s in %s", ", ".join(f.name for f in shipper.followers), shipper.log_dir)
    shipper.run()


if __name__ == "__main__":
    main()
