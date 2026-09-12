import argparse
import ipaddress
import random
import re
from dataclasses import dataclass
from typing import List

import jinja2

TEMPLATE = """
locals {
  seed = "{{ seed }}"

  sandbox_bridges = {
    {% for network in networks %}
    {{ network.key }} = {
      name         = "{{ network.name }}"
      network_cidr = "{{ network.cidr }}"
    }
    {% endfor %}
  }

  sandbox_vms = {
    {% for vm in sandbox_vms %}
    {{ vm.key }} = {
      cpu               = {{ vm.cpu }}
      ram               = {{ vm.ram }}
      disk_file_id      = local.platform.os_images.debian.file_id
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


def dns_name(value: str) -> str:
    """
    Convert a value into a DNS-compatible hostname.

    DNS labels:
      - contain only a-z, 0-9 and '-'
      - cannot start or end with '-'
      - are at most 63 characters
    """
    value = value.lower()
    value = re.sub(r"[^a-z0-9-]+", "-", value)
    value = re.sub(r"-+", "-", value)
    value = value.strip("-")

    if not value:
        value = "vm"

    return value[:63].rstrip("-")


class ArchitectureGenerator:
    def __init__(
        self,
        seed: str,
        network_count: int,
        vm_count: int,
        max_interfaces: int = 4,
    ) -> None:
        self.seed = seed
        self.random = random.Random(seed)

        self.network_count = max(1, network_count)
        self.vm_count = max(1, vm_count)
        self.max_interfaces = max(1, max_interfaces)

        self.networks: List[NetworkBridge] = []
        self.vms: List[VM] = []

    def generate(self) -> List[VM]:
        self._generate_networks()
        self._generate_vms()
        return self.vms

    def _generate_networks(self) -> None:
        for index in range(self.network_count):
            start_network = 5
            end_network = 200
            span = end_network - start_network

            second_octet = (index % span) + start_network
            third_octet = (index // 256) % 256

            cidr = f"10.{second_octet}.{third_octet}.0/24"

            # Both key and name are valid identifiers/names.
            #
            # The key is deliberately DNS-safe too, so it can be used
            # consistently throughout the generated architecture.
            key = f"bridge-{index + 1000}"

            self.networks.append(
                NetworkBridge(
                    key=key,
                    name=f"vmbr{1000 + index}",
                    cidr=cidr,
                )
            )

    def _generate_vms(self) -> None:
        for vm_index in range(self.vm_count):
            # DNS-safe VM name and key.
            #
            # Including the index guarantees uniqueness even if the
            # seed contains characters that normalize to the same value.
            base_name = dns_name(f"sandbox-{self.seed}-vm-{vm_index + 1}")

            # A DNS name must remain <= 63 characters.
            vm_name = base_name[:63].rstrip("-")

            interface_count = self._interface_count()

            bridges = self.random.sample(
                self.networks,
                k=min(interface_count, len(self.networks)),
            )

            ip_configs = [
                self._allocate_ip_config(
                    bridge=bridge,
                    vm_index=vm_index,
                    interface_index=interface_index,
                )
                for interface_index, bridge in enumerate(bridges)
            ]

            self.vms.append(
                VM(
                    key=vm_name,
                    name=vm_name,
                    cpu=self._cpu(),
                    ram=self._ram(),
                    maintenance_cidr=self._maintenance_cidr(vm_index),
                    bridges=bridges,
                    ip_configs=ip_configs,
                )
            )

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
            population=[1, 2],
            weights=[10, 30],
            k=1,
        )[0]

    def _ram(self) -> int:
        return self.random.choices(
            population=[256, 512, 1024, 2048],
            weights=[50, 30, 10, 1],
            k=1,
        )[0]

    def _maintenance_cidr(self, vm_index: int) -> str:
        host = vm_index + 10
        return f"10.3.0.{host}/24"

    def _allocate_ip_config(
        self,
        bridge: NetworkBridge,
        vm_index: int,
        interface_index: int,
    ) -> IpConfig:
        network = ipaddress.ip_network(bridge.cidr)

        bridge_index = self.networks.index(bridge)

        host = 10 + vm_index * 3 + interface_index
        host = 10 + ((host + bridge_index * 17) % 240)

        address = str(network.network_address + host)

        gateway = None

        if self.random.random() < 0.70:
            gateway = str(network.network_address + 1)

        return IpConfig(
            cidr=f"{address}/{network.prefixlen}",
            gateway=gateway,
        )


def render(
    seed: str,
    networks: List[NetworkBridge],
    vms: List[VM],
) -> str:
    assert len(vms) == len(set([vm.maintenance_cidr for vm in vms]))

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
        description="Generate a synthetic Terraform network architecture."
    )

    parser.add_argument(
        "--seed",
        default="sandbox",
        help="Deterministic architecture seed.",
    )

    parser.add_argument(
        "--networks",
        type=int,
        default=8,
        help="Number of virtual network bridges.",
    )

    parser.add_argument(
        "--vms",
        type=int,
        default=100,
        help="Number of virtual machines.",
    )

    parser.add_argument(
        "--max-interfaces",
        type=int,
        default=4,
        help="Maximum number of network interfaces per VM.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    generator = ArchitectureGenerator(
        seed=args.seed,
        network_count=args.networks,
        vm_count=args.vms,
        max_interfaces=args.max_interfaces,
    )

    vms = generator.generate()

    print(
        render(
            seed=args.seed,
            networks=generator.networks,
            vms=vms,
        )
    )


if __name__ == "__main__":
    main()
