#!/usr/bin/env bash
# Reports the state of the Fabric installation in ~/fabric
F="$HOME/fabric/fabric-samples"
echo "== log tail =="; tail -n 5 "$HOME/fabric-install.log" 2>/dev/null
echo "== peer version =="; "$F/bin/peer" version 2>&1 | head -4
echo "== binaries =="; ls "$F/bin"
echo "== images =="; docker images --format '{{.Repository}}:{{.Tag}}' | grep -i hyperledger | sort
