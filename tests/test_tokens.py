"""The Proxmox tokens (src/worms_cli/tokens.py): one definition of what each may
do, and `worms secrets issue` / `check` as the only way to write and verify them.
No Proxmox needed: pveum and the API are faked.
"""

from __future__ import annotations

import json
import sys

import pytest
import yaml

from conftest import REPO

sys.path.insert(0, str(REPO / "src"))

from worms_cli import tokens  # noqa: E402
from worms_cli.secrets import EXTERNAL, GENERATED, SUPERSEDED, Vault  # noqa: E402
from worms_cli.session import Session, SessionError  # noqa: E402
from worms_cli.tokens import TOKENS, TokenError, check, issue  # noqa: E402

SECRET = "11111111-2222-3333-4444-555555555555"


class FakePveum:
    """Answers like pveum; records what it was asked."""

    def __init__(self, acl=(), existing_tokens=(), fail=None):
        self.calls = []
        self.acl = list(acl)
        self.existing_tokens = list(existing_tokens)
        self.fail = fail  # a command prefix that fails

    def __call__(self, argv):
        command = " ".join(argv)
        self.calls.append(command)
        if self.fail and command.startswith(self.fail):
            return 1, "", "boom"
        if command.startswith("pveum acl list"):
            return 0, json.dumps(self.acl), ""
        if command.startswith("pveum user token list"):
            return 0, json.dumps([{"tokenid": t} for t in self.existing_tokens]), ""
        if command.startswith("pveum user token add"):
            return 0, json.dumps({"full-tokenid": "x", "value": SECRET}), ""
        if command.startswith("pveum role add") and "exists" in str(self.fail):
            return 1, "", "role already exists"
        return 0, "", ""


def test_every_entered_proxmox_secret_has_a_definition():
    assert set(TOKENS) == set(EXTERNAL)
    for spec in TOKENS.values():
        assert "!" in spec.full_id and spec.privs and spec.path.startswith("/")


def test_no_token_can_change_permissions_or_the_host():
    """Least privilege: none of them may grant rights or administer the node."""
    forbidden = {"Permissions.Modify", "User.Modify", "Realm.Allocate", "Sys.PowerMgmt", "Sys.Console", "Group.Allocate"}
    for name, spec in TOKENS.items():
        assert not forbidden & set(spec.privs), name
        assert spec.user != "root@pam", name


def test_power_token_is_scoped_to_the_sandbox_pool():
    spec = TOKENS["orchestrator-token"]
    assert spec.path == "/pool/sandbox"
    assert set(spec.privs) == {"VM.Audit", "VM.PowerMgmt"}


def test_topology_token_only_reads():
    privs = TOKENS["topology-token"].privs
    assert all(p.endswith(".Audit") for p in privs)
    # Without it Proxmox hides the bridges without any error: 1 interface, not 13.
    assert "SDN.Audit" in privs


def test_definitions_match_what_ansible_uses():
    group_vars = yaml.safe_load((REPO / "ansible" / "group_vars" / "all.yml").read_text())
    power = TOKENS["orchestrator-token"]
    assert (group_vars["proxmox_api_user"], group_vars["proxmox_api_token_name"]) == (power.user, power.token_id)
    readonly = TOKENS["topology-token"]
    assert (group_vars["proxmox_readonly_api_user"], group_vars["proxmox_readonly_api_token_name"]) == (
        readonly.user, readonly.token_id)  # fmt: skip
    role = yaml.safe_load((REPO / "ansible" / "roles" / "host" / "round_agent" / "defaults" / "main.yml").read_text())
    assert role["round_agent_power_role"] == power.role
    assert set(role["round_agent_power_privs"].split(",")) == set(power.privs)
    assert f"/pool/{role['round_agent_sandbox_pool']}" == power.path


def test_issue_creates_the_token_privilege_separated_and_grants_both_sides():
    pveum = FakePveum()
    spec = TOKENS["orchestrator-token"]
    assert issue(spec, pveum) == SECRET
    calls = "\n".join(pveum.calls)
    assert f"pveum role modify {spec.role} --privs VM.Audit,VM.PowerMgmt" in calls
    assert f"pveum user token add {spec.user} {spec.token_id} --privsep 1" in calls
    assert f"pveum acl modify /pool/sandbox --roles {spec.role} --users {spec.user}" in calls
    assert f"pveum acl modify /pool/sandbox --roles {spec.role} --tokens {spec.full_id}" in calls
    assert "token remove" not in calls  # nothing to rotate


def test_issue_gives_the_full_form_where_opentofu_needs_it():
    spec = TOKENS["proxmox-token"]
    assert issue(spec, FakePveum()) == f"{spec.full_id}={SECRET}"


