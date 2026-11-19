"""The patient thread: intake with Eleanor, her messages and replies, and the unreachable ladder.

Claude drafts her messages and code checks and fills them, except the cost message,
which is always a code-owned template because Claude never writes about money.
"""

from __future__ import annotations

import json
import re
from datetime import date, time, timedelta
from typing import TYPE_CHECKING, Any

from dme import clock, drafts
from dme.engine import Event
from dme.evidence import normalise
from dme.facts import as_int
from dme.facts import supplier_fact as sf
from dme.models import EscalationReason as R
from dme.models import LlmUnavailable, PatientReplyRead
from dme.protocols import CallContext
from dme.rules import gate, handoff, holds, pcp, signals, suppliers
from dme.rules.cost import cost_facts, cost_range

if TYPE_CHECKING:
    from dme.inbox import InboxItem
    from dme.rules.runtime import Reading, Runtime

FIRST_CONTACT_SCRIPT = (
    "identity (name, date of birth); consent to proceed and to share with candidate suppliers; address and ZIP; "
    "in-home use; home layout; any wheelchair in the last 5 years; help from Illinois Medicaid or a Medicare "
    "Savings Program; preferred supplier; a helper we may talk to; where her number came from and consent to "
    "calls and texts; Medicare number if she is comfortable"
)
QUESTIONS = {  # fixed questions; the proposer may pick one by key
    "needs_medicare_id": "The supplier needs your Medicare number to check your coverage. Are you comfortable "
    "sharing it with them?",
    "needs_demographics": "The supplier needs your address to confirm delivery. May we share it with them?",
    "needs_order_copy": "May we send the supplier a copy of your doctor's order?",
    "medicaid": "Do you get help paying Medicare costs from Illinois Medicaid or a Medicare Savings Program?",
    "home_layout": "Could you describe the rooms you would use the wheelchair in, and any steps or narrow doors?",
    "availability": "Which days and times are best for the delivery?",
    "identity": "Before we go on, could you confirm your full name and date of birth?",
    "consent": "May we arrange the wheelchair for you and share your details with medical equipment suppliers?",
}
INTAKE_REPLIES = ("first_contact", "ask_identity", "ask_consent", "ask_medicaid")
PATIENT_ONLY = {
    "identity_name",
    "identity_dob",
    "consent_proceed",
    "consent_share_with_suppliers",
    "consent_calls_texts",
    "medicaid_or_msp",
    "medicare_id",
    "medicare_id_ok_to_share",
    "helper_permission",
}  # only from her own turns: a helper cannot grant herself permission
DELIVERY_ANSWERS = {"delivery_received", "fit_ok", "shown_how_to_use", "has_repair_number"}
REREAD_HOURS = 1  # a reply Claude could not read is read once more this much later
FIRST_CONTACT_TIMEOUT_HOURS = 4  # no reply, no voicemail, nothing: the ladder starts
INTAKE_REASK_HOURS = 4  # how long re-asked intake questions wait before a person takes over
TEXT_FACTS = {
    "identity_dob": "patient.dob",
    "address": "patient.address",
    "zip": "patient.zip",
    "home_layout": "patient.home_layout",
    "preferred_supplier": "patient.preferred_supplier",
    "helper_name": "patient.helper_name",
    "phone_source": "patient.phone_source",
    "medicare_id": "patient.medicare_id",
    "availability": "patient.availability",
}
YES_NO_FACTS = {
    "in_home_use": "patient.in_home_use",
    "prior_wheelchair_5y": "patient.prior_wheelchair_5y",
    "helper_permission": "patient.helper_permission",
    "consent_proceed": "patient.consent_proceed",
    "consent_share_with_suppliers": "patient.consent_share_with_suppliers",
    "consent_calls_texts": "patient.consent_calls_texts",
    "medicare_id_ok_to_share": "patient.medicare_id_ok_to_share",
    "delivery_received": "patient.delivery_received",
    "fit_ok": "patient.fit_ok",
    "shown_how_to_use": "patient.shown_how_to_use",
    "has_repair_number": "patient.has_repair_number",
}
TEMPLATES = {  # code-owned fallbacks, as sentences; a sentence whose facts are missing is left out
    "cost_explanation": [
        "Medicare Part B rents the wheelchair to you month by month, and you own it at the end of the rental period.",
        "Each year your Part B deductible is paid before Medicare starts paying, and after that you pay your share "
        "of each month's Medicare-approved amount.",
        "Your supplier accepts Medicare assignment, so it bills you only for your share, at or after delivery.",
        "In total we estimate your share at about ${cost.low} if your deductibles are already met by other bills, "
        "and up to about ${cost.high} if they are not.",
        "The estimate assumes the Part B deductible for {assumed_deductibles}, because that has not been "
        "announced yet.",
    ],
    "cost_structure": [
        "Medicare Part B rents the wheelchair to you month by month, and you own it at the end of the rental period.",
        "Each year your Part B deductible is paid before Medicare starts paying, and after that you pay your share "
        "of each month's Medicare-approved amount.",
        "We will tell you the expected amounts once a supplier has confirmed it accepts Medicare's rate for the "
        "whole rental.",
    ],
    "supplier_will_call": [  # "supplier.ID." is pointed at the real supplier fact ids passed in
        "{supplier.ID.name} will call you from {supplier.ID.phone} to arrange your wheelchair delivery.",
        "Medicare rules say a supplier may only call you with your written permission, so we are sending you a "
        "short form to sign electronically.",
    ],
    "delivery_check": [
        "Has your wheelchair arrived?",
        "Does it fit you and your home, did the driver show you how to use it, and do you have the supplier's "
        "repair phone number?",
    ],
    "status_update": ["We are still working on your wheelchair order and will be in touch soon."],
}
LADDER = ("attempt", "attempt", "helper", "letter", "pcp", "escalate")  # after the first attempt fails
RETRY_TIMES = (time(10, 30), time(17, 45))  # later attempts: other days, other times of day
LADDER_WAIT_DAYS = {"helper": 1, "letter": 5, "pcp": 2}


