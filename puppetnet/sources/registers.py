"""Official register harvesters (structured, confidence weight 1.0).

Two adapters:

:class:`CompaniesHouseAdapter`
    UK Companies House REST API — company profiles, officers and Persons with
    Significant Control (PSC). Requires ``COMPANIES_HOUSE_API_KEY`` (free).

:class:`RegisterFilesAdapter`
    A *declarative* reader for any official CSV/TSV/JSON/NDJSON export
    (national commercial registries, aircraft registers, vessel registries,
    sanctions lists, land registries). Column→predicate mappings live in
    ``config/sources.yaml`` or the ``REGISTER_FILES`` environment variable, so
    adding a new national register is a config change, not a code change.

PSC natures-of-control map onto the ownership/control vocabulary::

    ownership-of-shares-75-to-100-percent     → OWNS        (evidence 1.0)
    ownership-of-shares-50-to-75-percent      → SHAREHOLDER_OF
    ownership-of-shares-25-to-50-percent      → SHAREHOLDER_OF (evidence 0.8)
    voting-rights-*                           → CONTROLS
    right-to-appoint-and-remove-directors     → CONTROLS    (evidence 1.0)
    right-to-surplus-assets / capital         → OWNS        (evidence 0.9)
    significant-influence-or-control          → CONTROLS    (evidence 0.7)
"""

from __future__ import annotations

import csv
import json
import re
from typing import Any, Iterator, Mapping, Sequence

from ..models import Document, Entity, EntityType, RelationType
from .base import SourceAdapter

__all__ = ["CompaniesHouseAdapter", "RegisterFilesAdapter", "PSC_CONTROL_MAP", "OFFICER_ROLE_MAP"]

PSC_CONTROL_MAP: tuple[tuple[tuple[str, ...], RelationType, float], ...] = (
    (("ownership-of-shares-75-to-100",), RelationType.OWNS, 1.0),
    (("ownership-of-shares-50-to-75",), RelationType.SHAREHOLDER_OF, 0.9),
    (("ownership-of-shares-25-to-50",), RelationType.SHAREHOLDER_OF, 0.8),
    (("ownership-of-shares",), RelationType.SHAREHOLDER_OF, 0.7),
    (("right-to-appoint-and-remove-directors",), RelationType.CONTROLS, 1.0),
    (("voting-rights-75-to-100",), RelationType.CONTROLS, 0.95),
    (("voting-rights-50-to-75",), RelationType.CONTROLS, 0.85),
    (("voting-rights-25-to-50",), RelationType.CONTROLS, 0.75),
    (("voting-rights",), RelationType.CONTROLS, 0.7),
    (("right-to-surplus-assets", "right-to-capital", "ownership-of-capital"), RelationType.OWNS, 0.9),
    (("significant-influence-or-control",), RelationType.CONTROLS, 0.7),
    (("right-to-appoint", "right-to-remove"), RelationType.CONTROLS, 0.85),
)

OFFICER_ROLE_MAP: dict[str, RelationType] = {
    "director": RelationType.DIRECTOR_OF,
    "corporate-director": RelationType.DIRECTOR_OF,
    "secretary": RelationType.OFFICER_OF,
    "corporate-secretary": RelationType.OFFICER_OF,
    "llp-member": RelationType.MEMBER_OF,
    "llp-designated-member": RelationType.DIRECTOR_OF,
    "member": RelationType.MEMBER_OF,
    "manager": RelationType.OFFICER_OF,
    "judicial-factor": RelationType.CONTROLS,
    "receiver": RelationType.CONTROLS,
    "receiver-manager": RelationType.CONTROLS,
    "cic-manager": RelationType.OFFICER_OF,
    "other": RelationType.ASSOCIATED_WITH,
}


# --------------------------------------------------------------------------- #
# UK Companies House
# --------------------------------------------------------------------------- #


