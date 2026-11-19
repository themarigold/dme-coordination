"""The single wrapper around Claude API calls, with a replay cache.

Calls never raise: every failure becomes LlmUnavailable. The stop reason is checked
before the output is validated, which is why this uses `messages.create`, not `messages.parse`.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, ValidationError

from dme.models import LlmUnavailable

MODEL = "claude-opus-5-5"
MAX_TOKENS = 16000
MAX_TOKENS_RETRY = 32000
# Live reads take 10-15 s; 180 s leaves room for the larger max_tokens retry, and one
# SDK retry bounds the worst case at about 2 x 180 s. An explicit timeout also stops
# the SDK from demanding streaming for a large non-streaming max_tokens.
TIMEOUT_S = 180.0
MAX_RETRIES = 1
PRICE_INPUT_PER_M = 4.0
PRICE_OUTPUT_PER_M = 20.0

ROOT = Path(__file__).resolve().parent.parent.parent
REPLAY_DIR = ROOT / "replay"
ENV_FILE = ROOT / ".env"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
PREAMBLE_FILE = PROMPTS_DIR / "_preamble.md"

Mode = Literal["live", "replay", "off"]


@dataclass
class CallRecord:
    """What one call cost and how it went; the engine turns this into a LogLine."""

    call: str
    mode: Mode
    model: str = MODEL
    prompt_hash: str = ""
    effort: str = ""
    replay_key: str = ""
    reached_api: bool = False  # False for replay hits, replay misses and injected faults
    requests: int = 0  # API requests this call dispatched, answered or not (a max_tokens retry is a second one)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    stop_reason: str = ""
    thinking: str = ""  # summarised thinking text, joined
    outcome: str = "ok"  # "ok" or the LlmUnavailable reason

    def log_fields(self) -> dict[str, Any]:
        """The LogLine fields for this call: llm_mode is "replay", "live" if a request reached the API, else ""."""
        if self.mode == "replay":
            llm_mode = "replay"
        else:
            llm_mode = "live" if self.reached_api else ""
        return {
            "llm_call": self.call,
            "llm_mode": llm_mode,
            "prompt_hash": self.prompt_hash,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
        }


@dataclass
class CallResult:
    output: Any  # an instance of model_cls, or LlmUnavailable
    record: CallRecord
    raw_text: str = ""


def cost_usd(input_tokens: int, output_tokens: int) -> float:
    return round(input_tokens * PRICE_INPUT_PER_M / 1e6 + output_tokens * PRICE_OUTPUT_PER_M / 1e6, 6)


def prompt_hash(call_name: str) -> str:
    """Hash of the call's prompt file and the shared preamble, logged as the prompt version."""
    h = hashlib.sha256()
    for path in (PREAMBLE_FILE, PROMPTS_DIR / f"{call_name}.md"):
        if path.exists():
            h.update(path.read_bytes())
    return h.hexdigest()


def schema_for(model_cls: type[BaseModel]) -> dict[str, Any]:
    return anthropic.transform_schema(model_cls)


def replay_key(system: str, user: str, model_cls: type[BaseModel], effort: str, model: str = MODEL) -> str:
    h = hashlib.sha256()
    for part in (model, system, user, json.dumps(schema_for(model_cls), sort_keys=True), effort):
        h.update(part.encode())
        h.update(b"\x00")  # separator so ("ab", "c") != ("a", "bc")
    return h.hexdigest()


_client_lock = threading.Lock()
_default_client: Any = None


def load_env() -> None:
    """Load the project .env into the environment; variables already set win."""
    try:
        from dotenv import load_dotenv

        load_dotenv(ENV_FILE)
    except ImportError:  # pragma: no cover - python-dotenv is a dependency
        pass


def has_api_key() -> bool:
    """Whether the live client will find credentials, in the environment or the project .env."""
    load_env()
    # The same two variables the SDK reads; it treats an empty value as unset.
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def default_client() -> Any:
    """The shared live client, created on first live use (loads the project .env)."""
    global _default_client
    with _client_lock:
        if _default_client is None:
            load_env()
            _default_client = anthropic.Anthropic(timeout=TIMEOUT_S, max_retries=MAX_RETRIES)
        return _default_client


def _unavailable(call: str, reason: str, detail: str, record: CallRecord) -> CallResult:
    record.outcome = reason
    return CallResult(LlmUnavailable(call=call, reason=reason, detail=detail[:500]), record)  # type: ignore[arg-type]


def _text_and_thinking(message: Any) -> tuple[str, str]:
    texts, thoughts = [], []
    for block in getattr(message, "content", None) or []:
        kind = getattr(block, "type", "")
        if kind == "text":
            texts.append(block.text)
        elif kind == "thinking":
            thoughts.append(getattr(block, "thinking", "") or "")
    return "".join(texts), "\n".join(t for t in thoughts if t)


