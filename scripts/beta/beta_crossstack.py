"""Beta S11: cross-stack data contract — pipeline -> export -> web build -> browser.

1. S7 rehearsal (real pipeline, Gemini document mode, fake endpoint).
2. Writer rows are projected exactly like ``read_neo4j`` returns them
   (nodes / edges / evidence + feed provenance), then an operator releases two
   reviewed cloud edges (``public_released``) — the documented review path.
3. ``export_slices`` writes into a throw-away copy of the repository.
4. ``validateStaticGraph`` from apps/web (TypeScript) must accept the file.
5. With ``--build``: ``next build`` in the copy, static server, Chromium checks
   that the dashboard boots with this live dataset (not demo/standby) and that
   the entity page shows the released relation.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).parent))

import beta_pipeline as bp  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))


def neo4j_projection(captured: list[dict]) -> dict:
    """Same shape as export_static_slices.read_neo4j()."""
    nodes, documents = {}, {}
    for batch in captured:
        for row in batch["rows"]:
            if batch["label"].startswith("entities:"):
                node = nodes.setdefault(row["canonical_key"], {
                    "canonical_key": row["canonical_key"], "name": row["name"], "entity_type": row["entity_type"],
                    "confidence": row["confidence"], "doc_ids": [], "updated_at": row["last_seen"],
                    "cloud_only": True})
                node["doc_ids"] = sorted({*node["doc_ids"], *row.get("doc_ids", [])})
                # same AND semantics as the Neo4j upsert; null = locally seen
                node["cloud_only"] = (node["cloud_only"] and row.get("cloud_only") is True) or None
            elif batch["label"] == "documents":
                documents[row["doc_id"]] = {"id": row["doc_id"], "title": row["title"], "url": row["url"],
                                            "source": row["source_id"], "date": row["published_at"],
                                            "feed_url": row["feed_url"]}
    edges = [{"source": r["subject_key"], "target": r["object_key"], "type": r["_type"], "method": r["method"],
              "confidence": r["confidence"], "weight": r.get("weight"), "doc_ids": [r["doc_id"]], "doc_id": r["doc_id"],
              "negated": r.get("negated"), "hedged": r.get("hedged")} for r in bp.relation_rows(captured)]
    sources = {doc_id: doc["feed_url"] for doc_id, doc in documents.items()}
    for row in [*nodes.values(), *edges]:
        row["feed_urls"] = sorted({sources[d] for d in [*(row.get("doc_ids") or []), row.get("doc_id")] if d in sources})
    return {"nodes": list(nodes.values()), "edges": edges, "evidence": list(documents.values()), "demo": False,
            "truncated": False}


def copy_repo(dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    ignore = shutil.ignore_patterns(".git", "node_modules", ".next", "out", ".state", "__pycache__", ".venv")
    shutil.copytree(REPO, dest, ignore=ignore, symlinks=True)
    for nm in REPO.rglob("node_modules"):
        if "node_modules" in nm.parent.parts or not nm.is_dir():
            continue
        target = dest / nm.relative_to(REPO)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.symlink_to(nm)


def main(build: bool) -> None:
    from src.export_static_slices import export_slices

    pipe, calls, captured, tmp = bp.run_pipeline()
    raw = neo4j_projection(captured)
    for edge in raw["edges"]:
        if edge["type"] == "ACQUIRED" or (edge["type"] == "OFFICER_OF" and "brenner" in edge["source"]):
            edge["public_released"] = True  # operator review in Neo4j
    work = Path("/tmp/puppetnet-crossstack")
    copy_repo(work)
    out = work / "apps/web/public/data"
    os.environ["VERIFIED_RSS_FEEDS"] = bp.FEED
    os.environ.pop("GEMINI_AUTO_RELEASE", None)
    graph = export_slices(raw, out)
    names = sorted(n["name"] for n in graph["nodes"])
    types = sorted(e["type"] for e in graph["edges"])
    print("   nodes:", names)
    print("   edges:", types)
    check("S11 export publishes exactly the two operator-released cloud edges", types == ["ACQUIRED", "OFFICER_OF"],
          str(types))
    check("S11 unreleased cloud edges stay private (LOCATED_IN, REGISTERED_IN, OPERATES_IN)",
          not {"LOCATED_IN", "REGISTERED_IN", "OPERATES_IN"} & set(types))
    cloud_only = sorted(n["name"] for n in raw["nodes"] if n.get("cloud_only"))
    check("S11 entities seen only by the cloud model are not published", cloud_only == ["Jonas Weber", "Limassol"]
          and not set(cloud_only) & set(names), f"cloud_only={cloud_only}")
    check("S11 endpoints of the released edges are public", {"Nordhafen Logistik AG", "Baltic Freight GmbH",
          "Karla Brenner"} <= set(names), str(names))
    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.json"))
    check("S11 graph_latest.json + manifest + entity slices written", {"graph_latest.json", "static-manifest.json"}
          <= set(files) and any(f.startswith("entities/") for f in files), f"{len(files)} files")

    validate = (
        "import { readFileSync } from 'node:fs';"
        "import { validateStaticGraph } from './apps/web/src/lib/dataFetcher.ts';"
        "const r = validateStaticGraph(JSON.parse(readFileSync('apps/web/public/data/graph_latest.json','utf8')));"
        "console.log(JSON.stringify({nodes: r.dataset.nodes.length, edges: r.dataset.edges.length, "
        "demo: Boolean(r.dataset.demo)}));")
    proc = subprocess.run(["node", "--import", "tsx", "--input-type=module", "-e", validate], cwd=work,
                          capture_output=True, text=True, timeout=120)
    detail = (proc.stdout.strip() or proc.stderr.strip())[-400:]
    check("S11 web validateStaticGraph accepts the pipeline export", proc.returncode == 0, detail)
    if not build:
        return

    env = dict(os.environ, NEXT_TELEMETRY_DISABLED="1", SITE_ORIGIN="https://puppetnet-web.pages.dev")
    t0 = time.time()
    proc = subprocess.run(["npm", "run", "build"], cwd=work, capture_output=True, text=True, timeout=1200, env=env)
    check("S11 next build with live data", proc.returncode == 0, f"{time.time() - t0:.0f}s "
          + (proc.stdout + proc.stderr).strip().splitlines()[-1][-200:])
    if proc.returncode != 0:
        print((proc.stdout + proc.stderr)[-3000:])
        return
    site = work / "apps/web/out"
    key = next(n["id"] for n in graph["nodes"] if n["name"] == "Nordhafen Logistik AG")
    pages = sorted(p.parent.name for p in (site / "entity").glob("*/index.html"))
    check("S11 static entity pages generated from the export", len(pages) == len(graph["nodes"]), f"{pages}")
    browser = subprocess.run(["node", str(Path(__file__).parent / "crossstack_browser.mjs"), str(site), key],
                             cwd=REPO, capture_output=True, text=True, timeout=300)
    print(browser.stdout.strip())
    if browser.returncode != 0:
        print(browser.stderr[-2000:])
    for line in browser.stdout.splitlines():
        if line.startswith(("PASS ", "FAIL ")):
            RESULTS.append((line[5:], line.startswith("PASS "), ""))
    if not any(line.startswith(("PASS ", "FAIL ")) for line in browser.stdout.splitlines()):
        check("S11 browser run", False, browser.stderr[-300:])


if __name__ == "__main__":
    try:
        main(build="--build" in sys.argv)
    except Exception as exc:  # pragma: no cover - harness
        import traceback
        traceback.print_exc()
        check("S11 crashed", False, repr(exc))
    failed = [r for r in RESULTS if not r[1]]
    print(f"\nCROSS-STACK BETA: {len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    sys.exit(1 if failed else 0)
