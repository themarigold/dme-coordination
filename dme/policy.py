"""The approval switch: one gated/autonomous setting per approval action type.

Only a human loosens a switch; the system tightens it after a miss or a downstream
failure. A hit counts toward the streak only if it was decided blind or later
confirmed downstream, so rubber-stamping cannot inflate the record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, get_args

from dme.models import ApprovalActionType

Mode = Literal["gated", "autonomous"]
Label = Literal["approve", "edit_approve", "reject"]
CONFIDENCE = 0.95


def lower_bound(successes_in_a_row: int) -> float:
    """95% one-sided lower bound on the success rate after n successes and no failures: 0.05 ** (1/n).

    5 -> 0.549, 20 -> 0.861, about 59 for 0.95.
    """
    n = successes_in_a_row
    return 0.0 if n <= 0 else (1 - CONFIDENCE) ** (1 / n)


@dataclass
class Decision:
    action_type: ApprovalActionType
    label: Label
    miss: bool
    seen_proposal: bool


@dataclass
class Switch:
    mode: Mode = "gated"
    streak: int = 0
    pending_confirmation: int = 0  # seen-proposal hits awaiting a downstream outcome
    history: list[str] = field(default_factory=list)  # human-readable record of changes


class ApprovalPolicy:
    def __init__(self, human_settings: dict[str, Mode] | None = None) -> None:
        """`human_settings` (from the CLI's --policy) counts as a human loosening."""
        self.switches: dict[str, Switch] = {t: Switch() for t in get_args(ApprovalActionType)}
        self.decisions: list[Decision] = []
        for action_type, mode in (human_settings or {}).items():
            if mode == "autonomous":
                self.loosen(action_type, actor="human")

    def is_gated(self, action_type: ApprovalActionType) -> bool:
        return self.switches[action_type].mode == "gated"

    def lower_bound(self, action_type: ApprovalActionType) -> float:
        return lower_bound(self.switches[action_type].streak)

    def record(
        self,
        action_type: ApprovalActionType,
        label: Label,
        *,
        seen_proposal: bool,
        structured_change: bool = False,
    ) -> Decision:
        """Record a human decision on a Claude proposal."""
        miss = label == "reject" or (label == "edit_approve" and structured_change)
        decision = Decision(action_type, label, miss, seen_proposal)
        self.decisions.append(decision)
        switch = self.switches[action_type]
        if miss:
            self.tighten(action_type, f"human {label}")
        elif seen_proposal:
            switch.pending_confirmation += 1
        else:
            switch.streak += 1  # blind match
        return decision

    def confirm_outcome(self, action_type: ApprovalActionType, ok: bool, detail: str = "") -> None:
        """A downstream outcome for an approved action: success moves a pending hit to the streak; failure tightens."""
        switch = self.switches[action_type]
        if not ok:
            self.tighten(action_type, f"downstream failure: {detail}")
        elif switch.pending_confirmation > 0:
            switch.pending_confirmation -= 1
            switch.streak += 1

    def tighten(self, action_type: ApprovalActionType, reason: str) -> None:
        """System or human: back to gated, streak reset."""
        switch = self.switches[action_type]
        switch.mode, switch.streak, switch.pending_confirmation = "gated", 0, 0
        switch.history.append(f"tightened: {reason}")

    def loosen(self, action_type: str, *, actor: str) -> None:
        """Only a human may loosen a switch."""
        if actor != "human":
            raise PermissionError("the system never loosens its own approval switch")
        self.switches[action_type].mode = "autonomous"
        self.switches[action_type].history.append("loosened by human")
