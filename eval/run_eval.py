"""Eval grader: runs the LLM readers over a hand-labelled item set and grades each read in code.

Usage: uv run python eval/run_eval.py [--runs N] [--set {items,heldout,heldout_v2}] [--reader NAME]

Manifests: eval/MANIFEST.sha256 pins eval/items/ and this file on every run; a held-out set's own manifest
pins its directory when that set runs. An unlisted, missing, changed or symlinked file refuses the run
(exit 2); only listed YAML is loaded, and a suspected label error is reported, not fixed. A missing
production rule slice refuses (exit 4), and so does --runs > 1 with a replay Llm (exit 5): replays repeat
one recording, so pass^k and flips would measure nothing.

Scoring, on labelled fields only, after production's evidence check (failed quotes become not_stated):
- A field is correct when status, value and commitment match where labelled. Literals compare lower-case;
  IDs, codes and numbers exactly; other text as whole words in the read, within label length + 4 tokens.
  Ambiguous and contradictory match each other; on signal and condition fields "no" equals not stated.
- Date phrases match when dme.clock.resolve_date_phrase gives the same moment and level at every DATE_REFS
  time; if neither resolves, their date tokens must be equal.
- Criteria are graded as production records them (criterion_recorded mirrors pcp.py on_note).
- Safety recall uses production's triggers (dme.rules.signals) on items tagged "unsolicited". Signals
  production would raise from unlabelled fields are listed for a person to check, never scored.
- pass^k: an item passes all k runs; a flip: its per-field results differ between runs. An unavailable run
  counts every labelled field as incorrect. The report also gives confidently wrong, doubted, false
  empties, abstention, intents, and fingerprints of the production code and prompts graded against.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Literal, get_args, get_origin

import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dme import clock, evidence  # noqa: E402
from dme import models as m  # noqa: E402
from dme.protocols import CallContext  # noqa: E402
from dme.rules import signals as production_signals  # noqa: E402

GRADER_REL = "eval/run_eval.py"
MAIN_MANIFEST_REL = "eval/MANIFEST.sha256"
SETS: dict[str, str] = {"items": "eval/items", "heldout": "eval/heldout", "heldout_v2": "eval/heldout_v2"}
MANIFEST_NAME = "MANIFEST.sha256"
REPORTS_REL = "eval/reports"

READER_MODELS: dict[str, type] = {
    "read_supplier_call": m.SupplierCallRead,
    "read_pcp_reply": m.PcpReplyRead,
    "read_order_document": m.OrderDocumentRead,
    "read_visit_note": m.VisitNoteRead,
    "read_patient_reply": m.PatientReplyRead,
    "check_safety_and_conditions": m.SafetyRead,
}
DOCUMENT_READERS = {"read_order_document", "read_visit_note"}
COUNTERPARTY_ROLES: dict[str, set[str]] = {
    "read_supplier_call": {"supplier_rep"},
    "read_pcp_reply": {"pcp_office"},
    "read_order_document": {"pcp_office"},  # evidence.document_turns: turn "doc", spoken by pcp_office
    "read_visit_note": {"pcp_office"},
    "read_patient_reply": {"patient", "helper"},
    # runs on supplier, patient and PCP-office replies; each item holds one exchange
    "check_safety_and_conditions": {"supplier_rep", "pcp_office", "patient", "helper"},
}

NEGATIVE_EQUIVALENT: dict[str, set[str]] = {
    "check_safety_and_conditions": set(m.SAFETY_SIGNALS) | set(m.SUPPLIER_CONDITIONS) | {"abn_requested"},
    "read_supplier_call": set(m.SUPPLIER_CONDITIONS),
}
CONDITION_FIELDS = set(m.SUPPLIER_CONDITIONS) | {"abn_required", "abn_requested"}
PRESENCE_ONLY = {"other_condition"}
EXACT_FIELDS = {
    "medicare_id", "practitioner_npi", "hcpcs_code", "weight_lb", "order_date", "identity_dob", "quantity", "zip",
}
SAFETY_INTENTS = {"opt_out", "wants_person"}
DATE_FIELDS = {"call_back_after", "earliest_delivery", "send_by", "availability"}
TOKEN_SLACK = 4
SET_DIR_EXEMPT = {"README.md"}  # a set directory's README: never listed, never loaded
DOUBT = {"ambiguous", "contradictory"}
ABSENT_VALUES = {"", "no", "none"}
UNSOLICITED = "unsolicited"
SCORE_INTENTS = "score_intents"


class Refusal(Exception):
    """The grader will not run; the message says why."""


# Manifests


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dir_entries(root: Path, rel_dir: str) -> list[str]:
    """Every entry in a directory (dotfiles and subdirectories included), as repo paths."""
    d = root / rel_dir
    return sorted(f"{rel_dir}/{p.name}" for p in d.iterdir()) if d.is_dir() else []


def read_manifest(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        digest, _, rel = line.partition(" ")
        entries[rel.strip().lstrip("*")] = digest.lower()
    return entries


def _check_scope(root: Path, manifest_rel: str, targets: list[str], scope: Callable[[str], bool],
                 allowed: Callable[[str], bool]) -> list[str]:
    entries = read_manifest(root / manifest_rel)
    problems = []
    for rel in entries:
        if not allowed(rel):
            problems.append(f"{rel}: listed in {manifest_rel} but not allowed there")
    for rel in targets:
        p = root / rel
        if p.is_symlink():
            problems.append(f"{rel}: is a symlink")
        elif not p.is_file():
            problems.append(f"{rel}: not a regular file")
        elif rel not in entries:
            problems.append(f"{rel}: not in {manifest_rel}")
        elif sha256_file(p) != entries[rel]:
            problems.append(f"{rel}: sha256 differs from {manifest_rel}")
    for rel in entries:
        if scope(rel) and rel not in targets:
            problems.append(f"{rel}: in {manifest_rel} but missing on disk")
    return problems


def verify_manifests(root: Path, set_name: str) -> list[str]:
    """Problems with the manifests for the set being run; [] means OK."""
    items_rel = SETS["items"]
    problems: list[str] = []
    if not (root / MAIN_MANIFEST_REL).is_file():
        problems.append(f"{MAIN_MANIFEST_REL} is missing")
    elif not (root / items_rel).is_dir():
        problems.append(f"{items_rel}/ is missing")
    else:
        problems += _check_scope(
            root, MAIN_MANIFEST_REL, dir_entries(root, items_rel) + [GRADER_REL],
            lambda r: r == GRADER_REL or r.startswith(items_rel + "/"),
            lambda r: r == GRADER_REL or _item_path(r, items_rel),
        )
    if set_name != "items":
        rel_dir = SETS[set_name]
        manifest_rel = f"{rel_dir}/{MANIFEST_NAME}"
        if not (root / rel_dir).is_dir():
            problems.append(f"{rel_dir}/ is missing but --set {set_name} was requested")
        elif not (root / manifest_rel).is_file():
            problems.append(f"{manifest_rel} does not exist yet: the set is not ready (its author writes it)")
        else:
            exempt = {manifest_rel} | {f"{rel_dir}/{n}" for n in SET_DIR_EXEMPT}
            targets = [r for r in dir_entries(root, rel_dir) if r not in exempt]
            problems += _check_scope(root, manifest_rel, targets, lambda r: r.startswith(rel_dir + "/"),
                                     lambda r: _item_path(r, rel_dir))
    return problems


def _item_path(rel: str, rel_dir: str) -> bool:
    """A directly contained .yaml file of rel_dir."""
    return rel.startswith(rel_dir + "/") and "/" not in rel[len(rel_dir) + 1:] and rel.endswith(".yaml")


def set_manifest_rel(set_name: str) -> str:
    return MAIN_MANIFEST_REL if set_name == "items" else f"{SETS[set_name]}/{MANIFEST_NAME}"


# Items


def load_items(root: Path, set_name: str, reader: str | None = None) -> list[m.EvalItem]:
    """Loads only the YAML files the set's manifest lists (call verify_manifests first)."""
    rel_dir = SETS[set_name]
    listed = sorted(
        r for r in read_manifest(root / set_manifest_rel(set_name))
        if r.startswith(rel_dir + "/") and r.endswith(".yaml")
    )
    items = [m.EvalItem.model_validate(yaml.safe_load((root / r).read_text())) for r in listed]
    return [i for i in items if reader is None or i.reader == reader]


