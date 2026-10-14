# Offline mirror for the private internet

The private internet can run fully offline, but the caches must be
**pre-populated** while the internet is still reachable (the prep phase).

## Package lists
- `apt-packages.txt` — everything apt-cacher-ng should hold (services, PoC build
  deps, non-free POSIX man pages, a useful CLI/dev/net base). MongoDB packages are
  listed but come from repo.mongodb.org (mirrored separately).
- `docker-images.txt` — container images to mirror into the docker-registry, **derived from the PoC
  Dockerfiles** (`grep -rhiE '^FROM ' pocs/**/Dockerfile`). Re-derive when PoCs change.
- `pip-packages.txt` — everything devpi should hold (our apps + full cve-search
  dependency closure + useful extras). feedformatter is a GitHub zip, handled apart.

## Workflow
1. **Population phase** — `make internet-allow`, then
   `cd ansible && ansible-playbook -e @group_vars/vault.yml SetupPrivateInternet.yml`
   (as root on the Proxmox node): the caches run in online mode and fetch upstream.
   (A plain `make` populates them too, but denies internet access when it ends.)
   On a helper client in the private internet, run:
   ```
   ./populate-caches.sh
   ```
   This pulls all apt packages (+deps, incl non-free) through apt-cacher-ng, all
   pip packages through devpi, mirrors the MongoDB .debs directly, and fetches the
   feedformatter zip.
2. **Wire the offline sources** (see script output): point
   `cve_search_feedformatter_src` at the local zip and `cve_search_mongodb_repo_url`
   at the local MongoDB repo.
3. **Go offline** — `make internet-deny offline`:
   Ansible flips the caches to offline mode (it follows the host state), so
   they serve purely from their local stores.
