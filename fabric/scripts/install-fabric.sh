#!/usr/bin/env bash
# Phase 2a - install Hyperledger Fabric samples, binaries and Docker images
# inside Ubuntu (WSL2) using the OFFICIAL install-fabric.sh script.
# Runtime lives in ~/fabric (Linux filesystem); project source stays on C:.
set -euo pipefail

FABRIC_VERSION="${FABRIC_VERSION:-2.5.16}"
CA_VERSION="${CA_VERSION:-1.5.17}"
FABRIC_HOME="${FABRIC_HOME:-$HOME/fabric}"

mkdir -p "$FABRIC_HOME"
cd "$FABRIC_HOME"

if [ ! -f install-fabric.sh ]; then
  curl -sSLO https://raw.githubusercontent.com/hyperledger/fabric/main/scripts/install-fabric.sh
  chmod +x install-fabric.sh
fi

./install-fabric.sh --fabric-version "$FABRIC_VERSION" --ca-version "$CA_VERSION" docker samples binary

echo "---- installed ----"
"$FABRIC_HOME/fabric-samples/bin/peer" version | head -3
ls "$FABRIC_HOME/fabric-samples/test-network" | head -30
docker images --format '{{.Repository}}:{{.Tag}}' | grep -E 'hyperledger' | sort
