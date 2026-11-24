# ABOUTME: Tests for the Bedrock model provider: provider selection from the environment, and
# that every model it hands out targets Bedrock's OpenAI-compatible endpoint with a fresh token.

import pytest
from agents import OpenAIChatCompletionsModel

import bedrock_model


def test_provider_is_none_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)

    assert bedrock_model.model_provider_from_env() is None


def test_provider_is_none_for_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "openai")

    assert bedrock_model.model_provider_from_env() is None


def test_bedrock_provider_reads_model_and_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "bedrock")
    monkeypatch.setenv("BEDROCK_MODEL", "us.openai.gpt-5.6-terra")
    monkeypatch.setenv("AWS_REGION", "us-west-2")

    provider = bedrock_model.model_provider_from_env()

    assert isinstance(provider, bedrock_model.BedrockModelProvider)
    assert provider.model == "us.openai.gpt-5.6-terra"
    assert provider.region == "us-west-2"


def test_unknown_provider_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "nope")

    with pytest.raises(ValueError, match="MODEL_PROVIDER"):
        bedrock_model.model_provider_from_env()


def test_get_model_ignores_requested_name_and_mints_a_token_per_model() -> None:
    tokens = iter(["bedrock-api-key-1", "bedrock-api-key-2"])
    provider = bedrock_model.BedrockModelProvider(
        model="us.openai.gpt-5.6-terra",
        region="us-west-2",
        token_source=lambda: next(tokens),
    )

    first = provider.get_model("gpt-5.6-luna")
    second = provider.get_model(None)

    assert isinstance(first, OpenAIChatCompletionsModel)
    assert first.model == "us.openai.gpt-5.6-terra"
    assert (
        str(first._client.base_url)
        == "https://bedrock-runtime.us-west-2.amazonaws.com/openai/v1/"
    )
    assert first._client.api_key == "bedrock-api-key-1"
    assert second._client.api_key == "bedrock-api-key-2"
    assert first._client.max_retries == 0
