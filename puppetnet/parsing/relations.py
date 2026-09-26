"""Relation typing: from a parsed triple to a bounded graph predicate.

The mapper is deliberately *rule-based and auditable*. A learned relation
extractor would need labelled OSINT data we do not have, and an unexplainable
edge in an intelligence graph is worse than a missing one. Each rule declares:

* the lexical triggers (verb lemmas / nominal heads / prepositions),
* the entity-type constraints that must hold for the rule to fire,
* whether the surface direction has to be flipped to reach the canonical
  graph direction (``X was sanctioned by Y`` → ``Y SANCTIONED_BY X``),
* a base evidence score that becomes the third confidence factor.

Hedging (``allegedly``, ``may``, ``reportedly``) and negation are detected here
because they are lexical/syntactic properties of the trigger, not of the graph.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from ..models import EntityType, RelationType

__all__ = [
    "RelationRule",
    "RelationDecision",
    "RelationMapper",
    "VERB_RULES",
    "NOUN_RULES",
    "PREPOSITION_RULES",
    "SYMMETRIC_RELATIONS",
    "HEDGE_CUES",
    "NEGATION_CUES",
]

# --------------------------------------------------------------------------- #
# Lexicons
# --------------------------------------------------------------------------- #

#: Relations whose direction carries no information — canonicalised by sorting
#: the endpoint keys so ``(A,B)`` and ``(B,A)`` merge into one edge.
SYMMETRIC_RELATIONS: frozenset[RelationType] = frozenset(
    {
        RelationType.MET_WITH,
        RelationType.FAMILY_OF,
        RelationType.TRAVELED_WITH,
        RelationType.ASSOCIATED_WITH,
        RelationType.AFFILIATED_WITH,
        RelationType.CONTRACTED_WITH,
        RelationType.LINKED_OFFSHORE,
    }
)

HEDGE_CUES: frozenset[str] = frozenset(
    (
        "alleged allegedly reported reportedly claimed purported purportedly supposedly "
        "apparently may might could would presumably rumored rumoured suspected believed "
        "understood speculated unverified unconfirmed"
    ).split()
)

NEGATION_CUES: frozenset[str] = frozenset(
    "not no never neither nor n't cannot cant dont doesnt didnt isnt arent wasnt werent "
    "denied denies deny refute refutes refuted rejected rejects dismiss dismisses "
    "disputed disputes contradicts lacks without absent free".split()
)

#: Verb lemma → relation rules, ordered by specificity.
@dataclass(frozen=True)
class RelationRule:
    """One lexical trigger → one graph predicate."""

    predicate: RelationType
    verbs: frozenset[str] = frozenset()
    nouns: frozenset[str] = frozenset()
    prepositions: frozenset[str] = frozenset()
    #: Allowed subject types (empty = any).
    subject_types: frozenset[EntityType] = frozenset()
    #: Allowed object types (empty = any).
    object_types: frozenset[EntityType] = frozenset()
    #: Flip surface direction to reach the canonical graph direction.
    reverse: bool = False
    #: Base evidence score in (0, 1].
    evidence: float = 1.0
    #: Higher wins when several rules match the same triple.
    priority: int = 0
    note: str = ""

    def matches_types(self, subject_type: EntityType, object_type: EntityType) -> bool:
        if self.subject_types and subject_type not in self.subject_types:
            return False
        if self.object_types and object_type not in self.object_types:
            return False
        return True


_P = EntityType.PERSON
_O = EntityType.ORGANIZATION
_L = EntityType.LOCATION
_C = EntityType.CRAFT
_ANY: frozenset[EntityType] = frozenset()

VERB_RULES: tuple[RelationRule, ...] = (
    # -- ownership & control ------------------------------------------------
    RelationRule(RelationType.OWNS, verbs=frozenset({"own", "owns", "owned", "owning", "hold", "holds", "held", "possess", "possesses"}), subject_types=frozenset({_P, _O}), object_types=frozenset({_O, _C, _L}), evidence=0.98, priority=6),
    RelationRule(RelationType.SHAREHOLDER_OF, verbs=frozenset({"stake", "invest", "invests", "invested", "subscribe", "underwrite"}), subject_types=frozenset({_P, _O}), object_types=frozenset({_O}), evidence=0.9, priority=5),
    RelationRule(RelationType.OWNS, verbs=frozenset({"hold", "holds", "held"}), object_types=frozenset({_C}), evidence=0.9, priority=4, note="holding a craft"),
    # Control is not ownership: a person who "controls" a company may hold no
    # shares (nominee structures), so this gets its own predicate and outranks
    # the ownership rule rather than sharing its verb list.
    RelationRule(RelationType.CONTROLS, verbs=frozenset({"control", "controls", "controlled", "dominate", "dominates", "direct", "directs", "run", "runs", "manage", "manages", "managed", "head", "heads", "headed", "lead", "leads", "led", "chair", "chairs", "chaired"}), subject_types=frozenset({_P, _O}), object_types=frozenset({_O, _C}), evidence=0.88, priority=7),
    RelationRule(RelationType.ACQUIRED, verbs=frozenset({"acquire", "acquires", "acquired", "buy", "buys", "bought", "purchase", "purchases", "purchased", "take over", "merges with", "absorb", "absorbs", "absorbed"}), subject_types=frozenset({_P, _O}), object_types=frozenset({_O, _C}), evidence=0.95, priority=7),
    RelationRule(RelationType.FOUNDED, verbs=frozenset({"found", "founds", "founded", "establish", "establishes", "established", "create", "creates", "created", "set up", "register", "registers", "registered", "incorporate", "incorporates", "incorporated", "launch", "launches", "launched", "form", "forms", "formed"}), subject_types=frozenset({_P, _O}), object_types=frozenset({_O}), evidence=0.95, priority=7),

    # -- governance & employment -------------------------------------------
    RelationRule(RelationType.DIRECTOR_OF, verbs=frozenset({"direct", "directs", "directed", "serve", "serves", "served", "sit", "sits", "sat"}), subject_types=frozenset({_P}), object_types=frozenset({_O}), evidence=0.85, priority=5, note="serves on / sits on a board"),
    RelationRule(RelationType.OFFICER_OF, verbs=frozenset({"officiate", "hold office", "hold", "holds"}), subject_types=frozenset({_P}), object_types=frozenset({_O}), evidence=0.8, priority=3),
    RelationRule(RelationType.APPOINTED_BY, verbs=frozenset({"appoint", "appoints", "appointed", "nominate", "nominates", "nominated", "name", "names", "named", "install", "installs", "installed", "designate", "designates", "designated", "elect", "elects", "elected", "promote", "promotes", "promoted", "hire", "hires", "hired", "recruit", "recruits", "recruited"}), subject_types=frozenset({_P, _O}), object_types=frozenset({_P}), reverse=True, evidence=0.9, priority=6),
    RelationRule(RelationType.EMPLOYED_BY, verbs=frozenset({"work", "works", "worked", "employ", "employs", "employed", "join", "joins", "joined", "staff", "staffs", "serve", "serves", "served", "consult", "consults", "consulted", "lobby", "lobbies", "lobbied"}), subject_types=frozenset({_P}), object_types=frozenset({_O}), evidence=0.85, priority=5),
    RelationRule(RelationType.EMPLOYS, verbs=frozenset({"employ", "employs", "employed", "hire", "hires", "hired", "recruit", "recruits", "recruited", "contract", "contracts", "contracted", "retain", "retains", "retained"}), subject_types=frozenset({_O}), object_types=frozenset({_P}), evidence=0.9, priority=6),
    RelationRule(RelationType.MEMBER_OF, verbs=frozenset({"join", "joins", "joined", "belong", "belongs", "member", "affiliate", "affiliated", "enrol", "enroll", "enrolled", "sign up"}), subject_types=frozenset({_P, _O}), object_types=frozenset({_O}), evidence=0.85, priority=5),

    # -- money flows --------------------------------------------------------
    RelationRule(RelationType.FUNDED, verbs=frozenset({"fund", "funds", "funded", "finance", "finances", "financed", "bankroll", "bankrolls", "bankrolled", "sponsor", "sponsors", "sponsored", "donate", "donates", "donated", "give", "gives", "gave", "grant", "grants", "granted", "lend", "lends", "lent", "loan", "loans", "loaned"}), subject_types=frozenset({_P, _O}), object_types=frozenset({_P, _O}), evidence=0.92, priority=6),
    RelationRule(RelationType.PAID_TO, verbs=frozenset({"pay", "pays", "paid", "transfer", "transfers", "transferred", "wire", "wires", "wired", "remit", "remits", "remitted", "send", "sends", "sent", "invoice", "invoices", "invoiced", "settle", "settles", "settled"}), evidence=0.85, priority=5),
    RelationRule(RelationType.CONTRACTED_WITH, verbs=frozenset({"contract", "contracts", "contracted", "sign", "signs", "signed", "award", "awards", "awarded", "procure", "procures", "procured", "tender", "tenders"}), evidence=0.85, priority=5),
    RelationRule(RelationType.TRANSFERRED_TO, verbs=frozenset({"transfer", "transfers", "transferred", "move", "moves", "moved", "shift", "shifts", "shifted", "relocate", "relocates", "relocated", "reassign", "reassigned", "hand over", "hands over", "handed over", "cede", "cedes", "ceded"}), evidence=0.8, priority=4),

    # -- geography & jurisdiction ------------------------------------------
    RelationRule(RelationType.LOCATED_IN, verbs=frozenset({"locate", "located", "base", "based", "headquarter", "headquartered", "situate", "situated", "operate from", "reside", "resides", "resided", "live", "lives", "lived", "domicile", "domiciled"}), object_types=frozenset({_L}), evidence=0.92, priority=6),
    RelationRule(RelationType.REGISTERED_IN, verbs=frozenset({"register", "registers", "registered", "incorporate", "incorporated", "file", "files", "filed", "flag", "flags", "flagged", "domicile"}), object_types=frozenset({_L}), evidence=0.95, priority=8),
    RelationRule(RelationType.NATIONAL_OF, verbs=frozenset({"naturalise", "naturalize", "born", "citizen"}), object_types=frozenset({_L}), evidence=0.85, priority=5),
    RelationRule(RelationType.OPERATES_IN, verbs=frozenset({"operate", "operates", "operated", "serve", "serves", "served", "fly to", "flies to", "sail to", "sails to", "expand into", "do business in"}), object_types=frozenset({_L}), evidence=0.85, priority=5),

    # -- movement (CRAFT-aware) --------------------------------------------
    RelationRule(RelationType.TRAVELED_TO, verbs=frozenset({"travel", "travels", "traveled", "travelled", "fly", "flies", "flew", "flown", "sail", "sails", "sailed", "arrive", "arrives", "arrived", "land", "lands", "landed", "visit", "visits", "visited", "go", "goes", "went", "journey", "journeys", "journeyed", "proceed", "proceeds", "proceeded", "return", "returns", "returned", "reach", "reaches", "reached", "docked at", "berthed at"}), object_types=frozenset({_L}), evidence=0.92, priority=7),
    RelationRule(RelationType.ARRIVED_FROM, verbs=frozenset({"depart", "departs", "departed", "leave", "leaves", "left", "fly from", "flies from", "sail from", "sails from", "take off from", "originated"}), object_types=frozenset({_L}), evidence=0.85, priority=6),
    RelationRule(RelationType.TRAVELED_WITH, verbs=frozenset({"accompany", "accompanies", "accompanied", "board", "boards", "boarded", "embark", "embarks", "embarked", "travel with", "travels with", "traveled with", "fly with", "flew with", "ride with", "rode with", "join", "joins", "joined"}), object_types=frozenset({_P, _C, _O}), evidence=0.88, priority=6),
    RelationRule(RelationType.OPERATES, verbs=frozenset({"operate", "operates", "operated", "fly", "flies", "flew", "sail", "sails", "sailed", "crew", "crews", "crewed", "pilot", "pilots", "piloted", "commandeer", "charter", "charters", "chartered", "lease", "leases", "leased", "deploy", "deploys", "deployed"}), object_types=frozenset({_C}), evidence=0.92, priority=8),
    RelationRule(RelationType.REGISTERED_TO, verbs=frozenset({"register", "registers", "registered", "flag", "flags", "flagged"}), object_types=frozenset({_C}), evidence=0.92, priority=7),

    # -- social & adversarial ----------------------------------------------
    RelationRule(RelationType.MET_WITH, verbs=frozenset({"meet", "meets", "met", "confer", "confers", "conferred", "talk with", "talks with", "negotiate with", "hold talks with", "host", "hosts", "hosted", "receive", "receives", "received", "visit", "visits", "visited", "call on", "calls on"}), subject_types=frozenset({_P, _O}), object_types=frozenset({_P, _O}), evidence=0.9, priority=7),
    RelationRule(RelationType.FAMILY_OF, verbs=frozenset({"marry", "marries", "married", "divorce", "father", "mother", "brother", "sister", "son", "daughter", "sibling", "spouse", "relative", "cousin", "nephew", "niece", "wed"}), evidence=0.85, priority=6),
    RelationRule(RelationType.SANCTIONED_BY, verbs=frozenset({"sanction", "sanctions", "sanctioned", "blacklist", "blacklists", "blacklisted", "designate", "designates", "designated", "ban", "bans", "banned", "restrict", "restricts", "restricted", "block", "blocks", "blocked", "freeze", "freezes", "froze", "frozen", "penalise", "penalize", "punish"}), object_types=frozenset({_P, _O, _C}), reverse=True, evidence=0.95, priority=8),
    RelationRule(RelationType.INVESTIGATED_BY, verbs=frozenset({"investigate", "investigates", "investigated", "probe", "probes", "probed", "examine", "examines", "examined", "scrutinise", "scrutinize", "audit", "audits", "audited", "search", "searches", "searched", "raid", "raids", "raided", "question", "questions", "questioned", "summon", "summons", "summoned"}), object_types=frozenset({_P, _O}), reverse=True, evidence=0.9, priority=7),
    # "The DOJ accused Kerimov" must land as (Kerimov)-[:ACCUSED_OF]->(DOJ),
    # matching SANCTIONED_BY / INVESTIGATED_BY, so the arguments are reversed.
    RelationRule(RelationType.ACCUSED_OF, verbs=frozenset({"accuse", "accuses", "accused", "charge", "charges", "charged", "indict", "indicts", "indicted", "suspect", "suspects", "suspected", "allege", "alleges", "alleged", "blame", "blames", "blamed", "convict", "convicts", "convicted", "sue", "sues", "sued", "prosecute", "prosecutes", "prosecuted"}), object_types=frozenset({_P, _O}), reverse=True, evidence=0.85, priority=6),
    RelationRule(RelationType.LINKED_OFFSHORE, verbs=frozenset({"link", "links", "linked", "tie", "ties", "tied", "connect", "connects", "connected", "associate", "associates", "associated", "relate", "relates", "related", "implicate", "implicates", "implicated", "reveal", "reveals", "revealed", "expose", "exposes", "exposed"}), evidence=0.8, priority=4),

    # -- generic ------------------------------------------------------------
    RelationRule(RelationType.AFFILIATED_WITH, verbs=frozenset({"affiliate", "affiliated", "ally", "allied", "partner", "partners", "partnered", "cooperate", "cooperates", "cooperated", "collaborate", "collaborates", "collaborated", "support", "supports", "supported", "back", "backs", "backed", "endorse", "endorses", "endorsed"}), evidence=0.85, priority=4),
    RelationRule(RelationType.ASSOCIATED_WITH, verbs=frozenset({"involve", "involves", "involved", "include", "includes", "included", "mention", "mentions", "mentioned", "name", "names", "named", "appear", "appears", "appeared", "list", "lists", "listed"}), evidence=0.7, priority=1),
)

#: Nominal heads → relation (apposition / copula / "X, owner of Y" patterns).
NOUN_RULES: tuple[RelationRule, ...] = (
    # "board" only denotes governance when the object is an organisation:
    # "Kerimov boarded the Amadea" is a CRAFT reading, not a directorship.
    RelationRule(RelationType.DIRECTOR_OF, nouns=frozenset({"director", "directors", "board member", "board", "chairman", "chairwoman", "chair", "chairperson", "non-executive director", "supervisory board", "trustee", "governor", "regent"}), subject_types=frozenset({_P, _O}), object_types=frozenset({_O}), evidence=0.9, priority=8),
    RelationRule(RelationType.OFFICER_OF, nouns=frozenset({"ceo", "chief executive", "cfo", "chief financial officer", "coo", "chief operating officer", "president", "vice president", "managing director", "executive", "officer", "manager", "head", "chief", "secretary", "treasurer", "chair"}), evidence=0.88, priority=7),
    RelationRule(RelationType.OWNS, nouns=frozenset({"owner", "owners", "proprietor", "beneficial owner", "beneficiary", "holder", "shareholder", "stockholder", "investor", "controller"}), evidence=0.92, priority=8),
    RelationRule(RelationType.FOUNDED, nouns=frozenset({"founder", "founders", "co-founder", "cofounder", "creator", "establisher"}), evidence=0.92, priority=8),
    RelationRule(RelationType.SUBSIDIARY_OF, nouns=frozenset({"subsidiary", "subsidiaries", "unit", "division", "arm", "affiliate", "branch", "wholly-owned subsidiary", "joint venture"}), evidence=0.9, priority=7),
    RelationRule(RelationType.PARENT_OF, nouns=frozenset({"parent", "parent company", "holding company", "holdings"}), reverse=True, evidence=0.88, priority=7),
    RelationRule(RelationType.EMPLOYED_BY, nouns=frozenset({"employee", "staff", "staffer", "worker", "personnel", "contractor", "consultant", "advisor", "adviser", "counsel", "lawyer", "accountant", "lobbyist", "fixer", "agent", "spokesman", "spokeswoman", "spokesperson"}), evidence=0.8, priority=5),
    RelationRule(RelationType.MEMBER_OF, nouns=frozenset({"member", "members", "party member", "delegate", "representative", "mp", "senator", "lawmaker", "politician"}), evidence=0.82, priority=5),
    # Kinship terms are person-to-person only. "Partner" is deliberately not
    # here: between organisations it means AFFILIATED_WITH ("Rosneft's partner").
    RelationRule(RelationType.FAMILY_OF, nouns=frozenset({"wife", "husband", "spouse", "son", "daughter", "brother", "sister", "father", "mother", "uncle", "aunt", "nephew", "niece", "cousin", "relative", "widow", "fiancé", "fiancée", "son-in-law", "daughter-in-law", "stepson", "stepdaughter"}), subject_types=frozenset({_P}), object_types=frozenset({_P}), evidence=0.9, priority=8),
    RelationRule(RelationType.AFFILIATED_WITH, nouns=frozenset({"partner", "partners", "business partner", "joint partner", "affiliate", "ally", "allies"}), subject_types=frozenset({_O, _P}), object_types=frozenset({_O}), evidence=0.8, priority=6),
    RelationRule(RelationType.NATIONAL_OF, nouns=frozenset({"national", "citizen", "resident", "passport holder", "dual national"}), subject_types=frozenset({_P}), object_types=frozenset({_L}), evidence=0.85, priority=6),
    RelationRule(RelationType.INTERMEDIARY_FOR, nouns=frozenset({"intermediary", "middleman", "broker", "proxy", "nominee", "front", "shell company", "shell firm", "offshore vehicle", "fixer", "facilitator", "agent", "law firm", "trustee"}), evidence=0.88, priority=7),
    RelationRule(RelationType.OPERATES, nouns=frozenset({"operator", "operators", "carrier", "airline", "shipowner", "ship owner", "manager", "flight", "vessel", "aircraft", "jet", "yacht"}), evidence=0.85, priority=6),
    RelationRule(RelationType.SHAREHOLDER_OF, nouns=frozenset({"stake", "share", "shares", "shareholding", "equity", "interest"}), evidence=0.85, priority=6),
    RelationRule(RelationType.LOCATED_IN, nouns=frozenset({"headquarters", "hq", "office", "offices", "address", "registered address", "registered office", "base", "residence", "domicile", "registry"}), evidence=0.85, priority=6),
    RelationRule(RelationType.SANCTIONED_BY, nouns=frozenset({"sanction", "sanctions", "blacklist", "denial list", "specially designated national", "sdn", "consolidated list"}), evidence=0.9, priority=7),
    RelationRule(RelationType.ASSOCIATED_WITH, nouns=frozenset({"associate", "associates", "ally", "allies", "colleague", "confidant", "friend", "aide", "aides", "associate of", "partner", "business partner"}), subject_types=frozenset({_P}), object_types=frozenset({_P}), evidence=0.75, priority=4),
    RelationRule(RelationType.ASSOCIATED_WITH, nouns=frozenset({"associate", "associates", "ally", "allies", "colleague", "confidant", "friend", "aide", "aides", "associate of"}), evidence=0.75, priority=3),
)

#: Preposition → relation for ``subject —prep→ object`` (no usable verb).
PREPOSITION_RULES: tuple[RelationRule, ...] = (
    RelationRule(RelationType.LOCATED_IN, prepositions=frozenset({"in", "at", "near", "inside", "within"}), object_types=frozenset({_L}), evidence=0.8, priority=5),
    RelationRule(RelationType.REGISTERED_IN, prepositions=frozenset({"in"}), subject_types=frozenset({_O, _C}), object_types=frozenset({_L}), evidence=0.82, priority=6),
    RelationRule(RelationType.MEMBER_OF, prepositions=frozenset({"of", "in"}), subject_types=frozenset({_P}), object_types=frozenset({_O}), evidence=0.7, priority=4),
    RelationRule(RelationType.TRAVELED_WITH, prepositions=frozenset({"with", "alongside", "together with", "aboard", "on board", "on"}), object_types=frozenset({_P, _C}), evidence=0.82, priority=6),
    RelationRule(RelationType.TRAVELED_TO, prepositions=frozenset({"to", "into", "toward", "towards", "for"}), object_types=frozenset({_L}), evidence=0.85, priority=6),
    RelationRule(RelationType.ARRIVED_FROM, prepositions=frozenset({"from", "out of"}), object_types=frozenset({_L}), evidence=0.8, priority=5),
    RelationRule(RelationType.EMPLOYED_BY, prepositions=frozenset({"for", "at", "with"}), subject_types=frozenset({_P}), object_types=frozenset({_O}), evidence=0.75, priority=5),
    RelationRule(RelationType.AFFILIATED_WITH, prepositions=frozenset({"with", "alongside"}), evidence=0.7, priority=3),
    RelationRule(RelationType.OWNS, prepositions=frozenset({"of"}), subject_types=frozenset({_P, _O}), object_types=frozenset({_O, _C}), evidence=0.6, priority=2, note="possessive 'of'"),
)

#: Verbs whose canonical reading depends on a directional complement:
#: "Gazprom sold Yugansk *to* Rosneft" is TRANSFERRED_TO, while a bare
#: "Gazprom sold Yugansk" is left to the type default rather than asserted.
DIRECTIONAL_TRANSFER_VERBS: frozenset[str] = frozenset(
    """sell sells sold selling divest divests divested offload offloads offloaded
    export exports exported ship ships shipped convey conveys conveyed assign
    assigns assigned transfer transfers transferred move moves moved relocate
    relocates relocated cede cedes ceded hand hands handed""".split()
)

_HEDGE_RE = re.compile(rf"\b({'|'.join(sorted(HEDGE_CUES))})\b", re.IGNORECASE)
_NEGATION_RE = re.compile(r"\b(not|never|no|neither|nor|n't|denied|denies|deny|refuted?|reject(?:s|ed)?|without|absent|dispute[ds]?)\b", re.IGNORECASE)


@dataclass(frozen=True)
class RelationDecision:
    """Outcome of mapping one syntactic triple to a graph predicate."""

    predicate: RelationType
    reverse: bool = False
    evidence: float = 1.0
    rule_note: str = ""
    hedged: bool = False
    negated: bool = False
    matched_on: str = "verb"

    @property
    def usable(self) -> bool:
        return not self.negated


class RelationMapper:
    """Turn ``(subject, verb/prep/noun, object)`` into a typed, scored edge."""

    def __init__(self, extra_rules: Iterable[RelationRule] = ()) -> None:
        self.verb_rules: tuple[RelationRule, ...] = tuple(sorted(VERB_RULES + tuple(extra_rules), key=lambda r: -r.priority))
        self.noun_rules: tuple[RelationRule, ...] = tuple(sorted(NOUN_RULES, key=lambda r: -r.priority))
        self.preposition_rules: tuple[RelationRule, ...] = tuple(sorted(PREPOSITION_RULES, key=lambda r: -r.priority))

    # ------------------------------------------------------------------ #
    _AUX_RE = re.compile(
        r"^(?:be|been|being|am|is|are|was|were|have|has|had|do|does|did|will|would|shall|should|"
        r"can|could|may|might|must|get|gets|got|become|becomes|became|remain|remains|remained)\s+"
    )
    _TRAILING_PREP_RE = re.compile(r"\s+(?:by|to|of|in|for|with|from|on|at|into|upon|up|out|over|through)$")

    @classmethod
    def normalize_verb(cls, lemma: str, surface: str = "") -> str:
        """Reduce a verb phrase to its lexicon key.

        Strips auxiliaries (``is owned by`` → ``owned``) and trailing
        particles/prepositions (``traveled to`` → ``travel``), then applies
        regular morphological stripping so unseen inflections still match.
        """
        text = re.sub(r"\s+", " ", (lemma or surface or "").strip().lower())
        while True:
            stripped = cls._AUX_RE.sub("", text)
            if stripped == text:
                break
            text = stripped
        text = cls._TRAILING_PREP_RE.sub("", text).strip()
        for suffix, replacement in (("ies", "y"), ("ied", "y"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
            if text.endswith(suffix) and len(text) - len(suffix) >= 3:
                stem = text[: -len(suffix)] + replacement
                return stem if stem else text
        return text

    @staticmethod
    def collapse_doubled_consonant(stem: str) -> str:
        """Undo the doubling that ``-ing`` stripping leaves behind.

        ``travelling`` → ``travell`` → ``travel``, ``running`` → ``runn`` →
        ``run``. Only applied as an *extra* candidate, never as a replacement:
        collapsing ``falling`` would give the non-word ``fal``, so the
        uncollapsed stem stays in play alongside it.
        """
        text = (stem or "").strip().lower()
        if len(text) >= 4 and text[-1] == text[-2] and text[-1].isalpha() and text[-1] not in "aeiou":
            return text[:-1]
        return text

    def from_verb(
        self,
        verb_lemma: str,
        subject_type: EntityType,
        object_type: EntityType,
        *,
        sentence: str = "",
        passive: bool = False,
        phrasal: Sequence[str] = (),
        preposition: str = "",
    ) -> RelationDecision:
        """Map a subject-verb-object triple."""
        raw = (verb_lemma or "").strip().lower()
        head = raw.split()[0] if raw else ""
        particles = " ".join(str(p).lower() for p in phrasal).strip()
        candidates: list[str] = []
        for candidate in (
            raw,
            self.normalize_verb(raw),
            self.collapse_doubled_consonant(self.normalize_verb(raw)),
            head,
            self.normalize_verb(head),
            self.collapse_doubled_consonant(self.normalize_verb(head)),
            particles,
            f"{raw} {particles}".strip(),
            self.normalize_verb(f"{raw} {particles}".strip()),
            f"{self.normalize_verb(head)} {particles}".strip(),
            *[f"{self.normalize_verb(head)} {p}" for p in particles.split()],
        ):
            candidate = candidate.strip()
            if candidate and candidate not in candidates:
                candidates.append(candidate)
        # Longest (most specific) phrasal forms first.
        candidates.sort(key=len, reverse=True)

        hedge = bool(sentence) and bool(_HEDGE_RE.search(sentence))
        negated = bool(sentence) and bool(_NEGATION_RE.search(sentence))

        best: RelationRule | None = None
        for rule in self.verb_rules:
            if not rule.verbs:
                continue
            if not any(c in rule.verbs for c in candidates):
                continue
            if not rule.matches_types(subject_type, object_type):
                continue
            if best is None or rule.priority > best.priority:
                best = rule
            if best is not None and best.priority >= 7:
                break

        # "Gazprom sold Yugansk *to* Rosneft": a transfer verb plus a
        # directional complement is a TRANSFERRED_TO edge. Without the
        # complement the direction is genuinely unknown, so the type default
        # applies instead of asserting something the sentence does not say.
        if preposition in {"to", "into", "toward", "towards"} and any(
            candidate in DIRECTIONAL_TRANSFER_VERBS for candidate in candidates
        ) and (best is None or best.priority < 5):
            evidence = 0.85
            if hedge:
                evidence *= 0.75
            return RelationDecision(
                predicate=RelationType.TRANSFERRED_TO,
                reverse=False,
                evidence=round(max(0.05, min(1.0, evidence)), 4),
                rule_note=f"verb+prep:{preposition}",
                hedged=hedge,
                negated=negated,
                matched_on="verb",
            )

        if best is None:
            # Type-driven fallback: an entity pair with no recognised verb still
            # gets a weak generic edge so the graph keeps its structure.
            predicate = self._type_default(subject_type, object_type)
            return RelationDecision(
                predicate=predicate,
                reverse=False,
                evidence=0.6,
                rule_note=f"no-lexical-rule:{candidates[0] if candidates else 'unknown'}",
                hedged=hedge,
                negated=negated,
                matched_on="type_default",
            )

        evidence = best.evidence
        if passive:
            # The caller is expected to have already restored the active
            # argument order (agent → patient) for full passives; what is left
            # is the reduced reliability of "by"-phrase attachment.
            evidence *= 0.95
        if hedge:
            evidence *= 0.75
        return RelationDecision(
            predicate=best.predicate,
            reverse=bool(best.reverse),
            evidence=round(max(0.05, min(1.0, evidence)), 4),
            rule_note=best.note or f"verb:{best.predicate.value.lower()}",
            hedged=hedge,
            negated=negated,
            matched_on="verb",
        )

    def from_noun(self, noun_lemma: str, subject_type: EntityType, object_type: EntityType, *, sentence: str = "", reverse_hint: bool = False) -> RelationDecision:
        """Map an appositional / nominal relation (``X, director of Y``)."""
        key = (noun_lemma or "").strip().lower()
        hedge = bool(sentence) and bool(_HEDGE_RE.search(sentence))
        negated = bool(sentence) and bool(_NEGATION_RE.search(sentence))
        for rule in self.noun_rules:
            if key in rule.nouns and rule.matches_types(subject_type, object_type):
                return RelationDecision(
                    predicate=rule.predicate,
                    reverse=bool(rule.reverse) or reverse_hint,
                    evidence=round(max(0.05, rule.evidence * (0.75 if hedge else 1.0)), 4),
                    rule_note=f"noun:{key}",
                    hedged=hedge,
                    negated=negated,
                    matched_on="noun",
                )
        return RelationDecision(
            predicate=self._type_default(subject_type, object_type),
            evidence=0.55,
            rule_note=f"no-noun-rule:{key}",
            hedged=hedge,
            negated=negated,
            matched_on="type_default",
        )

    def from_preposition(self, preposition: str, subject_type: EntityType, object_type: EntityType, *, sentence: str = "") -> RelationDecision:
        """Map a ``subject —prep→ object`` relation with no usable verb."""
        key = (preposition or "").strip().lower()
        hedge = bool(sentence) and bool(_HEDGE_RE.search(sentence))
        negated = bool(sentence) and bool(_NEGATION_RE.search(sentence))
        for rule in self.preposition_rules:
            if key in rule.prepositions and rule.matches_types(subject_type, object_type):
                return RelationDecision(
                    predicate=rule.predicate,
                    reverse=bool(rule.reverse),
                    evidence=round(max(0.05, rule.evidence * (0.75 if hedge else 1.0)), 4),
                    rule_note=f"prep:{key}",
                    hedged=hedge,
                    negated=negated,
                    matched_on="preposition",
                )
        return RelationDecision(
            predicate=self._type_default(subject_type, object_type),
            evidence=0.5,
            rule_note=f"no-prep-rule:{key}",
            hedged=hedge,
            negated=negated,
            matched_on="type_default",
        )

    def cooccurrence(self, left_type: EntityType, right_type: EntityType, *, distance: int = 0, max_distance: int = 30) -> RelationDecision:
        """Sentence-level fallback relation (carries the 0.2 penalty upstream).

        The evidence score only adds a mild proximity decay on top of the
        ``COOCCURRENCE_FACTOR`` penalty so that two entities 25 words apart are
        worth less than two entities in the same clause.
        """
        decay = 0.0 if max_distance <= 0 else min(0.3, 0.3 * (max(0, distance) / float(max_distance)))
        return RelationDecision(
            predicate=self._type_default(left_type, right_type, generic=True),
            evidence=round(max(0.4, 1.0 - decay), 4),
            rule_note=f"cooccurrence:distance={distance}",
            matched_on="cooccurrence",
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _type_default(subject_type: EntityType, object_type: EntityType, *, generic: bool = False) -> RelationType:
        """Type-aware default predicate when no lexical rule fired."""
        if generic:
            craft_involved = subject_type is EntityType.CRAFT or object_type is EntityType.CRAFT
            if craft_involved and object_type is EntityType.LOCATION:
                # "9H-VUC … Malta" is a movement claim, not a residence claim.
                return RelationType.TRAVELED_TO
            if craft_involved:
                return RelationType.TRAVELED_WITH
            if object_type is EntityType.LOCATION:
                return RelationType.LOCATED_IN
            if subject_type is EntityType.PERSON and object_type is EntityType.PERSON:
                return RelationType.MET_WITH
            return RelationType.ASSOCIATED_WITH
        if subject_type is EntityType.PERSON and object_type is EntityType.ORGANIZATION:
            return RelationType.AFFILIATED_WITH
        if subject_type is EntityType.ORGANIZATION and object_type is EntityType.ORGANIZATION:
            return RelationType.ASSOCIATED_WITH
        if object_type is EntityType.LOCATION:
            return RelationType.LOCATED_IN
        if object_type is EntityType.CRAFT:
            # A person and a craft co-occurring is travel; an organisation and a
            # craft is operation. OPERATES would be a false claim for the former.
            return RelationType.TRAVELED_WITH if subject_type is EntityType.PERSON else RelationType.OPERATES
        return RelationType.ASSOCIATED_WITH

    @staticmethod
    def canonicalise(subject_key: str, object_key: str, predicate: RelationType) -> tuple[str, str, RelationType, bool]:
        """Order symmetric edges deterministically.

        Returns ``(subject_key, object_key, predicate, flipped)``. Flipping a
        symmetric predicate loses no meaning and prevents ``A—MET_WITH—B`` and
        ``B—MET_WITH—A`` from becoming two parallel edges.
        """
        if predicate in SYMMETRIC_RELATIONS and object_key < subject_key:
            return object_key, subject_key, predicate, True
        return subject_key, object_key, predicate, False

    def describe(self) -> dict[str, int]:
        return {
            "verb_rules": len(self.verb_rules),
            "noun_rules": len(self.noun_rules),
            "preposition_rules": len(self.preposition_rules),
            "predicates": len({r.predicate for r in self.verb_rules} | {r.predicate for r in self.noun_rules} | {r.predicate for r in self.preposition_rules}),
        }
