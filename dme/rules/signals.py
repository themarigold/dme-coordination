"""Safety signals, supplier-condition checks, the opt-out backstop and injection detection.

A signal fires if either reader reports it, even doubtfully, because a signal only
makes the case more careful. Nothing read from a turn flagged for prompt injection
becomes a fact.
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING

from dme.evidence import fold_untrusted, normalise
from dme.models import (
    SAFETY_SIGNALS,
    SUPPLIER_CONDITIONS,
    EscalationReason,
    PatientReplyRead,
    SafetyRead,
    SupplierCallRead,
    Turn,
)
from dme.rules.gate import found

if TYPE_CHECKING:
    from dme.rules.runtime import Runtime

SIGNAL_REASONS: dict[str, EscalationReason] = {
    "deceased": EscalationReason.PATIENT_DECEASED,  # first: freezes everything
    "opts_out": EscalationReason.PATIENT_OPTED_OUT,
    "wants_person": EscalationReason.PATIENT_WANTS_PERSON,
    "hospitalised": EscalationReason.PATIENT_HOSPITALISED,
    "hospice": EscalationReason.PATIENT_HOSPICE,
    "medicare_advantage": EscalationReason.MEDICARE_ADVANTAGE,
    "medicaid_or_msp": EscalationReason.MEDICAID_OR_MSP,
}

OPT_OUT_KEYWORDS = (
    "stop calling",
    "stop texting",
    "don't call",
    "do not call",
    "don't contact",
    "do not contact",
    "remove me",
    "take me off",
    "leave me alone",
    "unsubscribe",
)
INJECTION_PATTERNS = re.compile(  # instructions aimed at a model, not ordinary speech
    r"\b(ignore|disregard|forget)\s+(all\s+|any\s+)?(of\s+)?(the\s+|your\s+)?"
    r"(previous|prior|above|earlier)\s+(instructions?|prompts?|rules|directions)"
    r"|(^|[.!?\n]\s*)(system|assistant|developer)\s*:"
    r"|\byou\s+are\s+now\s+(in\s+)?(an?\s+)?(admin|administrator|developer|debug|god|jailbr\w*|unrestricted)\b"
    r"|\byou\s+are\s+now\s+in\s+[\w-]+\s+mode\b"
    r"|\bnew\s+instructions\s*:"
    r"|</?\s*(system|assistant|user|untrusted|instructions?)\s*>",
    re.IGNORECASE,
)


DOUBT = ("ambiguous", "contradictory")  # a safety read that is unsure either way still fires


def fires(field) -> bool:
    """A "yes", or a hedged or contradictory read, with evidence. Fails safe."""
    return found(field) or field.status in DOUBT


def safety_signals(safety: SafetyRead | None, primary: set[str] = frozenset()) -> list[str]:
    """Signals reported by either reader, in SIGNAL_REASONS order."""
    fired = set(primary)
    if safety is not None:
        fired |= {s for s in SAFETY_SIGNALS if fires(getattr(safety, s))}
    return [s for s in SIGNAL_REASONS if s in fired]


def hedged_signals(safety: SafetyRead | None) -> list[str]:
    """Signals the safety reader was unsure about or read both ways."""
    return [] if safety is None else [s for s in SAFETY_SIGNALS if getattr(safety, s).status in DOUBT]


def raise_signals(
    rt: Runtime,
    safety: SafetyRead | None,
    who: str,
    primary: set[str] = frozenset(),
    expected: set[str] = frozenset(),
) -> None:
    """Escalate every fired signal (safety first), except answers the script just asked for."""
    hedged = hedged_signals(safety)
    for signal in safety_signals(safety, primary):
        if signal in expected:
            continue
        how = " (safety reader unsure; escalated to be safe)" if signal in hedged and signal not in primary else ""
        rt.escalate(SIGNAL_REASONS[signal], f"{signal} reported by {who}{how}")


def medicaid_doubt(field) -> bool:
    """A Medicaid/Savings Program read that is anything but a clear "no". Fails safe."""
    return (field.status == "found" and field.value in ("yes", "not_sure")) or field.status in DOUBT


def patient_primary_signals(read: PatientReplyRead) -> set[str]:
    """Signals in a primary patient read: opt-out or wants-a-person intents, or a Medicaid answer not a clear "no"."""
    intents = {i.value for i in read.intents}
    signals = {s for s, intent in (("opts_out", "opt_out"), ("wants_person", "wants_person")) if intent in intents}
    if medicaid_doubt(read.medicaid_or_msp):
        signals.add("medicaid_or_msp")
    return signals


def keyword_opt_out(turns: list[Turn]) -> bool:
    """Backstop: an opt-out phrase in a patient or helper turn."""
    return any(
        any(k in normalise(t.text) for k in OPT_OUT_KEYWORDS) for t in turns if t.speaker in ("patient", "helper")
    )


def injection_turns(turns: list[Turn]) -> set[str]:
    """Ids of counterparty turns containing prompt-injection patterns.

    Matching runs on the text as the prompt renders it, NFKC-normalised, so
    zero-width and fullwidth look-alikes cannot hide a pattern.
    """
    return {
        t.turn_id
        for t in turns
        if t.speaker != "us" and INJECTION_PATTERNS.search(unicodedata.normalize("NFKC", fold_untrusted(t.text)))
    }


UNKNOWN = "unknown"


def _condition(field, downgraded: bool = False) -> str:
    """Normalised condition for comparison: not stated counts as "no", doubt as unknown.

    A doubtful or downgraded read is never "no": a supplier must not qualify on a
    condition nobody could read.
    """
    if downgraded or field.status in DOUBT:
        return UNKNOWN
    if field.status != "found" or field.value in ("", "no"):
        return "no"
    return field.value


def condition_disagreements(
    primary: SupplierCallRead, safety: SafetyRead, primary_downgraded=frozenset(), safety_downgraded=frozenset()
) -> list[str]:
    """Supplier-condition fields the two readers disagree on, or that either could not read clearly."""
    pairs = [(name, name, name) for name in SUPPLIER_CONDITIONS] + [("abn_required", "abn_required", "abn_requested")]
    both_need_info = _condition(primary.needs_info) == _condition(safety.needs_info) == "yes"
    disagree = []
    for name, a_name, b_name in pairs:
        a = _condition(getattr(primary, a_name), a_name in primary_downgraded)
        b = _condition(getattr(safety, b_name), b_name in safety_downgraded)
        if name == "needs_info_item" and not both_need_info:
            continue
        if UNKNOWN in (a, b):
            disagree.append(name)
        elif name == "other_condition":  # presence only, never wording
            if (a != "no") != (b != "no"):
                disagree.append(name)
        elif a != b:
            disagree.append(name)
    return disagree
