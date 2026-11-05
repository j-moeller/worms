"""The live journey (tests/live) needs a deployed platform, so what can break it offline is
checked here: that it stays out of the offline suite, that its scripts parse, and its
small pieces of logic. Offline part of `worms test`."""

from __future__ import annotations

import shutil
import subprocess
import sys

import pytest
from conftest import REPO, require

LIVE = REPO / "tests" / "live"
sys.path.insert(0, str(LIVE))

livelib = pytest.importorskip("livelib")


def run_pytest(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", *args], cwd=REPO, capture_output=True, text=True, timeout=300)


def test_without_the_option_nothing_of_the_journey_runs():
    """Run against the real system by accident, it would create accounts and VMs."""
    proc = run_pytest("tests/live")
    assert proc.returncode == 0, proc.stdout
    assert " passed" not in proc.stdout and " failed" not in proc.stdout
    assert "skipped" in proc.stdout and "--live" in proc.stdout


def test_the_option_selects_only_the_journey():
    """Offline tests point Django at a throwaway database: they must not share a run with it."""
    proc = run_pytest("--live", "--collect-only", "tests/test_live_suite.py", "tests/live")
    assert "test_devbox_journey" in proc.stdout
    assert "test_without_the_option" not in proc.stdout


@pytest.mark.parametrize("script", ["on_vm.sh", "vm_state.sh"])
def test_the_vm_scripts_parse(script):
    subprocess.run(["sh", "-n", str(LIVE / script)], check=True)


def test_the_vm_scripts_never_print_a_token():
    on_vm = (LIVE / "on_vm.sh").read_text()
    # The token only goes into the request to scoring; curl's output goes nowhere (-o /dev/null).
    assert "/etc/worms/flag" in on_vm and "-o /dev/null" in on_vm
    assert "cat /etc/worms/flag" not in on_vm
    assert "sha256sum /etc/worms/flag" in (LIVE / "vm_state.sh").read_text()


def test_the_browser_driver_parses():
    require("node")
    subprocess.run(["node", "--check", str(LIVE / "dashboard.js")], check=True)


def test_passwords_never_reach_a_screenshot_or_the_command_line():
    driver = (LIVE / "dashboard.js").read_text()
    # The page shows the one-time passwords twice, and a mask on the table alone missed the CSV block.
    assert 'this.page.locator("main table tbody td"), this.page.locator("main pre")' in driver
    for shot in ("04-user-created-passwords-hidden", "05-csv-import-passwords-hidden"):
        assert f'"{shot}", s.passwordMask()' in driver
    # Requests carry passwords: on stdin, never in argv, where `ps` shows them.
    assert "process.stdin" in driver and "process.argv" not in driver
    assert "input=json.dumps(request)" in (LIVE / "livelib.py").read_text()


def test_wiping_the_database_needs_the_node_named():
    assert livelib.check_wipe_allowed(None, "node-a") is False
    assert livelib.check_wipe_allowed("", "node-a") is False
    assert livelib.check_wipe_allowed("node-a", "node-a") is True
    with pytest.raises(livelib.NotBaseState):
        livelib.check_wipe_allowed("node-b", "node-a")


def test_key_values():
    text = "host=devbox-t1-a\nscoring=http=201 tls_verify=0\nnot a result\nEMPTY=no\nfile=\n"
    assert livelib.parse_key_values(text) == {"host": "devbox-t1-a", "scoring": "http=201 tls_verify=0", "file": ""}


def test_the_ssh_command_of_the_page():
    assert livelib.split_ssh_command("ssh -A -J team@worms.example:2222 root@10.7.0.11") == ("team", "worms.example:2222", "10.7.0.11")
    for bad in ("", None, "ssh root@10.7.0.11", "ssh -J team@h:1 root@10.7.0.11", "ssh -A -J team@h:1 root@10.7.0.11; rm -rf /"):
        with pytest.raises(ValueError):
            livelib.split_ssh_command(bad)


def test_wait_for():
    assert livelib.wait_for("x", lambda: "value", timeout=1, interval=0) == "value"
    with pytest.raises(TimeoutError, match="waiting for the thing"):
        livelib.wait_for("the thing", lambda: None, timeout=0, interval=0)


def test_report_keeps_every_row_as_it_goes(tmp_path):
    report = livelib.Report(tmp_path)
    report.stage("Stage", "prior")
    report.row("a | b", "x", "y\nz", True)
    # Written before the run ends: a run that dies leaves everything so far.
    text = (tmp_path / "report.md").read_text()
    assert "a | b" in text and "y z" in text and "PASS" in text
    report.row("c", "x", "y", False)
    report.finish()
    text = (tmp_path / "report.md").read_text()
    assert "**FAIL**" in text and "1 checks passed, 1 failed" in text


def test_members_have_the_dashboards_form_of_address():
    member = livelib.Member("Anna", "Alpha", "alpha")
    assert member.email == "anna.alpha@example.org" and member.short == "anna"
    assert member.password != livelib.Member("Anna", "Alpha", "alpha").password
    assert shutil.which("sh")
