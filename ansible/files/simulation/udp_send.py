#!/usr/bin/env python3
"""Send COUNT UDP datagrams of SIZE payload bytes from SRC:SPORT to DST:DPORT
(ansible/Simulate.yml): a flow whose packet and byte counts are known exactly.

argv: src sport dst dport count size
"""
import socket
import sys
import time

src, sport, dst, dport, count, size = sys.argv[1:7]
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
    s.bind((src, int(sport)))
    for _ in range(int(count)):
        s.sendto(b"w" * int(size), (dst, int(dport)))
        time.sleep(0.05)
print(count)
