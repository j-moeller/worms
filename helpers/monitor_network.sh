MONITORED=vmbr1
MIRROR=vmbr4

VM_INTERFACE=ens19

bridge link | grep "master ${MONITORED}"

# Enable ingress
tc qdisc add dev ${MONITORED} clsact
tc qdisc show dev ${MONITORED}
# Expected: qdisc clsact ffff:

# Mirror ingress
tc filter add \
    dev ${MONITORED} \
    ingress \
    matchall \
    action mirred egress mirror dev ${MIRROR}

# Mirror egress
tc filter add \
    dev ${MONITORED} \
    egress \
    matchall \
    action mirred egress mirror dev ${MIRROR}

tc -s filter show dev ${MONITORED} ingress
tc -s filter show dev ${MONITORED} egress

# On VM
# This should show VM_INTERFACE:
ip addr
ip link set ${VM_INTERFACE} up
ip link set ${VM_INTERFACE} promisc on

tcpdump -s 0 -nn -i ens18