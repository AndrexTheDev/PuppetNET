"""Graph layer: Neo4j client retries/batching, writer ordering, resolver.

Nothing here touches a database. Two doubles cover the two modes the client
runs in:

* ``dry_run=True`` — the production default in CI and in ``ingest.py --dry-run``.
  Every statement is captured by :class:`DryRunRecorder` instead of executed, so
  the tests assert on *what would have been written* (statement kind, parameter
  keys, row counts).
* ``dry_run=False`` with an injected ``driver_factory`` — an API-compatible fake
  of the official driver (``session(database=...)`` → ``execute_write(work)`` →
  ``tx.run(...).consume()``), which is what lets the retry/backoff, batching and
  fatal-error paths be exercised without AuraDB.
"""

from __future__ import annotations

import dataclasses

import pytest

from puppetnet.graph import (
    DryRunRecorder,
    EntityResolver,
    GraphWriter,
    Neo4jClient,
    Neo4jUnavailable,
    build_entity_upsert,
    build_relation_upsert,
    chunked,
    ensure_schema_statements,
    merge_entities_by_key,
    schema,
)
from puppetnet.graph.resolver import _fold
from puppetnet.models import (
    Document,
    Entity,
    EntityMention,
    EntityType,
    ExtractionMethod,
    IngestStats,
    Relation,
    RelationType,
    SourceSpec,
    SourceType,
    is_safe_relationship_type,
)

# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #


class ServiceUnavailable(Exception):
    """Name-matched by ``TRANSIENT_ERROR_NAMES``."""


class TransientError(Exception):
    """Name-matched by ``TRANSIENT_ERROR_NAMES``."""


class AuthenticationError(Exception):
    """Name-matched by ``FATAL_ERROR_NAMES``."""


class CodedTransient(Exception):
    """Detected via ``.code`` rather than by class name."""

    code = "Neo.TransientError.General.MemoryPoolOutOfMemoryError"


class CodedSecurityFailure(Exception):
    code = "Neo.ClientError.Security.Unauthorized"


class FakeCounters:
    nodes_created = 1
    relationships_created = 1
    properties_set = 4


class FakeSummary:
    counters = FakeCounters()


class FakeRecord:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def data(self) -> dict:
        return dict(self._payload)


class FakeResult:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.consumed = False

    def __iter__(self):
        return iter([FakeRecord(row) for row in self._rows])

    def consume(self) -> FakeSummary:
        self.consumed = True
        return FakeSummary()


class FakeTransaction:
    def __init__(self, driver: FakeDriver) -> None:
        self._driver = driver

    def run(self, query: str, params: dict):
        self._driver.executed.append((query, params))
        return self._driver._next_outcome()


class FakeSession:
    def __init__(self, driver: FakeDriver, database: str) -> None:
        self._driver = driver
        self.database = database
        self.closed = False

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *exc_info) -> bool:
        self.closed = True
        return False

    def execute_write(self, work):
        return work(FakeTransaction(self._driver))

    # neo4j driver < 5.0 fallback
    def write_transaction(self, work):
        return work(FakeTransaction(self._driver))

    def run(self, query: str, params: dict):
        self._driver.executed.append((query, params))
        return self._driver._next_outcome()


class FakeDriver:
    """Minimal stand-in for ``neo4j.Driver``.

    ``outcomes`` is a queue consumed one entry per executed statement: an
    exception instance is raised, a list of dicts is returned as records.
    """

    def __init__(self, outcomes=None, *, connectivity_errors: int = 0) -> None:
        self.outcomes: list = list(outcomes or [])
        self.executed: list[tuple[str, dict]] = []
        self.sessions: list[FakeSession] = []
        self.connectivity_errors = connectivity_errors
        self.verify_calls = 0
        self.closed = False
        self.uri = ""
        self.auth = None

    def _next_outcome(self):
        outcome = self.outcomes.pop(0) if self.outcomes else []
        if isinstance(outcome, BaseException):
            raise outcome
        return FakeResult(list(outcome))

    def session(self, database: str = "neo4j") -> FakeSession:
        session = FakeSession(self, database)
        self.sessions.append(session)
        return session

    def verify_connectivity(self) -> None:
        self.verify_calls += 1
        if self.verify_calls <= self.connectivity_errors:
            raise ServiceUnavailable("instance waking up")

    def close(self) -> None:
        self.closed = True


def make_driver_factory(driver: FakeDriver):
    def factory(uri: str, auth, settings):
        driver.uri = uri
        driver.auth = auth
        return driver

    return factory


class OfflineReadClient(Neo4jClient):
    """A Neo4jClient whose read path always fails (Aura asleep / no creds)."""

    def read(self, query, params=None):
        raise RuntimeError("offline")


class RecordingSleeper:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class StubReadClient:
    """Resolver-side double: returns canned rows, records the query."""

    def __init__(self, rows=None, *, error: Exception | None = None) -> None:
        self.rows = list(rows or [])
        self.error = error
        self.queries: list[tuple[str, dict]] = []

    def read(self, query: str, params: dict | None = None):
        self.queries.append((query, params or {}))
        if self.error is not None:
            raise self.error
        return list(self.rows)


# --------------------------------------------------------------------------- #
# Fixtures / builders
# --------------------------------------------------------------------------- #


@pytest.fixture()
def dry_settings(settings):
    """Dry-run settings with a small batch size so batching is observable."""
    return dataclasses.replace(
        settings,
        dry_run=True,
        run_id="run-2026-09-27",
        neo4j_batch_size=2,
        neo4j_ensure_schema=True,
        github_run_id="987654",
        github_sha="deadbeef",
        github_workflow="daily_ingest.yml",
    )


