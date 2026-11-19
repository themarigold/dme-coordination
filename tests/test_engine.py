import json
from datetime import datetime, timedelta

import pytest

from dme import engine as engine_mod
from dme.clock import TZ, add_business_days
from dme.engine import Engine
from dme.inbox import Inbox
from dme.models import Case, Scenario
from dme.world import UnfiredFollowUps, World, WorldUnscripted

START = datetime(2026, 11, 17, 10, 0, tzinfo=TZ)


def make(tmp_path, rows=(), mode="test", seed=7, log_name=None):
    case = Case(case_id="c1")
    world = World(
        Scenario.model_validate(
            {
                "name": "t",
                "case_file": "scenarios/eleanor_case.yaml",
                "start": START.isoformat(),
                "rows": list(rows),
                "expected": {"terminal_state": "delivered", "human_touches": []},
            }
        ),
        mode=mode,
    )
    inbox = Inbox(case, tmp_path / "regressions.jsonl")
    log_path = tmp_path / log_name if log_name else None
    return Engine(case, START, inbox, world=world, seed=seed, log_path=log_path)


def recorder(seen):
    return lambda eng, ev: seen.append((ev.kind, ev.payload.get("n"), eng.now))


def test_order_by_due_then_priority_then_sequence(tmp_path):
    eng, seen = make(tmp_path), []
    eng.on("tick", recorder(seen))
    later = START + timedelta(hours=1)
    eng.schedule(later, "tick", {"n": "late"})
    eng.schedule(START, "tick", {"n": "low-priority"}, priority=9)
    eng.schedule(START, "tick", {"n": "first-queued"})
    eng.schedule(START, "tick", {"n": "second-queued"})
    eng.schedule(START, "tick", {"n": "urgent"}, priority=0)
    assert eng.run() == "idle"
    assert [n for _, n, _ in seen] == ["urgent", "first-queued", "second-queued", "low-priority", "late"]
    assert seen[-1][2] == later


def test_world_replies_dispatch_and_superseded_attempts_are_stale(tmp_path):
    rows = [
        {"counterparty": "supplier:2", "action": "qualify_call", "attempt": "default", "delay": "30m", "event": "reply"}
    ]
    eng, seen = make(tmp_path, rows), []
    eng.on("reply", recorder(seen))
    first = eng.next_attempt("supplier:2", "qualify_call")
    eng.send("supplier:2", "qualify_call", first)
    second = eng.next_attempt("supplier:2", "qualify_call")  # supersedes the first call's reply
    eng.send("supplier:2", "qualify_call", second)
    eng.run()
    assert len(seen) == 1
    assert [l.kind for l in eng.log_lines].count("stale_ignored") == 1
    assert eng.inbox.items == []  # a stale ordinary reply is not news


def test_stale_delivered_raises_stale_news(tmp_path):
    rows = [
        {"counterparty": "supplier:4", "action": "send_packet", "attempt": 1, "delay": "1h", "event": "accept"},
        {"counterparty": "supplier:4", "action": "after:accept", "attempt": 1, "delay": "1bd", "event": "delivered"},
    ]
    eng = make(tmp_path, rows)
    seen = []
    eng.on("accept", lambda e, ev: (seen.append("accept"), e.next_attempt("supplier:4", "send_packet")))
    eng.on("delivered", lambda e, ev: seen.append("delivered"))
    eng.send("supplier:4", "send_packet", eng.next_attempt("supplier:4", "send_packet"))
    eng.run()
    assert seen == ["accept"]  # the chained delivery inherited the superseded attempt
    assert eng.inbox.touches() == ["stale_news"]


def test_repeated_send_with_same_key_is_not_resent(tmp_path):
    rows = [{"counterparty": "pcp", "action": "order_request", "attempt": 1, "event": "reply"}]
    eng = make(tmp_path, rows)
    eng.on("reply", lambda e, ev: None)
    attempt = eng.next_attempt("pcp", "order_request")
    assert eng.send("pcp", "order_request", attempt) is True
    assert eng.send("pcp", "order_request", attempt) is False
    eng.run()
    assert [l.kind for l in eng.log_lines].count("world_reply") == 1
    assert [l.kind for l in eng.log_lines].count("duplicate_suppressed") == 1


