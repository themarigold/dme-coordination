"""Asks Claude for the next action: one allowed choice, a one-sentence reason, and the facts it relies on.

Legality and fact checks happen in the rules.
"""

from __future__ import annotations

from dme.llm.client import Caller
from dme.llm.context import build_messages
from dme.models import LlmUnavailable, Proposal
from dme.protocols import CallContext

PROPOSER_EFFORT = "medium"
CALL = "propose_next_action"


def propose_next_action(
    caller: Caller, case_summary: str, allowed_actions: list[str], ctx: CallContext, effort: str = PROPOSER_EFFORT
) -> Proposal | LlmUnavailable:
    allowed = "\n".join(f"- {a}" for a in allowed_actions) or "(none)"
    extra = f"Allowed actions (pick exactly one):\n{allowed}"
    if ctx.reason_code:
        extra = f"Inbox item reason code: {ctx.reason_code}\n\n{extra}"
    # The case summary is built by code but may quote counterparties: it is untrusted.
    system, user = build_messages(
        CALL, ctx, case_summary, extra=extra, source="the case summary (may quote other people)"
    )
    return caller(CALL, Proposal, system, user, effort).output  # type: ignore[return-value]
