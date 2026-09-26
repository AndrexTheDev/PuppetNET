"""Wikidata SPARQL harvester (structured, confidence weight 1.0).

Wikidata is a crowd-sourced knowledge base, but its *sourcing* discipline and
stable Q-identifiers make it the best free structured backbone for corporate
ownership and political-affiliation graphs. PuppetNET queries a fixed set of
named SPARQL patterns, each of which maps cleanly onto one predicate:

======================  ============================================  ====================
Query                   Wikidata property                             PuppetNET predicate
======================  ============================================  ====================
ownership               P127 (owned by)                               OWNED_BY
subsidiaries            P355 (has subsidiary)                         PARENT_OF
board_members           P3320 (board member)                          DIRECTOR_OF
political_positions     P39 (position held) + P102 (member of party)  MEMBER_OF / OFFICER_OF
aircraft_operators      P137 (operator) on aircraft instances           OPERATES
vessel_operators        P137 (operator) on ship instances               OPERATES
employer                P108 (employer)                               EMPLOYED_BY
sanctions               P1275/P3872-adjacent + instance-of checks     SANCTIONED_BY
======================  ============================================  ====================

Endpoint policy: a descriptive ``User-Agent`` with contact details is mandatory
(query.wikidata.org rejects anonymous bots), and the adapter defaults to one
query every ~10 s with ``maxlag=5`` so it never competes with interactive use.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from typing import Any

from ..models import Document, Entity, EntityType, RelationType, has_organizational_marker
from .base import SourceAdapter

__all__ = ["WikidataAdapter", "WIKIDATA_QUERIES", "WikidataQuery"]


class WikidataQuery:
    """One named SPARQL query plus its graph mapping."""

    def __init__(
        self,
        name: str,
        sparql: str,
        *,
        subject_var: str,
        subject_type: EntityType,
        object_var: str,
        object_type: EntityType,
        predicate: RelationType,
        label_vars: Sequence[str] = (),
        extra_vars: Sequence[str] = (),
        description: str = "",
        default_limit: int = 150,
        reverse: bool = False,
    ) -> None:
        self.name = name
        self.sparql = sparql
        self.subject_var = subject_var
        self.subject_type = subject_type
        self.object_var = object_var
        self.object_type = object_type
        self.predicate = predicate
        self.label_vars = list(label_vars)
        self.extra_vars = list(extra_vars)
        self.description = description
        self.default_limit = default_limit
        self.reverse = reverse


WIKIDATA_QUERIES: dict[str, WikidataQuery] = {
    "ownership": WikidataQuery(
        name="ownership",
        description="Companies/organisations and their owners (P127), restricted to entities with a GLEIF/LEI or stock-exchange id where available.",
        subject_var="company",
        subject_type=EntityType.ORGANIZATION,
        object_var="owner",
        object_type=EntityType.ORGANIZATION,   # refined per-row: persons get PERSON
        predicate=RelationType.OWNED_BY,
        label_vars=("companyLabel", "ownerLabel"),
        extra_vars=("companyType", "jurisdictionLabel", "lei"),
        sparql="""
