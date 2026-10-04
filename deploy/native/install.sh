#!/usr/bin/env bash
# One-time setup of DataBridge WITHOUT Docker on an Ubuntu/Debian VPS (e.g. Hostinger).
# After this, GitHub Actions (deploy-native.yml) deploys every push to main.
#
#   sudo DOMAIN=databridge.example.com EMAIL=you@example.com bash install.sh
#
# Options (environment variables):
#   DOMAIN          public host name (required)
#   EMAIL           for Let's Encrypt (required with PROXY=nginx)
#   PROXY           nginx (default) | traefik (Dockerised Traefik already on 80/443) | none
#   DEPLOY_USER     user that GitHub Actions SSHes in as and the service runs as (default: the sudo caller)
#   DB              sqlite (default) | postgres (installs PostgreSQL locally)
#   ADMIN_USER      first studio admin account (default: admin); its temporary password is printed once
#   PORT            app port on the host (default: 8000)
set -euo pipefail

DOMAIN="${DOMAIN:?set DOMAIN=your.domain}"
PROXY="${PROXY:-nginx}"
DEPLOY_USER="${DEPLOY_USER:-${SUDO_USER:-$(whoami)}}"
DB="${DB:-sqlite}"
ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASSWORD="$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | head -c 14)-Db7"
PORT="${PORT:-8000}"
BASE=/opt/databridge
SRC="$(cd "$(dirname "$0")" && pwd)"

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
id "$DEPLOY_USER" >/dev/null || { echo "User $DEPLOY_USER does not exist." >&2; exit 1; }
say() { echo; echo "== $*"; }

say "Packages"
apt-get update -qq
PKGS=(curl ca-certificates tar openssl)
[ "$PROXY" = "nginx" ] && PKGS+=(nginx certbot python3-certbot-nginx)
[ "$DB" = "postgres" ] && PKGS+=(postgresql)
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${PKGS[@]}"
# Optional: SQL Server ODBC driver -> https://learn.microsoft.com/sql/connect/odbc/linux-mac/installing-the-microsoft-odbc-driver-for-sql-server

say "uv (manages Python ${PYTHON_VERSION:-3.12} for DataBridge, independent of the OS Python)"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi
uv --version

say "Folders in $BASE"
mkdir -p "$BASE"/{releases,incoming,shared/data,backups,python,.uv-cache,bin}
chown -R "$DEPLOY_USER":"$DEPLOY_USER" "$BASE"
chmod 750 "$BASE"

if [ "$DB" = "postgres" ]; then
  say "PostgreSQL database"
  PGPASS="$(openssl rand -hex 16)"
  sudo -u postgres psql -qtc "SELECT 1 FROM pg_roles WHERE rolname='databridge'" | grep -q 1 || \
    sudo -u postgres psql -qc "CREATE ROLE databridge LOGIN PASSWORD '$PGPASS'"
  sudo -u postgres psql -qtc "SELECT 1 FROM pg_database WHERE datname='databridge'" | grep -q 1 || \
    sudo -u postgres createdb -O databridge databridge
  DB_URL="postgresql+psycopg://databridge:$PGPASS@127.0.0.1:5432/databridge"
else
  DB_URL=""
fi

ENV_FILE="$BASE/shared/.env"
if [ ! -f "$ENV_FILE" ]; then
  say "Settings file $ENV_FILE"
  BIND_HOST=127.0.0.1
  [ "$PROXY" = "traefik" ] && BIND_HOST="$(ip -4 addr show docker0 2>/dev/null | awk '/inet /{sub(/\/.*/,"",$2); print $2}')" && BIND_HOST="${BIND_HOST:-172.17.0.1}"
  FERNET="$(openssl rand -base64 32 | tr '+/' '-_')"
  cat > "$ENV_FILE" <<EOF
# DataBridge settings (native install). Back this file up: the secret key decrypts saved passwords.
BIND_HOST=$BIND_HOST
PORT=$PORT
DATABRIDGE_DATA_DIR=$BASE/shared/data
DATABRIDGE_DATABASE_URL=$DB_URL
DATABRIDGE_SECRET_KEY=$FERNET
DATABRIDGE_PUBLIC_BASE_URL=https://$DOMAIN
DATABRIDGE_MAX_UPLOAD_MB=100
DATABRIDGE_NO_CDN=false
# First admin: created at first start when no accounts exist; must change password at first sign-in.
# Remove DATABRIDGE_ADMIN_PASSWORD after that.
DATABRIDGE_ADMIN_USERNAME=$ADMIN_USER
DATABRIDGE_ADMIN_PASSWORD=$ADMIN_PASSWORD
EOF
  chown "$DEPLOY_USER":"$DEPLOY_USER" "$ENV_FILE"; chmod 600 "$ENV_FILE"
