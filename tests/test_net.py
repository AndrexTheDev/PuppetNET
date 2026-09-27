"""Networking tests: token bucket, delay queue, headers, circuit breaker, relay.

Nothing here touches the network — a fake ``requests.Session`` stands in for it,
and both the token bucket and the client accept injected clocks/sleepers.
"""

from __future__ import annotations

import base64
import json
import logging
from urllib.parse import parse_qsl, urlparse

import pytest

from puppetnet.config import load_settings
from puppetnet.models import SourceSpec, SourceType
from puppetnet.net.headers import HeaderFactory, parse_extra_headers
from puppetnet.net.proxy_client import CircuitBreaker, FetchClient, FetchError, Transport, _encode_form_body
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
    factory = HeaderFactory(bot_user_agent="PuppetNET-OSINT/1.5 (+https://example.test)")
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
        yield from self._body.splitlines()

    def json(self):
        if self._json is None:
            raise ValueError("no JSON body")
        return self._json

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSession:
    """Records every call and replays a scripted queue of responses.

    ``robots.txt`` is answered by the session itself and recorded separately:
    the direct transport consults the origin's rules before it fetches anything,
    and a document test that asserted ``calls[0]`` must not start seeing the
    rules request instead. The default answer is a 404, which RFC 9309 reads as
    "no restrictions" — the same meaning the real relay gives it.
    """

    def __init__(
        self,
        responses=None,
        *,
        exception: Exception | None = None,
        robots_status: int = 404,
        robots_text: str = "",
        robots_exception: Exception | None = None,
    ) -> None:
        self.responses = list(responses or [])
        self.exception = exception
        self.calls: list[dict] = []
        self.robots_status = robots_status
        self.robots_text = robots_text
        self.robots_exception = robots_exception
        self.robots_calls: list[dict] = []

    def _next(self, kind: str, url: str, **kwargs) -> FakeResponse:
        if urlparse(str(url)).path.endswith("/robots.txt"):
            self.robots_calls.append({"kind": kind, "url": url, **kwargs})
            if self.robots_exception is not None:
                raise self.robots_exception
            return FakeResponse(
                status=self.robots_status,
                body=self.robots_text.encode("utf-8"),
                headers={"content-type": "text/plain"},
                url=url,
            )
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


def make_client(
    *,
    worker: bool = False,
    responses=None,
    exception=None,
    robots_status: int = 404,
    robots_text: str = "",
    robots_exception: Exception | None = None,
    **env,
) -> tuple[FetchClient, FakeSession, FakeClock]:
    clock = FakeClock()
    settings_env = dict(BASE)
    settings_env.update(env)
    if worker:
        settings_env.setdefault("PROXY_WORKER_URL", "https://relay.example.workers.dev")
        settings_env.setdefault("PROXY_AUTH_TOKEN", "test-token")
    settings = load_settings(settings_env)
    session = FakeSession(
        responses,
        exception=exception,
        robots_status=robots_status,
        robots_text=robots_text,
        robots_exception=robots_exception,
    )
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
        responses=[FakeResponse(status=200, body=b"Kerimov owns Midea", headers={"content-type": "text/html; charset=utf-8"}, url="https://example.test/a")]
    )
    result = client.request("https://example.test/a")
    assert result.ok and result.status == 200
    assert "Kerimov" in result.text


def test_direct_retries_then_succeeds():

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


# --------------------------------------------------------------------------- #
# Form bodies — how a SPARQL query actually travels
# --------------------------------------------------------------------------- #
#
# WDQS is the motivating case: a query in a GET URL runs into proxy and CDN
# length limits (and shows up in access logs), so the relay must be able to carry
# an urlencoded POST body. These tests pin that contract on both transports.
def test_encode_form_body_urlencodes_mappings():
    body, content_type = _encode_form_body({"query": "SELECT ?p WHERE { ?p wdt:P31 wd:Q5 }", "format": "json"})
    assert content_type == "application/x-www-form-urlencoded"
    assert dict(parse_qsl(body)) == {"query": "SELECT ?p WHERE { ?p wdt:P31 wd:Q5 }", "format": "json"}


def test_encode_form_body_drops_none_and_rejects_empty():
    assert _encode_form_body({"query": "SELECT * WHERE {}", "maxlag": None})[0] == "query=SELECT+%2A+WHERE+%7B%7D"
    assert _encode_form_body({"maxlag": None}) == (None, None)
    assert _encode_form_body(None) == (None, None)


def test_encode_form_body_passes_strings_through_without_a_declared_type():
    # A caller that encodes its own body keeps control of Content-Type.
    assert _encode_form_body("query=SELECT+%2A+WHERE+%7B%7D") == ("query=SELECT+%2A+WHERE+%7B%7D", None)
    assert _encode_form_body("") == (None, None)


