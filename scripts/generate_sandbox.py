import argparse
import ipaddress
import json
import random
import re
from dataclasses import asdict, dataclass
from typing import List

import jinja2

# Sandbox management network (vmbr4). Must match networks.sandbox_maintenance
# in opentofu/shared/shared-variables.tf. Host addresses .1-.9 are reserved
# (the Proxmox host is .1).
SANDBOX_MAINTENANCE_NETWORK = ipaddress.ip_network("10.4.0.0/16")

TEMPLATE = """
locals {
  seed = "{{ seed }}"

  sandbox_bridges = {
    {% for network in networks %}
    {{ network.key }} = {
      name         = "{{ network.name }}"
      network_cidr = "{{ network.cidr }}"
    }{% if not loop.last %},{% endif %}
    {% endfor %}
  }

  sandbox_vms = {
    {% for vm in sandbox_vms %}
    {{ vm.key }} = {
      cpu               = {{ vm.cpu }}
      ram               = {{ vm.ram }}
      os_image          = local.platform.os_images.debian
      user_data_file_id = local.platform.cloud_init.blank.file_id

      maintenance_network_cidr = "{{ vm.maintenance_cidr }}"

      network_devices = [
        {% for bridge in vm.bridges %}
        {
          bridge = local.sandbox_bridges.{{ bridge.key }}.name
        }{% if not loop.last %},{% endif %}
        {% endfor %}
      ]

      ip_configs = [
        {% for ip_config in vm.ip_configs %}
        {
          network_cidr = "{{ ip_config.cidr }}"
          gateway      = {% if ip_config.gateway is none %}null{% else %}"{{ ip_config.gateway }}"{% endif %}
        }{% if not loop.last %},{% endif %}
        {% endfor %}
      ]
    }{% if not loop.last %},{% endif %}
    {% endfor %}
  }
}
"""


@dataclass
class NetworkBridge:
    key: str
    name: str
    cidr: str
    network_type: str
    layer: str
    site: str


@dataclass
class IpConfig:
    cidr: str
    gateway: str | None


@dataclass
class VM:
    key: str
    name: str
    cpu: int
    ram: int
    maintenance_cidr: str
    bridges: List[NetworkBridge]
    ip_configs: List[IpConfig]


@dataclass
class GraphNode:
    id: str
    label: str
    type: str
    layer: str
    site: str
    network_type: str | None = None
    cidr: str | None = None


@dataclass
class GraphEdge:
    source: str
    target: str
    relationship: str
    network_type: str | None = None


class NetworkGraph:
    """
    Serializable representation of the generated topology.

    The resulting JSON deliberately uses a simple nodes/edges structure:

        {
          "nodes": [...],
          "edges": [...]
        }

    This makes the file straightforward to consume from NetworkX,
    Cytoscape, D3, Graphviz conversion scripts, etc.
    """

    def __init__(self) -> None:
        self.nodes: list[GraphNode] = []
        self.edges: list[GraphEdge] = []

    def add_node(self, node: GraphNode) -> None:
        self.nodes.append(node)

    def add_edge(self, edge: GraphEdge) -> None:
        self.edges.append(edge)

    def write(self, filename: str, seed: str) -> None:
        data = {
            "format": "enterprise-network-graph-v1",
            "seed": seed,
            "nodes": [asdict(node) for node in self.nodes],
            "edges": [asdict(edge) for edge in self.edges],
        }

        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")


def dns_name(value: str) -> str:
    """
    Convert a value into a valid DNS hostname.

    Labels:
      - a-z
      - 0-9
      - '-'
      - maximum 63 characters
      - cannot start/end with '-'
    """

    value = value.lower()
    value = re.sub(r"[^a-z0-9-]+", "-", value)
    value = re.sub(r"-+", "-", value)
    value = value.strip("-")

    if not value:
        value = "node"

    return value[:63].rstrip("-")


