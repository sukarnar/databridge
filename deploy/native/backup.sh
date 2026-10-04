#!/usr/bin/env bash
# Nightly backup for the native install: metadata DB (SQLite or Postgres), data folder and .env.
# Installed by install.sh as a cron job:  15 2 * * * /opt/databridge/bin/backup.sh
set -euo pipefail
BASE="${DATABRIDGE_BASE:-/opt/databridge}"
KEEP_DAYS="${KEEP_DAYS:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$BASE/backups"; mkdir -p "$OUT"
set -a; . "$BASE/shared/.env"; set +a
PY="$BASE/current/.venv/bin/python"

if [ -n "${DATABRIDGE_DATABASE_URL:-}" ]; then
  echo "[$STAMP] pg_dump"
  PGURL="$(echo "$DATABRIDGE_DATABASE_URL" | sed 's|postgresql+psycopg://|postgresql://|')"
  pg_dump -Fc "$PGURL" > "$OUT/db-$STAMP.dump"
else
  echo "[$STAMP] SQLite online backup"
  "$PY" - "$DATABRIDGE_DATA_DIR/databridge.db" "$OUT/db-$STAMP.sqlite" <<'EOF'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
EOF
  gzip "$OUT/db-$STAMP.sqlite"
fi

echo "[$STAMP] data folder"
tar czf "$OUT/data-$STAMP.tar.gz" --exclude='databridge.db*' -C "$DATABRIDGE_DATA_DIR" .
cp "$BASE/shared/.env" "$OUT/env-$STAMP.bak" && chmod 600 "$OUT/env-$STAMP.bak"

find "$OUT" -type f \( -name 'db-*' -o -name 'data-*.tar.gz' -o -name 'env-*.bak' \) -mtime +"$KEEP_DAYS" -delete
echo "[$STAMP] done: $(du -sh "$OUT" | cut -f1) in $OUT. Copy backups off the VPS as well."
