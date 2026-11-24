# ABOUTME: Amazon Bedrock AgentCore Runtime entry point that runs the ReAct agent's Temporal Worker.
# Starts the Worker on invocation, polls until idle, then drains — a scale-to-zero Serverless Worker.

"""Runs a Temporal Worker hosting the harness ReAct agent inside an Amazon Bedrock AgentCore
Runtime.

The AgentCore SDK provides the HTTP contract: ``BedrockAgentCoreApp`` serves /ping and
/invocations, and reports "HealthyBusy" while an async task is registered.

Temporal Cloud invokes the runtime when Tasks arrive on the Worker Deployment's task queue. The
Worker only starts *after* invocation, and it runs in the background: /invocations registers an
async task and acknowledges immediately rather than holding the response open for the whole polling
window. AgentCore keeps the session alive for as long as /ping reports "HealthyBusy", which is
exactly what add_async_task/complete_async_task drive.

The Worker polls until it has been idle for AGENTCORE_DEBOUNCE_SECONDS. Model calls (the OpenAI
plugin's model activities) and the weather/geo/IP tools are all Activities, so the interceptor
below sees the agent's whole turn and the Worker is not torn down mid-thought. When the agent
parks on ``ask_user``, nothing is in flight: the Worker drains, and Temporal Cloud invokes the
runtime again when the user's answer arrives.

Plugin wiring is the harness streaming path (see the harness react_agent example): the OpenAI
plugin's ``stream_to_provider`` + ``harness_observer_factory`` hooks route streamed model events
into the live turn stream; the harness plugin is listed last so its data converter layers on top.

Activity sessions (experimental; unreleased temporalio build, see ../prerelease/build-local-wheels.sh): every
Activity the agent workflow starts on this task queue -- model calls and tools -- runs on the
*same* Worker, and therefore the same AgentCore microVM, that holds the workflow
(``ActivitySessionInterceptor``). The Worker is keyed to the AgentCore session
(``context.session_id``) with ``SingleSessionAdmission``, so one runtime session serves exactly
one workflow for the Worker's lifetime. This needs a Temporal Service with sticky activity
execution enabled.

Two heartbeats feed the local status page (../agent_status/status_page.py):

- The Temporal Worker heartbeat: the SDK reports the Worker to the Temporal server every
  WORKER_HEARTBEAT, read back with ListWorkers. It stops when the Worker drains. The Worker's
  identity is ``agentcore:<session id>``, so the workflow history names the session that ran each
  workflow task.
- The session heartbeat (session_heartbeat.py): a process-level task, started on the first invoke
  when SESSION_HEARTBEAT_TABLE is set, that writes to DynamoDB until AgentCore destroys the
  microVM. It outlives the Worker and must never register an async task, or the session would
  report HealthyBusy and never go idle.
"""

import asyncio
import functools
import os
import time
from datetime import timedelta

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from temporal_agent_harness.ai_sdks.openai_agents import (
    ModelActivityParameters,
    OpenAIAgentsPlugin,
)
from temporal_agent_harness.ai_sdks.openai_agents_harness import (
    harness_observer_factory,
    stream_to_provider,
)
from temporal_agent_harness.plugin import AgentHarnessPlugin
from temporalio.client import Client
from temporalio.common import VersioningBehavior
from temporalio.runtime import Runtime, TelemetryConfig
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    Interceptor,
    Worker,
    WorkerDeploymentConfig,
    WorkerDeploymentVersion,
    WorkerSessionConfig,
)

from activity_sessions import ActivitySessionInterceptor, session_config_for
from bedrock_model import model_provider_from_env
from human_tools import HUMAN_TOOLS
from runtime_secrets import load_secrets_into_env
from session_heartbeat import SessionHeartbeat
from tool_activities import ALL_TOOLS
from workflow import TASK_QUEUE, ReactAgentWorkflow

app = BedrockAgentCoreApp()
log = app.logger

# The background Worker, when one is running. Holds the strong reference that
# asyncio.create_task() does not keep, and doubles as the "already polling?" flag.
_worker: asyncio.Task[None] | None = None
# This session's liveness heartbeat, started on the first invoke; it lives as long as the process.
_heartbeat: SessionHeartbeat | None = None

DEPLOYMENT_NAME = os.environ.get(
    "TEMPORAL_DEPLOYMENT_NAME", "react-agent-sessions-python"
)
BUILD_ID = os.environ.get("TEMPORAL_BUILD_ID", "1.0.0")
WORKER_TASK_QUEUE = os.environ.get("REACT_AGENT_TASK_QUEUE", TASK_QUEUE)
# The Secrets Manager secret (JSON) holding the keys below. Unset for local runs, where the
# keys come from the shell environment instead.
SECRET_ID = os.environ.get("REACT_AGENT_SECRET_ID")
# OPENAI_API_KEY is only needed when the model calls go to the OpenAI API (see bedrock_model.py).
SECRET_KEYS = ["TEMPORAL_API_KEY"] + (
    []
    if os.environ.get("MODEL_PROVIDER", "openai").strip().lower() == "bedrock"
    else ["OPENAI_API_KEY"]
)

