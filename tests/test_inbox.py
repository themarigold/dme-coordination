import json
from datetime import datetime

import pytest

from dme.clock import TZ
from dme.inbox import Inbox
from dme.models import Case, EscalationReason, Proposal
from dme.policy import ApprovalPolicy
from dme.rules.escalation import raise_escalation

NOW = datetime(2026, 11, 17, 10, 0, tzinfo=TZ)


def proposal(action="proceed_with_pending", why="all gate facts present", **kw) -> Proposal:
    base = {
        "action": action,
        "param_party": "",
        "param_supplier": "supplier:5",
        "param_items": [],
        "param_question": "",
        "param_time_phrase": "",
        "param_reason": "",
        "why": why,
        "facts_used": [{"fact_id": "supplier.5.status", "value": "primary"}],
    }
    return Proposal.model_validate({**base, **kw})


@pytest.fixture
def setup(tmp_path):
    case = Case(case_id="c1")
    calls = []
    policy = ApprovalPolicy()
    inbox = Inbox(case, tmp_path / "regressions.jsonl", rerun_hook=lambda c, e: calls.append(e), policy=policy)
    return case, inbox, calls, policy, tmp_path / "regressions.jsonl"


def approval(case, inbox, prop=None, blind=False):
    return raise_escalation(
        case,
        EscalationReason.APPROVAL_REQUIRED,
        "packet to supplier 5 ready",
        ["gate passed"],
        inbox,
        now=NOW,
        approval_type="supplier_handoff",
        proposal=prop or proposal(),
        blind=blind,
    )


def test_item_has_spec_fields_and_dedupes(setup):
    case, inbox, *_ = setup
    item = approval(case, inbox)
    assert approval(case, inbox) is item  # same open item: no second touch
    view = item.view()
    for key in ("reason", "blocking_fact", "tried", "proposal", "approval_type"):
        assert key in view
    assert view["proposal"]["why"] and view["proposal"]["facts_used"]
    assert inbox.touches() == ["approval_required:supplier_handoff"]


def test_approve_resolves_and_reruns(setup):
    case, inbox, calls, policy, path = setup
    item = approval(case, inbox)
    inbox.approve(item.item_id)
    assert item.status == "resolved"
    assert calls[-1]["action"] == "approve" and calls[-1]["item_id"] == item.item_id
    assert policy.decisions[-1].label == "approve" and policy.decisions[-1].seen_proposal
    assert not path.exists()


def test_approving_hold_pending_keeps_item_open(setup):
    case, inbox, calls, *_ = setup
    item = approval(case, inbox, proposal("hold_pending", why="written permission not yet signed"))
    inbox.approve(item.item_id)
    assert item.status == "open" and calls[-1]["item_open"] is True
    inbox.update_proposal(item.item_id, proposal())
    inbox.approve(item.item_id)
    assert item.status == "resolved"


def test_cosmetic_edit_is_no_regression_structural_edit_is(setup):
    case, inbox, calls, policy, path = setup
    a = approval(case, inbox)
    inbox.edit_approve(a.item_id, {"why": "Packet ready; everything checks out."})
    assert not policy.decisions[-1].miss and not path.exists()
    b = raise_escalation(
        case,
        EscalationReason.SECOND_HANDOFF,
        "supplier 4 rejected after packet",
        [],
        inbox,
        now=NOW,
        proposal=proposal("send_packet_to"),
    )
    inbox.edit_approve(b.item_id, {"param_supplier": "supplier:6"})
    assert policy.decisions[-1].miss and policy.decisions[-1].action_type == "claude_next_action"
    record = json.loads(path.read_text().splitlines()[-1])
    assert record["changed_fields"] == ["param_supplier"] and record["human_action"] == "edit_approve"
    assert calls[-1]["proposal"]["param_supplier"] == "supplier:6"


