"""The handoff thread: from a passed packet gate to a confirmed delivery.

Re-confirm the supplier, get Eleanor's written permission, send the packet behind the
approval switch, then acceptance, scheduling and delivery. Before any packet a lost
supplier fails over to the warm backup automatically; after one, a human decides.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from dme import clock
from dme.engine import Event
from dme.facts import as_int
from dme.facts import supplier_fact as sf
from dme.models import EscalationReason as R
from dme.models import SupplierCallRead
from dme.rules import gate, holds, patient, pcp, signals, suppliers

if TYPE_CHECKING:
    from dme.rules.runtime import Reading, Runtime

RECONFIRM_CALLBACK_HOURS = 2  # the one callback lands inside the 4-business-hour budget
RECONFIRM_TIMEOUT_HOURS = 1  # a re-confirm call with no outcome at all counts as no answer
OUTAGE_RETRY_HOURS = 1
PERMISSION_DAYS = 2
STATUS_CALL_HOURS = 4  # after a budget's status call, this long before a human is asked
FOLLOWUP_DAYS = 2  # fit, training or repair-number problems: this long for the supplier to sort them out
POST_PACKET = ("packet_sent", "accepted", "scheduled", "delivered_by_supplier", "followup")
DOCS_STEPS = (*POST_PACKET, "start")  # steps broken documents park; before the packet the send check catches them
STEP = {"packet_sent": "4", "accepted": "5", "scheduled": "6", "delivered_by_supplier": "6", "followup": "6"}
CONTACT_STEPS = ("permission", "delivered_by_supplier")  # steps that message her, so contact holds stop them
PACKET_FACTS = (
    "patient.name",
    "patient.address",
    "patient.availability",
    "patient.home_layout",
    "order.version",
    "note.version",
)
RECONFIRMED = ("k0001_in_stock", "delivers_to_zip", "accepts_assignment_all_months")
RECONFIRM_GOAL = "Re-confirm before handoff: K0001 in stock, delivers to her ZIP, accepts assignment, earliest date."
BUDGET_OF = {"packet_sent": "accept", "accepted": "schedule", "scheduled": "delivery", "followup": "followup"}
# Items that hand the handoff to a person: while it waits on one (`stuck`), one of them stays open.
OWNER_REASONS = {R.SECOND_HANDOFF, R.NO_HOME_ASSESSMENT, R.SAME_OR_SIMILAR, R.STALE_NEWS, R.HANDOFF_SILENCE}


def register(rt: Runtime) -> None:
    rt.route("reply", "supplier:", "reconfirm_call", on_reconfirm_reply)
    rt.route("no_answer", "supplier:", "reconfirm_call", on_reconfirm_no_answer)
    rt.route("voicemail", "supplier:", "reconfirm_call", on_reconfirm_no_answer)
    rt.route("reply", "supplier:", "status_call", on_status_reply)
    rt.route("no_answer", "supplier:", "status_call", on_status_silent)
    rt.route("accept", "supplier:", "*", on_accept)
    rt.route("reject", "supplier:", "*", on_reject)
    rt.route("scheduled", "supplier:", "*", on_scheduled)
    rt.route("delivered", "supplier:", "*", on_delivered)
    rt.timer("reconfirm_callback", on_reconfirm_callback)
    rt.timer("reconfirm_timeout", on_reconfirm_timeout)
    rt.timer("permission_budget", on_permission_budget)
    rt.timer("handoff_budget", on_budget)
    rt.timer("status_call_due", on_status_call_due)


def current(rt: Runtime) -> int | None:
    return as_int(rt.case.get("handoff.supplier"), None)


def _state(rt: Runtime, state: str, step: str = "") -> None:
    rt.case.thread_state["handoff"] = state
    if step:
        rt.case.set("handoff.step", step)
    rt.log("handoff", f"step {rt.case.get('handoff.step')}: {state} (supplier {rt.case.get('handoff.supplier')})")


def docs_broken(rt: Runtime) -> list[str]:
    """What the current order and visit note no longer pass, e.g. after a replacement arrived."""
    case = rt.case
    gaps = gate.document_gaps(case)
    if case.get("note.criteria_accepted") != "yes" or case.thread_state.get("criteria_accepted_version") != case.get(
        "note.version"
    ):
        gaps.append("criteria reading of the current visit note accepted")
    return gaps


def blocked(rt: Runtime, step: str) -> bool:
    """Whether a hold or broken documents stop `step`."""
    return (
        holds.held(rt.case, "handoff")
        or (step in CONTACT_STEPS and holds.held(rt.case, "patient_contact"))
        or (step in DOCS_STEPS and bool(docs_broken(rt)))
    )


def _held(rt: Runtime, resume_as: str) -> bool:
    if blocked(rt, resume_as):
        _state(rt, f"held_{resume_as}")
        _docs_item(rt, resume_as)
        return True
    return False


def _docs_item(rt: Runtime, step: str) -> None:
    """Raise one docs_still_missing item if broken documents park `step`."""
    broken = docs_broken(rt) if step in DOCS_STEPS else []
    if not broken or any(i.reason == R.DOCS_STILL_MISSING for i in rt.inbox.open_items()):
        return
    where = f"supplier {current(rt)} has the packet" if step in POST_PACKET else f"handoff to supplier {current(rt)}"
    rt.escalate(R.DOCS_STILL_MISSING, f"{where} but the documents no longer pass: {', '.join(broken)}; handoff paused")


def stuck(rt: Runtime) -> bool:
    """Whether the handoff is waiting on a person: they have it, or nothing is due after the packet."""
    state = rt.case.thread_state.get("handoff", "")
    if state == "with_human":
        return True
    if state not in POST_PACKET:
        return False
    gen = int(rt.case.thread_state.get("handoff_budget_gen", "0"))
    for event in rt.engine.pending():
        if (
            (event.kind == "handoff_budget" and event.payload.get("gen") == gen)
            or (event.kind == "status_call_due" and event.payload.get("supplier") == current(rt))
            or event.payload.get("counterparty") == f"supplier:{current(rt)}"
        ):
            return False
    return True


def status_call(rt: Runtime, sid: int) -> None:
    """A person asked to call the handoff supplier again after the packet: one status call, fresh budget.

    If the person has the handoff and the supplier has since accepted with every check met,
    it means going on with that supplier instead, and no call is made.
    """
    state = rt.case.thread_state.get("handoff", "")
    if state == "with_human" and not resume_blockers(rt, sid):
        rt.log(
            "accepted_by_person",
            f"the advocate went on with supplier {sid}, which has since accepted with every acceptance check met",
            actor="human",
        )
        _accepted(rt, sid)
        return
    deferred = {"supplier": sid, "status": rt.case.get(sf(sid, "status"))}
    if rt.after_hours(f"status call to supplier {sid}", "status_call_due", deferred):
        return
    rt.send(f"supplier:{sid}", "status_call", stage=state)
    if state in BUDGET_OF:
        _start_budget(rt, BUDGET_OF[state], hours=STATUS_CALL_HOURS)


def on_status_call_due(rt: Runtime, event: Event) -> None:
    """Place a deferred status call if the supplier is still current and unchanged."""
    sid = event.payload["supplier"]
    now = rt.case.get(sf(sid, "status"))
    if sid != current(rt) or now != event.payload.get("status", now):
        rt.log(
            "call_dropped",
            f"deferred status call to supplier {sid} not placed: it is {now or 'unknown'} now, "
            f"handoff supplier {current(rt)}",
        )
        return
    status_call(rt, sid)


def advance(rt: Runtime) -> None:
    """Start the handoff once the packet gate passes, pause it when a hold lands, and resume it when the hold lifts."""
    state = rt.case.thread_state.get("handoff", "")
    if state in POST_PACKET and blocked(rt, state):
        _move(rt, state)  # pauses it
        return
    if state == "permission" and blocked(rt, state):
        _state(rt, "held_permission")  # asked again in full when the hold lifts
        return
    if state == "reconfirm" and blocked(rt, state):
        _state(rt, "held_reconfirm")  # a fresh re-confirm call when the hold lifts
        return
    if state.startswith("held_"):
        resume_as = state.removeprefix("held_")
        if blocked(rt, resume_as):
            _docs_item(rt, resume_as)
            return
        if resume_as in POST_PACKET:
            if rt.case.thread_state.pop("handoff_paused", None):
                rt.send(f"supplier:{current(rt)}", "resume")
            _move(rt, resume_as)
        else:
            {
                "start": lambda: start(rt, current(rt) or 0),
                "reconfirm": lambda: _resume_reconfirm(rt),
                "permission": lambda: ask_permission(rt),
                "send": lambda: request_send(rt),
                "sending": lambda: send_packet(rt),
            }[resume_as]()
        return
    if state == "blocked_send":  # an approved packet failed its send-time check: re-approve once it passes
        sid = current(rt)
        if sid is not None and not send_unmet(rt, sid) and not holds.held(rt.case, "handoff"):
            request_send(rt)
        elif sid is not None:
            _blocked_item(rt, sid, send_unmet(rt, sid))  # in case its item was closed with the checks still failing
        return
    # After "no_backup", a supplier that qualifies later restarts the handoff.
    if (state and state != "no_backup") or rt.case.thread_state.get("sourcing") == "active":
        return
    primary = suppliers.with_status(rt, "primary")
    if not primary:  # the primary was lost before the handoff started: the backup takes over
        spare = suppliers.with_status(rt, "backup") or suppliers.with_status(rt, "qualified")
        if spare:
            suppliers.set_status(rt, spare[0], "primary", "no primary before the handoff started")
            primary = spare[:1]
    if primary and not gate.packet_gate(rt.case, primary[0]):
        start(rt, primary[0])


def _move(rt: Runtime, stage: str) -> None:
    """Enter a post-packet stage and start its budget.

    Under a handoff hold the stage is only recorded and the supplier is told to pause.
    """
    broken = docs_broken(rt)
    if holds.held(rt.case, "handoff") or broken:
        if not rt.case.thread_state.get("handoff", "").startswith("held_") and not rt.case.thread_state.get(
            "handoff_paused"
        ):
            rt.send(f"supplier:{current(rt)}", "pause")
            rt.case.thread_state["handoff_paused"] = "yes"
        _state(rt, f"held_{stage}")
        _docs_item(rt, stage)
        return
    if blocked(rt, stage):  # only patient contact is held: the supplier carries on, she is asked later
        rt.log("handoff_contact_held", f"{stage}: patient contact is on hold; she is asked once it lifts")
        _state(rt, f"held_{stage}")
        return
    _state(rt, stage, STEP[stage])
    sid = current(rt)
    if stage == "packet_sent":
        _start_budget(rt, "accept", hours=4)
    elif stage == "accepted":
        _start_budget(rt, "schedule", days=2)
    elif stage == "scheduled":
        # A scheduled date we cannot act on counts from now.
        day = clock.iso_date(rt.case.get(sf(sid, "scheduled_date")), rt.now)
        agreed = clock.at(day, clock.BUSINESS_END) if day else rt.now
        _start_budget(rt, "delivery", due=clock.add_business_days(agreed, 1))
    elif stage == "followup":
        _start_budget(rt, "followup", days=FOLLOWUP_DAYS)  # a fresh budget after a hold
    elif rt.case.get("patient.delivery_received"):
        on_delivery_check(rt)  # she already answered while the handoff was held
    else:
        patient.message(rt, "delivery_check", {})
        _start_budget(rt, "patient_confirm", days=1)


def start(rt: Runtime, sid: int) -> None:
    """Step 1: tell the PCP office which supplier was chosen and re-confirm it by call."""
    rt.case.set("handoff.supplier", str(sid))
    if _held(rt, "start"):
        return
    _state(rt, "reconfirm", "1")
    rt.case.thread_state.pop("reconfirm_callback", None)
    rt.case.thread_state.pop(f"supplier.{sid}.reconfirm_outage", None)  # a fresh re-confirm gets its own retry
    pcp.notify_supplier_chosen(rt, sid)
    _reconfirm_call(rt, sid)


def _resume_reconfirm(rt: Runtime) -> None:
    """Place a fresh re-confirm call after the hold that parked it lifts."""
    sid = current(rt)
    if sid is None:
        return
    _state(rt, "reconfirm", "1")
    rt.case.thread_state.pop("reconfirm_callback", None)
    rt.case.thread_state.pop(f"supplier.{sid}.reconfirm_outage", None)
    _reconfirm_call(rt, sid)


def _reconfirm_call(rt: Runtime, sid: int) -> None:
    """Place one re-confirm call, with a timeout in case no outcome comes back."""
    if rt.after_hours(f"re-confirm call to supplier {sid}", "reconfirm_callback", {"supplier": sid}):
        return
    rt.send(f"supplier:{sid}", "reconfirm_call")
    attempt = rt.case.attempts.get(f"supplier:{sid}:reconfirm_call", 0)
    rt.in_business_hours(RECONFIRM_TIMEOUT_HOURS, "reconfirm_timeout", {"supplier": sid, "attempt": attempt})


def on_reconfirm_timeout(rt: Runtime, event: Event) -> None:
    sid, attempt = event.payload["supplier"], event.payload["attempt"]
    if (
        sid != current(rt)
        or rt.case.thread_state.get("handoff") != "reconfirm"
        or rt.case.attempts.get(f"supplier:{sid}:reconfirm_call") != attempt
        or rt.case.thread_state.get("reconfirm_answered") == f"{sid}#{attempt}"
    ):
        return
    rt.log("reconfirm_timeout", f"supplier {sid}: no outcome within {RECONFIRM_TIMEOUT_HOURS} business hour")
    _reconfirm_unanswered(rt, sid)


def _sid(event: Event) -> int:
    return int(event.payload["counterparty"].split(":")[1])


def on_reconfirm_reply(rt: Runtime, event: Event) -> None:
    sid = _sid(event)
    if sid != current(rt):
        return
    if rt.case.thread_state.get("handoff") == "held_reconfirm":  # parked: only its safety signals count
        safety = rt.read(
            "check_safety_and_conditions", event, "Find safety signals and supplier conditions.", escalate=False
        )
        signals.raise_signals(rt, safety.raw if safety else None, f"supplier {sid}")
        rt.log(
            "reconfirm_parked",
            f"supplier {sid} answered the re-confirm call while the step is held: "
            "re-confirmed afresh when the hold lifts",
        )
        return
    rt.case.thread_state["reconfirm_answered"] = f"{sid}#{event.attempt}"  # attempts count per supplier
    reading = rt.read("read_supplier_call", event, RECONFIRM_GOAL, ("patient.zip",), escalate=False)
    safety = rt.read(
        "check_safety_and_conditions", event, "Find safety signals and supplier conditions.", escalate=False
    )
    signals.raise_signals(rt, safety.raw if safety else None, f"supplier {sid}")
    if reading is None or safety is None:  # a failed read never lets the handoff advance
        _reconfirm_unreadable(rt, sid)
        return
    read: SupplierCallRead = reading.read
    flagged = suppliers.flagged_conditions(reading, safety)
    if not flagged:  # what it says now replaces what it said at qualification
        for name in RECONFIRMED:
            field = getattr(read, name)
            if field.status == "found" and field.value in ("yes", "no"):
                rt.case.set(sf(sid, name), field.value)
    if gate.found(read.abn_required) or gate.found(safety.read.abn_requested):  # an ABN always escalates
        rt.case.set(sf(sid, "abn_required"), "yes")
        rt.escalate(R.ABN_REQUESTED, f"supplier {sid} would ask Eleanor to sign an ABN (said at re-confirm)")
        failover(rt, "wants an ABN (said at re-confirm)")
        return
    if (
        read.call_outcome.status == "found"
        and read.call_outcome.value in ("call_back_later", "voicemail", "no_answer")
        and not flagged
    ):
        _reconfirm_unanswered(rt, sid)
        return
    if not conditions_still_clear(rt, sid, read, safety.read, flagged, reading.downgraded, safety.downgraded):
        return
    still_yes = all(gate.found(getattr(read, f)) for f in RECONFIRMED)
    when, _ = clock.resolve_date_phrase(
        read.earliest_delivery.value, read.earliest_delivery.commitment, rt.now, purpose="delivery"
    )
    if when:
        rt.case.set(sf(sid, "earliest_delivery"), when.date().isoformat())
    if still_yes:
        ask_permission(rt)
    else:
        failover(rt, "could not re-confirm stock, ZIP or assignment")


def conditions_still_clear(
    rt: Runtime,
    sid: int,
    read: SupplierCallRead,
    safety,
    flagged: list[str] = (),
    primary_downgraded=frozenset(),
    safety_downgraded=frozenset(),
) -> bool:
    """Apply the qualification condition checks again at re-confirm. False when the handoff stops here."""
    if flagged:
        suppliers.escalate_flagged_conditions(rt, sid, list(flagged), " at re-confirm")
        failover(rt, "conditions stated in a turn flagged for prompt injection at re-confirm: " + ", ".join(flagged))
        return False
    disagreements = signals.condition_disagreements(read, safety, primary_downgraded, safety_downgraded)
    if disagreements:
        key = f"supplier.{sid}.reconfirm_reasked"
        if rt.case.thread_state.get(key) != "yes":
            rt.case.thread_state[key] = "yes"
            rt.log("reconfirm_reask", f"supplier {sid}: readers disagree on {', '.join(disagreements)}; asking again")
            _reconfirm_call(rt, sid)
        else:
            rt.escalate(
                R.SUPPLIER_CONDITION_DISAGREEMENT,
                f"supplier {sid}: readers disagree on {', '.join(disagreements)} at re-confirm after a re-ask",
            )
            failover(rt, "conditions still unclear at re-confirm")
        return False
    if gate.found(read.full_price_or_no_assignment):
        failover(rt, "wants full price up front or will not take assignment (said at re-confirm)")
        return False
    condition = read.other_condition
    if condition.status == "found" and condition.value:
        rt.escalate(
            R.SUPPLIER_CONDITION_UNKNOWN, f'supplier {sid} states a condition at re-confirm: "{condition.value}"'
        )
        failover(rt, "condition of unknown kind at re-confirm")
        return False
    if gate.found(read.needs_info):
        item = read.needs_info_item.value if read.needs_info_item.status == "found" else ""
        if item in suppliers.SHAREABLE and suppliers.can_share(rt, item):
            rt.send(f"supplier:{sid}", "share_info", item=item)
            rt.log("info_shared", f"supplier {sid}: {item} shared at re-confirm (consented, minimum necessary)")
            return True
        if item not in suppliers.SHAREABLE:
            rt.escalate(
                R.SUPPLIER_CONDITION_UNKNOWN,
                f"supplier {sid} needs information we do not know how to share (at re-confirm)",
            )
        failover(rt, f"needs information we cannot share ({item or 'unknown'}) at re-confirm")
        return False
    return True


def _reconfirm_unreadable(rt: Runtime, sid: int) -> None:
    """Call again once if the re-confirm call could not be read; a second failure fails over."""
    key = f"supplier.{sid}.reconfirm_outage"
    if rt.case.thread_state.get(key) != "yes":
        rt.case.thread_state[key] = "yes"
        rt.log(
            "reconfirm_unreadable",
            f"supplier {sid}: Claude could not read the re-confirm call; calling again "
            f"in {OUTAGE_RETRY_HOURS} business hour",
        )
        rt.in_business_hours(OUTAGE_RETRY_HOURS, "reconfirm_callback", {"supplier": sid})
        return
    rt.escalate(
        R.LLM_UNAVAILABLE,
        f"supplier {sid}'s re-confirm call could not be read twice",
        ["read once", f"called again {OUTAGE_RETRY_HOURS} business hour later and read again"],
    )
    failover(rt, "re-confirm call could not be read twice")


def on_reconfirm_no_answer(rt: Runtime, event: Event) -> None:
    sid = _sid(event)
    if sid != current(rt) or rt.case.thread_state.get("handoff") == "held_reconfirm":
        return  # parked: a fresh re-confirm when the hold lifts
    rt.case.thread_state["reconfirm_answered"] = f"{sid}#{event.attempt}"  # attempts count per supplier
    _reconfirm_unanswered(rt, sid)


def _reconfirm_unanswered(rt: Runtime, sid: int) -> None:
    """No answer, voicemail or "call back later": one callback, then fail over."""
    if rt.case.thread_state.get("reconfirm_callback") != "done":
        rt.case.thread_state["reconfirm_callback"] = "done"
        rt.in_business_hours(RECONFIRM_CALLBACK_HOURS, "reconfirm_callback", {"supplier": sid})
    else:
        failover(rt, "silent on the re-confirm call and the callback")


def on_reconfirm_callback(rt: Runtime, event: Event) -> None:
    sid = event.payload["supplier"]
    if sid == current(rt) and rt.case.thread_state.get("handoff") == "reconfirm":
        _reconfirm_call(rt, sid)


def failover(rt: Runtime, why: str) -> None:
    """Switch to the warm backup before any packet; after a packet, a human decides."""
    sid = current(rt)
    if as_int(rt.case.get("handoff.packets_sent")) > 0:
        _state(rt, "with_human")
        rt.escalate(
            R.SECOND_HANDOFF, f"supplier {sid}: {why} after the packet was sent", [f"packet sent to supplier {sid}"]
        )
        return
    suppliers.release(rt, sid, why)
    backups = suppliers.with_status(rt, "backup", "qualified")
    if not backups:
        _state(rt, "no_backup")
        rt.escalate(R.SUPPLIERS_EXHAUSTED, f"supplier {sid}: {why}; no warm backup left")
        return
    suppliers.set_status(rt, backups[0], "primary", f"automatic failover from supplier {sid}")
    start(rt, backups[0])


def ask_permission(rt: Runtime) -> None:
    """Step 2: who will call her, and her written permission for that supplier."""
    sid = current(rt)
    if _held(rt, "permission"):
        return
    _state(rt, "permission", "2")
    who = {sf(sid, f): rt.case.get(sf(sid, f)) for f in ("name", "phone", "address")}
    patient.message(rt, "supplier_will_call", who)
    patient.contact(rt, "esign_supplier_permission", supplier=sid)
    generation = int(rt.case.thread_state.get("permission_gen", "0")) + 1  # older budgets lapse
    rt.case.thread_state["permission_gen"] = str(generation)
    rt.in_business_days(PERMISSION_DAYS, "permission_budget", {"supplier": sid, "gen": generation})


def on_permission(rt: Runtime, signed_for: int | None) -> None:
    """She signed the permission form for `signed_for`; it counts only for the current supplier."""
    sid = current(rt)
    if sid is None or rt.case.thread_state.get("handoff") not in ("permission", "held_permission"):
        return
    if signed_for != sid:
        rt.log(
            "permission_not_current",
            f"e-signed permission was for supplier {signed_for}, not the current supplier {sid}: not counted",
        )
        return
    rt.case.set(sf(sid, "call_permission"), "yes")
    request_send(rt)


def on_permission_budget(rt: Runtime, event: Event) -> None:
    sid = event.payload["supplier"]
    if (
        event.payload.get("gen") != int(rt.case.thread_state.get("permission_gen", "0"))
        or sid != current(rt)
        or rt.case.get(sf(sid, "call_permission")) == "yes"
    ):
        return
    if rt.case.thread_state.get("handoff") != "permission" or blocked(rt, "permission"):
        advance(rt)  # parked under a hold: asked again in full when it lifts
        return
    patient.start_ladder(rt, "esign_supplier_permission")


def request_send(rt: Runtime) -> None:
    """Step 3, behind the approval switch."""
    sid = current(rt)
    if _held(rt, "send"):
        return
    _state(rt, "awaiting_approval", "3")
    approved = _binding(rt)  # the approval covers this supplier and these document versions only
    rt.request_approval(
        "supplier_handoff",
        f"order packet for supplier {sid} is ready",
        [f"supplier {sid} re-confirmed", "written permission signed"],
        lambda rt: send_packet(rt, approved),
    )


def _binding(rt: Runtime) -> str:
    """What a packet approval is for: the supplier and the order and note versions."""
    return f"{current(rt)}|{rt.case.get('order.version')}|{rt.case.get('note.version')}"


def send_unmet(rt: Runtime, sid: int) -> list[str]:
    """Send-time checks still unmet: the packet gate, her permission, and approval for a second handoff."""
    unmet = gate.packet_gate(rt.case, sid)
    if rt.case.get(sf(sid, "call_permission")) != "yes":
        unmet.append("written permission")
    packets = as_int(rt.case.get("handoff.packets_sent"))
    if packets > 0 and rt.case.thread_state.get("second_handoff_approved") != str(sid):
        unmet.append("a human approval for a second handoff")
    return unmet


IDENTITY_CHECKS = ("patient.identity_verified", "patient.consent_proceed", "patient.consent_share_with_suppliers")


def _blocked_item(rt: Runtime, sid: int, unmet: list[str]) -> None:
    """Send an approved but blocked packet to a person, under the reason its failed checks point to."""
    if not unmet:
        return
    identity = [u for u in unmet if u in IDENTITY_CHECKS]
    docs = [u for u in unmet if u in docs_broken(rt)]
    reason = R.INTAKE_INCOMPLETE if identity else R.DOCS_STILL_MISSING if docs else R.CASE_STALLED
    if any(i.reason == reason for i in rt.inbox.open_items()):
        return
    rt.escalate(reason, f"approved packet for supplier {sid} not sent: {', '.join(unmet)}")


def packet_contents(rt: Runtime) -> dict[str, str]:
    """Minimum necessary: the Medicare ID only if she said it may be shared."""
    contents = {f: rt.case.get(f) for f in PACKET_FACTS}
    if rt.case.get("patient.medicare_id_ok_to_share") == "yes":
        contents["patient.medicare_id"] = rt.case.get("patient.medicare_id")
    return contents


def send_packet(rt: Runtime, approved: str | None = None) -> None:
    """Send the approved packet if every check still holds at this moment.

    An approval binds one supplier and one set of document versions; if either has
    changed, nothing is sent and a fresh approval is asked for instead.
    """
    sid = current(rt)
    state = rt.case.thread_state
    if approved is not None:
        state["packet_approved"] = approved
    approved = state.get("packet_approved") or _binding(rt)  # a direct call is approved as things are now
    if approved != _binding(rt):
        rt.log(
            "packet_approval_stale",
            f"approval for {approved!r} (supplier|order|note) does not match {_binding(rt)!r}: nothing sent",
        )
        state.pop("packet_approved", None)
        if approved.split("|")[0] == str(sid) and state.get("handoff") in ("awaiting_approval", "held_sending"):
            request_send(rt)  # same supplier, newer documents
        return
    if _held(rt, "sending"):
        return
    unmet = send_unmet(rt, sid)
    if unmet or rt.case.frozen:
        rt.log("packet_blocked", f"packet to supplier {sid} not sent: {', '.join(unmet) or 'case frozen'}")
        state.pop("packet_approved", None)
        if state.get("handoff") in ("awaiting_approval", "held_sending"):
            _state(rt, "blocked_send")  # advance asks a fresh approval once the checks pass
            _blocked_item(rt, sid, unmet)
        return
    state.pop("packet_approved", None)
    packets = as_int(rt.case.get("handoff.packets_sent"))
    contents = packet_contents(rt)
    rt.send(f"supplier:{sid}", "send_packet", contents=contents)
    rt.case.set(sf(sid, "packet_sent"), "yes")
    rt.case.set("handoff.packets_sent", str(packets + 1))
    _move(rt, "packet_sent")


def in_progress(rt: Runtime) -> bool:
    """Whether the handoff is running with its current supplier."""
    return rt.case.thread_state.get("handoff", "") not in ("", "delivered", "no_backup")


def release_current(rt: Runtime, why: str) -> None:
    """Release the handoff's current supplier at a person's request.

    Before any packet this is an ordinary failover; after one, the handoff goes to a person.
    """
    sid = current(rt)
    if sid is None:
        return
    if as_int(rt.case.get("handoff.packets_sent")) == 0:
        failover(rt, why)
        return
    suppliers.release(rt, sid, why)
    if rt.case.thread_state.get("handoff") == "with_human":
        return  # a person already has the handoff
    _state(rt, "with_human")
    rt.escalate(R.SECOND_HANDOFF, f"supplier {sid}: {why} after a packet was sent", [f"released supplier {sid}"])


def second_handoff(rt: Runtime, target: int) -> None:
    """Approved by a human: release the first supplier; the backup re-enters at step 1."""
    old = current(rt)
    if old is not None:
        suppliers.release(rt, old, "second handoff approved by the advocate")
    rt.case.thread_state["second_handoff_approved"] = str(target)
    suppliers.set_status(rt, target, "primary", "second handoff")
    start(rt, target)


def _start_budget(rt: Runtime, stage: str, hours: float = 0, days: int = 0, due=None) -> None:
    """Start the budget for a stage; any older budget lapses."""
    generation = int(rt.case.thread_state.get("handoff_budget_gen", "0")) + 1
    rt.case.thread_state["handoff_budget_gen"] = str(generation)
    payload = {"stage": stage, "supplier": current(rt), "gen": generation, "status_called": False}
    if due is None:
        due = clock.add_business_hours(rt.now, hours) if hours else clock.add_business_days(rt.now, days)
    rt.schedule(due, "handoff_budget", payload)


def on_budget(rt: Runtime, event: Event) -> None:
    """A stage's budget ran out: one status call, then a human."""
    stage, sid = event.payload["stage"], event.payload["supplier"]
    if event.payload["gen"] != int(rt.case.thread_state.get("handoff_budget_gen", "0")) or sid != current(rt):
        return
    state = rt.case.thread_state.get("handoff", "")
    if state in POST_PACKET and blocked(rt, state):
        advance(rt)  # a hold is on that has not paused the step yet: pause it now
        return
    if state.startswith("held_") or state == "with_human":
        return  # paused until the hold lifts, or a person has it
    if stage == "patient_confirm":
        patient.start_ladder(rt, "delivery_check")
    elif stage == "followup":
        if state != "followup":
            return
        item = rt.escalate(
            R.HANDOFF_SILENCE,
            f"supplier {sid}: delivery problems still open after the follow-up "
            f"budget: {rt.case.thread_state.get('delivery_problems', '')}",
        )
        rt.pending_approvals[item.item_id] = lambda rt: _close_delivered(
            rt, "closed by the advocate with the delivery follow-up noted"
        )
    elif not event.payload["status_called"]:
        if rt.after_hours(f"status call to supplier {sid}", "handoff_budget", event.payload):
            return  # the same budget event, at the next business moment
        rt.send(f"supplier:{sid}", "status_call", stage=stage)
        rt.in_business_hours(STATUS_CALL_HOURS, "handoff_budget", {**event.payload, "status_called": True})
    else:
        rt.escalate(R.HANDOFF_SILENCE, f"supplier {sid} silent past the {stage} budget after one status call")


