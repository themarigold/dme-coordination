"""ClaudeLlm offline: Protocol conformance, settings, last_usage, readers, proposer output."""

from __future__ import annotations

import inspect
import json

import pytest
from llm_fakes import FakeClient, empty_payload, message, ok

from dme import models as m
from dme.llm import ClaudeLlm
from dme.protocols import CallContext, Llm

CTX = CallContext(
    case_id="case-1", goal="Read the reply.", facts={"patient.zip": "60629"}, row_key="supplier:4|qualify_call|1#1"
)
TURNS = [
    m.Turn(turn_id="t1", speaker="us", text="In stock?"),
    m.Turn(turn_id="t2", speaker="supplier_rep", text="Yes."),
]
ENV = ("DME_LLM_MODE", "DME_FAULT", "DME_EFFORT_READERS", "DME_EFFORT_PROPOSER")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)


def test_satisfies_the_llm_protocol():
    llm: Llm = ClaudeLlm()  # static conformance; runtime check below
    for name, proto in inspect.getmembers(Llm, inspect.isfunction):
        if name.startswith("_"):
            continue
        impl = getattr(llm, name)
        assert list(inspect.signature(impl).parameters) == list(inspect.signature(proto).parameters)[1:], name


def test_constructible_with_no_arguments_and_defaults():
    llm = ClaudeLlm()
    assert (llm.mode, llm.fault, llm.reader_effort, llm.proposer_effort, llm.writer_effort) == (
        "live",
        None,
        "low",
        "medium",
        "low",
    )
    assert llm.last_usage == {"input_tokens": None, "output_tokens": None, "cost_usd": None}


def test_env_vars_then_kwargs_take_precedence(monkeypatch):
    monkeypatch.setenv("DME_LLM_MODE", "replay")
    monkeypatch.setenv("DME_FAULT", "llm_down")
    monkeypatch.setenv("DME_EFFORT_READERS", "medium")
    monkeypatch.setenv("DME_EFFORT_PROPOSER", "high")
    llm = ClaudeLlm()
    assert (llm.mode, llm.fault, llm.reader_effort, llm.proposer_effort) == ("replay", "llm_down", "medium", "high")
    llm = ClaudeLlm(mode="off", fault="", reader_effort="low", proposer_effort="medium")
    assert (llm.mode, llm.fault, llm.reader_effort, llm.proposer_effort) == ("off", None, "low", "medium")


def test_env_effort_reaches_the_request(monkeypatch):
    monkeypatch.setenv("DME_EFFORT_READERS", "medium")
    fake = FakeClient([ok(m.SupplierCallRead)])
    ClaudeLlm(mode="off", client=fake).read_supplier_call(TURNS, CTX)
    assert fake.messages.calls[0]["output_config"]["effort"] == "medium"


def test_bad_settings_are_rejected():
    with pytest.raises(ValueError):
        ClaudeLlm(mode="record")
    with pytest.raises(ValueError):
        ClaudeLlm(reader_effort="extreme")


def test_last_usage_after_live_call_and_none_when_api_not_reached(tmp_path):
    fake = FakeClient([message(json.dumps(empty_payload(m.SupplierCallRead)), input_tokens=1000, output_tokens=200)])
    llm = ClaudeLlm(mode="live", client=fake, replay_dir=tmp_path)
    assert isinstance(llm.read_supplier_call(TURNS, CTX), m.SupplierCallRead)
    assert llm.last_usage == {"input_tokens": 1000, "output_tokens": 200, "cost_usd": pytest.approx(0.008)}

    replay = ClaudeLlm(mode="replay", replay_dir=tmp_path)
    assert isinstance(replay.read_supplier_call(TURNS, CTX), m.SupplierCallRead)  # replay hit
    assert replay.last_usage == {"input_tokens": None, "output_tokens": None, "cost_usd": None}

    down = ClaudeLlm(fault="llm_down")
    out = down.read_supplier_call(TURNS, CTX)
    assert isinstance(out, m.LlmUnavailable) and out.reason == "fault_injected"
    assert down.last_usage["cost_usd"] is None


def test_writer_usage_sums_both_attempts():
    fake = FakeClient(
        [
            message(json.dumps({"text": "Call us tomorrow."}), input_tokens=10, output_tokens=5),
            message(json.dumps({"text": "We are working on it."}), input_tokens=20, output_tokens=7),
        ]
    )
    llm = ClaudeLlm(mode="off", client=fake)
    out = llm.write_patient_message("status_update", {}, CTX)
    assert out == m.PatientMessageDraft(text="We are working on it.")
    assert llm.last_usage["input_tokens"] == 30 and llm.last_usage["output_tokens"] == 12
    assert llm.last_writer_result.source == "claude_regenerated"


