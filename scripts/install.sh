#!/usr/bin/env bash
# Install Overstate on openSUSE Tumbleweed as root Podman Quadlets.
# Run from a repo checkout: `sudo ./scripts/install.sh`.
# Idempotent: re-running keeps existing secrets, certs, and data.
set -euo pipefail

ETC=/etc/overstate
VAR=/var/lib/overstate
UNITS=/etc/containers/systemd
REPO="$(cd "$(dirname "$0")/.." && pwd)"

YES=0
FORCE=0
ADMIN_PASSWORD=""
HOSTNAME_OVERRIDE=""

usage() {
  cat <<EOF
Usage: sudo ./scripts/install.sh [--yes] [--force] [--admin-password PW] [--hostname NAME]

  --yes              skip the confirmation prompt
  --force            allow non-Tumbleweed systems (openSUSE Leap, etc.)
  --admin-password   set the initial admin password (else printed to the
                     app container log on first boot; change it after login)
  --hostname         hostname baked into the self-signed app cert
                     (default: this machine's hostname)
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --yes) YES=1 ;;
    --force) FORCE=1 ;;
    --admin-password) ADMIN_PASSWORD="${2:?missing value}"; shift ;;
    --admin-password=*) ADMIN_PASSWORD="${1#*=}" ;;
    --hostname) HOSTNAME_OVERRIDE="${2:?missing value}"; shift ;;
    --hostname=*) HOSTNAME_OVERRIDE="${1#*=}" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "error: unknown flag $1" >&2; usage >&2; exit 1 ;;
  esac
  shift
done

if [ "$(id -u)" -ne 0 ]; then
  echo "error: run as root (system Quadlets and /etc writes need it)" >&2
  exit 1
fi
if [ ! -f "$REPO/Containerfile" ] || [ ! -d "$REPO/deploy/quadlet" ]; then
  echo "error: run from an Overstate repo checkout ($REPO is not one)" >&2
  exit 1
fi
# shellcheck disable=SC1091
source /etc/os-release
if [ "${ID:-}" != "opensuse-tumbleweed" ] && { [ "${ID_LIKE:-}" != *"suse"* ] || [ "$FORCE" -ne 1 ]; }; then
  echo "error: ${PRETTY_NAME:-this system} is not Tumbleweed; pass --force to proceed anyway" >&2
  exit 1
fi
if [ "$YES" -ne 1 ]; then
  cat <<EOF
Overstate install plan (Tumbleweed, Podman Quadlets):
  units:  $UNITS/overstate-{app,worker,salt-master,postgres,redis,caddy}.container
  config: $ETC (env, salt-config, tls, Caddyfile)
  data:   $VAR (salt roots), podman volumes overstate-pgdata/overstate-saltdata
  web:    https://overstate.sigaint.au via Caddy (needs DNS + ports 80/443)
EOF
  read -r -p "Continue? [y/N] " answer
  [ "$answer" = "y" ] || [ "$answer" = "Y" ] || exit 0
fi

if ! command -v podman >/dev/null; then
  zypper -n install podman openssl
fi

echo "==> building images"
podman build -t localhost/overstate:latest "$REPO"
podman build -f "$REPO/Containerfile.salt-master" -t localhost/overstate-salt-master:latest "$REPO"

echo "==> laying down $ETC and $VAR"
mkdir -p "$ETC" "$VAR/srv" "$UNITS"
if [ ! -d "$ETC/salt-config" ]; then
  cp -r "$REPO/salt-config" "$ETC/salt-config"
  chmod 755 "$ETC/salt-config"
fi
if [ ! -f "$VAR/srv/top.sls" ] && [ -f "$REPO/salt-srv/salt/top.sls" ]; then
  cp -r "$REPO/salt-srv/salt/." "$VAR/srv/"
fi
if [ ! -f "$ETC/Caddyfile" ]; then
  cp "$REPO/deploy/Caddyfile" "$ETC/Caddyfile"
  chmod 644 "$ETC/Caddyfile"
fi

