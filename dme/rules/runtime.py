"""One case at run time: the engine, mock world, inbox, policy and Claude, wired to the thread rules.

Rules reach Claude only through `read` (always evidence-checked), `call_llm` and `escalate`.
"""

# Case.thread_state holds string progress markers per thread (shown to the proposer) and
# bookkeeping such as retry counters and timer generations (hidden: actions.BOOKKEEPING_KEYS).
# Facts live in Case.facts. Keys whose meaning is not obvious from their use:
#   sourcing           "parked" while she may not be disclosed to suppliers yet
#   handoff            step name; held_<step> while a hold stops it, blocked_send when an approved packet
#                      failed its send-time check, with_human once a person has it
#   settled.<reason>   "yes" once a person supplied a settling fact after the reason was raised
#   medicaid_supplied  "yes" once a person supplied the Medicaid answer; no reader overwrites it
#   packet_approved    "<supplier>|<order v>|<note v>" the packet approval was given for
#   patient_details    JSON of what was sent with each patient action, so retries repeat it

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from dme import clock, knowledge
from dme.engine import MAX_CLAUDE_CALLS, Engine, Event
from dme.evidence import document_turns, drop_flagged, validate_read
from dme.inbox import Inbox, InboxItem
from dme.models import ApprovalActionType, LlmUnavailable, Proposal, Scenario, Turn
from dme.models import EscalationReason as R
from dme.policy import ApprovalPolicy
from dme.protocols import CallContext, Llm
from dme.rules import actions, handoff, holds, patient, pcp, signals, suppliers
from dme.world import ROOT, World, load_case, load_directory

READER_ROLES = {"supplier": {"supplier_rep"}, "pcp": {"pcp_office"}, "patient": {"patient", "helper"}}
DOCUMENT_READERS = ("read_order_document", "read_visit_note")
NO_PROPOSAL = {R.CASE_STALLED, R.LLM_UNAVAILABLE, R.NO_VALID_PROPOSAL, R.WORLD_UNSCRIPTED, R.SUSPICIOUS_CONTENT}
ONE_OPEN_PER_REASON = set(holds.HOLDS_BY_REASON) | {R.PATIENT_DECEASED}  # never two items for one of these
SAFETY_REASONS = set(signals.SIGNAL_REASONS.values())
PROCEED_ACTIONS = ("approve", "edit_approve", "choose_action")  # human actions that can carry a proceed
BLIND_SAMPLE_RATE = 0.2  # share of approval items decided without seeing Claude's proposal
HUMAN_RESPONSE_HOURS = 1  # the scripted advocate acts this many business hours after an item appears

Handler = Callable[["Runtime", Event], None]


class ScenarioMismatch(AssertionError):
    """The run asked the scripted advocate something the scenario did not expect."""


def party_kind(counterparty: str) -> Literal["supplier", "pcp", "patient"]:
    return "supplier" if counterparty.startswith("supplier:") else counterparty  # type: ignore[return-value]


@dataclass
class Reading:
    """A validated read plus where it came from.

    Facts come only from `read`, which has every field from a flagged turn emptied
    (all fields, if a patient or helper turn is flagged: someone else may have her phone). Safety
    signals use `raw`, from before that drop, because a signal only ever tightens.
    """

    read: Any
    turns: list[Turn]
    row_key: str
    flagged: set[str] = field(default_factory=set)  # turn ids with prompt-injection patterns
    raw: Any = None
    facts_blocked: bool = False  # every fact of this reply was dropped
    downgraded: frozenset[str] = frozenset()  # fields the evidence check downgraded
    dropped: frozenset[str] = frozenset()  # fields emptied because their turn was flagged

    def unknown(self, *names: str) -> bool:
        """Whether any of these fields was read but cannot be used: unknown, so never counted as "no"."""
        return bool(set(names) & (self.downgraded | self.dropped))


