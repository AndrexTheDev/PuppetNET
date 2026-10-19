"""CRAFT detector tests — aircraft, vessels, flights, IMO/MMSI, ground craft.

CRAFT is the vehicle/aircraft entity class the OSINT graph needs for sanctions
and movement tracking, and it is the noisiest to detect: tail numbers collide
with flight numbers, and vessel prefixes collide with product names. Every case
below is a real-world false positive or true positive observed while tuning the
gazetteers.
"""

from __future__ import annotations

import logging

import pytest

from puppetnet.models import EntityType
from puppetnet.parsing.craft import (
    AIRLINE_IATA_CODES,
    FLIGHT_NUMBER_BLOCKLIST,
    REGISTRY_PREFIX_COUNTRY,
    VESSEL_NAME_BLOCKLIST,
    VESSEL_PREFIXES,
    CraftDetector,
    _is_product_name,
    imo_checksum_valid,
)

logging.disable(logging.CRITICAL)


@pytest.fixture(scope="module")
def detector() -> CraftDetector:
    return CraftDetector()


def kinds(matches) -> list[str]:
    return [m.kind for m in matches]


def texts(matches) -> list[str]:
    return [m.text for m in matches]


def only(detector, text: str):
    return detector.find_all(text)


# --------------------------------------------------------------------------- #
# Aircraft registrations (ICAO / national)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "registration,country",
    [
        ("9H-VUC", "Malta"),           # the Amadea-linked business jet
        ("RA-67895", "Russia"),
        ("N97GA", "United States"),
        ("VP-CGY", "Cayman Islands"),
        ("G-EUPA", "United Kingdom"),
        ("B-1234", "China"),
    ],
)
def test_aircraft_registrations_and_their_registry(detector, registration, country):
    matches = only(detector, f"Aircraft {registration} landed in Malta yesterday.")
    assert texts(matches) == [registration]
    match = matches[0]
    assert match.kind == "aircraft_registration"
    assert match.properties["craft_kind"] == "Aircraft"
    assert match.properties["registry_country"] == country
    assert match.confidence >= 0.85


def test_registry_prefix_table_is_well_formed():
    assert REGISTRY_PREFIX_COUNTRY["9H"] == "Malta"
    assert REGISTRY_PREFIX_COUNTRY["N"] == "United States"
    # Every country must be a non-empty string; no sentinel values.
    assert all(isinstance(country, str) and country for country in REGISTRY_PREFIX_COUNTRY.values())


# --------------------------------------------------------------------------- #
# Flight numbers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("flight,airline", [("BA286", "BA"), ("EK137", "EK"), ("SU213", "SU")])
def test_flight_numbers(detector, flight, airline):
    matches = only(detector, f"Flight {flight} arrived from Dubai.")
    assert texts(matches) == [flight]
    assert matches[0].kind == "flight_number"
    assert matches[0].properties["airline_code"] == airline
    assert matches[0].confidence == pytest.approx(0.7)


def test_flight_numbers_are_gated_on_real_airline_codes(detector):
    # ZZ is not an assigned IATA code, so "ZZ123" is never a flight number.
    matches = only(detector, "Reference ZZ123 was filed.")
    assert "flight_number" not in kinds(matches)
    # (It does read as the Zimbabwean tail Z-Z123, which is correct: single-letter
    #  prefixes are gated on the registry table, and the raw surface is preserved.)
    registration = [m for m in matches if m.kind == "aircraft_registration"]
    assert registration and registration[0].properties["surface"] == "ZZ123"
    assert registration[0].properties["registry_country"] == "Zimbabwe"
    assert "BA" in AIRLINE_IATA_CODES and "EK" in AIRLINE_IATA_CODES


def test_flight_scan_precedes_registration_scan(detector):
    """EK137 must not also be re-claimed as the Armenian tail EK-137."""
    matches = only(detector, "Flight BA286 and EK137 were diverted.")
    assert kinds(matches) == ["flight_number", "flight_number"]
    assert "EK-137" not in texts(matches)


def test_airframe_models_are_not_flights(detector):
    assert "A380" in FLIGHT_NUMBER_BLOCKLIST
    matches = only(detector, "The Airbus A380 and Boeing 737 were involved.")
    assert kinds(matches) == ["aircraft_type", "aircraft_type"]
    assert texts(matches) == ["Airbus A380", "Boeing 737"]


# --------------------------------------------------------------------------- #
# Vessel prefixes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("prefix", ["MV", "M/V", "M-V", "MT", "M/T", "MS", "SS", "S.S.", "RV", "R/V", "R-V", "FV", "MY", "M-Y", "USS", "HMS", "TS", "NV"])
def test_vessel_prefixes_are_recognised(detector, prefix):
    assert prefix in VESSEL_PREFIXES
    matches = only(detector, f"{prefix} Ocean Star sailed from Malta.")
    assert kinds(matches) == ["vessel_prefixed"]
    assert matches[0].properties["craft_kind"] == "Vessel"
    assert matches[0].confidence == pytest.approx(0.9)


def test_multiword_vessel_names(detector):
    matches = only(detector, "MT Sea Diamond docked at Istanbul.")
    assert texts(matches) == ["MT Sea Diamond"]