def on_status_reply(rt: Runtime, event: Event) -> None:
    """Run the safety reader on a status-call reply; the step itself waits for the supplier's event."""
    sid = _sid(event)
    safety = rt.read("check_safety_and_conditions", event, "Find safety signals and supplier conditions.")
    signals.raise_signals(rt, safety.raw if safety else None, f"supplier {sid}")
    if safety is not None and sid == current(rt) and rt.case.get(sf(sid, "packet_sent")) == "yes":
        post_packet_condition(rt, sid, safety)
    rt.log("status_call", f"supplier {sid} answered the status call; waiting for its event")
    rt.evaluate()  # a hold from a signal pauses the handoff now


def post_packet_condition(rt: Runtime, sid: int, safety: Reading) -> bool:
    """Handle a condition the supplier states after it has the packet. True if one was stated.

    The supplier is paused, what it said is recorded so it no longer counts as
    eligible, and the handoff goes to a person. Doubtful reads count (fail safe).
    """
    raw, flagged = safety.raw, safety.flagged
    stated = {
        name: field
        for name, field in (
            ("abn", raw.abn_requested),
            ("full_price_or_no_assignment", raw.full_price_or_no_assignment),
            ("other_condition", raw.other_condition),
            ("needs_info", raw.needs_info),
        )
        if suppliers.states_condition(field)
    }
    if not stated:
        return False
    if "abn" in stated:
        rt.case.set(sf(sid, "abn_required"), "yes")
        rt.escalate(
            R.ABN_REQUESTED, f"supplier {sid} would ask Eleanor to sign an ABN (said on a status call after the packet)"
        )
    if "full_price_or_no_assignment" in stated:
        rt.case.set(sf(sid, "accepts_assignment_all_months"), "no")
    unknown = [n for n in ("other_condition", "needs_info") if n in stated]
    if unknown:
        other = stated.get("other_condition")
        quoted = (
            f': "{other.value}"'
            if other is not None and other.status == "found" and other.value and other.evidence_turn_id not in flagged
            else ""
        )  # a flagged turn's words are never quoted
        rt.escalate(
            R.SUPPLIER_CONDITION_UNKNOWN,
            f"supplier {sid} states {', '.join(unknown)} on a status call after the packet{quoted}",
        )
    if not rt.case.thread_state.get("handoff_paused") and not rt.case.thread_state.get("handoff", "").startswith(
        "held_"
    ):
        rt.send(f"supplier:{sid}", "pause")
        rt.case.thread_state["handoff_paused"] = "yes"
    if rt.case.thread_state.get("handoff") != "with_human":
        failover(rt, "states " + ", ".join(stated) + " on a status call")
    return True


