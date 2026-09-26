"""Token-bucket rate limiter and the local delay queue.

This is the *fallback* half of PuppetNET's proxy story: when the Cloudflare
relay is unreachable, returns 5xx, or its circuit breaker has tripped, the
harvester talks to origins directly and politeness is enforced here instead.

Two primitives:

:class:`TokenBucket`
    Classic leaky/token bucket with a monotonic clock. ``acquire()`` blocks
    until a token is available and returns the time actually spent waiting,
    which the pipeline accumulates into ``IngestStats.seconds_throttled``.

:class:`DelayQueue`
    A per-host scheduler layered on top of the buckets. It serialises requests
    to the same host, adds jitter so we do not emit a metronomic signature,
    honours ``Retry-After`` hints from origins, and degrades a host to a
    longer cooldown after repeated failures.
"""

from __future__ import annotations

import random
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from ..logging_utils import get_logger

__all__ = ["TokenBucket", "DelayQueue", "HostState", "AdmissionDecision"]

logger = get_logger("net.delay")


@dataclass
class AdmissionDecision:
    """Outcome of a :meth:`DelayQueue.admit` call."""

    host: str
    allowed: bool
    wait_seconds: float = 0.0
    reason: str = ""
    tokens_remaining: float = 0.0

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.allowed


class TokenBucket:
    """Thread-safe token bucket on a monotonic clock.

    ``rate`` tokens are added per second up to ``burst`` capacity. A cost
    greater than the burst is admitted immediately after a full refill wait so
    callers never deadlock.
    """

    __slots__ = ("rate", "burst", "_tokens", "_last", "_lock", "_total_wait", "_acquired", "_clock")

    def __init__(self, rate: float, burst: float | None = None, *, clock: Callable[[], float] = time.monotonic) -> None:
        if rate <= 0:
            raise ValueError("TokenBucket rate must be positive")
        self.rate = float(rate)
        self.burst = float(burst if burst is not None else max(1.0, rate))
        self._tokens = self.burst
        self._clock = clock
        self._last = clock()
        self._lock = threading.Lock()
        self._total_wait = 0.0
        self._acquired = 0

    # -- internals --------------------------------------------------------
    def _refill_locked(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
        self._last = now

    # -- public API -------------------------------------------------------
    @property
    def tokens(self) -> float:
        with self._lock:
            self._refill_locked()
            return self._tokens

    @property
    def total_wait_seconds(self) -> float:
        with self._lock:
            return self._total_wait

    @property
    def acquired(self) -> int:
        with self._lock:
            return self._acquired

    def _clamp_cost(self, cost: float) -> float:
        """Cap a request at the bucket's capacity.

        A cost above ``burst`` can never be satisfied — the refill stops at
        capacity — so an unclamped request would spin forever. ``stream_lines``
        legitimately asks for ``cost=2`` on a bucket whose burst is
        ``1 / GLOBAL_MIN_INTERVAL_SECONDS`` (1.33 by default); clamping turns
        that into "wait for a full bucket, then go", which is what the caller
        means.
        """
        if cost > self.burst:
            logger.debug("token cost %.2f exceeds burst %.2f for rate %.2f — clamping", cost, self.burst, self.rate)
            return self.burst
        return cost

    def wait_time(self, cost: float = 1.0) -> float:
        """Seconds until ``cost`` tokens will be available (0 if available now)."""
        cost = self._clamp_cost(cost)
        with self._lock:
            self._refill_locked()
            if self._tokens >= cost:
                return 0.0
            deficit = cost - self._tokens
            return deficit / self.rate

    def try_acquire(self, cost: float = 1.0) -> bool:
        """Non-blocking acquisition."""
        cost = self._clamp_cost(cost)
        with self._lock:
            self._refill_locked()
            if self._tokens >= cost:
                self._tokens -= cost
                self._acquired += 1
                return True
            return False

    def acquire(self, cost: float = 1.0, timeout: float | None = None, sleeper: Callable[[float], None] = time.sleep) -> float:
        """Block until ``cost`` tokens are available.

        Returns the number of seconds spent waiting. If ``timeout`` elapses
        first the tokens are *not* consumed and the elapsed wait is returned
        with a negative-token budget untouched.
        """
        if cost <= 0:
            return 0.0
        cost = self._clamp_cost(cost)
        waited = 0.0
        # The deadline must be measured on the *injected* clock, otherwise a
        # test (or a paused worker) driving a synthetic clock mixes time bases.
        deadline = None if timeout is None else self._clock() + timeout
        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= cost:
                    self._tokens -= cost
                    self._acquired += 1
                    self._total_wait += waited
                    return waited
                deficit = cost - self._tokens
                wait = deficit / self.rate
                if deadline is not None:
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        self._total_wait += waited
                        return waited
                    wait = min(wait, remaining)
            # Never spin: a zero-length nap would burn CPU without advancing
            # the bucket, which is how an unclamped cost used to deadlock.
            sleeper(max(wait, 1.0 / (self.rate * 1000.0)))
            waited += wait

    def reset(self, tokens: float | None = None) -> None:
        with self._lock:
            self._tokens = self.burst if tokens is None else float(tokens)
            self._last = self._clock()

    def describe(self) -> dict[str, float]:
        with self._lock:
            self._refill_locked()
            return {
                "rate": self.rate,
                "burst": self.burst,
                "tokens": round(self._tokens, 4),
                "acquired": self._acquired,
                "total_wait_seconds": round(self._total_wait, 3),
            }


@dataclass
class HostState:
    """Per-host politeness bookkeeping."""

    host: str
    bucket: TokenBucket
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    last_request_at: float = 0.0
    request_count: int = 0
    failure_history: deque[float] = field(default_factory=lambda: deque(maxlen=20))

    def penalise(self, retry_after: float = 0.0, backoff: float = 0.0, *, now: float | None = None) -> None:
        """Push the host into cooldown after a 429/5xx or a transport error.

        ``now`` must come from the queue's injected clock: computing a cooldown
        against ``time.monotonic()`` while the queue compares it to a synthetic
        clock mixes time bases and produces absurd waits.
        """
        moment = time.monotonic() if now is None else float(now)
        self.consecutive_failures += 1
        self.failure_history.append(moment)
        delay = max(retry_after, backoff, 2.0 ** min(self.consecutive_failures, 6))
        self.cooldown_until = moment + delay

    def succeed(self) -> None:
        self.consecutive_failures = 0
        self.cooldown_until = 0.0


class DelayQueue:
    """Serialise outbound requests per host with jittered, adaptive pacing.

    The queue is intentionally simple and *blocking*: the daily GitHub Actions
    run is throughput-insensitive (a few thousand requests over ~30 minutes)
    but very sensitive to being IP-banned, so correctness and politeness beat
    concurrency here. Hosts are independent of each other, and the optional
    ``global_bucket`` caps the aggregate request rate across all hosts.
    """

    def __init__(
        self,
        rate_per_sec: float = 0.4,
        burst: int = 3,
        *,
        jitter_seconds: float = 0.35,
        global_rate_per_sec: float | None = 1.3,
        min_interval_seconds: float = 0.75,
        max_wait_seconds: float = 300.0,
        rng: random.Random | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.rate_per_sec = float(rate_per_sec)
        self.burst = float(burst)
        self.jitter_seconds = float(jitter_seconds)
        self.min_interval_seconds = float(min_interval_seconds)
        self.max_wait_seconds = float(max_wait_seconds)
        self._rng = rng or random.Random(1337)
        self._sleep = sleeper
        self._clock = clock
        self._hosts: dict[str, HostState] = {}
        self._host_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._registry_lock = threading.Lock()
        self._global_bucket = (
            TokenBucket(global_rate_per_sec, burst=max(1.0, global_rate_per_sec), clock=clock)
            if global_rate_per_sec
            else None
        )
        self._total_wait = 0.0
        self._admissions = 0
        self._deferrals = 0

    # -- host registry ----------------------------------------------------
    def host_state(self, host: str) -> HostState:
        with self._registry_lock:
            state = self._hosts.get(host)
            if state is None:
                state = HostState(host=host, bucket=TokenBucket(self.rate_per_sec, self.burst, clock=self._clock))
                self._hosts[host] = state
            return state

    def set_host_policy(self, host: str, rate_per_sec: float | None = None, burst: float | None = None) -> None:
        """Override politeness for a specific host (e.g. a slow official register)."""
        state = self.host_state(host)
        rate = float(rate_per_sec or self.rate_per_sec)
        capacity = float(burst if burst is not None else self.burst)
        state.bucket = TokenBucket(rate, capacity, clock=self._clock)

    # -- admission --------------------------------------------------------
    def admit(self, host: str, cost: float = 1.0) -> AdmissionDecision:
        """Non-blocking view of what :meth:`acquire` would do."""
        state = self.host_state(host)
        now = self._clock()
        cooldown = max(0.0, state.cooldown_until - now)
        since_last = now - state.last_request_at if state.last_request_at else float("inf")
        gap = max(0.0, self.min_interval_seconds - since_last)
        wait = max(cooldown, gap, state.bucket.wait_time(cost))
        if self._global_bucket is not None:
            wait = max(wait, self._global_bucket.wait_time(cost))
        allowed = wait <= self.max_wait_seconds
        reason = ""
        if not allowed:
            reason = f"host {host} needs {wait:.1f}s (> max_wait {self.max_wait_seconds}s)"
        elif cooldown > 0:
            reason = "cooldown"
        elif wait > 0:
            reason = "throttled"
        return AdmissionDecision(
            host=host,
            allowed=allowed,
            wait_seconds=round(min(wait, self.max_wait_seconds), 3),
            reason=reason,
            tokens_remaining=round(state.bucket.tokens, 4),
        )

    def acquire(self, host: str, cost: float = 1.0, timeout: float | None = None) -> float:
        """Block until the host (and the global budget) permit one request.

        Returns the total seconds spent waiting. Raises :class:`TimeoutError`
        if the permission would take longer than ``timeout``/``max_wait_seconds``.
        """
        lock = self._host_locks[host]
        budget = self.max_wait_seconds if timeout is None else min(timeout, self.max_wait_seconds)
        waited = 0.0
        with lock:
            while True:
                decision = self.admit(host, cost)
                if not decision.allowed and decision.wait_seconds > budget:
                    self._deferrals += 1
                    raise TimeoutError(
                        f"Rate-limit delay queue: {host} requires {decision.wait_seconds}s "
                        f"which exceeds the {budget}s budget"
                    )
                if decision.wait_seconds <= 0:
                    break
                nap = min(decision.wait_seconds + self._jitter(), budget - waited if budget else decision.wait_seconds)
                nap = max(0.0, nap)
                self._sleep(nap)
                waited += nap
                self._total_wait += nap
                if budget and waited >= budget:
                    break

            state = self.host_state(host)
            state.bucket.acquire(cost, timeout=max(1.0, budget - waited) if budget else None, sleeper=self._sleep)
            if self._global_bucket is not None:
                extra = self._global_bucket.acquire(cost, timeout=max(1.0, budget) , sleeper=self._sleep)
                waited += extra
                self._total_wait += extra
            state.last_request_at = self._clock()
            state.request_count += 1
            self._admissions += 1
        return waited

    def _jitter(self) -> float:
        if self.jitter_seconds <= 0:
            return 0.0
        return self._rng.uniform(0, self.jitter_seconds)

    # -- feedback ---------------------------------------------------------
    def report_success(self, host: str) -> None:
        self.host_state(host).succeed()

    def report_failure(self, host: str, *, retry_after: float = 0.0, status: int | None = None) -> float:
        """Record a failed attempt and return the cooldown applied (seconds)."""
        state = self.host_state(host)
        backoff = 0.0
        if status in (429, 503):
            backoff = max(retry_after, 5.0)
        elif status is not None and 500 <= status < 600:
            backoff = 2.0
        state.penalise(retry_after=retry_after, backoff=backoff, now=self._clock())
        cooldown = max(0.0, state.cooldown_until - self._clock())
        logger.debug(
            "host %s penalised (status=%s retry_after=%.1fs consecutive=%d cooldown=%.1fs)",
            host, status, retry_after, state.consecutive_failures, cooldown,
        )
        return cooldown

    def respect_crawl_delay(self, host: str, crawl_delay_seconds: float) -> None:
        """Adopt a robots.txt Crawl-delay as the host's rate ceiling."""
        if crawl_delay_seconds and crawl_delay_seconds > 0:
            self.set_host_policy(host, rate_per_sec=min(self.rate_per_sec, 1.0 / crawl_delay_seconds), burst=1)

    # -- introspection ----------------------------------------------------
    def stats(self) -> dict[str, object]:
        with self._registry_lock:
            hosts = {
                host: {
                    "requests": state.request_count,
                    "consecutive_failures": state.consecutive_failures,
                    "cooldown_remaining": round(max(0.0, state.cooldown_until - self._clock()), 2),
                    "bucket": state.bucket.describe(),
                }
                for host, state in sorted(self._hosts.items())
            }
        return {
            "admissions": self._admissions,
            "deferrals": self._deferrals,
            "total_wait_seconds": round(self._total_wait, 3),
            "global_bucket": self._global_bucket.describe() if self._global_bucket else None,
            "hosts": hosts,
        }

    def iter_hosts(self) -> Iterable[str]:
        with self._registry_lock:
            return tuple(self._hosts.keys())
