"""FakeLlm: the Llm Protocol served from a scenario, for offline tests.

Reads come from each world row's `expected_reads` (primary, or safety for check_safety_and_conditions)
by CallContext.row_key; an unlisted field is not_stated (criteria: "missing"). Short forms:
    [turn_id, quote, value]               -> status found
    [turn_id, quote, value, commitment]   -> DatePhraseRead
    [turn_id, quote, "", "ambiguous"]     -> status ambiguous (any field type)
Typed replies ("typed|<n>") read from `typed_reads` by reader name; proposals come from scripted_proposals
by (reason_code, nth), drafts from scripted_drafts by "<purpose>#<n>". Anything unscripted returns
LlmUnavailable(replay_miss), which the rules handle like a real outage.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from dme.clock import TZ
from dme.llm.writer import faq_answer
from dme.models import (
    CriterionRead,
    DatePhraseRead,
    IntentRead,
    LlmUnavailable,
    MessagePurpose,
    OrderDocumentRead,
    PatientMessageDraft,
    PatientReplyRead,
    PcpReplyRead,
    Proposal,
    SafetyRead,
    Scenario,
    ScriptedReads,
    SupplierCallRead,
    Turn,
    VisitNoteRead,
)
from dme.protocols import CallContext
from dme.world import row_keys


def _field(field_cls: type[BaseModel], spec: Any) -> BaseModel:
    if isinstance(spec, dict):
        return field_cls.model_validate(spec)
    turn, quote, value, *rest = spec
    data: dict[str, Any] = {"evidence_turn_id": turn, "evidence_quote": quote, "status": "found", "value": value}
    if rest and rest[0] == "ambiguous":
        data["status"] = "ambiguous"
    if field_cls is DatePhraseRead:
        data["commitment"] = rest[0] if rest and rest[0] != "ambiguous" else "firm"
    return field_cls.model_validate(data)


def _empty(field_cls: type[BaseModel]) -> BaseModel:
    data: dict[str, Any] = {
        "evidence_turn_id": "",
        "evidence_quote": "",
        "status": "not_stated",
        "value": "missing" if field_cls is CriterionRead else "",
    }
    if field_cls is DatePhraseRead:
        data["commitment"] = ""
    return field_cls.model_validate(data)


def expand(model_cls: type[BaseModel], partial: dict[str, Any]) -> BaseModel:
    """A full reader output from a partial scripted one."""
    unknown = set(partial) - set(model_cls.model_fields)
    if unknown:
        raise KeyError(f"{model_cls.__name__} has no fields {sorted(unknown)}")
    values: dict[str, Any] = {}
    for name, info in model_cls.model_fields.items():
        if name == "intents":
            values[name] = [_field(IntentRead, s) for s in partial.get(name, [])]
        else:
            field_cls = info.annotation
            values[name] = _field(field_cls, partial[name]) if name in partial else _empty(field_cls)
    return model_cls(**values)


class FakeLlm:
    mode = ""  # not a live or replayed Claude call

    def __init__(self, scenario: Scenario, typed_reads: dict[str, dict[str, Any]] | None = None) -> None:
        self.scenario = scenario
        self.reads = row_keys(scenario)
        self.typed_reads = typed_reads or {}
        self.calls: list[tuple[str, str]] = []  # (call name, row_key / lookup key)

    def _read(self, call: str, model_cls: type[BaseModel], side: str, ctx: CallContext) -> Any:
        self.calls.append((call, ctx.row_key))
        if ctx.row_key.startswith("typed|"):  # a reply a person typed
            return expand(model_cls, self.typed_reads.get(call, {}))
        if ctx.row_key not in self.reads:
            return LlmUnavailable(call=call, reason="replay_miss", detail=f"no world row {ctx.row_key}")
        scripted = self.reads[ctx.row_key] or ScriptedReads()
        return expand(model_cls, getattr(scripted, side))

    def read_supplier_call(self, turns: list[Turn], ctx: CallContext) -> SupplierCallRead | LlmUnavailable:
        return self._read("read_supplier_call", SupplierCallRead, "primary", ctx)

    def read_pcp_reply(self, turns: list[Turn], ctx: CallContext) -> PcpReplyRead | LlmUnavailable:
        return self._read("read_pcp_reply", PcpReplyRead, "primary", ctx)

    def read_order_document(self, text: str, ctx: CallContext) -> OrderDocumentRead | LlmUnavailable:
        return self._read("read_order_document", OrderDocumentRead, "primary", ctx)

    def read_visit_note(self, text: str, ctx: CallContext) -> VisitNoteRead | LlmUnavailable:
        return self._read("read_visit_note", VisitNoteRead, "primary", ctx)

    def read_patient_reply(self, turns: list[Turn], ctx: CallContext) -> PatientReplyRead | LlmUnavailable:
        return self._read("read_patient_reply", PatientReplyRead, "primary", ctx)

    def check_safety_and_conditions(self, turns: list[Turn], ctx: CallContext) -> SafetyRead | LlmUnavailable:
        return self._read("check_safety_and_conditions", SafetyRead, "safety", ctx)

    def write_patient_message(
        self, purpose: MessagePurpose, facts: dict[str, str], ctx: CallContext
    ) -> PatientMessageDraft | LlmUnavailable:
        key = f"{purpose}#{ctx.nth}"
        self.calls.append(("write_patient_message", key))
        if purpose == "answer_question":  # the writer's FAQ path: no Claude call, "" means a person answers
            return PatientMessageDraft(text=faq_answer(ctx.goal) or "")
        text = self.scenario.scripted_drafts.get(key)
        if text is None:
            return LlmUnavailable(call="write_patient_message", reason="replay_miss", detail=key)
        return PatientMessageDraft(text=text)

    def propose_next_action(
        self, case_summary: str, allowed_actions: list[str], ctx: CallContext
    ) -> Proposal | LlmUnavailable:
        self.calls.append(("propose_next_action", f"{ctx.reason_code}#{ctx.nth}"))
        for scripted in self.scenario.scripted_proposals:
            if (scripted.reason_code.value, scripted.nth) == (ctx.reason_code, ctx.nth):
                return Proposal.model_validate(scripted.proposal)
        return LlmUnavailable(call="propose_next_action", reason="replay_miss", detail=f"{ctx.reason_code}#{ctx.nth}")


# -- builders for small offline worlds -------------------------------------------------------------

START = datetime(2026, 11, 17, 9, 0, tzinfo=TZ)  # a Tuesday
LATE = datetime(2026, 11, 17, 21, 30, tzinfo=TZ)  # the same Tuesday night: outside business hours and her window


def scenario(rows: list[dict], *, start: datetime = START, **extra: Any) -> Scenario:
    expected = extra.pop("expected", {"terminal_state": "delivered", "human_touches": []})
    return Scenario.model_validate(
        {
            "name": "test",
            "case_file": "scenarios/eleanor_case.yaml",
            "start": start.isoformat(),
            "rows": rows,
            "expected": expected,
            **extra,
        }
    )


def runtime(
    rows: list[dict],
    tmp_path: Path,
    *,
    start: datetime = START,
    policy: Any = None,
    scripted_humans: bool = True,
    backstop: bool = True,
    **extra: Any,
):
    """A Runtime over a small scripted world, with FakeLlm. Threads are not started.
    `backstop=False` drops the case_stalled item, for tests of one thread in isolation."""
    from dme.rules.runtime import Runtime

    sc = scenario(rows, start=start, **extra)
    llm = FakeLlm(sc)
    rt = Runtime(
        sc, llm, regressions_path=tmp_path / "regressions.jsonl", policy=policy, scripted_humans=scripted_humans
    )
    rt.backstop = backstop
    return rt, llm


def ready_patient(rt: Any, **overrides: str) -> None:
    """Eleanor's first contact done: identity, consents, address, Medicaid no, Medicare ID."""
    facts = {
        "patient.identity_verified": "yes",
        "patient.dob": "1954-03-03",
        "patient.consent_proceed": "yes",
        "patient.consent_share_with_suppliers": "yes",
        "patient.consent_calls_texts": "yes",
        "patient.address": "4512 N Kedzie Ave, Apartment 2, Chicago, IL 60625",
        "patient.zip": "60625",
        "patient.in_home_use": "yes",
        "patient.home_layout": "one floor, wide doorways",
        "patient.medicaid_or_msp": "no",
        "patient.medicare_id": "1EG4-TE5-MK73",
        "patient.medicare_id_ok_to_share": "yes",
        "patient.availability": "mornings",
    }
    facts.update(overrides)
    rt.case.facts.update(facts)


