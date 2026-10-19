"""Text-extraction tests: HTML boilerplate stripping, PDF, JSON, CSV, cleaning."""

from __future__ import annotations

import io
import logging

import pytest

from puppetnet.parsing.text_extract import (
    clean_text,
    csv_to_text,
    detect_kind,
    extract_text,
    html_to_text,
    json_to_text,
    pdf_to_text,
)

logging.disable(logging.CRITICAL)


# --------------------------------------------------------------------------- #
# Fixture builders
# --------------------------------------------------------------------------- #
def build_pdf(text: str, *, pages: int = 1) -> bytes:
    """A minimal but *structurally valid* PDF (real xref table + startxref).

    pypdf rejects hand-waved PDFs, and the daily run genuinely has to parse
    ICIJ/OCCRP report PDFs, so this builds one the library accepts. Object
    numbering: 1 = catalog, 2 = pages, then (page, contents) pairs, then font.
    """
    font_id = 3 + 2 * pages
    objects: list[bytes] = [b"<</Type/Catalog/Pages 2 0 R>>"]
    page_ids = [3 + 2 * index for index in range(pages)]
    objects.append(
        b"<</Type/Pages/Kids[" + b" ".join(f"{page_id} 0 R".encode() for page_id in page_ids) + f"]/Count {pages}>>".encode()
    )
    for index, page_id in enumerate(page_ids):
        objects.append(
            f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
            f"/Resources<</Font<</F1 {font_id} 0 R>>>>/Contents {page_id + 1} 0 R>>".encode()
        )
        stream = f"BT /F1 12 Tf 72 720 Td ({text} {index + 1}) Tj ET".encode("latin-1")
        objects.append(b"<</Length " + str(len(stream)).encode() + b">>\nstream\n" + stream + b"\nendstream")
    objects.append(b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>")

    buffer = io.BytesIO()
    buffer.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(buffer.tell())
        buffer.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref_position = buffer.tell()
    buffer.write(f"xref\n0 {len(objects) + 1}\n".encode())
    buffer.write(b"0000000000 65535 f \n")
    for offset in offsets:
        buffer.write(f"{offset:010d} 00000 n \n".encode())
    buffer.write(f"trailer\n<</Size {len(objects) + 1}/Root 1 0 R>>\nstartxref\n{xref_position}\n%%EOF".encode())
    return buffer.getvalue()


ARTICLE_HTML = b"""<!DOCTYPE html>
<html lang="en">
<head>
  <title>Kerimov sanctions probe</title>
  <meta name="author" content="Jane Reporter">
  <meta property="article:published_time" content="2024-03-01T08:30:00Z">
  <script>var tracking = "should be dropped";</script>
  <style>.hidden { display: none; }</style>
</head>
<body>
  <nav><a href="/">Home</a><a href="/world">World</a></nav>
  <header>Breaking news banner</header>
  <article>
    <h1>Kerimov sanctions probe</h1>
    <p>Suleiman Kerimov owns Midea Holdings Ltd, according to filings.</p>
    <p>The superyacht <b>Amadea</b> sailed from Fiji to Istanbul.</p>
    <blockquote>"We deny everything," said a spokesman.</blockquote>
  </article>
  <aside>Related stories you may like</aside>
  <footer>Copyright 2024 Example News</footer>
</body>
</html>"""


# --------------------------------------------------------------------------- #
# detect_kind
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "content_type,url,expected",
    [
        ("application/pdf", "", "pdf"),
        ("application/pdf; charset=binary", "https://x.test/a", "pdf"),
        ("text/html", "https://x.test/a", "html"),
        ("application/xhtml+xml", "https://x.test/a", "html"),
        ("application/rss+xml", "https://x.test/feed", "xml"),
        ("application/atom+xml", "https://x.test/feed", "xml"),
        ("application/json", "https://x.test/a", "json"),
        ("application/ld+json", "https://x.test/a", "json"),
        ("text/csv", "https://x.test/a", "csv"),
        ("text/tab-separated-values", "https://x.test/a", "csv"),
        ("text/plain", "https://x.test/a", "text"),
        ("application/zip", "https://x.test/a", "archive"),
        ("application/gzip", "https://x.test/a", "archive"),
        ("image/png", "https://x.test/a", "image"),
        ("", "https://x.test/report.pdf", "pdf"),
        ("", "https://x.test/feed.xml", "xml"),
        ("", "https://x.test/data.json", "json"),
        ("", "https://x.test/data.csv", "csv"),
        ("", "https://x.test/story.html", "html"),
    ],
)
def test_detect_kind_from_headers_and_urls(content_type, url, expected):
    assert detect_kind(content_type, url) == expected


