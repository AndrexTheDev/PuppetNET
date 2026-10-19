"""Shared pytest fixtures.

The suite deliberately never downloads a spaCy model: network egress to
``raw.githubusercontent.com`` is unavailable in CI sandboxes, and the
production code path is designed to degrade gracefully. Tests that need a
dependency parse use the API-compatible doubles in :mod:`tests.fakes`.
"""

from __future__ import annotations

import logging
import os

import pytest

from puppetnet.config import load_settings

logging.disable(logging.CRITICAL)

#: Every test runs hermetically — no Neo4j, no Cloudflare Worker, no network.
TEST_ENV = {
    "DRY_RUN": "true",
    "LOG_LEVEL": "ERROR",
    "SPACY_MODEL": "",
    "NEO4J_URI": "",
    "NEO4J_USER": "",
    "NEO4J_PASSWORD": "",
    "EDGE_WORKER_URL": "",
    "EDGE_WORKER_TOKEN": "",
    "STATE_DIR": "",
}


@pytest.fixture()
def settings():
    """Minimal, offline-safe settings object."""
    env = {k: v for k, v in os.environ.items()}
    env.update(TEST_ENV)
    return load_settings(env)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail loudly if any test reaches out to the network."""

    def _blocked(*args, **kwargs):  # pragma: no cover - only on misuse
        raise AssertionError("tests must not perform network I/O")

    import socket

    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket.socket, "connect", _blocked)