def register(rt: Runtime) -> None:
    rt.route("reply", "patient", "*", on_reply)
    rt.route("no_answer", "patient", "*", on_no_answer)
    rt.route("voicemail", "patient", "*", on_no_answer)
    rt.route("esign", "patient", "*", on_esign)
    rt.timer("patient_contact_due", on_contact_due)
    rt.timer("patient_message_due", on_message_due)
    rt.timer("patient_ladder", on_ladder)
    rt.timer("first_contact_timeout", on_first_contact_timeout)
    rt.timer("intake_reask_due", on_intake_reask_due)
    rt.timer("patient_reread", on_reply)


def on_message_due(rt: Runtime, event: Event) -> None:
    """Send a message deferred to the contact window, if it still fits the case; a cost message is recomposed."""
    purpose = event.payload["purpose"]
    if purpose in GATED_MESSAGES:
        send_cost_message(rt)
        return
    if _moved_on(rt, purpose, {"supplier": sid} if (sid := _supplier_in(event.payload["facts"])) else {}):
        return
    message(rt, **event.payload)


def on_contact_due(rt: Runtime, event: Event) -> None:
    """Send a contact deferred to the contact window, if it still fits the case."""
    action, details = event.payload["action"], event.payload["details"]
    if _moved_on(rt, action, details):
        return
    contact(rt, action, **details)


def _supplier_in(facts: dict[str, str]) -> str:
    """The supplier id whose facts a message carries ("" if none)."""
    return next((m.group(1) for k in facts if (m := re.fullmatch(r"supplier\.(\d+)\.name", k))), "")


def _moved_on(rt: Runtime, action: str, details: dict[str, Any]) -> bool:
    """Whether `action` no longer belongs to where the case is (logged if so).

    The step it belongs to asks again by itself once it is current again.
    """
    why = ""
    if action in SUPPLIER_STEPS and not _permission_step_current(rt, details):
        why = "the permission step it belongs to has moved on"
    elif action == "delivery_check" and rt.case.thread_state.get("handoff") != "delivered_by_supplier":
        why = "the handoff is not waiting on the delivery check"
    elif action == "esign_release" and not suppliers.may_disclose(rt.case):
        rt.case.thread_state["pcp_release"] = "wanted"  # asked again once she may be disclosed
        why = "her identity, consent or a contact hold stops it now"
    if why:
        rt.log("patient_contact_dropped", f"{action} not sent: {why}")
    return bool(why)


