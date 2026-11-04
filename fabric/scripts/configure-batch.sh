#!/usr/bin/env bash
# Sets the orderer block-cutting rules in the test-network's configtx.yaml.
#   BATCH_COUNT   max transactions per block   (default 50 - project requirement)
#   BATCH_TIMEOUT cut a partial block after this long (default 5s)
# A block is cut when BATCH_COUNT transactions are waiting OR BATCH_TIMEOUT expires.
# Must run BEFORE the channel is created (network-up.sh does this).
set -euo pipefail
CFG="$HOME/fabric/fabric-samples/test-network/configtx/configtx.yaml"
COUNT="${BATCH_COUNT:-50}"
TIMEOUT="${BATCH_TIMEOUT:-5s}"
sed -i -E "s/^(\s*MaxMessageCount:\s*)[0-9]+/\1${COUNT}/; s/^(\s*BatchTimeout:\s*)[0-9a-z]+/\1${TIMEOUT}/" "$CFG"
grep -nE "BatchTimeout|MaxMessageCount|AbsoluteMaxBytes|PreferredMaxBytes" "$CFG"
