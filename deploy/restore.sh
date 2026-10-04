#!/usr/bin/env bash
# Restore from a backup made by backup.sh.
#   ./deploy/restore.sh backups/db-20260927-021500.dump backups/data-20260927-021500.tar.gz
set -euo pipefail
DB_DUMP="$1"; DATA_TGZ="$2"
DIR="$(cd "$(dirname "$0")/.." && pwd)"; cd "$DIR"

docker compose stop app
docker compose exec -T postgres pg_restore -U databridge -d databridge --clean --if-exists < "$DB_DUMP"
VOLUME="$(docker volume ls -q | grep -E '(^|_)databridge-data$' | head -1)"
docker run --rm -v "$VOLUME":/data -v "$(cd "$(dirname "$DATA_TGZ")" && pwd)":/backup alpine \
  sh -c "rm -rf /data/* && tar xzf /backup/$(basename "$DATA_TGZ") -C /data"
docker compose start app
echo "Restored. Make sure .env has the same DATABRIDGE_SECRET_KEY as when the backup was taken."
