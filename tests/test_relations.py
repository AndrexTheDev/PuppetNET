"""Relation-mapper tests: verb / noun / preposition rules and canonicalisation."""

from __future__ import annotations

import logging

import pytest

from puppetnet.models import EntityType, RelationType
from puppetnet.parsing.relations import (
    DIRECTIONAL_TRANSFER_VERBS,
    NOUN_RULES,
    PREPOSITION_RULES,
    SYMMETRIC_RELATIONS,
    VERB_RULES,
    RelationMapper,
)

logging.disable(logging.CRITICAL)

P = EntityType.PERSON
O = EntityType.ORGANIZATION
L = EntityType.LOCATION
C = EntityType.CRAFT


@pytest.fixture(scope="module")
def mapper() -> RelationMapper:
    return RelationMapper()


# --------------------------------------------------------------------------- #
# Rule-table hygiene
# --------------------------------------------------------------------------- #
def test_rule_tables_are_populated():
    assert len(VERB_RULES) >= 30
    assert len(NOUN_RULES) >= 15
    assert len(PREPOSITION_RULES) >= 8
    assert all(rule.predicate in RelationType for rule in VERB_RULES + NOUN_RULES + PREPOSITION_RULES)


def test_every_rule_predicate_is_inside_the_closed_vocabulary():
    for rule in VERB_RULES + NOUN_RULES + PREPOSITION_RULES:
        assert RelationType.coerce(rule.predicate) is rule.predicate


def test_evidence_and_priority_ranges():
    for rule in VERB_RULES + NOUN_RULES + PREPOSITION_RULES:
        assert 0.0 < rule.evidence <= 1.0
        assert 0 <= rule.priority <= 10


def test_symmetric_relations_can_be_flipped():
    assert RelationType.MET_WITH in SYMMETRIC_RELATIONS
    assert RelationType.TRAVELED_WITH in SYMMETRIC_RELATIONS
    assert RelationType.FAMILY_OF in SYMMETRIC_RELATIONS
    # Directional predicates must never be treated as symmetric.
    assert RelationType.OWNS not in SYMMETRIC_RELATIONS
    assert RelationType.DIRECTOR_OF not in SYMMETRIC_RELATIONS


# --------------------------------------------------------------------------- #
# Verb rules — the spec's named relation types
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "verb,subject_type,object_type,expected",
    [
        ("own", P, O, RelationType.OWNS),
        ("owns", P, O, RelationType.OWNS),
        ("hold", P, O, RelationType.OWNS),
        ("control", O, O, RelationType.CONTROLS),
        ("acquire", O, O, RelationType.ACQUIRED),
        ("bought", O, C, RelationType.ACQUIRED),
        ("found", P, O, RelationType.FOUNDED),
        ("invest", P, O, RelationType.SHAREHOLDER_OF),
        ("employ", O, P, RelationType.EMPLOYS),
        ("work", P, O, RelationType.EMPLOYED_BY),
        ("fund", P, O, RelationType.FUNDED),
        ("pay", O, O, RelationType.PAID_TO),
        ("meet", P, P, RelationType.MET_WITH),
        ("met", P, P, RelationType.MET_WITH),
        ("visit", P, P, RelationType.MET_WITH),
        ("travel", P, L, RelationType.TRAVELED_TO),
        ("sail", C, L, RelationType.TRAVELED_TO),
        ("fly", C, L, RelationType.TRAVELED_TO),
        ("land", C, L, RelationType.TRAVELED_TO),
        ("depart", C, L, RelationType.ARRIVED_FROM),
        ("board", P, C, RelationType.TRAVELED_WITH),
        ("accompany", P, P, RelationType.TRAVELED_WITH),
        ("operate", O, C, RelationType.OPERATES),
        ("charter", O, C, RelationType.OPERATES),
        ("register", C, L, RelationType.REGISTERED_IN),
        ("headquarter", O, L, RelationType.LOCATED_IN),
        ("sanction", O, P, RelationType.SANCTIONED_BY),
        ("investigate", O, P, RelationType.INVESTIGATED_BY),
        ("accuse", O, P, RelationType.ACCUSED_OF),
        ("marry", P, P, RelationType.FAMILY_OF),
        ("link", O, O, RelationType.LINKED_OFFSHORE),
    ],
)
def test_verb_rules(mapper, verb, subject_type, object_type, expected):
    decision = mapper.from_verb(verb, subject_type, object_type)
    assert decision.predicate is expected, f"{verb}: got {decision.predicate}"
    assert decision.matched_on == "verb"
    assert decision.evidence > 0.6


