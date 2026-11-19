"""The mock world: scripted counterparty responses for one scenario.

Rows are keyed by (counterparty, action, attempt), with an `attempt: default` fallback;
`after:<event>` rows are follow-ups the counterparty starts itself. The engine only
receives events from here and never reads hidden facts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml

from dme import clock
from dme.models import Case, HumanDecision, Proposal, Scenario, WorldRow

ROOT = Path(__file__).resolve().parent.parent  # repository root; scenario paths are relative to it


class WorldUnscripted(LookupError):
    """No world row (and no default row) for a key."""


class UnfiredFollowUps(RuntimeError):
    """Scripted `after:` rows that never fired: the scenario and the run disagree."""


@dataclass(frozen=True)
class WorldEvent:
    due: datetime
    row: WorldRow
    row_key: str  # "counterparty|action|attempt#i", the key offline reads are looked up by
    counterparty: str
    action: str
    attempt: int


def parse_delay(delay: str, start: datetime) -> datetime:
    """ "<n>m" wall-clock minutes, "<n>h" business hours, "<n>bd" business days."""
    m = re.fullmatch(r"\s*(\d+)\s*(m|h|bd)\s*", delay)
    if not m:
        raise ValueError(f"bad delay {delay!r}; expected <n>m, <n>h or <n>bd")
    n, unit = int(m.group(1)), m.group(2)
    if unit == "m":
        return start + timedelta(minutes=n)
    if unit == "h":
        return clock.add_business_hours(start, n)
    return clock.add_business_days(start, n)


def load_scenario(path: Path | str) -> Scenario:
    return Scenario.model_validate(yaml.safe_load(Path(path).read_text()))


def row_keys(scenario: Scenario) -> dict[str, Any]:
    """row_key -> that row's expected_reads, numbered the same way the world numbers events."""
    seen: dict[tuple[str, str, str], int] = {}
    keys = {}
    for row in scenario.rows:
        key = (row.counterparty, row.action, str(row.attempt))
        seen[key] = seen.get(key, 0) + 1
        keys[f"{row.counterparty}|{row.action}|{row.attempt}#{seen[key]}"] = row.expected_reads
    return keys


def load_case(scenario: Scenario, root: Path = ROOT) -> Case:
    seed = yaml.safe_load((root / scenario.case_file).read_text())
    return Case(case_id=seed["case_id"], facts=seed["facts"])


def load_directory(scenario: Scenario, root: Path = ROOT) -> list[dict]:
    return yaml.safe_load((root / scenario.directory_file).read_text())["suppliers"]


class World:
    def __init__(self, scenario: Scenario, mode: Literal["test", "demo"] = "test") -> None:
        self.scenario = scenario
        self.mode = mode
        self._event_counts: dict[tuple[str, str], int] = {}  # (counterparty, event) -> occurrences
        self._fired: set[int] = set()  # indexes (in scenario.rows) of rows that have been used

    def _rows(self, counterparty: str, action: str, attempt: int) -> tuple[list[WorldRow], str]:
        """Rows for the exact key, else the default rows; plus the attempt label used in row keys."""
        for label in (attempt, "default"):
            found = [
                (i, r)
                for i, r in enumerate(self.scenario.rows)
                if (r.counterparty, r.action, r.attempt) == (counterparty, action, label)
            ]
            if found:
                self._fired.update(i for i, _ in found)
                return [r for _, r in found], str(label)
        return [], ""

    def unfired_follow_ups(self) -> list[str]:
        """`counterparty|after:<event>|attempt` of every follow-up row that never fired."""
        return [
            f"{r.counterparty}|{r.action}|{r.attempt}"
            for i, r in enumerate(self.scenario.rows)
            if r.action.startswith("after:") and i not in self._fired
        ]

    def _events(self, counterparty: str, action: str, attempt: int, now: datetime) -> list[WorldEvent]:
        rows, label = self._rows(counterparty, action, attempt)
        events, due = [], now
        for i, row in enumerate(rows, start=1):
            due = parse_delay(row.delay, due)
            if row.event != "never":
                key = f"{counterparty}|{action}|{label}#{i}"
                events.append(WorldEvent(due, row, key, counterparty, action, attempt))
        return events

    def respond(self, counterparty: str, action: str, attempt: int, now: datetime) -> list[WorldEvent]:
        """The scripted responses to one outbound action. A `never` row means silence."""
        if not self._rows(counterparty, action, attempt)[0]:
            raise WorldUnscripted(f"{counterparty}|{action}|{attempt}")
        return self._events(counterparty, action, attempt, now)

    def follow_ups(self, counterparty: str, event: str, now: datetime) -> list[WorldEvent]:
        """Counterparty-initiated events chained on `event` (rows with action `after:<event>`); none is not a miss."""
        key = (counterparty, event)
        self._event_counts[key] = self._event_counts.get(key, 0) + 1
        return self._events(counterparty, f"after:{event}", self._event_counts[key], now)

    # Offline stand-ins

    def human_decision(self, reason_code: str, nth: int) -> HumanDecision | None:
        for d in self.scenario.human_decisions:
            if (d.reason_code, d.nth) == (reason_code, nth):
                return d
        return None

    def scripted_proposal(self, reason_code: str, nth: int) -> Proposal | None:
        for p in self.scenario.scripted_proposals:
            if (p.reason_code, p.nth) == (reason_code, nth):
                return Proposal.model_validate(p.proposal)
        return None

    def scripted_draft(self, purpose: str, n: int) -> str | None:
        return self.scenario.scripted_drafts.get(f"{purpose}#{n}")
