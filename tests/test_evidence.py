from dme.evidence import document_turns, readback_position_ok, validate_read
from dme.models import (
    CriterionRead,
    DatePhraseRead,
    IntentRead,
    PatientReplyRead,
    SupplierCallRead,
    TextRead,
    Turn,
    VisitNoteRead,
)

SUPPLIER = {"supplier_rep"}
TURNS = [
    Turn(turn_id="t1", speaker="us", text="Do you have the K0001 in stock?"),
    Turn(turn_id="t2", speaker="supplier_rep", text="We  DON’T have it right now,\nmaybe Thursday."),
    Turn(turn_id="readback", speaker="us", text="Let me read that back: no stock, Thursday. Correct?"),
    Turn(turn_id="t4", speaker="supplier_rep", text="Yes, that's right. I'm Dana."),
]


def yn(turn="", quote="", status="not_stated", value=""):
    return {"evidence_turn_id": turn, "evidence_quote": quote, "status": status, "value": value}


def supplier_read(**fields) -> SupplierCallRead:
    base = {name: yn() for name in SupplierCallRead.model_fields}
    for name in ("call_back_after", "earliest_delivery"):
        base[name] = {**yn(), "commitment": ""}
    base.update(fields)
    return SupplierCallRead.model_validate(base)


def test_valid_quote_passes_after_normalising_case_whitespace_and_curly_quotes():
    read = supplier_read(k0001_in_stock=yn("t2", "we don't have it right now", "found", "no"))
    out, flags = validate_read(read, TURNS, SUPPLIER)
    assert out.k0001_in_stock.value == "no" and out.k0001_in_stock.status == "found"
    assert "negation:don't" in flags["k0001_in_stock"]  # flagged, not changed


def test_quote_not_in_turn_is_downgraded():
    read = supplier_read(k0001_in_stock=yn("t2", "we have plenty", "found", "yes"))
    out, flags = validate_read(read, TURNS, SUPPLIER)
    assert (out.k0001_in_stock.status, out.k0001_in_stock.value) == ("not_stated", "")
    assert flags["k0001_in_stock"] == ["downgraded:quote_not_found"]


def test_wrong_speaker_is_downgraded():
    read = supplier_read(k0001_in_stock=yn("t1", "do you have the k0001 in stock", "found", "yes"))
    out, flags = validate_read(read, TURNS, SUPPLIER)
    assert out.k0001_in_stock.status == "not_stated"
    assert flags["k0001_in_stock"] == ["downgraded:wrong_speaker"]


def test_unknown_turn_and_empty_quote_are_downgraded():
    read = supplier_read(rep_name=yn("t9", "Dana", "found", "Dana"), delivers_to_zip=yn("t4", "", "found", "yes"))
    out, flags = validate_read(read, TURNS, SUPPLIER)
    assert out.rep_name.value == "" and out.delivers_to_zip.value == ""
    assert flags["rep_name"] == ["downgraded:unknown_turn"]
    assert flags["delivers_to_zip"] == ["downgraded:empty_quote"]


def test_not_stated_is_forced_empty():
    read = supplier_read(rep_name=yn("t4", "Dana", "not_stated", "Dana"))
    out, _ = validate_read(read, TURNS, SUPPLIER)
    assert (out.rep_name.value, out.rep_name.evidence_quote) == ("", "")


def test_date_phrase_downgrade_loses_commitment_and_hedge_is_flagged():
    good = {**yn("t2", "maybe Thursday", "found", "Thursday"), "commitment": "tentative"}
    bad = {**yn("t2", "Friday for sure", "found", "Friday"), "commitment": "firm"}
    out, flags = validate_read(supplier_read(earliest_delivery=good, call_back_after=bad), TURNS, SUPPLIER)
    assert out.earliest_delivery.commitment == "tentative"
    assert "hedge:maybe" in flags["earliest_delivery"]
    assert (out.call_back_after.value, out.call_back_after.commitment) == ("", "")
    assert isinstance(out.call_back_after, DatePhraseRead)


def test_document_is_one_turn_and_criteria_downgrade_to_missing():
    note = "Pt cannot ambulate safely with a walker. Weight 158 lb."
    criteria = {f"criterion_{c}": yn() | {"value": "missing"} for c in "abcdefg"}
    criteria["criterion_b"] = yn("doc", "cannot ambulate safely with a walker", "found", "documented")
    criteria["criterion_a"] = yn("doc", "toileting impaired", "found", "documented")  # not in the note
    read = VisitNoteRead.model_validate(
        {**criteria, "weight_lb": yn("doc", "Weight 158 lb", "found", "158"), "beneficiary_name": yn()}
    )
    out, _ = validate_read(read, document_turns(note), {"pcp_office"})
    assert out.criterion_b.value == "documented" and out.weight_lb.value == "158"
    assert (out.criterion_a.status, out.criterion_a.value) == ("not_stated", "missing")
    assert isinstance(out.criterion_a, CriterionRead)


