#!/usr/bin/env python3
"""worms-sbom: a software bill of materials of the platform, one CSV per component in sbom/ and
all of them in sbom/sbom.csv (src/worms_cli/sbom.py, docs/03-manage-py.md#worms-sbom).

    worms-sbom() { /root/app/.venv-test/bin/python /root/app/src/worms_sbom.py "$@"; }
    worms-sbom                  # every component
    worms-sbom host devpi       # these only
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from worms_cli.sbom import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
