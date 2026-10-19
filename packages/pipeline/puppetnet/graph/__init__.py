"""Graph layer: Neo4j client, schema DDL, batched writer, entity resolver."""

from __future__ import annotations

from .algorithms import brandes_betweenness, shortest_cycle_through, strongly_connected_components
from .neo4j_client import DryRunRecorder, Neo4jClient, Neo4jUnavailable, chunked
from .puppet_master import CapacityGuardReport, CycleReport, CypherAnalytics, PuppetMasterReport, capacity_guard
from .resolver import EntityResolver, ResolverStats, merge_entities_by_key
from .schema import (
    CONSTRAINTS,
    FULLTEXT_INDEXES,
    INDEXES,
    build_entity_upsert,
    build_relation_upsert,
    ensure_schema_statements,
)
from .writer import GraphWriter, WriteSummary, summarise_relations

__all__ = [
    "Neo4jClient",
    "Neo4jUnavailable",
    "DryRunRecorder",
    "chunked",
    "brandes_betweenness",
    "shortest_cycle_through",
    "strongly_connected_components",
    "CypherAnalytics",
    "PuppetMasterReport",
    "CycleReport",
    "CapacityGuardReport",
    "capacity_guard",
    "GraphWriter",
    "WriteSummary",
    "summarise_relations",
    "EntityResolver",
    "ResolverStats",
    "merge_entities_by_key",
    "CONSTRAINTS",
    "INDEXES",
    "FULLTEXT_INDEXES",
    "build_entity_upsert",
    "build_relation_upsert",
    "ensure_schema_statements",
]
