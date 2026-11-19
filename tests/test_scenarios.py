"""The three scenario files run end to end offline and match their expected outcomes."""

import json
from pathlib import Path

import pytest
from fakes import FakeLlm, expand, outbound, row_keys

from dme.evidence import document_turns, validate_read
from dme.facts import supplier_fact as sf
from dme.models import (
    OrderDocumentRead,
    PatientReplyRead,
    PcpReplyRead,
    SafetyRead,
    SupplierCallRead,
    VisitNoteRead,
)
from dme.rules.runtime import READER_ROLES, Runtime, party_kind
from dme.world import ROOT, load_scenario

SCENARIOS = ["eleanor_main", "eleanor_happy", "eleanor_fallback"]
ORACLE = ["approval_required:criteria_acceptance", "approval_required:supplier_handoff"]


def run(name: str, tmp_path: Path, log: str = "") -> Runtime:
    sc = load_scenario(ROOT / "scenarios" / f"{name}.yaml")
    rt = Runtime(
        sc, FakeLlm(sc), regressions_path=tmp_path / "regressions.jsonl", log_path=tmp_path / log if log else None
    )
    rt.run()
    return rt


@pytest.mark.parametrize("name", SCENARIOS)
def test_scenario_reaches_its_expected_outcome(name, tmp_path):
    rt = run(name, tmp_path)
    assert rt.mismatches() == []
    assert rt.outcome()["terminal_state"] == "delivered"


def test_main_scenario_oracle_is_unchanged():
    expected = load_scenario(ROOT / "scenarios/eleanor_main.yaml").expected
    assert (expected.terminal_state, expected.human_touches) == ("delivered", ORACLE)


@pytest.mark.parametrize("name", SCENARIOS)
def test_every_scripted_read_survives_the_evidence_check(name):
    """No scripted field is downgraded: a live run reads these texts, so each quote must be in its turn."""
    sc = load_scenario(ROOT / "scenarios" / f"{name}.yaml")
    models = {"reply": {"supplier": SupplierCallRead, "pcp": PcpReplyRead, "patient": PatientReplyRead}}
    for key, row in zip(row_keys(sc), sc.rows):
        if row.expected_reads is None:
            continue
        party = party_kind(row.counterparty)
        if row.event == "document":
            model = OrderDocumentRead if row.payload["doc_type"] == "order" else VisitNoteRead
            turns = document_turns(row.text)
        else:
            model, turns = models[row.event][party], row.turns
        for side, model_cls in (("primary", model), ("safety", SafetyRead)):
            partial = getattr(row.expected_reads, side)
            _, flags = validate_read(expand(model_cls, partial), turns, READER_ROLES[party])
            downgraded = {f: v for f, v in flags.items() if any(x.startswith("downgraded") for x in v)}
            assert downgraded == {}, (key, side)


def test_main_timeline_follows_spec_section_4(tmp_path):
    rt = run("eleanor_main", tmp_path)
    c = rt.case
    status = {s: c.get(sf(s, "status")) for s in (1, 2, 3, 4, 5)}
    assert status == {1: "skipped", 2: "candidate", 3: "dropped", 4: "released", 5: "primary"}
    sent = outbound(rt)
    # supplier 4's Medicare ID need was met automatically (she had consented)
    assert ("supplier:4", "share_info") in sent
    # the lost PCP request was caught by the confirmation call and re-sent
    assert sent.count(("pcp", "order_request")) == 2 and sent.count(("pcp", "confirm_call")) == 2
    # signed release, K0003 question, one specific request for criterion B
    assert ("patient", "esign_release") in sent and ("pcp", "send_release") in sent
    assert ("pcp", "ask_item_code") in sent and sent.count(("pcp", "note_fix_request")) == 1
    assert c.get("order.hcpcs_on_order") == "K0001" and c.get("order.changes_ok") == "yes"
    # supplier 4 silent twice before any packet: automatic failover, no human
    assert sent.count(("supplier:4", "reconfirm_call")) == 2 and ("supplier:4", "release") in sent
    assert ("supplier:4", "send_packet") not in sent
    assert sent.count(("pcp", "supplier_chosen")) == 2  # told again on failover
    # Eleanor's cost question got the range, with figures (supplier qualified, Medicaid no)
    cost_line = next(l for l in rt.engine.log_lines if l.kind == "outbound" and l.data["action"] == "cost_explanation")
    assert f"${c.get('cost.low')}" in cost_line.data["text"] and f"${c.get('cost.high')}" in cost_line.data["text"]
    # one packet, after permission, to supplier 5; delivery confirmed by both
    assert sent.count(("supplier:5", "send_packet")) == 1
    assert sent.index(("patient", "esign_supplier_permission")) < sent.index(("supplier:5", "send_packet"))
    assert c.get(sf(5, "call_permission")) == "yes" and c.get(sf(5, "accepted")) == "yes"
    assert c.get("patient.fit_ok") == "yes" and c.get("patient.delivery_received") == "yes"
    # supplier 2's no answer got its callback while the line was free again
    assert sent.count(("supplier:2", "qualify_call")) == 2
    # the supplier's acceptance confirms the handoff approval, not the criteria reading
    assert rt.policy.switches["criteria_acceptance"].streak == 0
    assert rt.policy.switches["supplier_handoff"].streak == 1


def test_fallback_drops_the_release_and_fit_check(tmp_path):
    rt = run("eleanor_fallback", tmp_path)
    assert ("patient", "esign_release") not in outbound(rt)
    assert rt.case.get("patient.fit_ok") == ""


def test_two_runs_give_identical_logs(tmp_path):
    a = run("eleanor_main", tmp_path, "a.jsonl")
    b = run("eleanor_main", tmp_path, "b.jsonl")
    lines = [[json.loads(x) for x in (tmp_path / n).read_text().splitlines()] for n in ("a.jsonl", "b.jsonl")]
    assert lines[0] == lines[1] and len(lines[0]) > 50
    assert a.outcome() == b.outcome()


@pytest.mark.parametrize("end, days", [((2026, 11, 21, 12), 0), ((2026, 11, 23, 9), 1), ((2026, 11, 27, 9), 4)])
def test_business_days_count_only_business_days_up_to_the_end(tmp_path, end, days):
    from datetime import datetime

    from fakes import runtime

    from dme.clock import TZ

    friday = datetime(2026, 11, 20, 9, 0, tzinfo=TZ)
    rt, _ = runtime([], tmp_path, start=friday)
    rt.engine.now = datetime(*end, tzinfo=TZ)  # Sat 21 / Mon 23 / Fri 27 (Thanksgiving on Thu 26)
    assert rt.outcome()["business_days"] == days
