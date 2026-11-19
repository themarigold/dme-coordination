"""What a run prints: headline, human touches, a tagged timeline, and read divergences.

Timeline tags: WORLD·MOCK (the mock world), TYPED (a reply a person typed),
OUT·MOCK (what we sent), CLAUDE·live|replay, HUMAN, and RULE (what the rules decided).
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from dme.models import SUPPLIER_CONDITIONS, LogLine, Scenario
from dme.rules.runtime import Runtime
from dme.world import row_keys

CALL_ACTIONS = {"qualify_call", "reconfirm_call", "confirm_call", "status_call", "first_contact", "helper_contact"}


def claude_cost(rt: Runtime) -> tuple[float, float]:
    """(spent live, replayed): cost of requests that reached the API, and original cost of replayed responses."""
    lines = [l for l in rt.engine.log_lines if l.llm_call]
    live = sum(l.cost_usd for l in lines if l.llm_mode == "live")
    replayed = sum(l.cost_usd for l in lines if l.llm_mode == "replay")
    return round(live, 4), round(replayed, 4)


def phone_calls(rt: Runtime) -> int:
    return sum(1 for l in rt.engine.log_lines if l.kind == "outbound" and l.data.get("action") in CALL_ACTIONS)


def headline(rt: Runtime, wall_s: float) -> str:
    """One line: N simulated days, X calls, Y Claude calls, Z human touches, cost, wall time."""
    out = rt.outcome()
    live, replayed = claude_cost(rt)
    cost = f"${live:.2f} live" + (f" (+ ${replayed:.2f} replayed, not spent)" if replayed else "")
    return (
        f"{rt.case.get('patient.name') or rt.case.case_id}: {out['terminal_state']} after "
        f"{out['business_days']} simulated business days, {phone_calls(rt)} calls, "
        f"{out['claude_calls']} Claude calls, {len(out['human_touches'])} human touches; "
        f"cost {cost}; wall time {wall_s:.0f} s"
    )


def approval_switches(rt: Runtime) -> list[str]:
    """Each approval switch at the end of the run: mode, streak, and the 95% lower bound that streak supports."""
    return [
        f"{action_type}: {switch.mode}, streak {switch.streak}, "
        f"95% lower bound {rt.policy.lower_bound(action_type):.2f}"  # type: ignore[arg-type]
        for action_type, switch in rt.policy.switches.items()
    ]


def touches(rt: Runtime) -> list[str]:
    """Each human touch and why, with Claude's proposal (action, why, cited facts)."""
    lines = []
    for item in rt.inbox.items:
        line = f"{item.touch}: {item.blocking_fact}"
        if item.proposal is not None:
            cited = ", ".join(f"{f.fact_id}={f.value}" for f in item.proposal.facts_used)
            line += f" | Claude proposes {item.proposal.action}: {item.proposal.why} [{cited}]"
        lines.append(line)
    return lines


def _tag(line: LogLine) -> str:
    if line.kind == "typed_reply":
        return "TYPED"
    if line.actor == "claude":
        return f"CLAUDE·{line.llm_mode or 'off'}"
    if line.actor == "world":
        return "WORLD·MOCK"
    if line.actor == "human":
        return "HUMAN"
    return "OUT·MOCK" if line.kind == "outbound" else "RULE"


def timeline(rt: Runtime) -> list[str]:
    lines = []
    for line in rt.engine.log_lines:
        cost = f" (${line.cost_usd:.3f}, {line.latency_ms} ms)" if line.llm_call and line.cost_usd else ""
        lines.append(f"{line.sim_time:%a %b %d %H:%M} [{_tag(line)}] {line.kind}: {line.summary}{cost}")
    return lines


# Live reads against the scenario's expected_reads


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip().lower()


def _expected(spec: Any) -> tuple[str, str]:
    """(status, value) from an expected_reads entry (full dict or short list form)."""
    if isinstance(spec, dict):
        return spec.get("status", ""), _norm(spec.get("value", ""))
    status = "ambiguous" if len(spec) > 3 and spec[3] == "ambiguous" else "found"
    return status, _norm(spec[2])


NO_IS_SILENCE = set(SUPPLIER_CONDITIONS) | {"abn_required", "has_changes"}
# Values the rules treat alike for a field: a mismatch between them stays visible as
# "minor", not "decision". The PCP office's reply to our item-code question: "other"
# and "will_send_by" both leave the answered code and the nudge clock as they are.
SAME_EFFECT: dict[tuple[str, str], set[str]] = {("read_pcp_reply", "outcome"): {"other", "will_send_by"}}
FREE_TEXT = {"TextRead"}  # wording may differ; reported, but not a decision difference


class ReadChecker:
    """Compares each live read with the scenario's expected reads for that row.

    Each divergence is a "decision" (status or value differs), "minor" (values the
    rules treat alike), "wording" (free text worded differently) or "unlisted" (a
    field the scenario leaves empty came back with a value).
    """

    def __init__(self, scenario: Scenario) -> None:
        self.expected: dict[str, Any] = row_keys(scenario)
        self.divergences: list[dict[str, Any]] = []

    def __call__(self, reader: str, row_key: str, raw: BaseModel, validated: BaseModel) -> None:
        scripted = self.expected.get(row_key)
        if scripted is None:
            return
        side = scripted.safety if reader == "check_safety_and_conditions" else scripted.primary
        for name in type(validated).model_fields:  # fields the scenario does not list should be empty
            field = getattr(validated, name)
            if name in side or name == "intents" or field.status == "not_stated":
                continue
            if _norm(field.value) == "no" and (reader == "check_safety_and_conditions" or name in NO_IS_SILENCE):
                continue  # for signals and conditions a "no" is the same answer as not stated
            raw_field = getattr(raw, name)
            self.divergences.append(
                {
                    "call": reader,
                    "row_key": row_key,
                    "field": name,
                    "expected": {"status": "not_stated (unlisted)"},
                    "live": {
                        "status": raw_field.status,
                        "value": raw_field.value,
                        "quote": raw_field.evidence_quote,
                        "turn": raw_field.evidence_turn_id,
                        "after_evidence_check": field.status,
                    },
                    "kind": "unlisted",
                }
            )
        for name, spec in side.items():
            if name == "intents":
                want = {_norm(s[2] if isinstance(s, list) else s["value"]) for s in spec}
                got = {_norm(i.value) for i in validated.intents}
                if not want <= got:
                    self._add(reader, row_key, name, {"values": sorted(want)}, {"values": sorted(got)}, "decision")
                continue
            field, raw_field = getattr(validated, name), getattr(raw, name)
            status, value = _expected(spec)
            if field.status == status and _norm(field.value) == value:
                continue
            if field.status == status and type(field).__name__ in FREE_TEXT:
                kind = "wording"
            elif field.status == status and {value, _norm(field.value)} <= SAME_EFFECT.get((reader, name), set()):
                kind = "minor"
            else:
                kind = "decision"
            live = {
                "status": raw_field.status,
                "value": raw_field.value,
                "quote": raw_field.evidence_quote,
                "turn": raw_field.evidence_turn_id,
                "after_evidence_check": field.status,
            }
            self._add(reader, row_key, name, {"status": status, "value": value}, live, kind)

    def _add(self, reader: str, row_key: str, name: str, expected: dict, live: dict, kind: str) -> None:
        self.divergences.append(
            {"call": reader, "row_key": row_key, "field": name, "expected": expected, "live": live, "kind": kind}
        )