class CompaniesHouseAdapter(SourceAdapter):
    """Official UK company register via the Companies House REST API."""

    adapter_name = "companies_house"

    # ------------------------------------------------------------------ #
    def harvest(self) -> Iterator[Document]:
        if not self.settings.companies_house_api_key:
            self.log.warning("COMPANIES_HOUSE_API_KEY is not set — skipping %s", self.spec.id)
            return

        company_numbers = [str(n).strip() for n in (self.option("company_numbers") or []) if str(n).strip()]
        queries = [str(q).strip() for q in (self.option("queries") or []) if str(q).strip()]

        for number in company_numbers:
            if self.ctx.budget_exhausted():
                return
            document = self._harvest_company(number)
            if document is not None:
                yield document

        for query in queries:
            if self.ctx.budget_exhausted():
                return
            for number in self._search(query):
                if self.ctx.budget_exhausted():
                    return
                document = self._harvest_company(number)
                if document is not None:
                    yield document

        if not company_numbers and not queries:
            self.log.info("no companies_house queries or company_numbers configured — nothing to harvest")

    # ------------------------------------------------------------------ #
    @property
    def base(self) -> str:
        return str(self.spec.base_url or "https://api.company-information.service.gov.uk").rstrip("/")

    def _headers(self) -> dict[str, str]:
        import base64

        token = base64.b64encode(f"{self.settings.companies_house_api_key}:".encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {token}", "Accept": "application/json"}

    def _get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
        return self.fetch_json(f"{self.base}/{path.lstrip('/')}", params=dict(params or {}), mode="bot", headers=self._headers())

    def _search(self, query: str) -> Iterator[str]:
        payload = self._get("/search/companies", {"q": query, "items_per_page": int(self.option("search_items_per_page", 20))})
        if not payload:
            return
        items = payload.get("items") or []
        self.log.info("Companies House search %r → %d hits", query, len(items))
        for item in items:
            number = str((item or {}).get("company_number") or "").strip()
            if number:
                yield number

    # ------------------------------------------------------------------ #
    def _harvest_company(self, company_number: str) -> Document | None:
        profile = self._get(f"/company/{company_number}")
        if not profile:
            self.stats.bump_source(self.spec.id, "errors")
            return None

        name = str(profile.get("company_name") or "").strip() or f"Company {company_number}"
        url = f"https://find-and-update.company-information.service.gov.uk/company/{company_number}"
        entities: list[Entity] = []
        relations: list[Relation] = []

        company = self.entity(
            name,
            EntityType.ORGANIZATION,
            properties=_compact(
                {
                    "company_number": company_number,
                    "company_type": profile.get("type"),
                    "company_status": profile.get("company_status"),
                    "date_of_creation": profile.get("date_of_creation"),
                    "date_of_cessation": profile.get("date_of_cessation"),
                    "jurisdiction": "United Kingdom",
                    "sic_codes": _join(profile.get("sic_codes")),
                    "registered_office": _address_string(profile.get("registered_office_address")),
                    "registered_office_is_in_dispute": profile.get("registered_office_is_in_dispute"),
                    "has_been_liquidated": profile.get("has_been_liquidated"),
                    "has_charges": profile.get("has_charges"),
                    "has_insolvency_history": profile.get("has_insolvency_history"),
                    "previous_names": _previous_names(profile),
                    "etag": profile.get("etag"),
                    "registry": "Companies House (UK)",
                }
            ),
            aliases=_previous_name_list(profile),
        )
        entities.append(company)

        uk = self.entity("United Kingdom", EntityType.LOCATION, properties={"registry": "Companies House"})
        entities.append(uk)
        relations.append(self.relation(company, RelationType.REGISTERED_IN, uk, evidence=f"Companies House company {company_number} is registered in the United Kingdom"))

        address = _address_string(profile.get("registered_office_address"))
        if address and len(address) > 8:
            place = self.entity(address[:200], EntityType.LOCATION, properties={"role": "registered_office"})
            entities.append(place)
            relations.append(self.relation(company, RelationType.LOCATED_IN, place, evidence=f"Registered office: {address[:200]}"))

        if self.option("include_officers", True):
            entities, relations = self._extend_with_officers(company_number, company, entities, relations)
        if self.option("include_psc", True):
            entities, relations = self._extend_with_psc(company_number, company, entities, relations)

        text = _render_company_text(profile, entities, relations)
        return self.make_document(
            url,
            title=f"Companies House — {name}",
            text=text,
            content_type="application/json",
            published_at=self.parse_datetime(profile.get("date_of_creation")),
            external_id=f"ch-{company_number}",
            entities=entities,
            relations=relations,
            extra={"registry": "companies_house", "company_number": company_number, "company_status": profile.get("company_status")},
        )

    def _extend_with_officers(
        self, company_number: str, company: Entity, entities: list[Entity], relations: list[Relation]
    ) -> tuple[list[Entity], list[Relation]]:
        payload = self._get(f"/company/{company_number}/officers", {"items_per_page": int(self.option("officers_per_page", 50))})
        if not payload:
            return entities, relations
        items = payload.get("items") or []
        for item in items[: int(self.option("max_officers", 50))]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            name = _normalise_person_name(name, item.get("forename"), item.get("surname"))
            role = str(item.get("officer_role") or "").lower()
            is_corporate = "corporate" in role or _looks_corporate(name)
            officer = self.entity(
                name,
                EntityType.ORGANIZATION if is_corporate else EntityType.PERSON,
                properties=_compact(
                    {
                        "officer_role": role,
                        "appointed_on": item.get("appointed_on"),
                        "resigned_on": item.get("resigned_on"),
                        "nationality": item.get("nationality"),
                        "occupation": item.get("occupation"),
                        "country_of_residence": item.get("country_of_residence"),
                        "date_of_birth": _partial_dob(item.get("date_of_birth")),
                        "address": _address_string(item.get("address")),
                        "registry": "Companies House (UK)",
                        "company_number": company_number,
                    }
                ),
            )
            entities.append(officer)
            predicate = OFFICER_ROLE_MAP.get(role, RelationType.OFFICER_OF)
            evidence_score = 1.0 if not item.get("resigned_on") else 0.6
            relations.append(
                self.relation(
                    officer,
                    predicate,
                    company,
                    evidence=(
                        f"Companies House officer record: {name} ({role or 'unspecified'}) at {company.name}"
                        + (f" until {item['resigned_on']}" if item.get("resigned_on") else "")
                    ),
                    evidence_score=evidence_score,
                    extra={"role": role, "appointed_on": item.get("appointed_on"), "resigned_on": item.get("resigned_on")},
                )
            )
        return entities, relations

    def _extend_with_psc(
        self, company_number: str, company: Entity, entities: list[Entity], relations: list[Relation]
    ) -> tuple[list[Entity], list[Relation]]:
        payload = self._get(f"/company/{company_number}/persons-with-significant-control", {"items_per_page": int(self.option("psc_per_page", 50))})
        if not payload:
            return entities, relations
        items = payload.get("items") or []
        for item in items[: int(self.option("max_psc", 50))]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            name = _normalise_person_name(name, None, None)
            kind = str(item.get("kind") or "").lower()
            is_corporate = "corporate-entity" in kind or _looks_corporate(name)
            controls = [str(c) for c in (item.get("natures_of_control") or [])]
            psc = self.entity(
                name,
                EntityType.ORGANIZATION if is_corporate else EntityType.PERSON,
                properties=_compact(
                    {
                        "psc_kind": kind,
                        "natures_of_control": ", ".join(controls),
                        "nationality": item.get("nationality"),
                        "country_of_residence": item.get("country_of_residence"),
                        "notified_on": item.get("notified_on"),
                        "ceased_on": item.get("ceased_on"),
                        "date_of_birth": _partial_dob(item.get("date_of_birth")),
                        "address": _address_string(item.get("address")),
                        "registry": "Companies House (UK)",
                        "company_number": company_number,
                    }
                ),
            )
            entities.append(psc)

            predicate, evidence_score = _control_mapping(controls)
            relations.append(
                self.relation(
                    psc,
                    predicate,
                    company,
                    evidence=(
                        f"Companies House PSC: {name} holds control of {company.name} "
                        f"({', '.join(controls) if controls else 'unspecified control'})"
                    ),
                    evidence_score=evidence_score * (0.6 if item.get("ceased_on") else 1.0),
                    extra={"natures_of_control": ", ".join(controls)[:200], "ceased_on": item.get("ceased_on")},
                )
            )
        return entities, relations


def _control_mapping(controls: Sequence[str]) -> tuple[RelationType, float]:
    for control in controls:
        lowered = control.lower()
        for triggers, predicate, score in PSC_CONTROL_MAP:
            if any(trigger in lowered for trigger in triggers):
                return predicate, score
    return RelationType.CONTROLS, 0.6


# --------------------------------------------------------------------------- #
# Generic declarative register files
# --------------------------------------------------------------------------- #


class RegisterFilesAdapter(SourceAdapter):
    """Map official register exports (CSV/TSV/JSON/NDJSON) onto the graph."""

    adapter_name = "register_files"

    # ------------------------------------------------------------------ #
    def harvest(self) -> Iterator[Document]:
        specs = self._file_specs()
        if not specs:
            self.log.info("no register files configured (options.files / REGISTER_FILES) — nothing to harvest")
            return

        row_limit = int(self.option("row_limit", 5_000))
        for file_spec in specs:
            if self.ctx.budget_exhausted():
                return
            document = self._harvest_file(file_spec, row_limit=row_limit)
            if document is not None:
                yield document

    # ------------------------------------------------------------------ #
    def _file_specs(self) -> list[dict[str, Any]]:
        specs: list[dict[str, Any]] = []
        for entry in list(self.option("files") or []):
            if isinstance(entry, str):
                specs.append({"url": entry})
            elif isinstance(entry, dict) and entry.get("url"):
                specs.append(dict(entry))
        for entry in list(self.settings.register_files or []):
            if isinstance(entry, str) and entry:
                if entry.strip().startswith("{"):
                    try:
                        parsed = json.loads(entry)
                        if isinstance(parsed, dict) and parsed.get("url"):
                            specs.append(parsed)
                            continue
                    except ValueError:
                        pass
                specs.append({"url": entry})
            elif isinstance(entry, dict) and entry.get("url"):
                specs.append(dict(entry))
        # De-duplicate by URL, keeping the richer definition.
        unique: dict[str, dict[str, Any]] = {}
        for spec in specs:
            key = str(spec["url"])
            if key not in unique or len(spec) > len(unique[key]):
                unique[key] = spec
        return list(unique.values())

    def _harvest_file(self, file_spec: dict[str, Any], *, row_limit: int) -> Document | None:
        url = str(file_spec.get("url"))
        fmt = str(file_spec.get("format") or _guess_format(url)).lower()
        name = str(file_spec.get("name") or _slug(url.split("/")[-1])[:60] or "register")
        mappings = file_spec.get("mappings")
        if mappings is None:
            mappings = [file_spec]
        mappings = [m for m in mappings if isinstance(m, dict) and (m.get("subject_column") or m.get("subject_field"))]
        if not mappings:
            self.log.warning("register file %s has no usable column mappings", url)
            return None

        entities: dict[str, Entity] = {}
        relations: list[Relation] = []
        rows_read = 0
        evidence_lines: list[str] = [f"Official register extract — {name}", f"URL: {url}", f"Format: {fmt}", ""]
        filters = file_spec.get("filters") or {}
        encoding = str(file_spec.get("encoding") or "utf-8")
        delimiter = str(file_spec.get("delimiter") or ("\t" if fmt == "tsv" else ","))

        for row in self._iter_rows(url, fmt=fmt, delimiter=delimiter, encoding=encoding):
            rows_read += 1
            if rows_read > row_limit:
                self.log.info("row limit (%d) reached for %s", row_limit, url)
                break
            if not self._row_matches_filters(row, filters):
                continue
            for mapping in mappings:
                produced = self._apply_mapping(row, mapping, file_spec, entities, relations)
                if produced and len(evidence_lines) < 400:
                    evidence_lines.append(produced)

        if not entities and not relations:
            self.log.info("register file %s produced no entities (%d rows read)", url, rows_read)
            return None

        evidence_lines += ["", f"Rows read: {rows_read}", f"Entities: {len(entities)}", f"Relations: {len(relations)}"]
        return self.make_document(
            url,
            title=f"Official register — {name}",
            text="\n".join(evidence_lines)[:400_000],
            content_type=f"text/{fmt}",
            external_id=f"register-{_slug(url)[:80]}",
            entities=list(entities.values()),
            relations=relations,
            extra={"register_name": name, "register_url": url, "rows_read": rows_read, "format": fmt},
        )

    # ------------------------------------------------------------------ #
    def _iter_rows(self, url: str, *, fmt: str, delimiter: str, encoding: str) -> Iterator[dict[str, str]]:
        lines = self.client.stream_lines(url, source=self.spec, source_id=self.spec.id, mode="bot", encoding=encoding)
        if fmt == "json":
            buffer = "".join(list(lines))
            try:
                payload = json.loads(buffer)
            except ValueError as exc:
                self.log.warning("invalid JSON register %s: %s", url, exc)
                return
            for record in _iter_json_records(payload):
                yield _stringify(record)
            return

        if fmt == "ndjson":
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    yield _stringify(record)
            return

        # CSV / TSV
        header: list[str] = []
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                cells = next(csv.reader([line], delimiter=delimiter, quotechar='"'))
            except (csv.Error, StopIteration):
                cells = line.split(delimiter)
            if index == 0:
                header = [_canonical_header(cell) for cell in cells]
                continue
            if len(cells) < 2:
                continue
            yield {key: (cells[i] if i < len(cells) else "") for i, key in enumerate(header)}

    def _apply_mapping(
        self,
        row: Mapping[str, str],
        mapping: Mapping[str, Any],
        file_spec: Mapping[str, Any],
        entities: dict[str, Entity],
        relations: list[Relation],
    ) -> str:
        subject_field = str(mapping.get("subject_column") or mapping.get("subject_field") or "")
        object_field = str(mapping.get("object_column") or mapping.get("object_field") or "")
        subject_value = _row_value(row, subject_field)
        object_value = _row_value(row, object_field) if object_field else ""
        if not subject_value:
            return ""

        subject_type = _entity_type(mapping.get("subject_type"), subject_value)
        subject_props = _extra_properties(row, mapping, file_spec)
        subject = self.entity(subject_value, subject_type, properties=subject_props)
        entities[subject.canonical_key] = subject

        if not object_value:
            return f"{subject.name} ({subject_type.value})"

        object_type = _entity_type(mapping.get("object_type"), object_value)
        obj = self.entity(object_value, object_type, properties=_extra_properties(row, mapping, file_spec, prefix="object_"))
        entities[obj.canonical_key] = obj

        predicate = RelationType.coerce(str(mapping.get("predicate") or "ASSOCIATED_WITH"))
        evidence_score = float(mapping.get("evidence_score") or 1.0)
        template = str(mapping.get("evidence_template") or "{subject} — {predicate} → {object}")
        evidence = template.format(subject=subject.name, object=obj.name, predicate=predicate.value, **{k: v for k, v in row.items() if isinstance(v, str)})
        relations.append(
            self.relation(
                subject,
                predicate,
                obj,
                evidence=evidence[:1000],
                evidence_score=evidence_score,
                extra={"register": str(file_spec.get("name") or file_spec.get("url") or "")[:200]},
            )
        )
        return evidence[:300]

    @staticmethod
    def _row_matches_filters(row: Mapping[str, str], filters: Mapping[str, Any]) -> bool:
        for key, expected in (filters or {}).items():
            actual = _row_value(row, str(key))
            if isinstance(expected, (list, tuple, set)):
                if actual.lower() not in {str(e).lower() for e in expected}:
                    return False
            elif str(expected).lower() != actual.lower():
                return False
        return True


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _guess_format(url: str) -> str:
    path = url.lower().split("?")[0]
    if path.endswith(".tsv") or path.endswith(".tab"):
        return "tsv"
    if path.endswith(".json"):
        return "json"
    if path.endswith(".ndjson") or path.endswith(".jsonl"):
        return "ndjson"
    if path.endswith(".csv"):
        return "csv"
    if path.endswith(".gz"):
        inner = path[:-3]
        if inner.endswith(".tsv"):
            return "tsv"
        if inner.endswith(".json"):
            return "json"
        return "csv"
    return "csv"


def _canonical_header(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name or "").strip().lower()).strip("_")


