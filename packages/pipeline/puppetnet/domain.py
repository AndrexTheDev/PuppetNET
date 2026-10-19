"""PuppetNET domain model: node classification and the opacity heuristics.

The graph stores one node per resolved actor (:class:`~puppetnet.models.Entity`,
unique on ``canonical_key``) and layers *domain labels* on top of it:

===========================  ==============================================
Label                        Applied when
===========================  ==============================================
``:Person``                  ``EntityType.PERSON``
``:Company``                 any ``ORGANIZATION`` that is not a foundation
``:ShellCompany``            a ``:Company`` that trips the opacity test
``:Foundation``              foundation / trust / charity / stichting / anstalt
``:Aircraft``                ``CRAFT`` with an aircraft registration or kind
``:Vessel``                  ``CRAFT`` with an IMO/MMSI or vessel prefix
``:Vehicle``                 ``CRAFT`` that is ground transport
``:Location``                ``EntityType.LOCATION``
``:Offshore``                any entity tied to a secrecy jurisdiction
===========================  ==============================================

``:Entity`` always stays on the node, so ``MATCH (e:Entity)`` keeps working and
``canonical_key`` remains the single uniqueness constraint.

Why labels are *set* rather than part of the ``MERGE`` key
---------------------------------------------------------
Domain labels are evidence-dependent and therefore **mutable**: a company
classified as ``:Company`` on Monday becomes ``:ShellCompany`` on Tuesday when a
leak dump reveals its nominee shareholder. ``MERGE (e:Entity:ShellCompany {…})``
would not match Monday's node and would try to create a second one — a unique
constraint violation on ``canonical_key``, which fails the whole batch. So the
writer merges on ``:Entity`` + ``entity_type`` (both immutable for a given key)
and then ``SET``s the current domain labels while ``REMOVE``ing the ones that no
longer apply. That converges instead of duplicating.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from .models import Entity, EntityType

__all__ = [
    "SECRECY_JURISDICTIONS",
    "OPACITY_JURISDICTIONS",
    "FOUNDATION_MARKERS",
    "DOMAIN_LABELS",
    "is_safe_label",
    "domain_labels",
    "stale_domain_labels",
    "is_shell_company",
    "shell_risk",
    "jurisdiction_class",
    "looks_like_tail_number",
    "normalize_address",
    "address_key",
    "entity_row_extras",
    "craft_label",
]

# --------------------------------------------------------------------------- #
# Jurisdictions
# --------------------------------------------------------------------------- #
#: Classic offshore secrecy jurisdictions: nominee-friendly company law, no
#: public register of beneficial owners, and the jurisdictions that dominate the
#: ICIJ leaks. Matched against ``jurisdiction`` / ``jurisdiction_code`` /
#: ``country`` properties and against the company name's tail.
SECRECY_JURISDICTIONS: frozenset[str] = frozenset(
    {
        # British Overseas Territories & Crown Dependencies with nominee-friendly
        # company law and no public beneficial-ownership register.
        "vg", "vgb", "bvi", "british virgin islands", "virgin islands, british",
        "ky", "cym", "cayman", "cayman islands",
        "bs", "bhs", "bahamas",
        "bm", "bmu", "bermuda",
        "ai", "aia", "anguilla",
        "tc", "tca", "turks and caicos",
        "ms", "msr", "montserrat",
        "gi", "gib", "gibraltar",
        # Caribbean / Central America.
        "pa", "pan", "panama",
        "bz", "blz", "belize",
        "vc", "vct", "st vincent", "saint vincent", "st vincent and the grenadines",
        "kn", "kna", "st kitts", "saint kitts and nevis",
        "lc", "lca", "st lucia", "saint lucia",
        "gd", "grd", "grenada",
        "ag", "atg", "antigua", "antigua and barbuda",
        "dm", "dma", "dominica",
        "sv", "slv", "el salvador",
        "pr", "pri", "puerto rico",
        "vi", "vir", "us virgin islands",
        # Pacific.
        "mh", "mhl", "marshall islands",
        "ws", "wsm", "samoa", "american samoa",
        "vu", "vut", "vanuatu",
        "ck", "cok", "cook islands",
        "nu", "niu", "niue",
        "pw", "plw", "palau",
        "nr", "nru", "nauru",
        "to", "ton", "tonga",
        "ki", "kir", "kiribati",
        "tv", "tuv", "tuvalu",
        "fm", "fsm", "micronesia",
        "gu", "gum", "guam",
        # Indian Ocean / Gulf free zones.
        "sc", "syc", "seychelles",
        "mu", "mus", "mauritius",
        "mv", "mdv", "maldives",
        "my-lbn", "labuan",
        "ae-rk", "ras al khaimah", "rak", "jebel ali", "dubai",
        # Dutch Caribbean.
        "cw", "cuw", "curacao", "curaçao",
        "aw", "abw", "aruba",
        "sx", "sxm", "sint maarten",
    }
)

#: Onshore centres whose company registry does **not** disclose beneficial
#: owners. Being registered in one of these is not evidence of wrongdoing — a
#: Delaware LLC is ordinary — but it removes the one signal that would otherwise
#: exonerate the vehicle, so it contributes to the opacity score at roughly a
#: third of the weight of :data:`SECRECY_JURISDICTIONS`.
OPACITY_JURISDICTIONS: frozenset[str] = frozenset(
    {
        # US states that permit anonymous LLCs and do not report in-state owners.
        "us-de", "delaware", "us-nv", "nevada", "us-sd", "south dakota",
        "us-wy", "wyoming",
        # European banking, holding and private-wealth centres.
        "lu", "lux", "luxembourg",
        "ch", "che", "switzerland", "zug", "geneva",
        "li", "lie", "liechtenstein",
        "mc", "mco", "monaco",
        "ad", "and", "andorra",
        "nl", "nld", "netherlands",
        "pt-30", "madeira",
        "cy", "cyp", "cyprus",
        "mt", "mlt", "malta",
        "je", "jey", "jersey",
        "gg", "ggy", "guernsey",
        "im", "imn", "isle of man",
        # Gulf free zones and Asian holding centres without BO registers.
        "ae", "are", "united arab emirates",
        "bh", "bhr", "bahrain",
        "qa", "qat", "qatar",
        "kw", "kwt", "kuwait",
        "om", "omn", "oman",
        "hk", "hkg", "hong kong",
        "sg", "sgp", "singapore",
        "my", "mys", "malaysia",
        "bn", "brn", "brunei",
        # New Zealand foreign trusts disclose no beneficiaries.
        "nz", "nzl", "new zealand",
    }
)

#: Name markers that identify a foundation / trust / charitable vehicle. These
#: get the ``:Foundation`` label instead of ``:Company`` — the distinction
#: matters because a foundation's trustees control assets they do not own.
FOUNDATION_MARKERS: frozenset[str] = frozenset(
    {
        "foundation", "stichting", "stiftung", "fondation", "fundacion", "fundación",
        "fondazione", "fundação", "stiftelsen", "säätiö", "fond", "fonds",
        "trust", "trustee", "trustees", "family trust", "unit trust", "treuhand",
        "fiducie", "fideicomiso", "fiducia", "waqf", "wakala",
        "charity", "charitable", "charitable trust", "ngo", "nonprofit", "non-profit",
        "endowment", "philanthrop", "philanthropic", "benevolent",
        "anstalt", "establishment", "st ecc", "ecclesiastical",
        "donor advised", "daf", "giving fund", "relief fund", "aid fund",
        "institute", "institut", "instituto", "think tank",
        "society", "vereniging", "association", "associazione", "asociacion",
        "club", "fraternal", "order of", "lodge",
    }
)

#: Every label this module can put on a node. The writer allowlists against it
#: before interpolating a label into Cypher.
DOMAIN_LABELS: frozenset[str] = frozenset(
    {
        "Entity", "Person", "Organization", "Location", "Craft",
        "Company", "ShellCompany", "Foundation", "Offshore",
        "Aircraft", "Vessel", "Vehicle",
    }
)

#: Mutually exclusive label families. When a node joins a family, the writer
#: removes its siblings so the classification converges instead of accumulating.
LABEL_FAMILIES: tuple[tuple[str, ...], ...] = (
    ("Company", "Foundation"),
    ("Aircraft", "Vessel", "Vehicle"),
)

_SAFE_LABEL_RE = re.compile(r"^[A-Z][A-Za-z]{0,63}$")

#: Two accepted shapes:
#:
#: * ``G-EUPA`` / ``VP-BBF`` / ``9H-VUC`` / ``P4-RMA`` — a one-or-two character
#:   registry prefix, hyphen, then the body (letter-only bodies are normal in the
#:   UK, Bermuda and most European registers);
#: * ``N707WA`` / ``B1234`` — no hyphen, but then it must contain a digit *and*
#:   a letter, which is what keeps ``Amadea``, ``Malta`` and ``12345`` out.
_TAIL_NUMBER_HYPHEN_RE = re.compile(r"^[A-Z0-9]{1,2}-[A-Z0-9]{1,5}$")
_TAIL_NUMBER_PLAIN_RE = re.compile(r"^(?=[A-Z0-9]{4,7}$)(?=.*\d)(?=.*[A-Z])[A-Z0-9]+$")


#: Ship-name prefixes that would otherwise read as a two-letter ICAO prefix
#: ("MT Renda" → "MT-RENDA"). A craft name starting with one of these is a
#: vessel, never an aircraft tail.
VESSEL_NAME_PREFIXES: frozenset[str] = frozenset(
    {
        "MT", "MV", "MS", "SS", "SY", "MY", "RV", "FV", "SV", "CS", "RMS", "HMS",
        "HMCS", "USS", "USNS", "HSC", "MSV", "FPSO", "FSO", "VLCC", "ULCC", "LPG",
        "HMT", "SSS", "TSS", "MVN", "GB", "CG", "ICGV", "NOAA",
    }
)


def looks_like_tail_number(name: str) -> bool:
    """True for a bare aircraft registration (``N707WA``, ``9H-VUC``, ``G-EUPA``)."""
    candidate = str(name or "").strip().upper().replace(" ", "-")
    prefix = candidate.split("-", 1)[0]
    if prefix in VESSEL_NAME_PREFIXES:
        return False
    return bool(_TAIL_NUMBER_HYPHEN_RE.match(candidate) or _TAIL_NUMBER_PLAIN_RE.match(candidate))

#: Property keys that carry a jurisdiction, in descending preference.
_JURISDICTION_KEYS: tuple[str, ...] = (
    "jurisdiction", "jurisdiction_code", "jurisdiction_description",
    "incorporation_jurisdiction", "registered_jurisdiction", "country",
    "country_codes", "countries", "flag", "registry_country", "state",
)

_ABBREVIATIONS: dict[str, str] = {
    "st": "street", "str": "street", "rd": "road", "ave": "avenue", "av": "avenue",
    "blvd": "boulevard", "dr": "drive", "ln": "lane", "ct": "court", "pl": "place",
    "sq": "square", "ter": "terrace", "hwy": "highway", "bldg": "building",
    "fl": "floor", "ste": "suite", "rm": "room", "ofc": "office", "apt": "apartment",
    "no": "number", "num": "number", "po box": "po box", "box": "po box",
    "house": "house", "tower": "tower", "plaza": "plaza", "centre": "center",
    "nr": "number", "unit": "unit", "block": "block",
}

_PUNCT_RE = re.compile(r"[^a-z0-9]+")
_TRAILING_COUNTRY_RE = re.compile(
    r"\b(?:united kingdom|great britain|united states of america|united states|"
    r"british virgin islands|cayman islands|turks and caicos|isle of man|"
    r"united arab emirates|new zealand|south africa|netherlands|switzerland|"
    r"luxembourg|liechtenstein|singapore|malaysia|seychelles|mauritius|panama|"
    r"bahamas|bermuda|gibraltar|monaco|andorra|ireland|spain|france|germany|"
    r"italy|belgium|austria|denmark|sweden|norway|finland|poland|portugal|"
    r"greece|cyprus|malta|jersey|guernsey|delaware|nevada|wyoming|"
    r"south dakota|florida|new york|california|texas|london|zurich|geneva)\b\s*$"
)


def is_safe_label(label: str) -> bool:
    """True for a label that may be interpolated into Cypher."""
    return bool(_SAFE_LABEL_RE.match(str(label))) and str(label) in DOMAIN_LABELS


def _lookup(properties: Mapping[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = properties.get(key)
        if isinstance(value, (list, tuple, set)):
            value = next((v for v in value if v), None)
        if value in (None, ""):
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def jurisdiction_class(entity: Entity) -> str:
    """``"secrecy"``, ``"opacity"`` or ``""`` for an entity's jurisdiction."""
    text = _lookup(entity.properties or {}, _JURISDICTION_KEYS).lower()
    if not text:
        # ICIJ/OpenCorporates sometimes only expose the jurisdiction inside the
        # company name tail ("Foo Holdings (BVI) Ltd", "Foo Ltd [Panama]").
        text = re.sub(r"[^a-z0-9]+", " ", str(entity.name).lower())
    if not text:
        return ""
    tokens = {t for t in text.split() if t} | {text}
    if tokens & SECRECY_JURISDICTIONS:
        return "secrecy"
    if tokens & OPACITY_JURISDICTIONS:
        return "opacity"
    for marker in SECRECY_JURISDICTIONS:
        if len(marker) > 3 and marker in text:
            return "secrecy"
    return ""