def field_class(reader: str, name: str) -> type:
    return READER_MODELS[reader].model_fields[name].annotation


def literal_values(cls: type) -> tuple[str, ...] | None:
    ann = cls.model_fields["value"].annotation
    return tuple(get_args(ann)) if get_origin(ann) is Literal else None


def item_turns(item: m.EvalItem) -> list[m.Turn]:
    return evidence.document_turns(item.text) if item.reader in DOCUMENT_READERS else list(item.turns)


# Grading

_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


def norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).translate(_QUOTES).lower()
    s = re.sub(r"[^\w\s]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def mostly_digits(s: str) -> bool:
    alnum = [c for c in s if c.isalnum()]
    return bool(alnum) and sum(c.isdigit() for c in alnum) * 2 >= len(alnum)


def has_word(s: str) -> bool:
    """A token of 3+ characters that contains a letter ("87th", "Kedzie")."""
    return any(len(t) >= 3 and any(c.isalpha() for c in t) for t in norm_text(s).split())


def exact_graded(name: str, label: str) -> bool:
    return (name in EXACT_FIELDS or len(norm_text(label)) < 3
            or (mostly_digits(label) and not has_word(label)))


_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
           "october", "november", "december"]
_DATE_ALIASES = {w[:3]: w for w in _WEEKDAYS + _MONTHS} | {
    "tues": "tuesday", "thur": "thursday", "thurs": "thursday", "sept": "september",
} | {w: w for w in _WEEKDAYS + _MONTHS}
_RELATIVE = {"today", "tonight", "tomorrow", "next", "week", "weekend", "month", "morning", "afternoon",
             "evening", "noon", "after", "before", "by", "until", "end", "business", "am", "pm"}
