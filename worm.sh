#!/usr/bin/env python3
"""A minimal WORMS worm (docs/worm-contract.md).

Rename to main.py, zip it, upload it on the dashboard. The round agent runs
`python3 main.py` in /home/debian/worm on a sandbox VM, as the `debian` user.
This one reads its own VM's flag, submits it to the scoring server (which
captures this VM for your team), and logs one line. HTTPS is verified against
the internal CA, which the sandbox trusts, so no --insecure is needed; no team
token or id is sent -- only one team runs at a time and the server knows which.

Capturing the other VMs is the worm's own job: reach them across the sandbox
network and submit their flags too. This skeleton only captures its start VM.
"""

import json
import urllib.request

FLAG_PATH = "/etc/worms/flag"
SCORING = "https://scoring.worms.internal/submit"
LOGGING = "https://logging.worms.internal/submit"


def post(url, payload):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status


def read_flag():
    with open(FLAG_PATH, encoding="utf-8") as handle:
        return handle.read().strip()


def main():
    flag = read_flag()
    status = post(SCORING, {"vm_flag": flag})
    post(LOGGING, {"log_data": {"event": "submitted start-vm flag", "scoring_status": status}})


if __name__ == "__main__":
    main()