def on_status_silent(rt: Runtime, event: Event) -> None:
    rt.log("status_call", f"supplier {_sid(event)} did not answer the status call")


def on_accept(rt: Runtime, event: Event) -> None:
    """Step 4: the supplier accepts, the second half of the delivery gate."""
    sid = _sid(event)
    if sid != current(rt) or rt.case.get(sf(sid, "packet_sent")) != "yes":
        rt.log("unexpected_accept", f"supplier {sid} accepted without a current packet")
        return
    details = event.payload["row"].payload
    rt.case.set(sf(sid, "home_assessment_recorded"), str(details.get("home_assessment_recorded", "") or ""))
    rt.case.set(sf(sid, "same_or_similar_clear"), str(details.get("same_or_similar_clear", "") or ""))
    if rt.case.thread_state.get("handoff") == "with_human":
        rt.case.set(sf(sid, "reject_reason"), "")  # this acceptance is newer than any rejection
        blockers = resume_blockers(rt, sid)
        _note_for_person(
            rt,
            f"supplier {sid} accepted (home assessment recorded: "
            f"{details.get('home_assessment_recorded', '') or 'not said'}, same-or-similar clear: "
            f"{details.get('same_or_similar_clear', '') or 'not said'}); "
            + (
                f"it cannot go on yet: {', '.join(blockers)}"
                if blockers
                else f"to go on with it, call supplier:{sid} again"
            ),
        )
        return
    if rt.case.get(sf(sid, "accepted")) == "yes":  # e.g. an answer to a status call: nothing restarts
        rt.log("repeat_accept", f"supplier {sid} accepted again: recorded only")
        return
    clear = details.get("same_or_similar_clear", "")
    if clear != "yes":
        _rejected(rt, sid, "same_or_similar_found" if clear == "no" else "same_or_similar_unconfirmed")
        return
    unmet = gate.acceptance_gate(rt.case, sid)
    if unmet:
        rt.log("acceptance_incomplete", f"supplier {sid}: {', '.join(unmet)}")
        _acceptance_unmet(rt, sid, unmet)
        return
    _accepted(rt, sid)


