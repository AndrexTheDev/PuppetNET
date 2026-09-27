"""Thin, retry-aware Neo4j driver wrapper (AuraDB Free compatible).

Responsibilities
----------------
* Connection management with a bounded pool and explicit timeouts (the free
  tier sleeps idle instances, so the first query of a run often pays a wake-up
  penalty — ``verify_connectivity`` is retried rather than failing the run).
* Write transactions with exponential backoff on transient errors
  (``ServiceUnavailable``, ``TransientError``, ``SessionExpired``,
  ``ClientConnectorError``).
* Chunked ``UNWIND`` batch execution so a 50k-row run stays inside the free
  tier's memory and transaction limits.
* A ``dry_run`` mode that records every statement + parameter shape without
  touching the database — this is what makes the pipeline unit-testable and
  lets ``ingest.py --dry-run`` validate a full harvest safely.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..logging_utils import get_logger

__all__ = ["Neo4jClient", "Neo4jUnavailable", "DryRunRecorder", "chunked"]

logger = get_logger("graph.neo4j")

#: Error class *names* we retry on. Matching by name keeps this module
#: importable (and testable) when the ``neo4j`` package is not installed.
TRANSIENT_ERROR_NAMES = frozenset(
    {
        "ServiceUnavailable",
        "TransientError",
        "SessionExpired",
        "ClientConnectorError",
        "ReadTimeout",
        "WriteServiceUnavailable",
        "ConnectionResetError",
        "BrokenPipeError",
    }
)

FATAL_ERROR_NAMES = frozenset({"AuthenticationError", "ConfigurationError", "Forbidden", "DatabaseNotFound"})


class Neo4jUnavailable(RuntimeError):
    """Raised when the database cannot be reached after all retries."""


@dataclass
class DryRunRecorder:
    """Captures what *would* have been executed."""

    statements: list[dict[str, Any]] = field(default_factory=list)

    def record(self, query: str, params: dict[str, Any] | None = None, *, rows: int = 0, kind: str = "write") -> None:
        self.statements.append(
            {
                "kind": kind,
                "query_preview": " ".join(query.split())[:400],
                "params_keys": sorted((params or {}).keys()),
                "rows": rows,
            }
        )

    @property
    def total_rows(self) -> int:
        return sum(entry["rows"] for entry in self.statements)

    def summary(self) -> dict[str, Any]:
        by_kind: dict[str, int] = {}
        for entry in self.statements:
            by_kind[entry["kind"]] = by_kind.get(entry["kind"], 0) + 1
        return {"statements": len(self.statements), "rows": self.total_rows, "by_kind": by_kind}


def chunked(items: Sequence[Any] | Iterable[Any], size: int) -> Iterator[list[Any]]:
    """Yield lists of at most ``size`` items."""
    if size <= 0:
        size = 1
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _is_transient(exc: BaseException) -> bool:
    name = exc.__class__.__name__
    if name in TRANSIENT_ERROR_NAMES:
        return True
    # neo4j.exceptions.Neo4jError exposes .code, e.g. 'Neo.TransientError.*'
    code = str(getattr(exc, "code", "") or "")
    return code.startswith("Neo.TransientError") or code == "Neo.ClientError.Request.TransactionTimedOut"


def _is_fatal(exc: BaseException) -> bool:
    name = exc.__class__.__name__
    if name in FATAL_ERROR_NAMES:
        return True
    code = str(getattr(exc, "code", "") or "")
    return code.startswith("Neo.ClientError.Security") or code in {"Neo.ClientError.Database.NotFound"}


class Neo4jClient:
    """Session/transaction manager with retries, batching and dry-run support."""

    def __init__(
        self,
        settings: Any,
        *,
        dry_run: bool | None = None,
        driver_factory: Callable[..., Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.uri = settings.neo4j_uri
        self.database = settings.neo4j_database or "neo4j"
        self.batch_size = max(1, int(settings.neo4j_batch_size))
        self.max_retries = max(0, int(settings.neo4j_max_retries))
        self.dry_run = bool(settings.dry_run if dry_run is None else dry_run)
        self.recorder = DryRunRecorder()
        self._sleeper = sleeper
        self._driver: Any = None
        self._driver_factory = driver_factory
        self._connected = False
        self.queries_executed = 0
        self.rows_written = 0
        self.retries = 0
        self._rng = random.Random(settings.seed if hasattr(settings, "seed") else 7)

    # ------------------------------------------------------------------ #
    # Connection lifecycle
    # ------------------------------------------------------------------ #
    @property
    def driver(self) -> Any:
        if self._driver is None and not self.dry_run:
            self._driver = self._create_driver()
        return self._driver

    def _create_driver(self) -> Any:
        if self._driver_factory is not None:
            return self._driver_factory(
                self.uri,
                (self.settings.neo4j_username, self.settings.neo4j_password),
                self.settings,
            )
        try:
            from neo4j import GraphDatabase
        except ImportError as exc:  # pragma: no cover - declared dependency
            raise Neo4jUnavailable(f"neo4j driver not installed: {exc}") from exc

        kwargs: dict[str, Any] = {
            "max_connection_pool_size": int(self.settings.neo4j_max_connection_pool_size),
            "connection_acquisition_timeout": float(self.settings.neo4j_connection_timeout_seconds),
            "connection_timeout": float(self.settings.neo4j_connection_timeout_seconds),
            "max_transaction_retry_time": float(self.settings.neo4j_connection_timeout_seconds) * 2,
        }
        if self.uri.startswith("neo4j+s://") or self.uri.startswith("neo4j+ssc://"):
            kwargs["max_connection_lifetime"] = 300.0  # Aura drops idle sockets aggressively
        return GraphDatabase.driver(self.uri, auth=(self.settings.neo4j_username, self.settings.neo4j_password), **kwargs)

    def verify(self) -> bool:
        """Confirm connectivity (and wake an Aura free-tier instance)."""
        if self.dry_run:
            logger.info("dry-run: skipping Neo4j connectivity check")
            # No socket is opened, so the client must not claim a session it
            # does not have — the run report reads this flag.
            self._connected = False
            return True
        last_error: BaseException | None = None
        for attempt in range(self.max_retries + 1):
            try:
                self.driver.verify_connectivity()
                self._connected = True
                logger.info("connected to Neo4j at %s (database=%s)", self.uri, self.database)
                return True
            except Exception as exc:  # noqa: BLE001 - driver raises many types
                last_error = exc
                if _is_fatal(exc):
                    raise Neo4jUnavailable(f"Neo4j authentication/configuration failure: {exc}") from exc
                if not _is_transient(exc) or attempt >= self.max_retries:
                    break
                delay = min(30.0, 1.5 ** attempt) + self._rng.uniform(0, 0.5)
                logger.warning("Neo4j connectivity attempt %d failed (%s) — retrying in %.1fs", attempt + 1, exc.__class__.__name__, delay)
                self._sleeper(delay)
        raise Neo4jUnavailable(f"could not reach Neo4j at {self.uri}: {last_error}")

    def close(self) -> None:
        """Release the pool. Safe to call twice, and in dry-run mode."""
        driver, self._driver = self._driver, None
        self._connected = False
        if driver is None:
            return
        try:
            driver.close()
        except Exception as exc:  # pragma: no cover
            logger.debug("error closing Neo4j driver: %s", exc)

    def __enter__(self) -> Neo4jClient:
        self.verify()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #
    def execute(self, query: str, params: dict[str, Any] | None = None, *, kind: str = "write", rows: int = 0) -> list[dict[str, Any]]:
        """Run a statement with retries, returning list-of-dicts records."""
        params = params or {}
        if self.dry_run:
            self.recorder.record(query, params, rows=rows or len(params.get("rows", []) or []), kind=kind)
            return []

        last_error: BaseException | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with self.driver.session(database=self.database) as session:
                    if kind == "read":
                        result = session.run(query, params)
                        records = [record.data() for record in result]
                        result.consume()
                    else:
                        records = self._execute_write(session, query, params)
                self.queries_executed += 1
                return records
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if _is_fatal(exc):
                    logger.error("fatal Neo4j error: %s", exc)
                    raise
                if not _is_transient(exc) or attempt >= self.max_retries:
                    logger.error("Neo4j query failed (%s): %s | query=%s", exc.__class__.__name__, exc, " ".join(query.split())[:200])
                    raise
                delay = min(30.0, (1.7 ** attempt)) + self._rng.uniform(0, 0.75)
                self.retries += 1
                logger.warning("transient Neo4j error (%s) — retry %d/%d in %.1fs", exc.__class__.__name__, attempt + 1, self.max_retries, delay)
                self._sleeper(delay)
        raise Neo4jUnavailable(f"Neo4j query failed after {self.max_retries} retries: {last_error}")

    @staticmethod
    def _execute_write(session: Any, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        def work(tx: Any) -> list[dict[str, Any]]:
            result = tx.run(query, params)
            records = [record.data() for record in result]
            summary = result.consume()
            counters = getattr(summary, "counters", None)
            if counters is not None:
                logger.debug(
                    "tx counters: nodes=%d rels=%d props=%d",
                    getattr(counters, "nodes_created", 0),
                    getattr(counters, "relationships_created", 0),
                    getattr(counters, "properties_set", 0),
                )
            return records

        if hasattr(session, "execute_write"):
            return session.execute_write(work)
        return session.write_transaction(work)  # neo4j driver < 5.0

    def read(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return self.execute(query, params, kind="read")

    def write(self, query: str, params: dict[str, Any] | None = None, *, rows: int = 0, kind: str = "write") -> list[dict[str, Any]]:
        """Execute a write transaction.

        ``kind`` labels the statement for the dry-run recorder and the logs
        (``run``, ``schema``, ``write``); only ``"read"`` changes execution
        semantics, so any other label still runs as a write transaction.
        """
        return self.execute(query, params, kind=kind, rows=rows)

    # ------------------------------------------------------------------ #
    # Batching
    # ------------------------------------------------------------------ #
    def execute_batches(
        self,
        query: str,
        rows: Sequence[dict[str, Any]],
        *,
        batch_size: int | None = None,
        kind: str = "write",
        label: str = "",
    ) -> int:
        """Execute ``query`` once per chunk of ``rows``; return rows submitted.

        ``kind`` is forwarded to every batch so the dry-run recorder can tell a
        harvested write from a computed one (the analytics layer passes
        ``kind="analytics"``); ``label`` only decorates the log lines.
        """
        if not rows:
            return 0
        size = batch_size or self.batch_size
        submitted = 0
        total_batches = (len(rows) + size - 1) // size
        for index, batch in enumerate(chunked(rows, size), start=1):
            self.write(query, {"rows": batch}, rows=len(batch), kind=kind)
            submitted += len(batch)
            logger.debug("%sbatch %d/%d (%d rows)", f"{label} " if label else "", index, total_batches, len(batch))
        self.rows_written += submitted
        if label:
            logger.info("%s: wrote %d rows in %d batch(es)", label, submitted, total_batches)
        return submitted

    def ensure_schema(self, statements: Sequence[str]) -> int:
        """Apply DDL idempotently. Returns the number of statements executed."""
        applied = 0
        for statement in statements:
            try:
                self.write(statement, {}, kind="schema")
                applied += 1
            except Exception as exc:  # noqa: BLE001
                # Fulltext/array indexes need Neo4j 4.4+/5; Aura Free has them,
                # but a self-hosted 4.x test instance may not. Degrade loudly
                # but keep going: the pipeline must not die on a missing index.
                logger.warning("schema statement skipped (%s): %s", exc.__class__.__name__, " ".join(statement.split())[:120])
        logger.info("schema ensured: %d/%d statements applied", applied, len(statements))
        return applied

    # ------------------------------------------------------------------ #
    def describe(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "database": self.database,
            "dry_run": self.dry_run,
            "connected": self._connected,
            "queries_executed": self.queries_executed,
            "rows_written": self.rows_written,
            "retries": self.retries,
            "batch_size": self.batch_size,
            "dry_run_summary": self.recorder.summary() if self.dry_run else None,
        }