@pytest.fixture()
def live_settings(settings):
    """Non-dry-run settings; the driver is always injected, never dialled."""
    return dataclasses.replace(
        settings,
        dry_run=False,
        neo4j_uri="neo4j+s://db.example.test:7687",
        neo4j_username="neo4j",
        neo4j_password="secret",
        neo4j_max_retries=2,
        neo4j_batch_size=10,
    )


def make_client(settings, driver=None, sleeper=None) -> tuple[Neo4jClient, FakeDriver | None, RecordingSleeper]:
    recorder = sleeper or RecordingSleeper()
    client = Neo4jClient(
        settings,
        driver_factory=make_driver_factory(driver) if driver is not None else None,
        sleeper=recorder,
    )
    return client, driver, recorder


def entity(name: str, entity_type: EntityType = EntityType.ORGANIZATION, **kwargs) -> Entity:
    return Entity(name=name, entity_type=entity_type, **kwargs)


def relation(
    subject: Entity,
    obj: Entity,
    predicate=RelationType.OWNS,
    confidence: float = 0.5,
    **kwargs,
) -> Relation:
    return Relation(
        subject=subject,
        predicate=predicate,
        obj=obj,
        confidence=confidence,
        method=ExtractionMethod.COOCCURRENCE,
        source_id="src",
        doc_id="doc",
        evidence=f"{subject.name} {predicate.value.lower()} {obj.name}",
        **kwargs,
    )


def spec(source_id: str = "icij", kind: SourceType = SourceType.STRUCTURED) -> SourceSpec:
    return SourceSpec(id=source_id, name=source_id.upper(), kind=kind, adapter="icij", base_url="https://example.test")


# --------------------------------------------------------------------------- #
# chunked / DryRunRecorder
# --------------------------------------------------------------------------- #


