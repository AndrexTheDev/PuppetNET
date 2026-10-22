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


def test_connection_check_is_manual_read_only_and_cannot_block_production():
    text = (ROOT / '.github/workflows/daily_ingestion.yml').read_text()
    data = yaml.safe_load(text)
    triggers = data.get('on', data.get(True))
    # Branch pushes share the production concurrency group: they must not start
    # runs that could replace a pending scheduled run.
    assert 'push' not in triggers
    task = triggers['workflow_dispatch']['inputs']['task']
    assert task['options'] == ['ingest', 'connection-check'] and task['default'] == 'ingest'
    check = data['jobs']['verify-connections']
    assert check['if'] == "github.event_name == 'workflow_dispatch' && inputs.task == 'connection-check'"
    assert check['permissions'] == {'contents': 'read'}
    runs = '\n'.join(step.get('run', '') for step in check['steps'])
    assert 'git push' not in runs and 'git commit' not in runs
    assert any(step.get('uses', '').startswith('actions/upload-artifact@') for step in check['steps'])
    assert "inputs.task != 'connection-check'" in data['jobs']['publish']['if']


def test_every_production_push_rebases_once_and_never_forces():
    text = (ROOT / '.github/workflows/daily_ingestion.yml').read_text()
    pushes = [line.strip() for line in text.splitlines() if 'git push' in line]
    assert len(pushes) == 3
    retry = ('git push origin "HEAD:$PUBLISH_BRANCH" || { git pull --rebase origin "$PUBLISH_BRANCH" && '
             'git push origin "HEAD:$PUBLISH_BRANCH"; }')
    assert all(line == retry for line in pushes), pushes
    assert '--force' not in text and 'push -f' not in text


def test_only_the_data_commit_may_spend_a_cloudflare_pages_build():
    # Pages Free allows 500 builds/month. Cloudflare documents the skip flag only
    # as a commit-message PREFIX; build watch paths are a dashboard setting that
    # can change. Ledger commits therefore lead with [skip ci], and the single
    # data commit (the reason a build exists) carries no flag.
    import re

    text = (ROOT / '.github/workflows/daily_ingestion.yml').read_text()
    messages = re.findall(r'git commit -m "([^"]+)"', text)
    assert len(messages) == 3, messages
    data = [m for m in messages if m.startswith('data: ')]
    assert data == ['data: publish auto-released static graph']
    for message in messages:
        if message not in data:
            assert message.lower().startswith('[skip ci] '), message
    assert 'skip' not in data[0].lower()