# --------------------------------------------------------------------------- #
# Craft classification
# --------------------------------------------------------------------------- #

_AIRCRAFT_SIGNALS: tuple[str, ...] = (
    "aircraft", "airplane", "aeroplane", "jet", "plane", "helicopter", "airliner",
    "airframe", "icao", "iata", "airworthiness", "tail number", "tailnumber",
    "serial number", "mode s", "airworth",
)
_VESSEL_SIGNALS: tuple[str, ...] = (
    "vessel", "ship", "yacht", "superyacht", "motor yacht", "sailboat", "barge",
    "ferry", "tanker", "container ship", "cruise", "boat", "imo", "mmsi",
    "flag state", "gross tonnage", "tonnage",
)
_GROUND_SIGNALS: tuple[str, ...] = (
    "vehicle", "car", "automobile", "truck", "lorry", "bus", "coach", "limousine",
    "vin", "chassis", "registration plate", "number plate", "licence plate",
    "motorcycle", "armoured", "armored",
)


def craft_label(entity: Entity) -> str:
    """``Aircraft`` / ``Vessel`` / ``Vehicle`` for a CRAFT entity."""
    props = entity.properties or {}
    kind = str(props.get("craft_kind") or props.get("craft_type") or "").lower()

    has_airframe_id = props.get("registration") or props.get("tail_number") or props.get("icao_hex")
    if has_airframe_id and ("vessel" not in kind and "ship" not in kind and "yacht" not in kind):
        return "Aircraft"
    if props.get("imo") or props.get("mmsi") or props.get("vessel_prefix"):
        return "Vessel"
    if props.get("vin") or props.get("plate") or props.get("chassis"):
        return "Vehicle"

    blob = " ".join([kind, str(props.get("model") or ""), str(entity.name)]).lower()
    if any(signal in blob for signal in _VESSEL_SIGNALS):
        return "Vessel"
    if any(signal in blob for signal in _AIRCRAFT_SIGNALS):
        return "Aircraft"
    if any(signal in blob for signal in _GROUND_SIGNALS):
        return "Vehicle"
    # A bare registration ("N707WA", "9H-VUC", "G-EUPA", "B-1234") is an
    # aircraft tail; nothing else about the name is enough to classify it.
    if looks_like_tail_number(entity.name):
        return "Aircraft"
    return "Craft"


