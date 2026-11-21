"""`worms db`: the platform's database. Its tables (`worms db`) and a table's newest rows (`worms show
table <name>`), as the CLI's admin role sees them (worms_cli/django_setup.py): a table's name is
checked against the tables there are before it reaches SQL; values are shown shortened, one row per
line. `worms db backup`: a pg_dump of it, here; `worms db restore <dump>`: the database back to one."""

from __future__ import annotations

import json

CELL = 60  # characters of a value shown


def _cursor():
    from django.db import connection

    return connection.cursor()


def tables() -> list[list[str]]:
    """Every table: its rows (PostgreSQL's estimate) and size on disk."""
    with _cursor() as cur:
        cur.execute(
            "SELECT relname, n_live_tup, pg_size_pretty(pg_total_relation_size(relid)) FROM pg_stat_user_tables "
            "ORDER BY relname"
        )
        return [[name, str(rows), size] for name, rows, size in cur.fetchall()]


def _known(table: str) -> str:
    names = [row[0] for row in tables()]
    if table not in names:
        raise KeyError(f"no table {table!r}: `worms db` lists them")
    return table


def schema(table: str) -> tuple[list[list[str]], list[str]]:
    """(columns: name, type, null, default; indexes and constraints as SQL)."""
    table = _known(table)
    with _cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type, is_nullable, coalesce(column_default, '') FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
            [table],
        )
        columns = [[name, kind, "null" if nullable == "YES" else "not null", default] for name, kind, nullable, default in cur.fetchall()]
        cur.execute("SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = %s ORDER BY indexname", [table])
        extra = [row[0] for row in cur.fetchall()]
        cur.execute(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = %s::regclass AND contype IN ('c', 'f') "
            "ORDER BY conname",
            [table],
        )
        extra += [f"{name}: {definition}" for name, definition in cur.fetchall()]
    return columns, extra


def rows(table: str, limit: int = 20) -> tuple[list[str], list[list[str]]]:
    """(columns, the newest `limit` rows: by id when there is one)."""
    from django.db import connection

    table = _known(table)
    columns = [name for name, *_ in schema(table)[0]]
    order = f" ORDER BY {connection.ops.quote_name('id')} DESC" if "id" in columns else ""
    with _cursor() as cur:
        cur.execute(f"SELECT * FROM {connection.ops.quote_name(table)}{order} LIMIT %s", [int(limit)])
        return columns, [[_cell(value) for value in row] for row in cur.fetchall()]


BACKUPS = "~/.worms/backups"  # 0700; each dump 0600: it holds every record, password hashes too


def backup(config, path: str | None = None) -> tuple[str, int]:
    """`worms db backup`: `pg_dump -Fc` of the platform's database (ansible/group_vars/all.yml,
    postgres_db) on the database VM, streamed over its management SSH (as `worms login` gets
    there) into a new local file, readable by its owner only. (path, bytes). Nothing is written
    on the VM; a failed dump leaves no file."""
    import os
    import shlex
    import subprocess
    from datetime import datetime
    from pathlib import Path

    from worms_cli.build import BuildError
    from worms_cli.session import SessionError, group_var
    from worms_cli.vms import Catalogue

    catalogue = Catalogue(config)
    session = catalogue.runner.session
    try:
        name = group_var(config.repo_root, "postgres_db")
    except SessionError as exc:
        raise BuildError(str(exc)) from None
    vm = catalogue.resolve("database")
    if vm.get("status") != "running":
        raise BuildError(f"the database VM is {vm.get('status')}: `worms vm start database`")
    if path is None:
        try:
            system = session.environment()["node"]
        except SessionError:
            system = "worms"
        directory = Path(BACKUPS).expanduser()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        path = str(directory / f"{system}-{name}-{datetime.now():%Y%m%d-%H%M%S}.dump")
    argv, env = catalogue.ssh_argv(vm, f"cd / && sudo -n -u postgres pg_dump -Fc {shlex.quote(name)}", interactive=False)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # never over an existing file
    try:
        with os.fdopen(fd, "wb") as out:
            proc = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.PIPE)
        size = os.path.getsize(path)
        with open(path, "rb") as dump:
            magic = dump.read(5)
        if proc.returncode != 0 or magic != b"PGDMP":
            raise BuildError(f"pg_dump of {name} on {vm['name']} failed (exit {proc.returncode}, {size} bytes): "
                             f"{proc.stderr.decode(errors='replace').strip()[-300:] or 'not a pg_dump archive'}")  # fmt: skip
    except BaseException:
        os.unlink(path)
        raise
    return path, size


def _cell(value) -> str:
    if value is None:
        return "∅"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = str(value).replace("\n", "⏎")
    return text if len(text) <= CELL else text[: CELL - 1] + "…"


def restore(config, path: str) -> int:
    """`worms db restore <dump>`: the database back to the dump at `path`. The dashboard, the
    orchestrator's services and the round agent are stopped meanwhile (they hold connections), then
    started again. Everything written since the dump is lost."""
    import shlex
    import subprocess

    from worms_cli import output
    from worms_cli.build import BuildError
    from worms_cli.services import SERVICES
    from worms_cli.session import SessionError, group_var
    from worms_cli.vms import Catalogue

    with open(path, "rb") as dump:
        if dump.read(5) != b"PGDMP":
            raise BuildError(f"{path} is no pg_dump archive (`worms db backup` makes one)")
    catalogue = Catalogue(config)
    session = catalogue.runner.session
    if any(vm.get("pool") == "sandbox" for vm in catalogue.vms()):
        raise BuildError("refusing during a round: sandbox VMs exist")
    try:
        name = group_var(config.repo_root, "postgres_db")
    except SessionError as exc:
        raise BuildError(str(exc)) from None
    holders = ["agent", "processor", "conductor", "api", "dashboard"]

    def units(action: str, names) -> None:
        for service_name in names:
            service = SERVICES[service_name]
            command = f"systemctl {action} {' '.join(service.units)}"
            if service.where == "node":
                code, _out, err = session.node_run(["sh", "-c", command])
            else:
                vm = catalogue.resolve(service.where)
                argv, env = catalogue.ssh_argv(vm, f"sudo -n {command}", interactive=False)
                proc = subprocess.run(argv, env=env, capture_output=True, text=True)
                code, err = proc.returncode, proc.stderr
            print(output.status_cell("ok" if code == 0 else "error", f"{service_name}: {action}" + (f" ({err.strip()[:120]})" if code else "")),
                  flush=True)  # fmt: skip

    vm = catalogue.resolve("database")
    units("stop", holders)
    try:
        argv, env = catalogue.ssh_argv(vm, f"cd / && sudo -n -u postgres pg_restore --clean --if-exists --single-transaction "
                                           f"-d {shlex.quote(name)}", interactive=False)  # fmt: skip
        with open(path, "rb") as dump:
            proc = subprocess.run(argv, env=env, stdin=dump, capture_output=True)
        if proc.returncode != 0:
            raise BuildError(f"pg_restore failed (nothing changed: one transaction): {proc.stderr.decode(errors='replace').strip()[-400:]}")
        print(output.status_cell("ok", f"{name}: restored from {path}"))
    finally:
        units("start", list(reversed(holders)))
    print(output.color("The grants are the dump's; this checkout's: `worms apply database`.", "dim"))
    return 0
