"""scripts/generate_devbox.py: deterministic, in-range devbox addressing and
valid HCL structure. (The generated file is also validated against the devbox
stack with `tofu validate` when tofu + the provider are available; here we
check the generator logic without Proxmox.)"""

from __future__ import annotations

import importlib.util
import ipaddress
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("generate_devbox", REPO / "scripts" / "generate_devbox.py")
gen = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gen)


def test_addressing_is_in_range_and_disjoint():
    a0 = gen.team_addressing("t1", 0)
    a1 = gen.team_addressing("t2", 1)
    devbox = ipaddress.ip_network("10.128.0.0/10")
    for a in (a0, a1):
        assert ipaddress.ip_network(a["ab_cidr"]).subnet_of(devbox)
        assert ipaddress.ip_network(a["bc_cidr"]).subnet_of(devbox)
        assert ipaddress.ip_address(a["services_cidr"].split("/")[0]) in ipaddress.ip_network("10.2.0.32/27")
    # Different teams get disjoint bridges, SNAT sources and SSH fabrics.
    assert a0["ab_name"] != a1["ab_name"]
    assert a0["services_cidr"] != a1["services_cidr"]
    assert a0["ssh_a"] != a1["ssh_a"]


def test_deterministic():
    assert gen.team_addressing("x", 3) == gen.team_addressing("x", 3)


def test_render_has_all_parts():
    hcl = gen.render([{"slug": "t1", "index": 0}, {"slug": "t2", "index": 1}])
    assert "devbox_bridges" in hcl and "devbox_gws" in hcl and "devbox_vms" in hcl
    # Three VMs per team, two bridges per team.
    assert hcl.count('team             = "t1"') >= 4  # gw + 3 VMs
    assert 'name             = "devbox-t1-b"' in hcl


def test_rejects_bad_input():
    import pytest

    with pytest.raises(ValueError):
        gen.render([{"slug": "BAD SLUG", "index": 0}])
    with pytest.raises(ValueError):
        gen.render([{"slug": "a", "index": 0}, {"slug": "a", "index": 1}])  # duplicate
    with pytest.raises(ValueError):
        gen.team_addressing("t", 99)  # out of the services block