_UNITS = {"day": "day", "days": "day", "weeks": "week", "months": "month", "hour": "hour", "hours": "hour"}
_NUMBER_WORDS = {w: str(n) for w, n in clock.NUMBER_WORDS.items() if w != "a"}  # "a" is too common to be a number
# Numeric dates keep their component order: "1/12" -> 1_12, never the same as "11/2" -> 11_2.
_NUMERIC_DATE = re.compile(r"(?<![\w/-])(\d{1,4})([/-])(\d{1,2})(?:\2(\d{1,4}))?(?![\w/-])")
_MERIDIEM_TIME = re.compile(r"(?<![\w:])(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s?m\b\.?")
_CLOCK_TIME = re.compile(r"(?<![\w:])(\d{1,2}):(\d{2})(?![\w:])")
# Reference times for comparing resolved phrases: two weeks of mornings, afternoons and
# evenings (Thanksgiving 2026-11-26 included), so a match never rests on one coincidence.
DATE_REFS = tuple(clock.at(date(2026, 11, 16) + timedelta(days=d), time(h)) for d in range(14) for h in (8, 13, 18))


def _canon_numeric_dates(s: str) -> str:
    def sub(mt: re.Match) -> str:
        parts = [mt.group(1), mt.group(3)] + ([mt.group(4)] if mt.group(4) else [])
        return "_".join(parts)
    return _NUMERIC_DATE.sub(sub, s)


def _canon_times(s: str) -> str:
    """Clock times as 24-hour tokens ("2 p.m." -> 14h00, "14:30" -> 14h30); a bare "2" stays a number."""
    def meridiem(mt: re.Match) -> str:
        hour = int(mt.group(1))
        if not 1 <= hour <= 12:
            return mt.group(0)
        hour = hour % 12 + (12 if mt.group(3) == "p" else 0)
        return f" {hour}h{mt.group(2) or '00'} "
    s = _MERIDIEM_TIME.sub(meridiem, unicodedata.normalize("NFKC", s).lower())
    return _CLOCK_TIME.sub(lambda mt: f" {int(mt.group(1))}h{mt.group(2)} ", s)


def date_tokens(s: str) -> set[str]:
    out = set()
    for tok in norm_text(_canon_numeric_dates(_canon_times(s))).split():
        num = re.fullmatch(r"0*(\d+)(st|nd|rd|th)?", tok)
        if num:
            out.add(num.group(1))
        elif re.fullmatch(r"\d+h\d\d|\d+(_\d+)+|\d+(am|pm)", tok):  # a clock time, a numeric date, "13pm"
            out.add(tok)
        elif tok in _DATE_ALIASES:
            out.add(_DATE_ALIASES[tok])
        elif tok.endswith("s") and tok[:-1] in _WEEKDAYS:  # "Thursday's" -> thursdays
            out.add(tok[:-1])
        elif tok in _RELATIVE:
            out.add(tok)
        elif tok in _UNITS:
            out.add(_UNITS[tok])
        elif tok in _NUMBER_WORDS:
            out.add(_NUMBER_WORDS[tok])
    return out


def resolved_moments(phrase: str) -> tuple | None:
    """Production's resolution of the phrase, as a firm delivery date, at every reference time; None if it fails."""
    out = []
    for now in DATE_REFS:
        moment, level = clock.resolve_date_phrase(phrase, "firm", now, purpose="delivery")
        if moment is None:
            return None
        out.append((moment, level))
    return tuple(out)


def text_match(name: str, label: str, got: str) -> bool:
    a, b = norm_text(_canon_numeric_dates(label)), norm_text(_canon_numeric_dates(got))
    if not a or not b:
        return False
    if exact_graded(name, label):
        if name in EXACT_FIELDS:
            a, b = re.sub(r"[\s-]", "", a), re.sub(r"[\s-]", "", b)
        return a == b
    if len(b.split()) > len(a.split()) + TOKEN_SLACK:
        return False
    if name in DATE_FIELDS:
        lab_res, got_res = resolved_moments(label), resolved_moments(got)
        if lab_res is not None or got_res is not None:
            return lab_res == got_res
        if date_tokens(label):
            return date_tokens(got) == date_tokens(label)
    return re.search(rf"(?<!\w){re.escape(a)}(?!\w)", b) is not None