# --------------------------------------------------------------------------- #
# Vessel prefix vs. software product (the false-positive minefield)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "MS Windows is an operating system.",
        "MS Office ships with Outlook.",
        "MV Android rolled out to devices.",
        "SS Enterprise Software launched a platform.",
        "MT Global Network Systems expanded.",
        "MS Enterprise edition is licensed.",
        "PS5 and Xbox are consoles.",
    ],
)
def test_product_names_are_not_vessels(detector, text):
    assert only(detector, text) == []


@pytest.mark.parametrize(
    "text",
    ["SS Enterprise sailed", "MV Enterprise docked", "MT Digital Horizon sailed"],
)
def test_genuine_vessel_names_survive_the_blocklist(detector, text):
    """A single ambiguous word is only a product under the MS/M-S prefix."""
    matches = only(detector, text)
    assert kinds(matches) == ["vessel_prefixed"], text


def test_product_name_helper():
    assert _is_product_name("Windows", "MS") is True
    assert _is_product_name("Enterprise Software", "SS") is True
    assert _is_product_name("Enterprise", "MS") is True     # Microsoft reading wins
    assert _is_product_name("Enterprise", "SS") is False    # a real motor ship
    assert _is_product_name("Amadea", "MV") is False
    assert _is_product_name("", "MV") is True


def test_blocklists_do_not_overlap_product_names_and_vessel_words():
    assert "WINDOWS" in VESSEL_NAME_BLOCKLIST
    assert "ENTERPRISE" not in VESSEL_NAME_BLOCKLIST  # phrase-level only


# --------------------------------------------------------------------------- #
# IMO / MMSI
# --------------------------------------------------------------------------- #
def test_imo_number(detector):
    # 9074729: 9·7 + 0·6 + 7·5 + 4·4 + 7·3 + 2·2 = 139 → check digit 9.
    matches = only(detector, "The vessel IMO 9074729 was tracked near Fiji.")
    imo = [m for m in matches if m.kind == "imo_number"]
    assert imo and imo[0].properties["imo_number"] == "9074729"
    assert imo[0].confidence >= 0.95


@pytest.mark.parametrize("digits", ["9074728", "9187404", "1234560", "0000001", "9999999", "12345"])
def test_imo_checksum_rejects_invalid_numbers(digits):
    assert imo_checksum_valid(digits) is False


@pytest.mark.parametrize("digits", ["9074729", "9187409"])
def test_imo_checksum_accepts_valid_numbers(digits):
    assert imo_checksum_valid(digits) is True


def test_invalid_imo_does_not_leak_into_an_appositive(detector):
    # A rejected IMO must not reappear as a craft literally named "IMO".
    matches = only(detector, "The vessel IMO 9074728 was tracked.")
    assert matches == []


def test_mmsi(detector):
    matches = only(detector, "MMSI 249111000 belongs to a Maltese vessel.")
    mmsi = [m for m in matches if m.kind == "mmsi"]
    assert mmsi and mmsi[0].properties["mmsi"] == "249111000"


# --------------------------------------------------------------------------- #
# Appositives: "superyacht Amadea"
# --------------------------------------------------------------------------- #
def test_craft_appositive(detector):
    matches = only(detector, "The superyacht Amadea was seized in Fiji.")
    assert kinds(matches) == ["craft_appositive"]
    assert matches[0].properties["craft_type"] == "superyacht"
    assert matches[0].properties["craft_name"] == "Amadea"
    assert matches[0].properties["display_name"] == "Amadea"


def test_appositive_does_not_swallow_following_sentences(detector):
    matches = only(detector, "The yacht Amadea was seized. Kerimov denied ownership.")
    assert texts(matches) == ["yacht Amadea"]


# --------------------------------------------------------------------------- #
# Non-craft text must stay clean
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text",
    [
        "Interstate I-95 was closed near Baltimore.",
        "D-Day commemorations drew crowds.",
        "The G-20 summit opened in Delhi.",
        "Documentation is available in pt-BR and en-US.",
        "E-commerce grew by 12 percent last quarter.",
        "Kerimov owns Midea Holdings Ltd, registered in the BVI.",
        "Section 230 protects platforms from liability.",
    ],
)
def test_no_false_positives_on_ordinary_text(detector, text):
    assert only(detector, text) == []


# --------------------------------------------------------------------------- #
# Contract with the rest of the package
# --------------------------------------------------------------------------- #
def test_matches_are_ordered_and_non_overlapping(detector):
    text = "MV Amadea carried aircraft 9H-VUC and flight BA286 from Malta."
    matches = only(detector, text)
    assert matches == sorted(matches, key=lambda m: m.start)
    for earlier, later in zip(matches, matches[1:], strict=False):
        assert earlier.end <= later.start, "craft matches must not overlap"


def test_spacy_patterns_are_valid_entity_ruler_entries(detector):
    patterns = detector.spacy_patterns()
    assert patterns, "the CRAFT gazetteer must produce EntityRuler patterns"
    for pattern in patterns[:50]:
        assert isinstance(pattern, dict)
        assert pattern.get("label") == "CRAFT"
        assert "pattern" in pattern


def test_craft_entity_type_maps_to_the_graph_label(detector):
    match = only(detector, "MV Amadea sailed")[0]
    assert EntityType.from_spacy("CRAFT") is EntityType.CRAFT
    assert match.properties["craft_kind"] in {"Vessel", "Aircraft", "Ground", "Unknown"}
