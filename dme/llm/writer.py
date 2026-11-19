"""Patient text: Claude drafts with placeholders; the rules check and fill the draft.

A rejected draft gets one retry, then the rules use their own template. Patient
questions never reach Claude: they are answered from a fixed FAQ or go to a human,
and a question about money always goes to a human.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from dme.drafts import APOSTROPHES, check_draft
from dme.llm.client import Caller
from dme.llm.context import build_messages
from dme.models import LlmUnavailable, MessagePurpose, PatientMessageDraft
from dme.protocols import CallContext

WRITER_EFFORT = "low"
CALL = "write_patient_message"

# (trigger phrases, answer). Answers contain no placeholders.
FAQ: list[tuple[tuple[str, ...], str]] = [
    (
        ("who are you", "who is this", "what is mira", "who is mira", "why are you contacting", "why are you calling"),
        "We are Mira Mace. We coordinate your wheelchair order between your doctor's office and a Medicare "
        "supplier, so you do not have to make those calls yourself.",
    ),
    (
        ("be home", "be there for", "be at home", "someone home"),
        "Yes, someone should be home for the delivery. The supplier will agree a time with you before it comes.",
    ),
    (
        ("show me how", "how to use", "teach me", "instructions"),
        "Yes. The supplier will show you how to use the wheelchair safely when it is delivered.",
    ),
    (
        ("repair", "broken", "breaks", "fix it", "doesn't fit", "does not fit", "too big", "too small"),
        "The supplier handles fitting and repairs. They will give you their number at delivery, and you can "
        "also tell us and we will contact them for you.",
    ),
    (
        ("own it", "keep it", "rent", "rental", "give it back", "return it"),
        "Medicare rents the chair month by month. Once the rental period is complete, the chair is yours to keep.",
    ),
    (
        ("share my", "my information", "my details", "privacy", "who will see", "who sees"),
        "We share only what is needed, only with your doctor's office and the supplier, and only with your permission.",
    ),
    (
        ("choose", "pick the supplier", "pick a supplier", "different supplier", "own supplier", "supplier i like"),
        "Yes, you can choose. Tell us the supplier you prefer and we will contact them before any other.",
    ),
]


MONEY_WORDS = re.compile(
    r"\$|\b(costs?|costing|price[sd]?|pay|paying|paid|payment|charges?|charged|bills?|billed|billing|money|"
    r"dollars?|fees?|expensive|cheap|afford|copays?|co-pays?|coinsurance|deductibles?|owe)\b"
)


def faq_answer(question: str) -> str | None:
    """The FAQ answer for a question, or None for a human; a question that mentions money always goes to one."""
    q = re.sub(r"\s+", " ", question.translate(APOSTROPHES).lower())
    if MONEY_WORDS.search(q):
        return None
    for triggers, answer in FAQ:
        if any(re.search(rf"\b{re.escape(t)}\b", q) for t in triggers):
            return answer
    return None


@dataclass
class WriterResult:
    """A draft (placeholders intact) or FAQ answer, with where it came from.

    Empty text with `needs_inbox` means no automated answer exists: raise an inbox item.
    """

    text: str
    source: Literal["claude", "claude_regenerated", "faq", "needs_inbox", "rejected", "unavailable"]
    needs_inbox: bool = False
    rejections: list[str] = field(default_factory=list)
    unavailable: LlmUnavailable | None = None

    def as_draft(self) -> PatientMessageDraft | LlmUnavailable:
        return self.unavailable if self.unavailable is not None else PatientMessageDraft(text=self.text)


def write_patient_message(
    caller: Caller, purpose: MessagePurpose, facts: dict[str, str], ctx: CallContext, effort: str = WRITER_EFFORT
) -> WriterResult:
    if purpose == "answer_question":
        answer = faq_answer(ctx.goal)
        if answer is None:
            return WriterResult(text="", source="needs_inbox", needs_inbox=True)
        return WriterResult(text=answer, source="faq")

    call_ctx = ctx.model_copy(update={"facts": dict(facts)})
    rejections: list[str] = []
    for attempt in (1, 2):
        extra = f"Message purpose: {purpose}"
        if rejections:
            extra += (
                "\n\nYour previous draft was rejected for: " + rejections[-1] + ". Write it again without that problem."
            )
        system, user = build_messages(CALL, call_ctx, None, extra=extra)
        out = caller(CALL, PatientMessageDraft, system, user, effort).output
        if isinstance(out, LlmUnavailable):
            return WriterResult(text="", source="unavailable", rejections=rejections, unavailable=out)
        reasons = check_draft(out.text, facts, purpose)
        if not reasons:
            return WriterResult(
                text=out.text, source="claude" if attempt == 1 else "claude_regenerated", rejections=rejections
            )
        rejections.append("; ".join(reasons))
    failed = LlmUnavailable(call=CALL, reason="parse_error", detail="both drafts rejected: " + " | ".join(rejections))
    return WriterResult(text="", source="rejected", rejections=rejections, unavailable=failed)
