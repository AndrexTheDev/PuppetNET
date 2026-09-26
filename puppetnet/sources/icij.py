"""ICIJ Offshore Leaks harvester (structured, confidence weight 1.0).

Two complementary modes, both usable in the same run:

``node_ids`` (watchlist)
    Fetch ``https://offshoreleaks.icij.org/nodes/{id}`` pages and read the
    entity/relationship tables ICIJ renders server-side. Cheap, precise, and
    the natural daily mode for entities PuppetNET already tracks.

``dataset_urls`` (bulk)
    Stream the published node/edge TSV exports (gzip or zip) and map ICIJ's own
    schema onto the PuppetNET graph. Offsets are checkpointed in ``.state`` so
    a nightly cron re-reads only what is new.

ICIJ node types → PuppetNET entity types::

    Entity          → ORGANIZATION  (offshore vehicle, trust, foundation)
    Officer         → PERSON        (or ORGANIZATION when the name looks corporate)
    Intermediary    → ORGANIZATION  (law firm, bank, consultant)
    Address         → LOCATION
    Other           → UNKNOWN (dropped)

ICIJ link types → PuppetNET predicates::

    officer_of / director_of / president_of   → DIRECTOR_OF
    intermediary_of                           → INTERMEDIARY_FOR
    shareholder_of / owner_of                 → OWNS  (SHAREHOLDER_OF when partial)
    beneficiary_of                            → OWNED_BY
    registered_address / address_of           → LOCATED_IN
    jurisdiction_of / incorporated_in         → REGISTERED_IN
    related_to / connected_to                 → ASSOCIATED_WITH
    parent_of / subsidiary_of                 → PARENT_OF / SUBSIDIARY_OF
"""

from __future__ import annotations

import csv
import gzip
import io
import os
import re
import tempfile
import zipfile
from typing import Any, Iterable, Iterator, Sequence

from ..models import Document, Entity, EntityType, Relation, RelationType
from .base import SourceAdapter

__all__ = ["IcijLeaksAdapter"]

NODE_TYPE_MAP = {
    "entity": EntityType.ORGANIZATION,
    "officer": EntityType.PERSON,
    "intermediary": EntityType.ORGANIZATION,
    "address": EntityType.LOCATION,
    "other": EntityType.UNKNOWN,
}

LINK_TYPE_MAP: dict[str, RelationType] = {
    "officer of": RelationType.DIRECTOR_OF,
    "director of": RelationType.DIRECTOR_OF,
    "president of": RelationType.DIRECTOR_OF,
    "vice president of": RelationType.DIRECTOR_OF,
    "treasurer of": RelationType.DIRECTOR_OF,
    "secretary of": RelationType.DIRECTOR_OF,
    "board member of": RelationType.DIRECTOR_OF,
    "board of directors of": RelationType.DIRECTOR_OF,
    "intermediary of": RelationType.INTERMEDIARY_FOR,
    "shareholder of": RelationType.SHAREHOLDER_OF,
    "owner of": RelationType.OWNS,
    "beneficial owner of": RelationType.OWNS,
    "beneficiary of": RelationType.OWNED_BY,
    "registered address": RelationType.LOCATED_IN,
    "address of": RelationType.LOCATED_IN,
    "registered address of": RelationType.LOCATED_IN,
    "jurisdiction of": RelationType.REGISTERED_IN,
    "incorporated in": RelationType.REGISTERED_IN,
    "registered in": RelationType.REGISTERED_IN,
    "parent of": RelationType.PARENT_OF,
    "subsidiary of": RelationType.SUBSIDIARY_OF,
    "related to": RelationType.ASSOCIATED_WITH,
    "connected to": RelationType.ASSOCIATED_WITH,
    "same name and address as": RelationType.ASSOCIATED_WITH,
    "nominee shareholder of": RelationType.SHAREHOLDER_OF,
    "trustee of": RelationType.CONTROLS,
    "settlor of": RelationType.FOUNDED,
    "protector of": RelationType.CONTROLS,
    "power of attorney of": RelationType.CONTROLS,
    "authorized representative of": RelationType.OFFICER_OF,
    "senior managing officer of": RelationType.OFFICER_OF,
    "alternative director of": RelationType.DIRECTOR_OF,
    "director / board member of": RelationType.DIRECTOR_OF,
    "member of": RelationType.MEMBER_OF,
    "partner of": RelationType.ASSOCIATED_WITH,
    "client of": RelationType.ASSOCIATED_WITH,
    "bank account of": RelationType.ASSOCIATED_WITH,
    "record id": RelationType.ASSOCIATED_WITH,
}

