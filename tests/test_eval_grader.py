"""The eval items validate, and the grader scores, refuses and reports as intended (stub Llms only)."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

import pytest
import yaml

from dme import facts
from dme import models as m

ROOT = Path(__file__).resolve().parent.parent


def _load_grader():
    spec = importlib.util.spec_from_file_location("run_eval", ROOT / "eval" / "run_eval.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_eval"] = mod
    spec.loader.exec_module(mod)
    return mod


ev = _load_grader()

ITEM_FILES = sorted((ROOT / "eval/items").glob("*.yaml"))
# A checkout without eval/heldout/ (kept out of tuning on purpose) skips the held-out tests.
HELDOUT_PRESENT = (ROOT / "eval/heldout").is_dir()
HELDOUT_FILES = sorted((ROOT / "eval/heldout").glob("*.yaml"))
needs_heldout = pytest.mark.skipif(not HELDOUT_PRESENT, reason="eval/heldout/ not present in this worktree")
ALL_FILES = ITEM_FILES + HELDOUT_FILES

REQUIRED_CATEGORIES = {
    "negated_stock",
    "hedged_date",
    "volunteered_early",
    "buried_opt_out",
    "coinsurance_vs_full_price",
    "wrong_item_code",
    "correction",
    "missing_criterion",
    "no_weight",
    "injection",
    "hospital",
    "medicaid",
    "pcp_not_received",
    "caregiver",
}
CONVERSATION_ROLES = {
    "read_supplier_call": {"us", "supplier_rep"},
    "read_pcp_reply": {"us", "pcp_office"},
    "read_patient_reply": {"us", "patient", "helper"},
    "check_safety_and_conditions": {"us", "supplier_rep", "pcp_office", "patient", "helper"},
}


def _item(path: Path) -> m.EvalItem:
    return m.EvalItem.model_validate(yaml.safe_load(path.read_text()))


def _by_id(iid: str) -> m.EvalItem:
    for p in ALL_FILES:
        if p.stem == iid:
            return _item(p)
    raise KeyError(iid)


# -- stub reads ------------------------------------------------------------------------------------


def blank_read(model_cls: type) -> dict:
    data: dict = {}
    for name, f in model_cls.model_fields.items():
        if name == "intents":
            data[name] = []
            continue
        cls = f.annotation
        d = {
            "evidence_turn_id": "",
            "evidence_quote": "",
            "status": "not_stated",
            "value": "missing" if cls is m.CriterionRead else "",
        }
        if cls is m.DatePhraseRead:
            d["commitment"] = ""
        data[name] = d
    return data


def make_read(model_cls: type, **fields):
    data = blank_read(model_cls)
    for k, v in fields.items():
        data[k] = v if k == "intents" else {**data[k], **v}
    return model_cls.model_validate(data)


def got(status: str, value: str = "", turn: str = "t2", quote: str = "quoted text", **kw) -> dict:
    ev_id, q = ("", "") if status == "not_stated" else (turn, quote)
    return {"evidence_turn_id": ev_id, "evidence_quote": q, "status": status, "value": value, **kw}


def real_evidence(item: m.EvalItem) -> tuple[str, str]:
    """A (turn id, quote) that passes the evidence check for this item."""
    roles = ev.COUNTERPARTY_ROLES[item.reader]
    turn = next(t for t in ev.item_turns(item) if t.speaker in roles)
    return turn.turn_id, turn.text.strip().splitlines()[0]


def perfect_read(item: m.EvalItem):
    """The read a perfect reader would return, built from the labels, with real evidence."""
    model_cls = ev.READER_MODELS[item.reader]
    tid, quote = real_evidence(item)
    fields = {}
    for name, lab in item.labels.items():
        cls = model_cls.model_fields[name].annotation
        value = lab.value if lab.value or cls is not m.CriterionRead else "missing"
        d = got(lab.status, value, turn=tid, quote=quote)
        if cls is m.DatePhraseRead:
            d["commitment"] = lab.commitment
        fields[name] = d
    if item.intents:
        fields["intents"] = [got("found", i, turn=tid, quote=quote) for i in item.intents]
    return make_read(model_cls, **fields)


class StubLlm:
    """Serves reads per item id (from ctx.case_id), cycling through a list per call."""

    def __init__(self, script: dict[str, list], usage: dict | None = None):
        self.script = script
        self.calls: Counter = Counter()
        self.contexts: list = []
        self.usage = usage if usage is not None else {"input_tokens": 100, "output_tokens": 20, "cost_usd": 0.0008}
        self.last_usage = {"input_tokens": None, "output_tokens": None, "cost_usd": None}

    def _serve(self, arg, ctx):
        self.contexts.append(ctx)
        iid = ctx.case_id.removeprefix("eval-")
        seq = self.script[iid]
        out = seq[self.calls[iid] % len(seq)]
        self.calls[iid] += 1
        self.last_usage = dict(self.usage)
        if isinstance(out, Exception):
            raise out
        return out

    read_supplier_call = read_pcp_reply = read_order_document = _serve
    read_visit_note = read_patient_reply = check_safety_and_conditions = _serve


def _run(llm, items, runs=1):
    return ev.run_items(llm, items, runs, lambda reader: "")


# -- item files ------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: p.name)
def test_every_item_file_validates(path):
    item = _item(path)
    assert item.item_id == path.stem
    assert item.goal.strip()
    model_cls = ev.READER_MODELS[item.reader]
    if item.reader in ev.DOCUMENT_READERS:
        assert item.text.strip() and not item.turns
    else:
        assert item.turns and not item.text
        ids = [t.turn_id for t in item.turns]
        assert len(ids) == len(set(ids))
        assert {t.speaker for t in item.turns} <= CONVERSATION_ROLES[item.reader]
    assert item.labels
    for name, lab in item.labels.items():
        assert name in model_cls.model_fields and name != "intents", name
        cls = model_cls.model_fields[name].annotation
        if lab.status == "found":
            assert lab.value, f"{name}: a found label needs a value"
        allowed = ev.literal_values(cls)
        if allowed is not None and lab.value:
            assert lab.value in allowed, f"{name}: {lab.value!r} not in {allowed}"
        if allowed is None and lab.value and not ev.exact_graded(name, lab.value) and name not in ev.PRESENCE_ONLY:
            # a containment label must not be a bare short number (it would match inside other numbers)
            toks = ev.norm_text(lab.value).split()
            assert len(toks) >= 2 or not any(c.isdigit() for c in lab.value), f"{name}: {lab.value!r}"
        if lab.commitment:
            assert cls is m.DatePhraseRead and lab.commitment in ("firm", "tentative"), name
        if cls is m.CriterionRead:
            assert (lab.status == "found") == (lab.value == "documented"), name
    if item.intents or ev.SCORE_INTENTS in item.tags:
        assert item.reader == "read_patient_reply"
    for fid in item.context_facts:
        assert facts.is_known(fid), fid


def test_item_ids_unique_and_set_sizes():
    ids = [_item(p).item_id for p in ALL_FILES]
    assert len(ids) == len(set(ids))
    assert 18 <= len(ITEM_FILES) <= 32
    if HELDOUT_PRESENT:
        assert 5 <= len(HELDOUT_FILES) <= 10


@pytest.mark.parametrize(
    "files", [ITEM_FILES, pytest.param(HELDOUT_FILES, marks=needs_heldout)], ids=["items", "heldout"]
)
def test_each_set_covers_every_reader_and_category(files):
    items = [_item(p) for p in files]
    assert {i.reader for i in items} == set(ev.READER_MODELS)
    tags = {t for i in items for t in i.tags}
    assert REQUIRED_CATEGORIES <= tags, REQUIRED_CATEGORIES - tags


@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: p.name)
def test_perfect_reads_score_full_marks(path):
    item = _item(path)
    res = ev.grade_read(item, perfect_read(item), 1)
    assert res.passed, [(f.field, f.expected, f.got) for f in res.fields if not f.correct]
    assert not res.downgraded_fields


# -- field scoring ---------------------------------------------------------------------------------


def L(status, value="", commitment=""):
    return m.EvalLabel(status=status, value=value, commitment=commitment)


def _field(reader, name, d):
    return getattr(make_read(ev.READER_MODELS[reader], **{name: d}), name)


def _grade(reader, name, label, d, **kw):
    return ev.grade_field(reader, name, label, _field(reader, name, d), **kw)


def test_correct_and_wrong_literal():
    r = _grade("read_supplier_call", "k0001_in_stock", L("found", "no"), got("found", "no"))
    assert r.correct and not r.confidently_wrong
    r = _grade("read_supplier_call", "k0001_in_stock", L("found", "no"), got("found", "yes"))
    assert not r.correct and r.confidently_wrong and not r.abstained


def test_status_mismatch_with_value_is_confidently_wrong():
    r = _grade("read_patient_reply", "delivery_received", L("not_stated"), got("found", "yes"))
    assert not r.correct and r.confidently_wrong


def test_abstention_on_non_critical_field():
    r = _grade("read_supplier_call", "accepting_new_medicare", L("found", "yes"), got("not_stated"))
    assert not r.correct and r.abstained and not r.false_empty and not r.confidently_wrong


def test_false_empty_on_unsolicited_safety_signal():
    r = _grade("check_safety_and_conditions", "opts_out", L("found", "yes"), got("not_stated"), unsolicited=True)
    assert not r.correct and r.false_empty and r.abstained and r.safety_positive and not r.safety_hit


def test_found_no_on_a_negative_equivalent_positive_is_a_false_empty():
    r = _grade("check_safety_and_conditions", "hospitalised", L("found", "yes"), got("found", "no"), unsolicited=True)
    assert not r.correct and r.false_empty and not r.confidently_wrong
    r = _grade("read_supplier_call", "collects_coinsurance_at_delivery", L("found", "yes"), got("found", "no"))
    assert r.false_empty and not r.confidently_wrong
    # a fixed question (not negative-equivalent): "no" for "yes" stays confidently wrong
    r = _grade("read_supplier_call", "abn_required", L("found", "yes"), got("found", "no"))
    assert r.confidently_wrong and not r.false_empty


def test_false_empty_on_supplier_condition_and_abn():
    r = _grade("read_supplier_call", "collects_coinsurance_at_delivery", L("found", "yes"), got("found", ""))
    assert r.false_empty and not r.safety_positive
    r = _grade("read_supplier_call", "abn_required", L("found", "yes"), got("not_stated"))
    assert r.false_empty


def test_negative_equivalence_only_where_contract_allows():
    for g, ok in [(got("not_stated"), True), (got("found", "no"), True), (got("found", "yes"), False)]:
        r = _grade("check_safety_and_conditions", "opts_out", L("not_stated"), g)
        assert r.correct is ok
        if not ok:
            assert r.confidently_wrong
    r = _grade("read_supplier_call", "k0001_in_stock", L("not_stated"), got("found", "no"))
    assert not r.correct
    r = _grade("read_patient_reply", "medicaid_or_msp", L("not_stated"), got("found", "no"))
    assert not r.correct


def test_safety_metrics_only_for_unsolicited_items():
    # The patient reader fires (patient_primary_signals via medicaid_doubt) on anything but a clear
    # "no": a found "yes" or "not_sure", or a hedged / contradictory read.
    r = _grade("read_patient_reply", "medicaid_or_msp", L("found", "yes"), got("found", "yes"), unsolicited=True)
    assert r.safety_positive and r.safety_hit
    # a found "not_sure" is a positive (and this empty read a false empty) only on an unsolicited item
    r = _grade("read_patient_reply", "medicaid_or_msp", L("found", "not_sure"), got("not_stated"), unsolicited=True)
    assert r.safety_positive and r.critical and r.false_empty and not r.safety_hit
    r = _grade("read_patient_reply", "medicaid_or_msp", L("found", "not_sure"), got("not_stated"))
    assert not r.safety_positive and not r.critical and not r.false_empty
    scripted = _by_id("pat_02_medicaid_not_sure")
    assert ev.UNSOLICITED not in scripted.tags
    for iid in (
        "saf_01_hospital_mention",
        "saf_02_buried_opt_out",
        "saf_03_medicaid_unprompted",
        "pat_05_address_then_opt_out",
    ):
        assert ev.UNSOLICITED in _by_id(iid).tags
    s = ev.summarize(_run(StubLlm({scripted.item_id: [make_read(m.PatientReplyRead)]}), [scripted]), 1)
    assert s["safety_signal_recall"]["n"] == 0 and s["false_empties"]["n"] == 0


def test_text_match_probes():
    assert not ev.text_match("weight_lb", "158", "15")
    assert not ev.text_match("weight_lb", "158", "1580")
    assert ev.text_match("weight_lb", "158", "158")
    assert not ev.text_match("earliest_delivery", "Monday the 30th", "Monday")
    assert ev.text_match("earliest_delivery", "Monday the 30th", "on Monday the 30th")
    assert not ev.text_match("earliest_delivery", "Wednesday next week", "Wednesday")  # one way only
    assert ev.text_match("earliest_delivery", "Wednesday next week", "maybe Wednesday next week")
    assert ev.text_match("medicare_id", "1EG4-TE5-MK00", "1EG4TE5MK00")
    assert not ev.text_match("medicare_id", "1EG4-TE5-MK00", "MBI 1EG4-TE5-MK00")
    assert not ev.text_match("hcpcs_code", "K0001", "K0001 K0003")
    assert not ev.text_match("order_date", "11/19/2026", "11/19/2026 4:12 PM")
    assert not ev.text_match("contact_name", "Dev", "Devon")  # whole words
    assert ev.text_match("contact_name", "Dev", "Dev at the front")
    assert not ev.text_match("contact_name", "Al", "Al Smith")  # label < 3 chars -> exact
    assert ev.text_match("call_back_after", "after 2", "after 2")  # mostly letters -> containment
    assert ev.text_match("rep_name", "Tanya", "T-A-N-Y-A")


def test_pat_01_name_needs_the_full_name():
    item = _by_id("pat_01_caregiver_answers")
    lab = item.labels["identity_name"]
    for value, ok in [("Eleanor Ruth Martinez", True), ("Eleanor Martinez", False), ("Eleanor", False)]:
        r = _grade("read_patient_reply", "identity_name", lab, got("found", value))
        assert r.correct is ok, value


def test_commitment_scored_only_when_labelled():
    g = got("found", "Thursday", commitment="tentative")
    assert not _grade("read_supplier_call", "earliest_delivery", L("found", "Thursday", "firm"), g).correct
    assert _grade("read_supplier_call", "earliest_delivery", L("found", "Thursday"), g).correct


def test_criteria_graded_on_value():
    assert _grade("read_visit_note", "criterion_b", L("not_stated", "missing"), got("not_stated", "missing")).correct
    r = _grade("read_visit_note", "criterion_b", L("not_stated", "missing"), got("found", "documented"))
    assert not r.correct and r.confidently_wrong
    r = _grade("read_visit_note", "criterion_a", L("found", "documented"), got("not_stated", "missing"))
    assert not r.correct and r.abstained and not r.confidently_wrong


def test_doubt_labels_accept_either_doubt_status():
    for status, ok in [("contradictory", True), ("ambiguous", True), ("found", False), ("not_stated", False)]:
        r = _grade(
            "read_supplier_call", "k0001_in_stock", L("contradictory"), got(status, "yes" if status == "found" else "")
        )
        assert r.correct is ok, status
    doubt_items = [i for i in map(_item, ITEM_FILES) if any(lab.status in ev.DOUBT for lab in i.labels.values())]
    assert {lab.status for i in doubt_items for lab in i.labels.values()} >= {"ambiguous", "contradictory"}


def test_other_condition_is_scored_on_presence_only():
    lab = L("found", "curbside")
    assert _grade(
        "check_safety_and_conditions", "other_condition", lab, got("found", "drivers will not carry it up the stairs")
    ).correct
    r = _grade("check_safety_and_conditions", "other_condition", lab, got("not_stated"))
    assert not r.correct and r.false_empty
    r = _grade("read_supplier_call", "other_condition", lab, got("found", "none"))
    assert not r.correct and r.false_empty
    r = _grade("check_safety_and_conditions", "other_condition", L("not_stated"), got("found", "weekend delivery"))
    assert not r.correct and r.confidently_wrong


# -- the evidence check runs before scoring --------------------------------------------------------


def test_a_bad_quote_is_downgraded_before_scoring():
    item = _by_id("sup_01_negated_stock")
    raw = make_read(m.SupplierCallRead, k0001_in_stock=got("found", "no", turn="t6", quote="we have none at all"))
    res = ev.grade_read(item, raw, 1)
    f = next(f for f in res.fields if f.field == "k0001_in_stock")
    assert f.raw["status"] == "found" and f.got["status"] == "not_stated"
    assert f.downgraded and not f.correct and f.abstained
    assert "k0001_in_stock" in res.downgraded_fields and res.raw_read["k0001_in_stock"]["value"] == "no"
    # a quote from our own turn (wrong speaker) is downgraded too
    raw = make_read(m.SupplierCallRead, k0001_in_stock=got("found", "no", turn="t5", quote="Do you have a K0001"))
    assert not ev.grade_read(item, raw, 1).fields[3].correct
    # the same value with a real quote passes
    raw = make_read(
        m.SupplierCallRead,
        k0001_in_stock=got("found", "no", turn="t6", quote="I'm not showing any K0001s in the warehouse"),
    )
    f = next(f for f in ev.grade_read(item, raw, 1).fields if f.field == "k0001_in_stock")
    assert f.correct and not f.downgraded


def test_documents_are_checked_as_one_doc_turn():
    item = _by_id("ord_01_wrong_item_code")
    good = make_read(m.OrderDocumentRead, hcpcs_code=got("found", "K0003", turn="doc", quote="HCPCS: K0003"))
    bad = make_read(m.OrderDocumentRead, hcpcs_code=got("found", "K0003", turn="t1", quote="HCPCS: K0003"))
    assert next(f for f in ev.grade_read(item, good, 1).fields if f.field == "hcpcs_code").correct
    assert next(f for f in ev.grade_read(item, bad, 1).fields if f.field == "hcpcs_code").downgraded


def test_downgrades_and_raw_reads_are_reported(tmp_path):
    item = _by_id("sup_01_negated_stock")
    raw = make_read(
        m.SupplierCallRead,
        k0001_in_stock=got("found", "no", turn="t6", quote="invented"),
        rep_name=got("found", "Bob", turn="t6", quote="also invented"),
    )
    s = ev.summarize(_run(StubLlm({item.item_id: [raw]}), [item]), 1)
    assert s["evidence_downgrades_all_fields"] == 2
    assert s["evidence_downgrades_labelled"]["k"] == 1


# -- runs, pass^k, flips, intents, unavailable, usage ----------------------------------------------


def _sup_item(iid: str) -> m.EvalItem:
    return m.EvalItem(
        item_id=iid,
        reader="read_supplier_call",
        turns=[
            m.Turn(turn_id="t1", speaker="us", text="q"),
            m.Turn(turn_id="t2", speaker="supplier_rep", text="quoted text"),
        ],
        labels={"k0001_in_stock": L("found", "no"), "collects_coinsurance_at_delivery": L("found", "yes")},
        goal="g",
    )


GOOD = make_read(
    m.SupplierCallRead, k0001_in_stock=got("found", "no"), collects_coinsurance_at_delivery=got("found", "yes")
)
BAD = make_read(
    m.SupplierCallRead, k0001_in_stock=got("found", "yes"), collects_coinsurance_at_delivery=got("not_stated")
)


def test_pass_k_and_flips():
    items = [_sup_item("steady"), _sup_item("flaky"), _sup_item("broken")]
    llm = StubLlm({"steady": [GOOD], "flaky": [GOOD, BAD, GOOD], "broken": [BAD]})
    results = _run(llm, items, runs=3)
    by = {r.item_id: r for r in results}
    assert by["steady"].pass_k and not by["steady"].flipped
    assert not by["flaky"].pass_k and by["flaky"].flipped
    assert not by["broken"].pass_k and not by["broken"].flipped
    s = ev.summarize(results, 3)
    assert s["pass^3"]["k"] == 1 and s["pass^3"]["n"] == 3
    assert s["flips"]["k"] == 1
    assert s["field_accuracy"] == {"k": 10, "n": 18, "rate": 10 / 18}
    assert s["confidently_wrong"]["k"] == 4
    assert s["false_empties"] == {"k": 4, "n": 9, "rate": 4 / 9}
    assert s["usage"] == {"input_tokens": 900, "output_tokens": 180, "cost_usd": 0.0072}


def test_intents_scored_per_intent_with_extras_reported():
    item = _by_id("pat_05_address_then_opt_out")
    assert ev.SCORE_INTENTS in item.tags and "opt_out" in item.intents
    tid, quote = real_evidence(item)
    base = perfect_read(item)
    extra = base.model_copy(
        update={
            "intents": base.intents
            + [m.IntentRead(evidence_turn_id=tid, evidence_quote=quote, status="found", value="cost_question")]
        }
    )
    missed = base.model_copy(update={"intents": [i for i in base.intents if i.value != "opt_out"]})
    results = _run(StubLlm({item.item_id: [extra, missed]}), [item], runs=2)
    r1, r2 = results[0].runs
    assert r1.passed and r1.intents_extra == ["cost_question"]
    assert not r2.passed and r2.intents_missing == ["opt_out"]
    s = ev.summarize(results, 2)
    assert s["intents_precision"] == {"k": 3, "n": 4, "rate": 0.75}
    assert s["intents_recall"] == {"k": 3, "n": 4, "rate": 0.75}
    assert s["intents_extras"]["k"] == 1
    assert s["intents_per_intent"]["opt_out"] == {"tp": 1, "extra": 0, "missed": 1, "precision": 1.0, "recall": 0.5}
    assert s["safety_signal_recall"] == {"k": 1, "n": 2, "rate": 0.5}
    assert s["false_empties"]["k"] == 1


def test_intent_with_bad_quote_is_dropped_by_the_evidence_check():
    item = _by_id("pat_05_address_then_opt_out")
    read = perfect_read(item)
    read = read.model_copy(
        update={
            "intents": [
                i if i.value != "opt_out" else i.model_copy(update={"evidence_quote": "stop calling me"})
                for i in read.intents
            ]
        }
    )
    res = ev.grade_read(item, read, 1)
    assert res.intents_missing == ["opt_out"] and not res.passed


def test_intents_ignored_without_score_tag():
    item = _by_id("pat_01_caregiver_answers")
    tid, quote = real_evidence(item)
    read = perfect_read(item).model_copy(
        update={
            "intents": [m.IntentRead(evidence_turn_id=tid, evidence_quote=quote, status="found", value="wants_person")]
        }
    )
    assert ev.grade_read(item, read, 1).passed


def test_unavailable_runs_excluded_from_the_confidently_wrong_denominator():
    item = _sup_item("mixed")
    down = m.LlmUnavailable(call="read_supplier_call", reason="api_error", detail="503")
    results = _run(StubLlm({"mixed": [down, RuntimeError("boom"), BAD]}), [item], runs=3)
    runs = results[0].runs
    assert runs[0].unavailable.startswith("api_error") and runs[1].unavailable.startswith("exception")
    s = ev.summarize(results, 3)
    assert s["unavailable"]["k"] == 2 and s["unavailable_field_observations"] == 4
    assert s["field_accuracy"] == {"k": 0, "n": 6, "rate": 0.0}
    assert s["confidently_wrong"] == {"k": 1, "n": 2, "rate": 0.5}
    assert s["false_empties"]["n"] == 1 and s["abstention"]["n"] == 2


def test_wrong_reader_type_is_unavailable():
    res = ev.grade_read(_sup_item("wrongtype"), make_read(m.PcpReplyRead), 1)
    assert res.unavailable.startswith("wrong_type") and not res.passed


def test_usage_with_none_values_is_not_reported():
    item = _sup_item("u")
    s = ev.summarize(
        _run(StubLlm({"u": [GOOD]}, usage={"input_tokens": None, "output_tokens": None, "cost_usd": None}), [item]), 1
    )
    assert s["usage"] == "not reported"
    s = ev.summarize(
        _run(StubLlm({"u": [GOOD]}, usage={"input_tokens": 5, "output_tokens": 2, "cost_usd": None}), [item]), 1
    )
    assert s["usage"] == {"input_tokens": 5, "output_tokens": 2, "cost_usd": "not reported"}


def test_context_built_from_goal_facts_and_rule_slice():
    item = _item(ITEM_FILES[0])
    llm = StubLlm({item.item_id: [perfect_read(item)]})
    ev.run_items(llm, [item], 2, lambda reader: f"SLICE:{reader}")
    c1, c2 = llm.contexts
    assert c1.goal == item.goal and c1.facts == item.context_facts
    assert c1.rule_slice == f"SLICE:{item.reader}"
    assert c1.tag(item.reader, "x") == c2.tag(item.reader, "x")  # runs repeat the same prompt


# -- labels and item hygiene -----------------------------------------------------------------------


def test_corrected_orders_label_the_code_and_drop_answer_hints():
    assert _by_id("ord_02_initialled_correction").labels["hcpcs_code"].value == "K0001"
    assert _by_id("ord_04_uninitialled_change").labels["hcpcs_code"].status == "contradictory"
    ids = ["ord_02_initialled_correction", "ord_04_uninitialled_change"]
    ids += ["h_ord_01_changed_code_no_initials"] if HELDOUT_PRESENT else []
    for iid in ids:
        text = _by_id(iid).text.lower()
        assert "initial" not in text and "no date" not in text, iid


@needs_heldout
def test_heldout_labels_for_a_tentative_date_assignment_and_quantity():
    h1 = _by_id("h_sup_01_backordered_tentative").labels["earliest_delivery"]
    assert (h1.value, h1.commitment) == ("9th of December", "tentative")
    acc = _by_id("h_sup_02_email_injection").labels["accepts_assignment_all_months"]
    assert (acc.status, acc.value) == ("found", "no")
    assert _by_id("h_ord_01_changed_code_no_initials").labels["quantity"].status == "contradictory"


# -- manifests, sets and CLI -----------------------------------------------------------------------


def _write_manifest(root: Path, manifest_rel: str, rels: list[str]) -> None:
    (root / manifest_rel).write_text("".join(f"{ev.sha256_file(root / r)}  {r}\n" for r in rels))


def _copy_tree(tmp_path: Path, heldout: bool = HELDOUT_PRESENT, v2: bool = False) -> Path:
    root = tmp_path / "repo"
    (root / "eval").mkdir(parents=True)
    shutil.copytree(ROOT / "eval/items", root / "eval/items")
    shutil.copy(ROOT / "eval/run_eval.py", root / "eval/run_eval.py")
    _write_manifest(root, ev.MAIN_MANIFEST_REL, ev.dir_entries(root, "eval/items") + [ev.GRADER_REL])
    if heldout:
        shutil.copytree(ROOT / "eval/heldout", root / "eval/heldout")
        (root / "eval/heldout/MANIFEST.sha256").unlink(missing_ok=True)
        _write_manifest(root, "eval/heldout/MANIFEST.sha256", ev.dir_entries(root, "eval/heldout"))
    (root / "eval/heldout_v2").mkdir()
    (root / "eval/heldout_v2/README.md").write_text("reserved\n")
    if v2:
        shutil.copy(ITEM_FILES[0], root / "eval/heldout_v2" / ITEM_FILES[0].name)
        _write_manifest(
            root,
            "eval/heldout_v2/MANIFEST.sha256",
            [r for r in ev.dir_entries(root, "eval/heldout_v2") if r.endswith(".yaml")],
        )
    return root


def _perfect_llm(root: Path, set_name: str) -> StubLlm:
    return StubLlm({i.item_id: [perfect_read(i)] for i in ev.load_items(root, set_name)})


def test_manifest_ok_then_refuses_tampered_item(tmp_path, capsys):
    root = _copy_tree(tmp_path)
    assert ev.verify_manifests(root, "items") == []
    if HELDOUT_PRESENT:
        assert ev.verify_manifests(root, "heldout") == []
    target = root / "eval/items" / ITEM_FILES[0].name
    target.write_text(target.read_text() + "# edited\n")
    assert any("sha256 differs" in p for p in ev.verify_manifests(root, "items"))
    llm = _perfect_llm(_copy_tree(tmp_path / "clean"), "items")
    assert ev.main(["--runs", "1"], llm=llm, root=root, rule_slicer=lambda r: "") == 2
    assert not llm.contexts  # refused before any call
    assert "REFUSING" in capsys.readouterr().err


def test_manifest_refuses_tampered_grader_extra_and_deleted_files(tmp_path):
    root = _copy_tree(tmp_path)
    (root / "eval/run_eval.py").write_text((root / "eval/run_eval.py").read_text() + "\n# tweak\n")
    assert any("run_eval.py" in p for p in ev.verify_manifests(root, "items"))

    root = _copy_tree(tmp_path / "b")
    (root / "eval/items/zz_extra.yaml").write_text("x: 1\n")
    assert any("not in" in p for p in ev.verify_manifests(root, "items"))

    root = _copy_tree(tmp_path / "c")
    (root / "eval/items" / ITEM_FILES[1].name).unlink()
    assert any("missing on disk" in p for p in ev.verify_manifests(root, "items"))


def test_dotfiles_and_unlisted_files_are_refused_and_never_loaded(tmp_path):
    root = _copy_tree(tmp_path)
    shutil.copy(ITEM_FILES[0], root / "eval/items/.sneaky.yaml")
    assert any(".sneaky.yaml" in p for p in ev.verify_manifests(root, "items"))
    (root / "eval/items/.DS_Store").write_bytes(b"\0")
    assert any(".DS_Store" in p for p in ev.verify_manifests(root, "items"))
    # load_items reads only what the manifest lists
    ids = [i.item_id for i in ev.load_items(root, "items")]
    assert len(ids) == len(ITEM_FILES) and len(set(ids)) == len(ids)


@needs_heldout
def test_heldout_checked_only_when_requested_and_may_be_absent(tmp_path):
    root = _copy_tree(tmp_path)
    h = root / "eval/heldout" / HELDOUT_FILES[0].name
    h.write_text(h.read_text() + "\n")
    assert ev.verify_manifests(root, "items") == []
    assert ev.verify_manifests(root, "heldout")

    root = _copy_tree(tmp_path / "b")
    shutil.rmtree(root / "eval/heldout")
    assert ev.verify_manifests(root, "items") == []
    assert any("--set heldout" in p for p in ev.verify_manifests(root, "heldout"))


def test_heldout_v2_refused_until_its_manifest_exists(tmp_path, capsys):
    root = _copy_tree(tmp_path)
    assert any("does not exist yet" in p for p in ev.verify_manifests(root, "heldout_v2"))
    assert ev.main(["--set", "heldout_v2"], llm=StubLlm({}), root=root, rule_slicer=lambda r: "") == 2
    root = _copy_tree(tmp_path / "b", v2=True)
    assert ev.verify_manifests(root, "heldout_v2") == []
    llm = _perfect_llm(root, "heldout_v2")
    assert ev.main(["--set", "heldout_v2"], llm=llm, root=root, rule_slicer=lambda r: "") == 0
    (root / "eval/heldout_v2/README.md").write_text("changed\n")  # exempt: never listed, never loaded
    assert ev.verify_manifests(root, "heldout_v2") == []
    (root / "eval/heldout_v2/notes.txt").write_text("x\n")
    assert ev.verify_manifests(root, "heldout_v2")


def test_main_runs_and_writes_report(tmp_path, capsys):
    root = _copy_tree(tmp_path)
    llm = _perfect_llm(root, "items")
    assert ev.main(["--runs", "2"], llm=llm, root=root, rule_slicer=lambda r: "") == 0
    reports = list((root / "eval/reports").glob("*-items.json"))
    assert len(reports) == 1
    data = json.loads(reports[0].read_text())
    s = data["summary"]
    assert s["items"] == len(ITEM_FILES) and s["field_accuracy"]["rate"] == 1.0
    assert s["pass^2"]["k"] == len(ITEM_FILES) and s["flips"]["k"] == 0
    assert s["confidently_wrong"]["k"] == 0 and s["safety_signal_recall"]["rate"] == 1.0
    assert s["evidence_downgrades_all_fields"] == 0
    assert data["items"][0]["runs"][0]["raw_read"]
    out = capsys.readouterr().out
    assert "field_accuracy" in out and "report:" in out


@needs_heldout
def test_main_heldout_alias_and_reader_filter(tmp_path):
    root = _copy_tree(tmp_path)
    llm = _perfect_llm(root, "heldout")
    assert ev.main(["--heldout", "--reader", "read_visit_note"], llm=llm, root=root, rule_slicer=lambda r: "") == 0
    data = json.loads(next((root / "eval/reports").glob("*-heldout-read_visit_note.json")).read_text())
    assert data["items"] and {i["reader"] for i in data["items"]} == {"read_visit_note"}
    assert all(i["item_id"].startswith("h_") for i in data["items"])
    assert ev.main(["--set", "heldout"], llm=_perfect_llm(root, "heldout"), root=root, rule_slicer=lambda r: "") == 0


def test_heldout_flag_conflicts_with_other_set(tmp_path):
    with pytest.raises(SystemExit):
        ev.main(["--heldout", "--set", "heldout_v2"], llm=StubLlm({}), root=_copy_tree(tmp_path))


def test_main_without_claude_llm_fails_clearly(tmp_path, capsys, monkeypatch):
    root = _copy_tree(tmp_path)
    monkeypatch.setitem(sys.modules, "dme.llm", None)  # import of dme.llm raises ImportError
    assert ev.main([], root=root, rule_slicer=lambda r: "") == 3
    assert "ClaudeLlm" in capsys.readouterr().err


def test_a_missing_rule_slice_refuses(tmp_path, capsys, monkeypatch):
    import dme

    root = _copy_tree(tmp_path)
    monkeypatch.setitem(sys.modules, "dme.knowledge", None)
    monkeypatch.delattr(dme, "knowledge", raising=False)
    with pytest.raises(ev.Refusal):
        ev.load_rule_slicer()
    llm = _perfect_llm(root, "items")
    assert ev.main([], llm=llm, root=root) == 4
    assert not llm.contexts and "rule_slice" in capsys.readouterr().err


def test_real_rule_slice_is_used_by_default(tmp_path):
    root = _copy_tree(tmp_path)
    llm = _perfect_llm(root, "items")
    assert ev.main(["--reader", "read_visit_note"], llm=llm, root=root) == 0
    assert llm.contexts and all(c.rule_slice.strip() for c in llm.contexts)


def test_committed_manifests_match_committed_files():
    assert ev.verify_manifests(ROOT, "items") == []
    if HELDOUT_PRESENT:
        assert ev.verify_manifests(ROOT, "heldout") == []


# -- matching, doubt reads, fingerprints, refusals and manifests -----------------------------------


def test_lists_and_turn_dumps_fail_and_clean_dates_pass():
    lab = "9th of December"
    assert not ev.text_match("earliest_delivery", lab, "the 8th of December or the 9th of December")
    for clean in ("9th of December", "Dec 9th", "December 9", "the 9th of December, tentatively"):
        assert ev.text_match("earliest_delivery", lab, clean), clean
    assert not ev.text_match("earliest_delivery", lab, "Dec 8th")
    if HELDOUT_PRESENT:
        item = _by_id("h_sup_01_backordered_tentative")
        dump = " ".join(t.text for t in item.turns if t.speaker == "supplier_rep")
        assert "9th of December" in dump and not ev.text_match("earliest_delivery", lab, dump)
    dump = "Yep, got four of them. Monday the 30th, I can hold that for her, and Tuesday the 1st too."
    assert not ev.text_match("earliest_delivery", "Monday the 30th", dump)
    # the token budget also applies to names and descriptions
    assert ev.text_match("rep_name", "Tanya", "It's Tanya, T-A-N-Y-A")
    assert not ev.text_match("rep_name", "Tanya", "That's all correct. It's Tanya, T-A-N-Y-A, call me back")


def test_relative_and_time_phrases():
    assert ev.text_match("call_back_after", "after 2", "after 2pm")
    assert not ev.text_match("call_back_after", "after 2", "before 2")
    assert not ev.text_match("call_back_after", "after 2", "after 3")
    assert ev.text_match("earliest_delivery", "Wednesday next week", "maybe Wednesday next week")
    assert not ev.text_match("earliest_delivery", "Wednesday next week", "Wednesday")


def test_an_extra_safety_intent_fails_the_run_and_is_counted():
    item = _by_id("pat_04_clean_consent")  # expects [confirm]
    tid, quote = real_evidence(item)
    base = perfect_read(item)
    extra = lambda v: base.model_copy(
        update={
            "intents": base.intents
            + [
                m.IntentRead(  # noqa: E731
                    evidence_turn_id=tid, evidence_quote=quote, status="found", value=v
                )
            ]
        }
    )
    results = _run(StubLlm({item.item_id: [extra("wants_person"), extra("cost_question"), base]}), [item], runs=3)
    assert [r.passed for r in results[0].runs] == [False, True, True]
    s = ev.summarize(results, 3)
    assert s["safety_intent_false_positive"] == {"k": 1, "n": 6, "rate": 1 / 6}
    assert s["intents_extras"]["k"] == 2


def test_a_doubt_read_on_a_found_label_is_doubted_not_confident():
    r = _grade("read_supplier_call", "k0001_in_stock", L("found", "no"), got("ambiguous", "yes"))
    assert not r.correct and r.doubted and not r.confidently_wrong
    r = _grade("read_supplier_call", "k0001_in_stock", L("found", "no"), got("contradictory", "no"))
    assert not r.correct and r.doubted and not r.confidently_wrong
    r = _grade("read_supplier_call", "k0001_in_stock", L("found", "no"), got("found", "yes"))
    assert not r.doubted and r.confidently_wrong
    item = _sup_item("doubt")
    read = make_read(
        m.SupplierCallRead, k0001_in_stock=got("ambiguous", "yes"), collects_coinsurance_at_delivery=got("found", "yes")
    )
    s = ev.summarize(_run(StubLlm({"doubt": [read]}), [item]), 1)
    assert s["doubted"] == {"k": 1, "n": 2, "rate": 0.5} and s["confidently_wrong"]["k"] == 0
    assert s["field_accuracy"]["k"] == 1


def test_exact_fields_ignore_spaces_and_dashes_and_addresses_use_containment():
    assert ev.text_match("medicare_id", "1EG4-TE5-MK00", "1EG4 TE5 MK00")
    assert ev.text_match("practitioner_npi", "9999999999", "999 999 9999")
    assert ev.text_match("zip", "60625-1234", "60625 1234")
    assert not ev.text_match("zip", "60625", "60625-1234")
    assert not ev.exact_graded("address", "1200 W 87th St")
    assert ev.text_match("address", "1200 W 87th St", "1200 W 87th St, Apt 3")
    assert ev.exact_graded("order_date", "11/19/2026") and ev.exact_graded("quantity", "1")
    assert ev.exact_graded("other_free_text", "2B")


def test_the_report_carries_code_fingerprints(tmp_path):
    root = _copy_tree(tmp_path)
    assert (
        ev.main(["--reader", "read_pcp_reply"], llm=_perfect_llm(root, "items"), root=root, rule_slicer=lambda r: "")
        == 0
    )
    fp = json.loads(next((root / "eval/reports").glob("*.json")).read_text())["code_fingerprints"]
    assert {"dme/evidence.py", "dme/models.py", "dme/knowledge.py"} <= set(fp)
    assert any(k.startswith("dme/knowledge/") and k.endswith(".yaml") for k in fp)
    assert fp["dme/models.py"] == ev.sha256_file(ROOT / "dme/models.py")


def test_a_missing_knowledge_yaml_refuses_before_any_call(tmp_path, capsys, monkeypatch):
    from dme import knowledge

    root = _copy_tree(tmp_path)
    llm = _perfect_llm(root, "items")
    monkeypatch.setattr(knowledge, "KNOWLEDGE_FILE", tmp_path / "missing.yaml")
    knowledge.load.cache_clear()
    try:
        assert ev.main([], llm=llm, root=root) == 4
    finally:
        monkeypatch.undo()
        knowledge.load.cache_clear()
    assert not llm.contexts and "rule_slice(" in capsys.readouterr().err

    # an exception from an injected slicer is also a refusal
    def boom(reader):
        raise KeyError(reader)

    assert ev.main([], llm=llm, root=root, rule_slicer=boom) == 4 and not llm.contexts


def test_the_manifest_refuses_non_yaml_outside_lines_and_symlinks(tmp_path):
    root = _copy_tree(tmp_path)
    (root / "eval/items/notes.txt").write_text("x\n")
    _write_manifest(root, ev.MAIN_MANIFEST_REL, ev.dir_entries(root, "eval/items") + [ev.GRADER_REL])
    assert any("notes.txt" in p and "not allowed" in p for p in ev.verify_manifests(root, "items"))

    root = _copy_tree(tmp_path / "b")
    with open(root / ev.MAIN_MANIFEST_REL, "a") as fh:
        fh.write(f"{'0' * 64}  dme/models.py\n")
    assert any("dme/models.py" in p for p in ev.verify_manifests(root, "items"))

    root = _copy_tree(tmp_path / "c")
    first = root / "eval/items" / ITEM_FILES[0].name
    outside = tmp_path / "outside.yaml"
    shutil.move(first, outside)
    first.symlink_to(outside)
    assert any("symlink" in p for p in ev.verify_manifests(root, "items"))

    if HELDOUT_PRESENT:
        root = _copy_tree(tmp_path / "d")
        with open(root / "eval/heldout/MANIFEST.sha256", "a") as fh:
            fh.write(f"{ev.sha256_file(root / ev.GRADER_REL)}  {ev.GRADER_REL}\n")
        assert ev.verify_manifests(root, "items") == []
        assert any("not allowed" in p for p in ev.verify_manifests(root, "heldout"))


def test_sup_06_readback_turn_follows_the_production_convention():
    from dme import evidence

    item = _by_id("sup_06_clean_qualified_readback")
    ids = [t.turn_id for t in item.turns]
    assert ids[ids.index(evidence.READBACK_TURN_ID) + 1] == "t9"
    read = make_read(m.SupplierCallRead, readback_confirmed=got("found", "yes", turn="t9", quote="That's all correct."))
    assert evidence.readback_position_ok(read, item.turns)
    assert next(f for f in ev.grade_read(item, read, 1).fields if f.field == "readback_confirmed").correct


def test_contradictory_code_and_quantity_labels():
    item = _by_id("ord_04_uninitialled_change")
    for status, ok in [("contradictory", True), ("ambiguous", True), ("found", False)]:
        r = _grade(
            "read_order_document",
            "hcpcs_code",
            item.labels["hcpcs_code"],
            got(status, "K0001" if status == "found" else ""),
        )
        assert r.correct is ok, status


# -- parity with production: dates, criteria, safety triggers, unscored signals, settings ----------


def test_am_pm_kept_where_production_resolves_a_different_moment():
    assert not ev.text_match("call_back_after", "after 2", "after 2am")  # production: 2 pm vs 2 am
    assert not ev.text_match("call_back_after", "after 2pm", "after 2am")
    assert ev.text_match("call_back_after", "after 2", "after 2pm")  # the same moment in dme.clock
    assert ev.text_match("call_back_after", "after 2", "after 2:00")


def test_am_pm_kept_in_phrases_production_cannot_resolve():
    assert not ev.text_match("call_back_after", "after 2pm on Thursday", "after 2am on Thursday")
    assert ev.text_match("call_back_after", "after 2pm on Thursday", "after 2 PM on Thursday")
    assert ev.text_match("call_back_after", "after 2pm on Thursday", "after 2:00 p.m. on Thursday")


def test_business_days_are_not_calendar_days():
    assert not ev.text_match("send_by", "in 3 business days", "in 3 days")  # resolved: different moments
    assert ev.text_match("send_by", "in 3 business days", "in three business days")
    assert not ev.text_match("send_by", "3 business days", "3 days")  # unresolvable: tokens keep "business"
    assert not ev.text_match("send_by", "within 2 weeks", "within 2 days")
    assert ev.text_match("send_by", "2 business days", "two business days")


def test_a_resolvable_phrase_never_matches_an_unresolvable_one():
    assert not ev.text_match("earliest_delivery", "next week", "next Monday")  # same moment, range vs firm
    assert not ev.text_match("call_back_after", "after 2", "after 2 o'clock")  # dme.clock cannot resolve it


def test_numeric_dates_keep_their_component_order():
    assert not ev.text_match("earliest_delivery", "Monday 1/12", "Monday 11/2")
    assert not ev.text_match("earliest_delivery", "on 1/12", "on 11/2")  # an exact-graded label
    assert not ev.text_match("order_date", "1/12/2026", "11/2/2026")
    assert ev.text_match("order_date", "11/19/2026", "11-19-2026")
    assert ev.text_match("earliest_delivery", "Monday 11/30", "Monday 11/30")


def test_an_ambiguous_documented_criterion_is_recorded_missing():
    r = _grade("read_visit_note", "criterion_b", L("found", "documented"), got("ambiguous", "documented"))
    assert not r.correct and r.doubted and not r.confidently_wrong and not r.abstained
    r = _grade("read_visit_note", "criterion_b", L("not_stated", "missing"), got("ambiguous", "documented"))
    assert r.correct  # production records it as missing, as the label says
    r = _grade("read_visit_note", "criterion_b", L("not_stated", "missing"), got("contradictory", "documented"))
    assert not r.correct
    r = _grade("read_visit_note", "criterion_b", L("found", "documented"), got("found", "documented"))
    assert r.correct
    r = _grade("read_visit_note", "criterion_b", L("found", "documented"), got("found", "missing"))
    assert not r.correct and r.abstained and not r.confidently_wrong


@pytest.mark.parametrize(
    "status, value",
    [
        ("found", "documented"),
        ("found", "missing"),
        ("found", "contradictory"),
        ("ambiguous", "documented"),
        ("ambiguous", "missing"),
        ("ambiguous", "contradictory"),
        ("contradictory", "documented"),
        ("contradictory", "missing"),
        ("contradictory", "contradictory"),
        ("not_stated", "missing"),
    ],
)
def test_the_criterion_mapping_matches_what_production_records(tmp_path, status, value):
    """The grader's mirror of pcp.on_note matches production for every status and value."""
    from test_pcp import B_TEXT, _run_note, note, note_read

    ev_id, quote = ("", "") if status == "not_stated" else ("doc", B_TEXT)
    field = {"evidence_turn_id": ev_id, "evidence_quote": quote, "status": status, "value": value}
    rt = _run_note(tmp_path, note(), {**note_read(), "criterion_b": field})
    assert rt.case.get("note.criterion_b") == ev.criterion_recorded(status, value)


