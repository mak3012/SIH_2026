#!/bin/sh
# Host firewall for the one compose network with a route out (`edge`, EDGE_SUBNET): drop every
# new outbound connection from it except to the destinations given. Linux hosts (Docker Engine
# with iptables); run as root after `docker compose up`, and from the host's boot scripts.
#
#   infra/firewall/no-egress.sh                              # ingress only: no egress at all
#   infra/firewall/no-egress.sh 10.20.0.15:443 10.1.2.3:443   # rig box: WITSML store, HQ
#
# Replies to inbound connections (published ports) are ESTABLISHED and stay allowed. The other
# networks are `internal` in compose and have no route out to begin with.
set -eu
SUBNET="${EDGE_SUBNET:-172.30.0.0/24}"
CHAIN=WELLS-EGRESS

iptables -N "$CHAIN" 2>/dev/null || iptables -F "$CHAIN"
iptables -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
iptables -A "$CHAIN" -d "$SUBNET" -j RETURN   # inside the network
for dest in "$@"; do
  iptables -A "$CHAIN" -d "${dest%:*}" -p tcp --dport "${dest##*:}" -j RETURN
done
iptables -A "$CHAIN" -j LOG --log-prefix "wells-egress-drop " --log-level 4
iptables -A "$CHAIN" -j DROP

# Jump from DOCKER-USER for traffic leaving the subnet (once).
iptables -C DOCKER-USER -s "$SUBNET" -j "$CHAIN" 2>/dev/null \
  || iptables -I DOCKER-USER -s "$SUBNET" -j "$CHAIN"
echo "egress from $SUBNET limited to: ${*:-nothing}"
