"""Beta scenarios S1-S6 for the Gemini free-tier broker against a real HTTP fake."""
from __future__ import annotations

import json
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).parent))

import src.gemini_broker as gb  # noqa: E402
from fake_gemini import KEY, FakeGemini, SharedClock  # noqa: E402
from src.gemini_tasks import RESPONSE_SCHEMA  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
A, B = "gemini-flash-lite-latest", "gemini-3.1-flash-lite"
FREE = {"rpm": 15, "rpd": 500, "tpm": 250_000}


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))


def make_broker(ledger: Path, clock: SharedClock, base: str, now: datetime, **env: object) -> gb.GeminiBroker:
    gb.API_BASE = base
    values = {"ENABLE_GEMINI_BROKER": "true", "GEMINI_API_KEY": KEY, "GITHUB_RUN_ID": "run"}
    values.update({k: str(v) for k, v in env.items()})
    return gb.GeminiBroker(ledger, env=values, clock=clock, sleep=clock.sleep, now=lambda: now)


DOC_PROMPT = "ALLOWED_PREDICATES=...\nKNOWN_ENTITIES=[]\nDATA=" + json.dumps({"title": "t", "text": "Wort " * 2400})  # ~12k chars


def s1_public_day() -> None:
    """8 scheduled + 3 manual runs x 30 documents, true free-tier limits on the server."""
    clock = SharedClock()
    tmp = Path(tempfile.mkdtemp())
    with FakeGemini(clock, {A: dict(FREE), B: dict(FREE)}) as fake:
        now = datetime(2026, 10, 21, 8, 0, tzinfo=timezone.utc)  # 01:00 PDT
        ok_total, runtime_max, shares = 0, 0.0, []
        for run in range(11):
            broker = make_broker(tmp / "ledger.json", clock, fake.base, now, GITHUB_RUN_ID=f"run-{run}",
                                 GEMINI_RUNS_PER_DAY=8, GEMINI_RUN_SECONDS=420)
            start = clock()
            ok = 0
            for _ in range(30):
                if broker.generate_json("extract", (A, B), DOC_PROMPT, system="rules", schema=RESPONSE_SCHEMA,
                                        max_output_tokens=4096, max_input_tokens=8000):
                    ok += 1
            shares.append(broker.run_base.get(A))
            runtime_max = max(runtime_max, clock() - start)
            ok_total += ok
            clock.advance(3 * 3600)  # ~3 h until the next slot (stays on the same Pacific day here)
        statuses = [e["status"] for e in fake.log]
        ledger = json.loads((tmp / "ledger.json").read_text())
    check("S1 public day: all 330 document requests succeed", ok_total == 330, f"ok={ok_total}")
    check("S1 public day: zero 429 from the server", statuses.count(429) == 0, f"429={statuses.count(429)}")
    check("S1 public day: model A stays <= 80 % of 500 RPD", ledger["models"][A]["requests"] <= 400,
          f"A={ledger['models'][A]['requests']}, B={ledger['models'].get(B, {}).get('requests', 0)}")
    check("S1 public day: every run fits into GEMINI_RUN_SECONDS=420 (simulated)", runtime_max <= 420,
          f"slowest run {runtime_max:.0f}s (throttled to 12 RPM)")
    check("S1 public day: fair share never starves later runs", all(s and s >= 30 for s in shares), f"shares={shares}")
    check("S1 public day: ledger counts 11 runs", len(ledger["runs"]) == 11)


def s2_real_limit_lower() -> None:
    """Project really has 20 RPD on A (as on Flash) but the broker assumes 500."""
    clock = SharedClock()
    tmp = Path(tempfile.mkdtemp())
    with FakeGemini(clock, {A: {"rpm": 15, "rpd": 20, "tpm": 250_000}, B: dict(FREE)}) as fake:
        now = datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc)
        results = []
        for run in range(3):
            broker = make_broker(tmp / "l.json", clock, fake.base, now, GITHUB_RUN_ID=f"r{run}", GEMINI_RUNS_PER_DAY=8)
            results += [broker.generate_json("extract", (A, B), "x") for _ in range(30)]
            clock.advance(3600)
        ledger = json.loads((tmp / "l.json").read_text())
        statuses = [(e["model"], e["status"]) for e in fake.log]
    count429 = sum(1 for _, s in statuses if s == 429)
    check("S2 lower real RPD: exactly one 429 in the whole day", count429 == 1, f"429={count429}")
    check("S2 lower real RPD: A marked exhausted + learned rpd=20", ledger["models"][A]["exhausted"] and ledger["learned"][A].get("rpd") == 20)
    check("S2 lower real RPD: work continues on B (90/90 ok)", sum(r is not None for r in results) == 90,
          f"ok={sum(r is not None for r in results)}")
    after = [m for m, s in statuses[statuses.index((A, 429)) + 1:]]
    check("S2 lower real RPD: no further request to A after the 429", A not in after)


