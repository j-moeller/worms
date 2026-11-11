"""Finding and loading gameplans: one file in the gameplans directory, one Gameplan class in it.

A gameplan is named by its file name and nothing else (`round_one.py`): that name is all the
dashboard ever stores (it shows a simulation of one too, made on the orchestrator:
gameplan/simulation.py). The file is read here only, on the node (the round agent) and on the
orchestrator; the dashboard VM has no copy and never loads one (docs/05-live-environment.md).

The name comes from the database, so it is untrusted: it must match NAME, and it must be a
regular file directly in the directory, listed by the directory itself. No path is built from
anything else, symlinks are refused (O_NOFOLLOW), and the bytes that were hashed are the bytes
that run.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import sys
import types
from dataclasses import dataclass
from pathlib import Path

from gameplan.definition import Gameplan, GameplanError

NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}\.py$")
MAX_SIZE = 1024 * 1024


@dataclass
class Loaded:
    filename: str
    sha256: str
    cls: type


def valid_name(filename) -> bool:
    return isinstance(filename, str) and bool(NAME.fullmatch(filename))


def available(directory) -> list[str]:
    """The gameplan files in `directory`: regular files with a valid name, not symlinks."""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    names = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if valid_name(entry.name) and entry.is_file(follow_symlinks=False):
                names.append(entry.name)
    return sorted(names)


def read(directory, filename) -> tuple[bytes, str]:
    """The file's bytes and their SHA-256."""
    if not valid_name(filename):
        raise GameplanError(f"{filename!r} is not a gameplan file name (lowercase, digits, _, ending in .py)")
    if filename not in available(directory):
        raise GameplanError(f"there is no gameplan {filename}")
    fd = os.open(Path(directory) / filename, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise GameplanError(f"{filename} is not a regular file")
        if info.st_size > MAX_SIZE:
            raise GameplanError(f"{filename} is larger than {MAX_SIZE} bytes")
        chunks = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
    finally:
        os.close(fd)
    source = b"".join(chunks)
    return source, hashlib.sha256(source).hexdigest()


def load(directory, filename, sha256=None) -> Loaded:
    """Run the file and return its Gameplan class. With `sha256`, the file must be exactly that
    one (the orchestrator and the node must run the same gameplan)."""
    source, digest = read(directory, filename)
    if sha256 is not None and digest != sha256:
        raise GameplanError(f"{filename} is not the version expected (sha256 {digest[:12]}, expected {sha256[:12]}): "
                            "deploy the same checkout to the node and the orchestrator")  # fmt: skip
    name = f"worms_gameplans.{filename[:-3]}_{digest[:12]}"
    module = types.ModuleType(name)
    module.__file__ = str(Path(directory) / filename)
    code = compile(source, module.__file__, "exec", dont_inherit=True)
    # Registered while it runs only (dataclasses and typing look a class's module up).
    sys.modules[name] = module
    try:
        exec(code, module.__dict__)  # noqa: S102  (a gameplan is code from the repository, on purpose)
    finally:
        sys.modules.pop(name, None)
    classes = [
        value for value in module.__dict__.values()
        if isinstance(value, type) and issubclass(value, Gameplan) and value.__module__ == name
    ]  # fmt: skip
    if len(classes) != 1:
        raise GameplanError(f"{filename} must define exactly one Gameplan subclass (found {len(classes)})")
    return Loaded(filename, digest, classes[0])
