#!/usr/bin/env bash
# Enrolls the demo identities used by the Fabric Gateway service.
# Org1 (MLS/admin side): pdsadmin, mls01op, mls02op
# Org2 (FPS side)      : fps01op, fps02op, auditor1
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E="$HERE/enroll-identity.sh"
bash "$E" 1 pdsadmin admin
bash "$E" 1 mls01op  mls MLS-01
bash "$E" 1 mls02op  mls MLS-02
bash "$E" 2 fps01op  fps FPS-01
bash "$E" 2 fps02op  fps FPS-02
bash "$E" 2 auditor1 auditor
ls "$HOME/pds/fabric-gateway/wallet"
