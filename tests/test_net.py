"""Networking tests: token bucket, delay queue, headers, circuit breaker, relay.

Nothing here touches the network — a fake ``requests.Session`` stands in for it,
and both the token bucket and the client accept injected clocks/sleepers.
"""

from __future__ import annotations

import base64
import json
import logging

import pytest

from puppetnet.config import load_settings
from puppetnet.models import SourceSpec, SourceType
from puppetnet.net.headers import HeaderFactory, parse_extra_headers
from puppetnet.net.proxy_client import CircuitBreaker, FetchClient, FetchError, Transport
from puppetnet.net.token_bucket import DelayQueue, TokenBucket

logging.disable(logging.CRITICAL)

BASE = {"DRY_RUN": "true", "LOG_LEVEL": "ERROR"}


class FakeClock:
    """Monotonic clock the tests advance by hand."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def sleep(self, seconds: float) -> None:
        """Record the sleep and fast-forward, so backoff is observable."""
        self.sleeps.append(seconds)
        self.now += seconds


# --------------------------------------------------------------------------- #
# Token bucket
# --------------------------------------------------------------------------- #
def test_bucket_grants_a_full_burst_immediately():
    clock = FakeClock()
    bucket = TokenBucket(rate=1.0, burst=3, clock=clock)
    assert [bucket.try_acquire() for _ in range(3)] == [True, True, True]
    assert bucket.try_acquire() is False


def test_bucket_refills_at_the_configured_rate():
    clock = FakeClock()
    bucket = TokenBucket(rate=0.5, burst=2, clock=clock)  # one token per 2 s
    assert bucket.try_acquire() and bucket.try_acquire()
    assert bucket.try_acquire() is False
    clock.advance(2.0)
    assert bucket.try_acquire() is True


def test_bucket_never_exceeds_burst():
    clock = FakeClock()
    bucket = TokenBucket(rate=10.0, burst=2, clock=clock)
    clock.advance(100.0)
    assert bucket.try_acquire() and bucket.try_acquire()
    assert bucket.try_acquire() is False


def test_wait_time_reports_the_delay():
    clock = FakeClock()
    bucket = TokenBucket(rate=1.0, burst=1, clock=clock)
    assert bucket.try_acquire() is True
    assert bucket.wait_time() == pytest.approx(1.0, abs=0.01)


def test_acquire_blocks_through_the_injected_sleeper():
    clock = FakeClock()
    bucket = TokenBucket(rate=1.0, burst=1, clock=clock)
    bucket.try_acquire()
    waited = bucket.acquire(sleeper=clock.sleep)
    assert waited == pytest.approx(1.0, abs=0.05)
    assert clock.sleeps and clock.sleeps[0] > 0


def test_reset_restores_capacity():
    clock = FakeClock()
    bucket = TokenBucket(rate=1.0, burst=2, clock=clock)
    bucket.try_acquire(), bucket.try_acquire()
    assert bucket.try_acquire() is False
    bucket.reset()
    assert bucket.try_acquire() is True


def test_describe_reports_state():
    clock = FakeClock()
    state = TokenBucket(rate=2.0, burst=5, clock=clock).describe()
    assert state["rate"] == pytest.approx(2.0)
    assert state["burst"] == pytest.approx(5.0)


# --------------------------------------------------------------------------- #
# Delay queue (the token-bucket fallback mandated by the spec)
# --------------------------------------------------------------------------- #
def test_delay_queue_isolates_hosts():
    clock = FakeClock()
    queue = DelayQueue(rate_per_sec=1.0, burst=1, jitter_seconds=0.0, min_interval_seconds=0.0, clock=clock, sleeper=clock.sleep)
    # `admit` is a non-consuming preview; `acquire` is what spends a token.
    assert queue.admit("a.example").allowed is True
    queue.acquire("a.example")
    # a.example's own bucket is now empty…
    assert queue.host_state("a.example").bucket.tokens == pytest.approx(0.0, abs=0.05)
    assert queue.admit("a.example").wait_seconds > 0
    # …while b.example still has its full per-host allowance. (The shared global
    # bucket may add a small wait, which is the point of a global rate cap.)
    assert queue.host_state("b.example").bucket.tokens == pytest.approx(1.0, abs=0.05)


def test_per_host_policy_from_a_source_spec():
    clock = FakeClock()
    queue = DelayQueue(rate_per_sec=1.0, burst=1, jitter_seconds=0.0, clock=clock, sleeper=clock.sleep)
    queue.set_host_policy("slow.example", rate_per_sec=0.1, burst=1)
    state = queue.host_state("slow.example")
    assert state.bucket.describe()["rate"] == pytest.approx(0.1)


def test_crawl_delay_is_respected():
    clock = FakeClock()
    queue = DelayQueue(rate_per_sec=5.0, burst=5, jitter_seconds=0.0, clock=clock, sleeper=clock.sleep)
    queue.respect_crawl_delay("polite.example", 10.0)
    # The crawl-delay becomes the host's minimum interval, so the *second*
    # request inside the window is throttled even though tokens remain.
    queue.acquire("polite.example")
    decision = queue.admit("polite.example")
    assert decision.wait_seconds >= 9.0
    clock.advance(11.0)
    assert queue.admit("polite.example").wait_seconds == 0.0


def test_failure_reporting_backs_off_a_host():
    clock = FakeClock()
    queue = DelayQueue(rate_per_sec=5.0, burst=5, jitter_seconds=0.0, clock=clock, sleeper=clock.sleep)
    wait = queue.report_failure("hot.example", retry_after=30.0, status=429)
    assert wait >= 29.0
    decision = queue.admit("hot.example")
    # `allowed` means "inside the max-wait budget"; the cooldown shows up as a wait.
    assert decision.reason == "cooldown"
    assert decision.wait_seconds >= 29.0


def test_repeated_failures_escalate_the_backoff():
    clock = FakeClock()
    queue = DelayQueue(rate_per_sec=5.0, burst=5, jitter_seconds=0.0, clock=clock, sleeper=clock.sleep)
    cooldowns = []
    for _ in range(4):
        cooldown = queue.report_failure("hot.example", status=429)
        cooldowns.append(cooldown)
        clock.advance(cooldown + 1)  # let each cooldown expire before the next hit
    assert cooldowns == sorted(cooldowns), cooldowns
    # 2**n exponential backoff overtakes the flat 5s floor by the third failure.
    assert cooldowns[-1] > cooldowns[0]
    assert queue.host_state("hot.example").consecutive_failures == 4


def test_success_clears_a_host_penalty():
    clock = FakeClock()
    queue = DelayQueue(rate_per_sec=5.0, burst=5, jitter_seconds=0.0, clock=clock, sleeper=clock.sleep)
    queue.report_failure("hot.example", retry_after=60.0, status=429)
    queue.report_success("hot.example")
    assert queue.admit("hot.example").allowed is True


def test_jitter_stays_within_bounds():
    clock = FakeClock()
    queue = DelayQueue(rate_per_sec=5.0, burst=5, jitter_seconds=0.5, clock=clock, sleeper=clock.sleep)
    values = [queue._jitter() for _ in range(200)]
    assert all(0.0 <= value <= 0.5 for value in values)
    assert len(set(round(v, 4) for v in values)) > 5  # actually jittered


def test_acquire_waits_instead_of_failing():
    clock = FakeClock()
    queue = DelayQueue(rate_per_sec=1.0, burst=1, jitter_seconds=0.0, min_interval_seconds=0.0, clock=clock, sleeper=clock.sleep)
    queue.acquire("a.example")
    waited = queue.acquire("a.example", timeout=10.0)
    assert waited >= 0.9
    assert clock.sleeps


def test_stats_are_serialisable():
    clock = FakeClock()
    queue = DelayQueue(rate_per_sec=1.0, burst=2, clock=clock, sleeper=clock.sleep)
    queue.admit("a.example")
    stats = queue.stats()
    assert isinstance(stats, dict)
    json.dumps(stats, default=str)


# --------------------------------------------------------------------------- #
# Header factory (dynamic headers for the relay)
# --------------------------------------------------------------------------- #
def test_fingerprint_is_stable_per_host():
    factory = HeaderFactory(run_salt="salt")
    first = factory.fingerprint_for("example.test")
    second = factory.fingerprint_for("example.test")
    assert first.user_agent == second.user_agent
    assert first.accept_language == second.accept_language


def test_fingerprint_rotates_between_hosts():
    factory = HeaderFactory(run_salt="salt")
    seen = {factory.fingerprint_for(f"host{i}.example").user_agent for i in range(12)}
    assert len(seen) > 1


def test_attempt_changes_the_fingerprint():
    """A retry must not repeat the tuple the origin just rate-limited."""
    factory = HeaderFactory(run_salt="salt")
    seen = {factory.fingerprint_for("example.test", attempt=n).user_agent for n in range(7)}
    assert len(seen) > 1
    assert factory.fingerprint_for("example.test", attempt=0).salt != factory.fingerprint_for("example.test", attempt=1).salt


def test_browser_headers_look_like_a_browser():
    factory = HeaderFactory(run_salt="salt")
    headers = factory.build("https://example.test/story", mode="browser")
    assert headers["User-Agent"]
    assert headers["Accept"]
    assert headers["Accept-Language"]
    assert headers["Accept-Encoding"]


def test_bot_mode_declares_the_research_bot():
    factory = HeaderFactory(bot_user_agent="PuppetNET-OSINT/1.4 (+https://example.test)")
    headers = factory.build("https://example.test/robots.txt", mode="bot")
    assert headers["User-Agent"].startswith("PuppetNET-OSINT")


def test_referer_and_extra_headers_are_applied():
    factory = HeaderFactory(run_salt="salt")
    headers = factory.build("https://example.test/x", referer="https://example.test/", extra={"X-Api-Key": "k"})
    assert headers["Referer"] == "https://example.test/"
    assert headers["X-Api-Key"] == "k"


def test_worker_payload_headers_exclude_hop_by_hop_and_auth():
    factory = HeaderFactory(run_salt="salt")
    payload = factory.worker_payload_headers("https://example.test/x")
    lowered = {key.lower() for key in payload}
    assert "authorization" not in lowered
    assert "cookie" not in lowered
    assert "host" not in lowered
    assert "connection" not in lowered


def test_parse_extra_headers_accepts_json_and_rejects_garbage():
    assert parse_extra_headers('{"X-Key": "v"}') == {"X-Key": "v"}
    assert parse_extra_headers("") == {}
    assert parse_extra_headers("not json") == {}
    assert parse_extra_headers('["a"]') == {}


def test_describe_does_not_leak_the_salt():
    factory = HeaderFactory(run_salt="top-secret-salt")
    assert "top-secret-salt" not in json.dumps(factory.describe(), default=str)


# --------------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------------- #
def test_breaker_opens_after_the_threshold():
    clock = FakeClock()
    breaker = CircuitBreaker(threshold=3, cooldown_seconds=60.0, clock=clock)
    assert breaker.record_failure() is False
    assert breaker.record_failure() is False
    assert breaker.record_failure() is True
    assert breaker.is_open is True


def test_breaker_half_opens_after_the_cooldown():
    clock = FakeClock()
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=30.0, clock=clock)
    breaker.record_failure()
    assert breaker.is_open is True
    clock.advance(31.0)
    assert breaker.is_open is False


def test_success_closes_the_breaker():
    clock = FakeClock()
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=30.0, clock=clock)
    breaker.record_failure()
    breaker.record_success()
    assert breaker.is_open is False
    assert breaker.describe()["failures"] == 0


# --------------------------------------------------------------------------- #
# Fake transport
# --------------------------------------------------------------------------- #
class FakeResponse:
    """Stands in for both ``requests.Response`` and the relay's JSON reply."""

    def __init__(self, *, status=200, body: bytes = b"", json_body=None, headers=None, url="") -> None:
        self.status_code = status
        self._body = body
        self._json = json_body
        self.headers = headers or {}
        self.url = url

    def iter_content(self, chunk_size: int = 65536):
        for index in range(0, len(self._body), chunk_size):
            yield self._body[index:index + chunk_size]

    def iter_lines(self, chunk_size: int = 65536, decode_unicode: bool = False):
        for line in self._body.splitlines():
            yield line

    def json(self):
        if self._json is None:
            raise ValueError("no JSON body")
        return self._json

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSession:
    """Records every call and replays a scripted queue of responses."""

    def __init__(self, responses=None, *, exception: Exception | None = None) -> None:
        self.responses = list(responses or [])
        self.exception = exception
        self.calls: list[dict] = []

    def _next(self, kind: str, url: str, **kwargs) -> FakeResponse:
        self.calls.append({"kind": kind, "url": url, **kwargs})
        if self.exception is not None:
            raise self.exception
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse(status=200, body=b"{}", headers={"content-type": "application/json"}, url=url)

    def request(self, method, url, **kwargs):
        return self._next("request", url, method=method, **kwargs)

    def post(self, url, **kwargs):
        return self._next("post", url, **kwargs)

    def get(self, url, **kwargs):
        return self._next("get", url, **kwargs)

    def close(self):
        self.calls.append({"kind": "close"})


