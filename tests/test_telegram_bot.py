"""Telegram alert engine: formatting, suppression ledger, transport, policy.

No test here reaches the network — the Bot API is not reachable from CI, and a
test that depends on it would be a test that silently never runs. Transport is
exercised through :class:`StubFetchClient`, which returns scripted
:class:`~puppetnet.net.proxy_client.FetchResult` objects, including the 429
envelope Telegram actually sends (``parameters.retry_after``).

The security-relevant behaviour — token redaction in logs, never routing the
token through the edge relay, never persisting it — is asserted explicitly,
because those are the properties that must not regress silently.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import telegram_bot as tb
from puppetnet.net.proxy_client import FetchResult

UTC = timezone.utc
TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def hours_ago(hours: float) -> str:
    return iso(datetime.now(UTC) - timedelta(hours=hours))


# --------------------------------------------------------------------------- #
# Doubles
# --------------------------------------------------------------------------- #


class StubDelayQueue:
    """Records the politeness policy the sender installs for its host."""

    def __init__(self) -> None:
        self.policies: dict[str, dict] = {}

    def set_host_policy(self, host: str, rate_per_sec=None, burst=None) -> None:
        self.policies[host] = {"rate_per_sec": rate_per_sec, "burst": burst}


class StubFetchClient:
    """API-compatible stand-in for :class:`FetchClient`."""

    def __init__(self, responses=None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict] = []
        self.closed = False
        self.delay_queue = StubDelayQueue()

    def request(self, url: str, **kwargs) -> FetchResult:
        self.calls.append({"url": url, **kwargs})
        if self.responses:
            item = self.responses.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return ok_result()

    def close(self) -> None:
        self.closed = True


def ok_result(message_id: int = 4242) -> FetchResult:
    return FetchResult(
        url="https://api.telegram.org/bot<TOKEN>/sendMessage",
        status=200,
        ok=True,
        content_type="application/json",
        text=json.dumps({"ok": True, "result": {"message_id": message_id}}),
    )


def error_result(status: int, description: str, *, retry_after: float | None = None) -> FetchResult:
    payload: dict = {"ok": False, "error_code": status, "description": description}
    if retry_after is not None:
        payload["parameters"] = {"retry_after": retry_after}
    return FetchResult(
        url="https://api.telegram.org/bot<TOKEN>/sendMessage",
        status=status,
        ok=False,
        content_type="application/json",
        text=json.dumps(payload),
        error=f"HTTP {status}",
    )


def bridge_alert(**overrides) -> dict:
    alert = {
        "canonical_key": "ORGANIZATION:kastelion",
        "name": "Kastelion Overseas Ltd",
        "entity_type": "organization",
        "labels": ["Entity", "ShellCompany"],
        "first_seen": hours_ago(3),
        "cluster_id": "cluster:0011",
        "bridged_clusters": ["c1", "c2"],
        "bridged_cluster_names": ["c1 (Alpha One, Alpha Two)", "c2 (Beta One)"],
        "neighbours": ["Alpha One", "Beta One"],
        "degree": 9,
        "articulation_point": True,
        "anomaly_score": 0.51,
        "betweenness": 0.188,
        "offshore_cluster_ratio": 0.9,
        "jurisdiction": "VG",
        "jurisdiction_class": "secrecy",
        "reasons": ["articulation point", "2 distinct neighbour clusters"],
        "dedupe_key": "bridge:ORGANIZATION:kastelion:c1|c2",
    }
    alert.update(overrides)
    return alert


def anomaly_row(key: str, score: float, **overrides) -> dict:
    row = {
        "canonical_key": key,
        "name": key.replace("ORGANIZATION:", "").title(),
        "entity_type": "organization",
        "labels": ["Entity", "Offshore"],
        "anomaly_score": score,
        "components": {
            "betweenness": score / 2,
            "degree_spike": 0.2,
            "offshore_cluster_ratio": 0.8,
            "offshore_neighbour_ratio": 0.7,
        },
        "reasons": ["offshore cluster"],
        "degree": 7,
        "cluster_id": "c1",
        "cluster_size": 12,
        "jurisdictions": ["PA"],
        "neighbours": ["Alpha One"],
    }
    row.update(overrides)
    return row


def report(*, bridges=None, top=None) -> dict:
    centrality = {
        "nodes_scored": 128,
        "clusters": 9,
        "engine": "python",
        "weights": {"betweenness": 0.40, "degree_spike": 0.35, "offshore_ratio": 0.25},
    }
    payload = {
        "run_id": "maint-20260927T040000Z",
        "generated_at": hours_ago(1),
        "status": "completed",
        "bridge_alerts": list(bridges or []),
        "top_anomalies": list(top or []),
        "centrality": centrality,
    }
    if top is None:
        centrality["top"] = []
    return payload


def sender(settings, responses=None, *, dry_run=False, token: str = TOKEN, sleeps=None) -> tuple:
    client = StubFetchClient(responses)
    bot = tb.TelegramSender(
        settings,
        token,
        client=client,
        dry_run=dry_run,
        sleeper=sleeps.append if sleeps is not None else (lambda _seconds: None),
    )
    return bot, client


def ledger_at(tmp_path: Path, *, suppression_hours: float = 24.0, persist: bool = True) -> tb.AlertLedger:
    return tb.AlertLedger.load(tmp_path / "telegram_alerts.json", suppression_hours=suppression_hours, persist=persist)


def engine_for(settings, tmp_path, *, chats=("111", "222"), responses=None, force=False, **kwargs) -> tuple:
    bot, client = sender(settings, responses)
    ledger = ledger_at(tmp_path)
    alert_engine = tb.AlertEngine(settings, bot, ledger, chats=list(chats), force=force, **kwargs)
    return alert_engine, bot, client, ledger


# --------------------------------------------------------------------------- #
# Formatting primitives
# --------------------------------------------------------------------------- #


def test_escape_neutralises_html_in_entity_names():
    """An unescaped ``<`` makes Telegram reject the whole alert with a 400."""
    assert tb.escape("Acme & Sons <holding>") == "Acme &amp; Sons &lt;holding&gt;"
    assert tb.escape(None) == ""
    assert tb.escape(42) == "42"


def test_tag_wraps_and_escapes():
    assert tb.tag("A <b> B") == "<b>A &lt;b&gt; B</b>"


def test_bullet_skips_empty_values_and_formats_numbers():
    assert tb.bullet("Degree", "") == ""
    assert tb.bullet("Degree", None) == ""
    assert tb.bullet("Degree", []) == ""
    assert tb.bullet("Betweenness", 0.123456, precision=4) == "• Betweenness: 0.1235"
    assert tb.bullet("Neighbours", ["A", "B"]) == "• Neighbours: A, B"
    assert tb.bullet("Flag", True) == "• Flag: yes"


def test_compact_drops_empty_lines():
    assert tb.compact(["a", "", "b"]) == "a\nb"


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #


def test_short_text_is_one_chunk():
    assert tb.chunk_text("hello") == ["hello"]
    assert tb.chunk_text("") == []


def test_long_text_is_split_under_the_api_limit():
    text = "\n".join(f"• line {index} with some padding text" for index in range(400))
    chunks = tb.chunk_text(text)
    assert len(chunks) > 1
    assert all(len(chunk) <= tb.MAX_MESSAGE_CHARS for chunk in chunks)
    # Every original line survives exactly once, in order.
    rejoined = "\n".join(chunk for chunk in chunks)
    for index in range(400):
        assert f"• line {index} " in rejoined
    assert f"<i>(1/{len(chunks)})</i>" in chunks[0]


def test_chunking_never_splits_inside_a_tag():
    """A stray ``<`` is a rejected message, so oversized lines break at spaces."""
    long_value = " ".join(f"token{index}" for index in range(900))
    text = f"<b>header</b>\n<code>{long_value}</code>"
    chunks = tb.chunk_text(text)
    assert all(len(chunk) <= tb.MAX_MESSAGE_CHARS for chunk in chunks)
    assert all(chunk.count("<") == chunk.count(">") or "<code>" in chunk or "</code>" in chunk for chunk in chunks)


def test_splits_inside_tag_detector():
    assert tb._splits_inside_tag("abc <b>def", 6) is True
    assert tb._splits_inside_tag("abc <b>def", 4) is False
    assert tb._splits_inside_tag("plain text", 5) is False


def test_an_unbreakable_long_line_is_still_cut():
    text = "x" * (tb.MAX_MESSAGE_CHARS * 2 + 50)
    chunks = tb.chunk_text(text)
    assert len(chunks) >= 2
    assert all(len(chunk) <= tb.MAX_MESSAGE_CHARS for chunk in chunks)
    assert "".join(chunks).count("x") >= len(text) - tb.CHUNK_SUFFIX_RESERVE * len(chunks)


# --------------------------------------------------------------------------- #
# Chat ids and token redaction
# --------------------------------------------------------------------------- #


def test_parse_chat_ids_accepts_the_common_spellings():
    assert tb.parse_chat_ids("-1001234567890, @puppetnet_alerts") == ["-1001234567890", "@puppetnet_alerts"]
    assert tb.parse_chat_ids("111\n222 333") == ["111", "222", "333"]
    assert tb.parse_chat_ids(["444", "555"]) == ["444", "555"]
    assert tb.parse_chat_ids("") == []
    assert tb.parse_chat_ids(None) == []


def test_redact_token_hides_the_configured_token_and_lookalikes():
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert TOKEN not in tb.redact_token(url, TOKEN)
    assert "<REDACTED>" in tb.redact_token(url, TOKEN)
    # Even without knowing the token, a bot-token-shaped string is scrubbed.
    assert TOKEN not in tb.redact_token(url)


def test_token_redactor_scrubs_library_log_records(caplog):
    """FetchClient logs the URL it gave up on — the token must not survive that."""
    redactor = tb.install_token_redactor(TOKEN)
    try:
        with caplog.at_level(logging.ERROR, logger="puppetnet"):
            logging.getLogger("puppetnet.net.client").error("giving up on %s after 3 attempts", f"bot{TOKEN}/sendMessage")
        assert TOKEN not in caplog.text
        assert "<REDACTED>" in caplog.text
    finally:
        tb.uninstall_token_redactor(redactor)


def test_install_token_redactor_is_idempotent():
    first = tb.install_token_redactor(TOKEN)
    second = tb.install_token_redactor(TOKEN)
    try:
        assert first is second
        assert sum(isinstance(f, tb.TokenRedactor) for f in logging.getLogger().filters) == 1
    finally:
        tb.uninstall_token_redactor(first)


def test_redactor_never_breaks_logging_on_odd_records():
    redactor = tb.TokenRedactor(TOKEN)
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "%s %d", ("text", 7), None)
    assert redactor.filter(record) is True


# --------------------------------------------------------------------------- #
# Report access
# --------------------------------------------------------------------------- #


def test_find_latest_report_picks_the_newest(tmp_path):
    old = tmp_path / "graph_maintenance_a.json"
    new = tmp_path / "graph_maintenance_b.json"
    old.write_text("{}")
    new.write_text("{}")
    import os

    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))
    assert tb.find_latest_report(tmp_path) == new
    assert tb.find_latest_report(tmp_path / "missing") is None


def test_load_report_rejects_missing_and_broken_files(tmp_path):
    with pytest.raises(FileNotFoundError):
        tb.load_report(tmp_path / "nope.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    with pytest.raises(ValueError):
        tb.load_report(broken)
    not_object = tmp_path / "list.json"
    not_object.write_text("[]")
    with pytest.raises(ValueError):
        tb.load_report(not_object)


def test_report_age_hours(tmp_path):
    assert tb.report_age_hours({"generated_at": hours_ago(5)}) == pytest.approx(5.0, abs=0.05)
    assert tb.report_age_hours({}) == float("inf")


def test_report_bridge_alerts_sort_by_betweenness():
    alerts = tb.report_bridge_alerts(
        report(bridges=[bridge_alert(betweenness=0.1), bridge_alert(betweenness=0.9, canonical_key="other")])
    )
    assert [alert["canonical_key"] for alert in alerts] == ["other", "ORGANIZATION:kastelion"]
    assert tb.report_bridge_alerts({}) == []


def test_report_top_anomalies_limits_and_falls_back():
    rows = [anomaly_row(f"ORGANIZATION:n{index}", 0.9 - index * 0.1) for index in range(8)]
    assert len(tb.report_top_anomalies(report(top=rows), 5)) == 5
    fallback = report(top=[])
    fallback["centrality"]["top"] = rows
    assert len(tb.report_top_anomalies(fallback, 3)) == 3
    assert tb.report_top_anomalies({}, 5) == []


def test_resolve_report_path(tmp_path, settings):
    (tmp_path / "graph_maintenance_x.json").write_text("{}")
    assert tb.resolve_report_path(settings, "", str(tmp_path)).name == "graph_maintenance_x.json"
    explicit = tmp_path / "specific.json"
    explicit.write_text("{}")
    assert tb.resolve_report_path(settings, str(explicit), str(tmp_path)) == explicit
    with pytest.raises(FileNotFoundError):
        tb.resolve_report_path(settings, "", str(tmp_path / "empty"))


# --------------------------------------------------------------------------- #
# Message formatting
# --------------------------------------------------------------------------- #


def test_bridge_alert_message_carries_the_evidence():
    text = tb.format_bridge_alert(bridge_alert(), rank=1, total=1)
    assert "NEW CLUSTER BRIDGE" in text
    assert "Kastelion Overseas Ltd" in text
    assert "c1 (Alpha One, Alpha Two)" in text
    assert "0.1880" in text          # betweenness
    assert "secrecy" in text         # jurisdiction class
    assert "bridge:ORGANIZATION:kastelion:c1|c2" in text


def test_bridge_alert_message_escapes_hostile_names():
    text = tb.format_bridge_alert(bridge_alert(name='<script>alert("x")</script> & Co'))
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_bridge_alert_message_numbers_itself_in_a_batch():
    assert "1/3" in tb.format_bridge_alert(bridge_alert(), rank=1, total=3).split("\n")[0]
    assert "3/3" in tb.format_bridge_alert(bridge_alert(), rank=3, total=3).split("\n")[0]
    single = tb.format_bridge_alert(bridge_alert(), rank=1, total=1).split("\n")[0]
    assert single == "<b>🚨 NEW CLUSTER BRIDGE</b>", "a lone alert carries no rank"


def test_bridge_alert_accepts_the_plain_clusters_key():
    alert = bridge_alert()
    del alert["bridged_cluster_names"]
    del alert["bridged_clusters"]
    alert["clusters"] = ["c9", "c8"]
    assert "c9" in tb.format_bridge_alert(alert)


def test_digest_lists_the_top_rows_and_the_run_metadata():
    rows = [anomaly_row(f"ORGANIZATION:n{index}", 0.9 - index * 0.1) for index in range(5)]
    text = tb.format_digest(report(top=rows), rows, top_n=5, window_hours=24)
    assert "DAILY ANOMALY DIGEST" in text
    assert "maint-20260927T040000Z" in text
    assert "betweenness 0.40 / spike 0.35 / offshore 0.25" in text
    for index in range(5):
        assert f"{index + 1}. " in text
    assert "N0" in text


def test_digest_says_so_when_nothing_is_anomalous():
    text = tb.format_digest(report(top=[]), [], top_n=5)
    assert "No nodes crossed the anomaly threshold" in text


def test_digest_only_uses_allowed_telegram_tags():
    rows = [anomaly_row("ORGANIZATION:n0", 0.9)]
    text = tb.format_digest(report(top=rows), rows)
    import re

    for tag_name in set(re.findall(r"</?([a-zA-Z]+)", text)):
        assert tag_name.lower() in tb._ALLOWED_TAGS, tag_name


def test_status_report_summary():
    text = tb.format_status_report({"bot_username": "puppetnet_bot", "chats": ["111"], "bridge_sent": 2,
                                    "bridge_suppressed": 1, "digest_sent": 1, "failures": 0, "dry_run": False})
    assert "puppetnet_bot" in text and "111" in text


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #


def test_ledger_starts_empty_and_tolerates_corruption(tmp_path):
    path = tmp_path / "telegram_alerts.json"
    assert tb.AlertLedger.load(path).data["sent"] == {}
    path.write_text("{not json")
    ledger = tb.AlertLedger.load(path)
    assert ledger.data["sent"] == {}


def test_ledger_suppresses_inside_the_window_only(tmp_path):
    ledger = ledger_at(tmp_path, suppression_hours=24.0)
    assert ledger.suppressed("bridge:x") is False
    ledger.mark_sent("bridge:x")
    assert ledger.suppressed("bridge:x") is True
    later = datetime.now(UTC) + timedelta(hours=25)
    assert ledger.suppressed("bridge:x", now=later) is False
    # A clock that runs behind the ledger entry must not un-suppress the alert.
    earlier = datetime.now(UTC) - timedelta(hours=25)
    assert ledger.suppressed("bridge:x", now=earlier) is True


def test_ledger_digest_guard_is_per_chat_per_utc_day(tmp_path):
    ledger = ledger_at(tmp_path)
    assert ledger.digest_sent_today("111") is False
    ledger.mark_digest("111")
    assert ledger.digest_sent_today("111") is True
    assert ledger.digest_sent_today("222") is False
    tomorrow = datetime.now(UTC) + timedelta(days=1)
    assert ledger.digest_sent_today("111", now=tomorrow) is False


def test_ledger_round_trips_through_disk(tmp_path):
    path = tmp_path / "telegram_alerts.json"
    ledger = tb.AlertLedger.load(path)
    ledger.mark_sent("bridge:x")
    ledger.mark_digest("111")
    ledger.bump("bridge_alerts_sent", 3)
    assert ledger.save() is True
    reloaded = tb.AlertLedger.load(path)
    assert reloaded.suppressed("bridge:x") is True
    assert reloaded.data["counters"]["bridge_alerts_sent"] == 3


def test_ledger_save_is_atomic_and_survives_a_readonly_directory(tmp_path):
    ledger = tb.AlertLedger.load(tmp_path / "sub" / "telegram_alerts.json")
    ledger.mark_sent("bridge:x")
    assert ledger.save() is True
    assert not list(tmp_path.glob("*.tmp"))
    assert not list((tmp_path / "sub").glob("*.tmp"))


def test_ledger_does_not_persist_when_asked_not_to(tmp_path):
    path = tmp_path / "telegram_alerts.json"
    ledger = tb.AlertLedger.load(path, persist=False)
    ledger.mark_sent("bridge:x")
    assert ledger.save() is True
    assert not path.exists(), "a dry run must not consume the suppression window"


def test_ledger_prune_drops_ancient_entries(tmp_path):
    ledger = ledger_at(tmp_path, suppression_hours=24.0)
    ledger.data["sent"]["old"] = iso(datetime.now(UTC) - timedelta(days=40))
    ledger.data["sent"]["new"] = iso(datetime.now(UTC))
    assert ledger.prune() == 1
    assert "old" not in ledger.data["sent"]
    assert "new" in ledger.data["sent"]


def test_ledger_describe_is_json_safe(tmp_path):
    ledger = ledger_at(tmp_path)
    ledger.mark_sent("bridge:x")
    payload = json.loads(json.dumps(ledger.describe()))
    assert payload["tracked_alerts"] == 1


# --------------------------------------------------------------------------- #
# Sender / transport
# --------------------------------------------------------------------------- #


def test_method_url_carries_the_token_and_host_is_derived(settings):
    bot, _client = sender(settings)
    assert bot.method_url("sendMessage").endswith(f"/bot{TOKEN}/sendMessage")
    assert bot.host == "api.telegram.org"
    bot.api_base = "https://telegram.example.test/api"
    assert bot.host == "telegram.example.test"


def test_sender_installs_a_politeness_policy_for_its_host(settings):
    bot, client = sender(settings)
    assert client.delay_queue.policies["api.telegram.org"]["rate_per_sec"] == pytest.approx(tb.DEFAULT_RATE_PER_SEC)
    assert client.delay_queue.policies["api.telegram.org"]["burst"] == pytest.approx(tb.DEFAULT_BURST)


def test_send_message_posts_html_without_link_previews(settings):
    bot, client = sender(settings, [ok_result(7)])
    outcome = bot.send_text("111", "<b>hello</b>", kind="bridge", dedupe_key="k")
    assert outcome.ok is True
    assert outcome.message_ids == [7]
    call = client.calls[0]
    assert call["method"] == "POST"
    assert call["json_body"]["chat_id"] == "111"
    assert call["json_body"]["parse_mode"] == "HTML"
    assert call["json_body"]["disable_web_page_preview"] is True
    assert call["mode"] == "bot"


def test_the_token_is_never_routed_through_the_edge_relay(settings):
    """Sending the token to a third-party worker would leak a live credential."""
    bot, client = sender(settings, [ok_result()])
    bot.send_text("111", "hello")
    assert client.calls[0]["allow_worker"] is False
    assert client.calls[0]["respect_robots"] is False


def test_long_messages_are_sent_as_several_calls(settings):
    text = "\n".join(f"• line {index} padded out to a reasonable length here" for index in range(300))
    bot, client = sender(settings, [ok_result(1), ok_result(2), ok_result(3), ok_result(4)])
    outcome = bot.send_text("111", text)
    assert outcome.ok is True
    assert outcome.chunks == len(client.calls) > 1
    assert outcome.message_ids == [1, 2, 3, 4][: len(client.calls)]


def test_dry_run_performs_no_http_at_all(settings, capsys):
    bot, client = sender(settings, dry_run=True)
    outcome = bot.send_text("111", "<b>dry</b>", kind="digest")
    assert outcome.ok is True and outcome.dry_run is True
    assert client.calls == []
    assert "[dry-run]" in capsys.readouterr().out


def test_429_is_honoured_with_the_server_retry_after(settings):
    sleeps: list[float] = []
    bot, client = sender(settings, [error_result(429, "Too Many Requests", retry_after=17), ok_result()], sleeps=sleeps)
    outcome = bot.send_text("111", "hello")
    assert outcome.ok is True
    assert sleeps == [17.0]
    assert len(client.calls) == 2
    assert bot.stats["throttled"] == 1


def test_retry_after_is_capped(settings):
    sleeps: list[float] = []
    bot, _client = sender(
        settings,
        [error_result(429, "slow down", retry_after=9999), ok_result()],
        sleeps=sleeps,
    )
    bot.send_text("111", "hello")
    assert sleeps == [tb.MAX_RETRY_AFTER_SECONDS]


def test_a_bad_chat_is_not_retried(settings):
    sleeps: list[float] = []
    bot, client = sender(settings, [error_result(400, "Bad Request: chat not found")], sleeps=sleeps)
    outcome = bot.send_text("nope", "hello")
    assert outcome.ok is False
    assert "chat not found" in outcome.error
    assert len(client.calls) == 1
    assert sleeps == []


def test_a_rejected_token_raises_instead_of_hammering(settings):
    bot, client = sender(settings, [error_result(401, "Unauthorized")])
    with pytest.raises(tb.TelegramAuthError):
        bot.send_text("111", "hello")
    assert len(client.calls) == 1


def test_transport_errors_are_retried_and_then_reported(settings):
    """A dropped connection is retried; exhausting the retries is a failure, not a crash."""
    sleeps: list[float] = []
    bot, client = sender(settings, [error_result(0, "boom")] * 3, sleeps=sleeps)
    outcome = bot.send_text("111", "hello")
    assert outcome.ok is False
    assert outcome.error
    assert len(client.calls) == 3
    assert len(sleeps) == 2


def test_a_failed_chunk_stops_the_rest_of_the_message(settings):
    """A half-delivered alert must be reported as a failure, not as success."""
    bot, client = sender(settings, [ok_result(1), error_result(400, "Bad Request: message is too long")])
    text = "\n".join(f"• line {index} padded out to a reasonable length here" for index in range(300))
    outcome = bot.send_text("111", text)
    assert outcome.ok is False
    assert len(outcome.message_ids) == 1
    assert len(client.calls) == 2


def test_get_me_returns_the_bot_identity(settings):
    body = {"ok": True, "result": {"id": 7, "username": "puppetnet_bot", "first_name": "PuppetNET"}}
    bot, _client = sender(settings, [FetchResult(url="u", status=200, ok=True, content_type="application/json",
                                                 text=json.dumps(body))])
    identity = bot.get_me()
    assert identity["username"] == "puppetnet_bot"
    assert identity["ok"] is True


def test_get_me_in_dry_run_does_not_call_out(settings):
    bot, client = sender(settings, dry_run=True)
    assert bot.get_me() == {"dry_run": True}
    assert client.calls == []


def test_a_non_json_response_does_not_crash_the_sender(settings):
    bot, _client = sender(settings, [FetchResult(url="u", status=200, ok=True, content_type="text/html",
                                                 text="<html>maintenance</html>")])
    outcome = bot.send_text("111", "hello")
    assert outcome.ok is True
    assert outcome.message_ids == []


def test_sender_close_releases_an_owned_client_only(settings):
    injected = StubFetchClient()
    bot = tb.TelegramSender(settings, TOKEN, client=injected)
    bot.close()
    assert injected.closed is False, "an injected client belongs to the caller"
    owned = tb.TelegramSender(settings, TOKEN, client=None, dry_run=True)
    owned.close()


# --------------------------------------------------------------------------- #
# Engine policy
# --------------------------------------------------------------------------- #


def test_select_bridges_suppresses_repeats(settings, tmp_path):
    alert_engine, _bot, _client, ledger = engine_for(settings, tmp_path, responses=[ok_result()])
    payload = report(bridges=[bridge_alert()])
    selected, suppressed, capped = alert_engine.select_bridges(payload)
    assert len(selected) == 1 and suppressed == 0 and capped == 0
    ledger.mark_sent(selected[0]["dedupe_key"])
    selected, suppressed, _capped = alert_engine.select_bridges(payload)
    assert selected == [] and suppressed == 1


def test_force_ignores_suppression(settings, tmp_path):
    alert_engine, _bot, _client, ledger = engine_for(settings, tmp_path, force=True, responses=[ok_result()])
    ledger.mark_sent(bridge_alert()["dedupe_key"])
    selected, suppressed, _capped = alert_engine.select_bridges(report(bridges=[bridge_alert()]))
    assert len(selected) == 1 and suppressed == 0


def test_bridge_limit_defers_the_weakest_alerts(settings, tmp_path):
    alerts = [bridge_alert(canonical_key=f"k{index}", dedupe_key=f"bridge:k{index}", betweenness=index / 10)
              for index in range(5)]
    alert_engine, _bot, _client, _ledger = engine_for(settings, tmp_path, bridge_limit=2, responses=[ok_result()] * 9)
    selected, _suppressed, capped = alert_engine.select_bridges(report(bridges=alerts))
    assert capped == 3
    assert [alert["canonical_key"] for alert in selected] == ["k4", "k3"]


def test_the_same_node_bridging_new_clusters_is_a_new_alert(settings, tmp_path):
    alert_engine, _bot, _client, ledger = engine_for(settings, tmp_path, responses=[ok_result()] * 4)
    first = bridge_alert()
    second = bridge_alert(bridged_clusters=["c1", "c3"], bridged_cluster_names=["c1 (a)", "c3 (b)"],
                          dedupe_key="bridge:ORGANIZATION:kastelion:c1|c3")
    ledger.mark_sent(first["dedupe_key"])
    selected, suppressed, _capped = alert_engine.select_bridges(report(bridges=[first, second]))
    assert suppressed == 1
    assert [alert["dedupe_key"] for alert in selected] == [second["dedupe_key"]]


def test_bridge_key_falls_back_when_the_report_has_none(settings, tmp_path):
    alert_engine, _bot, _client, _ledger = engine_for(settings, tmp_path)
    alert = bridge_alert()
    del alert["dedupe_key"]
    assert alert_engine._bridge_key(alert) == "bridge:ORGANIZATION:kastelion:c1 (Alpha One, Alpha Two)|c2 (Beta One)"


def test_push_bridges_sends_to_every_chat_and_marks_once(settings, tmp_path):
    alert_engine, _bot, client, ledger = engine_for(settings, tmp_path, chats=("111", "222"),
                                                    responses=[ok_result(), ok_result()])
    outcomes = alert_engine.push_bridges(report(bridges=[bridge_alert()]))
    assert len(outcomes) == 2
    assert all(outcome.ok for outcome in outcomes)
    assert [call["json_body"]["chat_id"] for call in client.calls] == ["111", "222"]
    assert ledger.data["sent"][bridge_alert()["dedupe_key"]]


def test_digest_is_sent_once_per_day(settings, tmp_path):
    alert_engine, bot, client, ledger = engine_for(settings, tmp_path, chats=("111",),
                                                   responses=[ok_result(), ok_result()])
    payload = report(top=[anomaly_row("ORGANIZATION:n0", 0.9)])
    assert len(alert_engine.send_digest(payload)) == 1
    assert ledger.digest_sent_today("111") is True
    assert alert_engine.send_digest(payload) == []
    assert len(client.calls) == 1


def test_digest_reaches_a_second_chat_that_has_not_had_one(settings, tmp_path):
    alert_engine, _bot, client, _ledger = engine_for(settings, tmp_path, chats=("111", "222"),
                                                     responses=[ok_result(), ok_result()])
    alert_engine.send_digest(report(top=[anomaly_row("ORGANIZATION:n0", 0.9)]))
    assert [call["json_body"]["chat_id"] for call in client.calls] == ["111", "222"]


def test_run_reports_a_failure_when_no_chat_is_configured(settings, tmp_path):
    alert_engine, _bot, client, _ledger = engine_for(settings, tmp_path, chats=())
    run_report = alert_engine.run(report())
    assert run_report.status == "failed"
    assert any("no chat ids" in error for error in run_report.errors)
    assert client.calls == []


def test_run_keeps_going_when_one_chat_is_unreachable(settings, tmp_path):
    alert_engine, _bot, client, ledger = engine_for(
        settings, tmp_path, chats=("bad", "good"),
        responses=[error_result(400, "Bad Request: chat not found"), ok_result(), ok_result(), ok_result()],
    )
    run_report = alert_engine.run(report(bridges=[bridge_alert()], top=[anomaly_row("ORGANIZATION:n0", 0.9)]))
    assert run_report.failures == 1
    assert run_report.bridge_sent == 1
    assert run_report.status == "partial"
    assert ledger.digest_sent_today("good") is True


def test_run_stops_on_an_authorisation_failure(settings, tmp_path):
    alert_engine, _bot, client, _ledger = engine_for(
        settings, tmp_path, chats=("111", "222"), responses=[error_result(401, "Unauthorized")]
    )
    run_report = alert_engine.run(report(bridges=[bridge_alert()], top=[anomaly_row("ORGANIZATION:n0", 0.9)]))
    assert run_report.status == "failed"
    assert any("rejected the bot token" in error for error in run_report.errors)
    assert len(client.calls) == 1, "a dead token must not be retried per chat"


def test_run_counts_bridges_and_digest_rows(settings, tmp_path):
    rows = [anomaly_row(f"ORGANIZATION:n{index}", 0.9 - index * 0.1) for index in range(5)]
    alert_engine, _bot, _client, _ledger = engine_for(
        settings, tmp_path, chats=("111",), responses=[ok_result()] * 3, digest_top_n=5
    )
    run_report = alert_engine.run(report(bridges=[bridge_alert()], top=rows))
    assert run_report.status == "completed"
    assert run_report.bridge_alerts_available == 1
    assert run_report.bridge_sent == 1
    assert run_report.digest_sent == 1
    assert run_report.digest_rows == 5
    assert run_report.report_run_id == "maint-20260927T040000Z"
    assert run_report.report_age_hours == pytest.approx(1.0, abs=0.1)


def test_run_report_is_json_serialisable(settings, tmp_path):
    alert_engine, _bot, _client, _ledger = engine_for(settings, tmp_path, chats=("111",), responses=[ok_result()] * 2)
    run_report = alert_engine.run(report(bridges=[bridge_alert()]))
    payload = json.loads(json.dumps(run_report.to_dict()))
    assert payload["outcomes"][0]["chat_id"] == "111"


def test_a_surprising_exception_in_one_pass_does_not_kill_the_other(settings, tmp_path, monkeypatch):
    alert_engine, _bot, _client, _ledger = engine_for(settings, tmp_path, chats=("111",), responses=[ok_result()])

    def explode(_report):
        raise RuntimeError("formatter exploded")

    monkeypatch.setattr(alert_engine, "push_bridges", explode)
    run_report = alert_engine.run(report(bridges=[bridge_alert()], top=[anomaly_row("ORGANIZATION:n0", 0.9)]))
    assert any("formatter exploded" in error for error in run_report.errors)
    assert run_report.digest_sent == 1


def test_error_text_never_contains_the_token(settings, tmp_path, monkeypatch):
    alert_engine, _bot, _client, _ledger = engine_for(settings, tmp_path, chats=("111",))

    def explode(_report):
        raise RuntimeError(f"connection to bot{TOKEN} refused")

    monkeypatch.setattr(alert_engine, "push_bridges", explode)
    run_report = alert_engine.run(report())
    assert all(TOKEN not in error for error in run_report.errors)
    assert "<REDACTED>" in run_report.errors[0]


def test_summarise_is_human_readable(settings, tmp_path):
    alert_engine, _bot, _client, _ledger = engine_for(settings, tmp_path, chats=("111",), responses=[ok_result()] * 2)
    run_report = alert_engine.run(report(bridges=[bridge_alert()]))
    text = tb.summarise(run_report)
    for expected in ("status", "bridges", "digest", "ledger"):
        assert expected in text


def test_github_output_is_written_when_available(settings, tmp_path, monkeypatch):
    output = tmp_path / "github_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    alert_engine, _bot, _client, _ledger = engine_for(settings, tmp_path, chats=("111",), responses=[ok_result()] * 2)
    tb.emit_github_output(alert_engine.run(report(bridges=[bridge_alert()])))
    content = output.read_text(encoding="utf-8")
    assert "telegram_status=completed" in content
    assert "telegram_bridge_sent=1" in content


def test_github_output_is_skipped_outside_actions(settings, tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    alert_engine, _bot, _client, _ledger = engine_for(settings, tmp_path, chats=("111",), responses=[ok_result()] * 2)
    tb.emit_github_output(alert_engine.run(report()))


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


def test_resolve_token_prefers_the_explicit_override(settings):
    assert tb.resolve_token(settings, "override") == "override"
    assert tb.resolve_token(settings, "") == ""


def test_resolve_token_reads_the_environment(settings, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    assert tb.resolve_token(settings) == TOKEN


def test_resolve_token_reads_settings(settings):
    configured = __import__("dataclasses").replace(settings, telegram_bot_token=TOKEN)
    assert tb.resolve_token(configured) == TOKEN


def test_resolve_chats_order_and_sources(settings, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "-1001, @chan")
    assert tb.resolve_chats(settings) == ["-1001", "@chan"]
    assert tb.resolve_chats(settings, "999") == ["999"]


def test_build_ledger_uses_the_state_directory(settings, tmp_path):
    configured = __import__("dataclasses").replace(settings, state_dir=str(tmp_path))
    ledger = tb.build_ledger(configured)
    assert ledger.path == tmp_path / tb.STATE_FILE
    assert ledger.persist is True
    assert tb.build_ledger(configured, persist=False).persist is False


def test_build_ledger_falls_back_to_dot_state(settings):
    """An empty STATE_DIR must not drop the ledger in the repository root."""
    configured = __import__("dataclasses").replace(settings, state_dir="")
    ledger = tb.build_ledger(configured)
    assert ledger.path.name == tb.STATE_FILE
    assert ledger.path.parent.name == ".state"


def test_build_sender_applies_settings(settings):
    configured = __import__("dataclasses").replace(
        settings,
        telegram_api_base="https://telegram.example.test",
        telegram_parse_mode="MarkdownV2",
        telegram_rate_per_sec=2.0,
        telegram_burst=9.0,
        telegram_send_retries=5,
    )
    bot = tb.build_sender(configured, TOKEN, dry_run=True, client=StubFetchClient())
    assert bot.api_base == "https://telegram.example.test"
    assert bot.parse_mode == "MarkdownV2"
    assert bot.retries == 5


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_refuses_to_run_without_a_token(monkeypatch, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("PUPPETNET_TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "111")
    assert tb.main(["--all", "--report-dir", str(tmp_path)]) == tb.EXIT_CONFIG


def test_cli_refuses_to_send_without_a_chat(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "")
    assert tb.main(["--all", "--report-dir", str(tmp_path)]) == tb.EXIT_CONFIG


def test_cli_reports_a_missing_report(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "111")
    assert tb.main(["--all", "--report-dir", str(tmp_path / "nothing")]) == tb.EXIT_CONFIG
    # stdout stays clean (it may be piped); the explanation goes to stderr.
    captured = capsys.readouterr()
    assert "no graph_maintenance_" in captured.err
    assert "no graph_maintenance_" not in captured.out


def test_cli_dry_run_needs_no_chat_and_sends_nothing(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.delenv("TELEGRAM_CHAT_IDS", raising=False)
    report_path = tmp_path / "graph_maintenance_x.json"
    report_path.write_text(json.dumps(report(bridges=[bridge_alert()], top=[anomaly_row("ORGANIZATION:n0", 0.9)])))
    code = tb.main(["--all", "--dry-run", "--report", str(report_path), "--state", str(tmp_path / "ledger.json")])
    assert code == tb.EXIT_OK
    out = capsys.readouterr().out
    assert "NEW CLUSTER BRIDGE" in out
    assert "DAILY ANOMALY DIGEST" in out
    assert not (tmp_path / "ledger.json").exists()


def test_cli_json_output_is_parseable(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "111")
    report_path = tmp_path / "graph_maintenance_x.json"
    report_path.write_text(json.dumps(report(bridges=[bridge_alert()])))
    code = tb.main([
        "--all", "--dry-run", "--report", str(report_path), "--state", str(tmp_path / "ledger.json"), "--json",
    ])
    assert code == tb.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"
    assert payload["dry_run"] is True


def test_cli_digest_only_skips_the_bridge_pass(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "111")
    report_path = tmp_path / "graph_maintenance_x.json"
    report_path.write_text(json.dumps(report(bridges=[bridge_alert()], top=[anomaly_row("ORGANIZATION:n0", 0.9)])))
    tb.main(["--digest", "--dry-run", "--report", str(report_path), "--state", str(tmp_path / "ledger.json")])
    out = capsys.readouterr().out
    assert "DAILY ANOMALY DIGEST" in out
    assert "NEW CLUSTER BRIDGE" not in out


def test_cli_bridge_only_skips_the_digest(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "111")
    report_path = tmp_path / "graph_maintenance_x.json"
    report_path.write_text(json.dumps(report(bridges=[bridge_alert()], top=[anomaly_row("ORGANIZATION:n0", 0.9)])))
    tb.main(["--bridge", "--dry-run", "--report", str(report_path), "--state", str(tmp_path / "ledger.json")])
    out = capsys.readouterr().out
    assert "NEW CLUSTER BRIDGE" in out
    assert "DAILY ANOMALY DIGEST" not in out


def test_cli_version(capsys):
    """argparse's own ``version`` action exits the process."""
    with pytest.raises(SystemExit) as excinfo:
        tb.main(["--version"])
    assert excinfo.value.code == 0
    assert tb.__version__ in capsys.readouterr().out


def test_cli_rejects_unknown_flags():
    with pytest.raises(SystemExit) as excinfo:
        tb.main(["--nope"])
    assert excinfo.value.code == 2


def test_cli_removes_its_log_filter_on_the_way_out(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "111")
    before = [f for f in logging.getLogger().filters if isinstance(f, tb.TokenRedactor)]
    report_path = tmp_path / "graph_maintenance_x.json"
    report_path.write_text(json.dumps(report()))
    tb.main(["--all", "--dry-run", "--report", str(report_path), "--state", str(tmp_path / "ledger.json")])
    after = [f for f in logging.getLogger().filters if isinstance(f, tb.TokenRedactor)]
    assert len(after) == len(before)


def test_cli_test_pass_without_chats_still_verifies_the_token(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "")
    code = tb.main(["--test", "--dry-run", "--state", str(tmp_path / "ledger.json")])
    assert code == tb.EXIT_OK
    assert "chats" in capsys.readouterr().out