class ArchitectureGenerator:
    """
    Generates a hierarchical enterprise network.

    Upper layers are hierarchical:

        Internet
           |
         Edge
           |
         Core
        /    \\
    Dist-A  Dist-B
      / \\     / \\
     ...      ...

    Lower layers intentionally contain heterogeneous network types:

        users
        servers
        wifi
        voice
        iot
        storage
        management
        dmz
        transit
    """

    NETWORK_TYPES = (
        "users",
        "servers",
        "wifi",
        "voice",
        "iot",
        "storage",
        "management",
        "dmz",
        "transit",
    )

    def __init__(
        self,
        seed: str,
        network_count: int,
        vm_count: int,
        max_interfaces: int = 4,
        sites: int = 1,
    ) -> None:
        self.seed = seed
        self.random = random.Random(seed)

        self.network_count = max(1, network_count)
        self.vm_count = max(1, vm_count)
        self.max_interfaces = max(1, max_interfaces)
        self.site_count = max(1, sites)

        self.networks: list[NetworkBridge] = []
        self.vms: list[VM] = []
        self.graph = NetworkGraph()

        self._used_ip_addresses: dict[str, set[int]] = {}

    def generate(self) -> list[VM]:
        self._generate_hierarchy()
        self._generate_networks()
        self._generate_vms()

        return self.vms

    # ------------------------------------------------------------------
    # Hierarchical physical/logical topology
    # ------------------------------------------------------------------

    def _generate_hierarchy(self) -> None:
        """
        Generate the upper network layers.

        The topology intentionally becomes less hierarchical as it
        approaches the access layer.
        """

        # Internet / WAN.
        self.graph.add_node(
            GraphNode(
                id="internet",
                label="Internet",
                type="external",
                layer="wan",
                site="global",
            )
        )

        # One edge router/firewall pair per site.
        for site_index in range(self.site_count):
            site = f"site-{site_index + 1}"

            edge_id = f"{site}-edge"
            self.graph.add_node(
                GraphNode(
                    id=edge_id,
                    label=f"{site} Edge",
                    type="router-firewall",
                    layer="edge",
                    site=site,
                )
            )

            self.graph.add_edge(
                GraphEdge(
                    source="internet",
                    target=edge_id,
                    relationship="wan-uplink",
                    network_type="transit",
                )
            )

            # Two core routers per site for redundancy.
            core_ids = []

            for core_index in range(2):
                core_id = f"{site}-core-{core_index + 1}"
                core_ids.append(core_id)

                self.graph.add_node(
                    GraphNode(
                        id=core_id,
                        label=f"{site} Core {core_index + 1}",
                        type="router",
                        layer="core",
                        site=site,
                    )
                )

                self.graph.add_edge(
                    GraphEdge(
                        source=edge_id,
                        target=core_id,
                        relationship="edge-core",
                        network_type="transit",
                    )
                )

            # Distribution layer.
            distribution_count = max(
                2,
                min(6, 2 + self.network_count // 10),
            )

            for dist_index in range(distribution_count):
                dist_id = f"{site}-distribution-{dist_index + 1}"

                self.graph.add_node(
                    GraphNode(
                        id=dist_id,
                        label=f"{site} Distribution {dist_index + 1}",
                        type="switch",
                        layer="distribution",
                        site=site,
                    )
                )

                # Each distribution switch connects to both cores.
                for core_id in core_ids:
                    self.graph.add_edge(
                        GraphEdge(
                            source=core_id,
                            target=dist_id,
                            relationship="core-distribution",
                            network_type="transit",
                        )
                    )

            # Access switches form the lower, more heterogeneous layer.
            access_count = max(
                2,
                min(12, self.network_count // 2 + 2),
            )

            for access_index in range(access_count):
                access_id = f"{site}-access-{access_index + 1}"

                self.graph.add_node(
                    GraphNode(
                        id=access_id,
                        label=f"{site} Access {access_index + 1}",
                        type="switch",
                        layer="access",
                        site=site,
                    )
                )

                # Deliberately spread access switches across distribution.
                dist_id = (
                    f"{site}-distribution-" f"{(access_index % distribution_count) + 1}"
                )

                self.graph.add_edge(
                    GraphEdge(
                        source=dist_id,
                        target=access_id,
                        relationship="distribution-access",
                        network_type="ethernet",
                    )
                )

    # ------------------------------------------------------------------
    # Mixed lower-layer networks
    # ------------------------------------------------------------------

    def _generate_networks(self) -> None:
        """
        Generate heterogeneous lower-layer network segments.

        Unlike the upper hierarchy, these networks are intentionally
        mixed: VLANs, Wi-Fi, storage, IoT, management, DMZ, etc.
        """

        network_type_cycle = list(self.NETWORK_TYPES)

        for index in range(self.network_count):
            network_type = network_type_cycle[index % len(network_type_cycle)]

            site_index = index % self.site_count
            site = f"site-{site_index + 1}"

            cidr = self._network_cidr(index)

            key = f"network-{index + 1000}"
            name = f"vmbr{1000 + index}"

            bridge = NetworkBridge(
                key=key,
                name=name,
                cidr=cidr,
                network_type=network_type,
                layer="access",
                site=site,
            )

            self.networks.append(bridge)

            self.graph.add_node(
                GraphNode(
                    id=key,
                    label=name,
                    type="network",
                    layer="access",
                    site=site,
                    network_type=network_type,
                    cidr=cidr,
                )
            )

            # Attach the network to an access switch.
            access_count = max(
                2,
                min(12, self.network_count // 2 + 2),
            )

            access_index = index % access_count

            access_id = f"{site}-access-{access_index + 1}"

            self.graph.add_edge(
                GraphEdge(
                    source=access_id,
                    target=key,
                    relationship="network-segment",
                    network_type=network_type,
                )
            )

    def _network_cidr(self, index: int) -> str:
        """
        Allocate a deterministic /24 from 10.0.0.0/8.

        The layout leaves enough room for large generated topologies.
        """

        start = 5
        end = 200
        span = end - start

        second = (index // 256) % span + start
        third = index % 256

        return f"10.{second}.{third}.0/24"

    # ------------------------------------------------------------------
    # Endpoint generation
    # ------------------------------------------------------------------

    def _generate_vms(self) -> None:
        for vm_index in range(self.vm_count):
            site_index = vm_index % self.site_count
            site = f"site-{site_index + 1}"

            vm_name = dns_name(f"sandbox-{site}-vm-{vm_index + 1}")

            interface_count = self._interface_count()

            # Select networks according to their type rather than treating
            # every network as equivalent.
            bridges = self._select_vm_networks(
                interface_count=interface_count,
                site=site,
            )

            ip_configs = [self._allocate_ip_config(bridge) for bridge in bridges]

            vm = VM(
                key=vm_name,
                name=vm_name,
                cpu=self._cpu(),
                ram=self._ram(),
                maintenance_cidr=self._maintenance_cidr(vm_index),
                bridges=bridges,
                ip_configs=ip_configs,
            )

            self.vms.append(vm)

            vm_node_id = f"vm:{vm.key}"

            self.graph.add_node(
                GraphNode(
                    id=vm_node_id,
                    label=vm.name,
                    type="virtual-machine",
                    layer="endpoint",
                    site=site,
                )
            )

            for bridge in bridges:
                self.graph.add_edge(
                    GraphEdge(
                        source=vm_node_id,
                        target=bridge.key,
                        relationship="attached-to",
                        network_type=bridge.network_type,
                    )
                )

    def _select_vm_networks(
        self,
        interface_count: int,
        site: str,
    ) -> list[NetworkBridge]:
        candidates = [network for network in self.networks if network.site == site]

        if not candidates:
            candidates = self.networks

        interface_count = min(interface_count, len(candidates))

        # Prefer a heterogeneous combination of network types.
        by_type: dict[str, list[NetworkBridge]] = {}

        for network in candidates:
            by_type.setdefault(network.network_type, []).append(network)

        selected: list[NetworkBridge] = []

        network_types = list(by_type)
        self.random.shuffle(network_types)

        for network_type in network_types:
            if len(selected) >= interface_count:
                break

            selected.append(self.random.choice(by_type[network_type]))

        # Fill any remaining slots.
        remaining = [network for network in candidates if network not in selected]

        if remaining and len(selected) < interface_count:
            selected.extend(
                self.random.sample(
                    remaining,
                    k=min(
                        interface_count - len(selected),
                        len(remaining),
                    ),
                )
            )

        return selected

    # ------------------------------------------------------------------
    # Resource allocation
    # ------------------------------------------------------------------

    def _interface_count(self) -> int:
        if self.max_interfaces == 1:
            return 1

        roll = self.random.random()

        if roll < 0.55:
            return 1
        if roll < 0.85:
            return min(2, self.max_interfaces)
        if roll < 0.97:
            return min(3, self.max_interfaces)

        return self.max_interfaces

    def _cpu(self) -> int:
        return self.random.choices(
            population=[1],
            weights=[1],
            k=1,
        )[0]

    def _ram(self) -> int:
        return self.random.choices(
            population=[1024, 2048, 4096],
            weights=[100, 50, 1],
            k=1,
        )[0]

    def _maintenance_cidr(self, vm_index: int) -> str:
        network = SANDBOX_MAINTENANCE_NETWORK
        host = vm_index + 10
        if host >= network.num_addresses - 1:
            raise ValueError(f"Too many sandbox VMs for {network}")
        return f"{network.network_address + host}/{network.prefixlen}"

    def _allocate_ip_config(
        self,
        bridge: NetworkBridge,
    ) -> IpConfig:
        network = ipaddress.ip_network(bridge.cidr)

        used = self._used_ip_addresses.setdefault(
            bridge.key,
            set(),
        )

        # Host addresses 1-9 are reserved for infrastructure.
        available = list(range(10, 250))

        # Deterministically shuffle the candidate addresses.
        self.random.shuffle(available)

        host = next(candidate for candidate in available if candidate not in used)

        used.add(host)

        address = str(network.network_address + host)

        # Some network types normally have a default gateway.
        gateway_types = {
            "users",
            "servers",
            "wifi",
            "voice",
            "iot",
            "management",
            "dmz",
        }

        gateway = None

        if bridge.network_type in gateway_types:
            gateway = str(network.network_address + 1)

        return IpConfig(
            cidr=f"{address}/{network.prefixlen}",
            gateway=gateway,
        )


def render(
    seed: str,
    networks: list[NetworkBridge],
    vms: list[VM],
) -> str:
    environment = jinja2.Environment(
        loader=jinja2.BaseLoader(),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    template = environment.from_string(TEMPLATE)

    return template.render(
        seed=seed,
        networks=networks,
        sandbox_vms=vms,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a hierarchical enterprise network " "and Terraform configuration."
        )
    )

    parser.add_argument(
        "--seed",
        default="sandbox",
        help="Deterministic architecture seed.",
    )

    parser.add_argument(
        "--networks",
        type=int,
        default=4,
        help="Number of lower-layer network segments.",
    )

    parser.add_argument(
        "--vms",
        type=int,
        default=20,
        help="Number of virtual machines.",
    )

    parser.add_argument(
        "--sites",
        type=int,
        default=2,
        help="Number of enterprise sites.",
    )

    parser.add_argument(
        "--max-interfaces",
        type=int,
        default=4,
        help="Maximum number of network interfaces per VM.",
    )

    parser.add_argument(
        "--network-file",
        metavar="FILE",
        help=(
            "Write the generated network graph as JSON to FILE. "
            "The JSON contains nodes and edges and can be used "
            "by network visualization tooling."
        ),
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    generator = ArchitectureGenerator(
        seed=args.seed,
        network_count=args.networks,
        vm_count=args.vms,
        max_interfaces=args.max_interfaces,
        sites=args.sites,
    )

    vms = generator.generate()

    # Terraform/HCL goes to stdout.
    print(
        render(
            seed=args.seed,
            networks=generator.networks,
            vms=vms,
        )
    )

    # The topology is independently serialized for visualization.
    if args.network_file:
        generator.graph.write(
            filename=args.network_file,
            seed=args.seed,
        )


if __name__ == "__main__":
    main()