def start(rt: Runtime) -> None:
    contact(rt, "first_contact", script=FIRST_CONTACT_SCRIPT)
    rt.in_business_hours(FIRST_CONTACT_TIMEOUT_HOURS, "first_contact_timeout")


def on_first_contact_timeout(rt: Runtime, event: Event) -> None:
    if rt.case.thread_state.get("patient_replied") != "yes":
        start_ladder(rt, "first_contact")


# Outbound


GATED_MESSAGES = ("cost_explanation",)  # always composed fresh, never resent from stored text
SUPPLIER_STEPS = ("supplier_will_call", "esign_supplier_permission")  # sent only while that supplier's step runs


def contact(rt: Runtime, action: str, **details: Any) -> None:
    """Any outbound contact with Eleanor, honouring holds and the 08:00-21:00 window."""
    if action in GATED_MESSAGES:
        send_cost_message(rt)
        return
    _contact(rt, action, **details)


def _contact(rt: Runtime, action: str, **details: Any) -> bool:
    """Send or defer a contact; False when a hold stops it. A deferral counts as sent."""
    sent = json.loads(rt.case.thread_state.get("patient_details", "{}"))
    rt.case.thread_state["patient_details"] = json.dumps({**sent, action: details}, default=str)
    if holds.held(rt.case, "patient_contact"):
        rt.log("patient_contact_blocked", f"{action} not sent: patient contact is on hold")
        return False
    if not clock.in_contact_window(rt.now):
        rt.schedule(clock.next_contact_time(rt.now), "patient_contact_due", {"action": action, "details": details})
        return True
    if rt.send("patient", action, **details):
        if "supplier" in details:  # remember which supplier a form was for, by attempt
            attempt = rt.case.attempts[f"patient:{action}"]
            rt.case.thread_state[f"form_supplier.{action}.{attempt}"] = str(details["supplier"])
        rt.case.set("patient.contact_attempts", str(as_int(rt.case.get("patient.contact_attempts")) + 1))
        rt.case.thread_state["patient_last_action"] = action
    return True


def _permission_step_current(rt: Runtime, details: dict[str, Any]) -> bool:
    """Whether the permission step this message or form belongs to is still running."""
    sid = handoff.current(rt)
    if rt.case.thread_state.get("handoff") != "permission" or sid is None:
        return False
    return "supplier" not in details or str(details["supplier"]) == str(sid)


def _details(rt: Runtime, action: str) -> dict[str, Any]:
    """What was sent with `action` the last time, so a retry repeats it in full."""
    return json.loads(rt.case.thread_state.get("patient_details", "{}")).get(action, {})


def resend(rt: Runtime, action: str) -> bool:
    """Send `action` again through the same gates; False when a gate withholds it now."""
    if action in GATED_MESSAGES:
        return send_cost_message(rt)
    details = _details(rt, action)
    if _moved_on(rt, action, details):
        return False
    return _contact(rt, action, **details)


def ask(rt: Runtime, key: str) -> None:
    """One fixed question."""
    contact(rt, f"ask_{key}", text=QUESTIONS[key])


def request_release(rt: Runtime) -> None:
    """Send her an e-signature request for the release the PCP office wants."""
    contact(rt, "esign_release", document="release of records to the supplier and to us")


def retry_pending(rt: Runtime) -> None:
    """A person asked to contact her again about what is pending (or was last sent)."""
    action = rt.case.thread_state.get("patient_pending") or rt.case.thread_state.get("patient_last_action")
    if action:
        resend(rt, action)


def template_text(key: str, facts: dict[str, str]) -> str:
    """The fixed template for `key`, keeping only sentences whose facts are all given."""
    sentences = TEMPLATES.get(key, [])
    sid = next((m.group(1) for k in facts if (m := re.fullmatch(r"supplier\.(\d+)\.name", k))), None)
    if sid is not None:
        sentences = [x.replace("supplier.ID.", f"supplier.{sid}.") for x in sentences]
    kept = [x for x in sentences if all(facts.get(k, "").strip() for k in drafts.PLACEHOLDER.findall(x))]
    return " ".join(kept)


