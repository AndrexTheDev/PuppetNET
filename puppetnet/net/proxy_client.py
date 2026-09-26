"""Outbound HTTP client: Cloudflare relay first, token-bucket fallback second.

Transport selection is the heart of PuppetNET's anti-rate-limiting strategy::

    request(url)
      │
      ├─ circuit OPEN? ─────────────────────────► direct (token bucket)
      │
      ├─ POST {worker}/fetch  ──200──────────────► result
      │        │
      │        ├──202 + task_id──────────────────► poll {worker}/tasks/{id}
      │        │
      │        ├──429 (host over budget)─────────► local DelayQueue backoff,
      │        │                                   then re-try / direct
      │        │
      │        └──5xx / network error────────────► circuit breaker++ , direct
      │
      └─ direct: DelayQueue.acquire(host) → requests.Session → retries

Every hop is counted into :class:`~puppetnet.models.IngestStats` so the daily
run report shows exactly how much traffic went through the edge and how much
had to fall back.
"""

from __future__ import annotations

import base64
import gzip
import io
import json
import time
import zlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import urlencode, urlparse, urlunparse

import requests
from requests.adapters import HTTPAdapter

from ..logging_utils import get_logger
from ..models import IngestStats, SourceSpec
from .headers import HeaderFactory
from .token_bucket import DelayQueue

__all__ = ["FetchClient", "FetchResult", "Transport", "CircuitBreaker", "FetchError"]

logger = get_logger("net.client")


class Transport(str, Enum):
    WORKER = "worker"
    WORKER_QUEUE = "worker_queue"
    DIRECT = "direct"
    CACHE = "cache"


class FetchError(RuntimeError):
    """Raised when every transport option has been exhausted."""

    def __init__(self, message: str, *, url: str = "", status: int | None = None, attempts: int = 0) -> None:
        super().__init__(message)
        self.url = url
        self.status = status
        self.attempts = attempts


@dataclass
class FetchResult:
    """Normalised response regardless of which transport served it."""

    url: str
    status: int = 0
    ok: bool = False
    transport: Transport = Transport.DIRECT
    content_type: str = ""
    text: str | None = None
    content: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    elapsed_ms: float = 0.0
    attempts: int = 1
    error: str = ""
    final_url: str = ""
    task_id: str = ""
    deferred: bool = False
    truncated: bool = False
    source_id: str = ""
    byte_length: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    # -- convenience -----------------------------------------------------
    def json(self) -> Any:
        if not self.text:
            raise ValueError(f"No JSON body in response for {self.url} (status={self.status})")
        return json.loads(self.text)

    @property
    def is_text(self) -> bool:
        ctype = (self.content_type or "").lower()
        if not ctype:
            return bool(self.text)
        return any(
            token in ctype
            for token in ("text/", "json", "xml", "rss", "atom", "csv", "tsv", "javascript", "html", "ndjson")
        )

    def raise_for_status(self) -> None:
        if not self.ok:
            raise FetchError(
                self.error or f"HTTP {self.status} for {self.url}", url=self.url, status=self.status, attempts=self.attempts
            )

    def describe(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "status": self.status,
            "ok": self.ok,
            "transport": self.transport.value,
            "content_type": self.content_type,
            "bytes": self.byte_length,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "attempts": self.attempts,
            "task_id": self.task_id or None,
            "error": self.error or None,
        }