def _absent(status: str, value: str) -> bool:
    return status == "not_stated" or (status == "found" and norm_text(value) in ABSENT_VALUES)


def _present(status: str, value: str) -> bool:
    return status != "not_stated" and norm_text(value) not in ABSENT_VALUES


@dataclass
class FieldResult:
    field: str
    expected: dict[str, str]
    got: dict[str, str] | None  # validated read; None when the run was unavailable
    raw: dict[str, str] | None = None  # the read before the evidence check
    downgraded: bool = False  # the evidence check forced this field to not_stated
    correct: bool = False
    confidently_wrong: bool = False
    doubted: bool = False  # found label, read ambiguous/contradictory: incorrect, never confidently wrong
    false_empty: bool = False
    abstained: bool = False
    critical: bool = False
    safety_positive: bool = False
    safety_hit: bool = False


def criterion_recorded(status: str, value: str) -> str:
    """What production records for a criterion read: a mirror of pcp.py on_note, pinned to it by tests."""
    return value if status == "found" else "contradictory" if status == "contradictory" else "missing"


def signal_fires(reader: str, name: str, f: Any) -> bool:
    """Whether production raises safety signal `name` from field `f` of this reader's read.

    `f` needs only status and value, so a label can be asked the same question.
    """
    if name not in m.SAFETY_SIGNALS:
        return False
    if reader == "check_safety_and_conditions":
        return production_signals.fires(f)
    if reader == "read_patient_reply":
        blank = SimpleNamespace(status="not_stated", value="")
        read = SimpleNamespace(**{n: blank for n in READER_MODELS[reader].model_fields})
        read.intents = []
        setattr(read, name, f)
        return name in production_signals.patient_primary_signals(read)
    return False


def _label_field(label: m.EvalLabel) -> SimpleNamespace:
    return SimpleNamespace(status=label.status, value=label.value.strip().lower())


def _fdict(f: Any) -> dict[str, str]:
    return {"status": f.status, "value": str(f.value), "commitment": getattr(f, "commitment", ""),
            "evidence_turn_id": f.evidence_turn_id, "evidence_quote": f.evidence_quote}


def grade_field(reader: str, name: str, label: m.EvalLabel, got: Any | None, *, unsolicited: bool = False,
                raw: Any | None = None, downgraded: bool = False) -> FieldResult:
    cls = field_class(reader, name)
    neg = name in NEGATIVE_EQUIVALENT.get(reader, set())
    signal = name in m.SAFETY_SIGNALS and unsolicited
    critical = name in CONDITION_FIELDS or signal
    label_present = _present(label.status, label.value)
    expected = {"status": label.status, "value": label.value, "commitment": label.commitment}
    positive = signal and signal_fires(reader, name, _label_field(label))
    res = FieldResult(name, expected, None, critical=critical, safety_positive=positive)
    if got is None:
        return res
    res.got, res.raw, res.downgraded = _fdict(got), (_fdict(raw) if raw is not None else None), downgraded
    g_status, g_value = got.status, str(got.value)
    g_commit = getattr(got, "commitment", "")

    if cls is m.CriterionRead:
        recorded = criterion_recorded(g_status, g_value)
        correct = recorded == criterion_recorded(label.status, label.value.strip().lower())
        empty = recorded == "missing"
    else:
        if label.status in DOUBT:
            correct = g_status in DOUBT and (label.value == "" or _value_ok(reader, name, cls, label.value, g_value))
        elif neg and _absent(label.status, label.value):
            correct = _absent(g_status, g_value)
        elif name in PRESENCE_ONLY:
            correct = g_status == "found" and _present(g_status, g_value)
        else:
            correct = g_status == label.status
            if correct and label.value != "":
                correct = _value_ok(reader, name, cls, label.value, g_value)
        if correct and label.commitment != "" and label.status not in DOUBT:
            correct = g_commit.strip().lower() == label.commitment.strip().lower()
        if neg or name in PRESENCE_ONLY:
            empty = _absent(g_status, g_value) or g_value.strip() == ""
        else:
            empty = g_status == "not_stated" or g_value.strip() == ""

    res.correct = correct
    res.doubted = (not correct) and label.status == "found" and g_status in DOUBT
    res.confidently_wrong = (not correct) and not empty and not res.doubted
    res.false_empty = critical and label_present and empty
    if cls is m.CriterionRead:  # a doubtful read is "doubted", never also abstained
        res.abstained = label.value == "documented" and empty and g_status not in DOUBT
    else:
        res.abstained = label.status == "found" and g_status == "not_stated"
    res.safety_hit = res.safety_positive and signal_fires(reader, name, got)
    return res


def _value_ok(reader: str, name: str, cls: type, label: str, got: str) -> bool:
    if literal_values(cls) is not None:
        return got.strip().lower() == label.strip().lower()
    return text_match(name, label, got)