def s3_rpm_lower() -> None:
    """Project really has 5 RPM on A while the broker plans 12."""
    clock = SharedClock()
    tmp = Path(tempfile.mkdtemp())
    with FakeGemini(clock, {A: {"rpm": 5, "rpd": 500, "tpm": 250_000}}) as fake:
        now = datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc)
        broker = make_broker(tmp / "l.json", clock, fake.base, now, GEMINI_RUN_SECONDS=900)
        ok = sum(broker.generate_json("extract", (A,), "x") is not None for _ in range(20))
        n429 = sum(1 for e in fake.log if e["status"] == 429)
        learned = json.loads((tmp / "l.json").read_text())["learned"].get(A)
        unavailable = dict(broker.unavailable)
        # A second run on the same day plans with the learned rpm and never hits 429.
        fake.log.clear()
        clock.advance(3600)
        second = make_broker(tmp / "l.json", clock, fake.base, now, GITHUB_RUN_ID="r2", GEMINI_RUN_SECONDS=900)
        ok2 = sum(second.generate_json("extract", (A,), "x") is not None for _ in range(20))
        n429_2 = sum(1 for e in fake.log if e["status"] == 429)
    check("S3 lower real RPM: learns rpm=5 from QuotaFailure", learned == {"rpm": 5}, f"learned={learned}")
    check("S3 lower real RPM: first run recovers via RetryInfo or cools down cleanly",
          ok >= 5 and n429 <= 2, f"ok={ok}/20, 429={n429}, unavailable={unavailable}")
    check("S3 lower real RPM: next run uses learned limit → 0 x 429, 20/20 ok", n429_2 == 0 and ok2 == 20,
          f"ok={ok2}, 429={n429_2}, planned={second.planned_limits(A)}")


def s4_faults() -> None:
    clock = SharedClock()
    tmp = Path(tempfile.mkdtemp())
    now = datetime(2026, 10, 21, 18, 0, tzinfo=timezone.utc)
    cases = {}
    for mode in ("http500", "truncated", "blocked", "empty", "fence"):
        with FakeGemini(clock, {A: dict(FREE, mode=mode), B: dict(FREE)},
                        responder=lambda m, b: {"entities": [], "relations": []}) as fake:
            broker = make_broker(tmp / f"{mode}.json", clock, fake.base, now)
            r1 = broker.generate_json("extract", (A, B), "x")
            r2 = broker.generate_json("extract", (A, B), "x")
            r3 = broker.generate_json("extract", (A, B), "x")
            cases[mode] = ([r and r.model for r in (r1, r2, r3)], [e["model"] for e in fake.log])
    check("S4 fence: Gemma-style ```json fence is accepted", cases["fence"][0] == [A, A, A], str(cases["fence"]))
    for mode in ("http500", "truncated", "blocked", "empty"):
        models, calls = cases[mode]
        check(f"S4 {mode}: falls back to B, A parked after 2 failures", models == [B, B, B] and calls.count(A) == 2,
              f"results={models}, calls={calls}")
    # Wrong key: disabled after the first 403, never retried.
    with FakeGemini(clock, {A: dict(FREE)}) as fake:
        gb.API_BASE = fake.base
        broker = gb.GeminiBroker(tmp / "k.json", env={"ENABLE_GEMINI_BROKER": "true", "GEMINI_API_KEY": "wrong"},
                                 clock=clock, sleep=clock.sleep, now=lambda: now)
        rs = [broker.generate_json("extract", (A,), "x") for _ in range(5)]
        check("S4 403 wrong key: one request, broker disabled", len(fake.log) == 1 and not broker.enabled and rs == [None] * 5)
    # Unknown/retired model (404) in the cascade.
    with FakeGemini(clock, {B: dict(FREE)}) as fake:
        broker = make_broker(tmp / "404.json", clock, fake.base, now)
        rs = [broker.generate_json("extract", ("gemini-2.5-flash-lite", B), "x") for _ in range(3)]
        calls = [e["model"] for e in fake.log]
        check("S4 404 retired model: one probe, then only B", calls.count("gemini-2.5-flash-lite") == 1 and all(r and r.model == B for r in rs), str(calls))
    # Real network timeout (smallest allowed timeout = 5 s).
    with FakeGemini(clock, {A: dict(FREE, mode="slow:7"), B: dict(FREE)}) as fake:
        broker = make_broker(tmp / "slow.json", clock, fake.base, now, GEMINI_REQUEST_TIMEOUT_SECONDS=5)
        t0 = time.monotonic()
        r = broker.generate_json("extract", (A, B), "x")
        took = time.monotonic() - t0
        led = json.loads((tmp / "slow.json").read_text())
        check("S4 read timeout: bounded (~5 s), counted, falls back to B", r is not None and r.model == B and took < 7
              and led["models"][A]["requests"] == 1 and led["models"][A]["errors"] == 1, f"took {took:.1f}s")


