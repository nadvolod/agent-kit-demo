# ABOUTME: Optional model provider that serves the agent's model calls from Amazon Bedrock's
# OpenAI-compatible Chat Completions endpoint, authenticated by the Worker's own AWS role.

"""Run the agent on OpenAI models hosted on Bedrock instead of the OpenAI API.

Set ``MODEL_PROVIDER=bedrock`` and ``BEDROCK_MODEL`` (e.g. ``us.openai.gpt-5.6-terra``; models
that need an inference profile take its ID). The workflow still asks for its own model name; this
provider ignores it and serves ``BEDROCK_MODEL``, so workflow code (and its determinism) is
untouched.

No API key is stored: each model gets a short-term Bedrock API key minted from the process's AWS
credentials (the runtime's execution role on AgentCore), which needs ``bedrock:CallWithBearerToken``
plus ``bedrock:InvokeModel*`` (see ``bedrock-policy.json``). Minting is a local presign, so a fresh
token per model call is cheap and never outlives the credentials it came from.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from agents import Model, ModelProvider, OpenAIChatCompletionsModel
from openai import AsyncOpenAI


def _bedrock_token(region: str) -> str:
    from aws_bedrock_token_generator import provide_token

    return provide_token(region=region)


class BedrockModelProvider(ModelProvider):
    """Hands out Chat Completions models on Bedrock's OpenAI-compatible endpoint."""

    def __init__(
        self,
        model: str,
        region: str,
        token_source: Callable[[], str] | None = None,
    ) -> None:
        self.model = model
        self.region = region
        self._token_source = token_source or (lambda: _bedrock_token(region))

    def get_model(self, model_name: str | None) -> Model:
        client = AsyncOpenAI(
            base_url=f"https://bedrock-runtime.{self.region}.amazonaws.com/openai/v1",
            api_key=self._token_source(),
            max_retries=0,  # Temporal retries the activity.
        )
        return OpenAIChatCompletionsModel(model=self.model, openai_client=client)


def model_provider_from_env() -> ModelProvider | None:
    """``None`` (the plugin's default OpenAI provider) unless ``MODEL_PROVIDER=bedrock``."""
    provider = os.environ.get("MODEL_PROVIDER", "openai").strip().lower()
    if provider == "openai":
        return None
    if provider == "bedrock":
        return BedrockModelProvider(
            model=os.environ.get("BEDROCK_MODEL", "us.openai.gpt-5.6-terra"),
            region=os.environ.get("AWS_REGION", "us-west-2"),
        )
    raise ValueError(f"MODEL_PROVIDER must be 'openai' or 'bedrock', got {provider!r}")
