"""The gameplan framework (src/gameplan, docs/05-live-environment.md): describing a sandbox,
validating it, loading a file safely, running execute() by its rules, and what the node builds."""

from __future__ import annotations

import os
import sys

import pytest

from conftest import REPO

sys.path.insert(0, str(REPO / "src"))

from gameplan import (  # noqa: E402
    Allow,
    BasicVM,
    FirewallRule,
    FreezeState,
    Gameplan,
    GameplanError,
    Incoming,
    Outgoing,
    RoundError,
)
from gameplan import build, loader  # noqa: E402
from gameplan.runtime import Backend, DryRun, Runner, instantiate, validate  # noqa: E402

GAMEPLANS = REPO / "gameplans"


class Chain(Gameplan):
    def __init__(self):
        self.a = BasicVM()
        self.b = BasicVM(memory=1024)
        self.key = self.generate_ssh_key()
        self.login = self.generate_ssh_pwd_login("alice")
        self.add_private_ssh_key(self.a, self.key)
        self.add_public_ssh_key(self.b, self.key)
        self.add_ssh_pwd_login(self.b, self.login)
        self.note = self.write_file(self.a, "~/note", f"{self.login.user}:{self.login.pwd}\n", at_reset=False)
        self.net = self.register_network([self.a, self.b])
        self.register_fw_rule(self.b, FirewallRule(Allow, 22, Incoming, peers=[self.a]))
        self.register_fw_rule(self.a, FirewallRule(Allow, 22, Outgoing))
        self.set_worm_entrypoint(self.a)

    def execute(self):
        flag = self.remove_flag(self.b)
        self.deploy_worm()
        self.wait(60)
        self.add_flag(flag)
        self.write_file(self.note)


def spec_of(cls):
    return instantiate(cls).spec()


def problems_of(init, execute=None, duration=None):
    def describe(self):
        init(self)  # the lambdas below return their tuple; __init__ must return None

    def run(self):
        execute(self)

    attrs = {"__init__": describe}
    if execute is not None:
        attrs["execute"] = run
    return validate(type("Plan", (Gameplan,), attrs), duration=duration)


# -- the example ------------------------------------------------------------------------


def test_the_example_round_loads_and_validates():
    loaded = loader.load(GAMEPLANS, "round_one.py")
    assert loaded.cls.__name__ == "RoundOne" and len(loaded.sha256) == 64
    assert validate(loaded.cls) == []
    spec = spec_of(loaded.cls)
    assert spec["entrypoint"] == "sandbox-vm0"
    assert [n["vms"] for n in spec["networks"]] == [["sandbox-vm0", "sandbox-vm1"], ["sandbox-vm1", "sandbox-vm2"]]
    # vm0 and vm2 share no network: there is no way from one to the other but through vm1.
    assert not set(spec["networks"][0]["vms"]) & {"sandbox-vm2"}
    note = next(f for f in spec["files"] if f["path"] == "/home/debian/note")
    assert note["vm"] == "sandbox-vm1" and "@@worms-secret:login1.password@@" in note["content"]


def test_every_gameplan_in_the_repository_is_valid_and_its_packages_are_cached():
    seeded = build.seeded_packages(REPO)
    names = loader.available(GAMEPLANS)
    assert names
    for name in names:
        assert validate(loader.load(GAMEPLANS, name).cls, packages=seeded) == [], name


# -- describing --------------------------------------------------------------------------


def test_the_spec_names_vms_after_their_attributes_and_assigns_addresses():
    spec = spec_of(Chain)
    vms = {vm["name"]: vm for vm in spec["vms"]}
    assert list(vms) == ["sandbox-a", "sandbox-b"]
    assert vms["sandbox-b"]["memory"] == 1024 and vms["sandbox-b"]["users"] == ["alice"]
    a_address = vms["sandbox-a"]["nics"][0]["address"]
    assert a_address.startswith("10.64.0.") and a_address.endswith("/24")
    assert vms["sandbox-a"]["nics"][0]["gateway"] == "10.64.0.1"
    # A rule with peers reaches exactly them; without, the VM's networks.
    assert vms["sandbox-b"]["firewall"] == [
        {"direction": "in", "action": "ACCEPT", "proto": "tcp", "dport": "22", "peers": [a_address.replace("/24", "/32")]}
    ]
    assert vms["sandbox-a"]["firewall"][0]["peers"] == ["10.64.0.0/24"]
    assert [s["op"] for s in spec["setup"]] == ["private_key", "public_key", "password"]
    assert spec["files"][0]["at_reset"] is False and spec["files"][0]["mode"] == "0644"
    # The same description twice is the same spec (the node and the orchestrator each build it).
    assert spec == spec_of(Chain)