def _note_for_person(rt: Runtime, what: str) -> None:
    """Note a supplier event on every open item that owns the handoff, so the person sees it."""
    rt.log("recorded_while_with_human", what)
    for item in rt.inbox.open_items():
        if item.reason in OWNER_REASONS:
            item.tried.append(what)


def resume_blockers(rt: Runtime, sid: int) -> list[str]:
    """Why the person cannot go on with `sid` on the strength of its acceptance."""
    blockers = gate.acceptance_gate(rt.case, sid)
    if rt.case.get(sf(sid, "reject_reason")):
        blockers.append(f"rejected ({rt.case.get(sf(sid, 'reject_reason'))})")
    if not gate.supplier_eligible(rt.case, sid):
        blockers.append(f"supplier {sid} no longer eligible ({rt.case.get(sf(sid, 'status'))})")
    if rt.case.get(sf(sid, "abn_required")) == "yes":
        blockers.append("an ABN on file")
    if rt.case.get(sf(sid, "accepted")) == "yes":
        blockers.append("accepted before")
    return blockers


def _accepted(rt: Runtime, sid: int) -> None:
    rt.case.set(sf(sid, "accepted"), "yes")
    rt.policy.confirm_outcome("supplier_handoff", ok=True)
    if not blocked(rt, "accepted") and rt.case.thread_state.pop("handoff_paused", None):
        rt.send(f"supplier:{sid}", "resume")  # it was paused while a person had the handoff
    _move(rt, "accepted")


