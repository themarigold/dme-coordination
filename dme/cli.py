"""Command line: run one scenario end to end and print the timeline and headline.

python -m dme run --scenario eleanor_main                # live Claude calls (needs the key)
python -m dme run --scenario eleanor_main --replay       # recorded responses only (offline)
python -m dme run --scenario eleanor_main --replay --live-misses --type-reply
    # the demo: recorded responses; live calls only where the typed reply leads somewhere new
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO, get_args

from dme import report
from dme.engine import Event
from dme.models import ApprovalActionType, EscalationReason, LlmUnavailable, Scenario
from dme.policy import ApprovalPolicy
from dme.protocols import Llm
from dme.rules import signals
from dme.rules.runtime import Runtime
from dme.world import ROOT, load_scenario

RUNS_DIR = ROOT / "runs"  # gitignored: logs, regressions and recordings of CLI runs
DEFAULT_TYPED_POINT = "patient|supplier_will_call"  # Eleanor's reply to "this supplier will call you"
TYPED_POINT = re.compile(r"(patient|pcp|supplier:\d+)\|[a-z_:]+")
EXIT_NO_KEY = 3  # 0 passed, 1 mismatch, 2 bad arguments (argparse)
NO_KEY_MESSAGE = (
    "ANTHROPIC_API_KEY is not set. Live runs and --live-misses call Claude; put the key in a .env file at the "
    "repo root (see README). --replay and --fault llm_down need no key."
)


class TypedReplies:
    """Lets a person type the counterparty's turns at one point in the scenario.

    Typed text is read as untrusted input like any other reply.
    """

    def __init__(self, rt: Runtime, point: str, read_line: Callable[[str], str], out: TextIO) -> None:
        self.rt, self.read_line, self.out = rt, read_line, out
        self.counterparty, self.action = point.split("|", 1)
        self.replies = 0
        self.flagged_turns: list[str] = []  # "typed|<n>#<turn>" for each typed turn flagged for injection

    def __call__(self, event: Event) -> None:
        payload = event.payload
        if (payload.get("counterparty"), payload.get("action"), event.kind) != (
            self.counterparty,
            self.action,
            "reply",
        ):
            return
        row = payload["row"]
        turns, typed = [], {}
        for turn in row.turns:
            if turn.speaker == "us":
                print(f"\n  us: {turn.text}", file=self.out)
                turns.append(turn)
                continue
            print(f"  scripted {turn.speaker}: {turn.text}", file=self.out)
            text = self.read_line(f"Type the {turn.speaker} reply (Enter keeps the scripted one): ").strip()
            turns.append(turn.model_copy(update={"text": text}) if text else turn)
            if text:
                typed[turn.turn_id] = text
        if not typed:
            return
        self.replies += 1
        row_key = f"typed|{self.replies}"
        payload["row"] = row.model_copy(update={"turns": turns, "expected_reads": None})
        payload["row_key"] = row_key
        flagged = signals.injection_turns([t for t in turns if t.turn_id in typed])
        self.flagged_turns += [f"{row_key}#{t}" for t in sorted(flagged)]
        self.rt.log(
            "typed_reply",
            f"a person typed the {self.counterparty} reply to {self.action} (untrusted input, {row_key})",
            actor="world",
            data={"typed": typed},
        )


class ReplayThenLive:
    """Answers from recordings first, calling Claude live only when none exists.

    Live responses are recorded under `live_dir`, never in the committed replay/.
    """

    def __init__(self, fault: str | None, live_dir: Path) -> None:
        from dme.llm import ClaudeLlm  # imported here: tests run the CLI with FakeLlm too

        self.records: list = []  # every CallRecord; the runtime logs cost and mode from them
        sink = self.records.append
        self.sources = [
            ClaudeLlm(mode="replay", fault=fault, on_record=sink),
            ClaudeLlm(mode="replay", fault=fault, replay_dir=live_dir, on_record=sink),
        ]
        self.live = ClaudeLlm(mode="live", fault=fault, replay_dir=live_dir, on_record=sink)
        self.mode = "replay"

    @property
    def budget(self):
        """Requests the run's cap still allows, passed to every source so live calls are capped too."""
        return self.live.budget

    @budget.setter
    def budget(self, value) -> None:
        for source in (*self.sources, self.live):
            source.budget = value

    def _first(self, method: str, *args: Any) -> Any:
        """The first source with an answer. Earlier replay misses are dropped so each call counts once."""
        start = len(self.records)
        for source in (*self.sources, self.live):
            before = len(self.records)
            out = getattr(source, method)(*args)
            if source is self.live or not (isinstance(out, LlmUnavailable) and out.reason == "replay_miss"):
                del self.records[start:before]
                return out
        raise AssertionError("unreachable")

    def read_supplier_call(self, turns, ctx):
        return self._first("read_supplier_call", turns, ctx)

    def read_pcp_reply(self, turns, ctx):
        return self._first("read_pcp_reply", turns, ctx)

    def read_order_document(self, text, ctx):
        return self._first("read_order_document", text, ctx)

    def read_visit_note(self, text, ctx):
        return self._first("read_visit_note", text, ctx)

    def read_patient_reply(self, turns, ctx):
        return self._first("read_patient_reply", turns, ctx)

    def check_safety_and_conditions(self, turns, ctx):
        return self._first("check_safety_and_conditions", turns, ctx)

    def write_patient_message(self, purpose, facts, ctx):
        return self._first("write_patient_message", purpose, facts, ctx)

    def propose_next_action(self, case_summary, allowed_actions, ctx):
        return self._first("propose_next_action", case_summary, allowed_actions, ctx)