#: Column aliases across ICIJ's various releases (Panama/Pandora/Paradise differ).
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "node_id": ("node_id", "id", "_id", "nodeid"),
    "name": ("name", "company", "entity name", "officer name", "title"),
    "original_name": ("original name", "original_name", "name (original)"),
    "node_type": ("node type", "node_type", "type", "category"),
    "jurisdiction": ("jurisdiction", "jurisdiction description", "jurisdictions", "country"),
    "address": ("address", "addresses", "registered address", "country of address"),
    "valid_until": ("valid until", "valid_until", "source url"),
    "source_url": ("source url", "source_url", "url"),
    "icij_id": ("icij id", "icij_id", "unique id"),
    "start_node": ("start id", "start_id", "node id (start)", "from"),
    "end_node": ("end id", "end_id", "node id (end)", "to"),
    "link_type": ("relationship", "link type", "link_type", "type", "relation"),
    "company": ("company name", "company", "entity"),
}

_STATE_FILE = "icij_state.json"


class IcijLeaksAdapter(SourceAdapter):
    """Harvest ICIJ Offshore Leaks nodes and relationships."""

    adapter_name = "icij"

    # ------------------------------------------------------------------ #
    def harvest(self) -> Iterator[Document]:
        yielded = False
        for document in self._harvest_watchlist():
            yielded = True
            yield document
        for document in self._harvest_datasets():
            yielded = True
            yield document
        if not yielded:
            self.log.info(
                "no ICIJ configuration found — set ICIJ_DATASET_URLS, or options.node_ids / "
                "options.search_terms for %s", self.spec.id,
            )

    # ------------------------------------------------------------------ #
    # Watchlist mode
    # ------------------------------------------------------------------ #
    def _harvest_watchlist(self) -> Iterator[Document]:
        node_ids = [str(nid).strip() for nid in (self.option("node_ids") or []) if str(nid).strip()]
        search_terms = [str(t).strip() for t in (self.option("search_terms") or list(self.settings.icij_officer_query_terms or [])) if t.strip()]

        if not node_ids and search_terms:
            node_ids = list(self._search_for_node_ids(search_terms))

        for node_id in node_ids:
            if self.ctx.budget_exhausted():
                return
            document = self._harvest_node(node_id)
            if document is not None:
                yield document

    def _search_for_node_ids(self, terms: Sequence[str]) -> Iterator[str]:
        """Resolve watchlist names to node ids via the public search page."""
        for term in terms[: int(self.option("max_search_terms", 10))]:
            url = f"{self.spec.base_url.rstrip('/')}/search"
            result = self.fetch(url, params={"q": term}, mode="browser")
            if not result.ok or not result.text:
                self.log.warning("ICIJ search failed for %r (%s)", term, result.status or result.error)
                self.stats.bump_source(self.spec.id, "errors")
                continue
            found = re.findall(r"/nodes/(\d+)", result.text or "")
            unique = list(dict.fromkeys(found))[: int(self.option("nodes_per_search", 5))]
            self.log.info("ICIJ search %r → %d node id(s)", term, len(unique))
            yield from unique

    def _harvest_node(self, node_id: str) -> Document | None:
        url = f"{self.spec.base_url.rstrip('/')}/nodes/{node_id}"
        result = self.fetch(url, mode="browser", referer=self.spec.base_url)
        if not result.ok or not result.text:
            self.stats.bump_source(self.spec.id, "errors")
            self.stats.record_error(self.spec.id, f"node fetch failed ({result.status or result.error}) {url}")
            return None

        from ..parsing.text_extract import html_to_text

        page = html_to_text(result.text or "", url=url, max_chars=120_000)
        record = self._parse_node_page(result.text or "", node_id=node_id)
        entities = record["entities"]
        relations = record["relations"]

        text_lines = [page.title or f"ICIJ Offshore Leaks node {node_id}", ""]
        text_lines += page.text.split("\n")[:400]
        text = "\n".join(line for line in text_lines if line.strip())

        document = self.make_document(
            url,
            title=page.title or f"ICIJ node {node_id}",
            text=text,
            content_type="text/html",
            published_at=self.parse_datetime(record.get("valid_until")),
            external_id=f"icij-node-{node_id}",
            entities=entities,
            relations=relations,
            extra={
                "icij_node_id": node_id,
                "icij_node_type": record.get("node_type", ""),
                "icij_jurisdiction": record.get("jurisdiction", ""),
                "icij_address": record.get("address", ""),
                "icij_source_release": record.get("source_release", ""),
                "structured_relations": len(relations),
            },
        )
        self.log.info(
            "ICIJ node %s (%s): %d entities, %d relations",
            node_id, record.get("node_type", "?"), len(entities), len(relations),
        )
        return document

    def _parse_node_page(self, html: str, *, node_id: str) -> dict[str, Any]:
        """Pull the node summary + connection table out of an ICIJ node page."""
        record: dict[str, Any] = {"entities": [], "relations": [], "node_id": node_id}
        if not html:
            return record

        def meta(pattern: str) -> str:
            match = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
            return re.sub(r"<[^>]+>", "", match.group(1)).strip() if match else ""

        record["name"] = meta(r"<h1[^>]*>(.*?)</h1>") or meta(r"<title>(.*?)</title>")
        record["node_type"] = meta(r"(?i)node type\s*</[^>]+>\s*<[^>]+>(.*?)<")
        record["jurisdiction"] = meta(r"(?i)jurisdiction\s*</[^>]+>\s*<[^>]+>(.*?)<")
        record["address"] = meta(r"(?i)address\s*</[^>]+>\s*<[^>]+>(.*?)<")
        record["valid_until"] = meta(r"(?i)valid until\s*</[^>]+>\s*<[^>]+>(.*?)<")
        record["source_release"] = meta(r"(?i)data from\s*</[^>]+>\s*<[^>]+>(.*?)<")

        entity_type = self._classify_officer(record.get("name", ""), record.get("node_type", ""))
        subject = self.entity(
            record.get("name") or f"ICIJ node {node_id}",
            entity_type,
            doc_id="",
            properties=_compact(
                {
                    "icij_node_id": node_id,
                    "icij_node_type": record.get("node_type"),
                    "jurisdiction": record.get("jurisdiction"),
                    "address": record.get("address"),
                    "source_release": record.get("source_release"),
                    "icij_url": f"{self.spec.base_url.rstrip('/')}/nodes/{node_id}",
                }
            ),
        )
        record["entities"].append(subject)

        # Connection rows: `<a href="/nodes/12345">Name</a>` next to a relationship label.
        rows = re.findall(
            r"<tr[^>]*>(.*?)</tr>", html, re.IGNORECASE | re.DOTALL
        )
        for row_html in rows:
            links = re.findall(r'href="[^"]*?/nodes/(\d+)[^"]*"[^>]*>(.*?)</a>', row_html, re.IGNORECASE | re.DOTALL)
            label = _strip_tags(row_html)
            if not links:
                continue
            for other_id, other_name in links[:4]:
                if other_id == node_id:
                    continue
                other_name = _strip_tags(other_name).strip()
                if not other_name:
                    continue
                predicate = self._predicate_from_text(label)
                other_type = self._classify_officer(other_name, "")
                obj = self.entity(
                    other_name,
                    other_type,
                    properties=_compact({"icij_node_id": other_id, "icij_url": f"{self.spec.base_url.rstrip('/')}/nodes/{other_id}"}),
                )
                record["entities"].append(obj)
                relation = self.relation(
                    subject,
                    predicate,
                    obj,
                    evidence=f"ICIJ Offshore Leaks node {node_id} connection: {label[:300]}",
                    extra={"icij_link_type": label[:120], "icij_target_node": other_id},
                )
                record["relations"].append(relation)
        return record

    def _predicate_from_text(self, label: str) -> RelationType:
        cleaned = _strip_tags(label).lower()
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,:;-")
        for phrase, predicate in LINK_TYPE_MAP.items():
            if phrase in cleaned:
                return predicate
        if "director" in cleaned or "board" in cleaned:
            return RelationType.DIRECTOR_OF
        if "intermediar" in cleaned:
            return RelationType.INTERMEDIARY_FOR
        if "shareholder" in cleaned:
            return RelationType.SHAREHOLDER_OF
        if "address" in cleaned:
            return RelationType.LOCATED_IN
        if "jurisdiction" in cleaned or "incorporated" in cleaned:
            return RelationType.REGISTERED_IN
        return RelationType.ASSOCIATED_WITH

    # ------------------------------------------------------------------ #
    # Bulk dataset mode
    # ------------------------------------------------------------------ #
    def _harvest_datasets(self) -> Iterator[Document]:
        urls = [u for u in (self.option("dataset_urls") or list(self.settings.icij_dataset_urls or [])) if u]
        if not urls:
            return
        row_limit = int(self.option("row_limit", 20_000))
        state = self._load_state()

        for url in urls:
            if self.ctx.budget_exhausted():
                return
            self.log.info("streaming ICIJ dataset %s", url)
            for member_name, text_stream in self._iter_dataset_members(url):
                consumed = 0
                skip = int(state.get(url, {}).get(member_name, 0) or 0)
                rows: list[dict[str, str]] = []
                for row in self._iter_tsv_rows(text_stream):
                    consumed += 1
                    if consumed <= skip:
                        continue
                    rows.append(row)
                    if len(rows) >= row_limit:
                        break
                if not rows:
                    self.log.info("no new rows in %s!%s (offset=%d)", url, member_name, skip)
                    continue
                document = self._rows_to_document(rows, url=url, member=member_name, start_offset=skip, consumed=consumed)
                state.setdefault(url, {})[member_name] = consumed
                if document is not None:
                    yield document

        self._save_state(state)

    def _iter_dataset_members(self, url: str) -> Iterator[tuple[str, Iterator[str]]]:
        """Yield ``(member_name, line_iterator)`` for a TSV/CSV/GZ/ZIP dataset URL."""
        lower = url.lower().split("?")[0]
        if lower.endswith(".zip"):
            yield from self._iter_zip_members(url)
            return

        lines = self.client.stream_lines(url, source=self.spec, source_id=self.spec.id, mode="bot")
        if lower.endswith(".gz"):
            yield (os.path.basename(lower)[:-3], _gunzip_lines_iter(self._read_all(lines)))
        else:
            yield (os.path.basename(lower) or "dataset", lines)

    def _read_all(self, lines: Iterator[str]) -> Iterator[bytes]:
        for line in lines:
            yield (line + "\n").encode("utf-8", "replace")

    def _iter_zip_members(self, url: str) -> Iterator[tuple[str, Iterator[str]]]:
        """Stream a ZIP to a temp file, then yield its TSV/CSV members."""
        max_bytes = int(self.settings.http_max_response_bytes)
        buffer = io.BytesIO()
        total = 0
        for line in self.client.stream_lines(url, source=self.spec, source_id=self.spec.id, mode="bot", max_bytes=max_bytes * 4):
            chunk = line.encode("utf-8", "replace")
            buffer.write(chunk)
            total += len(chunk)
            if total > max_bytes * 4:
                self.log.warning("ZIP payload exceeded the %d-byte cap; processing what was received", max_bytes * 4)
                break

        buffer.seek(0)
        tmp_path = ""
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as handle:
                handle.write(buffer.getvalue())
                tmp_path = handle.name
            with zipfile.ZipFile(tmp_path) as archive:
                for info in archive.infolist():
                    if info.is_dir() or not info.filename.lower().endswith((".tsv", ".csv", ".txt")):
                        continue
                    with archive.open(info) as member:
                        wrapper = io.TextIOWrapper(member, encoding="utf-8", errors="replace")
                        yield (info.filename, wrapper)
        except (zipfile.BadZipFile, OSError, ValueError) as exc:
            self.log.warning("could not process ICIJ ZIP %s: %s", url, exc)
            self.stats.bump_source(self.spec.id, "errors")
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _iter_tsv_rows(self, lines: Iterator[str]) -> Iterator[dict[str, str]]:
        """Parse a TSV/CSV stream into dicts using canonicalised column names."""
        header: list[str] = []
        delimiter = "\t"
        for index, line in enumerate(lines):
            if not line or not line.strip():
                continue
            if index == 0:
                delimiter = "\t" if line.count("\t") >= line.count(",") else ","
                header = [_canonical_column(cell) for cell in _split_delimited(line, delimiter)]
                continue
            cells = _split_delimited(line.rstrip("\n"), delimiter)
            if len(cells) < 2:
                continue
            row = {key: (cells[i] if i < len(cells) else "") for i, key in enumerate(header)}
            if any(v.strip() for v in row.values()):
                yield row

    def _rows_to_document(self, rows: Sequence[dict[str, str]], *, url: str, member: str, start_offset: int, consumed: int) -> Document | None:
        """Map a slice of an ICIJ export onto entities + relations."""
        entities: dict[str, Entity] = {}
        relations: list[Relation] = []
        node_cache: dict[str, Entity] = {}

        def entity_for(row: dict[str, str], *, id_keys: tuple[str, ...], name_keys: tuple[str, ...]) -> Entity | None:
            node_id = first_present(row, id_keys)
            name = first_present(row, name_keys) or first_present(row, ("original_name",))
            if not (node_id or name):
                return None
            key = node_id or name.lower()
            if key in node_cache:
                return node_cache[key]
            node_type = first_present(row, ("node_type",)).lower()
            etype = self._classify_officer(name, node_type)
            entity = self.entity(
                name or key,
                etype,
                properties=_compact(
                    {
                        "icij_node_id": node_id,
                        "icij_node_type": node_type,
                        "original_name": first_present(row, ("original_name",)),
                        "jurisdiction": first_present(row, ("jurisdiction",)),
                        "address": first_present(row, ("address",)),
                        "source_release": first_present(row, ("source_release", "dataset")),
                        "icij_url": f"{self.spec.base_url.rstrip('/')}/nodes/{node_id}" if node_id else "",
                    }
                ),
            )
            node_cache[key] = entity
            entities[entity.canonical_key] = entity
            return entity

        for row in rows:
            link_type = first_present(row, ("link_type",))
            if link_type:
                subject = entity_for(row, id_keys=("start_node",), name_keys=("start_name", "subject"))
                obj = entity_for(row, id_keys=("end_node",), name_keys=("end_name", "object"))
                if subject is None or obj is None or subject.canonical_key == obj.canonical_key:
                    continue
                predicate = LINK_TYPE_MAP.get(link_type.lower().strip()) or self._predicate_from_text(link_type)
                relations.append(
                    self.relation(
                        subject,
                        predicate,
                        obj,
                        evidence=f"ICIJ Offshore Leaks: {link_type} ({member} row {start_offset + consumed})",
                        extra={"icij_link_type": link_type[:120], "dataset_member": member},
                    )
                )
                continue

            entity = entity_for(row, id_keys=("node_id",), name_keys=("name",))
            if entity is not None:
                jurisdiction = first_present(row, ("jurisdiction",))
                address = first_present(row, ("address",))
                if jurisdiction:
                    place = self.entity(jurisdiction, EntityType.LOCATION, properties={"icij_role": "jurisdiction"})
                    entities[place.canonical_key] = place
                    relations.append(self.relation(entity, RelationType.REGISTERED_IN, place, evidence=f"ICIJ jurisdiction: {jurisdiction}"))
                if address and len(address) > 6 and address.lower() != jurisdiction.lower():
                    place = self.entity(address[:120], EntityType.LOCATION, properties={"icij_role": "address"})
                    entities[place.canonical_key] = place
                    relations.append(self.relation(entity, RelationType.LOCATED_IN, place, evidence=f"ICIJ registered address: {address[:120]}"))

        if not entities and not relations:
            return None

        summary = ", ".join(sorted({r.rel_type for r in relations}))[:400]
        text = "\n".join(
            [
                f"ICIJ Offshore Leaks extract — {member}",
                f"Rows {start_offset + 1}-{start_offset + len(rows)} of {url}",
                "",
                *[
                    f"{entity.name} ({entity.entity_type.value}) jurisdiction={entity.properties.get('jurisdiction', '')}"
                    for entity in list(entities.values())[:200]
                ],
                "",
                f"Relationships observed: {summary}",
            ]
        )
        return self.make_document(
            f"{url}#{member}",
            title=f"ICIJ Offshore Leaks — {member}",
            text=text,
            content_type="text/tab-separated-values",
            external_id=f"icij-{_stable_id(url + member)}",
            entities=list(entities.values()),
            relations=relations,
            extra={"icij_member": member, "icij_rows": len(rows), "icij_dataset": url},
        )

    # ------------------------------------------------------------------ #
    def _classify_officer(self, name: str, node_type: str) -> EntityType:
        lowered = (node_type or "").lower()
        for key, etype in NODE_TYPE_MAP.items():
            if lowered.startswith(key):
                if etype is EntityType.PERSON and _looks_corporate(name):
                    return EntityType.ORGANIZATION
                return etype
        if _looks_corporate(name):
            return EntityType.ORGANIZATION
        if _looks_like_address(name):
            return EntityType.LOCATION
        return EntityType.PERSON

    # ------------------------------------------------------------------ #
    def _state_path(self) -> str:
        return os.path.join(str(self.settings.state_path), _STATE_FILE)

    def _load_state(self) -> dict[str, Any]:
        import json

        path = self._state_path()
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        import json

        path = self._state_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
            self.log.info("ICIJ dataset offsets checkpointed → %s", path)
        except OSError as exc:
            self.log.warning("could not persist ICIJ state to %s: %s", path, exc)


