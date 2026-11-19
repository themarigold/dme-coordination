"""Claude's proposals: the allowed actions, the legality check, and carrying out an approved action.

Claude proposes one action from a list the rules filtered for the current state.
Code checks that the action and its parameters are legal and that every cited fact
matches the value on file. After two rejections the human decides without a proposal.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING

from dme import clock, facts
from dme.facts import supplier_fact as sf
from dme.inbox import InboxItem
from dme.models import ActionName, Case, EscalationReason, LlmUnavailable, Proposal
from dme.protocols import CallContext
from dme.rules import gate, handoff, holds, patient, pcp, suppliers

if TYPE_CHECKING:
    from dme.rules.runtime import Runtime

APPROVAL_ACTIONS: list[ActionName] = ["proceed_with_pending", "hold_pending"]
LIVE_SUPPLIER = ("candidate", "calling", "hold_needs_info", "qualified", "primary", "backup")

# thread_state keys that are internal bookkeeping, hidden from the proposer.
BOOKKEEPING_KEYS = re.compile(
    r"held_for|settled\..+|medicaid_supplied|criteria_accepted_version|pcp_nudge_gen"
    r"|pcp_nudges|pcp_nudges_parked|pcp_release_waiting|sourcing_opens|handoff_budget_gen|permission_gen|handoff_paused|packet_approved|stated_name"
    r"|patient_details|reconfirm_answered|form_supplier\..+"
    r"|supplier\.\d+\.(callbacks|called_back|reasked|reconfirm_reasked|reconfirm_outage|outage|layout_resent)"
)


def supplier_id(param: str | None) -> int | None:
    """ "supplier:4" -> 4, else None (also for an empty or missing param)."""
    if not param:
        return None
    prefix, _, rest = param.partition(":")
    return int(rest) if prefix == "supplier" and rest.isascii() and rest.isdigit() else None


def allowed_actions(
    case: Case, supplier_ids: list[int], approval_type: str | None, reason: EscalationReason | None = None
) -> list[ActionName]:
    """The actions that are legal in the current state."""
    if approval_type:
        return list(APPROVAL_ACTIONS)
    allowed: list[ActionName] = ["escalate_to_human", "wait_until"]
    if reason in holds.HOLDS_BY_REASON or reason == EscalationReason.PATIENT_DECEASED:
        allowed += APPROVAL_ACTIONS  # deceased: proceed closes the case, hold keeps it frozen
    if case.frozen:
        return allowed
    status = {s: case.get(sf(s, "status")) for s in supplier_ids}
    allowed.append("call_again")
    if "candidate" in status.values():
        allowed.append("try_next_supplier")
    if gate.document_gaps(case) or case.get("note.criteria_accepted") != "yes":
        allowed.append("request_from_pcp")
    if not holds.held(case, "patient_contact"):
        allowed.append("ask_patient")
    if any(v in LIVE_SUPPLIER for v in status.values()):
        allowed.append("release_supplier")
    current = case.get("handoff.supplier")
    if facts.as_int(case.get("handoff.packets_sent")) >= 1 and any(
        v in ("qualified", "backup") and str(s) != current for s, v in status.items()
    ):
        allowed.append("send_packet_to")
    return allowed


def validate(
    proposal: Proposal,
    case: Case,
    allowed: list[ActionName],
    supplier_ids: list[int],
    now: datetime,
    cited_facts: bool = True,
) -> str:
    """ "" if the proposal is legal, else why not. Never raises.

    A person's choice is checked with `cited_facts` False: action and parameters only.
    """
    try:
        return _check(proposal, case, allowed, supplier_ids, now, cited_facts)
    except Exception as e:  # a malformed parameter must not stop the case
        return f"invalid proposal ({type(e).__name__}: {e})"


def _check(
    proposal: Proposal,
    case: Case,
    allowed: list[ActionName],
    supplier_ids: list[int],
    now: datetime,
    cited_facts: bool = True,
) -> str:
    if proposal.action not in allowed:
        return f"action {proposal.action} is not allowed now (allowed: {', '.join(allowed)})"
    for used in proposal.facts_used if cited_facts else ():
        if not facts.is_known(used.fact_id):
            return f"unknown fact id {used.fact_id}"
        if case.get(used.fact_id) != used.value:
            return f"{used.fact_id} is {case.get(used.fact_id)!r}, not {used.value!r}"
    action, party = proposal.action, proposal.param_party
    target = supplier_id(proposal.param_supplier)
    if action == "proceed_with_pending" and cited_facts and not proposal.facts_used:
        return "proceed_with_pending must cite the facts the gated action relies on"
    if action == "hold_pending" and cited_facts and not proposal.facts_used:
        # An approved hold comes back only when a fact it cites changes, so a hold citing
        # nothing would never come back.
        return "hold_pending must cite the facts it waits on (a missing one with its empty value)"
    if action == "call_again" and party not in ("pcp", "patient") and supplier_id(party) not in supplier_ids:
        return f"call_again needs a known party, got {party!r}"
    if action == "call_again" and party == "patient" and holds.held(case, "patient_contact"):
        return "patient contact is on hold"
    if action in ("send_packet_to", "release_supplier") and target not in supplier_ids:
        return f"{action} needs a known supplier, got {proposal.param_supplier!r}"
    if action == "send_packet_to" and (
        case.get(sf(target, "status")) not in ("qualified", "backup") or str(target) == case.get("handoff.supplier")
    ):
        return f"supplier {target} is not a qualified backup"
    if action == "request_from_pcp" and not proposal.param_items:
        return "request_from_pcp needs the items to request"
    if action == "ask_patient" and proposal.param_question not in patient.QUESTIONS:
        return f"unknown patient question {proposal.param_question!r}"
    if action == "wait_until":
        if clock.resolve_date_phrase(proposal.param_time_phrase, "firm", now)[0] is None:
            return f"cannot resolve {proposal.param_time_phrase!r}"
    if action == "escalate_to_human" and proposal.param_reason not in {r.value for r in EscalationReason}:
        return f"unknown escalation reason {proposal.param_reason!r}"
    return ""


def relies_on(rt: Runtime, item: InboxItem) -> tuple:
    """What an item's gated action depends on; an approved hold comes back when this changes."""
    case = rt.case
    common = (tuple(sorted(case.holds)), case.frozen)
    if item.approval_type == "supplier_handoff":
        sid = handoff.current(rt)
        return (*common, handoff._binding(rt), tuple(handoff.send_unmet(rt, sid)) if sid is not None else ())
    if item.approval_type == "criteria_acceptance":
        return (*common, case.get("note.version"), tuple(gate.criteria_gaps(case)), gate.weight_ok(case))
    return (*common, tuple(holds.unsettled(case, item.reason)), case.get("patient.deceased"))