def _acceptance_unmet(rt: Runtime, sid: int, unmet: list[str]) -> None:
    """The supplier accepted with a check unmet.

    Criterion C alone takes the no-home-assessment path; anything else is ours to fix and goes to a person.
    """
    ours = [u for u in unmet if not u.startswith("criterion C")]
    if ours:
        _state(rt, "with_human")
        rt.escalate(R.STALE_NEWS, f"supplier {sid} accepted the packet, but {', '.join(ours)} is not on file")
        return
    _rejected(rt, sid, "no_home_assessment")


def on_reject(rt: Runtime, event: Event) -> None:
    sid = _sid(event)
    if sid != current(rt) or rt.case.get(sf(sid, "packet_sent")) != "yes":
        rt.log("unexpected_reject", f"supplier {sid} rejected without a current packet")
        return
    if rt.case.thread_state.get("handoff") == "with_human":
        code = event.payload["row"].payload.get("reason_code", "other")
        rt.case.set(sf(sid, "reject_reason"), code)
        rt.case.set(sf(sid, "accepted"), "")  # a rejection withdraws any acceptance
        _note_for_person(rt, f"supplier {sid} rejected the packet ({code}) while you have the handoff")
        return
    _rejected(rt, sid, event.payload["row"].payload.get("reason_code", "other"))


