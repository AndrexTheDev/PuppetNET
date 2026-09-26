#!/usr/bin/env python3
"""PuppetNET daily data harvesting + NLP parsing engine — entry point.

Executed by ``.github/workflows/daily_ingest.yml`` on a cron schedule (once per
day) and equally usable by hand::

    python ingest.py --dry-run --sources news_world,aviation_news --limit 10
    python ingest.py --sources icij_leaks,opencorporates,wikidata
    python ingest.py --doctor          # validate config + connectivity, exit
    python ingest.py --list-sources    # print the source registry as JSON

Exit codes
----------
``0`` success · ``1`` configuration/deploy error · ``2`` runtime failure ·
``3`` partial success (some sources failed but the graph was written).

Environment
-----------
All configuration comes from the environment (see ``.env.example``):
``NEO4J_URI``, ``NEO4J_USERNAME``, ``NEO4J_PASSWORD``, ``PROXY_WORKER_URL``,
``PROXY_AUTH_TOKEN``, ``OPENCORPORATES_API_TOKEN``, ``COMPANIES_HOUSE_API_KEY``,
``WIKIDATA_USER_AGENT``, ``SPACY_MODELS``, ``ENABLED_SOURCES``, ``DRY_RUN`` …
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from collections.abc import Sequence
from pathlib import Path
from typing import Any

# Allow running from a source checkout without installation.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from puppetnet.config import ConfigError, Settings, load_settings  # noqa: E402
from puppetnet.logging_utils import banner, configure_logging, get_logger  # noqa: E402
from puppetnet.models import IngestStats  # noqa: E402
from puppetnet.pipeline import IngestPipeline, PipelineOptions  # noqa: E402
from puppetnet.sources.registry import describe_registry  # noqa: E402

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_RUNTIME = 2
EXIT_PARTIAL = 3

logger = get_logger("ingest")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ingest.py",
        description="PuppetNET serverless OSINT harvester: fetch → parse → NLP → weighted graph writes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python ingest.py --dry-run --limit 5 --sources news_world\n"
            "  python ingest.py --sources icij_leaks,opencorporates,wikidata\n"
            "  python ingest.py --doctor\n"
            "  python ingest.py --list-sources\n"
        ),
    )
    parser.add_argument("--sources", default="", help="Comma-separated source ids or adapter names (default: all enabled).")
    parser.add_argument("--limit", type=int, default=None, help="Maximum documents per source for this run.")
    parser.add_argument("--dry-run", action="store_true", help="Harvest + parse but write nothing to Neo4j.")
    parser.add_argument("--skip-nlp", action="store_true", help="Skip spaCy entirely (structured sources only).")
    parser.add_argument("--skip-graph", action="store_true", help="Force the Neo4j client into dry-run mode.")
    parser.add_argument("--fail-on-error", action="store_true", help="Exit non-zero if any source recorded an error.")
    parser.add_argument("--log-level", default="", help="DEBUG | INFO | WARNING | ERROR (default: LOG_LEVEL env).")
    parser.add_argument("--log-json", action="store_true", help="Emit structured JSON logs (set automatically by LOG_JSON).")
    parser.add_argument("--report-dir", default="", help="Where to write the JSON/Markdown run report.")
    parser.add_argument("--run-id", default="", help="Explicit run id (default: generated).")
    parser.add_argument("--max-runtime", type=int, default=None, help="Hard wall-clock budget in seconds.")
    parser.add_argument("--no-report", action="store_true", help="Do not write report files.")
    parser.add_argument("--doctor", action="store_true", help="Validate configuration, connectivity and model availability, then exit.")
    parser.add_argument("--list-sources", action="store_true", help="Print the source registry as JSON and exit.")
    parser.add_argument("--print-config", action="store_true", help="Print the resolved (redacted) configuration and exit.")
    parser.add_argument("--version", action="store_true", help="Print version information and exit.")
    return parser


def options_from_args(args: argparse.Namespace) -> PipelineOptions:
    sources = [item.strip().lower() for item in args.sources.replace(";", ",").split(",") if item.strip()]
    return PipelineOptions(
        sources=sources,
        limit_per_source=args.limit,
        dry_run=True if args.dry_run else None,
        skip_nlp=args.skip_nlp,
        skip_graph=args.skip_graph,
        write_report=not args.no_report,
        report_dir=args.report_dir,
        fail_on_error=True if args.fail_on_error else None,
    )


def apply_cli_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    if args.log_level:
        settings.log_level = args.log_level.upper()
    if args.log_json:
        settings.log_json = True
    if args.dry_run:
        settings.dry_run = True
    if args.report_dir:
        settings.report_dir = args.report_dir
    if args.run_id:
        settings.run_id = args.run_id
    if args.max_runtime:
        settings.max_runtime_seconds = max(60, int(args.max_runtime))
    if args.fail_on_error:
        settings.fail_on_error = True
    return settings


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #


def command_list_sources() -> int:
    payload = describe_registry()
    print(json.dumps({"count": len(payload), "sources": payload}, indent=2, ensure_ascii=False))
    return EXIT_OK


def command_print_config(settings: Settings) -> int:
    print(json.dumps(settings.describe(), indent=2, ensure_ascii=False, default=str))
    return EXIT_OK


def command_doctor(settings: Settings) -> int:
    """Validate every external dependency without performing a harvest."""
    results: list[dict[str, Any]] = []

    def record(component: str, ok: bool, detail: str) -> None:
        results.append({"component": component, "ok": ok, "detail": detail})
        marker = "PASS" if ok else "FAIL"
        print(f"[{marker}] {component}: {detail}")

    # 1. Configuration
    try:
        settings.validate()
        record("config", True, "environment configuration is valid")
    except ConfigError as exc:
        record("config", False, str(exc).replace("\n", " | "))

    # 2. Edge relay
    if settings.worker_configured:
        ok, detail = _probe_worker(settings)
        record("edge-relay", ok, detail)
    else:
        record(
            "edge-relay",
            settings.direct_fallback_enabled,
            "not configured (PROXY_WORKER_URL/PROXY_AUTH_TOKEN missing)"
            + (" — direct token-bucket fallback enabled" if settings.direct_fallback_enabled else " — NOTHING CAN FETCH"),
        )

    # 3. Neo4j
    if settings.neo4j_configured or settings.dry_run:
        ok, detail = _probe_neo4j(settings)
        record("neo4j", ok, detail)
    else:
        record("neo4j", False, "NEO4J_URI/NEO4J_PASSWORD missing (use --dry-run to skip)")

    # 4. spaCy
    ok, detail = _probe_spacy(settings)
    record("nlp", ok, detail)

    # 5. Source credentials
    record("credentials", True, _credential_summary(settings))

    # 6. Writable paths
    try:
        settings.state_path.mkdir(parents=True, exist_ok=True)
        settings.report_path.mkdir(parents=True, exist_ok=True)
        probe = settings.report_path / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        record("filesystem", True, f"state={settings.state_path} reports={settings.report_path}")
    except OSError as exc:
        record("filesystem", False, f"cannot write: {exc}")

    failures = [item for item in results if not item["ok"]]
    print(banner(f"doctor: {len(results) - len(failures)}/{len(results)} checks passed"))
    return EXIT_OK if not failures else EXIT_CONFIG


def _probe_worker(settings: Settings) -> tuple[bool, str]:
    """Query ``/health`` on the Worker and report reachability + bindings."""
    import requests

    endpoint = f"{settings.worker_url.rstrip('/')}/health"
    try:
        response = requests.get(endpoint, timeout=15)
        payload = response.json() if response.content else {}
        bindings = payload.get("bindings", {})
        detail = (
            f"{endpoint} → HTTP {response.status_code} "
            f"worker={payload.get('worker')}@{payload.get('version')} colo={payload.get('colo')} "
            f"kv={bindings.get('rate_limit_kv')}/{bindings.get('result_kv')} queue={bindings.get('queue')} "
            f"auth_configured={bindings.get('auth_configured')}"
        )
        return response.ok, detail
    except Exception as exc:  # noqa: BLE001
        logger.debug("worker probe failed: %s", traceback.format_exc())
        return False, f"{endpoint} unreachable: {exc.__class__.__name__}: {exc}"


def _probe_neo4j(settings: Settings) -> tuple[bool, str]:
    from puppetnet.graph.neo4j_client import Neo4jClient, Neo4jUnavailable

    client = Neo4jClient(settings, dry_run=settings.dry_run and not settings.neo4j_password)
    try:
        client.verify()
        if settings.dry_run and not settings.neo4j_password:
            return True, "dry-run mode: writes will be recorded, not executed"
        version = "unknown"
        try:
            rows = client.read("CALL dbms.components() YIELD name, versions RETURN name, versions LIMIT 1")
            if rows:
                versions = rows[0].get("versions") or []
                version = f"{rows[0].get('name')} {versions[0] if versions else ''}".strip()
        except Exception as exc:  # noqa: BLE001
            version = f"connected (version probe failed: {exc.__class__.__name__})"
        return True, f"connected to {settings.neo4j_uri} ({version}, database={settings.neo4j_database})"
    except Neo4jUnavailable as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"connection failed: {exc.__class__.__name__}: {exc}"
    finally:
        client.close()


def _probe_spacy(settings: Settings) -> tuple[bool, str]:
    try:
        from puppetnet.parsing.nlp_engine import NLPEngine

        engine = NLPEngine(settings, eager_load=True)
        detail = f"backend={engine.backend} parser={engine.has_parser} ner={engine.has_ner}"
        if engine.load_errors:
            detail += f" | fallbacks: {'; '.join(engine.load_errors)[:220]}"
        usable = engine.available
        engine.close()
        return usable, detail
    except Exception as exc:  # noqa: BLE001
        return False, f"NLP engine failed to initialise: {exc.__class__.__name__}: {exc}"


def _credential_summary(settings: Settings) -> str:
    present = []
    missing = []
    for label, value in (
        ("OPENCORPORATES_API_TOKEN", settings.opencorporates_api_token),
        ("COMPANIES_HOUSE_API_KEY", settings.companies_house_api_key),
        ("WIKIDATA_USER_AGENT", settings.wikidata_user_agent),
        ("PROXY_AUTH_TOKEN", settings.worker_token),
        ("ICIJ_DATASET_URLS", settings.icij_dataset_urls),
        ("RSS_FEEDS", settings.rss_feeds),
    ):
        (present if value else missing).append(label)
    return f"present: {', '.join(present) or 'none'} | missing: {', '.join(missing) or 'none'}"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.version:
        from puppetnet import __version__

        print(f"PuppetNET ingest engine {__version__} (python {sys.version.split()[0]})")
        return EXIT_OK

    # Bootstrap logging early so config errors are visible.
    configure_logging(level=(args.log_level or os.environ.get("LOG_LEVEL", "INFO")), json_output=bool(args.log_json or os.environ.get("LOG_JSON", "").lower() in {"1", "true", "yes"}))

    if args.list_sources:
        return command_list_sources()

    try:
        settings = apply_cli_overrides(load_settings(), args)
    except ConfigError as exc:
        logger.error("configuration error: %s", exc)
        return EXIT_CONFIG

    configure_logging(level=settings.log_level, json_output=settings.log_json)

    if args.print_config:
        return command_print_config(settings)
    if args.doctor:
        return command_doctor(settings)

    started = time.perf_counter()
    options = options_from_args(args)
    logger.info(banner(f"PuppetNET daily ingest — {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}"))

    try:
        pipeline = IngestPipeline(settings=settings, options=options)
    except Exception as exc:  # noqa: BLE001
        logger.error("could not initialise the pipeline: %s", exc)
        logger.debug("%s", traceback.format_exc())
        return EXIT_CONFIG

    try:
        stats = pipeline.run()
    except SystemExit as exc:  # --fail-on-error path
        # SystemExit carries either an int status or a *message*; the pipeline
        # raises the latter, so it must not be fed to int().
        code = exc.code
        if code is None:
            return EXIT_PARTIAL
        if isinstance(code, int):
            return code
        logger.error("%s", code)
        return EXIT_PARTIAL
    except KeyboardInterrupt:  # pragma: no cover
        logger.warning("interrupted — exiting")
        return EXIT_RUNTIME
    except Exception as exc:  # noqa: BLE001
        logger.error("run failed: %s", exc)
        logger.debug("%s", traceback.format_exc())
        return EXIT_RUNTIME

    elapsed = time.perf_counter() - started
    _print_final_summary(stats, elapsed)
    _maybe_emit_github_notice(stats)

    if stats.errors and settings.fail_on_error:
        return EXIT_PARTIAL
    if stats.documents_fetched == 0 and stats.errors:
        return EXIT_RUNTIME
    return EXIT_OK


def _print_final_summary(stats: IngestStats, elapsed: float) -> None:
    print()
    print(banner("RUN SUMMARY"))
    print(stats.markdown_summary())
    print(f"\nWall clock: {elapsed:.1f}s")
    if stats.errors:
        print(f"\n{len(stats.errors)} error(s) recorded — see the report JSON for details.")


def _maybe_emit_github_notice(stats: IngestStats) -> None:
    """Surface hard failures as GitHub Actions annotations."""
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        try:
            with open(output_path, "a", encoding="utf-8") as handle:
                handle.write(f"status={'degraded' if stats.errors else 'ok'}\n")
        except OSError:
            pass
    if stats.errors:
        for error in stats.errors[:5]:
            print(f"::warning title=PuppetNET ingest::{error}")


if __name__ == "__main__":
    raise SystemExit(main())