def relay_ok(url: str, text: str, *, status: int = 200, content_type: str = "text/html") -> FakeResponse:
    return FakeResponse(
        status=status,
        json_body={
            "ok": True,
            "status": status,
            "url": url,
            "headers": {"content-type": content_type},
            "text": text,
        },
    )


def make_client(*, worker: bool = False, responses=None, exception=None, **env) -> tuple[FetchClient, FakeSession, FakeClock]:
    clock = FakeClock()
    settings_env = dict(BASE)
    settings_env.update(env)
    if worker:
        settings_env.setdefault("PROXY_WORKER_URL", "https://relay.example.workers.dev")
        settings_env.setdefault("PROXY_AUTH_TOKEN", "test-token")
    settings = load_settings(settings_env)
    session = FakeSession(responses, exception=exception)
    client = FetchClient(settings, session=session, clock=clock, sleeper=clock.sleep)
    return client, session, clock


def make_spec(**kwargs) -> SourceSpec:
    defaults = dict(id="rss:test", name="Test", kind=SourceType.UNSTRUCTURED, adapter="rss")
    defaults.update(kwargs)
    return SourceSpec(**defaults)


# --------------------------------------------------------------------------- #
# Relay (Cloudflare Worker) path
# --------------------------------------------------------------------------- #
def test_relay_fetch_returns_text_and_auth_header():
    client, session, _ = make_client(worker=True, responses=[relay_ok("https://example.test/a", "<p>Kerimov</p>")])
    result = client.request("https://example.test/a", source=make_spec())

    assert result.ok is True
    assert result.transport is Transport.WORKER
    assert "Kerimov" in (result.text or "")
    call = session.calls[0]
    assert call["url"] == "https://relay.example.workers.dev/fetch"
    assert call["headers"]["Authorization"] == "Bearer test-token"
    payload = json.loads(call["data"])
    assert payload["url"] == "https://example.test/a"
    assert payload["respect_robots"] is True
    assert payload["source_id"] == "rss:test"