S = "check_safety_and_conditions"


def test_a_hedged_safety_read_with_no_value_is_a_hit_as_in_production():
    r = _grade(S, "hospitalised", L("found", "yes"), got("ambiguous", ""), unsolicited=True)
    assert r.safety_positive and r.safety_hit
    r = _grade(S, "deceased", L("found", "yes"), got("contradictory", "no"), unsolicited=True)
    assert r.safety_positive and r.safety_hit
    r = _grade(S, "deceased", L("found", "yes"), got("found", "no"), unsolicited=True)
    assert r.safety_positive and not r.safety_hit


def test_a_hedged_label_with_no_value_is_a_positive():
    r = _grade(S, "opts_out", L("ambiguous"), got("ambiguous", ""), unsolicited=True)
    assert r.safety_positive and r.safety_hit
    r = _grade(S, "opts_out", L("ambiguous"), got("not_stated"), unsolicited=True)
    assert r.safety_positive and not r.safety_hit
    item = _by_id("saf_01_hospital_mention")  # labels opts_out ambiguous with no value
    s = ev.summarize(_run(StubLlm({item.item_id: [perfect_read(item)]}), [item]), 1)
    assert s["safety_signal_recall"] == {"k": 2, "n": 2, "rate": 1.0}


def test_the_patient_reader_fires_on_anything_but_a_clear_no():
    # production (patient_primary_signals via medicaid_doubt) fires on a found "yes" or "not_sure", or on
    # any ambiguous / contradictory read; a positive is a label that fires, a hit one whose read fires too
    P = "read_patient_reply"
    r = _grade(P, "medicaid_or_msp", L("found", "yes"), got("ambiguous", "yes"), unsolicited=True)
    assert r.safety_positive and r.safety_hit  # a hedged read still fires
    r = _grade(P, "medicaid_or_msp", L("found", "yes"), got("contradictory", "no"), unsolicited=True)
    assert r.safety_positive and r.safety_hit  # contradictory fires even with value "no"
    r = _grade(P, "medicaid_or_msp", L("found", "not_sure"), got("found", "not_sure"), unsolicited=True)
    assert r.safety_positive and r.safety_hit  # "not sure" is not a clear no
    r = _grade(P, "medicaid_or_msp", L("contradictory"), got("found", "no"), unsolicited=True)
    assert r.safety_positive and not r.safety_hit  # a contradictory label is a positive; a clear no misses it
    r = _grade(P, "medicaid_or_msp", L("found", "no"), got("found", "yes"), unsolicited=True)
    assert not r.safety_positive and not r.safety_hit  # a clear "no" is never a positive, whatever the read


