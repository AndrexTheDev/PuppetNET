"""CLI contract tests for ``ingest.py`` (the workflow's entry point).

The script is loaded by path rather than imported, because it lives at the
repository root and is not part of the ``puppetnet`` package. Every test runs
offline: the source registry and adapter table are stubbed, Neo4j stays in
dry-run, and no spaCy model is required.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from puppetnet.config import ConfigError, load_settings
from puppetnet.models import IngestStats, SourceSpec, SourceType
from puppetnet.pipeline import IngestPipeline, PipelineOptions
from puppetnet.sources import ADAPTERS, SourceAdapter

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_ingest():
    spec = importlib.util.spec_from_file_location("puppetnet_ingest_cli", REPO_ROOT / "ingest.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ingest = _load_ingest()


# --------------------------------------------------------------------------- #
# Doubles / fixtures
# --------------------------------------------------------------------------- #


class StubAdapter(SourceAdapter):
    adapter_name = "stub"

    def harvest(self):
        error = self.spec.options.get("error")
        if error is not None:
            raise error
        yield from self.spec.options.get("documents", ())


@pytest.fixture(autouse=True)
def _stub_adapter(monkeypatch):
    monkeypatch.setitem(ADAPTERS, "stub", StubAdapter)


def stub_spec(source_id: str, kind: SourceType = SourceType.UNSTRUCTURED, **options) -> SourceSpec:
    return SourceSpec(
        id=source_id,
        name=source_id.title(),
        kind=kind,
        adapter="stub",
        base_url="https://example.test",
        max_documents=5,
        options=options,
    )


@pytest.fixture()
def cli_env(monkeypatch, tmp_path):
    """Environment for ``main()``: dry-run, tmp dirs, no credentials."""
    values = {
        "DRY_RUN": "true",
        "LOG_LEVEL": "ERROR",
        "SPACY_MODEL": "",
        "NEO4J_URI": "",
        "NEO4J_USER": "",
        "NEO4J_PASSWORD": "",
        "EDGE_WORKER_URL": "",
        "EDGE_WORKER_TOKEN": "",
        "PROXY_WORKER_URL": "",
        "PROXY_AUTH_TOKEN": "",
        "STATE_DIR": str(tmp_path / "state"),
        "REPORT_DIR": str(tmp_path / "reports"),
        "SOURCES_FILE": str(tmp_path / "no-sources.yaml"),
        "RUN_ID": "run-cli-0001",
        "MAX_DOCUMENTS_TOTAL": "20",
        "MAX_RUNTIME_SECONDS": "600",
        "GITHUB_STEP_SUMMARY": "",
        "GITHUB_OUTPUT": "",
    }
    for key, value in values.items():
        if value:
            monkeypatch.setenv(key, value)
        else:
            monkeypatch.delenv(key, raising=False)
    return values


@pytest.fixture()
def registry(monkeypatch):
    """Replace the pipeline's source registry with stub specs."""

    def _install(*specs: SourceSpec) -> None:
        import puppetnet.pipeline as pipeline_module

        monkeypatch.setattr(pipeline_module, "SOURCE_REGISTRY", tuple(specs))

    return _install


def args(**overrides):
    parsed = ingest.build_parser().parse_args([])
    for key, value in overrides.items():
        setattr(parsed, key.replace("-", "_"), value)
    return parsed


def settings_for(cli_env, **overrides):
    settings = load_settings()
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #


def test_parser_defaults_are_inert():
    parsed = ingest.build_parser().parse_args([])
    assert parsed.sources == ""
    assert parsed.limit is None
    assert parsed.dry_run is False
    assert parsed.skip_nlp is False
    assert parsed.skip_graph is False
    assert parsed.fail_on_error is False
    assert parsed.no_report is False
    assert parsed.doctor is False
    assert parsed.list_sources is False


