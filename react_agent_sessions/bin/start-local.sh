#!/bin/bash
set -euo pipefail

# ABOUTME: One command to try the deployed agent: starts the session manager and the HTTP API in the
# background, waits until both are ready, opens the terminal chat client, and stops them all on exit.
#
# Usage (from anywhere):  react_agent_sessions/bin/start-local.sh
#
# Needs TEMPORAL_API_KEY in the environment (e.g. exported in ~/.zshrc). TEMPORAL_ADDRESS and
# TEMPORAL_NAMESPACE default to the values the AgentCore runtime uses (agentcore/agentcore.json),
# so the local pieces and the deployed Worker always talk to the same namespace.
# Logs go to $LOG_DIR (default: a temp dir, printed at start). PORT defaults to 8000.

SAMPLE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SAMPLE_DIR"

PORT="${PORT:-8000}"
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

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $PORT is already in use. Stop whatever is on it, or run with PORT=<other port>." >&2
  exit 1
fi

pids=()
cleanup() {
  trap - EXIT INT TERM
  echo
  echo "Stopping local session manager and API server..."
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
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

cat <<EOF

Ready. In the chat client:
  n                              start a new session (then type your question)
  What is the weather in Tokyo?  chains get_coordinates -> get_weather
  What's the weather?            uses "current location"; press Enter to approve each tool
  :sessions / :quit              back to the session list / leave (stops everything)

The first message after an idle spell takes a few extra seconds while AgentCore starts a microVM.

EOF

uv run python client.py --server "http://127.0.0.1:$PORT"
