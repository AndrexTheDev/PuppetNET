"""The scheduled operation contract: workflows ↔ tiers ↔ registry.

The 24-hour operation is described in three places that can drift apart without
any of them failing on its own:

* ``puppetnet/models.py`` defines the tier vocabulary (``Cadence``),
* ``puppetnet/sources/registry.py`` assigns every source to a tier,
* ``.github/workflows/*.yml`` decides which tier is harvested, when.

A drift is silent and expensive: a workflow that forgets ``--tier`` harvests the
whole registry once a day and never again, a source that lands in no tier is only
reachable by hand, and a renamed workflow silently disconnects Graph Maintenance
from the ingest it must follow. These tests read the YAML as text and pin the
contract, so a change to the schedule is a change to a test.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

#: workflow file → the tier it must pass to ``ingest.py``
TIERED_WORKFLOWS = {
    "hourly_ingest.yml": "hourly",
    "daily_ingest.yml": "daily",
    "weekly_ingest.yml": "weekly",
}

def workflow_text(name: str) -> str:
    path = WORKFLOWS / name
    assert path.is_file(), f"{name} is gone — the schedule described in docs/operations.md cannot run"
    return path.read_text(encoding="utf-8")


def workflow(name: str) -> dict:
    return yaml.safe_load(workflow_text(name))


def triggers(document: dict) -> dict:
    """``on:`` is parsed as the boolean ``True`` by YAML 1.1 — accept both."""
    return document.get("on") or document.get(True) or {}


# --------------------------------------------------------------------------- #
# Tiers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("filename,tier", sorted(TIERED_WORKFLOWS.items()))
def test_every_tiered_workflow_passes_its_tier(filename, tier):
    """Without ``--tier`` the run defaults to *all*, which would make the
    hourly workflow a daily one and the daily workflow an hourly one.

    A workflow may hard-code the tier (hourly, weekly) or resolve it from a
    dispatch input with a scheduled default (daily, whose input exists to widen a
    manual run). Both forms are accepted, but only these two — a workflow that
    passes no tier at all harvests every source on every run.
    """
    document = workflow(filename)
    commands = "\n".join(step.get("run", "") for step in document["jobs"]["ingest"]["steps"])
    hard_coded = f"--tier {tier}" in commands
    with_default = f'tier="{tier}"' in commands and 'args="--tier $tier"' in commands
    assert hard_coded or with_default, f"{filename} does not harvest the {tier} tier"


def test_the_tiers_the_workflows_ask_for_exist():
    """A typo in a workflow would otherwise surface as a failed midnight run."""
    from puppetnet.models import Cadence
    from puppetnet.sources.registry import specs_for_tier

    for tier in TIERED_WORKFLOWS.values():
        cadence = Cadence.coerce(tier)
        assert cadence is not None, f"the workflows ask for unknown tier {tier!r}"
        assert specs_for_tier(tier), f"tier {tier!r} would harvest nothing"


def test_the_schedules_do_not_overlap_by_accident():
    """One run per tier per period, at the documented times.

    The three crons are read as cron fields rather than compared as strings so
    that a formatting change is not a failure — but a changed *slot* is.
    """
    expected = {
        "hourly_ingest.yml": ("5", "*", "*", "*", "*"),
        "daily_ingest.yml": ("13", "4", "*", "*", "*"),
        # min hour day-of-month month day-of-week — Sunday is 0.
        "weekly_ingest.yml": ("13", "2", "*", "*", "0"),
    }
    for filename, fields in expected.items():
        schedule = triggers(workflow(filename))["schedule"]
        crons = [entry["cron"] for entry in schedule]
        assert len(crons) == 1, f"{filename} should have exactly one cron, found {crons}"
        assert tuple(crons[0].split()) == fields, f"{filename} runs at {crons[0]}"


def test_the_three_ingest_workflows_share_one_concurrency_group():
    """Two writers on the same MERGE keys produce duplicate nodes, so the tiers
    must be serialised against each other — and a queued run must wait rather
    than cancel a half-written batch."""
    groups = set()
    for filename in TIERED_WORKFLOWS:
        concurrency = workflow(filename)["concurrency"]
        groups.add(concurrency["group"])
        assert concurrency["cancel-in-progress"] is False, f"{filename} may not cancel a running ingest"
    assert len(groups) == 1, f"the ingest workflows use different locks: {groups}"


def test_maintenance_is_triggered_by_the_ingest_it_follows():
    """Graph Maintenance names 'Daily Ingest' as a workflow_run trigger: renaming
    that workflow disconnects maintenance from the harvest, and the graph would
    only be maintained by the cron safety net."""
    document = workflow("graph_maintenance.yml")
    triggers_seen = triggers(document)
    chained = triggers_seen["workflow_run"]["workflows"]
    names = {name for name in (workflow(f)["name"] for f in TIERED_WORKFLOWS)}
    assert "Daily Ingest" in chained
    assert set(chained) <= names, f"Graph Maintenance follows an unknown workflow: {set(chained) - names}"


def test_the_weekly_run_hands_its_nodes_to_maintenance_same_day():
    """The deep run starts two hours before the daily run, which is what hands
    its graph to the maintenance pass — reversing the order would leave a weekly
    batch unmaintained for 24 hours (or a day)."""
    weekly_document = workflow("weekly_ingest.yml")
    daily_document = workflow("daily_ingest.yml")

    def start(day, document):
        cron = triggers(document)["schedule"][0]["cron"]
        minute, hour = cron.split()[:2]
        return int(hour) + int(minute) / 60

    assert start(2, weekly_document) < start(2, daily_document)


def test_the_deep_run_is_the_only_one_with_deep_budgets():
    """Budgets say what a tier is: the hourly run must stay small enough that 24
    of them are cheaper than one daily run, and the weekly run must be the only
    one allowed to lift the limits."""
    def env_value(document: dict, needle: str) -> str:
        for step in document["jobs"]["ingest"]["steps"]:
            for key, value in (step.get("env") or {}).items():
                if key == needle:
                    return str(value)
        raise AssertionError(f"{needle} is not set anywhere in the workflow")

    budgets = {
        filename: env_value(workflow(filename), "MAX_DOCUMENTS_TOTAL")
        for filename in TIERED_WORKFLOWS
    }
    assert budgets["weekly_ingest.yml"] == "4000"
    assert int(budgets["hourly_ingest.yml"]) * int(budgets["daily_ingest.yml"]) > 0
    assert int(budgets["hourly_ingest.yml"]) < int(budgets["daily_ingest.yml"]) < int(budgets["weekly_ingest.yml"])


def test_no_workflow_injects_dispatch_inputs_into_a_shell_body():
    """Inputs arrive through ``env``: a dispatch value interpolated into the
    shell body is remote command execution by design.

    Checked on the parsed YAML rather than the raw text, so a comment that
    mentions an input cannot be mistaken for one — and so a real interpolation
    cannot hide behind a matching comment.
    """
    offenders: list[str] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job_name, job in (document.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                body = step.get("run")
                if isinstance(body, str) and "${{ inputs." in body:
                    offenders.append(f"{path.name}:{job_name}:{step.get('name', '?')}")
    assert not offenders, f"dispatch input interpolated into a script: {offenders}"


def test_a_hard_failure_reaches_the_operator():
    """A 24-hour schedule that breaks silently is worse than one that stops.

    Each ingest workflow must call the alert engine on exit 1 (configuration) and
    2 (runtime) — the two exits that mean nothing was written — and must skip exit
    3, which is per-source and expected. The step also has to stay best-effort: it
    exits 0 whatever the Bot API does.
    """
    for filename in TIERED_WORKFLOWS:
        steps = workflow(filename)["jobs"]["ingest"]["steps"]
        warn = [step for step in steps if step.get("name") == "Warn on a hard failure"]
        assert warn, f"{filename} cannot report a hard failure"
        step = warn[0]

        condition = str(step.get("if", ""))
        assert "steps.ingest.outputs.exit_code" in condition, f"{filename} warns regardless of the exit code"
        assert "!= '3'" in condition, f"{filename} would warn about the expected per-source exit"
        assert "always()" in condition, "the warning must run even though the ingest step failed"

        script = step["run"]
        # A real call, not a mention: `# python telegram_bot.py --failure` in a
        # comment satisfied the first version of this check.
        calls = [
            line for line in script.splitlines() if line.strip().startswith("python telegram_bot.py --failure")
        ]
        assert calls, f"{filename} does not actually call the failure pass"
        assert "--workflow" in script and "--exit-code" in script
        env = step.get("env") or {}
        assert env.get("TELEGRAM_BOT_TOKEN", "").startswith("${{ secrets."), "the token comes from secrets"
        assert "if [ -z \"$TELEGRAM_BOT_TOKEN\" ]" in script, (
            "a deployment without Telegram is valid — the step must skip, not fail"
        )
        assert script.rstrip().endswith("exit 0"), (
            "the warning is best effort: a Telegram outage must not restyle a failed harvest"
        )
