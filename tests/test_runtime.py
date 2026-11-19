"""The runtime: the nothing-running backstop, engine caps and re-arming, and checks on a person's input."""

import pytest
from fakes import drain, only_suppliers, outbound, proposal, qualify_row, ready_patient, runtime

from dme import clock
from dme.engine import MAX_BUSINESS_DAYS, MAX_CLAUDE_CALLS
from dme.facts import canonical_problem
from dme.models import EscalationReason as R


def test_a_case_with_nothing_running_gets_one_case_stalled_item_naming_its_state(tmp_path):
    rt, _ = runtime([], tmp_path, scripted_humans=False)
    rt.case.thread_state["handoff"] = "with_human"
    rt.schedule(rt.now, "re_evaluate", {})  # an event that moves nothing
    drain(rt)
    (item,) = rt.inbox.open_items()
    assert item.reason == R.CASE_STALLED and "handoff with_human" in item.blocking_fact
    rt.ensure_running("a check")  # never a second item while one is open
    assert rt.inbox.touches() == ["case_stalled"]


def test_the_backstop_comes_back_after_a_person_closes_its_item_with_nothing_running(tmp_path):
    rt, _ = runtime([], tmp_path, scripted_humans=False)
    rt.schedule(rt.now, "re_evaluate", {})
    drain(rt)
    (item,) = rt.inbox.open_items()
    rt.inbox.choose_action(item.item_id, proposal("wait_until", param_time_phrase="tomorrow"))
    assert rt.inbox.open_items() == [] and rt.engine.pending()  # the wait is running
    drain(rt)
    assert rt.inbox.touches() == ["case_stalled", "case_stalled"] and rt.inbox.items[1].status == "open"


def test_a_closed_case_or_one_with_something_queued_raises_nothing(tmp_path):
    rt, _ = runtime([], tmp_path, scripted_humans=False)
    rt.schedule(rt.now, "re_evaluate", {})
    rt.schedule(rt.now, "re_evaluate", {})
    rt.engine.step()  # the second is still queued
    assert rt.inbox.items == []
    rt.case.closed = True
    rt.ensure_running("a check")
    assert rt.inbox.items == []


def test_the_runtime_checks_the_cap_before_each_call(tmp_path):
    from dme.engine import MAX_CLAUDE_CALLS
    from dme.models import LlmUnavailable

    rt, _ = runtime([], tmp_path, scripted_humans=False)
    rt.engine.claude_calls = MAX_CLAUDE_CALLS
    called = []
    result = rt.call_llm("read_supplier_call", lambda: called.append(1))
    assert isinstance(result, LlmUnavailable) and called == [] and rt.engine.claude_calls == MAX_CLAUDE_CALLS


def test_a_persons_action_on_the_stall_item_rearms_the_engine_with_fresh_caps(tmp_path):
    rt, _ = runtime([], tmp_path, scripted_humans=False)
    rt.schedule(rt.now, "re_evaluate", {})
    rt.engine.claude_calls = MAX_CLAUDE_CALLS  # the cap is used up with something still queued
    assert rt.engine.step() is False and rt.engine.stalled
    (item,) = rt.inbox.open_items()
    assert item.reason == R.CASE_STALLED and rt.engine.stall_item == item.item_id
    rt.inbox.choose_action(item.item_id, proposal("wait_until", param_time_phrase="tomorrow"))
    assert not rt.engine.stalled and rt.engine.claude_left() == MAX_CLAUDE_CALLS
    assert any(l.kind == "engine_rearmed" for l in rt.engine.log_lines)
    assert rt.engine.step() is True


def test_a_stalled_engine_with_events_queued_counts_as_nothing_running(tmp_path):
    rt, _ = runtime([], tmp_path, scripted_humans=False)
    rt.schedule(rt.now, "re_evaluate", {})
    rt.engine.stalled = True  # stalled, its item closed some other way
    rt.ensure_running("a check")
    (item,) = rt.inbox.open_items()
    assert item.reason == R.CASE_STALLED and rt.engine.stall_item == item.item_id
    rt.inbox.supply_fact(item.item_id, "patient.availability", "mornings", "called her")
    assert not rt.engine.stalled and rt.engine.step() is True


def test_a_persons_action_on_another_item_does_not_rearm_a_stalled_engine(tmp_path):
    rt, _ = runtime([], tmp_path, scripted_humans=False)
    rt.schedule(rt.now, "re_evaluate", {})
    rt.engine.claude_calls = MAX_CLAUDE_CALLS
    rt.engine.step()
    other = rt.escalate(R.PCP_SILENT, "no documents")
    rt.inbox.choose_action(other.item_id, proposal("wait_until", param_time_phrase="tomorrow"))
    assert rt.engine.stalled