def test_parser_accepts_every_documented_flag():
    parsed = ingest.build_parser().parse_args(
        [
            "--sources", "icij_leaks,wikidata",
            "--limit", "7",
            "--dry-run", "--skip-nlp", "--skip-graph", "--fail-on-error",
            "--log-level", "debug", "--log-json",
            "--report-dir", "/tmp/reports", "--run-id", "run-x",
            "--max-runtime", "120", "--no-report",
        ]
    )
    assert parsed.sources == "icij_leaks,wikidata"
    assert parsed.limit == 7
    assert parsed.log_level == "debug"
    assert parsed.max_runtime == 120
    assert parsed.no_report is True


def test_parser_rejects_a_non_integer_limit():
    with pytest.raises(SystemExit):
        ingest.build_parser().parse_args(["--limit", "many"])


def test_options_from_args_splits_and_normalises_sources():
    options = ingest.options_from_args(args(sources=" ICIJ_Leaks ; wikidata ,, "))
    assert options.sources == ["icij_leaks", "wikidata"]


def test_options_from_args_leaves_dry_run_unset_unless_requested():
    """``dry_run=None`` means "the environment decides" — the CLI must not
    silently force a real run into dry-run mode, nor the reverse."""
    assert ingest.options_from_args(args()).dry_run is None
    assert ingest.options_from_args(args(dry_run=True)).dry_run is True
    assert ingest.options_from_args(args()).fail_on_error is None
    assert ingest.options_from_args(args(fail_on_error=True)).fail_on_error is True


def test_options_from_args_honours_no_report_and_report_dir():
    options = ingest.options_from_args(args(no_report=True, report_dir="/tmp/x", limit=3))
    assert options.write_report is False
    assert options.report_dir == "/tmp/x"
    assert options.limit_per_source == 3


def test_apply_cli_overrides_updates_settings(cli_env):
    settings = settings_for({}, log_level="INFO", dry_run=False, report_dir="reports", run_id="", max_runtime_seconds=2100)
    updated = ingest.apply_cli_overrides(
        settings,
        args(log_level="debug", log_json=True, dry_run=True, report_dir="/tmp/r", run_id="run-9", max_runtime=30, fail_on_error=True),
    )
    assert updated.log_level == "DEBUG"
    assert updated.log_json is True
    assert updated.dry_run is True
    assert updated.report_dir == "/tmp/r"
    assert updated.run_id == "run-9"
    assert updated.max_runtime_seconds == 60, "the floor keeps the budget sane"
    assert updated.fail_on_error is True


def test_apply_cli_overrides_is_a_no_op_without_flags(cli_env):
    settings = settings_for({})
    before = (settings.log_level, settings.dry_run, settings.max_runtime_seconds)
    ingest.apply_cli_overrides(settings, args())
    assert (settings.log_level, settings.dry_run, settings.max_runtime_seconds) == before


# --------------------------------------------------------------------------- #
# Informational commands
# --------------------------------------------------------------------------- #


def test_list_sources_prints_the_registry(cli_env, capsys):
    assert ingest.command_list_sources() == ingest.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == len(payload["sources"])
    assert payload["count"] > 0
    first = payload["sources"][0]
    assert {"id", "adapter", "kind", "confidence"} <= set(first)


def test_print_config_redacts_secrets(cli_env, capsys, monkeypatch):
    monkeypatch.setenv("NEO4J_PASSWORD", "super-secret-value")
    monkeypatch.setenv("PROXY_AUTH_TOKEN", "worker-token-value")
    settings = load_settings()
    assert ingest.command_print_config(settings) == ingest.EXIT_OK

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert "super-secret-value" not in output
    assert "worker-token-value" not in output
    assert payload["neo4j_password"] == "***redacted***"
    assert payload["worker_token"] == "***redacted***"
    assert payload["dry_run"] is True


def test_version_flag(cli_env, capsys):
    from puppetnet import __version__

    assert ingest.main(["--version"]) == ingest.EXIT_OK
    assert __version__ in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