def _save_replay(directory: Path, key: str, entry: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(entry, f, indent=1, sort_keys=True)
    os.replace(tmp, directory / f"{key}.json")


def _validate(call: str, model_cls: type[BaseModel], text: str, record: CallRecord) -> CallResult:
    try:
        return CallResult(model_cls.model_validate_json(text), record, text)
    except (ValidationError, ValueError) as e:
        return _unavailable(call, "parse_error", str(e), record)


def call_claude(
    call_name: str,
    model_cls: type[BaseModel],
    system: str,
    user: str,
    effort: str,
    *,
    replay: Mode = "live",
    fault: str | None = None,
    client: Any = None,
    replay_dir: Path | None = None,
    max_requests: int | None = None,
) -> CallResult:
    """One structured-output call. Never raises; failures are LlmUnavailable.

    `max_requests` is what the run's request cap still allows. A request counts
    when it is sent, so timeouts and errors count too.
    """
    record = CallRecord(call=call_name, mode=replay, prompt_hash=prompt_hash(call_name), effort=effort)
    if fault == "llm_down":
        return _unavailable(call_name, "fault_injected", "--fault llm_down", record)
    try:
        key = replay_key(system, user, model_cls, effort)
    except Exception as e:  # schema generation failed: a programming error
        return _unavailable(call_name, "api_error", f"schema: {e}", record)
    record.replay_key = key
    directory = replay_dir or REPLAY_DIR

    if replay == "replay":
        # Identical requests share one recording; a replay run is meant to be deterministic.
        path = directory / f"{key}.json"
        if not path.exists():
            return _unavailable(call_name, "replay_miss", f"no replay entry for {call_name} (key {key[:12]})", record)
        try:
            entry = json.loads(path.read_text())
            text = entry["text"]
            if not isinstance(text, str):
                raise TypeError("replay entry 'text' is not a string")
            record.input_tokens = int(entry.get("input_tokens", 0) or 0)
            record.output_tokens = int(entry.get("output_tokens", 0) or 0)
            record.thinking = str(entry.get("thinking", "") or "")
            record.stop_reason = str(entry.get("stop_reason", "") or "")
        except Exception as e:  # corrupt or truncated cache file
            return _unavailable(
                call_name,
                "parse_error",
                f"corrupt replay entry {key[:12]} for {call_name}: {type(e).__name__}: {e}",
                record,
            )
        record.cost_usd = cost_usd(record.input_tokens, record.output_tokens)
        return _validate(call_name, model_cls, text, record)

    try:
        client = client or default_client()
    except Exception as e:  # no API key or no SDK
        return _unavailable(call_name, "api_error", f"no client: {type(e).__name__}: {e}", record)
    started = time.monotonic()
    message = None
    retried_at = 0
    for max_tokens in (MAX_TOKENS, MAX_TOKENS_RETRY):
        if max_requests is not None and record.requests >= max_requests:
            record.latency_ms = int((time.monotonic() - started) * 1000)
            return _unavailable(call_name, "api_error", "Claude call cap reached: request not sent", record)
        record.requests += 1  # dispatched: counts toward the cap whatever comes back
        try:
            message = client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                output_config={"format": {"type": "json_schema", "schema": schema_for(model_cls)}, "effort": effort},
                thinking={"type": "adaptive", "display": "summarized"},
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.APITimeoutError as e:
            record.latency_ms = int((time.monotonic() - started) * 1000)
            return _unavailable(call_name, "timeout", str(e), record)
        except Exception as e:  # APIError and anything else
            record.latency_ms = int((time.monotonic() - started) * 1000)
            return _unavailable(call_name, "api_error", f"{type(e).__name__}: {e}", record)
        record.reached_api = True
        usage = getattr(message, "usage", None)
        record.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        record.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        record.cost_usd = cost_usd(record.input_tokens, record.output_tokens)
        record.stop_reason = str(getattr(message, "stop_reason", "") or "")
        if record.stop_reason != "max_tokens":
            break
        retried_at = MAX_TOKENS_RETRY
    record.latency_ms = int((time.monotonic() - started) * 1000)
    text, thinking = _text_and_thinking(message)
    record.thinking = thinking

    if record.stop_reason == "refusal":
        return _unavailable(call_name, "refusal", "stop_reason=refusal", record)
    if record.stop_reason in ("max_tokens", "model_context_window_exceeded"):
        detail = f"stop_reason={record.stop_reason}"
        if record.stop_reason == "max_tokens" and retried_at:
            detail += f" after one retry at max_tokens={retried_at}"
        return _unavailable(call_name, "max_tokens", detail, record)
    if record.stop_reason not in ("end_turn", "stop_sequence"):
        return _unavailable(call_name, "api_error", f"unexpected stop_reason={record.stop_reason!r}", record)

    result = _validate(call_name, model_cls, text, record)
    if replay == "live" and not isinstance(result.output, LlmUnavailable):
        try:
            _save_replay(
                directory,
                key,
                {
                    "call": call_name,
                    "model": MODEL,
                    "effort": effort,
                    "prompt_hash": record.prompt_hash,
                    "text": text,
                    "stop_reason": record.stop_reason,
                    "thinking": thinking,
                    "input_tokens": record.input_tokens,
                    "output_tokens": record.output_tokens,
                },
            )
        except OSError:
            pass  # a cache write failure never fails the call
    return result


@dataclass
class Caller:
    """Settings shared by the calls of one ClaudeLlm, plus an optional record sink."""

    mode: Mode = "live"
    fault: str | None = None
    client: Any = None
    replay_dir: Path | None = None
    on_record: Any = None  # callable(CallRecord) or None
    budget: Any = None  # callable() -> requests the run's cap still allows, or None
    records: list[CallRecord] = field(default_factory=list)

    def __call__(self, call_name: str, model_cls: type[BaseModel], system: str, user: str, effort: str) -> CallResult:
        left = None if self.budget is None else self.budget() - sum(r.requests for r in self.records)
        result = call_claude(
            call_name,
            model_cls,
            system,
            user,
            effort,
            replay=self.mode,
            fault=self.fault,
            client=self.client,
            replay_dir=self.replay_dir,
            max_requests=left,
        )
        self.emit(result.record)
        return result

    def emit(self, record: CallRecord) -> None:
        self.records.append(record)
        if self.on_record is not None:
            self.on_record(record)