echo "==> TLS (self-signed; replace with real certs for production)"
HOSTNAME="${HOSTNAME_OVERRIDE:-$(hostname -f 2>/dev/null || hostname)}"
mkdir -p "$ETC/tls"
if [ ! -f "$ETC/tls/ca.crt" ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 825 -sha256 \
    -subj "/CN=overstate-ca" \
    -keyout "$ETC/tls/ca.key" -out "$ETC/tls/ca.crt" 2>/dev/null
  chmod 600 "$ETC/tls/ca.key"
fi
gen_cert() { # name, SANs
  local name="$1" sans="$2"
  [ -f "$ETC/tls/$name.crt" ] && return 0
  openssl req -newkey rsa:2048 -nodes -subj "/CN=$name" \
    -keyout "$ETC/tls/$name.key" -out "$ETC/tls/$name.csr" 2>/dev/null
  printf 'subjectAltName=%s\n' "$sans" > "$ETC/tls/$name.ext"
  openssl x509 -req -days 825 -sha256 -CA "$ETC/tls/ca.crt" \
    -CAkey "$ETC/tls/ca.key" -CAcreateserial \
    -in "$ETC/tls/$name.csr" -out "$ETC/tls/$name.crt" \
    -extfile "$ETC/tls/$name.ext" 2>/dev/null
  rm -f "$ETC/tls/$name.csr" "$ETC/tls/$name.ext" "$ETC/tls/ca.srl"
  chmod 600 "$ETC/tls/$name.key"
  chmod 644 "$ETC/tls/$name.crt"
}
gen_cert api "DNS:salt-master,DNS:localhost,IP:127.0.0.1"
gen_cert app "DNS:$HOSTNAME,DNS:localhost,IP:127.0.0.1"

echo "==> secrets in $ETC/overstate.env"
rand() { openssl rand -hex 24; }
ENV_FILE="$ETC/overstate.env"
if [ ! -f "$ENV_FILE" ]; then
  PG_PASS="$(rand)"
  EAUTH_PASS="$(rand)"
  cat > "$ENV_FILE" <<EOF
# Overstate service environment (root-only, managed by install.sh).
# DATABASE_URL and POSTGRES_PASSWORD must stay in sync.
DATABASE_URL=postgresql+psycopg://overstate:${PG_PASS}@postgres:5432/overstate
POSTGRES_PASSWORD=${PG_PASS}
REDIS_URL=redis://redis:6379/0
SALT_API_URL=https://salt-master:8000
SALT_API_VERIFY_CA=/srv/tls/ca.crt
SALT_EAUTH_USER=overstate
SALT_EAUTH_PASSWORD=${EAUTH_PASS}
SALT_API_USER_PASS=${EAUTH_PASS}
SECRET_KEY=$(rand)
FILE_ROOTS=/srv/states/salt
TLS_CERT=/srv/tls/app.crt
TLS_KEY=/srv/tls/app.key
APP_DOMAIN=overstate.sigaint.au
API_DOMAIN=overstate-api.sigaint.au
EOF
  chmod 600 "$ENV_FILE"
fi
for var in "APP_DOMAIN=overstate.sigaint.au" \
    "API_DOMAIN=overstate-api.sigaint.au"; do
  grep -q "^${var%%=*}=" "$ENV_FILE" || printf '%s\n' "$var" >> "$ENV_FILE"
done
if [ -n "$ADMIN_PASSWORD" ] && ! grep -q "^ADMIN_PASSWORD=" "$ENV_FILE"; then
  printf 'ADMIN_PASSWORD=%s\n' "$ADMIN_PASSWORD" >> "$ENV_FILE"
  chmod 600 "$ENV_FILE"
fi

echo "==> installing Quadlet units"
cp "$REPO"/deploy/quadlet/overstate-*.container "$REPO"/deploy/quadlet/overstate.network "$UNITS/"
systemctl daemon-reload
systemctl enable --now overstate-postgres.service overstate-redis.service \
  overstate-salt-master.service

echo "==> waiting for salt-api"
API_UP=""
for _ in $(seq 1 30); do
  # 401 from /login proves the API is up (no token yet).
  if [ "$(curl -sk -o /dev/null -w '%{http_code}' \
    https://127.0.0.1:8001/login)" = "401" ]; then
    API_UP=1
    break
  fi
  sleep 2
done
[ -n "$API_UP" ] || echo "WARNING: salt-api is not answering; check 'journalctl -u overstate-salt-master'" >&2

systemctl enable --now overstate-worker.service overstate-app.service \
  overstate-caddy.service

# shellcheck disable=SC1091
source "$ENV_FILE"

cat <<EOF

Overstate is starting:
  web:      https://$APP_DOMAIN (Caddy, automatic ACME TLS)
  salt-api: https://$API_DOMAIN (Caddy) and 127.0.0.1:8001 (localhost only)
  data:     $VAR, $ETC, volumes overstate-pgdata/overstate-saltdata/caddy-data
  needs:    DNS A records for both names at this host; ports 80+443 reachable
            or Caddy cannot issue certificates
EOF
if grep -q "^ADMIN_PASSWORD=" "$ENV_FILE"; then
  echo "  admin login: user 'admin' with your --admin-password (remove that"
  echo "  line from $ENV_FILE after first login, then change the password)."
else
  echo "  admin login: user 'admin'; the generated password is in the app log:"
  echo "    podman logs overstate-app | grep 'seeded admin'"
fi
cat <<EOF
  next: accept minion keys on the Keys page, point minions at this master.
  docs: docs/install-opensuse.md
EOF
