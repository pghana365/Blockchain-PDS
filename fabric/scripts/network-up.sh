#!/usr/bin/env bash
# Phase 2b - start the official Fabric test network with CAs and create the
# PDS channel. Org1 = MLS side, Org2 = FPS side (test-network defaults).
set -euo pipefail
TN="$HOME/fabric/fabric-samples/test-network"
CHANNEL="${CHANNEL:-pdschannel}"

cd "$TN"
./network.sh down || true
./network.sh up createChannel -c "$CHANNEL" -ca

echo "---- running containers ----"
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
