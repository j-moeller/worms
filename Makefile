# =============================================================================
# worms-platform
#
# All functionality now lives in the `worms` CLI (src/manage.py); this Makefile
# is only a thin entry point. See docs/03-manage-py.md and `worms --help`.
#
#   make            Build and configure the entire infrastructure end to end
#                   (idempotent): = `worms apply --all`. Runs on the Proxmox node (or
#                   set WORMS_SSH to drive it over SSH from your laptop).
#   make destroy    Destroy the VM stacks (keeps the firewall and platform).
#   make destroy_all Destroy everything (VM stacks, firewall, platform).
#
# Everything else is a `worms` command (`worms --help`): status, show, log,
# apply <component> (`worms apply --help`), vm, jobs, internet, test, ...
# =============================================================================

WORMS = python3 src/manage.py

.PHONY: all destroy destroy_all

all:
	$(WORMS) apply --all --yes

destroy:
	$(WORMS) destroy --yes

destroy_all:
	$(WORMS) destroy --all --yes
