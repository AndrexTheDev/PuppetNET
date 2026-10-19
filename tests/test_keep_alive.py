"""AuraDB Free keep-alive: a minimal, read-only ``RETURN 1`` heartbeat.

The tests pin three properties that make the heartbeat safe to run forever:

* it issues exactly one statement, and that statement is ``RETURN 1``;
* it never writes — a heartbeat that writes would consume the node budget;
* it fails loudly (exit 2) when the database does not answer, instead of
  turning a paused instance into a green check.
"""

from __future__ import annotations

import json

import pytest

import keep_alive as ka


class StubClient:
    def __init__(self, *, rows=None, error: Exception | None = None, database: str = "neo4j") -> None:
        self.rows = rows if rows is not None else [{"ok": 1}]
        self.error = error
        self.database = database
        self.queries: list[str] = []
        self.closed = False

    def read(self, query, params=None):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return list(self.rows)

    def close(self):
        self.closed = True


class FakeClock:
    def __init__(self, *ticks: float) -> None:
        self.ticks = list(ticks)
        self.index = 0

    def __call__(self) -> float:
        value = self.ticks[min(self.index, len(self.ticks) - 1)]
        self.index += 1
        return value


def test_ping_issues_only_return_one():
    client = StubClient()
    result = ka.ping_once(client)
    assert result.ok is True
    assert client.queries == ["RETURN 1 AS ok"]
    assert ka.PING == "RETURN 1 AS ok"
    assert not hasattr(client, "writes")


def test_ping_reports_the_database_and_attempt():
    result = ka.ping_once(StubClient(database="puppetnet"))
    assert result.database == "puppetnet"
    assert result.attempts == 1
    assert result.latency_ms >= 0


def test_a_wrong_answer_is_a_failure():
    result = ka.ping_once(StubClient(rows=[{"ok": 0}]))
    assert result.ok is False
    assert "unexpected response" in result.error


def test_an_empty_response_is_a_failure():
    result = ka.ping_once(StubClient(rows=[]))
    assert result.ok is False


def test_a_driver_error_is_reported_not_raised():
    result = ka.ping_once(StubClient(error=RuntimeError("database paused")))
    assert result.ok is False
    assert "database paused" in result.error


def test_a_slow_first_response_is_reported_as_a_resume():
    clock = FakeClock(0.0, 12.0)
    result = ka.ping_once(StubClient(), clock=clock)
    assert result.ok is True
    assert result.woke_from_pause is True
    assert result.latency_ms == pytest.approx(12_000, abs=1)


def test_a_fast_response_is_not_a_resume():
    clock = FakeClock(0.0, 0.120)
    result = ka.ping_once(StubClient(), clock=clock)
    assert result.woke_from_pause is False


def test_github_output_is_namespaced(tmp_path, monkeypatch):
    output = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    ka.emit_github_output(ka.KeepAliveResult(ok=True, checked_at="now", latency_ms=12.5))
    content = output.read_text(encoding="utf-8")
    assert "keep_alive_ok=true" in content
    assert "keep_alive_latency_ms=12.5" in content
    assert "\nok=" not in content


def test_cli_refuses_a_dry_run(monkeypatch):
    """A dry run opens no socket, so it cannot reset the inactivity clock."""
    monkeypatch.setenv("DRY_RUN", "true")
    assert ka.main(["--json"]) == ka.EXIT_CONFIG


def test_cli_pings_and_reports_json(monkeypatch, capsys):
    client = StubClient()
    monkeypatch.setattr(ka, "_build_client", lambda settings, **kwargs: client)
    monkeypatch.setattr(ka, "_describe", lambda client: None)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("NEO4J_URI", "neo4j+s://example.invalid:7687")
    monkeypatch.setenv("NEO4J_PASSWORD", "secret")
    assert ka.main(["--json"]) == ka.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert client.queries == ["RETURN 1 AS ok"]


def test_cli_exits_two_when_the_database_does_not_answer(monkeypatch):
    client = StubClient(error=RuntimeError("unreachable"))
    monkeypatch.setattr(ka, "_build_client", lambda settings, **kwargs: client)
    monkeypatch.setattr(ka, "_describe", lambda client: None)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("NEO4J_URI", "neo4j+s://example.invalid:7687")
    monkeypatch.setenv("NEO4J_PASSWORD", "secret")
    monkeypatch.setenv("KEEP_ALIVE_RETRIES", "0")
    assert ka.main(["--json"]) == ka.EXIT_RUNTIME


def test_cli_version_exits_zero(capsys):
    assert ka.main(["--version"]) == ka.EXIT_OK
    assert "keep-alive" in capsys.readouterr().out