else
  say "$ENV_FILE exists; left unchanged"
  ADMIN_PASSWORD="(not changed: existing .env kept; see the create-admin command below if you have no account)"
fi

say "systemd service"
sed -e "s|__USER__|$DEPLOY_USER|g" -e "s|__BASE__|$BASE|g" "$SRC/databridge.service" > /etc/systemd/system/databridge.service
systemctl daemon-reload
systemctl enable databridge >/dev/null   # starts on the first deploy (needs $BASE/current)

say "Let $DEPLOY_USER restart the service without a password (used by deploy.sh)"
SYSTEMCTL="$(command -v systemctl)"
echo "$DEPLOY_USER ALL=(root) NOPASSWD: $SYSTEMCTL restart databridge, $SYSTEMCTL is-active databridge, $SYSTEMCTL status databridge" > /etc/sudoers.d/databridge
chmod 440 /etc/sudoers.d/databridge && visudo -cf /etc/sudoers.d/databridge >/dev/null

cp "$SRC/deploy.sh" "$SRC/backup.sh" "$BASE/bin/" && chmod +x "$BASE/bin/"*.sh && chown -R "$DEPLOY_USER":"$DEPLOY_USER" "$BASE/bin"

case "$PROXY" in
  nginx)
    if ss -ltn '( sport = :80 or sport = :443 )' | grep -q LISTEN && ! systemctl is-active --quiet nginx; then
      echo "Ports 80/443 are already in use (Traefik/Docker?). Re-run with PROXY=traefik." >&2; exit 1
    fi
    say "Nginx site + HTTPS"
    sed -e "s|__DOMAIN__|$DOMAIN|g" -e "s|__PORT__|$PORT|g" "$SRC/nginx-databridge.conf" > /etc/nginx/sites-available/databridge
    ln -sfn /etc/nginx/sites-available/databridge /etc/nginx/sites-enabled/databridge
    nginx -t && systemctl reload nginx
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "${EMAIL:?set EMAIL=you@example.com for the HTTPS certificate}" --redirect
    ;;
  traefik)
    say "Traefik file-provider config"
    OUT="$BASE/traefik-databridge.yml"
    sed -e "s|__DOMAIN__|$DOMAIN|g" -e "s|__PORT__|$PORT|g" \
        "$SRC/traefik-dynamic.yml" > "$OUT"
    echo "Wrote $OUT. Copy it into Traefik's dynamic config folder and follow the steps at the top of the file."
    ;;
  none) say "No reverse proxy configured (app listens on 127.0.0.1:$PORT)";;
esac

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 'Nginx Full' >/dev/null 2>&1 || true
  [ "$PROXY" = "traefik" ] && ufw allow from 172.16.0.0/12 to any port "$PORT" proto tcp >/dev/null
fi

say "Nightly backup at 02:15"
( crontab -u "$DEPLOY_USER" -l 2>/dev/null | grep -v 'databridge/bin/backup.sh'; \
  echo "15 2 * * * $BASE/bin/backup.sh >> $BASE/backups/backup.log 2>&1" ) | crontab -u "$DEPLOY_USER" -

cat <<EOF

Done.
  First sign-in:  $ADMIN_USER / $ADMIN_PASSWORD   (after the first deploy; you will choose a new password.
                  Existing .env? Then create the admin with:
                  cd $BASE/current && sudo -u $DEPLOY_USER env \$(cat $BASE/shared/.env | xargs) .venv/bin/python -m databridge.manage create-admin --username admin)
  Settings:       $ENV_FILE
  Next:           in GitHub set repo variable DEPLOY_MODE=native and secrets VPS_HOST, VPS_USER=$DEPLOY_USER, VPS_SSH_KEY,
                  then push to main (or Actions -> deploy-native -> Run workflow).
  Logs:           journalctl -u databridge -f
EOF