def ready_docs(rt: Any) -> None:
    """A correct K0001 order and a visit note documenting A, B, D, E, F; criteria accepted."""
    for f in (
        "order.received",
        "order.elements_complete",
        "order.item_matches_k0001",
        "order.changes_ok",
        "note.received",
        "note.criteria_accepted",
    ):
        rt.case.set(f, "yes")
    for c in "abdef":
        rt.case.set(f"note.criterion_{c}", "documented")
    rt.case.set("note.weight_lb", "158")
    rt.case.set("note.version", "1")
    rt.case.thread_state["criteria_accepted_version"] = "1"


def qualified_supplier(rt: Any, sid: int, status: str) -> None:
    """A supplier that already passed its qualification call."""
    from dme.facts import supplier_fact as sf

    entry = rt.supplier(sid)
    for name in ("name", "phone", "address"):
        rt.case.set(sf(sid, name), entry[name])
    for f in (
        "accepting_new_medicare",
        "bills_medicare_k0001",
        "k0001_in_stock",
        "delivers_to_zip",
        "accepts_assignment_all_months",
        "readback_confirmed",
    ):
        rt.case.set(sf(sid, f), "yes")
    rt.case.set(sf(sid, "status"), status)


def ready_for_handoff(rt: Any, sid: int = 4, backup: int | None = 5) -> None:
    """Patient, documents, a primary (and backup) qualified, sourcing done, `sid` the handoff supplier."""
    ready_patient(rt)
    ready_docs(rt)
    qualified_supplier(rt, sid, "primary")
    if backup:
        qualified_supplier(rt, backup, "backup")
    only_suppliers(rt, *[s for s in (sid, backup) if s])
    rt.case.thread_state["sourcing"] = "done"
    rt.case.set("handoff.supplier", str(sid))


