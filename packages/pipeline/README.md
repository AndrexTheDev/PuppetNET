# PuppetNET pipeline

Python ingestion and NLP engine, retained from commit 127ab47.

From the repository root: `pip install -e ".[dev]"` (compatibility path),
or `pip install -e "./packages/pipeline[dev]"`.
Run `python packages/pipeline/ingest.py --dry-run --list-sources`.

Configuration lives in `config/sources.yaml` beside this file.
The process working directory controls local report/state paths. No paid models or keys are required.
