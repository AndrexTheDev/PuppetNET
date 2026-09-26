"""CRAFT entity detection — aircraft, vessels and vehicles.

spaCy's stock models have no vehicle/aircraft class, so PuppetNET builds one:

* **Regex identifiers** — ICAO aircraft registrations (``9H-VUC``, ``VP-CGY``,
  ``N97GA``), IATA/ICAO flight numbers (``BA286``), IMO numbers
  (``IMO 9321483``), MMSI radio ids, and vessel name prefixes (``MV Baltic
  Leader``).
* **Gazetteers** — airframe types (``Gulfstream G650``, ``Boeing 737 MAX 8``,
  ``Il-76``), vessel classes (``VLCC``, ``bulk carrier``, ``superyacht``) and
  ground craft (``armoured convoy``, ``Toyota Land Cruiser convoy``).

Matches are exposed three ways:

1. :meth:`CraftDetector.iter_matches` — pure regex over raw text (works with no
   model installed, used by the heuristic backend and by tests).
2. :meth:`CraftDetector.spacy_patterns` — EntityRuler patterns injected into a
   spaCy pipeline so CRAFT spans participate in the real dependency parse and
   can become triple subjects/objects (``9H-VUC`` → ``TRAVELED_TO`` → ``Moscow``).
3. :meth:`CraftDetector.annotate` — post-hoc span injection when the ruler is
   unavailable, resolving overlaps in favour of the more specific match.

Every match carries structured properties (registry country, craft kind,
identifier class) which land on the Neo4j node.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Sequence

from ..logging_utils import get_logger

__all__ = [
    "CraftMatch",
    "CraftDetector",
    "AIRCRAFT_TYPE_GAZETTEER",
    "VESSEL_TYPE_GAZETTEER",
    "VESSEL_PREFIXES",
    "imo_checksum_valid",
    "AIRLINE_IATA_CODES",
    "REGISTRY_PREFIX_COUNTRY",
]

logger = get_logger("parsing.craft")

# --------------------------------------------------------------------------- #
# Gazetteers
# --------------------------------------------------------------------------- #

#: Registry prefix → jurisdiction. Covers the prefixes that dominate OSINT
#: aviation reporting (offshore registries, sanctions-evasion flag states, and
#: the usual business-jet tail-number families).
REGISTRY_PREFIX_COUNTRY: dict[str, str] = {
    "N": "United States",
    "G": "United Kingdom",
    "M": "Isle of Man",
    "2": "United Kingdom",
    "EI": "Ireland",
    "F": "France",
    "D": "Germany",
    "OO": "Belgium",
    "PH": "Netherlands",
    "OE": "Austria",
    "HB": "Switzerland",
    "I": "Italy",
    "EC": "Spain",
    "CS": "Portugal",
    "SX": "Greece",
    "LZ": "Bulgaria",
    "HA": "Hungary",
    "SP": "Poland",
    "OK": "Czech Republic",
    "OM": "Slovakia",
    "SE": "Sweden",
    "LN": "Norway",
    "OH": "Finland",
    "OY": "Denmark",
    "TF": "Iceland",
    "ES": "Estonia",
    "YL": "Latvia",
    "LY": "Serbia",
    "9H": "Malta",
    "VP-C": "Cayman Islands",
    "VP-B": "Bermuda",
    "VQ-B": "Bermuda",
    "T7": "San Marino",
    "T8": "Palau",
    "P4": "Aruba",
    "A6": "United Arab Emirates",
    "A7": "Qatar",
    "A9C": "Bahrain",
    "A4O": "Oman",
    "HZ": "Saudi Arabia",
    "JY": "Jordan",
    "OD": "Lebanon",
    "YK": "Syria",
    "EP": "Iran",
    "TC": "Turkey",
    "4L": "Georgia",
    "RA": "Russia",
    "RF": "Russia",
    "UR": "Ukraine",
    "EW": "Belarus",
    "UP": "Kazakhstan",
    "EK": "Armenia",
    "4K": "Azerbaijan",
    "EY": "Tajikistan",
    "EX": "Kyrgyzstan",
    "C-F": "Canada",
    "C-G": "Canada",
    "XA": "Mexico",
    "XB": "Mexico",
    "XC": "Mexico",
    "HK": "Colombia",
    "YV": "Venezuela",
    "PP": "Brazil",
    "PR": "Brazil",
    "PS": "Brazil",
    "PT": "Brazil",
    "LV": "Argentina",
    "LU": "Argentina",
    "CC": "Chile",
    "OB": "Peru",
    "CP": "Bolivia",
    "ZK": "New Zealand",
    "VH": "Australia",
    "9V": "Singapore",
    "B": "China",
    "VT": "India",
    "PK": "Indonesia",
    "9M": "Malaysia",
    "RP": "Philippines",
    "HS": "Thailand",
    "VN": "Nepal",
    "S2": "Bangladesh",
    "AP": "Pakistan",
    "5Y": "Kenya",
    "5H": "Tanzania",
    "ZS": "South Africa",
    "5N": "Nigeria",
    "9A": "Croatia",
    "E7": "Bosnia and Herzegovina",
    "Z3": "North Macedonia",
    "J5": "Guinea-Bissau",
    "3DC": "Equatorial Guinea",
    "3X": "Guinea",
    "TR": "Gabon",
    "TJ": "Cameroon",
    "5V": "Togo",
    "6V": "Senegal",
    "6W": "Senegal",
    "5U": "Niger",
    "XT": "Burkina Faso",
    "TU": "Ivory Coast",
    "3B": "Mauritius",
    "D2": "Angola",
    "9Q": "Democratic Republic of the Congo",
    "7Q": "Malawi",
    "Z": "Zimbabwe",
}

AIRCRAFT_TYPE_GAZETTEER: tuple[str, ...] = (
    # Business jets — the workhorses of oligarch/sanctions-evasion reporting.
    "Gulfstream G650", "Gulfstream G550", "Gulfstream G450", "Gulfstream G280", "Gulfstream G200",
    "Bombardier Global 7500", "Bombardier Global 6000", "Bombardier Global 5000", "Bombardier Challenger 650",
    "Bombardier Challenger 605", "Bombardier Challenger 850", "Bombardier Learjet 60", "Bombardier Learjet 45",
    "Dassault Falcon 900", "Dassault Falcon 7X", "Dassault Falcon 8X", "Dassault Falcon 2000",
    "Embraer Legacy 600", "Embraer Legacy 450", "Embraer Phenom 300", "Embraer Praetor 600", "Embraer Lineage 1000",
    "Cessna Citation X", "Cessna Citation Latitude", "Cessna Citation Sovereign", "Cessna Citation Mustang",
    "Hawker 800XP", "Beechcraft King Air 350", "Piaggio Avanti",
    # Airliners
    "Boeing 737", "Boeing 737 MAX", "Boeing 737 MAX 8", "Boeing 747", "Boeing 747-400", "Boeing 747-8",
    "Boeing 757", "Boeing 767", "Boeing 777", "Boeing 777-300ER", "Boeing 787", "Boeing 787 Dreamliner",
    "Boeing 787-8", "Boeing 787-9", "Boeing 737-800",
    "Airbus A319", "Airbus A320", "Airbus A320neo", "Airbus A321", "Airbus A321neo", "Airbus A330",
    "Airbus A340", "Airbus A350", "Airbus A380", "Airbus A220", "Airbus ACJ320",
    "Antonov An-124", "Antonov An-225", "Antonov An-26", "Antonov An-72", "Antonov An-148",
    "Ilyushin Il-76", "Ilyushin Il-96", "Ilyushin Il-62", "Ilyushin Il-18",
    "Tupolev Tu-134", "Tupolev Tu-154", "Tupolev Tu-204", "Tupolev Tu-214",
    "Yakovlev Yak-40", "Yakovlev Yak-42", "Sukhoi Superjet 100", "Sukhoi Superjet",
    "Embraer ERJ-145", "Embraer E190", "Embraer E195", "ATR 72", "ATR 42", "De Havilland Dash 8",
    "Bombardier CRJ-900", "Bombardier Q400", "Fokker 100", "McDonnell Douglas MD-11", "MD-80",
    "Lockheed Martin C-130", "C-130 Hercules", "Boeing C-17", "Boeing 707", "Ilyushin Il-112",
    # Helicopters
    "Sikorsky S-76", "Sikorsky S-92", "Sikorsky UH-60", "Sikorsky Black Hawk",
    "Bell 407", "Bell 429", "Bell 206", "Bell V-22 Osprey",
    "Eurocopter EC135", "Eurocopter EC145", "Airbus Helicopters H145", "AgustaWestland AW139",
    "Agusta A109", "Mil Mi-8", "Mil Mi-17", "Mil Mi-24", "Mil Mi-26", "Kamov Ka-32", "Kamov Ka-52",
    "Robinson R44", "Robinson R66", "Leonardo AW169",
    # Generic craft nouns worth capturing with an adjacent proper name
    "business jet", "private jet", "charter jet", "charter flight", "cargo plane", "cargo aircraft",
    "military transport aircraft", "air ambulance", "helicopter", "drones", "unmanned aerial vehicle",
    "fighter jet", "seaplane", "turboprop",
)

VESSEL_TYPE_GAZETTEER: tuple[str, ...] = (
    "superyacht", "mega yacht", "megayacht", "motor yacht", "sailing yacht", "luxury yacht", "yacht",
    "VLCC", "ULCC", "Suezmax tanker", "Aframax tanker", "crude oil tanker", "oil tanker", "product tanker",
    "chemical tanker", "LNG carrier", "LPG carrier", "bulk carrier", "container ship", "container vessel",
    "ro-ro ferry", "cruise ship", "cruise liner", "research vessel", "icebreaker", "cable ship",
    "fishing trawler", "trawler", "dredger", "tugboat", "barge", "floating crane", "semi-submersible",
    "dark fleet tanker", "shadow fleet tanker", "shadow fleet vessel",
)

VESSEL_PREFIXES: tuple[str, ...] = (
    "MV", "M/V", "M-V", "MT", "M/T", "M-T", "MS", "M/S", "SS", "S.S.", "SV", "S/V",
    "RV", "R/V", "R-V", "FV", "F/V", "MY", "M/Y", "M-Y",
    "HSC", "HSV", "NV", "TS", "PS", "RMS", "USS", "HMS", "IMO",
)

GROUND_CRAFT_GAZETTEER: tuple[str, ...] = (
    "armoured convoy", "armored convoy", "military convoy", "convoy", "motorcade",
    "armoured vehicle", "armored vehicle", "MRAP", "Humvee", "technical",
    "Toyota Land Cruiser", "Land Cruiser", "Toyota Hilux", "Hilux pickup",
    "Mercedes-Benz S-Class", "Mercedes S600", "Maybach", "Rolls-Royce Phantom", "Bentley Mulsanne",
    "Range Rover", "BMW 7 Series", "Audi A8", "Cadillac Escalade", "Lexus LX", "Toyota Century",
    "ZIL limousine", "Chaika limousine", "Ural truck", "KamAZ truck", "KamAZ", "Ural",
    "armoured train", "armored train", "military railway car",
    "BTR-80", "BMP-2", "BMP-3", "T-72", "T-90", "Leopard 2", "Abrams tank", "M1 Abrams",
    "S-400 launcher", "Iskander launcher", "Patriot launcher",
)

#: IATA/ICAO two-letter airline codes used to disambiguate flight numbers from
#: ordinary letter+digit noise. Kept broad (legacy + current carriers).
AIRLINE_IATA_CODES: frozenset[str] = frozenset(
    """AA AB AC AD AE AF AI AK AM AR AS AT AV AY AZ B2 B6 B7 BA BD BG BI BJ BL BP BR BT BX CA CI CL CM CO CX
    CY CZ DL DP DT DY E2 EA EI EK EN ET EW EY EZ F7 F9 FB FI FJ FM FR FV FZ G3 GA GE GF GH HA HF HM HR HU HX HY
    IB IC IE IG IK IR JJ JL JM JO JP JU JX K6 KC KE KL KM KN KP KQ KU KX LA LG LH LO LP LX LY M3 MH MK MM MS MU
    NF NH NI NK NO NZ OA OK OM OS OU OZ PC PG PK PM PR PS PX PZ QF QH QR QS QZ RA RB RJ RO SU SV SW SY TC TG TK
    TM TN TP TU TX TZ UA U2 U6 U7 U8 UI UL UM UT UU UX VF VI VN VR VS VY W5 W6 W9 WF WS WY X3 X7 XQ Y4 YK YP YU
    ZB ZH ZL 3K 3O 3U 4U 5J 5K 5N 5W 6H 7H 8U 9C 9U 9V A3 A5 A9 A4 B0 B3 B4 B5 B8 B9 C4 C5 C6 C7 C8 C9 CC CG CI
    CM CX D7 D3 D8 D9 E5 EK 4Z 4M 3V 2P 2J""".split()
)

# --------------------------------------------------------------------------- #
# Regular expressions
# --------------------------------------------------------------------------- #

#: Known registry prefixes, longest first (``VP-C`` must beat ``V``).
_PREFIX_ALTERNATION = "|".join(sorted(REGISTRY_PREFIX_COUNTRY.keys(), key=len, reverse=True))

#: Multi-character registry prefixes (``9H-VUC``, ``VP-CGY``, ``RA-67895``,
#: ``T7-KMR``, ``C-FXYZ``, ``A6-BMA``). The prefix is validated against the
#: registry table after matching.
_MULTI_PREFIX_ALTERNATION = "|".join(sorted((k for k in REGISTRY_PREFIX_COUNTRY if len(k) > 1), key=len, reverse=True))
TAIL_MULTI_RE = re.compile(
    rf"(?<![A-Za-z0-9])(?P<prefix>{_MULTI_PREFIX_ALTERNATION})-?(?P<body>[A-Z0-9]{{2,5}})(?![A-Za-z0-9])"
)

#: Single-letter prefixes are ambiguous in prose (``E-COMMERCE``, ``I-95``,
#: ``A-LIST``), so only the registries that dominate aviation reporting are
#: accepted, the body shape is constrained, and common English words are
#: rejected outright.
SINGLE_CHAR_TAIL_PREFIXES: frozenset[str] = frozenset("G M F D I B N Z V T U X Y H E L O P S".split())
TAIL_SINGLE_RE = re.compile(
    r"(?<![A-Za-z0-9-])(?P<prefix>[A-Z])-?(?P<body>[A-Z]{1,2}[0-9]{1,3}|[0-9]{1,3}[A-Z]{1,2}|[A-Z]{4})(?![A-Za-z0-9])"
)
#: Bodies that are ordinary words rather than registrations.
TAIL_BODY_BLOCKLIST: frozenset[str] = frozenset(
    """AMERICA APPLE BEACH BOARD BRAND BUILD CALL CARD CARE CASE CENT CHIEF CITY CLASS CLUB CODE
    COME COMMERCE CORE CORP COUNTY COURT CROSS DATA DATE DEAL DEPT DESIGN DOOR DRIVE EAST EDGE
    EMAIL EURO FACE FILE FILM FINE FIRE FIRM FLAG FLOOR FOOD FORM GAME GATE GIRL GLOBAL GOLD
    GREEN GROUP GUIDE HAND HARD HEAD HEALTH HEART HELP HIGH HOME HOPE HOUSE INFO ISLAND ITEM
    JUST KING LAND LEVEL LIFE LINE LINK LIST LIVE LOOK MADE MAIL MAIN MAKE MARK MASS MEET MIND
    MODE MONEY MONTH MOON MORNING MUSIC NAME NEWS NEXT NIGHT NORTH NOTE NOVA NUMBER OFFER OFFICE
    ONE OPEN ORDER PARK PART PARTY PEOPLE PLACE PLAN PLANT PLAY POINT PORT POST POWER PRESS PRICE
    PRIME PRO PUBLIC QUEEN RADIO RANGE RATE REAL RECORD RED REPORT RIGHT RING RISE ROAD ROCK ROYAL
    RULE RUN SAFE SAGE SALE SAVE SCHOOL SCIENCE SEA SECOND SENSE SERIES SERVICE SET SHIP SHOP SHOW
    SIDE SIGN SITE SIZE SKY SMART SOFT SOUND SOURCE SOUTH SPACE SPECIAL SPEED SPORT SPOT SPRING
    SQUARE STAFF STAGE STAND STAR START STATE STATION STEEL STEP STOCK STONE STOP STORE STORY
    STREET STRONG STUDY STYLE SUGAR SUMMER SUN SUPER SURE SWEET SYSTEM TABLE TEAM TECH TELE TIME
    TOWER TOWN TRADE TRAIN TRAVEL TRIBE TRIP TRUE TRUST TYPE UNION UNIT UPPER VALUE VIEW VILLAGE
    VOICE WALL WATER WAVE WEEK WEST WIDE WILD WIND WINE WING WIRE WISE WOLF WOMAN WOND WORLD WORTH
    YEAR YORK YOUNG ZONE""".split()
)

#: China writes numeric registrations behind a single-letter prefix (B-1234).
TAIL_CHINA_RE = re.compile(r"(?<![A-Za-z0-9-])B-(?P<body>[0-9]{4,5}[A-Z]?)(?![A-Za-z0-9])")

#: US FAA registrations are the one family written without a hyphen
#: (``N97GA``, ``N123AB``, ``N4MC``). Restricted to the real FAA shape.
US_TAIL_RE = re.compile(r"(?<![A-Za-z0-9])N(?P<body>[1-9][0-9]{0,4}[A-Z]{0,2})(?![A-Za-z0-9a-z])")

#: Flight number, e.g. BA286, EK 137, SU2731, "flight BA286".
FLIGHT_NUMBER_RE_STRICT = re.compile(
    r"(?<![A-Za-z0-9])(?:flight\s+)?(?P<code>[A-Z]{2}|\d[A-Z]|[A-Z]\d)\s?(?P<number>\d{1,4})(?![A-Za-z0-9])"
)

#: IMO number for merchant vessels.
IMO_NUMBER_RE = re.compile(r"\bIMO\s*(?:number\s*|no\.?\s*|:)?\s*(?P<digits>\d{7})\b", re.IGNORECASE)

#: Maritime Mobile Service Identity.
MMSI_RE = re.compile(r"\bMMSI\s*(?:number\s*|no\.?\s*|:)?\s*(?P<digits>\d{9})\b", re.IGNORECASE)

#: Vessel name with a prefix: "MV Baltic Leader", "M/T Grace Ferrari".
VESSEL_PREFIX_RE = re.compile(
    rf"(?<![A-Za-z0-9])(?P<prefix>{'|'.join(re.escape(p) for p in VESSEL_PREFIXES if p != 'IMO')})\.?\s+"
    r"(?P<name>[A-Z][A-Za-z0-9'\u00c0-\u024f.\-]*(?:\s+[A-Z][A-Za-z0-9'\u00c0-\u024f.\-]*){0,4})"
)

#: Unambiguous software/product names. A vessel prefix followed by one of these
#: is always a false positive ("MS Windows", "MV Android").
VESSEL_NAME_BLOCKLIST: frozenset[str] = frozenset(
    """WINDOWS OFFICE WORD EXCEL OUTLOOK TEAMS XBOX PLAYSTATION ANDROID IOS LINUX DOS
    UNIX CHROME SAFARI FIREFOX OPERA VISTA SQL AZURE KUBERNETES""".split()
)

#: Words that are legitimate vessel names on their own (*SS Enterprise*,
#: *MT Digital Horizon*) but read as a product when every word of the name comes
#: from this set ("Enterprise Software", "Global Network Systems") or when the
#: single word follows the Microsoft-ambiguous ``MS``/``M/S`` prefix.
VESSEL_NAME_PHRASE_BLOCKLIST: frozenset[str] = frozenset(
    """ENTERPRISE SERVER SYSTEMS SOLUTIONS TECHNOLOGIES SOFTWARE NETWORK DIGITAL
    ONLINE GLOBAL PROFESSIONAL EDGE PLATFORM SERVICES CLOUD""".split()
)

#: Tokens that make a multi-word name a product regardless of the prefix. Ships
#: are named "Digital Horizon"; nothing afloat is named "Horizon Software".
VESSEL_NAME_STRONG_PRODUCT_WORDS: frozenset[str] = frozenset(
    """SOFTWARE SYSTEMS SOLUTIONS TECHNOLOGIES PLATFORM CLOUD SERVICES SERVER
    EDITION SUITE LICENCE LICENSE INSTALL UPDATE DATABASE""".split()
)

#: Prefixes that double as a software vendor ("MS Windows", "M/S Enterprise").
MICROSOFT_AMBIGUOUS_PREFIXES: frozenset[str] = frozenset({"MS", "M/S", "M.S"})


def imo_checksum_valid(digits: str) -> bool:
    """Validate an IMO number's check digit (IMO Resolution A.1117(30)).

    The final digit must equal the units digit of the first six digits weighted
    7,6,5,4,3,2. ``9074729`` passes; ``9074728`` does not.
    """
    cleaned = "".join(ch for ch in str(digits) if ch.isdigit())
    if len(cleaned) != 7:
        return False
    total = sum(int(digit) * weight for digit, weight in zip(cleaned[:6], (7, 6, 5, 4, 3, 2)))
    return total % 10 == int(cleaned[6])


def _is_product_name(name: str, prefix: str) -> bool:
    """True when a vessel-prefixed name is really a software product.

    ``SS Enterprise`` and ``MT Digital Horizon`` are ships; ``SS Enterprise
    Software``, ``MT Global Network Systems`` and ``MS Enterprise`` are not.
    """
    words = [word.upper().strip(".,") for word in name.split() if word.strip()]
    if not words:
        return True
    if words[0] in VESSEL_NAME_BLOCKLIST:
        return True

    microsoft_ambiguous = prefix.upper().replace(".", "/") in {
        candidate.upper().replace(".", "/") for candidate in MICROSOFT_AMBIGUOUS_PREFIXES
    }
    if len(words) == 1:
        return microsoft_ambiguous and words[0] in VESSEL_NAME_PHRASE_BLOCKLIST

    if any(word in VESSEL_NAME_STRONG_PRODUCT_WORDS for word in words):
        return True
    # Every word drawn from the product vocabulary ("Global Network Systems") is
    # a product; one ordinary word alongside it ("Digital Horizon") is a ship.
    return all(word in VESSEL_NAME_PHRASE_BLOCKLIST or word in VESSEL_NAME_BLOCKLIST for word in words)

#: Tokens that look like flight numbers but are airframe models or products.
FLIGHT_NUMBER_BLOCKLIST: frozenset[str] = frozenset(
    """A380 A350 A340 A330 A321 A320 A319 A318 A220 A310 A300 A130 A140 A150 A160 A170 A180 A190
    B737 B747 B757 B767 B777 B787 B707 B717 B727 B720 B730 B740 B750 B760 B770 B780 B790
    C130 C135 C145 C150 C160 C170 C180 C190 D810 D812 E135 E145 E170 E175 E190 E195 F100 F145 F170 F190
    PS4 PS5 PS6 X360 X750 MD11 MD80 MD90 Q400 Q500 A100 H145 H160 S76 S92 U60 M17 M18""".split()
)

#: Vehicle identification number (17 chars, no I/O/Q).
VIN_RE = re.compile(r"\b(?![A-Z]*[IOQ])(?=[A-HJ-NPR-Z0-9]{17}\b)[A-HJ-NPR-Z0-9]{17}\b")

#: Words that look like a proper name after a craft type but are identifier
#: keywords ("the vessel IMO 9074728", "aircraft Registration 9H-VUC").
CRAFT_APPOSITIVE_NAME_BLOCKLIST: frozenset[str] = frozenset(
    """IMO MMSI REGISTRATION REGISTERED NUMBER NO ID IDENT IDENTIFIER TYPE CLASS
    MODEL NAME NAMED CALLED KNOWN DESIGNATED SERIAL TAIL CALLSIGN FLAG FLAGGED
    BUILT OWNED OPERATED CHARTERED UNDER WITH FROM NEAR UNKNOWN UNIDENTIFIED
    THAT WHICH WAS WERE IS ARE HAD HAS""".split()
)

#: Generic "named craft" trigger: <type> <ProperName>, e.g. "yacht Amadea".
CRAFT_APPOSITIVE_RE = re.compile(
    r"\b(?P<type>superyacht|megayacht|yacht|vessel|tanker|ship|barge|trawler|icebreaker|"
    r"ferry|cruiser|aircraft|jet|airplane|aeroplane|plane|helicopter|drone|limousine|"
    r"motorcade|convoy)\s+(?P<name>[A-Z][A-Za-z0-9'\u00c0-\u024f.\-]{2,40}(?:\s+[A-Z][A-Za-z0-9'\u00c0-\u024f.\-]{2,40}){0,3})\b"
)

_MIN_CONFIDENCE = {
    "aircraft_registration": 0.95,
    "imo_number": 0.97,
    "mmsi": 0.95,
    "flight_number": 0.7,
    "vessel_prefixed": 0.9,
    "aircraft_type": 0.85,
    "vessel_type": 0.8,
    "ground_craft": 0.75,
    "craft_appositive": 0.8,
    "vin": 0.9,
}


@dataclass(frozen=True)
class CraftMatch:
    """One detected craft mention."""

    text: str
    kind: str
    start: int
    end: int
    confidence: float = 0.9
    properties: dict[str, Any] = field(default_factory=dict)

    @property
    def craft_class(self) -> str:
        if self.kind in {"aircraft_registration", "flight_number", "aircraft_type"}:
            return "Aircraft"
        if self.kind in {"imo_number", "mmsi", "vessel_prefixed", "vessel_type"}:
            return "Vessel"
        if self.kind in {"ground_craft", "vin"}:
            return "GroundVehicle"
        return "Craft"

    def registry_country(self) -> str | None:
        return self.properties.get("registry_country")


def _is_contained(start: int, end: int, matches: "list[CraftMatch] | tuple[CraftMatch, ...]") -> bool:
    """True when ``[start, end)`` is fully inside an already-accepted match.

    Partially overlapping candidates are *kept* here and resolved later by
    :func:`_dedupe_overlaps`, which prefers the longest surface form — that is
    what lets ``superyacht Amadea`` win over the bare ``superyacht`` gazetteer
    hit while still suppressing nested duplicates.
    """
    return any(m.start <= start and m.end >= end for m in matches)


def _registry_country(prefix: str) -> str | None:
    prefix = (prefix or "").strip().upper()
    if not prefix:
        return None
    for candidate in (prefix, prefix[:3], prefix[:2], prefix[:1]):
        if candidate in REGISTRY_PREFIX_COUNTRY:
            return REGISTRY_PREFIX_COUNTRY[candidate]
    return None


def _build_gazetteer_regex(terms: Iterable[str], kind: str) -> re.Pattern[str]:
    escaped = sorted({re.escape(term) for term in terms if term}, key=len, reverse=True)
    return re.compile(rf"(?<![A-Za-z])(?:{'|'.join(escaped)})(?![A-Za-z])", re.IGNORECASE if kind == "vessel_type" else 0)


class CraftDetector:
    """Stateless-ish detector; build once per process and reuse."""

    def __init__(self, extra_terms: Sequence[str] = ()) -> None:
        self.extra_terms = tuple(t.strip() for t in extra_terms if t and t.strip())
        all_aircraft = AIRCRAFT_TYPE_GAZETTEER + self.extra_terms
        self._aircraft_re = _build_gazetteer_regex(all_aircraft, "aircraft_type")
        self._vessel_type_re = _build_gazetteer_regex(VESSEL_TYPE_GAZETTEER, "vessel_type")
        self._ground_re = _build_gazetteer_regex(GROUND_CRAFT_GAZETTEER, "ground_craft")
        self._cache: dict[int, tuple[CraftMatch, ...]] = {}

    # ------------------------------------------------------------------ #
    def iter_matches(self, text: str) -> Iterator[CraftMatch]:
        """Yield every craft mention in ``text`` in offset order."""
        if not text:
            return
        cache_key = hash(text) if len(text) < 20_000 else None
        if cache_key is not None and cache_key in self._cache:
            yield from self._cache[cache_key]
            return

        found: list[CraftMatch] = []

        # 1. Flight numbers (scanned first: airline-code gated, high precision) -----
        for match in FLIGHT_NUMBER_RE_STRICT.finditer(text):
            code = match.group("code").upper()
            if code not in AIRLINE_IATA_CODES:
                continue
            compact = f"{code}{match.group('number')}"
            if compact in FLIGHT_NUMBER_BLOCKLIST:
                continue
            if _is_contained(match.start(), match.end(), found):
                continue
            found.append(
                CraftMatch(
                    text=f"{code}{match.group('number')}",
                    kind="flight_number",
                    start=match.start(),
                    end=match.end(),
                    confidence=_MIN_CONFIDENCE["flight_number"],
                    properties={
                        "craft_kind": "Aircraft",
                        "identifier_class": "flight_number",
                        "airline_code": code,
                        "flight_number": match.group("number"),
                        "display_name": f"flight {code}{match.group('number')}",
                    },
                )
            )

        # 2. Aircraft registrations ------------------------------------
        def _registration(prefix: str, body: str, start: int, end: int, confidence: float) -> None:
            if prefix == "N":
                surface = f"N{body}"
            elif "-" in prefix:
                surface = f"{prefix}{body}"
            else:
                surface = f"{prefix}-{body}"
            found.append(
                CraftMatch(
                    text=surface,
                    kind="aircraft_registration",
                    start=start,
                    end=end,
                    confidence=confidence,
                    properties={
                        "craft_kind": "Aircraft",
                        "identifier_class": "icao_registration",
                        # `text`/`registration` are the normalised ICAO form so
                        # the same airframe merges across documents; `surface`
                        # keeps exactly what the article said, for evidence.
                        "registration": surface,
                        "surface": text[start:end],
                        "registry_prefix": prefix,
                        "registry_country": REGISTRY_PREFIX_COUNTRY.get(prefix, ""),
                        "display_name": surface,
                    },
                )
            )

        for match in TAIL_MULTI_RE.finditer(text):
            if _is_contained(match.start(), match.end(), found):
                continue
            prefix = (match.group("prefix") or "").upper()
            body = (match.group("body") or "").upper()
            if prefix not in REGISTRY_PREFIX_COUNTRY:
                continue
            surface_len = len(prefix) + len(body) + (0 if "-" in prefix else 1)
            # Locale tags (PT-BR, EN-US) and short codes are 5 chars or fewer;
            # every real registration we care about is 6 or more.
            if surface_len < 6:
                continue
            _registration(prefix, body, match.start(), match.end(), _MIN_CONFIDENCE["aircraft_registration"])

        for match in TAIL_SINGLE_RE.finditer(text):
            if _is_contained(match.start(), match.end(), found):
                continue
            prefix = match.group("prefix").upper()
            body = match.group("body").upper()
            if prefix not in SINGLE_CHAR_TAIL_PREFIXES or prefix not in REGISTRY_PREFIX_COUNTRY:
                continue
            if not 3 <= len(body) <= 5:
                continue
            if body in TAIL_BODY_BLOCKLIST or any(body.split("-")[0] == w for w in ()):
                continue
            confidence = _MIN_CONFIDENCE["aircraft_registration"] - (0.1 if body.isalpha() else 0.0)
            _registration(prefix, body, match.start(), match.end(), confidence)

        for match in US_TAIL_RE.finditer(text):
            if _is_contained(match.start(), match.end(), found):
                continue
            body = match.group("body")
            if len(body) < 2:
                continue
            _registration("N", body, match.start(), match.end(), _MIN_CONFIDENCE["aircraft_registration"] - 0.05)

        for match in TAIL_CHINA_RE.finditer(text):
            if _is_contained(match.start(), match.end(), found):
                continue
            body = match.group("body")
            if not body.isdigit() and len(body) < 5:
                continue
            _registration("B", body, match.start(), match.end(), _MIN_CONFIDENCE["aircraft_registration"])

        # 3. IMO / MMSI ---------------------------------------------------
        for match in IMO_NUMBER_RE.finditer(text):
            digits = match.group("digits")
            # IMO numbers carry a check digit; validating it removes the whole
            # class of "any 7 digits after the word IMO" false positives.
            if not imo_checksum_valid(digits):
                continue
            found.append(
                CraftMatch(
                    text=match.group(0).strip(),
                    kind="imo_number",
                    start=match.start(),
                    end=match.end(),
                    confidence=_MIN_CONFIDENCE["imo_number"],
                    properties={"craft_kind": "Vessel", "identifier_class": "imo", "imo_number": digits},
                )
            )
        for match in MMSI_RE.finditer(text):
            found.append(
                CraftMatch(
                    text=match.group(0).strip(),
                    kind="mmsi",
                    start=match.start(),
                    end=match.end(),
                    confidence=_MIN_CONFIDENCE["mmsi"],
                    properties={"craft_kind": "Vessel", "identifier_class": "mmsi", "mmsi": match.group("digits")},
                )
            )

        # 4. Prefixed vessel names ----------------------------------------
        for match in VESSEL_PREFIX_RE.finditer(text):
            if _is_contained(match.start(), match.end(), found):
                continue
            name = match.group("name").strip(" .,-")
            if name.lower().startswith("the "):
                name = name[4:].strip()
            if not name or len(name) < 3 or name.upper() in {"THE", "OF", "AND", "AND THE"}:
                continue
            if _is_product_name(name, match.group("prefix")):
                continue
            found.append(
                CraftMatch(
                    text=f"{match.group('prefix')} {name}".strip(),
                    kind="vessel_prefixed",
                    start=match.start(),
                    end=match.end(),
                    confidence=_MIN_CONFIDENCE["vessel_prefixed"],
                    properties={"craft_kind": "Vessel", "identifier_class": "name", "vessel_prefix": match.group("prefix"), "vessel_name": name},
                )
            )

        # 5. Gazetteers ----------------------------------------------------
        for regex, kind, craft_kind, prop_key in (
            (self._aircraft_re, "aircraft_type", "Aircraft", "aircraft_type"),
            (self._vessel_type_re, "vessel_type", "Vessel", "vessel_type"),
            (self._ground_re, "ground_craft", "GroundVehicle", "vehicle_type"),
        ):
            for match in regex.finditer(text):
                if _is_contained(match.start(), match.end(), found):
                    continue
                found.append(
                    CraftMatch(
                        text=match.group(0).strip(),
                        kind=kind,
                        start=match.start(),
                        end=match.end(),
                        confidence=_MIN_CONFIDENCE[kind],
                        properties={"craft_kind": craft_kind, "identifier_class": "type", prop_key: match.group(0).strip()},
                    )
                )

        # 6. "the yacht Amadea" appositives --------------------------------
        for match in CRAFT_APPOSITIVE_RE.finditer(text):
            if _is_contained(match.start(), match.end(), found):
                continue
            name = match.group("name").strip(" .,-")
            craft_type = match.group("type").lower()
            craft_kind = "Vessel" if craft_type in {"superyacht", "megayacht", "yacht", "vessel", "tanker", "ship", "barge", "trawler", "icebreaker", "ferry", "cruiser"} else (
                "Aircraft" if craft_type in {"aircraft", "jet", "airplane", "aeroplane", "plane", "helicopter", "drone"} else "GroundVehicle"
            )
            if not name or name.lower() in {"the", "a", "an", "of"}:
                continue
            # "the vessel IMO 9074728" must not yield a craft named *IMO*: an
            # identifier keyword is never a name, whatever follows it.
            if name.split()[0].upper().strip(".,:") in CRAFT_APPOSITIVE_NAME_BLOCKLIST:
                continue
            found.append(
                CraftMatch(
                    text=f"{craft_type} {name}",
                    kind="craft_appositive",
                    start=match.start(),
                    end=match.end(),
                    confidence=_MIN_CONFIDENCE["craft_appositive"],
                    properties={
                        "craft_kind": craft_kind,
                        "identifier_class": "name",
                        "craft_type": craft_type,
                        "craft_name": name,
                        "display_name": name,
                    },
                )
            )

        # 7. VINs -----------------------------------------------------------
        for match in VIN_RE.finditer(text):
            if _is_contained(match.start(), match.end(), found):
                continue
            found.append(
                CraftMatch(
                    text=match.group(0),
                    kind="vin",
                    start=match.start(),
                    end=match.end(),
                    confidence=_MIN_CONFIDENCE["vin"],
                    properties={"craft_kind": "GroundVehicle", "identifier_class": "vin", "vin": match.group(0)},
                )
            )

        found.sort(key=lambda m: (m.start, -m.confidence))
        deduped = _dedupe_overlaps(found)
        if cache_key is not None:
            self._cache[cache_key] = tuple(deduped)
        yield from deduped

    def find_all(self, text: str) -> list[CraftMatch]:
        return list(self.iter_matches(text))

    # ------------------------------------------------------------------ #
    def spacy_patterns(self) -> list[dict[str, Any]]:
        """EntityRuler patterns (phrase + token-regex) for a spaCy pipeline."""
        patterns: list[dict[str, Any]] = []

        # Deliberately *no* bare type-gazetteer patterns ("superyacht",
        # "military transport aircraft"): a type is not an identity, and letting
        # the EntityRuler assert one creates graph nodes named "superyacht" that
        # shadow the real craft ("superyacht Amadea"). Types still contribute
        # through find_all(), which powers the appositive and craft_type
        # properties, and through the vessel-prefix patterns below.

        # Tail numbers as a single-token regex, plus hyphenated two-token forms.
        patterns.append(
            {
                "label": "CRAFT",
                "id": "aircraft_registration",
                "pattern": [
                    {"ORTH": {"REGEX": rf"^(?:{_PREFIX_ALTERNATION})$"}},
                    {"ORTH": "-"},
                    {"ORTH": {"REGEX": "^[A-Z0-9]{3,5}$"}},
                ],
            }
        )
        patterns.append(
            {
                "label": "CRAFT",
                "id": "aircraft_registration_us",
                "pattern": [{"ORTH": {"REGEX": r"^N[1-9][0-9]{0,4}[A-Z]{0,2}$"}}],
            }
        )
        patterns.append(
            {
                "label": "CRAFT",
                "id": "imo_number",
                "pattern": [{"LOWER": "imo"}, {"ORTH": {"REGEX": r"^\d{7}$"}}],
            }
        )
        # Flight numbers are gated on assigned IATA codes: an ungated
        # two-letter-plus-digits pattern matches "ZZ123", "No 5" and half the
        # product codes in a news article.
        airline_alternation = "|".join(sorted(AIRLINE_IATA_CODES))
        patterns.append(
            {
                "label": "CRAFT",
                "id": "flight_number",
                "pattern": [{"ORTH": {"REGEX": rf"^(?:{airline_alternation})$"}}, {"ORTH": {"REGEX": r"^\d{1,4}$"}}],
            }
        )
        patterns.append(
            {
                "label": "CRAFT",
                "id": "mmsi",
                "pattern": [{"LOWER": "mmsi"}, {"ORTH": {"REGEX": r"^\d{9}$"}}],
            }
        )
        for prefix in VESSEL_PREFIXES:
            if prefix == "IMO":
                continue
            patterns.append(
                {
                    "label": "CRAFT",
                    "id": "vessel_prefixed",
                    "pattern": [{"ORTH": prefix}, {"IS_ASCII": True, "IS_TITLE": True}],
                }
            )
        return patterns

    # ------------------------------------------------------------------ #
    def annotate(self, doc: Any) -> Any:
        """Inject CRAFT spans into a spaCy ``Doc`` (regex path, overlap-safe)."""
        try:
            from spacy.tokens import Span
        except ImportError:  # pragma: no cover
            return doc

        text = doc.text
        new_ents: list[Any] = []
        existing = list(getattr(doc, "ents", ()) or ())
        for match in self.iter_matches(text):
            # Snap to token boundaries: spaCy rejects spans that split tokens.
            start_token = _token_index_at(doc, match.start, side="start")
            end_token = _token_index_at(doc, match.end, side="end")
            if start_token is None or end_token is None or end_token <= start_token:
                continue
            overlaps = any(
                start_token < ent.end and end_token > ent.start for ent in existing
            )
            if overlaps:
                continue
            span = Span(doc, start_token, end_token, label="CRAFT")
            try:  # extension is registered by NLPEngine when spaCy is present
                span._.craft_props = dict(match.properties)
            except (AttributeError, ValueError):
                pass
            new_ents.append(span)

        if new_ents:
            try:
                doc.set_ents(existing + new_ents, default="unmodified")
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("could not set CRAFT entities: %s", exc)
        return doc


def _token_index_at(doc: Any, char_offset: int, *, side: str) -> int | None:
    for index, token in enumerate(doc):
        if side == "start" and token.idx <= char_offset < token.idx + len(token.text):
            return index
        if side == "end":
            if token.idx < char_offset <= token.idx + len(token.text):
                return index + 1
            if token.idx >= char_offset:
                return index
    return len(doc) if side == "end" else None


def _dedupe_overlaps(matches: Sequence[CraftMatch]) -> list[CraftMatch]:
    """Keep the longest/highest-confidence match from each overlapping cluster."""
    ordered = sorted(matches, key=lambda m: (m.start, -(m.end - m.start), -m.confidence))
    kept: list[CraftMatch] = []
    for candidate in ordered:
        overlapping = [k for k in kept if candidate.start < k.end and candidate.end > k.start]
        if not overlapping:
            kept.append(candidate)
            continue
        best = max(overlapping, key=lambda k: ((k.end - k.start), k.confidence))
        if (candidate.end - candidate.start, candidate.confidence) > (best.end - best.start, best.confidence):
            kept = [k for k in kept if k is not best]
            kept.append(candidate)
    kept.sort(key=lambda m: m.start)
    return kept


def summarise(matches: Iterable[CraftMatch]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for match in matches:
        counts[match.kind] = counts.get(match.kind, 0) + 1
    return counts