def _row_value(row: Mapping[str, str], field: str) -> str:
    if not field:
        return ""
    if field in row:
        return str(row[field] or "").strip()
    canonical = _canonical_header(field)
    for key, value in row.items():
        if _canonical_header(key) == canonical:
            return str(value or "").strip()
    return ""


def _extra_properties(row: Mapping[str, str], mapping: Mapping[str, Any], file_spec: Mapping[str, Any], *, prefix: str = "") -> dict[str, Any]:
    props: dict[str, Any] = {}
    for column in mapping.get("property_columns") or []:
        value = _row_value(row, str(column))
        if value:
            props[f"{prefix}{_canonical_header(column)}"] = value[:400]
    static = mapping.get("static_properties") or file_spec.get("static_properties") or {}
    for key, value in static.items():
        props[f"{prefix}{key}"] = str(value)[:400]
    props[f"{prefix}register"] = str(file_spec.get("name") or file_spec.get("url") or "")[:200]
    return _compact(props)


def _entity_type(value: Any, surface: str) -> EntityType:
    if isinstance(value, EntityType):
        return value
    text = str(value or "").strip().lower()
    mapping = {
        "person": EntityType.PERSON,
        "per": EntityType.PERSON,
        "individual": EntityType.PERSON,
        "organization": EntityType.ORGANIZATION,
        "organisation": EntityType.ORGANIZATION,
        "org": EntityType.ORGANIZATION,
        "company": EntityType.ORGANIZATION,
        "location": EntityType.LOCATION,
        "loc": EntityType.LOCATION,
        "country": EntityType.LOCATION,
        "address": EntityType.LOCATION,
        "craft": EntityType.CRAFT,
        "aircraft": EntityType.CRAFT,
        "vessel": EntityType.CRAFT,
        "ship": EntityType.CRAFT,
        "vehicle": EntityType.CRAFT,
    }
    if text in mapping:
        return mapping[text]
    if _looks_corporate(surface):
        return EntityType.ORGANIZATION
    return EntityType.UNKNOWN if not surface else EntityType.ORGANIZATION


