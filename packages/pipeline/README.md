# PuppetNET Python pipeline

From repository root:

```sh
pip install -e './packages/pipeline[dev]'
python -m pytest -q -m 'not integration'
python packages/pipeline/ingest.py --dry-run --list-sources
```

Optional CPU GLiNER:

```sh
pip install 'torch>=2.2,<3' --index-url https://download.pytorch.org/whl/cpu
pip install -e './packages/pipeline[gliner]'
NLP_BACKEND=gliner DRY_RUN=true python packages/pipeline/ingest.py --doctor
```

GLiNER weights download on first use; no paid inference API. Set `NLP_BACKEND=gliner`
to select it explicitly, otherwise spaCy remains the local default. See
`docs/2.0-scaffold.md` at repository root for model/relationship limitations and live
Neo4j setup. Source configuration is in `config/sources.yaml` beside this file.
Report/state locations are relative to the process working directory.

## Multilingual extraction (v1.6)

The `nlp/` package mirrors `puppetnet/parsing/` for the multilingual path:

| Module | Purpose |
| --- | --- |
| `nlp/labels.py` | Nine OSINT labels → `EntityType` / spaCy label |
| `nlp/chunking.py` | Sentence-aligned 384-token windows with 64-token overlap |
| `nlp/extractor.py` | `urchade/gliner_multi-v2.1` via PyTorch or ONNX Runtime |
| `nlp/triplet_extractor.py` | Workers AI triples with a 10,000-neuron/day ledger |
| `nlp/graph_bridge.py` | Triples → existing `Entity`/`Relation` model |

## Graph maintenance (v1.7)

| Module | Purpose |
| --- | --- |
| `entity_resolution.py` | APOC Jaro-Winkler/Levenshtein blocking, `apoc.refactor.mergeNodes`, evidence audit |
| `graph_analytics.py` | Dedupe/prune/centrality plus `--puppet-master`, `--cycles` and `--guard` |
| `puppetnet/graph/puppet_master.py` | Cypher PageRank + betweenness + in-degree blend, SCC cycle detection, 180k guard |
| `keep_alive.py` | Read-only `RETURN 1` heartbeat against AuraDB Free's 72-hour pause |

```sh
python entity_resolution.py --dry-run --json      # plan APOC merges
python graph_analytics.py --guard --json          # exit 4 at 180 000 nodes
python graph_analytics.py --all --json            # dedupe → prune → centrality → puppet-master → cycles
python keep_alive.py --json                       # the daily heartbeat
```

```sh
# entity extraction only (torch)
pip install 'torch>=2.2,<3' --index-url https://download.pytorch.org/whl/cpu
pip install -e '.[gliner]'
python - <<'PY'
from nlp.extractor import GLiNERExtractor
with GLiNERExtractor(eager_load=False) as extractor:
    print(extractor.extract("Dr. Anna Müller traf Acme in Berlin.", language="de").to_dict())
PY

# optional relation extraction (free Workers AI tier)
export TRIPLET_BACKEND=cloudflare
export CLOUDFLARE_ACCOUNT_ID=... CLOUDFLARE_API_TOKEN=...
python - <<'PY'
from nlp.triplet_extractor import CloudflareTripletExtractor
from puppetnet.config import load_settings
with CloudflareTripletExtractor.from_settings(load_settings()) as extractor:
    print(extractor.extract("Die Acme GmbH gehört der Familie Müller.", language="de").to_dict())
PY
```

`CF_AI_MAX_DOCUMENTS` caps the stage per ingest run (`0` disables it). The ledger
is `.state/cloudflare_ai_budget.json` by default, resets by UTC date and is also
enforced in-process when the filesystem is read-only. Full guide:
`docs/nlp-pipeline.md#multilingual-gliner--workers-ai-v16`.