def _is_foundation(entity: Entity) -> bool:
    props = entity.properties or {}
    explicit = str(props.get("organization_kind") or props.get("company_type") or props.get("legal_form") or "").lower()
    if any(marker in explicit for marker in FOUNDATION_MARKERS):
        return True
    if str(props.get("is_foundation") or "").lower() in {"1", "true", "yes"}:
        return True
    name = re.sub(r"[^a-z0-9]+", " ", str(entity.name).lower())
    if not name:
        return False
    tokens = set(name.split())
    if tokens & FOUNDATION_MARKERS:
        return True
    return any(marker in name for marker in ("family trust", "charitable trust", "donor advised", "stichting", "anstalt"))


# --------------------------------------------------------------------------- #
# Shell-company opacity test
# --------------------------------------------------------------------------- #

#: Statuses that mean the vehicle is not trading — a classic shelf/shell signal.
_DORMANT_STATUSES: frozenset[str] = frozenset(
    {
        "dormant", "dissolved", "struck off", "struck-off", "inactive", "closed",
        "cancelled", "canceled", "expired", "liquidated", "in liquidation",
        "wound up", "defunct", "lapsed", "revoked", "terminated",
    }
)

#: Name markers typical of holding vehicles that never trade.
_HOLDING_MARKERS: frozenset[str] = frozenset(
    {
        "holdings", "holding", "investments", "investment", "ventures", "capital",
        "enterprises", "international", "group", "assets", "asset management",
        "properties", "property", "estates", "estate", "trading", "commercial",
        "consultants", "consulting", "services", "management", "partners",
        "associates", "overseas", "offshore", "global", "worldwide", "universal",
        "general", "continental", "transnational", "nominees", "nominee",
    }
)