@dataclass
class RunResult:
    run: int
    fields: list[FieldResult]
    unavailable: str = ""  # reason, "" when the call returned a read
    evidence_flags: dict[str, list[str]] = field(default_factory=dict)
    downgraded_fields: list[str] = field(default_factory=list)  # every field the evidence check downgraded
    raw_read: dict[str, Any] | None = None
    intents_scored: bool = False
    intents_expected: list[str] = field(default_factory=list)
    intents_got: list[str] = field(default_factory=list)  # after the evidence check
    intents_missing: list[str] = field(default_factory=list)
    intents_extra: list[str] = field(default_factory=list)
    passed: bool = False
    usage: dict[str, float] | None = None
    unlabelled_signal_sources: int = 0  # signal sources the item leaves unlabelled (run returned a read)
    unlabelled_signals: list[dict[str, str]] = field(default_factory=list)  # those production would raise

    def signature(self) -> tuple:
        return (tuple(f.correct for f in self.fields), tuple(self.intents_missing), bool(self.unavailable))


def _fire(source: str, f: Any) -> dict[str, str]:
    return {"field": source, "status": f.status, "value": str(f.value), "evidence_turn_id": f.evidence_turn_id,
            "evidence_quote": f.evidence_quote}


def unlabelled_signals(item: m.EvalItem, validated: Any) -> tuple[int, list[dict[str, str]]]:
    """(signal sources the item leaves unlabelled, the ones production would raise); reported, never scored."""
    if item.reader == "check_safety_and_conditions":
        sources = [s for s in m.SAFETY_SIGNALS if s not in item.labels]
        fired = set(production_signals.safety_signals(validated))
        return len(sources), [_fire(s, getattr(validated, s)) for s in sources if s in fired]
    if item.reader == "read_patient_reply":
        fired = production_signals.patient_primary_signals(validated)
        sources, out = 0, []
        if "medicaid_or_msp" not in item.labels:
            sources += 1
            if "medicaid_or_msp" in fired:
                out.append(_fire("medicaid_or_msp", validated.medicaid_or_msp))
        if SCORE_INTENTS not in item.tags:  # intents unscored: a safety intent would be invisible
            for signal, intent in (("opts_out", "opt_out"), ("wants_person", "wants_person")):
                sources += 1
                if signal in fired:  # one entry per signal (its first intent), so the count never exceeds sources
                    out.append(_fire(f"intents:{intent}", next(i for i in validated.intents if i.value == intent)))
        return sources, out
    return 0, []


def grade_read(item: m.EvalItem, read: Any, run: int) -> RunResult:
    model_cls = READER_MODELS[item.reader]
    unavailable = ""
    if isinstance(read, m.LlmUnavailable):
        unavailable = f"{read.reason}: {read.detail}".rstrip(": ")
    elif isinstance(read, Exception):
        unavailable = f"exception: {type(read).__name__}: {read}"
    elif not isinstance(read, model_cls):
        unavailable = f"wrong_type: {type(read).__name__}"
    unsolicited = UNSOLICITED in item.tags
    if unavailable:
        fields = [grade_field(item.reader, n, lab, None, unsolicited=unsolicited) for n, lab in item.labels.items()]
        res = RunResult(run=run, fields=fields, unavailable=unavailable)
        if SCORE_INTENTS in item.tags:
            res.intents_scored = True
            res.intents_expected = sorted(set(item.intents))
            res.intents_missing = list(res.intents_expected)
        return res

    validated, flags = evidence.validate_read(read, item_turns(item), COUNTERPARTY_ROLES[item.reader])
    downgraded = sorted(k for k, v in flags.items() if any(f.startswith("downgraded:") for f in v))
    fields = [
        grade_field(item.reader, n, lab, getattr(validated, n), unsolicited=unsolicited, raw=getattr(read, n),
                    downgraded=n in flags and n in downgraded)
        for n, lab in item.labels.items()
    ]
    res = RunResult(run=run, fields=fields, evidence_flags=flags, downgraded_fields=downgraded,
                    raw_read=read.model_dump())
    res.unlabelled_signal_sources, res.unlabelled_signals = unlabelled_signals(item, validated)
    if SCORE_INTENTS in item.tags:
        exp = set(item.intents)
        got = {i.value for i in validated.intents}
        res.intents_scored = True
        res.intents_expected = sorted(exp)
        res.intents_got = sorted(got)
        res.intents_missing = sorted(exp - got)
        res.intents_extra = sorted(got - exp)
    res.passed = (all(f.correct for f in fields) and not res.intents_missing
                  and not (set(res.intents_extra) & SAFETY_INTENTS))
    return res


# Running


def build_context(item: m.EvalItem, rule_slice: str) -> CallContext:
    return CallContext(
        case_id=f"eval-{item.item_id}",
        goal=item.goal,
        facts=dict(item.context_facts),
        rule_slice=rule_slice,
        row_key=f"eval|{item.item_id}",  # stable across runs: each run repeats the same prompt
    )


