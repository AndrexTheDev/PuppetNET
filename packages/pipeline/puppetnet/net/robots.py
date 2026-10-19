"""`robots.txt` for the **direct** transport.

The edge relay fetches and evaluates `robots.txt` for every request it serves
(see [docs/edge-relay.md](../../docs/edge-relay.md) § robots.txt). That protection
is spent the moment the relay is unavailable: `DIRECT_FALLBACK_ENABLED` makes the
runner talk to origins itself, and that path had no robots handling at all — it
checked `respect_robots` only for the task it sent to the Worker. A relay outage
therefore silently turned a polite crawler into an impolite one, which is the
worst kind of regression: it happens exactly when nobody is watching, and the
first symptom is a refused IP.

This module closes that hole: the client consults it before a direct request or a
direct stream, and it refuses what the origin forbids. It implements RFC 9309 as
the Worker does — longest-match precedence, `*` wildcards, `$` anchoring, group
selection by user-agent — but the two implementations stay separate on purpose:

* the Worker is TypeScript on V8 with a per-colo KV cache, this is Python on a
  GitHub runner with an in-process cache;
* the verdicts must agree, so the rule set and the fixtures are shared by the
  smoke suites (`tests/worker_smoke.mjs` G15 and `tests/test_net.py`).

A fetch failure is *fail-open*: a robots.txt the origin cannot serve is not a
license to stop reading it, and failing closed would let a flaky origin take a
source offline. The refusal is fail-closed, and it is terminal — the caller must
not retry it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from ..logging_utils import get_logger

__all__ = ["RobotsPolicy", "RobotsDecision", "RobotsGroup", "parse_robots", "path_allowed"]

logger = get_logger("net.robots")

#: Longest a robots.txt body is read. RFC 9309 suggests crawlers cap it; 512 KiB
#: matches the Worker so the two evaluators see the same rules.
MAX_ROBOTS_BYTES = 512 * 1024

#: How long a parsed ruleset is kept. The Worker caches for 6 h in KV; a runner
#: lives at most a couple of hours, so the cache mostly saves the per-request
#: round trip inside one run.
DEFAULT_TTL_SECONDS = 21_600.0


@dataclass
class RobotsGroup:
    """One `User-agent:` block with its rules."""

    agents: list[str] = field(default_factory=list)
    rules: list[tuple[str, str]] = field(default_factory=list)  # (type, pattern)
    crawl_delay: float | None = None


def parse_robots(text: str) -> list[RobotsGroup]:
    """Parse a robots.txt body into groups.

    Deliberately tolerant, like the Worker's parser: unknown directives are
    ignored, a rule before any `User-agent:` is dropped (it belongs to no group),
    and a malformed `Crawl-delay` does not invalidate the rest of the file.
    """
    groups: list[RobotsGroup] = []
    current: RobotsGroup | None = None
    #: A group's agent list stays open only until its first directive line. Any
    #: non-`User-agent` line closes it — including `Crawl-delay` and directives we
    #: do not know, because a later `User-agent:` is then a new record, not another
    #: name for the previous one. Keying this on "has rules yet" instead merged
    #: `User-agent: *.` into the block above it whenever that block's only line was
    #: a `Crawl-delay`, which spread one crawler's crawl-delay over every other bot.
    agent_list_open = False

    for raw_line in str(text or "").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        field_name, _, value = line.partition(":")
        field_name = field_name.strip().lower()
        value = value.strip()

        if field_name == "user-agent":
            if current is not None and agent_list_open:
                current.agents.append(value.lower())
            else:
                current = RobotsGroup()
                groups.append(current)
                current.agents.append(value.lower())
                agent_list_open = True
            continue

        agent_list_open = False
        if current is None:
            continue

        if field_name == "disallow" or field_name == "allow":
            # An empty `Disallow:` ("restrictions lifted") is *not* a rule: it
            # matches nothing and therefore loses to every real pattern. Reading it
            # as `Allow: /` would let it outvote a one-character `Disallow: /` on
            # the tie-break below and crawl a site that had just banned everyone.
            if value:
                current.rules.append((field_name, value))
        elif field_name == "crawl-delay":
            try:
                delay = float(value)
            except ValueError:
                continue
            if delay > 0:
                current.crawl_delay = delay

    return groups


def path_allowed(pattern: str, path: str) -> bool:
    """Match one robots.txt pattern against a path (RFC 9309 § 2.2.2).

    `*` spans any characters, a trailing `$` anchors the end, everything else is
    a literal prefix match — so `/fish` covers `/fish/chips` *and* `/fishheads`.
    The Worker implements the same function; a pattern that behaves differently in
    the two places is a bug in one of them, which is why both suites test the same
    fixtures.
    """
    regex = ""
    for index, char in enumerate(pattern):
        if char == "*":
            regex += ".*"
        elif char == "$" and index == len(pattern) - 1:
            regex += "$"
        else:
            regex += "\\" + char if char in ".+?^{}()|[]\\$" else char
    try:
        import re

        return re.match(f"^{regex}", path) is not None
    except Exception:  # noqa: BLE001 - never let a malformed pattern break a run
        stripped = pattern.replace("*", "").replace("$", "")
        return path.startswith(stripped)


def _group_for(groups: list[RobotsGroup], user_agent: str) -> RobotsGroup | None:
    """The most specific group that applies to `user_agent`, else `*`."""
    ua = str(user_agent or "").lower()
    for group in groups:
        for agent in group.agents:
            # An empty `User-agent:` line matches nothing (JavaScript's
            # `"".includes` semantics would make it match *everything*).
            if agent and agent != "*" and agent in ua:
                return group
    for group in groups:
        if "*" in group.agents:
            return group
    return None


def verdict_for(groups: list[RobotsGroup], path: str, user_agent: str) -> tuple[bool, float | None]:
    """`(allowed, crawl_delay)` for one path and one user agent."""
    group = _group_for(groups, user_agent)
    if group is None:
        return True, None
    best: tuple[str, str] | None = None
    for rule_type, pattern in group.rules:
        if not path_allowed(pattern, path):
            continue
        # RFC 9309 §2.2.2: the longest match wins, and on a tie `Allow` beats
        # `Disallow` (order in the file must never decide). The first version of
        # the Worker's evaluator took the first longest line instead, which made
        # the *file order* decide — the same trap the stdlib parser falls into.
        if best is None or len(pattern) > len(best[1]) or (len(pattern) == len(best[1]) and rule_type == "allow"):
            best = (rule_type, pattern)
    allowed = True if best is None else best[0] == "allow"
    return allowed, group.crawl_delay


@dataclass
class RobotsDecision:
    """The client's answer for one URL."""

    allowed: bool
    crawl_delay: float | None = None
    source: str = "empty"  # robots.txt | empty | denied | fetch-error | disabled
    status: int = 0

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.allowed


