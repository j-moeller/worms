"""`worms upgrade` (src/worms_cli/upgrade.py): its order, --resume after the code step, and its
refusals, on fakes of what it does to the world (git, the CLI it runs, the host's records)."""

from __future__ import annotations

import sys

import pytest

from conftest import REPO

sys.path.insert(0, str(REPO / "src"))

from worms_cli import upgrade  # noqa: E402

A, B = "a000001", "b000002"


class NewCode(Exception):
    """continue_with_new_code: the process would be the new code's from here."""


class FakeOps:
    def __init__(self, blockers=(), verified=True):
        self.head, self.calls, self.progress = A, [], {}
        self._blockers, self.verified, self.interrupt, self.behind = list(blockers), verified, False, False

    def git(self, *args):
        self.calls.append(("git", *args[:2]))
        if args[:2] == ("rev-parse", "--verify"):
            return 0, B
        if args[:1] == ("rev-parse",):
            return 0, self.head
        if args[:2] == ("merge-base", "--is-ancestor"):
            return (1 if self.behind else 0), ""
        if args[:2] == ("merge", "--ff-only"):
            self.head = B
            return 0, "Fast-forward"
        return 0, ""

    def worms(self, *args):
        self.calls.append(("worms", *args))
        if args[:1] == ("apply",) and len(args) == 2 and self.interrupt:
            self.interrupt = False
            raise KeyboardInterrupt
        return 0

    def worms_json(self, *args):
        line = f"● system: on {B}" if self.verified else "● system: mixed"
        return {"sections": [{"heading": "version", "lines": [line], "tables": []},
                             {"heading": "attention", "lines": [], "tables": [[["", "● nothing found"]]]}]}  # fmt: skip

    def blockers(self):
        self.calls.append(("blockers",))
        return self._blockers

    def backup(self):
        self.calls.append(("backup",))
        return ["/root/.worms/backups/dump", "/root/.worms/backups/state.tar.gz"]

    def read(self):
        return dict(self.progress)

    def write(self, record):
        self.progress = dict(record)

    def continue_with_new_code(self, args):
        self.calls.append(("new code", *args))
        raise NewCode


def _steps(ops):
    return [c for c in ops.calls if c[0] in ("backup", "worms", "new code") or c[:2] == ("git", "merge")]


def test_an_upgrade_runs_its_steps_in_order_and_the_rest_with_the_new_code(capsys):
    ops = FakeOps()
    with pytest.raises(NewCode):
        upgrade.run(None, "main", yes=True, ops=ops)
    assert _steps(ops) == [("backup",), ("git", "merge", "--ff-only"), ("new code", "--resume", "--yes")]
    assert ops.progress["steps_done"] == ["preflight", "backup", "code"] and ops.head == B
    # The new code goes on after the code step: migrations first, then what changed, then the workspace.
    ops.calls.clear()
    assert upgrade.run(None, None, resume=True, yes=True, ops=ops) == 0
    assert _steps(ops) == [("worms", "apply", "orchestrator", "--yes"), ("worms", "apply", "--yes"),
                           ("worms", "apply", "round-agent", "gameplans", "--yes")]  # fmt: skip
    assert ops.progress["finished"] and f"system: on {B}" in capsys.readouterr().out
    # Once more: nothing to do.
    ops.calls.clear()
    assert upgrade.run(None, "main", yes=True, ops=ops) == 0
    assert _steps(ops) == [] and f"already on {B}" in capsys.readouterr().out


def test_an_interrupted_upgrade_resumes_after_its_last_step():
    ops = FakeOps()
    ops.head = B
    ops.progress = {"target": B, "how": "rev", "from": A, "steps_done": ["preflight", "backup", "code", "orchestrator"],
                    "kept": ["d", "t"]}  # fmt: skip
    assert upgrade.run(None, None, resume=True, yes=True, ops=ops) == 0
    assert _steps(ops) == [("worms", "apply", "--yes"), ("worms", "apply", "round-agent", "gameplans", "--yes")]


def test_an_upgrade_with_a_sandbox_refuses_before_changing_anything(capsys):
    ops = FakeOps(blockers=["a round's sandbox exists (20 VMs): finish the round, then `worms round teardown`"])
    assert upgrade.run(None, "main", yes=True, ops=ops) == 1
    assert _steps(ops) == [] and ops.head == A
    assert "worms round teardown" in capsys.readouterr().out and ops.progress == {}


def test_an_upgrade_that_does_not_verify_ends_failed(capsys):
    ops = FakeOps(verified=False)
    ops.head = B
    ops.progress = {"target": B, "how": "rev", "from": A, "steps_done": ["preflight", "backup", "code", "orchestrator",
                                                                            "apply", "round-agent"]}  # fmt: skip
    assert upgrade.run(None, None, resume=True, yes=True, ops=ops) == 1
    out = capsys.readouterr().out
    assert "system: mixed" in out and "worms upgrade --resume" in out and not ops.progress.get("finished")


def test_a_ctrl_c_during_the_apply_stops_there_and_resume_goes_on_from_it(capsys):
    ops = FakeOps()
    ops.head, ops.interrupt = B, True
    ops.progress = {"target": B, "how": "rev", "from": A, "steps_done": ["preflight", "backup", "code"], "kept": ["d", "t"]}
    assert upgrade.run(None, None, resume=True, yes=True, ops=ops) == 130
    assert ops.progress["steps_done"] == ["preflight", "backup", "code", "orchestrator"]
    assert "stopped at apply" in capsys.readouterr().out
    ops.calls.clear()
    assert upgrade.run(None, None, resume=True, yes=True, ops=ops) == 0
    assert _steps(ops) == [("worms", "apply", "--yes"), ("worms", "apply", "round-agent", "gameplans", "--yes")]


def test_the_cli_it_runs_is_waited_for_on_ctrl_c_never_killed(monkeypatch, tmp_path):
    from types import SimpleNamespace

    class Child:
        waits, killed = 0, False

        def __init__(self, argv):
            pass

        def wait(self):
            Child.waits += 1
            if Child.waits == 1:
                raise KeyboardInterrupt  # the terminal's Ctrl-C: the child got it too, and stops its work
            return 130

        def kill(self):
            Child.killed = True

    monkeypatch.setattr(upgrade.subprocess, "Popen", Child)
    assert upgrade.Ops(SimpleNamespace(repo_root=tmp_path)).worms("apply") == 130
    assert Child.waits == 2 and not Child.killed


def test_a_bundle_that_is_no_fast_forward_is_refused_before_the_backup(capsys, tmp_path):
    bundle = tmp_path / "worms-update.bundle"
    bundle.write_text("")

    class Bundled(FakeOps):
        def git(self, *args):
            if args[:2] == ("bundle", "list-heads"):
                return 0, f"{B}9999 refs/heads/main"
            return super().git(*args)

    ops = Bundled()
    ops.behind = True
    assert upgrade.run(None, str(bundle), yes=True, ops=ops) == 1
    assert ("git", "fetch", "-q") in ops.calls and _steps(ops) == [] and ops.progress == {}
    assert "no fast-forward" in capsys.readouterr().out
