"""Patient-text writer: the shared guard (dme.drafts), one regeneration, unfilled drafts, FAQ."""

from __future__ import annotations

import json

import pytest
from llm_fakes import FakeClient, message

from dme import drafts
from dme.llm import writer as w
from dme.llm.client import Caller
from dme.models import LlmUnavailable
from dme.protocols import CallContext

FACTS = {
    "cost.low": "71.97",
    "cost.high": "325.78",
    "patient.name": "Eleanor Martinez",
    "supplier.scheduled": "2026-12-03",
}
GOOD = "Hello {patient.name}. Your wheelchair is due on {supplier.scheduled}."


def draft(text: str):
    return message(json.dumps({"text": text}))


def caller(responses):
    fake = FakeClient(responses)
    return Caller(mode="off", client=fake), fake


def ctx(goal: str = "Tell her the cost range."):
    return CallContext(case_id="case-1", goal=goal, facts={})


def test_good_draft_comes_back_with_placeholders_intact():
    cl, fake = caller([draft(GOOD)])
    r = w.write_patient_message(cl, "status_update", FACTS, ctx())
    assert r.source == "claude" and not r.needs_inbox
    assert r.text == GOOD and r.as_draft().text == GOOD  # the rules fill it, not the writer
    assert drafts.fill(r.text, FACTS) == "Hello Eleanor Martinez. Your wheelchair is due on Thursday, December 3."
    assert len(fake.messages.calls) == 1
    assert "patient.name = Eleanor Martinez" in fake.messages.calls[0]["messages"][0]["content"]


def test_iso_dates_are_spelled_out():
    assert drafts.fill("Delivery is {supplier.scheduled}.", FACTS) == "Delivery is Thursday, December 3."


BAD = [
    ("digit", "Your share is about $72."),
    ("number word", "You will rent it for thirteen months."),
    ("relative date", "The supplier will call you tomorrow."),
    ("weekday", "Expect delivery on Thursday."),
    ("url", "See www.medicare.gov for details."),
    ("email", "Write to help@example.org."),
    ("banned free", "Your wheelchair is free."),
    ("banned guaranteed", "Delivery is guaranteed."),
    ("banned fully covered", "It is fully covered by Medicare."),
    ("banned sign", "You won't need to sign anything."),
    ("banned no cost", "There is no cost to you."),
    ("unknown placeholder", "Your share is ${cost.middle}."),
]


@pytest.mark.parametrize("label,bad", BAD, ids=[b[0] for b in BAD])
def test_each_banned_class_regenerates_once_then_succeeds(label, bad):
    assert drafts.check_draft(bad, FACTS), label
    cl, fake = caller([draft(bad), draft(GOOD)])
    r = w.write_patient_message(cl, "status_update", FACTS, ctx())
    assert r.source == "claude_regenerated" and len(r.rejections) == 1
    second_prompt = fake.messages.calls[1]["messages"][0]["content"]
    assert "previous draft was rejected" in second_prompt and r.rejections[0] in second_prompt


@pytest.mark.parametrize("label,bad", BAD, ids=[b[0] for b in BAD])
def test_two_bad_drafts_return_llm_unavailable_so_the_rules_use_their_template(label, bad):
    cl, fake = caller([draft(bad), draft(bad)])
    r = w.write_patient_message(cl, "status_update", FACTS, ctx())
    assert r.source == "rejected" and len(r.rejections) == 2 and len(fake.messages.calls) == 2
    out = r.as_draft()
    assert isinstance(out, LlmUnavailable) and out.reason == "parse_error" and r.text == ""


def test_digits_inside_placeholders_are_fine_but_empty_facts_are_not():
    assert drafts.check_draft("Due on {supplier.scheduled}.", FACTS) == []
    assert drafts.check_draft("Due on {supplier.scheduled}.", {"supplier.scheduled": ""})


def test_llm_unavailable_is_passed_through():
    cl, fake = caller([])
    cl.fault = "llm_down"
    r = w.write_patient_message(cl, "status_update", {}, ctx())
    assert r.source == "unavailable" and r.as_draft() == r.unavailable and r.unavailable.reason == "fault_injected"


def test_writer_keeps_no_templates_of_its_own():
    assert not hasattr(w, "TEMPLATES") and not hasattr(w, "template_text") and not hasattr(w, "fill")


def test_every_faq_answer_passes_the_guard():
    for _, answer in w.FAQ:
        assert drafts.check_draft(answer, {}) == [], answer


def test_claude_llm_returns_the_unfilled_draft_through_the_protocol():
    from dme.llm import ClaudeLlm

    llm = ClaudeLlm(mode="off", client=FakeClient([draft(GOOD)]))
    out = llm.write_patient_message("status_update", FACTS, ctx())
    assert out.text == GOOD and "{supplier.scheduled}" in out.text


def test_faq_answers_without_claude():
    cl, fake = caller([])
    r = w.write_patient_message(
        cl, "answer_question", {}, ctx(goal="Patient asks: do I need to be home for the delivery?")
    )
    assert r.source == "faq" and "home" in r.text and fake.messages.calls == []


def test_unknown_question_goes_to_inbox():
    cl, fake = caller([])
    r = w.write_patient_message(cl, "answer_question", {}, ctx(goal="Patient asks: can my cat ride in it?"))
    assert r.needs_inbox and r.text == "" and r.source == "needs_inbox" and fake.messages.calls == []
    assert r.as_draft().text == ""


def test_faq_matches_whole_words_only():
    assert w.faq_answer("my parent asked something") is None  # "rent" inside "parent"
    assert w.faq_answer("Do I own it after the rental?") is not None


VAGUE_OR_FALSE_PHRASES = [
    "It may cost a few dollars.",
    "You pay half.",
    "The second supplier will call.",
    "It arrives within a week.",
    "We will call this afternoon.",
    "Delivery is around Christmas.",
    "Delivery is in early May.",
    "It is completely covered by Medicare.",
    "You will not have to pay anything.",
    "You will pay very little.",
    "Medicare pays most of it, about twenty percent is yours.",
    "You pay a quarter.",
    "We call each morning.",
    "Delivery after Thanksgiving.",
    "It is totally covered.",
    "You won't have to pay.",
    "Your share is 20%.",
]


@pytest.mark.parametrize("phrase", VAGUE_OR_FALSE_PHRASES)
def test_vague_amounts_times_and_coverage_claims_are_rejected(phrase):
    assert drafts.check_draft(phrase, {}), phrase


def test_may_the_verb_is_allowed():
    assert drafts.check_draft("You may call us with any questions.", {}) == []


def test_writer_prompt_example_passes_the_guards():
    from dme.llm.context import load_prompt

    system, _ = load_prompt("write_patient_message")
    example = json.loads(system[system.index('{"text"') :].split("\n")[0])["text"]
    assert drafts.check_draft(example, {"cost.low": "71.97", "cost.high": "325.78"}) == []


@pytest.mark.parametrize(
    "question",
    [
        "will the repair cost me anything?",
        "Do I have to pay to own it?",
        "Who pays for the delivery, is there a $ fee?",
    ],
)
def test_money_questions_go_to_inbox_even_when_an_faq_entry_matches(question):
    assert w.faq_answer(question) is None
    cl, fake = caller([])
    r = w.write_patient_message(cl, "answer_question", {}, ctx(goal=f"Patient asks: {question}"))
    assert r.needs_inbox and r.text == "" and fake.messages.calls == []
