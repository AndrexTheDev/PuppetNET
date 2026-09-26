"""PuppetNET — serverless OSINT network-analysis harvest engine.

Public API surface::

    from puppetnet.config import Settings, load_settings
    from puppetnet.pipeline import IngestPipeline, PipelineOptions, run_pipeline
    from puppetnet.parsing.nlp_engine import NLPEngine
    from puppetnet.models import Document, Entity, Relation, RelationType

``ingest.py`` at the repository root is the executable entry point used by the
GitHub Actions cron workflow.
"""

from __future__ import annotations

__version__ = "1.4.0"
__all__ = ["__version__", "Settings", "load_settings", "IngestPipeline", "PipelineOptions", "run_pipeline"]


def __getattr__(name: str):  # pragma: no cover - lazy re-exports
    """Lazily expose the most-used symbols without importing spaCy/neo4j at import time."""
    if name in {"Settings", "load_settings"}:
        from .config import Settings, load_settings

        return {"Settings": Settings, "load_settings": load_settings}[name]
    if name in {"IngestPipeline", "PipelineOptions", "run_pipeline"}:
        from .pipeline import IngestPipeline, PipelineOptions, run_pipeline

        return {"IngestPipeline": IngestPipeline, "PipelineOptions": PipelineOptions, "run_pipeline": run_pipeline}[name]
    raise AttributeError(f"module 'puppetnet' has no attribute {name!r}")