class RobotsPolicy:
    """Per-host robots.txt cache with the evaluation the Worker performs.

    :param fetcher: ``fetcher(url) -> FetchResult``; the client passes its own
        ``_direct`` in, so robots.txt travels through the same politeness path as
        the documents the policy governs — a dedicated transport here would be a
        second code path with its own rate limits.
    """

    def __init__(
        self,
        fetcher: Callable[[str], Any],
        *,
        user_agent: str = "PuppetNET",
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        enabled: bool = True,
    ) -> None:
        self._fetch = fetcher
        self.user_agent = str(user_agent or "PuppetNET")
        self.ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self.enabled = bool(enabled)
        self._cache: dict[str, tuple[float, list[RobotsGroup], str]] = {}
        self._denied: set[str] = set()
        #: Counters for the run report: how many URLs each verdict refused/found.
        self.stats: dict[str, int] = {"checked": 0, "refused": 0, "fetches": 0, "errors": 0, "cached": 0, "stale_reuse": 0}

    def _robots_url(self, url: str) -> tuple[str, str]:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return f"{parsed.scheme}://{parsed.netloc}/robots.txt", host

    def _rules_for(self, url: str) -> tuple[list[RobotsGroup], str]:
        """Fetch, parse and cache one origin's ruleset."""
        robots_url, host = self._robots_url(url)
        now = self._clock()
        cached = self._cache.get(host)
        if cached is not None and now - cached[0] < self.ttl_seconds:
            self.stats["cached"] += 1
            return cached[1], cached[2]

        if host in self._denied:
            # The origin refused the robots.txt itself (401/403): RFC 9309 says
            # that means "everything disallowed" until it changes its mind, and a
            # 403 is not a transient error.
            return [], "denied"

        self.stats["fetches"] += 1
        try:
            result = self._fetch(robots_url)
        except Exception as exc:  # noqa: BLE001 - a broken fetch must not abort a harvest
            logger.warning("robots.txt fetch for %s raised (%s)", host, exc)
            return self._unreachable(host, cached)

        status = int(getattr(result, "status", 0) or 0)
        if status in (401, 403):
            logger.info("robots.txt for %s answered %d — treating the origin as fully disallowed", host, status)
            self._denied.add(host)
            return [], "denied"
        if status >= 500 or status == 0:
            # RFC 9309 §2.3.1.4: an unreachable robots.txt means "assume complete
            # disallow". Failing open here would hand anyone who can break their
            # own rules file a free crawl — so this refuses, but *never caches*
            # the refusal: the next request asks again.
            logger.info("robots.txt for %s is unavailable (%d) — not fetching this host", host, status)
            return self._unreachable(host, cached)
        if status >= 400 or not getattr(result, "ok", False):
            # 404/410 and friends mean "no restrictions" (RFC 9309 §2.3.1.3).
            self._cache[host] = (now, [], "empty")
            return [], "empty"

        text = getattr(result, "text", None) or ""
        if len(text) > MAX_ROBOTS_BYTES:
            text = text[:MAX_ROBOTS_BYTES]
        groups = parse_robots(text)
        self._cache[host] = (now, groups, "robots.txt")
        logger.debug("robots.txt for %s parsed into %d group(s)", host, len(groups))
        return groups, "robots.txt"

    def _unreachable(self, host: str, cached: tuple[float, list[RobotsGroup], str] | None) -> tuple[list[RobotsGroup], str]:
        """RFC 9309 §2.3.1.4 with the escape hatch the spec allows.

        "Unreachable" must be read as complete disallow — but a crawler that
        already holds a copy of the rules may keep using it. Inside one run that
        cached copy is what keeps a host with a flaky rules file from stalling the
        whole source for an hour; without one, the answer is a refusal.
        """
        self.stats["errors"] += 1
        if cached is not None:
            logger.info("robots.txt for %s is unavailable — reusing the copy from earlier in this run", host)
            self.stats["stale_reuse"] += 1
            return cached[1], cached[2]
        return [], "unreachable"

    # ------------------------------------------------------------------ #
    def check(self, url: str, *, respect: bool = True) -> RobotsDecision:
        """Whether the direct transport may fetch `url`."""
        if not self.enabled or not respect:
            return RobotsDecision(allowed=True, source="disabled")
        self.stats["checked"] += 1
        parsed = urlparse(url)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        groups, source = self._rules_for(url)
        if source == "denied":
            self.stats["refused"] += 1
            return RobotsDecision(allowed=False, source="denied", status=403)
        if source == "unreachable":
            self.stats["refused"] += 1
            # 502, not 403: nothing in the file refused us — the file was not
            # there to be read. The caller still must not retry (RFC 9309 treats
            # it as a disallow), but the *reason* must not read as a verdict.
            return RobotsDecision(allowed=False, source="unreachable", status=502)
        allowed, crawl_delay = verdict_for(groups, path, self.user_agent)
        if not allowed:
            self.stats["refused"] += 1
            logger.info("robots.txt disallows %s (direct transport)", url)
        return RobotsDecision(allowed=allowed, crawl_delay=crawl_delay, source=source)

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "user_agent": self.user_agent,
            "ttl_seconds": self.ttl_seconds,
            "hosts_cached": len(self._cache),
            "hosts_denied": len(self._denied),
            **self.stats,
        }
