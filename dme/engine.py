"""The deterministic event loop for one case.

An event is stale once a newer attempt of the same action exists; stale events are
logged but not acted on. Hitting a cap (steps, Claude calls, business days) stalls
the engine until a person acts on the case_stalled item.
"""

from __future__ import annotations

import heapq
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from dme import clock
from dme.inbox import Inbox, InboxItem
from dme.models import Actor, ApprovalActionType, Case, EscalationReason, LogLine, Proposal
from dme.rules.escalation import raise_escalation
from dme.world import UnfiredFollowUps, World, WorldEvent, WorldUnscripted

MAX_STEPS = 500
MAX_CLAUDE_CALLS = 200
MAX_BUSINESS_DAYS = 30
DEFAULT_PRIORITY = 5  # lower runs first among events due at the same moment
STALE_NEWS_KINDS = {"delivered"}


@dataclass(order=True)
class Event:
    due: datetime
    priority: int
    seq: int
    kind: str = field(compare=False)
    payload: dict[str, Any] = field(compare=False, default_factory=dict)
    attempt_key: str = field(compare=False, default="")  # "" = never stale
    attempt: int = field(compare=False, default=0)


Handler = Callable[["Engine", Event], None]


class Engine:
    def __init__(
        self,
        case: Case,
        start: datetime,
        inbox: Inbox,
        *,
        world: World | None = None,
        seed: int = 7,
        log_path: Path | None = None,
    ) -> None:
        self.case = case
        self.start = self.now = start.astimezone(clock.TZ)
        self.inbox = inbox
        self.world = world
        self.rng = random.Random(seed)  # used only for blind sampling
        self.log_path = log_path
        self.log_lines: list[LogLine] = []
        self.handlers: dict[str, list[tuple[str, Handler]]] = {}  # kind -> [(prefix, handler)]
        self.sent: set[tuple[str, str, int]] = set()  # idempotency keys
        self.steps = 0
        self.claude_calls = 0
        self.stalled = False
        self.stall_item = ""  # the case_stalled item; a person's action on it re-arms the engine
        self.steps_base = 0  # caps count from here; rearm moves the bases forward
        self.claude_base = 0
        self.never_stale: set[str] = set()  # counterparties (people texting back) whose replies are never stale
        self.after_step: Callable[[], None] | None = None  # run after every event, stale or not
        self.deadline = clock.add_business_days(self.start, MAX_BUSINESS_DAYS)
        self._heap: list[Event] = []
        self._seq = 0

    # Registration and scheduling

    def on(self, kind: str, handler: Handler, counterparty_prefix: str | None = None) -> None:
        """Register a handler for `kind`, optionally only for counterparties starting with `counterparty_prefix`."""
        prefix = counterparty_prefix or ""
        registered = self.handlers.setdefault(kind, [])
        if any(p == prefix for p, _ in registered):
            raise ValueError(f"handler for {kind!r} with prefix {prefix!r} already registered")
        registered.append((prefix, handler))

    def handlers_for(self, event: Event) -> list[Handler]:
        """Matching handlers, most specific prefix first (registration order on ties)."""
        counterparty = event.payload.get("counterparty", "")
        matching = [(p, h) for p, h in self.handlers.get(event.kind, []) if counterparty.startswith(p)]
        return [h for _, h in sorted(matching, key=lambda ph: -len(ph[0]))]

    def schedule(
        self,
        due: datetime,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        priority: int = DEFAULT_PRIORITY,
        attempt_key: str = "",
        attempt: int = 0,
    ) -> Event:
        """Queue an event. A due time in the past runs now."""
        self._seq += 1
        event = Event(max(due, self.now), priority, self._seq, kind, payload or {}, attempt_key, attempt)
        heapq.heappush(self._heap, event)
        return event

    def next_attempt(self, party: str, action: str) -> int:
        """A new attempt id for (party, action); pending events of older attempts become stale."""
        key = f"{party}:{action}"
        self.case.attempts[key] = self.case.attempts.get(key, 0) + 1
        return self.case.attempts[key]

    def is_stale(self, event: Event) -> bool:
        if event.payload.get("counterparty") in self.never_stale:
            return False
        return bool(event.attempt_key) and self.case.attempts.get(event.attempt_key, 0) != event.attempt

    def pending(self) -> list[Event]:
        """Queued events that will still be acted on (stale ones are only logged)."""
        return [e for e in self._heap if not self.is_stale(e)]

    def sample_blind(self, rate: float) -> bool:
        """Whether an approval item is decided blind (proposal hidden)."""
        return self.rng.random() < rate

    # Outbound

    def send(self, counterparty: str, action: str, attempt: int, payload: dict[str, Any] | None = None) -> bool:
        """Carry out one outbound action over the mock transport; False for a duplicate or a frozen case.

        The attempt must come from next_attempt, or every reply would be stale.
        """
        if not 1 <= attempt <= self.case.attempts.get(f"{counterparty}:{action}", 0):
            raise ValueError(f"attempt {attempt} for {counterparty}:{action} was not issued by next_attempt")
        key = (self.case.case_id, f"{counterparty}|{action}", attempt)
        if self.case.frozen:
            self.log("rule", "outbound_blocked", f"{counterparty} {action} #{attempt} blocked: case frozen")
            return False
        if key in self.sent:
            self.log("rule", "duplicate_suppressed", f"{counterparty} {action} #{attempt} already sent")
            return False
        self.sent.add(key)
        self.log(
            "rule",
            "outbound",
            f"{action} -> {counterparty} (attempt {attempt})",
            data={"counterparty": counterparty, "action": action, "attempt": attempt, **(payload or {})},
        )
        if self.world is None:
            return True
        try:
            responses = self.world.respond(counterparty, action, attempt, self.now)
        except WorldUnscripted as miss:
            if self.world.mode == "test":
                raise
            self.escalate(
                EscalationReason.WORLD_UNSCRIPTED,
                f"no scripted response for {miss}",
                [f"{action} -> {counterparty} (attempt {attempt})"],
            )
            return True
        for response in responses:
            self._schedule_world(response, f"{counterparty}:{action}", attempt)
        return True

    def _schedule_world(self, response: WorldEvent, attempt_key: str, attempt: int) -> None:
        payload = {
            "counterparty": response.counterparty,
            "action": response.action,
            "attempt": response.attempt,
            "row_key": response.row_key,
            "row": response.row,
        }
        self.schedule(response.due, response.row.event, payload, attempt_key=attempt_key, attempt=attempt)

    # Log and escalation

    def log(self, actor: Actor, kind: str, summary: str, **fields: Any) -> LogLine:
        """Append one provenance line. A line with an llm_call counts its requests toward the Claude cap."""
        line = LogLine(
            seq=len(self.log_lines) + 1,
            sim_time=self.now,
            case_id=self.case.case_id,
            actor=actor,
            kind=kind,
            summary=summary,
            **fields,
        )
        self.log_lines.append(line)
        if line.llm_call:  # one line per call, which may carry several requests
            self.claude_calls += int(line.data.get("requests", 1))
        if self.log_path is not None:
            with self.log_path.open("a") as f:
                f.write(line.model_dump_json() + "\n")
        return line

    def escalate(
        self,
        reason: EscalationReason,
        blocking_fact: str,
        tried: list[str],
        *,
        approval_type: ApprovalActionType | None = None,
        proposal: Proposal | None = None,
        blind: bool = False,
    ) -> InboxItem:
        item = raise_escalation(
            self.case,
            reason,
            blocking_fact,
            tried,
            self.inbox,
            now=self.now,
            approval_type=approval_type,
            proposal=proposal,
            blind=blind,
        )
        self.log(
            "rule",
            "escalation",
            f"{item.touch}: {blocking_fact}",
            data={"item_id": item.item_id, "reason": reason.value, "approval_type": approval_type},
        )
        return item

    # The loop

    def _stall(self, why: str) -> None:
        self.stalled = True
        item = self.escalate(
            EscalationReason.CASE_STALLED, why, [f"{self.steps} steps, {self.claude_calls} Claude calls"]
        )
        self.stall_item = item.item_id

    def claude_left(self) -> int:
        """Claude requests the cap still allows (from the last re-arm)."""
        return MAX_CLAUDE_CALLS - (self.claude_calls - self.claude_base)

    def rearm(self, why: str) -> None:
        """Restart after a person acts on the stall item, with every cap counting afresh.

        The new deadline counts from the next queued event if that is later than now,
        so the event the business-day cap stopped can run.
        """
        self.stalled = False
        self.stall_item = ""
        self.steps_base, self.claude_base = self.steps, self.claude_calls
        upcoming = min((e.due for e in self._heap), default=self.now)
        self.deadline = clock.add_business_days(max(self.now, upcoming), MAX_BUSINESS_DAYS)
        self.log(
            "human",
            "engine_rearmed",
            f"engine re-armed after {why}: fresh caps from now "
            f"({MAX_STEPS} steps, {MAX_CLAUDE_CALLS} Claude calls, "
            f"{MAX_BUSINESS_DAYS} business days)",
        )

    def step(self) -> bool:
        """Run the next event. False when there is nothing (more) to run."""
        if not self._heap or self.case.closed or self.stalled:
            return False
        if self.steps - self.steps_base >= MAX_STEPS:
            self._stall(f"step cap {MAX_STEPS} reached")
            return False
        if self.claude_left() <= 0:
            self._stall(f"Claude call cap {MAX_CLAUDE_CALLS} reached")
            return False
        if self._heap[0].due > self.deadline:
            self._stall(f"{MAX_BUSINESS_DAYS} business-day cap reached")
            return False

        event = heapq.heappop(self._heap)
        self.steps += 1
        self.now = event.due
        row_key = event.payload.get("row_key", "")
        if row_key:
            assert self.world is not None  # only the world schedules events with a row_key
            self.log(
                "world",
                f"world_{event.kind}",
                f"{event.payload['counterparty']}: {event.kind}",
                data={"row_key": row_key},
            )
            # The world carries on regardless of what we do with this event.
            for follow_up in self.world.follow_ups(event.payload["counterparty"], event.kind, self.now):
                self._schedule_world(follow_up, event.attempt_key, event.attempt)

        if self.is_stale(event):
            superseded = f"{event.attempt_key} #{event.attempt}"
            self.log("rule", "stale_ignored", f"{event.kind} for superseded attempt {superseded}")
            if event.kind in STALE_NEWS_KINDS:
                who = event.payload.get("counterparty", "unknown")
                self.escalate(
                    EscalationReason.STALE_NEWS, f"{who} reported {event.kind} on a superseded attempt", [superseded]
                )
        else:
            handlers = self.handlers_for(event)
            if not handlers:
                who = event.payload.get("counterparty", "")
                raise LookupError(f"no handler registered for event kind {event.kind!r} (counterparty {who!r})")
            for handler in handlers:
                handler(self, event)
        if self.after_step is not None:
            self.after_step()
        return True

    def run(self) -> str:
        """Run until idle, closed or stalled; returns which."""
        while self.step():
            pass
        if self.stalled:
            return "stalled"
        self._report_unfired_follow_ups()
        return "closed" if self.case.closed else "idle"

    def _report_unfired_follow_ups(self) -> None:
        if self.world is None:
            return
        unfired = self.world.unfired_follow_ups()
        if not unfired:
            return
        if self.world.mode == "test":
            raise UnfiredFollowUps(", ".join(unfired))
        self.log("world", "world_warning", f"scripted follow-ups never fired: {', '.join(unfired)}")
