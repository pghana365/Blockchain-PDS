#!/usr/bin/env bash
# Retry helper: Fabric CA binaries + Docker images (run after install-fabric.sh
# if a download was interrupted).
set -uo pipefail
FABRIC_VERSION="${FABRIC_VERSION:-2.5.16}"
CA_VERSION="${CA_VERSION:-1.5.17}"
cd "$HOME/fabric"

echo "== samples tags (latest 8) =="
git -C fabric-samples tag --sort=-v:refname | head -8
echo "== samples HEAD =="; git -C fabric-samples log -1 --format='%h %ad %s' --date=short

for attempt in 1 2 3; do
  echo "== attempt $attempt: binary + docker =="
  ./install-fabric.sh --fabric-version "$FABRIC_VERSION" --ca-version "$CA_VERSION" binary docker && break
  sleep 5
done

echo "== bin =="; ls fabric-samples/bin
echo "== images =="; docker images --format '{{.Repository}}:{{.Tag}}' | grep -i hyperledger | sort
