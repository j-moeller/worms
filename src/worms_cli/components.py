"""What `worms apply <component>` builds: one component per service, with what it needs first.

A component is one of:

  a stack       platform, firewall: an OpenTofu stack of its own (no VM)
  a host play   host-network, round-agent: an Ansible play on the Proxmox host
  a VM          dns, database, dashboard, ...: one VM of a shared OpenTofu stack (built with
                `tofu apply -target`, recreated with `-replace`) and its share of the
                configuration playbooks (`--limit` to its inventory group)
  devbox        the teams' devboxes: the round agent builds them (orchestrator chain)
  gameplans     gameplans/ and the framework (src/gameplan), copied to the orchestrator and the
                host, which then check them: no packages, no internet window

`deps` are what a component needs to exist before it is built. `worms apply` builds a missing
dependency only after showing the plan and asking (build.confirm). Whether a component exists
(`built`) is answered from the host: a VM by its name in Proxmox, a host play by its service,
a stack by its OpenTofu state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Component:
    name: str
    help: str
    deps: tuple[str, ...] = ()
    kind: str = "vm"  # "vm", "stack", "host", "devbox", "gameplans"
    stack: str = ""  # its OpenTofu stack
    module: str = ""  # its VM's module in that stack (-target)
    resource: str = ""  # its VM resource in that module (-replace)
    group: str = ""  # its inventory group (--limit)
    service: bool = False  # a service VM: snapshot `clean`, which a reset returns to
    stateful: bool = False  # holds records a rebuild loses
    host_service: str = ""  # for kind "host": the systemd unit that says it is built
    playbook: str = ""  # for kind "host": its play


def _persistent(name, help, deps, stateful=False):
    return Component(name, help, deps, stack="infrastructure/persistent", module=f'module.infrastructure_vms["{name}"]',
                     resource="proxmox_virtual_environment_vm.infrastructure", group=name, stateful=stateful)  # fmt: skip


def _private(name, help):
    key = name.replace("-", "_")
    return Component(name, help, ("dns",), stack="private_internet", module=f'module.private_internet_vms["{key}"]',
                     resource="proxmox_virtual_environment_vm.service_vm", group=key, service=True)  # fmt: skip


COMPONENTS: dict[str, Component] = {
    c.name: c
    for c in (
        Component("platform", "bridges, OS images, cloud-init files, pools, hookscript", kind="stack", stack="platform"),
        Component("firewall", "the Proxmox cluster and node firewall, security groups", ("platform",), kind="stack",
                  stack="firewall"),  # fmt: skip
        Component("host-network", "the host's nftables, NAT, sysctls and firewall watchdog", ("firewall",), kind="host",
                  host_service="worms-host-firewall", playbook="SetupHostNetwork.yml"),  # fmt: skip
        Component("dns", "internal names, and the resolver the VMs use", ("host-network",), stack="infrastructure/ephemeral",
                  module="module.dns", resource="proxmox_virtual_environment_vm.service_vm", group="dns", service=True),  # fmt: skip
        _persistent("syslog", "the central log server: every VM's audit log", ("dns",)),
        _persistent("database", "PostgreSQL: the platform's one database", ("dns", "syslog"), stateful=True),
        _persistent("orchestrator", "the conductor, the attempt processor, the orchestrator API; runs the migrations",
                    ("database",)),  # fmt: skip
        _persistent("dashboard", "the website: teams, uploads, results, staff pages, team API", ("database", "orchestrator")),
        Component("scoring", "the sandboxes' scoring API (flags)", ("database",), stack="infrastructure/ephemeral",
                  module='module.sandbox_apis["scoring"]', resource="proxmox_virtual_environment_vm.infrastructure",
                  group="scoring", service=True),  # fmt: skip
        Component("logging", "the sandboxes' logging API", ("database",), stack="infrastructure/ephemeral",
                  module='module.sandbox_apis["logging"]', resource="proxmox_virtual_environment_vm.infrastructure",
                  group="logging", service=True),  # fmt: skip
        Component("monitor", "the network monitor (Zeek): connections into the database", ("database",),
                  stack="infrastructure/ephemeral", module="module.monitor", resource="proxmox_virtual_environment_vm.monitor",
                  group="monitor", service=True),  # fmt: skip
        _private("apt-cache", "the private internet: Debian/Ubuntu packages"),
        _private("devpi", "the private internet: Python packages"),
        _private("docker-registry", "the private internet: container images"),
        _private("manpages", "the private internet: man pages"),
        _private("ntp", "the private internet: time"),
        _private("cve-search", "the private internet: CVE search"),
        _private("wikipedia", "the private internet: Wikipedia"),
        Component("round-agent", "the round agent on the host: runs rounds and builds devboxes", ("database", "orchestrator"),
                  kind="host", host_service="worms-round-agent", playbook="SetupRoundAgent.yml"),  # fmt: skip
        Component("gameplans", "the gameplans and their framework, copied to the orchestrator and the host, "
                  "checked at once; no packages, no internet; not during a round", ("orchestrator", "round-agent"),
                  kind="gameplans"),  # fmt: skip
        Component("devbox", "the teams' missing devboxes, built by the round agent; --rebuild: every team's (or "
                  "--team's) from nothing", ("round-agent", "scoring", "logging", "apt-cache"), kind="devbox"),  # fmt: skip
    )
}

# Names for several components at once (in build order).
GROUPS: dict[str, tuple[tuple[str, ...], str]] = {
    "infrastructure": (("dns", "syslog", "database", "orchestrator", "dashboard", "scoring", "logging", "monitor"),
                       "the platform's own VMs"),  # fmt: skip
    "private-internet": (tuple(n for n, c in COMPONENTS.items() if c.stack == "private_internet"),
                         "the private internet's VMs"),  # fmt: skip
    "services": (tuple(n for n, c in COMPONENTS.items() if c.service),
                 "the VMs teams reach, which a reset returns to their snapshot"),  # fmt: skip
}

ORDER = list(COMPONENTS)  # build order: every component after its deps


def expand(names) -> list[str]:
    """Component and group names -> component names, in build order. KeyError on an unknown one."""
    wanted = set()
    for name in names:
        if name in GROUPS:
            wanted.update(GROUPS[name][0])
        elif name in COMPONENTS:
            wanted.add(name)
        else:
            raise KeyError(name)
    return [name for name in ORDER if name in wanted]


def closure(names) -> list[str]:
    """`names` and everything they need, in build order."""
    seen, stack = set(), list(names)
    while stack:
        name = stack.pop()
        if name not in seen:
            seen.add(name)
            stack.extend(COMPONENTS[name].deps)
    return [name for name in ORDER if name in seen]


def stack_built(repo_root, stack: str) -> bool:
    state = Path(repo_root) / "opentofu" / "stacks" / stack / "terraform.tfstate"
    try:
        return bool(json.loads(state.read_text() or "{}").get("resources"))
    except (OSError, ValueError):
        return False


@dataclass
class Plan:
    """What a build does, component by component: create (missing), recreate (--rebuild),
    reconfigure (exists: its configuration again), or a missing dependency it creates."""

    steps: list[tuple[str, str]] = field(default_factory=list)  # (component, action)
    notes: list[str] = field(default_factory=list)

    def names(self, *actions) -> list[str]:
        return [name for name, action in self.steps if not actions or action in actions]

    @property
    def added(self) -> list[str]:
        return self.names("dependency", "migrations")

    def table(self) -> list[list[str]]:
        what = {
            "create": "create (missing)",
            "recreate": "RECREATE (destroy and build again)",
            "reconfigure": "configure again",
            "unfinished": "configure again, in the window: the build that created it stopped before its end",
            "dependency": "create: a missing dependency",
            "migrations": "configure again: the database lacks migrations",
            "provision": "build the teams' missing devboxes (round agent)",
            "deploy": "copy to the host and the orchestrator, check at once",
        }
        return [[name, what[action], COMPONENTS[name].help] for name, action in self.steps]

    def touches(self, packages: bool = False) -> tuple[dict[str, str], list[str]]:
        """What the build of this plan touches, as worms_cli/pipeline.py (_plan_components) runs
        it: per component its OpenTofu stack and targets, its playbooks and their --limit; then,
        for the whole plan, the internet window, the devboxes off, the service VMs rolled back
        and snapshotted again, what restarts."""
        from worms_cli.pipeline import SITE

        actions = dict(self.steps)
        vms = [COMPONENTS[n] for n in actions if COMPONENTS[n].kind == "vm"]
        new = [c for c in vms if actions[c.name] in ("create", "recreate", "dependency")]
        unfinished = [c.name for c in vms if actions[c.name] == "unfinished"]
        services = [c for c in vms if c.service]
        window = bool(new) or bool(unfinished) or (packages and bool(vms))
        hosts = [c.group for c in vms] + (["database"] if "orchestrator" in actions and "database" not in actions else [])
        limit = ",".join([*hosts, "localhost"])
        rows = {}
        for name, action in self.steps:
            c = COMPONENTS[name]
            if c.kind == "stack":
                rows[name] = f"OpenTofu: init + apply {c.stack}"
            elif c.kind == "host":
                rows[name] = f"Ansible: {c.playbook} on the host" + (" (the agent restarts once idle)" if name == "round-agent" else "")
            elif c.kind == "gameplans":
                rows[name] = "Ansible: SetupRoundAgent.yml --tags agent-sync; Site.yml --limit orchestrator --tags gameplans"
            elif c.kind == "devbox":
                rows[name] = "a host job for the round agent (devbox_rebuild per team)" if action == "recreate" else \
                    "a host job for the round agent (devbox_provision)"  # fmt: skip
            else:
                tofu = (f"OpenTofu: apply -target {c.stack}" + (" -replace" if action == "recreate" else "") + "; ") if c in new else ""
                rows[name] = tofu + f"Ansible: Site.yml for {c.group}"
        summary = []
        if vms:
            summary.append(f"Ansible: {SITE} (every VM's roles, in phases, the VMs in parallel), --limit {limit}"
                           + ("" if window else " --skip-tags packages"))  # fmt: skip
            why = "new VMs" if new else f"unfinished: {', '.join(unfinished)}" if unfinished else "--packages"
            summary.append("internet window: " + (f"OPEN for the VMs' part ({why})" if window else "closed"))
            off = window or bool(services)
            summary.append("devbox VMs: " + ("shut down for the VMs' part, started again after" if off else "left running"))
            if services:
                summary.append(f"service VMs rolled back to `clean`, configured, snapshotted again: {', '.join(c.name for c in services)}")
            if window and any(c.name == "dns" or c.stack == "private_internet" for c in vms):
                summary.append("after the window: DNS and the caches back to offline mode")
        restarts = []
        if "orchestrator" in actions:
            restarts.append("the orchestrator's services (it migrates with them stopped)")
        if "gameplans" in actions:
            restarts.append("the conductor (it checks the gameplans)")
        if restarts:
            summary.append("restarts: " + "; ".join(restarts))
        return rows, summary


def plan(requested, exists, rebuild=False, pending_migrations=lambda: [], unfinished=lambda component: False) -> Plan:
    """The plan for `worms apply <requested>`. `exists(component)` says whether it is built;
    `unfinished(component)`, whether the build that created its VM stopped before its end
    ("Install apt-cacher-ng" with no internet, say; the VM history). Configured again offline, its
    packages would be fetched with the window closed and wait on the firewall for good, so it is
    configured in the window, as a new VM is."""
    result = Plan()
    requested = list(requested)
    if rebuild:
        stacks = [name for name in requested if COMPONENTS[name].kind == "stack"]
        if stacks:
            raise ValueError(f"{', '.join(stacks)}: everything else stands on it, so it is not rebuilt alone "
                             "(`worms destroy --all`, then `worms apply --all`)")  # fmt: skip
    for name in closure(requested):
        component = COMPONENTS[name]
        if name in requested:
            if component.kind == "gameplans":
                action = "deploy"
            elif component.kind == "devbox":
                action = "recreate" if rebuild else "provision"
            elif rebuild and component.kind == "vm":
                action = "recreate"
            else:
                action = "reconfigure" if exists(component) else "create"
                if action == "reconfigure" and component.kind == "vm" and unfinished(component):
                    action = "unfinished"
            result.steps.append((name, action))
        elif not exists(component):
            result.steps.append((name, "dependency"))
    if "orchestrator" in result.names() and "database" not in result.names():
        result.notes.append("the database's grants are applied again after the orchestrator's migrations")
    # Only the orchestrator migrates and the database grants: new dashboard code needs both, and
    # the database's grants need the tables the migrations make.
    configured = set(result.names("create", "recreate", "reconfigure", "unfinished", "dependency"))
    if configured & {"dashboard", "database"} and "orchestrator" not in configured:
        pending = pending_migrations()
        if pending:
            result.notes.append(f"the database lacks {len(pending)} migration(s) of this checkout ({', '.join(pending)})")
            for name in ("database", "orchestrator"):
                if name not in configured:
                    result.steps.append((name, "migrations"))
            result.steps.sort(key=lambda step: ORDER.index(step[0]))
    return result
