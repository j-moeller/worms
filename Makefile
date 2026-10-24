# =============================================================================
# worms-platform
#
# All functionality now lives in the `worms` CLI (src/manage.py); this Makefile
# is only a thin entry point. See docs/management.md and `worms --help`.
#
#   make            Build and configure the entire infrastructure end to end
#                   (idempotent): = `worms build`. Runs on the Proxmox node (or
#                   set WORMS_SSH to drive it over SSH from your laptop).
#   make destroy    Destroy the VM stacks (keeps the firewall and platform).
#   make destroy_all Destroy everything (VM stacks, firewall, platform).
#
# Everything else the Makefile used to do is a CLI command or `worms build
# <component>` (see `worms build --list`): secrets, platform, firewall,
# host-network, infrastructure, inventory, configure/provision, the internet
# window, round/devbox builds, ping, debug, simulate, test.
# =============================================================================

WORMS = python3 src/manage.py

.PHONY: all destroy destroy_all

all:
	$(WORMS) build

destroy:
	$(WORMS) destroy --yes

destroy_all:
	$(WORMS) destroy-all --yes
