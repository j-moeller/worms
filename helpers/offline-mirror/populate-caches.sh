#!/usr/bin/env bash
# Populate the private-internet caches so they can run fully offline.
#
# Run on a helper Debian/Ubuntu client INSIDE the private internet during the
# population phase (INTERNET_ENABLED=true: caches reachable and the subnet still
# has direct internet). Afterwards run everything with INTERNET_ENABLED=false and
# redeploy -> the platform firewall seals egress and the caches serve offline.
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
DOCKER_REGISTRY_HOST="${DOCKER_REGISTRY_HOST:-10.2.0.22}"; DOCKER_REGISTRY_PORT="${DOCKER_REGISTRY_PORT:-5000}"
MIRROR_DIR="${MIRROR_DIR:-/srv/offline-mirror}"
DEVPI_INDEX="http://${DEVPI_HOST}:${DEVPI_PORT}/root/pypi/+simple/"

sudo install -d "$MIRROR_DIR"/{mongodb,pip}
sudo chown -R "$(id -u):$(id -g)" "$MIRROR_DIR"  # runs as non-root; downloads below write here

echo "==> prerequisites (dpkg-dev for dpkg-scanpackages, pip/venv for devpi fill, skopeo for images)"
sudo apt-get install -y dpkg-dev curl ca-certificates python3-pip python3-venv skopeo || true

echo "==> [apt] route apt through apt-cacher-ng (path-style; caches HTTPS upstream), enable non-free"
ACNG="http://${APT_CACHE_HOST}:${APT_CACHE_PORT}"
# Path-style (http://cache/deb.debian.org/debian) -- apt-cacher-ng does the HTTPS
# upstream fetch and caches over HTTP. Do NOT use an Acquire CONNECT proxy with
# https:// sources: apt-cacher-ng refuses the tunnel and nothing gets cached.
sudo tee /etc/apt/sources.list.d/debian.sources >/dev/null <<SRC
Types: deb
URIs: ${ACNG}/deb.debian.org/debian
Suites: trixie trixie-updates trixie-backports
Components: main contrib non-free non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg

Types: deb
URIs: ${ACNG}/deb.debian.org/debian-security
Suites: trixie-security
Components: main contrib non-free non-free-firmware
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
SRC
sudo rm -f /etc/apt/apt.conf.d/01proxy
# MongoDB's HTTPS 3rd-party repo can't be cached by apt-cacher-ng; with no apt proxy
# set, apt fetches it directly during the prep phase (mirrored to MIRROR_DIR below).
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

echo "==> [docker] mirror images into the private registry (skopeo, no docker daemon needed)"
REGISTRY="${DOCKER_REGISTRY_HOST}:${DOCKER_REGISTRY_PORT}"
command -v skopeo >/dev/null 2>&1 || sudo apt-get install -y skopeo || echo "   (skopeo unavailable; skipping docker)"
if command -v skopeo >/dev/null 2>&1; then
  mapfile -t IMAGES < <(grep -vE '^\s*#|^\s*$' "$HERE/docker-images.txt")
  for img in "${IMAGES[@]}"; do
    echo "   -> $img"
    skopeo copy --dest-tls-verify=false "docker://$img" "docker://$REGISTRY/$img" || echo "   (skip: $img)"
  done
fi

echo
echo "DONE. Next:"
echo "  1) copy $MIRROR_DIR/feedformatter.zip to the cve-search VM; set cve_search_feedformatter_src to it."
echo "  2) serve $MIRROR_DIR/mongodb as a local apt repo; set cve_search_mongodb_repo_url to it."
echo "  3) redeploy with INTERNET_ENABLED=false."