def s5_crash_safety() -> None:
    """SIGKILL during an in-flight request: the ledger must be valid and count it."""
    clock = SharedClock()
    tmp = Path(tempfile.mkdtemp())
    with FakeGemini(clock, {A: dict(FREE, mode="slow:30")}) as fake:
        script = textwrap.dedent(f"""
            import sys; sys.path.insert(0, {str(REPO)!r})
            import src.gemini_broker as gb
            gb.API_BASE = {fake.base!r}
            b = gb.GeminiBroker(__import__('pathlib').Path({str(tmp / 'crash.json')!r}),
                env={{"ENABLE_GEMINI_BROKER": "true", "GEMINI_API_KEY": {KEY!r}, "GITHUB_RUN_ID": "crash"}})
            print("start", flush=True)
            b.generate_json("extract", ({A!r},), "x")
        """)
        proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
        assert proc.stdout and proc.stdout.readline().strip() == "start"
        deadline = time.time() + 10
        while time.time() < deadline and not fake.log:
            time.sleep(0.05)
        time.sleep(0.3)
        proc.send_signal(signal.SIGKILL)
        proc.wait()
    data = json.loads((tmp / "crash.json").read_text())
    leftovers = list(tmp.glob("*.tmp"))
    check("S5 SIGKILL mid-request: ledger valid JSON and request already counted",
          data["models"][A]["requests"] == 1 and data["runs"] == ["crash"], f"ledger={data['models']}")
    check("S5 SIGKILL mid-request: no half-written temp file left", not leftovers, str(leftovers))
    # The next run reads it fine and continues counting.
    with FakeGemini(clock, {A: dict(FREE)}) as fake:
        broker = make_broker(tmp / "crash.json", clock, fake.base, datetime.now(timezone.utc), GITHUB_RUN_ID="after")
        r = broker.generate_json("extract", (A,), "x")
    check("S5 next run after crash continues the same ledger", r is not None and
          json.loads((tmp / "crash.json").read_text())["models"][A]["requests"] == 2)


def s6_pacific_rollover() -> None:
    """Counts reset exactly at midnight America/Los_Angeles, including DST switches."""
    clock = SharedClock()
    tmp = Path(tempfile.mkdtemp())
    limits = json.dumps({A: {"rpm": 1000, "rpd": 5, "tpm": 10_000_000}})  # planned rpd 4
    cases = [  # (last instant of a Pacific day in UTC, description)
        (datetime(2026, 10, 22, 6, 59, 59, tzinfo=timezone.utc), "PDT (UTC-7)"),
        (datetime(2026, 11, 1, 6, 59, 59, tzinfo=timezone.utc), "night of DST end (1 Nov 2026)"),
        (datetime(2026, 11, 2, 7, 59, 59, tzinfo=timezone.utc), "PST (UTC-8)"),
        (datetime(2026, 3, 8, 7, 59, 59, tzinfo=timezone.utc), "night of DST start (8 Mar 2026)"),
    ]
    for i, (last, label) in enumerate(cases):
        ledger = tmp / f"d{i}.json"
        with FakeGemini(clock, {A: {"rpm": 1000, "rpd": 1000, "tpm": 10_000_000}}) as fake:
            b1 = make_broker(ledger, clock, fake.base, last, GEMINI_MODEL_LIMITS=limits, GEMINI_RUNS_PER_DAY=1)
            used = sum(b1.generate_json("extract", (A,), "x") is not None for _ in range(6))
            b2 = make_broker(ledger, clock, fake.base, last, GEMINI_MODEL_LIMITS=limits, GEMINI_RUNS_PER_DAY=1, GITHUB_RUN_ID="same-day")
            blocked = b2.generate_json("extract", (A,), "x") is None
            b3 = make_broker(ledger, clock, fake.base, last + timedelta(seconds=1), GEMINI_MODEL_LIMITS=limits,
                             GEMINI_RUNS_PER_DAY=1, GITHUB_RUN_ID="next-day")
            fresh = b3.generate_json("extract", (A,), "x") is not None
            day_before, day_after = gb.pacific_day(last), gb.pacific_day(last + timedelta(seconds=1))
        check(f"S6 rollover {label}: 4/day used, blocked until midnight PT, fresh 1 s later",
              used == 4 and blocked and fresh and day_before != day_after, f"{day_before} → {day_after}")


if __name__ == "__main__":
    for scenario in (s1_public_day, s2_real_limit_lower, s3_rpm_lower, s4_faults, s5_crash_safety, s6_pacific_rollover):
        try:
            scenario()
        except Exception as exc:  # report, keep going
            import traceback
            traceback.print_exc()
            check(f"{scenario.__name__} crashed", False, repr(exc))
    failed = [r for r in RESULTS if not r[1]]
    print(f"\nBROKER BETA: {len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    sys.exit(1 if failed else 0)
