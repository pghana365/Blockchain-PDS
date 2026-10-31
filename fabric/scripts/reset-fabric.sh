#!/usr/bin/env bash
# Wipe the Fabric ledger and rebuild: network + pdschannel + chaincode + demo identities.
# Run in Ubuntu:  bash ~/pds/fabric/scripts/reset-fabric.sh
# WARNING: destroys all on-chain data. Reset the MySQL database too (scripts/reset_db.py),
# otherwise the database and ledger will disagree.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
rm -rf "$HOME/fabric/identities"                 # CA admin/user enrolments belong to the old CAs
rm -rf "$HERE/../../fabric-gateway/wallet"       # old keys are invalid on the new network
bash "$HERE/network-up.sh"
bash "$HERE/deploy-chaincode.sh"
bash "$HERE/enroll-demo-identities.sh"
echo "Fabric reset complete."