def test_writer_inbox_case_through_protocol():
    llm = ClaudeLlm(mode="off", client=FakeClient([]))
    ctx = CTX.model_copy(update={"goal": "Patient asks: what colour is it?"})
    assert llm.write_patient_message("answer_question", {}, ctx) == m.PatientMessageDraft(text="")
    assert llm.last_writer_result.needs_inbox


@pytest.mark.parametrize(
    "method,arg,model",
    [
        ("read_supplier_call", TURNS, m.SupplierCallRead),
        ("read_pcp_reply", TURNS, m.PcpReplyRead),
        ("read_order_document", "ORDER", m.OrderDocumentRead),
        ("read_visit_note", "NOTE", m.VisitNoteRead),
        ("read_patient_reply", TURNS, m.PatientReplyRead),
        ("check_safety_and_conditions", TURNS, m.SafetyRead),
    ],
)
def test_each_reader_returns_its_model_with_low_effort(method, arg, model):
    fake = FakeClient([ok(model)])
    llm = ClaudeLlm(mode="off", client=fake)
    assert isinstance(getattr(llm, method)(arg, CTX), model)
    kw = fake.messages.calls[0]
    assert kw["output_config"]["effort"] == "low"
    assert kw["messages"][0]["content"].startswith("<untrusted id=")
    assert llm.records[-1].call == method


def test_proposer_output_shape_and_prompt():
    proposal = {
        "action": "PROCEED_WITH_PENDING",
        "param_party": "",
        "param_supplier": "",
        "param_items": [],
        "param_question": "",
        "param_time_phrase": "",
        "param_reason": "",
        "why": "Everything the handoff needs is in place.",
        "facts_used": [{"fact_id": "supplier.5.call_permission", "value": "yes"}],
    }
    fake = FakeClient([message(json.dumps(proposal))])
    llm = ClaudeLlm(mode="off", client=fake)
    ctx = CTX.model_copy(update={"reason_code": "approval_required", "facts": {"supplier.5.call_permission": "yes"}})
    out = llm.propose_next_action("Packet ready for supplier 5.", ["proceed_with_pending", "hold_pending"], ctx)
    assert isinstance(out, m.Proposal) and out.action == "proceed_with_pending"
    assert out.facts_used == [m.FactUsed(fact_id="supplier.5.call_permission", value="yes")]
    kw = fake.messages.calls[0]
    assert kw["output_config"]["effort"] == "medium"
    user = kw["messages"][0]["content"]
    assert user.startswith("<untrusted id=") and "- proceed_with_pending\n- hold_pending" in user
    assert user.index("</untrusted id=") < user.index("Allowed actions")
    assert "approval_required" in user


def test_proposer_unavailable_is_returned_not_raised():
    out = ClaudeLlm(fault="llm_down").propose_next_action("x", ["wait_until"], CTX)
    assert isinstance(out, m.LlmUnavailable) and out.call == "propose_next_action"


@pytest.mark.parametrize("value,expected", [("LLM_DOWN", "llm_down"), (" llm_down ", "llm_down"), ("", None)])
def test_fault_is_lower_cased(monkeypatch, value, expected):
    monkeypatch.setenv("DME_FAULT", value)
    assert ClaudeLlm().fault == expected


def test_unknown_fault_is_rejected(monkeypatch):
    with pytest.raises(ValueError):
        ClaudeLlm(fault="network_flaky")
    monkeypatch.setenv("DME_FAULT", "llm_slow")
    with pytest.raises(ValueError):
        ClaudeLlm()


def test_safety_call_uses_the_safety_slice_even_when_ctx_has_another():
    from dme import knowledge

    fake = FakeClient([ok(m.SafetyRead), ok(m.SafetyRead)])
    llm = ClaudeLlm(mode="off", client=fake)
    ctx = CTX.model_copy(update={"rule_slice": "OTHER-SLICE-MARKER"})
    llm.check_safety_and_conditions(TURNS, ctx)
    content = fake.messages.calls[0]["messages"][0]["content"]
    rules = content.split("Rules (with citations):\n", 1)[1]
    assert rules.startswith(knowledge.rule_slice("check_safety_and_conditions").strip() + "\n\n")
    assert "OTHER-SLICE-MARKER" not in content


def test_proposer_prompt_examples_validate_against_the_frozen_model():
    from dme.llm.context import load_prompt

    system, _ = load_prompt("propose_next_action")
    outputs = [line[len("Output: ") :] for line in system.splitlines() if line.startswith("Output: {")]
    proposals = [m.Proposal.model_validate_json(o) for o in outputs]
    assert {p.action for p in proposals} >= {"proceed_with_pending", "hold_pending", "send_packet_to"}
