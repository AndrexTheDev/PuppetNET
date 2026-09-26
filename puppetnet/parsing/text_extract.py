"""Raw payload → analysable text.

Handles the four shapes the harvester actually meets:

* HTML news pages and blog posts (boilerplate-stripped main content extraction)
* PDF reports (ICIJ/OCCRP/NGO investigations, sanction notices)
* RSS/Atom feeds (delegated to :mod:`puppetnet.sources.rss`, but the entry
  HTML inside ``content:encoded`` is cleaned here)
* plain text / JSON / CSV passthrough

Everything is normalised: unicode NFC, tabs/nbsp collapsed, repeated blank
lines squeezed, and zero-width/junk characters dropped so the spaCy tokenizer
and the dedupe hash both behave.
"""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..logging_utils import get_logger

__all__ = ["ExtractedText", "extract_text", "clean_text", "html_to_text", "pdf_to_text", "detect_kind"]

logger = get_logger("parsing.extract")

_ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff\u00ad]")
_MULTI_BLANK = re.compile(r"\n{3,}")
_MULTI_SPACE = re.compile(r"[ \t\u00a0]{2,}")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

#: Tags that never carry analysable prose.
_STRIP_TAGS = (
    "script", "style", "noscript", "template", "svg", "canvas", "iframe",
    "form", "button", "select", "option", "nav", "footer", "header",
    "aside", "figure", "figcaption", "picture", "source", "video", "audio",
    "advert", "advertisement",
)

#: Containers, in preference order, that usually hold the article body.
_MAIN_SELECTORS = (
    "article",
    "main",
    "[role=main]",
    "div.article-body",
    "div.story-body",
    "div.post-content",
    "div.entry-content",
    "div#content",
    "div.content",
    "section.content",
)

_HTML_SUFFIXES = (".html", ".htm", ".xhtml", ".shtml", ".asp", ".aspx", ".php")


@dataclass
class ExtractedText:
    """Result of a text-extraction pass."""

    text: str
    title: str = ""
    author: str = ""
    language: str = "en"
    published_at: datetime | None = None
    kind: str = "text"
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return len(self.text)

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    @property
    def is_usable(self) -> bool:
        return self.word_count >= 12


def detect_kind(content_type: str, url: str, body: bytes | None = None) -> str:
    """Best-effort payload classification."""
    ctype = (content_type or "").lower().split(";")[0].strip()
    path = (url or "").split("?")[0].lower()

    if ctype:
        if ctype == "application/pdf" or path.endswith(".pdf"):
            return "pdf"
        if "html" in ctype:
            return "html"
        if "xml" in ctype or "rss" in ctype or "atom" in ctype:
            return "xml"
        if "json" in ctype or "ld+json" in ctype:
            return "json"
        if "csv" in ctype or "tsv" in ctype or "tab-separated" in ctype or "comma-separated" in ctype:
            return "csv"
        if ctype.startswith("text/"):
            return "text"
        if ctype.startswith(("application/zip", "application/gzip", "application/x-tar")):
            return "archive"
        if ctype.startswith("image/"):
            return "image"

    # Content bytes are the strongest remaining signal. Registries and leak
    # archives routinely serve PDFs as application/octet-stream with no useful
    # URL, and treating those as HTML produces empty documents.
    if body:
        head = body[:1024]
        stripped = head.lstrip()
        if stripped.startswith(b"%PDF-"):
            return "pdf"
        if stripped.startswith((b"PK\x03\x04",)):
            return "archive"
        if stripped.startswith(b"\x1f\x8b"):
            return "archive"
        if not ctype or ctype in {"application/octet-stream", "binary/octet-stream", "application/download"}:
            if stripped.startswith(b"<"):
                return "html"
            if stripped.startswith((b"{", b"[")):
                return "json"
            if stripped.startswith(b"<?xml"):
                return "xml"

    if path.endswith(".pdf"):
        return "pdf"
    if path.endswith(_HTML_SUFFIXES) or path.endswith("/") or path == "":
        return "html"
    if path.endswith((".xml", ".rss", ".atom")):
        return "xml"
    if path.endswith(".json"):
        return "json"
    if path.endswith((".csv", ".tsv")):
        return "csv"
    return "text"


