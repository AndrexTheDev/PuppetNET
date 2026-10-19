"""Deterministic doubles for the GLiNER + Workers AI pipeline.

The production modules are designed around injected models, tokenizers and HTTP
sessions precisely so the unit suite can prove the wiring — chunk offsets,
label mapping, grounding, budget arithmetic, retries — without downloading a
209M-parameter checkpoint or spending a neuron.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


class WordTokenizer:
    """Whitespace/punctuation tokenizer with the transformers call shape."""

    def __call__(self, text: str, **kwargs: Any) -> dict[str, list[str]]:
        return {"input_ids": self._pieces(text)}

    def encode(self, text: str, add_special_tokens: bool = True) -> list[str]:
        return self._pieces(text)

    @staticmethod
    def _pieces(text: str) -> list[str]:
        return re.findall(r"\w+|[^\w\s]", text, re.UNICODE)


class CharTokenizer:
    """One token per character; makes token limits exact and easy to assert."""

    def __call__(self, text: str, **kwargs: Any) -> dict[str, list[str]]:
        return {"input_ids": list(text)}

    def encode(self, text: str, add_special_tokens: bool = True) -> list[str]:
        return list(text)


@dataclass
class Mention:
    """A surface the fake model should find, with the label it echoes back."""

    surface: str
    label: str = "Person"
    score: float = 0.9
    #: Return the label spelling in the model response (real checkpoints may
    #: echo lower-case labels even when given the canonical form).
    echo_label: str | None = None


class FakeGLiNER:
    """Find configured surfaces case-sensitively; respect the label set."""

    def __init__(self, mentions: list[Mention], *, error: Exception | None = None) -> None:
        self.mentions = list(mentions)
        self.error = error
        self.calls: list[str] = []
        self.kwargs: list[dict[str, Any]] = []

    def predict_entities(self, text: str, labels: list[str], threshold: float = 0.5, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(text)
        self.kwargs.append({"labels": list(labels), "threshold": threshold, **kwargs})
        if self.error is not None:
            raise self.error
        allowed = {label.casefold(): label for label in labels}
        predictions: list[dict[str, Any]] = []
        for mention in self.mentions:
            canonical = allowed.get(mention.label.casefold())
            if canonical is None or mention.score < threshold:
                continue
            for match in re.finditer(re.escape(mention.surface), text):
                predictions.append(
                    {
                        "start": match.start(),
                        "end": match.end(),
                        "text": mention.surface,
                        "label": mention.echo_label or mention.label,
                        "score": mention.score,
                    }
                )
        return predictions


class FakeResponse:
    def __init__(
        self,
        payload: Any,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.payload = payload
        self.status_code = status_code
        self.headers = dict(headers or {})

    def json(self) -> Any:
        if isinstance(self.payload, str):
            raise ValueError("not JSON")
        return self.payload


class FakeSession:
    """Queue of responses; records every POST and never opens a socket."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def post(self, url: str, **kwargs: Any) -> Any:
        self.calls.append({"url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected extra Workers AI request")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


@dataclass
class CloudflareBody:
    """Convenience builder for a successful Workers AI JSON response."""

    triplets: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 600
    completion_tokens: int = 120
    neurons: float | None = None
    fenced: bool = False

    def payload(self) -> dict[str, Any]:
        content = json.dumps({"triplets": self.triplets}, ensure_ascii=False)
        if self.fenced:
            content = f"```json\n{content}\n```"
        usage: dict[str, Any] = {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
        }
        if self.neurons is not None:
            usage["neurons"] = self.neurons
        return {
            "success": True,
            "result": {"response": content, "usage": usage},
            "errors": [],
            "messages": [],
        }


def triplet(
    subject: str,
    predicate: str,
    obj: str,
    *,
    confidence: float = 0.8,
    evidence: str = "",
    subject_label: str = "",
    object_label: str = "",
) -> dict[str, Any]:
    return {
        "subject": subject,
        "predicate": predicate,
        "object": obj,
        "confidence": confidence,
        "evidence": evidence,
        "subject_label": subject_label,
        "object_label": object_label,
    }