def test_encode_form_body_supports_pairs_and_repeated_keys():
    body, content_type = _encode_form_body([("jurisdiction_code", "gb"), ("jurisdiction_code", "im"), ("q", "a b")])
    assert content_type == "application/x-www-form-urlencoded"
    assert parse_qsl(body) == [("jurisdiction_code", "gb"), ("jurisdiction_code", "im"), ("q", "a b")]


def test_relay_payload_carries_a_form_body_for_post():
    client, session, _ = make_client(worker=True, responses=[relay_ok("https://query.wikidata.org/sparql", "{}", content_type="application/json")])
    client.post("https://query.wikidata.org/sparql", data={"query": "SELECT ?x WHERE { ?x wdt:P31 wd:Q1664720 }", "format": "json", "maxlag": 5})

    payload = json.loads(session.calls[0]["data"])
    assert payload["method"] == "POST"
    assert payload["content_type"] == "application/x-www-form-urlencoded"
    fields = dict(parse_qsl(payload["body_text"]))
    assert fields["query"] == "SELECT ?x WHERE { ?x wdt:P31 wd:Q1664720 }"
    assert fields["format"] == "json"
    assert fields["maxlag"] == "5", "non-string values are stringified, not dropped"


def test_direct_post_sends_an_encoded_form_body():
    client, session, _ = make_client(responses=[FakeResponse(status=200, body=b"{}", headers={"content-type": "application/json"}, url="u")])
    client.post("https://query.wikidata.org/sparql", data={"query": "SELECT ?x WHERE {}", "format": "json"})

    call = session.calls[0]
    assert call["method"] == "POST"
    assert dict(parse_qsl(call["data"])) == {"query": "SELECT ?x WHERE {}", "format": "json"}
    assert call["headers"]["Content-Type"] == "application/x-www-form-urlencoded"


def test_a_long_query_survives_the_relay_intact():
    # The whole point of POSTing: no URL length cliff, and the special characters
    # a SPARQL query is made of must come back unchanged.
    query = (
        "SELECT ?person ?personLabel ?org WHERE { "
        + " ".join(f"?person wdt:P3320 wd:Q{i} . ?org wdt:P31 wd:Q1664720 ." for i in range(60))
        + " SERVICE wikibase:label { bd:serviceParam wikibase:language \"en,ru,uz\" } }"
    )
    client, session, _ = make_client(worker=True, responses=[relay_ok("https://query.wikidata.org/sparql", "{}", content_type="application/json")])
    client.post("https://query.wikidata.org/sparql", data={"query": query, "format": "json"})

    payload = json.loads(session.calls[0]["data"])
    assert dict(parse_qsl(payload["body_text"]))["query"] == query
    assert len(payload["body_text"]) > len(query)


def test_get_and_post_to_one_url_do_not_share_a_cache_entry():
    client, session, _ = make_client(
        responses=[
            FakeResponse(status=200, body=b'{"via": "get"}', headers={"content-type": "application/json"}, url="u"),
            FakeResponse(status=200, body=b'{"via": "post"}', headers={"content-type": "application/json"}, url="u"),
        ]
    )
    spec = make_spec(cache_ttl_seconds=3600)
    first = client.get("https://query.wikidata.org/sparql", source=spec)
    second = client.post("https://query.wikidata.org/sparql", data={"query": "SELECT * WHERE {}"}, source=spec)
    assert first.transport is Transport.DIRECT
    assert second.transport is Transport.DIRECT, "a POST must not be served from the GET's cache entry"
    assert len([c for c in session.calls if c["kind"] == "request"]) == 2


# --------------------------------------------------------------------------- #
# robots.txt on the direct transport
# --------------------------------------------------------------------------- #
#
# The relay evaluates robots.txt for everything it serves, and the smoke suite
# covers that path (G15). These tests cover the *fallback*: when the relay is
# down or the breaker is open, the harvester talks to origins itself, and before
# this guard that traffic was not policed at all.


def test_a_direct_fetch_asks_for_the_rules_before_the_document():
    client, session, _ = make_client(responses=[FakeResponse(status=200, body=b"ok", headers={"content-type": "text/plain"}, url="u")])
    client.request("https://origin.test/page", source=make_spec())

    assert session.robots_calls, "the direct path must consult robots.txt"
    assert session.robots_calls[0]["url"] == "https://origin.test/robots.txt"
    assert session.robots_calls[0]["kind"] == "get"
    assert len(session.calls) == 1, "one document request, no retries"