def _rejected(rt: Runtime, sid: int, code: str) -> None:
    """Handle a rejected packet: a downstream failure for the approval switch, then by reason.

    Any earlier acceptance is withdrawn, so no later event moves the handoff on it.
    """
    rt.case.set(sf(sid, "reject_reason"), code)
    rt.case.set(sf(sid, "accepted"), "")
    rt.policy.confirm_outcome("supplier_handoff", ok=False, detail=f"supplier {sid} rejected: {code}")
    if code == "no_home_assessment" and rt.case.thread_state.get(f"supplier.{sid}.layout_resent") != "yes":
        rt.case.thread_state[f"supplier.{sid}.layout_resent"] = "yes"
        rt.send(f"supplier:{sid}", "resend_layout", home_layout=rt.case.get("patient.home_layout"))
        _start_budget(rt, "accept", hours=4)  # the old acceptance budget lapses
    elif code == "no_home_assessment":
        _state(rt, "with_human")
        rt.escalate(R.NO_HOME_ASSESSMENT, f"supplier {sid} still has no home assessment after the layout was resent")
    elif code in ("same_or_similar_found", "same_or_similar_unconfirmed"):
        _state(rt, "with_human")
        found = "found a prior wheelchair" if code == "same_or_similar_found" else "was not confirmed clear"
        rt.escalate(R.SAME_OR_SIMILAR, f"supplier {sid}'s same-or-similar check {found}")
    else:
        failover(rt, f"rejected the packet ({code})")


