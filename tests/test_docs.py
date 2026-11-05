"""The commands the documentation tells you to run: the CLI has to accept them.

The CLI replaced most of the Makefile, and the docs went on naming targets that
were gone and arguments that were refused, because nothing checked. Dated run logs
and research notes (docs/archive/) are records of what was typed at the time, and
stay as they are.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import re
import shlex
import sys

import pytest

from conftest import REPO

sys.path.insert(0, str(REPO / "src"))

from worms_cli import pipeline  # noqa: E402

DOCS = [
    REPO / "README.md",
    *sorted((REPO / "docs").glob("*.md")),
    *sorted((REPO / "helpers").rglob("*.md")),
]
SHELL_OPERATORS = {"<", ">", "|", "&&", ";", "2>&1"}


def parser():
    spec = importlib.util.spec_from_file_location("worms_manage", REPO / "src" / "manage.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    return cli.build_parser()


def documented_commands(text):
    """`worms ...` as inline code, and lines of a code block that start with it."""
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif fenced:
            if line.startswith("worms "):
                yield number, line
        else:
            yield from ((number, span) for span in re.findall(r"`(worms [^`]+)`", line))


def arguments(command):
    """What the CLI would get, or None for a pattern such as `worms build <component>`."""
    words = shlex.split(command, comments=True)[1:]
    end = next((i for i, word in enumerate(words) if word in SHELL_OPERATORS), len(words))
    words = words[:end]
    if any(word == "/" or word == "..." or word[0] in "<[{" for word in words):
        return None
    return words


def refusal(argv, build=parser()):
    """Why the CLI refuses these arguments. A missing argument is fine: prose names
    `worms secrets issue` without saying which token."""
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            build.parse_args(argv)
    except SystemExit:
        message = err.getvalue()
        if "invalid choice" in message or "unrecognized arguments" in message:
            return message.strip().splitlines()[-1]
    return None


@pytest.mark.parametrize("doc", DOCS, ids=lambda path: str(path.relative_to(REPO)))
def test_the_cli_accepts_what_the_docs_say_to_run(doc):
    refused = []
    for number, command in documented_commands(doc.read_text()):
        argv = arguments(command)
        reason = refusal(argv) if argv is not None else None
        if reason:
            refused.append(f"{doc.name}:{number}: {command}: {reason}")
    assert not refused, "\n".join(refused)


def test_the_check_would_catch_the_mistakes_it_is_for():
    for command in ("worms simulate -- -e x=1", "worms internet-deny", "worms test -k shipper", "worms --dry-run status"):
        assert refusal(arguments(command)), command
    for command in ("worms build simulate -- -e x=1", "worms test -- -k shipper", 'worms teams delete "Neural Net"  # x',
                    "worms secrets set ssh-key < key", "worms secrets issue"):  # fmt: skip
        assert refusal(arguments(command)) is None, command
    assert arguments("worms build <component>") is None


def test_nothing_names_a_make_target_that_is_gone():
    targets = set(re.findall(r"^([a-z_]+):", (REPO / "Makefile").read_text(), re.M))
    gone = (set(pipeline.COMPONENTS) | {"test", "debug", "apply"}) - targets
    pattern = re.compile(rf"\bmake ({'|'.join(sorted(gone, key=len, reverse=True))})\b")
    files = [
        *DOCS,
        REPO / "pytest.ini",
        REPO / "tests" / "conftest.py",
        *(path for path in (REPO / "ansible").glob("*.yml") if path.name != "inventory.generated.yml"),
        *(REPO / "helpers").rglob("*.sh"),
        *(REPO / "opentofu" / "stacks").glob("*/main.tf"),
        *(REPO / "scripts").rglob("*.py"),
        *(REPO / "src").rglob("*.py"),
    ]
    stale = [
        f"{path.relative_to(REPO)}:{number}: {line.strip()}"
        for path in files
        for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1)
        if pattern.search(line)
    ]
    assert not stale, "\n".join(stale)