SELECT DISTINCT ?company ?companyLabel ?owner ?ownerLabel ?jurisdictionLabel ?lei WHERE {
  ?company wdt:P127 ?owner .
  OPTIONAL { ?company wdt:P159/wdt:P17 ?jurisdiction . }
  OPTIONAL { ?company wdt:P1278 ?lei . }
  FILTER EXISTS { ?company wdt:P31/wdt:P279* wd:Q4830453 . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
LIMIT %LIMIT%
""",
    ),
    "subsidiaries": WikidataQuery(
        name="subsidiaries",
        description="Parent organisations and their subsidiaries (P355).",
        subject_var="parent",
        subject_type=EntityType.ORGANIZATION,
        object_var="subsidiary",
        object_type=EntityType.ORGANIZATION,
        predicate=RelationType.PARENT_OF,
        label_vars=("parentLabel", "subsidiaryLabel"),
        extra_vars=("inception",),
        sparql="""
SELECT DISTINCT ?parent ?parentLabel ?subsidiary ?subsidiaryLabel WHERE {
  ?parent wdt:P355 ?subsidiary .
  FILTER EXISTS { ?parent wdt:P31/wdt:P279* wd:Q4830453 . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
LIMIT %LIMIT%
""",
    ),
    "board_members": WikidataQuery(
        name="board_members",
        description="Board members (P3320) of organisations.",
        subject_var="person",
        subject_type=EntityType.PERSON,
        object_var="org",
        object_type=EntityType.ORGANIZATION,
        predicate=RelationType.DIRECTOR_OF,
        label_vars=("personLabel", "orgLabel"),
        sparql="""
SELECT DISTINCT ?person ?personLabel ?org ?orgLabel WHERE {
  ?org wdt:P3320 ?person .
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
LIMIT %LIMIT%
""",
    ),
    "political_positions": WikidataQuery(
        name="political_positions",
        description="Politicians, the positions they hold (P39) and their party membership (P102).",
        subject_var="person",
        subject_type=EntityType.PERSON,
        object_var="party",
        object_type=EntityType.ORGANIZATION,
        predicate=RelationType.MEMBER_OF,
        label_vars=("personLabel", "partyLabel"),
        extra_vars=("positionLabel", "countryLabel"),
        sparql="""
SELECT DISTINCT ?person ?personLabel ?party ?partyLabel ?positionLabel ?countryLabel WHERE {
  ?person wdt:P39 ?position .
  ?person wdt:P102 ?party .
  OPTIONAL { ?position wdt:P17 ?country . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
LIMIT %LIMIT%
""",
    ),
    "employer": WikidataQuery(
        name="employer",
        description="People and their employers (P108).",
        subject_var="person",
        subject_type=EntityType.PERSON,
        object_var="employer",
        object_type=EntityType.ORGANIZATION,
        predicate=RelationType.EMPLOYED_BY,
        label_vars=("personLabel", "employerLabel"),
        sparql="""
SELECT DISTINCT ?person ?personLabel ?employer ?employerLabel WHERE {
  ?person wdt:P108 ?employer .
  FILTER EXISTS { ?person wdt:P31 wd:Q5 . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
LIMIT %LIMIT%
""",
    ),
    "aircraft_operators": WikidataQuery(
        name="aircraft_operators",
        description=(
            "Individual aircraft (CRAFT) and their operators (P137), plus registration (P13068) "
            "and country of registry (P17) — the aviation half of the movement graph."
        ),
        subject_var="craft",
        subject_type=EntityType.CRAFT,
        object_var="operator",
        object_type=EntityType.ORGANIZATION,
        predicate=RelationType.OPERATES,
        label_vars=("craftLabel", "operatorLabel"),
        extra_vars=("registration", "countryLabel"),
        reverse=True,
        sparql="""
SELECT DISTINCT ?craft ?craftLabel ?operator ?operatorLabel ?registration ?countryLabel WHERE {
  ?craft wdt:P31/wdt:P279* wd:Q11436 .
  ?craft wdt:P137 ?operator .
  OPTIONAL { ?craft wdt:P13068 ?registration . }
  OPTIONAL { ?craft wdt:P17 ?country . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
LIMIT %LIMIT%
""",
    ),
    "vessel_operators": WikidataQuery(
        name="vessel_operators",
        description="Named vessels (CRAFT) with an operator (P137) or owner (P127), plus IMO number (P458).",
        subject_var="craft",
        subject_type=EntityType.CRAFT,
        object_var="operator",
        object_type=EntityType.ORGANIZATION,
        predicate=RelationType.OPERATES,
        label_vars=("craftLabel", "operatorLabel"),
        extra_vars=("imo", "flagLabel"),
        reverse=True,
        sparql="""
SELECT DISTINCT ?craft ?craftLabel ?operator ?operatorLabel ?imo ?flagLabel WHERE {
  ?craft wdt:P31/wdt:P279* wd:Q11707950 .
  ?craft wdt:P137|wdt:P127 ?operator .
  OPTIONAL { ?craft wdt:P458 ?imo . }
  OPTIONAL { ?craft wdt:P17 ?flag . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
LIMIT %LIMIT%
""",
    ),
    "state_owned": WikidataQuery(
        name="state_owned",
        description="Organisations owned by a state (P127 → country) — state-capture analysis.",
        subject_var="org",
        subject_type=EntityType.ORGANIZATION,
        object_var="state",
        object_type=EntityType.LOCATION,
        predicate=RelationType.OWNED_BY,
        label_vars=("orgLabel", "stateLabel"),
        sparql="""
SELECT DISTINCT ?org ?orgLabel ?state ?stateLabel WHERE {
  ?org wdt:P127 ?state .
  ?state wdt:P31 wd:Q3624078 .
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
LIMIT %LIMIT%
""",
    ),
    "sanctioned_entities": WikidataQuery(
        name="sanctioned_entities",
        description=(
            "Entities carrying a sanctions designation (P3872 / instance-of sanctioned entity) "
            "linked to the sanctioning authority."
        ),
        subject_var="entity",
        subject_type=EntityType.ORGANIZATION,
        object_var="authority",
        object_type=EntityType.ORGANIZATION,
        predicate=RelationType.SANCTIONED_BY,
        label_vars=("entityLabel", "authorityLabel"),
        sparql="""
SELECT DISTINCT ?entity ?entityLabel ?authority ?authorityLabel WHERE {
  ?entity wdt:P1275 ?authority .
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
LIMIT %LIMIT%
""",
    ),
    "custom": WikidataQuery(
        name="custom",
        description="Ad-hoc SPARQL supplied via options.custom_sparql (must SELECT ?subject ?subjectLabel ?object ?objectLabel).",
        subject_var="subject",
        subject_type=EntityType.ORGANIZATION,
        object_var="object",
        object_type=EntityType.ORGANIZATION,
        predicate=RelationType.ASSOCIATED_WITH,
        label_vars=("subjectLabel", "objectLabel"),
        sparql="",
    ),
}

_QID_RE = re.compile(r"/Q\d+$")
_URL_CLEAN_RE = re.compile(r"\s+")


class WikidataAdapter(SourceAdapter):
    """Run named SPARQL queries against Wikidata and map bindings to the graph."""

    adapter_name = "wikidata"

    # ------------------------------------------------------------------ #
    def harvest(self) -> Iterator[Document]:
        requested = list(self.option("queries") or list(self.settings.wikidata_queries or []))
        if not requested:
            requested = ["ownership", "subsidiaries", "board_members"]
        limit = max(1, min(5000, int(self.option("limit_per_query", 150))))

        for name in requested:
            if self.ctx.budget_exhausted():
                return
            query = WIKIDATA_QUERIES.get(str(name).lower())
            if query is None:
                self.log.warning("unknown Wikidata query %r — skipping", name)
                self.stats.bump_source(self.spec.id, "errors")
                continue
            sparql = query.sparql
            if query.name == "custom":
                sparql = str(self.option("custom_sparql") or "").strip()
                if not sparql:
                    self.log.info("custom query requested but options.custom_sparql is empty — skipping")
                    continue
            sparql = sparql.replace("%LIMIT%", str(limit))
            document = self._run_query(query, sparql, limit=limit)
            if document is not None:
                yield document

    # ------------------------------------------------------------------ #
    def _run_query(self, query: WikidataQuery, sparql: str, *, limit: int) -> Document | None:
        endpoint = str(self.spec.base_url or "https://query.wikidata.org/sparql")
        params = {"query": sparql, "format": "json"}
        maxlag = self.option("maxlag", 5)
        if maxlag:
            params["maxlag"] = int(maxlag)

        headers = {
            "User-Agent": str(self.settings.wikidata_user_agent or self.settings.http_user_agent),
            "Accept": "application/sparql-results+json;q=0.9,application/json;q=0.8",
        }
        self.log.info("running Wikidata query %r (limit=%d)", query.name, limit)
        payload = self.fetch_json(endpoint, params=params, mode="bot", headers=headers)
        if not payload:
            return None

        bindings = ((payload.get("results") or {}).get("bindings")) or []
        if not isinstance(bindings, list) or not bindings:
            self.log.info("Wikidata query %r returned no bindings", query.name)
            return None

        entities: dict[str, Entity] = {}
        relations = []
        rows_kept = 0
        text_lines = [
            f"Wikidata structured extract — {query.name}",
            query.description,
            f"Endpoint: {endpoint}",
            f"Bindings returned: {len(bindings)}",
            "",
        ]

        for binding in bindings[: limit * 2]:
            if not isinstance(binding, dict):
                continue
            subject_label = self._binding_value(binding, query.label_vars[0] if query.label_vars else f"{query.subject_var}Label")
            object_label = self._binding_value(binding, query.label_vars[1] if len(query.label_vars) > 1 else f"{query.object_var}Label")
            subject_uri = self._binding_value(binding, query.subject_var)
            object_uri = self._binding_value(binding, query.object_var)
            if not subject_label or not object_label:
                continue
            if subject_label.strip().lower() == object_label.strip().lower():
                continue

            subject_type = query.subject_type
            if subject_uri and subject_uri.endswith("/Q5"):
                subject_type = EntityType.PERSON
            object_type = query.object_type
            if _looks_like_person(object_label):
                object_type = EntityType.PERSON

            subject = self.entity(
                subject_label,
                subject_type,
                properties=_compact(
                    {
                        "wikidata_id": _qid(subject_uri),
                        "wikidata_url": subject_uri,
                        **{var: self._binding_value(binding, var) for var in query.extra_vars},
                    }
                ),
            )
            obj = self.entity(
                object_label,
                object_type,
                properties=_compact({"wikidata_id": _qid(object_uri), "wikidata_url": object_uri}),
            )
            entities[subject.canonical_key] = subject
            entities[obj.canonical_key] = obj

            first, second = (obj, subject) if query.reverse else (subject, obj)
            relations.append(
                self.relation(
                    first,
                    query.predicate,
                    second,
                    evidence=f"Wikidata {query.name}: {first.name} — {query.predicate.value} → {second.name} ({_qid(subject_uri) or 'no-qid'})",
                    extra={"wikidata_query": query.name, "sparql_limit": limit},
                )
            )
            rows_kept += 1
            if rows_kept <= 200:
                text_lines.append(f"{first.name} — {query.predicate.value} → {second.name}")

        if not relations:
            self.log.info("Wikidata query %r produced no usable triples", query.name)
            return None

        text_lines += ["", f"Triples mapped: {len(relations)}", f"Entities resolved: {len(entities)}"]
        return self.make_document(
            f"{endpoint}#query={query.name}",
            title=f"Wikidata — {query.name}",
            text="\n".join(text_lines)[:400_000],
            content_type="application/sparql-results+json",
            external_id=f"wikidata-{query.name}-{limit}",
            entities=list(entities.values()),
            relations=relations,
            extra={"wikidata_query": query.name, "bindings": len(bindings), "triples": len(relations)},
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _binding_value(binding: dict[str, Any], variable: str) -> str:
        node = binding.get(variable)
        if isinstance(node, dict):
            value = node.get("value")
            if isinstance(value, str):
                return _URL_CLEAN_RE.sub(" ", value).strip()
        return ""


def _qid(uri: str) -> str:
    if not uri:
        return ""
    match = _QID_RE.search(uri)
    return match.group(0)[1:] if match else ""


def _looks_like_person(label: str) -> bool:
    """Heuristic: 2–4 capitalised tokens, no corporate marker → PERSON.

    The marker test is shared with :func:`puppetnet.models.normalize_name` so a
    Wikidata label such as "Gazprom PJSC" or "Nord Stream AG" is never typed as
    a person just because it happens to be two words long.
    """
    if not label:
        return False
    if has_organizational_marker(label):
        return False
    tokens = [t for t in label.split() if t]
    if not 2 <= len(tokens) <= 4:
        return False
    return all(token[:1].isupper() and token.isalpha() for token in tokens)


def _compact(properties: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in properties.items():
        if value in (None, "", [], {}):
            continue
        out[key] = _URL_CLEAN_RE.sub(" ", str(value)).strip()[:400]
    return out