def _looks_corporate(name: str) -> bool:
    lowered = (name or "").lower()
    markers = ("ltd", "limited", "inc", "llc", "llp", "plc", "gmbh", "corp", "corporation", "company", "holdings", "group", "bank", "trust", "foundation", "pte", "b.v.", " nv", " ag", "ooo", "pjsc", "ojsc", "srl", "sas", "partners", "airlines", "airways", "shipping", "maritime")
    return any(marker in lowered for marker in markers)


def _normalise_person_name(name: str, forename: Any = None, surname: Any = None) -> str:
    """Companies House writes ``SURNAME, Forename`` — flip it to natural order."""
    cleaned = re.sub(r"\s+", " ", str(name or "")).strip()
    if "," in cleaned:
        parts = [part.strip() for part in cleaned.split(",", 1) if part.strip()]
        if len(parts) == 2:
            cleaned = f"{parts[1]} {parts[0]}"
    if not cleaned and forename and surname:
        cleaned = f"{forename} {surname}".strip()
    return cleaned.title() if cleaned.isupper() else cleaned


def _address_string(address: Any) -> str:
    if not isinstance(address, dict):
        return str(address or "").strip()
    keys = ("address_line_1", "address_line_2", "premises", "thoroughfare", "locality", "postal_code", "country", "region", "po_box")
    parts = [str(address.get(key)).strip() for key in keys if address.get(key)]
    return ", ".join(parts)