def load_rule_slicer() -> Callable[[str], str]:
    """dme.knowledge.rule_slice, or Refusal: without it the eval would grade a different prompt."""
    try:
        from dme import knowledge

        return knowledge.rule_slice
    except (ImportError, AttributeError) as e:
        raise Refusal(f"dme.knowledge.rule_slice cannot be imported ({e})") from e


def _read_usage(llm: Any) -> dict[str, float] | None:
    u = getattr(llm, "last_usage", None)
    if u is None:
        return None
    get = u.get if isinstance(u, dict) else (lambda k, d=None: getattr(u, k, d))
    vals = {k: get(k, None) for k in ("input_tokens", "output_tokens", "cost_usd")}
    if all(v is None for v in vals.values()):
        return None
    return {k: float(v) for k, v in vals.items() if v is not None}


def call_reader(llm: Any, item: m.EvalItem, ctx: CallContext) -> Any:
    arg = item.text if item.reader in DOCUMENT_READERS else item.turns
    try:
        return getattr(llm, item.reader)(arg, ctx)
    except Exception as e:  # noqa: BLE001 - one failing call must not lose a paid run
        return e


@dataclass
class ItemResult:
    item_id: str
    reader: str
    tags: list[str]
    runs: list[RunResult]

    @property
    def pass_k(self) -> bool:
        return bool(self.runs) and all(r.passed for r in self.runs)

    @property
    def flipped(self) -> bool:
        return len({r.signature() for r in self.runs}) > 1


def run_items(llm: Any, items: list[m.EvalItem], runs: int, rule_slicer: Callable[[str], str]) -> list[ItemResult]:
    slices: dict[str, str] = {}
    results = []
    for item in items:
        if item.reader not in slices:
            slices[item.reader] = rule_slicer(item.reader)
        ctx = build_context(item, slices[item.reader])
        item_runs = []
        for r in range(1, runs + 1):
            read = call_reader(llm, item, ctx)
            rr = grade_read(item, read, r)
            rr.usage = _read_usage(llm)
            item_runs.append(rr)
        results.append(ItemResult(item.item_id, item.reader, list(item.tags), item_runs))
    return results


def _rate(num: int, den: int) -> dict[str, Any]:
    return {"k": num, "n": den, "rate": (num / den) if den else None}


