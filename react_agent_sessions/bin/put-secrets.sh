#!/bin/bash
set -euo pipefail

# ABOUTME: Creates or updates the Secrets Manager secret the AgentCore worker reads its API keys from.
# Reads TEMPORAL_API_KEY (and OPENAI_API_KEY, if set) from the environment so they never land in a file.
#
# The secret name must match REACT_AGENT_SECRET_ID in agentcore/agentcore.json, and the
# runtime's execution role is granted read access to it by secrets-policy.json.

SECRET_ID="${1:-react-agent-sessions/keys}"
TARGET="${2:-default}"
: "${TEMPORAL_API_KEY:?export TEMPORAL_API_KEY first}"
# OPENAI_API_KEY is optional: not needed with MODEL_PROVIDER=bedrock in agentcore.json.

SAMPLE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# The secret must live in the runtime's region, which `agentcore deploy` takes from aws-targets.json.
REGION="$(python3 -c 'import json, sys; print(next(t["region"] for t in json.load(open(sys.argv[1])) if t["name"] == sys.argv[2]))' "$SAMPLE_DIR/agentcore/aws-targets.json" "$TARGET")"

SECRET_JSON="$(python3 -c 'import json, os; print(json.dumps({k: os.environ[k] for k in ("TEMPORAL_API_KEY", "OPENAI_API_KEY") if os.environ.get(k)}))')"

if aws secretsmanager describe-secret --region "$REGION" --secret-id "$SECRET_ID" >/dev/null 2>&1; then
  aws secretsmanager put-secret-value --region "$REGION" --secret-id "$SECRET_ID" --secret-string "$SECRET_JSON" >/dev/null
  echo "Updated secret $SECRET_ID in $REGION" >&2
else
  aws secretsmanager create-secret --region "$REGION" --name "$SECRET_ID" --secret-string "$SECRET_JSON" >/dev/null
  echo "Created secret $SECRET_ID in $REGION" >&2
fi