def on_scheduled(rt: Runtime, event: Event) -> None:
    """Step 5: the supplier scheduled delivery with her."""
    sid = _sid(event)
    if sid != current(rt) or rt.case.get(sf(sid, "accepted")) != "yes":
        rt.log("unexpected_schedule", f"supplier {sid} scheduled without an accepted packet")
        return
    given = event.payload["row"].payload.get("date", "")
    if rt.case.thread_state.get("handoff") == "with_human":  # a person has it: no step moves
        rt.case.set(sf(sid, "scheduled_date"), _date_text(given))
        rt.log(
            "unexpected_schedule", f"supplier {sid} scheduled delivery while a person has the handoff: recorded only"
        )
        return
    rt.case.set(sf(sid, "scheduled_date"), _date_text(given))
    _move(rt, "scheduled")


def _date_text(given) -> str:
    """A scheduled date as fact text. YAML parses an unquoted date as a date object, so it is converted to ISO."""
    if isinstance(given, datetime):
        given = given.date()
    return given.isoformat() if isinstance(given, date) else str(given or "")


def on_delivered(rt: Runtime, event: Event) -> None:
    """Step 6, supplier side. A delivery claim before acceptance is stale news."""
    sid = _sid(event)
    if sid != current(rt) or rt.case.get(sf(sid, "accepted")) != "yes":
        rt.escalate(R.STALE_NEWS, f"supplier {sid} reported a delivery the gate never allowed")
        return
    rt.case.set(sf(sid, "delivered_date"), rt.now.date().isoformat())
    if rt.case.thread_state.get("handoff") == "with_human":  # a person has it and decides
        rt.escalate(R.STALE_NEWS, f"supplier {sid} reported a delivery while a person has the handoff")
        return
    _move(rt, "delivered_by_supplier")


