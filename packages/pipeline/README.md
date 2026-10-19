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