class Runtime:
    def __init__(
        self,
        scenario: Scenario,
        llm: Llm,
        *,
        regressions_path: Path,
        mode: Literal["test", "demo"] = "test",
        root: Path = ROOT,
        log_path: Path | None = None,
        policy: ApprovalPolicy | None = None,
        scripted_humans: bool = True,
    ) -> None:
        self.scenario = scenario
        self.llm = llm
        self.world = World(scenario, mode)
        self.directory = load_directory(scenario, root)
        self.policy = policy or ApprovalPolicy()
        case = load_case(scenario, root)
        self.inbox = Inbox(
            case,
            regressions_path,
            rerun_hook=self.on_human_action,
            policy=self.policy,
            check_hook=self.check_human_choice,
            supply_check=self.check_supplied_fact,
        )
        self.engine = Engine(case, scenario.start, self.inbox, world=self.world, seed=scenario.seed, log_path=log_path)
        self.engine.after_step = lambda: self.ensure_running("an event")
        self.engine.never_stale = {"patient"}  # she replies whenever she gets to it, not to one attempt
        self.scripted_humans = scripted_humans
        self.backstop = True  # ensure_running; tests of one thread in isolation may turn it off
        self.proposal_nth: Counter[str] = Counter()  # proposals asked per reason
        self.decision_nth: Counter[str] = Counter()  # items raised per reason (scripted advocate lookup)
        self.draft_nth: Counter[str] = Counter()  # drafts asked per message purpose
        self.suspicious_turns: dict[str, InboxItem] = {}  # "row_key#turn" -> its suspicious_content item
        self.pending_approvals: dict[str, Callable[[Runtime], None]] = {}  # item id -> gated action
        self.hold_baselines: dict[str, tuple] = {}  # item id -> what its gated action relied on when a hold was chosen
        self._routes: dict[tuple[str, str], dict[str, Handler]] = {}
        # CLI hooks: replace a world reply with typed text, and observe every read.
        self.retype: Callable[[Event], None] | None = None
        self.on_read: Callable[[str, str, Any, Any], None] | None = None
        if hasattr(llm, "budget"):  # the cap is also checked before each request
            llm.budget = self.engine.claude_left
        for module in (suppliers, pcp, patient, handoff):
            module.register(self)
        self.timer("human_decision", lambda rt, ev: rt._scripted_decision(ev))
        self.timer("re_evaluate", lambda rt, ev: rt._on_re_evaluate(ev))

    # Basics

    @property
    def case(self):
        return self.engine.case

    @property
    def now(self) -> datetime:
        return self.engine.now

    @property
    def supplier_ids(self) -> list[int]:
        return [s["id"] for s in self.directory]

    def supplier(self, supplier_id: int) -> dict:
        return next(s for s in self.directory if s["id"] == supplier_id)

    def log(self, kind: str, summary: str, actor: str = "rule", **fields: Any) -> None:
        self.engine.log(actor, kind, summary, **fields)  # type: ignore[arg-type]

    def suspicious_turn(self, row_key: str, turn_id: str, claims: list[str]) -> None:
        """Raise one suspicious_content item per flagged turn, naming what the reads claimed from it."""
        key = f"{row_key}#{turn_id}"
        item = self.suspicious_turns.get(key)
        claimed = sorted(set(item.tried if item else []) | set(claims))
        what = f"; it claimed {', '.join(claimed)} (no fact recorded; safety signals still apply)" if claimed else ""
        text = f"turn {turn_id} of {row_key} is flagged for prompt injection{what}"
        if item is None:
            self.suspicious_turns[key] = self.escalate(R.SUSPICIOUS_CONTENT, text, claimed)
        elif claimed != item.tried:  # the safety read claimed more from the same turn
            item.tried, item.blocking_fact = claimed, text

    # Events

    def route(self, kind: str, prefix: str, action: str, handler: Handler) -> None:
        """Handle `kind` events from counterparties starting with `prefix` that answer `action` ("*" = any)."""
        key = (kind, prefix)
        if key not in self._routes:
            self._routes[key] = {}
            self.engine.on(kind, lambda eng, ev, key=key: self._dispatch(key, ev), counterparty_prefix=prefix)
        self._routes[key][action] = handler

    def _dispatch(self, key: tuple[str, str], event: Event) -> None:
        table = self._routes[key]
        handler = table.get(event.payload.get("action", "")) or table.get("*")
        if handler is None:
            raise LookupError(f"no rule for {key} answering {event.payload.get('action')!r}")
        if self.retype is not None:
            self.retype(event)
        handler(self, event)

    def timer(self, kind: str, handler: Handler) -> None:
        self.engine.on(kind, lambda eng, ev: handler(self, ev))

    def schedule(self, due: datetime, kind: str, payload: dict[str, Any] | None = None) -> None:
        self.engine.schedule(due, kind, payload)

    def in_business_hours(self, hours: float, kind: str, payload: dict[str, Any] | None = None) -> None:
        self.schedule(clock.add_business_hours(self.now, hours), kind, payload)

    def after_hours(self, what: str, kind: str, payload: dict[str, Any] | None = None) -> bool:
        """Outside business hours, defer timer `kind` to the next business moment and return True.

        A call that could not have been answered is never placed, so it never uses up a callback.
        """
        if clock.in_business_hours(self.now):
            return False
        due = clock.next_business_moment(self.now)
        self.log("call_deferred", f"{what}: outside business hours, placed at {due.isoformat()}")
        self.schedule(due, kind, payload)
        return True

    def in_business_days(self, days: int, kind: str, payload: dict[str, Any] | None = None) -> None:
        self.schedule(clock.add_business_days(self.now, days), kind, payload)

    def send(self, counterparty: str, action: str, **payload: Any) -> bool:
        """Send one outbound action with a fresh attempt id."""
        attempt = self.engine.next_attempt(counterparty, action)
        return self.engine.send(counterparty, action, attempt, payload)

    # Claude

    def call_llm(self, name: str, call: Callable[[], Any]) -> Any:
        """Run one Claude call and log it, counting its requests toward the cap."""
        records = getattr(self.llm, "records", None)
        before = len(records) if records is not None else 0
        if self.engine.claude_left() <= 0:  # checked before each call, not only between events
            self.engine.log("rule", "llm_not_called", f"{name}: Claude call cap {MAX_CLAUDE_CALLS} reached")
            return LlmUnavailable(call=name, reason="api_error", detail="Claude call cap reached")
        result = call()
        failed = isinstance(result, LlmUnavailable)
        new = records[before:] if records is not None else []
        if records is not None and not any(
            r.requests or r.reached_api or (r.mode == "replay" and r.outcome == "ok") for r in new
        ):
            # No request was sent or replayed (an FAQ answer, a fault, a replay miss): not a Claude call.
            what = f"unavailable: {result.reason}" if failed else "answered without a request"
            self.engine.log("rule", "llm_not_called", f"{name} {what}")
            return result
        if records is None and failed:  # an Llm without records: unavailable is not a call either
            self.engine.log("rule", "llm_not_called", f"{name} unavailable: {result.reason}")
            return result
        # A writer's regeneration or a max_tokens retry is a second request; a replayed
        # response counts as the one request it recorded.
        requests = sum(r.requests or (r.mode == "replay" and r.outcome == "ok") for r in new) if new else 1
        fields: dict[str, Any] = {
            "llm_call": name,
            "llm_mode": "" if records is not None else getattr(self.llm, "mode", ""),
            "data": {"requests": requests},
        }
        if new:
            last = new[-1].log_fields()
            fields |= {
                "llm_mode": last["llm_mode"],
                "prompt_hash": last["prompt_hash"],
                "model": last["model"],
                "input_tokens": sum(r.input_tokens for r in new),
                "output_tokens": sum(r.output_tokens for r in new),
                "cost_usd": round(sum(r.cost_usd for r in new), 6),
                "latency_ms": sum(r.latency_ms for r in new),
            }
        self.engine.log("claude", "llm_call", f"{name}{' unavailable: ' + result.reason if failed else ''}", **fields)
        return result

    def read(
        self, reader: str, event: Event, goal: str, fact_ids: tuple[str, ...] = (), escalate: bool = True
    ) -> Reading | None:
        """Read a world event's turns (or document text) and validate the evidence."""
        row = event.payload["row"]
        party = party_kind(event.payload["counterparty"])
        row_key = event.payload.get("row_key") or event.payload["reread_row_key"]
        if reader in DOCUMENT_READERS:
            return self.read_input(reader, row.text, document_turns(row.text), party, row_key, goal, fact_ids, escalate)
        return self.read_input(reader, row.turns, row.turns, party, row_key, goal, fact_ids, escalate)

    def schedule_reread(self, hours: float, kind: str, event: Event) -> None:
        """Read the same world reply once more later.

        The row travels as reread_row_key, not row_key, because a re-read is our timer,
        not a world event, and must not trigger the world's follow-ups.
        """
        payload = {k: v for k, v in event.payload.items() if k != "row_key"}
        payload |= {"reread": True, "reread_row_key": event.payload.get("row_key") or event.payload["reread_row_key"]}
        self.log("reread_scheduled", f"{payload['reread_row_key']}: read again in {hours:g} business hour(s)")
        self.in_business_hours(hours, kind, payload)

    def read_input(
        self,
        reader: str,
        given: Any,
        turns: list[Turn],
        party: str,
        row_key: str,
        goal: str,
        fact_ids: tuple[str, ...] = (),
        escalate: bool = True,
    ) -> Reading | None:
        """One validated Claude read, or None on failure (raising an item if `escalate`)."""
        ctx = CallContext(
            case_id=self.case.case_id,
            goal=goal,
            facts={f: self.case.get(f) for f in fact_ids},
            rule_slice=knowledge.rule_slice(reader),
            row_key=row_key,
        )
        result = self.call_llm(reader, lambda: getattr(self.llm, reader)(given, ctx))
        if isinstance(result, LlmUnavailable):
            if escalate:
                self.escalate(R.LLM_UNAVAILABLE, f"{reader} unavailable ({result.reason}) on {row_key}")
            return None
        validated, flags = validate_read(result, turns, READER_ROLES[party])
        if self.on_read is not None:
            self.on_read(reader, row_key, result, validated)
        if flags:
            self.log("evidence_flags", f"{reader} on {row_key}", data={"flags": flags})
        downgraded = frozenset(name for name, marks in flags.items() if any(m.startswith("downgraded:") for m in marks))
        flagged = signals.injection_turns(turns)
        if not flagged:
            return Reading(validated, turns, row_key, flagged, raw=validated, downgraded=downgraded)
        # No fact rests on a flagged turn, and the turn goes to a person (always for patient
        # and typed replies; otherwise when it claimed something). Safety signals still apply.
        whole = party == "patient" and any(t.turn_id in flagged for t in turns if t.speaker in ("patient", "helper"))
        _, claims = drop_flagged(validated, flagged)
        clean, _ = drop_flagged(validated, {t.turn_id for t in turns} if whole else flagged)
        self.log(
            "injection_flag",
            f"prompt-injection pattern in {row_key} turns {sorted(flagged)}; facts not "
            f"recorded{' from the whole reply' if whole else ''}: {claims or 'nothing claimed'}",
            data={"claims": claims},
        )
        always = party == "patient" or row_key.startswith("typed|")
        for turn in sorted(flagged if always else claims):
            self.suspicious_turn(row_key, turn, claims.get(turn, []))
        dropped = frozenset(
            name
            for name, value in validated
            if getattr(value, "status", "not_stated") != "not_stated" and getattr(clean, name).status == "not_stated"
        )
        return Reading(
            clean, turns, row_key, flagged, raw=validated, facts_blocked=whole, downgraded=downgraded, dropped=dropped
        )

    # Humans

    def escalate(
        self,
        reason: R,
        blocking_fact: str,
        tried: list[str] | tuple[str, ...] = (),
        *,
        approval_type: ApprovalActionType | None = None,
        extra_holds: tuple[str, ...] = (),
    ) -> InboxItem:
        """Put the case in front of a human, with Claude's proposal where one makes sense.

        `extra_holds` are kept under this item, so only proceeding on it lifts them.
        """
        holds.unsettle(self.case, reason)  # a settling fact supplied before this report no longer counts
        if reason == R.PATIENT_DECEASED:
            self.case.set("patient.deceased", "")  # an earlier "found false" no longer counts
        for item in self.inbox.open_items():
            same = (item.reason, item.approval_type, item.blocking_fact) == (reason, approval_type, blocking_fact)
            if same or (reason in ONE_OPEN_PER_REASON and item.reason == reason):
                if extra_holds:
                    holds.apply(self.case, reason, extra_holds, item.item_id)
                return item
        item_id = self.inbox.next_id()  # the item raised below
        holds.apply(self.case, reason, extra_holds, item_id)  # deceased freezes at once; only a human closes it
        proposal, rejection = None, ""
        if reason not in NO_PROPOSAL:
            self.proposal_nth[reason.value] += 1
            allowed = actions.allowed_actions(self.case, self.supplier_ids, approval_type, reason)
            proposal, rejection = actions.propose(
                self, reason, self.proposal_nth[reason.value], blocking_fact, allowed, approval_type=approval_type
            )
        if rejection:
            tried = [*tried, f"no valid proposal: {rejection}"]
        self.decision_nth[reason.value] += 1
        nth = self.decision_nth[reason.value]
        decision = self.world.human_decision(reason.value, nth) if self.scripted_humans else None
        if decision is not None:
            blind = proposal is not None and not decision.seen_proposal
        else:
            blind = proposal is not None and self.engine.sample_blind(BLIND_SAMPLE_RATE)
        item = self.engine.escalate(
            reason, blocking_fact, list(tried), approval_type=approval_type, proposal=proposal, blind=blind
        )
        assert item.item_id == item_id, "the item's extra holds were keyed to another id"
        if self.scripted_humans:
            self.in_business_hours(HUMAN_RESPONSE_HOURS, "human_decision", {"item_id": item.item_id, "nth": nth})
        return item

    def allowed_for_human(self, item: InboxItem) -> list[str]:
        """What a person may choose on `item` now.

        Proceed is offered only where it would act; hold_pending is always allowed. An
        approval item allows only proceed and hold, so no choice can orphan its gated action.
        """
        allowed = list(actions.allowed_actions(self.case, self.supplier_ids, item.approval_type, item.reason))
        acts = item.item_id in self.pending_approvals or holds.item_holds(self.case, item.reason, item.item_id)
        if acts and "proceed_with_pending" not in allowed:  # its gated action, or the extra holds it put on
            allowed.append("proceed_with_pending")
        return allowed + ([] if "hold_pending" in allowed else ["hold_pending"])

    def check_human_choice(self, item: InboxItem, chosen: Proposal) -> str:
        """Why a person's action on `item` is not legal now, or "" if it is."""
        return actions.validate(
            chosen, self.case, self.allowed_for_human(item), self.supplier_ids, self.now, cited_facts=False
        )

    def check_supplied_fact(self, item: InboxItem, fact_id: str, value: str) -> str:
        """Why a person's supplied fact is not taken on `item` now, or "" if it is.

        Supplying patient.deceased = "no" on the open deceased item is how a person
        clears a false report; to confirm one, they proceed on the item instead.
        """
        if fact_id != "patient.deceased":
            return ""
        if item.reason != R.PATIENT_DECEASED or not self.case.frozen or self.case.closed:
            return "patient.deceased is supplied only on the open patient_deceased item of a frozen case"
        if value != "no":
            return 'only "no" (the report is false) is supplied; to confirm it, proceed on the item'
        return ""

    def _clear_deceased(self, item: InboxItem) -> None:
        """Unfreeze after a person found the deceased report false; she is asked again (replies were not acted on)."""
        self.case.frozen = False
        self.log(
            "deceased_report_cleared",
            "a person found the deceased report false: the case is unfrozen and runs again",
            actor="human",
        )
        patient.ask_again_after_freeze(self, item)

    def _close_for_deceased(self) -> None:
        """Release every supplier we spoke to, then close and freeze the case for good."""
        self.case.frozen = False  # just long enough to send the releases
        state = self.case.thread_state
        spoken = suppliers.with_status(self, "primary", "backup", "qualified", "hold_needs_info", "calling")
        spoken += [
            s
            for s in suppliers.with_status(self, "candidate")  # a callback was promised
            if state.get(f"supplier.{s}.callback_pending") or state.get(f"supplier.{s}.callback_ready")
        ]
        for sid in spoken:
            suppliers.release(self, sid, "case closed")
        self.case.frozen = True
        self.case.closed = True
        self.log("case_closed", "deceased confirmed by a person: suppliers released, case closed and frozen")

    def request_approval(
        self,
        approval_type: ApprovalActionType,
        blocking_fact: str,
        tried: list[str],
        on_proceed: Callable[[Runtime], None],
    ) -> None:
        """Run `on_proceed` now if the switch is autonomous, else after a human approves."""
        if not self.policy.is_gated(approval_type):
            self.log("autonomous", f"{approval_type}: {blocking_fact} (switch loosened by a human)")
            on_proceed(self)
            return
        item = self.escalate(R.APPROVAL_REQUIRED, blocking_fact, tried, approval_type=approval_type)
        self.pending_approvals[item.item_id] = on_proceed

    def on_human_action(self, case, event: dict[str, Any]) -> None:
        """Carry out what the human decided, lift holds, and re-run the rules."""
        item = self.inbox.get(event["item_id"])
        self.log(
            "human_action",
            f"{event['action']} on {item.touch}",
            actor="human",
            data={k: v for k, v in event.items() if k != "type"},
        )
        if self.engine.stalled and item.item_id == self.engine.stall_item and item.status == "resolved":
            self.engine.rearm(f"{event['action']} on {item.touch} ({item.item_id})")
        if event["action"] == "supply_fact":  # a person's fact: newer than any report it settles
            holds.note_supplied(case, event["fact_id"], event["value"])
            if event["fact_id"] == "patient.medicaid_or_msp":
                case.thread_state["medicaid_supplied"] = "yes"  # no reader overwrites it
            if event["fact_id"] == "patient.deceased" and item.reason == R.PATIENT_DECEASED:
                self._clear_deceased(item)
            if event["value"] == "no" and event["fact_id"] in patient.OPT_OUT_FACTS:
                patient.declined(self)  # her refusal, whoever records it, is an opt-out
        proposal = Proposal.model_validate(event["proposal"]) if event.get("proposal") else None
        proceeded = (
            proposal is not None and proposal.action == "proceed_with_pending" and event["action"] in PROCEED_ACTIONS
        )
        if proposal is not None and proposal.action == "hold_pending" and event["action"] in PROCEED_ACTIONS:
            self.hold_baselines[item.item_id] = actions.relies_on(self, item)  # re-proposed once this changes
        if proposal is not None and event["action"] in PROCEED_ACTIONS:
            if proposal.action == "proceed_with_pending" and item.reason == R.PATIENT_DECEASED:
                self._close_for_deceased()  # a person confirmed it; the case never unfreezes
            elif proposal.action == "proceed_with_pending":
                # The only human decision that lifts the item's holds.
                holds.release(case, item.reason, item.item_id)
                gated = self.pending_approvals.pop(item.item_id, None)
                if gated is not None:
                    gated(self)
            elif proposal.action != "hold_pending":
                actions.execute(self, proposal, item)
        self.evaluate()
        # No hold is ever left without an open item: closing a holding item without lifting
        # its hold raises it again. A handoff only a person can move comes back too, unless
        # the person raised a fresh item for it.
        waiting = proposal is not None and proposal.action == "wait_until"  # re-raised when the wait ends
        escalated = proposal is not None and proposal.action == "escalate_to_human"
        if item.status == "resolved" and not waiting:
            if self.still_holding(item.reason, item.item_id):
                unmet = holds.unsettled(case, item.reason)
                needs = ", ".join(f"{f} is {v!r} (it is {case.get(f)!r})" for f, v in unmet)
                why = (
                    f"proceeded, but the hold lifts only once {needs}: supply the fact"
                    if proceeded and unmet
                    else "closed without proceeding; hold still on"
                )
                self.reraise(item.reason, item.item_id, item.blocking_fact, [*item.tried, why])
            elif not escalated and self._handoff_owned_by(item.reason):
                still = case.thread_state.get("handoff")
                self.escalate(
                    item.reason,
                    item.blocking_fact,
                    [
                        *item.tried,
                        f"{event['action']}: the handoff is still {still} with nothing running; "
                        "send the packet to a backup, release, or wait",
                    ],
                )
        self.ensure_running("a human action")

    def _handoff_owned_by(self, reason: R) -> bool:
        """Whether an item for `reason` owns a handoff only a person can move, with no other item open for it."""
        return (
            reason in handoff.OWNER_REASONS
            and handoff.stuck(self)
            and not any(i.reason in handoff.OWNER_REASONS for i in self.inbox.open_items())
        )

    def owns(self, reason: R, item_id: str = "") -> bool:
        """Whether an item still has something only a person can move: a hold, a freeze, or a stuck handoff."""
        return self.still_holding(reason, item_id) or self._handoff_owned_by(reason)

    def ensure_running(self, after: str) -> None:
        """Backstop: an open case with no open item and nothing queued raises case_stalled.

        Events queued on a stalled engine do not count as queued.
        """
        running = self.engine.pending() and not self.engine.stalled
        if not self.backstop or self.case.closed or self.inbox.open_items() or running:
            return
        state = self.case.thread_state
        what = (
            f"handoff {state.get('handoff') or 'not started'}, sourcing {state.get('sourcing') or 'not started'}, "
            f"holds {', '.join(self.case.holds) or 'none'}, frozen {'yes' if self.case.frozen else 'no'}"
        )
        stalled = " (the engine is stalled at a cap)" if self.engine.stalled else ""
        self.log("nothing_running", f"after {after}: no open item and nothing queued{stalled} ({what})")
        item = self.escalate(R.CASE_STALLED, f"nothing running after {after}{stalled}: {what}")
        if self.engine.stalled:
            self.engine.stall_item = item.item_id

    def still_holding(self, reason: R, item_id: str = "") -> bool:
        """Whether `reason` or its item's extra holds still hold the case, or a deceased report still freezes it."""
        return holds.holding(self.case, reason, item_id) or (
            reason == R.PATIENT_DECEASED and self.case.frozen and not self.case.closed
        )

    def reraise(self, reason: R, item_id: str, blocking_fact: str, tried: list[str]) -> None:
        """Raise a resolved item's reason again; the new item carries its extra holds."""
        extra = holds.item_holds(self.case, reason, item_id)
        new = self.escalate(reason, blocking_fact, tried, extra_holds=extra)
        if new.item_id != item_id:
            holds.drop_item(self.case, reason, item_id)

    def _on_re_evaluate(self, event: Event) -> None:
        """A wait_until came due: re-raise the item if its hold is still on."""
        reason, item_id = event.payload.get("reraise"), event.payload.get("item_id", "")
        if reason and self.still_holding(R(reason), item_id):
            self.reraise(R(reason), item_id, event.payload["blocking_fact"], ["waited as the advocate chose"])
        elif reason and self._handoff_owned_by(R(reason)):
            self.escalate(
                R(reason),
                event.payload["blocking_fact"],
                ["waited as the advocate chose; the handoff is still with a person"],
            )
        self.evaluate()

    def _scripted_decision(self, event: Event) -> None:
        item = self.inbox.get(event.payload["item_id"])
        if item.status != "open":
            return
        decision = self.world.human_decision(item.reason.value, event.payload["nth"])
        if decision is None:
            self.log("awaiting_human", f"{item.touch}: no scripted decision")
            return
        if decision.approval_type and decision.approval_type != item.approval_type:
            raise ScenarioMismatch(f"{item.touch}: expected approval type {decision.approval_type}")
        if decision.expected_action and (item.proposal is None or item.proposal.action != decision.expected_action):
            got = item.proposal.action if item.proposal else "no proposal"
            raise ScenarioMismatch(f"{item.touch}: expected proposal {decision.expected_action}, got {got}")
        p = decision.payload
        if decision.decision == "approve":
            self.inbox.approve(item.item_id)
        elif decision.decision == "edit_approve":
            self.inbox.edit_approve(item.item_id, p)
        elif decision.decision == "reject":
            self.inbox.reject(item.item_id, p.get("reason", ""))
        elif decision.decision == "supply_fact":
            self.inbox.supply_fact(item.item_id, p["fact_id"], p["value"], p["source"])
        else:
            self.inbox.choose_action(item.item_id, Proposal.model_validate(p))

    # Running

    def evaluate(self) -> None:
        """Re-run the rules that depend on several threads (after any new fact)."""
        if self.case.closed or self.case.frozen:
            return
        holds.release_settled(self.case, {i.reason for i in self.inbox.open_items()})
        self._repropose_held_approvals()
        suppliers.retry_holds(self)
        pcp.resume_nudges(self)
        pcp.resume_release(self)
        patient.resume_cost_question(self)
        suppliers.start(self)
        handoff.advance(self)

    def _repropose_held_approvals(self) -> None:
        """Ask Claude again on an approved hold once something it waits on changes.

        That is a fact the hold cited or anything the gated action depends on, so a hold
        citing an unrelated fact still comes back when its real blocker is fixed.
        """
        for item in self.inbox.open_items():
            held = item.proposal
            baseline = self.hold_baselines.get(item.item_id)
            cited_changed = (
                held is not None
                and held.action == "hold_pending"
                and bool(item.history)
                and not all(self.case.get(f.fact_id) == f.value for f in held.facts_used)
            )
            depends_changed = baseline is not None and baseline != actions.relies_on(self, item)
            if not (cited_changed or depends_changed):
                continue
            self.proposal_nth[item.reason.value] += 1
            allowed = actions.allowed_actions(self.case, self.supplier_ids, item.approval_type, item.reason)
            proposal, _ = actions.propose(
                self,
                item.reason,
                self.proposal_nth[item.reason.value],
                item.blocking_fact,
                allowed,
                approval_type=item.approval_type,
            )
            if proposal is None:
                continue  # the baseline stays, so it is tried again at the next re-run
            if baseline is not None:
                self.hold_baselines[item.item_id] = actions.relies_on(self, item)  # once per change
            self.inbox.update_proposal(item.item_id, proposal)
            self.log("reproposed", f"{item.touch}: {proposal.action} (a cited fact changed)")
            self.decision_nth[item.reason.value] += 1
            if self.scripted_humans:
                self.in_business_hours(
                    HUMAN_RESPONSE_HOURS,
                    "human_decision",
                    {"item_id": item.item_id, "nth": self.decision_nth[item.reason.value]},
                )

    def start(self) -> None:
        self.log("case_started", f"case {self.case.case_id} at {self.now.isoformat()}")
        patient.start(self)
        pcp.start(self)

    def run(self) -> str:
        self.start()
        return self.engine.run()

    def terminal_state(self) -> str:
        """delivered, escalated, closed or idle. Never "delivered" while a safety item is open."""
        open_items = self.inbox.open_items()
        if self.case.frozen and self.case.closed:
            return "closed"
        safety_open = any(i.reason in SAFETY_REASONS for i in open_items)
        if self.case.closed and self.case.thread_state.get("handoff") == "delivered" and not safety_open:
            return "delivered"
        if open_items:
            return "escalated"
        return "closed" if self.case.closed else "idle"

    def outcome(self) -> dict[str, Any]:
        days, d = 0, self.engine.start.date()
        while d < self.now.date():  # business days after the start date, up to and including today
            d += timedelta(days=1)
            days += clock.is_business_day(d)
        return {
            "terminal_state": self.terminal_state(),
            "human_touches": self.inbox.touches(),
            "business_days": days,
            "claude_calls": self.engine.claude_calls,
            "steps": self.engine.steps,
        }

    def mismatches(self, exclude: list | tuple = ()) -> list[str]:
        """Differences between this run and the scenario's expected outcome, ignoring items in `exclude`."""
        expected, got = self.scenario.expected, self.outcome()
        touches = [i.touch for i in self.inbox.items if i not in exclude]
        problems = []
        if got["terminal_state"] != expected.terminal_state:
            problems.append(f"terminal state {got['terminal_state']} != {expected.terminal_state}")
        if touches != expected.human_touches:
            problems.append(f"human touches {touches} != {expected.human_touches}")
        if expected.max_sim_business_days is not None and got["business_days"] > expected.max_sim_business_days:
            problems.append(f"{got['business_days']} business days > {expected.max_sim_business_days}")
        return problems
