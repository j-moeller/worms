"""Tiny terminal formatting helpers (stdlib only): colours, tables, status
dots. The CLI runs on a laptop; no third-party dependency is required for its
output."""

from __future__ import annotations

import os
import sys

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

_CODES = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "cyan": "\033[36m",
    "grey": "\033[90m",
}


def color(text: str, *names: str) -> str:
    if not _COLOR or not names:
        return text
    prefix = "".join(_CODES.get(n, "") for n in names)
    return f"{prefix}{text}{_CODES['reset']}"


def heading(text: str) -> str:
    return color(f"── {text} ", "bold", "cyan")


STATUS_STYLE = {
    "ok": ("green", "●"),
    "running": ("green", "●"),
    "active": ("green", "●"),
    "up": ("green", "●"),
    "warn": ("yellow", "●"),
    "stopped": ("grey", "○"),
    "down": ("red", "●"),
    "error": ("red", "✗"),
    "unknown": ("grey", "?"),
    "n/a": ("grey", "·"),
}


def dot(status: str) -> str:
    style, glyph = STATUS_STYLE.get(status, ("grey", "·"))
    return color(glyph, style)


def status_cell(status: str, label: str | None = None) -> str:
    return f"{dot(status)} {label if label is not None else status}"


def table(rows: list[list[str]], headers: list[str] | None = None) -> str:
    """Render a simple left-aligned table. Colour codes are ignored for width."""
    import re

    strip = re.compile(r"\033\[[0-9;]*m")

    def width(cell: str) -> int:
        return len(strip.sub("", cell))

    all_rows = ([headers] if headers else []) + rows
    if not all_rows:
        return ""
    cols = max(len(r) for r in all_rows)
    widths = [0] * cols
    for row in all_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], width(str(cell)))

    def fmt(row: list[str]) -> str:
        out = []
        for i in range(cols):
            cell = str(row[i]) if i < len(row) else ""
            pad = widths[i] - width(cell)
            out.append(cell + " " * pad)
        return "  ".join(out).rstrip()

    lines = []
    if headers:
        lines.append(color(fmt(headers), "bold"))
        lines.append(color("  ".join("─" * w for w in widths), "grey"))
    lines.extend(fmt(row) for row in rows)
    return "\n".join(lines)


def warn(message: str) -> None:
    print(color(f"warning: {message}", "yellow"), file=sys.stderr)


def error(message: str) -> None:
    print(color(f"error: {message}", "red"), file=sys.stderr)
