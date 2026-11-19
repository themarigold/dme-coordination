"""Shared types: Claude's input and output shapes, case state, and file formats.

Output types are sent to Claude as structured-output schemas, so every field is
required, nothing is nullable ("" means not stated), and enum-like values are
Literals. Their docstrings are part of the schema, and so of the replay keys.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Conversation input

SpeakerRole = Literal["us", "supplier_rep", "pcp_office", "patient", "helper", "unknown"]


class Turn(BaseModel):
    """One turn of a call transcript, text message or document section."""

    model_config = ConfigDict(extra="forbid")
    turn_id: str  # e.g. "t1", "t2"; unique within one exchange
    speaker: SpeakerRole
    text: str


# Read fields. Evidence comes first in every field, so the model commits to a
# quote before a value. Status "found" requires a non-empty quote.

Status = Literal["found", "not_stated", "ambiguous", "contradictory"]


class _Read(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_turn_id: str  # "" when not_stated
    evidence_quote: str  # verbatim text from that turn; "" when not_stated
    status: Status

    @field_validator("status", mode="before")
    @classmethod
    def _lower_status(cls, v: Any) -> Any:
        # Enum casing is not guaranteed by structured outputs.
        return v.strip().lower() if isinstance(v, str) else v


def _lower(v: Any) -> Any:
    return v.strip().lower() if isinstance(v, str) else v


class TextRead(_Read):
    value: str  # free text exactly as stated (names, addresses, codes); "" if not stated


class YesNoRead(_Read):
    value: Literal["yes", "no", ""]

    _v = field_validator("value", mode="before")(classmethod(lambda cls, v: _lower(v)))


class DatePhraseRead(_Read):
    """A time expression. Claude returns the phrase verbatim; code resolves it
    against the simulated clock (clock.resolve_date_phrase)."""

    value: str  # the time expression verbatim, as the speaker said it
    commitment: Literal["firm", "tentative", ""]

    _c = field_validator("commitment", mode="before")(classmethod(lambda cls, v: _lower(v)))


class CallOutcomeRead(_Read):
    value: Literal["answered", "voicemail", "no_answer", "call_back_later", "wrong_number", "closed", ""]

    _v = field_validator("value", mode="before")(classmethod(lambda cls, v: _lower(v)))


class PcpOutcomeRead(_Read):
    value: Literal[
        "received",
        "not_received",
        "will_send_by",
        "needs_signed_release",
        "already_sent_to_supplier",
        "wants_supplier_form",
        "needs_visit_or_declines",
        "other",
        "",
    ]

    _v = field_validator("value", mode="before")(classmethod(lambda cls, v: _lower(v)))


class NeedsInfoItemRead(_Read):
    value: Literal["medicare_id", "demographics", "order_copy", "other", ""]

    _v = field_validator("value", mode="before")(classmethod(lambda cls, v: _lower(v)))


class MedicaidRead(_Read):
    value: Literal["yes", "no", "not_sure", ""]

    _v = field_validator("value", mode="before")(classmethod(lambda cls, v: _lower(v)))


class CriterionRead(_Read):
    """Coverage criterion from the visit note. Map: documented ↔ status found."""

    value: Literal["documented", "missing", "contradictory"]

    _v = field_validator("value", mode="before")(classmethod(lambda cls, v: _lower(v)))


PatientIntent = Literal[
    "confirm", "decline", "gives_fact", "cost_question", "other_question", "opt_out", "wants_person"
]


class IntentRead(_Read):
    value: PatientIntent

    _v = field_validator("value", mode="before")(classmethod(lambda cls, v: _lower(v)))


# Reader outputs, one per Claude reader


class SupplierCallRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    call_outcome: CallOutcomeRead
    call_back_after: DatePhraseRead
    accepting_new_medicare: YesNoRead
    bills_medicare_k0001: YesNoRead
    k0001_in_stock: YesNoRead
    delivers_to_zip: YesNoRead
    accepts_assignment_all_months: YesNoRead
    abn_required: YesNoRead
    earliest_delivery: DatePhraseRead
    rep_name: TextRead
    readback_confirmed: YesNoRead  # rep confirms our read-back; position checked in code
    # Conditions (same names as SafetyRead's conditions, so code can compare them):
    needs_info: YesNoRead
    needs_info_item: NeedsInfoItemRead
    collects_coinsurance_at_delivery: YesNoRead
    full_price_or_no_assignment: YesNoRead
    other_condition: TextRead


class PcpReplyRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: PcpOutcomeRead
    send_by: DatePhraseRead
    contact_name: TextRead
    supplier_named: TextRead
    item_confirmed: TextRead  # code/item the office confirms when asked "did you mean K0001?"


class OrderDocumentRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    beneficiary_name: TextRead
    medicare_id: TextRead
    item_description: TextRead
    hcpcs_code: TextRead  # as written; mapped to items only through the knowledge code table
    quantity: TextRead
    order_date: TextRead
    practitioner_name: TextRead
    practitioner_npi: TextRead
    signature_present: YesNoRead
    has_changes: YesNoRead
    changes_initialled_dated: YesNoRead


class VisitNoteRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    beneficiary_name: TextRead  # the patient's name as the note gives it (checked against hers in code)
    criterion_a: CriterionRead
    criterion_b: CriterionRead
    criterion_c: CriterionRead
    criterion_d: CriterionRead
    criterion_e: CriterionRead
    criterion_f: CriterionRead
    criterion_g: CriterionRead
    weight_lb: TextRead  # digits as written, e.g. "158"


class PatientReplyRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intents: list[IntentRead]
    identity_name: TextRead
    identity_dob: TextRead
    medicare_id: TextRead
    address: TextRead
    zip: TextRead
    in_home_use: YesNoRead
    home_layout: TextRead
    prior_wheelchair_5y: YesNoRead
    medicaid_or_msp: MedicaidRead
    preferred_supplier: TextRead
    helper_name: TextRead
    helper_permission: YesNoRead
    phone_source: TextRead
    consent_proceed: YesNoRead
    consent_share_with_suppliers: YesNoRead
    consent_calls_texts: YesNoRead
    supplier_call_permission: YesNoRead
    medicare_id_ok_to_share: YesNoRead
    availability: DatePhraseRead
    fit_ok: YesNoRead
    shown_how_to_use: YesNoRead
    has_repair_number: YesNoRead
    delivery_received: YesNoRead


class SafetyRead(BaseModel):
    """Second, narrow reader. Safety signals are OR-combined with the primary
    reader; supplier conditions are compared field by field with SupplierCallRead."""

    model_config = ConfigDict(extra="forbid")
    # safety signals
    opts_out: YesNoRead
    wants_person: YesNoRead
    deceased: YesNoRead
    hospitalised: YesNoRead
    hospice: YesNoRead
    medicare_advantage: YesNoRead
    medicaid_or_msp: YesNoRead
    # supplier conditions
    needs_info: YesNoRead
    needs_info_item: NeedsInfoItemRead
    collects_coinsurance_at_delivery: YesNoRead
    full_price_or_no_assignment: YesNoRead
    abn_requested: YesNoRead
    other_condition: TextRead


SAFETY_SIGNALS: tuple[str, ...] = (
    "opts_out",
    "wants_person",
    "deceased",
    "hospitalised",
    "hospice",
    "medicare_advantage",
    "medicaid_or_msp",
)
SUPPLIER_CONDITIONS: tuple[str, ...] = (
    "needs_info",
    "needs_info_item",
    "collects_coinsurance_at_delivery",
    "full_price_or_no_assignment",
    "other_condition",
)


# Writer and proposer outputs

MessagePurpose = Literal[
    "first_contact",
    "cost_explanation",
    "status_update",
    "supplier_will_call",
    "delivery_check",
    "answer_question",
]


class PatientMessageDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str  # plain language; every number/date/money as a {placeholder} from the facts given


ActionName = Literal[
    "call_again",
    "try_next_supplier",
    "request_from_pcp",
    "ask_patient",
    "send_packet_to",
    "release_supplier",
    "wait_until",
    "escalate_to_human",
    "proceed_with_pending",  # on an approval item: carry out the gated action as prepared by the rules
    "hold_pending",  # on an approval item: do not carry it out yet (why says what is missing)
]


class FactUsed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fact_id: str
    value: str  # canonical string form (see Case.facts)


class Proposal(BaseModel):
    """Flat on purpose: one action plus flat parameters; legality checked in code
    (rules/actions.py), not by a per-state schema."""

    model_config = ConfigDict(extra="forbid")
    action: ActionName
    param_party: str  # "pcp" | "patient" | "supplier:<id>" | ""
    param_supplier: str  # "supplier:<id>" | ""
    param_items: list[str]  # for request_from_pcp, e.g. ["criterion_b", "weight_lb"]
    param_question: str  # for ask_patient: a key from the fixed question list, or ""
    param_time_phrase: str  # for wait_until, verbatim phrase, or ""
    param_reason: str  # for escalate_to_human: an EscalationReason value, or ""
    why: str  # one sentence
    facts_used: list[FactUsed]

    _a = field_validator("action", mode="before")(classmethod(lambda cls, v: _lower(v)))


class LlmUnavailable(BaseModel):
    """Returned (never raised) when a Claude call finally fails."""

    model_config = ConfigDict(extra="forbid")
    call: str  # e.g. "read_supplier_call"
    reason: Literal["refusal", "max_tokens", "timeout", "api_error", "fault_injected", "replay_miss", "parse_error"]
    detail: str = ""


# Case state, escalations, events


class EscalationReason(StrEnum):
    PCP_CHANGED_ITEM = "pcp_changed_item"
    WEIGHT_OVER_250 = "weight_over_250"
    SUPPLIER_CONDITION_UNKNOWN = "supplier_condition_unknown"
    SUPPLIER_CONDITION_DISAGREEMENT = "supplier_condition_disagreement"
    ABN_REQUESTED = "abn_requested"
    PATIENT_OPTED_OUT = "patient_opted_out"
    PATIENT_WANTS_PERSON = "patient_wants_person"
    PATIENT_QUESTION = "patient_question"  # a question outside the FAQ: inbox only, no contact hold
    INTAKE_INCOMPLETE = "intake_incomplete"  # identity or consent still unconfirmed after the re-ask
    PATIENT_DECEASED = "patient_deceased"
    PATIENT_HOSPITALISED = "patient_hospitalised"
    PATIENT_HOSPICE = "patient_hospice"
    MEDICARE_ADVANTAGE = "medicare_advantage"
    MEDICAID_OR_MSP = "medicaid_or_msp"
    PATIENT_UNREACHABLE = "patient_unreachable"
    SUPPLIERS_EXHAUSTED = "suppliers_exhausted"
    PCP_SILENT = "pcp_silent"
    PCP_DECLINED = "pcp_declined"
    DOCS_STILL_MISSING = "docs_still_missing"
    OUTSIDE_HOME_ONLY = "outside_home_only"
    SAME_OR_SIMILAR = "same_or_similar"
    HANDOFF_SILENCE = "handoff_silence"
    NO_HOME_ASSESSMENT = "no_home_assessment"
    SECOND_HANDOFF = "second_handoff"
    STALE_NEWS = "stale_news"
    SUSPICIOUS_CONTENT = "suspicious_content"
    CASE_STALLED = "case_stalled"
    LLM_UNAVAILABLE = "llm_unavailable"
    NO_VALID_PROPOSAL = "no_valid_proposal"
    WORLD_UNSCRIPTED = "world_unscripted"
    APPROVAL_REQUIRED = "approval_required"  # an approval-switch gate, not a failure


ApprovalActionType = Literal["supplier_handoff", "claude_next_action", "criteria_acceptance"]


class Case(BaseModel):
    """The case record: canonical facts (ids from dme/facts.py) plus per-thread state."""

    model_config = ConfigDict(extra="forbid")
    case_id: str
    facts: dict[str, str] = Field(default_factory=dict)
    thread_state: dict[str, str] = Field(default_factory=dict)  # thread name -> state name
    attempts: dict[str, int] = Field(default_factory=dict)  # attempt counter per (party:action) key
    holds: list[str] = Field(default_factory=list)  # active hold names, e.g. "handoff", "cost_messages"
    frozen: bool = False  # deceased: all outbound frozen
    closed: bool = False

    def get(self, fact_id: str) -> str:
        return self.facts.get(fact_id, "")

    def set(self, fact_id: str, value: str) -> None:
        self.facts[fact_id] = value


Actor = Literal["rule", "claude", "human", "world"]


class LogLine(BaseModel):
    """One line of the JSONL run log, with provenance."""

    model_config = ConfigDict(extra="forbid")
    seq: int
    sim_time: datetime
    case_id: str
    actor: Actor
    kind: str  # e.g. "call_placed", "read", "escalation", "approval", "delivered"
    summary: str  # one human-readable line for the timeline
    transport_mocked: bool = True
    llm_call: str = ""  # reader/writer/proposer name when actor == "claude"
    llm_mode: Literal["live", "replay", ""] = ""
    prompt_hash: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    data: dict[str, Any] = Field(default_factory=dict)


# File formats


class ScriptedReads(BaseModel):
    """The reads expected for a world row; offline runs return them in place of Claude.

    Fields not listed default to not_stated with an empty value ("missing" for criteria).
    """

    model_config = ConfigDict(extra="forbid")
    primary: dict[str, Any] = Field(default_factory=dict)
    safety: dict[str, Any] = Field(default_factory=dict)


class WorldRow(BaseModel):
    """One scripted response of the mock world, keyed by (counterparty, action, attempt)."""

    model_config = ConfigDict(extra="forbid")
    counterparty: str  # "supplier:<id>" | "pcp" | "patient"
    action: str  # outbound action kind that triggers this row, e.g. "qualify_call", "order_request"
    attempt: int | Literal["default"]
    delay: str = "0m"  # "<n>m" minutes, "<n>h" business hours, "<n>bd" business days
    event: Literal[
        "reply",
        "no_answer",
        "voicemail",
        "document",
        "accept",
        "reject",
        "scheduled",
        "delivered",
        "esign",
        "never",
    ]
    turns: list[Turn] = Field(default_factory=list)  # for calls / messages
    text: str = ""  # for documents (order, visit note)
    payload: dict[str, Any] = Field(default_factory=dict)  # structured mock events (reason_code, date)
    expected_reads: ScriptedReads | None = None


class HumanDecision(BaseModel):
    """A scripted advocate decision for non-interactive runs, matched by (reason_code, nth)."""

    model_config = ConfigDict(extra="forbid")
    reason_code: EscalationReason
    nth: int = 1
    approval_type: ApprovalActionType | None = None
    expected_action: ActionName | None = None  # the proposal we expect on the item
    decision: Literal["approve", "edit_approve", "reject", "supply_fact", "choose_action"]
    payload: dict[str, Any] = Field(default_factory=dict)  # supply_fact: {fact_id, value, source}; edit: {field: value}
    seen_proposal: bool = True  # False = blind sample


class ScriptedProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason_code: EscalationReason
    nth: int = 1
    proposal: dict[str, Any]  # a Proposal as a dict


class ExpectedOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    terminal_state: Literal["delivered", "escalated", "closed"]
    human_touches: list[str]  # in order, each "<reason_code>" or "<reason_code>:<approval_type>"
    max_sim_business_days: int | None = None


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    case_file: str  # path to the case seed YAML
    directory_file: str = "scenarios/directory.yaml"
    start: datetime  # sim start, America/Chicago
    seed: int = 7
    rows: list[WorldRow]
    human_decisions: list[HumanDecision] = Field(default_factory=list)
    scripted_proposals: list[ScriptedProposal] = Field(default_factory=list)
    scripted_drafts: dict[str, str] = Field(default_factory=dict)  # "<purpose>#<n>" -> offline draft text
    expected: ExpectedOutcome


ReaderName = Literal[
    "read_supplier_call",
    "read_pcp_reply",
    "read_order_document",
    "read_visit_note",
    "read_patient_reply",
    "check_safety_and_conditions",
]


class EvalLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Status
    value: str = ""  # canonical lower-case for Literal fields; free text compared after normalisation
    commitment: str = ""  # DatePhraseRead fields only: "firm" | "tentative"; "" = commitment not scored


class EvalItem(BaseModel):
    """Hand-written eval item. Labels cover only the fields the item is about;
    unlabelled fields are not scored."""

    model_config = ConfigDict(extra="forbid")
    item_id: str
    reader: ReaderName
    turns: list[Turn] = Field(default_factory=list)  # for conversational readers
    text: str = ""  # for document readers
    labels: dict[str, EvalLabel]  # field name -> expected
    intents: list[PatientIntent] = Field(default_factory=list)  # scored as a set only when tagged "score_intents"
    goal: str = ""  # CallContext.goal for this item, as production would build it
    context_facts: dict[str, str] = Field(default_factory=dict)  # CallContext.facts for this item
    tags: list[str] = Field(default_factory=list)  # e.g. ["negation", "safety"]
    notes: str = ""