def test_send_needs_an_issued_attempt(tmp_path):
    eng = make(tmp_path)
    with pytest.raises(ValueError, match="next_attempt"):
        eng.send("pcp", "order_request", 1)


def test_frozen_case_sends_nothing(tmp_path):
    eng = make(tmp_path, [{"counterparty": "patient", "action": "first_contact", "attempt": 1, "event": "reply"}])
    eng.case.frozen = True
    assert eng.send("patient", "first_contact", eng.next_attempt("patient", "first_contact")) is False
    assert eng._heap == []


def test_world_miss_raises_in_test_mode_and_goes_to_inbox_in_demo_mode(tmp_path):
    with pytest.raises(WorldUnscripted):
        eng = make(tmp_path)
        eng.send("supplier:9", "qualify_call", eng.next_attempt("supplier:9", "qualify_call"))
    demo = make(tmp_path, mode="demo")
    demo.send("supplier:9", "qualify_call", demo.next_attempt("supplier:9", "qualify_call"))
    assert demo.inbox.touches() == ["world_unscripted"]


def test_step_cap_stalls(tmp_path, monkeypatch):
    monkeypatch.setattr(engine_mod, "MAX_STEPS", 3)
    eng = make(tmp_path)
    eng.on("tick", lambda e, ev: e.schedule(e.now + timedelta(minutes=1), "tick"))
    eng.schedule(START, "tick")
    assert eng.run() == "stalled" and eng.steps == 3
    assert eng.inbox.touches() == ["case_stalled"]


def test_default_step_cap_is_500(tmp_path):
    eng = make(tmp_path)
    eng.on("tick", lambda e, ev: e.schedule(e.now + timedelta(minutes=1), "tick"))
    eng.schedule(START, "tick")
    assert eng.run() == "stalled" and eng.steps == 500


def test_claude_call_cap_stalls(tmp_path):
    eng = make(tmp_path)

    def read(e, ev):
        e.log("claude", "read", "read_supplier_call", llm_call="read_supplier_call", llm_mode="replay")
        e.schedule(e.now + timedelta(minutes=1), "tick")

    eng.on("tick", read)
    eng.schedule(START, "tick")
    assert eng.run() == "stalled" and eng.claude_calls == 200
    assert "Claude call cap" in eng.inbox.items[0].blocking_fact


def test_business_day_cap_is_30_business_days(tmp_path):
    eng, seen = make(tmp_path), []
    eng.on("tick", recorder(seen))
    eng.schedule(add_business_days(START, 30), "tick", {"n": "day 30"})
    eng.schedule(add_business_days(START, 31), "tick", {"n": "day 31"})
    assert eng.run() == "stalled"
    assert [n for _, n, _ in seen] == ["day 30"]  # day 30 still runs; day 31 is past the cap
    assert "business-day cap" in eng.inbox.items[0].blocking_fact


def test_only_lines_naming_a_claude_call_count_toward_the_cap(tmp_path):
    eng = make(tmp_path)
    eng.log("claude", "thinking", "summarised thinking")  # no llm_call: not a call
    eng.log("claude", "read", "read_pcp_reply", llm_call="read_pcp_reply")
    assert eng.claude_calls == 1


def test_handlers_route_by_counterparty_most_specific_first(tmp_path):
    rows = [
        {"counterparty": cp, "action": "ask", "attempt": 1, "event": "reply"} for cp in ("supplier:4", "pcp", "patient")
    ]
    eng, calls = make(tmp_path, rows), []
    eng.on("reply", lambda e, ev: calls.append(("any", ev.payload["counterparty"])))
    eng.on(
        "reply", lambda e, ev: calls.append(("supplier", ev.payload["counterparty"])), counterparty_prefix="supplier:"
    )
    eng.on(
        "reply",
        lambda e, ev: calls.append(("supplier:4", ev.payload["counterparty"])),
        counterparty_prefix="supplier:4",
    )
    eng.on("reply", lambda e, ev: calls.append(("pcp", ev.payload["counterparty"])), counterparty_prefix="pcp")
    for cp in ("supplier:4", "pcp", "patient"):
        eng.send(cp, "ask", eng.next_attempt(cp, "ask"))
    eng.run()
    assert calls == [
        ("supplier:4", "supplier:4"),
        ("supplier", "supplier:4"),
        ("any", "supplier:4"),
        ("pcp", "pcp"),
        ("any", "pcp"),
        ("any", "patient"),
    ]
    with pytest.raises(ValueError):
        eng.on("reply", lambda e, ev: None, counterparty_prefix="pcp")


