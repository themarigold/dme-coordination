"""The check every patient message must pass, and the fill that puts facts into it.

A draft carries every number, date, amount, name and phone number as a `{key}`
placeholder for a fact it was given. Outside the placeholders it may not contain
digits, number words, amounts, dates, web addresses, banned claims, or (except in
the code-owned cost message) any word about money. Claude writes; code fills.
"""

from __future__ import annotations

import re
from datetime import date

PLACEHOLDER = re.compile(r"\{([^{}]*)\}")

NUMBER_WORDS = (
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety "
    "hundred thousand million billion dozen twice"
).split()
# Fractions, percentages, currency words and ordinals ("the second supplier").
ORDINALS = "first second third fourth fifth sixth seventh eighth ninth tenth".split()
AMOUNT_PATTERNS = [
    r"\b(half|halves|quarter|quarters)\b",
    r"%|\bper\s?cent(age)?\b",
    r"\b(dollars?|cents?|bucks)\b",
    r"\b(" + "|".join(ORDINALS) + r")\b",
    r"\b\d+(st|nd|rd|th)\b",
]
WEEKDAYS = "monday tuesday wednesday thursday friday saturday sunday".split()
MONTHS = "january february march april june july august september october november december".split()  # not "may"
RELATIVE_DATE_PATTERNS = [
    r"\b(today|tonight|tomorrow|yesterday|weekend|fortnight)\b",
    r"\b(next|this|last|coming)\s+(week|month|year|" + "|".join(WEEKDAYS) + r")\b",
    r"\b(a few|a couple of|several|in a)\s+(days?|weeks?|months?|hours?)\b",
    r"\bend of (the )?(day|week|month)\b",
    r"\b(" + "|".join(WEEKDAYS + MONTHS) + r")\b",
    r"\b(early|mid|late|in|by|until|before|after|since|during|end of)[\s-]+may\b",  # the month, not the verb
    r"\b(within|in)\s+(a|an)\b",
    r"\b(morning|afternoon|evening|tonight|overnight)s?\b",
    r"\b(christmas|thanksgiving|new year'?s?|easter|holidays?)\b",
]
URL_PATTERNS = [
    r"https?://",
    r"\bwww\.",
    r"[\w.+-]+@[\w-]+\.[a-z]{2,}",
    r"\b[a-z0-9-]+\.(com|org|net|gov|io|us|co|health)\b",
]
BANNED_CLAIMS = [
    r"(?<!feel\s)\bfree\b",
    r"\bguarantee(d|s)?\b",
    r"\bfully covered\b",
    r"\bwon'?t need to sign\b",
    r"\bwill not need to sign\b",
    r"\bno cost\b",
    r"\bno charge\b",
    r"\bcosts? nothing\b",
    r"\bwon'?t cost\b",
    r"\bnothing to pay\b",
    r"\b(completely|totally|fully|entirely)\s+(covered|paid)\b",
    r"(\bnot|n't|\bnever)\s+(have\s+to\s+|need\s+to\s+)?pay\b",
    r"\bpay\s+(very\s+)?little\b",
    # billing assurances: owing nothing, someone paying or covering all of it
    r"\bowe\s+(us\s+|them\s+|anyone\s+|anything\s+)?nothing\b",
    r"(\bnot|n't|\bnever)\s+owe\b",
    r"\bpa(y|ys|id|ying)\s+(for\s+)?(it\s+)?(all|everything|in\s+full|(the|your)\s+(entire|whole|full|total)\b)",
    r"\bcover(s|ed|ing)?\s+(it\s+)?(all|everything|in\s+full|(the|your)\s+(entire|whole|full|total)\b)",
    r"\b(entire|whole|full|total)\s+(bill|cost|costs|amount|price|charge)\b",
    r"\b(one\s+)?hundred\s+percent\b",
    r"\bnothing\s+(out\s+of\s+pocket|to\s+worry)\b",
    # the class of "covers / pays it all": a cover or pay verb, then within the same clause (no
    # punctuation between) "completely", "in full", "everything", "all", "the whole" ...
    r"\b(cover|covers|covered|covering|pay|pays|paid|paying)\b(\s+[\w'-]+){0,4}?\s+"
    r"(completely|entirely|totally|fully|in\s+full|everything|all|the\s+(entire|whole|full|total))\b",
    # never charged or billed: "won't be charged", "you will not be billed", "they never bill you"
    r"(\bnot|n't|\bnever|\bwont)\s+(be\s+|get\s+|being\s+)?(charged|billed|invoiced)\b",
    r"(\bnot|n't|\bnever|\bwont)\s+(charge|bill|invoice)\b",
    # no copay / coinsurance / deductible / charges; exempt from or waived
    r"\bno\s+(co-?pays?|co-?payments?|co-?insurance|deductibles?|charges?|bills?|fees?|costs?"
    r"|out[\s-]of[\s-]pocket)\b",
    r"\bwithout\s+(any\s+)?(co-?pays?|co-?payments?|co-?insurance|deductibles?|costs?|charges?|fees?|paying)\b",
    r"\bexempt(ed|ion)?\b",
    r"\bwaive[ds]?\b",
]

