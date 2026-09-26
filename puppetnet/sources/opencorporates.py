"""OpenCorporates harvester (structured, confidence weight 1.0).

Uses the public v0.4 REST API::

    GET /companies/search?q=…&jurisdiction_code=…        company records
    GET /companies/{jurisdiction}/{number}?fields=…      one company + officers
    GET /officers/search?q=…                             officer→company links
    GET /corporate_groupings/search?q=…                  group memberships

Mapping to the PuppetNET graph
------------------------------
=====================================  ====================================
OpenCorporates field                   Graph element
=====================================  ====================================
``company.name``                       ``:Entity:Organization``
``company.jurisdiction_code``          ``:Entity:Location`` + ``REGISTERED_IN``
``registered_address_in_full``         ``:Entity:Location`` + ``LOCATED_IN``
``officers[].officer.name``            ``:Entity:Person`` (or Organization)
``officers[].officer.position``        ``DIRECTOR_OF`` / ``OFFICER_OF`` /
                                       ``EMPLOYED_BY`` depending on title
``parent_company``                     ``PARENT_OF``
``corporate_grouping`` members         ``MEMBER_OF``
=====================================  ====================================

An API token (``OPENCORPORATES_API_TOKEN``) raises rate limits substantially;
anonymous access works but is throttled hard, so the adapter backs off on 429
via the shared delay queue.
"""

from __future__ import annotations

import re
from typing import Any, Iterator, Sequence

from ..models import Document, Entity, EntityType, RelationType
from .base import SourceAdapter

__all__ = ["OpenCorporatesAdapter", "POSITION_MAP"]

#: Officer position → predicate. Checked in order; first substring match wins.
POSITION_MAP: tuple[tuple[tuple[str, ...], RelationType], ...] = (
    (("director", "board member", "director/", "managing director", "executive director", "non-executive"), RelationType.DIRECTOR_OF),
    (("chairman", "chairperson", "chair woman", "chair", "president", "vice-president", "vice president"), RelationType.DIRECTOR_OF),
    (("ceo", "chief executive", "cfo", "chief financial", "coo", "chief operating", "cto", "chief technology", "general manager", "manager", "secretary", "treasurer", "officer"), RelationType.OFFICER_OF),
    (("partner", "member", "trustee"), RelationType.MEMBER_OF),
    (("shareholder", "owner", "beneficial", "investor", "subscriber"), RelationType.SHAREHOLDER_OF),
    (("consultant", "advisor", "adviser", "lawyer", "attorney", "agent", "nominee", "representative", "intermediary"), RelationType.INTERMEDIARY_FOR),
    (("employee", "staff", "clerk", "assistant", "analyst"), RelationType.EMPLOYED_BY),
)

_JURISDICTION_NAMES = {
    "gb": "United Kingdom", "us_de": "Delaware, United States", "us_ny": "New York, United States",
    "us": "United States", "hk": "Hong Kong", "sg": "Singapore", "ch": "Switzerland",
    "lu": "Luxembourg", "cy": "Cyprus", "mt": "Malta", "vg": "British Virgin Islands",
    "ky": "Cayman Islands", "pa": "Panama", "sc": "Seychelles", "bz": "Belize", "bs": "Bahamas",
    "bm": "Bermuda", "je": "Jersey", "gg": "Guernsey", "im": "Isle of Man", "nl": "Netherlands",
    "be": "Belgium", "fr": "France", "de": "Germany", "it": "Italy", "es": "Spain",
    "pt": "Portugal", "ie": "Ireland", "at": "Austria", "se": "Sweden", "no": "Norway",
    "fi": "Finland", "dk": "Denmark", "pl": "Poland", "cz": "Czech Republic", "hu": "Hungary",
    "ro": "Romania", "bg": "Bulgaria", "gr": "Greece", "ru": "Russia", "ua": "Ukraine",
    "by": "Belarus", "tr": "Turkey", "ae": "United Arab Emirates", "sa": "Saudi Arabia",
    "qa": "Qatar", "kw": "Kuwait", "bh": "Bahrain", "om": "Oman", "lb": "Lebanon", "jo": "Jordan",
    "il": "Israel", "in": "India", "cn": "China", "jp": "Japan", "kr": "South Korea",
    "ca": "Canada", "au": "Australia", "nz": "New Zealand", "br": "Brazil", "ar": "Argentina",
    "mx": "Mexico", "za": "South Africa", "ng": "Nigeria", "ke": "Kenya", "ee": "Estonia",
    "lv": "Latvia", "lt": "Lithuania", "gi": "Gibraltar", "mh": "Marshall Islands", "vu": "Vanuatu",
    "ws": "Samoa", "aw": "Aruba", "cw": "Curacao", "kz": "Kazakhstan", "az": "Azerbaijan",
    "am": "Armenia", "ge": "Georgia", "md": "Moldova", "ir": "Iran", "eg": "Egypt", "ma": "Morocco",
}


