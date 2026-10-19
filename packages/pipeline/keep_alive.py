#!/usr/bin/env python3
"""AuraDB Free keep-alive — one ``RETURN 1``, every day, forever.

AuraDB Free pauses an instance after **72 hours without a connection**. A paused
instance is not lost — it wakes on the next connect — but the wake-up costs tens
of seconds and the scheduled ingestion run that hits it looks like a timeout.
The cheapest fix is a heartbeat: the workflow pings the database daily with the
smallest possible query.

Design constraints, all deliberate:

* **Read-only.** The ping is ``RETURN 1 AS ok`` — no node, no relationship, no
  property, no transaction that could touch the 200 000-node budget. A heartbeat
  that writes is a heartbeat that eventually fills the database.
* **Short timeout, few retries.** A keep-alive that hangs for five minutes is a
  bad keep-alive. The defaults are 30 s and two retries, both configurable.
* **No credentials in logs.** The URI is printed without its credentials (the
  driver never puts them in the URI anyway) and the password never leaves the
  settings object.
* **Wake-up is reported.** A first query that takes 20 s almost certainly woke a
  paused instance; the report says so instead of hiding the latency in a log
  line.

Usage
-----
::

    python keep_alive.py                  # one ping (the scheduled job)
    python keep_alive.py --json           # machine-readable result
    python keep_alive.py --loop --interval-minutes 360   # long-running host

Exit codes: ``0`` the database answered · ``1`` configuration ·
``2`` the database did not answer (after retries).
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - import shim
    sys.path.insert(0, str(REPO_ROOT))

from puppetnet.config import ConfigError, Settings, load_settings  # noqa: E402
from puppetnet.graph.neo4j_client import Neo4jClient, Neo4jUnavailable  # noqa: E402
from puppetnet.logging_utils import configure_logging, get_logger  # noqa: E402
from puppetnet.models import iso, utcnow  # noqa: E402

logger = get_logger("graph.keep_alive")

__all__ = ["PING", "KeepAliveResult", "ping_once", "main"]

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_RUNTIME = 2

#: The smallest query Neo4j accepts. No label, no property, no write.
PING = "RETURN 1 AS ok"

#: A first response slower than this is reported as a probable wake-up. Aura's
#: pause/resume is server-side, so the only client-visible symptom is latency.
WAKE_THRESHOLD_SECONDS = 5.0


@dataclass
class KeepAliveResult:
    ok: bool
    checked_at: str
    latency_ms: float = 0.0
    woke_from_pause: bool = False
    database: str = ""
    server: str = ""
    attempts: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _build_client(settings: Settings, *, timeout: float | None = None, retries: int | None = None) -> Neo4jClient:
    """A client with the keep-alive's short timeout, not the harvest's."""
    tuned = dataclasses.replace(
        settings,
        neo4j_connection_timeout_seconds=float(timeout if timeout is not None else getattr(settings, "keep_alive_timeout_seconds", 30.0)),
        neo4j_max_retries=int(retries if retries is not None else getattr(settings, "keep_alive_retries", 2)),
    )
    return Neo4jClient(tuned)


def ping_once(client: Any, *, clock: Callable[[], float] = time.perf_counter) -> KeepAliveResult:
    """Run one ``RETURN 1`` and describe what happened.

    ``client`` is anything with ``read``/``database`` (a real
    :class:`Neo4jClient` or a double). A wrong answer — no row, a row without
    ``ok == 1`` — is a failure even though the connection succeeded: it means
    the ping reached something that is not the graph.
    """
    result = KeepAliveResult(
        ok=False,
        checked_at=iso(utcnow()) or "",
        database=str(getattr(client, "database", "") or ""),
        attempts=1,
    )
    started = clock()
    try:
        rows = client.read(PING)
    except Exception as exc:  # noqa: BLE001 - every driver failure is a failed ping
        result.latency_ms = round((clock() - started) * 1000, 1)
        result.error = f"{exc.__class__.__name__}: {exc}"
        logger.error("keep-alive failed: %s", result.error)
        return result
    result.latency_ms = round((clock() - started) * 1000, 1)
    row = (rows or [{}])[0] if rows else {}
    if int(row.get("ok") or 0) != 1:
        result.error = f"unexpected response: {row!r}"
        logger.error("keep-alive got an unexpected answer: %r", row)
        return result
    result.ok = True
    result.woke_from_pause = result.latency_ms >= WAKE_THRESHOLD_SECONDS * 1000
    if result.woke_from_pause:
        logger.warning("keep-alive took %.1fs — the instance was probably paused and has been resumed", result.latency_ms / 1000)
    else:
        logger.info("keep-alive ok in %.0f ms (database=%s)", result.latency_ms, result.database or "?")
    return result


def _describe(client: Any) -> None:
    """Log the negotiated server version without opening a second connection."""
    try:
        rows = client.read("CALL dbms.components() YIELD name, versions, edition RETURN name, versions[0] AS version, edition")
        if rows:
            row = rows[0]
            logger.info("connected to %s %s (%s)", row.get("name") or "Neo4j", row.get("version") or "?", row.get("edition") or "?")
    except Exception:  # noqa: BLE001 - informational only; the ping already succeeded
        logger.debug("server version probe failed", exc_info=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="keep_alive.py",
        description="Keep an AuraDB Free instance awake with a minimal RETURN 1 ping.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit codes: 0 answered · 1 configuration · 2 no answer after retries.",
    )
    parser.add_argument("--once", action="store_true", help="Ping once and exit (default).")
    parser.add_argument("--loop", action="store_true", help="Keep pinging on a fixed interval (for a long-running host).")
    parser.add_argument("--interval-minutes", type=float, default=360.0, help="Minutes between pings with --loop (default 360 = 6 h).")
    parser.add_argument("--timeout", type=float, default=None, help="Connection timeout in seconds (default KEEP_ALIVE_TIMEOUT_SECONDS).")
    parser.add_argument("--retries", type=int, default=None, help="Retry count (default KEEP_ALIVE_RETRIES).")
    parser.add_argument("--json", action="store_true", help="Print the result as JSON.")
    parser.add_argument("--log-level", default="", help="DEBUG | INFO | WARNING | ERROR.")
    parser.add_argument("--log-json", action="store_true", help="Emit structured JSON logs.")
    parser.add_argument("--version", action="store_true", help="Print version information and exit.")
    return parser


def _emit(result: KeepAliveResult, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
    else:
        print(
            f"keep-alive {'ok' if result.ok else 'FAILED'} in {result.latency_ms:.0f} ms"
            f" (database={result.database or '?'}{', resumed from pause' if result.woke_from_pause else ''})"
        )
    emit_github_output(result)


def emit_github_output(result: KeepAliveResult) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"keep_alive_ok={str(result.ok).lower()}\n")
            handle.write(f"keep_alive_latency_ms={result.latency_ms}\n")
            handle.write(f"keep_alive_woke={str(result.woke_from_pause).lower()}\n")
    except OSError as exc:
        logger.warning("could not write GITHUB_OUTPUT: %s", exc)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.version:
        from puppetnet import __version__  # noqa: PLC0415

        print(f"PuppetNET keep-alive {__version__} (python {sys.version.split()[0]})")
        return EXIT_OK

    env_overrides: dict[str, str] = {}
    if args.log_level:
        env_overrides["LOG_LEVEL"] = args.log_level.upper()
    if args.log_json:
        env_overrides["LOG_JSON"] = "true"
    try:
        settings = load_settings({**dict(os.environ), **env_overrides})
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if bool(getattr(settings, "dry_run", False)):
        # A dry run opens no socket, so it cannot reset the inactivity clock.
        # Refusing is the only way to keep the job's green check meaningful.
        print("DRY_RUN=true would not contact the database; set DRY_RUN=false for keep-alive", file=sys.stderr)
        return EXIT_CONFIG

    configure_logging(settings.log_level, settings.log_json, stream=sys.stderr if args.json else None)
    client = _build_client(settings, timeout=args.timeout, retries=args.retries)
    interval = max(60.0, float(args.interval_minutes) * 60.0)

    try:
        if not args.loop:
            result = ping_once(client)
            _describe(client)
            _emit(result, args.json)
            return EXIT_OK if result.ok else EXIT_RUNTIME
        while True:
            result = ping_once(client)
            _emit(result, args.json)
            if not result.ok:
                # One failed ping in a loop is not fatal: the next one may be
                # the one that wakes a sleeping instance. Keep the process alive
                # but make the failure obvious in the log.
                logger.warning("keep-alive iteration failed — retrying in %.0f minutes", interval / 60)
            time.sleep(interval)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("interrupted", file=sys.stderr)
        return EXIT_RUNTIME
    except Neo4jUnavailable as exc:
        print(f"Neo4j unavailable: {exc}", file=sys.stderr)
        return EXIT_RUNTIME
    finally:
        with contextlib.suppress(Exception):
            client.close()


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
