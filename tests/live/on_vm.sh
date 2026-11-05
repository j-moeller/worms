#!/bin/sh
# Runs on a devbox VM as root, in a team member's own SSH session: what a team does there.
# Prints one key=value line per result, and never the VM's token.
#
#   sh on_vm.sh <marker> <apt package> <pip package>
MARKER=$1
APT_PACKAGE=$2
PIP_PACKAGE=$3
FILE=/root/worms-live-marker

report() { printf '%s=%s\n' "$1" "$(printf '%s' "$2" | tr '\n' ' ' | cut -c1-160)"; }
post() { # service, JSON body
  curl -sS -o /dev/null --max-time 20 -w 'http=%{http_code} tls_verify=%{ssl_verify_result}' \
    -X POST "https://$1.worms.internal/submit" -H 'Content-Type: application/json' -d "$2" 2>&1
}
closed() { # URL: anything but a connection counts as closed
  if curl -sS -o /dev/null --max-time 6 "$1" 2>/dev/null; then echo open; else echo closed; fi
}

report host "$(hostname)"
report user "$(id -un)"

# A file
echo "$MARKER" > "$FILE"
report file "$(cat "$FILE")"

# apt, through the private internet
report apt_source "$(grep -hE '^URIs' /etc/apt/sources.list.d/*.sources 2>/dev/null | sort -u | head -1)"
timeout 200 apt-get update >/dev/null 2>&1
report apt_update_rc "$?"
report apt_before "$(command -v "$APT_PACKAGE" || echo absent)"
DEBIAN_FRONTEND=noninteractive timeout 280 apt-get install -y --no-install-recommends "$APT_PACKAGE" >/dev/null 2>&1
report apt_install_rc "$?"
report apt_after "$(command -v "$APT_PACKAGE" || echo absent)"

# pip, through the private internet, with no special flags
report pip_source "$(pip3 config get global.index-url 2>&1)"
report pip_before "$(python3 -c "import $PIP_PACKAGE; print('present')" 2>/dev/null || echo absent)"
timeout 200 pip3 install --no-cache-dir "$PIP_PACKAGE" >/dev/null 2>&1
report pip_install_rc "$?"
report pip_after "$(python3 -c "import $PIP_PACKAGE; print('present')" 2>/dev/null || echo absent)"

# Scoring and logging, over verified TLS
report scoring "$(post scoring "{\"vm_flag\":\"$(tr -d '\n' </etc/worms/flag)\"}")"
report logging "$(post logging "{\"log_data\":{\"marker\":\"$MARKER\",\"vm\":\"$(hostname)\"}}")"

# The real internet is out of reach
report internet_debian "$(closed https://deb.debian.org/)"
report internet_pypi "$(closed https://pypi.org/simple/)"
report internet_ip "$(closed http://1.1.1.1/)"
