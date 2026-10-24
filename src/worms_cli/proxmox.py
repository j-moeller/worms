"""Infrastructure status from the Proxmox API (VMs, bridges, firewall, pools).

Read-only. Lazily constructed, so the CLI runs without proxmoxer installed or
Proxmox configured -- the status command then marks the infrastructure probes
`unavailable` instead of failing.
"""

from __future__ import annotations


class ProxmoxUnavailable(RuntimeError):
    pass


class Infra:
    def __init__(self, config):
        self._cfg = config.proxmox()
        self._api = None

    @property
    def configured(self) -> bool:
        return self._cfg is not None

    def api(self):
        if self._cfg is None:
            raise ProxmoxUnavailable(
                "no Proxmox API configured (WORMS_PROXMOX_URL / _TOKEN_NAME / _TOKEN_VALUE)"
            )
        if self._api is None:
            try:
                from proxmoxer import ProxmoxAPI
            except ModuleNotFoundError as exc:
                raise ProxmoxUnavailable("proxmoxer is not installed (pip install proxmoxer)") from exc
            from urllib.parse import urlsplit

            url = self._cfg["url"]
            parts = urlsplit(url if "://" in url else f"https://{url}")
            self._api = ProxmoxAPI(
                parts.hostname,
                port=parts.port or 8006,
                user=self._cfg["user"],
                token_name=self._cfg["token_name"],
                token_value=self._cfg["token_value"],
                verify_ssl=self._cfg["verify_ssl"],
                timeout=10,
            )
        return self._api

    @property
    def node(self) -> str:
        return (self._cfg or {}).get("node", "worms-pve")

    def vms(self) -> list[dict]:
        """Every VM on the node with its status, name, VMID and pool."""
        node = self.api().nodes(self.node)
        vms = node.qemu.get()
        # Map VMID -> pool.
        pool_of: dict[int, str] = {}
        for pool in self.api().pools.get():
            members = self.api().pools(pool["poolid"]).get().get("members", [])
            for member in members:
                if member.get("type") == "qemu":
                    pool_of[member["vmid"]] = pool["poolid"]
        for vm in vms:
            vm["pool"] = pool_of.get(vm["vmid"], "")
        return vms

    def bridges(self) -> list[dict]:
        """Linux bridges configured on the node."""
        return self.api().nodes(self.node).network.get(type="bridge")

    def firewall_enabled(self) -> bool:
        options = self.api().cluster.firewall.options.get()
        return bool(options.get("enable"))
