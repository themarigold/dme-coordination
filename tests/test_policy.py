import pytest

from dme.policy import ApprovalPolicy, lower_bound


def test_lower_bound_values():
    assert lower_bound(0) == 0.0
    assert lower_bound(5) == pytest.approx(0.549, abs=0.001)
    assert lower_bound(20) == pytest.approx(0.861, abs=0.001)
    assert lower_bound(59) >= 0.95 > lower_bound(58)


def test_all_types_start_gated_and_never_self_loosen():
    p = ApprovalPolicy()
    for t in ("supplier_handoff", "claude_next_action", "criteria_acceptance"):
        assert p.is_gated(t)
    for _ in range(100):
        p.record("criteria_acceptance", "approve", seen_proposal=False)
    assert p.lower_bound("criteria_acceptance") > 0.95
    assert p.is_gated("criteria_acceptance")  # a long streak alone never loosens it
    with pytest.raises(PermissionError):
        p.loosen("criteria_acceptance", actor="rule")
    p.loosen("criteria_acceptance", actor="human")
    assert not p.is_gated("criteria_acceptance")


def test_reject_tightens_and_resets_streak():
    p = ApprovalPolicy({"supplier_handoff": "autonomous"})
    assert not p.is_gated("supplier_handoff")
    p.record("supplier_handoff", "approve", seen_proposal=False)
    p.record("supplier_handoff", "reject", seen_proposal=True)
    assert p.is_gated("supplier_handoff") and p.switches["supplier_handoff"].streak == 0


def test_downstream_failure_tightens():
    p = ApprovalPolicy({"supplier_handoff": "autonomous"})
    p.confirm_outcome("supplier_handoff", ok=False, detail="supplier rejected: out of stock")
    assert p.is_gated("supplier_handoff")
    assert "downstream failure" in p.switches["supplier_handoff"].history[-1]


def test_cosmetic_edit_is_not_a_miss_structural_edit_is():
    p = ApprovalPolicy()
    cosmetic = p.record("claude_next_action", "edit_approve", seen_proposal=False, structured_change=False)
    assert not cosmetic.miss and p.switches["claude_next_action"].streak == 1
    structural = p.record("claude_next_action", "edit_approve", seen_proposal=False, structured_change=True)
    assert structural.miss and p.switches["claude_next_action"].streak == 0


def test_blind_flag_recorded_and_seen_approvals_wait_for_an_outcome():
    p = ApprovalPolicy()
    seen = p.record("supplier_handoff", "approve", seen_proposal=True)
    blind = p.record("supplier_handoff", "approve", seen_proposal=False)
    assert (seen.seen_proposal, blind.seen_proposal) == (True, False)
    s = p.switches["supplier_handoff"]
    assert (s.streak, s.pending_confirmation) == (1, 1)  # only the blind match counts yet
    p.confirm_outcome("supplier_handoff", ok=True)
    assert (s.streak, s.pending_confirmation) == (2, 0)
