"""`robots.txt` evaluation: the same fixtures the Worker's smoke suite runs.

The relay (TypeScript) and the direct transport (this package) each evaluate
robots.txt on their own, because they run in different runtimes with different
caches. They must still agree: a rule that means "refused" on the edge and
"allowed" in the runner would make the answer depend on which transport happened
to serve the request. :data:`RFC_FIXTURES` below is therefore duplicated verbatim
in ``tests/worker_smoke.mjs`` (check G15b) — change one, change both.
"""

from __future__ import annotations

import time

import pytest

from puppetnet.net.proxy_client import FetchResult
from puppetnet.net.robots import (
    RobotsPolicy,
    parse_robots,
    path_allowed,
    verdict_for,
)

#: ``(rules, path, user agent, allowed)`` — RFC 9309 §2.2.2 behaviour.
RFC_FIXTURES: list[tuple[str, str, str, bool]] = [
    # Longest match wins; on equal specificity Allow beats Disallow, and the
    # order of the lines must not matter.
    ("User-agent: *\nDisallow: /page\nAllow: /page\n", "/page", "PuppetNET", True),
    ("User-agent: *\nAllow: /page\nDisallow: /page\n", "/page", "PuppetNET", True),
    # ...but a longer Disallow still wins over a shorter Allow.
    ("User-agent: *\nAllow: /private\nDisallow: /private/secret\n", "/private/secret/a", "PuppetNET", False),
    # Prefix matching: a pattern covers everything that starts with it.
    ("User-agent: *\nDisallow: /private\nAllow: /private/public\n", "/private/x", "PuppetNET", False),
    ("User-agent: *\nDisallow: /private\nAllow: /private/public\n", "/private/public/x", "PuppetNET", True),
    ("User-agent: *\nDisallow: /private\nAllow: /private/public\n", "/privateer/x", "PuppetNET", False),
    # Wildcard and end anchor.
    ("User-agent: *\nDisallow: /*.json$\n", "/data.json", "PuppetNET", False),
    ("User-agent: *\nDisallow: /*.json$\n", "/data.json?v=2", "PuppetNET", True),
    ("User-agent: *\nDisallow: /*.json$\n", "/data.jsonp", "PuppetNET", True),
    ("User-agent: *\nDisallow: /tmp/*\nAllow: /tmp/public/\n", "/tmp/public/a", "PuppetNET", True),
    # Consecutive `User-agent:` lines are one group; the first rule closes it.
    (
        "User-agent: AlphaBot\nUser-agent: BetaBot\nDisallow: /x\n\nUser-agent: *\nAllow: /\n",
        "/x",
        "BetaBot/1.0",
        False,
    ),
    (
        "User-agent: AlphaBot\nUser-agent: BetaBot\nDisallow: /x\n\nUser-agent: *\nAllow: /\n",
        "/x",
        "GammaBot/1.0",
        True,
    ),
    # A group written for another crawler never applies to ours.
    ("User-agent: Googlebot\nDisallow: /\n\nUser-agent: *\nAllow: /\n", "/page", "PuppetNET", True),
    # An empty `Disallow:` is not a rule: it matches nothing and must not outvote
    # the `Disallow: /` above it on the tie-break.
    ("User-agent: *\nDisallow: /\nDisallow:\n", "/x", "PuppetNET", False),
    # A directive line closes the agent list, so the `*` group below it is really
    # a second group — reading it as a continuation handed SlowBot's crawl-delay
    # (and its rules) to every other bot on the host.
    ("User-agent: SlowBot\nCrawl-delay: 5\n\nUser-agent: *\nDisallow: /x\n", "/x", "OtherBot", False),
    ("User-agent: SlowBot\nCrawl-delay: 5\n\nUser-agent: *\nDisallow: /x\n", "/x", "SlowBot/1", True),
    # Nothing in the file mentions us ⇒ allowed.
    ("User-agent: Googlebot\nDisallow: /\n", "/x", "PuppetNET", True),
]


@pytest.mark.parametrize("rules,path,agent,allowed", RFC_FIXTURES)
def test_rfc_9309_fixtures(rules: str, path: str, agent: str, allowed: bool):
    verdict, _ = verdict_for(parse_robots(rules), path, agent)
    assert verdict is allowed, f"{rules!r} vs {path} for {agent}"