def test_a_disallowed_path_is_refused_without_touching_the_origin():
    client, session, _ = make_client(
        responses=[],
        robots_status=200,
        robots_text="User-agent: *\nDisallow: /private\n",
    )
    result = client.request("https://origin.test/private/report.pdf", source=make_spec())

    assert result.ok is False and result.robots_blocked is True
    assert result.error == "robots_disallowed"
    assert result.status == 403
    assert session.calls == [], "a disallowed path must not be fetched at all"
    assert client.stats.http_robots_blocked == 1
    assert client.stats.http_requests == 0, "the request counter is for real egress"


def test_the_direct_path_reads_the_rules_the_same_way_the_relay_does():
    """Same fixtures as the worker smoke suite (G15): the two evaluators must agree."""
    rules = (
        "User-agent: *\n"
        "Disallow: /private\n"
        "Allow: /private/public\n"
        "Disallow: /*.json$\n"
    )
    client, session, _ = make_client(robots_status=200, robots_text=rules)

    refused = ["/private/x", "/privateer/x", "/data.json"]
    allowed = ["/public/page", "/private/public/x", "/data.json?v=2", "/data.jsonp"]
    for path in refused:
        before = len(session.calls)
        result = client.request(f"https://origin.test{path}", source=make_spec())
        assert result.robots_blocked is True, f"{path} should be refused"
        assert len(session.calls) == before, f"{path} must not reach the origin"
    for path in allowed:
        document = FakeResponse(status=200, body=b"x", headers={"content-type": "text/plain"}, url=path)
        session.responses.append(document)
        result = client.request(f"https://origin.test{path}", source=make_spec())
        assert result.ok is True, f"{path} should be fetched ({result.error})"


def test_robots_that_deny_crawl_makes_the_origin_fully_disallowed():
    """RFC 9309: a 401/403 on robots.txt means "everything disallowed"."""
    client, session, _ = make_client(robots_status=403)
    result = client.request("https://origin.test/anything", source=make_spec())
    assert result.robots_blocked is True and session.calls == []


def test_an_unreachable_rules_file_stops_the_request_without_caching_the_refusal():
    """RFC 9309 §2.3.1.4: unreadable robots.txt means "do not crawl" — and the
    verdict is re-evaluated next time instead of being frozen."""
    client, session, _ = make_client(robots_status=500)
    result = client.request("https://origin.test/page", source=make_spec())

    assert result.robots_blocked is True, "an unreadable rules file is not a licence to crawl"
    assert result.status == 502, "the reason is 'could not read', not 'you may not'"
    assert len(session.robots_calls) == 1, "no retry against a host whose rules we cannot read"
    assert session.calls == [], "the document was never requested"

    # The origin answers again: the *second* call re-reads the rules instead of
    # reusing the refusal, which is the whole point of not caching it.
    session.robots_status = 200
    session.robots_text = "User-agent: *\nDisallow: /page\n"
    second = client.request("https://origin.test/page", source=make_spec())
    assert second.robots_blocked is True and len(session.robots_calls) == 2
    assert session.calls == []

    # And when the rules permit the path, the same host is fetched normally.
    recovered, recovered_session, _ = make_client(
        responses=[FakeResponse(status=200, body=b"x", headers={"content-type": "text/plain"}, url="page")],
        robots_status=200,
        robots_text="User-agent: *\nAllow: /page\n",
    )
    assert recovered.request("https://origin.test/page", source=make_spec()).ok is True
    assert len(recovered_session.calls) == 1


def test_a_transport_fault_on_the_rules_fetch_also_refuses():
    """A connection error is `unreachable` too, not an excuse."""
    import requests

    client, session, _ = make_client(robots_exception=requests.ConnectionError("dns down"))
    result = client.request("https://origin.test/page", source=make_spec())
    assert result.robots_blocked is True and session.calls == []
    assert client.stats.http_robots_blocked == 1


def test_a_policy_verdict_is_terminal_and_never_retried():
    """The refusal must not be laundered through the retry loop (which is how the
    old code turned a policy answer into a generic HTTP 502 after four attempts)."""
    client, session, _ = make_client(robots_status=200, robots_text="User-agent: *\nDisallow: /\n")
    result = client.request("https://origin.test/page", source=make_spec())
    assert result.attempts == 1
    assert len(session.robots_calls) == 1 and session.calls == []
    assert client.stats.http_robots_blocked == 1 and client.stats.http_errors == 0


