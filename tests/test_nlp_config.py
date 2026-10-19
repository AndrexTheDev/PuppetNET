"""Configuration contract for the multilingual NLP + Workers AI stages."""

from __future__ import annotations

import pytest

from puppetnet.config import ConfigError, load_settings

BASE = {
    "DRY_RUN": "true",
    "LOG_LEVEL": "ERROR",
    "NEO4J_URI": "",
    "NEO4J_PASSWORD": "",
    "PROXY_WORKER_URL": "",
    "PROXY_AUTH_TOKEN": "",
    "STATE_DIR": "",
}


def env(**overrides: str) -> dict[str, str]:
    values = dict(BASE)
    values.update(overrides)
    return values


def test_multilingual_defaults_are_bound_to_384_tokens():
    settings = load_settings(env())
    assert settings.gliner_model == "urchade/gliner_small-v2.1"
    assert settings.gliner_runtime == "torch"
    assert settings.gliner_context_tokens == 384
    assert settings.gliner_overlap_tokens == 64
    assert settings.triplet_backend == "off"
    assert settings.cf_ai_daily_neurons == 10_000.0
    assert settings.cf_ai_model == "@cf/meta/llama-3.1-8b-instruct"
    assert settings.cf_ai_max_output_tokens == 700


def test_multilingual_checkpoint_can_be_selected():
    settings = load_settings(
        env(
            NLP_BACKEND="gliner",
            GLINER_MODEL="urchade/gliner_multi-v2.1",
            GLINER_CONTEXT_TOKENS="384",
            GLINER_OVERLAP_TOKENS="64",
        )
    )
    assert settings.gliner_model == "urchade/gliner_multi-v2.1"


def test_workers_ai_requires_credentials_when_enabled():
    with pytest.raises(ConfigError, match="CLOUDFLARE_ACCOUNT_ID"):
        load_settings(env(TRIPLET_BACKEND="cloudflare"))
    with pytest.raises(ConfigError, match="CLOUDFLARE_ACCOUNT_ID"):
        load_settings(env(TRIPLET_BACKEND="cloudflare", CLOUDFLARE_ACCOUNT_ID="account-only"))


def test_workers_ai_configuration_is_loaded_and_redacted():
    settings = load_settings(
        env(
            TRIPLET_BACKEND="cloudflare",
            CLOUDFLARE_ACCOUNT_ID="0123456789abcdef0123456789abcdef",
            CLOUDFLARE_API_TOKEN="super-secret-token",
            CF_AI_MODEL="@cf/meta/llama-3.1-8b-instruct-fp8-fast",
            CF_AI_DAILY_NEURONS="8000",
            CF_AI_MAX_CHUNKS="12",
            GLINER_RUNTIME="onnx",
            GLINER_CONTEXT_TOKENS="384",
            GLINER_OVERLAP_TOKENS="48",
        )
    )
    assert settings.cf_ai_model.endswith("-fp8-fast")
    assert settings.cf_ai_daily_neurons == 8000.0
    assert settings.cf_ai_max_chunks == 12
    assert settings.gliner_runtime == "onnx"
    described = settings.describe()
    assert described["cf_ai_api_token"] == "***redacted***"
    assert described["cf_ai_account_id"] == "***redacted***"
    assert "super-secret-token" not in str(described)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"GLINER_RUNTIME": "tensorflow"}, "GLINER_RUNTIME"),
        ({"GLINER_CONTEXT_TOKENS": "4"}, "GLINER_CONTEXT_TOKENS"),
        ({"GLINER_CONTEXT_TOKENS": "384", "GLINER_OVERLAP_TOKENS": "384"}, "GLINER_OVERLAP_TOKENS"),
        ({"TRIPLET_BACKEND": "openai"}, "TRIPLET_BACKEND"),
    ],
)
def test_impossible_configuration_is_rejected(overrides, message):
    with pytest.raises(ConfigError, match=message):
        load_settings(env(**overrides))


def test_workers_ai_budget_must_be_finite_and_positive():
    credentials = {
        "TRIPLET_BACKEND": "cloudflare",
        "CLOUDFLARE_ACCOUNT_ID": "account",
        "CLOUDFLARE_API_TOKEN": "token",
    }
    with pytest.raises(ConfigError, match="CF_AI_DAILY_NEURONS"):
        load_settings(env(**credentials, CF_AI_DAILY_NEURONS="0"))
    with pytest.raises(ConfigError, match="CF_AI_DAILY_NEURONS"):
        load_settings(env(**credentials, CF_AI_DAILY_NEURONS="1000001"))


def test_wrangler_account_id_alone_does_not_enable_workers_ai():
    # The repo already documents CLOUDFLARE_ACCOUNT_ID for Wrangler; having it
    # set must not silently turn on neuron spending.
    settings = load_settings(env(CLOUDFLARE_ACCOUNT_ID="wrangler-account"))
    assert settings.triplet_backend == "off"
    assert settings.cf_ai_account_id == "wrangler-account"
