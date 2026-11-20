#!/usr/bin/env bash
#
# gen-prod-ca.sh — PRODUCTION RAUC signing hierarchy for the Ctrlable Pro fleet.
#
# WHY A CA (not a single self-signed cert like gen-dev-keys.sh): the cert baked into the image as
# the RAUC *keyring* is the permanent trust anchor — it verifies EVERY future OTA bundle a fielded
# box ever receives, and it can only be changed by reflashing the box. If we bake the *signing*
# cert directly (as dev does), rotating the signer later would reject updates on every fielded box.
# So we bake the CA cert as the keyring and sign bundles with a short-lived LEAF that chains to it.
# Rotating the signer = issue a new leaf from the same CA; fielded boxes keep trusting it. No reflash.
#
#   ca-cert.pem      -> baked into the image as /etc/rauc/keyring.pem  (RAUC_KEYRING)   PUBLIC, 20y
#   ca-key.pem       -> OFFLINE. Signs leaves only. NEVER in CI, NEVER on a build host.  SECRET
#   signing-cert.pem -> the bundle signer, a chain file (leaf + CA)    (RAUC_CERT)       PUBLIC, ~2y
#   signing-key.pem  -> signs bundles. Lives in CI secrets.            (RAUC_KEY)        SECRET
#
# Usage:  scripts/gen-prod-ca.sh [out-dir]          (default: keys/prod ; gitignored)
# Then :  make bundle RAUC_KEYRING=keys/prod/ca-cert.pem \
#                      RAUC_CERT=keys/prod/signing-cert.pem RAUC_KEY=keys/prod/signing-key.pem
#
set -euo pipefail
dir="${1:-keys/prod}"
CA_DAYS="${CA_DAYS:-7300}"       # ~20y root — long, because replacing it means reflashing the fleet
LEAF_DAYS="${LEAF_DAYS:-825}"    # ~27mo signer — rotate before expiry by re-running the leaf step
ORG="${RAUC_ORG:-Ctrlable}"
command -v openssl >/dev/null || { echo "gen-prod-ca: install openssl"; exit 1; }
mkdir -p "$dir"; chmod 700 "$dir"

if [ -f "$dir/ca-cert.pem" ]; then
  echo "gen-prod-ca: CA already exists in $dir/ (refusing to overwrite the trust anchor)."
  echo "gen-prod-ca: to rotate the SIGNER only, delete signing-*.pem and re-run; the CA stays."
  [ -f "$dir/signing-cert.pem" ] && exit 0
else
  echo "gen-prod-ca: creating ROOT CA (O=$ORG, ${CA_DAYS}d) -> $dir/ca-{key,cert}.pem"
  openssl req -x509 -newkey rsa:4096 -nodes \
    -keyout "$dir/ca-key.pem" -out "$dir/ca-cert.pem" -days "$CA_DAYS" \
    -subj "/O=$ORG/CN=Ctrlable Pro Root CA" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" >/dev/null 2>&1
  chmod 600 "$dir/ca-key.pem"
fi

echo "gen-prod-ca: issuing signing LEAF (codeSigning, ${LEAF_DAYS}d) -> $dir/signing-*.pem"
openssl req -newkey rsa:4096 -nodes \
  -keyout "$dir/signing-key.pem" -out "$dir/signing.csr" \
  -subj "/O=$ORG/CN=Ctrlable Pro OTA Signer" >/dev/null 2>&1
chmod 600 "$dir/signing-key.pem"
# Leaf: NOT a CA. extendedKeyUsage = codeSigning (intent) + emailProtection. The emailProtection is
# REQUIRED, not cosmetic: RAUC verifies bundles with OpenSSL CMS, whose default purpose is S/MIME
# signing — a codeSigning-ONLY leaf is rejected as "unsuitable certificate purpose". Including
# emailProtection (non-critical EKU) makes the leaf pass RAUC's DEFAULT verification everywhere
# (box install + `rauc info`) with no per-host check-purpose config. Signed by the root above.
openssl x509 -req -in "$dir/signing.csr" -days "$LEAF_DAYS" \
  -CA "$dir/ca-cert.pem" -CAkey "$dir/ca-key.pem" -CAcreateserial \
  -extfile <(printf 'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=codeSigning,emailProtection\n') \
  -out "$dir/signing-leaf.pem" >/dev/null 2>&1
# RAUC_CERT must carry the full chain (leaf THEN ca) so verification works even without intermediates on the box.
cat "$dir/signing-leaf.pem" "$dir/ca-cert.pem" > "$dir/signing-cert.pem"
rm -f "$dir/signing.csr"

echo
echo "gen-prod-ca: DONE. Custody matters — these are the fleet's trust roots:"
echo "  BAKE (public):   $dir/ca-cert.pem        -> image keyring (RAUC_KEYRING)"
echo "  CI SECRET:       $dir/signing-key.pem    + $dir/signing-cert.pem  (RAUC_KEY / RAUC_CERT)"
echo "  OFFLINE SECRET:  $dir/ca-key.pem         -> move OFF this machine (password-store / HSM /"
echo "                                             encrypted backup). Only needed to issue new leaves."
echo "  $dir/ is gitignored. Verify a signed bundle later with:  rauc info --keyring=$dir/ca-cert.pem <b>.raucb"