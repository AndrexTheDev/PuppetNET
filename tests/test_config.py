"""Configuration tests: environment parsing, validation, YAML overlay."""

from __future__ import annotations

import logging
import os

import pytest

from puppetnet.config import REPO_ROOT, ConfigError, Settings, load_settings, resolve_source_specs
from puppetnet.models import SourceSpec, SourceType
from puppetnet.sources.registry import SOURCE_REGISTRY

logging.disable(logging.CRITICAL)

#: Baseline that always validates: dry run + direct fallback.
BASE = {"DRY_RUN": "true", "LOG_LEVEL": "ERROR"}


def test_defaults_are_offline_safe():
    settings = load_settings(dict(BASE))
    assert settings.dry_run is True
    assert settings.log_level == "ERROR"
    assert settings.spacy_models, "a model preference list must always exist"
    assert settings.max_documents_total > 0


def test_spacy_model_preference_order():
    settings = load_settings(dict(BASE))
    assert settings.spacy_models[0] == "en_core_web_trf"
    assert "en_core_web_lg" in settings.spacy_models


def test_legacy_single_model_variable_is_honoured_first():
    env = dict(BASE, SPACY_MODEL="en_core_web_sm")
    assert load_settings(env).spacy_models[0] == "en_core_web_sm"


def test_numeric_and_boolean_parsing():
    env = dict(
        BASE,
        NEO4J_BATCH_SIZE="250",
        MAX_RUNTIME_SECONDS="600",
        MIN_EDGE_CONFIDENCE="0.15",
        TOKEN_BUCKET_RATE_PER_SEC="0.75",
        ONLY_NEW_DOCUMENTS="false",
        LOG_JSON="yes",
        NLP_ENABLED="0",
    )
    settings = load_settings(env)
    assert settings.neo4j_batch_size == 250
    assert settings.max_runtime_seconds == 600
    assert settings.min_edge_confidence == pytest.approx(0.15)
    assert settings.token_bucket_rate_per_sec == pytest.approx(0.75)
    assert settings.only_new_documents is False
    assert settings.log_json is True
    assert settings.nlp_enabled is False


def test_list_parsing_accepts_commas_and_spaces():
    env = dict(BASE, ENABLED_SOURCES="icij_leaks, wikidata ,opencorporates", RSS_FEEDS="https://a.example/rss,https://b.example/rss")
    settings = load_settings(env)
    assert settings.enabled_sources == ["icij_leaks", "wikidata", "opencorporates"]
    assert settings.rss_feeds == ["https://a.example/rss", "https://b.example/rss"]


def test_invalid_numbers_raise_config_error():
    with pytest.raises(ConfigError):
        load_settings(dict(BASE, NEO4J_BATCH_SIZE="not-a-number"))


def test_worker_url_is_normalised():
    settings = load_settings(dict(BASE, PROXY_WORKER_URL="https://relay.example.workers.dev/"))
    assert settings.worker_url == "https://relay.example.workers.dev"
    assert settings.worker_configured is False  # still needs a token
    settings = load_settings(dict(BASE, PROXY_WORKER_URL="https://relay.example.workers.dev", PROXY_AUTH_TOKEN="secret"))
    assert settings.worker_configured is True


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def test_neo4j_credentials_are_required_outside_dry_run():
    with pytest.raises(ConfigError) as excinfo:
        load_settings({"DRY_RUN": "false"})
    message = str(excinfo.value)
    assert "NEO4J_PASSWORD" in message or "NEO4J_URI" in message


def test_dry_run_needs_no_credentials():
    assert load_settings(dict(BASE)).neo4j_password == ""


def test_no_transport_at_all_is_rejected():
    with pytest.raises(ConfigError) as excinfo:
        load_settings({"DRY_RUN": "true", "DIRECT_FALLBACK_ENABLED": "false", "PROXY_WORKER_ENABLED": "false"})
    assert "direct fallback" in str(excinfo.value).lower() or "relay" in str(excinfo.value).lower()


def test_runtime_floor_is_enforced():
    with pytest.raises(ConfigError):
        load_settings(dict(BASE, MAX_RUNTIME_SECONDS="10"))


