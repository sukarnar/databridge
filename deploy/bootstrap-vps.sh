#!/usr/bin/env bash
# One-time setup on the Hostinger VPS (run as the deploy user, who must be in the docker group).
# After this, every push to main on GitHub deploys automatically.
#
#   curl -fsSL https://raw.githubusercontent.com/<you>/databridge/main/deploy/bootstrap-vps.sh | bash
#   (or copy the file over and run: bash bootstrap-vps.sh)
set -euo pipefail

DEPLOY_DIR=/opt/databridge
sudo mkdir -p "$DEPLOY_DIR"
sudo chown "$USER":"$USER" "$DEPLOY_DIR"
mkdir -p "$DEPLOY_DIR"/{inbox,backups,deploy}
cd "$DEPLOY_DIR"

echo "== Docker and Traefik"
docker --version
docker compose version
echo "Docker networks (your Traefik network should be listed; put its name in TRAEFIK_NETWORK):"
docker network ls --format '  {{.Name}}' | grep -v -E '^  (bridge|host|none)$' || true

echo "== GHCR login (needed if the package is private)"
echo "Create a GitHub token with read:packages, then:"
echo "   echo <token> | docker login ghcr.io -u <github-user> --password-stdin"

if [ ! -f .env ]; then
  SECRET=$(docker run --rm python:3.12-slim sh -c "pip -q install cryptography >/dev/null 2>&1 && python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'")
  PGPASS=$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32)
  ADMINPASS="$(head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 14)-Db7"
  cat > .env <<EOF
DATABRIDGE_HOST=databridge.example.com
GHCR_USER=sukarnar
IMAGE_TAG=latest
TRAEFIK_NETWORK=traefik
TRAEFIK_ENTRYPOINT=websecure
TRAEFIK_CERTRESOLVER=letsencrypt
DATABRIDGE_ADMIN_USERNAME=admin
DATABRIDGE_ADMIN_PASSWORD=$ADMINPASS
POSTGRES_PASSWORD=$PGPASS
APP_MEMORY_LIMIT=2g
DATABRIDGE_SECRET_KEY=$SECRET
DATABRIDGE_MAX_UPLOAD_MB=100
DATABRIDGE_NO_CDN=false
EOF
  chmod 600 .env
  echo "== Created $DEPLOY_DIR/.env with a generated secret key and Postgres password."
  echo "   Edit DATABRIDGE_HOST and TRAEFIK_*."
  echo "   First studio sign-in: admin / $ADMINPASS  (you will be asked to choose a new password;"
  echo "   then remove DATABRIDGE_ADMIN_PASSWORD from .env)"
else
  echo "== $DEPLOY_DIR/.env already exists; left unchanged."
fi

echo "== Nightly backups at 02:15"
( crontab -l 2>/dev/null | grep -v 'databridge/deploy/backup.sh'; \
  echo "15 2 * * * $DEPLOY_DIR/deploy/backup.sh >> $DEPLOY_DIR/backups/backup.log 2>&1" ) | crontab -

echo
echo "Done. Next: add the GitHub secrets VPS_HOST, VPS_USER, VPS_SSH_KEY and push to main."