def compose(
    rt: Runtime, purpose: str, facts: dict[str, str], template_key: str, goal: str = "", writer: bool = True
) -> str | None:
    """Claude's draft, checked and filled, else the fixed template; None means a person must answer.

    The cost message always uses the template, so Claude never writes about money.
    """
    if not writer or purpose == "cost_explanation":
        return _template(rt, purpose, template_key, facts)
    rt.draft_nth[purpose] += 1
    ctx = CallContext(
        case_id=rt.case.case_id,
        goal=goal or f"Write a short {purpose} message to Eleanor.",
        facts=facts,
        nth=rt.draft_nth[purpose],
    )
    draft = rt.call_llm("write_patient_message", lambda: rt.llm.write_patient_message(purpose, facts, ctx))
    if not isinstance(draft, LlmUnavailable):
        if draft.text == "":
            return None
        problems = drafts.check_draft(draft.text, facts, purpose)
        if not problems:
            return drafts.fill(draft.text, facts)
        rt.log("draft_rejected", f"{purpose} draft rejected: {'; '.join(problems)}")
    return _template(rt, purpose, template_key, facts)


def _template(rt: Runtime, purpose: str, template_key: str, facts: dict[str, str]) -> str | None:
    template = template_text(template_key, facts)
    if not template:
        return None
    rt.log("template_used", f"{purpose}: fixed template")
    return drafts.fill(template, facts)


def message(
    rt: Runtime, purpose: str, facts: dict[str, str], template_key: str = "", goal: str = "", writer: bool = True
) -> bool:
    """Send a message whose every fact comes from `facts`. False when a hold or gate withholds it."""
    if purpose == "cost_explanation" and holds.held(rt.case, "cost_messages"):
        rt.log("cost_message_held", "cost message not sent: cost messages are on hold")
        return False
    if purpose == "cost_explanation" and facts and cost_figures_allowed(rt) is None:
        # Figures from a deferred send whose supplier is no longer in play.
        rt.log("cost_figures_withdrawn", "cost figures not sent: no qualified supplier's terms to rest on now")
        facts, template_key = {}, "cost_structure"
    if holds.held(rt.case, "patient_contact"):
        rt.log("patient_contact_blocked", f"{purpose} not sent: patient contact is on hold")
        return False
    if not clock.in_contact_window(rt.now):
        rt.schedule(
            clock.next_contact_time(rt.now),
            "patient_message_due",
            {"purpose": purpose, "facts": facts, "template_key": template_key, "goal": goal, "writer": writer},
        )
        return True
    text = compose(rt, purpose, facts, template_key or purpose, goal, writer)
    if text is None:
        rt.escalate(R.PATIENT_QUESTION, f"needs a person to answer: {goal or purpose}")
        return True
    return _contact(rt, purpose, text=text)


def cost_figures_allowed(rt: Runtime) -> int | None:
    """The supplier whose terms cost figures may rest on, or None if figures may not be shown yet.

    That needs an eligible supplier (assignment for all months) and a Medicaid answer of no.
    """
    if rt.case.get("patient.medicaid_or_msp") != "no":
        return None
    chosen = handoff.current(rt)
    candidates = ([chosen] if chosen is not None else []) + suppliers.with_status(rt, *gate.ELIGIBLE_STATUSES)
    for sid in candidates:
        if gate.supplier_eligible(rt.case, sid):
            return sid
    return None


def send_cost_message(rt: Runtime) -> bool:
    """Send the cost message, with figures only if they may be shown now. False when a hold withholds it."""
    sid = cost_figures_allowed(rt)
    if sid is None:  # no supplier's terms to rest on yet
        return message(rt, "cost_explanation", {}, template_key="cost_structure")
    # A delivery date we may act on, else today.
    delivery = clock.iso_date(rt.case.get(sf(sid, "earliest_delivery")), rt.now)
    estimate = cost_range(delivery or rt.now.date())
    rt.case.facts.update(cost_facts(estimate))
    facts = {"cost.low": rt.case.get("cost.low"), "cost.high": rt.case.get("cost.high")}
    assumed = estimate["assumed_deductibles"]
    if assumed:  # name every year whose deductible is an assumption
        facts["assumed_deductibles"] = assumed_deductibles_text(assumed)
    return message(rt, "cost_explanation", facts)


