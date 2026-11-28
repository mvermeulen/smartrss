#!/usr/bin/env bash
# SmartRSS guided, idempotent first-time server setup (NFR-OPS-02, E-09, 1d-ops §4).
# Safe to re-run: never overwrites an existing .env and never runs sudo automatically.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RENDER_HELPER="$REPO_ROOT/scripts/_render_env.py"

NON_INTERACTIVE=0
WORKER_MODE=""
LAN_PORT="8080"
SSRF_BLOCKED_CIDRS=""
OPML_FILE=""
DEVICE_NAME="Primary Laptop"
DEVICE_PLATFORM="ubuntu"
INSTALL_WORKER_TIMER=""
SKIP_PAIR=0
ENV_FILE="$REPO_ROOT/.env"
LAN_HOSTS=()

usage() {
  cat <<'EOF'
Usage: scripts/setup-server.sh [OPTIONS]

Guided, idempotent SmartRSS server setup (safe to re-run; never overwrites .env or runs sudo).

Options:
  --non-interactive              Use defaults/flags without prompting
  --worker-mode <daemon|cron>    Worker execution mode (default: daemon)
  --lan-host <host-or-ip>        LAN hostname or IPv4 (repeatable or comma-separated)
  --lan-port <1..65535>          LAN web entry port (default: 8080)
  --ssrf-blocked-cidrs <cidrs>   Optional comma-separated extra blocked CIDRs (e.g. WAN /32)
  --opml <path>                  Optional host OPML file to import after migration
  --device-name <name>           Device name for initial pairing code (default: "Primary Laptop")
  --device-platform <p>          ubuntu | android | web (default: ubuntu)
  --install-worker-timer         Install/enable systemd user timer in cron mode
  --no-worker-timer              Skip systemd user timer installation in cron mode
  --skip-pair                    Skip generating a pairing code
  --env-file <path>              Target .env path (default: <repo>/.env)
  -h, --help                     Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --non-interactive)
      NON_INTERACTIVE=1
      shift
      ;;
    --worker-mode)
      [[ $# -ge 2 ]] || { echo "ERROR: --worker-mode requires daemon or cron" >&2; exit 2; }
      WORKER_MODE="$2"
      shift 2
      ;;
    --lan-host)
      [[ $# -ge 2 ]] || { echo "ERROR: --lan-host requires a hostname or IPv4" >&2; exit 2; }
      LAN_HOSTS+=("$2")
      shift 2
      ;;
    --lan-port)
      [[ $# -ge 2 ]] || { echo "ERROR: --lan-port requires a port number" >&2; exit 2; }
      LAN_PORT="$2"
      shift 2
      ;;
    --ssrf-blocked-cidrs)
      [[ $# -ge 2 ]] || { echo "ERROR: --ssrf-blocked-cidrs requires a value" >&2; exit 2; }
      SSRF_BLOCKED_CIDRS="$2"
      shift 2
      ;;
    --opml)
      [[ $# -ge 2 ]] || { echo "ERROR: --opml requires a file path" >&2; exit 2; }
      OPML_FILE="$2"
      shift 2
      ;;
    --device-name)
      [[ $# -ge 2 ]] || { echo "ERROR: --device-name requires a value" >&2; exit 2; }
      DEVICE_NAME="$2"
      shift 2
      ;;
    --device-platform)
      [[ $# -ge 2 ]] || { echo "ERROR: --device-platform requires ubuntu|android|web" >&2; exit 2; }
      DEVICE_PLATFORM="$2"
      shift 2
      ;;
    --install-worker-timer)
      INSTALL_WORKER_TIMER="yes"
      shift
      ;;
    --no-worker-timer)
      INSTALL_WORKER_TIMER="no"
      shift
      ;;
    --skip-pair)
      SKIP_PAIR=1
      shift
      ;;
    --env-file)
      [[ $# -ge 2 ]] || { echo "ERROR: --env-file requires a path" >&2; exit 2; }
      ENV_FILE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

# 1. Fail-fast argument validation (before touching disk or Docker)
if [[ -n "$WORKER_MODE" && "$WORKER_MODE" != "daemon" && "$WORKER_MODE" != "cron" ]]; then
  echo "ERROR: --worker-mode must be 'daemon' or 'cron' (got '$WORKER_MODE')" >&2
  exit 2
fi
if ! [[ "$LAN_PORT" =~ ^[0-9]+$ ]] || (( LAN_PORT < 1 || LAN_PORT > 65535 )); then
  echo "ERROR: --lan-port must be an integer between 1 and 65535 (got '$LAN_PORT')" >&2
  exit 2
fi
if [[ "$DEVICE_PLATFORM" != "ubuntu" && "$DEVICE_PLATFORM" != "android" && "$DEVICE_PLATFORM" != "web" ]]; then
  echo "ERROR: --device-platform must be ubuntu, android, or web (got '$DEVICE_PLATFORM')" >&2
  exit 2
fi
if [[ -n "$OPML_FILE" ]]; then
  if [[ ! -f "$OPML_FILE" || ! -r "$OPML_FILE" ]]; then
    echo "ERROR: --opml file is missing or unreadable: $OPML_FILE" >&2
    exit 2
  fi
  opml_bytes="$(wc -c < "$OPML_FILE")"
  if (( opml_bytes > 2097152 )); then
    echo "ERROR: --opml file exceeds 2 MiB limit ($opml_bytes bytes): $OPML_FILE" >&2
    exit 2
  fi
fi

# 2. Prerequisite checks (never runs sudo)
"$REPO_ROOT/scripts/check-prereqs.sh" --mode server
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: Missing required command 'python3'. Install: sudo apt install python3" >&2
  exit 1
fi

detect_primary_ipv4() {
  local ip_out=""
  if command -v ip >/dev/null 2>&1; then
    ip_out="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") {print $(i+1); exit}}' || true)"
  fi
  if [[ -z "$ip_out" ]] && command -v hostname >/dev/null 2>&1; then
    ip_out="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  fi
  if [[ "$ip_out" == 127.* ]]; then
    ip_out=""
  fi
  echo "$ip_out"
}

detect_mdns_host() {
  local short_host=""
  if command -v hostname >/dev/null 2>&1; then
    short_host="$(hostname -s 2>/dev/null || true)"
  fi
  if [[ -n "$short_host" && "$short_host" != "localhost" ]] && command -v getent >/dev/null 2>&1; then
    if getent hosts "${short_host}.local" >/dev/null 2>&1; then
      echo "${short_host}.local"
      return 0
    fi
  fi
  echo ""
}

# 3. Atomically non-destructive .env creation / inspection
PRIMARY_LAN_HOST="localhost"
EFFECTIVE_PORT="$LAN_PORT"
EFFECTIVE_WORKER_MODE="${WORKER_MODE:-daemon}"

inspect_existing_env() {
  local meta_json
  meta_json="$(python3 "$RENDER_HELPER" inspect --env-file "$ENV_FILE")"
  PRIMARY_LAN_HOST="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["primary_lan_host"])' "$meta_json")"
  EFFECTIVE_PORT="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["lan_port"])' "$meta_json")"
  EFFECTIVE_WORKER_MODE="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["worker_mode"])' "$meta_json")"
  local insecure placeholder mode_octal
  insecure="$(python3 -c 'import json,sys; print("1" if json.loads(sys.argv[1])["insecure_permissions"] else "0")' "$meta_json")"
  placeholder="$(python3 -c 'import json,sys; print("1" if json.loads(sys.argv[1])["placeholder_password"] else "0")' "$meta_json")"
  mode_octal="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["mode_octal"])' "$meta_json")"
  echo "[setup] Preserved existing ${ENV_FILE} (edit it manually to reconfigure)."
  if [[ "$insecure" == "1" ]]; then
    echo "[setup] WARNING: ${ENV_FILE} has permissions ${mode_octal}; restrict it with: chmod 600 ${ENV_FILE}" >&2
  fi
  if [[ "$placeholder" == "1" ]]; then
    echo "[setup] WARNING: ${ENV_FILE} still uses the default placeholder POSTGRES_PASSWORD!" >&2
  fi
}

if [[ -f "$ENV_FILE" ]]; then
  inspect_existing_env
else
  DETECTED_IPV4="$(detect_primary_ipv4)"
  MDNS_HOST="$(detect_mdns_host)"

  if [[ "$NON_INTERACTIVE" -eq 0 && -t 0 ]]; then
    if [[ "${#LAN_HOSTS[@]}" -eq 0 ]]; then
      default_host="${DETECTED_IPV4:-localhost}"
      read -r -p "LAN host/IP for clients [${default_host}]: " user_host
      if [[ -n "${user_host// }" ]]; then
        LAN_HOSTS+=("$user_host")
      fi
    fi
    if [[ -z "$WORKER_MODE" ]]; then
      read -r -p "Worker mode (daemon/cron) [daemon]: " user_mode
      WORKER_MODE="${user_mode:-daemon}"
    fi
  fi

  EFFECTIVE_WORKER_MODE="${WORKER_MODE:-daemon}"
  gen_args=(
    "$RENDER_HELPER" generate
    --template "$REPO_ROOT/.env.example"
    --output "$ENV_FILE"
    --lan-port "$LAN_PORT"
    --worker-mode "$EFFECTIVE_WORKER_MODE"
    --detected-ipv4 "$DETECTED_IPV4"
    --mdns-host "$MDNS_HOST"
    --ssrf-blocked-cidrs "$SSRF_BLOCKED_CIDRS"
  )
  for h in "${LAN_HOSTS[@]}"; do
    gen_args+=(--lan-host "$h")
  done

  gen_rc=0
  gen_out="$(python3 "${gen_args[@]}")" || gen_rc=$?
  if [[ "$gen_rc" -eq 10 ]]; then
    # Concurrent creator won atomic os.link() (O-09)
    inspect_existing_env
  elif [[ "$gen_rc" -ne 0 ]]; then
    exit "$gen_rc"
  else
    PRIMARY_LAN_HOST="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["primary_lan_host"])' "$gen_out")"
    EFFECTIVE_PORT="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["lan_port"])' "$gen_out")"
    EFFECTIVE_WORKER_MODE="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["worker_mode"])' "$gen_out")"
    echo "[setup] Created mode-0600 ${ENV_FILE} (worker_mode=${EFFECTIVE_WORKER_MODE}, primary_lan_host=${PRIMARY_LAN_HOST})."
  fi
fi

# 4. Build shared backend + web images, start db+api, migrate, then start remaining services (O-01, O-10)
cd "$REPO_ROOT"
echo "[setup] Building backend (shared by api & worker) and web images..."
docker compose build api web

echo "[setup] Starting db and api..."
docker compose up -d --wait db api

echo "[setup] Running database migrations..."
docker compose exec -T api smartrss db migrate

echo "[setup] Starting remaining services..."
docker compose up -d --wait

# Verify end-to-end health through web -> api -> db (subset check tolerating version field, O-10)
HEALTH_URL="${SETUP_HEALTH_URL:-http://127.0.0.1:${EFFECTIVE_PORT}/api/v1/health}"
HEALTH_TIMEOUT="${SETUP_HEALTH_TIMEOUT_SECONDS:-30}"
echo "[setup] Verifying health endpoint at ${HEALTH_URL}..."
if ! python3 - "$HEALTH_URL" "$HEALTH_TIMEOUT" <<'PY'
import json, sys, time, urllib.request

url = sys.argv[1]
deadline = time.monotonic() + float(sys.argv[2])
last_err = "no response"
while time.monotonic() <= deadline:
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            if resp.status == 200:
                payload = json.loads(resp.read().decode("utf-8"))
                if payload.get("status") == "ok" and payload.get("db") == "ok":
                    sys.exit(0)
                last_err = f"unexpected payload: {payload!r}"
            else:
                last_err = f"HTTP {resp.status}"
    except Exception as exc:
        last_err = str(exc)
    time.sleep(1.0)
print(f"ERROR: Health check failed at {url}: {last_err}", file=sys.stderr)
sys.exit(1)
PY
then
  echo "[setup] Inspect container logs with: docker compose logs --tail=30 api web" >&2
  exit 1
fi
echo "[setup] Health check passed."

# 5. Cron-mode systemd user timer (O-06)
LINGER_WARNING=0
if [[ "$EFFECTIVE_WORKER_MODE" == "cron" ]]; then
  want_timer="$INSTALL_WORKER_TIMER"
  if [[ -z "$want_timer" ]]; then
    if [[ "$NON_INTERACTIVE" -eq 0 && -t 0 ]]; then
      read -r -p "Install 15-minute systemd user timer for cron-mode worker? [Y/n]: " ans
      if [[ -z "$ans" || "$ans" =~ ^[Yy] ]]; then
        want_timer="yes"
      else
        want_timer="no"
      fi
    else
      want_timer="no"
    fi
  fi

  if [[ "$want_timer" == "yes" ]]; then
    existing_cron=""
    if command -v crontab >/dev/null 2>&1; then
      existing_cron="$(crontab -l 2>/dev/null | grep -F 'smartrss worker run-once' || true)"
    fi
    proceed_timer=1
    if [[ -n "$existing_cron" ]]; then
      echo "[setup] WARNING: Existing SmartRSS crontab entry detected:" >&2
      echo "  ${existing_cron}" >&2
      echo "[setup] Remove that crontab entry to avoid double-scheduling alongside smartrss-worker.timer." >&2
      if [[ "$NON_INTERACTIVE" -eq 0 && -t 0 ]]; then
        read -r -p "Continue installing smartrss-worker.timer anyway? [y/N]: " c_ans
        if [[ ! "$c_ans" =~ ^[Yy] ]]; then
          proceed_timer=0
        fi
      fi
    fi

    if [[ "$proceed_timer" -eq 1 ]]; then
      SYSTEMD_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
      mkdir -p "$SYSTEMD_DIR"
      python3 "$RENDER_HELPER" render-unit \
        --template "$REPO_ROOT/deploy/systemd/smartrss-worker.service" \
        --output "$SYSTEMD_DIR/smartrss-worker.service" \
        --repo-root "$REPO_ROOT"
      install -m 0644 "$REPO_ROOT/deploy/systemd/smartrss-worker.timer" "$SYSTEMD_DIR/smartrss-worker.timer"
      systemctl --user daemon-reload
      systemctl --user enable --now smartrss-worker.timer
      echo "[setup] Installed and enabled smartrss-worker.timer."

      linger_val=""
      if command -v loginctl >/dev/null 2>&1; then
        linger_val="$(loginctl show-user "${USER:-$(id -un)}" -p Linger --value 2>/dev/null || true)"
      fi
      if [[ "$linger_val" != "yes" ]]; then
        LINGER_WARNING=1
      fi
    fi
  fi
fi

# 6. Optional OPML feed import (via stdin, zero bind mounts)
if [[ -n "$OPML_FILE" ]]; then
  echo "[setup] Importing feeds from ${OPML_FILE}..."
  docker compose exec -T api smartrss feeds import-opml - < "$OPML_FILE"
fi

# 7. Pairing code & final summary
PAIR_OUTPUT=""
if [[ "$SKIP_PAIR" -eq 0 ]]; then
  PAIR_OUTPUT="$(docker compose exec -T api smartrss pair create --name "$DEVICE_NAME" --platform "$DEVICE_PLATFORM")"
fi

echo ""
echo "============================================================"
echo "SmartRSS Server Setup Complete"
echo "============================================================"
if [[ -n "$PAIR_OUTPUT" ]]; then
  echo "$PAIR_OUTPUT"
  echo ""
fi
echo "Connected LAN Web UI:  http://${PRIMARY_LAN_HOST}:${EFFECTIVE_PORT}"
echo "Local Web UI:          http://localhost:${EFFECTIVE_PORT}"
echo ""
echo "Install detached Ubuntu laptop client:"
echo "  make install-client API_ORIGIN=http://${PRIMARY_LAN_HOST}:${EFFECTIVE_PORT}"
echo ""
echo "Note: Configure a DHCP reservation on your router for ${PRIMARY_LAN_HOST}"
echo "      so LAN URLs, ALLOWED_HOSTS, and CORS_ALLOW_ORIGINS stay stable."
if [[ "$LINGER_WARNING" -eq 1 ]]; then
  echo ""
  echo "WARNING: systemd user linger is NOT enabled for '${USER:-$(id -un)}'."
  echo "         smartrss-worker.timer will NOT fire after a reboot on a headless"
  echo "         server until you run:"
  echo "           sudo loginctl enable-linger ${USER:-$(id -un)}"
fi
echo ""
echo "Manual path (edit .env, make up, make migrate) remains fully supported."
