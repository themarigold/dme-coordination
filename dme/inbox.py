"""The advocate inbox: items that need a human, and the actions a human can take.

On a blind item Claude's proposal stays hidden until the human decides. Misses are
appended to a regressions file to become eval items.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from dme import facts
from dme.models import ApprovalActionType, Case, EscalationReason, Proposal
from dme.policy import ApprovalPolicy, Label
from dme.protocols import RerunHook

HumanAction = Literal["approve", "edit_approve", "reject", "supply_fact", "choose_action"]
NOT_STRUCTURED = {"why"}  # Proposal fields whose edit is cosmetic
ACTION_FIELDS = (
    "action",
    "param_party",
    "param_supplier",
    "param_items",
    "param_question",
    "param_time_phrase",
    "param_reason",
)  # what a chosen action is compared on


def structural_changes(before: Proposal, after: Proposal) -> list[str]:
    """Proposal fields that differ, ignoring cosmetic ones (the `why` wording)."""
    return [f for f in Proposal.model_fields if f not in NOT_STRUCTURED and getattr(before, f) != getattr(after, f)]


def same_action(proposed: Proposal, chosen: Proposal) -> bool:
    """Whether a human's chosen action matches Claude's on action and parameters (not why or cited facts)."""
    return all(getattr(proposed, f) == getattr(chosen, f) for f in ACTION_FIELDS)


class InboxItem(BaseModel):
    item_id: str
    case_id: str
    reason: EscalationReason
    approval_type: ApprovalActionType | None = None
    blocking_fact: str
    tried: list[str] = Field(default_factory=list)
    proposal: Proposal | None = None
    blind: bool = False
    created_at: datetime
    status: Literal["open", "resolved"] = "open"
    history: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def touch(self) -> str:
        """How this item appears in a scenario's expected human_touches list."""
        return f"{self.reason.value}:{self.approval_type}" if self.approval_type else self.reason.value

    @property
    def hidden(self) -> bool:
        """A blind item's proposal stays hidden until the human has decided."""
        return self.blind and not self.history

    def view(self) -> dict[str, Any]:
        """What the human is shown."""
        shown = self.model_dump(mode="json")
        if self.hidden:
            shown["proposal"] = None
        return shown