def test_the_fixture_table_covers_both_verdicts():
    """A table that only asserts "allowed" would pass against a stub."""
    assert {row[3] for row in RFC_FIXTURES} == {True, False}


def test_an_empty_disallow_does_not_lift_anything():
    """The classic `User-agent: *` / `Disallow:` idiom means "no restriction" —
    it is simply the absence of rules (see the fixture row). A stray empty
    `Disallow:` *below* a real rule is not an escape hatch."""
    empty_only = parse_robots("User-agent: *\nDisallow:\n")
    assert verdict_for(empty_only, "/anything", "PuppetNET")[0] is True

    with_rule = parse_robots("User-agent: *\nDisallow: /private/\nDisallow:\n")
    assert verdict_for(with_rule, "/private/a", "PuppetNET")[0] is False
    assert with_rule[0].rules == [("disallow", "/private/")], "the empty line adds no rule"


def test_a_directive_line_closes_the_agent_list():
    """`Crawl-delay` is a directive like any other: the next `User-agent:` line
    starts a new record instead of joining the block above it."""
    groups = parse_robots("User-agent: SlowBot\nCrawl-delay: 5\n\nUser-agent: *\nDisallow: /x\n")
    assert [(g.agents, g.rules) for g in groups] == [(["slowbot"], []), (["*"], [("disallow", "/x")])]
    assert verdict_for(groups, "/x", "OtherBot")[0] is False
    assert verdict_for(groups, "/x", "OtherBot")[1] is None, "the crawl-delay stayed with its own group"


def test_consecutive_user_agent_lines_share_one_group_until_a_rule_follows():
    groups = parse_robots("User-agent: AlphaBot\nUser-agent: BetaBot\nDisallow: /x\nUser-agent: GammaBot\n")
    assert [(g.agents, g.rules) for g in groups] == [
        (["alphabot", "betabot"], [("disallow", "/x")]),
        (["gammabot"], []),
    ]


def test_disallow_wins_when_it_is_the_longer_match():
    rules = parse_robots("User-agent: *\nDisallow: /a/b\nAllow: /a\n")
    assert verdict_for(rules, "/a/b/c", "PuppetNET")[0] is False
    assert verdict_for(rules, "/a/other", "PuppetNET")[0] is True


def test_crawl_delay_is_reported_for_the_matching_group():
    rules = parse_robots("User-agent: SlowBot\nCrawl-delay: 10\n\nUser-agent: *\nAllow: /\n")
    assert verdict_for(rules, "/x", "SlowBot/2")[1] == 10.0
    assert verdict_for(rules, "/x", "OtherBot")[1] is None


def test_a_malformed_crawl_delay_does_not_invalidate_the_file():
    rules = parse_robots("User-agent: *\nCrawl-delay: soon\nDisallow: /x\n")
    assert verdict_for(rules, "/x", "PuppetNET")[0] is False


def test_a_zero_crawl_delay_is_ignored():
    rules = parse_robots("User-agent: *\nCrawl-delay: 0\nAllow: /\n")
    assert verdict_for(rules, "/x", "PuppetNET")[1] is None


