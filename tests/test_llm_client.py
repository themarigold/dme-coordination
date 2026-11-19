"""call_claude: stop-reason paths, errors, replay cache, fault flag, record (offline)."""

from __future__ import annotations

import json

import anthropic
import httpx2
import pytest
from llm_fakes import FakeClient, empty_payload, message, ok
from pydantic import BaseModel

from dme.llm import client as c
from dme.models import LlmUnavailable, SupplierCallRead, VisitNoteRead, YesNoRead

REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def run(responses, model_cls=SupplierCallRead, **kw):
    fake = FakeClient(responses)
    kw.setdefault("replay", "off")
    result = c.call_claude("read_supplier_call", model_cls, "sys", "user", "low", client=fake, **kw)
    return result, fake


def test_request_shape_uses_create_with_json_schema_and_adaptive_thinking():
    result, fake = run([ok(SupplierCallRead)])
    assert isinstance(result.output, SupplierCallRead)
    (kw,) = fake.messages.calls
    assert kw["model"] == "claude-opus-5-5"
    assert kw["max_tokens"] == 16000
    assert kw["output_config"] == {
        "format": {"type": "json_schema", "schema": anthropic.transform_schema(SupplierCallRead)},
        "effort": "low",
    }
    assert kw["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert kw["system"] == "sys" and kw["messages"] == [{"role": "user", "content": "user"}]
    for banned in ("temperature", "top_p", "top_k", "tool_choice", "tools"):
        assert banned not in kw
    assert all(m["role"] == "user" for m in kw["messages"])  # no assistant prefill


def test_record_has_usage_cost_thinking_and_prompt_hash():
    result, _ = run(
        [
            message(
                json.dumps(empty_payload(SupplierCallRead)),
                input_tokens=1000,
                output_tokens=500,
                thinking="I looked at t2.",
            )
        ]
    )
    r = result.record
    assert (r.input_tokens, r.output_tokens) == (1000, 500)
    assert r.cost_usd == pytest.approx(1000 * 4 / 1e6 + 500 * 20 / 1e6)
    assert r.thinking == "I looked at t2."
    assert r.prompt_hash == c.prompt_hash("read_supplier_call") and len(r.prompt_hash) == 64
    assert r.reached_api and r.outcome == "ok" and r.stop_reason == "end_turn"


def test_refusal_is_checked_before_validation():
    # Valid JSON, but a refusal stop reason: must not be accepted.
    result, _ = run([message(json.dumps(empty_payload(SupplierCallRead)), stop_reason="refusal")])
    assert isinstance(result.output, LlmUnavailable) and result.output.reason == "refusal"
    assert result.output.call == "read_supplier_call"


def test_max_tokens_retries_once_at_32000_then_succeeds():
    result, fake = run(
        [message('{"call_out', stop_reason="max_tokens", input_tokens=10, output_tokens=16000), ok(SupplierCallRead)]
    )
    assert isinstance(result.output, SupplierCallRead)
    assert [k["max_tokens"] for k in fake.messages.calls] == [16000, 32000]
    assert result.record.output_tokens == 16000 + 50  # usage of both attempts


def test_max_tokens_twice_is_unavailable():
    result, fake = run([message("{", stop_reason="max_tokens"), message("{", stop_reason="max_tokens")])
    assert isinstance(result.output, LlmUnavailable) and result.output.reason == "max_tokens"
    assert len(fake.messages.calls) == 2


def test_invalid_json_is_parse_error():
    result, _ = run([message("not json at all")])
    assert result.output.reason == "parse_error"


def test_schema_violation_is_parse_error():
    payload = empty_payload(SupplierCallRead)
    payload["k0001_in_stock"]["value"] = "maybe"  # not in the Literal
    result, _ = run([message(json.dumps(payload))])
    assert result.output.reason == "parse_error"


def test_enum_casing_is_normalised():
    payload = empty_payload(SupplierCallRead)
    payload["k0001_in_stock"] = {
        "evidence_turn_id": "t2",
        "evidence_quote": "We have it",
        "status": "FOUND",
        "value": "Yes",
    }
    result, _ = run([message(json.dumps(payload))])
    assert result.output.k0001_in_stock == YesNoRead(
        evidence_turn_id="t2", evidence_quote="We have it", status="found", value="yes"
    )


@pytest.mark.parametrize(
    "exc,reason",
    [
        (anthropic.APITimeoutError(request=REQ), "timeout"),
        (anthropic.APIConnectionError(request=REQ), "api_error"),
        (anthropic.RateLimitError("slow down", response=httpx2.Response(429, request=REQ), body=None), "api_error"),
        (anthropic.InternalServerError("boom", response=httpx2.Response(500, request=REQ), body=None), "api_error"),
        (RuntimeError("anything else"), "api_error"),
    ],
)
def test_errors_become_unavailable_never_raised(exc, reason):
    result, _ = run([exc])
    assert isinstance(result.output, LlmUnavailable) and result.output.reason == reason
    assert not result.record.reached_api


def test_unexpected_stop_reason_is_unavailable():
    result, _ = run([message(json.dumps(empty_payload(SupplierCallRead)), stop_reason="pause_turn")])
    assert result.output.reason == "api_error"


def test_default_client_settings(monkeypatch):
    monkeypatch.setattr(c, "_default_client", None)
    client = c.default_client()
    assert client.timeout == 180.0 and client.max_retries == 1
    monkeypatch.setattr(c, "_default_client", None)


def test_fault_flag_short_circuits():
    result, fake = run([], fault="llm_down")
    assert result.output.reason == "fault_injected" and fake.messages.calls == []


# -- replay cache ----------------------------------------------------------------------------------


def test_replay_key_is_stable_and_sensitive():
    k1 = c.replay_key("sys", "user", SupplierCallRead, "low")
    assert k1 == c.replay_key("sys", "user", SupplierCallRead, "low")
    assert k1 != c.replay_key("sys", "user", SupplierCallRead, "medium")
    assert k1 != c.replay_key("sys", "user2", SupplierCallRead, "low")
    assert k1 != c.replay_key("sys2", "user", SupplierCallRead, "low")
    assert k1 != c.replay_key("sys", "user", VisitNoteRead, "low")

    class V1(BaseModel):
        a: str

    class V2(BaseModel):
        a: str
        b: str

    V2.__name__ = V1.__name__
    assert c.replay_key("s", "u", V1, "low") != c.replay_key("s", "u", V2, "low")  # schema change -> new key


def test_live_records_then_replay_serves_without_api(tmp_path):
    result, fake = run([ok(SupplierCallRead)], replay="live", replay_dir=tmp_path)
    assert isinstance(result.output, SupplierCallRead)
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    entry = json.loads(files[0].read_text())
    assert entry["input_tokens"] == 100 and entry["call"] == "read_supplier_call" and "text" in entry

    again, fake2 = run([], replay="replay", replay_dir=tmp_path)
    assert again.output == result.output
    assert fake2.messages.calls == [] and not again.record.reached_api
    assert again.record.mode == "replay" and again.record.input_tokens == 100


def test_replay_miss_names_the_call(tmp_path):
    result, fake = run([], replay="replay", replay_dir=tmp_path)
    assert result.output.reason == "replay_miss" and "read_supplier_call" in result.output.detail
    assert fake.messages.calls == []


def test_off_mode_records_nothing_and_failures_are_not_recorded(tmp_path):
    run([ok(SupplierCallRead)], replay="off", replay_dir=tmp_path)
    run([message("bad json")], replay="live", replay_dir=tmp_path)
    assert list(tmp_path.glob("*.json")) == []


def test_prompt_hash_changes_with_template(tmp_path, monkeypatch):
    (tmp_path / "x.md").write_text("one")
    monkeypatch.setattr(c, "PROMPTS_DIR", tmp_path)
    monkeypatch.setattr(c, "PREAMBLE_FILE", tmp_path / "_preamble.md")
    h1 = c.prompt_hash("x")
    (tmp_path / "x.md").write_text("two")
    assert c.prompt_hash("x") != h1


# -- retries, stop reasons, schemas ----------------------------------------------------------------


@pytest.mark.parametrize(
    "content", ['{"text": "{\\"call_out', '{"input_tokens": 3, "output_tokens": 4}'], ids=["truncated", "no_text_key"]
)
def test_corrupt_replay_entry_is_unavailable_not_raised(tmp_path, content):
    key = c.replay_key("sys", "user", SupplierCallRead, "low")
    (tmp_path / f"{key}.json").write_text(content)
    result, fake = run([], replay="replay", replay_dir=tmp_path)
    assert isinstance(result.output, LlmUnavailable) and result.output.reason == "parse_error"
    assert "corrupt replay entry" in result.output.detail and fake.messages.calls == []


def test_max_tokens_detail_says_retry_only_when_retried():
    result, _ = run([message("{", stop_reason="max_tokens"), message("{", stop_reason="max_tokens")])
    assert "after one retry at max_tokens=32000" in result.output.detail
    result, fake = run([message("{", stop_reason="model_context_window_exceeded")])
    assert result.output.reason == "max_tokens" and "retry" not in result.output.detail
    assert len(fake.messages.calls) == 1


def test_log_fields_fit_logline_and_never_say_off():
    from datetime import datetime

    from dme.models import LogLine

    live_off, _ = run([ok(SupplierCallRead)], replay="off")
    down, _ = run([], replay="off", fault="llm_down")
    for rec, mode in ((live_off.record, "live"), (down.record, "")):
        fields = rec.log_fields()
        assert fields["llm_mode"] == mode
        LogLine(
            seq=1, sim_time=datetime(2026, 11, 18, 9), case_id="c", actor="claude", kind="read", summary="s", **fields
        )


def _sdk_message(text: str) -> dict:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [{"type": "thinking", "thinking": "t", "signature": "s"}, {"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 5, "output_tokens": 6},
    }


def test_real_sdk_client_accepts_max_tokens_32000_without_streaming():
    """With our timeout, the real SDK (mocked transport) sends the 32000 retry without streaming."""
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body["max_tokens"])
        if body["max_tokens"] == 16000:
            return httpx2.Response(200, json={**_sdk_message("{"), "stop_reason": "max_tokens"})
        return httpx2.Response(200, json=_sdk_message(json.dumps(empty_payload(SupplierCallRead))))

    sdk = anthropic.Anthropic(
        api_key="test-key",
        timeout=c.TIMEOUT_S,
        max_retries=c.MAX_RETRIES,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    result = c.call_claude("read_supplier_call", SupplierCallRead, "sys", "user", "low", replay="off", client=sdk)
    assert isinstance(result.output, SupplierCallRead), result.output
    assert seen == [16000, 32000] and result.record.thinking == "t"


def test_streaming_required_valueerror_would_be_api_error():
    """With the SDK default timeout, the 32000 retry's ValueError becomes api_error."""

    def handler(request):
        return httpx2.Response(200, json={**_sdk_message("{"), "stop_reason": "max_tokens"})

    sdk = anthropic.Anthropic(api_key="test-key", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    result = c.call_claude("read_supplier_call", SupplierCallRead, "sys", "user", "low", replay="off", client=sdk)
    assert isinstance(result.output, LlmUnavailable) and result.output.reason == "api_error"
    assert "Streaming is required" in result.output.detail


def _call(responses, **kw):
    from llm_fakes import FakeClient

    from dme.llm import client as c
    from dme.models import SupplierCallRead

    fake = FakeClient(responses)
    return c.call_claude(
        "read_supplier_call", SupplierCallRead, "sys", "user", "low", client=fake, replay="off", **kw
    ), fake


def test_a_request_that_times_out_still_counts_toward_the_cap():
    import anthropic
    import httpx2

    from dme.models import LlmUnavailable

    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    result, _ = _call([anthropic.APITimeoutError(request=req)])
    assert isinstance(result.output, LlmUnavailable) and result.record.requests == 1


def test_no_request_is_sent_once_the_cap_is_used_up():
    from llm_fakes import ok

    from dme.models import LlmUnavailable, SupplierCallRead

    result, fake = _call([ok(SupplierCallRead)], max_requests=0)
    assert isinstance(result.output, LlmUnavailable) and fake.messages.calls == [] and result.record.requests == 0


def test_a_missing_client_is_unavailable_not_raised(monkeypatch):
    from dme.llm import client as c
    from dme.models import LlmUnavailable, SupplierCallRead

    def no_key():
        raise RuntimeError("ANTHROPIC_API_KEY is not set")

    monkeypatch.setattr(c, "default_client", no_key)
    result = c.call_claude("read_supplier_call", SupplierCallRead, "sys", "user", "low", replay="off")
    assert isinstance(result.output, LlmUnavailable) and "no client" in result.output.detail