class CircuitBreaker:
    """Trips after repeated relay failures; half-opens to probe recovery."""

    def __init__(self, threshold: int = 4, cooldown_seconds: float = 300.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.threshold = max(1, int(threshold))
        self.cooldown_seconds = float(cooldown_seconds)
        self._clock = clock
        self.failures = 0
        self.opened_at: float | None = None
        self.trip_count = 0

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if self._clock() - self.opened_at >= self.cooldown_seconds:
            # Half-open: allow a probe, keep the failure budget tight.
            self.failures = max(0, self.threshold - 1)
            return False
        return True

    def record_failure(self) -> bool:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = self._clock()
            self.trip_count += 1
            logger.warning("Edge relay circuit OPEN after %d consecutive failures (cooldown %.0fs)", self.failures, self.cooldown_seconds)
            return True
        return False

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def describe(self) -> dict[str, Any]:
        return {
            "open": self.is_open,
            "failures": self.failures,
            "threshold": self.threshold,
            "trip_count": self.trip_count,
            "cooldown_seconds": self.cooldown_seconds,
        }


def _decode_body(content: bytes, headers: Mapping[str, str], content_type: str) -> tuple[str, bytes]:
    """Decode (and transparently gunzip/inflate) a body into text + bytes."""
    encoding = (headers.get("content-encoding") or "").lower().strip()
    body = content
    try:
        if encoding == "gzip":
            body = gzip.GzipFile(fileobj=io.BytesIO(content)).read()
        elif encoding == "deflate":
            body = zlib.decompress(content, -zlib.MAX_WBITS)
        elif encoding == "br":
            try:
                import brotli  # type: ignore

                body = brotli.decompress(content)
            except ImportError:
                body = content
    except (OSError, zlib.error, ValueError) as exc:
        logger.debug("Body decompression failed (%s); using raw bytes", exc)
        body = content

    charset = ""
    for part in (content_type or "").split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            charset = part.split("=", 1)[1].strip("\"' ")
    candidates = [c for c in (charset, "utf-8", "cp1252", "latin-1") if c]
    for candidate in candidates:
        try:
            return body.decode(candidate, errors="strict"), body
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace"), body


def _gunzip_lines(raw_stream: Any, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
    """Incrementally gunzip a urllib3 raw stream and yield lines."""
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    buffer = b""
    while True:
        try:
            chunk = raw_stream.read(chunk_size, decode_content=False)
        except Exception as exc:  # noqa: BLE001 - urllib3 raises many types
            logger.warning("gzip stream read failed: %s", exc)
            break
        if not chunk:
            break
        try:
            buffer += decompressor.decompress(chunk)
        except zlib.error as exc:
            logger.warning("gzip decompression failed: %s", exc)
            break
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            yield line
    try:
        buffer += decompressor.flush()
    except zlib.error:
        pass
    if buffer:
        yield buffer


class FetchClient:
    """Polite, resilient HTTP client for the harvester."""

    def __init__(
        self,
        settings: Any,
        *,
        delay_queue: DelayQueue | None = None,
        headers_factory: HeaderFactory | None = None,
        stats: IngestStats | None = None,
        session: requests.Session | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.stats = stats if stats is not None else IngestStats()
        self.delay_queue = delay_queue or DelayQueue(
            rate_per_sec=settings.token_bucket_rate_per_sec,
            burst=settings.token_bucket_burst,
            jitter_seconds=settings.token_bucket_jitter_seconds,
            global_rate_per_sec=1.0 / max(0.05, settings.global_min_interval_seconds),
            min_interval_seconds=settings.global_min_interval_seconds,
            sleeper=sleeper,
            clock=clock,
        )
        self.headers_factory = headers_factory or HeaderFactory(
            bot_user_agent=settings.http_user_agent, run_salt=settings.run_id or "puppetnet"
        )
        self.breaker = CircuitBreaker(
            threshold=settings.worker_circuit_breaker_threshold,
            cooldown_seconds=settings.worker_circuit_breaker_cooldown_seconds,
            clock=clock,
        )
        self._sleeper = sleeper
        self._clock = clock
        self._session = session or self._build_session(settings)
        self._local_cache: dict[str, FetchResult] = {}

    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_session(settings: Any) -> requests.Session:
        session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=settings.concurrency * 2,
            pool_maxsize=max(4, settings.concurrency * 4),
            max_retries=0,  # retries are ours: they must feed the delay queue
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.trust_env = False  # never inherit proxy settings from the runner
        return session

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # pragma: no cover - best effort
            pass

    def __enter__(self) -> "FetchClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def get(self, url: str, **kwargs: Any) -> FetchResult:
        return self.request(url, method="GET", **kwargs)

    def get_json(self, url: str, **kwargs: Any) -> Any:
        result = self.get(url, accept="application/json", **kwargs)
        result.raise_for_status()
        try:
            return result.json()
        except ValueError as exc:
            # An HTML error page served with a 200 is the common case here; the
            # caller wants a FetchError, not a JSON decode traceback.
            raise FetchError(f"response from {url} is not valid JSON: {exc}", url=url, status=result.status, attempts=result.attempts) from exc

    def get_bytes(self, url: str, **kwargs: Any) -> bytes:
        result = self.get(url, **kwargs)
        result.raise_for_status()
        return result.content

    def post(self, url: str, *, json_body: Any = None, data: Any = None, **kwargs: Any) -> FetchResult:
        return self.request(url, method="POST", json_body=json_body, data=data, **kwargs)

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        data: Any = None,
        headers: Mapping[str, str] | None = None,
        mode: str = "browser",
        source: SourceSpec | None = None,
        source_id: str = "",
        referer: str | None = None,
        accept: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        respect_robots: bool | None = None,
        cache_ttl_seconds: int | None = None,
        allow_defer: bool | None = None,
        rate_limit: Mapping[str, Any] | None = None,
        allow_worker: bool = True,
    ) -> FetchResult:
        """Fetch ``url`` with relay-first, bucket-fallback semantics."""
        source_id = source_id or (source.id if source else "")
        if params:
            url = self._append_params(url, params)

        parsed = urlparse(url)
        host = parsed.hostname or ""
        cache_key = f"{method}|{url}|{json.dumps(headers or {}, sort_keys=True)}"
        if cache_key in self._local_cache:
            cached = self._local_cache[cache_key]
            logger.debug("in-process cache hit for %s", url)
            return FetchResult(**{**cached.__dict__, "transport": Transport.CACHE})

        if source is not None:
            self.delay_queue.set_host_policy(host, rate_per_sec=source.rate_per_sec, burst=source.burst)

        timeout = timeout or (source.timeout_ms / 1000.0 if source else self.settings.http_timeout_seconds)
        retries = self.settings.http_max_retries if max_retries is None else max_retries
        robots = (source.respect_robots if source else True) if respect_robots is None else respect_robots
        cache_ttl = (source.cache_ttl_seconds if source else 0) if cache_ttl_seconds is None else cache_ttl_seconds
        defer = self.settings.worker_queue_on_limit if allow_defer is None else allow_defer
        bucket_policy = dict(rate_limit or {})
        if source is not None:
            bucket_policy.setdefault("rate_per_sec", source.rate_per_sec)
            bucket_policy.setdefault("burst", source.burst)

        task: dict[str, Any] = {
            "url": url,
            "method": method.upper(),
            "headers": dict(headers or {}),
            "mode": mode,
            "referer": referer,
            "accept": accept,
            "timeout_ms": int(timeout * 1000),
            "max_attempts": max(1, retries),
            "respect_robots": robots,
            "cache_ttl_seconds": cache_ttl,
            "source_id": source_id,
            "rate_limit": bucket_policy,
            "json": json_body,
            "body_text": data if isinstance(data, str) else None,
        }

        last_error = ""
        last_status = 0
        attempts = 0
        started = self._clock()

        use_worker = allow_worker and self.settings.worker_configured and not self.breaker.is_open
        if allow_worker and self.settings.worker_configured and self.breaker.is_open:
            logger.info("Edge relay circuit open — serving %s directly", host)

        for attempt in range(1, retries + 2):
            attempts = attempt
            if use_worker:
                result = self._via_worker(task, source_id=source_id, allow_defer=defer, attempt=attempt)
                if result is not None:
                    result.attempts = attempt
                    result.elapsed_ms = (self._clock() - started) * 1000.0
                    result.source_id = source_id
                    if result.ok:
                        self.breaker.record_success()
                        if cache_ttl:
                            self._local_cache[cache_key] = result
                        return result
                    last_status = result.status
                    last_error = result.error or f"HTTP {result.status}"
                    if result.status in (401, 403, 404, 410, 451):
                        # Origin-level verdicts: retrying through another transport will not help.
                        self._count("http_errors")
                        return result
                    if result.status == 429 or result.deferred:
                        self._count("http_rate_limited")
                        self.delay_queue.report_failure(host, retry_after=float(result.meta.get("retry_after") or 5.0), status=429)
                        if not self.settings.direct_fallback_enabled:
                            wait = min(float(result.meta.get("retry_after") or 5.0), 60.0)
                            logger.info("relay rate-limited %s — backing off %.1fs", host, wait)
                            self._sleep(wait)
                            continue
                    logger.warning("relay attempt %d for %s failed (%s) — falling back to direct", attempt, host, last_error)
                    use_worker = False  # degrade to direct for the remaining attempts
            elif self.settings.worker_configured and self.breaker.is_open and attempt > 1:
                # Re-probe the relay on later attempts (half-open).
                use_worker = False

            if not self.settings.direct_fallback_enabled:
                break

            result = self._direct(task, host=host, attempt=attempt)
            result.elapsed_ms = (self._clock() - started) * 1000.0
            result.source_id = source_id
            # The relay branch records this; without it a direct fetch always
            # reports attempts=1 and the run report under-counts retries.
            result.attempts = attempt
            if result.ok:
                self.delay_queue.report_success(host)
                if cache_ttl:
                    self._local_cache[cache_key] = result
                return result
            last_status = result.status
            last_error = result.error or f"HTTP {result.status}"
            if result.status in (401, 403, 404, 410, 451):
                self._count("http_errors")
                return result
            retry_after = float(result.headers.get("retry-after") or 0) if result.headers else 0.0
            cooldown = self.delay_queue.report_failure(host, retry_after=retry_after, status=result.status or None)
            if attempt <= retries:
                backoff = min(self.settings.http_backoff_cap_seconds, self.settings.http_backoff_base_seconds * (2 ** (attempt - 1)))
                wait = max(backoff, min(cooldown, self.settings.http_backoff_cap_seconds))
                logger.info("direct attempt %d for %s failed (%s) — sleeping %.1fs", attempt, host, last_error, wait)
                self._sleep(wait)
                self.stats.seconds_throttled += wait

        self._count("http_errors")
        failure = FetchResult(
            url=url,
            status=last_status,
            ok=False,
            transport=Transport.DIRECT,
            error=last_error or "all transports exhausted",
            attempts=attempts,
            elapsed_ms=(self._clock() - started) * 1000.0,
            source_id=source_id,
        )
        logger.error("giving up on %s after %d attempts: %s", url, attempts, failure.error)
        return failure

    # ------------------------------------------------------------------ #
    # Streaming (large datasets: ICIJ TSV dumps, register exports)
    # ------------------------------------------------------------------ #
    def stream_lines(
        self,
        url: str,
        *,
        source: SourceSpec | None = None,
        source_id: str = "",
        mode: str = "bot",
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        max_bytes: int | None = None,
        encoding: str = "utf-8",
        decompress_gzip: bool = True,
    ) -> Iterator[str]:
        """Yield decoded lines without buffering the whole payload.

        Streaming cannot traverse the edge relay (the Worker buffers a response
        before returning it, and free-tier CPU limits cap payload size), so this
        path is always direct — but it is still policed by the same token
        bucket and honours the source's politeness policy.
        """
        source_id = source_id or (source.id if source else "")
        host = urlparse(url).hostname or ""
        if source is not None:
            self.delay_queue.set_host_policy(host, rate_per_sec=source.rate_per_sec, burst=source.burst)
        cap = int(max_bytes or self.settings.http_max_response_bytes)
        timeout_seconds = float(timeout or (source.timeout_ms / 1000.0 if source else self.settings.http_timeout_seconds))

        waited = self.delay_queue.acquire(host, cost=2.0)  # a long-lived stream costs more
        if waited:
            self.stats.seconds_throttled += waited
        self._count("http_requests")
        self._count("http_direct_fallback")

        request_headers = self.headers_factory.build(url, mode=mode, extra=dict(headers or {}))
        request_headers["Accept-Encoding"] = "gzip" if decompress_gzip else "identity"
        try:
            with self._session.get(url, headers=request_headers, timeout=(15.0, timeout_seconds), stream=True) as response:
                if response.status_code >= 400:
                    logger.warning("stream %s returned HTTP %d", url, response.status_code)
                    self.delay_queue.report_failure(host, status=response.status_code)
                    return
                self.delay_queue.report_success(host)
                iterator: Any = response.iter_lines(decode_unicode=False)
                if decompress_gzip and (url.lower().endswith(".gz") or "gzip" in (response.headers.get("content-encoding") or "").lower()):
                    iterator = _gunzip_lines(response.raw)
                total = 0
                for raw_line in iterator:
                    if raw_line is None:
                        continue
                    if isinstance(raw_line, bytes):
                        total += len(raw_line)
                        if total > cap:
                            logger.warning("stream cap reached (%d bytes) for %s", cap, url)
                            return
                        line = raw_line.decode(encoding, errors="replace")
                    else:
                        line = str(raw_line)
                    yield line
        except requests.RequestException as exc:
            self._count("http_errors")
            self.delay_queue.report_failure(host, status=None)
            logger.error("streaming %s failed: %s", url, exc)
            return

    # ------------------------------------------------------------------ #
    # Transports
    # ------------------------------------------------------------------ #
    def _via_worker(self, task: Mapping[str, Any], *, source_id: str, allow_defer: bool, attempt: int) -> FetchResult | None:
        """POST the task to the Cloudflare relay. ``None`` ⇒ transport unusable."""
        endpoint = f"{self.settings.worker_url}/fetch"
        mode = task.get("mode", "browser")
        fingerprint = self.headers_factory.fingerprint_for(urlparse(task["url"]).hostname or "", attempt=attempt)

        payload: dict[str, Any] = {
            "url": task["url"],
            "method": task.get("method", "GET"),
            "timeout_ms": task.get("timeout_ms", 25_000),
            "max_attempts": task.get("max_attempts", 3),
            "respect_robots": task.get("respect_robots", False),
            "cache_ttl_seconds": task.get("cache_ttl_seconds", 0),
            "source_id": source_id or "unknown",
            "rate_limit": task.get("rate_limit") or {},
            "queue_on_limit": bool(allow_defer),
            "queue_delay_seconds": 0,
            "fingerprint_salt": fingerprint.salt,
            "referer": task.get("referer"),
            "cache_buster": "timestamp" if mode == "browser" else "off",
        }
        if task.get("json") is not None:
            payload["json"] = task["json"]
        if task.get("body_text"):
            payload["body_text"] = task["body_text"]

        # Headers the caller needs to control (API keys, Accept, UA policy).
        outbound_headers = self.headers_factory.worker_payload_headers(
            str(task["url"]), mode=mode, attempt=attempt, extra=task.get("headers") or {}
        )
        if task.get("accept"):
            outbound_headers["Accept"] = str(task["accept"])
        if mode == "bot":
            outbound_headers["User-Agent"] = self.headers_factory.bot_user_agent
        payload["headers"] = outbound_headers

        auth_headers = {
            "Authorization": f"Bearer {self.settings.worker_token}",
            "Content-Type": "application/json",
            "X-Source-Id": source_id or "unknown",
            "X-Request-Id": f"{self.settings.run_id or 'local'}:{attempt}:{abs(hash(task['url'])) % 10**9}",
        }

        started = self._clock()
        try:
            response = self._session.post(
                endpoint,
                data=json.dumps(payload),
                headers=auth_headers,
                timeout=(min(10.0, self.settings.worker_timeout_seconds), self.settings.worker_timeout_seconds + 10.0),
            )
        except requests.RequestException as exc:
            tripped = self.breaker.record_failure()
            logger.warning("edge relay unreachable (%s)%s", exc.__class__.__name__, " — circuit opened" if tripped else "")
            return None

        self._count("http_requests")
        elapsed = (self._clock() - started) * 1000.0

        # 5xx from the relay itself ⇒ transport failure, not an origin failure.
        if response.status_code >= 500 and response.status_code != 502:
            self.breaker.record_failure()
            logger.warning("edge relay returned %d", response.status_code)
            return None

        try:
            body = response.json()
        except ValueError:
            # No parseable body: a 401/403 here really is the relay rejecting us.
            if response.status_code in (401, 403):
                logger.error("edge relay rejected our credentials (HTTP %d) — check PROXY_AUTH_TOKEN", response.status_code)
            else:
                logger.warning("edge relay returned a non-JSON body (HTTP %d)", response.status_code)
            self.breaker.record_failure()
            return None

        error_body = body.get("error") if isinstance(body, dict) else None
        error_code = str((error_body or {}).get("code") or "") if isinstance(error_body, dict) else ""

        # A robots.txt refusal is an *origin policy verdict*, not a relay
        # failure. It must be terminal: falling back to a direct fetch here
        # would crawl a path the site explicitly disallowed, and tripping the
        # circuit breaker would take the relay down for every other host.
        if response.status_code == 403 and error_code == "robots_disallowed":
            logger.info("relay reports robots.txt disallows %s", task["url"])
            self._count("http_robots_blocked")
            return FetchResult(
                url=str(task["url"]),
                status=403,
                ok=False,
                transport=Transport.WORKER,
                error="robots_disallowed",
                meta={"policy": "robots.txt"},
            )

        if response.status_code in (401, 403):
            logger.error("edge relay rejected our credentials (HTTP %d) — check PROXY_AUTH_TOKEN", response.status_code)
            self.breaker.record_failure()
            return None

        self._count("http_via_worker")

        # Deferred into the Cloudflare Queue ⇒ poll for the parked result.
        if response.status_code == 202 or body.get("deferred"):
            task_id = str(body.get("task_id") or "")
            self._count("http_deferred_to_queue")
            logger.info("relay deferred %s to the edge queue (task=%s retry_after=%ss)", task["url"], task_id, body.get("retry_after"))
            if not task_id:
                return None
            return self._poll_task(task_id, url=str(task["url"]), source_id=source_id, waited_ms=elapsed)

        if response.status_code == 429 or body.get("error", {}).get("code") in {"host_rate_limited", "global_rate_limited"}:
            error = body.get("error", {})
            result = FetchResult(
                url=str(task["url"]),
                status=429,
                ok=False,
                transport=Transport.WORKER,
                error=error.get("message", "rate limited by edge relay"),
                elapsed_ms=elapsed,
                source_id=source_id,
                meta={"retry_after": error.get("retry_after") or 5, "policy": error.get("policy")},
            )
            return result


        # Normal relay response (origin status is nested inside).
        data = body if isinstance(body, dict) else {}
        if data.get("ok") is False and not data.get("status"):
            error = data.get("error", {})
            return FetchResult(
                url=str(task["url"]),
                status=int(error.get("status") or 0),
                ok=False,
                transport=Transport.WORKER,
                error=str(error.get("message") or error.get("code") or "relay error"),
                elapsed_ms=elapsed,
                source_id=source_id,
            )

        status = int(data.get("status") or response.status_code)
        headers = {str(k).lower(): str(v) for k, v in (data.get("headers") or {}).items()}
        text = data.get("text")
        content = b""
        if data.get("body_b64"):
            try:
                content = base64.b64decode(data["body_b64"])
            except (ValueError, TypeError) as exc:
                logger.warning("could not decode base64 body from relay: %s", exc)
        if text is None and content:
            text, content = _decode_body(content, headers, str(data.get("content_type") or ""))
        elif text is not None and not content:
            content = text.encode("utf-8", errors="replace")

        return FetchResult(
            url=str(task["url"]),
            final_url=str(data.get("url") or task["url"]),
            status=status,
            ok=bool(data.get("ok")) and 200 <= status < 400,
            transport=Transport.WORKER,
            content_type=str(data.get("content_type") or headers.get("content-type") or ""),
            text=text,
            content=content,
            headers=headers,
            elapsed_ms=float(data.get("elapsed_ms") or elapsed),
            attempts=int(data.get("attempts") or 1),
            error=str(data.get("error") or ""),
            truncated=bool(data.get("truncated")),
            byte_length=int(data.get("byte_length") or len(content)),
            source_id=source_id,
            meta={
                "colo": data.get("colo"),
                "cached": bool(data.get("cached")),
                "fingerprint": data.get("fingerprint_used"),
                "attempts_log": data.get("attempts_log"),
                "country": data.get("client_country"),
            },
        )

    def _poll_task(self, task_id: str, *, url: str, source_id: str, waited_ms: float) -> FetchResult:
        """Poll ``GET /tasks/{id}`` until the queued fetch resolves."""
        endpoint = f"{self.settings.worker_url}/tasks/{task_id}"
        headers = {"Authorization": f"Bearer {self.settings.worker_token}"}
        deadline = self._clock() + self.settings.worker_task_poll_seconds
        interval = max(0.5, self.settings.worker_task_poll_interval_seconds)
        polls = 0

        while self._clock() < deadline:
            polls += 1
            try:
                response = self._session.get(endpoint, headers=headers, timeout=(5.0, 20.0))
                body = response.json()
            except (requests.RequestException, ValueError) as exc:
                logger.debug("task poll %s failed: %s", task_id, exc)
                self._sleep(interval)
                continue

            status = str(body.get("status") or "")
            if status in {"done", "failed"}:
                payload = body.get("result") or {}
                self.breaker.record_success()
                if status == "failed":
                    return FetchResult(
                        url=url,
                        status=int(payload.get("status") or 0),
                        ok=False,
                        transport=Transport.WORKER_QUEUE,
                        error=str(payload.get("error") or "queued fetch failed"),
                        task_id=task_id,
                        source_id=source_id,
                    )
                text = payload.get("text")
                content = b""
                if payload.get("body_b64"):
                    try:
                        content = base64.b64decode(payload["body_b64"])
                    except (ValueError, TypeError):
                        content = b""
                if text is None and content:
                    text, content = _decode_body(content, {}, str(payload.get("content_type") or ""))
                elif text is not None and not content:
                    content = text.encode("utf-8", errors="replace")
                upstream_status = int(payload.get("status") or 200)
                return FetchResult(
                    url=url,
                    final_url=str(payload.get("url") or url),
                    status=upstream_status,
                    ok=bool(payload.get("ok")) and 200 <= upstream_status < 400,
                    transport=Transport.WORKER_QUEUE,
                    content_type=str(payload.get("content_type") or ""),
                    text=text,
                    content=content,
                    headers={str(k).lower(): str(v) for k, v in (payload.get("headers") or {}).items()},
                    elapsed_ms=waited_ms + float(payload.get("elapsed_ms") or 0.0),
                    attempts=int(payload.get("attempts") or 1),
                    error=str(payload.get("error") or ""),
                    byte_length=int(payload.get("byte_length") or len(content)),
                    task_id=task_id,
                    source_id=source_id,
                    meta={"queued": True, "polls": polls, "colo": payload.get("colo")},
                )

            # still queued
            retry_after = float(body.get("retry_after") or interval)
            self._sleep(min(max(retry_after, 1.0), interval * 3))

        logger.warning("queued task %s did not resolve within %.0fs", task_id, self.settings.worker_task_poll_seconds)
        return FetchResult(
            url=url,
            status=0,
            ok=False,
            transport=Transport.WORKER_QUEUE,
            error="queued task timed out",
            task_id=task_id,
            source_id=source_id,
            deferred=True,
            meta={"polls": polls},
        )

    def _direct(self, task: Mapping[str, Any], *, host: str, attempt: int) -> FetchResult:
        """Fetch straight from the runner, policed by the local delay queue."""
        url = str(task["url"])
        mode = task.get("mode", "browser")
        accept = task.get("accept")

        try:
            waited = self.delay_queue.acquire(host, timeout=self.settings.http_backoff_cap_seconds * 4)
        except TimeoutError as exc:
            self._count("http_rate_limited")
            return FetchResult(url=url, ok=False, status=0, error=f"delay queue: {exc}", transport=Transport.DIRECT)
        if waited:
            self.stats.seconds_throttled += waited

        headers = self.headers_factory.build(
            url,
            mode=mode,
            attempt=attempt,
            referer=task.get("referer"),
            accept=accept,
            extra=task.get("headers") or {},
        )
        timeout_seconds = max(5.0, float(task.get("timeout_ms") or self.settings.http_timeout_seconds * 1000) / 1000.0)
        started = self._clock()
        self._count("http_requests")
        self._count("http_direct_fallback")

        kwargs: dict[str, Any] = {
            "headers": headers,
            "timeout": (min(10.0, timeout_seconds), timeout_seconds),
            "allow_redirects": True,
            "stream": True,
        }
        if task.get("json") is not None:
            kwargs["data"] = json.dumps(task["json"])
            headers.setdefault("Content-Type", "application/json")
        elif task.get("body_text"):
            kwargs["data"] = task["body_text"]

        try:
            with self._session.request(str(task.get("method", "GET")), url, **kwargs) as response:
                raw = self._read_capped(response, self.settings.http_max_response_bytes)
                content_type = response.headers.get("content-type", "")
                text, content = _decode_body(raw, response.headers, content_type) if self._looks_textual(content_type) else (None, raw)
                result = FetchResult(
                    url=url,
                    final_url=response.url or url,
                    status=int(response.status_code),
                    ok=200 <= response.status_code < 400,
                    transport=Transport.DIRECT,
                    content_type=content_type,
                    text=text,
                    content=content,
                    headers={str(k).lower(): str(v) for k, v in response.headers.items()},
                    elapsed_ms=(self._clock() - started) * 1000.0,
                    byte_length=len(content),
                    truncated=len(content) >= self.settings.http_max_response_bytes,
                )
                if not result.ok:
                    result.error = f"HTTP {result.status}"
                return result
        except requests.Timeout as exc:
            return FetchResult(url=url, ok=False, status=0, transport=Transport.DIRECT, error=f"timeout: {exc}", elapsed_ms=(self._clock() - started) * 1000.0)
        except requests.RequestException as exc:
            return FetchResult(
                url=url,
                ok=False,
                status=0,
                transport=Transport.DIRECT,
                error=f"{exc.__class__.__name__}: {exc}",
                elapsed_ms=(self._clock() - started) * 1000.0,
            )

    @staticmethod
    def _read_capped(response: requests.Response, max_bytes: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total >= max_bytes:
                break
        body = b"".join(chunks)
        return body[:max_bytes]

    @staticmethod
    def _looks_textual(content_type: str) -> bool:
        ctype = (content_type or "").lower()
        if not ctype:
            return True
        return any(token in ctype for token in ("text/", "json", "xml", "rss", "atom", "csv", "tsv", "html", "javascript", "ndjson"))

    # ------------------------------------------------------------------ #
    @staticmethod
    def _append_params(url: str, params: Mapping[str, Any]) -> str:
        if not params:
            return url
        parsed = urlparse(url)
        query = parsed.query
        extra = urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
        merged = f"{query}&{extra}" if query and extra else (extra or query)
        return urlunparse(parsed._replace(query=merged))

    def _sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self._sleeper(min(seconds, 300.0))

    def _count(self, field_name: str, amount: int = 1) -> None:
        current = getattr(self.stats, field_name, 0)
        setattr(self.stats, field_name, int(current) + amount)

    # ------------------------------------------------------------------ #
    def describe(self) -> dict[str, Any]:
        return {
            "worker_configured": self.settings.worker_configured,
            "worker_url": self.settings.worker_url or None,
            "circuit_breaker": self.breaker.describe(),
            "delay_queue": self.delay_queue.stats(),
            "headers": self.headers_factory.describe(),
            "local_cache_entries": len(self._local_cache),
        }
