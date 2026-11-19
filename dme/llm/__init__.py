"""The Claude layer: `ClaudeLlm` implements the `Llm` protocol.

Settings come from constructor kwargs, then DME_LLM_MODE (live, replay or off),
DME_FAULT (llm_down), DME_EFFORT_READERS and DME_EFFORT_PROPOSER, then defaults.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from dme.llm import proposer, readers, writer
from dme.llm.client import Caller, CallRecord
from dme.models import (
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
from dme.protocols import CallContext

__all__ = ["ClaudeLlm"]

MODES = ("live", "replay", "off")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
FAULTS = (None, "llm_down")


def _pick(explicit: str | None, env: str, default: str) -> str:
    if explicit is not None:
        return explicit
    return os.environ.get(env, "").strip() or default


class ClaudeLlm:
    def __init__(
        self,
        *,
        mode: str | None = None,
        fault: str | None = None,
        reader_effort: str | None = None,
        proposer_effort: str | None = None,
        writer_effort: str | None = None,
        client: Any = None,
        replay_dir: Path | None = None,
        on_record: Callable[[CallRecord], None] | None = None,
    ) -> None:
        self.mode = _pick(mode, "DME_LLM_MODE", "live").lower()
        self.fault = _pick(fault, "DME_FAULT", "").strip().lower() or None
        if self.fault not in FAULTS:
            raise ValueError(f"fault must be one of {FAULTS[1:]} or empty, got {self.fault!r}")
        self.reader_effort = _pick(reader_effort, "DME_EFFORT_READERS", readers.READER_EFFORT).lower()
        self.proposer_effort = _pick(proposer_effort, "DME_EFFORT_PROPOSER", proposer.PROPOSER_EFFORT).lower()
        self.writer_effort = (writer_effort or writer.WRITER_EFFORT).lower()
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {self.mode!r}")
        for name in ("reader_effort", "proposer_effort", "writer_effort"):
            if getattr(self, name) not in EFFORTS:
                raise ValueError(f"{name} must be one of {EFFORTS}, got {getattr(self, name)!r}")
        self.client = client
        self.replay_dir = replay_dir
        self.on_record = on_record
        self.records: list[CallRecord] = []
        self.budget: Callable[[], int] | None = None  # requests the run's cap still allows (set by the Runtime)
        self.last_usage: dict[str, Any] = {"input_tokens": None, "output_tokens": None, "cost_usd": None}
        self.last_writer_result: writer.WriterResult | None = None

    # Plumbing

    def _caller(self) -> Caller:
        """A fresh Caller per call, so `last_usage` counts only that call's requests even under concurrency."""
        return Caller(
            mode=self.mode,
            fault=self.fault,
            client=self.client,  # type: ignore[arg-type]
            replay_dir=self.replay_dir,
            on_record=self._sink,
            budget=self.budget,
        )

    def _sink(self, record: CallRecord) -> None:
        self.records.append(record)
        if self.on_record is not None:
            self.on_record(record)

    def _usage(self, caller: Caller) -> None:
        live = [r for r in caller.records if r.reached_api]
        if not live:
            self.last_usage = {"input_tokens": None, "output_tokens": None, "cost_usd": None}
            return
        self.last_usage = {
            "input_tokens": sum(r.input_tokens for r in live),
            "output_tokens": sum(r.output_tokens for r in live),
            "cost_usd": round(sum(r.cost_usd for r in live), 6),
        }

    def _run(self, fn: Callable[..., Any], *args: Any) -> Any:
        caller = self._caller()
        try:
            return fn(caller, *args)
        finally:
            self._usage(caller)

    # Llm Protocol

    def read_supplier_call(self, turns: list[Turn], ctx: CallContext) -> SupplierCallRead | LlmUnavailable:
        return self._run(readers.read_supplier_call, turns, ctx, self.reader_effort)

    def read_pcp_reply(self, turns: list[Turn], ctx: CallContext) -> PcpReplyRead | LlmUnavailable:
        return self._run(readers.read_pcp_reply, turns, ctx, self.reader_effort)

    def read_order_document(self, text: str, ctx: CallContext) -> OrderDocumentRead | LlmUnavailable:
        return self._run(readers.read_order_document, text, ctx, self.reader_effort)

    def read_visit_note(self, text: str, ctx: CallContext) -> VisitNoteRead | LlmUnavailable:
        return self._run(readers.read_visit_note, text, ctx, self.reader_effort)

    def read_patient_reply(self, turns: list[Turn], ctx: CallContext) -> PatientReplyRead | LlmUnavailable:
        return self._run(readers.read_patient_reply, turns, ctx, self.reader_effort)

    def check_safety_and_conditions(self, turns: list[Turn], ctx: CallContext) -> SafetyRead | LlmUnavailable:
        return self._run(readers.check_safety_and_conditions, turns, ctx, self.reader_effort)

    def write_patient_message(
        self, purpose: MessagePurpose, facts: dict[str, str], ctx: CallContext
    ) -> PatientMessageDraft | LlmUnavailable:
        """The unfilled draft, or LlmUnavailable. Empty text means a human must answer."""
        result = self._run(writer.write_patient_message, purpose, facts, ctx, self.writer_effort)
        self.last_writer_result = result
        return result.as_draft()

    def propose_next_action(
        self, case_summary: str, allowed_actions: list[str], ctx: CallContext
    ) -> Proposal | LlmUnavailable:
        return self._run(proposer.propose_next_action, case_summary, allowed_actions, ctx, self.proposer_effort)
