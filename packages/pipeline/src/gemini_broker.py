"""Gemini free-tier budget broker: the single door for every Gemini call.

Why a broker
------------
The old design sent one ~110-token request per ambiguous sentence and capped
the day at six requests. Measured against the free tier that uses roughly 1 %
of the daily *request* allowance and far less than 0.1 % of the token
allowance. On the free tier the scarce resource is **requests per day (RPD)**,
not tokens, so every request should carry a whole document (or a batch of
items) and return structured JSON. This module makes that safe:

* **Per project, per model buckets.** Google applies free-tier limits per
  project and per model (RPM, input TPM, RPD). Each model is budgeted
  separately, so a cascade over several free models multiplies capacity.
* **Pacific-time day.** RPD resets at midnight America/Los_Angeles, not UTC.
  Without tzdata the broker uses UTC-8 all year: during daylight saving time
  the day then flips one hour *late*, never early (conservative).
* **Safety margin.** Google publishes no free-tier numbers any more ("view
  your limits in AI Studio", "not guaranteed"). Operators copy the values from
  AI Studio into ``GEMINI_MODEL_LIMITS``; without that the broker uses
  reference values measured on a fresh project (2026-09-02). Either way only
  ``GEMINI_SAFETY_FACTOR`` (default 0.8) of a limit is ever planned.
* **Learns from 429.** A quota rejection carries ``google.rpc.QuotaFailure``
  with ``quotaId``/``quotaValue``. Per-day violations exhaust the model for the
  rest of the Pacific day and lower the learned limit; per-minute violations
  are retried once after ``RetryInfo`` (bounded), then the model cools down
  for the run.
* **Fair share per run.** ``GEMINI_RUNS_PER_DAY`` spreads the remaining daily
  budget over the remaining planned runs, so the first run cannot starve the
  rest; the last run of the day may use whatever is left.
* **Reserve before calling, persist always.** Each request is written to the
  Git-persisted ledger *before* it is sent. A corrupt ledger disables the
  broker (fail closed) instead of resetting the budget.
* **Wall-clock bound.** ``GEMINI_RUN_SECONDS`` stops cloud work early enough
  for the job timeout; local extraction continues regardless.

Nothing here assumes a paid plan. No prompt text or key is ever logged.
Facts and sources: ``docs/gemini-strategy.md``.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
LEDGER_VERSION = 1
_MODEL_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")
_TASK_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_RETRY_DELAY = re.compile(r"^(\d+(?:\.\d+)?)s$")

#: Reference free-tier limits per model family. The flash-lite, flash and
#: embedding rows were read from ``QuotaFailure.quotaValue`` on a fresh project
#: (2026-09-02, published measurement); TPM and the Gemma row are unverified
#: conservative assumptions. Google does not guarantee any of these numbers;
#: configure the real values from AI Studio via GEMINI_MODEL_LIMITS.
FAMILY_REFERENCE_LIMITS: dict[str, dict[str, int]] = {
    "flash-lite": {"rpm": 15, "rpd": 500, "tpm": 250_000},
    "flash": {"rpm": 5, "rpd": 20, "tpm": 250_000},
    "embedding": {"rpm": 100, "rpd": 1_000, "tpm": 100_000},
    "gemma": {"rpm": 10, "rpd": 500, "tpm": 15_000},
}

#: Model-name prefixes whose thinking stage must be budgeted to zero explicitly
#: (same contract as token_saver_nlp.GeminiFallback).
THINKING_MODEL_PREFIXES = ("gemini-2.5-", "gemini-3.")

#: Hard ceilings, independent of configuration (defence against a bad variable).
MAX_RPD_PER_MODEL = 20_000
MAX_LEDGER_RUNS = 64
MAX_LEDGER_MODELS = 16


def model_family(model: str) -> str | None:
    """Return the free-tier family of a model id, or None if not free-capable."""
    name = model.lower()
    if "embedding" in name:
        return "embedding"
    if name.startswith("gemma"):
        return "gemma"
    if "flash-lite" in name:
        return "flash-lite"
    if "flash" in name:
        return "flash"
    return None  # Pro and unknown models are never budgeted implicitly


def pacific_day(now: datetime | None = None) -> str:
    """ISO date of the Gemini quota day (midnight America/Los_Angeles)."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        return now.astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()
    except Exception:  # pragma: no cover - depends on host tzdata
        return (now.astimezone(timezone.utc) - timedelta(hours=8)).date().isoformat()