def make_llm(
    *, replay: bool, fault: str | None, live_misses: bool, record_replay: bool, runs_dir: Path, run_id: str
) -> Llm:
    from dme.llm import ClaudeLlm  # imported here: tests run the CLI with FakeLlm too

    if replay and live_misses:
        return ReplayThenLive(fault, runs_dir / "live-misses")
    if replay:
        return ClaudeLlm(mode="replay", fault=fault)
    return ClaudeLlm(mode="live", fault=fault, replay_dir=None if record_replay else runs_dir / run_id / "replay")


def run(
    scenario: Scenario,
    llm: Llm,
    *,
    policy: dict[str, str] | None = None,
    type_reply: str | None = None,
    read_line: Callable[[str], str] = input,
    out: TextIO = sys.stdout,
    runs_dir: Path = RUNS_DIR,
    run_id: str = "",
) -> tuple[Runtime, report.ReadChecker, float, str, TypedReplies | None]:
    """Run one scenario in demo mode; returns runtime, checker, wall time, early-stop error ("" if none), typed hook."""
    run_id = run_id or f"{scenario.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    runs_dir.mkdir(parents=True, exist_ok=True)
    rt = Runtime(
        scenario,
        llm,
        mode="demo",
        regressions_path=runs_dir / f"{run_id}-regressions.jsonl",
        log_path=runs_dir / f"{run_id}.jsonl",
        policy=ApprovalPolicy(policy or {}),
    )  # type: ignore[arg-type]
    checker = report.ReadChecker(scenario)
    rt.on_read = checker
    typed = TypedReplies(rt, type_reply, read_line, out) if type_reply else None
    rt.retype = typed
    started = time.monotonic()
    error = ""
    try:
        rt.run()
    except Exception as e:  # e.g. the scripted advocate met a proposal the scenario did not expect
        error = f"{type(e).__name__}: {e}"
    return rt, checker, time.monotonic() - started, error, typed


def pass_rule(rt: Runtime, typed: TypedReplies | None) -> str:
    expected = rt.scenario.expected
    extra = f" plus exactly one suspicious_content per flagged typed turn ({len(typed.flagged_turns)})" if typed else ""
    return (
        f"Pass rule: terminal state {expected.terminal_state}, human touches {expected.human_touches}{extra}, "
        "nothing else; no world_unscripted; under --replay alone, no replay miss."
    )


def problems(rt: Runtime, error: str = "", typed: TypedReplies | None = None, replay_misses: int = 0) -> list[str]:
    """Every way this run differs from the pass rule."""
    typed_items = [
        i for i in rt.inbox.items if i.reason == EscalationReason.SUSPICIOUS_CONTENT and " of typed|" in i.blocking_fact
    ]
    found = [f"run stopped: {error}"] if error else []
    found += rt.mismatches(exclude=typed_items)
    flagged = len(typed.flagged_turns) if typed else 0
    if len(typed_items) != flagged:
        found.append(f"{len(typed_items)} suspicious_content item(s) for typed turns, expected {flagged}")
    unscripted = [i for i in rt.inbox.items if i.reason == EscalationReason.WORLD_UNSCRIPTED]
    if unscripted:
        found.append(f"{len(unscripted)} world_unscripted item(s)")
    if replay_misses:
        found.append(f"{replay_misses} Claude call(s) had no recording (replay_miss); re-record, or add --live-misses")
    return found


