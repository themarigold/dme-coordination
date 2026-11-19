"""The evidence check. The rules act only on reads that pass it.

It proves where a value came from (a quote really spoken by the counterparty), not
that it was read correctly. Quotes match on word boundaries, so "no" does not match
inside "know". Negation and hedge words near a quote are flagged for the log only.
"""

from __future__ import annotations

import re
from typing import TypeVar

from pydantic import BaseModel

from dme.models import CriterionRead, DatePhraseRead, IntentRead, SupplierCallRead, Turn, _Read

R = TypeVar("R", bound=BaseModel)

_QUOTES = str.maketrans(
    {"‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'", "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"'}
)
NEGATIONS = {
    "not",
    "no",
    "never",
    "don't",
    "doesn't",
    "isn't",
    "aren't",
    "can't",
    "cannot",
    "won't",
    "wouldn't",
    "didn't",
    "without",
    "nope",
}
HEDGES = {
    "maybe",
    "might",
    "probably",
    "possibly",
    "perhaps",
    "think",
    "guess",
    "hopefully",
    "should",
    "likely",
    "tentatively",
    "unsure",
    "not sure",
}
FLAG_WINDOW_WORDS = 4  # words before or after the quote that count as "next to" it
READBACK_TURN_ID = "readback"
WORD = re.compile(r"[a-z0-9']+")


_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)
_FULLWIDTH = str.maketrans({"\uff1c": "<", "\uff1e": ">", "\uff0f": "/"})


def fold_untrusted(text: str) -> str:
    """Remove zero-width characters and fold fullwidth < > / to ASCII.

    The prompt renderer and injection detection both use this, so a trick that
    slips past one cannot slip past the other.
    """
    return text.translate(_ZERO_WIDTH).translate(_FULLWIDTH)


def normalise(text: str) -> str:
    """Lower-case, straight quotes, single spaces."""
    return re.sub(r"\s+", " ", text.translate(_QUOTES).lower()).strip()


def find_quote(quote: str, text: str) -> int:
    """Index of the quote in the text after normalising both, on word boundaries; -1 if absent."""
    quote, text = normalise(quote), normalise(text)
    if not quote:
        return -1
    start = r"(?<!\w)" if re.match(r"\w", quote[0]) else ""
    end = r"(?!\w)" if re.match(r"\w", quote[-1]) else ""
    m = re.search(start + re.escape(quote) + end, text)
    return m.start() if m else -1


def document_turns(text: str, speaker: str = "pcp_office") -> list[Turn]:
    """A document is checked as one turn "doc" spoken by the counterparty."""
    return [Turn(turn_id="doc", speaker=speaker, text=text)]  # type: ignore[arg-type]


def _empty(field: _Read) -> _Read:
    update: dict[str, str] = {
        "status": "not_stated",
        "evidence_turn_id": "",
        "evidence_quote": "",
        "value": "missing" if isinstance(field, CriterionRead) else "",
    }
    if isinstance(field, DatePhraseRead):
        update["commitment"] = ""
    return field.model_copy(update=update)


def _check(field: _Read, turns_by_id: dict[str, Turn], roles: set[str]) -> str:
    """ "" if the evidence holds, else the reason it fails."""
    turn = turns_by_id.get(field.evidence_turn_id)
    if turn is None:
        return "unknown_turn"
    if turn.speaker not in roles:
        return "wrong_speaker"
    if not normalise(field.evidence_quote):
        return "empty_quote"
    if find_quote(field.evidence_quote, turn.text) < 0:
        return "quote_not_found"
    return ""


def _nearby_flags(field: _Read, turns_by_id: dict[str, Turn]) -> list[str]:
    text = normalise(turns_by_id[field.evidence_turn_id].text)
    quote = normalise(field.evidence_quote)
    start = find_quote(quote, text)
    before = WORD.findall(text[:start])[-FLAG_WINDOW_WORDS:]
    after = WORD.findall(text[start + len(quote) :])[:FLAG_WINDOW_WORDS]
    nearby = " ".join(before + WORD.findall(quote) + after)
    words = set(nearby.split())
    flags = [f"negation:{w}" for w in sorted(NEGATIONS & words)]
    flags += [
        f"hedge:{h}"
        for h in sorted(HEDGES)
        if (h in words if " " not in h else re.search(rf"(?<!\w){re.escape(h)}(?!\w)", nearby))
    ]
    return flags


def validate_read(read: R, turns: list[Turn], counterparty_roles: set[str]) -> tuple[R, dict[str, list[str]]]:
    """Return (validated copy, flags per field).

    A field keeps its value only if its quote appears in a turn spoken by one of
    `counterparty_roles`; otherwise it becomes not_stated. Failing intents are dropped.
    """
    turns_by_id = {t.turn_id: t for t in turns}
    flags: dict[str, list[str]] = {}
    update: dict[str, object] = {}

    def check_one(name: str, field: _Read) -> _Read | None:
        if field.status == "not_stated":
            return _empty(field)
        failure = _check(field, turns_by_id, counterparty_roles)
        if failure:
            flags[name] = [f"downgraded:{failure}"]
            return None
        nearby = _nearby_flags(field, turns_by_id)
        if nearby:
            flags[name] = nearby
        return field

    for name in type(read).model_fields:
        value = getattr(read, name)
        if isinstance(value, _Read):
            update[name] = check_one(name, value) or _empty(value)
        elif isinstance(value, list) and all(isinstance(v, IntentRead) for v in value):
            kept = []
            for i, intent in enumerate(value):
                if intent.status != "not_stated" and check_one(f"{name}[{i}]", intent) is not None:
                    kept.append(intent)
            update[name] = kept
    return read.model_copy(update=update), flags


def drop_flagged(read: R, flagged: set[str]) -> tuple[R, dict[str, list[str]]]:
    """Empty every field and drop every intent whose evidence is a `flagged` turn.

    Returns (clean copy, turn id -> the field names that turn claimed).
    """
    claims: dict[str, list[str]] = {}
    update: dict[str, object] = {}
    for name in type(read).model_fields:
        value = getattr(read, name)
        if isinstance(value, _Read) and value.status != "not_stated" and value.evidence_turn_id in flagged:
            claims.setdefault(value.evidence_turn_id, []).append(name)
            update[name] = _empty(value)
        elif isinstance(value, list) and all(isinstance(v, IntentRead) for v in value):
            for intent in value:
                if intent.evidence_turn_id in flagged:
                    claims.setdefault(intent.evidence_turn_id, []).append(f"intent {intent.value}")
            update[name] = [i for i in value if i.evidence_turn_id not in flagged]
    return read.model_copy(update=update), claims


def readback_position_ok(read: SupplierCallRead, turns: list[Turn]) -> bool:
    """True when the rep's "yes" comes from the turn right after our read-back turn (by position, not phrase)."""
    confirmed = read.readback_confirmed
    if confirmed.status != "found" or confirmed.value != "yes":
        return False
    for i, turn in enumerate(turns[:-1]):
        if turn.turn_id == READBACK_TURN_ID and turn.speaker == "us":
            reply = turns[i + 1]
            return reply.turn_id == confirmed.evidence_turn_id and reply.speaker != "us"
    return False