# --------------------------------------------------------------------------- #
# Module helpers
# --------------------------------------------------------------------------- #

_TAG_STRIP_RE = re.compile(r"<[^>]+>")


def _strip_tags(fragment: str) -> str:
    return _TAG_STRIP_RE.sub(" ", fragment or "").strip()


def _canonical_column(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            normalised = re.sub(r"[^a-z0-9]+", "_", alias.strip().lower()).strip("_")
            if cleaned == normalised:
                return canonical
    return cleaned


def _split_delimited(line: str, delimiter: str) -> list[str]:
    try:
        return next(csv.reader([line], delimiter=delimiter, quotechar='"'))
    except (csv.Error, StopIteration):
        return line.split(delimiter)


def first_present(row: dict[str, str], keys: Iterable[str]) -> str:
    for key in keys:
        value = (row.get(key) or "").strip()
        if value and value.lower() not in {"null", "none", "n/a", "-"}:
            return value
    return ""


def _gunzip_lines_iter(chunks: Iterator[bytes]) -> Iterator[str]:
    """Incremental gzip → text lines."""
    decompressor = None
    try:
        import zlib

        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    except Exception:  # pragma: no cover
        decompressor = None
    if decompressor is None:  # pragma: no cover
        for chunk in chunks:
            yield chunk.decode("utf-8", "replace")
        return
    buffer = b""
    for chunk in chunks:
        try:
            buffer += decompressor.decompress(chunk)
        except Exception:  # noqa: BLE001 - malformed gzip
            break
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            yield line.decode("utf-8", "replace")
    if buffer:
        yield buffer.decode("utf-8", "replace")


def _looks_corporate(name: str) -> bool:
    if not name:
        return False
    lowered = name.lower()
    markers = (
        " ltd", " limited", " inc", " incorporated", " llc", " llp", " plc", " gmbh", " s.a.", " sa ",
        " corporation", " corp", " company", " co.", " holdings", " group", " international", " bank",
        " trust", " foundation", " partners", " enterprises", " trading", " investments", " capital",
        " pte", " pvt", " b.v.", " nv", " ag ", " oy", " ab ", " ojsc", " pjsc", " oao", " zao", " ooo",
        " s.r.l.", " srl", " sas", " consulting", " logistics", " shipping", " offshore", " services",
    )
    return any(marker in f" {lowered} " for marker in markers) or lowered.endswith(("ltd", "inc", "llc", "plc", "gmbh", "sa"))


def _looks_like_address(name: str) -> bool:
    if not name:
        return False
    lowered = name.lower()
    return bool(re.search(r"\b(street|road|avenue|building|floor|suite|po box|postal|drive|blvd|platz|rue|casa)\b", lowered)) or (
        bool(re.search(r"\d", name)) and "," in name
    )


def _compact(properties: dict[str, Any]) -> dict[str, Any]:
    return {key: str(value).strip()[:500] for key, value in properties.items() if value not in (None, "", [], {})}


def _stable_id(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
