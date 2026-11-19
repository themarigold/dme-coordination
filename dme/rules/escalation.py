"""Raising an escalation: one inbox item per open (reason, approval type, blocking fact)."""

from __future__ import annotations

from datetime import datetime

from dme.inbox import Inbox, InboxItem
from dme.models import ApprovalActionType, Case, EscalationReason, Proposal


def raise_escalation(
    case: Case,
    reason: EscalationReason,
    blocking_fact: str,
    tried: list[str],
    inbox: Inbox,
    *,
    now: datetime,
    approval_type: ApprovalActionType | None = None,
    proposal: Proposal | None = None,
    blind: bool = False,
) -> InboxItem:
    """Put the case in front of a human; an identical open item is returned instead of a second touch."""
    for item in inbox.open_items():
        if (item.reason, item.approval_type, item.blocking_fact) == (reason, approval_type, blocking_fact):
            return item
    return inbox.add(
        InboxItem(
            item_id=inbox.next_id(),
            case_id=case.case_id,
            reason=reason,
            approval_type=approval_type,
            blocking_fact=blocking_fact,
            tried=list(tried),
            proposal=proposal,
            blind=blind,
            created_at=now,
        )
    )