class OpenCorporatesAdapter(SourceAdapter):
    """Harvest company/officer/grouping records from OpenCorporates."""

    adapter_name = "opencorporates"

    # ------------------------------------------------------------------ #
    def harvest(self) -> Iterator[Document]:
        if not self.api_token and not self.option("allow_anonymous", True):
            self.log.warning("OPENCORPORATES_API_TOKEN is not set and anonymous access is disabled — skipping")
            return

        queries = list(self.option("queries") or [])
        jurisdictions = list(self.option("jurisdictions") or [])
        per_page = max(1, min(30, int(self.option("per_page", 30))))

        if not queries:
            self.log.info("no OpenCorporates queries configured (options.queries) — nothing to harvest")
            return

        for query in queries:
            if self.ctx.budget_exhausted():
                return
            if self.option("include_groupings", True):
                yield from self._harvest_groupings(query)
            for jurisdiction in jurisdictions or [None]:
                if self.ctx.budget_exhausted():
                    return
                yield from self._harvest_companies(query, jurisdiction, per_page)
            # include_officers gates both the per-company officer lookup and the
            # standalone officer search — otherwise turning it off still spends
            # the source's rate budget on an endpoint nobody asked for.
            if self.option("include_officers", True):
                yield from self._harvest_officers(query, per_page)

    # ------------------------------------------------------------------ #
    @property
    def api_token(self) -> str:
        return str(self.settings.opencorporates_api_token or "")

    @property
    def base(self) -> str:
        return str(self.spec.base_url or "https://api.opencorporates.com/v0.4").rstrip("/")

    def _params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"per_page": self.option("per_page", 30)}
        if self.api_token:
            params["api_token"] = self.api_token
        params.update(extra or {})
        return {k: v for k, v in params.items() if v not in (None, "", [])}

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        url = f"{self.base}/{path.lstrip('/')}"
        payload = self.fetch_json(url, params=self._params(params), mode="bot")
        if payload is None:
            return None
        if isinstance(payload, dict) and payload.get("api_version"):
            return payload
        return payload if isinstance(payload, dict) else None

    # ------------------------------------------------------------------ #
    def _harvest_companies(self, query: str, jurisdiction: str | None, per_page: int) -> Iterator[Document]:
        params: dict[str, Any] = {"q": query, "per_page": per_page}
        if jurisdiction:
            params["jurisdiction_code"] = jurisdiction
        payload = self._get("/companies/search", params)
        if not payload:
            return
        companies = _extract_list(payload, ("results", "companies"), "company")
        self.log.info("OpenCorporates company search %r (jurisdiction=%s) → %d hits", query, jurisdiction or "any", len(companies))
        for company in companies:
            if self.ctx.budget_exhausted():
                return
            document = self._company_document(company, query=query)
            if document is not None:
                if self.option("include_officers", True):
                    self._attach_officers(document, company)
                yield document

    def _harvest_officers(self, query: str, per_page: int) -> Iterator[Document]:
        payload = self._get("/officers/search", {"q": query, "per_page": per_page})
        if not payload:
            return
        officers = _extract_list(payload, ("results", "officers"), "officer")
        self.log.info("OpenCorporates officer search %r → %d hits", query, len(officers))
        for officer in officers:
            if self.ctx.budget_exhausted():
                return
            document = self._officer_document(officer, query=query)
            if document is not None:
                yield document

    def _harvest_groupings(self, query: str) -> Iterator[Document]:
        payload = self._get("/corporate_groupings/search", {"q": query, "per_page": min(10, int(self.option("per_page", 30)))})
        if not payload:
            return
        groupings = _extract_list(payload, ("results", "corporate_groupings"), "corporate_grouping")
        self.log.info("OpenCorporates grouping search %r → %d hits", query, len(groupings))
        for grouping in groupings:
            if self.ctx.budget_exhausted():
                return
            document = self._grouping_document(grouping, query=query)
            if document is not None:
                yield document

    # ------------------------------------------------------------------ #
    # Record → graph mapping
    # ------------------------------------------------------------------ #
    def _company_document(self, company: dict[str, Any], *, query: str) -> Document | None:
        name = str(company.get("name") or "").strip()
        if not name:
            return None
        jurisdiction_code = str(company.get("jurisdiction_code") or "").strip().lower()
        number = str(company.get("company_number") or "").strip()
        url = str(company.get("opencorporates_url") or f"{self.base}/companies/{jurisdiction_code}/{number}")
        slug = re.sub(r"\W+", "-", url).strip("-")[:80]
        external_id = f"oc-company-{jurisdiction_code}-{number}" if number else f"oc-company-{slug}"

        entities: list[Entity] = []
        relations: list[Relation] = []
        doc_id_placeholder = ""

        subject = self.entity(
            name,
            EntityType.ORGANIZATION,
            doc_id=doc_id_placeholder,
            properties=_compact(
                {
                    "opencorporates_url": url,
                    "company_number": number,
                    "jurisdiction_code": jurisdiction_code,
                    "jurisdiction": _JURISDICTION_NAMES.get(jurisdiction_code, jurisdiction_code),
                    "incorporation_date": company.get("incorporation_date"),
                    "dissolution_date": company.get("dissolution_date"),
                    "current_status": company.get("current_status"),
                    "registered_address": _address_string(company),
                    "industry": _industry_string(company),
                    "previous_names": _join_names(company.get("previous_names")),
                    "source_publisher": (company.get("source") or {}).get("publisher") if isinstance(company.get("source"), dict) else None,
                    "query": query,
                }
            ),
            aliases=_alias_list(company),
        )
        entities.append(subject)

        if jurisdiction_code:
            place_name = _JURISDICTION_NAMES.get(jurisdiction_code, jurisdiction_code.upper())
            place = self.entity(place_name, EntityType.LOCATION, properties={"jurisdiction_code": jurisdiction_code})
            entities.append(place)
            relations.append(self.relation(subject, RelationType.REGISTERED_IN, place, evidence=f"OpenCorporates jurisdiction: {place_name}"))

        address = _address_string(company)
        if address and len(address) > 8:
            address_entity = self.entity(address[:200], EntityType.LOCATION, properties={"role": "registered_address"})
            entities.append(address_entity)
            relations.append(self.relation(subject, RelationType.LOCATED_IN, address_entity, evidence=f"OpenCorporates registered address: {address[:200]}"))

        parent = company.get("parent_company")
        if isinstance(parent, dict) and parent.get("name"):
            parent_entity = self.entity(
                str(parent["name"]),
                EntityType.ORGANIZATION,
                properties=_compact({"opencorporates_url": parent.get("opencorporates_url"), "jurisdiction_code": parent.get("jurisdiction_code")}),
            )
            entities.append(parent_entity)
            relations.append(
                self.relation(subject, RelationType.SUBSIDIARY_OF, parent_entity, evidence=f"OpenCorporates parent company: {parent['name']}")
            )

        text = _render_company_text(company, query)
        return self.make_document(
            url,
            title=f"OpenCorporates — {name}",
            text=text,
            content_type="application/json",
            published_at=self.parse_datetime(company.get("retrieved_at") or company.get("created_at")),
            external_id=external_id,
            entities=entities,
            relations=relations,
            extra={"opencorporates_type": "company", "jurisdiction_code": jurisdiction_code, "query": query},
        )

    def _attach_officers(self, document: Document, company: dict[str, Any]) -> None:
        """Fetch and attach the officer list for one company record."""
        jurisdiction_code = str(company.get("jurisdiction_code") or "").strip().lower()
        number = str(company.get("company_number") or "").strip()
        officers = company.get("officers") or []
        if not officers and jurisdiction_code and number:
            payload = self._get(f"/companies/{jurisdiction_code}/{number}", {"fields": "officers,parent_company"})
            if payload:
                officers = _extract_list(payload, ("results", "company"), "officers") or _extract_list(payload, ("results",), "officer")

        company_entity = next((e for e in document.entities if e.entity_type is EntityType.ORGANIZATION and e.properties.get("company_number") == number), None)
        if company_entity is None:
            company_entity = document.entities[0] if document.entities else None
        if company_entity is None:
            return

        for entry in officers[: int(self.option("max_officers_per_company", 25))]:
            officer = entry.get("officer") if isinstance(entry, dict) and "officer" in entry else entry
            if not isinstance(officer, dict):
                continue
            name = str(officer.get("name") or "").strip()
            if not name:
                continue
            position = str(officer.get("position") or "").strip()
            entity_type = EntityType.ORGANIZATION if _looks_corporate(name) else EntityType.PERSON
            entity = self.entity(
                name,
                entity_type,
                doc_id=document.doc_id,
                properties=_compact(
                    {
                        "position": position,
                        "start_date": officer.get("start_date"),
                        "end_date": officer.get("end_date"),
                        "opencorporates_url": officer.get("opencorporates_url"),
                        "nationality": officer.get("nationality"),
                        "occupation": officer.get("occupation"),
                        "address": _officer_address(officer),
                    }
                ),
            )
            document.entities.append(entity)
            document.relations.append(
                self.relation(
                    entity,
                    _predicate_for_position(position),
                    company_entity,
                    doc_id=document.doc_id,
                    evidence=f"OpenCorporates officer record: {name} — {position or 'unspecified position'} at {company_entity.name}",
                    extra={"position": position[:120], "start_date": officer.get("start_date"), "end_date": officer.get("end_date")},
                )
            )
        document.extra["officer_count"] = len(officers)

    def _officer_document(self, officer: dict[str, Any], *, query: str) -> Document | None:
        name = str(officer.get("name") or "").strip()
        company_name = ""
        nested_company = officer.get("company")
        if isinstance(nested_company, dict):
            company_name = str(nested_company.get("name") or "").strip()
        if not company_name:
            company_name = str(officer.get("company_name") or "").strip()
        if not name:
            return None
        url = str(officer.get("opencorporates_url") or f"{self.base}/officers/search?q={name}")
        entity_type = EntityType.ORGANIZATION if _looks_corporate(name) else EntityType.PERSON
        person = self.entity(
            name,
            entity_type,
            properties=_compact(
                {
                    "position": officer.get("position"),
                    "nationality": officer.get("nationality"),
                    "occupation": officer.get("occupation"),
                    "start_date": officer.get("start_date"),
                    "end_date": officer.get("end_date"),
                    "opencorporates_url": url,
                    "address": _officer_address(officer),
                    "query": query,
                }
            ),
        )
        entities = [person]
        relations = []
        if company_name:
            company = self.entity(
                company_name,
                EntityType.ORGANIZATION,
                properties=_compact(
                    {
                        "jurisdiction_code": officer.get("company_jurisdiction_code") or officer.get("jurisdiction_code"),
                        "opencorporates_url": (officer.get("company") or {}).get("opencorporates_url") if isinstance(officer.get("company"), dict) else None,
                    }
                ),
            )
            entities.append(company)
            relations.append(
                self.relation(
                    person,
                    _predicate_for_position(str(officer.get("position") or "")),
                    company,
                    evidence=f"OpenCorporates officer search: {name} — {officer.get('position') or 'unspecified'} at {company_name}",
                )
            )

        text = "\n".join(
            [
                f"OpenCorporates officer record — {name}",
                f"Position: {officer.get('position') or 'unspecified'}",
                f"Company: {company_name or 'unknown'}",
                f"Jurisdiction: {officer.get('company_jurisdiction_code') or officer.get('jurisdiction_code') or 'unknown'}",
                f"Dates: {officer.get('start_date') or '?'} to {officer.get('end_date') or 'present'}",
                f"Query: {query}",
            ]
        )
        return self.make_document(
            url,
            title=f"OpenCorporates officer — {name}",
            text=text,
            content_type="application/json",
            published_at=self.parse_datetime(officer.get("retrieved_at")),
            external_id=f"oc-officer-{_slug(name)[:60]}-{_slug(company_name)[:40]}",
            entities=entities,
            relations=relations,
            extra={"opencorporates_type": "officer", "query": query},
        )

    def _grouping_document(self, grouping: dict[str, Any], *, query: str) -> Document | None:
        name = str(grouping.get("name") or "").strip()
        if not name:
            return None
        grouping_slug = _slug(name).replace("-", "_")
        url = str(grouping.get("opencorporates_url") or f"{self.base}/corporate_groupings/{grouping_slug}")
        group_entity = self.entity(
            name,
            EntityType.ORGANIZATION,
            properties=_compact({"opencorporates_url": url, "grouping": True, "query": query}),
        )
        entities = [group_entity]
        relations = []
        lines = [f"OpenCorporates corporate grouping — {name}"]

        for entry in (grouping.get("members") or [])[: int(self.option("max_grouping_members", 30))]:
            member = entry.get("corporate_grouping_membership") if isinstance(entry, dict) and "corporate_grouping_membership" in entry else entry
            if not isinstance(member, dict):
                continue
            company = member.get("company") if isinstance(member.get("company"), dict) else {}
            member_name = str((company or {}).get("name") or member.get("name") or "").strip()
            if not member_name:
                continue
            member_entity = self.entity(
                member_name,
                EntityType.ORGANIZATION,
                properties=_compact(
                    {
                        "jurisdiction_code": (company or {}).get("jurisdiction_code"),
                        "opencorporates_url": (company or {}).get("opencorporates_url"),
                        "membership_start": member.get("start_date"),
                        "membership_end": member.get("end_date"),
                    }
                ),
            )
            entities.append(member_entity)
            relations.append(
                self.relation(
                    member_entity,
                    RelationType.MEMBER_OF,
                    group_entity,
                    evidence=f"OpenCorporates corporate grouping member: {member_name} in {name}",
                )
            )
            lines.append(f"Member: {member_name} ({(company or {}).get('jurisdiction_code', '?')})")

        return self.make_document(
            url,
            title=f"OpenCorporates grouping — {name}",
            text="\n".join(lines),
            content_type="application/json",
            published_at=self.parse_datetime(grouping.get("retrieved_at")),
            external_id=f"oc-grouping-{_slug(name)[:60]}",
            entities=entities,
            relations=relations,
            extra={"opencorporates_type": "corporate_grouping", "query": query},
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _predicate_for_position(position: str) -> RelationType:
    lowered = (position or "").lower().strip()
    if not lowered:
        return RelationType.OFFICER_OF
    for triggers, predicate in POSITION_MAP:
        if any(trigger in lowered for trigger in triggers):
            return predicate
    return RelationType.OFFICER_OF


def _slug(value: str) -> str:
    """Filesystem/URL-safe lowercase identifier (no backslashes inside f-strings)."""
    return re.sub(r"\W+", "-", str(value or "").lower()).strip("-")


def _looks_corporate(name: str) -> bool:
    lowered = (name or "").lower()
    markers = ("ltd", "limited", "inc", "llc", "llp", "plc", "gmbh", "corp", "corporation", "company", "holdings", "group", "s.a.", "sa ", "bank", "trust", "foundation", "pte", "b.v.", "nv", "ag", "ooo", "pjsc", "ojsc", "srl", "sas", "partners")
    return any(marker in lowered for marker in markers)


def _extract_list(payload: dict[str, Any], path: Sequence[str], item_key: str) -> list[dict[str, Any]]:
    """Navigate ``results.companies`` and unwrap the ``{"company": {...}}`` envelope."""
    node: Any = payload
    for key in path:
        if not isinstance(node, dict):
            return []
        node = node.get(key)
        if node is None:
            return []
    out: list[dict[str, Any]] = []
    if isinstance(node, list):
        for item in node:
            if isinstance(item, dict):
                out.append(item.get(item_key) if isinstance(item.get(item_key), dict) else item)
    elif isinstance(node, dict):
        inner = node.get(item_key)
        if isinstance(inner, list):
            out.extend([i for i in inner if isinstance(i, dict)])
        elif isinstance(inner, dict):
            out.append(inner)
        else:
            out.append(node)
    return [item for item in out if item]


def _address_string(company: dict[str, Any]) -> str:
    direct = company.get("registered_address_in_full")
    if direct:
        return re.sub(r"\s+", " ", str(direct)).strip()
    address = company.get("registered_address")
    if isinstance(address, dict):
        parts = [address.get(key) for key in ("street_address", "locality", "region", "postal_code", "country")]
        return ", ".join(str(p).strip() for p in parts if p)
    return ""


def _officer_address(officer: dict[str, Any]) -> str:
    address = officer.get("address")
    if isinstance(address, str):
        return re.sub(r"\s+", " ", address).strip()
    if isinstance(address, dict):
        return ", ".join(str(v).strip() for v in address.values() if v)
    return ""


def _industry_string(company: dict[str, Any]) -> str:
    codes = company.get("industry_codes") or []
    labels: list[str] = []
    for entry in codes:
        if isinstance(entry, dict):
            code = entry.get("industry_code") or {}
            if isinstance(code, dict):
                labels.append(str(code.get("label") or code.get("code") or "").strip())
            elif code:
                labels.append(str(code).strip())
    return "; ".join(label for label in labels if label)[:250]


def _join_names(entries: Any) -> str:
    if not isinstance(entries, list):
        return ""
    return "; ".join(str(e.get("company_name", "")).strip() for e in entries if isinstance(e, dict) and e.get("company_name"))[:250]


def _alias_list(company: dict[str, Any]) -> list[str]:
    aliases: list[str] = []
    for entry in company.get("previous_names") or []:
        if isinstance(entry, dict) and entry.get("company_name"):
            aliases.append(str(entry["company_name"]).strip())
    alternative = company.get("alternative_names") or []
    if isinstance(alternative, list):
        aliases.extend(str(a).strip() for a in alternative if isinstance(a, str))
    return [a for a in aliases if a][:16]


def _compact(properties: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in properties.items():
        if value in (None, "", [], {}):
            continue
        out[key] = re.sub(r"\s+", " ", str(value)).strip()[:500]
    return out


def _render_company_text(company: dict[str, Any], query: str) -> str:
    """Readable provenance text for the Document node (searchable in the UI)."""
    lines = [
        f"OpenCorporates company record — {company.get('name', 'unknown')}",
        f"Jurisdiction: {_JURISDICTION_NAMES.get(str(company.get('jurisdiction_code', '')).lower(), company.get('jurisdiction_code', 'unknown'))}",
        f"Company number: {company.get('company_number') or 'unknown'}",
        f"Status: {company.get('current_status') or 'unknown'}",
        f"Incorporated: {company.get('incorporation_date') or 'unknown'}",
        f"Dissolved: {company.get('dissolution_date') or 'n/a'}",
        f"Registered address: {_address_string(company) or 'unknown'}",
        f"Industry: {_industry_string(company) or 'unknown'}",
    ]
    if _join_names(company.get("previous_names")):
        lines.append(f"Previous names: {_join_names(company.get('previous_names'))}")
    parent = company.get("parent_company")
    if isinstance(parent, dict) and parent.get("name"):
        lines.append(f"Parent company: {parent['name']}")
    officers = company.get("officers") or []
    for entry in officers[:15]:
        officer = entry.get("officer") if isinstance(entry, dict) and "officer" in entry else entry
        if isinstance(officer, dict) and officer.get("name"):
            lines.append(f"Officer: {officer.get('name')} — {officer.get('position') or 'unspecified'}")
    lines.append(f"Search query: {query}")
    lines.append(f"Source URL: {company.get('opencorporates_url') or 'unknown'}")
    return "\n".join(lines)