APOSTROPHES = str.maketrans({"’": "'", "‘": "'"})

# Claude never writes about money: every cost sentence comes from a code-owned template.
# A draft with any money word is rejected and the template goes instead, so
# over-blocking here costs nothing.
MONEY_TALK = re.compile(
    r"\$|\b(pay|pays|paid|paying|payments?|costs?|costing|prices?|priced|bills?|billed|billing|charges?|charged"
    r"|owe[sd]?|owing|fees?|deductibles?|co-?insurance|co-?pays?|co-?payments?|insurance|insurer|medigap"
    r"|supplement(al)?|assignment|dollars?|cents?|money|cover|covers|covered|covering|coverage|expenses?|afford"
    r"|reimburse\w*|invoice\w*|refund\w*|waive[ds]?|pocket|spend\w*|spent|tab|dime|penn(y|ies)|for nothing)\b"
    r"|(?<!feel\s)\bfree\b|\b(your|her|my|the)\s+share\b|\bshare\s+of\b"
    r"|\b(on|by)\s+medicare\b|\bmedicare\s+(pays|covers|handles|takes|picks|foots)\b"
)


def _skeleton(text: str) -> str:
    return PLACEHOLDER.sub(" ", text).translate(APOSTROPHES).lower()


def check_draft(text: str, facts: dict[str, str], purpose: str = "") -> list[str]:
    """Reasons the draft is rejected, or [] if it passes."""
    reasons: list[str] = []
    if not text.strip():
        return ["empty text"]
    for name in PLACEHOLDER.findall(text):
        if name not in facts:
            reasons.append(f"placeholder {{{name}}} is not one of the facts given")
        elif not facts[name].strip():
            reasons.append(f"placeholder {{{name}}} has no value")
    skel = _skeleton(text)
    if "{" in skel or "}" in skel:
        reasons.append("unbalanced placeholder braces")
    if re.search(r"\d", skel):
        reasons.append("a digit written outside a placeholder")
    words = [w for w in NUMBER_WORDS if re.search(rf"\b{w}\b", skel)]
    if words:
        reasons.append(f"number word(s) {words} written outside a placeholder")
    for pat in AMOUNT_PATTERNS:
        m = re.search(pat, skel)
        if m:
            reasons.append(f"amount or rank {m.group(0)!r} written outside a placeholder")
    for pat in RELATIVE_DATE_PATTERNS:
        m = re.search(pat, skel)
        if m:
            reasons.append(f"date or relative date {m.group(0)!r} written outside a placeholder")
    for pat in URL_PATTERNS:
        m = re.search(pat, skel)
        if m:
            reasons.append(f"web address or e-mail {m.group(0)!r}")
    for pat in BANNED_CLAIMS:
        m = re.search(pat, skel)
        if m:
            reasons.append(f"claim not allowed: {m.group(0)!r}")
    money = MONEY_TALK.search(skel)
    if money and purpose != "cost_explanation":
        reasons.append(f"claim not allowed (money is written by code only): {money.group(0)!r}")
    return reasons


def format_value(value: str) -> str:
    """Canonical fact string -> patient-facing text (ISO dates spelled out)."""
    v = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        try:
            d = date.fromisoformat(v)
            return f"{d.strftime('%A, %B')} {d.day}"
        except ValueError:
            pass
    return value


def fill(text: str, facts: dict[str, str]) -> str:
    """Replace every placeholder with its value. Call only on a draft that passed check_draft."""
    return PLACEHOLDER.sub(lambda m: format_value(facts[m.group(1)]), text)