def _partial_dob(dob: Any) -> str:
    if not isinstance(dob, dict):
        return ""
    month = str(dob.get("month") or "").strip()
    year = str(dob.get("year") or "").strip()
    if month and year:
        return f"{year}-{month.zfill(2)}"
    return year


def _previous_name_list(profile: Mapping[str, Any]) -> list[str]:
    names = profile.get("previous_company_names") or []
    return [str(entry.get("name")).strip() for entry in names if isinstance(entry, dict) and entry.get("name")][:16]


def _previous_names(profile: Mapping[str, Any]) -> str:
    return "; ".join(_previous_name_list(profile))


def _join(values: Any) -> str:
    if isinstance(values, (list, tuple)):
        return ", ".join(str(v) for v in values if v)
    return str(values or "")


def _compact(properties: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in properties.items():
        if value in (None, "", [], {}):
            continue
        out[key] = re.sub(r"\s+", " ", str(value)).strip()[:500]
    return out


def _iter_json_records(payload: Any) -> Iterator[dict[str, Any]]:
    """Find the record array inside a JSON register export."""
    if isinstance(payload, list):
        yield from (item for item in payload if isinstance(item, dict))
        return
    if not isinstance(payload, dict):
        return
    for key in ("items", "results", "records", "data", "rows", "entries", "companies", "persons"):
        value = payload.get(key)
        if isinstance(value, list):
            yield from (item for item in value if isinstance(item, dict))
            return
        if isinstance(value, dict):
            nested = value.get("items") or value.get("results") or value.get("company")
            if isinstance(nested, list):
                yield from (item for item in nested if isinstance(item, dict))
                return
            if isinstance(nested, dict):
                yield nested
                return
    yield payload


def _stringify(record: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in record.items():
        if value is None:
            out[str(key)] = ""
        elif isinstance(value, (dict, list)):
            out[str(key)] = json.dumps(value, ensure_ascii=False)[:500]
        else:
            out[str(key)] = str(value)
    return out


def _render_company_text(profile: Mapping[str, Any], entities: Sequence[Entity], relations: Sequence[Relation]) -> str:
    lines = [
        f"Companies House record — {profile.get('company_name', 'unknown')}",
        f"Company number: {profile.get('company_number', 'unknown')}",
        f"Status: {profile.get('company_status', 'unknown')}",
        f"Type: {profile.get('type', 'unknown')}",
        f"Incorporated: {profile.get('date_of_creation', 'unknown')}",
        f"Registered office: {_address_string(profile.get('registered_office_address')) or 'unknown'}",
        f"SIC codes: {_join(profile.get('sic_codes')) or 'none'}",
        f"Previous names: {_previous_names(profile) or 'none'}",
        "",
        f"Linked entities ({len(entities)}):",
    ]
    lines += [f"  - {entity.name} [{entity.entity_type.value}]" for entity in entities[:40]]
    lines += ["", f"Relationships ({len(relations)}):"]
    lines += [f"  - {rel.subject.name} —{rel.rel_type}→ {rel.obj.name}" for rel in relations[:40]]
    return "\n".join(lines)


def _slug(value: str) -> str:
    return re.sub(r"\W+", "-", str(value or "").lower()).strip("-")