#: Numeric/placeholder names ICIJ renders for redacted vehicles.
_PLACEHOLDER_NAME_RE = re.compile(r"^(?:company|entity|node|record|officer)\s*#?\d*$", re.IGNORECASE)


def shell_risk(entity: Entity) -> tuple[float, list[str]]:
    """Opacity score in ``[0, 1]`` plus the human-readable reasons behind it.

    The score is deliberately explainable: every point comes from a named
    signal, and the reasons are written to the node so an analyst can see *why*
    PuppetNET called a company a shell instead of trusting a black box.
    """
    props = entity.properties or {}
    if entity.entity_type is not EntityType.ORGANIZATION:
        return 0.0, []
    if _is_foundation(entity):
        # Foundations are their own label; they can still be opaque, but the
        # "shell company" test is about corporate vehicles.
        return 0.0, []

    score = 0.0
    reasons: list[str] = []

    classification = jurisdiction_class(entity)
    if classification == "secrecy":
        score += 0.35
        reasons.append("secrecy jurisdiction")
    elif classification == "opacity":
        score += 0.12
        reasons.append("registry does not disclose beneficial owners")

    status = str(props.get("status") or props.get("company_status") or "").lower()
    if any(marker in status for marker in _DORMANT_STATUSES):
        score += 0.2
        reasons.append(f"non-trading status: {status.strip()[:40]}")

    if not str(props.get("reg_number") or props.get("company_number") or props.get("lei") or "").strip():
        score += 0.1
        reasons.append("no registration number on record")

    name = str(entity.name or "").strip()
    tokens = {t for t in re.sub(r"[^a-z0-9]+", " ", name.lower()).split() if t}
    if tokens & _HOLDING_MARKERS:
        score += 0.1
        reasons.append("holding-vehicle name marker")
    if _PLACEHOLDER_NAME_RE.match(name):
        score += 0.3
        reasons.append("placeholder name (redacted vehicle)")
    if re.search(r"\(\s*(?:bvi|panama|seychelles|cayman|samoa|belize|vanuatu|nevis)\s*\)", name, re.IGNORECASE):
        score += 0.1
        reasons.append("jurisdiction named in the company name")

    nominee = props.get("nominee") or props.get("is_nominee")
    if str(nominee).lower() in {"1", "true", "yes"} or props.get("nominee_of"):
        score += 0.25
        reasons.append("nominee shareholder/director")
    if props.get("beneficial_owner") or props.get("beneficiary_of"):
        score += 0.1
        reasons.append("beneficial owner disclosed separately")
    if props.get("intermediary") or props.get("service_provider") or props.get("intermediary_of"):
        score += 0.1
        reasons.append("formed through an intermediary")

    shared = props.get("shared_address_count")
    try:
        shared_count = int(shared)
    except (TypeError, ValueError):
        shared_count = 0
    if shared_count >= 5:
        score += 0.2
        reasons.append(f"{shared_count} companies share this registered address")
    elif shared_count >= 2:
        score += 0.1
        reasons.append(f"{shared_count} companies share this registered address")

    if str(props.get("is_offshore") or "").lower() in {"1", "true", "yes"}:
        score += 0.15
        reasons.append("flagged offshore by the source")
    if props.get("icij_node_id") or props.get("icij_id"):
        score += 0.15
        reasons.append("appears in an ICIJ Offshore Leaks release")

    if props.get("website") or props.get("phone") or props.get("employees"):
        score -= 0.1
        reasons.append("has trading presence (website/phone/staff)")

    score = max(0.0, min(1.0, round(score, 4)))
    return score, reasons


