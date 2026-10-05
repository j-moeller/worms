# Offline mirror for the private internet

The private internet can run fully offline, but the caches must be
**pre-populated** while the internet is still reachable (the prep phase).

## Package lists
- `apt-packages.txt` — everything apt-cacher-ng should hold (services, PoC build
  deps, non-free POSIX man pages, a useful CLI/dev/net base). MongoDB packages are
  listed but come from repo.mongodb.org (mirrored separately).
- `pip-packages.txt` — everything devpi should hold (our apps + full cve-search
  dependency closure + useful extras). feedformatter is a GitHub zip, handled apart.

## Workflow
1. **Population phase** — deploy with `INTERNET_ENABLED=true` (the host egress is
   open, so the VMs reach the internet directly; caches fetch straight upstream).
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
3. **Go offline** — redeploy with `INTERNET_ENABLED=false`. The platform stack
   activates a proxmox-firewall forward rule that drops the subnet's egress to the
   WAN (persistent, in `/etc/pve`), and Ansible flips the caches to offline mode,
   so they serve purely from their local stores.
