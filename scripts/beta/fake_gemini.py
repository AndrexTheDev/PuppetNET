"""Realistic local stand-in for generativelanguage.googleapis.com (beta tests only).

* Speaks real HTTP (ThreadingHTTPServer) so the broker's real requests.Session,
  timeouts and JSON handling are exercised.
* Enforces per-model RPM (60 s sliding window on a SHARED fake clock), RPD and
  input-TPM exactly like the free tier and answers violations with Google's
  error shape: 429 RESOURCE_EXHAUSTED + google.rpc.QuotaFailure + RetryInfo.
* 404 for unknown models, 403 for a wrong key, plus fault modes per model:
  "http500", "truncated", "blocked", "empty", "slow:<sec>", "fence".
"""
from __future__ import annotations

import contextlib
import json
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

KEY = "beta-key"


class SharedClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []
        self.lock = threading.Lock()

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        with self.lock:
            self.sleeps.append(seconds)
            self.t += seconds

    def advance(self, seconds: float) -> None:
        self.t += seconds


def quota_error(quota_id: str, value: int, retry: float | None) -> dict[str, Any]:
    details: list[dict[str, Any]] = [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                         "quotaId": quota_id,
                         "quotaDimensions": {"location": "global", "model": "m"},
                         "quotaValue": str(value)}]},
        {"@type": "type.googleapis.com/google.rpc.Help",
         "links": [{"description": "Learn more", "url": "fake://rate-limits"}]},
    ]
    if retry is not None:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": f"{math.ceil(retry)}s"})
    return {"error": {"code": 429, "message": "You exceeded your current quota.", "status": "RESOURCE_EXHAUSTED",
                      "details": details}}


class FakeGemini:
    def __init__(self, clock: SharedClock, models: dict[str, dict[str, Any]],
                 responder: Callable[[str, dict[str, Any]], Any] | None = None):
        self.clock = clock
        self.models = models
        self.responder = responder or (lambda model, body: {"entities": [], "relations": []})
        self.windows: dict[str, deque[tuple[float, int]]] = {m: deque() for m in models}
        self.day_requests: dict[str, int] = {m: 0 for m in models}
        self.log: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silence
                return

            def _send(self, status: int, payload: Any, raw: bytes | None = None) -> None:
                data = raw if raw is not None else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                model = self.path.split("/models/", 1)[-1].split(":", 1)[0]
                status, payload, raw, delay = fake.handle(model, self.headers.get("x-goog-api-key"), body)
                if delay:
                    time.sleep(delay)
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    self._send(status, payload, raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1beta"

    def __enter__(self) -> FakeGemini:
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()

    def new_day(self) -> None:
        self.day_requests = {m: 0 for m in self.models}

    def handle(self, model: str, key: str | None, body: dict[str, Any]):
        with self.lock:
            entry = {"model": model, "t": self.clock(), "status": None}
            self.log.append(entry)
            if key != KEY:
                entry["status"] = 403
                return 403, {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "API key not valid"}}, None, 0
            cfg = self.models.get(model)
            if cfg is None:
                entry["status"] = 404
                return 404, {"error": {"code": 404, "status": "NOT_FOUND", "message": f"models/{model} is not found"}}, None, 0
            text = json.dumps(body)
            tokens = math.ceil(len(text) / 4)
            now = self.clock()
            window = self.windows[model]
            while window and now - window[0][0] >= 60:
                window.popleft()
            # Google counts rejected requests too? It does not count them against RPD; we don't either.
            if self.day_requests[model] >= cfg["rpd"]:
                entry["status"] = 429
                return 429, quota_error("GenerateRequestsPerDayPerProjectPerModel-FreeTier", cfg["rpd"], None), None, 0
            if len(window) >= cfg["rpm"]:
                entry["status"] = 429
                retry = 60 - (now - window[0][0])
                return 429, quota_error("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", cfg["rpm"], retry), None, 0
            if sum(t for _, t in window) + tokens > cfg.get("tpm", 250_000):
                entry["status"] = 429
                retry = 60 - (now - window[0][0]) if window else 60
                return 429, quota_error("GenerateContentInputTokensPerModelPerMinute-FreeTier", cfg.get("tpm", 250_000), retry), None, 0
            window.append((now, tokens))
            self.day_requests[model] += 1
            mode = cfg.get("mode", "")
            delay = float(mode.split(":", 1)[1]) if mode.startswith("slow:") else 0
            entry["status"] = 200
            entry["prompt_tokens"] = tokens
            if mode == "http500":
                entry["status"] = 500
                return 500, {"error": {"code": 500, "status": "INTERNAL"}}, None, 0
            if mode == "blocked":
                return 200, {"promptFeedback": {"blockReason": "SAFETY"}, "usageMetadata": {"promptTokenCount": tokens}}, None, 0
            if mode == "empty":
                return 200, {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": []}}],
                             "usageMetadata": {"promptTokenCount": tokens, "candidatesTokenCount": 4096}}, None, 0
            answer = json.dumps(self.responder(model, body), ensure_ascii=False)
            if mode == "truncated":
                answer = answer[: max(1, len(answer) // 2)]
            if mode == "fence":
                answer = "```json\n" + answer + "\n```"
            payload = {"candidates": [{"finishReason": "STOP", "content": {"role": "model", "parts": [{"text": answer}]}}],
                       "usageMetadata": {"promptTokenCount": tokens, "candidatesTokenCount": math.ceil(len(answer) / 4),
                                         "totalTokenCount": tokens + math.ceil(len(answer) / 4)},
                       "modelVersion": model}
            return 200, payload, None, delay