def test_reject_writes_regression_tightens_and_keeps_item_open(setup):
    case, inbox, calls, policy, path = setup
    policy.loosen("supplier_handoff", actor="human")
    item = approval(case, inbox)
    inbox.reject(item.item_id, "supplier 5 never confirmed the date")
    assert item.status == "open" and policy.is_gated("supplier_handoff")
    assert json.loads(path.read_text())["reason"] == "approval_required"
    assert calls[-1]["action"] == "reject"


def test_supply_fact_sets_fact_with_source(setup):
    case, inbox, calls, *_ = setup
    item = raise_escalation(
        case, EscalationReason.MEDICAID_OR_MSP, "patient.medicaid_or_msp = not_sure", ["asked once"], inbox, now=NOW
    )
    with pytest.raises(ValueError):
        inbox.supply_fact(item.item_id, "patient.medicaid_or_msp", "no", source=" ")
    with pytest.raises(KeyError):
        inbox.supply_fact(item.item_id, "patient.made_up", "no", source="call")
    inbox.supply_fact(item.item_id, "patient.medicaid_or_msp", "no", source="advocate call with Eleanor")
    assert case.get("patient.medicaid_or_msp") == "no" and item.status == "resolved"
    assert calls[-1]["fact_id"] == "patient.medicaid_or_msp" and calls[-1]["source"]


def test_choose_action_other_than_proposal_is_a_miss(setup):
    case, inbox, calls, policy, path = setup
    item = raise_escalation(
        case,
        EscalationReason.PCP_SILENT,
        "no reply in 3 business days",
        ["2 nudges"],
        inbox,
        now=NOW,
        proposal=proposal("call_again", param_party="pcp"),
    )
    chosen = proposal("escalate_to_human", param_reason="pcp_silent", param_supplier="")
    inbox.choose_action(item.item_id, chosen)
    assert item.status == "resolved" and policy.decisions[-1].miss
    assert json.loads(path.read_text())["chosen"]["action"] == "escalate_to_human"
    assert calls[-1]["proposal"]["action"] == "escalate_to_human"
    with pytest.raises(ValueError):
        inbox.approve(item.item_id)  # already resolved


def test_blind_item_hides_proposal_and_cannot_be_approved_edited_or_rejected(setup):
    case, inbox, calls, policy, _ = setup
    item = approval(case, inbox, blind=True)
    assert item.view()["proposal"] is None
    for act in (
        lambda: inbox.approve(item.item_id),
        lambda: inbox.reject(item.item_id, "no"),
        lambda: inbox.edit_approve(item.item_id, {"why": "x"}),
    ):
        with pytest.raises(ValueError, match="blind"):
            act()
    assert policy.decisions == [] and calls == [] and item.status == "open"


def test_blind_choice_matching_claude_is_a_hit(setup):
    case, inbox, _, policy, path = setup
    item = approval(case, inbox, blind=True)
    inbox.choose_action(item.item_id, proposal(why="my own reasons", facts_used=[]))
    d = policy.decisions[-1]
    assert (d.miss, d.seen_proposal) == (False, False)
    assert policy.switches["supplier_handoff"].streak == 1
    assert item.view()["proposal"] is not None  # revealed once decided
    assert not path.exists()


def test_blind_choice_of_a_different_action_is_a_miss(setup):
    case, inbox, _, policy, path = setup
    item = approval(case, inbox, blind=True)
    inbox.choose_action(item.item_id, proposal("hold_pending"))
    d = policy.decisions[-1]
    assert (d.miss, d.seen_proposal) == (True, False)
    assert policy.switches["supplier_handoff"].streak == 0
    assert json.loads(path.read_text())["blind"] is True


def test_seen_choice_with_same_action_but_other_cited_facts_is_a_hit(setup):
    case, inbox, _, policy, path = setup
    item = approval(case, inbox)
    inbox.choose_action(item.item_id, proposal(facts_used=[{"fact_id": "supplier.5.k0001_in_stock", "value": "yes"}]))
    d = policy.decisions[-1]
    assert (d.miss, d.seen_proposal) == (False, True)
    assert policy.switches["supplier_handoff"].pending_confirmation == 1  # seen: waits for an outcome
    assert not path.exists()