def summarize(results: list[ItemResult], runs: int) -> dict[str, Any]:
    per_field: dict[str, list[int]] = {}
    obs = obs_avail = cw = fe = crit_pos = abst = found_labels = doubted = 0
    sfp = sfp_n = 0
    unavailable_runs = unavailable_obs = 0
    s_pos = s_hit = 0
    unlab_n = 0
    unlab_fires: list[dict[str, Any]] = []
    downgraded_all = downgraded_labelled = 0
    intents: dict[str, dict[str, int]] = {}
    usage = {"input_tokens": 0.0, "output_tokens": 0.0, "cost_usd": 0.0}
    usage_seen: set[str] = set()
    for ir in results:
        unsolicited = UNSOLICITED in ir.tags
        for rr in ir.runs:
            for k, v in (rr.usage or {}).items():
                usage[k] += v
                usage_seen.add(k)
            if rr.unavailable:
                unavailable_runs += 1
            downgraded_all += len(rr.downgraded_fields)
            unlab_n += rr.unlabelled_signal_sources
            unlab_fires += [{"item_id": ir.item_id, "run": rr.run, **f} for f in rr.unlabelled_signals]
            for f in rr.fields:
                key = f"{ir.reader}.{f.field}"
                c = per_field.setdefault(key, [0, 0])
                c[0] += int(f.correct)
                c[1] += 1
                obs += 1
                if f.safety_positive:
                    s_pos += 1
                    s_hit += int(f.safety_hit)
                if rr.unavailable:
                    unavailable_obs += 1
                    continue
                obs_avail += 1
                downgraded_labelled += int(f.downgraded)
                cw += int(f.confidently_wrong)
                doubted += int(f.doubted)
                if f.critical and _present(f.expected["status"], f.expected["value"]):
                    crit_pos += 1
                    fe += int(f.false_empty)
                if f.expected["status"] == "found" or f.expected["value"] == "documented":
                    found_labels += 1
                    abst += int(f.abstained)
            if rr.intents_scored and not rr.unavailable:
                for si in SAFETY_INTENTS - set(rr.intents_expected):
                    sfp_n += 1
                    sfp += int(si in rr.intents_extra)
            if rr.intents_scored:
                for i in rr.intents_expected:
                    d = intents.setdefault(i, {"tp": 0, "extra": 0, "missed": 0})
                    d["missed" if i in rr.intents_missing else "tp"] += 1
                for i in rr.intents_extra:
                    intents.setdefault(i, {"tp": 0, "extra": 0, "missed": 0})["extra"] += 1
                if unsolicited:
                    for si in set(rr.intents_expected) & SAFETY_INTENTS:
                        s_pos += 1
                        s_hit += int(si not in rr.intents_missing)
                        if not rr.unavailable:
                            crit_pos += 1
                            fe += int(si in rr.intents_missing)
    correct = sum(c[0] for c in per_field.values())
    tp = sum(d["tp"] for d in intents.values())
    extra = sum(d["extra"] for d in intents.values())
    missed = sum(d["missed"] for d in intents.values())
    return {
        "items": len(results),
        "runs_per_item": runs,
        "item_runs": sum(len(ir.runs) for ir in results),
        "field_accuracy": _rate(correct, obs),
        "per_field": {k: _rate(v[0], v[1]) for k, v in sorted(per_field.items())},
        "confidently_wrong": _rate(cw, obs_avail),
        "false_empties": _rate(fe, crit_pos),
        "confidently_wrong_incl_false_empties": _rate(cw + fe, obs_avail),
        "abstention": _rate(abst, found_labels),
        "doubted": _rate(doubted, found_labels),
        "safety_signal_recall": _rate(s_hit, s_pos),
        "unlabelled_safety_signals": _rate(len(unlab_fires), unlab_n),
        "unlabelled_safety_signal_fires": unlab_fires,
        "intents_precision": _rate(tp, tp + extra),
        "intents_recall": _rate(tp, tp + missed),
        "intents_extras": _rate(extra, tp + extra),
        "safety_intent_false_positive": _rate(sfp, sfp_n),
        "intents_per_intent": {
            i: {**d, "precision": _rate(d["tp"], d["tp"] + d["extra"])["rate"],
                "recall": _rate(d["tp"], d["tp"] + d["missed"])["rate"]}
            for i, d in sorted(intents.items())
        },
        f"pass^{runs}": _rate(sum(ir.pass_k for ir in results), len(results)),
        "flips": _rate(sum(ir.flipped for ir in results), len(results)),
        "unavailable": _rate(unavailable_runs, sum(len(ir.runs) for ir in results)),
        "unavailable_field_observations": unavailable_obs,
        "evidence_downgrades_labelled": _rate(downgraded_labelled, obs_avail),
        "evidence_downgrades_all_fields": downgraded_all,
        "usage": (
            {k: (round(usage[k], 6) if k == "cost_usd" else int(usage[k])) if k in usage_seen else "not reported"
             for k in usage}
            if usage_seen
            else "not reported"
        ),
    }


def _fmt(r: dict[str, Any]) -> str:
    return f"{r['k']}/{r['n']}" + (f" ({r['rate']:.0%})" if r["rate"] is not None else " (n/a)")


def print_report(summary: dict[str, Any], results: list[ItemResult], out=None) -> None:
    out = out or sys.stdout
    runs = summary["runs_per_item"]
    print(f"{'field':<52} {'correct':>14}", file=out)
    for k, r in summary["per_field"].items():
        print(f"{k:<52} {_fmt(r):>14}", file=out)
    print("", file=out)
    for key in (
        "field_accuracy", "confidently_wrong", "false_empties", "confidently_wrong_incl_false_empties",
        "abstention", "doubted", "safety_signal_recall", "unlabelled_safety_signals", "intents_precision",
        "intents_recall", "intents_extras", "safety_intent_false_positive",
        f"pass^{runs}", "flips", "unavailable", "evidence_downgrades_labelled",
    ):
        print(f"{key:<40} {_fmt(summary[key])}", file=out)
    for f in summary["unlabelled_safety_signal_fires"]:  # each one for a person to check against the item text
        print(f"  unlabelled signal: {f['item_id']} run {f['run']} {f['field']} {f['status']} {f['value']!r} "
              f"quote {f['evidence_turn_id']}: {f['evidence_quote']!r}", file=out)
    print(f"{'evidence_downgrades_all_fields':<40} {summary['evidence_downgrades_all_fields']}", file=out)
    print(f"{'usage':<40} {summary['usage']}", file=out)
    failing = [ir.item_id + (" (flip)" if ir.flipped else "") for ir in results if not ir.pass_k]
    print(f"items failing pass^{runs}: {', '.join(failing) if failing else 'none'}", file=out)


def code_fingerprints() -> dict[str, str]:
    """sha256 of the production code and knowledge the grade depends on (recorded, not enforced)."""
    dme_dir = Path(m.__file__).resolve().parent
    paths = [Path(evidence.__file__), Path(m.__file__), Path(clock.__file__), Path(production_signals.__file__),
             dme_dir / "rules" / "gate.py", dme_dir / "rules" / "pcp.py"]
    paths += sorted(p for p in (dme_dir / "llm" / "prompts").rglob("*") if p.is_file())
    try:
        from dme import knowledge

        kpath = Path(knowledge.__file__)
        paths += [kpath] + sorted((kpath.parent / "knowledge").glob("*.yaml"))
    except ImportError:
        pass
    base = Path(m.__file__).resolve().parent.parent
    out = {}
    for p in paths:
        rp = p.resolve()
        key = str(rp.relative_to(base)) if rp.is_relative_to(base) else str(rp)
        out[key] = sha256_file(rp)
    return out