def test_the_trigger_is_the_production_function_itself(monkeypatch):
    from dme.rules import signals

    assert ev.production_signals is signals
    monkeypatch.setattr(signals, "fires", lambda f: True)
    r = _grade(S, "hospice", L("found", "yes"), got("not_stated"), unsolicited=True)
    assert r.safety_hit  # follows production, whatever it says
    monkeypatch.setattr(signals, "patient_primary_signals", lambda read: {"medicaid_or_msp"})
    r = _grade("read_patient_reply", "medicaid_or_msp", L("found", "yes"), got("not_stated"), unsolicited=True)
    assert r.safety_hit


def test_a_signal_on_an_unlabelled_field_is_listed_not_scored(capsys):
    item = _by_id("saf_02_buried_opt_out")
    assert "deceased" not in item.labels
    tid, quote = real_evidence(item)
    read = perfect_read(item).model_copy(
        update={"deceased": m.YesNoRead(evidence_turn_id=tid, evidence_quote=quote, status="found", value="yes")}
    )
    res = ev.grade_read(item, read, 1)
    assert res.passed  # never an accuracy error
    unlabelled = [s for s in m.SAFETY_SIGNALS if s not in item.labels]
    assert res.unlabelled_signal_sources == len(unlabelled)
    assert res.unlabelled_signals == [
        {"field": "deceased", "status": "found", "value": "yes", "evidence_turn_id": tid, "evidence_quote": quote}
    ]
    results = _run(StubLlm({item.item_id: [read]}), [item])
    s = ev.summarize(results, 1)
    assert s["unlabelled_safety_signals"] == {"k": 1, "n": len(unlabelled), "rate": 1 / len(unlabelled)}
    assert s["unlabelled_safety_signal_fires"] == [
        {
            "item_id": item.item_id,
            "run": 1,
            "field": "deceased",
            "status": "found",
            "value": "yes",
            "evidence_turn_id": tid,
            "evidence_quote": quote,
        }
    ]
    assert s["field_accuracy"]["rate"] == 1.0 and s["confidently_wrong"]["k"] == 0
    ev.print_report(s, results)
    out = capsys.readouterr().out
    assert "unlabelled_safety_signals" in out and f"unlabelled signal: {item.item_id} run 1 deceased" in out