# Session Workers require a fixed-size activity slot supplier, which is what
# max_concurrent_activities (without a tuner) gives.
ACTIVITY_SLOTS = 4

# How long the Worker keeps polling after it goes idle.
DEBOUNCE = float(os.environ.get("AGENTCORE_DEBOUNCE_SECONDS", "60"))
# How long the drain waits for in-flight Activities (a model or tool call).
DRAIN = timedelta(seconds=120)
# How often the SDK reports this Worker to the Temporal server (read back with ListWorkers). A new
# Worker first shows there one interval after it starts, so keep it short for the status page.
WORKER_HEARTBEAT = timedelta(seconds=2)


def activity_sessions_enabled() -> bool:
    """USE_ACTIVITY_SESSIONS (default on). Turn off on a namespace without worker sessions.

    Production Temporal Cloud doesn't have the prerelease worker-sessions feature yet. There a
    session-mode Worker never receives tasks, so set USE_ACTIVITY_SESSIONS=0 to run as an ordinary
    versioned serverless Worker (activities may then land on other microVMs).
    """
    return os.environ.get("USE_ACTIVITY_SESSIONS", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def session_wiring(
    session_id: str | None, *, enabled: bool
) -> tuple[WorkerSessionConfig | None, list[Interceptor]]:
    """The Worker's session config and session interceptors, or neither when sessions are off."""
    if not enabled:
        return None, []
    return session_config_for(session_id), [ActivitySessionInterceptor()]


# The DynamoDB table for the session heartbeat. Unset (local runs) turns the heartbeat off.
HEARTBEAT_TABLE = os.environ.get("SESSION_HEARTBEAT_TABLE")


class ActivityTracker(Interceptor):
    """Tracks in-flight activities and blocks until AGENTCORE_DEBOUNCE_SECONDS elapses with no events."""

    def __init__(self) -> None:
        self.inflight = 0
        self.changed = asyncio.Event()

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        return _TrackedActivity(next, self)

    async def wait_until_idle(self, debounce: float) -> None:
        """Return once no Activity has run for ``debounce`` seconds."""
        while True:
            self.changed.clear()
            try:
                # Wake the moment an Activity starts or finishes; a timeout
                # instead means nothing has happened for the whole window.
                await asyncio.wait_for(self.changed.wait(), timeout=debounce)
            except TimeoutError:
                if self.inflight == 0:
                    return


class _TrackedActivity(ActivityInboundInterceptor):
    def __init__(
        self, next: ActivityInboundInterceptor, tracker: ActivityTracker
    ) -> None:
        super().__init__(next)
        self._tracker = tracker

    async def execute_activity(self, input: ExecuteActivityInput):
        self._tracker.inflight += 1
        self._tracker.changed.set()
        log.info("activity in flight: %d", self._tracker.inflight)
        try:
            return await self.next.execute_activity(input)
        finally:
            self._tracker.inflight -= 1
            self._tracker.changed.set()


def _openai_agents_plugin() -> OpenAIAgentsPlugin:
    return OpenAIAgentsPlugin(
        model_params=ModelActivityParameters(
            # Streaming leans on activity heartbeats to notice a stuck model call; keep the
            # heartbeat timeout well under the overall start-to-close.
            start_to_close_timeout=timedelta(minutes=2),
            heartbeat_timeout=timedelta(seconds=30),
            # The harness streaming seam: route streamed events to the in-flight turn.
            stream_to_provider=stream_to_provider,
            # Each flush is one publish Signal; 250ms trades a little smoothness for less
            # workflow history than the source example's 50ms.
            streaming_batch_interval=timedelta(milliseconds=250),
        ),
        observer_factory=harness_observer_factory,
        # MODEL_PROVIDER=bedrock serves model calls from Bedrock (see bedrock_model.py);
        # None keeps the plugin's default OpenAI provider.
        model_provider=model_provider_from_env(),
    )


def worker_identity(session_id: str | None) -> str | None:
    """The Temporal identity for this session's Worker; None keeps the SDK's default."""
    return f"agentcore:{session_id}" if session_id else None


@functools.cache
def _temporal_runtime() -> Runtime:
    # Created once: each Runtime starts its own thread pool.
    return Runtime(
        telemetry=TelemetryConfig(), worker_heartbeat_interval=WORKER_HEARTBEAT
    )


def _heartbeat_writer(table_name: str):
    table = boto3.resource("dynamodb").Table(table_name)
    return lambda item: table.put_item(Item=item)


def _start_heartbeat(session_id: str | None) -> None:
    """Start this session's heartbeat on the first invoke that can name the session."""
    global _heartbeat
    if _heartbeat is not None or not (HEARTBEAT_TABLE and session_id):
        return
    _heartbeat = SessionHeartbeat(
        session_id, WORKER_TASK_QUEUE, _heartbeat_writer(HEARTBEAT_TABLE)
    )
    _heartbeat.start()


async def run_worker(session_id: str | None) -> None:
    """Poll until idle, then drain."""
    if SECRET_ID:
        load_secrets_into_env(
            SECRET_ID, SECRET_KEYS, client=boto3.client("secretsmanager")
        )
    api_key = os.environ.get("TEMPORAL_API_KEY") or None
    client = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
        api_key=api_key,
        tls=bool(api_key),
        identity=worker_identity(session_id),
        runtime=_temporal_runtime(),
        plugins=[
            _openai_agents_plugin(),
            # Hands over the agent's whole toolset: the plugin registers the four tool activity
            # bodies and skips ask_user (a callback tool with no worker-side body). Offload is
            # off: the harness default writes to local disk, which is ephemeral here and
            # unreadable by the locally run server — an oversized payload should fail loudly.
            AgentHarnessPlugin(
                tools=[*ALL_TOOLS, *HUMAN_TOOLS], large_payload_offload=None
            ),
        ],
    )

    tracker = ActivityTracker()
    sessions = activity_sessions_enabled()
    session_config, session_interceptors = session_wiring(session_id, enabled=sessions)
    log.info(
        "polling %s as %s/%s (session %s, activity sessions %s)",
        WORKER_TASK_QUEUE,
        DEPLOYMENT_NAME,
        BUILD_ID,
        session_id,
        "on" if sessions else "off",
    )
    worker = Worker(
        client,
        task_queue=WORKER_TASK_QUEUE,
        workflows=[ReactAgentWorkflow],
        max_concurrent_activities=ACTIVITY_SLOTS,
        session_config=session_config,
        interceptors=[tracker, *session_interceptors],
        deployment_config=WorkerDeploymentConfig(
            version=WorkerDeploymentVersion(
                deployment_name=DEPLOYMENT_NAME, build_id=BUILD_ID
            ),
            use_worker_versioning=True,
            default_versioning_behavior=VersioningBehavior.PINNED,
        ),
        graceful_shutdown_timeout=DRAIN,
    )
    await poll_until_idle(worker, tracker, DEBOUNCE)