#: Non-ASCII spaces (NBSP, figure/punctuation spaces, line/paragraph separators).
_UNICODE_SPACES = re.compile("[\u00a0\u1680\u2000-\u200a\u2007\u202f\u205f\u2028\u2029\u3000]")


def clean_text(text: str, *, max_chars: int = 400_000) -> str:
    """Normalise whitespace/unicode so hashes and tokenizers are stable."""
    if not text:
        return ""
    import unicodedata

    text = unicodedata.normalize("NFC", text)
    text = _ZERO_WIDTH.sub("", text)
    text = _CONTROL.sub(" ", text)
    # Every Unicode space flavour becomes an ASCII space: regexes, tokenizers
    # and content hashes all have to agree on what a word boundary is.
    text = _UNICODE_SPACES.sub(" ", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    text = _MULTI_SPACE.sub(" ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = _MULTI_BLANK.sub("\n\n", text)
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars]
        logger.debug("truncated text to %d characters", max_chars)
    return text


def _parse_meta_date(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    from dateutil import parser as date_parser  # local import: optional dep

    try:
        parsed = date_parser.parse(value, fuzzy=True)
    except (ValueError, OverflowError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed.year < 1900 or parsed > datetime.now(timezone.utc).replace(year=datetime.now(timezone.utc).year + 1):
        return None
    return parsed.astimezone(timezone.utc)


def html_to_text(html: str | bytes, *, url: str = "", max_chars: int = 400_000) -> ExtractedText:
    """Boilerplate-stripped HTML → prose, plus title/author/date/lang metadata."""
    try:
        from bs4 import BeautifulSoup, Comment
    except ImportError:  # pragma: no cover - dependency is declared in requirements
        logger.error("beautifulsoup4 is not installed; falling back to regex stripping")
        stripped = re.sub(r"<[^>]+>", " ", html.decode("utf-8", "replace") if isinstance(html, bytes) else html)
        return ExtractedText(text=clean_text(stripped, max_chars=max_chars), kind="html", warnings=["bs4-missing"])

    warnings: list[str] = []
    markup = html.decode("utf-8", "replace") if isinstance(html, bytes) else html
    soup = BeautifulSoup(markup, "lxml" if _lxml_available() else "html.parser")

    language = ""
    html_tag = soup.find("html")
    if html_tag and html_tag.get("lang"):
        language = str(html_tag["lang"])[:8]

    title = ""
    for candidate in (
        soup.find("meta", attrs={"property": "og:title"}),
        soup.find("meta", attrs={"name": "twitter:title"}),
        soup.find("title"),
        soup.find("h1"),
    ):
        if candidate:
            value = candidate.get("content") if candidate.name == "meta" else candidate.get_text(" ", strip=True)
            if value and str(value).strip():
                title = re.sub(r"\s+", " ", str(value)).strip()[:512]
                break

    author = ""
    for meta_name in ("author", "article:author", "og:article:author", "parsely-author", "dc.creator"):
        tag = soup.find("meta", attrs={"name": meta_name}) or soup.find("meta", attrs={"property": meta_name})
        if tag and tag.get("content"):
            author = str(tag["content"]).strip()[:256]
            break
    if not author:
        for selector in ("[rel=author]", ".author", ".byline", "span.author-name"):
            node = soup.select_one(selector)
            if node:
                candidate = node.get_text(" ", strip=True)
                if candidate and len(candidate) < 120:
                    author = re.sub(r"^(by|written by)\s*", "", candidate, flags=re.I).strip()
                    break

    published_at = None
    for meta_name in ("article:published_time", "og:article:published_time", "datePublished", "pubdate", "publish-date", "date", "sailthru.date"):
        tag = soup.find("meta", attrs={"property": meta_name}) or soup.find("meta", attrs={"name": meta_name})
        if tag and tag.get("content"):
            published_at = _parse_meta_date(str(tag["content"]))
            if published_at:
                break
    if published_at is None:
        time_tag = soup.find("time")
        if time_tag:
            published_at = _parse_meta_date(str(time_tag.get("datetime") or time_tag.get_text(strip=True)))

    for tag_name in _STRIP_TAGS:
        for node in soup.find_all(tag_name):
            node.decompose()
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    for node in soup.find_all(attrs={"aria-hidden": "true"}):
        node.decompose()
    for node in soup.find_all(class_=re.compile(r"(newsletter|subscribe|promo|advert|sidebar|share|social|related|comment|cookie|paywall|breadcrumb|menu|nav|footer|header|modal|popup)", re.I)):
        node.decompose()
    for node in soup.find_all(id=re.compile(r"(newsletter|subscribe|promo|advert|sidebar|share|social|related|comment|cookie|paywall|breadcrumb|menu|nav|footer|header|modal|popup)", re.I)):
        node.decompose()

    root = None
    for selector in _MAIN_SELECTORS:
        try:
            root = soup.select_one(selector)
        except Exception:  # pragma: no cover - malformed selector guard
            root = None
        if root:
            break
    if root is None:
        root = soup.body or soup

    blocks: list[str] = []
    for element in root.find_all(["h1", "h2", "h3", "h4", "h5", "p", "li", "blockquote", "td", "pre", "article", "section", "div"]):
        if element.name == "div" and element.find(["p", "article", "section"]):
            continue  # avoid duplicating nested content
        text = element.get_text(" ", strip=True)
        if not text:
            continue
        if element.name in {"h1", "h2", "h3", "h4", "h5"}:
            blocks.append(f"\n{text}\n")
        elif element.name == "li":
            blocks.append(f"- {text}")
        else:
            blocks.append(text)

    body = "\n".join(blocks)
    if len(body.split()) < 40:
        # Container heuristics failed — fall back to the whole document text.
        fallback = (soup.body or soup).get_text("\n", strip=True)
        if len(fallback.split()) > len(body.split()):
            body = fallback
            warnings.append("main-content-heuristic-missed")

    # Drop navigation-ish leftovers: very short lines in long runs.
    lines = [line.strip() for line in body.split("\n")]
    kept: list[str] = []
    short_run = 0
    for line in lines:
        if not line:
            kept.append("")
            continue
        if len(line) < 25 and not line.endswith((".", "!", "?", ":", ";")):
            short_run += 1
            if short_run > 3:
                continue
        else:
            short_run = 0
        kept.append(line)

    text = clean_text("\n".join(kept), max_chars=max_chars)
    if not text:
        warnings.append("empty-after-extraction")

    return ExtractedText(
        text=text,
        title=title,
        author=author,
        language=language or "en",
        published_at=published_at,
        kind="html",
        metadata={"url": url, "selectors_tried": len(_MAIN_SELECTORS)},
        warnings=warnings,
    )


def _lxml_available() -> bool:
    try:
        import lxml  # noqa: F401

        return True
    except ImportError:
        return False


def pdf_to_text(data: bytes, *, url: str = "", max_pages: int = 200, max_chars: int = 400_000) -> ExtractedText:
    """Extract text from a PDF, page-limited and de-hyphenated."""
    warnings: list[str] = []
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except ImportError:
            logger.error("pypdf is not installed; cannot parse PDF %s", url)
            return ExtractedText(text="", kind="pdf", warnings=["pypdf-missing"], metadata={"url": url})

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        logger.warning("unreadable PDF %s: %s", url, exc)
        return ExtractedText(text="", kind="pdf", warnings=[f"unreadable: {exc}"], metadata={"url": url})

    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception:
            warnings.append("encrypted")

    title = ""
    author = ""
    try:
        info = reader.metadata or {}
        title = str(info.get("/Title") or "").strip()[:512]
        author = str(info.get("/Author") or "").strip()[:256]
    except Exception:  # pragma: no cover - malformed metadata
        pass

    pages: list[str] = []
    page_count = 0
    try:
        page_count = len(reader.pages)
    except Exception:  # pragma: no cover
        page_count = 0

    for index, page in enumerate(reader.pages):
        if index >= max_pages:
            warnings.append(f"page-limit-{max_pages}")
            break
        try:
            chunk = page.extract_text() or ""
        except Exception as exc:
            warnings.append(f"page-{index + 1}-error")
            logger.debug("PDF page %d extraction failed: %s", index + 1, exc)
            continue
        if chunk.strip():
            pages.append(f"\n[page {index + 1}]\n{chunk}")

    text = clean_text("\n".join(pages), max_chars=max_chars)
    # Repair the hyphenated line breaks PDF layout produces.
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    if not text:
        warnings.append("no-extractable-text")
    return ExtractedText(
        text=text,
        title=title,
        author=author,
        language="en",
        kind="pdf",
        metadata={"url": url, "pages": page_count, "pages_read": min(page_count, max_pages)},
        warnings=warnings,
    )


def json_to_text(data: bytes | str, *, url: str = "", max_chars: int = 400_000) -> ExtractedText:
    """Flatten a JSON document into readable ``path: value`` lines.

    Structured adapters parse JSON themselves; this path exists for ad-hoc
    endpoints whose records still contain prose worth NLP-ing.
    """
    try:
        payload = json.loads(data)
    except (ValueError, TypeError) as exc:
        return ExtractedText(text="", kind="json", warnings=[f"invalid-json: {exc}"], metadata={"url": url})

    lines: list[str] = []

    def walk(node: Any, path: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for index, value in enumerate(node[:200]):
                walk(value, f"{path}[{index}]")
        elif node is None:
            return
        else:
            text = str(node).strip()
            if text:
                lines.append(f"{path}: {text}")

    walk(payload)
    return ExtractedText(text=clean_text("\n".join(lines), max_chars=max_chars), kind="json", metadata={"url": url})


def csv_to_text(data: bytes | str, *, url: str = "", max_rows: int = 500, max_chars: int = 400_000) -> ExtractedText:
    """Render CSV/TSV rows as ``col=value`` sentences (readable by the NLP pass)."""
    import csv as csv_module

    if isinstance(data, bytes):
        text = data.decode("utf-8", "replace")
    else:
        text = data
    sample = text[:4096]
    try:
        dialect = csv_module.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv_module.Error:
        dialect = csv_module.excel
    reader = csv_module.reader(io.StringIO(text), dialect)
    rows: list[str] = []
    header: list[str] = []
    for index, row in enumerate(reader):
        if index == 0:
            header = [cell.strip() for cell in row]
            continue
        if index > max_rows:
            break
        pairs = []
        for column, value in zip(header or [f"col{i}" for i in range(len(row))], row):
            value = (value or "").strip()
            if value:
                pairs.append(f"{column}={value}")
        if pairs:
            rows.append("; ".join(pairs))
    return ExtractedText(text=clean_text("\n".join(rows), max_chars=max_chars), kind="csv", metadata={"url": url, "rows": len(rows)})


def extract_text(
    body: bytes | str,
    *,
    content_type: str = "",
    url: str = "",
    kind: str | None = None,
    max_chars: int = 400_000,
) -> ExtractedText:
    """Dispatch on payload type and return normalised text + metadata."""
    resolved = kind or detect_kind(content_type, url, body if isinstance(body, bytes) else body.encode("utf-8", "replace")[:1024])
    raw = body if isinstance(body, bytes) else body.encode("utf-8", "replace")

    if resolved == "pdf":
        return pdf_to_text(raw, url=url, max_chars=max_chars)
    if resolved == "html":
        return html_to_text(raw, url=url, max_chars=max_chars)
    if resolved == "json":
        return json_to_text(raw, url=url, max_chars=max_chars)
    if resolved == "csv":
        return csv_to_text(raw, url=url, max_chars=max_chars)
    if resolved == "xml":
        # Feed XML is handled by the RSS adapter; strip tags for anything else.
        stripped = re.sub(r"<[^>]+>", " ", raw.decode("utf-8", "replace"))
        return ExtractedText(text=clean_text(stripped, max_chars=max_chars), kind="xml", metadata={"url": url})
    if resolved in {"image", "archive"}:
        return ExtractedText(text="", kind=resolved, warnings=[f"unsupported-kind:{resolved}"], metadata={"url": url})

    text = raw.decode("utf-8", "replace") if isinstance(body, bytes) else body
    return ExtractedText(text=clean_text(text, max_chars=max_chars), kind="text", metadata={"url": url})
