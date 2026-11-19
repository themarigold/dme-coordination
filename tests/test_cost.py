from datetime import date

import pytest

from dme.rules.cost import cost_facts, cost_range


def test_delivery_mid_december_2026():
    r = cost_range(date(2026, 12, 15))
    # Low: 20% of 3 x 34.27 + 10 x 25.70 = 71.97.
    assert r["low"] == pytest.approx(71.97, abs=0.05)
    # High: 1 month in 2026 (all to the deductible) + 12 in 2027 at the assumed $283.
    assert r["high"] == pytest.approx(325.78, abs=0.05)
    assert any(a.startswith("2027_deductible_assumed") for a in r["assumptions"])


def test_every_unannounced_year_is_flagged():
    # A 2027 delivery runs into 2028: both deductibles are assumptions.
    flags = [a for a in cost_range(date(2027, 3, 1))["assumptions"] if "_deductible_assumed" in a]
    assert [f[:4] for f in flags] == ["2027", "2028"]


def test_low_is_independent_of_delivery_month():
    assert cost_range(date(2026, 12, 15))["low"] == cost_range(date(2027, 6, 1))["low"]


def test_cost_facts_are_canonical_money_strings():
    f = cost_facts(cost_range(date(2026, 12, 15)))
    assert (f["cost.low"], f["cost.high"]) == ("71.97", "325.78")
    assert "2027" in f["cost.assumptions"]


def test_assumed_deductibles_are_returned_as_data():
    assert cost_range(date(2026, 11, 24))["assumed_deductibles"] == [{"year": 2027, "amount": "283"}]
    assert [a["year"] for a in cost_range(date(2027, 1, 5))["assumed_deductibles"]] == [2027, 2028]
