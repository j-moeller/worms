# Offline mirror for the private internet

The private internet can run fully offline (`internet_enabled=false`), but the
caches must be **pre-populated** while internet is still available.

## Package lists
- `apt-packages.txt` — everything apt-cacher-ng should hold (services, PoC build
  deps, non-free POSIX man pages, a useful CLI/dev/net base). MongoDB packages are
  listed but come from repo.mongodb.org (mirrored separately).
- `pip-packages.txt` — everything devpi should hold (our apps + full cve-search
  dependency closure + useful extras). feedformatter is a GitHub zip, handled apart.

## Workflow
1. **Population phase** — deploy with `internet_enabled=true` (egress-proxy up,
   caches in proxy mode). On a helper client in the private internet, run:
   ```
   ./populate-caches.sh
   ```
   This pulls all apt packages (+deps, incl non-free) through apt-cacher-ng, all
   pip packages through devpi, mirrors the MongoDB .debs directly, and fetches the
   feedformatter zip.
2. **Wire the offline sources** (see script output): point
   `cve_search_feedformatter_src` at the local zip and `cve_search_mongodb_repo_url`
   at the local MongoDB repo.
3. **Go offline** — set `internet_enabled=false` in both
   `opentofu/stacks/private_internet` (var) and `ansible/group_vars/all.yml`,
   then redeploy. The egress-proxy VM is destroyed; caches serve from their stores.