def test_rearming_after_the_business_day_cap_runs_the_far_event_instead_of_stalling_again(tmp_path):
    rt, _ = runtime([], tmp_path, scripted_humans=False, backstop=False)  # the backstop's own item aside
    far = clock.add_business_days(rt.now, MAX_BUSINESS_DAYS + 15)
    rt.schedule(far, "re_evaluate", {})
    assert rt.engine.step() is False and rt.engine.stalled
    (item,) = rt.inbox.open_items()
    rt.inbox.supply_fact(item.item_id, "patient.availability", "mornings", "the advocate looked at the case")
    assert not rt.engine.stalled
    assert rt.engine.step() is True and rt.now == far  # the event the cap stopped runs
    assert not rt.engine.stalled and [i.reason for i in rt.inbox.items].count(R.CASE_STALLED) == 1


@pytest.mark.parametrize(
    "reason, fact, value",
    [(R.MEDICAID_OR_MSP, "patient.medicaid_or_msp", "not_sure"), (R.OUTSIDE_HOME_ONLY, "patient.in_home_use", "no")],
)
def test_proceeding_on_an_unsettled_reason_keeps_an_item_open(tmp_path, reason, fact, value):
    rt, _ = runtime([], tmp_path, scripted_humans=False)
    rt.case.set(fact, value)
    item = rt.escalate(reason, "x")
    rt.inbox.choose_action(item.item_id, proposal("proceed_with_pending"))
    assert "handoff" in rt.case.holds
    (again,) = rt.inbox.open_items()
    assert again.reason == reason and "supply the fact" in again.tried[-1]


def test_proceeding_on_intake_incomplete_without_identity_raises_it_again(tmp_path):
    rt, _ = runtime([qualify_row(4)], tmp_path, scripted_humans=False)
    ready_patient(rt, **{"patient.identity_verified": "no"})
    only_suppliers(rt, 4)
    item = rt.escalate(R.INTAKE_INCOMPLETE, "first contact: identity not confirmed after asking twice")
    rt.inbox.choose_action(item.item_id, proposal("proceed_with_pending"))
    again = rt.inbox.open_items()
    assert [i.reason for i in again] == [R.INTAKE_INCOMPLETE] and "supply the fact" in again[0].tried[-1]
    assert "patient_contact" in rt.case.holds and outbound(rt) == []
    rt.inbox.supply_fact(again[0].item_id, "patient.identity_verified", "yes", source="advocate called her")
    assert rt.case.holds == [] and ("supplier:4", "qualify_call") in outbound(rt)  # sourcing starts


def test_call_again_on_an_unknown_supplier_never_reaches_the_directory(tmp_path):
    rt, _ = runtime([], tmp_path, scripted_humans=False)
    ready_patient(rt)
    item = rt.escalate(R.HANDOFF_SILENCE, "supplier 4 silent")
    with pytest.raises(ValueError, match="needs a known party"):
        rt.inbox.choose_action(item.item_id, proposal("call_again", param_party="supplier:99"))
    assert item.status == "open"


@pytest.mark.parametrize(
    "fact_id,value",
    [
        ("supplier.4.earliest_delivery", "Nov 20"),
        ("supplier.4.earliest_delivery", "2026-02-30"),
        ("handoff.packets_sent", "two"),
        ("order.version", "v2"),
        ("patient.contact_attempts", "1.5"),
        ("handoff.supplier", "supplier:4"),
        ("patient.consent_proceed", "y"),
        ("supplier.4.status", "great"),
        ("patient.medicaid_or_msp", "maybe"),
        ("note.criterion_b", "yes"),
        ("note.weight_lb", "158 lb"),
    ],
)
def test_a_supplied_fact_that_is_not_canonical_is_refused_and_the_item_stays_open(tmp_path, fact_id, value):
    rt, _ = runtime([], tmp_path, scripted_humans=False, backstop=False)
    item = rt.escalate(R.PCP_SILENT, "no documents after two nudges")
    with pytest.raises(ValueError, match="supplied fact refused"):
        rt.inbox.supply_fact(item.item_id, fact_id, value, "phoned the supplier")
    assert item.status == "open" and rt.case.get(fact_id) == ""


@pytest.mark.parametrize(
    "fact_id,value",
    [
        ("supplier.4.earliest_delivery", "2026-11-20"),
        ("handoff.packets_sent", "2"),
        ("patient.consent_proceed", "no"),
        ("patient.medicaid_or_msp", "not_sure"),
        ("note.weight_lb", "158.5"),
        ("patient.address", "Nov 20 Street"),
    ],
)
def test_canonical_values_are_taken(fact_id, value):
    assert canonical_problem(fact_id, value) == ""
