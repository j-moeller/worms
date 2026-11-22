#!/usr/bin/env python3
"""worms-gui -- an htop-like, interactive dashboard of the WORMS platform, beside the `worms` CLI.

Every VM, host job, round, service and component, live; the CLI's own commands on the marked ones
(`worms vm stop`, `worms jobs cancel`, `worms login`, ...): `?` in it lists the keys. Operators define

    worms-gui() { /root/app/.venv-test/bin/python /root/app/src/worms_gui.py "$@"; }

as they define `worms`. It runs where `worms` runs: on the host, or a laptop with WORMS_SSH=root@<host>.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from worms_cli.gui.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