def test_vms_in_a_list_register_one_by_one():
    def init(self):
        self.workers = [BasicVM(), BasicVM()]
        self.register_network(self.workers)
        self.set_worm_entrypoint(self.workers[0])

    def execute(self):
        self.deploy_worm()

    plan = type("Plan", (Gameplan,), {"__init__": init, "execute": execute})
    assert [vm["name"] for vm in spec_of(plan)["vms"]] == ["sandbox-workers-0", "sandbox-workers-1"]


@pytest.mark.parametrize(
    "init, message",
    [
        (lambda self: None, "has no VM"),
        (lambda self: setattr(self, "a", BasicVM()) or self.register_network([self.a]), "no worm entry point"),
        (lambda self: (setattr(self, "a", BasicVM()), self.set_worm_entrypoint(self.a)), "in no network"),
        (lambda self: (setattr(self, "a", BasicVM(cores=99)), self.register_network(self.a), self.set_worm_entrypoint(self.a)),
         "cores"),
        (lambda self: (setattr(self, "a", BasicVM(image="windows")), self.register_network(self.a),
                       self.set_worm_entrypoint(self.a)), "image"),
        (lambda self: (setattr(self, "a", BasicVM()), self.a.install("foo; rm -rf /"), self.register_network(self.a),
                       self.set_worm_entrypoint(self.a)), "not a Debian package"),
    ],
)  # fmt: skip
def test_a_broken_description_is_refused_with_its_reason(init, message):
    problems = problems_of(init, lambda self: self.deploy_worm())
    assert any(message in problem for problem in problems), problems


@pytest.mark.parametrize("path", ["../etc/passwd", "~/../../etc/shadow", "/etc/worms/flag", "/etc/worms/x", "note", "/a b", "/"])
def test_files_stay_out_of_the_flag_and_need_a_clean_path(path):
    def init(self):
        self.a = BasicVM()
        self.write_file(self.a, path, "x")

    with pytest.raises(GameplanError, match="not a path"):
        instantiate(type("Plan", (Gameplan,), {"__init__": init}))


def test_api_misuse_is_refused_where_it_happens():
    def unknown_vm(self):
        self.register_network([BasicVM()])

    def owner_without_login(self):
        self.a = BasicVM()
        self.write_file(self.a, "~/x", "x", owner="alice")

    def reserved_user(self):
        self.generate_ssh_pwd_login("postgres")

    def runtime_in_init(self):
        self.a = BasicVM()
        self.a.stop()

    for init, message in [(unknown_vm, "not a VM of this gameplan"), (owner_without_login, "not a user of"),
                          (reserved_user, "a gameplan can create"), (runtime_in_init, "call it in execute")]:  # fmt: skip
        with pytest.raises(GameplanError, match=message):
            instantiate(type("Plan", (Gameplan,), {"__init__": init}))


def test_secrets_belong_in_run_files_not_in_the_build():
    class Leaky(BasicVM):
        setup = ()

    def init(self):
        self.login = self.generate_ssh_pwd_login()
        self.a = Leaky()
        self.a._setup.append(f"echo {self.login.pwd} > /root/x")
        self.register_network(self.a)
        self.set_worm_entrypoint(self.a)

    assert any("secrets go into files" in p for p in problems_of(init, lambda self: self.deploy_worm()))


def test_a_description_that_depends_on_chance_is_refused():
    import random

    def init(self):
        self.a = BasicVM(memory=random.choice([512, 768, 1024, 2048, 4096]) + random.randrange(1, 100))
        self.register_network(self.a)
        self.set_worm_entrypoint(self.a)

    assert any("different sandbox" in p for p in problems_of(init, lambda self: self.deploy_worm()))


# -- running ------------------------------------------------------------------------------