def is_shell_company(entity: Entity) -> bool:
    """True when the opacity score clears the shell threshold (0.5)."""
    score, _ = shell_risk(entity)
    return score >= 0.5


# --------------------------------------------------------------------------- #
# Labels
# --------------------------------------------------------------------------- #

def domain_labels(entity: Entity) -> tuple[str, ...]:
    """Every label this entity should currently carry, in ``SET`` order."""
    etype = entity.entity_type if isinstance(entity.entity_type, EntityType) else EntityType.coerce(entity.entity_type)
    labels = ["Entity", etype.value]

    if etype is EntityType.ORGANIZATION:
        if _is_foundation(entity):
            labels.append("Foundation")
        else:
            labels.append("Company")
            if is_shell_company(entity):
                labels.append("ShellCompany")
    elif etype is EntityType.CRAFT:
        labels.append(craft_label(entity))

    flagged_offshore = str((entity.properties or {}).get("is_offshore") or "").lower() in {"1", "true", "yes"}
    if flagged_offshore or (etype is EntityType.ORGANIZATION and jurisdiction_class(entity) == "secrecy"):
        labels.append("Offshore")

    return tuple(dict.fromkeys(label for label in labels if is_safe_label(label)))


def stale_domain_labels(entity: Entity) -> tuple[str, ...]:
    """Family siblings to ``REMOVE`` so a reclassification converges."""
    current = set(domain_labels(entity))
    stale: list[str] = []
    for family in LABEL_FAMILIES:
        if current & set(family):
            stale.extend(label for label in family if label not in current)
    return tuple(dict.fromkeys(stale))


