"""The six Claude readers. Each returns its read model or LlmUnavailable.

Reads are returned raw; the evidence check runs in the rules.
"""

from __future__ import annotations

from pydantic import BaseModel

from dme.llm.client import Caller
from dme.llm.context import build_messages, render_document, render_turns
from dme.models import (
    LlmUnavailable,
    OrderDocumentRead,
    PatientReplyRead,
    PcpReplyRead,
    SafetyRead,
    SupplierCallRead,
    Turn,
    VisitNoteRead,
)
from dme.protocols import CallContext

READER_EFFORT = "low"

_SOURCES = {
    "read_supplier_call": "a call with a medical equipment supplier",
    "read_pcp_reply": "the doctor's office",
    "read_order_document": "a document sent by the doctor's office",
    "read_visit_note": "a document sent by the doctor's office",
    "read_patient_reply": "the patient or her helper",
    "check_safety_and_conditions": "a supplier, the patient or the doctor's office",
}


def _read(
    caller: Caller, call_name: str, model_cls: type[BaseModel], untrusted: str, ctx: CallContext, effort: str
) -> BaseModel | LlmUnavailable:
    system, user = build_messages(call_name, ctx, untrusted, source=_SOURCES[call_name])
    return caller(call_name, model_cls, system, user, effort).output


def read_supplier_call(
    caller: Caller, turns: list[Turn], ctx: CallContext, effort: str = READER_EFFORT
) -> SupplierCallRead | LlmUnavailable:
    return _read(caller, "read_supplier_call", SupplierCallRead, render_turns(turns), ctx, effort)  # type: ignore[return-value]


def read_pcp_reply(
    caller: Caller, turns: list[Turn], ctx: CallContext, effort: str = READER_EFFORT
) -> PcpReplyRead | LlmUnavailable:
    return _read(caller, "read_pcp_reply", PcpReplyRead, render_turns(turns), ctx, effort)  # type: ignore[return-value]


def read_order_document(
    caller: Caller, text: str, ctx: CallContext, effort: str = READER_EFFORT
) -> OrderDocumentRead | LlmUnavailable:
    return _read(caller, "read_order_document", OrderDocumentRead, render_document(text), ctx, effort)  # type: ignore[return-value]


def read_visit_note(
    caller: Caller, text: str, ctx: CallContext, effort: str = READER_EFFORT
) -> VisitNoteRead | LlmUnavailable:
    return _read(caller, "read_visit_note", VisitNoteRead, render_document(text), ctx, effort)  # type: ignore[return-value]


def read_patient_reply(
    caller: Caller, turns: list[Turn], ctx: CallContext, effort: str = READER_EFFORT
) -> PatientReplyRead | LlmUnavailable:
    return _read(caller, "read_patient_reply", PatientReplyRead, render_turns(turns), ctx, effort)  # type: ignore[return-value]


def check_safety_and_conditions(
    caller: Caller, turns: list[Turn], ctx: CallContext, effort: str = READER_EFFORT
) -> SafetyRead | LlmUnavailable:
    return _read(caller, "check_safety_and_conditions", SafetyRead, render_turns(turns), ctx, effort)  # type: ignore[return-value]
