"""Header / fingerprint generation for the direct (non-Worker) fetch path.

The Cloudflare Worker rotates fingerprints at the edge; when the harvester has
to talk to an origin directly it still needs to look like a well-behaved,
*consistent* client. Fully random headers per request are worse than a stable
identity (they trip "impossible browser" heuristics), so each host is pinned to
one identity per run and rotated only across attempts after a failure.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse

__all__ = ["Fingerprint", "HeaderFactory", "FINGERPRINTS", "BOT_USER_AGENTS"]

#: Coherent browser identities (UA ↔ client hints ↔ language match).
FINGERPRINTS: tuple[dict[str, Any], ...] = (
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Chromium";v="131", "Google Chrome";v="131", "Not_A Brand";v="24"',
        "sec_ch_ua_platform": '"Windows"',
        "accept_language": "en-US,en;q=0.9",
    },
    {
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
        "sec_ch_ua_platform": '"macOS"',
        "accept_language": "en-US,en;q=0.9",
    },
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
        "sec_ch_ua": None,
        "sec_ch_ua_platform": None,
        "accept_language": "en-US,en;q=0.5",
    },
    {
        "ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Chromium";v="131", "Google Chrome";v="131", "Not_A Brand";v="24"',
        "sec_ch_ua_platform": '"Linux"',
        "accept_language": "en-GB,en;q=0.9",
    },
    {
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
        "sec_ch_ua": None,
        "sec_ch_ua_platform": None,
        "accept_language": "en-GB,en;q=0.9",
    },
)

#: Honest bots. Structured APIs (Wikidata, Companies House, OpenCorporates)
#: ask for a descriptive UA with contact details, and lying to them is both
#: pointless and a terms-of-service violation.
BOT_USER_AGENTS: tuple[str, ...] = (
    "PuppetNET-OSINT/1.4 (+https://github.com/AndrexTheDev/PuppetNET; research bot)",
    "PuppetNET-Research/1.4 (contact: ops@puppetnet.example.org)",
)

_UNSAFE_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "transfer-encoding",
        "cookie2",
        "keep-alive",
        "proxy-authorization",
        "te",
        "upgrade",
        "expect",
    }
)


@dataclass(frozen=True)
class Fingerprint:
    """A pinned client identity for one host during one run."""

    user_agent: str
    accept: str
    accept_language: str
    sec_ch_ua: str | None = None
    sec_ch_ua_platform: str | None = None
    salt: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "user_agent": self.user_agent,
            "accept": self.accept,
            "accept_language": self.accept_language,
            "sec_ch_ua": self.sec_ch_ua or "",
            "sec_ch_ua_platform": self.sec_ch_ua_platform or "",
            "salt": self.salt,
        }


def _stable_index(seed: str, modulus: int) -> int:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % max(1, modulus)


class HeaderFactory:
    """Build request headers with per-host identity pinning.

    ``mode="browser"`` rotates realistic browser fingerprints (used for
    unstructured news/HTML scraping). ``mode="bot"`` presents an honest
    research-bot UA with contact details (used for structured APIs that publish
    a UA policy — Wikidata in particular rejects anonymous clients).
    """

    def __init__(
        self,
        *,
        bot_user_agent: str = BOT_USER_AGENTS[0],
        run_salt: str = "",
        extra_headers: Mapping[str, str] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.bot_user_agent = bot_user_agent
        self.run_salt = run_salt or hashlib.sha1(str(random.random()).encode()).hexdigest()[:12]
        self.extra_headers = dict(extra_headers or {})
        self._rng = rng or random.Random(0xC0FFEE)
        self._pinned: dict[str, Fingerprint] = {}

    # ------------------------------------------------------------------ #
    def fingerprint_for(self, host: str, attempt: int = 0) -> Fingerprint:
        """Return the pinned fingerprint for ``host``, rotating on ``attempt``."""
        host = (host or "").lower()
        if attempt == 0 and host in self._pinned:
            return self._pinned[host]
        # Rotate deterministically per attempt: a retry must not present the
        # identical User-Agent/sec-CH-UA tuple the origin just rate-limited.
        base_index = _stable_index(f"{self.run_salt}|{host}", len(FINGERPRINTS))
        index = (base_index + int(attempt)) % len(FINGERPRINTS)
        profile = FINGERPRINTS[index]
        fingerprint = Fingerprint(
            user_agent=str(profile["ua"]),
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
            accept_language=str(profile["accept_language"]),
            sec_ch_ua=profile.get("sec_ch_ua"),
            sec_ch_ua_platform=profile.get("sec_ch_ua_platform"),
            salt=f"{self.run_salt}:{attempt}",
        )
        if attempt == 0:
            self._pinned[host] = fingerprint
        return fingerprint

    def build(
        self,
        url: str,
        *,
        mode: str = "browser",
        attempt: int = 0,
        referer: str | None = None,
        accept: str | None = None,
        extra: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """Assemble the final header mapping for one request."""
        host = urlparse(url).hostname or ""
        headers: dict[str, str] = {}

        if mode == "bot":
            headers["User-Agent"] = self.bot_user_agent
            headers["Accept"] = accept or "application/json, application/xml;q=0.9, text/plain;q=0.8, */*;q=0.5"
            headers["Accept-Language"] = "en-US,en;q=0.8"
            headers["Accept-Encoding"] = "gzip, deflate"
        else:
            fingerprint = self.fingerprint_for(host, attempt=attempt)
            headers["User-Agent"] = fingerprint.user_agent
            headers["Accept"] = accept or fingerprint.accept
            headers["Accept-Language"] = fingerprint.accept_language
            headers["Accept-Encoding"] = "gzip, deflate, br"
            headers["Upgrade-Insecure-Requests"] = "1"
            headers["Sec-Fetch-Dest"] = "document"
            headers["Sec-Fetch-Mode"] = "navigate"
            headers["Sec-Fetch-Site"] = "cross-site" if referer else "none"
            headers["Sec-Fetch-User"] = "?1"
            if fingerprint.sec_ch_ua:
                headers["sec-ch-ua"] = fingerprint.sec_ch_ua
                headers["sec-ch-ua-mobile"] = "?0"
                headers["sec-ch-ua-platform"] = fingerprint.sec_ch_ua_platform or '"Unknown"'
            headers["DNT"] = "1"

        headers["Connection"] = "keep-alive"
        headers["Cache-Control"] = "no-cache"
        headers["Pragma"] = "no-cache"
        if referer:
            headers["Referer"] = referer

        for mapping in (self.extra_headers, extra or {}):
            for key, value in mapping.items():
                if value is None:
                    headers.pop(key, None)
                else:
                    headers[key] = str(value)

        return {k: v for k, v in headers.items() if k.lower() not in _UNSAFE_HEADERS}

    # ------------------------------------------------------------------ #
    def worker_payload_headers(self, url: str, *, mode: str = "browser", attempt: int = 0, **kwargs: Any) -> dict[str, str]:
        """Headers to forward to the relay (the relay adds its own fingerprint)."""
        if mode == "bot":
            return {"User-Agent": self.bot_user_agent, **{k: v for k, v in kwargs.get("extra", {}).items()}}
        host = urlparse(url).hostname or ""
        fingerprint = self.fingerprint_for(host, attempt=attempt)
        payload: dict[str, str] = {"User-Agent": fingerprint.user_agent}
        payload.update({k: v for k, v in (kwargs.get("extra") or {}).items()})
        return payload

    def describe(self) -> dict[str, Any]:
        return {
            # The salt seeds fingerprint selection; log a digest, not the value.
            "run_salt": hashlib.sha256((self.run_salt or "").encode("utf-8")).hexdigest()[:12] if self.run_salt else "",
            "bot_user_agent": self.bot_user_agent,
            "pinned_hosts": len(self._pinned),
            "extra_header_keys": sorted(self.extra_headers),
        }


def parse_extra_headers(raw: str) -> dict[str, str]:
    """Parse ``EXTRA_HEADERS_JSON`` (object or host→object mapping)."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    flat: dict[str, str] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    flat[f"{key}:{sub_key}"] = str(sub_value)
            else:
                flat[str(key)] = str(value)
    return flat
