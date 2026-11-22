"""Tiny terminal formatting helpers (stdlib only): colours, tables, status
dots. The CLI runs on a laptop; no third-party dependency is required for its
output.

With `--json` (capture_start, src/manage.py) nothing is drawn: what a command shows is gathered as
it shows it, in sections (a heading starts one), each with its tables (rows keyed by the table's
headers) and its other lines, and printed at the end as one JSON document (capture_result). A
command may add a field of its own beside them (capture_field: `worms check` gives `checks`)."""

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


_CAPTURE: dict | None = None  # --json: the document being gathered
_MARK = "\x00worms-captured\x00"  # what heading() and table() return then: their line is dropped


def capturing() -> bool:
    """Whether --json gathers what is shown (a command may then give rows where it draws lines)."""
    return _CAPTURE is not None


def capture_start() -> None:
    """From now on gather what is shown (tables, headings, lines) instead of drawing it."""
    global _CAPTURE, _COLOR
    _COLOR = False
    _CAPTURE = {"sections": [{"heading": None, "tables": [], "lines": []}]}


def _section() -> dict:
    return _CAPTURE["sections"][-1]


class Sink:
    """sys.stdout while capturing: each line printed goes to the current section."""

    def __init__(self):
        self.pending = ""

    def write(self, text: str) -> int:
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            self._line(line)
        return len(text)

    def _line(self, line: str) -> None:
        line = _strip(line).rstrip()
        if line and _MARK not in line:
            _section()["lines"].append(line)

    def flush(self) -> None:
        if self.pending:
            self._line(self.pending)
            self.pending = ""

    def isatty(self) -> bool:
        return False


def capture_field(name: str, value) -> None:
    """--json: a field of the document beside its sections, the command's own structured results (plain
    JSON values); nothing without --json. Never `command`, `exit` or `sections`."""
    if _CAPTURE is not None and name not in ("command", "exit", "sections"):
        _CAPTURE.setdefault("fields", {})[name] = value


def capture_result(command: str, code: int) -> dict:
    """The document: the command, its exit code, its sections (empty ones left out), its own fields."""
    sections = [s for s in _CAPTURE["sections"] if s["heading"] or s["tables"] or s["lines"]]
    return {"command": command, "exit": code, "sections": sections, **_CAPTURE.get("fields", {})}


def _strip(text: str) -> str:
    import re

    return re.sub(r"\033\[[0-9;]*m", "", str(text))


def heading(text: str) -> str:
    if _CAPTURE is not None:
        _CAPTURE["sections"].append({"heading": text.strip(), "tables": [], "lines": []})
        return _MARK
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

    if _CAPTURE is not None:
        keys = []
        for i, header in enumerate(headers or []):
            key = _strip(header).strip() or f"column {i + 1}"
            keys.append(key if key not in keys else f"{key} {i + 1}")
        cleaned = [[_strip(cell).strip() for cell in row] for row in rows]
        _section()["tables"].append([dict(zip(keys, row)) for row in cleaned] if headers else cleaned)
        return _MARK

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
