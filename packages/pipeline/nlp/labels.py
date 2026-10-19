"""Canonical OSINT entity labels shared by the extractor and the graph bridge.

The label strings are part of the model contract: they are sent to GLiNER and
echoed in Cloudflare Workers AI prompts. Changing them changes extraction
quality, so they live in one place and are normalised case-insensitively.

Only the first four labels map onto an existing ``EntityType``. Date and the
identifier labels are deliberately kept as *span* labels: they are valuable for
investigation output and local provenance, but promoting them to graph nodes
would require new Neo4j labels and a schema migration. ``entity_type_for_label``
maps the craft identifiers to ``Craft`` and the remaining labels to ``Unknown``;
callers that need the exact label use ``span.label``.
"""

from __future__ import annotations

from typing import Final

from puppetnet.models import EntityType

__all__ = [
    "OSINT_LABELS",
    "LABEL_DESCRIPTIONS",
    "IDENTIFIER_LABELS",
    "DATE_LABEL",
    "LABEL_TO_ENTITY_TYPE",
    "SPACY_LABEL_BY_OSINT_LABEL",
    "canonical_label",
    "entity_type_for_label",
    "graph_label",
    "is_graph_label",
    "properties_for_label",
]

#: Prompt/GLiNER labels, in the documented order. GLiNER performs best with a
#: focused label set (the upstream guidance is <= 20); all nine are cheap.
OSINT_LABELS: Final[tuple[str, ...]] = (
    "Person",
    "Organization",
    "Location",
    "Date",
    "Vehicle",
    "Aircraft_Registration",
    "IMO_Number",
    "Bank_Account",
    "Crypto_Address",
)

#: Short descriptions used when a backend accepts label descriptions and in the
#: Workers AI prompt. They must not introduce a second vocabulary.
LABEL_DESCRIPTIONS: Final[dict[str, str]] = {
    "Person": "a natural person, including aliases and titles",
    "Organization": "a company, foundation, state body, armed group or other organisation",
    "Location": "a country, city, address, facility or other geographic place",
    "Date": "an explicit calendar date or date range",
    "Vehicle": "a car, truck, vessel, aircraft or other conveyance",
    "Aircraft_Registration": "an aircraft tail number / registration mark",
    "IMO_Number": "an IMO ship identification number",
    "Bank_Account": "a bank account number, IBAN or equivalent account identifier",
    "Crypto_Address": "a cryptocurrency wallet or blockchain address",
}

#: Labels whose surface is a hard identifier rather than a name. The extractor
#: records them in ``EntitySpan.properties`` so a graph bridge can decide what
#: to do without re-parsing the surface.
IDENTIFIER_LABELS: Final[frozenset[str]] = frozenset(
    {"Aircraft_Registration", "IMO_Number", "Bank_Account", "Crypto_Address"}
)
DATE_LABEL: Final[str] = "Date"

LABEL_TO_ENTITY_TYPE: Final[dict[str, EntityType]] = {
    "Person": EntityType.PERSON,
    "Organization": EntityType.ORGANIZATION,
    "Location": EntityType.LOCATION,
    "Vehicle": EntityType.CRAFT,
    "Aircraft_Registration": EntityType.CRAFT,
    "IMO_Number": EntityType.CRAFT,
    "Date": EntityType.UNKNOWN,
    "Bank_Account": EntityType.UNKNOWN,
    "Crypto_Address": EntityType.UNKNOWN,
}

#: spaCy-style labels understood by :class:`puppetnet.parsing.nlp_engine.NLPEngine`.
SPACY_LABEL_BY_OSINT_LABEL: Final[dict[str, str]] = {
    "Person": "PERSON",
    "Organization": "ORG",
    "Location": "GPE",
    "Vehicle": "CRAFT",
    "Aircraft_Registration": "CRAFT",
    "IMO_Number": "CRAFT",
    "Date": "DATE",
    "Bank_Account": "ACCOUNT",
    "Crypto_Address": "CRYPTO_ADDRESS",
}

#: Labels that may become graph nodes without a schema migration.
_GRAPH_SAFE_LABELS: Final[frozenset[str]] = frozenset(
    {"Person", "Organization", "Location", "Vehicle", "Aircraft_Registration", "IMO_Number"}
)

_ALIASES: Final[dict[str, str]] = {
    # Keep the aliases predictable: lower-case, punctuation folded to "_".
    "person": "Person",
    "people": "Person",
    "per": "Person",
    "human": "Person",
    "organization": "Organization",
    "organisation": "Organization",
    "org": "Organization",
    "company": "Organization",
    "institution": "Organization",
    "location": "Location",
    "loc": "Location",
    "gpe": "Location",
    "place": "Location",
    "address": "Location",
    "date": "Date",
    "dates": "Date",
    "time": "Date",
    "vehicle": "Vehicle",
    "craft": "Vehicle",
    "vessel": "Vehicle",
    "ship": "Vehicle",
    "aircraft": "Vehicle",
    "aircraft_registration": "Aircraft_Registration",
    "aircraft_registrations": "Aircraft_Registration",
    "aircraft registration": "Aircraft_Registration",
    "tail_number": "Aircraft_Registration",
    "tail number": "Aircraft_Registration",
    "registration": "Aircraft_Registration",
    "imo_number": "IMO_Number",
    "imo number": "IMO_Number",
    "imo": "IMO_Number",
    "bank_account": "Bank_Account",
    "bank account": "Bank_Account",
    "iban": "Bank_Account",
    "account_number": "Bank_Account",
    "account number": "Bank_Account",
    "crypto_address": "Crypto_Address",
    "crypto address": "Crypto_Address",
    "cryptocurrency_address": "Crypto_Address",
    "wallet": "Crypto_Address",
    "wallet_address": "Crypto_Address",
    "bitcoin_address": "Crypto_Address",
    "blockchain_address": "Crypto_Address",
}


def _fold(value: object) -> str:
    return " ".join(str(value or "").strip().replace("-", "_").split()).lower()


def canonical_label(value: object) -> str | None:
    """Return the documented spelling for a model label, or ``None``.

    GLiNER returns the label string it was given in most versions, but custom
    label maps and older checkpoints can return lower-case or space-separated
    variants. Unknown labels are rejected rather than silently promoted to
    ``Person``/``Organization``.
    """

    folded = _fold(value)
    if folded in _ALIASES:
        return _ALIASES[folded]
    for label in OSINT_LABELS:
        if folded == _fold(label):
            return label
    return None


def entity_type_for_label(value: object) -> EntityType:
    label = canonical_label(value)
    return LABEL_TO_ENTITY_TYPE[label] if label else EntityType.UNKNOWN


def graph_label(value: object) -> str:
    """Map an OSINT label to the label expected by ``NLPEngine``.

    Unknown labels are returned unchanged so callers can decide whether to
    retain them; ``NLPEngine`` drops labels outside its own keep-list.
    """

    label = canonical_label(value)
    return SPACY_LABEL_BY_OSINT_LABEL.get(label or "", str(value or "").strip().upper())


def is_graph_label(value: object) -> bool:
    label = canonical_label(value)
    return bool(label and label in _GRAPH_SAFE_LABELS)


def properties_for_label(value: object) -> dict[str, object]:
    """Structured provenance attached to an extracted span."""

    label = canonical_label(value)
    if not label:
        return {}
    props: dict[str, object] = {"osint_label": label, "entity_type": entity_type_for_label(label).value}
    if label in IDENTIFIER_LABELS:
        props["identifier"] = True
    if label == DATE_LABEL:
        props["date_mention"] = True
    return props
