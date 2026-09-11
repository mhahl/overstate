#!/usr/bin/env bash
# Generate the dev-only TLS PKI: a self-signed CA plus server certs for
# salt-api and the Overstate app. Never use outside local dev; production
# must mount real certificates and never commit these files (gitignored).
set -euo pipefail
cd "$(dirname "$0")/.."

OUT="salt-config/tls"
if [ -f "$OUT/ca.crt" ] && [ -f "$OUT/api.crt" ] && [ -f "$OUT/app.crt" ]; then
  echo "dev certs already present in $OUT"
  exit 0
fi
mkdir -p "$OUT"

openssl req -x509 -newkey rsa:2048 -nodes -days 825 -sha256 \
  -subj "/CN=overstate-dev-ca" \
  -keyout "$OUT/ca.key" -out "$OUT/ca.crt" 2>/dev/null

gen() { # name, SANs
  local name="$1" sans="$2"
  openssl req -newkey rsa:2048 -nodes -subj "/CN=$name" \
    -keyout "$OUT/$name.key" -out "$OUT/$name.csr" 2>/dev/null
  printf 'subjectAltName=%s\n' "$sans" > "$OUT/$name.ext"
  openssl x509 -req -days 825 -sha256 -CA "$OUT/ca.crt" -CAkey "$OUT/ca.key" \
    -CAcreateserial -in "$OUT/$name.csr" -out "$OUT/$name.crt" \
    -extfile "$OUT/$name.ext" 2>/dev/null
  rm -f "$OUT/$name.csr" "$OUT/$name.ext" "$OUT/ca.srl"
  # api.key is bind-mounted into the master container where the salt user
  # must read it; dev-only cert, world-readable by design.
  if [ "$name" = "api" ]; then chmod 644 "$OUT/$name.key";
  else chmod 600 "$OUT/$name.key"; fi
}

gen api "DNS:salt-master,DNS:localhost,IP:127.0.0.1"
gen app "DNS:localhost,IP:127.0.0.1"
chmod 600 "$OUT/ca.key"
echo "dev certs written to $OUT"
