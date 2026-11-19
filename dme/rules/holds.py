"""Holds: what each always-escalate reason stops, and how it is lifted.

Only a human lifts a hold, by proceeding on its item or by supplying a fact that
settles it. A settling value already on file never lifts a hold, because the report
that raised the hold is newer.
"""

from __future__ import annotations

from dme.models import Case
from dme.models import EscalationReason as R

HOLDS_BY_REASON: dict[R, tuple[str, ...]] = {
    R.PATIENT_HOSPITALISED: ("handoff",),
    R.PATIENT_HOSPICE: ("handoff", "cost_messages"),  # hospice supplies related DME
    R.MEDICARE_ADVANTAGE: ("handoff", "cost_messages"),
    R.MEDICAID_OR_MSP: ("handoff", "cost_messages"),
    R.OUTSIDE_HOME_ONLY: ("handoff",),
    # the handoff puts a supplier on the phone with her: a person must clear these first
    R.PATIENT_OPTED_OUT: ("patient_contact", "handoff"),
    R.PATIENT_WANTS_PERSON: ("patient_contact", "handoff"),
    R.INTAKE_INCOMPLETE: ("patient_contact",),
}
SETTLED_BY: dict[R, tuple[tuple[str, str], ...]] = {  # reason -> the (fact id, value) pairs that settle it
    R.MEDICAID_OR_MSP: (("patient.medicaid_or_msp", "no"),),
    R.OUTSIDE_HOME_ONLY: (("patient.in_home_use", "yes"),),
    R.INTAKE_INCOMPLETE: (
        ("patient.identity_verified", "yes"),
        ("patient.consent_proceed", "yes"),
        ("patient.consent_share_with_suppliers", "yes"),
    ),
}


def unsettled(case: Case, reason: R) -> list[tuple[str, str]]:
    """The settling facts of `reason` that do not hold now ([] for a reason without any)."""
    return [(fact, value) for fact, value in SETTLED_BY.get(reason, ()) if case.get(fact) != value]


def _entries(case: Case) -> dict[str, tuple[str, ...]]:
    """Key -> the holds it put on, parsed from thread_state["held_for"].

    A key is a reason, or "reason@item_id" for extra holds one item put on, so each
    item lifts only its own.
    """
    entries = {}
    for entry in filter(None, case.thread_state.get("held_for", "").split(",")):
        key, _, names = entry.partition("=")
        entries[key] = tuple(filter(None, names.split("+")))
    return entries


def _reason(key: str) -> R:
    return R(key.partition("@")[0])


def _item_key(reason: R, item_id: str) -> str:
    return f"{reason.value}@{item_id}"


def held_for(case: Case) -> list[R]:
    """The reasons currently holding the case."""
    return list(dict.fromkeys(_reason(k) for k in _entries(case)))


def holding(case: Case, reason: R, item_id: str = "") -> bool:
    """Whether `reason`'s own holds, or the extra holds item `item_id` put on, are on."""
    entries = _entries(case)
    return reason.value in entries or bool(item_id and _item_key(reason, item_id) in entries)


def item_holds(case: Case, reason: R, item_id: str) -> tuple[str, ...]:
    """The extra holds item `item_id` (raised for `reason`) put on."""
    return _entries(case).get(_item_key(reason, item_id), ())


def _set(case: Case, entries: dict[str, tuple[str, ...]]) -> None:
    case.thread_state["held_for"] = ",".join(f"{k}={'+'.join(h)}" for k, h in entries.items())
    case.holds[:] = sorted({h for names in entries.values() for h in names})


def _add(entries: dict[str, tuple[str, ...]], key: str, names: tuple[str, ...]) -> bool:
    if not names or not set(names) - set(entries.get(key, ())):
        return False
    entries[key] = tuple(dict.fromkeys(entries.get(key, ()) + names))
    return True


def apply(case: Case, reason: R, extra: tuple[str, ...] = (), item_id: str = "") -> None:
    """Put on the holds for `reason`, plus any `extra` holds kept under item `item_id`."""
    if reason == R.PATIENT_DECEASED:
        case.frozen = True
    entries = _entries(case)
    changed = _add(entries, reason.value, tuple(dict.fromkeys(HOLDS_BY_REASON.get(reason, ()))))
    extra = tuple(h for h in dict.fromkeys(extra) if h not in HOLDS_BY_REASON.get(reason, ()))
    if extra and not item_id:
        raise ValueError("extra holds need the item that puts them on")
    if extra:
        changed |= _add(entries, _item_key(reason, item_id), extra)
    if changed:
        _set(case, entries)


def drop_item(case: Case, reason: R, item_id: str) -> None:
    """Forget the extra holds of item `item_id` (they were moved to another item)."""
    entries = _entries(case)
    if entries.pop(_item_key(reason, item_id), None) is not None:
        _set(case, entries)


def release(case: Case, reason: R, item_id: str = "") -> None:
    """Lift `reason`'s holds and item `item_id`'s extra holds after a human proceeds.

    A reason with settling facts lifts only once they all hold, so a confirmed
    Medicaid "yes" keeps the case out of automation. Holds that another reason or
    item needs stay on.
    """
    if unsettled(case, reason):
        return
    entries = _entries(case)
    keys = [reason.value] + ([_item_key(reason, item_id)] if item_id else [])
    lifted = [entries.pop(k, None) for k in keys]  # pop every key; no short-circuit
    if any(h is not None for h in lifted):
        _set(case, entries)


def _settled_key(reason: R) -> str:
    return f"settled.{reason.value}"


def unsettle(case: Case, reason: R) -> None:
    """`reason` was raised again, so no settling fact supplied before now counts."""
    case.thread_state.pop(_settled_key(reason), None)


def note_supplied(case: Case, fact_id: str, value: str) -> None:
    """Record that a person supplied a settling fact, newer than any report that raised its reasons."""
    for reason, settling in SETTLED_BY.items():
        if (fact_id, value) in settling:
            case.thread_state[_settled_key(reason)] = "yes"


def release_settled(case: Case, open_reasons: set[R]) -> None:
    """Lift a reason's holds once a person settled it, its settling facts hold, and no item for it is open."""
    for reason in SETTLED_BY:
        if (
            reason in held_for(case)
            and reason not in open_reasons
            and not unsettled(case, reason)
            and case.thread_state.get(_settled_key(reason)) == "yes"
        ):
            release(case, reason)


def held(case: Case, hold: str) -> bool:
    return case.frozen or hold in case.holds
