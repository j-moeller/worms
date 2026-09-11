# Service Network
# iptables -t nat -A POSTROUTING -s 10.1.0.0/24 -o vmbr0 -j MASQUERADE
# iptables -t nat -A POSTROUTING -s 10.2.0.0/24 -o vmbr0 -j MASQUERADE
# iptables -t nat -A POSTROUTING -s 10.3.0.0/24 -o vmbr0 -j MASQUERADE

# iptables -t nat -L POSTROUTING -n -v --line-numbers
# iptables -t nat -D POSTROUTING 1