def test_adversarial_rules_reverse_direction(mapper):
    """SANCTIONED_BY / INVESTIGATED_BY read 'X sanctioned Y' as Y ← X."""
    for verb in ("sanction", "investigate", "accuse"):
        decision = mapper.from_verb(verb, O, P)
        assert decision.reverse is True, verb


def test_passive_verb_loses_a_little_evidence(mapper):
    active = mapper.from_verb("acquire", O, O)
    passive = mapper.from_verb("acquire", O, O, passive=True)
    assert passive.predicate is RelationType.ACQUIRED
    assert passive.evidence < active.evidence
    assert passive.evidence == pytest.approx(active.evidence * 0.95, abs=1e-6)


def test_hedged_sentence_discounts_evidence(mapper):
    plain = mapper.from_verb("own", P, O, sentence="Kerimov owns Midea Holdings.")
    hedged = mapper.from_verb("own", P, O, sentence="Kerimov allegedly owns Midea Holdings.")
    assert hedged.hedged is True and plain.hedged is False
    assert hedged.evidence == pytest.approx(plain.evidence * 0.75, abs=1e-6)


def test_negated_sentence_is_flagged(mapper):
    decision = mapper.from_verb("own", P, O, sentence="Kerimov does not own Midea.")
    assert decision.negated is True


def test_unknown_verb_falls_back_to_a_type_default(mapper):
    decision = mapper.from_verb("frobnicate", P, O)
    assert decision.matched_on == "type_default"
    assert decision.predicate is RelationType.AFFILIATED_WITH
    assert decision.evidence == pytest.approx(0.6)
    assert "frobnicate" in decision.rule_note


# --------------------------------------------------------------------------- #
# Directional transfer verbs
# --------------------------------------------------------------------------- #
def test_sold_without_a_direction_is_not_asserted(mapper):
    decision = mapper.from_verb("sold", O, O)
    assert decision.matched_on == "type_default"


def test_sold_with_a_direction_becomes_transferred_to(mapper):
    assert "sold" in DIRECTIONAL_TRANSFER_VERBS
    decision = mapper.from_verb("sold", O, O, preposition="to")
    assert decision.predicate is RelationType.TRANSFERRED_TO
    assert decision.matched_on == "verb"
    assert decision.rule_note == "verb+prep:to"
    assert decision.evidence == pytest.approx(0.85)


@pytest.mark.parametrize("verb", ["sell", "sold", "divest", "export", "ship", "convey", "cede", "hand"])
def test_transfer_verbs_are_direction_gated(mapper, verb):
    assert verb in DIRECTIONAL_TRANSFER_VERBS
    assert mapper.from_verb(verb, O, O, preposition="to").predicate is RelationType.TRANSFERRED_TO


# --------------------------------------------------------------------------- #
# Noun rules — copular / appositional readings
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "noun,subject_type,object_type,expected",
    [
        ("director", P, O, RelationType.DIRECTOR_OF),
        ("chairman", P, O, RelationType.DIRECTOR_OF),
        ("board member", P, O, RelationType.DIRECTOR_OF),
        ("trustee", P, O, RelationType.DIRECTOR_OF),
        ("ceo", P, O, RelationType.OFFICER_OF),
        ("president", P, O, RelationType.OFFICER_OF),
        ("owner", P, O, RelationType.OWNS),
        ("beneficial owner", P, O, RelationType.OWNS),
        ("shareholder", P, O, RelationType.OWNS),
        ("founder", P, O, RelationType.FOUNDED),
        ("subsidiary", O, O, RelationType.SUBSIDIARY_OF),
        ("parent company", O, O, RelationType.PARENT_OF),
        ("intermediary", P, O, RelationType.INTERMEDIARY_FOR),
        ("shell company", O, O, RelationType.INTERMEDIARY_FOR),
        ("employee", P, O, RelationType.EMPLOYED_BY),
        ("lawyer", P, O, RelationType.EMPLOYED_BY),
        ("member", P, O, RelationType.MEMBER_OF),
        ("senator", P, O, RelationType.MEMBER_OF),
        ("wife", P, P, RelationType.FAMILY_OF),
        ("brother", P, P, RelationType.FAMILY_OF),
        ("partner", O, O, RelationType.AFFILIATED_WITH),
        ("national", P, L, RelationType.NATIONAL_OF),
        ("citizen", P, L, RelationType.NATIONAL_OF),
        ("headquarters", O, L, RelationType.LOCATED_IN),
        ("registered office", O, L, RelationType.LOCATED_IN),
        ("operator", O, C, RelationType.OPERATES),
        ("sanctions", O, P, RelationType.SANCTIONED_BY),
        ("associate", P, P, RelationType.ASSOCIATED_WITH),
    ],
)
def test_noun_rules(mapper, noun, subject_type, object_type, expected):
    decision = mapper.from_noun(noun, subject_type, object_type)
    assert decision.predicate is expected, f"{noun}: got {decision.predicate}"


