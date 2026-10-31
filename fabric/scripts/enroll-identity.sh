#!/usr/bin/env bash
# Register + enroll one Fabric identity with PDS attributes and export it to
# the gateway wallet.
#
#   enroll-identity.sh <org:1|2> <enrollmentId> <role> [location]
#   e.g. enroll-identity.sh 1 mls01op mls MLS-01
#
# Org1 (Org1MSP) = MLS side + admin.  Org2 (Org2MSP) = FPS side + auditor.
# The password is random and discarded after enrolment - the private key in
# the wallet is the credential.  Uses the official fabric-ca-client binary.
set -euo pipefail
ORG="${1:?org 1|2}"; NAME="${2:?enrollment id}"; ROLE="${3:?role}"; LOC="${4:-}"
PROJECT="${PROJECT:-$HOME/pds}"
FH="$HOME/fabric"; TN="$FH/fabric-samples/test-network"
export PATH="$FH/fabric-samples/bin:$PATH"

case "$ORG" in
  1) PORT=7054; CANAME=ca-org1; MSP=Org1MSP; DOM=org1.example.com; CERTDIR="$TN/organizations/fabric-ca/org1";;
  2) PORT=8054; CANAME=ca-org2; MSP=Org2MSP; DOM=org2.example.com; CERTDIR="$TN/organizations/fabric-ca/org2";;
  *) echo "org must be 1 or 2"; exit 1;;
esac
TLS="$CERTDIR/ca-cert.pem"
WALLET="$PROJECT/fabric-gateway/wallet"
WORK="$FH/identities/$NAME"
SECRET="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20)"

mkdir -p "$WORK" "$WALLET/$NAME" "$WALLET/_tls"
ADMINHOME="$FH/identities/_ca-admin-org$ORG"
mkdir -p "$ADMINHOME"

# bootstrap registrar (CA admin) - enrolled once per org
if [ ! -d "$ADMINHOME/msp" ]; then
  FABRIC_CA_CLIENT_HOME="$ADMINHOME" fabric-ca-client enroll -u "https://admin:adminpw@localhost:$PORT" \
    --caname "$CANAME" --tls.certfiles "$TLS" >/dev/null 2>&1
fi

ATTRS="pds.role=$ROLE:ecert"
[ -n "$LOC" ] && ATTRS="$ATTRS,pds.location=$LOC:ecert"

# register; if the identity already exists, just reset its secret (re-runnable)
if ! FABRIC_CA_CLIENT_HOME="$ADMINHOME" fabric-ca-client register --caname "$CANAME" \
  --id.name "$NAME" --id.secret "$SECRET" --id.type client --id.attrs "$ATTRS" \
  --tls.certfiles "$TLS" >/dev/null 2>&1; then
  FABRIC_CA_CLIENT_HOME="$ADMINHOME" fabric-ca-client identity modify "$NAME" --caname "$CANAME" \
    --secret "$SECRET" --attrs "$ATTRS" --tls.certfiles "$TLS" >/dev/null 2>&1 \
    || { echo "cannot register or modify $NAME"; exit 1; }
fi

rm -rf "$WORK/msp"
if ! FABRIC_CA_CLIENT_HOME="$WORK" fabric-ca-client enroll -u "https://$NAME:$SECRET@localhost:$PORT" \
      --caname "$CANAME" -M "$WORK/msp" --tls.certfiles "$TLS" --enrollment.attrs "pds.role,pds.location:opt" >/dev/null 2>&1; then
  echo "Enrollment failed for $NAME (already registered with an unknown password?)."
  echo "Remove it by using a new enrollment id."; exit 1
fi

cp "$WORK/msp/signcerts/"*.pem "$WALLET/$NAME/cert.pem"
cp "$WORK/msp/keystore/"*_sk   "$WALLET/$NAME/key.pem"
printf '{"mspId":"%s","enrollmentId":"%s","role":"%s","location":"%s"}\n' "$MSP" "$NAME" "$ROLE" "$LOC" > "$WALLET/$NAME/identity.json"
# peer TLS CA certs for the gateway connection
cp "$TN/organizations/peerOrganizations/$DOM/tlsca/tlsca.$DOM-cert.pem" "$WALLET/_tls/org$ORG-tlsca.pem"
echo "enrolled $NAME ($MSP) role=$ROLE location=${LOC:-n/a} -> $WALLET/$NAME"
