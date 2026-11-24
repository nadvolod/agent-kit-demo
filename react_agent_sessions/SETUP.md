# Setup runbook: `react_agent_sessions` on your own Temporal Cloud namespace and AWS account

A step-by-step, copy-pasteable version of [README.md](README.md), recorded while setting this up
on a fresh macOS (Apple silicon) machine. It covers the gaps the README doesn't: missing
prerequisites, Docker disk space, and pointing the project at your own namespace and account.
Read the README for the *why*; this file is the *how*.

Values used in this run (replace with yours):

| Setting | Value |
|---|---|
| Temporal namespace | `test-nikk.a2dd6` |
| Temporal gRPC address | `us-west-2.aws.api.temporal.io:7233` (production Temporal Cloud) |
| AWS profile / account / region | `devdays` / `448592647839` / `us-west-2` |

## 0. Get the branch

The branch lives in `cdavisafc/reInvent-2026`. From a clone of your fork:

```bash
git remote add upstream https://github.com/cdavisafc/reInvent-2026.git
git fetch upstream react-agent-sessions
git checkout -b react-agent-sessions upstream/react-agent-sessions
git push -u origin react-agent-sessions
```

## 1. Install prerequisites (macOS)

```bash
brew install uv awscli temporal node pnpm protobuf rustup
rustup default stable
npm install -g @aws/agentcore@latest aws-cdk
```

Gotchas found during setup:

- **`protoc` is required** for the macOS temporalio wheel (`brew install protobuf`), but the
  README doesn't list it. Without it the build dies in `temporalio-protos` with
  ``Could not find `protoc` ``.
