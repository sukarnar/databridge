#!/usr/bin/env bash
# Native (no Docker) release switcher, run ON the VPS by GitHub Actions or by hand.
#
#   deploy.sh <release-id> <tarball>   unpack a new release, install deps, switch, restart, health check
#   deploy.sh <release-id>             switch to an already-installed release (rollback)
#
# Layout:  $BASE/releases/<id>/   code + .venv of each release
#          $BASE/current           symlink to the live release
#          $BASE/shared/.env       settings (DATABRIDGE_*), shared/data = snapshots, datasets, SQLite DB
# If the new release fails its health check, the previous release is restored automatically.
set -euo pipefail

BASE="${DATABRIDGE_BASE:-/opt/databridge}"
RELEASE="${1:?usage: deploy.sh <release-id> [tarball]}"
TARBALL="${2:-}"
KEEP="${KEEP_RELEASES:-5}"
RESTART_CMD="${RESTART_CMD:-sudo systemctl restart databridge}"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
export UV_PYTHON_INSTALL_DIR="$BASE/python" UV_CACHE_DIR="$BASE/.uv-cache"

set -a; [ -f "$BASE/shared/.env" ] && . "$BASE/shared/.env"; set +a
PORT="${PORT:-8000}"
HEALTH_HOST="${BIND_HOST:-127.0.0.1}"; [ "$HEALTH_HOST" = "0.0.0.0" ] && HEALTH_HOST=127.0.0.1
# Built-in TLS: the local health check talks https to the loopback address (certificate name won't match).
HEALTH_SCHEME=http; HEALTH_TLS=""
[ -n "${DATABRIDGE_TLS_CERT_FILE:-}" ] && HEALTH_SCHEME=https && HEALTH_TLS="--insecure"

log() { echo "[deploy $(date +%H:%M:%S)] $*"; }
DIR="$BASE/releases/$RELEASE"

if [ -n "$TARBALL" ]; then
  log "unpacking $TARBALL -> $DIR"
  rm -rf "$DIR.tmp" && mkdir -p "$DIR.tmp"
  tar xzf "$TARBALL" -C "$DIR.tmp"
  rm -rf "$DIR" && mv "$DIR.tmp" "$DIR"
  log "installing Python $PYTHON_VERSION and dependencies (uv)"
  uv venv --quiet --python "$PYTHON_VERSION" "$DIR/.venv"
  REQS=(-r "$DIR/requirements.txt")
  if [ -f "$BASE/shared/requirements-drivers.txt" ]; then REQS+=(-r "$BASE/shared/requirements-drivers.txt")
  else REQS+=(-r "$DIR/requirements-drivers.txt"); fi
  uv pip install --quiet --python "$DIR/.venv/bin/python" "${REQS[@]}"
  "$DIR/.venv/bin/python" -m compileall -q "$DIR/databridge" >/dev/null
  rm -f "$TARBALL"
elif [ ! -x "$DIR/.venv/bin/python" ]; then
  echo "Release $RELEASE is not installed in $BASE/releases; pass a tarball." >&2; exit 1
fi

PREV="$(readlink "$BASE/current" 2>/dev/null || true)"
log "switching current -> releases/$RELEASE (was: ${PREV:-none})"
ln -sfn "$DIR" "$BASE/current.tmp" && mv -Tf "$BASE/current.tmp" "$BASE/current"
eval "$RESTART_CMD"

healthy() {
  for _ in $(seq 1 45); do
    # Must be healthy AND served by the release we just switched to (not a stale process).
    if curl -fsS $HEALTH_TLS "$HEALTH_SCHEME://$HEALTH_HOST:$PORT/health" 2>/dev/null | grep -q "\"release\":\"$(basename "$(readlink "$BASE/current")")\""; then
      return 0
    fi
    sleep 2
  done
  return 1
}

if healthy; then
  log "healthy: $RELEASE"
else
  log "health check FAILED for $RELEASE"
  if [ -n "$PREV" ] && [ "$PREV" != "$DIR" ]; then
    log "rolling back to $PREV"
    ln -sfn "$PREV" "$BASE/current.tmp" && mv -Tf "$BASE/current.tmp" "$BASE/current"
    eval "$RESTART_CMD"
    healthy && log "rollback healthy" || log "rollback ALSO unhealthy - check: journalctl -u databridge -n 100"
  fi
  exit 1
fi

# Keep the newest $KEEP releases (never the live or previous one).
cd "$BASE/releases"
ls -1t | grep -v '\.tmp$' | tail -n +"$((KEEP + 1))" | while read -r old; do
  [ "$BASE/releases/$old" = "$DIR" ] || [ "$BASE/releases/$old" = "$PREV" ] || { log "pruning $old"; rm -rf "$old"; }
done
log "done. Installed releases: $(ls -1t | tr '\n' ' ')"
