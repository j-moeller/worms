#!/usr/bin/env python3
"""Try to transmit COUNT broadcast frames on IFACE (ansible/Simulate.yml, on
the monitor): its capture NIC must be receive-only, the host drops them.

argv: iface count
"""
import socket
import sys

iface, count = sys.argv[1], int(sys.argv[2])
frame = b"\xff" * 6 + b"\x02\x00\x00\x00\x00\x01" + b"\x88\xb5" + b"worms-simulation".ljust(46, b"\x00")
with socket.socket(socket.AF_PACKET, socket.SOCK_RAW) as s:
    s.bind((iface, 0))
    sent = sum(1 for _ in range(count) if s.send(frame))
print(sent)