# --------------------------------------------------------------------------- #
# Addresses (the SHARES_ADDRESS signal)
# --------------------------------------------------------------------------- #

def normalize_address(address: str) -> str:
    """Canonical, comparable form of a postal address.

    Two records describing the same desk in the same registered-agent office
    arrive as very different strings ("Vistra Corporate Services, Road Town,
    Tortola, VG" vs "VISTRA CORPORATE SERVICES CENTRE, WICKHAMS CAY II, ROAD
    TOWN, TORTOLA, BRITISH VIRGIN ISLANDS"). Normalisation folds case and
    punctuation, expands the usual abbreviations and drops a trailing country so
    those two can be compared — the point of ``SHARES_ADDRESS`` is the overlap,
    and a miss costs a real signal.
    """
    text = str(address or "").strip().lower()
    if not text:
        return ""
    text = text.replace("\n", ", ").replace("\r", " ")
    parts = [part.strip() for part in text.split(",") if part.strip()]
    normalised: list[str] = []
    for part in parts:
        words = _PUNCT_RE.sub(" ", part).split()
        expanded = [_ABBREVIATIONS.get(word, word) for word in words]
        # Re-join "po box" style two-word abbreviations that expanded to one.
        joined = " ".join(expanded)
        joined = re.sub(r"\bpo po box\b", "po box", joined)
        if joined:
            normalised.append(joined)
    if not normalised:
        return ""
    out = ", ".join(normalised)
    previous = None
    while previous != out:
        previous = out
        out = _TRAILING_COUNTRY_RE.sub("", out).strip().rstrip(",").strip()
    return re.sub(r"\s{2,}", " ", out)