def test_board_is_directorship_only_for_organisations(mapper):
    """"boarded the Amadea" is travel; "board of Gazprom" is governance."""
    assert mapper.from_noun("board", P, O).predicate is RelationType.DIRECTOR_OF
    assert mapper.from_noun("board", P, C).predicate is RelationType.TRAVELED_WITH


def test_partner_is_affiliation_between_organisations_not_kinship(mapper):
    assert mapper.from_noun("partner", O, O).predicate is RelationType.AFFILIATED_WITH
    assert mapper.from_noun("partner", P, P).predicate is not RelationType.FAMILY_OF


def test_parent_company_reverses(mapper):
    decision = mapper.from_noun("parent company", O, O)
    assert decision.predicate is RelationType.PARENT_OF
    assert decision.reverse is True


def test_unknown_noun_uses_type_default(mapper):
    decision = mapper.from_noun("widget", O, O)
    assert decision.matched_on == "type_default"
    assert decision.evidence == pytest.approx(0.55)


# --------------------------------------------------------------------------- #
# Preposition rules
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "preposition,subject_type,object_type,expected",
    [
        ("in", O, L, RelationType.REGISTERED_IN),
        ("from", C, L, RelationType.ARRIVED_FROM),
        ("to", P, L, RelationType.TRAVELED_TO),
        ("with", P, C, RelationType.TRAVELED_WITH),
        ("aboard", P, C, RelationType.TRAVELED_WITH),
        ("of", P, O, RelationType.MEMBER_OF),
        ("for", P, O, RelationType.EMPLOYED_BY),
    ],
)
def test_preposition_rules(mapper, preposition, subject_type, object_type, expected):
    decision = mapper.from_preposition(preposition, subject_type, object_type)
    assert decision.predicate is expected
    assert decision.matched_on == "preposition"


def test_unknown_preposition_falls_back(mapper):
    decision = mapper.from_preposition("notwithstanding", O, O)
    assert decision.matched_on == "type_default"


# --------------------------------------------------------------------------- #
# Co-occurrence fallback (the 0.2-penalty route)
# --------------------------------------------------------------------------- #
def test_cooccurrence_decays_with_distance(mapper):
    near = mapper.cooccurrence(P, O, distance=1)
    far = mapper.cooccurrence(P, O, distance=20)
    assert near.matched_on == "cooccurrence"
    assert near.evidence > far.evidence
    assert 0.0 < far.evidence <= near.evidence <= 1.0


def test_cooccurrence_uses_type_aware_defaults(mapper):
    assert mapper.cooccurrence(C, L, distance=2).predicate is RelationType.TRAVELED_TO
    assert mapper.cooccurrence(P, C, distance=2).predicate is RelationType.TRAVELED_WITH
    assert mapper.cooccurrence(O, L, distance=2).predicate is RelationType.LOCATED_IN
    assert mapper.cooccurrence(P, P, distance=2).predicate is RelationType.MET_WITH
    assert mapper.cooccurrence(P, O, distance=2).predicate is RelationType.ASSOCIATED_WITH


def test_cooccurrence_evidence_floors_at_max_distance(mapper):
    """Beyond the window the decay saturates: 0.7 is the weakest co-occurrence.

    Combined with the mandated 0.2 penalty a news-sourced co-occurrence edge
    therefore lands at 0.4 × 0.8 × 0.7 = 0.224 — above MIN_EDGE_CONFIDENCE but
    far below any dependency-parsed edge.
    """
    decision = mapper.cooccurrence(P, O, distance=200, max_distance=30)
    assert decision.evidence == pytest.approx(0.7)
    at_the_edge = mapper.cooccurrence(P, O, distance=30, max_distance=30)
    assert at_the_edge.evidence == pytest.approx(0.7)
    assert 0.4 * 0.8 * decision.evidence == pytest.approx(0.224, abs=1e-3)