def case_summary(case: Case, blocking_fact: str) -> str:
    """Plain facts for the proposer: what is blocking, then every known fact."""
    lines = [f"blocking: {blocking_fact}", f"holds: {', '.join(case.holds) or 'none'}"]
    lines += [f"{k} = {v}" for k, v in sorted(case.facts.items()) if v]
    lines += [
        f"thread {k}: {v}" for k, v in sorted(case.thread_state.items()) if v and not BOOKKEEPING_KEYS.fullmatch(k)
    ]
    return "\n".join(lines)


def proposal_goal(case: Case, reason: EscalationReason, approval_type: str | None) -> str:
    """The proposer's goal. On an approval item it names exactly what proceeding does."""
    if approval_type == "criteria_acceptance":
        return (
            "Approval required: accept the visit-note criteria A-G for the packet (proceeding sends "
            "nothing; the order and packet are gated separately)."
        )
    if approval_type == "supplier_handoff":
        sid = case.get("handoff.supplier")
        name = case.get(sf(sid, "name")) if sid else ""
        return (
            f"Approval required: send the order packet to supplier {sid} ({name}) (proceeding sends the "
            f"order, the visit note and {case.get('patient.name')}'s delivery details to that supplier; "
            "nothing else)."
        )
    return f"Propose the next action for: {reason.value}"