def answer_cost_question(rt: Runtime) -> None:
    """Answer her cost question now, or remember it until the hold that withholds it lifts."""
    if send_cost_message(rt):
        rt.case.thread_state.pop("cost_question_pending", None)
    else:
        rt.case.thread_state["cost_question_pending"] = "yes"
        rt.log("cost_question_pending", "her cost question is answered once the hold that withholds it lifts")


def resume_cost_question(rt: Runtime) -> None:
    """Answer a withheld cost question once nothing holds it."""
    if (
        rt.case.thread_state.get("cost_question_pending") == "yes"
        and not holds.held(rt.case, "cost_messages")
        and not holds.held(rt.case, "patient_contact")
    ):
        answer_cost_question(rt)


def assumed_deductibles_text(assumed: list[dict]) -> str:
    """ "2027 stays at $283", "2027 and 2028 stays at $283", or each year with its own amount."""
    years = [str(a["year"]) for a in assumed]
    amounts = {a["amount"] for a in assumed}
    if len(amounts) == 1:
        joined = years[0] if len(years) == 1 else f"{', '.join(years[:-1])} and {years[-1]}"
        return f"{joined} stays at ${amounts.pop()}"
    return " and ".join(f"{a['year']} (${a['amount']})" for a in assumed)


# Inbound


def on_reply(rt: Runtime, event: Event) -> None:
    state = rt.case.thread_state
    state["patient_ladder"] = ""  # she answered: the ladder stops
    state.pop("patient_pending", None)
    state["patient_replied"] = "yes"
    action = event.payload["action"]
    reread = event.payload.get("reread", False)
    reading = rt.read(
        "read_patient_reply",
        event,
        f"Read Eleanor's reply to our {action.replace('_', ' ')}.",
        ("patient.name",),
        escalate=False,
    )
    safety = rt.read("check_safety_and_conditions", event, "Find safety signals in Eleanor's reply.", escalate=False)
    reported = signals.patient_primary_signals(reading.raw) if reading else set()
    if signals.keyword_opt_out(event.payload["row"].turns):  # every patient and helper turn, flagged or not
        reported.add("opts_out")
    # The script's Medicaid rule handles her own answer; any other Medicaid signal (a
    # helper's answer, a failed read, a safety read disagreeing with her) escalates.
    supplied = rt.case.thread_state.get("medicaid_supplied") == "yes"
    answer = medicaid_answer(reading, action, supplied) if reading is not None else ""
    scripted = answer != "" and safety is not None and safety_read_is_her_answer(safety, answer)
    expected = {"medicaid_or_msp"} if scripted else set()
    signals.raise_signals(rt, safety.raw if safety else None, "Eleanor's reply", reported, expected)
    if reading is not None:
        # Her "no" only tightens, so it applies at once: even if the safety read failed,
        # from a flagged turn, or while the case is frozen.
        apply_refusals(rt, reading, action)
    if reading is None or safety is None:
        if not reread:  # read the same reply once more later
            rt.schedule_reread(REREAD_HOURS, "patient_reread", event)
        else:
            rt.escalate(
                R.LLM_UNAVAILABLE,
                f"Eleanor's reply to {action} could not be read twice",
                ["read once", f"read again {REREAD_HOURS} business hour later"],
                extra_holds=("patient_contact", "handoff"),
            )
        return
    if rt.case.frozen:  # a deceased report froze the case: the reply is not acted on
        turns = ", ".join(t.turn_id for t in reading.turns)
        rt.log(
            "patient_reply_not_acted_on",
            f"her reply to {action} ({turns}) is not acted on while the case is "
            "frozen; she is asked again if a person finds the report false",
            data={"action": action, "turns": turns},
        )
        return
    read: PatientReplyRead = reading.read
    apply_answers(rt, reading, action)
    if action.startswith("ask_"):  # she answered this re-asked question, whatever she said
        waiting = rt.case.thread_state.get("intake_reask_waiting", "").split(",")
        rt.case.thread_state["intake_reask_waiting"] = ",".join(k for k in waiting if k and k != action[4:])
    if action in INTAKE_REPLIES:
        check_intake(rt)
    intents = {i.value for i in read.intents}
    if "cost_question" in intents:
        answer_cost_question(rt)
    for intent in read.intents:
        if intent.value == "other_question":
            answer_question(rt, intent.evidence_quote)
    if action == "delivery_check":
        handoff.on_delivery_check(rt)
    suppliers.start(rt)  # checks consent, ZIP and holds itself
    rt.evaluate()


