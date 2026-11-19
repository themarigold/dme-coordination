"""Live Claude calls, one per function: `uv run pytest -q -m live` with a key in .env (off by default).
Responses are recorded in a tmp replay dir, never in replay/."""

from __future__ import annotations

import pytest

from dme import models as m
from dme.llm import ClaudeLlm
from dme.llm.context import make_context

pytestmark = pytest.mark.live


@pytest.fixture
def llm(tmp_path):
    return ClaudeLlm(mode="live", replay_dir=tmp_path)


def _ok(out, model):
    assert not isinstance(out, m.LlmUnavailable), out
    assert isinstance(out, model)


def test_live_read_supplier_call(llm):
    turns = [
        m.Turn(turn_id="t1", speaker="us", text="Do you have a K0001 standard wheelchair in stock?"),
        m.Turn(turn_id="t2", speaker="supplier_rep", text="We're out right now, sorry. This is Kim."),
    ]
    out = llm.read_supplier_call(turns, make_context("live", "read_supplier_call", "Qualify the supplier.", {}))
    _ok(out, m.SupplierCallRead)
    assert out.k0001_in_stock.value == "no" and out.k0001_in_stock.evidence_turn_id == "t2"


def test_live_read_pcp_reply(llm):
    turns = [m.Turn(turn_id="t1", speaker="pcp_office", text="We never got your fax. This is Rosa.")]
    out = llm.read_pcp_reply(turns, make_context("live", "read_pcp_reply", "Confirm the request arrived.", {}))
    _ok(out, m.PcpReplyRead)
    assert out.outcome.value == "not_received"


def test_live_read_order_document(llm):
    text = "Patient: Jane Doe. Item: standard manual wheelchair K0001. Qty 1. Date 10/01/2026. Dr. Lee NPI 1234567893. /s/ Dr. Lee"
    out = llm.read_order_document(text, make_context("live", "read_order_document", "Check the order.", {}))
    _ok(out, m.OrderDocumentRead)
    assert out.hcpcs_code.value == "K0001"


def test_live_read_visit_note(llm):
    text = "Pt cannot toilet or dress at home due to COPD dyspnea; walker insufficient. Wt 140 lb."
    out = llm.read_visit_note(text, make_context("live", "read_visit_note", "Check the criteria.", {}))
    _ok(out, m.VisitNoteRead)
    assert out.weight_lb.value == "140"


def test_live_read_patient_reply(llm):
    turns = [
        m.Turn(turn_id="t1", speaker="us", text="Have you had a wheelchair in the last five years?"),
        m.Turn(turn_id="t2", speaker="patient", text="No, never had one."),
    ]
    out = llm.read_patient_reply(turns, make_context("live", "read_patient_reply", "Read her answer.", {}))
    _ok(out, m.PatientReplyRead)
    assert out.prior_wheelchair_5y.value == "no"


def test_live_check_safety_and_conditions(llm):
    turns = [m.Turn(turn_id="t1", speaker="patient", text="Stop calling me about this.")]
    out = llm.check_safety_and_conditions(
        turns, make_context("live", "check_safety_and_conditions", "Safety check.", {})
    )
    _ok(out, m.SafetyRead)
    assert out.opts_out.value == "yes"


def test_live_write_patient_message(llm):
    out = llm.write_patient_message(
        "status_update",
        {"supplier.5.name": "Lakeshore Mobility"},  # never cost: code writes money
        make_context("live", "write_patient_message", "Give her an update.", {}),
    )
    _ok(out, m.PatientMessageDraft)
    assert out.text and llm.last_writer_result.source in ("claude", "claude_regenerated")


def test_live_propose_next_action(llm):
    ctx = make_context(
        "live",
        "propose_next_action",
        "Decide the approval item.",
        {"order.elements_complete": "yes", "supplier.5.call_permission": "yes"},
        reason_code="approval_required",
    )
    out = llm.propose_next_action("Packet for supplier 5 is ready.", ["proceed_with_pending", "hold_pending"], ctx)
    _ok(out, m.Proposal)
    assert out.action in ("proceed_with_pending", "hold_pending")
