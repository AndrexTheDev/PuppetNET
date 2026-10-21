#!/usr/bin/env bash
# Beta rehearsal of the Gemini free-tier path (docs/testing/beta-2026-10-21.md).
# Offline: a local fake Gemini endpoint on 127.0.0.1, no API key, no quota use.
# Runs outside pytest on purpose: tests/conftest.py blocks all sockets.
set -euo pipefail
cd "$(dirname "$0")/../.."
status=0
for script in beta_broker beta_pipeline beta_validator; do
  echo "== $script"
  python "scripts/beta/$script.py" || status=1
done
exit "$status"