def ask_again_after_freeze(rt: Runtime, item: InboxItem) -> None:
    """After a false deceased report is cleared, resend each message she answered while frozen."""
    lines = rt.engine.log_lines
    cleared = [n for n, line in enumerate(lines) if line.kind == "deceased_report_cleared"]
    since = cleared[-2] if len(cleared) > 1 else -1  # this freeze began after the previous clearing
    unread = [line.data for line in lines[since + 1 :] if line.kind == "patient_reply_not_acted_on"]
    if not unread:
        return
    item.tried.append(
        "not acted on while the case was frozen: her replies to "
        + "; ".join(f"{d['action']} ({d['turns']})" for d in unread)
        + "; she is asked again"
    )
    for action in dict.fromkeys(d["action"] for d in unread):
        resend(rt, action)


def apply_answers(rt: Runtime, reading: Reading, action: str) -> None:
    """Record what she said, using only fields the read found and `usable` allows."""
    read: PatientReplyRead = reading.read
    for name, fact_id in {**TEXT_FACTS, **YES_NO_FACTS}.items():
        field = getattr(read, name)
        if field.status == "found" and field.value and usable(reading, name, action):
            rt.case.set(fact_id, field.value)
    # Identity: her name and date of birth from her own turns, in one reply or two, and only
    # in replies to intake questions (a later "This is Eleanor" sign-off is not an answer).
    name, dob = read.identity_name, read.identity_dob
    intake = action in INTAKE_REPLIES
    said_name = intake and name.status == "found" and bool(name.value) and usable(reading, "identity_name", action)
    said_dob = intake and dob.status == "found" and bool(dob.value) and usable(reading, "identity_dob", action)
    if said_name:
        rt.case.thread_state["stated_name"] = name.value
    stated = rt.case.thread_state.get("stated_name", "")
    if (said_name or said_dob) and stated and rt.case.get("patient.dob"):
        same = normalise(stated) == normalise(rt.case.get("patient.name")) and dob_matches_age(rt)
        rt.case.set("patient.identity_verified", "yes" if same else "no")
    answer = medicaid_answer(reading, action, rt.case.thread_state.get("medicaid_supplied") == "yes")
    if answer:  # yes or not sure: held, and a human decides
        rt.case.set("patient.medicaid_or_msp", answer)
        if answer in ("yes", "not_sure"):
            rt.escalate(R.MEDICAID_OR_MSP, f"Medicaid/Savings Program answer: {answer}")
    if rt.case.get("patient.in_home_use") == "no":
        rt.escalate(R.OUTSIDE_HOME_ONLY, "Eleanor says she would use the chair only outside the home")
    declined(rt)


REFUSALS = (
    "consent_proceed",
    "consent_share_with_suppliers",
    "consent_calls_texts",
    "helper_permission",
    "medicare_id_ok_to_share",
)  # a "no" to any of these only tightens


def apply_refusals(rt: Runtime, reading: Reading, action: str) -> None:
    """Record her refusals at once, never waiting until the reply can be read in full.

    They are read from before the flagged-turn drop, because a refusal only makes the
    case more careful; they still count only from her own turns.
    """
    for name in REFUSALS:
        field = getattr(reading.raw, name)
        if field.status == "found" and field.value == "no" and usable(reading, name, action, reading.raw):
            rt.case.set(YES_NO_FACTS[name], "no")
    declined(rt)


def usable(reading: Reading, name: str, action: str, read: Any = None) -> bool:
    """Whether field `name` may become a fact.

    Identity, consent, the Medicaid answer and permissions count only from her own
    turns; a helper may give logistics. Delivery answers count only in the reply to
    the delivery check.
    """
    field = getattr(read if read is not None else reading.read, name)
    who = {t.turn_id: t.speaker for t in reading.turns}.get(field.evidence_turn_id)
    if who not in ("patient", "helper") or (name in PATIENT_ONLY and who != "patient"):
        return False  # an unknown speaker gives nothing; a helper gives only logistics
    return not (name in DELIVERY_ANSWERS and action != "delivery_check")