def test_confidence_bounds_are_enforced():
    with pytest.raises(ConfigError):
        load_settings(dict(BASE, MIN_EDGE_CONFIDENCE="1.5"))
    with pytest.raises(ConfigError):
        load_settings(dict(BASE, MIN_EDGE_CONFIDENCE="-0.2"))


def test_batch_size_bounds_are_enforced():
    with pytest.raises(ConfigError):
        load_settings(dict(BASE, NEO4J_BATCH_SIZE="0"))
    with pytest.raises(ConfigError):
        load_settings(dict(BASE, NEO4J_BATCH_SIZE="999999"))


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #
def test_describe_redacts_secrets():
    env = dict(
        BASE,
        NEO4J_PASSWORD="super-secret",
        PROXY_AUTH_TOKEN="worker-token",
        OPENCORPORATES_API_TOKEN="oc-token",
        COMPANIES_HOUSE_API_KEY="ch-key",
        PROXY_WORKER_URL="https://relay.example",
    )
    snapshot = load_settings(env).describe()
    blob = repr(snapshot)
    for secret in ("super-secret", "worker-token", "oc-token", "ch-key"):
        assert secret not in blob
    # …but the operator can still see that something was configured.
    assert snapshot["neo4j_password"]


# --------------------------------------------------------------------------- #
# Source registry & YAML overlay
# --------------------------------------------------------------------------- #
def test_registry_covers_the_specified_source_families():
    adapters = {spec.adapter for spec in SOURCE_REGISTRY}
    assert {"icij", "opencorporates", "wikidata", "rss"} <= adapters
    kinds = {spec.kind for spec in SOURCE_REGISTRY}
    assert kinds == {SourceType.STRUCTURED, SourceType.UNSTRUCTURED}


def test_registry_weights_follow_the_spec():
    for spec in SOURCE_REGISTRY:
        family = 1.0 if spec.kind is SourceType.STRUCTURED else 0.4
        expected = family if spec.confidence_override is None else spec.confidence_override
        assert spec.confidence == pytest.approx(expected), spec.id


def test_bundled_sources_yaml_is_valid_and_applies(tmp_path, monkeypatch):
    """The shipped overlay must parse and actually change the registry."""
    settings = load_settings(dict(BASE))
    path = settings.sources_file_path()
    assert path is not None, "config/sources.yaml must ship with the repo"

    resolved = {spec.id: spec for spec in resolve_source_specs(settings, SOURCE_REGISTRY)}
    assert "icij_leaks" in resolved
    assert resolved["icij_leaks"].options["search_terms"], "overlay should inject query terms"
    # companies_house is disabled in the shipped overlay (needs an API key)
    assert "companies_house" not in resolved


def test_overlay_can_disable_and_retune_a_source(tmp_path):
    config = tmp_path / "sources.yaml"
    config.write_text(
        """
version: 1
sources:
  - id: wikidata
    enabled: false
  - id: news_world
    rate_per_sec: 2.5
    burst: 9
    feeds:
      - https://example.test/rss
""",
        encoding="utf-8",
    )
    settings = load_settings(dict(BASE, SOURCES_FILE=str(config)))
    resolved = {spec.id: spec for spec in resolve_source_specs(settings, SOURCE_REGISTRY)}

    assert "wikidata" not in resolved
    news = resolved["news_world"]
    assert news.rate_per_sec == pytest.approx(2.5)
    assert news.burst == 9
    assert news.options["feeds"] == ["https://example.test/rss"]


def test_overlay_can_retype_a_source(tmp_path):
    config = tmp_path / "sources.yaml"
    config.write_text("sources:\n  - id: news_world\n    kind: structured\n", encoding="utf-8")
    settings = load_settings(dict(BASE, SOURCES_FILE=str(config)))
    resolved = {spec.id: spec for spec in resolve_source_specs(settings, SOURCE_REGISTRY)}
    assert resolved["news_world"].kind is SourceType.STRUCTURED
    assert resolved["news_world"].confidence == 1.0


