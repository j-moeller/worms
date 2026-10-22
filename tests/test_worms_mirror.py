"""Functional test of worms-mirror (ansible role host_network), the host-side
traffic mirror for the monitor VM.

Builds a miniature Proxmox host in network namespaces: a sandbox bridge with
two "VMs" and a "sandbox-gw", plus the mirror bridge with the "monitor" on it.
Ports are veth pairs named like Proxmox taps (tap<vmid>i<n>). Runs the real
rendered script against it and checks with tcpdump on the monitor that

  * every frame between sandbox VMs is copied exactly once,
  * from the router port only frames from the services network are copied,
  * other tc filters (Proxmox' rate limit at pref 50) still run (`continue`),
  * the monitor's port is receive-only (drop filter),
  * a port that lost its filter (Proxmox re-plugging a tap) is repaired.

Needs root, iproute2 (ip, tc with the u32 classifier and mirred action) and
tcpdump. Everything happens in private namespaces named wt-<pid>-*, so it is
safe to run on a real Proxmox host.
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from conftest import require, require_root

SERVICES = "10.2.0.0/24"


def sh(*args, check=True, ns=None, env=None):
    cmd = (["ip", "netns", "exec", ns] if ns else []) + list(args)
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if check and proc.returncode != 0:
        raise AssertionError(f"{' '.join(cmd)} failed:\n{proc.stdout}{proc.stderr}")
    return proc


def udp(ns, src, dst, count=3, sport=40000, dport=9999):
    """Send `count` UDP datagrams from `src` (bound) to `dst` inside `ns`."""
    code = (
        "import socket,sys;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);"
        f"s.bind(('{src}',{sport}));"
        f"[s.sendto(b'worms-test',('{dst}',{dport})) for _ in range({count})]"
    )
    sh("python3", "-c", code, ns=ns)


@pytest.fixture
def lab(tmp_path, render):
    require_root()
    require("ip", "tc", "tcpdump")

    prefix = f"wt{os.getpid() % 100000}"
    names = {k: f"{prefix}-{k}" for k in ("host", "a", "b", "gw", "mon")}
    script = tmp_path / "worms-mirror"
    script.write_text(
        render(
            "ansible/roles/host/host_network",
            "worms-mirror.sh.j2",
            bridges={"mirror": "wtmir", "sandbox_glob": "wtsb*"},
            networks={"services": SERVICES},
            host_mirror_router_name="sandbox-gw",
            host_mirror_settle_seconds=1,
            host_mirror_interval=3,
        )
    )
    script.chmod(0o755)
    pve = tmp_path / "pve"
    pve.mkdir()
    (pve / "950.conf").write_text("name: sandbox-gw\ncores: 1\n")
    (pve / "901.conf").write_text("name: sandbox-vm01\n")

    try:
        for ns in names.values():
            sh("ip", "netns", "add", ns)
    except AssertionError as exc:
        for ns in names.values():
            sh("ip", "netns", "del", ns, check=False)
        pytest.skip(f"cannot create network namespaces: {exc}")

    host = names["host"]
    try:
        sh("ip", "link", "set", "lo", "up", ns=host)
        for bridge in ("wtsb0", "wtmir"):
            sh("ip", "link", "add", bridge, "type", "bridge", ns=host)
            sh("ip", "link", "set", bridge, "up", ns=host)

        # port name, bridge, namespace, addresses
        ports = [
            ("tap901i0", "wtsb0", names["a"], ["10.64.0.11/24"]),
            ("tap902i0", "wtsb0", names["b"], ["10.64.0.12/24"]),
            ("tap950i0", "wtsb0", names["gw"], ["10.64.0.1/24", "10.2.0.14/32"]),
            ("tap960i2", "wtmir", names["mon"], []),
        ]
        for port, bridge, ns, addrs in ports:
            sh("ip", "link", "add", port, "type", "veth", "peer", "name", "eth0", "netns", ns, ns=host)
            sh("ip", "link", "set", port, "master", bridge, "up", ns=host)
            sh("ip", "link", "set", "eth0", "up", ns=ns)
            sh("ip", "link", "set", "lo", "up", ns=ns)
            for addr in addrs:
                sh("ip", "addr", "add", addr, "dev", "eth0", ns=ns)
        sh("ip", "link", "set", "eth0", "promisc", "on", ns=names["mon"])

        # A stand-in for Proxmox' rate-limit filter (pref 50) on a VM port.
        sh("tc", "qdisc", "add", "dev", "tap901i0", "handle", "ffff:", "ingress", ns=host)
        sh(
            "tc", "filter", "add", "dev", "tap901i0", "parent", "ffff:", "pref", "50",
            "protocol", "all", "u32", "match", "u32", "0", "0",
            "action", "mirred", "egress", "mirror", "dev", "wtsb0", "continue", ns=host,
        )

        env = dict(os.environ, WORMS_MIRROR_PVE_CONF_DIR=str(pve))

        def mirror(*args, check=True):
            return sh(str(script), *args, ns=host, env=env, check=check)

        yield {"ns": names, "mirror": mirror, "host": host, "script": script, "env": env}
    finally:
        for ns in names.values():
            sh("ip", "netns", "del", ns, check=False)


def capture(lab, action):
    """Run `action` while tcpdump records on the monitor; return the lines."""
    mon = lab["ns"]["mon"]
    pcap = f"/tmp/{mon}.pcap"
    proc = subprocess.Popen(
        ["ip", "netns", "exec", mon, "tcpdump", "-n", "-U", "-i", "eth0", "-w", pcap, "udp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(1.0)
        action()
        time.sleep(1.0)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    out = sh("tcpdump", "-n", "-r", pcap).stdout.splitlines()
    os.unlink(pcap)
    return out


def count(lines, src, dst):
    return sum(1 for line in lines if f" {src}.40000 > {dst}.9999:" in line)


def test_mirror_copies_each_frame_once(lab):
    lab["mirror"]("once")
    status = lab["mirror"]("status", check=False).stdout
    assert "tap901i0" in status and "mirror-all" in status.split("tap901i0", 1)[1].splitlines()[0]
    assert "mirror-router" in status.split("tap950i0", 1)[1].splitlines()[0]

    ns = lab["ns"]

    def traffic():
        udp(ns["a"], "10.64.0.11", "10.64.0.12")  # VM -> VM
        udp(ns["b"], "10.64.0.12", "10.64.0.11")  # VM -> VM
        udp(ns["gw"], "10.2.0.14", "10.64.0.11")  # services reply via the router
        udp(ns["gw"], "10.64.0.1", "10.64.0.12")  # routed sandbox traffic (seen elsewhere)

    lines = capture(lab, traffic)
    assert count(lines, "10.64.0.11", "10.64.0.12") == 3, lines
    assert count(lines, "10.64.0.12", "10.64.0.11") == 3, lines
    assert count(lines, "10.2.0.14", "10.64.0.11") == 3, lines
    assert count(lines, "10.64.0.1", "10.64.0.12") == 0, lines


def test_other_filters_still_run(lab):
    """The mirror filter must `continue`, or Proxmox' rate limit is skipped."""
    lab["mirror"]("once")
    udp(lab["ns"]["a"], "10.64.0.11", "10.64.0.12")
    out = sh("tc", "-s", "filter", "show", "dev", "tap901i0", "parent", "ffff:", "pref", "50", ns=lab["host"]).stdout
    sent = [int(line.split()[1]) for line in out.splitlines() if line.strip().startswith("Sent")]
    assert sent and max(sent) > 0, out


