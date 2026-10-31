#!/usr/bin/env bash
# Phase 3 - package, install, approve (Org1+Org2) and commit the PDS chaincode
# using the official test-network `deployCC` command.
# The chaincode source is copied into the Linux filesystem first (faster, and
# avoids Windows file-permission problems); node_modules is not copied.
set -euo pipefail
PROJECT="${PROJECT:-$HOME/pds}"
FABRIC_HOME="$HOME/fabric"
TN="$FABRIC_HOME/fabric-samples/test-network"
CHANNEL="${CHANNEL:-pdschannel}"
CC_NAME="${CC_NAME:-pds}"
CC_VERSION="${CC_VERSION:-1.0}"
CC_SEQ="${CC_SEQ:-1}"
CC_SRC="$FABRIC_HOME/chaincode/pds-contract"

rm -rf "$CC_SRC"; mkdir -p "$CC_SRC"
( cd "$PROJECT/fabric/chaincode/pds-contract" && tar --exclude=node_modules -cf - . ) | ( cd "$CC_SRC" && tar xf - )
ls "$CC_SRC"

cd "$TN"
./network.sh deployCC -c "$CHANNEL" -ccn "$CC_NAME" -ccp "$CC_SRC" -ccl javascript -ccv "$CC_VERSION" -ccs "$CC_SEQ"

echo "---- chaincode '$CC_NAME' v$CC_VERSION committed on $CHANNEL ----"
