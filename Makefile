# =============================================================================
# worms-platform
#
# All functionality now lives in the `worms` CLI (src/manage.py); this Makefile
# is only a thin entry point. See docs/03-manage-py.md and `worms --help`.
#
#   make            Build and configure the entire infrastructure end to end
#                   (idempotent): = `worms build`. Runs on the Proxmox node (or
#                   set WORMS_SSH to drive it over SSH from your laptop).
#   make destroy    Destroy the VM stacks (keeps the firewall and platform).
#   make destroy_all Destroy everything (VM stacks, firewall, platform).
#
# Everything else is a `worms` verb (`worms --help`): status, show, login,
# build <component> (`worms build --help`), internet, reset, test, step, ...
# =============================================================================

WORMS = python3 src/manage.py

.PHONY: all destroy destroy_all

all:
	$(WORMS) build

destroy:
	$(WORMS) destroy --yes

destroy_all:
	$(WORMS) destroy --all --yes