def test_the_dry_run_finds_what_execute_gets_wrong():
    def init(self):
        self.a = BasicVM()
        self.register_network(self.a)
        self.set_worm_entrypoint(self.a)

    assert any("without self.deploy_worm" in p for p in problems_of(init, lambda self: None))
    assert any("deployed once" in p for p in problems_of(init, lambda self: (self.deploy_worm(), self.deploy_worm())))
    assert any("has a flag" in p for p in problems_of(init, lambda self: (self.deploy_worm(), self.add_flag(self.a))))
    assert any("cannot resume" in p for p in problems_of(init, lambda self: (self.deploy_worm(), self.a.resume())))
    assert any("describes the sandbox" in p for p in problems_of(init, lambda self: self.register_network(self.a)))
    assert any("still going" in p for p in problems_of(init, lambda self: (self.deploy_worm(), self.wait(4000))))
    assert problems_of(init, lambda self: (self.deploy_worm(), self.wait(4000)), duration=7200) == []


class Recording(DryRun):
    """A dry run whose guest steps can fail, as the worm can make them fail."""

    def __init__(self, vms, broken=()):
        super().__init__(vms)
        self.broken, self.events, self.lines = set(broken), [], []

    def _result(self, vms, strict, what):
        out = {vm: ("unreachable" if vm in self.broken else None) for vm in vms}
        if strict and any(out.values()):
            raise RoundError(f"{what} failed")
        self.steps += [(self.clock, what, vm) for vm in vms]
        return out

    def remove_flags(self, vms, strict):
        return self._result(vms, strict, "remove_flag")

    def place_flags(self, vms, strict):
        return self._result(vms, strict, "add_flag")

    def event(self, kind, vm=None, **data):
        self.events.append((kind, vm))

    def log(self, message):
        self.lines.append(message)


def test_before_the_worm_every_step_must_work_after_it_the_worm_may_break_them():
    # Before deploy_worm: the attempt fails.
    backend = Recording(["sandbox-a", "sandbox-b"], broken=["sandbox-b"])
    with pytest.raises(RoundError):
        Runner(instantiate(Chain), backend).run()
    # After: logged and skipped, and the run goes on to its end.
    class Late(Chain):
        def execute(self):
            self.deploy_worm()
            flag = self.remove_flag(self.b)
            self.add_flag(flag)

    backend = Recording(["sandbox-a", "sandbox-b"], broken=["sandbox-b"])
    notes = Runner(instantiate(Late), backend).run()
    assert len(notes) == 2 and all("sandbox-b" in note for note in notes)
    assert backend.clock == 3600  # the rest of the hour passed


def test_the_run_ends_on_time_even_if_execute_does_not():
    class Endless(Chain):
        def execute(self):
            self.deploy_worm()
            while True:
                try:
                    self.wait(100)
                except Exception:  # noqa: BLE001  (a gameplan cannot catch the end of the run)
                    pass

    backend = DryRun(["sandbox-a", "sandbox-b"])
    runner = Runner(instantiate(Endless), backend, duration=1000)
    runner.run()
    assert runner.cut and backend.clock == 1000


class Power(DryRun):
    def __init__(self, vms, stuck=()):
        super().__init__(vms)
        self.stuck = set(stuck)

    def power(self, vm, action, timeout=None):
        if action == "resume" and vm in self.stuck:
            return  # the API said yes, the VM stayed paused
        super().power(vm, action, timeout)


def test_freezing_suspends_everything_and_checks_it_runs_again():
    class Freeze(Chain):
        def execute(self):
            self.deploy_worm()
            with FreezeState(self.vms) as freeze:
                freeze.unfreeze(self.a)
                self.a.shutdown()

    backend = Power(["sandbox-a", "sandbox-b"])
    Runner(instantiate(Freeze), backend).run()
    assert backend.state == {"sandbox-a": "stopped", "sandbox-b": "running"}
    assert [s[1:] for s in backend.steps if s[1] in ("suspend", "resume", "shutdown")] == [
        ("suspend", "sandbox-a"), ("suspend", "sandbox-b"), ("resume", "sandbox-a"),
        ("shutdown", "sandbox-a"), ("resume", "sandbox-b"),
    ]  # fmt: skip

    with pytest.raises(RoundError, match="sandbox-b is paused"):
        Runner(instantiate(Freeze), Power(["sandbox-a", "sandbox-b"], stuck=["sandbox-b"])).run()


