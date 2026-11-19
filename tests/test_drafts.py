"""The patient-text check (dme.drafts): billing claims, money talk from Claude, code-owned wording."""

import json
import re
from pathlib import Path

import pytest
from fakes import never, outbound, qualified_supplier, ready_patient, runtime

from dme import drafts
from dme.drafts import check_draft
from dme.rules import patient

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "text",
    [
        "You owe us nothing.",
        "You won't owe anything for the chair.",
        "Medicare will pay your entire bill.",
        "Medicare pays for everything.",
        "Medicare pays it all.",
        "It is covered in full.",
        "Medicare covers the whole cost.",
        "Your plan covers everything.",
        "The chair is free.",
        "It comes free of charge.",
        "Medicare covers it one hundred percent.",
        "Medicare pays the full amount.",
        "You are free to ask the supplier anything.",  # only "feel free" is ordinary wording
    ],
)
def test_the_draft_check_refuses_false_billing_assurances(text):
    from dme.drafts import check_draft

    assert any("claim not allowed" in r for r in check_draft(text, {}))


@pytest.mark.parametrize(
    "text",
    [
        "The wheelchair is free to you.",
        "It comes free to Medicare patients.",
        "Medicare picks up the tab, so it is free to keep.",
    ],
)
def test_free_to_you_is_a_banned_claim(text):
    assert any("claim not allowed" in r for r in check_draft(text, {}, "cost_explanation"))


@pytest.mark.parametrize(
    "text",
    [
        "Medicare covers your chair completely.",
        "You won't be charged for the chair.",
        "you will not be billed",
        "You are exempt from any copay.",
        "There is no copay.",
        "No coinsurance applies to you.",
        "Medicare pays for the whole rental.",
        "They never bill you.",
        "It is covered completely by Medicare.",
        "There are no charges for you.",
        "The supplier waived the copay.",
        "You wont be billed.",
        "Medicare covers it all.",
        "You will not get charged.",
        "Without any copay at all.",
    ],
)
def test_false_assurances_are_rejected(text):
    assert any("claim not allowed" in r for r in drafts.check_draft(text, {}))


@pytest.mark.parametrize(
    "text",
    [
        "Please feel free to call us with any question.",
        "Each year your Part B deductible is paid before Medicare starts paying, and after that you pay your share "
        "of each month's Medicare-approved amount.",
        "We will tell you the expected amounts once a supplier has confirmed it accepts Medicare's rate for the "
        "whole rental.",
    ],
)
def test_the_draft_check_allows_ordinary_wording(text):
    from dme.drafts import check_draft

    assert check_draft(text, {}, "cost_explanation") == []  # code-owned cost wording


def test_feel_free_is_allowed():
    assert check_draft("Please feel free to call us with any question.", {}) == []


@pytest.mark.parametrize(
    "text",
    [
        "Feel free to call us.",
        "You pay your share of each month's Medicare-approved amount.",
        "The supplier bills you only for your share, at or after delivery.",
        "Once the rental period is complete, the chair is yours to keep.",
    ],
)
def test_ordinary_cost_wording_passes(text):
    assert drafts.check_draft(text, {}, "cost_explanation") == []  # code-owned cost wording


def test_every_code_owned_template_sentence_passes_the_check():
    from dme.drafts import PLACEHOLDER, check_draft

    for key, sentences in patient.TEMPLATES.items():
        purpose = "cost_explanation" if key.startswith("cost") else key  # only the cost message talks money
        for sentence in sentences:
            facts = {n: "x" for n in PLACEHOLDER.findall(sentence.replace("supplier.ID.", "supplier.4."))}
            assert check_draft(sentence.replace("supplier.ID.", "supplier.4."), facts, purpose) == [], sentence


def test_every_code_owned_sentence_and_faq_answer_passes_the_check():
    from dme.llm.writer import FAQ

    for key, sentences in patient.TEMPLATES.items():
        purpose = "cost_explanation" if key.startswith("cost") else key  # only the cost message talks money
        for sentence in sentences:
            names = drafts.PLACEHOLDER.findall(sentence.replace("supplier.ID.", "supplier.4."))
            facts = {n: "x" for n in names}
            assert drafts.check_draft(sentence.replace("supplier.ID.", "supplier.4."), facts, purpose) == [], (
                key,
                sentence,
            )
    for _, answer in FAQ:
        assert drafts.check_draft(answer, {}) == [], answer


MONEY_SENTENCES = [
    # phrasings that slip past a phrase-by-phrase check
    "Medigap pays your share.",
    "You owe: nothing.",
    "The supplier bills Medicare, not you.",
    "It's all on Medicare.",
    "It costs you nothing.",
    "Your share is nothing.",
    "Medicare takes care of all of it.",
    "You will never receive a bill.",
    "Medicare covers the chair, completely.",
    "Nothing comes out of your pocket.",
    "Medicare picks up the full tab.",
    "You get the chair for nothing.",
    "You will not need to spend a dime.",
    "Everything will be taken care of by Medicare.",
    "Your coinsurance is waived.",
    "No payment is needed from you.",
    # any money talk at all, true or false
    "Your deductible may already be met.",
    "The price is set by Medicare.",
    "Your insurance handles that part.",
    "Medicare pays its share once your deductible is met, and you pay the rest.",
]