def medicaid_answer(reading: Reading, action: str, supplied: bool = False) -> str:
    """Her own answer to the Medicaid question in an intake reply: yes, no, not_sure, or "".

    A doubtful read counts as not_sure. Once a person has supplied the answer, no
    reader overwrites it, and anything she says about it is a signal instead.
    """
    if action not in INTAKE_REPLIES or supplied:
        return ""
    medicaid = reading.read.medicaid_or_msp
    answer = "not_sure" if medicaid.status in signals.DOUBT else medicaid.value if medicaid.status == "found" else ""
    return answer if answer and usable(reading, "medicaid_or_msp", action) else ""


def safety_read_is_her_answer(safety: Reading, answer: str) -> bool:
    """Whether the safety reader's Medicaid signal, if any, is exactly her own answer; anything else escalates."""
    field = safety.raw.medicaid_or_msp
    if not signals.fires(field):
        return True
    who = {t.turn_id: t.speaker for t in safety.turns}.get(field.evidence_turn_id)
    return who == "patient" and field.status == "found" and field.value == answer


OPT_OUT_FACTS = ("patient.consent_proceed", "patient.consent_share_with_suppliers", "patient.consent_calls_texts")


def declined(rt: Runtime) -> bool:
    """Treat a "no" to going ahead, to sharing, or to calls and texts as an opt-out.

    A person then decides how to reach her; contact and the handoff are held.
    """
    case = rt.case
    if "no" in (case.get("patient.consent_proceed"), case.get("patient.consent_share_with_suppliers")):
        rt.escalate(R.PATIENT_OPTED_OUT, "Eleanor declined to go ahead or to share her details with suppliers")
        return True
    if case.get("patient.consent_calls_texts") == "no":
        rt.escalate(R.PATIENT_OPTED_OUT, "Eleanor does not want automated calls or texts")
        return True
    return False


def birth_years(dob: str, today: date) -> list[int]:
    """The birth years a stated date of birth can mean.

    A four-digit year is taken as written; a two-digit year ("3/3/84") can be 19xx or
    20xx, whichever is not in the future.
    """
    four = re.search(r"\b(19|20)\d{2}\b", dob)
    if four:
        return [int(four.group(0))]
    two = re.findall(r"(?<![0-9])([0-9]{2})(?![0-9])", dob)
    if not two:
        return []
    yy = int(two[-1])
    return [y for y in (1900 + yy, 2000 + yy) if y <= today.year]


def dob_matches_age(rt: Runtime) -> bool:
    """Whether the stated date of birth fits the age on file, within a year; one with no year never does."""
    years = birth_years(rt.case.get("patient.dob"), rt.now.date())
    if not years:
        return False
    age = rt.case.get("patient.age")
    if not age.isdigit():
        return True
    return any(abs(rt.now.year - year - int(age)) <= 1 for year in years)


def answer_question(rt: Runtime, question: str) -> None:
    """Answer a free-form question from the FAQ, or hand it to a person."""
    message(rt, "answer_question", {}, goal=f'Patient asks: "{question}"')


def check_intake(rt: Runtime, timed_out: bool = False) -> None:
    """Check that first contact established identity, consent and the Medicaid answer.

    Anything missing is asked once more. If it is still missing after she has answered
    every re-asked question, or after INTAKE_REASK_HOURS, a person takes over.
    """
    case, state = rt.case, rt.case.thread_state
    if declined(rt):
        return
    missing = [
        key
        for key, ok in (
            ("identity", case.get("patient.identity_verified") == "yes"),
            (
                "consent",
                case.get("patient.consent_proceed") == "yes"
                and case.get("patient.consent_share_with_suppliers") == "yes",
            ),
            ("medicaid", bool(case.get("patient.medicaid_or_msp"))),
        )
        if not ok
    ]
    if not missing:
        state.pop("intake_reask_waiting", None)
        return
    if state.get("intake_reasked") != "yes":
        state["intake_reasked"] = "yes"
        state["intake_reask_waiting"] = ",".join(missing)
        for key in missing:
            ask(rt, key)
        rt.in_business_hours(INTAKE_REASK_HOURS, "intake_reask_due")
        return
    waiting = [k for k in state.get("intake_reask_waiting", "").split(",") if k in missing]
    if waiting and not timed_out:
        state["intake_reask_waiting"] = ",".join(waiting)
        rt.log("intake_waiting", f"still missing {', '.join(missing)}; waiting for her answers to {', '.join(waiting)}")
        return
    state.pop("intake_reask_waiting", None)
    for key in missing:
        if key == "medicaid":  # no answer after asking twice counts as "not sure"
            case.set("patient.medicaid_or_msp", "not_sure")
            rt.escalate(R.MEDICAID_OR_MSP, "Medicaid/Savings Program question unanswered after asking twice")
        else:
            rt.escalate(R.INTAKE_INCOMPLETE, f"first contact: {key} not confirmed after asking twice")