def test_repairs_a_replugged_port(lab):
    lab["mirror"]("once")
    # Proxmox deletes the ingress qdisc when it (re)applies a rate limit.
    sh("tc", "qdisc", "del", "dev", "tap902i0", "ingress", ns=lab["host"])
    status = lab["mirror"]("status", check=False)
    assert status.returncode == 1
    assert "none" in status.stdout.split("tap902i0", 1)[1].splitlines()[0]
    lab["mirror"]("once")
    status = lab["mirror"]("status", check=False).stdout
    assert "mirror-all" in status.split("tap902i0", 1)[1].splitlines()[0]


def test_monitor_port_is_receive_only(lab):
    lab["mirror"]("once")
    line = lab["mirror"]("status", check=False).stdout.split("tap960i2", 1)[1].splitlines()[0]
    if line.split()[-1] != "drop":
        pytest.skip("tc action 'gact' (drop) not available in this kernel")
    # The monitor tries to talk (ARP + UDP) on its capture NIC.
    sh("ip", "addr", "add", "10.99.0.1/24", "dev", "eth0", ns=lab["ns"]["mon"])
    udp(lab["ns"]["mon"], "10.99.0.1", "10.99.0.2", count=5)
    time.sleep(1.5)
    out = sh("tc", "-s", "filter", "show", "dev", "tap960i2", "parent", "ffff:", ns=lab["host"]).stdout
    assert "Sent" in out and "Sent 0 bytes" not in out, out
    # ... and nothing reached the mirror bridge.
    rx = sh("cat", "/sys/class/net/wtmir/statistics/rx_packets", ns=lab["host"]).stdout.strip()
    assert rx == "0", rx


def test_clear_removes_everything(lab):
    lab["mirror"]("once")
    lab["mirror"]("clear")
    status = lab["mirror"]("status", check=False)
    assert status.returncode == 1
    assert "mirror-all" not in "".join(line.split()[-1] for line in status.stdout.splitlines()[1:])


def test_daemon_picks_up_new_ports(lab):
    """`run`: ports that appear later (a VM starting) are mirrored after the
    settle time, triggered by the link event."""
    host = lab["host"]
    daemon = subprocess.Popen(
        ["ip", "netns", "exec", host, str(lab["script"]), "run"],
        env=lab["env"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(3)
        assert lab["mirror"]("status", check=False).stdout.count("mirror-all") >= 2
        sh("ip", "link", "add", "tap903i0", "type", "veth", "peer", "name", "tap903p", ns=host)
        sh("ip", "link", "set", "tap903i0", "master", "wtsb0", "up", ns=host)
        deadline = time.time() + 10
        while time.time() < deadline:
            line = lab["mirror"]("status", check=False).stdout.split("tap903i0", 1)[1].splitlines()[0]
            if line.split()[-1] == "mirror-all":
                break
            time.sleep(0.5)
        else:
            pytest.fail(f"new port not mirrored: {line}")
        assert daemon.poll() is None, "daemon exited"
    finally:
        daemon.terminate()
        daemon.wait(timeout=10)
