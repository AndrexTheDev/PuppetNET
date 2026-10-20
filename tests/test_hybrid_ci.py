"""Hybrid deployment contract tested in Python CI without JavaScript tooling."""
from __future__ import annotations

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_only_python_workflows_remain():
    workflows = list((ROOT / '.github/workflows').glob('*.yml'))
    assert sorted(path.name for path in workflows) == ['daily_ingestion.yml', 'ingestion-pipeline.yml']
    text = (ROOT / '.github/workflows/ingestion-pipeline.yml').read_text()
    data = yaml.safe_load(text)
    assert data['permissions'] == {'contents': 'read'}
    assert data['concurrency']['cancel-in-progress'] is False
    for job in data['jobs'].values():
        assert job['runs-on'] == 'ubuntu-latest'
    for forbidden in ['npm ', 'setup-node', 'wrangler', 'ci-budget', 'billing', 'GH_TOKEN']:
        assert forbidden not in text
    assert not (ROOT / 'scripts/ci-budget.mjs').exists()


def test_ingestion_requires_tests_main_optin_and_real_secrets():
    data = yaml.safe_load((ROOT / '.github/workflows/ingestion-pipeline.yml').read_text())
    job = data['jobs']['ingestion']
    assert job['needs'] == 'tests'
    for guard in ['refs/heads/main', "ENABLE_INGESTION == 'true'", "github.event_name == 'schedule'", 'inputs.run_ingestion']:
        assert guard in job['if']
    assert job['env']['NLP_BACKEND'] == 'gliner'
    assert job['env']['DRY_RUN'] == 'false'
    assert any('GLiNEREngine' in step.get('run', '') for step in job['steps'])
    credentials = next(step['run'] for step in job['steps'] if step.get('name') == 'Require actual database credentials')
    for name in ['NEO4J_URI', 'NEO4J_USER', 'NEO4J_PASSWORD']:
        assert name in credentials


def test_pages_and_worker_have_independent_build_paths():
    data = json.loads((ROOT / 'cloudflare-builds.json').read_text())
    for component, directory in [('pages', 'apps/web'), ('worker', 'apps/worker')]:
        assert data[component]['rootDirectory'] == directory
        assert directory + '/*' in data[component]['watchPaths']
        assert 'package-lock.json' in data[component]['watchPaths']
    assert data['pages']['outputDirectory'] == 'out'
    assert data['pages']['deployCommand'] is None
    assert 'deploy:pages' in data['pages']['manualDeployCommand']
    assert 'versions upload' in data['worker']['previewCommand']
    assert 'pages_build_output_dir' in (ROOT / 'apps/web/wrangler.toml').read_text()
    assert 'main = "src/index.ts"' in (ROOT / 'apps/worker/wrangler.toml').read_text()