def test_issue_rotates_an_existing_token():
    spec = TOKENS["topology-token"]
    pveum = FakePveum(existing_tokens=[spec.token_id])
    issue(spec, pveum)
    remove = pveum.calls.index(f"pveum user token remove {spec.user} {spec.token_id}")
    add = next(i for i, c in enumerate(pveum.calls) if c.startswith("pveum user token add"))
    assert remove < add


def test_issue_removes_every_wider_grant_of_that_identity():
    spec = TOKENS["topology-token"]
    acl = [
        {"path": "/", "ugid": spec.user, "roleid": "PVEAuditor", "type": "user"},
        {"path": "/", "ugid": spec.full_id, "roleid": "PVEAuditor", "type": "token"},
        {"path": spec.path, "ugid": spec.user, "roleid": spec.role, "type": "user"},
        {"path": "/", "ugid": "someone-else@pve", "roleid": "Administrator", "type": "user"},
    ]
    pveum = FakePveum(acl=acl)
    issue(spec, pveum)
    assert f"pveum acl delete / --roles PVEAuditor --users {spec.user}" in pveum.calls
    assert f"pveum acl delete / --roles PVEAuditor --tokens {spec.full_id}" in pveum.calls
    assert not any("someone-else" in c for c in pveum.calls)
    assert not any(f"acl delete {spec.path} --roles {spec.role}" in c for c in pveum.calls)


def test_issue_stops_at_the_first_failure_and_never_shows_the_secret():
    pveum = FakePveum(fail="pveum acl modify")
    with pytest.raises(TokenError) as exc:
        issue(TOKENS["orchestrator-token"], pveum)
    assert SECRET not in str(exc.value)


class FakeApi:
    def __init__(self, permissions, status=200):
        self.permissions, self.status = permissions, status

    def __call__(self, secret, path):
        if self.status != 200:
            raise tokens.ApiError(self.status)
        return {path: self.permissions.get(path, {})}


@pytest.mark.parametrize(
    "permissions, status, verdict",
    [
        ({"/pool/sandbox": {"VM.Audit": 1, "VM.PowerMgmt": 1}}, 200, "ok"),
        ({}, 401, "invalid"),
        ({"/pool/sandbox": {"VM.Audit": 1}}, 200, "missing"),
        ({"/pool/sandbox": {"VM.Audit": 1, "VM.PowerMgmt": 1, "VM.Allocate": 1}}, 200, "broader"),
        ({"/pool/sandbox": {"VM.Audit": 1, "VM.PowerMgmt": 1}, "/": {"Sys.Audit": 1}}, 200, "broader"),
    ],
)
def test_check_tells_a_wrong_value_and_wrong_rights_apart(permissions, status, verdict):
    state = check(TOKENS["orchestrator-token"], "a-secret", FakeApi(permissions, status))
    assert state.startswith(verdict)


def test_check_of_an_empty_value():
    assert check(TOKENS["topology-token"], "", FakeApi({})).startswith("not set")


def test_superseded_vault_keys_are_removed(tmp_path, monkeypatch):
    monkeypatch.setattr("worms_cli.secrets.shutil.which", lambda name: None if name == "ansible-vault" else "/bin/x")
    monkeypatch.setattr("worms_cli.secrets.generate_ssh_key", lambda: ("private\n", "public"))
    vault = Vault(tmp_path)
    vault.file.parent.mkdir(parents=True)
    vault.file.write_text('proxmox_api_token: "x@pve!y=old"\nvault_something_else: "kept"\n')
    vault.ensure()
    values = vault.load()
    assert not SUPERSEDED & set(values)
    assert values["vault_something_else"] == "kept"


def test_devpi_password_is_generated():
    assert "vault_devpi_root_password" in GENERATED


class FakeConfig:
    ssh = ""

    def __init__(self, root):
        self.repo_root = root


def test_a_credentials_file_next_to_opentofu_is_refused(tmp_path):
    """*.auto.tfvars overrides the TF_VAR_* the CLI sets from the vault, so a
    stray one would silently win over the one place tokens are kept."""
    stack = tmp_path / "opentofu" / "stacks" / "firewall"
    stack.mkdir(parents=True)
    session = Session(FakeConfig(tmp_path))
    assert session.stray_tofu_credentials() == []
    (stack / "credentials.auto.tfvars").write_text('proxmox_api_token = "x"\n')
    assert [p.name for p in session.stray_tofu_credentials()] == ["credentials.auto.tfvars"]
    session.vault.file.parent.mkdir(parents=True)
    session.vault.file.write_text('vault_tofu_proxmox_api_token: "x@pve!y=z"\n')
    with pytest.raises(SessionError, match="overrides the vault"):
        session.tofu_env()