def test_an_empty_user_agent_line_matches_nothing():
    """`"" in ua` is true for every string — the guard has to be explicit."""
    rules = parse_robots("User-agent:\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
    assert verdict_for(rules, "/x", "PuppetNET")[0] is True


def test_comments_and_blank_lines_are_ignored():
    rules = parse_robots("# comment\n\nUser-agent: *   # inline\n  Disallow: /x  \n")
    assert verdict_for(rules, "/x", "PuppetNET")[0] is False
    assert verdict_for(rules, "/y", "PuppetNET")[0] is True


def test_an_unknown_directive_does_not_leak_into_the_rules():
    rules = parse_robots("User-agent: *\nSitemap: https://example.test/s.xml\nDisallow: /x\n")
    assert len(rules) == 1 and rules[0].rules == [("disallow", "/x")]


@pytest.mark.parametrize(
    "pattern,path,expected",
    [
        ("/a", "/a/b", True),
        ("/a", "/b", False),
        ("*.php", "/x/y.php", True),
        ("/*.php$", "/x/y.php", True),
        ("/*.php$", "/x/y.php?q=1", False),
        ("/caf\u00e9", "/caf\u00e9/menu", True),
        ("/a+b", "/a+b", True),
        ("/a.b", "/axb", False),
        ("/a(b)", "/a(b)", True),
    ],
)
def test_pattern_matching_edge_cases(pattern: str, path: str, expected: bool):
    assert path_allowed(pattern, path) is expected


# --------------------------------------------------------------------------- #
# Policy: caching, and what an unreachable rules file means
# --------------------------------------------------------------------------- #
class FakeFetcher:
    """``fetch(url) -> FetchResult`` with a scripted answer per call."""

    def __init__(self, *results) -> None:
        self.results = list(results)
        self.calls: list[str] = []

    def __call__(self, url: str) -> FetchResult:
        self.calls.append(url)
        if not self.results:
            return FetchResult(url=url, ok=False, status=404, transport=None)  # type: ignore[arg-type]
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def rules_result(text: str, status: int = 200) -> FetchResult:
    return FetchResult(url="https://host.test/robots.txt", status=status, ok=status < 400, text=text, transport=None)  # type: ignore[arg-type]


def test_the_same_ruleset_is_reused_within_the_run():
    fetcher = FakeFetcher(rules_result("User-agent: *\nDisallow: /x\n"))
    policy = RobotsPolicy(fetcher, user_agent="PuppetNET")
    assert policy.check("https://host.test/x").allowed is False
    assert policy.check("https://host.test/y").allowed is True
    assert len(fetcher.calls) == 1, "one rules fetch per host, not one per document"
    assert policy.stats["cached"] == 1


def test_a_404_means_no_restrictions():
    fetcher = FakeFetcher(rules_result("", status=404))
    policy = RobotsPolicy(fetcher, user_agent="PuppetNET")
    assert policy.check("https://host.test/x").allowed is True
    assert policy.check("https://host.test/y").allowed is True
    assert len(fetcher.calls) == 1, "a 4xx answer is cached as 'no rules'"


def test_a_401_or_403_denies_the_whole_host():
    fetcher = FakeFetcher(rules_result("", status=403))
    policy = RobotsPolicy(fetcher, user_agent="PuppetNET")
    decision = policy.check("https://host.test/x")
    assert decision.allowed is False and decision.source == "denied"
    assert policy.check("https://host.test/other").source == "denied"
    assert len(fetcher.calls) == 1, "a denied host is not asked again in the same run"


def test_an_unreachable_rules_file_denies_the_host_without_caching_it():
    """RFC 9309 §2.3.1.4 — and a refusal that is re-evaluated, not remembered."""
    fetcher = FakeFetcher(rules_result("", status=503), rules_result("User-agent: *\nAllow: /\n"))
    policy = RobotsPolicy(fetcher, user_agent="PuppetNET")

    first = policy.check("https://host.test/x")
    assert first.allowed is False, "an unreadable rules file is not a licence to crawl"
    assert first.source == "unreachable"
    assert first.status == 502, "the reason is 'could not read', not 'you may not'"

    second = policy.check("https://host.test/x")
    assert second.allowed is True, "the origin recovered and is served again"
    assert len(fetcher.calls) == 2, "the refusal was never frozen into the cache"


def test_a_network_error_is_treated_the_same_way():
    fetcher = FakeFetcher(ConnectionError("dns is down"))
    policy = RobotsPolicy(fetcher, user_agent="PuppetNET")
    assert policy.check("https://host.test/x").source == "unreachable"
    assert policy.stats["errors"] == 1


def test_the_copy_from_earlier_in_the_run_carries_a_host_through_an_outage():
    """The spec allows a crawler to keep using the rules it already holds."""
    rules = "User-agent: *\nDisallow: /secret\n"
    fetcher = FakeFetcher(rules_result(rules), rules_result("", status=502))
    policy = RobotsPolicy(fetcher, user_agent="PuppetNET", ttl_seconds=0.0)

    assert policy.check("https://host.test/public").allowed is True
    decision = policy.check("https://host.test/secret")
    assert decision.allowed is False and decision.source == "robots.txt"
    assert policy.stats["stale_reuse"] == 1, "the old rules decided, not a fresh fetch"


def test_robots_can_be_switched_off_for_a_host_we_are_allowed_to_call():
    fetcher = FakeFetcher(rules_result("User-agent: *\nDisallow: /\n"))
    policy = RobotsPolicy(fetcher, user_agent="PuppetNET", enabled=False)
    assert policy.check("https://host.test/x").allowed is True
    assert fetcher.calls == [], "switched off means no request at all"


def test_respect_false_per_call_skips_the_lookup():
    fetcher = FakeFetcher(rules_result("User-agent: *\nDisallow: /\n"))
    policy = RobotsPolicy(fetcher, user_agent="PuppetNET")
    assert policy.check("https://host.test/x", respect=False).allowed is True
    assert fetcher.calls == []


def test_the_ttl_expiry_triggers_a_refetch():
    clock = {"now": 100.0}
    fetcher = FakeFetcher(rules_result("User-agent: *\nAllow: /\n"), rules_result("User-agent: *\nDisallow: /\n"))
    policy = RobotsPolicy(fetcher, user_agent="PuppetNET", ttl_seconds=60.0, clock=lambda: clock["now"])
    assert policy.check("https://host.test/x").allowed is True
    clock["now"] += 61.0
    assert policy.check("https://host.test/x").allowed is False, "the new rules take effect"
    assert len(fetcher.calls) == 2


def test_the_policy_url_is_per_origin():
    fetcher = FakeFetcher(rules_result("User-agent: *\nAllow: /\n"), rules_result("User-agent: *\nAllow: /\n"))
    policy = RobotsPolicy(fetcher, user_agent="PuppetNET")
    policy.check("https://a.test/x?q=1")
    policy.check("https://b.test/x")
    assert fetcher.calls == ["https://a.test/robots.txt", "https://b.test/robots.txt"]


def test_the_query_string_takes_part_in_matching():
    fetcher = FakeFetcher(rules_result("User-agent: *\nDisallow: /search?\n"))
    policy = RobotsPolicy(fetcher, user_agent="PuppetNET")
    assert policy.check("https://host.test/search?q=x").allowed is False
    assert policy.check("https://host.test/other").allowed is True


def test_describe_is_serialisable_and_counts_the_work():
    fetcher = FakeFetcher(rules_result("User-agent: *\nDisallow: /x\n"))
    policy = RobotsPolicy(fetcher, user_agent="PuppetNET")
    policy.check("https://host.test/x")
    payload = policy.describe()
    assert payload["refused"] == 1 and payload["checked"] == 1 and payload["hosts_cached"] == 1
    assert payload["enabled"] is True and payload["ttl_seconds"] > 0


def test_the_user_agent_selects_the_group():
    fetcher = FakeFetcher(rules_result("User-agent: PuppetNET\nDisallow: /\n\nUser-agent: *\nAllow: /\n"))
    policy = RobotsPolicy(fetcher, user_agent="PuppetNET-OSINT/1.5")
    assert policy.check("https://host.test/x").allowed is False

    polite = FakeFetcher(rules_result("User-agent: PuppetNET\nDisallow: /\n\nUser-agent: *\nAllow: /\n"))
    other = RobotsPolicy(polite, user_agent="SomeoneElse")
    assert other.check("https://host.test/x").allowed is True


def test_the_cache_is_bounded_by_hosts_not_by_requests():
    fetcher = FakeFetcher(*[rules_result("User-agent: *\nAllow: /\n") for _ in range(50)])
    policy = RobotsPolicy(fetcher, user_agent="PuppetNET")
    for index in range(50):
        policy.check(f"https://host{index}.test/x")
    assert len(policy._cache) == 50 and len(fetcher.calls) == 50
    for index in range(50):
        policy.check(f"https://host{index}.test/y")
    assert len(fetcher.calls) == 50, "the second pass over the same hosts costs nothing"


def test_a_timeout_of_the_rules_fetch_does_not_hang_the_run():
    """The policy must not turn an unresponsive origin into an unbounded wait."""

    class Slow:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, url: str):
            self.calls += 1
            return rules_result("", status=504)

    fetcher = Slow()
    policy = RobotsPolicy(fetcher, user_agent="PuppetNET", clock=time.monotonic)
    started = time.monotonic()
    assert policy.check("https://slow.test/x").allowed is False
    assert time.monotonic() - started < 1.0
    assert fetcher.calls == 1