def test_chunked_splits_evenly_and_keeps_the_remainder():
    assert list(chunked([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    assert list(chunked([], 3)) == []
    assert list(chunked([1, 2, 3], 10)) == [[1, 2, 3]]


def test_chunked_never_yields_empty_batches_for_a_non_positive_size():
    assert list(chunked([1, 2], 0)) == [[1], [2]]
    assert list(chunked([1, 2], -5)) == [[1], [2]]


def test_chunked_accepts_a_generator():
    assert list(chunked((i for i in range(5)), 2)) == [[0, 1], [2, 3], [4]]


def test_dry_run_recorder_captures_shape_not_values():
    recorder = DryRunRecorder()
    recorder.record("UNWIND $rows AS row MERGE (e:Entity)", {"rows": [1, 2]}, rows=2, kind="write")
    recorder.record("MATCH (n) RETURN n", {"limit": 5}, kind="read")

    assert recorder.total_rows == 2
    summary = recorder.summary()
    assert summary == {"statements": 2, "rows": 2, "by_kind": {"write": 1, "read": 1}}
    # Secrets and payloads never reach the summary — only parameter *names*.
    assert recorder.statements[0]["params_keys"] == ["rows"]
    assert recorder.statements[1]["params_keys"] == ["limit"]


def test_dry_run_recorder_truncates_the_query_preview():
    recorder = DryRunRecorder()
    recorder.record("MERGE (e:Entity)\n" + "  SET e.x = 1\n" * 200, {})
    assert len(recorder.statements[0]["query_preview"]) <= 400
    assert "\n" not in recorder.statements[0]["query_preview"]


# --------------------------------------------------------------------------- #
# Neo4jClient — dry-run mode
# --------------------------------------------------------------------------- #


def test_dry_run_client_never_builds_a_driver(dry_settings):
    client, driver, sleeper = make_client(dry_settings)
    assert client.dry_run is True
    assert client.driver is None
    assert client.verify() is True
    assert driver is None


def test_dry_run_execute_records_and_returns_no_rows(dry_settings):
    client, _, _ = make_client(dry_settings)
    assert client.execute("MERGE (n:X)", {"id": 1}, kind="write", rows=3) == []
    assert client.recorder.statements[-1] == {
        "kind": "write",
        "query_preview": "MERGE (n:X)",
        "params_keys": ["id"],
        "rows": 3,
    }


def test_dry_run_execute_infers_row_count_from_the_rows_parameter(dry_settings):
    client, _, _ = make_client(dry_settings)
    client.write("UNWIND $rows AS row MERGE (n)", {"rows": [{"a": 1}, {"a": 2}, {"a": 3}]})
    assert client.recorder.statements[-1]["rows"] == 3


def test_dry_run_batches_split_on_the_configured_batch_size(dry_settings):
    client, _, _ = make_client(dry_settings)  # neo4j_batch_size = 2
    rows = [{"canonical_key": f"ORGANIZATION:x-{i}"} for i in range(5)]
    submitted = client.execute_batches("UNWIND $rows AS row MERGE (e:Entity)", rows, label="entities")

    assert submitted == 5
    assert client.rows_written == 5
    assert len(client.recorder.statements) == 3
    assert client.recorder.total_rows == 5


def test_execute_batches_with_no_rows_is_a_no_op(dry_settings):
    client, _, _ = make_client(dry_settings)
    assert client.execute_batches("UNWIND $rows AS row MERGE (e:Entity)", []) == 0
    assert client.recorder.statements == []


def test_ensure_schema_applies_every_statement(dry_settings):
    client, _, _ = make_client(dry_settings)
    statements = ensure_schema_statements()
    assert statements, "schema DDL must not be empty"
    assert client.ensure_schema(statements) == len(statements)
    assert client.recorder.summary()["by_kind"]["schema"] == len(statements)


def test_ensure_schema_survives_a_statement_the_server_rejects(dry_settings):
    """A 4.x instance without fulltext indexes must not kill the run."""

    class ExplodingClient(Neo4jClient):
        def write(self, query, params=None, *, rows: int = 0, kind: str = "write"):
            if "FULLTEXT" in query:
                raise RuntimeError("syntax error")
            return super().write(query, params, rows=rows, kind=kind)

    client = ExplodingClient(dry_settings, sleeper=RecordingSleeper())
    statements = ensure_schema_statements()
    applied = client.ensure_schema(statements)
    fulltext = sum(1 for statement in statements if "FULLTEXT" in statement)
    assert applied == len(statements) - fulltext
    assert applied > 0


def test_describe_reports_dry_run_summary(dry_settings):
    client, _, _ = make_client(dry_settings)
    client.write("MERGE (n)", {"a": 1}, rows=1)
    description = client.describe()
    assert description["dry_run"] is True
    assert description["batch_size"] == 2
    assert description["dry_run_summary"]["statements"] == 1
    assert "password" not in description


# --------------------------------------------------------------------------- #
# Neo4jClient — live mode against the fake driver
# --------------------------------------------------------------------------- #


def test_live_client_builds_the_driver_through_the_factory(live_settings):
    driver = FakeDriver()
    client, _, _ = make_client(live_settings, driver=driver)
    assert client.driver is driver
    assert driver.uri == "neo4j+s://db.example.test:7687"
    assert driver.auth == ("neo4j", "secret")


def test_verify_wakes_a_sleeping_aura_instance(live_settings):
    driver = FakeDriver(connectivity_errors=2)
    client, _, sleeper = make_client(live_settings, driver=driver)
    assert client.verify() is True
    assert driver.verify_calls == 3
    assert len(sleeper.calls) == 2
    assert all(delay > 0 for delay in sleeper.calls)


def test_verify_raises_on_authentication_failure_without_retrying(live_settings):
    class RefusingDriver(FakeDriver):
        def verify_connectivity(self) -> None:
            raise AuthenticationError("bad credentials")

    driver = RefusingDriver()
    client, _, sleeper = make_client(live_settings, driver=driver)
    with pytest.raises(Neo4jUnavailable, match="authentication"):
        client.verify()
    assert sleeper.calls == [], "a fatal error must not be retried"


def test_verify_gives_up_after_the_retry_budget(live_settings):
    driver = FakeDriver(connectivity_errors=99)
    client, _, sleeper = make_client(live_settings, driver=driver)
    with pytest.raises(Neo4jUnavailable, match="could not reach Neo4j"):
        client.verify()
    assert driver.verify_calls == live_settings.neo4j_max_retries + 1
    assert len(sleeper.calls) == live_settings.neo4j_max_retries


def test_write_returns_records_and_counts_the_query(live_settings):
    driver = FakeDriver(outcomes=[[{"written": 7}]])
    client, _, _ = make_client(live_settings, driver=driver)
    rows = client.write("UNWIND $rows AS row MERGE (e:Entity)", {"rows": [{"a": 1}]})
    assert rows == [{"written": 7}]
    assert client.queries_executed == 1
    assert driver.sessions[0].database == live_settings.neo4j_database
    assert driver.sessions[0].closed is True


def test_read_uses_the_read_path(live_settings):
    driver = FakeDriver(outcomes=[[{"canonical_key": "ORGANIZATION:gazprom-1"}]])
    client, _, _ = make_client(live_settings, driver=driver)
    rows = client.read("MATCH (e:Entity) RETURN e.canonical_key AS canonical_key", {"limit": 10})
    assert rows == [{"canonical_key": "ORGANIZATION:gazprom-1"}]
    assert driver.executed[0][1] == {"limit": 10}


def test_transient_errors_are_retried_with_backoff(live_settings):
    driver = FakeDriver(outcomes=[ServiceUnavailable("flaky"), CodedTransient(), [{"ok": True}]])
    client, _, sleeper = make_client(live_settings, driver=driver)

    rows = client.write("MERGE (n)", {})

    assert rows == [{"ok": True}]
    assert client.retries == 2
    assert len(sleeper.calls) == 2
    assert sleeper.calls[1] > sleeper.calls[0], "backoff must grow"
    assert all(delay > 0 for delay in sleeper.calls)
    assert len(driver.executed) == 3


def test_fatal_errors_are_raised_immediately(live_settings):
    driver = FakeDriver(outcomes=[AuthenticationError("nope")])
    client, _, sleeper = make_client(live_settings, driver=driver)
    with pytest.raises(AuthenticationError):
        client.write("MERGE (n)", {})
    assert sleeper.calls == []
    assert client.retries == 0


def test_coded_security_failures_are_fatal(live_settings):
    driver = FakeDriver(outcomes=[CodedSecurityFailure()])
    client, _, sleeper = make_client(live_settings, driver=driver)
    with pytest.raises(CodedSecurityFailure):
        client.write("MERGE (n)", {})
    assert sleeper.calls == []


def test_exhausted_retries_propagate_the_last_error(live_settings):
    driver = FakeDriver(outcomes=[TransientError("a"), TransientError("b"), TransientError("c")])
    client, _, sleeper = make_client(live_settings, driver=driver)
    with pytest.raises(TransientError):
        client.write("MERGE (n)", {})
    assert len(driver.executed) == live_settings.neo4j_max_retries + 1
    assert len(sleeper.calls) == live_settings.neo4j_max_retries


def test_close_is_idempotent_and_marks_the_client_disconnected(live_settings):
    driver = FakeDriver()
    client, _, _ = make_client(live_settings, driver=driver)
    client.verify()
    assert client.describe()["connected"] is True

    client.close()
    assert driver.closed is True
    assert client.describe()["connected"] is False

    client.close()  # a second close must not raise on an already-closed driver
    # The pool is rebuilt lazily, so the next use after close reconnects.
    assert client.driver is driver


def test_context_manager_verifies_and_closes(live_settings):
    driver = FakeDriver()
    with Neo4jClient(live_settings, driver_factory=make_driver_factory(driver), sleeper=RecordingSleeper()) as client:
        assert client.driver is driver
    assert driver.closed is True


def test_execute_batches_live_counts_rows_written(live_settings):
    driver = FakeDriver()
    client, _, _ = make_client(live_settings, driver=driver)
    rows = [{"canonical_key": f"ORGANIZATION:e-{i}"} for i in range(25)]
    submitted = client.execute_batches("UNWIND $rows AS row MERGE (e:Entity)", rows, label="entities")
    assert submitted == 25
    assert client.rows_written == 25
    # batch size 10 → 10 + 10 + 5
    assert [len(params["rows"]) for _, params in driver.executed] == [10, 10, 5]


# --------------------------------------------------------------------------- #
# schema builders — Cypher injection guards
# --------------------------------------------------------------------------- #


def test_entity_upsert_interpolates_only_whitelisted_labels():
    query = build_entity_upsert(EntityType.CRAFT)
    assert ":Craft" in query
    assert "MERGE (e:Entity:Craft" in query


def test_entity_upsert_rejects_an_unsafe_label():
    with pytest.raises(ValueError):
        build_entity_upsert("Entity} DETACH DELETE n //")


def test_relation_upsert_uses_the_predicate_whitelist():
    query = build_relation_upsert(RelationType.OWNED_BY)
    assert "-[r:OWNED_BY]->" in query


def test_relation_upsert_never_emits_an_unlisted_predicate():
    """Unknown predicates are coerced, so injected text can never reach Cypher."""
    assert is_safe_relationship_type("HACKS] DETACH DELETE n //") is False
    query = build_relation_upsert("HACKS] DETACH DELETE n //")
    assert "HACKS" not in query and "DELETE" not in query
    assert "-[r:ASSOCIATED_WITH]->" in query


def test_schema_statements_are_idempotent_ddl():
    statements = ensure_schema_statements()
    assert all("IF NOT EXISTS" in statement for statement in statements)
    assert any("canonical_key IS UNIQUE" in statement for statement in statements)


# --------------------------------------------------------------------------- #
# Row builders
# --------------------------------------------------------------------------- #


def test_entity_row_flattens_nested_properties():
    node = entity(
        "9H-VUC",
        EntityType.CRAFT,
        properties={
            "craft_kind": "Aircraft",
            "registry": {"country": "Malta", "prefix": "9H"},
            "tags": ["sanctioned", "aircraft"],
            "empty": "",
            "nothing": None,
        },
    )
    row = schema.properties_for_entity_row(node, run_id="run-1")

    assert row["canonical_key"] == node.canonical_key
    assert row["entity_type"] == "Craft"
    assert row["run_id"] == "run-1"
    # Neo4j rejects nested maps: they become dotted/underscored scalars.
    assert row["properties"]["registry_country"] == "Malta"
    assert row["properties"]["registry_prefix"] == "9H"
    assert row["properties"]["craft_kind"] == "Aircraft"
    assert row["properties"]["tags"] == ["sanctioned", "aircraft"]
    assert "empty" not in row["properties"]
    assert "nothing" not in row["properties"]
    # Reserved keys stay top-level so the upsert template can address them.
    assert "canonical_key" not in row["properties"]


def test_relation_row_carries_keys_evidence_and_flags():
    subject = entity("Gazprom")
    obj = entity("Nord Stream AG")
    edge = relation(subject, obj, RelationType.OWNS, confidence=0.72, verb="owns")
    edge.extra = {"rule": "verb:owns", "evidence_score": 0.9, "hedged": True, "passive": False}

    row = schema.properties_for_relation_row(edge, run_id="run-9")

    assert row["subject_key"] == subject.canonical_key
    assert row["object_key"] == obj.canonical_key
    assert row["rel_type"] == "OWNS"
    assert row["confidence"] == 0.72
    assert row["source_weight"] == pytest.approx(0.4)
    assert row["method"] == "cooccurrence"
    assert row["rule"] == "verb:owns"
    assert row["evidence_score"] == 0.9
    assert row["hedged"] is True
    assert row["run_id"] == "run-9"
    assert row["evidence"].startswith("Gazprom")


def test_relation_row_falls_back_to_the_subject_as_evidence():
    edge = relation(entity("A"), entity("B"))
    edge.evidence = ""
    assert schema.properties_for_relation_row(edge)["evidence"] == "A"


def test_mention_rows_group_by_document():
    node = entity(
        "Igor Sechin",
        EntityType.PERSON,
        doc_ids={"doc-1", "doc-2"},
        mentions=[
            EntityMention(text="Igor Sechin", entity_type=EntityType.PERSON, start_char=4, confidence=0.9),
            EntityMention(text="Sechin", entity_type=EntityType.PERSON, start_char=90, confidence=0.7),
        ],
    )
    node.doc_ids = {"doc-1"}
    rows = schema.properties_for_mention_rows([node])
    assert rows, "at least one MENTIONS row per document"
    row = rows[0]
    assert row["canonical_key"] == node.canonical_key
    assert row["doc_id"] == "doc-1"
    assert row["count"] == 2
    assert row["first_offset"] == 4
    assert row["confidence"] == pytest.approx(0.9)


def test_mention_rows_skip_entities_without_a_document():
    node = entity("Ghost", EntityType.PERSON, mentions=[EntityMention(text="Ghost", entity_type=EntityType.PERSON)])
    node.doc_ids = set()
    assert schema.properties_for_mention_rows([node]) == []


# --------------------------------------------------------------------------- #
# EntityResolver
# --------------------------------------------------------------------------- #


def test_resolver_starts_cold_and_keeps_the_extracted_key():
    resolver = EntityResolver()
    resolver.load(StubReadClient(rows=[]))
    node = entity("Gazprom")
    assert resolver.resolve(node) == node.canonical_key
    assert resolver.stats.new_keys == 1
    assert resolver.stats.alias_hits == 0


def test_resolver_maps_a_new_surface_form_onto_a_known_node():
    """The whole point of the alias index: OJSC Rosneft == Rosneft."""
    rows = [
        {
            "canonical_key": "ORGANIZATION:rosneft-aaaa1111",
            "entity_type": "Organization",
            "name": "Rosneft",
            "aliases": ["OJSC Rosneft", "ROSNEFT OIL COMPANY"],
            "mention_count": 12,
        }
    ]
    resolver = EntityResolver().load(StubReadClient(rows=rows))
    newcomer = entity("ojsc rosneft")

    assert resolver.resolve(newcomer) == "ORGANIZATION:rosneft-aaaa1111"
    assert resolver.stats.alias_hits == 1
    assert resolver.size >= 3


def test_resolver_refuses_to_merge_across_entity_types():
    rows = [
        {
            "canonical_key": "PERSON:sechin-bbbb2222",
            "entity_type": "Person",
            "name": "Sechin",
            "aliases": [],
            "mention_count": 5,
        }
    ]
    resolver = EntityResolver().load(StubReadClient(rows=rows))
    organisation = entity("Sechin", EntityType.ORGANIZATION)

    assert resolver.resolve(organisation) == organisation.canonical_key
    assert resolver.stats.type_conflicts == 1


def test_resolver_load_survives_an_unreadable_index():
    resolver = EntityResolver().load(StubReadClient(error=RuntimeError("database offline")))
    assert resolver.loaded is True
    assert resolver.size == 0
    node = entity("Gazprom")
    assert resolver.resolve(node) == node.canonical_key


def test_resolver_ignores_rows_without_a_key_and_bad_types():
    rows = [
        {"canonical_key": "", "entity_type": "Organization", "name": "Nothing"},
        {"canonical_key": "ORGANIZATION:x-1", "entity_type": "Martian", "name": "Zorg Corp", "aliases": []},
        {"canonical_key": "PERSON:y-2", "entity_type": "Person", "name": "ab", "aliases": ["ab"]},
    ]
    resolver = EntityResolver().load(StubReadClient(rows=rows))
    # Unknown types degrade to UNKNOWN rather than raising.
    assert resolver.resolve(entity("Zorg Corp")) == "ORGANIZATION:x-1"
    # Surfaces shorter than three folded characters are never indexed.
    assert resolver.resolve(entity("ab", EntityType.PERSON)) != "PERSON:y-2"


def test_resolver_limit_has_a_floor():
    assert EntityResolver(limit=10).limit == 1000
    assert EntityResolver(limit=50_000).limit == 50_000


def test_resolve_batch_returns_one_target_key_per_source_key():
    resolver = EntityResolver().load(StubReadClient(rows=[]))
    first, second = entity("Gazprom"), entity("Rosneft")
    mapping = resolver.resolve_batch([first, second])
    assert mapping == {first.canonical_key: first.canonical_key, second.canonical_key: second.canonical_key}


def test_merge_entities_by_key_collapses_duplicates():
    """Two spellings resolved onto one key must become one row."""
    first = entity("Rosneft", mentions=[EntityMention(text="Rosneft", entity_type=EntityType.ORGANIZATION)])
    second = entity("OJSC Rosneft", confidence=0.9)
    target = "ORGANIZATION:rosneft-aaaa1111"
    key_map = {first.canonical_key: target, second.canonical_key: target}

    merged = merge_entities_by_key([first, second], key_map)

    assert len(merged) == 1
    node = merged[0]
    assert node.canonical_key == target
    assert node.name == "OJSC Rosneft", "the longest surface form wins the display name"
    assert {"Rosneft", "OJSC Rosneft"} <= node.aliases
    assert node.confidence == pytest.approx(0.9)
    assert len(node.mentions) == 1


def test_merge_entities_by_key_leaves_distinct_keys_alone():
    first, second = entity("Gazprom"), entity("Rosneft")
    merged = merge_entities_by_key([first, second], {})
    assert {node.canonical_key for node in merged} == {first.canonical_key, second.canonical_key}


def test_fold_ignores_case_accents_and_punctuation():
    assert _fold("  PJSC “Gazprom” ") == _fold("pjsc gazprom")
    assert _fold("Société Générale") == _fold("societe generale")
    assert _fold("") == ""


class ScriptedGraphClient(Neo4jClient):
    """Live-mode client whose reads come from a table and whose writes are recorded."""

    def __init__(self, settings, reads=None, **kwargs) -> None:
        super().__init__(settings, dry_run=False, **kwargs)
        self.reads = dict(reads or {})
        self.read_queries: list[str] = []

    def read(self, query, params=None):
        self.read_queries.append(query)
        for fragment, rows in self.reads.items():
            if fragment in query:
                return rows
        return []

    def write(self, query, params=None, *, rows: int = 0, kind: str = "write"):
        payload = (params or {}).get("rows") or []
        submitted = rows or len(payload)
        self.recorder.record(query, params, rows=submitted, kind=kind)
        self.rows_written += submitted
        self.queries_executed += 1
        return []


def budget_writer(live_settings, *, node_count: int, alias_rows=None, cap: int = 200_000):
    settings = dataclasses.replace(live_settings, aura_node_cap=cap, neo4j_batch_size=50)
    client = ScriptedGraphClient(
        settings,
        reads={
            "count(e) AS nodes": [{"nodes": node_count}],
            "e.aliases AS aliases": list(alias_rows or []),
        },
        sleeper=RecordingSleeper(),
    )
    return GraphWriter(client, settings, stats=IngestStats()), client


# --------------------------------------------------------------------------- #
# AuraDB Free node budget
# --------------------------------------------------------------------------- #


def test_node_budget_refuses_only_new_nodes(live_settings):
    """At the ceiling, existing nodes still update; newcomers are refused."""
    graph, _ = budget_writer(
        live_settings,
        node_count=199_999,
        alias_rows=[
            {
                "canonical_key": "ORGANIZATION:gazprom-8d3be1d8",
                "entity_type": "Organization",
                "name": "Gazprom",
                "aliases": ["Gazprom"],
                "mention_count": 8,
            }
        ],
        cap=200_000,
    )
    known = entity("Gazprom")
    newcomers = [entity("Rosneft"), entity("Nord Stream AG")]

    written, resolved = graph.write_entities([known, *newcomers])

    assert written == 2, "one existing node + one admitted newcomer"
    assert graph.summary.entities_capped == 1
    names = {node.name for node in resolved}
    assert "Gazprom" in names
    assert len(names) == 2


def test_node_budget_keeps_the_strongest_newcomers(live_settings):
    graph, _ = budget_writer(live_settings, node_count=99, cap=100)
    weak = entity("Weak Signal", confidence=0.1)
    strong = entity("Strong Signal", confidence=0.95)

    _, resolved = graph.write_entities([weak, strong])

    assert {node.name for node in resolved} == {"Strong Signal"}
    assert graph.summary.entities_capped == 1
    assert weak.canonical_key in graph._capped_keys


def test_edges_to_a_refused_node_are_counted_not_silently_lost(live_settings):
    graph, _ = budget_writer(live_settings, node_count=100, cap=100)
    kept = entity("Gazprom", confidence=0.9)
    refused = entity("Rosneft", confidence=0.1)

    graph.write_entities([kept, refused])
    written = graph.write_relations([relation(kept, refused, confidence=0.9)])

    assert written == 0
    assert graph.summary.relations_capped == 1
    assert graph.summary.relations_dropped == 1


def test_node_budget_is_not_probed_in_dry_run(dry_settings):
    client, _, _ = make_client(dry_settings)
    graph = GraphWriter(client, dry_settings, stats=IngestStats())
    assert graph.entity_node_count() is None
    written, resolved = graph.write_entities([entity(f"Company {i}") for i in range(5)])
    assert written == 5
    assert graph.summary.entities_capped == 0
    assert not any("count(e)" in entry["query_preview"] for entry in recorded(client))


def test_node_budget_can_be_disabled(live_settings):
    graph, client = budget_writer(live_settings, node_count=10_000_000, cap=0)
    written, _ = graph.write_entities([entity("Gazprom"), entity("Rosneft")])
    assert written == 2
    assert graph.summary.entities_capped == 0
    assert not any("count(e)" in query for query in client.read_queries)


def test_an_unreadable_node_count_does_not_block_the_harvest(live_settings):
    settings = dataclasses.replace(live_settings, aura_node_cap=200_000)

    class FailingCountClient(ScriptedGraphClient):
        def read(self, query, params=None):
            if "count(e) AS nodes" in query:
                raise RuntimeError("database asleep")
            return super().read(query, params)

    client = FailingCountClient(settings, reads={"e.aliases AS aliases": []}, sleeper=RecordingSleeper())
    graph = GraphWriter(client, settings, stats=IngestStats())

    assert graph.entity_node_count() is None
    written, _ = graph.write_entities([entity("Gazprom"), entity("Rosneft")])
    assert written == 2, "a failed probe must degrade to 'no cap', not 'write nothing'"
    assert graph.summary.entities_capped == 0


def test_node_count_is_probed_once_per_run(live_settings):
    graph, client = budget_writer(live_settings, node_count=0, cap=200_000)
    graph.write_entities([entity("Gazprom")])
    graph.write_entities([entity("Rosneft")])
    probes = [query for query in client.read_queries if "count(e) AS nodes" in query]
    assert len(probes) == 1, "the population is probed once, not per flush"
    assert graph._node_count == 2, "the cached count tracks what this run admitted"


def test_run_summary_reports_the_node_budget(live_settings):
    graph, client = budget_writer(live_settings, node_count=100, cap=100)
    graph.begin_run("run-cap")
    graph.write_entities([entity("Gazprom"), entity("Rosneft")])
    graph.finish_run(status="completed")

    assert graph.summary.entities == 0, "nothing may be created at the ceiling"
    assert graph.summary.entities_capped == 2
    assert any(entry["kind"] == "run" for entry in client.recorder.statements)

    # The counters reach the run node so a capped deployment is visible later.
    captured: dict = {}

    class Spy(ScriptedGraphClient):
        def write(self, query, params=None, *, rows: int = 0, kind: str = "write"):
            if "duration_seconds" in str(params):
                captured.update((params or {}).get("properties", {}))
            return super().write(query, params, rows=rows, kind=kind)

    spy_settings = dataclasses.replace(live_settings, aura_node_cap=100)
    spy = Spy(spy_settings, reads={"count(e) AS nodes": [{"nodes": 100}], "e.aliases AS aliases": []}, sleeper=RecordingSleeper())
    spy_graph = GraphWriter(spy, spy_settings, stats=IngestStats())
    spy_graph.begin_run("run-cap")
    spy_graph.write_entities([entity("Gazprom")])
    spy_graph.finish_run(status="completed")
    assert captured["entities_capped_node_budget"] == 1


# --------------------------------------------------------------------------- #
# GraphWriter (dry-run: assert on the recorded statements)
# --------------------------------------------------------------------------- #


def recorded(client: Neo4jClient) -> list[dict]:
    return client.recorder.statements


@pytest.fixture()
def writer(dry_settings):
    client, _, _ = make_client(dry_settings)
    stats = IngestStats(run_id=dry_settings.run_id)
    return GraphWriter(client, dry_settings, stats=stats)


def test_begin_run_records_github_provenance(writer):
    writer.begin_run("run-42", extra={"backend": "spacy:blank+gazetteer"})
    entry = recorded(writer.client)[-1]
    assert entry["kind"] == "run"
    assert entry["params_keys"] == ["properties", "run_id"]
    assert writer.run_id == "run-42"


def test_begin_run_properties_include_workflow_metadata(writer, dry_settings):
    captured: dict = {}

    class Spy(Neo4jClient):
        def write(self, query, params=None, *, rows: int = 0, kind: str = "write"):
            if query is schema.RUN_UPSERT:
                captured.update(params or {})
                captured["kind"] = kind
            return super().write(query, params, rows=rows, kind=kind)

    client = Spy(dry_settings, sleeper=RecordingSleeper())
    graph = GraphWriter(client, dry_settings, stats=IngestStats())
    graph.begin_run("run-7", extra={"backend": "spacy:blank+gazetteer", "nested": {"ignored": True}})

    assert captured["kind"] == "run"
    assert captured["run_id"] == "run-7"
    properties = captured["properties"]
    assert properties["run_id"] == "run-7"
    assert properties["status"] == "running"
    assert properties["github_run_id"] == "987654"
    assert properties["github_sha"] == "deadbeef"
    assert properties["github_workflow"] == "daily_ingest.yml"
    assert properties["dry_run"] is True
    assert properties["backend"] == "spacy:blank+gazetteer"
    # Neo4j rejects nested maps, so the settings blob is flattened to scalars.
    assert properties["settings_backend"] == "spacy:blank+gazetteer"
    assert not any(key.startswith("settings_nested") for key in properties)
    assert all(not isinstance(value, dict) for value in properties.values())
    assert properties["started_at"], "the run must be timestamped"


def test_finish_run_writes_the_summary_and_per_source_rows(writer):
    writer.stats.documents_fetched = 12
    writer.stats.entities_written = 40
    writer.stats.bump_source("icij", "documents", 5)
    writer.stats.bump_source("icij", "entities", 20)
    writer.finish_run(status="completed")

    kinds = [entry["kind"] for entry in recorded(writer.client)]
    assert kinds.count("run") == 2
    summary_params = writer.client.recorder.statements[-2]["params_keys"]
    assert summary_params == ["finished_at", "properties", "run_id"]
    assert writer.client.recorder.statements[-1]["rows"] == 1


def test_upsert_sources_writes_one_row_per_spec(writer):
    written = writer.upsert_sources([spec("icij"), spec("rss", SourceType.UNSTRUCTURED)])
    assert written == 2
    assert writer.summary.sources == 2
    entry = recorded(writer.client)[-1]
    assert entry["kind"] == "write"
    assert entry["rows"] == 2


def test_upsert_sources_with_nothing_to_do(writer):
    assert writer.upsert_sources([]) == 0
    assert recorded(writer.client) == []


def test_write_documents_stamps_the_run_id(writer):
    document = Document(doc_id="doc-1", source_id="icij", url="https://example.test/a", text="Gazprom owns Rosneft.")
    assert writer.write_documents([document]) == 1
    assert writer.summary.documents == 1


def test_write_entities_groups_one_statement_per_type(writer):
    nodes = [
        entity("Gazprom"),
        entity("Rosneft"),
        entity("Igor Sechin", EntityType.PERSON),
        entity("9H-VUC", EntityType.CRAFT, properties={"registry_country": "Malta"}),
    ]
    written, resolved = writer.write_entities(nodes)

    assert written == 4
    assert len(resolved) == 4
    assert writer.stats.entities_written == 4
    assert writer.summary.entities_by_type == {"Organization": 2, "Person": 1, "Craft": 1}
    queries = [entry["query_preview"] for entry in recorded(writer.client)]
    assert any(":Organization" in query for query in queries)
    assert any(":Person" in query for query in queries)
    assert any(":Craft" in query for query in queries)


def test_write_entities_batches_on_the_configured_size(writer):
    nodes = [entity(f"Company {i}") for i in range(5)]  # batch size 2 → 3 statements
    assert writer.write_entities(nodes)[0] == 5
    entity_statements = [entry for entry in recorded(writer.client) if entry["rows"]]
    assert sum(entry["rows"] for entry in entity_statements) == 5
    assert len(entity_statements) == 3


def test_run_counters_accumulate_across_flushes(writer):
    """The pipeline flushes once per source; stats must cover the whole run."""
    writer.write_entities([entity("Gazprom"), entity("Rosneft")])
    assert writer.stats.entities_written == 2
    writer.write_entities([entity("Igor Sechin", EntityType.PERSON)])
    assert writer.stats.entities_written == 3, "the second flush must not reset the run counter"
    assert writer.summary.entities == 1, "the per-flush summary stays per-flush"

    subject, obj = entity("A"), entity("B")
    writer.write_relations([relation(subject, obj, confidence=0.9)])
    writer.write_relations([relation(obj, subject, RelationType.OWNED_BY, confidence=0.9)])
    assert writer.stats.relations_written == 2
    assert writer.summary.relations == 1


def test_write_entities_is_a_no_op_for_an_empty_list(writer):
    assert writer.write_entities([]) == (0, [])
    assert recorded(writer.client) == []


def test_write_entities_applies_the_alias_index(writer, dry_settings):
    rows = [
        {
            "canonical_key": "ORGANIZATION:rosneft-aaaa1111",
            "entity_type": "Organization",
            "name": "Rosneft",
            "aliases": ["OJSC Rosneft"],
            "mention_count": 9,
        }
    ]
    writer.resolver = EntityResolver().load(StubReadClient(rows=rows))
    written, resolved = writer.write_entities([entity("OJSC Rosneft"), entity("Gazprom")])

    assert written == 2
    keys = {node.canonical_key for node in resolved}
    assert "ORGANIZATION:rosneft-aaaa1111" in keys


def test_write_mentions_follows_entities(writer):
    node = entity("Igor Sechin", EntityType.PERSON, doc_ids={"doc-1"})
    node.mentions = [EntityMention(text="Igor Sechin", entity_type=EntityType.PERSON, start_char=0, confidence=0.8)]
    assert writer.write_mentions([node]) == 1
    assert writer.summary.mentions == 1


def test_write_relations_drops_self_loops(writer):
    node = entity("Gazprom")
    assert writer.write_relations([relation(node, node)]) == 0
    assert writer.summary.relations_dropped == 1
    assert recorded(writer.client) == []


def test_write_relations_drops_edges_below_the_threshold(writer, dry_settings):
    subject, obj = entity("Gazprom"), entity("Rosneft")
    weak = relation(subject, obj, confidence=0.001)
    assert writer.write_relations([weak]) == 0
    assert writer.stats.relations_dropped_low_confidence == 1
    assert writer.summary.relations_dropped == 1


def test_write_relations_drops_predicates_outside_the_whitelist(writer):
    """A relationship type is interpolated into Cypher, so it must be checked."""

    class Tampered:
        rel_type = "OWNS]->(b) DETACH DELETE b //"
        confidence = 0.99
        subject = entity("Gazprom")
        obj = entity("Rosneft")

    assert writer.write_relations([Tampered()]) == 0
    assert writer.summary.relations_dropped == 1
    assert recorded(writer.client) == [], "no statement may be built for it"


def test_write_relations_groups_by_predicate(writer):
    gazprom, rosneft, putin = entity("Gazprom"), entity("Rosneft"), entity("Vladimir Putin", EntityType.PERSON)
    edges = [
        relation(gazprom, rosneft, RelationType.OWNS, confidence=0.8),
        relation(rosneft, gazprom, RelationType.OWNED_BY, confidence=0.6),
        relation(putin, gazprom, RelationType.OWNS, confidence=0.7),
    ]
    written = writer.write_relations(edges)

    assert written == 3
    assert writer.summary.relations_by_type == {"OWNS": 2, "OWNED_BY": 1}
    assert writer.stats.relations_written == 3
    queries = [entry["query_preview"] for entry in recorded(writer.client)]
    assert any("OWNS" in query for query in queries)
    assert any("OWNED_BY" in query for query in queries)


def test_persist_runs_the_steps_in_dependency_order(writer):
    document = Document(doc_id="doc-1", source_id="icij", url="https://example.test/a", text="Gazprom owns Rosneft.")
    subject, obj = entity("Gazprom"), entity("Rosneft")
    subject.doc_ids = {"doc-1"}
    obj.doc_ids = {"doc-1"}
    subject.mentions = [EntityMention(text="Gazprom", entity_type=EntityType.ORGANIZATION, start_char=0)]

    summary = writer.persist([document], [subject, obj], [relation(subject, obj, confidence=0.9)])

    previews = [entry["query_preview"] for entry in recorded(writer.client)]
    # Each step MATCHes what the previous one created, so order is load-bearing.
    positions = {
        "documents": next(i for i, q in enumerate(previews) if ":Document" in q),
        "entities": next(i for i, q in enumerate(previews) if "MERGE (e:Entity" in q),
        "mentions": next(i for i, q in enumerate(previews) if "MENTIONS" in q),
        "relations": next(i for i, q in enumerate(previews) if "-[r:OWNS]->" in q),
    }
    assert list(positions.values()) == sorted(positions.values()), positions
    assert summary.documents == 1
    assert summary.entities == 2
    assert summary.mentions == 1
    assert summary.relations == 1
    assert summary.seconds >= 0.0
    assert summary.to_dict()["seconds"] == round(summary.seconds, 3)


def test_persist_summary_is_serialisable(writer):
    summary = writer.persist([], [], [])
    payload = summary.to_dict()
    assert payload["entities"] == 0
    assert payload["relations_by_type"] == {}


def test_recent_content_hashes_reads_the_dedupe_window(writer):
    seen: dict = {}

    class StubClient(Neo4jClient):
        def read(self, query, params=None):
            assert query is schema.RECENT_CONTENT_HASHES
            seen.update(params or {})
            return [{"content_hash": "abc"}, {"content_hash": "def"}, {"content_hash": ""}]

    client = StubClient(writer.settings, sleeper=RecordingSleeper())
    graph = GraphWriter(client, client.settings, stats=IngestStats())

    assert graph.recent_content_hashes() == {"abc", "def"}, "empty hashes are not ids"
    assert seen["since"], "the window must be passed to Cypher"
    assert seen["since"][:4].isdigit(), seen["since"]


def test_recent_content_hashes_degrades_when_the_read_fails(writer):
    client = OfflineReadClient(writer.settings, sleeper=RecordingSleeper())
    graph = GraphWriter(client, writer.settings, stats=IngestStats())
    assert graph.recent_content_hashes() == set(), "an unreadable index must not fail the run"


def test_recent_content_hashes_honours_an_explicit_window(writer):
    seen: dict = {}

    class Spy(Neo4jClient):
        def read(self, query, params=None):
            seen.update(params or {})
            return []

    client = Spy(writer.settings, sleeper=RecordingSleeper())
    graph = GraphWriter(client, client.settings, stats=IngestStats())

    graph.recent_content_hashes(days=1)
    one_day = seen["since"]
    graph.recent_content_hashes(days=365)
    one_year = seen["since"]
    assert one_year < one_day, "a wider window must reach further back"

    # A non-positive window means "since now" — effectively dedupe-free.
    graph.recent_content_hashes(days=0)
    assert seen["since"] > one_day


def test_ensure_schema_respects_the_feature_flag(dry_settings):
    settings = dataclasses.replace(dry_settings, neo4j_ensure_schema=False)
    client, _, _ = make_client(settings)
    graph = GraphWriter(client, settings, stats=IngestStats())
    graph.ensure_schema()
    assert recorded(client) == []


def test_ensure_schema_emits_ddl_when_enabled(writer):
    writer.ensure_schema()
    statements = [entry["query_preview"] for entry in recorded(writer.client)]
    assert len(statements) == len(ensure_schema_statements())
    assert all(entry["kind"] == "schema" for entry in recorded(writer.client))
