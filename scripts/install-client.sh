#!/usr/bin/env bash
# Idempotent installer, updater, and rollback script for the SmartRSS localhost runner (U-18, 1c §4.3).
# Usage:
#   scripts/install-client.sh                   # Build frontend/dist locally with npm ci && npm run build
#   scripts/install-client.sh --from-dist DIR   # Install from an existing pre-built dist directory
#   scripts/install-client.sh --from-docker     # Extract /usr/share/nginx/html from built smartrss web image
#   scripts/install-client.sh --rollback        # Swap dist.prev back to dist and restart runner
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARE_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/smartrss"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/smartrss-client"
SYSTEMD_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

SOURCE_MODE="local"
FROM_DIST=""
DO_ROLLBACK=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --from-dist)
      SOURCE_MODE="dist"
      FROM_DIST="${2:-}"
      shift 2
      ;;
    --from-docker)
      SOURCE_MODE="docker"
      shift
      ;;
    --rollback)
      DO_ROLLBACK=1
      shift
      ;;
    -h|--help)
      sed -n '2,7p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

if [[ "$DO_ROLLBACK" -eq 1 ]]; then
  if [[ ! -d "$SHARE_DIR/dist.prev" ]]; then
    echo "ERROR: No previous bundle found at $SHARE_DIR/dist.prev" >&2
    exit 1
  fi
  rm -rf "$SHARE_DIR/dist.bad"
  if [[ -d "$SHARE_DIR/dist" ]]; then
    mv "$SHARE_DIR/dist" "$SHARE_DIR/dist.bad"
  fi
  mv "$SHARE_DIR/dist.prev" "$SHARE_DIR/dist"
  rm -rf "$SHARE_DIR/dist.bad"
  systemctl --user restart smartrss-client.service
  echo "Rolled back SmartRSS client runner to previous bundle and restarted smartrss-client.service."
  exit 0
fi

if [[ "$SOURCE_MODE" == "local" ]]; then
  "$REPO_ROOT/scripts/check-prereqs.sh" --mode client
else
  "$REPO_ROOT/scripts/check-prereqs.sh" --mode client --no-node-build
fi

mkdir -p "$SHARE_DIR" "$CONFIG_DIR" "$SYSTEMD_DIR" "$APPS_DIR"
rm -rf "$SHARE_DIR/dist.new"
mkdir -p "$SHARE_DIR/dist.new"

case "$SOURCE_MODE" in
  local)
    npm --prefix "$REPO_ROOT/frontend" ci
    npm --prefix "$REPO_ROOT/frontend" run build
    cp -a "$REPO_ROOT/frontend/dist/." "$SHARE_DIR/dist.new/"
    ;;
  dist)
    if [[ -z "$FROM_DIST" || ! -f "$FROM_DIST/index.html" ]]; then
      echo "ERROR: --from-dist directory must contain index.html (got '$FROM_DIST')" >&2
      exit 1
    fi
    cp -a "$FROM_DIST/." "$SHARE_DIR/dist.new/"
    ;;
  docker)
    "$REPO_ROOT/scripts/check-prereqs.sh" --mode server
    COMPOSE_ARGS=(-f "$REPO_ROOT/docker-compose.yml")
    export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-smartrss-client-build-placeholder}"
    docker compose "${COMPOSE_ARGS[@]}" build web
    docker compose "${COMPOSE_ARGS[@]}" create --no-deps web
    CID="$(docker compose "${COMPOSE_ARGS[@]}" ps -a -q web | head -n1)"
    if [[ -z "$CID" ]]; then
      echo "ERROR: Failed to resolve created web container ID for --from-docker." >&2
      exit 1
    fi
    trap 'docker rm -f "$CID" >/dev/null 2>&1 || true' EXIT
    docker cp "$CID:/usr/share/nginx/html/." "$SHARE_DIR/dist.new/"
    ;;
esac

# Atomic swap preserving dist.prev for rollback
rm -rf "$SHARE_DIR/dist.prev"
if [[ -d "$SHARE_DIR/dist" ]]; then
  mv "$SHARE_DIR/dist" "$SHARE_DIR/dist.prev"
fi
mv "$SHARE_DIR/dist.new" "$SHARE_DIR/dist"

install -m 0755 "$REPO_ROOT/client-runner/smartrss_client_runner.py" "$SHARE_DIR/smartrss_client_runner.py"

if [[ ! -f "$CONFIG_DIR/config.env" ]]; then
  install -m 0644 "$REPO_ROOT/deploy/systemd/smartrss-client.env.example" "$CONFIG_DIR/config.env"
  echo "Created $CONFIG_DIR/config.env (edit SMARTRSS_API_ORIGIN to point to your LAN server)."
else
  echo "Preserved existing $CONFIG_DIR/config.env."
fi

install -m 0644 "$REPO_ROOT/deploy/systemd/smartrss-client.service" "$SYSTEMD_DIR/smartrss-client.service"
install -m 0644 "$REPO_ROOT/deploy/desktop/smartrss.desktop" "$APPS_DIR/smartrss.desktop"

systemctl --user daemon-reload
systemctl --user enable --now smartrss-client.service
systemctl --user restart smartrss-client.service

echo "SmartRSS client installed and running at http://localhost:4180"