class Inbox:
    def __init__(
        self,
        case: Case,
        regressions_path: Path,
        *,
        rerun_hook: RerunHook | None = None,
        policy: ApprovalPolicy | None = None,
        check_hook: Callable[[InboxItem, Proposal], str] | None = None,
        supply_check: Callable[[InboxItem, str, str], str] | None = None,
    ) -> None:
        self.case = case
        self.regressions_path = Path(regressions_path)
        self.rerun_hook = rerun_hook
        self.policy = policy
        self.check_hook = check_hook  # why a person's action is not legal on an item now ("" if it is)
        self.supply_check = supply_check  # why a supplied fact is not taken on an item now ("" if it is)
        self.items: list[InboxItem] = []

    # Raising and reading

    def add(self, item: InboxItem) -> InboxItem:
        self.items.append(item)
        return item

    def next_id(self) -> str:
        return f"{self.case.case_id}-{len(self.items) + 1}"

    def get(self, item_id: str) -> InboxItem:
        for item in self.items:
            if item.item_id == item_id:
                return item
        raise KeyError(f"no inbox item {item_id!r}")

    def open_items(self) -> list[InboxItem]:
        return [i for i in self.items if i.status == "open"]

    def touches(self) -> list[str]:
        """Every item ever raised, in order (each is a human touch)."""
        return [i.touch for i in self.items]

    def update_proposal(self, item_id: str, proposal: Proposal) -> None:
        """Rules re-proposed on an open item (e.g. after a cited fact changed)."""
        self.get(item_id).proposal = proposal

    # Typed human actions

    def approve(self, item_id: str) -> None:
        """Approve Claude's proposal. Approving `hold_pending` keeps the item open."""
        item = self._visible_proposal(item_id, "approve")
        self._check_allowed(item, item.proposal)  # a supplier may have dropped out since it was proposed
        self._label(item, "approve", seen_proposal=True)
        keep_open = item.proposal.action == "hold_pending"
        self._finish(item, "approve", {"proposal": item.proposal.model_dump()}, keep_open=keep_open)

    def edit_approve(self, item_id: str, edits: dict[str, Any]) -> None:
        """Approve after editing the proposal. A miss only if a structured field changed."""
        item = self._visible_proposal(item_id, "edit")
        original = item.proposal
        assert original is not None
        edited = Proposal.model_validate({**original.model_dump(), **edits})
        self._check_allowed(item, edited)
        changed = structural_changes(original, edited)
        self._label(item, "edit_approve", seen_proposal=True, structured_change=bool(changed))
        if changed:
            self._regression(item, "edit_approve", {"edited": edited.model_dump(), "changed_fields": changed})
        item.proposal = edited
        self._finish(
            item,
            "edit_approve",
            {"proposal": edited.model_dump(), "changed_fields": changed},
            keep_open=edited.action == "hold_pending",
        )

    def reject(self, item_id: str, reason: str) -> None:
        """Reject Claude's proposal; the item stays open for another action."""
        item = self._visible_proposal(item_id, "reject")
        self._label(item, "reject", seen_proposal=True)
        self._regression(item, "reject", {"rejection_reason": reason})
        self._finish(item, "reject", {"rejection_reason": reason}, keep_open=True)

    def supply_fact(self, item_id: str, fact_id: str, value: str, source: str) -> None:
        """A human supplies a fact and its source, which resolves the item.

        Non-canonical values ("Nov 20", "two", "y") and facts the rules do not take
        on this item are refused. An approval item stays open, because only proceed
        or hold decides its gated action.
        """
        item = self._open(item_id)
        if not facts.is_known(fact_id):
            raise KeyError(f"unknown fact id {fact_id!r}")
        if not source.strip():
            raise ValueError("a supplied fact needs its source")
        problem = facts.canonical_problem(fact_id, value)  # the rules parse facts, so only canonical values go in
        if problem:
            raise ValueError(f"supplied fact refused: {problem}")
        if self.supply_check is not None and (problem := self.supply_check(item, fact_id, value)):
            raise ValueError(f"supplied fact refused: {problem}")
        self.case.set(fact_id, value)
        self._finish(
            item,
            "supply_fact",
            {"fact_id": fact_id, "value": value, "source": source},
            keep_open=item.approval_type is not None,
        )

    def choose_action(self, item_id: str, chosen: Proposal) -> None:
        """The human picks an allowed action, which resolves the item.

        It counts as a hit against Claude's proposal when action and parameters
        match, else a miss. This is the only way to decide a blind item.
        """
        item = self._open(item_id)
        self._check_allowed(item, chosen)
        if item.proposal is not None:
            seen = not item.hidden
            if same_action(item.proposal, chosen):
                self._label(item, "approve", seen_proposal=seen)
            else:
                self._label(item, "reject", seen_proposal=seen)
                self._regression(item, "choose_action", {"chosen": chosen.model_dump(), "blind": not seen})
        self._finish(
            item, "choose_action", {"proposal": chosen.model_dump()}, keep_open=chosen.action == "hold_pending"
        )

    # Internals

    def _check_allowed(self, item: InboxItem, chosen: Proposal) -> None:
        """Refuse a human choice that is not legal on this item now (the legality checks Claude's proposals get)."""
        if self.check_hook is None:
            return
        problem = self.check_hook(item, chosen)
        if problem:
            raise ValueError(f"{chosen.action} refused on {item.item_id}: {problem}")

    def _open(self, item_id: str) -> InboxItem:
        item = self.get(item_id)
        if item.status != "open":
            raise ValueError(f"{item_id} is already resolved")
        return item

    def _visible_proposal(self, item_id: str, verb: str) -> InboxItem:
        """An open item whose proposal the human has been shown."""
        item = self._open(item_id)
        if item.proposal is None:
            raise ValueError(f"{item_id} has no proposal to {verb}")
        if item.hidden:
            raise ValueError(f"{item_id} is a blind item: decide with choose_action")
        return item

    def _label(self, item: InboxItem, label: Label, *, seen_proposal: bool, structured_change: bool = False) -> None:
        if self.policy is None:
            return
        action_type: ApprovalActionType = item.approval_type or "claude_next_action"
        self.policy.record(action_type, label, seen_proposal=seen_proposal, structured_change=structured_change)

    def _regression(self, item: InboxItem, action: str, detail: dict[str, Any]) -> None:
        record = {
            "case_id": item.case_id,
            "item_id": item.item_id,
            "reason": item.reason.value,
            "approval_type": item.approval_type,
            "blocking_fact": item.blocking_fact,
            "proposal": item.proposal.model_dump() if item.proposal else None,
            "human_action": action,
            **detail,
        }
        self.regressions_path.parent.mkdir(parents=True, exist_ok=True)
        with self.regressions_path.open("a") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")

    def _finish(self, item: InboxItem, action: HumanAction, detail: dict[str, Any], keep_open: bool = False) -> None:
        item.history.append({"action": action, **detail})
        if not keep_open:
            item.status = "resolved"
        if self.rerun_hook is not None:
            self.rerun_hook(
                self.case,
                {
                    "type": "human_action",
                    "action": action,
                    "item_id": item.item_id,
                    "reason": item.reason.value,
                    "approval_type": item.approval_type,
                    "item_open": item.status == "open",
                    **detail,
                },
            )