def test_failed_intents_are_dropped():
    turns = [Turn(turn_id="p1", speaker="patient", text="How much will this cost me? Please stop calling.")]
    base = {name: yn() for name in PatientReplyRead.model_fields if name != "intents"}
    base["availability"] = {**yn(), "commitment": ""}
    base["intents"] = [
        yn("p1", "how much will this cost me", "found", "cost_question"),
        yn("p1", "I want a person", "found", "wants_person"),  # not in the turn
        yn("", "", "not_stated", "confirm"),
    ]
    out, flags = validate_read(PatientReplyRead.model_validate(base), turns, {"patient", "helper"})
    assert [i.value for i in out.intents] == ["cost_question"]
    assert flags["intents[1]"] == ["downgraded:quote_not_found"]
    assert all(isinstance(i, IntentRead) for i in out.intents)


def test_original_read_is_not_mutated():
    read = supplier_read(rep_name=yn("t4", "Bob", "found", "Bob"))
    validate_read(read, TURNS, SUPPLIER)
    assert read.rep_name == TextRead(**yn("t4", "Bob", "found", "Bob"))


def test_quotes_match_on_word_boundaries():
    turns = [Turn(turn_id="p1", speaker="patient", text="I don't know yet.")]
    out, flags = validate_read(supplier_read(needs_info=yn("p1", "no", "found", "no")), turns, {"patient"})
    assert out.needs_info.status == "not_stated"
    assert flags["needs_info"] == ["downgraded:quote_not_found"]
    ends_a_word = [Turn(turn_id="p1", speaker="patient", text="Ask my daughter Juno.")]
    out, _ = validate_read(supplier_read(needs_info=yn("p1", "no", "found", "no")), ends_a_word, {"patient"})
    assert out.needs_info.status == "not_stated"
    out, _ = validate_read(supplier_read(needs_info=yn("p1", "know yet.", "found", "no")), turns, {"patient"})
    assert out.needs_info.status == "found"


def test_negation_and_hedge_after_the_quote_are_flagged():
    turns = [Turn(turn_id="t1", speaker="supplier_rep", text="We have it in stock, I think, but not the right size.")]
    _, flags = validate_read(
        supplier_read(k0001_in_stock=yn("t1", "we have it in stock", "found", "yes")), turns, SUPPLIER
    )
    assert "hedge:think" in flags["k0001_in_stock"] and "negation:not" in flags["k0001_in_stock"]


def test_readback_position():
    right = supplier_read(readback_confirmed=yn("t4", "that's right", "found", "yes"))
    assert readback_position_ok(right, TURNS)
    # The same words in a turn that does not follow our read-back do not count.
    early = [TURNS[0], Turn(turn_id="t2", speaker="supplier_rep", text="Yes, that's right.")]
    assert not readback_position_ok(supplier_read(readback_confirmed=yn("t2", "that's right", "found", "yes")), early)
    # An opening "calling to confirm..." turn is not our read-back turn.
    opening = [
        Turn(turn_id="t1", speaker="us", text="Hi, I'm calling to confirm a few things for a patient."),
        Turn(turn_id="t2", speaker="supplier_rep", text="Sure, that's right, go ahead."),
    ]
    assert not readback_position_ok(supplier_read(readback_confirmed=yn("t2", "that's right", "found", "yes")), opening)
    # A confirmation two turns after the read-back does not count either.
    late = TURNS + [Turn(turn_id="t5", speaker="supplier_rep", text="Yep, that's right.")]
    assert not readback_position_ok(supplier_read(readback_confirmed=yn("t5", "that's right", "found", "yes")), late)
    # The read-back must be ours: a rep's own "to confirm" does not count.
    reps_own = [
        Turn(turn_id="t1", speaker="supplier_rep", text="To confirm, we have it."),
        Turn(turn_id="t2", speaker="supplier_rep", text="Yes, that's right."),
    ]
    rep_confirms = supplier_read(readback_confirmed=yn("t2", "that's right", "found", "yes"))
    assert not readback_position_ok(rep_confirms, reps_own)
    # A "no" or a not-found confirmation never counts.
    assert not readback_position_ok(supplier_read(readback_confirmed=yn("t4", "that's right", "found", "no")), TURNS)


def test_a_multi_word_hedge_matches_whole_words_only():
    from dme.evidence import _nearby_flags
    from dme.models import TextRead, Turn

    turns = {"t1": Turn(turn_id="t1", speaker="patient", text="I am not surely going to pay that")}
    field = TextRead(evidence_turn_id="t1", evidence_quote="going to pay", status="found", value="x")
    assert "hedge:not sure" not in _nearby_flags(field, turns)
    turns["t1"] = Turn(turn_id="t1", speaker="patient", text="I am not sure, going to pay that")
    assert "hedge:not sure" in _nearby_flags(field, turns)
