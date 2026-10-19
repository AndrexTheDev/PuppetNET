"""Calculated graph layer: influence scoring and ``PUPPET_MASTER_OF`` edges.

Nothing here extracts new facts. It reads what the harvest already wrote —
control edges, shared addresses, flight logs, sanctions — and answers the
question the rest of the pipeline exists to serve:

    *Who sits behind this network, and how much of it do they control?*

Two outputs, both idempotent:

``Person.risk_score``
    A ``[0, 1]`` score per person built from six weighted components, each of
    which is written back as a property so an analyst can see the breakdown
    rather than a bare number (:data:`RISK_COMPONENT_WEIGHTS`).

``(:Person)-[:PUPPET_MASTER_OF {score}]->(:Entity)``
    Person → controlled entity, directly or through a chain of vehicles. The
    ``score`` is the person's reach discounted per hop, so someone who owns a
    shell that owns the asset scores lower than someone who owns the asset
    outright — but still scores, which is the point.

Design rules
------------
* **Deterministic.** Same inputs → same edges in the same order. Everything is
  sorted by ``(-score, depth, key)`` before it is capped.
* **Bounded.** One nominee behind 40 000 vehicles cannot blow up the write:
  per-person targets (:data:`MAX_CHAIN_DEPTH`, ``puppet_master_top_n``) and a
  global edge cap (``puppet_master_max_edges``) both apply.
* **Explainable.** Every edge carries ``reasons`` and a readable ``evidence``
  chain; every person carries ``risk_reasons`` and ``risk_components``.
* **Never fatal.** The pipeline wraps this pass; a scoring bug must not throw
  away a completed harvest.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from ..domain import domain_labels, jurisdiction_class
from ..domain import shell_risk as shell_risk_of
from ..logging_utils import get_logger
from ..models import Entity, EntityType, Relation, RelationType, iso, utcnow
from . import schema

__all__ = [
    "ADVERSARIAL_PREDICATES",
    "AnalysisResult",
    "AnalyticsEngine",
    "CONTROL_PREDICATES",
    "CONVERGENCE_PREDICATES",
    "MOVEMENT_PREDICATES",
    "PersonAssessment",
    "PuppetTarget",
    "RISK_COMPONENT_WEIGHTS",
]

logger = get_logger("graph.analytics")

# --------------------------------------------------------------------------- #
# Predicate tables
# --------------------------------------------------------------------------- #
#: Control/ownership predicates → ``(subject_controls_object, strength)``.
#:
#: ``subject_controls_object`` is ``False`` when the predicate is passive and the
#: edge must be flipped before use (``OWNED_BY``, ``NOMINEE_OF``, …).
#: ``strength`` is how much of the target is attributable through this kind of
#: edge: an outright owner gets 1.0, the principal behind a nominee 0.8, a
#: nominee-fronted intermediary 0.6, and a plain directorship 0.5 — because a
#: directorship is influence, not ownership, and claiming otherwise is how
#: network analysis earns a reputation for false positives.
CONTROL_PREDICATES: dict[RelationType, tuple[bool, float]] = {
    RelationType.CONTROLS: (True, 1.0),
    RelationType.OWNS: (True, 1.0),
    RelationType.OWNED_BY: (False, 1.0),
    RelationType.BENEFICIARY_OF: (True, 0.95),
    RelationType.SHAREHOLDER_OF: (True, 0.85),
    RelationType.PARENT_OF: (True, 0.9),
    RelationType.SUBSIDIARY_OF: (False, 0.9),
    RelationType.ACQUIRED: (True, 0.85),
    RelationType.NOMINEE_OF: (False, 0.8),
    RelationType.TRUSTEE_OF: (True, 0.8),
    RelationType.INTERMEDIARY_FOR: (True, 0.6),
    RelationType.FOUNDED: (True, 0.6),
    RelationType.DIRECTOR_OF: (True, 0.5),
    RelationType.OFFICER_OF: (True, 0.45),
    RelationType.FUNDED: (True, 0.4),
    RelationType.DONATED_TO: (True, 0.35),
}

#: Adversarial context, weighted by how much it raises the stakes.
ADVERSARIAL_PREDICATES: dict[RelationType, float] = {
    RelationType.SANCTIONED_BY: 1.0,
    RelationType.INVESTIGATED_BY: 0.8,
    RelationType.ACCUSED_OF: 0.6,
    RelationType.LINKED_OFFSHORE: 0.5,
}

#: Person-to-person edges meaning two actors move in the same circle. Treated as
#: undirected: ``SHARES_ADDRESS`` is symmetric in everything but its storage.
CONVERGENCE_PREDICATES: dict[RelationType, float] = {
    RelationType.SHARES_ADDRESS: 1.0,
    RelationType.FAMILY_OF: 0.9,
    RelationType.MET_WITH: 0.6,
    RelationType.MENTIONED_WITH: 0.3,
    RelationType.ASSOCIATED_WITH: 0.2,
    RelationType.AFFILIATED_WITH: 0.2,
}

#: Craft edges: private aviation and yachts are the movement half of the graph.
MOVEMENT_PREDICATES: dict[RelationType, float] = {
    RelationType.PASSENGER_ON: 1.0,
    RelationType.REGISTERED_TO: 0.9,
    RelationType.OPERATES: 0.7,
    RelationType.TRAVELED_WITH: 0.6,
    RelationType.TRAVELED_TO: 0.3,
    RelationType.ARRIVED_FROM: 0.3,
}

#: Contribution of each component to ``Person.risk_score``. Sums to 1.0.
RISK_COMPONENT_WEIGHTS: dict[str, float] = {
    "control_breadth": 0.30,
    "opacity": 0.25,
    "layering": 0.20,
    "convergence": 0.10,
    "adversarial": 0.10,
    "movement": 0.05,
}

#: Control attributable through a chain is discounted per hop: a shell that owns
#: the asset is 70% as attributable as owning it outright, its parent 49%, …
DEPTH_DECAY: float = 0.7
#: Chain length limit. Depth 4 already means "person → shell → holding → asset".
MAX_CHAIN_DEPTH: int = 4
#: Paths weaker than this are not worth writing.
MIN_PATH_STRENGTH: float = 0.01
#: Effective vehicles that saturate ``control_breadth``. Four is enough to call
#: someone a network operator: a single shell is a tax arrangement, four is a
#: structure. Counted *effectively* — an opaque vehicle is 1.0, a trading
#: company 0.4 — so twenty directorships of real businesses still score low.
BREADTH_SATURATION: float = 4.0
#: Distinct associates that saturate ``convergence``.
CONVERGENCE_SATURATION: float = 3.0
#: Craft links that saturate ``movement``.
MOVEMENT_SATURATION: float = 3.0

#: Edge predicates that mean "this person hides behind someone else".
_LAYERING_PREDICATES: frozenset[str] = frozenset({"NOMINEE_OF", "BENEFICIARY_OF", "INTERMEDIARY_FOR"})


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #
@dataclass
class NodeFacts:
    """Everything the scorer knows about one node."""

    key: str
    name: str = ""
    entity_type: EntityType = EntityType.UNKNOWN
    labels: tuple[str, ...] = ()
    shell_risk: float = 0.0
    jurisdiction_class: str = ""
    jurisdiction: str = ""
    reg_number: str = ""

    @property
    def is_person(self) -> bool:
        return self.entity_type is EntityType.PERSON or "Person" in self.labels

    @property
    def is_opaque(self) -> bool:
        """A shell, an offshore vehicle, or anything the source flagged as one."""
        return self.shell_risk >= 0.5 or "ShellCompany" in self.labels or "Offshore" in self.labels or self.jurisdiction_class == "secrecy"

    @property
    def is_vehicle(self) -> bool:
        """A corporate vehicle (company, shell or foundation) rather than an asset."""
        if self.entity_type is EntityType.ORGANIZATION:
            return True
        return bool({"Company", "ShellCompany", "Foundation", "Organization", "Offshore"} & set(self.labels))

    def control_value(self) -> float:
        """How much controlling this node counts for toward breadth."""
        return 1.0 if self.is_opaque else 0.4

    def absorb(self, other: NodeFacts) -> None:
        self.name = self.name or other.name
        self.shell_risk = max(self.shell_risk, other.shell_risk)
        self.labels = tuple(sorted(set(self.labels) | set(other.labels)))
        self.jurisdiction_class = self.jurisdiction_class or other.jurisdiction_class
        self.jurisdiction = self.jurisdiction or other.jurisdiction
        self.reg_number = self.reg_number or other.reg_number
        if self.entity_type is EntityType.UNKNOWN and other.entity_type is not EntityType.UNKNOWN:
            self.entity_type = other.entity_type


@dataclass
class PuppetTarget:
    """One entity a person is scored as standing behind."""

    key: str
    name: str
    score: float
    depth: int
    strength: float = 0.0
    chain: list[str] = field(default_factory=list)

    def evidence(self) -> str:
        return " → ".join(self.chain) if self.chain else self.name


@dataclass
class PersonAssessment:
    """A scored person plus the targets they control."""

    key: str
    name: str
    risk_score: float
    components: dict[str, float]
    reasons: list[str]
    targets: list[PuppetTarget] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_key": self.key,
            "name": self.name,
            "risk_score": self.risk_score,
            "components": dict(self.components),
            "reasons": list(self.reasons),
            "targets": [{"key": t.key, "name": t.name, "score": t.score, "depth": t.depth, "chain": list(t.chain)} for t in self.targets],
        }


@dataclass
class AnalysisResult:
    """Scored people plus the rows the writer will persist."""

    persons: list[PersonAssessment] = field(default_factory=list)
    puppet_master_rows: list[dict[str, Any]] = field(default_factory=list)
    risk_rows: list[dict[str, Any]] = field(default_factory=list)
    pruned: int = 0
    nodes_considered: int = 0
    edges_considered: int = 0
    graph_edges_read: int = 0
    seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        top = sorted(self.persons, key=lambda person: (-person.risk_score, person.key))[:20]
        return {
            "persons_scored": len(self.persons),
            "puppet_master_edges": len(self.puppet_master_rows),
            "risk_scores_updated": len(self.risk_rows),
            "pruned_stale_edges": self.pruned,
            "nodes_considered": self.nodes_considered,
            "edges_considered": self.edges_considered,
            "graph_edges_read": self.graph_edges_read,
            "seconds": round(self.seconds, 3),
            "top_persons": [person.to_dict() for person in top],
        }


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class AnalyticsEngine:
    """Scores people and writes the calculated layer for one run."""

    def __init__(self, client: Any, settings: Any, *, stats: Any = None, writer: Any = None, run_id: str = "") -> None:
        self.client = client
        self.settings = settings
        self.stats = stats
        self.writer = writer
        self.run_id = run_id or str(getattr(settings, "run_id", "") or "")

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def analyse(self, *, entities: Iterable[Entity] = (), relations: Iterable[Relation] = ()) -> AnalysisResult:
        """Score every person reachable from ``relations`` (and ``entities``).

        ``entities`` is optional: each :class:`~puppetnet.models.Relation`
        already carries its endpoints, so the node index is built from the edges
        when no entity list is supplied.
        """
        started = time.perf_counter()
        relation_list = list(relations or ())
        nodes = self._index_nodes(list(entities or ()) + _relation_endpoints(relation_list))
        control, links, movement, adversarial, craft_passengers = self._index_relations(relation_list)
        graph_edges = self._absorb_graph(nodes, control, links, adversarial)

        result = AnalysisResult(
            nodes_considered=len(nodes),
            edges_considered=len(control) + len(links) + len(movement) + len(adversarial),
            graph_edges_read=graph_edges,
        )

        max_persons = int(getattr(self.settings, "analytics_max_persons", 5_000))
        candidates = sorted(
            (node for node in nodes.values() if node.is_person and self._participates(node.key, control, links, movement, adversarial)),
            key=lambda node: node.key,
        )[:max_persons]

        assessments = [
            self._assess(
                person,
                nodes=nodes,
                control_edges=control,
                link_edges=links,
                movement_edges=movement,
                adversarial_edges=adversarial,
                craft_passengers=craft_passengers,
            )
            for person in candidates
        ]

        now = iso(utcnow())
        threshold = float(getattr(self.settings, "puppet_master_min_score", 0.40))
        top_n = int(getattr(self.settings, "puppet_master_top_n", 8))
        max_edges = int(getattr(self.settings, "puppet_master_max_edges", 2_000))

        assessments.sort(key=lambda item: (-item.risk_score, item.key))
        for assessment in assessments:
            result.risk_rows.append(
                {
                    "canonical_key": assessment.key,
                    "risk_score": assessment.risk_score,
                    "components": dict(assessment.components),
                    "reasons": assessment.reasons[:12],
                    "updated_at": now,
                    "run_id": self.run_id,
                }
            )
            if assessment.risk_score < threshold:
                continue
            capped = False
            for target in assessment.targets[:top_n]:
                if len(result.puppet_master_rows) >= max_edges:
                    capped = True
                    break
                result.puppet_master_rows.append(
                    {
                        "subject_key": assessment.key,
                        "object_key": target.key,
                        "score": target.score,
                        "weight": round(min(1.0, target.score), 6),
                        "confidence": round(max(target.score, assessment.risk_score * target.strength), 6),
                        "components": dict(assessment.components),
                        "reasons": assessment.reasons[:12],
                        "evidence": target.evidence()[:900],
                        "depth": int(target.depth),
                        "run_id": self.run_id,
                        "computed_at": now,
                    }
                )
            if capped:
                logger.warning("puppet-master edge cap (%d) reached — truncating the calculated layer", max_edges)
                break

        result.persons = assessments
        result.seconds = round(time.perf_counter() - started, 3)
        logger.info(
            "analytics: %d node(s), %d run edge(s), %d graph edge(s) → %d person(s) scored, %d PUPPET_MASTER_OF row(s)",
            result.nodes_considered,
            result.edges_considered,
            result.graph_edges_read,
            len(result.risk_rows),
            len(result.puppet_master_rows),
        )
        return result

    def write(self, result: AnalysisResult) -> tuple[int, int]:
        """Persist the calculated layer. Returns ``(edges, persons_written)``."""
        prune_days = int(getattr(self.settings, "analytics_prune_days", 14))
        if prune_days > 0 and result.puppet_master_rows:
            cutoff = iso(utcnow() - timedelta(days=prune_days))
            try:
                result.pruned = self._prune(cutoff)
            except Exception as exc:  # noqa: BLE001 - pruning is housekeeping, not correctness
                logger.warning("could not prune stale PUPPET_MASTER_OF edges: %s", exc)
        try:
            edges = self._write_edges(result.puppet_master_rows)
            persons = self._write_risk(result.risk_rows)
        except Exception as exc:  # noqa: BLE001 - never lose a completed harvest
            logger.error("analytics write failed: %s", exc)
            if self.stats is not None:
                self.stats.record_error("analytics", f"{exc.__class__.__name__}: {exc}")
            return 0, 0
        return edges, persons

    # ------------------------------------------------------------------ #
    # Indexing
    # ------------------------------------------------------------------ #
    @staticmethod
    def _index_nodes(entities: Iterable[Entity]) -> dict[str, NodeFacts]:
        nodes: dict[str, NodeFacts] = {}
        for entity in entities:
            key = getattr(entity, "canonical_key", "")
            if not key:
                continue
            props = dict(getattr(entity, "properties", {}) or {})
            risk = props.get("shell_risk")
            if not isinstance(risk, (int, float)):
                risk, _ = shell_risk_of(entity)
            node = NodeFacts(
                key=key,
                name=str(getattr(entity, "name", "") or "")[:200],
                entity_type=entity.entity_type if isinstance(entity.entity_type, EntityType) else EntityType.coerce(entity.entity_type),
                labels=domain_labels(entity),
                shell_risk=round(float(risk or 0.0), 4),
                jurisdiction_class=str(props.get("jurisdiction_class") or jurisdiction_class(entity) or ""),
                jurisdiction=str(props.get("jurisdiction") or props.get("jurisdiction_code") or props.get("country") or "")[:80],
                reg_number=str(props.get("reg_number") or props.get("company_number") or "")[:60],
            )
            existing = nodes.get(key)
            if existing is None:
                nodes[key] = node
            else:
                existing.absorb(node)
        return nodes

    @staticmethod
    def _index_relations(
        relations: Iterable[Relation],
    ) -> tuple[
        dict[str, list[tuple[str, str, float]]],
        dict[str, list[tuple[str, str, float]]],
        dict[str, list[tuple[str, str, float]]],
        dict[str, list[tuple[str, str, float]]],
        dict[str, set[str]],
    ]:
        """Sort relations into the four buckets the scorer reads.

        Each bucket maps ``subject_key → [(object_key, predicate, strength)]``,
        with control edges already oriented *controller → controlled*.
        """
        control: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
        links: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
        movement: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
        adversarial: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
        craft_passengers: dict[str, set[str]] = defaultdict(set)

        for relation in relations:
            predicate = relation.predicate if isinstance(relation.predicate, RelationType) else RelationType.coerce(relation.predicate)
            subject_key = getattr(relation.subject, "canonical_key", "")
            object_key = getattr(relation.obj, "canonical_key", "")
            if not subject_key or not object_key or subject_key == object_key:
                continue
            # Confidence is the trust in the observation; it scales how much of
            # the relationship the scorer is willing to attribute.
            confidence = round(max(0.0, min(1.0, float(relation.confidence or 0.0))), 4)

            if predicate in CONTROL_PREDICATES:
                forward, base = CONTROL_PREDICATES[predicate]
                controller, controlled = (subject_key, object_key) if forward else (object_key, subject_key)
                _append_unique(control[controller], (controlled, predicate.value, round(base * max(confidence, 0.2), 4)))
            if predicate in CONVERGENCE_PREDICATES:
                weight = round(CONVERGENCE_PREDICATES[predicate] * max(confidence, 0.2), 4)
                _append_unique(links[subject_key], (object_key, predicate.value, weight))
                _append_unique(links[object_key], (subject_key, predicate.value, weight))
            if predicate in MOVEMENT_PREDICATES:
                _append_unique(movement[subject_key], (object_key, predicate.value, MOVEMENT_PREDICATES[predicate]))
                if predicate is RelationType.PASSENGER_ON:
                    craft_passengers[object_key].add(subject_key)
            if predicate in ADVERSARIAL_PREDICATES:
                _append_unique(adversarial[subject_key], (object_key, predicate.value, ADVERSARIAL_PREDICATES[predicate]))

        return dict(control), dict(links), dict(movement), dict(adversarial), dict(craft_passengers)

    def _absorb_graph(
        self,
        nodes: dict[str, NodeFacts],
        control: dict[str, list[tuple[str, str, float]]],
        links: dict[str, list[tuple[str, str, float]]],
        adversarial: dict[str, list[tuple[str, str, float]]],
    ) -> int:
        """Fold previously written edges in, so the score reflects the whole graph.

        A daily cron only sees today's harvest; without this, the layering depth
        of a network assembled over months stays invisible. Reads are best
        effort — a dry run returns nothing and the in-run data stands alone.
        """
        limit = int(getattr(self.settings, "analytics_graph_edge_limit", 50_000))
        min_confidence = float(getattr(self.settings, "min_edge_confidence", 0.05))
        if limit <= 0 or bool(getattr(self.client, "dry_run", False)):
            return 0

        absorbed = 0
        try:
            rows = self.client.read(
                schema.ANALYTICS_CONTROL_EDGES,
                {"control_types": [p.value for p in CONTROL_PREDICATES], "min_confidence": min_confidence, "limit": limit},
            )
        except Exception as exc:  # noqa: BLE001 - graph reads are optional
            logger.warning("analytics could not read existing control edges: %s", exc)
            return 0

        for row in rows or ():
            subject_key = str(row.get("subject_key") or "")
            object_key = str(row.get("object_key") or "")
            predicate = RelationType.coerce(str(row.get("predicate") or ""))
            if not subject_key or not object_key or predicate not in CONTROL_PREDICATES:
                continue
            forward, base = CONTROL_PREDICATES[predicate]
            controller, controlled = (subject_key, object_key) if forward else (object_key, subject_key)
            confidence = float(row.get("confidence") or 0.0)
            entry = (controlled, predicate.value, round(base * max(confidence, 0.2), 4))
            if _append_unique(control.setdefault(controller, []), entry):
                absorbed += 1
            self._absorb_node(nodes, row, "subject")
            self._absorb_node(nodes, row, "object")

        for query, param_key, bucket, table in (
            (schema.ANALYTICS_PERSON_LINKS, "link_types", links, CONVERGENCE_PREDICATES),
            (schema.ANALYTICS_ADVERSARIAL_EDGES, "adversarial_types", adversarial, ADVERSARIAL_PREDICATES),
        ):
            try:
                rows = self.client.read(query, {param_key: [p.value for p in table], "min_confidence": min_confidence, "limit": limit})
            except Exception as exc:  # noqa: BLE001
                logger.warning("analytics could not read %s: %s", param_key, exc)
                continue
            for row in rows or ():
                subject_key = str(row.get("subject_key") or "")
                object_key = str(row.get("object_key") or "")
                predicate = RelationType.coerce(str(row.get("predicate") or ""))
                if not subject_key or not object_key or predicate not in table:
                    continue
                entry = (object_key, predicate.value, round(table[predicate] * max(float(row.get("confidence") or 0.0), 0.2), 4))
                if _append_unique(bucket.setdefault(subject_key, []), entry):
                    absorbed += 1
                if bucket is links:
                    _append_unique(bucket.setdefault(object_key, []), (subject_key, predicate.value, entry[2]))
                self._absorb_node(nodes, row, "subject")
                self._absorb_node(nodes, row, "object")

        if absorbed:
            logger.info("analytics absorbed %d previously written edge(s) from the graph", absorbed)
        return absorbed

    @staticmethod
    def _absorb_node(nodes: dict[str, NodeFacts], row: dict[str, Any], side: str) -> None:
        key = str(row.get(f"{side}_key") or "")
        if not key:
            return
        labels = tuple(str(label) for label in (row.get(f"{side}_labels") or ()))
        incoming = NodeFacts(
            key=key,
            name=str(row.get(f"{side}_name") or "")[:200],
            entity_type=_type_from_labels(labels),
            labels=labels,
            shell_risk=round(float(row.get(f"{side}_shell_risk") or 0.0), 4),
            jurisdiction_class=str(row.get(f"{side}_jurisdiction_class") or ""),
            jurisdiction=str(row.get(f"{side}_jurisdiction") or "")[:80],
            reg_number=str(row.get(f"{side}_reg_number") or "")[:60],
        )
        existing = nodes.get(key)
        if existing is None:
            nodes[key] = incoming
        else:
            existing.absorb(incoming)

    @staticmethod
    def _participates(key: str, *buckets: dict[str, list[tuple[str, str, float]]]) -> bool:
        """Only score people who actually appear in an edge somewhere."""
        return any(key in bucket for bucket in buckets)

    # ------------------------------------------------------------------ #
    # Scoring
    # ------------------------------------------------------------------ #
    def _assess(
        self,
        person: NodeFacts,
        *,
        nodes: dict[str, NodeFacts],
        control_edges: dict[str, list[tuple[str, str, float]]],
        link_edges: dict[str, list[tuple[str, str, float]]],
        movement_edges: dict[str, list[tuple[str, str, float]]],
        adversarial_edges: dict[str, list[tuple[str, str, float]]],
        craft_passengers: dict[str, set[str]],
    ) -> PersonAssessment:
        reach, paths = self._walk_control(person, nodes, control_edges)
        targets = self._select_targets(nodes, reach, paths)

        # 1. Breadth: how much of what they control is opaque.
        breadth_value = sum(strength * _node(nodes, key).control_value() for key, (strength, _) in reach.items())
        control_breadth = min(1.0, breadth_value / BREADTH_SATURATION)

        # 2. Opacity: mean shell risk across the *vehicles* they control, plus a
        #    bump when the structure uses nominees or an intermediary. Assets at
        #    the end of the chain (a tower, a yacht) are excluded — they are what
        #    the structure holds, not evidence of how opaque it is.
        vehicles = [_node(nodes, key) for key in reach if _is_vehicle(_node(nodes, key))]
        opacities = [vehicle.shell_risk for vehicle in vehicles]
        opacity = (sum(opacities) / len(opacities)) if opacities else 0.0
        if any(predicate in _LAYERING_PREDICATES for _, predicate, _ in control_edges.get(person.key, ())):
            opacity = min(1.0, opacity + 0.25)

        # 3. Layering: how deep the control chain runs. A single direct holding
        #    is not layering, so it counts half.
        deepest = max((depth for _, depth in reach.values()), default=0)
        layering = min(1.0, max(0, deepest - 1) / (MAX_CHAIN_DEPTH - 1))
        if len(reach) <= 1:
            layering *= 0.5

        # 4. Convergence: distinct other people sharing an address, a flight or a
        #    family tie — the "same room" signal.
        associates = self._associates(person.key, link_edges, movement_edges, craft_passengers)
        convergence = min(1.0, len(associates) / CONVERGENCE_SATURATION)

        # 5. Movement: private craft, weighted toward secrecy-jurisdiction flags.
        craft = {key for key, _, _ in movement_edges.get(person.key, ())}
        offshore_craft = sum(1 for key in craft if _node(nodes, key).jurisdiction_class == "secrecy")
        movement = 0.0
        if craft:
            base = min(1.0, len(craft) / MOVEMENT_SATURATION)
            movement = base * (0.6 + 0.4 * (offshore_craft / len(craft)))

        # 6. Adversarial: the strongest sanction/investigation/accusation signal.
        hits = adversarial_edges.get(person.key, ())
        adversarial = max((strength for _, _, strength in hits), default=0.0)
        if len(hits) > 1:
            adversarial = min(1.0, adversarial + 0.1 * (len(hits) - 1))

        components = {
            "control_breadth": round(control_breadth, 4),
            "opacity": round(opacity, 4),
            "layering": round(layering, 4),
            "convergence": round(convergence, 4),
            "adversarial": round(adversarial, 4),
            "movement": round(movement, 4),
        }
        risk_score = round(min(1.0, max(0.0, sum(RISK_COMPONENT_WEIGHTS[name] * value for name, value in components.items()))), 4)
        reasons = _reasons(components, reach, paths, associates, craft, hits, nodes)
        return PersonAssessment(key=person.key, name=person.name, risk_score=risk_score, components=components, reasons=reasons, targets=targets)

    @staticmethod
    def _walk_control(
        person: NodeFacts,
        nodes: dict[str, NodeFacts],
        control_edges: dict[str, list[tuple[str, str, float]]],
    ) -> tuple[dict[str, tuple[float, int]], dict[str, list[str]]]:
        """Bounded breadth-first walk of the control graph from ``person``.

        Returns ``(reach, paths)``: ``reach[key] = (strength, depth)`` is the
        *strongest* attributable path to ``key`` and ``paths[key]`` the readable
        chain behind it. Cycle-safe (a path is only extended when it beats the
        best known strength) and capped at :data:`MAX_CHAIN_DEPTH` hops, so a
        nominee loop cannot hang the run.
        """
        reach: dict[str, tuple[float, int]] = {}
        paths: dict[str, list[str]] = {}
        frontier: list[tuple[str, float, int, list[str]]] = [(person.key, 1.0, 0, [person.name or person.key])]

        while frontier:
            current, strength, depth, chain = frontier.pop(0)
            if depth >= MAX_CHAIN_DEPTH:
                continue
            for target_key, predicate, edge_strength in control_edges.get(current, ()):
                if target_key == person.key or target_key == current:
                    continue
                next_strength = round(strength * edge_strength * DEPTH_DECAY**depth, 4)
                if next_strength <= MIN_PATH_STRENGTH:
                    continue
                previous = reach.get(target_key)
                if previous is not None and previous[0] >= next_strength:
                    continue
                reach[target_key] = (next_strength, depth + 1)
                target_name = _node(nodes, target_key).name or target_key
                next_chain = [*chain, predicate, target_name]
                paths[target_key] = next_chain
                frontier.append((target_key, next_strength, depth + 1, next_chain))

        return reach, paths

    @staticmethod
    def _select_targets(
        nodes: dict[str, NodeFacts],
        reach: dict[str, tuple[float, int]],
        paths: dict[str, list[str]],
    ) -> list[PuppetTarget]:
        targets: list[PuppetTarget] = []
        for key, (strength, depth) in reach.items():
            node = _node(nodes, key)
            # Opacity is why the edge matters: controlling a trading company is
            # ordinary business, controlling an unattributable vehicle is not.
            score = round(min(1.0, strength + (0.15 if node.is_opaque else 0.0)), 4)
            targets.append(PuppetTarget(key=key, name=node.name or key, score=score, depth=depth, strength=strength, chain=paths.get(key, [])))
        targets.sort(key=lambda target: (-target.score, target.depth, target.key))
        return targets

    @staticmethod
    def _associates(
        person_key: str,
        link_edges: dict[str, list[tuple[str, str, float]]],
        movement_edges: dict[str, list[tuple[str, str, float]]],
        craft_passengers: dict[str, set[str]],
    ) -> set[str]:
        """Distinct other people this person converges with.

        Includes co-passengers: two people on the same private aircraft are
        converging even when no document ever names them together.
        """
        associates = {key for key, _, _ in link_edges.get(person_key, ()) if key != person_key}
        for craft_key, _, _ in movement_edges.get(person_key, ()):
            associates.update(fellow for fellow in craft_passengers.get(craft_key, ()) if fellow != person_key)
        return associates

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    def _write_edges(self, rows: Sequence[dict[str, Any]]) -> int:
        if not rows:
            return 0
        if self.writer is not None:
            return int(self.writer.write_puppet_master(rows))
        return int(self.client.execute_batches(schema.PUPPET_MASTER_UPSERT, list(rows), kind="analytics", label="analytics:puppet_master"))

    def _write_risk(self, rows: Sequence[dict[str, Any]]) -> int:
        if not rows:
            return 0
        if self.writer is not None:
            return int(self.writer.update_risk_scores(rows))
        return int(self.client.execute_batches(schema.PERSON_RISK_UPDATE, list(rows), kind="analytics", label="analytics:risk_scores"))

    def _prune(self, cutoff: str) -> int:
        if self.writer is not None:
            return int(self.writer.prune_puppet_master(cutoff))
        rows = self.client.read(schema.PUPPET_MASTER_PRUNE, {"cutoff": cutoff})
        return int(rows[0].get("pruned", 0)) if rows else 0


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
_EMPTY_NODE = NodeFacts(key="")


def _node(nodes: dict[str, NodeFacts], key: str) -> NodeFacts:
    return nodes.get(key) or _EMPTY_NODE


def _is_vehicle(node: NodeFacts) -> bool:
    return node.is_vehicle


def _relation_endpoints(relations: Sequence[Relation]) -> list[Entity]:
    endpoints: list[Entity] = []
    for relation in relations:
        for side in (relation.subject, relation.obj):
            if isinstance(side, Entity):
                endpoints.append(side)
    return endpoints


def _append_unique(bucket: list[tuple[str, str, float]], entry: tuple[str, str, float]) -> bool:
    """Append when the same (target, predicate) is not already there, stronger wins."""
    for index, existing in enumerate(bucket):
        if existing[0] == entry[0] and existing[1] == entry[1]:
            if entry[2] > existing[2]:
                bucket[index] = entry
            return False
    bucket.append(entry)
    return True


def _type_from_labels(labels: Iterable[str]) -> EntityType:
    for label in labels:
        try:
            return EntityType(label)
        except ValueError:
            continue
    return EntityType.UNKNOWN


def _reasons(
    components: dict[str, float],
    reach: dict[str, tuple[float, int]],
    paths: dict[str, list[str]],
    associates: set[str],
    craft: set[str],
    adversarial_hits: Sequence[tuple[str, str, float]],
    nodes: dict[str, NodeFacts],
) -> list[str]:
    """Human-readable explanation of the score, most damning first."""
    reasons: list[str] = []
    opaque = [key for key in reach if _node(nodes, key).is_opaque]
    if opaque:
        reasons.append(f"controls {len(opaque)} opaque vehicle(s) of {len(reach)} reachable")
    if components["layering"] > 0 and reach:
        deepest_key = max(reach, key=lambda key: (reach[key][1], reach[key][0]))
        chain = " → ".join(paths.get(deepest_key, [])[:6])
        reasons.append(f"control chain runs {reach[deepest_key][1]} hop(s): {chain}"[:300])
    if associates:
        reasons.append(f"shares addresses, flights or family ties with {len(associates)} other actor(s)")
    if craft:
        reasons.append(f"linked to {len(craft)} private aircraft/vessel(s)")
    for _, predicate, _ in adversarial_hits[:3]:
        reasons.append(predicate.lower().replace("_", " "))
    if not reasons:
        reasons.append("no control, convergence or adversarial signal")
    return reasons[:12]
