from datetime import datetime, timedelta

import pytest

from dme.clock import TZ
from dme.models import EscalationReason, Scenario
from dme.world import ROOT, World, WorldUnscripted, load_case, load_directory, parse_delay

START = datetime(2026, 11, 17, 10, 0, tzinfo=TZ)


def scenario(rows, **extra) -> Scenario:
    return Scenario.model_validate(
        {
            "name": "t",
            "case_file": "scenarios/eleanor_case.yaml",
            "start": START.isoformat(),
            "rows": rows,
            "expected": {"terminal_state": "delivered", "human_touches": []},
            **extra,
        }
    )


def reply(cp, action, attempt, delay="0m", event="reply", **kw):
    return {"counterparty": cp, "action": action, "attempt": attempt, "delay": delay, "event": event, **kw}


def test_exact_row_then_default_fallback():
    w = World(
        scenario(
            [
                reply("supplier:2", "qualify_call", 1, event="no_answer"),
                reply("supplier:2", "qualify_call", "default", delay="5m"),
            ]
        )
    )
    first = w.respond("supplier:2", "qualify_call", 1, START)
    assert [(e.row.event, e.row_key) for e in first] == [("no_answer", "supplier:2|qualify_call|1#1")]
    third = w.respond("supplier:2", "qualify_call", 3, START)
    assert third[0].row_key == "supplier:2|qualify_call|default#1"
    assert third[0].due == START + timedelta(minutes=5)


def test_miss_raises_world_unscripted_in_test_mode():
    w = World(scenario([reply("pcp", "order_request", 1)]))
    with pytest.raises(WorldUnscripted, match="supplier:9"):
        w.respond("supplier:9", "qualify_call", 1, START)
    assert EscalationReason.WORLD_UNSCRIPTED.value == "world_unscripted"


def test_rows_sharing_a_key_come_in_list_order_with_cumulative_delays():
    w = World(
        scenario(
            [
                reply("pcp", "order_request", 1, delay="2h", event="reply"),
                reply(
                    "pcp",
                    "order_request",
                    1,
                    delay="1bd",
                    event="document",
                    text="ORDER",
                    payload={"doc_type": "order"},
                ),
            ]
        )
    )
    a, b = w.respond("pcp", "order_request", 1, START)
    assert (a.row_key, b.row_key) == ("pcp|order_request|1#1", "pcp|order_request|1#2")
    assert a.due == datetime(2026, 11, 17, 12, 0, tzinfo=TZ)
    assert b.due == datetime(2026, 11, 18, 12, 0, tzinfo=TZ)


def test_never_means_silence():
    w = World(scenario([reply("supplier:4", "reconfirm_call", 1, event="never")]))
    assert w.respond("supplier:4", "reconfirm_call", 1, START) == []


def test_after_event_chaining_counts_occurrences():
    w = World(
        scenario(
            [
                reply("supplier:5", "after:accept", 1, delay="1bd", event="scheduled", payload={"date": "2026-12-15"}),
                reply("supplier:5", "after:scheduled", "default", delay="2bd", event="delivered"),
            ]
        )
    )
    (sched,) = w.follow_ups("supplier:5", "accept", START)
    assert sched.row.event == "scheduled" and sched.row_key == "supplier:5|after:accept|1#1"
    assert w.follow_ups("supplier:5", "accept", START) == []  # 2nd accept: no row, no follow-up, no error
    (deliv,) = w.follow_ups("supplier:5", "scheduled", START)
    assert deliv.row_key == "supplier:5|after:scheduled|default#1"


def test_delay_parsing():
    assert parse_delay("90m", START) == START + timedelta(minutes=90)
    assert parse_delay("8h", START) == datetime(2026, 11, 18, 10, 0, tzinfo=TZ)
    assert parse_delay("1bd", datetime(2026, 11, 25, 10, 0, tzinfo=TZ)) == datetime(2026, 11, 27, 10, 0, tzinfo=TZ)
    with pytest.raises(ValueError):
        parse_delay("2 weeks", START)


def test_offline_stand_ins_by_reason_and_nth():
    w = World(
        scenario(
            [],
            human_decisions=[
                {"reason_code": "approval_required", "nth": 1, "decision": "approve"},
                {"reason_code": "approval_required", "nth": 2, "decision": "reject"},
            ],
            scripted_proposals=[
                {
                    "reason_code": "approval_required",
                    "nth": 2,
                    "proposal": {
                        "action": "proceed_with_pending",
                        "param_party": "",
                        "param_supplier": "supplier:5",
                        "param_items": [],
                        "param_question": "",
                        "param_time_phrase": "",
                        "param_reason": "",
                        "why": "all set",
                        "facts_used": [{"fact_id": "supplier.5.status", "value": "primary"}],
                    },
                }
            ],
            scripted_drafts={"cost_explanation#1": "Your cost is {cost.low} to {cost.high}."},
        )
    )
    assert w.human_decision("approval_required", 2).decision == "reject"
    assert w.human_decision("approval_required", 3) is None
    assert w.scripted_proposal("approval_required", 2).param_supplier == "supplier:5"
    assert w.scripted_proposal("approval_required", 1) is None
    assert w.scripted_draft("cost_explanation", 1).startswith("Your cost")
    assert w.scripted_draft("cost_explanation", 2) is None


def test_case_and_directory_load_from_repo_files():
    s = scenario([])
    assert load_case(s, ROOT).get("order.hcpcs_expected") == "K0001"
    assert len(load_directory(s, ROOT)) == 12