def test_hedged_unlabelled_signals_and_unscored_patient_intents_are_listed():
    item = _by_id("saf_03_medicaid_unprompted")
    tid, quote = real_evidence(item)
    read = perfect_read(item).model_copy(
        update={"hospice": m.YesNoRead(evidence_turn_id=tid, evidence_quote=quote, status="ambiguous", value="")}
    )
    assert [f["field"] for f in ev.grade_read(item, read, 1).unlabelled_signals] == ["hospice"]
    assert ev.grade_read(item, perfect_read(item), 1).unlabelled_signals == []

    item = _by_id("pat_01_caregiver_answers")  # intents not scored, medicaid not labelled
    assert ev.SCORE_INTENTS not in item.tags and "medicaid_or_msp" not in item.labels
    tid, quote = real_evidence(item)
    opt_out = m.IntentRead(evidence_turn_id=tid, evidence_quote=quote, status="found", value="opt_out")
    read = perfect_read(item).model_copy(update={"intents": [opt_out, opt_out]})
    res = ev.grade_read(item, read, 1)
    assert res.passed and res.unlabelled_signal_sources == 3
    assert [f["field"] for f in res.unlabelled_signals] == ["intents:opt_out"]  # once per signal


def test_unavailable_runs_list_nothing():
    item = _by_id("saf_02_buried_opt_out")
    down = m.LlmUnavailable(call=S, reason="api_error", detail="503")
    s = ev.summarize(_run(StubLlm({item.item_id: [down]}), [item]), 1)
    assert s["unlabelled_safety_signals"] == {"k": 0, "n": 0, "rate": None}