def test_doctor_passes_in_dry_run_without_credentials(cli_env, capsys):
    settings = settings_for(cli_env)
    assert ingest.command_doctor(settings) == ingest.EXIT_OK

    output = capsys.readouterr().out
    for component in ("config", "edge-relay", "neo4j", "nlp", "credentials", "filesystem"):
        assert f"[PASS] {component}" in output, output
    assert "6/6 checks passed" in output


def test_doctor_reports_the_blank_nlp_backend(cli_env, capsys):
    settings = settings_for(cli_env)
    ingest.command_doctor(settings)
    output = capsys.readouterr().out
    assert "backend=spacy:blank" in output


def test_doctor_fails_when_nothing_can_fetch_or_write(cli_env, capsys):
    settings = settings_for(
        cli_env,
        dry_run=False,
        neo4j_uri="",
        neo4j_password="",
        direct_fallback_enabled=False,
        worker_url="",
        worker_token="",
    )
    assert ingest.command_doctor(settings) == ingest.EXIT_CONFIG

    output = capsys.readouterr().out
    assert "[FAIL] config" in output
    assert "[FAIL] edge-relay" in output
    assert "NOTHING CAN FETCH" in output
    assert "[FAIL] neo4j" in output


def test_doctor_reports_an_unwritable_report_directory(cli_env, capsys, tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    settings = settings_for(cli_env, report_dir=str(blocked / "reports"))
    assert ingest.command_doctor(settings) == ingest.EXIT_CONFIG
    assert "[FAIL] filesystem" in capsys.readouterr().out


def test_probe_neo4j_dry_run_needs_no_server(cli_env):
    ok, detail = ingest._probe_neo4j(settings_for(cli_env))
    assert ok is True
    assert "dry-run" in detail


def test_probe_neo4j_reports_an_unreachable_server(cli_env):
    settings = settings_for(cli_env, dry_run=False, neo4j_uri="neo4j+s://127.0.0.1:1", neo4j_password="x", neo4j_max_retries=0)
    ok, detail = ingest._probe_neo4j(settings)
    assert ok is False
    assert detail


def test_probe_worker_reports_an_unreachable_relay(cli_env, monkeypatch):
    import requests

    def refuse(*_args, **_kwargs):
        raise requests.exceptions.ConnectionError("no route to host")

    monkeypatch.setattr(requests, "get", refuse)
    settings = settings_for(cli_env, worker_url="https://relay.example.test", worker_token="t", worker_enabled=True)
    ok, detail = ingest._probe_worker(settings)
    assert ok is False
    assert "unreachable" in detail


def test_probe_worker_reports_bindings_when_healthy(cli_env, monkeypatch):
    import requests

    class Response:
        ok = True
        status_code = 200
        content = b"{}"

        @staticmethod
        def json():
            return {
                "worker": "puppetnet-relay",
                "version": "1.4.0",
                "colo": "CDG",
                "bindings": {
                    "rate_limit_kv": "RATE_LIMIT_KV",
                    "result_kv": "RESULT_KV",
                    "queue": "FETCH_QUEUE",
                    "auth_configured": True,
                },
            }

    monkeypatch.setattr(requests, "get", lambda *a, **k: Response())
    settings = settings_for(cli_env, worker_url="https://relay.example.test/", worker_token="t", worker_enabled=True)
    ok, detail = ingest._probe_worker(settings)
    assert ok is True
    assert "https://relay.example.test/health" in detail
    assert "colo=CDG" in detail and "auth_configured=True" in detail


def test_probe_spacy_degrades_to_the_blank_backend(cli_env):
    ok, detail = ingest._probe_spacy(settings_for(cli_env))
    assert ok is True
    assert "backend=" in detail and "parser=False" in detail


def test_credential_summary_lists_present_and_missing(cli_env, monkeypatch):
    monkeypatch.setenv("OPENCORPORATES_API_TOKEN", "oc-token")
    settings = load_settings()
    summary = ingest._credential_summary(settings)
    assert "OPENCORPORATES_API_TOKEN" in summary.split("|")[0]
    assert "COMPANIES_HOUSE_API_KEY" in summary.split("missing:")[1]


# --------------------------------------------------------------------------- #
# main()
# --------------------------------------------------------------------------- #


def test_main_list_sources_exits_zero(cli_env, capsys):
    assert ingest.main(["--list-sources"]) == ingest.EXIT_OK
    assert json.loads(capsys.readouterr().out)["count"] > 0


def test_main_print_config_exits_zero(cli_env, capsys):
    assert ingest.main(["--print-config"]) == ingest.EXIT_OK
    assert json.loads(capsys.readouterr().out)["dry_run"] is True


def test_main_doctor_exits_zero(cli_env):
    assert ingest.main(["--doctor"]) == ingest.EXIT_OK


def test_main_runs_a_dry_run_harvest(cli_env, registry, capsys):
    from puppetnet.models import Document

    registry(
        stub_spec(
            "news_world",
            documents=[Document(doc_id="", source_id="news_world", url="https://example.test/a", text="Gazprom owns Nord Stream AG.")],
        )
    )
    code = ingest.main(["--dry-run", "--limit", "5"])
    output = capsys.readouterr().out

    assert code == ingest.EXIT_OK
    assert "RUN SUMMARY" in output
    assert "Documents" in output
    assert (Path(cli_env["REPORT_DIR"]) / "run-cli-0001.json").exists()


def test_a_model_less_environment_is_not_an_error(cli_env, registry, capsys):
    """No spaCy model is installed here; the run must still be clean.

    The blank+gazetteer backend is the documented degraded mode, so it may not
    poison ``stats.errors`` — that would flip the exit code and emit GitHub
    warnings on every scheduled run.
    """
    from puppetnet.models import Document

    registry(
        stub_spec(
            "news_world",
            documents=[Document(doc_id="", source_id="news_world", url="https://example.test/a", text="Gazprom owns Nord Stream AG.")],
        )
    )
    code = ingest.main(["--dry-run"])
    output = capsys.readouterr().out
    assert code == ingest.EXIT_OK
    assert "::warning" not in output
    assert "error(s) recorded" not in output

    report = json.loads((Path(cli_env["REPORT_DIR"]) / "run-cli-0001.json").read_text(encoding="utf-8"))
    assert report["nlp"]["backend"].startswith("spacy:blank"), report["nlp"]
    assert report["nlp"]["load_errors"], "the fallbacks must still be visible in the report"
    assert report["stats"]["errors"] == []


def test_main_honours_no_report(cli_env, registry):
    registry(stub_spec("news_world", documents=[]))
    assert ingest.main(["--dry-run", "--no-report"]) == ingest.EXIT_OK
    assert not (Path(cli_env["REPORT_DIR"]) / "run-cli-0001.json").exists()


def test_main_returns_runtime_error_when_a_run_raises(cli_env, registry, monkeypatch):
    class ExplodingPipeline(IngestPipeline):
        def run(self):
            raise RuntimeError("harvest exploded")

    monkeypatch.setattr(ingest, "IngestPipeline", ExplodingPipeline)
    registry(stub_spec("news_world"))
    assert ingest.main(["--dry-run"]) == ingest.EXIT_RUNTIME


def test_main_returns_config_error_when_the_pipeline_cannot_be_built(cli_env, registry, monkeypatch):
    class BrokenPipeline(IngestPipeline):
        def __init__(self, *a, **k):
            raise ValueError("bad configuration")

    monkeypatch.setattr(ingest, "IngestPipeline", BrokenPipeline)
    registry(stub_spec("news_world"))
    assert ingest.main(["--dry-run"]) == ingest.EXIT_CONFIG


def test_main_returns_config_error_when_settings_are_invalid(cli_env, monkeypatch):
    def boom(*_a, **_k):
        raise ConfigError("NEO4J_PASSWORD is required")

    monkeypatch.setattr(ingest, "load_settings", boom)
    assert ingest.main(["--dry-run"]) == ingest.EXIT_CONFIG


def test_main_returns_partial_when_fail_on_error_trips(cli_env, registry, monkeypatch):
    class FailingPipeline(IngestPipeline):
        def run(self):
            self.stats.errors.append("[broken] upstream 500")
            self.stats.documents_fetched = 1
            raise SystemExit("run finished with 1 recorded error(s)")

    monkeypatch.setattr(ingest, "IngestPipeline", FailingPipeline)
    registry(stub_spec("news_world"))
    assert ingest.main(["--dry-run", "--fail-on-error"]) == ingest.EXIT_PARTIAL


def test_main_returns_runtime_error_when_nothing_was_harvested(cli_env, registry, monkeypatch):
    class EmptyFailingPipeline(IngestPipeline):
        def run(self):
            self.stats.record_error("pipeline", "every source failed")
            return self.stats

    monkeypatch.setattr(ingest, "IngestPipeline", EmptyFailingPipeline)
    registry(stub_spec("news_world"))
    assert ingest.main(["--dry-run"]) == ingest.EXIT_RUNTIME


def test_main_returns_ok_for_an_empty_but_healthy_run(cli_env, registry):
    registry()  # no sources at all
    # An empty registry records "no enabled sources", which is a hard failure.
    assert ingest.main(["--dry-run"]) == ingest.EXIT_RUNTIME


def test_main_passes_options_through_to_the_pipeline(cli_env, registry, monkeypatch):
    captured: dict = {}

    class RecordingPipeline(IngestPipeline):
        def __init__(self, settings=None, options=None):
            captured["options"] = options
            captured["settings"] = settings
            super().__init__(settings=settings, options=options)

    monkeypatch.setattr(ingest, "IngestPipeline", RecordingPipeline)
    registry(stub_spec("news_world", documents=[]))
    ingest.main(["--dry-run", "--sources", "news_world", "--limit", "4", "--skip-nlp", "--report-dir", cli_env["REPORT_DIR"]])

    options = captured["options"]
    assert isinstance(options, PipelineOptions)
    assert options.sources == ["news_world"]
    assert options.limit_per_source == 4
    assert options.dry_run is True
    assert options.skip_nlp is True
    assert captured["settings"].report_dir == cli_env["REPORT_DIR"]


def test_github_notice_is_emitted_for_degraded_runs(cli_env, tmp_path, monkeypatch, capsys):
    output_file = tmp_path / "gh_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
    stats = IngestStats()
    stats.record_error("news_world", "upstream 500")

    ingest._maybe_emit_github_notice(stats)

    assert output_file.read_text(encoding="utf-8").strip() == "status=degraded"
    assert "::warning title=PuppetNET ingest::" in capsys.readouterr().out


def test_github_notice_reports_ok_for_a_clean_run(cli_env, tmp_path, monkeypatch, capsys):
    output_file = tmp_path / "gh_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    ingest._maybe_emit_github_notice(IngestStats())

    assert output_file.read_text(encoding="utf-8").strip() == "status=ok"
    assert "::warning" not in capsys.readouterr().out


def test_final_summary_prints_the_markdown_report(cli_env, capsys):
    stats = IngestStats(run_id="run-1", documents_fetched=3, entities_written=12, relations_written=5)
    ingest._print_final_summary(stats, 1.234)
    output = capsys.readouterr().out
    assert "RUN SUMMARY" in output
    assert "**Documents**: 3 fetched" in output
    assert "Wall clock: 1.2s" in output


def test_final_summary_flags_recorded_errors(cli_env, capsys):
    stats = IngestStats()
    stats.record_error("src", "boom")
    ingest._print_final_summary(stats, 0.1)
    assert "1 error(s) recorded" in capsys.readouterr().out


def test_exit_codes_are_distinct():
    codes = {ingest.EXIT_OK, ingest.EXIT_CONFIG, ingest.EXIT_RUNTIME, ingest.EXIT_PARTIAL}
    assert codes == {0, 1, 2, 3}