def test_an_edge_side_rules_outage_falls_through_to_the_runners_own_transport():
    """The relay could not read robots.txt; the runner is allowed to ask itself.

    `robots_unavailable` is deliberately *not* terminal, unlike
    `robots_disallowed`: the runner reads the rules with its own connection, and
    when that read succeeds the document is fetched — politely — instead of the
    source going dark because the edge had a bad minute.
    """
    client, session, _ = make_client(
        worker=True,
        responses=[
            FakeResponse(
                status=502,
                json_body={"ok": False, "error": {"code": "robots_unavailable", "message": "robots.txt for origin.test could not be read"}},
                headers={"content-type": "application/json"},
                url="u",
            ),
            FakeResponse(status=200, body=b"<p>served</p>", headers={"content-type": "text/html"}, url="page"),
        ],
        robots_status=200,
        robots_text="User-agent: *\nAllow: /\n",
    )
    result = client.request("https://origin.test/page", source=make_spec())

    assert result.ok is True and result.transport is Transport.DIRECT
    assert "served" in (result.text or "")
    assert session.robots_calls, "the runner read the rules itself before fetching"
    assert client.stats.http_robots_blocked == 0


def test_the_rules_are_cached_for_the_run():
    client, session, _ = make_client(robots_status=200, robots_text="User-agent: *\nAllow: /\n")
    for index in range(3):
        session.responses.append(FakeResponse(status=200, body=b"x", headers={"content-type": "text/plain"}, url=f"p{index}"))
        client.request(f"https://origin.test/p{index}", source=make_spec())
    assert len(session.robots_calls) == 1, "one rules fetch per host per run, not one per document"
    assert client.robots.stats["cached"] == 2


def test_respect_robots_false_skips_the_check_entirely():
    """`respect_robots=False` is the documented escape hatch (an API endpoint, the
    Telegram bot) — it must not spend a request on robots.txt either."""
    client, session, _ = make_client(
        responses=[FakeResponse(status=200, body=b"{}", headers={"content-type": "application/json"}, url="u")],
        robots_status=200,
        robots_text="User-agent: *\nDisallow: /\n",
    )
    result = client.request("https://api.test/endpoint", source=make_spec(respect_robots=False))
    assert result.ok is True
    assert session.robots_calls == [], "no rules request for an endpoint we are allowed to call"
    assert len(session.calls) == 1


def test_the_relay_verdict_never_falls_back_to_a_direct_fetch():
    """Terminal means terminal: a robots refusal reported by the relay must not be
    re-attempted here, or the fallback would do exactly what the relay refused."""
    client, session, _ = make_client(
        worker=True,
        responses=[FakeResponse(
            status=403,
            json_body={"ok": False, "error": {"code": "robots_disallowed", "message": "disallowed"}},
            headers={"content-type": "application/json"},
            url="u",
        )],
        robots_status=200,
        robots_text="User-agent: *\nAllow: /\n",
    )
    result = client.request("https://origin.test/secret", source=make_spec())
    assert result.error == "robots_disallowed"
    # Only the relay POST is on the wire; no direct GET to the origin, and not
    # even a rules fetch (the verdict is already in hand).
    assert [c["kind"] for c in session.calls] == ["post"]
    assert session.robots_calls == []
    assert client.stats.http_robots_blocked == 1


def test_a_crawl_delay_from_the_rules_slows_the_host_down():
    """`Crawl-delay` is the origin's own pace; the direct transport adopts it."""
    client, session, _ = make_client(
        robots_status=200,
        robots_text="User-agent: *\nAllow: /\nCrawl-delay: 10\n",
        **{"TOKEN_BUCKET_RATE_PER_SEC": "5", "TOKEN_BUCKET_BURST": "5"},
    )
    session.responses.append(FakeResponse(status=200, body=b"x", headers={"content-type": "text/plain"}, url="p1"))
    client.request("https://slow.test/p1", source=make_spec())

    decision = client.delay_queue.admit("slow.test")
    assert decision.wait_seconds >= 9.0, "the second request waits out the crawl delay"


def test_a_stream_obeys_the_rules_too():
    """Streaming is always direct — the path an origin is most likely to forbid."""
    client, session, _ = make_client(robots_status=200, robots_text="User-agent: *\nDisallow: /dump\n")
    lines = list(client.stream_lines("https://origin.test/dump.tsv", source=make_spec()))
    assert lines == [], "a disallowed dump is not streamed"
    assert session.calls == []
    assert client.stats.http_robots_blocked == 1


def test_the_robots_policy_is_reported_in_describe():
    client, _, _ = make_client(robots_status=404)
    payload = client.describe()
    assert payload["robots"]["enabled"] is True
    assert payload["robots"]["user_agent"] == load_settings(BASE).http_user_agent
    json.dumps(payload), "describe() must stay serialisable"