def address_key(address: str) -> str:
    """Hashable grouping key for :func:`normalize_address` output."""
    normalised = normalize_address(address)
    return re.sub(r"[^a-z0-9]+", "", normalised)


# --------------------------------------------------------------------------- #
# Node properties
# --------------------------------------------------------------------------- #

def entity_row_extras(entity: Entity) -> dict[str, Any]:
    """Domain properties stamped onto every entity node.

    Kept separate from :meth:`Entity.to_node_properties` so the analysis layer
    can evolve without touching the extraction models.
    """
    props = entity.properties or {}
    extras: dict[str, Any] = {}

    aliases = sorted({a for a in entity.aliases if a and a != entity.name})
    if aliases:
        extras["alias"] = aliases[0][:200]

    if entity.entity_type is EntityType.ORGANIZATION:
        score, reasons = shell_risk(entity)
        if score:
            extras["shell_risk"] = score
            extras["shell_risk_reasons"] = reasons[:8]
            extras["is_shell"] = score >= 0.5
        classification = jurisdiction_class(entity)
        if classification:
            extras["jurisdiction_class"] = classification

    if entity.entity_type is EntityType.CRAFT:
        kind = craft_label(entity)
        extras["craft_kind"] = str(props.get("craft_kind") or kind)
        tail = str(props.get("tail_number") or props.get("registration") or "").strip()
        if tail:
            extras["tail_number"] = tail.upper().replace(" ", "-")[:16]
        owner = props.get("owner") or props.get("registrant") or props.get("registered_owner")
        if owner:
            extras["owner"] = str(owner)[:200]

    if entity.entity_type is EntityType.LOCATION:
        address = str(props.get("address") or entity.name or "").strip()
        if address:
            extras["address"] = address[:400]
            key = address_key(address)
            if key:
                extras["address_key"] = key[:200]
        gps = props.get("gps") or props.get("coordinates") or props.get("location")
        if gps:
            extras["gps"] = _format_gps(gps)

    return {k: v for k, v in extras.items() if v not in (None, "", [], {})}


def _format_gps(value: Any) -> str:
    """``"lat,lon"`` from whatever a source gave us (string, list or WKT-ish)."""
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        lat, lon = value[0], value[1]
    else:
        text = re.sub(r"[^0-9.,+-]+", " ", str(value or "")).strip()
        parts = [p for p in re.split(r"[\s,]+", text) if p]
        if len(parts) < 2:
            return ""
        lat, lon = parts[0], parts[1]
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return ""
    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
        return ""
    return f"{lat_f:.6f},{lon_f:.6f}"
