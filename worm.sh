#!/usr/bin/env bash
# A minimal WORMS worm (docs/08-user-guide.md).
#
# Rename to main.sh, zip it, upload it on the dashboard. It runs as `bash main.sh`
# in /home/debian/worm on a sandbox VM, as the `debian` user. This one reads its own
# VM's flag, submits it to the scoring server (which captures this VM for your team),
# and logs one line. HTTPS is verified against the internal CA, which the sandbox
# trusts, so no --insecure is needed; no team token or id is sent: only one team runs
# at a time and the server knows which.
#
# Capturing the other VMs is the worm's own job: reach them across the sandbox network
# and submit their flags too. This skeleton only captures its start VM.
set -eu

FLAG_PATH="${FLAG_PATH:-/etc/worms/flag}"
SCORING=https://scoring.worms.internal/submit
LOGGING=https://logging.worms.internal/submit

post() { # URL, JSON body. Prints the HTTP status.
    curl -sS --max-time 10 -o /dev/null -w '%{http_code}' -X POST "$1" \
        -H 'Content-Type: application/json' -d "$2"
}

flag=$(cat "$FLAG_PATH")
status=$(post "$SCORING" "{\"vm_flag\":\"$flag\"}")
post "$LOGGING" "{\"log_data\":{\"event\":\"submitted start-vm flag\",\"scoring_status\":$status}}" >/dev/null
