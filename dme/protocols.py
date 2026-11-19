"""Interfaces between the rules and the rest of the system.

The rules depend only on these protocols: `Llm` for every Claude call, `ValidateRead`
for the evidence check, and `RerunHook` for re-running the rules after a human action.
"""

from __future__ import annotations

from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from dme.models import (
    Case,
    LlmUnavailable,
    MessagePurpose,
    OrderDocumentRead,
    PatientMessageDraft,
    PatientReplyRead,
    PcpReplyRead,
    Proposal,
    SafetyRead,
    SupplierCallRead,
    Turn,
    VisitNoteRead,
)


class CallContext(BaseModel):
    """What one Claude call may see: its goal, only the relevant facts, and its rule slice."""

    case_id: str
    goal: str  # one sentence: what this call is for
    facts: dict[str, str]
    rule_slice: str = ""  # relevant excerpt of knowledge/medicare_k0001.yaml, with citations
    row_key: str = ""  # "counterparty|action|attempt#i", or "typed|<n>" for a typed reply
    reason_code: str = ""  # for proposals: the inbox item's EscalationReason
    nth: int = 1  # which occurrence of the reason code, or which draft of a purpose

    def tag(self, call_name: str, untrusted_text: str) -> str:
        """Id for the <untrusted id=...> wrapper.

        It hashes the text itself, so the text's author cannot forge the closing tag,
        and stays stable across runs so replay keys do not change.
        """
        import hashlib

        raw = f"{self.case_id}|{self.row_key}|{call_name}|{untrusted_text}"
        return hashlib.sha256(raw.encode()).hexdigest()[:8]


class Llm(Protocol):
    def read_supplier_call(self, turns: list[Turn], ctx: CallContext) -> SupplierCallRead | LlmUnavailable: ...

    def read_pcp_reply(self, turns: list[Turn], ctx: CallContext) -> PcpReplyRead | LlmUnavailable: ...

    def read_order_document(self, text: str, ctx: CallContext) -> OrderDocumentRead | LlmUnavailable: ...

    def read_visit_note(self, text: str, ctx: CallContext) -> VisitNoteRead | LlmUnavailable: ...

    def read_patient_reply(self, turns: list[Turn], ctx: CallContext) -> PatientReplyRead | LlmUnavailable: ...

    def check_safety_and_conditions(self, turns: list[Turn], ctx: CallContext) -> SafetyRead | LlmUnavailable: ...

    def write_patient_message(
        self, purpose: MessagePurpose, facts: dict[str, str], ctx: CallContext
    ) -> PatientMessageDraft | LlmUnavailable: ...

    def propose_next_action(
        self, case_summary: str, allowed_actions: list[str], ctx: CallContext
    ) -> Proposal | LlmUnavailable: ...


R = TypeVar("R", bound=BaseModel)


class ValidateRead(Protocol):
    """The evidence check (dme/evidence.py): returns (validated copy of the read, flags per field)."""

    def __call__(self, read: R, turns: list[Turn], counterparty_roles: set[str]) -> tuple[R, dict[str, list[str]]]: ...


class RerunHook(Protocol):
    def __call__(self, case: Case, event: dict[str, Any]) -> None: ...