- **Homebrew's `rustup` isn't on `PATH`** (it's keg-only). The wheel script looks for `cargo` on
  `PATH` or in `~/.cargo/bin`, so export this in any shell that builds wheels:
  `export PATH=/opt/homebrew/opt/rustup/bin:$PATH`.
- **Docker needs about 10 GB free inside its VM** for the manylinux image and the Rust build. If
  `docker run` fails with `no space left on device`, run `docker system df`, then
  `docker image prune -a`, or raise the disk limit in Docker Desktop → Settings → Resources.
- Docker must be running and able to run `linux/arm64`, which is native on Apple silicon.

Check:

```bash
for t in uv docker cargo protoc agentcore cdk aws temporal pnpm; do command -v $t >/dev/null && echo "ok  $t" || echo "MISSING $t"; done
agentcore --version   # must be 1.x+
```

## 2. Credentials

```bash
# Temporal Cloud API key for your namespace, and OpenAI key (e.g. in ~/.zshrc)
export TEMPORAL_API_KEY=...
export OPENAI_API_KEY=...
export TEMPORAL_ADDRESS=us-west-2.aws.api.temporal.io:7233
export TEMPORAL_NAMESPACE=test-nikk.a2dd6

# AWS via SSO
aws sso login --profile devdays
export AWS_PROFILE=devdays AWS_REGION=us-west-2
aws sts get-caller-identity
```

Check the Temporal key works against the namespace:

```bash
temporal workflow list --address "$TEMPORAL_ADDRESS" --namespace "$TEMPORAL_NAMESPACE" \
  --api-key "$TEMPORAL_API_KEY" --tls --limit 1
```

`authentication failed` means the key belongs to a different account or namespace.

## 3. Point the project at your namespace and account

Edit these two files in `react_agent_sessions/agentcore/`:

- `aws-targets.json`: `account` and `region`.
- `agentcore.json` → `envVars`: `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`, and `AWS_REGION`
  (must equal the region in `aws-targets.json`).

> **⚠️ Production namespaces: turn activity sessions off.** Activity sessions need the
> prerelease **worker sessions / sticky activities** server feature. Production Temporal Cloud
> namespaces (`*.tmprl.cloud` / `*.api.temporal.io`) don't have it yet. There a session-mode
> Worker never receives tasks, and the agent never answers (see [Troubleshooting](#troubleshooting)).
>
> On a production namespace, add this to `envVars` in `agentcore.json`:
>
> ```json
> { "name": "USE_ACTIVITY_SESSIONS", "value": "0" }
> ```
>
> The Worker then runs as an ordinary versioned Serverless Worker. Everything works, but a
> workflow's activities may run in a different microVM from the workflow, so expect some extra
> cold starts. The startup log line says `activity sessions off`. To get sessions, use a
> staging-cell namespace that has them (e.g. `ns-agentcore-demo-001.temporal-dev`), or have them
> enabled on yours. Then remove the variable (it defaults to on) and redeploy.
>
> This run uses `test-nikk.a2dd6` (production) with `USE_ACTIVITY_SESSIONS=0`.

### Model provider: OpenAI API or Bedrock

By default the agent calls the OpenAI API with `OPENAI_API_KEY`. Without an OpenAI key, run it on
**OpenAI models hosted on Amazon Bedrock**, authenticated by the runtime's own AWS role, so no key
is stored. Add to `envVars` in `agentcore.json`:

```json
{ "name": "MODEL_PROVIDER", "value": "bedrock" },
{ "name": "BEDROCK_MODEL", "value": "us.openai.gpt-5.6-terra" }
```

and add `"bedrock-policy.json"` to `additionalPolicies`. This run uses both.

- `BEDROCK_MODEL` must work with Bedrock's OpenAI-compatible **Chat Completions** endpoint.
  `us.openai.gpt-5.6-terra` (an inference profile; the bare `openai.gpt-5.6-terra` returns 400
  "on-demand throughput isn't supported") and `openai.gpt-oss-120b-1:0` were both verified to work,
  including streamed tool calls. List candidates with
  `aws bedrock list-foundation-models --query 'modelSummaries[?providerName==\`OpenAI\`].modelId'`.
- The workflow still asks for `gpt-5.6-luna`. `bedrock_model.py` ignores the requested name and
  serves `BEDROCK_MODEL`, so workflow code doesn't change.
- With `MODEL_PROVIDER=bedrock`, the secret needs only `TEMPORAL_API_KEY`, and
  `./bin/put-secrets.sh` doesn't require `OPENAI_API_KEY`.
- To check a model from your machine (mints a short-term Bedrock API key from your AWS session):

  ```bash
  TOKEN=$(uvx -q --from aws-bedrock-token-generator --with boto3 python -I -c \
    'from aws_bedrock_token_generator import provide_token; print(provide_token(region="us-west-2"))')
  curl -s https://bedrock-runtime.us-west-2.amazonaws.com/openai/v1/chat/completions \
    -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
    -d '{"model":"us.openai.gpt-5.6-terra","messages":[{"role":"user","content":"say hi"}]}'
  ```

## 4. Build the prerelease temporalio wheels

```bash
export PATH=/opt/homebrew/opt/rustup/bin:$PATH   # Homebrew rustup
../prerelease/build-local-wheels.sh               # ~10-15 min first time; re-locks uv.lock
uv sync
uv run pytest                                      # expect 38 passed
```

You should end up with two wheels in `../prerelease/wheels/`, one `manylinux2014_aarch64` and
one `macosx_11_0_arm64`, plus `BUILD_INFO`.

## 5. Create the AWS prerequisites (one time per account/region)

```bash
export AWS_PROFILE=devdays AWS_REGION=us-west-2
cdk bootstrap aws://448592647839/us-west-2        # only if the CDKToolkit stack doesn't exist
./bin/put-secrets.sh                               # secret react-agent-sessions/keys (needs both keys exported)
../agent_status/bin/mk-heartbeat-table.sh          # DynamoDB table agent-session-heartbeats
```

If you change `TEMPORAL_API_KEY` or `OPENAI_API_KEY` later, re-run `./bin/put-secrets.sh`.

## 6. Deploy the runtime

```bash
./bin/create-runtime.sh                            # first run also scaffolds agentcore/cdk (gitignored)

export RUNTIME_ARN=$(aws cloudformation describe-stacks \
  --stack-name AgentCore-ReactAgentSessions-default \
  --query 'Stacks[0].Outputs[?starts_with(OutputKey, `ApplicationRuntimeTemporalWorkerRuntimeArnOutput`)].OutputValue' \
  --output text)
echo "$RUNTIME_ARN"
```

## 7. Create the invoke role

Production Temporal Cloud: leave the invoker ARNs out (the template default trusts the prod
invokers). For the staging cell, pass them as the 4th argument (see README step 4).

```bash
export EXTERNAL_ID=$(uuidgen)
./bin/mk-invoke-role.sh react-agent-sessions-invoke "$EXTERNAL_ID" "${RUNTIME_ARN}*"
# staging cell instead:
# ./bin/mk-invoke-role.sh react-agent-sessions-invoke "$EXTERNAL_ID" "${RUNTIME_ARN}*" \
#   "arn:aws:iam::031568301006:role/wci-lambda-invoke,arn:aws:iam::815250390068:role/wci-lambda-invoke"

export ROLE_ARN=$(aws cloudformation describe-stacks --stack-name react-agent-sessions-invoke \
  --query 'Stacks[0].Outputs[?OutputKey==`RoleARN`].OutputValue' --output text)
```

Lost `EXTERNAL_ID` in a new shell? Recover it:

```bash
export EXTERNAL_ID=$(aws cloudformation describe-stacks --stack-name react-agent-sessions-invoke \
  --query 'Stacks[0].Parameters[?ParameterKey==`AssumeRoleExternalId`].ParameterValue' --output text)
```

## 8. Create the Worker Deployment Version

```bash
export TEMPORAL_TLS=true   # with TEMPORAL_ADDRESS / TEMPORAL_NAMESPACE / TEMPORAL_API_KEY from step 2

temporal worker deployment create --name react-agent-sessions-python
temporal worker deployment create-version \
  --deployment-name react-agent-sessions-python --build-id 1.0.0 \
  --aws-agentcore-endpoint-arn "${RUNTIME_ARN}/runtime-endpoint/DEFAULT" \
  --aws-agentcore-assume-role-arn "$ROLE_ARN" \
  --aws-agentcore-assume-role-external-id "$EXTERNAL_ID"
temporal worker deployment set-current-version \
  --deployment-name react-agent-sessions-python --build-id 1.0.0 --allow-no-pollers --yes
```

## 9. Run the local pieces and test

**The easy way: one command.** It needs only `TEMPORAL_API_KEY` (read from `~/.zshrc` if it
isn't exported) and takes the namespace from `agentcore/agentcore.json`:

```bash
react_agent_sessions/bin/start-local.sh
```

It starts the session manager and the HTTP API in the background (logs in
`$TMPDIR/react-agent-sessions-logs/`), waits for both, and opens the terminal chat client. Type `n`
for a new session, ask a question, and press Enter to approve tool calls. `:quit` stops everything.

**By hand:** three terminals, each with the step 2 env exported, in `react_agent_sessions/`:

```bash
uv run temporal-agent-harness session-manager
uv run temporal-agent-harness serve agents.toml --port 8000
uv run python client.py        # chat; answers ask_user
```

Or test headless:

```bash
SID=$(curl -s -X POST localhost:8000/api/sessions -H 'content-type: application/json' \
  -d '{"agent_workflow_type":"ReactAgent"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["workflow_id"])')
curl -sN -X POST localhost:8000/api/chat -H 'content-type: application/json' \
  -d "{\"session_id\":\"$SID\",\"message\":\"What's the weather in Tokyo?\"}"
```

`POST /api/sessions` returns the session ID as `workflow_id`, not `session_id`.

Expected results (verified in this run, about 10 seconds per turn):

- *"What is the weather in Tokyo?"*: `get_coordinates` → `get_weather` → a short weather summary.
- *"What's the weather?"*: the agent uses *current location*. `get_ip_address` and
  `get_location_info` each emit `tool_approval_requested` and wait. Approve each one (the web UI
  and `client.py` prompt you; headless, use the call below). The answer names a place near
  **Boardman, Oregon**: that's the AgentCore microVM's IP in AWS us-west-2, which proves the
  tools ran in AgentCore.

```bash
curl -s -X POST localhost:8000/api/approve -H 'content-type: application/json' \
  -d '{"session_id":"<SID>","tool_id":"<tool_id from tool_approval_requested>","approved":true}'
```

For the web UI, see README → *Running the UI from the agent kit (temporary)*.

## Troubleshooting

Useful commands:

```bash
# Worker logs (plain lines only)
aws logs filter-log-events \
  --log-group-name "/aws/bedrock-agentcore/runtimes/$(basename "$RUNTIME_ARN")-DEFAULT" \
  --start-time $(( ($(date +%s) - 900) * 1000 )) --query 'events[].message' --output text \
  | tr '\t' '\n' | grep -E '^20[0-9]{2}-' | grep -v -E 'botocore|opentelemetry'

temporal worker deployment describe-version --deployment-name react-agent-sessions-python --build-id 1.0.0
temporal task-queue describe --task-queue react-agent-sessions --select-build-id react-agent-sessions-python:1.0.0
temporal task-queue describe --task-queue react-agent-sessions     # unversioned backlog
```

**Chat hangs; the Worker logs `polling ...` then `worker idle for 60.0s; draining`, over and over.**
The namespace doesn't support worker sessions. What you'll see:

1. `describe-version` shows **no Task Queues** under the version. Session-mode polls don't
   register the queue with the version.
2. The new session's first workflow task sits in the **UNVERSIONED** backlog, which has no
   compute config, so nothing invokes AgentCore for it.
3. If the queue does get registered (e.g. by a non-session Worker), the server invokes AgentCore
   about once a minute, but the session Workers still never receive the task. **Terminate the
   session** (`temporal workflow terminate -w <id>`), or the invoke loop keeps starting microVMs.

Fix: set `USE_ACTIVITY_SESSIONS=0` (see the warning in step 3) and re-run `./bin/create-runtime.sh`,
or use a namespace with worker sessions enabled.

**The turn ends at once with `stream_error: Activity task failed`.** Read the real error from
history:

```bash
temporal workflow show -w <session-id> -o json | python3 -c '
import json,sys
for e in json.load(sys.stdin)["events"]:
    for k,v in e.items():
        if k.endswith("Attributes") and "failure" in v:
            f=v["failure"]
            while f: print(f.get("message","")[:300]); f=f.get("cause")'
```

`OpenAI status code: 401 ... expired_secret_key` means the OpenAI key in the secret has expired.
Export a fresh `OPENAI_API_KEY`, then run `./bin/put-secrets.sh`. New microVMs pick it up; nothing
needs redeploying. Or switch to `MODEL_PROVIDER=bedrock` (see step 3).

Check an OpenAI key before storing it:
`curl -s -o /dev/null -w '%{http_code}\n' https://api.openai.com/v1/models -H "Authorization: Bearer $OPENAI_API_KEY"`
(200 = good, 401 = expired or invalid).

**`aws sts get-caller-identity` → `ForbiddenException ... GetRoleCredentials: No access`.**
SSO login worked, but your user has no permission set on that account. Check with
`aws sso list-accounts --access-token <token from ~/.aws/sso/cache>`, and ask IT for access.

**`temporal ... Error: unknown flag: --address ... --namespace ...`.** zsh doesn't word-split
`$VAR`, so you can't keep flags in a variable. Export `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`,
`TEMPORAL_API_KEY` and `TEMPORAL_TLS=true` instead.

## Teardown

```bash
aws cloudformation delete-stack --stack-name AgentCore-ReactAgentSessions-default
aws cloudformation delete-stack --stack-name react-agent-sessions-invoke
aws cloudformation delete-stack --stack-name agent-session-heartbeats   # shared with other agents
aws secretsmanager delete-secret --secret-id react-agent-sessions/keys --force-delete-without-recovery
temporal worker deployment delete --name react-agent-sessions-python    # after its versions drain
```