def on_intake_reask_due(rt: Runtime, event: Event) -> None:
    """Hand whatever intake is still missing to a person once the re-ask times out."""
    if rt.case.thread_state.get("intake_reask_waiting") and not rt.case.frozen:
        check_intake(rt, timed_out=True)
        rt.evaluate()


def on_esign(rt: Runtime, event: Event) -> None:
    rt.case.thread_state["patient_ladder"] = ""
    action = event.payload["action"]
    rt.log("esign", f"Eleanor e-signed ({action}) (MOCK)")
    if action == "esign_release":
        pcp.on_release_signed(rt)
    elif action == "esign_supplier_permission":
        signed_for = rt.case.thread_state.get(f"form_supplier.{action}.{event.attempt}", "")
        handoff.on_permission(rt, as_int(signed_for, None))


# Unreachable ladder


def on_no_answer(rt: Runtime, event: Event) -> None:
    rt.log("patient_no_answer", f"no answer to {event.payload['action']}")
    start_ladder(rt, event.payload["action"])


def start_ladder(rt: Runtime, action: str) -> None:
    """She did not answer `action`: retry on other days and times, then helper, letter, PCP office, a human."""
    state = rt.case.thread_state
    if state.get("patient_ladder"):
        return  # already climbing
    if holds.held(rt.case, "patient_contact"):
        rt.log("patient_ladder", f"not started for {action}: patient contact is on hold")
        return
    state["patient_pending"] = action
    _schedule_rung(rt, 1)


def _schedule_rung(rt: Runtime, rung: int) -> None:
    rt.case.thread_state["patient_ladder"] = str(rung)
    step = LADDER[rung - 1]
    if step == "attempt":
        day = rt.now.date() + timedelta(days=1)
        due = clock.at(day, RETRY_TIMES[rung - 1])
    elif rung > 1 and LADDER[rung - 2] in LADDER_WAIT_DAYS:
        due = clock.add_business_days(rt.now, LADDER_WAIT_DAYS[LADDER[rung - 2]])
    else:
        due = clock.add_business_days(rt.now, 1)
    rt.schedule(due, "patient_ladder", {"rung": rung})


def on_ladder(rt: Runtime, event: Event) -> None:
    state = rt.case.thread_state
    rung = event.payload["rung"]
    if state.get("patient_ladder") != str(rung):
        return  # she answered, or the ladder moved on
    step, pending = LADDER[rung - 1], state.get("patient_pending", "")
    if holds.held(rt.case, "patient_contact"):
        state["patient_ladder"] = ""
        rt.log("patient_ladder", f"stopped at rung {rung}: patient contact is on hold")
        return
    rt.log("patient_ladder", f"rung {rung}: {step} ({pending})")
    if step == "attempt":
        if not resend(rt, pending):  # a gate withholds it now, so there is nothing to chase
            state["patient_ladder"] = ""
            state.pop("patient_pending", None)
            rt.log("patient_ladder", f"stopped at rung {rung}: {pending} is withheld now")
            return
    elif step == "helper":
        if rt.case.get("patient.helper_permission") == "yes" and rt.case.get("patient.helper_name"):
            contact(rt, "helper_contact", helper=rt.case.get("patient.helper_name"), about=pending)
        else:
            rt.log("patient_ladder", "no helper with her permission: skipping that rung")
    elif step == "letter":
        rt.send("patient", "letter", about=pending)
    elif step == "pcp":
        rt.send("pcp", "reach_patient", about=pending)
    else:
        state["patient_ladder"] = ""  # reset: a later failed contact climbs again
        rt.escalate(
            R.PATIENT_UNREACHABLE,
            f"Eleanor unreachable about {pending}",
            ["three calls on different days and times", "helper", "letter", "PCP office"],
        )
        return
    _schedule_rung(rt, rung + 1)