def _settings_llm(root, **attrs):
    llm = _perfect_llm(root, "items")
    for k, v in attrs.items():
        setattr(llm, k, v)
    return llm


def test_the_report_records_llm_settings_and_prompt_fingerprints(tmp_path, capsys):
    root = _copy_tree(tmp_path)
    llm = _settings_llm(root, mode="live", reader_effort="low", model="claude-test")
    assert ev.main(["--reader", "read_pcp_reply"], llm=llm, root=root, rule_slicer=lambda r: "") == 0
    data = json.loads(next((root / "eval/reports").glob("*.json")).read_text())
    assert data["llm"] == {
        "llm_class": f"{StubLlm.__module__}.StubLlm",
        "model": "claude-test",
        "reader_effort": "low",
        "llm_mode": "live",
    }
    fp = data["code_fingerprints"]
    prompts = sorted(p for p in (ROOT / "dme/llm/prompts").iterdir() if p.is_file())
    assert prompts and all(fp[f"dme/llm/prompts/{p.name}"] == ev.sha256_file(p) for p in prompts)
    assert {"dme/rules/signals.py", "dme/rules/gate.py", "dme/rules/pcp.py", "dme/clock.py"} <= set(fp)
    assert "model=claude-test effort=low mode=live" in capsys.readouterr().out
    # an Llm that does not say is recorded as such
    assert ev.llm_settings(StubLlm({}))["llm_mode"] == "not reported"


