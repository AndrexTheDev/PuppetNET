"""RSS/Atom harvester — the unstructured (weight 0.4) half of the pipeline.

Feeds are metadata-rich but content-poor: most publishers put two paragraphs in
the summary. ``fetch_full_articles`` therefore resolves each entry's link and
runs the boilerplate-stripped article text through the NLP engine instead,
while keeping the feed's GUID, publication date and author as provenance.

Dedupe keys: entry ``id``/``link`` (cross-run, via ``external_id``) plus the
content hash of the extracted text.
"""

from __future__ import annotations

import re
from datetime import timezone
from typing import Any, Iterable, Iterator

from ..logging_utils import get_logger
from ..models import Document
from ..parsing.text_extract import clean_text, extract_text, html_to_text
from .base import SourceAdapter

__all__ = ["RssAdapter"]

logger = get_logger("sources.rss")

_TAG_RE = re.compile(r"<[^>]+>")


class RssAdapter(SourceAdapter):
    """Harvest one or more RSS/Atom feeds into documents."""

    adapter_name = "rss"

    # ------------------------------------------------------------------ #
    def harvest(self) -> Iterator[Document]:
        try:
            import feedparser  # noqa: F401
        except ImportError as exc:  # pragma: no cover - declared dependency
            self.log.error("feedparser is not installed (%s) — skipping %s", exc, self.spec.id)
            return

        feeds = list(self._feed_urls())
        if not feeds:
            self.log.warning("no feed URLs configured for %s — nothing to harvest", self.spec.id)
            return

        per_feed_cap = max(1, int(self.option("per_feed_limit", max(5, self.spec.max_documents // max(1, len(feeds)) or 5))))
        fetch_full = bool(self.option("fetch_full_articles", True))
        content_cap = int(self.option("entry_content_max_chars", 20_000))
        window_days = int(self.option("recent_days", max(1, int(self.settings.dedupe_window_days or 7))))

        for feed_url in feeds:
            if self.ctx.budget_exhausted():
                return
            for document in self._harvest_feed(feed_url, per_feed_cap=per_feed_cap, fetch_full=fetch_full, content_cap=content_cap, window_days=window_days):
                yield document

    # ------------------------------------------------------------------ #
    def _feed_urls(self) -> Iterable[str]:
        urls: list[str] = []
        for candidate in list(self.option("feeds", []) or []):
            if isinstance(candidate, dict):
                url = candidate.get("url") or candidate.get("feed")
            else:
                url = candidate
            if url and url not in urls:
                urls.append(str(url))
        # The generic "news_world" spec can be fed straight from the environment.
        if not urls or self.spec.id == "news_world":
            for url in self.settings.rss_feeds or []:
                if url and url not in urls:
                    urls.append(url)
        if self.spec.base_url and self.spec.base_url not in urls and not urls:
            urls.append(self.spec.base_url)
        return urls

    def _harvest_feed(self, feed_url: str, *, per_feed_cap: int, fetch_full: bool, content_cap: int, window_days: int) -> Iterator[Document]:
        import feedparser

        self.log.info("fetching feed %s", feed_url)
        result = self.fetch(feed_url, mode="bot", accept="application/rss+xml, application/atom+xml, application/xml, text/xml, */*")
        if not result.ok or not (result.text or result.content):
            self.stats.bump_source(self.spec.id, "errors")
            self.stats.record_error(self.spec.id, f"feed fetch failed ({result.status or result.error}) {feed_url}")
            return

        raw = result.text if result.text else result.content.decode("utf-8", "replace")
        try:
            parsed = feedparser.parse(raw)
        except Exception as exc:  # noqa: BLE001 - feedparser is defensive but not immune
            self.log.warning("could not parse feed %s: %s", feed_url, exc)
            self.stats.bump_source(self.spec.id, "errors")
            return

        bozo = bool(getattr(parsed, "bozo", False))
        entries = list(getattr(parsed, "entries", []) or [])
        if not entries:
            self.log.warning("feed %s contained no entries (bozo=%s)", feed_url, bozo)
            return

        self.log.info("feed %s yielded %d entries", feed_url, len(entries))
        emitted = 0
        for entry in entries:
            if emitted >= per_feed_cap or self.ctx.budget_exhausted():
                break
            document = self._entry_to_document(entry, feed_url=feed_url, fetch_full=fetch_full, content_cap=content_cap, window_days=window_days)
            if document is None:
                continue
            emitted += 1
            yield document

    # ------------------------------------------------------------------ #
    def _entry_to_document(
        self,
        entry: Any,
        *,
        feed_url: str,
        fetch_full: bool,
        content_cap: int,
        window_days: int,
    ) -> Document | None:
        link = str(entry.get("link") or "").strip()
        guid = str(entry.get("id") or link or "").strip()
        if not link and not guid:
            return None

        published = self.parse_datetime(entry.get("published_parsed") and _struct_to_iso(entry["published_parsed"]) or entry.get("published") or entry.get("updated"))
        if published is not None and not self.within_window(published, window_days):
            self.log.debug("entry outside the %d-day window: %s", window_days, link or guid)
            return None

        title = clean_text(_strip_html(str(entry.get("title") or "")), max_chars=1024)
        summary = _strip_html(str(entry.get("summary") or entry.get("description") or ""))
        content_blocks = entry.get("content") or []
        if isinstance(content_blocks, list):
            for block in content_blocks:
                if isinstance(block, dict) and block.get("value"):
                    summary += "\n" + _strip_html(str(block["value"]))
        author = ""
        authors = entry.get("authors") or []
        if isinstance(authors, list) and authors and isinstance(authors[0], dict):
            author = str(authors[0].get("name") or "")
        if not author:
            author = str(entry.get("author") or "")
        categories = [str(tag.get("term", "")) for tag in (entry.get("tags") or []) if isinstance(tag, dict) and tag.get("term")]

        text = clean_text(f"{title}\n\n{summary}", max_chars=content_cap)
        content_type = "text/plain"
        url = link or f"{feed_url}#{guid}"
        article_meta: dict[str, Any] = {"feed_url": feed_url, "categories": categories[:12], "guid": guid}

        # Prefer the full article: feeds rarely carry enough prose for reliable
        # dependency parsing, and the CRAFT signal (tail numbers, vessel names)
        # usually lives in the body.
        if fetch_full and link:
            article = self._fetch_article(link, referer=feed_url, content_cap=content_cap)
            if article is not None:
                text = article.text or text
                title = title or article.title
                author = author or article.author
                published = published or article.published_at
                content_type = "text/html"
                article_meta["article_kind"] = article.kind
                if article.warnings:
                    article_meta["article_warnings"] = article.warnings[:5]

        if len(text.split()) < 25:
            self.log.debug("entry too short to parse (%d words): %s", len(text.split()), url)
            return None

        return self.make_document(
            url,
            title=title[:1024],
            text=text,
            content_type=content_type,
            published_at=published,
            author=str(author)[:256],
            external_id=guid or url,
            language=str(entry.get("language") or self.option("language", "en")),
            extra=article_meta,
        )

    def _fetch_article(self, url: str, *, referer: str, content_cap: int) -> Any | None:
        result = self.fetch(url, mode="browser", referer=referer, accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
        if not result.ok:
            self.log.debug("article fetch failed (%s) for %s — using feed text", result.status or result.error, url)
            return None
        ctype = (result.content_type or "").lower()
        body = result.content or (result.text or "").encode("utf-8", "replace")
        if "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
            extracted = extract_text(body, content_type=ctype, url=url, kind="pdf", max_chars=content_cap)
        elif result.text is not None and "html" not in ctype and "<" not in (result.text or "")[:200]:
            extracted = extract_text(body, content_type=ctype, url=url, kind="text", max_chars=content_cap)
        else:
            extracted = html_to_text(body, url=url, max_chars=content_cap)
        if not extracted.is_usable:
            self.log.debug("article text unusable (%s) for %s", ",".join(extracted.warnings) or "empty", url)
            return None
        return extracted


def _strip_html(fragment: str) -> str:
    """Remove markup and decode the most common entities without bs4."""
    if not fragment:
        return ""
    if "<" not in fragment and "&" not in fragment:
        return fragment
    if "<" in fragment:
        try:
            extracted = html_to_text(fragment, max_chars=50_000)
            if extracted.text:
                return extracted.text
        except Exception:  # noqa: BLE001 - fall through to the regex stripper
            pass
        fragment = _TAG_RE.sub(" ", fragment)
    replacements = {
        "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&apos;": "'",
        "&nbsp;": " ", "&ndash;": "–", "&mdash;": "—", "&hellip;": "…", "&rsquo;": "’",
    }
    for entity, char in replacements.items():
        fragment = fragment.replace(entity, char)
    numeric = re.compile(r"&#(\d+);")
    fragment = numeric.sub(lambda m: chr(int(m.group(1))), fragment)
    hexa = re.compile(r"&#x([0-9a-fA-F]+);")
    fragment = hexa.sub(lambda m: chr(int(m.group(1), 16)), fragment)
    return clean_text(fragment, max_chars=200_000)


def _struct_to_iso(value: Any) -> str | None:
    """Convert feedparser's ``time.struct_time`` to an ISO-8601 string."""
    try:
        from datetime import datetime

        moment = datetime(*value[:6], tzinfo=timezone.utc)
        return moment.isoformat()
    except (TypeError, ValueError):
        return None