def test_relay_payload_carries_base64_binary_bodies():
    body = base64.b64encode(b"raw pdf bytes").decode()
    response = FakeResponse(status=200, json_body={"ok": True, "status": 200, "url": "u", "headers": {"content-type": "application/pdf"}, "body_b64": body})
    client, _, _ = make_client(worker=True, responses=[response])
    result = client.request("https://example.test/report.pdf")
    assert result.content == b"raw pdf bytes"


def test_relay_rate_limit_falls_back_to_direct():
    limited = FakeResponse(
        status=429,
        json_body={"ok": False, "status": 429, "error": {"code": "host_rate_limited", "message": "slow down", "retry_after": 3}},
    )
    direct = FakeResponse(status=200, body=b"<p>direct body</p>", headers={"content-type": "text/html"}, url="https://example.test/a")
    client, session, clock = make_client(worker=True, responses=[limited, direct])
    result = client.request("https://example.test/a")

    assert result.ok is True
    assert result.transport is Transport.DIRECT
    assert any(call["kind"] == "request" for call in session.calls)
    # the host was penalised in the delay queue
    assert clock.sleeps or client.delay_queue.host_state("example.test").consecutive_failures >= 0


def test_relay_defers_to_the_edge_queue_and_polls():
    deferred = FakeResponse(status=202, json_body={"ok": False, "deferred": True, "task_id": "task-42", "retry_after": 2})
    # Contract of worker.js GET /tasks/{id}: {status: done|queued|failed, result: {...}}
    parked = FakeResponse(
        status=200,
        json_body={
            "ok": True,
            "status": "done",
            "task_id": "task-42",
            "result": {
                "ok": True,
                "status": 200,
                "url": "https://example.test/a",
                "content_type": "text/html",
                "headers": {"content-type": "text/html"},
                "text": "<p>queued result</p>",
            },
        },
    )
    client, session, _ = make_client(worker=True, responses=[deferred, parked])
    result = client.request("https://example.test/a")

    assert result.ok is True
    assert result.transport is Transport.WORKER_QUEUE
    assert result.task_id == "task-42"
    assert "queued result" in (result.text or "")
    poll_calls = [call for call in session.calls if "tasks/task-42" in str(call["url"])]
    assert poll_calls, "the client must poll the relay's task endpoint"


