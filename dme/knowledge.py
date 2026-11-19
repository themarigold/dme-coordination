"""Loader for the cited Medicare knowledge file, shared by the rules and the prompts."""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import yaml

KNOWLEDGE_FILE = Path(__file__).parent / "knowledge" / "medicare_k0001.yaml"

# Each reader sees only the knowledge topics relevant to its call.
READER_TOPICS: dict[str, tuple[str, ...]] = {
    "read_supplier_call": ("supplier",),
    "read_pcp_reply": ("pcp",),
    "read_order_document": ("order",),
    "read_visit_note": ("visit_note",),
    "read_patient_reply": ("patient",),
    "check_safety_and_conditions": ("safety", "supplier"),
}


@cache
def load() -> dict[str, Any]:
    """The parsed knowledge file (cached; treat as read-only)."""
    return yaml.safe_load(KNOWLEDGE_FILE.read_text())


def entry(entry_id: str) -> dict[str, Any]:
    for e in load()["entries"]:
        if e["id"] == entry_id:
            return e
    raise KeyError(f"no knowledge entry {entry_id!r}")


def entries_for(*topics: str) -> list[dict[str, Any]]:
    """Entries tagged with any of `topics`, in file order."""
    return [e for e in load()["entries"] if set(e["topics"]) & set(topics)]


def topic_slice(*topics: str) -> str:
    """The entries for `topics` as YAML text, citations included."""
    picked = [{k: v for k, v in e.items() if k != "topics"} for e in entries_for(*topics)]
    return yaml.safe_dump(picked, sort_keys=False, allow_unicode=True, width=100)


def rule_slice(reader_name: str) -> str:
    """The knowledge excerpt (with citations) for one reader (a models.ReaderName)."""
    if reader_name not in READER_TOPICS:
        raise KeyError(f"unknown reader {reader_name!r}")
    return topic_slice(*READER_TOPICS[reader_name])


def code_entry(code: str) -> dict[str, Any] | None:
    """The code-table entry for an HCPCS code as written, or None; exact match, so misreads stay unknown."""
    return entry("code_table")["codes"].get(code.strip().upper())
