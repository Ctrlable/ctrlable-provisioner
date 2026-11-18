#!/usr/bin/env bash
#
# gen-dev-keys.sh — self-signed DEV keys for signing/verifying RAUC bundles locally.
# DEV ONLY. Production signing uses keys held in CI secrets (pass RAUC_CERT/RAUC_KEY), never these.
#
set -euo pipefail
dir="${1:-keys}"
mkdir -p "$dir"
if [ -f "$dir/dev-key.pem" ] && [ -f "$dir/dev-cert.pem" ]; then
  echo "gen-dev-keys: keys already exist in $dir/"
  exit 0
fi
command -v openssl >/dev/null || { echo "gen-dev-keys: install openssl"; exit 1; }
openssl req -x509 -newkey rsa:4096 -nodes \
  -keyout "$dir/dev-key.pem" -out "$dir/dev-cert.pem" \
  -subj "/O=Ctrlable/CN=ctrlable-pro-dev" -days 3650 >/dev/null 2>&1
chmod 600 "$dir/dev-key.pem"
echo "gen-dev-keys: wrote $dir/dev-cert.pem (keyring, baked into image) + $dir/dev-key.pem (signing)."
echo "gen-dev-keys: DEV ONLY — do not ship these; production keys live in CI secrets."
