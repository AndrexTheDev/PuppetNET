"""Shared archive & delimited-file streaming for bulk OSINT dumps.

ICIJ Offshore Leaks and the FAA aircraft registry are both published as ZIP
archives of comma/tab-delimited text, hundreds of megabytes unpacked. Neither
can be buffered in memory on a GitHub Actions runner, and neither can travel
through the edge relay (the Worker buffers a response before returning it), so
both are streamed directly — still policed by the source's token bucket — and
parsed line by line.

This module owns that machinery so an adapter is only the mapping code:

* :func:`stream_archive_members` — ZIP / GZ / plain text → ``(member, lines)``
* :func:`iter_delimited_rows` — a header-aware CSV/TSV row iterator that
  canonicalises column names, so ``"N_Number"``, ``"N NUMBER"`` and
  ``"n-number"`` all resolve to ``n_number``
* :func:`first_present` — read the first non-empty value across aliases

Column *aliases* stay in each adapter: what a column means is domain knowledge,
how to stream and split it is not.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import tempfile
import zipfile
from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from ..logging_utils import get_logger

__all__ = [
    "canonical_column",
    "first_present",
    "gunzip_lines",
    "iter_delimited_rows",
    "read_local_text_lines",
    "split_delimited",
    "stream_archive_members",
]

logger = get_logger("sources.archive")

_WHITESPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def canonical_column(name: str) -> str:
    """``"Registrant Business Name"`` → ``registrant_business_name``."""
    folded = _WHITESPACE_RE.sub(" ", str(name or "")).strip().lower()
    return _NON_ALNUM_RE.sub("_", folded).strip("_")


def split_delimited(line: str, delimiter: str) -> list[str]:
    """Split one delimited line, tolerating quoted cells with embedded delimiters."""
    import csv

    try:
        return next(csv.reader([line], delimiter=delimiter, quotechar='"', skipinitialspace=True))
    except (StopIteration, csv.Error):
        return [cell.strip() for cell in line.split(delimiter)]


def first_present(row: dict[str, str], keys: Iterable[str]) -> str:
    """First non-empty value among ``keys`` (already canonicalised)."""
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def gunzip_lines(chunks: Iterator[bytes]) -> Iterator[str]:
    """Incremental gzip → text lines, degrading to raw UTF-8 on a bad stream."""
    try:
        import zlib

        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    except Exception:  # pragma: no cover - zlib is stdlib and always present
        decompressor = None
    if decompressor is None:  # pragma: no cover
        for chunk in chunks:
            yield chunk.decode("utf-8", "replace")
        return

    buffer = b""
    for chunk in chunks:
        try:
            buffer += decompressor.decompress(chunk)
        except Exception:  # noqa: BLE001 - malformed gzip: keep what decoded
            break
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            yield line.decode("utf-8", "replace")
    with contextlib.suppress(Exception):
        buffer += decompressor.flush()
    if buffer:
        yield buffer.decode("utf-8", "replace")


def stream_archive_members(
    client: Any,
    url: str,
    *,
    spec: Any,
    max_bytes: int,
    member_suffixes: Sequence[str] = (".tsv", ".csv", ".txt"),
    member_filter: Any = None,
) -> Iterator[tuple[str, Iterator[str]]]:
    """Yield ``(member_name, line_iterator)`` for a ZIP / GZ / plain-text URL.

    ``url`` may also be a local path, which is how an operator points the
    pipeline at an already-downloaded dump without touching the network.
    """
    if _is_local_path(url):
        yield from read_local_text_lines(url, max_bytes=max_bytes, member_suffixes=member_suffixes, member_filter=member_filter)
        return

    lower = url.lower().split("?")[0]
    if lower.endswith(".zip"):
        yield from _stream_zip(client, url, spec=spec, max_bytes=max_bytes, member_suffixes=member_suffixes, member_filter=member_filter)
        return

    lines = client.stream_lines(url, source=spec, source_id=getattr(spec, "id", ""), mode="bot", max_bytes=max_bytes)
    if lower.endswith((".gz", ".gzip")):
        yield (os.path.basename(lower).rsplit(".", 1)[0] or "member", gunzip_lines(_lines_to_bytes(lines)))
    else:
        yield (os.path.basename(lower) or "member", lines)


def read_local_text_lines(
    path: str,
    *,
    max_bytes: int = 512 * 1024 * 1024,
    member_suffixes: Sequence[str] = (".tsv", ".csv", ".txt"),
    member_filter: Any = None,
) -> Iterator[tuple[str, Iterator[str]]]:
    """Read a local dump (``.zip``, ``.gz`` or a delimited file)."""
    expanded = os.path.expanduser(str(path))
    if not os.path.exists(expanded):
        logger.warning("local dump %s does not exist", expanded)
        return
    lower = expanded.lower()
    if lower.endswith(".zip"):
        tmp = None
        try:
            with zipfile.ZipFile(expanded) as archive:
                for info in archive.infolist():
                    if info.is_dir() or not _wanted_member(info.filename, member_suffixes, member_filter):
                        continue
                    with archive.open(info) as member:
                        yield (info.filename, io.TextIOWrapper(member, encoding="utf-8", errors="replace", newline=""))
        except (zipfile.BadZipFile, OSError, ValueError) as exc:
            logger.warning("could not read local ZIP %s: %s", expanded, exc)
        finally:
            if tmp:  # pragma: no cover - only used if a temp copy was made
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
        return
    if lower.endswith((".gz", ".gzip")):
        import gzip

        with gzip.open(expanded, "rt", encoding="utf-8", errors="replace") as handle:
            yield (os.path.basename(expanded).rsplit(".", 1)[0], _bounded(handle, max_bytes))
        return
    with open(expanded, encoding="utf-8", errors="replace", newline="") as handle:
        yield (os.path.basename(expanded), _bounded(handle, max_bytes))


def iter_delimited_rows(
    lines: Iterator[str],
    *,
    aliases: dict[str, Sequence[str]] | None = None,
    delimiter: str | None = None,
) -> Iterator[dict[str, str]]:
    """Parse a CSV/TSV stream into dicts of canonical column name → value.

    The delimiter is sniffed from the header line unless the caller pins it.
    When ``aliases`` is given, each logical field is resolved to the first matching canonical column, so a
    caller can read ``row["tail"]`` regardless of whether the dump called it
    ``N_NUMBER``, ``n-number`` or ``TAIL NUMBER``.
    """
    header: list[str] = []
    sep = delimiter
    resolved: dict[str, str] = {}

    for line in lines:
        if not line or not line.strip():
            continue
        if not header:
            if sep is None:
                sep = "\t" if line.count("\t") >= line.count(",") else ","
            header = [canonical_column(cell) for cell in split_delimited(line.rstrip("\n"), sep)]
            if aliases:
                resolved = _resolve_aliases(header, aliases)
            continue

        cells = split_delimited(line.rstrip("\n"), sep or ",")
        # A single-column table (a name-only manifest) is legitimate; a
        # multi-column table that splits into one cell is a malformed row.
        if not cells or (len(cells) < 2 and len(header) > 1):
            continue
        row = {name: (cells[index].strip() if index < len(cells) else "") for index, name in enumerate(header) if name}
        if aliases:
            for logical, column in resolved.items():
                row.setdefault(logical, row.get(column, ""))
        yield row


def _resolve_aliases(header: Sequence[str], aliases: dict[str, Sequence[str]]) -> dict[str, str]:
    present = set(header)
    resolved: dict[str, str] = {}
    for logical, candidates in aliases.items():
        for candidate in candidates:
            canonical = canonical_column(candidate)
            if canonical in present:
                resolved[logical] = canonical
                break
    return resolved


def _wanted_member(filename: str, suffixes: Sequence[str], member_filter: Any) -> bool:
    lower = filename.lower()
    if not lower.endswith(tuple(suffixes)):
        return False
    if member_filter is None:
        return True
    return bool(member_filter(filename))


def _stream_zip(
    client: Any,
    url: str,
    *,
    spec: Any,
    max_bytes: int,
    member_suffixes: Sequence[str],
    member_filter: Any,
) -> Iterator[tuple[str, Iterator[str]]]:
    """Buffer a remote ZIP to a temp file, then stream its delimited members."""
    buffer = io.BytesIO()
    total = 0
    truncated = False
    for line in client.stream_lines(url, source=spec, source_id=getattr(spec, "id", ""), mode="bot", max_bytes=max_bytes):
        chunk = line.encode("utf-8", "replace")
        buffer.write(chunk)
        total += len(chunk)
        if total >= max_bytes:
            truncated = True
            logger.warning("ZIP payload hit the %d-byte cap; processing what was received", max_bytes)
            break

    buffer.seek(0)
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as handle:
            handle.write(buffer.getvalue())
            tmp_path = handle.name
        with zipfile.ZipFile(tmp_path) as archive:
            for info in archive.infolist():
                if info.is_dir() or not _wanted_member(info.filename, member_suffixes, member_filter):
                    continue
                with archive.open(info) as member:
                    yield (info.filename, io.TextIOWrapper(member, encoding="utf-8", errors="replace", newline=""))
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        logger.warning(
            "could not process ZIP %s: %s%s", url, exc, " (payload was truncated — raise STREAM_MAX_BYTES)" if truncated else ""
        )
    finally:
        if tmp_path:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)


def _lines_to_bytes(lines: Iterator[str]) -> Iterator[bytes]:
    for line in lines:
        yield (line + "\n").encode("utf-8", "replace")


def _bounded(handle: Iterable[str], max_bytes: int) -> Iterator[str]:
    """Yield lines until ``max_bytes`` of text have been read."""
    total = 0
    for line in handle:
        total += len(line)
        if total > max_bytes:
            logger.warning("local dump exceeded the %d-byte cap; stopping", max_bytes)
            return
        yield line


def _is_local_path(url: str) -> bool:
    text = str(url or "").strip()
    if not text or "://" in text:
        return False
    return text.startswith(("/", "~", "./", "../")) or (len(text) > 1 and text[1] == ":")
