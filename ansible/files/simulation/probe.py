#!/usr/bin/env python3
"""TCP reachability probes for ansible/Simulate.yml, run on a VM.

argv[1]: JSON list of {"name", "host", "port"}. Prints JSON {name: result},
result = open | refused | timeout | error: <reason>. A firewall that drops
gives `timeout`; `refused` means the packet got through to a closed port.
"""
import concurrent.futures
import json
import socket
import sys


def probe(p):
    try:
        with socket.create_connection((p["host"], int(p["port"])), timeout=float(p.get("timeout", 3))):
            return "open"
    except ConnectionRefusedError:
        return "refused"
    except socket.timeout:
        return "timeout"
    except OSError as exc:
        return f"error: {exc.strerror or exc}"


probes = json.loads(sys.argv[1])
with concurrent.futures.ThreadPoolExecutor(16) as pool:
    print(json.dumps(dict(zip((p["name"] for p in probes), pool.map(probe, probes)))))