def propose(
    rt: Runtime,
    reason: EscalationReason,
    nth: int,
    blocking_fact: str,
    allowed: list[ActionName],
    approval_type: str | None = None,
) -> tuple[Proposal | None, str]:
    """Ask Claude for one allowed action, retrying once with the rejection reason.

    Returns (proposal, ""), (None, why both were rejected), or (None, "") when Claude is unavailable.
    """
    summary = case_summary(rt.case, blocking_fact)
    ctx = CallContext(
        case_id=rt.case.case_id,
        goal=proposal_goal(rt.case, reason, approval_type),
        facts={},
        reason_code=reason.value,
        nth=nth,
    )
    problem = ""
    for _ in range(2):
        result = rt.call_llm("propose_next_action", lambda: rt.llm.propose_next_action(summary, list(allowed), ctx))
        if isinstance(result, LlmUnavailable):
            return None, ""
        problem = validate(result, rt.case, allowed, rt.supplier_ids, rt.now)
        if not problem:
            return result, ""
        rt.log("proposal_rejected", f"{result.action}: {problem}")
        summary += f"\nYour previous proposal was rejected: {problem}"
    return None, problem


def execute(rt: Runtime, proposal: Proposal, item: InboxItem | None = None) -> None:
    """Carry out an action a human approved or chose.

    Calling the handoff supplier after it has the packet is a status call, never a new qualification call.
    """
    action, target = proposal.action, supplier_id(proposal.param_supplier)
    if action == "call_again":
        party_supplier = supplier_id(proposal.param_party)
        if (
            party_supplier is not None
            and party_supplier == handoff.current(rt)
            and rt.case.get(sf(party_supplier, "packet_sent")) == "yes"
        ):
            handoff.status_call(rt, party_supplier)
        elif party_supplier in rt.supplier_ids:
            suppliers.call(rt, party_supplier)
        elif proposal.param_party == "pcp":
            pcp.confirm_call(rt)
        elif proposal.param_party == "patient":
            patient.retry_pending(rt)
    elif action == "try_next_supplier":
        suppliers.resume(rt)
    elif action == "request_from_pcp":
        pcp.request_items(rt, proposal.param_items)
    elif action == "ask_patient":
        patient.ask(rt, proposal.param_question)
    elif action == "release_supplier" and target is not None:
        if target == handoff.current(rt) and handoff.in_progress(rt):
            handoff.release_current(rt, "released by the advocate")  # fails over, or goes to a person after a packet
        else:
            suppliers.release(rt, target, "released by the advocate")
            suppliers.call_next(rt)  # it may have been the one on the line
    elif action == "send_packet_to" and target is not None:
        handoff.second_handoff(rt, target)
    elif action == "escalate_to_human":  # a fresh item under the reason the advocate gave
        context = f": {item.blocking_fact}" if item is not None else ""
        given = proposal.param_reason
        valid = given in {r.value for r in EscalationReason}
        # The inbox refuses an unknown reason; a direct call with one keeps the item's own.
        reason = (
            EscalationReason(given) if valid else item.reason if item is not None else EscalationReason.CASE_STALLED
        )
        note = "" if valid else f" (reason {given!r} not known; kept {reason.value})"
        rt.escalate(reason, f"escalated by the advocate{context}{note}")
    elif action == "wait_until":
        moment, _ = clock.resolve_date_phrase(proposal.param_time_phrase, "firm", rt.now)
        reraise = (
            {"reraise": item.reason.value, "blocking_fact": item.blocking_fact, "item_id": item.item_id}
            if item is not None and rt.owns(item.reason, item.item_id)
            else {}
        )
        rt.schedule(moment or rt.now, "re_evaluate", reraise)
    rt.log("action_executed", f"{action} carried out", data=proposal.model_dump())