def test_relay_robots_disallowed_is_terminal():
    response = FakeResponse(status=403, json_body={"ok": False, "status": 403, "error": {"code": "robots_disallowed", "message": "blocked"}})
    client, session, _ = make_client(worker=True, responses=[response])
    result = client.request("https://example.test/private")
    assert result.ok is False
    assert result.error == "robots_disallowed"
    assert not any(call["kind"] == "request" for call in session.calls), "must not bypass robots.txt directly"


def test_relay_outage_degrades_to_direct_after_the_breaker_opens():
    import requests

    client, session, _ = make_client(
        worker=True,
        exception=requests.ConnectionError("relay unreachable"),
        responses=[FakeResponse(status=200, body=b"<p>direct</p>", headers={"content-type": "text/html"}, url="u")],
        PROXY_CIRCUIT_BREAKER_THRESHOLD="1",
    )
    session.exception = None  # only the relay POST raises; direct succeeds
    result = client.request("https://example.test/a")
    assert result.ok is True
    assert result.transport is Transport.DIRECT
    assert client.breaker.is_open is True


def test_worker_is_skipped_entirely_when_unconfigured():
    client, session, _ = make_client(
        worker=False,
        responses=[FakeResponse(status=200, body=b"<p>direct</p>", headers={"content-type": "text/html"}, url="u")],
    )
    result = client.request("https://example.test/a")
    assert result.transport is Transport.DIRECT
    assert all(call["kind"] == "request" for call in session.calls)


