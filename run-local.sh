#!/usr/bin/env bash
# Local development launcher. Starts the API and the dashboard together.
#
#   ./run-local.sh          both (API :8000, web :5173)
#   ./run-local.sh api      API only
#   ./run-local.sh web      dashboard only
set -euo pipefail
cd "$(dirname "$0")"

# --- secrets: never hardcode, never commit ---
if [ -f ~/.gemini_env ]; then set -a; source ~/.gemini_env; set +a; fi
if [ -f .env ]; then set -a; source .env; set +a; fi

: "${GEMINI_API_KEY:?Set GEMINI_API_KEY (in ~/.gemini_env or .env)}"
: "${CURBSIDE_FROM_NAME:?Set the return address vars - see .env.example}"

MODE="${1:-both}"
pids=()
cleanup() { for p in "${pids[@]:-}"; do kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT INT TERM

if [ "$MODE" = "api" ] || [ "$MODE" = "both" ]; then
  echo "API      http://localhost:8000        (docs at /docs)"
  python3 -m uvicorn curbside.api.app:app --reload --port 8000 &
  pids+=($!)
fi

if [ "$MODE" = "web" ] || [ "$MODE" = "both" ]; then
  [ -d web/node_modules ] || (cd web && npm install)
  echo "Dashboard http://localhost:5173"
  (cd web && npm run dev) &
  pids+=($!)
fi

echo
echo "Ctrl-C to stop."
wait