def test_no_handler_for_the_counterparty_is_a_lookup_error(tmp_path):
    eng = make(tmp_path, [{"counterparty": "patient", "action": "ask", "attempt": 1, "event": "reply"}])
    eng.on("reply", lambda e, ev: None, counterparty_prefix="supplier:")
    eng.send("patient", "ask", eng.next_attempt("patient", "ask"))
    with pytest.raises(LookupError, match="patient"):
        eng.run()


UNFIRED = [
    {"counterparty": "supplier:5", "action": "send_packet", "attempt": 1, "event": "reject"},
    {"counterparty": "supplier:5", "action": "after:accept", "attempt": 1, "delay": "1bd", "event": "scheduled"},
]


def test_unfired_follow_up_rows_fail_a_test_run(tmp_path):
    eng = make(tmp_path, UNFIRED)
    eng.on("reject", lambda e, ev: None)
    eng.send("supplier:5", "send_packet", eng.next_attempt("supplier:5", "send_packet"))
    with pytest.raises(UnfiredFollowUps, match="supplier:5\\|after:accept\\|1"):
        eng.run()


def test_unfired_follow_up_rows_are_a_warning_in_demo_mode(tmp_path):
    eng = make(tmp_path, UNFIRED, mode="demo")
    eng.on("reject", lambda e, ev: None)
    eng.send("supplier:5", "send_packet", eng.next_attempt("supplier:5", "send_packet"))
    assert eng.run() == "idle"
    assert eng.log_lines[-1].kind == "world_warning" and "after:accept" in eng.log_lines[-1].summary


def test_different_seeds_sample_differently_same_seed_the_same(tmp_path):
    def draws(seed):
        eng = make(tmp_path, seed=seed)
        return [eng.sample_blind(0.5) for _ in range(32)]

    assert draws(1) == draws(1)
    assert draws(1) != draws(2)


def test_handler_registration_is_explicit(tmp_path):
    eng = make(tmp_path)
    eng.on("tick", lambda e, ev: None)
    with pytest.raises(ValueError):
        eng.on("tick", lambda e, ev: None)
    eng.schedule(START, "unregistered")
    with pytest.raises(LookupError):
        eng.run()


def _run_seeded(tmp_path, name):
    key = {"counterparty": "supplier:1", "action": "qualify_call", "attempt": "default"}
    rows = [{**key, "delay": "20m", "event": "reply"}, {**key, "delay": "1h", "event": "voicemail"}]
    eng = make(tmp_path, rows, seed=11, log_name=name)

    def on_reply(e, ev):
        blind = e.sample_blind(0.5)
        e.log(
            "claude",
            "read",
            "read_supplier_call",
            llm_call="read_supplier_call",
            llm_mode="live",
            input_tokens=123 if name == "a" else 456,
            latency_ms=9 if name == "a" else 99,
            data={"blind": blind},
        )

    eng.on("reply", on_reply)
    eng.on("voicemail", lambda e, ev: e.log("rule", "voicemail", "left voicemail"))
    for _ in range(3):
        eng.send("supplier:1", "qualify_call", eng.next_attempt("supplier:1", "qualify_call"))
        eng.run()
    return [json.loads(line) for line in (tmp_path / name).read_text().splitlines()]


def test_two_seeded_runs_give_identical_logs_minus_usage(tmp_path):
    drop = {"latency_ms", "input_tokens", "output_tokens", "cost_usd"}
    a, b = _run_seeded(tmp_path, "a"), _run_seeded(tmp_path, "b")
    assert len(a) == len(b) > 5
    assert [{k: v for k, v in x.items() if k not in drop} for x in a] == [
        {k: v for k, v in x.items() if k not in drop} for x in b
    ]
    assert all(set(line) >= {"actor", "transport_mocked", "llm_mode", "prompt_hash"} for line in a)