def on_delivery_check(rt: Runtime) -> None:
    """Step 6, Eleanor's side: close the case once both confirm delivery with no problems left.

    A fit, training or repair-number problem goes to the supplier as a follow-up first.
    """
    sid = current(rt)
    state = rt.case.thread_state.get("handoff")
    if sid is None or state not in ("delivered_by_supplier", "followup"):
        return
    if holds.held(rt.case, "handoff") or rt.case.frozen:
        rt.log("delivery_close_deferred", "delivery answer recorded; a hold or freeze is on, so nothing closes yet")
        return
    if rt.case.get("patient.delivery_received") == "no":
        _state(rt, "with_human")
        rt.escalate(R.STALE_NEWS, f"supplier {sid} reported delivery but Eleanor says the chair has not arrived")
        return
    problems = [
        f for f in ("patient.fit_ok", "patient.shown_how_to_use", "patient.has_repair_number") if rt.case.get(f) == "no"
    ]
    if problems:
        if state != "followup":
            rt.send(f"supplier:{sid}", "delivery_followup", issues=problems)
            rt.case.thread_state["delivery_problems"] = ", ".join(problems)
            _state(rt, "followup")
            _start_budget(rt, "followup", days=FOLLOWUP_DAYS)
        return
    if rt.case.get("patient.delivery_received") == "yes":
        _close_delivered(rt, f"delivery confirmed by supplier {sid} and by Eleanor")


def _close_delivered(rt: Runtime, why: str) -> None:
    """Close as delivered, unless a freeze or a handoff hold landed meanwhile."""
    if rt.case.frozen or holds.held(rt.case, "handoff"):
        rt.log("delivery_close_deferred", f"not closed ({why}): a hold or freeze is on")
        return
    _state(rt, "delivered")
    rt.case.closed = True
    rt.log("delivered", why)