def test_claude_llm_settings_come_from_the_llm():
    from dme.llm import ClaudeLlm
    from dme.llm.client import MODEL

    s = ev.llm_settings(ClaudeLlm(mode="replay", reader_effort="medium"))
    assert s == {"llm_class": "dme.llm.ClaudeLlm", "model": MODEL, "reader_effort": "medium", "llm_mode": "replay"}


def test_replay_refuses_more_than_one_run(tmp_path, capsys):
    from dme.llm import ClaudeLlm

    root = _copy_tree(tmp_path)
    llm = _settings_llm(root, mode="replay")
    assert ev.main(["--runs", "2"], llm=llm, root=root, rule_slicer=lambda r: "") == 5
    assert not llm.contexts and "replay" in capsys.readouterr().err
    real = ClaudeLlm(mode="replay", replay_dir=tmp_path / "none")
    assert ev.main(["--runs", "3"], llm=real, root=root, rule_slicer=lambda r: "") == 5
    assert not real.records
    assert ev.main(["--runs", "1", "--reader", "read_pcp_reply"], llm=llm, root=root, rule_slicer=lambda r: "") == 0
    assert (
        ev.main(
            ["--runs", "2", "--reader", "read_pcp_reply"],
            llm=_settings_llm(root, mode="live"),
            root=root,
            rule_slicer=lambda r: "",
        )
        == 0
    )
