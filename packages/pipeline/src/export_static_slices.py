"""Publish a bounded, explicitly approved graph for ALL static consumers.

python packages/pipeline/src/export_static_slices.py --input reviewed-graph.json
python packages/pipeline/src/export_static_slices.py --neo4j
No database-to-public dump: nodes AND relationships require public=true.
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
    """Durable quota shared by scheduled/manual runs: TOTAL <=2/day, cron <=1.

    Caller MUST serialize writers and persist this file even on ingestion failure.
    This is not a distributed database lock. A missing file starts a new ledger;
    a corrupt file raises and fails closed instead of silently replenishing quota.
    """
    day = day or datetime.now(timezone.utc).date().isoformat()
    state = json.loads(path.read_text()) if path.exists() else {}
    if state.get("day") != day:
        state = {"day": day, "total": 0, "scheduled": 0, "manual": 0}
    if event not in {"schedule", "repository_dispatch", "workflow_dispatch"}:
        raise ValueError("Unsupported run trigger")
    if any(type(state.get(k)) is not int or not 0 <= state[k] <= 2 for k in ("total", "scheduled", "manual")):
        raise ValueError("Invalid daily quota ledger")
    if state["scheduled"] > 1 or state["manual"] + state["scheduled"] != state["total"]:
        raise ValueError("Inconsistent daily quota ledger")
    kind = "scheduled" if event == "schedule" else "manual"
    if state["total"] >= 2 or (kind == "scheduled" and state["scheduled"] >= 1):
        return False
    state["total"] += 1
    state[kind] += 1
    atomic_json(path, state)
    return True


def approved(row: dict) -> bool:
    return row.get("public") is True and not row.get("private") and not row.get("noindex")


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
    """Two bounded queries in one read transaction; no unrestricted properties()."""
    from neo4j import GraphDatabase, unit_of_work
    uri, user, password = (os.environ.get(k, "") for k in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD"))
    if not all((uri, user, password)):
        raise ValueError("NEO4J_URI, NEO4J_USERNAME and NEO4J_PASSWORD are required")
    if not uri.startswith("neo4j+s://"):
        raise ValueError("Production export requires a TLS-verified neo4j+s:// URI")

    @unit_of_work(timeout=20)
    def read(tx):
        nodes = [dict(row["node"]) for row in tx.run("""
            MATCH (e:Entity) WHERE e.public = true
              AND coalesce(e.private,false) = false AND coalesce(e.noindex,false) = false
            WITH e ORDER BY coalesce(e.updated_at,e.last_seen,'') DESC, e.canonical_key
            LIMIT $limit
            RETURN e{.canonical_key,.name,.entity_type,.description,.public,.private,.noindex,
                     .updated_at,.last_seen,.lat,.lon,.confidence,.doc_ids} AS node
        """, limit=limit)]
        keys = [node["canonical_key"] for node in nodes]
        edges = [dict(row["edge"]) for row in tx.run("""
            MATCH (a:Entity)-[r]->(b:Entity)
            WHERE a.canonical_key IN $keys AND b.canonical_key IN $keys
              AND r.public = true AND coalesce(r.private,false) = false
              AND coalesce(r.noindex,false) = false AND coalesce(r.requires_review,false) = false
              AND type(r) <> 'PUPPET_MASTER_OF'
            RETURN {source:a.canonical_key,target:b.canonical_key,type:type(r),public:true,
                    confidence:r.confidence,weight:r.weight} AS edge
            ORDER BY edge.source,edge.target,edge.type LIMIT $limit
        """, keys=keys, limit=MAX_EDGES)]
        evidence = [dict(row["doc"]) for row in tx.run("""
            MATCH (e:Entity)-[r:EVIDENCED_BY|MENTIONS]-(d:Document)
            WHERE e.canonical_key IN $keys AND d.public = true AND r.public = true
              AND coalesce(d.private,false) = false AND coalesce(d.noindex,false) = false
            RETURN DISTINCT {id:d.doc_id,title:d.title,url:d.url,source:d.source_id,
                             date:d.published_at,public:true} AS doc LIMIT 5000
        """, keys=keys)]
        return {"nodes": nodes, "edges": edges, "evidence": evidence, "demo": False}

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
    by_id = {}
    for row in raw["nodes"]:
        if not isinstance(row, dict) or not approved(row):
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
                "entity_type": kind, "public": True, "slug": entity_slug(key, name),
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
        if not isinstance(item, dict) or not approved(item):
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
        if not isinstance(row, dict) or not approved(row) or row.get("requires_review"):
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
                            "public": True, "confidence": score(row.get("confidence")), "weight": score(row.get("weight"))}
    return {"schemaVersion": 1, "demo": raw.get("demo") is True, "nodes": nodes,
            "edges": [edges[key] for key in sorted(edges)[:MAX_EDGES]],
            "evidence": sorted(evidence, key=lambda item: item["id"]), "truncated": len(by_id) > len(nodes) or len(edges) > MAX_EDGES}


def export_slices(raw: dict, output: Path = DEFAULT_OUT, *, allow_empty: bool = False) -> dict:
    graph = build_public_graph(raw)
    if not graph["nodes"] and not allow_empty:
        raise ValueError("No explicitly public entities: refusing to erase existing publication; use --allow-empty deliberately")
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
    print(f"Published {len(graph['nodes'])} reviewed nodes, {len(graph['edges'])} relationships; revision {graph['revision'][:12]}")


if __name__ == "__main__":
    main()
