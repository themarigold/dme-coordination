"""The supplier thread: finding and qualifying a primary supplier and a warm backup.

One call at a time, in directory order, with her preferred supplier first. A supplier
qualifies only on all "yes" answers plus the rep's confirmation of our read-back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dme import clock
from dme.engine import Event
from dme.evidence import normalise, readback_position_ok
from dme.facts import supplier_fact as sf
from dme.models import EscalationReason as R
from dme.models import SupplierCallRead
from dme.rules import gate, holds, patient, signals

if TYPE_CHECKING:
    from dme.rules.runtime import Reading, Runtime

MAX_CALLBACKS = 2  # no-answer callbacks per supplier
MAX_CALL_BACK_LATER = 2  # "call back later" callbacks per supplier; asked again after these, it is skipped
CALLBACK_HOURS = 0.25  # redial a quarter of an hour later, as soon as the line is free
QUALIFY_TIMEOUT_HOURS = 1  # a call with no outcome at all counts as no answer
OUTAGE_RETRY_HOURS = 1  # Claude could not read the call: call again later
NEEDS_INFO_DAYS = 2
SHAREABLE = ("medicare_id", "demographics", "order_copy")
GOAL = (
    "Qualify a supplier for a standard manual wheelchair (K0001) for an Original Medicare patient: "
    "new Medicare patients, bills Medicare for K0001, in stock, delivers to her ZIP, accepts assignment "
    "for all 13 rental months, ABN, earliest delivery, rep name, read-back confirmed."
)
ANSWER_FIELDS = gate.SUPPLIER_MUST_SAY_YES + (
    "abn_required",
    "rep_name",
    "readback_confirmed",
    "collects_coinsurance_at_delivery",
    "needs_info_item",
)


def register(rt: Runtime) -> None:
    rt.route("reply", "supplier:", "qualify_call", on_reply)
    rt.route("no_answer", "supplier:", "qualify_call", on_no_answer)
    rt.route("voicemail", "supplier:", "qualify_call", on_no_answer)
    rt.timer("supplier_callback", on_callback_due)
    rt.timer("needs_info_expiry", on_needs_info_expiry)
    rt.timer("qualify_timeout", on_qualify_timeout)
    rt.timer("sourcing_opens", on_sourcing_opens)
    rt.timer("supplier_call_due", on_call_due)


def status(rt: Runtime, sid: int) -> str:
    return rt.case.get(sf(sid, "status"))


def set_status(rt: Runtime, sid: int, value: str, why: str = "") -> None:
    rt.case.set(sf(sid, "status"), value)
    rt.log("supplier_status", f"supplier {sid} -> {value}{': ' + why if why else ''}")


def with_status(rt: Runtime, *values: str) -> list[int]:
    return [s for s in rt.supplier_ids if status(rt, s) in values]


def directory_match(rt: Runtime, named: str) -> int | None:
    """The directory supplier a person named, if any (name match, ignoring case)."""
    wanted = normalise(named)
    if not wanted:
        return None
    for s in rt.directory:
        name = normalise(s["name"])
        if wanted in name or name in wanted:
            return s["id"]
    return None


def preferred_id(rt: Runtime) -> int | None:
    """The supplier to call first: the one the PCP office sent documents to, else her preferred one."""
    return directory_match(rt, rt.case.thread_state.get("pcp_sent_to", "")) or directory_match(
        rt, rt.case.get("patient.preferred_supplier")
    )


def may_disclose(case) -> bool:
    """Whether anything about her may go to a supplier: identity, consent to proceed and share, no contact hold."""
    return (
        case.get("patient.identity_verified") == "yes"
        and case.get("patient.consent_proceed") == "yes"
        and case.get("patient.consent_share_with_suppliers") == "yes"
        and not holds.held(case, "patient_contact")
    )


def start(rt: Runtime) -> None:
    """Begin sourcing, or resume it if parked, once she may be disclosed and her ZIP is known."""
    case = rt.case
    if (
        case.thread_state.get("sourcing") not in ("", None, "parked")
        or not case.get("patient.zip")
        or not may_disclose(case)
    ):
        return
    rt.case.thread_state["sourcing"] = "active"
    for sid in rt.supplier_ids:
        if not status(rt, sid):
            rt.case.set(sf(sid, "status"), "candidate")
    call_next(rt)


def resume(rt: Runtime) -> None:
    """Run sourcing again because a person asked to try the next supplier."""
    rt.case.thread_state["sourcing"] = "active"
    rt.log("sourcing_resumed", "sourcing resumed by the advocate")
    call_next(rt)


def _park(rt: Runtime) -> bool:
    """Park sourcing while she may not be disclosed, since a qualification call states her ZIP."""
    if may_disclose(rt.case):
        return False
    if rt.case.thread_state.get("sourcing") == "active":
        rt.case.thread_state["sourcing"] = "parked"
        rt.log("sourcing_parked", "no qualification call: her identity, consent or a contact hold stops disclosure")
    return True


def call_next(rt: Runtime) -> None:
    """Place the next qualification call, if sourcing still needs one."""
    state = rt.case.thread_state
    if state.get("sourcing") != "active" or state.get("supplier_call"):
        return
    if _park(rt):
        return
    if with_status(rt, "primary") and with_status(rt, "backup"):
        finish(rt)
        return
    candidates = with_status(rt, "candidate")
    due_callbacks = [s for s in candidates if state.get(f"supplier.{s}.callback_ready")]
    fresh = [s for s in candidates if not state.get(f"supplier.{s}.callback_pending")]
    preferred = preferred_id(rt)
    if preferred in fresh:
        fresh.remove(preferred)
        fresh.insert(0, preferred)
    ready = due_callbacks + fresh  # a callback that came due while the line was busy goes first
    if ready:
        if not clock.in_business_hours(rt.now):  # in order, once business hours begin
            if not state.get("sourcing_opens"):
                state["sourcing_opens"] = "yes"
                rt.after_hours("next qualification call", "sourcing_opens")
            return
        call(rt, ready[0])
    elif not any(state.get(f"supplier.{s}.callback_pending") for s in rt.supplier_ids) and not with_status(
        rt, "hold_needs_info"
    ):
        if with_status(rt, "primary"):
            finish(rt)  # no backup to be had; go on with the primary alone
        else:
            state["sourcing"] = "exhausted"
            rt.escalate(
                R.SUPPLIERS_EXHAUSTED,
                "no supplier in the directory qualified",
                [f"supplier {s}: {status(rt, s)}" for s in rt.supplier_ids],
            )


def on_sourcing_opens(rt: Runtime, event: Event) -> None:
    rt.case.thread_state.pop("sourcing_opens", None)
    call_next(rt)


def on_call_due(rt: Runtime, event: Event) -> None:
    """Place a call deferred to business hours, unless things changed while it waited."""
    sid, attempt = event.payload["supplier"], event.payload["attempt"]
    if rt.case.attempts.get(f"supplier:{sid}:qualify_call", 0) != attempt:
        return
    why = ""
    if status(rt, sid) != event.payload.get("status", status(rt, sid)):
        why = f"it is {status(rt, sid) or 'unknown'} now (was {event.payload['status']} when deferred)"
    elif event.payload.get("sourcing") == "active" and rt.case.thread_state.get("sourcing") != "active":
        why = "sourcing no longer needs it"
    if why:
        rt.case.thread_state.pop(f"supplier.{sid}.callback_pending", None)
        rt.log("call_dropped", f"deferred qualification call to supplier {sid} not placed: {why}")
        return
    busy = rt.case.thread_state.get("supplier_call")
    if busy and busy != str(sid):
        rt.in_business_hours(CALLBACK_HOURS, "supplier_call_due", event.payload)
        return
    call(rt, sid)


def call(rt: Runtime, sid: int) -> None:
    """Place a qualification call to one supplier.

    One deferred to business hours is owed as a callback, so sourcing does not call it fresh meanwhile.
    """
    if _park(rt):
        rt.case.thread_state.pop(f"supplier.{sid}.callback_pending", None)  # called fresh when sourcing resumes
        return
    attempt = rt.case.attempts.get(f"supplier:{sid}:qualify_call", 0)
    deferred = {
        "supplier": sid,
        "attempt": attempt,
        "status": status(rt, sid),  # re-checked when it is due
        "sourcing": rt.case.thread_state.get("sourcing", ""),
    }
    if rt.after_hours(f"qualification call to supplier {sid}", "supplier_call_due", deferred):
        rt.case.thread_state[f"supplier.{sid}.callback_pending"] = "yes"
        return
    entry = rt.supplier(sid)
    for name in ("name", "phone", "address"):
        rt.case.set(sf(sid, name), entry[name])
    state = rt.case.thread_state
    state["supplier_call"] = str(sid)
    state.pop(f"supplier.{sid}.callback_pending", None)
    state.pop(f"supplier.{sid}.callback_ready", None)
    set_status(rt, sid, "calling")
    rt.send(f"supplier:{sid}", "qualify_call")
    attempt = rt.case.attempts[f"supplier:{sid}:qualify_call"]
    rt.in_business_hours(QUALIFY_TIMEOUT_HOURS, "qualify_timeout", {"supplier": sid, "attempt": attempt})


def on_qualify_timeout(rt: Runtime, event: Event) -> None:
    """No reply, no voicemail, no no-answer: the call is treated as unanswered."""
    sid, attempt = event.payload["supplier"], event.payload["attempt"]
    if (
        rt.case.thread_state.get("supplier_call") == str(sid)
        and rt.case.attempts.get(f"supplier:{sid}:qualify_call") == attempt
    ):
        rt.log("qualify_timeout", f"supplier {sid}: no outcome within {QUALIFY_TIMEOUT_HOURS} business hour")
        _no_answer(rt, sid)


def finish(rt: Runtime) -> None:
    rt.case.thread_state["sourcing"] = "done"
    rt.log("sourcing_done", f"primary {with_status(rt, 'primary')}, backup {with_status(rt, 'backup')}")
    rt.evaluate()


def release(rt: Runtime, sid: int, why: str, new_status: str = "released") -> None:
    """Drop a supplier we spoke to, with an explicit release message, and forget any owed callback."""
    set_status(rt, sid, new_status, why)
    _end_call(rt, sid)
    for key in ("callback_pending", "callback_ready"):
        rt.case.thread_state.pop(f"supplier.{sid}.{key}", None)
    rt.send(f"supplier:{sid}", "release")


def skip(rt: Runtime, sid: int, why: str) -> None:
    """Not qualified: release it and move on."""
    release(rt, sid, why, new_status="skipped")


def _end_call(rt: Runtime, sid: int) -> None:
    if rt.case.thread_state.get("supplier_call") == str(sid):
        rt.case.thread_state["supplier_call"] = ""


def _sid(event: Event) -> int:
    return int(event.payload["counterparty"].split(":")[1])


def _not_on_the_line(rt: Runtime, sid: int, what: str) -> bool:
    """Ignore a call outcome from a supplier no longer being called, so it is not requalified."""
    if status(rt, sid) == "calling":
        return False
    rt.log("supplier_reply_ignored", f"supplier {sid} {what}, but it is {status(rt, sid) or 'unknown'} now: ignored")
    _end_call(rt, sid)
    call_next(rt)
    return True


def on_no_answer(rt: Runtime, event: Event) -> None:
    """No answer or voicemail: call back at most twice."""
    sid = _sid(event)
    if _not_on_the_line(rt, sid, f"{event.kind} on the qualification call"):
        return
    if event.kind == "voicemail":
        rt.log("voicemail", f"supplier {sid}: left the fixed callback message (no health information)")
    _no_answer(rt, sid)


def _no_answer(rt: Runtime, sid: int) -> None:
    _end_call(rt, sid)
    callbacks = int(rt.case.thread_state.get(f"supplier.{sid}.callbacks", "0"))
    if callbacks < MAX_CALLBACKS:
        rt.case.thread_state[f"supplier.{sid}.callbacks"] = str(callbacks + 1)
        rt.case.thread_state[f"supplier.{sid}.callback_pending"] = "yes"
        set_status(rt, sid, "candidate", f"no answer; callback {callbacks + 1} of {MAX_CALLBACKS}")
        rt.in_business_hours(CALLBACK_HOURS, "supplier_callback", {"supplier": sid})
    else:
        set_status(rt, sid, "skipped", "no answer after callbacks")
    call_next(rt)


def on_callback_due(rt: Runtime, event: Event) -> None:
    sid = event.payload["supplier"]
    state = rt.case.thread_state
    if state.get("sourcing") != "active" or status(rt, sid) != "candidate":
        state.pop(f"supplier.{sid}.callback_pending", None)
        rt.log("callback_skipped", f"supplier {sid}: sourcing no longer needs it")
        return
    if state.get("supplier_call"):
        state[f"supplier.{sid}.callback_ready"] = "yes"  # one call at a time: next when the line is free
        return
    call(rt, sid)


def on_reply(rt: Runtime, event: Event) -> None:
    sid = _sid(event)
    if status(rt, sid) != "calling":  # released meanwhile: only its safety signals still count
        safety = rt.read("check_safety_and_conditions", event, "Find safety signals and supplier conditions.")
        signals.raise_signals(rt, safety.raw if safety else None, f"supplier {sid}")
        _not_on_the_line(rt, sid, "answered the qualification call")
        return
    _end_call(rt, sid)
    primary = rt.read("read_supplier_call", event, GOAL, ("patient.zip",))
    safety = rt.read("check_safety_and_conditions", event, "Find safety signals and supplier conditions.")
    signals.raise_signals(rt, safety.raw if safety else None, f"supplier {sid}")  # even if the other read failed
    if primary is None or safety is None:
        key = f"supplier.{sid}.outage"
        if rt.case.thread_state.get(key) != "yes":  # call again once, later
            rt.case.thread_state[key] = "yes"
            rt.case.thread_state[f"supplier.{sid}.callback_pending"] = "yes"
            set_status(rt, sid, "candidate", "Claude could not read the call; calling again later")
            rt.in_business_hours(OUTAGE_RETRY_HOURS, "supplier_callback", {"supplier": sid})
        else:  # unreadable twice: skip it and move on
            skip(rt, sid, "Claude could not read the call twice")
        call_next(rt)
        return
    read: SupplierCallRead = primary.read
    outcome = read.call_outcome.value
    if outcome in ("wrong_number", "closed"):
        set_status(rt, sid, "dropped", outcome.replace("_", " "))
    elif outcome in ("no_answer", "voicemail"):
        on_no_answer(rt, event)
        return
    elif outcome == "call_back_later":
        asked = int(rt.case.thread_state.get(f"supplier.{sid}.called_back", "0"))
        if asked >= MAX_CALL_BACK_LATER:  # never an endless loop of callbacks
            skip(rt, sid, f"asked us to call back again after {asked} callbacks")
        else:
            rt.case.thread_state[f"supplier.{sid}.called_back"] = str(asked + 1)
            moment, _ = clock.resolve_date_phrase(read.call_back_after.value, read.call_back_after.commitment, rt.now)
            rt.case.thread_state[f"supplier.{sid}.callback_pending"] = "yes"
            set_status(
                rt,
                sid,
                "candidate",
                f"asked us to call back ({read.call_back_after.value or 'no time given'}); "
                f"callback {asked + 1} of {MAX_CALL_BACK_LATER}",
            )
            due = clock.next_business_moment(moment or clock.add_business_hours(rt.now, CALLBACK_HOURS))
            rt.schedule(due, "supplier_callback", {"supplier": sid})  # a call back only when it can be answered
    else:
        assess(rt, sid, primary, safety)
    call_next(rt)


# Supplier conditions that only tighten, as (primary reader field, safety reader field).
TIGHTENING_CONDITIONS = {
    "abn": ("abn_required", "abn_requested"),
    "full_price_or_no_assignment": ("full_price_or_no_assignment", "full_price_or_no_assignment"),
    "needs_info": ("needs_info", "needs_info"),
    "other_condition": ("other_condition", "other_condition"),
}


def states_condition(field) -> bool:
    """A condition field that says anything other than "no". A doubtful read counts (fail safe)."""
    return field.status in signals.DOUBT or (field.status == "found" and field.value not in ("", "no"))


def flagged_conditions(primary: Reading, safety: Reading) -> list[str]:
    """Supplier conditions stated in a turn flagged for prompt injection.

    Facts from a flagged turn are dropped, but a condition only tightens, so it must
    never be lost silently: the caller never qualifies a supplier on that call.
    """
    flagged = primary.flagged | safety.flagged
    if not flagged:
        return []
    stated = []
    for name, fields in TIGHTENING_CONDITIONS.items():
        for reading, field_name in zip((primary, safety), fields):
            field = getattr(reading.raw, field_name)
            if field.evidence_turn_id in flagged and states_condition(field):
                stated.append(name)
                break
    return stated


def escalate_flagged_conditions(rt: Runtime, sid: int, flagged: list[str], where: str = "") -> None:
    """Escalate conditions from a flagged turn as an unflagged call would, without quoting the turn."""
    if "abn" in flagged:
        rt.escalate(
            R.ABN_REQUESTED,
            f"supplier {sid} would ask Eleanor to sign an ABN{where} (said in a turn flagged for prompt injection)",
        )
    unknown = [c for c in flagged if c in ("needs_info", "other_condition")]
    if unknown:
        rt.escalate(
            R.SUPPLIER_CONDITION_UNKNOWN,
            f"supplier {sid} states {', '.join(unknown)}{where} in a turn flagged for prompt injection (not quoted)",
        )


def assess(rt: Runtime, sid: int, primary: Reading, safety_reading: Reading) -> None:
    """Decide what an answered qualification call means."""
    read: SupplierCallRead = primary.read
    safety = safety_reading.read
    flagged = flagged_conditions(primary, safety_reading)
    abn = gate.found(read.abn_required) or gate.found(safety.abn_requested)
    for name in ANSWER_FIELDS:  # only a found answer counts; a hedged "yes" is no answer
        field = getattr(read, name)
        rt.case.set(sf(sid, name), field.value if field.status == "found" else "")
    rt.case.set(sf(sid, "call_outcome"), "answered")
    readback_ok = readback_position_ok(read, primary.turns)
    rt.case.set(sf(sid, "readback_confirmed"), "yes" if readback_ok else "no")
    when, _ = clock.resolve_date_phrase(
        read.earliest_delivery.value, read.earliest_delivery.commitment, rt.now, purpose="delivery"
    )
    rt.case.set(sf(sid, "earliest_delivery"), when.date().isoformat() if when else "")

    # Disqualify first: a clear "no" on a must-have ends it before any condition comparison
    # can trigger a re-ask. An ABN request always escalates, so it is raised before returning.
    answers = {f: rt.case.get(sf(sid, f)) for f in gate.SUPPLIER_MUST_SAY_YES}
    if "no" in answers.values():
        skip(rt, sid, "said no to " + ", ".join(f for f, v in answers.items() if v == "no"))
        if abn:
            rt.escalate(R.ABN_REQUESTED, f"supplier {sid} would ask Eleanor to sign an ABN")
        elif "abn" in flagged:
            escalate_flagged_conditions(rt, sid, ["abn"])
        return
    if flagged:  # conditions only tighten: one stated in a flagged turn still disqualifies
        skip(rt, sid, "conditions stated in a turn flagged for prompt injection: " + ", ".join(flagged))
        if abn:
            rt.escalate(R.ABN_REQUESTED, f"supplier {sid} would ask Eleanor to sign an ABN")
        escalate_flagged_conditions(rt, sid, [c for c in flagged if not (c == "abn" and abn)])
        return
    disagreements = signals.condition_disagreements(read, safety, primary.downgraded, safety_reading.downgraded)
    if disagreements:
        if rt.case.thread_state.get(f"supplier.{sid}.reasked") != "yes":
            rt.case.thread_state[f"supplier.{sid}.reasked"] = "yes"
            set_status(rt, sid, "candidate", f"readers disagree on {', '.join(disagreements)}; asking again")
            call(rt, sid)
        else:
            skip(rt, sid, "conditions still unclear")
            rt.escalate(
                R.SUPPLIER_CONDITION_DISAGREEMENT,
                f"supplier {sid}: readers disagree on {', '.join(disagreements)} after a re-ask",
            )
        return
    if abn:
        skip(rt, sid, "wants an ABN")
        rt.escalate(R.ABN_REQUESTED, f"supplier {sid} would ask Eleanor to sign an ABN")
        return
    if gate.found(read.full_price_or_no_assignment):
        skip(rt, sid, "wants full price up front or will not take assignment")
        return
    condition = read.other_condition
    if condition.status == "found" and condition.value:
        skip(rt, sid, "condition of unknown kind")
        rt.escalate(R.SUPPLIER_CONDITION_UNKNOWN, f'supplier {sid} states a condition: "{condition.value}"')
        return
    if not gate.supplier_qualified(rt.case, sid):
        missing = [f for f, v in answers.items() if v != "yes"] + ([] if readback_ok else ["read-back"])
        skip(rt, sid, "unconfirmed: " + ", ".join(missing))
        return
    if gate.found(read.needs_info):
        item = read.needs_info_item.value if read.needs_info_item.status == "found" else ""
        if item not in SHAREABLE:
            skip(rt, sid, "needs information of unknown kind")
            rt.escalate(R.SUPPLIER_CONDITION_UNKNOWN, f"supplier {sid} needs information we do not know how to share")
            return
        share_or_hold(rt, sid, item)
        return
    qualify(rt, sid)


def can_share(rt: Runtime, item: str) -> bool:
    """Whether `item` may be shared: minimum necessary, and only once she may be disclosed."""
    case = rt.case
    if not may_disclose(case):
        return False
    if item == "medicare_id":
        return bool(case.get("patient.medicare_id")) and case.get("patient.medicare_id_ok_to_share") == "yes"
    if item == "demographics":
        return bool(case.get("patient.address"))
    if item == "order_copy":  # only an order with no gaps
        return all(
            case.get(f) == "yes" for f in ("order.elements_complete", "order.item_matches_k0001", "order.changes_ok")
        )
    return False


def share_or_hold(rt: Runtime, sid: int, item: str) -> None:
    if can_share(rt, item):
        rt.send(f"supplier:{sid}", "share_info", item=item)
        rt.log("info_shared", f"supplier {sid}: {item} shared (consented, minimum necessary)")
        qualify(rt, sid)
        return
    set_status(rt, sid, "hold_needs_info", f"needs {item}")
    rt.case.set(sf(sid, "needs_info_item"), item)
    patient.ask(rt, f"needs_{item}")
    rt.in_business_days(NEEDS_INFO_DAYS, "needs_info_expiry", {"supplier": sid})


def retry_holds(rt: Runtime) -> None:
    """A supplier waiting for information qualifies as soon as it can be shared."""
    for sid in with_status(rt, "hold_needs_info"):
        item = rt.case.get(sf(sid, "needs_info_item"))
        if can_share(rt, item):
            share_or_hold(rt, sid, item)


def on_needs_info_expiry(rt: Runtime, event: Event) -> None:
    sid = event.payload["supplier"]
    if status(rt, sid) == "hold_needs_info":
        skip(rt, sid, "information not available within two business days")
        call_next(rt)


def qualify(rt: Runtime, sid: int) -> None:
    """Qualified: becomes primary, else warm backup, else a spare."""
    if not with_status(rt, "primary"):
        set_status(rt, sid, "primary")
    elif not with_status(rt, "backup"):
        set_status(rt, sid, "backup")
    else:
        set_status(rt, sid, "qualified")
    if rt.case.get(sf(sid, "collects_coinsurance_at_delivery")) == "yes":
        rt.log(
            "supplier_terms",
            f"supplier {sid} will bill Eleanor's 20% and deductible at or after delivery "
            "(normal for an assigning supplier; she is told in the cost message)",
        )
    rt.evaluate()
    call_next(rt)