def test_broken_overlay_is_ignored_not_fatal(tmp_path):
    config = tmp_path / "sources.yaml"
    config.write_text("sources: [ this is not valid yaml", encoding="utf-8")
    settings = load_settings(dict(BASE, SOURCES_FILE=str(config)))
    # Falls back to the code registry instead of crashing the daily run.
    assert len(resolve_source_specs(settings, SOURCE_REGISTRY)) >= 5


def test_missing_overlay_file_is_fine(tmp_path):
    settings = load_settings(dict(BASE, SOURCES_FILE=str(tmp_path / "nope.yaml")))
    assert settings.sources_file_path() is None
    assert len(resolve_source_specs(settings, SOURCE_REGISTRY)) == len(
        [spec for spec in SOURCE_REGISTRY if spec.enabled]
    )


# --------------------------------------------------------------------------- #
# Enable / disable filters
# --------------------------------------------------------------------------- #
def make_spec(source_id: str, *, enabled: bool = True) -> SourceSpec:
    return SourceSpec(id=source_id, name=source_id, kind=SourceType.UNSTRUCTURED, adapter="rss", enabled=enabled)


def test_allowlist_filter():
    settings = load_settings(dict(BASE, SOURCES_FILE="/nonexistent.yaml", ENABLED_SOURCES="a,b"))
    assert settings.source_enabled(make_spec("a")) is True
    assert settings.source_enabled(make_spec("c")) is False


def test_denylist_filter():
    settings = load_settings(dict(BASE, SOURCES_FILE="/nonexistent.yaml", DISABLED_SOURCES="b"))
    assert settings.source_enabled(make_spec("a")) is True
    assert settings.source_enabled(make_spec("b")) is False


def test_spec_level_disable_wins():
    settings = load_settings(dict(BASE, SOURCES_FILE="/nonexistent.yaml", ENABLED_SOURCES="a"))
    assert settings.source_enabled(make_spec("a", enabled=False)) is False


def test_per_source_document_cap_is_applied():
    settings = load_settings(dict(BASE, SOURCES_FILE="/nonexistent.yaml", MAX_DOCUMENTS_PER_SOURCE="10"))
    registry = [SourceSpec(id="x", name="X", kind=SourceType.UNSTRUCTURED, adapter="rss", max_documents=500)]
    assert resolve_source_specs(settings, registry)[0].max_documents == 10


def test_settings_dataclass_defaults_are_complete():
    settings = Settings()
    assert settings.neo4j_database == "neo4j"
    assert settings.token_bucket_rate_per_sec > 0
    assert settings.min_edge_confidence >= 0


def test_load_settings_accepts_os_environ_itself(monkeypatch):
    """Regression: ``load_settings(os.environ)`` used to clear the very mapping
    it was asked to read, so every live value was silently dropped."""
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("MAX_DOCUMENTS_TOTAL", "42")

    settings = load_settings(os.environ)

    assert settings.dry_run is True
    assert settings.max_documents_total == 42
    # The caller's environment must survive the call untouched.
    assert os.environ["MAX_DOCUMENTS_TOTAL"] == "42"


def test_shipped_overlay_keeps_the_wikidata_domain_queries():
    """config/sources.yaml overrides the registry defaults, so a trimmed
    ``queries`` list here would silently switch foundation trustees off."""
    settings = load_settings(dict(BASE, DRY_RUN="true", SOURCES_FILE=str(REPO_ROOT / "config" / "sources.yaml")))
    specs = {spec.id: spec for spec in resolve_source_specs(settings, SOURCE_REGISTRY)}

    wikidata = specs["wikidata"]
    assert "foundation_trustees" in wikidata.options["queries"]
    assert "board_members" in wikidata.options["queries"]
    assert wikidata.options["link_shared_organizations"] is True
    assert wikidata.options["max_shared_org_members"] == 24
    assert wikidata.options["max_pairs_per_org"] == 12

    # The aviation sources the ADS-B work added, with their confidence ladder.
    assert specs["faa_registry"].confidence_override == 1.0
    assert specs["adsb_exchange"].confidence_override == 0.8
    assert specs["flight_logs"].confidence_override == 0.8
    assert specs["flight_logs"].options["link_copassengers"] is True
