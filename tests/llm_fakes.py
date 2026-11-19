"""Offline stand-ins for the Anthropic client (no network) used by the Claude-client tests."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel

from dme.models import CriterionRead, DatePhraseRead, _Read


def empty_payload(model_cls: type[BaseModel]) -> dict[str, Any]:
    """A valid all-not_stated payload for a reader model (or minimal for others)."""
    out: dict[str, Any] = {}
    for name, f in model_cls.model_fields.items():
        ann = f.annotation
        if isinstance(ann, type) and issubclass(ann, _Read):
            d = {
                "evidence_turn_id": "",
                "evidence_quote": "",
                "status": "not_stated",
                "value": "missing" if ann is CriterionRead else "",
            }
            if ann is DatePhraseRead:
                d["commitment"] = ""
            out[name] = d
        elif getattr(ann, "__origin__", None) is list:
            out[name] = []
        else:
            out[name] = ""
    if "action" in out:
        out["action"] = "wait_until"
    return out


def message(
    text: str = "{}",
    stop_reason: str = "end_turn",
    input_tokens: int = 100,
    output_tokens: int = 50,
    thinking: str = "summary of thinking",
) -> SimpleNamespace:
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking=thinking, signature="sig"),
            SimpleNamespace(type="text", text=text),
        ],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class FakeMessages:
    def __init__(self, responses: list[Any]):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        if callable(r):
            return r(kwargs)
        return r

    def parse(self, **kwargs: Any) -> Any:  # must never be used
        raise AssertionError("messages.parse must not be used")


class FakeClient:
    def __init__(self, responses: list[Any]):
        self.messages = FakeMessages(responses)


def ok(model_cls: type[BaseModel], **overrides: Any) -> SimpleNamespace:
    payload = empty_payload(model_cls)
    payload.update(overrides)
    return message(json.dumps(payload))
