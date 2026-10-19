"""The OSINT label vocabulary is a model contract, not a suggestion."""

from __future__ import annotations

import pytest

from nlp.labels import (
    DATE_LABEL,
    IDENTIFIER_LABELS,
    OSINT_LABELS,
    canonical_label,
    entity_type_for_label,
    graph_label,
    is_graph_label,
    properties_for_label,
)
from puppetnet.models import EntityType


def test_osint_labels_are_exactly_the_documented_nine():
    assert OSINT_LABELS == (
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
    assert len(set(OSINT_LABELS)) == 9


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Person", "Person"),
        ("person", "Person"),
        ("ORGANISATION", "Organization"),
        ("organization", "Organization"),
        ("aircraft-registration", "Aircraft_Registration"),
        ("aircraft registration", "Aircraft_Registration"),
        ("tail_number", "Aircraft_Registration"),
        ("imo", "IMO_Number"),
        ("PER", "Person"),
        ("IMO number", "IMO_Number"),
        ("iban", "Bank_Account"),
        ("wallet", "Crypto_Address"),
        ("gpe", "Location"),
        ("vessel", "Vehicle"),
    ],
)
def test_aliases_fold_to_the_documented_spelling(raw, expected):
    assert canonical_label(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "Email", "Phone_Number", "Money", "Spaceship"])
def test_unknown_labels_are_rejected_without_a_guess(raw):
    assert canonical_label(raw) is None


def test_entity_type_mapping_keeps_span_only_labels_out_of_the_graph():
    assert entity_type_for_label("Person") is EntityType.PERSON
    assert entity_type_for_label("Organization") is EntityType.ORGANIZATION
    assert entity_type_for_label("Location") is EntityType.LOCATION
    assert entity_type_for_label("Vehicle") is EntityType.CRAFT
    assert entity_type_for_label("Aircraft_Registration") is EntityType.CRAFT
    assert entity_type_for_label("IMO_Number") is EntityType.CRAFT
    # A Date and an account number cannot be represented by the current node
    # schema; they stay span metadata rather than becoming fake entities.
    assert entity_type_for_label(DATE_LABEL) is EntityType.UNKNOWN
    assert entity_type_for_label("Bank_Account") is EntityType.UNKNOWN
    assert entity_type_for_label("Crypto_Address") is EntityType.UNKNOWN
    assert entity_type_for_label("nonsense") is EntityType.UNKNOWN


def test_graph_labels_match_the_nlp_engine_vocabulary():
    assert graph_label("Person") == "PERSON"
    assert graph_label("Organization") == "ORG"
    assert graph_label("Location") == "GPE"
    assert graph_label("Vehicle") == "CRAFT"
    assert graph_label("IMO_Number") == "CRAFT"
    assert graph_label("Date") == "DATE"
    assert graph_label("Bank_Account") == "ACCOUNT"
    assert is_graph_label("Person")
    assert is_graph_label("IMO_Number")
    assert not is_graph_label("Date")
    assert not is_graph_label("Crypto_Address")


def test_properties_record_the_span_label_and_identifier_flag():
    person = properties_for_label("person")
    assert person == {"osint_label": "Person", "entity_type": "Person"}
    assert "identifier" not in person
    account = properties_for_label("iban")
    assert account["identifier"] is True
    assert account["osint_label"] == "Bank_Account"
    assert properties_for_label("Date")["date_mention"] is True
    assert properties_for_label("unknown") == {}
    assert IDENTIFIER_LABELS == {
        "Aircraft_Registration",
        "IMO_Number",
        "Bank_Account",
        "Crypto_Address",
    }