def _bounded_float(raw: Any, default: float, low: float, high: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return min(high, max(low, value)) if math.isfinite(value) else default


def _bounded_int(raw: Any, default: int, low: int, high: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return min(high, max(low, value))


def parse_model_limits(raw: str) -> dict[str, dict[str, int]]:
    """Parse GEMINI_MODEL_LIMITS: ``{"model": {"rpm": 15, "rpd": 500, "tpm": 250000}}``.

    Invalid input raises ValueError: a typo in a budget variable must not
    silently fall back to a different budget.
    """
    if not raw or not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict) or len(data) > MAX_LEDGER_MODELS:
        raise ValueError("GEMINI_MODEL_LIMITS must be an object with at most 16 models")
    limits: dict[str, dict[str, int]] = {}
    for model, values in data.items():
        if not isinstance(model, str) or not _MODEL_NAME.fullmatch(model) or not isinstance(values, dict):
            raise ValueError("GEMINI_MODEL_LIMITS: invalid model entry")
        entry = {}
        for key in ("rpm", "rpd", "tpm"):
            if key in values:
                value = values[key]
                if type(value) is not int or not 0 <= value <= (MAX_RPD_PER_MODEL if key == "rpd" else 10_000_000):
                    raise ValueError(f"GEMINI_MODEL_LIMITS: {model}.{key} must be a bounded integer")
                entry[key] = value
        limits[model] = entry
    return limits


def parse_task_caps(raw: str) -> dict[str, int]:
    """Parse GEMINI_TASK_CAPS: ``{"extract": 300, "brief": 2}`` (requests per day)."""
    if not raw or not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("GEMINI_TASK_CAPS must be an object")
    caps = {}
    for task, value in data.items():
        if not isinstance(task, str) or not _TASK_NAME.fullmatch(task) or type(value) is not int or not 0 <= value <= MAX_RPD_PER_MODEL:
            raise ValueError("GEMINI_TASK_CAPS: invalid entry")
        caps[task] = value
    return caps


def parse_model_list(raw: str, default: Iterable[str]) -> tuple[str, ...]:
    models = tuple(m.strip() for m in (raw or "").split(",") if m.strip()) or tuple(default)
    for model in models:
        if not _MODEL_NAME.fullmatch(model):
            raise ValueError("Invalid Gemini model name in cascade")
    return models[:8]


def parse_quota_failure(payload: Any) -> tuple[list[dict[str, Any]], float | None]:
    """Extract QuotaFailure violations and the RetryInfo delay from a 429 body."""
    violations: list[dict[str, Any]] = []
    retry: float | None = None
    error = payload.get("error") if isinstance(payload, dict) else None
    details = error.get("details") if isinstance(error, dict) else None
    for detail in details if isinstance(details, list) else []:
        if not isinstance(detail, dict):
            continue
        kind = str(detail.get("@type", ""))
        if kind.endswith("google.rpc.QuotaFailure"):
            for item in detail.get("violations") or []:
                if isinstance(item, dict):
                    value = item.get("quotaValue")
                    try:
                        number = int(str(value))
                    except (TypeError, ValueError):
                        number = None
                    violations.append({"id": str(item.get("quotaId", ""))[:200],
                                       "metric": str(item.get("quotaMetric", ""))[:200], "value": number})
        elif kind.endswith("google.rpc.RetryInfo"):
            match = _RETRY_DELAY.fullmatch(str(detail.get("retryDelay", "")))
            if match:
                retry = float(match.group(1))
    return violations, retry


@dataclass
class CallResult:
    """A successful structured Gemini answer."""

    data: Any
    model: str
    input_tokens: int
    output_tokens: int


class GeminiBroker:
    """Budgeted, serialized access to free-tier Gemini models for one run."""

    def __init__(self, ledger: Path, *, session: Any = None, env: Mapping[str, str] | None = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], datetime] | None = None):
        env = os.environ if env is None else env
        self.ledger = Path(ledger)
        self.session = session or requests.Session()
        self.key = env.get("GEMINI_API_KEY", "")
        self.clock = clock
        self.sleep = sleep
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.disabled_reason = ""
        self.enabled = env.get("ENABLE_GEMINI_BROKER", "false").strip().lower() == "true" and bool(self.key)
        if not self.enabled:
            self.disabled_reason = "ENABLE_GEMINI_BROKER is not 'true' or GEMINI_API_KEY is missing"
        self.safety = _bounded_float(env.get("GEMINI_SAFETY_FACTOR"), 0.8, 0.1, 0.95)
        self.runs_per_day = _bounded_int(env.get("GEMINI_RUNS_PER_DAY"), 2, 1, 48)
        self.run_seconds = _bounded_float(env.get("GEMINI_RUN_SECONDS"), 240.0, 10.0, 3_600.0)
        self.max_retry_wait = _bounded_float(env.get("GEMINI_MAX_RETRY_WAIT_SECONDS"), 30.0, 0.0, 120.0)
        self.request_timeout = _bounded_float(env.get("GEMINI_REQUEST_TIMEOUT_SECONDS"), 60.0, 5.0, 300.0)
        try:
            self.limits = parse_model_limits(env.get("GEMINI_MODEL_LIMITS", ""))
            self.task_caps = parse_task_caps(env.get("GEMINI_TASK_CAPS", ""))
        except (ValueError, json.JSONDecodeError) as exc:
            self.limits, self.task_caps = {}, {}
            self._disable(f"invalid budget configuration: {exc}")
        self.started = clock()
        self.run_id = str(env.get("GITHUB_RUN_ID", "") or f"local-{int(time.time())}")[:40]
        self.state: dict[str, Any] | None = None
        self.run_registered = False
        self.run_base: dict[str, int] = {}
        self.run_used: dict[str, int] = {}
        self.runs_before = 0
        self.unavailable: dict[str, str] = {}
        self.windows: dict[str, deque[tuple[float, int]]] = {}
        self.stats: dict[str, Any] = {"ok": 0, "failed": 0, "rate_limited": 0, "skipped_budget": 0,
                                      "input_tokens": 0, "output_tokens": 0, "by_model": {}, "by_task": {}}

    # ------------------------------------------------------------------ ledger
    def _disable(self, reason: str) -> None:
        self.enabled = False
        self.disabled_reason = reason
        logger.warning("Gemini broker disabled: %s", reason)

    def _empty_day(self, day: str, learned: dict[str, Any]) -> dict[str, Any]:
        return {"version": LEDGER_VERSION, "day": day, "models": {}, "tasks": {}, "runs": [], "learned": learned}

    def _load(self) -> dict[str, Any] | None:
        if self.state is not None:
            return self.state
        day = pacific_day(self.now())
        try:
            data = json.loads(self.ledger.read_text(encoding="utf-8")) if self.ledger.exists() else None
            if data is None:
                state = self._empty_day(day, {})
            else:
                self._validate(data)
                state = data if data["day"] == day else self._empty_day(day, data["learned"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._disable(f"corrupt Gemini ledger ({type(exc).__name__}); budget is NOT reset")
            return None
        self.state = state
        return state

    @staticmethod
    def _validate(data: Any) -> None:
        if not isinstance(data, dict) or data.get("version") != LEDGER_VERSION:
            raise ValueError("unsupported ledger version")
        if not isinstance(data.get("day"), str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", data["day"]):
            raise ValueError("invalid ledger day")
        models, tasks, runs, learned = data.get("models"), data.get("tasks"), data.get("runs"), data.get("learned")
        if not all(isinstance(x, dict) for x in (models, tasks, learned)) or not isinstance(runs, list):
            raise ValueError("invalid ledger shape")
        if len(models) > MAX_LEDGER_MODELS or len(learned) > MAX_LEDGER_MODELS or len(runs) > MAX_LEDGER_RUNS:
            raise ValueError("ledger exceeds bounds")
        for name, entry in models.items():
            if not _MODEL_NAME.fullmatch(str(name)) or not isinstance(entry, dict):
                raise ValueError("invalid model entry")
            for key in ("requests", "input_tokens", "output_tokens", "errors"):
                if type(entry.get(key, 0)) is not int or entry.get(key, 0) < 0:
                    raise ValueError("invalid counter")
            if entry.get("requests", 0) > MAX_RPD_PER_MODEL or not isinstance(entry.get("exhausted", False), bool):
                raise ValueError("invalid model counter")
        for name, value in tasks.items():
            if not _TASK_NAME.fullmatch(str(name)) or type(value) is not int or not 0 <= value <= MAX_RPD_PER_MODEL:
                raise ValueError("invalid task counter")
        for name, entry in learned.items():
            if not _MODEL_NAME.fullmatch(str(name)) or not isinstance(entry, dict):
                raise ValueError("invalid learned entry")
            for key, value in entry.items():
                if key not in {"rpm", "rpd", "tpm"} or type(value) is not int or value < 0:
                    raise ValueError("invalid learned limit")
        if not all(isinstance(run, str) and len(run) <= 40 for run in runs):
            raise ValueError("invalid run entry")

    def _save(self) -> None:
        if self.state is None:
            return
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        temp = self.ledger.with_suffix(".tmp")
        temp.write_text(json.dumps(self.state, sort_keys=True, indent=1) + "\n", encoding="utf-8")
        temp.replace(self.ledger)

    def _model_entry(self, model: str) -> dict[str, Any]:
        if self.state is None:
            raise ValueError("ledger not loaded")
        models = self.state["models"]
        if model not in models:
            if len(models) >= MAX_LEDGER_MODELS:
                raise ValueError("too many models in one day")
            models[model] = {"requests": 0, "input_tokens": 0, "output_tokens": 0, "errors": 0, "exhausted": False}
        return models[model]

    def _register_run(self) -> None:
        if self.run_registered or self.state is None:
            return
        runs = self.state["runs"]
        self.runs_before = len([run for run in runs if run != self.run_id])
        if self.run_id not in runs:
            runs.append(self.run_id)
            del runs[:-MAX_LEDGER_RUNS]
        self.run_registered = True

    # ------------------------------------------------------------------ limits
    def planned_limits(self, model: str) -> dict[str, int] | None:
        """Limits the broker plans with: min(configured|reference, learned) x safety."""
        family = model_family(model)
        base = dict(FAMILY_REFERENCE_LIMITS.get(family or "", {}))
        base.update(self.limits.get(model, {}))
        if not base or not all(key in base for key in ("rpm", "rpd", "tpm")):
            return None  # unknown model without explicit limits: never call it
        learned = (self.state or {}).get("learned", {}).get(model, {})
        for key, value in learned.items():
            base[key] = min(base[key], value)
        return {"rpm": max(1, math.floor(base["rpm"] * self.safety)) if base["rpm"] else 0,
                "rpd": math.floor(base["rpd"] * self.safety),
                "tpm": math.floor(base["tpm"] * self.safety)}

    def remaining_today(self, model: str) -> int:
        state = self._load()
        plan = self.planned_limits(model)
        if state is None or plan is None:
            return 0
        entry = state["models"].get(model, {})
        if entry.get("exhausted"):
            return 0
        return max(0, plan["rpd"] - int(entry.get("requests", 0)))

    def run_allowance(self, model: str) -> int:
        """Requests this run may still send to ``model`` (fair share of the day)."""
        if model not in self.run_base:
            self._register_run()
            remaining = self.remaining_today(model)
            share = math.ceil(remaining / max(1, self.runs_per_day - self.runs_before)) if remaining else 0
            self.run_base[model] = share
        return max(0, self.run_base[model] - self.run_used.get(model, 0))

    def _task_left(self, task: str) -> bool:
        cap = self.task_caps.get(task)
        return cap is None or int((self.state or {}).get("tasks", {}).get(task, 0)) < cap

    def time_left(self) -> float:
        return self.run_seconds - (self.clock() - self.started)

    def available(self, task: str, models: Iterable[str]) -> bool:
        """Cheap pre-check so callers can skip preparing a prompt."""
        if not self.enabled or self._load() is None or not self._task_left(task) or self.time_left() <= 0:
            return False
        return any(model not in self.unavailable and self.run_allowance(model) > 0 for model in models)

    def _throttle(self, model: str, plan: dict[str, int], tokens: int) -> bool:
        """Sleep until RPM and input-TPM windows admit the request; False if it would overrun."""
        window = self.windows.setdefault(model, deque())
        while True:
            now = self.clock()
            while window and now - window[0][0] >= 60.0:
                window.popleft()
            used_tokens = sum(t for _, t in window)
            if len(window) < plan["rpm"] and used_tokens + tokens <= plan["tpm"]:
                return True
            if not window:
                return False  # a single request larger than the TPM plan never fits
            wait = 60.0 - (now - window[0][0]) + 0.05
            if wait > self.time_left():
                return False
            self.sleep(wait)

    # -------------------------------------------------------------------- call
    @staticmethod
    def estimate_tokens(*texts: str) -> int:
        # ~3 characters per token is a deliberate over-estimate for German/English text.
        return math.ceil(sum(len(t) for t in texts) / 3) + 64

    def _body(self, model: str, prompt: str, system: str, schema: dict[str, Any] | None,
              max_output_tokens: int) -> dict[str, Any]:
        config: dict[str, Any] = {"temperature": 0, "maxOutputTokens": max_output_tokens}
        family = model_family(model)
        text = prompt
        if family == "gemma":
            # Gemma endpoints do not take systemInstruction/JSON schema: inline both.
            text = (system + "\n\n" if system else "") + prompt + "\nAnswer with one JSON object only."
        else:
            config["responseMimeType"] = "application/json"
            if schema is not None:
                config["responseSchema"] = schema
            if model.startswith(THINKING_MODEL_PREFIXES):
                config["thinkingConfig"] = {"thinkingBudget": 0}
        body: dict[str, Any] = {"contents": [{"role": "user", "parts": [{"text": text}]}], "generationConfig": config}
        if system and family != "gemma":
            body["systemInstruction"] = {"parts": [{"text": system}]}
        return body

    def _count(self, task: str, model: str, key: str, amount: int = 1) -> None:
        self.stats[key] += amount
        self.stats["by_model"].setdefault(model, {"ok": 0, "failed": 0, "rate_limited": 0})
        self.stats["by_task"].setdefault(task, {"ok": 0, "failed": 0, "rate_limited": 0})
        if key in {"ok", "failed", "rate_limited"}:
            self.stats["by_model"][model][key] += amount
            self.stats["by_task"][task][key] += amount

    def generate_json(self, task: str, models: Iterable[str], prompt: str, *, system: str = "",
                      schema: dict[str, Any] | None = None, max_output_tokens: int = 2_048,
                      max_input_tokens: int = 12_000) -> CallResult | None:
        """Send one structured request through the first model with budget left.

        Returns None when every model is exhausted, unavailable, over time or
        failed — the caller then simply keeps its local result.
        """
        if not _TASK_NAME.fullmatch(task):
            raise ValueError("invalid task name")
        if not self.enabled or self._load() is None:
            return None
        estimate = self.estimate_tokens(system, prompt)
        if estimate > max_input_tokens:
            self.stats["skipped_budget"] += 1
            return None
        for model in models:
            if model in self.unavailable or not _MODEL_NAME.fullmatch(model):
                continue
            plan = self.planned_limits(model)
            if plan is None:
                self.unavailable[model] = "no free-tier limits known"
                continue
            for attempt in range(2):  # second attempt only after a per-minute 429
                if not self.enabled or not self._task_left(task) or self.time_left() <= 0:
                    self.stats["skipped_budget"] += 1
                    return None
                if self.run_allowance(model) <= 0 or estimate > plan["tpm"]:
                    break
                if not self._throttle(model, plan, estimate):
                    break
                outcome = self._send(task, model, plan, prompt, system, schema, max_output_tokens, estimate)
                if isinstance(outcome, CallResult):
                    return outcome
                if outcome == "retry" and attempt == 0:
                    continue
                if outcome == "fatal":
                    return None
                break  # "next": try the next model in the cascade
        self.stats["skipped_budget"] += 1
        return None

    def _send(self, task: str, model: str, plan: dict[str, int], prompt: str, system: str,
              schema: dict[str, Any] | None, max_output_tokens: int, estimate: int) -> CallResult | str:
        if self.state is None:
            return "next"
        try:
            entry = self._model_entry(model)
        except ValueError:
            self.unavailable[model] = "ledger already tracks the maximum number of models today"
            return "next"
        # Reserve BEFORE the request; the ledger survives a crash or job timeout.
        entry["requests"] += 1
        self.state["tasks"][task] = int(self.state["tasks"].get(task, 0)) + 1
        self.run_used[model] = self.run_used.get(model, 0) + 1
        self._save()
        self.windows.setdefault(model, deque()).append((self.clock(), estimate))
        url = f"{API_BASE}/models/{model}:generateContent"
        try:
            response = self.session.post(url, headers={"x-goog-api-key": self.key},
                                         json=self._body(model, prompt, system, schema, max_output_tokens),
                                         timeout=(5, self.request_timeout), allow_redirects=False)
        except requests.RequestException as exc:
            return self._failure(task, model, entry, f"transport {type(exc).__name__}")
        status = int(getattr(response, "status_code", 200) or 200)
        if status == 429:
            return self._rate_limited(task, model, entry, response)
        if status in (401, 403):
            self._failure(task, model, entry, f"HTTP {status}")
            self._disable(f"Gemini rejected the API key (HTTP {status})")
            self._save()
            return "fatal"
        if status in (400, 404):
            self.unavailable[model] = f"HTTP {status} (model not served for this key/project)"
            return self._failure(task, model, entry, f"HTTP {status}")
        if status >= 300:
            return self._failure(task, model, entry, f"HTTP {status}")
        try:
            payload = response.json()
            usage = payload.get("usageMetadata") or {}
            prompt_tokens = usage.get("promptTokenCount", 0)
            output_tokens = usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
            if type(prompt_tokens) is not int or type(output_tokens) is not int:
                prompt_tokens, output_tokens = estimate, 0
            entry["input_tokens"] += max(0, prompt_tokens)
            entry["output_tokens"] += max(0, output_tokens)
            self.stats["input_tokens"] += max(0, prompt_tokens)
            self.stats["output_tokens"] += max(0, output_tokens)
            candidate = (payload.get("candidates") or [])[0]
            parts = candidate.get("content", {}).get("parts", [])
            text = "".join(str(part.get("text", "")) for part in parts if isinstance(part, dict) and not part.get("thought"))
            data = json.loads(_strip_fences(text))
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            return self._failure(task, model, entry, "unparseable or truncated answer")
        self._save()
        self._count(task, model, "ok")
        return CallResult(data=data, model=model, input_tokens=max(0, prompt_tokens), output_tokens=max(0, output_tokens))

    def _failure(self, task: str, model: str, entry: dict[str, Any], reason: str) -> str:
        entry["errors"] += 1
        self._save()
        self._count(task, model, "failed")
        logger.info("Gemini %s request on %s failed: %s", task, model, reason)
        failures = self.stats["by_model"][model]["failed"]
        if failures >= 2:
            self.unavailable.setdefault(model, "repeated failures this run")
        return "next"

    def _rate_limited(self, task: str, model: str, entry: dict[str, Any], response: Any) -> str:
        self._count(task, model, "rate_limited")
        try:
            violations, retry = parse_quota_failure(response.json())
        except ValueError:
            violations, retry = [], None
        learned = self.state["learned"].setdefault(model, {})  # type: ignore[index]
        daily = False
        for violation in violations:
            quota_id, value = violation["id"], violation["value"]
            if "PerDay" in quota_id:
                daily = True  # requests OR tokens per day: either way the model is done today
                if value is not None and "Token" not in quota_id:
                    learned["rpd"] = min(learned.get("rpd", value), value)
            elif "PerMinute" in quota_id and value is not None:
                key = "tpm" if "Token" in quota_id else "rpm"
                learned[key] = min(learned.get(key, value), value)
        if daily:
            entry["exhausted"] = True
            self.unavailable[model] = "daily free-tier quota exhausted (resets at midnight Pacific)"
            self._save()
            return "next"
        self._save()
        if retry is not None and retry <= self.max_retry_wait and retry < self.time_left():
            self.sleep(retry)
            return "retry"
        self.unavailable[model] = "per-minute quota; cooling down for this run"
        return "next"

    # ---------------------------------------------------------------- reporting
    def summary(self) -> dict[str, Any]:
        state = self.state or {}
        models = {}
        for model, entry in (state.get("models") or {}).items():
            plan = self.planned_limits(model) or {}
            models[model] = {"requests_today": entry.get("requests", 0), "planned_rpd": plan.get("rpd"),
                             "exhausted": entry.get("exhausted", False), "errors_today": entry.get("errors", 0)}
        return {"enabled": self.enabled, "disabled_reason": self.disabled_reason, "pacific_day": state.get("day"),
                "run": {key: self.stats[key] for key in ("ok", "failed", "rate_limited", "skipped_budget",
                                                          "input_tokens", "output_tokens")},
                "by_task": self.stats["by_task"], "models": models, "unavailable": dict(self.unavailable)}

    def write_step_summary(self, path: str | None = None) -> None:
        target = path or os.getenv("GITHUB_STEP_SUMMARY")
        if not target:
            return
        info = self.summary()
        lines = ["### Gemini free-tier broker", "",
                 f"- enabled: `{info['enabled']}`" + (f" ({info['disabled_reason']})" if info["disabled_reason"] else ""),
                 f"- Pacific quota day: `{info['pacific_day']}`",
                 "- this run: " + ", ".join(f"{k}={v}" for k, v in info["run"].items()), "",
                 "| model | requests today | planned RPD | exhausted |", "| --- | --- | --- | --- |"]
        lines += [f"| `{m}` | {v['requests_today']} | {v['planned_rpd']} | {v['exhausted']} |" for m, v in info["models"].items()]
        with open(target, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


def _strip_fences(text: str) -> str:
    """Tolerate a ```json fence (Gemma has no JSON mode); never repair content."""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped
