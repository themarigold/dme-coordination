"""Cost to Eleanor for a 13-month capped rental of K0001.

low  = deductibles already met by other Part B bills: 20% of every allowed month.
high = no other Part B bills: each calendar year's deductible is paid first, then 20%.
Months are billed monthly from the delivery date and split by calendar year.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from dme import knowledge

CENT = Decimal("0.01")


def _add_months(d: date, months: int) -> date:
    year, month = divmod(d.month - 1 + months, 12)
    return date(d.year + year, month + 1, min(d.day, 28))  # day clamped: only the year matters


def cost_range(delivery_date: date) -> dict[str, Any]:
    """Low and high cost, assumptions, and assumed deductibles for a delivery on `delivery_date`.

    Every year whose Part B deductible is not yet announced is listed with the amount assumed for it.
    """
    rules = knowledge.entry("rental_cost")
    first = Decimal(rules["monthly_allowed_months_1_3"])
    later = first * Decimal(rules["later_month_fraction"])
    coinsurance = Decimal(rules["coinsurance"])
    deductibles: dict[int, dict[str, Any]] = rules["part_b_deductible"]

    allowed_by_year: dict[int, Decimal] = {}
    for month in range(rules["rental_months"]):
        year = _add_months(delivery_date, month).year
        allowed_by_year[year] = allowed_by_year.get(year, Decimal(0)) + (first if month < 3 else later)

    assumptions = [
        f"allowed amount ${first}/month for months 1-3, {rules['later_month_fraction']} of that for months 4-13",
        "low: her Part B deductibles are already met by other bills; high: no other Part B bills",
        "supplier accepts assignment for all 13 months",
    ]
    low = high = Decimal(0)
    assumed_deductibles: list[dict[str, Any]] = []
    latest_known = max(deductibles)
    for year, allowed in sorted(allowed_by_year.items()):
        known = deductibles.get(year)
        deductible = Decimal((known or deductibles[latest_known])["amount"])
        if known is None or known.get("assumed"):
            assumed_deductibles.append({"year": year, "amount": str(deductible)})
            assumptions.append(
                f"{year}_deductible_assumed: {year} Part B deductible assumed ${deductible} (not yet announced)"
            )
        low += allowed * coinsurance
        paid_to_deductible = min(allowed, deductible)
        high += paid_to_deductible + (allowed - paid_to_deductible) * coinsurance

    return {
        "low": float(low.quantize(CENT, ROUND_HALF_UP)),
        "high": float(high.quantize(CENT, ROUND_HALF_UP)),
        "assumptions": assumptions,
        "assumed_deductibles": assumed_deductibles,
    }


def cost_facts(result: dict[str, Any]) -> dict[str, str]:
    """cost_range output as canonical case facts (money as "123.45")."""
    return {
        "cost.low": f"{result['low']:.2f}",
        "cost.high": f"{result['high']:.2f}",
        "cost.assumptions": "; ".join(result["assumptions"]),
    }
