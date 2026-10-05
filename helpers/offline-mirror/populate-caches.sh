#!/usr/bin/env bash
# Populate the private-internet caches so they can run fully offline.
#
# Run on a helper Debian/Ubuntu client INSIDE the private internet during the
# population phase (INTERNET_ENABLED=true: caches reachable, real internet still
# available via the egress-proxy). Afterwards run everything with
# INTERNET_ENABLED=false and redeploy -> the caches serve offline.
#
# Fills the existing caches by pulling content THROUGH them:
#   - apt-cacher-ng  <- apt-get --download-only of apt-packages.txt (+deps, incl non-free)
#   - devpi          <- pip download of pip-packages.txt (through the devpi index)
# plus two things the caches can't hold on their own:
#   - MongoDB .debs  (repo.mongodb.org HTTPS 3rd-party -> direct deb mirror)
#   - feedformatter github zip (not on PyPI)
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

APT_CACHE_HOST="${APT_CACHE_HOST:-10.2.0.20}"; APT_CACHE_PORT="${APT_CACHE_PORT:-3142}"
DEVPI_HOST="${DEVPI_HOST:-10.2.0.21}";         DEVPI_PORT="${DEVPI_PORT:-3141}"
MONGO_VER="${MONGO_VER:-8.0}"; MONGO_REL="${MONGO_REL:-bookworm}"
MIRROR_DIR="${MIRROR_DIR:-/srv/offline-mirror}"
DEVPI_INDEX="http://${DEVPI_HOST}:${DEVPI_PORT}/root/pypi/+simple/"

sudo install -d "$MIRROR_DIR"/{mongodb,pip}

echo "==> prerequisites (dpkg-dev provides dpkg-scanpackages)"
sudo apt-get install -y dpkg-dev curl ca-certificates || true

echo "==> [apt] point apt at apt-cacher-ng, enable non-free, add mongodb repo"
echo "Acquire::http::Proxy \"http://${APT_CACHE_HOST}:${APT_CACHE_PORT}\";" | sudo tee /etc/apt/apt.conf.d/01proxy >/dev/null
sudo sed -i -E 's/^Components:.*$/Components: main contrib non-free non-free-firmware/' /etc/apt/sources.list.d/debian.sources || true
sudo install -d /etc/apt/keyrings
curl -fsSL "https://www.mongodb.org/static/pgp/server-${MONGO_VER}.asc" | sudo tee /etc/apt/keyrings/mongodb-server-${MONGO_VER}.asc >/dev/null
echo "deb [ signed-by=/etc/apt/keyrings/mongodb-server-${MONGO_VER}.asc ] https://repo.mongodb.org/apt/debian ${MONGO_REL}/mongodb-org/${MONGO_VER} main" \
  | sudo tee /etc/apt/sources.list.d/mongodb-org-${MONGO_VER}.list >/dev/null
sudo apt-get update || true

echo "==> [apt] download each package (+deps) through apt-cacher-ng (tolerant)"
mapfile -t APT_PKGS < <(grep -vE '^\s*#|^\s*$' "$HERE/apt-packages.txt")
for p in "${APT_PKGS[@]}"; do
  sudo apt-get install -y --download-only --no-install-recommends "$p" || echo "   (skip: $p)"
done

echo "==> [mongodb] direct deb mirror (apt-cacher-ng can't cache the HTTPS 3rd-party repo)"
cd "$MIRROR_DIR/mongodb"
for p in mongodb-org mongodb-org-server mongodb-org-mongos mongodb-org-tools mongodb-org-database mongodb-mongosh; do
  apt-get download "$p" 2>/dev/null || echo "   (skip: $p)"
done
dpkg-scanpackages . /dev/null 2>/dev/null | gzip -9c | sudo tee Packages.gz >/dev/null || echo "   (dpkg-scanpackages failed; is dpkg-dev installed?)"
cd - >/dev/null

echo "==> [pip] download through devpi (bulk, else per-line fallback)"
if ! python3 -m pip download --index-url "$DEVPI_INDEX" --trusted-host "$DEVPI_HOST" \
       -r "$HERE/pip-packages.txt" -d "$MIRROR_DIR/pip"; then
  echo "   bulk resolve failed -> retrying per package (tolerant)"
  grep -vE '^\s*#|^\s*$' "$HERE/pip-packages.txt" | while read -r pkg; do
    python3 -m pip download --index-url "$DEVPI_INDEX" --trusted-host "$DEVPI_HOST" \
      "$pkg" -d "$MIRROR_DIR/pip" || echo "   (skip: $pkg)"
  done
fi

echo "==> [feedformatter] fetch the github zip (not on PyPI)"
curl -fsSL "https://github.com/marianoguerra/feedformatter/archive/master.zip" \
  -o "$MIRROR_DIR/feedformatter.zip" || echo "   (feedformatter download failed)"

echo
echo "DONE. Next:"
echo "  1) copy $MIRROR_DIR/feedformatter.zip to the cve-search VM; set cve_search_feedformatter_src to it."
echo "  2) serve $MIRROR_DIR/mongodb as a local apt repo; set cve_search_mongodb_repo_url to it."
echo "  3) redeploy with INTERNET_ENABLED=false."
