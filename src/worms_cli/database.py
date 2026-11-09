"""`worms show database [<table>]`: the platform's database, as the CLI's admin role sees it
(worms_cli/django_setup.py). Read only. A table's name is checked against the tables there are
before it reaches SQL; values are shown shortened, one row per line."""

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
        raise KeyError(f"no table {table!r}: `worms show database` lists them")
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


def _cell(value) -> str:
    if value is None:
        return "∅"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = str(value).replace("\n", "⏎")
    return text if len(text) <= CELL else text[: CELL - 1] + "…"
