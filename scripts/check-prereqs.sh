#!/usr/bin/env bash
# Preflight dependency checker for SmartRSS (Q-4, 1c §3.2).
# Usage: scripts/check-prereqs.sh --mode <dev|server|client|e2e> [--no-node-build]
set -euo pipefail

MODE="dev"
NEED_NODE_FOR_CLIENT=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)
      MODE="${2:-}"
      shift 2
      ;;
    --no-node-build)
      NEED_NODE_FOR_CLIENT=0
      shift
      ;;
    -h|--help)
      echo "Usage: $0 --mode <dev|server|client|e2e> [--no-node-build]"
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

MISSING=0

check_cmd() {
  local cmd="$1"
  local hint="$2"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: Missing required command '$cmd'. Install: $hint" >&2
    MISSING=1
    return 1
  fi
  return 0
}

check_python312() {
  if check_cmd python3 "sudo apt install python3"; then
    local ver
    ver="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    local major="${ver%%.*}"
    local minor="${ver##*.}"
    if (( major < 3 || (major == 3 && minor < 12) )); then
      echo "ERROR: Python >= 3.12 required (found $ver)." >&2
      MISSING=1
    fi
  fi
}

check_node22() {
  if check_cmd node "sudo apt install nodejs (Node >= 22 LTS required)"; then
    local ver major
    ver="$(node -v | sed 's/^v//')"
    major="${ver%%.*}"
    if (( major < 22 )); then
      echo "ERROR: Node.js >= 22 required (found v$ver)." >&2
      MISSING=1
    fi
  fi
  check_cmd npm "sudo apt install npm"
}

check_docker() {
  if check_cmd docker "https://docs.docker.com/engine/install/ubuntu/"; then
    if ! docker compose version >/dev/null 2>&1; then
      echo "ERROR: 'docker compose' plugin is missing. Install: sudo apt install docker-compose-plugin" >&2
      MISSING=1
    fi
  fi
}

case "$MODE" in
  server)
    check_docker
    ;;
  client)
    check_python312
    check_cmd systemctl "systemd user session required for smartrss-client.service"
    if [[ "$NEED_NODE_FOR_CLIENT" -eq 1 ]]; then
      check_node22
    fi
    ;;
  dev)
    check_python312
    check_cmd uv "curl -LsSf https://astral.sh/uv/install.sh | sh"
    check_docker
    if [[ "${FRONTEND_RUNTIME:-host}" == "host" ]]; then
      check_node22
    fi
    ;;
  e2e)
    check_python312
    check_cmd uv "curl -LsSf https://astral.sh/uv/install.sh | sh"
    check_docker
    check_node22
    if [[ ! -d "${HOME}/.cache/ms-playwright" ]] && [[ -z "${PLAYWRIGHT_BROWSERS_PATH:-}" ]]; then
      echo "NOTE: Playwright Chromium cache not found under ~/.cache/ms-playwright. Run: npm --prefix frontend exec playwright install chromium" >&2
    fi
    ;;
  *)
    echo "ERROR: Unknown --mode '$MODE' (expected dev|server|client|e2e)" >&2
    exit 2
    ;;
esac

if [[ "$MISSING" -ne 0 ]]; then
  echo "Preflight check failed for mode '$MODE'. Install the missing prerequisites above and retry." >&2
  exit 1
fi