def print_run(
    rt: Runtime, checker: report.ReadChecker, wall_s: float, out: TextIO, typed: TypedReplies | None = None
) -> None:
    print("\n".join(report.timeline(rt)), file=out)
    print("\nHuman touches:", file=out)
    for line in report.touches(rt) or ["(none)"]:
        print(f"  - {line}", file=out)
    if checker.divergences:
        print("\nLive reads that differ from the scenario's expected_reads:", file=out)
        for d in checker.divergences:
            print(
                f"  - [{d['kind']}] {d['call']} {d['row_key']} {d['field']}: expected {d['expected']}, "
                f"live {json.dumps(d['live'])}",
                file=out,
            )
    print("\nApproval switches:", file=out)
    for line in report.approval_switches(rt):
        print(f"  - {line}", file=out)
    print("\n" + report.headline(rt, wall_s), file=out)
    print(pass_rule(rt, typed), file=out)


def main(
    argv: list[str] | None = None,
    llm: Llm | None = None,
    read_line: Callable[[str], str] = input,
    out: TextIO = sys.stdout,
    runs_dir: Path = RUNS_DIR,
) -> int:
    """Run the CLI and return its exit code. Tests inject `llm`, `read_line` and `runs_dir`."""
    parser = argparse.ArgumentParser(prog="python -m dme")
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("run", help="run one scenario")
    cmd.add_argument("--scenario", required=True, help="name in scenarios/ (e.g. eleanor_main) or a path")
    cmd.add_argument("--replay", action="store_true", help="recorded Claude responses only (no API calls)")
    cmd.add_argument(
        "--live-misses",
        action="store_true",
        help="with --replay: call Claude live where no recording exists (recorded under runs/)",
    )
    cmd.add_argument(
        "--record-replay",
        action="store_true",
        help="live run: record into the committed replay/ (default: runs/<run-id>/replay)",
    )
    cmd.add_argument("--fault", choices=["llm_down"], help="make every Claude call unavailable")
    cmd.add_argument(
        "--type-reply",
        nargs="?",
        const=DEFAULT_TYPED_POINT,
        metavar="COUNTERPARTY|ACTION",
        help=f"type one reply yourself (default point: {DEFAULT_TYPED_POINT})",
    )
    cmd.add_argument(
        "--policy",
        action="append",
        default=[],
        metavar="TYPE=MODE",
        help="approval switch set by a human, e.g. supplier_handoff=autonomous",
    )
    args = parser.parse_args(argv)
    if args.live_misses and not args.replay:
        parser.error("--live-misses needs --replay")
    if args.record_replay and args.replay:
        parser.error("--record-replay is for live runs")
    if args.type_reply and not TYPED_POINT.fullmatch(args.type_reply):
        parser.error(
            f"--type-reply {args.type_reply!r}: expected COUNTERPARTY|ACTION, "
            "e.g. patient|supplier_will_call or supplier:5|reconfirm_call"
        )
    policy = parse_policy(args.policy, parser)
    if llm is None and not args.fault and (not args.replay or args.live_misses):
        from dme.llm.client import has_api_key  # imported here: tests run the CLI with FakeLlm too

        if not has_api_key():
            print(NO_KEY_MESSAGE, file=sys.stderr)
            return EXIT_NO_KEY

    path = Path(args.scenario)
    scenario = load_scenario(path if path.suffix else ROOT / "scenarios" / f"{args.scenario}.yaml")
    run_id = f"{scenario.name}-{time.strftime('%Y%m%d-%H%M%S')}"
    llm = llm or make_llm(
        replay=args.replay,
        fault=args.fault,
        live_misses=args.live_misses,
        record_replay=args.record_replay,
        runs_dir=runs_dir,
        run_id=run_id,
    )
    rt, checker, wall_s, error, typed = run(
        scenario,
        llm,
        policy=policy,
        type_reply=args.type_reply,
        read_line=read_line,
        out=out,
        runs_dir=runs_dir,
        run_id=run_id,
    )
    print_run(rt, checker, wall_s, out, typed)
    misses = (
        sum(1 for r in getattr(llm, "records", []) if r.outcome == "replay_miss")
        if args.replay and not args.live_misses
        else 0
    )
    found = problems(rt, error, typed, misses)
    for problem in found:
        print(f"MISMATCH: {problem}", file=out)
    return 0 if not found else 1


# claude_next_action stays gated: a human cannot switch the advocate's own proposals to autonomous.
POLICY_TYPES = tuple(t for t in get_args(ApprovalActionType) if t != "claude_next_action")


def parse_policy(pairs: list[str], parser: argparse.ArgumentParser) -> dict[str, str]:
    """Parse --policy TYPE=MODE pairs into a dict."""
    policy = {}
    for pair in pairs:
        key, _, value = pair.partition("=")
        if key not in POLICY_TYPES:
            parser.error(f"--policy {pair!r}: type must be one of {', '.join(POLICY_TYPES)}")
        if value not in ("gated", "autonomous"):
            parser.error(f"--policy {pair!r}: value must be gated or autonomous")
        policy[key] = value
    return policy


def entry() -> None:
    sys.exit(main())


if __name__ == "__main__":
    entry()