WRITTEN_PURPOSES = ["first_contact", "status_update", "supplier_will_call", "delivery_check", "answer_question"]


@pytest.mark.parametrize("text", MONEY_SENTENCES)
def test_a_money_sentence_is_rejected_in_every_purpose_claude_writes(text):
    for purpose in WRITTEN_PURPOSES:
        reasons = drafts.check_draft(f"Hi Eleanor. {text} Thank you.", {}, purpose)
        assert any("money" in r for r in reasons), (purpose, text)


@pytest.mark.parametrize("text", MONEY_SENTENCES)
def test_a_status_update_about_money_falls_back_to_the_template(tmp_path, text):
    rt, _ = runtime(
        [never("patient", "status_update")],
        tmp_path,
        scripted_drafts={"status_update#1": f"Your chair is on its way. {text}"},
    )
    ready_patient(rt)
    patient.message(rt, "status_update", {})
    sent = [line.data["text"] for line in rt.engine.log_lines if line.kind == "outbound"]
    assert sent == [" ".join(patient.TEMPLATES["status_update"])]
    assert [line for line in rt.engine.log_lines if line.kind == "draft_rejected"]


def _written_texts():
    """(purpose, text) for code-owned sentences of Claude's purposes, FAQ answers and the prompt example."""
    from dme.llm.writer import FAQ

    texts = [
        (key, s.replace("supplier.ID.", "supplier.4."))
        for key, sentences in patient.TEMPLATES.items()
        if not key.startswith("cost")
        for s in sentences
    ]
    texts += [("answer_question", answer) for _, answer in FAQ]
    prompt = (ROOT / "dme/llm/prompts/write_patient_message.md").read_text()
    example = re.search(r"Worked example \(purpose (\w+)\):\n(\{[^\n]*\})", prompt)
    assert example and example.group(1) == "status_update", "the writer prompt's worked example moved"
    return texts + [("status_update", json.loads(example.group(2))["text"])]


def test_the_worked_example_and_every_written_template_sentence_pass():
    for purpose, text in _written_texts():
        facts = {name: "x" for name in drafts.PLACEHOLDER.findall(text)}
        assert drafts.check_draft(text, facts, purpose) == [], text
    for key in patient.TEMPLATES:  # whole template messages as code builds them, too
        built = patient.template_text(
            key,
            {
                "supplier.4.name": "x",
                "supplier.4.phone": "x",
                "cost.low": "1",
                "cost.high": "2",
                "assumed_deductibles": "x",
            },
        )
        names = drafts.PLACEHOLDER.findall(built)
        purpose = "cost_explanation" if key.startswith("cost") else key
        assert drafts.check_draft(built, {n: "x" for n in names}, purpose) == [], key


def _writer_calls(llm):
    return [c for c in llm.calls if c[0] == "write_patient_message"]


@pytest.mark.parametrize("figures", [True, False])
def test_the_cost_message_never_calls_the_writer(tmp_path, figures):
    rt, llm = runtime(
        [never("patient", "cost_explanation")],
        tmp_path,
        scripted_drafts={"cost_explanation#1": "Medicare rents the chair to you month by month."},
    )
    ready_patient(rt)
    if figures:
        qualified_supplier(rt, 4, "primary")
        rt.case.set("supplier.4.earliest_delivery", "2026-12-15")
    patient.send_cost_message(rt)
    assert _writer_calls(llm) == []
    (text,) = [line.data["text"] for line in rt.engine.log_lines if line.kind == "outbound"]
    if figures:  # the code-owned template with its estimate sentences, exactly as code fills them
        facts = {"cost.low": "71.97", "cost.high": "325.78", "assumed_deductibles": "2027 stays at $283"}
        assert text == drafts.fill(patient.template_text("cost_explanation", facts), facts)
        assert (
            "In total we estimate your share at about $71.97 if your deductibles are already met by other "
            "bills, and up to about $325.78 if they are not."
            in text
            and "for 2027 stays at $283" in text
        )
    else:
        assert text == patient.template_text("cost_structure", {})


def test_no_route_to_a_cost_message_calls_the_writer(tmp_path):
    rt, llm = runtime(
        [never("patient", "cost_explanation")],
        tmp_path,
        scripted_drafts={"cost_explanation#1": "Medicare rents the chair to you month by month."},
    )
    ready_patient(rt)
    assert patient.compose(rt, "cost_explanation", {}, "cost_structure", writer=True).startswith("Medicare Part B")
    patient.message(rt, "cost_explanation", {})
    assert _writer_calls(llm) == [] and ("patient", "cost_explanation") in outbound(rt)