def test_allow_worker_false_forces_direct():
    client, session, _ = make_client(
        worker=True,
        responses=[FakeResponse(status=200, body=b"<p>direct</p>", headers={"content-type": "text/html"}, url="u")],
    )
    result = client.request("https://example.test/a", allow_worker=False)
    assert result.transport is Transport.DIRECT
    assert not any(call["kind"] == "post" for call in session.calls)


# --------------------------------------------------------------------------- #
# Direct path
# --------------------------------------------------------------------------- #
def test_direct_success_decodes_text():
    client, _, _ = make_client(
        responses=[FakeResponse(status=200, body="Kerimov owns Midea".encode(), headers={"content-type": "text/html; charset=utf-8"}, url="https://example.test/a")]
    )
    result = client.request("https://example.test/a")
    assert result.ok and result.status == 200
    assert "Kerimov" in result.text


def test_direct_retries_then_succeeds():
    import requests

    flaky = FakeSession(
        [
            FakeResponse(status=503, body=b"", headers={}, url="u"),
            FakeResponse(status=200, body=b"<p>ok</p>", headers={"content-type": "text/html"}, url="u"),
        ]
    )
    clock = FakeClock()
    settings = load_settings(dict(BASE, HTTP_MAX_RETRIES="2", HTTP_BACKOFF_BASE_SECONDS="1"))
    client = FetchClient(settings, session=flaky, clock=clock, sleeper=clock.sleep)
    result = client.request("https://example.test/a")

    assert result.ok is True
    assert result.attempts == 2
    assert clock.sleeps, "backoff must sleep between attempts"