def test_files_are_written_by_handle_only():
    class Free(Chain):
        def execute(self):
            self.deploy_worm()
            self.write_file(self.a, "~/x", "anything")

    with pytest.raises(GameplanError, match="declared in __init__"):
        Runner(instantiate(Free), DryRun(["sandbox-a", "sandbox-b"])).run()
    backend = DryRun(["sandbox-a", "sandbox-b"])
    Runner(instantiate(Chain), backend).run()
    assert (60.0, "write_file", 0) in backend.steps


def test_the_backend_interface_is_complete_in_the_dry_run():
    for name in ("now", "sleep", "power", "status", "deploy_worm", "remove_flags", "place_flags", "write_file"):
        assert getattr(DryRun, name) is not getattr(Backend, name), name


# -- loading ------------------------------------------------------------------------------


def _write(directory, name, text):
    path = directory / name
    path.write_text(text)
    return path


SOURCE = """
from gameplan import BasicVM, Gameplan

class One(Gameplan):
    def __init__(self):
        self.a = BasicVM()
        self.register_network(self.a)
        self.set_worm_entrypoint(self.a)

    def execute(self):
        self.deploy_worm()
"""


def test_loading_takes_names_from_the_directory_only(tmp_path):
    plans = tmp_path / "plans"
    plans.mkdir()
    _write(plans, "good.py", SOURCE)
    _write(tmp_path, "outside.py", SOURCE)
    os.symlink(tmp_path / "outside.py", plans / "linked.py")
    _write(plans, "Bad-Name.py", SOURCE)
    (plans / "sub.py").mkdir() if False else None
    assert loader.available(plans) == ["good.py"]
    for name in ["../outside.py", "linked.py", "Bad-Name.py", "good", "/etc/passwd", "good.py/", "", None, 7]:
        with pytest.raises(GameplanError):
            loader.load(plans, name)
    loaded = loader.load(plans, "good.py")
    assert loaded.cls.__name__ == "One"
    with pytest.raises(GameplanError, match="not the version expected"):
        loader.load(plans, "good.py", sha256="0" * 64)
    assert loader.load(plans, "good.py", sha256=loaded.sha256).cls.__name__ == "One"


def test_a_file_defines_exactly_one_gameplan(tmp_path):
    _write(tmp_path, "two.py", SOURCE + "\nclass Two(One):\n    pass\n")
    _write(tmp_path, "none.py", "from gameplan import Gameplan\n")
    _write(tmp_path, "imported.py", "from gameplan import Gameplan as G\nclass X(G):\n    pass\n")
    with pytest.raises(GameplanError, match="exactly one"):
        loader.load(tmp_path, "two.py")
    with pytest.raises(GameplanError, match="exactly one"):
        loader.load(tmp_path, "none.py")
    assert loader.load(tmp_path, "imported.py").cls.__name__ == "X"


# -- what the node builds -----------------------------------------------------------------------


def test_the_build_renders_bridges_vms_rules_and_ansible_vars():
    spec = spec_of(Chain)
    text = build.hcl(spec, "chain.py", "a" * 64)
    assert 'gameplan_sha256 = "' + "a" * 64 + '"' in text
    assert '"net0" = {' in text and 'name         = "vmbr1000"' in text
    assert 'disk_file_id             = local.platform.os_images["debian"].file_id' in text
    assert 'type = "in", action = "ACCEPT", proto = "tcp", dport = "22"' in text
    assert "@@worms-secret" not in text and "alice" not in text
    config = build.ansible_vars(spec)["sandbox_vm_config"]
    assert config["sandbox-b"]["users"] == ["alice"]
    graph = build.topology(spec)
    assert {e["relationship"] for e in graph["edges"]} == {"gateway", "attached-to"}
    assert all("firewall" not in node and "content" not in node for node in graph["nodes"])


def test_an_infrastructure_error_fails_the_attempt_even_if_execute_catches_it():
    class Swallow(Chain):
        def execute(self):
            self.deploy_worm()
            try:
                self.a.resume()  # the dry run refuses: it is running
            except Exception:  # noqa: BLE001
                pass
            self.wait(10)

    class Refusing(DryRun):
        def power(self, vm, action, timeout=None):
            raise RoundError("the API refused")

    with pytest.raises(RoundError, match="the API refused"):
        Runner(instantiate(Swallow), Refusing(["sandbox-a", "sandbox-b"])).run()
