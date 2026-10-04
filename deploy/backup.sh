#!/usr/bin/env bash
# Nightly backup of DataBridge on the VPS: Postgres dump + the data volume (snapshots, datasets, uploads).
# Install:  crontab -e  ->  15 2 * * * /opt/databridge/deploy/backup.sh >> /opt/databridge/backups/backup.log 2>&1
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
KEEP_DAYS="${KEEP_DAYS:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$DIR/backups"
mkdir -p "$OUT"
cd "$DIR"

echo "[$STAMP] dumping Postgres"
docker compose exec -T postgres pg_dump -U databridge -Fc databridge > "$OUT/db-$STAMP.dump"

echo "[$STAMP] archiving data volume"
VOLUME="$(docker volume ls -q | grep -E '(^|_)databridge-data$' | head -1)"
docker run --rm -v "$VOLUME":/data:ro -v "$OUT":/backup alpine \
  tar czf "/backup/data-$STAMP.tar.gz" -C /data .

# Keep a copy of .env: it holds DATABRIDGE_SECRET_KEY, without which saved passwords cannot be decrypted.
cp "$DIR/.env" "$OUT/env-$STAMP.bak" && chmod 600 "$OUT/env-$STAMP.bak"

find "$OUT" -type f \( -name 'db-*.dump' -o -name 'data-*.tar.gz' -o -name 'env-*.bak' \) -mtime +"$KEEP_DAYS" -delete
echo "[$STAMP] done: $(du -sh "$OUT" | cut -f1) in $OUT"
# Copy $OUT off the VPS too (Hostinger snapshots, rclone to cloud storage, or scp to another machine).
