# Offline mirror for the private internet

**This workflow cannot run as written** (`docs/06-background-processes.md`, "DNS and private internet"). It needs a helper
client on the services bridge while the internet window is open, and none can exist: only the
sandbox and devbox gateways and VMs may reach devpi and the registry, the window is refused while
any of them runs, the private-internet VMs reach only the apt cache, and the node has no address
on the services bridge. What `worms build` puts into the caches today is the apt seed
(`apt_cache_seed_packages`) and a minimal devpi seed.

## Package lists

- `apt-packages.txt`: what apt-cacher-ng should hold (services, PoC build dependencies, non-free
  man pages, a CLI/dev/net base). The MongoDB packages come from repo.mongodb.org.
- `docker-images.txt`: images for the registry, derived from the PoC Dockerfiles.
- `pip-packages.txt`: what devpi should hold (the apps, cve-search's dependencies, extras).

## The intended workflow

1. `worms build internet-allow`, then `SetupPrivateInternet.yml`, so the caches run online.
2. On a helper client in the private internet: `./populate-caches.sh` (apt, pip, MongoDB `.deb`s,
   the feedformatter zip).
3. Point `cve_search_feedformatter_src` and `cve_search_mongodb_repo_url` at the local copies.
4. `worms build internet-deny`, then `worms build offline`: the caches switch to offline mode.