async def poll_until_idle(worker, tracker: ActivityTracker, debounce: float) -> None:
    """Run the Worker until no Activity has run for ``debounce`` seconds, then drain it."""
    async with worker:
        await tracker.wait_until_idle(debounce)
        # Nothing is in flight, and the Worker stops polling as the drain starts. Report that
        # now: the shutdown itself can take a while.
        if _heartbeat is not None:
            _heartbeat.set_worker_running(False)
        log.info("worker idle for %ss; draining", debounce)
        drain_started = time.monotonic()
    log.info("worker drained in %.1fs", time.monotonic() - drain_started)


async def _run_until_idle(task_id: int, session_id: str | None) -> None:
    """Own the Worker's whole life, and always release the async task."""
    if _heartbeat is not None:
        _heartbeat.set_worker_running(True, worker_identity(session_id))
    try:
        await run_worker(session_id)
    except Exception:
        # Nothing awaits this task, so an error would otherwise be swallowed.
        log.exception("worker failed in async task")
    finally:
        # Without this the session stays HealthyBusy until MaxLifetime.
        app.complete_async_task(task_id)
        if _heartbeat is not None:
            _heartbeat.set_worker_running(False)


@app.entrypoint
async def invoke(payload: dict, context) -> dict:
    """Start the Worker and acknowledge. The payload is unused."""
    session_id = context.session_id or None
    # Prevent duplicate workers since we exit early
    global _worker
    if _worker is not None and not _worker.done():
        log.info("worker already polling %s", WORKER_TASK_QUEUE)
        return {"message": "worker already polling", "task_queue": WORKER_TASK_QUEUE}

    _start_heartbeat(session_id)
    task_id = app.add_async_task("temporal-worker")
    _worker = asyncio.create_task(_run_until_idle(task_id, session_id))

    return {"message": "worker starting", "task_queue": WORKER_TASK_QUEUE}


if __name__ == "__main__":
    app.run()