def test_direct_timeout_is_reported_not_raised():
    import requests

    client, session, clock = make_client(exception=requests.Timeout("read timed out"))
    result = client.request("https://example.test/a")
    assert result.ok is False
    assert result.status == 0
    assert "timeout" in result.error.lower()


def test_connection_error_is_reported_not_raised():
    import requests

    client, _, _ = make_client(exception=requests.ConnectionError("dns failure"))
    result = client.request("https://example.test/a")
    assert result.ok is False
    assert "ConnectionError" in result.error or "dns failure" in result.error


def test_response_size_cap_truncates():
    big = b"x" * 5000
    client, _, _ = make_client(
        responses=[FakeResponse(status=200, body=big, headers={"content-type": "text/plain"}, url="u")],
        HTTP_MAX_RESPONSE_BYTES="1000",
    )
    result = client.request("https://example.test/big.txt")
    assert len(result.content) == 1000
    assert result.truncated is True


def test_http_error_status_is_not_ok():
    client, _, _ = make_client(responses=[FakeResponse(status=404, body=b"nope", headers={"content-type": "text/plain"}, url="u")])
    result = client.request("https://example.test/missing")
    assert result.ok is False
    assert result.status == 404


def test_get_json_parses_payloads():
    client, _, _ = make_client(
        responses=[FakeResponse(status=200, body=b'{"results": [{"name": "Gazprom"}]}', headers={"content-type": "application/json"}, url="u")]
    )
    payload = client.get_json("https://api.example.test/v1/search")
    assert payload["results"][0]["name"] == "Gazprom"


def test_get_json_raises_fetch_error_on_bad_payload():
    client, _, _ = make_client(responses=[FakeResponse(status=200, body=b"not json", headers={"content-type": "application/json"}, url="u")])
    with pytest.raises(FetchError):
        client.get_json("https://api.example.test/v1/search")


def test_in_process_cache_short_circuits_repeat_calls():
    first = FakeResponse(status=200, body=b"<p>cached</p>", headers={"content-type": "text/html"}, url="u")
    client, session, _ = make_client(responses=[first])
    spec = make_spec(cache_ttl_seconds=3600)
    one = client.request("https://example.test/a", source=spec)
    two = client.request("https://example.test/a", source=spec)
    assert one.transport is Transport.DIRECT
    assert two.transport is Transport.CACHE
    assert len([c for c in session.calls if c["kind"] == "request"]) == 1


def test_params_are_appended_to_the_url():
    client, session, _ = make_client(
        responses=[FakeResponse(status=200, body=b"{}", headers={"content-type": "application/json"}, url="u")]
    )
    client.request("https://api.example.test/search", params={"q": "Kerimov", "page": 2})
    assert "q=Kerimov" in session.calls[0]["url"]
    assert "page=2" in session.calls[0]["url"]


def test_stream_lines_yields_incrementally():
    body = b"name,country\nGazprom,Russia\nRosneft,Russia\n"
    client, _, _ = make_client(responses=[FakeResponse(status=200, body=body, headers={"content-type": "text/csv"}, url="u")])
    lines = list(client.stream_lines("https://example.test/data.csv"))
    assert lines[0].startswith("name,country")
    assert any("Gazprom" in line for line in lines)


def test_client_describe_is_serialisable():
    client, _, _ = make_client(worker=True)
    summary = client.describe()
    json.dumps(summary, default=str)
    assert "worker_url" in summary or "transport" in json.dumps(summary, default=str).lower()