def test_detect_kind_sniffs_magic_bytes_when_the_server_is_vague():
    """ICIJ/OCCRP serve PDFs as octet-stream with no useful URL."""
    pdf = build_pdf("Kerimov owns Midea Holdings")
    assert detect_kind("application/octet-stream", "", pdf) == "pdf"
    assert detect_kind("", "", pdf) == "pdf"
    assert detect_kind("application/octet-stream", "", b"PK\x03\x04payload") == "archive"
    assert detect_kind("", "", b"\x1f\x8b\x08payload") == "archive"
    assert detect_kind("application/octet-stream", "", b'{"results": []}') == "json"
    assert detect_kind("", "", b"<html><body>x</body></html>") == "html"


def test_detect_kind_trusts_an_explicit_text_content_type():
    assert detect_kind("text/plain", "", b"<html>looks like markup</html>") == "text"


def test_detect_kind_prefers_bytes_over_a_misleading_url():
    """A .pdf URL that returns an HTML 404 page must parse as HTML."""
    assert detect_kind("", "https://x.test/missing.pdf", b"<html><body>Not found</body></html>") == "html"


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
def test_html_extraction_drops_boilerplate_and_keeps_prose():
    result = html_to_text(ARTICLE_HTML, url="https://example.test/story")
    assert result.kind == "html"
    assert result.title == "Kerimov sanctions probe"
    assert "Suleiman Kerimov owns Midea Holdings" in result.text
    assert "superyacht Amadea sailed from Fiji" in result.text
    assert "We deny everything" in result.text
    # script/style/nav/footer boilerplate must not survive
    assert "should be dropped" not in result.text
    assert "display: none" not in result.text
    assert "Related stories you may like" not in result.text
    assert "Copyright 2024" not in result.text


def test_html_extraction_reads_metadata():
    result = html_to_text(ARTICLE_HTML, url="https://example.test/story")
    assert result.author == "Jane Reporter"
    assert result.language.startswith("en")
    assert result.published_at is not None
    assert result.published_at.year == 2024


def test_html_handles_str_input_and_bad_markup():
    assert "Kerimov" in html_to_text("<p>Kerimov owns Midea</p>").text
    result = html_to_text("<p>unclosed <b>markup")
    assert "unclosed" in result.text


def test_html_empty_document_is_not_fatal():
    result = html_to_text(b"")
    assert result.text == ""
    assert result.warnings


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
def test_pdf_extraction():
    data = build_pdf("Kerimov owns Midea Holdings Ltd in Fiji")
    result = pdf_to_text(data, url="https://example.test/report.pdf")
    assert result.kind == "pdf"
    assert not result.warnings
    assert "Kerimov owns Midea Holdings" in result.text
    assert "[page 1]" in result.text  # page markers keep evidence traceable


def test_pdf_multipage_extraction_keeps_every_page():
    data = build_pdf("Sanctions finding", pages=3)
    result = pdf_to_text(data)
    for page in (1, 2, 3):
        assert f"[page {page}]" in result.text


def test_pdf_max_pages_is_respected():
    data = build_pdf("Sanctions finding", pages=4)
    result = pdf_to_text(data, max_pages=2)
    assert "[page 3]" not in result.text


def test_corrupt_pdf_degrades_with_a_warning():
    result = pdf_to_text(b"%PDF-1.4\nthis is not a real pdf\n%%EOF")
    assert result.kind == "pdf"
    assert result.warnings


