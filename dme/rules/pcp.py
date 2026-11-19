"""The PCP thread: getting the written order and visit note from the doctor's office.

A fixed request, a confirmation call, then nudges. The criteria reading of a complete
visit note goes through the approval switch.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING

from dme import clock, knowledge
from dme.engine import Event
from dme.facts import as_int
from dme.models import EscalationReason as R
from dme.models import OrderDocumentRead, PcpReplyRead, VisitNoteRead
from dme.rules import gate, patient, signals, suppliers

if TYPE_CHECKING:
    from dme.rules.runtime import Runtime

CONFIRM_AFTER_HOURS = 2
NUDGE_EVERY_DAYS = 2
MAX_NUDGES = 2
REREAD_HOURS = 1  # a reply Claude could not read is read once more this much later
CRITERIA = "abcdefg"
WITH_A_HUMAN = {R.DOCS_STILL_MISSING, R.WEIGHT_OVER_250, R.PCP_CHANGED_ITEM, R.PCP_DECLINED, R.PCP_SILENT}
REPLY_GOAL = (
    "Read the PCP office's reply about our request for the standard written order and visit note: "
    "did they receive it, when will they send it, what do they need, which item code they confirm."
)


def register(rt: Runtime) -> None:
    rt.route("reply", "pcp", "*", on_reply)
    rt.route("no_answer", "pcp", "*", on_no_answer)
    rt.route("document", "pcp", "*", on_document)
    rt.timer("pcp_confirm_due", on_confirm_due)
    rt.timer("pcp_nudge", on_nudge)
    rt.timer("pcp_reread", on_reply)
    rt.timer("pcp_call_due", lambda rt, ev: confirm_call(rt))
    rt.timer("pcp_code_call_due", lambda rt, ev: _ask_code_call(rt))


def start(rt: Runtime) -> None:
    send_request(rt)


def send_request(rt: Runtime) -> None:
    """Fax the fixed request, then confirm by phone within a few hours."""
    rt.send("pcp", "order_request", items=["standard written order", "visit note"])
    rt.case.set("pcp.request_status", "sent")
    rt.in_business_hours(CONFIRM_AFTER_HOURS, "pcp_confirm_due")
    schedule_nudge(rt)  # every request has a budget, even if no call ever gets through


def on_confirm_due(rt: Runtime, event: Event) -> None:
    if rt.case.get("pcp.request_status") in ("sent", "not_received"):
        confirm_call(rt)


def confirm_call(rt: Runtime) -> None:
    """A call to the office: in business hours only (else at the next business moment)."""
    if rt.after_hours("confirmation call to the office", "pcp_call_due"):
        return
    rt.send("pcp", "confirm_call")


def docs_complete(rt: Runtime) -> bool:
    return not gate.document_gaps(rt.case)


def schedule_nudge(rt: Runtime) -> None:
    """(Re)start the nudge clock; older nudge timers lapse."""
    generation = int(rt.case.thread_state.get("pcp_nudge_gen", "0")) + 1
    rt.case.thread_state["pcp_nudge_gen"] = str(generation)
    rt.in_business_days(NUDGE_EVERY_DAYS, "pcp_nudge", {"gen": generation})


def on_nudge(rt: Runtime, event: Event) -> None:
    state = rt.case.thread_state
    if event.payload["gen"] != int(state.get("pcp_nudge_gen", "0")) or docs_complete(rt):
        return
    if _person_owns_docs(rt):
        state["pcp_nudges_parked"] = "yes"  # a person owns the question; the clock restarts after
        return
    nudges = int(state.get("pcp_nudges", "0"))
    if nudges < MAX_NUDGES and rt.after_hours("nudge to the office", "pcp_nudge", event.payload):
        return  # the same nudge (same generation), at the next business moment
    if nudges >= MAX_NUDGES:
        rt.escalate(
            R.PCP_SILENT,
            "documents not received after the request and two nudges",
            ["fax request", "confirmation call", f"{nudges} nudges"],
        )
        return
    state["pcp_nudges"] = str(nudges + 1)
    rt.send("pcp", "nudge")
    schedule_nudge(rt)


def _person_owns_docs(rt: Runtime) -> bool:
    return any(item.reason in WITH_A_HUMAN for item in rt.inbox.open_items())


def resume_nudges(rt: Runtime) -> None:
    """Restart a parked nudge clock once no person owns the documents question."""
    state = rt.case.thread_state
    if state.get("pcp_nudges_parked") == "yes" and not _person_owns_docs(rt):
        state.pop("pcp_nudges_parked")
        if not docs_complete(rt):
            schedule_nudge(rt)


def on_no_answer(rt: Runtime, event: Event) -> None:
    rt.log("pcp_no_answer", f"office did not answer ({event.payload['action']})")
    schedule_nudge(rt)


# Replies


def on_reply(rt: Runtime, event: Event) -> None:
    reread = event.payload.get("reread", False)
    reading = rt.read(
        "read_pcp_reply", event, REPLY_GOAL, ("pcp.name", "pcp.practice", "order.hcpcs_on_order"), escalate=reread
    )
    safety = rt.read("check_safety_and_conditions", event, "Find safety signals about the patient.", escalate=reread)
    signals.raise_signals(rt, safety.raw if safety else None, "the PCP office")  # even if one read failed
    if reading is None or safety is None:
        if not reread:  # never drop a reply: read it once more later; a second failure goes to a person
            rt.schedule_reread(REREAD_HOURS, "pcp_reread", event)
        return
    read: PcpReplyRead = reading.read
    if read.contact_name.status == "found":
        rt.case.set("pcp.contact_name", read.contact_name.value)
    if rt.case.thread_state.get("pcp_code_question") and read.item_confirmed.status == "found":
        on_code_answer(rt, read.item_confirmed.value)
    outcome = read.outcome.value
    if outcome == "not_received":
        rt.case.set("pcp.request_status", "not_received")
        rt.log("pcp_request_lost", "office never got the request: resending and confirming again")
        send_request(rt)
        return
    if (
        outcome
        in ("received", "will_send_by", "needs_signed_release", "already_sent_to_supplier", "wants_supplier_form")
        and rt.case.get("pcp.request_status") != "docs_received"
    ):
        rt.case.set("pcp.request_status", "confirmed")
    if outcome == "needs_signed_release":
        request_release(rt)
    elif outcome == "already_sent_to_supplier":
        named = read.supplier_named.value
        rt.case.thread_state["pcp_sent_to"] = named
        rt.log("pcp_sent_to_supplier", f"office says it already sent the documents to {named!r}")
    elif outcome == "wants_supplier_form":
        rt.case.thread_state["pcp_wants_supplier_form"] = "yes"
        rt.log("pcp_wants_supplier_form", "office wants the supplier's own form (logged only; not routed yet)")
    elif outcome == "needs_visit_or_declines":
        rt.escalate(R.PCP_DECLINED, "office needs a new visit or declines to send the order")
        return
    elif outcome == "will_send_by":
        moment, _ = clock.resolve_date_phrase(read.send_by.value, read.send_by.commitment, rt.now, purpose="deadline")
        rt.log("pcp_will_send", f"office will send by {moment.isoformat() if moment else read.send_by.value!r}")
    if not docs_complete(rt):
        schedule_nudge(rt)


def on_code_answer(rt: Runtime, code: str) -> None:
    """The office answered "did you mean K0001?"."""
    entry = knowledge.code_entry(code)
    rt.case.thread_state.pop("pcp_code_question", None)
    if entry is not None and entry["requested"]:
        rt.log("pcp_code_confirmed", f"office confirms {code}; waiting for the initialled correction or a new order")
    elif entry is not None:
        rt.escalate(R.PCP_CHANGED_ITEM, f"office confirms {code} ({entry['item']}), not K0001")
    else:
        ask_code(rt, code or "unclear answer")


def ask_code(rt: Runtime, code: str) -> None:
    """Ask the office once which item they meant; a second time goes to a person."""
    state = rt.case.thread_state
    if state.get("pcp_code_asked") == "yes":
        state.pop("pcp_code_question", None)
        rt.escalate(R.PCP_CHANGED_ITEM, "item code still unclear after asking the office once")
        return
    state["pcp_code_asked"] = "yes"
    state["pcp_code_question"] = code
    _ask_code_call(rt)


def _ask_code_call(rt: Runtime) -> None:
    """Phone the office with the item-code question, in business hours only."""
    code = rt.case.thread_state.get("pcp_code_question")
    if not code or rt.after_hours("item-code question to the office", "pcp_code_call_due"):
        return
    rt.send("pcp", "ask_item_code", code_on_order=code, item_requested="K0001")


def request_release(rt: Runtime) -> None:
    """The office wants her signed release; the request goes out once she may be disclosed."""
    rt.case.thread_state["pcp_release"] = "wanted"
    resume_release(rt)


def on_release_signed(rt: Runtime) -> None:
    """Eleanor signed the release; it is forwarded once she may be disclosed."""
    rt.case.set("pcp.release_signed", "yes")
    rt.case.thread_state["pcp_release"] = "signed"
    resume_release(rt)


def resume_release(rt: Runtime) -> None:
    """Send the pending release request or signed release, if she may be disclosed now.

    Disclosure is checked at the moment anything about her goes out, not when it was queued.
    """
    state = rt.case.thread_state
    step = state.get("pcp_release", "")
    if step not in ("wanted", "signed"):
        return
    if not suppliers.may_disclose(rt.case):
        if state.get("pcp_release_waiting") != step:
            state["pcp_release_waiting"] = step
            rt.log(
                "release_deferred",
                f"release {'request' if step == 'wanted' else 'forwarding'} waits: her "
                "identity, consent or a contact hold stops it for now",
            )
        return
    state.pop("pcp_release_waiting", None)
    if step == "wanted":
        state["pcp_release"] = "requested"
        patient.request_release(rt)
    else:
        state["pcp_release"] = "sent"
        rt.send("pcp", "send_release")


def notify_supplier_chosen(rt: Runtime, supplier_id: int) -> None:
    """Fixed template: send the documents to this supplier, with a copy to us."""
    rt.case.set("pcp.supplier_notified", str(supplier_id))
    rt.send("pcp", "supplier_chosen", supplier=supplier_id)


def request_items(rt: Runtime, items: list[str]) -> None:
    """Send the office a specific request, with its own nudge clock."""
    rt.send("pcp", "note_fix_request", items=items)
    schedule_nudge(rt)


# Documents


def on_document(rt: Runtime, event: Event) -> None:
    doc_type = event.payload["row"].payload.get("doc_type")
    if doc_type == "order":
        on_order(rt, event)
    elif doc_type == "visit_note":
        on_note(rt, event)
    else:
        raise ValueError(f"document without a known doc_type: {doc_type!r}")
    if docs_complete(rt):
        rt.case.set("pcp.request_status", "docs_received")
    else:
        schedule_nudge(rt)  # the clock restarts with each document
    rt.evaluate()


def _found(field) -> bool:
    return field.status == "found" and bool(field.value)


def _bump(rt: Runtime, fact_id: str) -> str:
    version = str(as_int(rt.case.get(fact_id)) + 1)
    rt.case.set(fact_id, version)
    return version


ORDER_ELEMENT_NAMES = {  # what the office is asked to fix, in its own words
    "order.beneficiary_ok": "patient name or Medicare ID",
    "order.description_ok": "item description or HCPCS code",
    "order.quantity_ok": "quantity",
    "order.order_date_ok": "order date",
    "order.practitioner_ok": "practitioner name or NPI",
    "order.signature_ok": "practitioner signature",
}


def _name_tokens(name: str) -> frozenset[str]:
    """Name words, case-folded, without initials or punctuation, so "Last, First" equals "First Last"."""
    return frozenset(w for w in re.findall(r"[a-z]+", name.lower()) if len(w) > 1)


def beneficiary_matches(rt: Runtime, read: OrderDocumentRead) -> tuple[bool, str]:
    """(whether the order is for her, the name it gives): her name or her Medicare ID must match."""
    named = read.beneficiary_name.value if _found(read.beneficiary_name) else ""
    ours = _name_tokens(rt.case.get("patient.name"))
    if named and ours and _name_tokens(named) == ours:
        return True, named
    on_file = re.sub(r"[^0-9A-Z]", "", rt.case.get("patient.medicare_id").upper())
    given = re.sub(r"[^0-9A-Z]", "", read.medicare_id.value.upper()) if _found(read.medicare_id) else ""
    return bool(on_file and given and given == on_file), named


NAME_TITLES = frozenset({"mr", "mrs", "ms", "miss", "mx", "dr", "patient", "pt"})


def note_beneficiary_problem(rt: Runtime, reading) -> str:
    """Why the visit note may not be hers, or "" when nothing says so.

    A name with a word hers lacks ("Robert Martinez") or a name the reader could not
    read clearly counts against it; titles and initials are ignored, so "Mrs.
    Martinez" is hers.
    """
    field = reading.read.beneficiary_name
    ours = _name_tokens(rt.case.get("patient.name"))
    if field.status in signals.DOUBT or reading.unknown("beneficiary_name"):
        return "the patient's name on the visit note is unclear"
    if not _found(field) or not ours:
        return ""
    theirs = _name_tokens(field.value) - NAME_TITLES
    if not theirs or not theirs <= ours:
        return f"the visit note names {field.value!r}, not {rt.case.get('patient.name')}"
    return ""


ORDER_PASSING = (
    "order.elements_complete",
    "order.item_matches_k0001",
    "order.changes_ok",
    "order.hcpcs_on_order",
    *ORDER_ELEMENT_NAMES,
)


def on_order(rt: Runtime, event: Event) -> None:
    reading = rt.read(
        "read_order_document",
        event,
        "Read the written order for a manual wheelchair.",
        ("patient.name", "order.hcpcs_expected"),
    )
    version = _bump(rt, "order.version")  # the previous version's facts stop counting
    if reading is None:  # unreadable: nothing about it passes
        for fact in ORDER_PASSING:
            rt.case.set(fact, "")
        rt.log("order_unreadable", f"order v{version} could not be read: its checks count as unmet")
        return
    read: OrderDocumentRead = reading.read
    rt.case.set("order.received", "yes")
    code = read.hcpcs_code.value.strip() if read.hcpcs_code.status == "found" else ""  # only a code read clearly
    rt.case.set("order.hcpcs_on_order", code)
    rt.case.set("order.item_description", read.item_description.value)
    beneficiary_ok, named = beneficiary_matches(rt, read)
    elements = {
        "order.beneficiary_ok": beneficiary_ok,
        "order.description_ok": _found(read.item_description) or _found(read.hcpcs_code),
        "order.quantity_ok": _found(read.quantity),
        "order.order_date_ok": _found(read.order_date),
        "order.practitioner_ok": _found(read.practitioner_name) or _found(read.practitioner_npi),
        "order.signature_ok": gate.found(read.signature_present),
    }
    for fact_id, ok in elements.items():
        rt.case.set(fact_id, "yes" if ok else "no")
    rt.case.set("order.elements_complete", "yes" if all(elements.values()) else "no")
    # A correction answer that was downgraded or dropped is unknown, never "no changes".
    uncertain = reading.unknown("has_changes", "changes_initialled_dated")
    unchanged = gate.found(read.has_changes, "no") or read.has_changes.status == "not_stated"
    changes_ok = not uncertain and (unchanged or gate.found(read.changes_initialled_dated))
    rt.case.set("order.changes_ok", "yes" if changes_ok else "no")

    entry = knowledge.code_entry(code) if code else None
    rt.case.set("order.item_matches_k0001", "yes" if entry and entry["requested"] else "no")
    rt.log(
        "order_read",
        f"order v{version}: code {code or '(none)'}, elements {'complete' if all(elements.values()) else 'incomplete'}",
    )
    if entry is None or not entry["requested"]:
        ask_code(rt, code or "no code")  # a different item, or an unknown or misread code
        return
    # One combined request: the correction question and every missing element together.
    items = []
    if uncertain:
        items.append("confirm whether the order was changed; initial and date any change, or sign a new order")
    elif rt.case.get("order.changes_ok") == "no":
        items.append("initial and date the change, or sign a new order")
    mismatch = f"patient on the order does not match: it names {named!r}, not {rt.case.get('patient.name')}"
    items += [
        mismatch if f == "order.beneficiary_ok" and named else ORDER_ELEMENT_NAMES[f]
        for f, ok in elements.items()
        if not ok
    ]
    if items:
        rt.send("pcp", "order_fix_request", items=items)


def on_note(rt: Runtime, event: Event) -> None:
    reading = rt.read(
        "read_visit_note",
        event,
        "Read the visit note: coverage criteria A-G and the patient's weight.",
        ("patient.name",),
    )
    version = _bump(rt, "note.version")
    rt.case.set("note.received", "yes")
    rt.case.set("note.criteria_accepted", "")  # a new version needs its own acceptance
    if reading is None:  # unreadable: no criterion counts
        for c in CRITERIA:
            rt.case.set(f"note.criterion_{c}", "missing")
        rt.case.set("note.weight_lb", "")
        rt.log("note_unreadable", f"visit note v{version} could not be read: no criterion counts")
        return
    read: VisitNoteRead = reading.read
    not_hers = note_beneficiary_problem(rt, reading)
    if not_hers:  # none of another patient's note counts for her
        for c in CRITERIA:
            rt.case.set(f"note.criterion_{c}", "missing")
        rt.case.set("note.weight_lb", "")
        rt.log("note_not_hers", f"visit note v{version}: {not_hers}; no criterion counts")
        if rt.case.thread_state.get("note_request") != "sent":
            rt.case.thread_state["note_request"] = "sent"
            request_items(rt, [f"{not_hers}: please send {rt.case.get('patient.name') or 'her'}'s own visit note"])
        else:
            rt.escalate(
                R.DOCS_STILL_MISSING, f"visit note v{version}: {not_hers}", ["one specific request to the office"]
            )
        return
    for c in CRITERIA:
        field = getattr(read, f"criterion_{c}")
        value = (
            field.value
            if field.status == "found"
            else "contradictory"
            if field.status == "contradictory"
            else "missing"
        )
        rt.case.set(f"note.criterion_{c}", value)
    rt.case.set("note.weight_lb", weight_in_lb(read.weight_lb))
    weight = rt.case.get("note.weight_lb")
    limit = knowledge.entry("weight_threshold")["k0006_over_lb"]
    if weight and float(weight) > limit:  # compared in full: 250.4 lb is over 250
        rt.escalate(R.WEIGHT_OVER_250, f"visit note v{version} states {weight} lb: K0006 territory, a different item")
        return
    gaps = gate.criteria_gaps(rt.case) + ([] if weight else ["weight_lb"])
    if gaps:
        if rt.case.thread_state.get("note_request") != "sent":
            rt.case.thread_state["note_request"] = "sent"
            rt.log("note_gaps", f"visit note v{version} is missing {', '.join(gaps)}: one specific request")
            request_items(rt, gaps)
        else:
            rt.escalate(
                R.DOCS_STILL_MISSING,
                f"visit note v{version} still missing {', '.join(gaps)}",
                ["one specific request to the office"],
            )
        return
    rt.request_approval(
        "criteria_acceptance",
        f"visit note v{version} documents A, B, D, E and F or G; weight {weight} lb",
        [f"read visit note v{version}"],
        lambda rt: accept_criteria(rt, version),
    )


def accept_criteria(rt: Runtime, version: str) -> None:
    """A person accepted the criteria reading of note `version`; it counts only while that version is current."""
    if version != rt.case.get("note.version"):
        rt.log(
            "criteria_acceptance_stale",
            f"acceptance of visit note v{version} ignored: v{rt.case.get('note.version')} is current",
        )
        return
    rt.case.set("note.criteria_accepted", "yes")
    rt.case.thread_state["criteria_accepted_version"] = version
    rt.log("criteria_accepted", f"criteria reading of visit note v{version} accepted")
    rt.evaluate()


KG_TO_LB = 2.20462
NUMBER = r"[0-9]+(?:\.[0-9]+)?"
KG_UNITS = ("kg", "kgs", "kilogram", "kilograms")
LB_UNITS = ("lb", "lbs", "pound", "pounds")


def _unit_kind(unit: str) -> str:
    """ "kg", "lb" or "" (no unit, or one we do not know)."""
    return "kg" if unit in KG_UNITS else "lb" if unit in LB_UNITS else ""


def weight_in_lb(field) -> str:
    """The weight in pounds, or "" unless the reader found one clear weight.

    The number and unit must come from the quote itself (no unit means pounds;
    kilograms are converted). Conflicting units, ranges, two numbers, or a number
    the quote does not carry give no weight, and the office is asked instead.
    """
    if field.status != "found":
        return ""
    m = re.fullmatch(rf"\s*({NUMBER})\s*([a-zA-Z]*)\.?\s*", field.value)
    quoted = {float(n) for n in re.findall(NUMBER, field.evidence_quote)} - {0.0}
    if not m or len(quoted) != 1:
        return ""
    number, unit = float(m.group(1)), m.group(2).lower()
    if unit and not _unit_kind(unit):
        return ""  # a unit we do not know (stone, ounces, ...)
    quote_kinds = {
        _unit_kind(u) for u in re.findall(r"[0-9]\s*(kg|kgs|kilograms?|lb|lbs|pounds?)\b", field.evidence_quote.lower())
    }
    if len(quote_kinds) > 1:
        return ""  # the quote gives the number in both units: not one clear weight
    quote_kind = quote_kinds.pop() if quote_kinds else ""
    (written,) = quoted
    if number != written:  # the value must be the quote's own number
        converted = written * KG_TO_LB
        if _unit_kind(unit) in ("lb", "") and quote_kind == "kg" and abs(converted - number) <= 0.5:
            return _pounds(converted, converted=True)  # the reader converted the quote's kilograms itself
        return ""
    kind = quote_kind or "lb"  # the quote decides: a number written with no unit is pounds
    if unit and _unit_kind(unit) != kind:
        return ""  # "120 lb" read from "120 kg" (or "74 kg" from a plain "74"): the units conflict
    return _pounds(number * KG_TO_LB, converted=True) if kind == "kg" else _pounds(number)


def _pounds(value: float, converted: bool = False) -> str:
    """Pounds as a string that never rounds down across the 250 lb threshold.

    A written number is kept exactly; a converted one is rounded up to one decimal,
    so 113.4 kg (250.004 lb) becomes "250.1", over the limit as it really is.
    """
    if value == int(value):
        return str(int(value))
    if not converted:
        return repr(value)
    tenths = math.ceil(value * 10 - 1e-9) / 10
    return str(int(tenths)) if tenths == int(tenths) else f"{tenths:.1f}"