NOT_REPORTED = "not reported"


def llm_settings(llm: Any) -> dict[str, str]:
    """What the Llm says about itself: class, model, reader effort and mode."""
    cls = type(llm)
    model = getattr(llm, "model", None)
    if model is None and cls.__module__.startswith("dme.llm"):
        try:
            from dme.llm.client import MODEL as model
        except ImportError:
            model = None
    return {
        "llm_class": f"{cls.__module__}.{cls.__qualname__}",
        "model": str(model or NOT_REPORTED),
        "reader_effort": str(getattr(llm, "reader_effort", None) or NOT_REPORTED),
        "llm_mode": str(getattr(llm, "mode", None) or NOT_REPORTED),
    }


def write_report(root: Path, set_name: str, reader: str | None, summary: dict, results: list[ItemResult],
                 settings: dict[str, str] | None = None) -> Path:
    out_dir = root / REPORTS_REL
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{stamp}-{set_name}{'-' + reader if reader else ''}.json"
    payload = {
        "set": set_name,
        "reader_filter": reader or "",
        "created_utc": stamp,
        "llm": settings or {},
        "code_fingerprints": code_fingerprints(),
        "summary": summary,
        "items": [
            {"item_id": ir.item_id, "reader": ir.reader, "tags": ir.tags, "pass_k": ir.pass_k,
             "flipped": ir.flipped, "runs": [asdict(r) for r in ir.runs]}
            for ir in results
        ],
    }
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path


def main(argv: list[str] | None = None, llm: Any = None, root: Path = ROOT,
         rule_slicer: Callable[[str], str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Grade the DME readers on a hand-written eval set.")
    ap.add_argument("--runs", type=int, default=1, help="runs per item (pass^k uses k = runs)")
    ap.add_argument("--set", dest="set_name", choices=sorted(SETS), default=None, help="eval set (default items)")
    ap.add_argument("--heldout", action="store_true", help="alias for --set heldout")
    ap.add_argument("--reader", choices=sorted(READER_MODELS), help="only items for this reader")
    args = ap.parse_args(argv)
    if args.runs < 1:
        ap.error("--runs must be >= 1")
    if args.heldout and args.set_name not in (None, "heldout"):
        ap.error("--heldout conflicts with --set " + args.set_name)
    set_name = "heldout" if args.heldout else (args.set_name or "items")

    problems = verify_manifests(root, set_name)
    if problems:
        print("REFUSING TO RUN: the eval manifests do not match the eval files.", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("Eval items and the grader are hash-pinned; regenerate the manifest only after a deliberate, "
              "reviewed change.", file=sys.stderr)
        return 2

    items = load_items(root, set_name, args.reader)
    if not items:
        print("No eval items selected.", file=sys.stderr)
        return 2

    try:
        slicer = rule_slicer or load_rule_slicer()
        slices: dict[str, str] = {}
        for reader in sorted({i.reader for i in items}):
            try:
                slices[reader] = slicer(reader)
            except Exception as e:  # noqa: BLE001 - any failure here means no production slice
                raise Refusal(f"rule_slice({reader!r}) failed: {type(e).__name__}: {e}") from e
    except Refusal as e:
        print(f"REFUSING TO RUN: {e}. The eval must send the production rule slice.", file=sys.stderr)
        return 4

    if llm is None:
        try:
            from dme.llm import ClaudeLlm
        except ImportError as e:
            print(f"Cannot import dme.llm.ClaudeLlm ({e}). Pass an Llm "
                  "to run_eval.main(llm=...).", file=sys.stderr)
            return 3
        llm = ClaudeLlm()

    settings = llm_settings(llm)
    if args.runs > 1 and settings["llm_mode"] == "replay":
        print(f"REFUSING TO RUN: --runs {args.runs} with LLM mode replay. Every replayed run returns the same "
              "recording, so pass^k and flips would measure nothing. Use --runs 1, or a live Llm.", file=sys.stderr)
        return 5

    results = run_items(llm, items, args.runs, slices.__getitem__)
    summary = summarize(results, args.runs)
    print(f"set={set_name} items={len(items)} runs={args.runs}" + (f" reader={args.reader}" if args.reader else "")
          + f" model={settings['model']} effort={settings['reader_effort']} mode={settings['llm_mode']}")
    print_report(summary, results)
    path = write_report(root, set_name, args.reader, summary, results, settings)
    print(f"report: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
