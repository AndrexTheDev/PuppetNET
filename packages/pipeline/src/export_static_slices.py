"""Publish a bounded graph using confidence/RSS release policy for all consumers.

python packages/pipeline/src/export_static_slices.py --input reviewed-graph.json
python packages/pipeline/src/export_static_slices.py --neo4j
Auto-release: confidence >=0.75 or exact verified RSS provenance; privacy vetoes win.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUT = ROOT / "apps/web/public/data"
MAX_NODES = 5000
MAX_EDGES = 20_000


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        tmp.write(content)
        name = tmp.name
    os.replace(name, path)


def reserve_run(path: Path, event: str, day: str | None = None) -> bool:
    """Durable quota shared by scheduled/manual runs: TOTAL <=3/day = cron <=2 + manual <=1.

    Two cron slots (03:00 and 15:00 UTC) guarantee the twice-daily publication;
    one manual slot keeps a dispatch/research run possible without eating a cron
    slot. Caller MUST serialize writers and persist this file even on ingestion
    failure. This is not a distributed database lock. A missing file starts a
    new ledger; a corrupt file raises and fails closed instead of silently
    replenishing quota. Ledgers written by the older 2-total/1-cron policy stay
    valid within these bounds; a legacy same-day ledger with 2 manual entries
    fails closed for the rest of that UTC day.
    """
    day = day or datetime.now(timezone.utc).date().isoformat()
    state = json.loads(path.read_text()) if path.exists() else {}
    if state.get("day") != day:
        state = {"day": day, "total": 0, "scheduled": 0, "manual": 0}
    if event not in {"schedule", "repository_dispatch", "workflow_dispatch"}:
        raise ValueError("Unsupported run trigger")
    bounds = {"total": 3, "scheduled": 2, "manual": 1}
    if any(type(state.get(k)) is not int or not 0 <= state[k] <= cap for k, cap in bounds.items()):
        raise ValueError("Invalid daily quota ledger")
    if state["manual"] + state["scheduled"] != state["total"]:
        raise ValueError("Inconsistent daily quota ledger")
    kind = "scheduled" if event == "schedule" else "manual"
    if state["total"] >= bounds["total"] or state[kind] >= bounds[kind]:
        return False
    state["total"] += 1
    state[kind] += 1
    atomic_json(path, state)
    return True


def verified_feeds() -> set[str]:
    """Operator allowlist: exact HTTPS feed URLs, never a feed-supplied boolean."""
    feeds = {value.strip() for value in os.getenv("VERIFIED_RSS_FEEDS", "").split(",") if value.strip()}
    if len(feeds) > 100:
        raise ValueError("At most 100 verified RSS feeds are allowed")
    for value in feeds:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("VERIFIED_RSS_FEEDS requires exact HTTPS feed URLs without credentials/fragments")
    return feeds


def auto_released(row: dict, feeds: set[str]) -> bool:
    # Booleans are not confidence numbers. Invalid/nonfinite/out-of-range data
    # must not become publishable by coercion or clamping.
    value = row.get("confidence")
    try:
        confidence = float(value) if not isinstance(value, bool) else -1.
    except (ValueError, TypeError):
        confidence = -1.
    provenance = row.get("feed_urls", [])
    if not isinstance(provenance, list):
        provenance = []
    return (math.isfinite(confidence) and .75 <= confidence <= 1.) or any(
        isinstance(url, str) and url in feeds for url in [row.get("feed_url"), *provenance])


def approved(row: dict, feeds: set[str] | None = None) -> bool:
    feeds = verified_feeds() if feeds is None else feeds
    if any(row.get(flag) for flag in ("private", "noindex", "release_blocked", "negated", "hedged", "calculated")):
        return False
    if row.get("public_released") is False:  # explicit operator withdrawal
        return False
    automatic = auto_released(row, feeds)
    if row.get("requires_review") and not automatic and row.get("public_released") is not True:
        return False
    # public=false may be an extraction draft default, not a privacy veto.
    return row.get("public") is True or row.get("public_released") is True or automatic


def entity_slug(key: str, name: str) -> str:
    """Exact frontend NFKD + UTF-16 FNV-1a contract, including non-BMP IDs."""
    base = unicodedata.normalize("NFKD", name)
    base = re.sub(r"[\u0300-\u036f]", "", base).replace("ß", "ss").lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:60].rstrip("-") or "entity"
    value = 2166136261
    encoded = key.encode("utf-16-le", errors="surrogatepass")
    for i in range(0, len(encoded), 2):
        value = ((value ^ int.from_bytes(encoded[i:i+2], "little")) * 16777619) & 0xffffffff
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    suffix = ""
    while value:
        value, digit = divmod(value, 36)
        suffix = digits[digit] + suffix
    return base + "-" + suffix.rjust(7, "0")


def clean(value: Any, limit: int = 200) -> str:
    return " ".join(str(value if value is not None else "").split())[:limit]


def score(value: Any) -> float:
    try:
        num = float(value)
        return max(0., min(1., num)) if math.isfinite(num) else 0.
    except (ValueError, TypeError):
        return 0.


def read_neo4j(limit: int = MAX_NODES) -> dict:
    """Bounded read snapshot, with feed provenance joined through stored doc IDs.

    No SET/properties() or trusting an arbitrary verified_rss boolean. Export
    release flags are materialized in JSON, not written back into the database.
    """
    from neo4j import GraphDatabase, unit_of_work
    uri, user, password = (os.getenv(key, "") for key in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD"))
    if not all((uri, user, password)):
        raise ValueError("NEO4J_URI, NEO4J_USER and NEO4J_PASSWORD are required")
    if not uri.startswith("neo4j+s://"):
        raise ValueError("Production export requires a TLS-verified neo4j+s:// URI")

    @unit_of_work(timeout=20)
    def read(tx):
        nodes = [dict(row["node"]) for row in tx.run("""
            MATCH (e:Entity)
            WHERE coalesce(e.private,false) = false AND coalesce(e.noindex,false) = false
            WITH e ORDER BY coalesce(e.public_released,e.public,false) DESC,
                            coalesce(e.confidence,0) DESC, e.canonical_key
            LIMIT $limit
            RETURN e{.canonical_key,.name,.entity_type,.description,.public,.public_released,
                     .private,.noindex,.release_blocked,.requires_review,.calculated,.negated,.hedged,
                     .updated_at,.last_seen,.lat,.lon,.confidence,.doc_ids} AS node
        """, limit=max(1, min(limit, MAX_NODES)))]
        keys = [node["canonical_key"] for node in nodes]
        edges = [dict(row["edge"]) for row in tx.run("""
            MATCH (a:Entity)-[r]->(b:Entity)
            WHERE a.canonical_key IN $keys AND b.canonical_key IN $keys
              AND coalesce(r.private,false) = false AND coalesce(r.noindex,false) = false
              AND type(r) <> 'PUPPET_MASTER_OF'
            RETURN {source:a.canonical_key,target:b.canonical_key,type:type(r),public:r.public,
                    public_released:r.public_released,release_blocked:r.release_blocked,
                    requires_review:r.requires_review,calculated:r.calculated,
                    negated:r.negated,hedged:r.hedged,confidence:r.confidence,weight:r.weight,
                    doc_ids:coalesce(r.doc_ids,[]),doc_id:r.doc_id} AS edge
            ORDER BY edge.source,edge.target,edge.type LIMIT $limit
        """, keys=keys, limit=MAX_EDGES)]
        ids = sorted({str(doc) for row in [*nodes, *edges]
                      for doc in [*(row.get("doc_ids") or []), row.get("doc_id")] if doc})[:5000]
        documents = [dict(row["doc"]) for row in tx.run("""
            UNWIND $ids AS doc_id
            MATCH (d:Document {doc_id:doc_id})
            WHERE coalesce(d.private,false) = false AND coalesce(d.noindex,false) = false
            RETURN {id:d.doc_id,title:d.title,url:d.url,source:d.source_id,date:d.published_at,
                    feed_url:d.feed_url,public:d.public,public_released:d.public_released,
                    release_blocked:d.release_blocked} AS doc LIMIT 5000
        """, ids=ids)]
        sources = {doc["id"]: doc.get("feed_url") for doc in documents
                   if doc.get("public_released") is not False and not doc.get("release_blocked")}
        for row in [*nodes, *edges]:
            row["feed_urls"] = sorted({sources[doc] for doc in [*(row.get("doc_ids") or []), row.get("doc_id")]
                                       if doc in sources and isinstance(sources[doc], str)})
        return {"nodes": nodes, "edges": edges, "evidence": documents, "demo": False,
                "truncated": len(nodes) >= min(limit, MAX_NODES) or len(edges) >= MAX_EDGES or len(ids) >= 5000}

    with (
        GraphDatabase.driver(uri, auth=(user, password), connection_timeout=10,
                             max_connection_pool_size=2, max_transaction_retry_time=0) as driver,
        driver.session(database=os.getenv("NEO4J_DATABASE", "neo4j"), default_access_mode="READ") as session,
    ):
        return session.execute_read(read)


def build_public_graph(raw: dict, limit: int = MAX_NODES) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("nodes"), list) or not isinstance(raw.get("edges"), list):
        raise ValueError("Expected nodes[] and edges[]")
    if len(raw["nodes"]) > 200_000 or len(raw["edges"]) > 500_000:
        raise ValueError("Local input exceeds safety limits")
    types = {"person": "Person", "per": "Person", "organization": "Organization", "org": "Organization",
             "company": "Organization", "location": "Location", "loc": "Location", "craft": "Craft"}
    feeds = verified_feeds()
    by_id = {}
    for row in raw["nodes"]:
        if not isinstance(row, dict) or not approved(row, feeds):
            continue
        key = clean(row.get("canonical_key") or row.get("key") or row.get("id"))
        name = clean(row.get("name") or row.get("label"))
        kind = types.get(clean(row.get("entity_type") or row.get("type")).lower())
        if not key or not name or not kind:
            continue
        if key in by_id:
            raise ValueError(f"Duplicate approved node id: {key}")
        # Allowlist: no private notes, document text, risk scores or arbitrary properties.
        node = {"confidence": score(row.get("confidence")), "id": key, "key": key, "name": name, "label": name, "type": kind,
                "entity_type": kind, "public": True, "public_released": True, "slug": entity_slug(key, name),
                "description": clean(row.get("description"), 320),
                "updatedAt": clean(row.get("updatedAt") or row.get("updated_at") or row.get("last_seen"), 40)}
        for field, bound in (("lat", 90), ("lon", 180)):
            try:
                num = float(row[field])
                if math.isfinite(num) and abs(num) <= bound:
                    node[field] = num
            except (KeyError, TypeError, ValueError):
                pass
        by_id[key] = node
    evidence = []
    for item in raw.get("evidence", [])[:5000]:
        if not isinstance(item, dict) or not approved(item, feeds):
            continue
        entry = {"id": clean(item.get("id")), "title": clean(item.get("title")),
                 "source": clean(item.get("source")), "date": clean(item.get("date"), 40)}
        if not entry["id"] or not entry["title"]:
            continue
        url = clean(item.get("url"), 2000)
        try:
            parsed = urlsplit(url)
            if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password:
                entry["url"] = url
        except ValueError:
            pass
        evidence.append(entry)
    public_doc_ids = {item["id"] for item in evidence}
    for row in raw["nodes"]:
        if not isinstance(row, dict):
            continue
        key = clean(row.get("canonical_key") or row.get("key") or row.get("id"))
        if key in by_id:
            refs = row.get("doc_ids", row.get("evidenceIds", []))
            by_id[key]["evidenceIds"] = sorted({str(i) for i in refs if str(i) in public_doc_ids}) if isinstance(refs, list) else []
    selected = sorted(by_id.values(), key=lambda n: (n["updatedAt"], n["id"]), reverse=True)[:max(1, min(limit, MAX_NODES))]
    nodes = sorted(selected, key=lambda n: n["id"])
    keys = {node["id"] for node in nodes}
    edges = {}
    for row in raw["edges"]:
        if not isinstance(row, dict) or not approved(row, feeds):
            continue
        source, target = clean(row.get("source")), clean(row.get("target"))
        predicate = clean(row.get("type") or row.get("predicate"), 64)
        # Predicate vocabulary shared with the pipeline; reject calculated accusations.
        from puppetnet.models import RelationType
        if source not in keys or target not in keys or predicate not in RelationType.names() or predicate == "PUPPET_MASTER_OF":
            continue
        signature = source + "|" + predicate + "|" + target
        edges[signature] = {"id": hashlib.sha256(signature.encode()).hexdigest()[:24],
                            "source": source, "target": target, "type": predicate, "predicate": predicate,
                            "public": True, "public_released": True, "confidence": score(row.get("confidence")), "weight": score(row.get("weight"))}
    return {"schemaVersion": 1, "demo": raw.get("demo") is True, "nodes": nodes,
            "edges": [edges[key] for key in sorted(edges)[:MAX_EDGES]],
            "evidence": sorted(evidence, key=lambda item: item["id"]), "truncated": raw.get("truncated") is True or len(by_id) > len(nodes) or len(edges) > MAX_EDGES}


def enrich_with_analytics(graph: dict) -> None:
    """Deterministic export-time link analysis over the published allowlist graph.

    Adds per-node degree, PageRank, label-propagation community and first/last
    mention dates, plus a bounded top-level summary — the standard OSINT analytics
    set (centrality, communities, temporal spans) precomputed once per revision.
    Consumers (dashboard, entity pages, third parties) get link-analysis value
    for free: no extra CI minutes, no extra Neo4j reads, no browser crunching
    required. Inputs are already the allowlisted public fields, so no private
    data can leak into the analytics. Synchronous label propagation with sorted
    iteration and smallest-label tie-breaks plus 20 damped PageRank passes keep
    results reproducible, which the stable revision digest depends on.
    Bounded cost: O(passes x (nodes + edges)) with passes <= 20 + 8.
    """
    nodes, edges = graph["nodes"], graph["edges"]
    order = [node["id"] for node in nodes]
    total = len(order)
    undirected: dict[str, list[str]] = {key: [] for key in order}
    outgoing: dict[str, list[str]] = {key: [] for key in order}
    degree: dict[str, int] = dict.fromkeys(order, 0)
    for edge in edges:
        source, target = edge["source"], edge["target"]
        degree[source] += 1
        degree[target] += 1
        outgoing[source].append(target)
        undirected[source].append(target)
        if target != source:
            undirected[target].append(source)
    rank: dict[str, float] = dict.fromkeys(order, 1.0 / total) if total else {}
    for _ in range(20):
        if not total:
            break
        dangling = sum(rank[key] for key in order if not outgoing[key])
        updates = {key: (.15 + .85 * dangling) / total for key in order}
        for key in order:
            links = outgoing[key]
            if not links:
                continue
            share = .85 * rank[key] / len(links)
            for target in links:
                updates[target] += share
        rank = updates
    labels = {key: key for key in order}
    for _ in range(8):
        changed = False
        propagated: dict[str, str] = {}
        for key in order:
            counts: dict[str, int] = {}
            for other in undirected[key]:
                counts[labels[other]] = counts.get(labels[other], 0) + 1
            best = max(sorted(counts), key=counts.get) if counts else key
            propagated[key] = best
            changed = changed or best != labels[key]
        labels = propagated
        if not changed:
            break
    groups: dict[str, list[str]] = {}
    for key in order:
        groups.setdefault(labels[key], []).append(key)
    # Stable community names: largest first, ties by smallest member id.
    ranked_groups = sorted(groups.values(), key=lambda members: (-len(members), members[0]))
    community: dict[str, str] = {}
    for index, members in enumerate(ranked_groups):
        for key in members:
            community[key] = f"c{index:03d}"
    dates = {item["id"]: item["date"][:10] for item in graph["evidence"]
             if re.match(r"\d{4}-\d{2}-\d{2}", str(item.get("date", "")))}
    for node in nodes:
        seen = sorted(dates[doc] for doc in node.get("evidenceIds", []) if doc in dates)
        node["degree"] = degree[node["id"]]
        node["rank"] = round(rank[node["id"]], 6)
        node["community"] = community[node["id"]]
        if seen:
            node["firstSeen"], node["lastSeen"] = seen[0], seen[-1]
    hubs = sorted(nodes, key=lambda node: (-node["rank"], node["id"]))[:10]
    firsts = [node["firstSeen"] for node in nodes if "firstSeen" in node]
    lasts = [node["lastSeen"] for node in nodes if "lastSeen" in node]
    graph["analytics"] = {
        "version": 1, "nodes": total, "edges": len(edges),
        "communities": len(ranked_groups),
        "largestCommunity": len(ranked_groups[0]) if ranked_groups else 0,
        "density": round(len(edges) / (total * (total - 1)), 6) if total > 1 else 0.0,
        "hubs": [{"id": node["id"], "name": node["name"], "rank": node["rank"], "degree": node["degree"]}
                 for node in hubs],
        **({"temporalRange": {"first": min(firsts), "last": max(lasts)}} if firsts else {}),
    }


INSIGHT_RULES_VERSION = 1
MAX_INSIGHTS_PER_RULE = 8
MAX_INSIGHTS_TOTAL = 24
_HUB_MIN_DEGREE = 8
_HUB_MAX_INTERCONNECTION = 0.10
_ROLE_PHRASES = {"NOMINEE_OF": "ist Strohmann/Strohfrau für",
                 "INTERMEDIARY_FOR": "ist Vermittler für",
                 "TRUSTEE_OF": "ist Treuhänder für"}
_CONTROL_PHRASES = {"OWNS": "besitzt", "CONTROLS": "kontrolliert",
                    "SHAREHOLDER_OF": "hält Anteile an"}


def correlation_insights(graph: dict) -> dict:
    """Bounded deterministic correlation rules over the published graph (Wave 4).

    SpiderFoot's correlation principle without an LLM and without any cost:
    small, auditable rules as versioned code, computed once per revision inside
    the existing export job (0 extra Actions minutes). Every rule runs only on
    the allowlisted public fields — the same data the dashboard already shows —
    and every insight cites the edge ids that support it, so each pattern is
    traceable to evidence. Patterns are structural hints, never accusations;
    the UI presents them that way.

    Rules (each capped, deterministic order, total bounded):
    * nominee-chain    A →(nominee/intermediary/trustee) B →(owns/controls/shares) C
    * hub-spoke        degree ≥ 8 and ≤10 % of the neighbourhood interlinked
    * community-bridge edge whose endpoints sit in different export communities
    * shared-officer   one person directing two organisations linked to each other

    Cost: O(E·k) for the chain/officer rules, O(Σ d²) bounded by the hub degree
    threshold; far below the export budget even at MAX_NODES/MAX_EDGES.
    """
    nodes = {node["id"]: node for node in graph["nodes"]}
    edges = graph["edges"]
    outgoing: dict[str, list] = {}
    adjacency: dict[str, set] = {key: set() for key in nodes}
    for edge in edges:
        outgoing.setdefault(edge["source"], []).append(edge)
        adjacency.setdefault(edge["source"], set()).add(edge["target"])
        adjacency.setdefault(edge["target"], set()).add(edge["source"])

    def ref(key: str) -> dict:
        return {"id": key, "name": nodes[key]["name"], "type": nodes[key]["type"]}

    items: list[dict] = []

    # R1 — nominee chains: an intermediary between a person/front and an asset.
    chains = []
    for first in edges:
        if first["type"] not in _ROLE_PHRASES:
            continue
        for second in outgoing.get(first["target"], []):
            if second["type"] in _CONTROL_PHRASES and second["target"] != first["source"]:
                chains.append((first, second))
    chains.sort(key=lambda pair: (pair[0]["source"], pair[0]["target"], pair[1]["target"],
                                  pair[0]["id"], pair[1]["id"]))
    for first, second in chains[:MAX_INSIGHTS_PER_RULE]:
        a, b, c = first["source"], first["target"], second["target"]
        items.append({
            "rule": "nominee-chain", "version": INSIGHT_RULES_VERSION,
            "title": "Mögliche Strohmann-Kette",
            "description": (f"{nodes[a]['name']} {_ROLE_PHRASES[first['type']]} {nodes[b]['name']}; "
                            f"{nodes[b]['name']} {_CONTROL_PHRASES[second['type']]} {nodes[c]['name']}."),
            "support": round(min(first["confidence"], second["confidence"]), 4),
            "entities": [ref(a), ref(b), ref(c)],
            "evidence": [first["id"], second["id"]],
        })

    # R2 — hub & spoke: a centre whose neighbours barely know each other.
    stars = []
    for key in nodes:
        neighbours = adjacency.get(key) or set()
        if len(neighbours) < _HUB_MIN_DEGREE:
            continue
        ordered = sorted(neighbours)
        pairs = linked = 0
        for i, left in enumerate(ordered):
            row = adjacency.get(left) or set()
            for right in ordered[i + 1:]:
                pairs += 1
                linked += right in row
        if pairs and linked / pairs <= _HUB_MAX_INTERCONNECTION:
            stars.append((len(neighbours), linked / pairs, key))
    stars.sort(key=lambda item: (-item[0], item[2]))
    for degree, ratio, key in stars[:MAX_INSIGHTS_PER_RULE]:
        spokes = sorted(adjacency[key], key=lambda n: (-nodes[n]["degree"], n))[:4]
        items.append({
            "rule": "hub-spoke", "version": INSIGHT_RULES_VERSION,
            "title": "Sternstruktur (Hub-and-Spoke)",
            "description": (f"{nodes[key]['name']} verbindet {degree} Nachbarn, von denen nur "
                            f"{ratio:.0%} untereinander verbunden sind — zentrale Vermittlungsposition."),
            "support": round(nodes[key]["rank"], 4),
            "entities": [ref(key), *[ref(spoke) for spoke in spokes]],
            "evidence": [],
        })

    # R3 — community bridges: published edges between two export communities.
    bridges = []
    for edge in edges:
        left, right = nodes.get(edge["source"]), nodes.get(edge["target"])
        if not left or not right:
            continue
        left_community, right_community = left.get("community"), right.get("community")
        if not left_community or not right_community or left_community == right_community:
            continue
        if left["degree"] < 2 or right["degree"] < 2:
            continue
        bridges.append((min(left["degree"], right["degree"]), edge))
    bridges.sort(key=lambda item: (-item[0], item[1]["id"]))
    for _, edge in bridges[:MAX_INSIGHTS_PER_RULE]:
        left, right = nodes[edge["source"]], nodes[edge["target"]]
        items.append({
            "rule": "community-bridge", "version": INSIGHT_RULES_VERSION,
            "title": "Brücke zwischen Communities",
            "description": (f"{left['name']} ({left.get('community')}) —[{edge['type']}]→ "
                            f"{right['name']} ({right.get('community')}): Kante zwischen zwei "
                            f"ansonsten getrennten Gruppen."),
            "support": round(edge["confidence"], 4),
            "entities": [ref(edge["source"]), ref(edge["target"])],
            "evidence": [edge["id"]],
        })

    # R4 — shared officers: one person directing two interconnected organisations.
    officer_edges: dict[str, dict[str, dict]] = {}
    for edge in edges:
        if edge["type"] in {"DIRECTOR_OF", "OFFICER_OF"} and nodes.get(edge["source"], {}).get("type") == "Person":
            officer_edges.setdefault(edge["source"], {})[edge["target"]] = edge
    pair_edges: dict[tuple, list] = {}
    for edge in edges:
        pair_edges.setdefault(tuple(sorted((edge["source"], edge["target"]))), []).append(edge["id"])
    interlocks = []
    for person, orgs in officer_edges.items():
        ordered = sorted(orgs)
        for i, first in enumerate(ordered):
            for second in ordered[i + 1:]:
                if second in (adjacency.get(first) or set()):
                    interlocks.append((person, first, second))
    interlocks.sort()
    for person, first, second in interlocks[:MAX_INSIGHTS_PER_RULE]:
        evidence = [officer_edges[person][first]["id"], officer_edges[person][second]["id"],
                    *pair_edges.get(tuple(sorted((first, second))), [])[:1]]
        items.append({
            "rule": "shared-officer", "version": INSIGHT_RULES_VERSION,
            "title": "Personenverflechtung",
            "description": (f"{nodes[person]['name']} hat Leitungsrollen bei {nodes[first]['name']} "
                            f"und {nodes[second]['name']}; beide Organisationen sind direkt verbunden."),
            "support": round(min(officer_edges[person][first]["confidence"],
                                 officer_edges[person][second]["confidence"]), 4),
            "entities": [ref(person), ref(first), ref(second)],
            "evidence": evidence,
        })

    return {"version": INSIGHT_RULES_VERSION,
            "rules": {"nominee-chain": len(chains), "hub-spoke": len(stars),
                      "community-bridge": len(bridges), "shared-officer": len(interlocks)},
            "items": items[:MAX_INSIGHTS_TOTAL]}


def export_slices(raw: dict, output: Path = DEFAULT_OUT, *, allow_empty: bool = False) -> dict:
    graph = build_public_graph(raw)
    enrich_with_analytics(graph)
    insights = correlation_insights(graph)
    if insights["items"]:
        # Optional top-level block: consumers that do not know it ignore it, and
        # graphs without any pattern publish byte-identical to before Wave 4.
        graph["insights"] = insights
    if not graph["nodes"] and not allow_empty:
        raise ValueError("No releasable entities: refusing to erase existing publication; use --allow-empty deliberately")
    # Stable content hash: no clock-only commits or unnecessary Pages rebuilds.
    digest = hashlib.sha256(json.dumps(graph, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    graph["revision"] = digest
    dates = [node["updatedAt"][:10] for node in graph["nodes"] if node["updatedAt"]]
    graph["generatedAt"] = max(dates, default="1970-01-01")
    slices = {}
    adjacency: dict[str, list] = {node["id"]: [] for node in graph["nodes"]}
    for edge in graph["edges"]:
        adjacency[edge["source"]].append(edge)
        if edge["target"] != edge["source"]:
            adjacency[edge["target"]].append(edge)
    for node in graph["nodes"]:
        file_id = hashlib.sha256(node["id"].encode()).hexdigest()  # raw IDs never become paths
        slices[file_id + ".json"] = {"revision": digest, "demo": graph["demo"], "entity": node,
                                      "edges": adjacency[node["id"]]}
    manifest = output / "static-manifest.json"
    old = json.loads(manifest.read_text()) if manifest.exists() else {"entityFiles": []}
    for name, data in slices.items():
        atomic_json(output / "entities" / name, data)
    atomic_json(output / "graph_latest.json", graph)
    for name in old.get("entityFiles", []):
        if (isinstance(name, str) and len(name) == 69 and name.endswith(".json")
                and all(c in "0123456789abcdef" for c in name[:-5]) and name not in slices):
            (output / "entities" / name).unlink(missing_ok=True)
    atomic_json(manifest, {"revision": digest, "entityFiles": sorted(slices),
                           "entityIds": {node["id"]: hashlib.sha256(node["id"].encode()).hexdigest() + ".json" for node in graph["nodes"]}})
    return graph


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path)
    source.add_argument("--neo4j", action="store_true")
    source.add_argument("--reserve-run", choices=["schedule", "repository_dispatch", "workflow_dispatch"])
    parser.add_argument("--ledger", type=Path, default=ROOT / ".pipeline-state/daily-budget.json")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--allow-empty", action="store_true")
    args = parser.parse_args()
    if args.reserve_run:
        allowed = reserve_run(args.ledger, args.reserve_run)
        if output := os.getenv("GITHUB_OUTPUT"):
            with open(output, "a", encoding="utf-8") as file:
                file.write(f"allowed={'true' if allowed else 'false'}\n")
        print("Run reserved" if allowed else "Daily run budget exhausted")
        return
    if args.input and args.input.stat().st_size > 64 * 1024 * 1024:
        parser.error("Local export input exceeds 64 MiB")
    raw = json.loads(args.input.read_text(encoding="utf-8")) if args.input else read_neo4j()
    graph = export_slices(raw, args.out, allow_empty=args.allow_empty)
    print(f"Published {len(graph['nodes'])} released nodes, {len(graph['edges'])} relationships; revision {graph['revision'][:12]}")


if __name__ == "__main__":
    main()
