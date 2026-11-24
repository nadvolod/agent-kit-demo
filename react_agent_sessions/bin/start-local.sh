#!/bin/bash
set -euo pipefail

# ABOUTME: One command to try the deployed agent: starts the session manager and the HTTP API in the
# background, waits until both are ready, then opens the terminal chat client or the web UI, and
# stops everything on exit.
#
# Usage (from anywhere):
#   react_agent_sessions/bin/start-local.sh         # terminal chat client
#   react_agent_sessions/bin/start-local.sh --ui    # web UI at http://127.0.0.1:5173 (Ctrl-C stops)
#
# --ui runs the agent kit's UI from ../temporal-agent-kit, which has the fix that lets you start a
# session while a serverless Worker is scaled to zero (the UI built into `serve` on :8000 refuses).
# The first --ui run installs its packages with the pnpm version the kit pins, via npx, so your
# global pnpm is untouched.
#
# Needs TEMPORAL_API_KEY in the environment (e.g. exported in ~/.zshrc). TEMPORAL_ADDRESS and
# TEMPORAL_NAMESPACE default to the values the AgentCore runtime uses (agentcore/agentcore.json),
# so the local pieces and the deployed Worker always talk to the same namespace.
# Logs go to $LOG_DIR (default: a temp dir, printed at start).

SAMPLE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SAMPLE_DIR"

MODE=client
case "${1:-}" in
  --ui) MODE=ui ;;
  "") ;;
  *)
    echo "Usage: $0 [--ui]" >&2
    exit 1
    ;;
esac

# The web UI's dev server forwards /api to 127.0.0.1:8000, so the API port is fixed.
PORT=8000
UI_PORT=5173
UI_DIR="$SAMPLE_DIR/../temporal-agent-kit/typescript"
LOG_DIR="${LOG_DIR:-${TMPDIR:-/tmp}/react-agent-sessions-logs}"
mkdir -p "$LOG_DIR"

if [ -z "${TEMPORAL_API_KEY:-}" ] && [ -f "$HOME/.zshrc" ]; then
  # Started from a shell that didn't load ~/.zshrc (e.g. an IDE task): pick the key up from it.
  TEMPORAL_API_KEY="$(zsh -ic 'printf %s "${TEMPORAL_API_KEY:-}"' 2>/dev/null || true)"
fi
: "${TEMPORAL_API_KEY:?export TEMPORAL_API_KEY (your Temporal Cloud API key) first}"
export TEMPORAL_API_KEY

runtime_env() {
  python3 -c 'import json, sys
for e in json.load(open("agentcore/agentcore.json"))["runtimes"][0]["envVars"]:
    if e["name"] == sys.argv[1]: print(e["value"])' "$1"
}
export TEMPORAL_ADDRESS="${TEMPORAL_ADDRESS:-$(runtime_env TEMPORAL_ADDRESS)}"
export TEMPORAL_NAMESPACE="${TEMPORAL_NAMESPACE:-$(runtime_env TEMPORAL_NAMESPACE)}"

port_busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
if port_busy "$PORT"; then
  echo "Port $PORT is already in use (another copy of this script?). Stop it first." >&2
  exit 1
fi
if [ "$MODE" = ui ] && port_busy "$UI_PORT"; then
  echo "Port $UI_PORT (the web UI) is already in use. Stop whatever is on it first." >&2
  exit 1
fi

pids=()
cleanup() {
  trap - EXIT INT TERM
  echo
  echo "Stopping local services..."
  for pid in "${pids[@]}"; do
    pkill -P "$pid" 2>/dev/null || true
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "Namespace: $TEMPORAL_NAMESPACE ($TEMPORAL_ADDRESS)"
echo "Logs:      $LOG_DIR"
echo "Starting session manager and API server (first run may take a minute while uv syncs)..."

uv sync -q
uv run temporal-agent-harness session-manager >"$LOG_DIR/session-manager.log" 2>&1 &
pids+=($!)
uv run temporal-agent-harness serve agents.toml --port "$PORT" >"$LOG_DIR/serve.log" 2>&1 &
pids+=($!)

for _ in $(seq 1 90); do
  for pid in "${pids[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "A background process exited early. Last log lines:" >&2
      tail -n 15 "$LOG_DIR/session-manager.log" "$LOG_DIR/serve.log" >&2
      exit 1
    fi
  done
  if grep -q "Session manager worker ready" "$LOG_DIR/session-manager.log" 2>/dev/null &&
    curl -sf "http://127.0.0.1:$PORT/api/sessions" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
if ! curl -sf "http://127.0.0.1:$PORT/api/sessions" >/dev/null 2>&1; then
  echo "Timed out waiting for the API server; see $LOG_DIR/serve.log" >&2
  exit 1
fi

if [ "$MODE" = client ]; then
  cat <<'READY'

Ready. In the chat client:
  n                              start a new session (then type your question)
  What is the weather in Tokyo?  chains get_coordinates -> get_weather
  What's the weather?            uses "current location"; press Enter to approve each tool
  :sessions / :quit              back to the session list / leave (stops everything)

The first message after an idle spell takes a few extra seconds while AgentCore starts a microVM.

READY
  uv run python client.py --server "http://127.0.0.1:$PORT"
  exit 0
fi

# --ui
PNPM_VERSION="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["packageManager"].split("@")[1])' "$UI_DIR/package.json")"
if [ ! -d "$UI_DIR/apps/ui/node_modules" ]; then
  echo "Installing web UI packages (one time, a minute or two)..."
  # ERR_PNPM_IGNORED_BUILDS is expected: the UI doesn't need those packages' build scripts.
  (cd "$UI_DIR" && npx -y "pnpm@$PNPM_VERSION" install >"$LOG_DIR/ui-install.log" 2>&1) || true
  # pnpm may write placeholder lines into the workspace file; keep the checkout clean.
  git -C "$UI_DIR" checkout -- pnpm-workspace.yaml 2>/dev/null || true
  if [ ! -d "$UI_DIR/apps/ui/node_modules" ]; then
    echo "Web UI install failed; see $LOG_DIR/ui-install.log" >&2
    exit 1
  fi
fi

(cd "$UI_DIR/apps/ui" && exec npx -y "pnpm@$PNPM_VERSION" dev --port "$UI_PORT" --strictPort) \
  >"$LOG_DIR/ui.log" 2>&1 &
pids+=($!)
for _ in $(seq 1 60); do
  curl -sf "http://127.0.0.1:$UI_PORT/" >/dev/null 2>&1 && break
  sleep 1
done
if ! curl -sf "http://127.0.0.1:$UI_PORT/" >/dev/null 2>&1; then
  echo "Timed out waiting for the web UI; see $LOG_DIR/ui.log" >&2
  exit 1
fi

cat <<READY

Ready: http://127.0.0.1:$UI_PORT  (opening it in your browser)

  Click "New session" (top of the Sessions list), then "Start session", and ask:
    What is the weather in Tokyo?   chains get_coordinates -> get_weather
    What's the weather?             uses "current location"; approve each tool in the chat
  The dialog may warn "No worker is polling react-agent-sessions". That's expected for a
  serverless Worker scaled to zero; Start session works anyway.

Press Ctrl-C to stop everything.
READY
open "http://127.0.0.1:$UI_PORT" 2>/dev/null || true
wait