# --------------------------------------------------------------------------- #
# Canonicalisation
# --------------------------------------------------------------------------- #
def test_symmetric_predicate_is_key_ordered(mapper):
    left, right, predicate, flipped = mapper.canonicalise("PERSON:zzz", "PERSON:aaa", RelationType.MET_WITH)
    assert (left, right) == ("PERSON:aaa", "PERSON:zzz")
    assert flipped is True
    assert predicate is RelationType.MET_WITH


def test_directional_predicate_is_never_flipped(mapper):
    left, right, predicate, flipped = mapper.canonicalise("PERSON:zzz", "ORGANIZATION:aaa", RelationType.OWNS)
    assert (left, right) == ("PERSON:zzz", "ORGANIZATION:aaa")
    assert flipped is False


def test_canonicalisation_is_idempotent(mapper):
    once = mapper.canonicalise("PERSON:zzz", "PERSON:aaa", RelationType.FAMILY_OF)
    twice = mapper.canonicalise(once[0], once[1], once[2])
    assert (once[0], once[1]) == (twice[0], twice[1])
    assert twice[3] is False


# --------------------------------------------------------------------------- #
# Verb normalisation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "surface,expected",
    [
        ("owns", "own"),
        ("owned", "own"),
        ("OWNED", "own"),
        ("was acquired by", "acquir"),
        ("sailed", "sail"),
        ("  met  ", "met"),
        ("traveled to", "travel"),
        ("is owned by", "own"),
    ],
)
def test_normalize_verb(mapper, surface, expected):
    assert mapper.normalize_verb(surface) == expected


def test_normalize_verb_leaves_doubled_consonants_for_the_candidate_stage(mapper):
    """"travelling" stems to "travell"; collapsing is a separate, additive step."""
    assert mapper.normalize_verb("travelling") == "travell"
    assert mapper.collapse_doubled_consonant("travell") == "travel"
    assert mapper.collapse_doubled_consonant("runn") == "run"
    # Never collapse a stem that is not doubled: "falling" must stay intact.
    assert mapper.collapse_doubled_consonant("falling") == "falling"
    assert mapper.collapse_doubled_consonant("") == ""
    # And the pipeline end result is the British spelling matching the rule.
    assert mapper.from_verb("travelling", P, L).predicate is RelationType.TRAVELED_TO


def test_control_is_not_ownership(mapper):
    """Nominee structures: controlling a company is not owning it."""
    assert mapper.from_verb("controls", P, O).predicate is RelationType.CONTROLS
    assert mapper.from_verb("control", O, O).predicate is RelationType.CONTROLS
    assert mapper.from_verb("controlled", O, C).predicate is RelationType.CONTROLS
    assert mapper.from_verb("owns", P, O).predicate is RelationType.OWNS
    assert mapper.from_verb("holds", P, O).predicate is RelationType.OWNS


def test_accusation_direction_matches_sanctions(mapper):
    for verb in ("accuse", "charged", "indict", "prosecute", "blame"):
        decision = mapper.from_verb(verb, O, P)
        assert decision.reverse is True, verb
        assert decision.predicate in {
            RelationType.ACCUSED_OF,
            RelationType.SANCTIONED_BY,
            RelationType.INVESTIGATED_BY,
        }


def test_describe_reports_rule_counts(mapper):
    summary = mapper.describe()
    assert summary["verb_rules"] == len(VERB_RULES)
    assert summary["noun_rules"] == len(NOUN_RULES)
    assert summary["preposition_rules"] == len(PREPOSITION_RULES)
    assert summary["predicates"] > 0


def test_extra_rules_can_be_injected():
    from puppetnet.parsing.relations import RelationRule

    custom = RelationRule(
        RelationType.MEMBER_OF,
        verbs=frozenset({"enlist", "enlisted"}),
        subject_types=frozenset({P}),
        object_types=frozenset({O}),
        evidence=0.91,
        priority=9,
        note="test rule",
    )
    mapper = RelationMapper(extra_rules=[custom])
    decision = mapper.from_verb("enlist", P, O)
    assert decision.predicate is RelationType.MEMBER_OF
    assert decision.evidence == pytest.approx(0.91)
    assert mapper.describe()["verb_rules"] == len(VERB_RULES) + 1