def only_suppliers(rt: Any, *ids: int) -> None:
    """Mark every other directory supplier dropped, so sourcing calls only `ids`."""
    from dme.facts import supplier_fact as sf

    for sid in rt.supplier_ids:
        if sid not in ids:
            rt.case.set(sf(sid, "status"), "dropped")


def t(turn_id: str, speaker: str, text: str) -> dict:
    return {"turn_id": turn_id, "speaker": speaker, "text": text}


def qualify_row(
    sid: int,
    attempt: int | str = 1,
    *,
    extra: str = "",
    primary: dict | None = None,
    safety: dict | None = None,
    readback: bool = True,
    delay: str = "10m",
    action: str = "qualify_call",
) -> dict:
    """A qualification call where the rep says yes to everything, plus optional extra words."""
    turns = [
        t(
            "t1",
            "us",
            "Hi, this is Sam from Mira Mace, arranging a standard manual wheelchair, K0001, for an "
            "Original Medicare patient in ZIP 60625. Are you taking new Medicare patients, do you bill "
            "Medicare for it, is it in stock, do you deliver to 60625, will you accept assignment for "
            "all 13 rental months, and would she need to sign an ABN?",
        ),
        t(
            "t2",
            "supplier_rep",
            "Yes, we take new Medicare patients. We bill Medicare for K0001 and it's in stock. "
            "We deliver to 60625 and accept assignment for all 13 months. No ABN. This is Robin.",
        ),
    ]
    if extra:
        turns.append(t("t3", "supplier_rep", extra))
    reads: dict[str, Any] = {
        "call_outcome": ["t2", "Yes, we take new Medicare patients", "answered"],
        "accepting_new_medicare": ["t2", "Yes, we take new Medicare patients", "yes"],
        "bills_medicare_k0001": ["t2", "We bill Medicare for K0001", "yes"],
        "k0001_in_stock": ["t2", "it's in stock", "yes"],
        "delivers_to_zip": ["t2", "We deliver to 60625", "yes"],
        "accepts_assignment_all_months": ["t2", "accept assignment for all 13 months", "yes"],
        "abn_required": ["t2", "No ABN", "no"],
        "rep_name": ["t2", "This is Robin", "Robin"],
    }
    if readback:
        turns += [
            t(
                "readback",
                "us",
                "Let me read that back: new Medicare patients, bills Medicare for K0001, "
                "in stock, delivers to 60625, assignment for all 13 months, no ABN. Correct?",
            ),
            t("t5", "supplier_rep", "Correct."),
        ]
        reads["readback_confirmed"] = ["t5", "Correct", "yes"]
    reads.update(primary or {})
    return {
        "counterparty": f"supplier:{sid}",
        "action": action,
        "attempt": attempt,
        "delay": delay,
        "event": "reply",
        "turns": turns,
        "expected_reads": {"primary": reads, "safety": safety or {}},
    }


def never(counterparty: str, action: str) -> dict:
    return {"counterparty": counterparty, "action": action, "attempt": "default", "event": "never"}


def field_read(turn: str, quote: str, value: str, status: str) -> dict:
    """One reader field written in full, for statuses the short list form cannot express."""
    return {"evidence_turn_id": turn, "evidence_quote": quote, "status": status, "value": value}


def proposal(action: str, **params: Any) -> Proposal:
    """A Proposal with every parameter empty unless given: how a person's chosen action is built."""
    base = {
        "action": action,
        "param_party": "",
        "param_supplier": "",
        "param_items": [],
        "param_question": "",
        "param_time_phrase": "",
        "param_reason": "",
        "why": "advocate",
        "facts_used": [],
    }
    return Proposal.model_validate({**base, **params})


def outbound(rt: Any) -> list[tuple[str, str]]:
    """(counterparty, action) of every outbound action actually sent, in order."""
    return [(l.data["counterparty"], l.data["action"]) for l in rt.engine.log_lines if l.kind == "outbound"]


def sent_lines(rt: Any, action: str) -> list:
    """The outbound log lines for one action."""
    return [l for l in rt.engine.log_lines if l.kind == "outbound" and l.data["action"] == action]


def drain(rt: Any) -> None:
    """Run every queued event without the end-of-run checks (for runs that pause on a human)."""
    while rt.engine.step():
        pass