def test_extract_text_dispatches_to_the_pdf_parser():
    data = build_pdf("Amadea sailed from Fiji")
    assert "Amadea sailed from Fiji" in extract_text(data, content_type="application/pdf").text


# --------------------------------------------------------------------------- #
# JSON
# --------------------------------------------------------------------------- #
def test_json_extraction_flattens_records():
    payload = b'{"results": [{"name": "Gazprom", "jurisdiction": "RU"}, {"name": "Rosneft"}]}'
    result = json_to_text(payload)
    assert result.kind == "json"
    assert "Gazprom" in result.text and "Rosneft" in result.text
    assert "jurisdiction: RU" in result.text


def test_json_extraction_accepts_str_and_lists():
    assert "Gazprom" in json_to_text('{"name": "Gazprom"}').text
    assert "Gazprom" in json_to_text(b'[{"name": "Gazprom"}]').text


def test_invalid_json_degrades_with_a_warning():
    result = json_to_text(b"{not json")
    assert result.warnings


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #
def test_csv_extraction_emits_key_value_lines():
    payload = b"name,country,identifier\nGazprom,Russia,Q7747\nRosneft,Russia,Q619058\n"
    result = csv_to_text(payload)
    assert result.kind == "csv"
    assert "name=Gazprom" in result.text
    assert "identifier=Q7747" in result.text
    assert result.text.count("\n") >= 1


def test_csv_row_cap_is_respected():
    rows = b"name\n" + b"".join(f"row{i}\n".encode() for i in range(50))
    result = csv_to_text(rows, max_rows=5)
    assert "row4" in result.text
    assert "row49" not in result.text


def test_tsv_is_accepted():
    result = csv_to_text(b"name\tcountry\nGazprom\tRussia\n")
    assert "name=Gazprom" in result.text or "Gazprom" in result.text


# --------------------------------------------------------------------------- #
# clean_text
# --------------------------------------------------------------------------- #
def test_clean_text_normalises_whitespace():
    assert clean_text("a   b\n\n\n\n\nc") == "a b\n\nc"
    assert clean_text("a\tb") == "a b"
    assert clean_text("a\r\nb\rc") == "a\nb\nc"
    assert clean_text("  padded  ") == "padded"


def test_clean_text_normalises_unicode_spaces():
    """NBSP and friends must not survive into tokenizers or content hashes."""
    assert clean_text("a\u00a0b\u202fc\u2003d\u3000e") == "a b c d e"


def test_clean_text_strips_zero_width_and_control_characters():
    assert clean_text("Ke\u200brimov\u200c") == "Kerimov"
    assert "\x00" not in clean_text("a\x00b")


def test_clean_text_truncates_to_the_cap():
    assert len(clean_text("x" * 10_000, max_chars=500)) == 500


def test_clean_text_handles_empty_input():
    assert clean_text("") == ""


def test_content_hash_is_stable_after_cleaning():
    from puppetnet.models import content_hash

    variants = ["Kerimov owns Midea", "Kerimov\u00a0owns  Midea", " Kerimov owns Midea \n"]
    hashes = {content_hash(clean_text(v)) for v in variants}
    assert len(hashes) == 1, "cosmetic differences must not defeat dedupe"


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "body,content_type,needle",
    [
        (b"<html><body><p>Kerimov owns Midea</p></body></html>", "text/html", "Kerimov owns Midea"),
        (b'{"name": "Gazprom"}', "application/json", "Gazprom"),
        (b"name\nGazprom\n", "text/csv", "Gazprom"),
        (b"plain prose about Rosneft", "text/plain", "Rosneft"),
    ],
)
def test_extract_text_dispatch(body, content_type, needle):
    assert needle in extract_text(body, content_type=content_type).text


def test_extract_text_accepts_str_input():
    assert "Rosneft" in extract_text("plain prose about Rosneft", content_type="text/plain").text


def test_extracted_text_metadata_defaults():
    result = extract_text(b"hello", content_type="text/plain")
    assert result.language == "en"
    assert result.metadata is not None
    assert isinstance(result.warnings, list)
