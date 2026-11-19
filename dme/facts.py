"""The fact ids a case may store, and their canonical string forms.

Values are "yes"/"no", ISO-8601 dates, money as "123.45", or "" for unknown.
"""

import re

# Patient (intake seed + first-contact script + later replies)
PATIENT = [
    "patient.name",
    "patient.age",
    "patient.coverage",
    "patient.medigap",
    "patient.dob",
    "patient.address",
    "patient.zip",
    "patient.phone_source",
    "patient.identity_verified",
    "patient.consent_proceed",
    "patient.consent_share_with_suppliers",
    "patient.consent_calls_texts",
    "patient.in_home_use",
    "patient.home_layout",
    "patient.prior_wheelchair_5y",
    "patient.medicaid_or_msp",  # yes | no | not_sure | ""
    "patient.preferred_supplier",
    "patient.helper_name",
    "patient.helper_permission",
    "patient.medicare_id",
    "patient.medicare_id_ok_to_share",
    "patient.availability",
    "patient.delivery_received",
    "patient.fit_ok",
    "patient.shown_how_to_use",
    "patient.has_repair_number",
    "patient.contact_attempts",  # integer as string
    "patient.deceased",  # set to "no" only when a person finds a deceased report false
]

# Written order (from read_order_document on our copy)
ORDER = [
    "order.item_requested",
    "order.hcpcs_expected",
    "order.received",
    "order.version",  # version: "1", "2", ...
    "order.hcpcs_on_order",
    "order.item_description",
    "order.item_matches_k0001",
    "order.beneficiary_ok",
    "order.description_ok",
    "order.quantity_ok",
    "order.order_date_ok",
    "order.practitioner_ok",
    "order.signature_ok",
    "order.changes_ok",
    "order.elements_complete",
]

# Visit note (from read_visit_note on our copy)
NOTE = [
    "note.received",
    "note.version",
    "note.criterion_a",
    "note.criterion_b",
    "note.criterion_c",
    "note.criterion_d",
    "note.criterion_e",
    "note.criterion_f",
    "note.criterion_g",  # documented | missing | contradictory
    "note.weight_lb",
    "note.criteria_accepted",  # criteria_accepted set only by the approval path
]

# PCP office
PCP = [
    "pcp.name",
    "pcp.practice",
    "pcp.phone",
    "pcp.visit",
    "pcp.contact_name",
    "pcp.request_status",  # sent | confirmed | not_received | docs_received
    "pcp.release_signed",
    "pcp.supplier_notified",
]

# Per supplier: use supplier_fact(supplier_id, name)
SUPPLIER_FIELDS = [
    "name",
    "phone",
    "address",  # copied from the directory when the supplier is first considered
    "status",  # candidate | calling | hold_needs_info | qualified | primary | backup | skipped | released | dropped
    "call_outcome",
    "accepting_new_medicare",
    "bills_medicare_k0001",
    "k0001_in_stock",
    "delivers_to_zip",
    "accepts_assignment_all_months",
    "abn_required",
    "earliest_delivery",
    "rep_name",
    "readback_confirmed",
    "collects_coinsurance_at_delivery",
    "needs_info_item",
    "call_permission",  # Eleanor's written permission for this supplier to call her
    "packet_sent",
    "accepted",
    "reject_reason",
    "home_assessment_recorded",
    "same_or_similar_clear",
    "scheduled_date",
    "delivered_date",
]

# Handoff and cost
HANDOFF = ["handoff.supplier", "handoff.step", "handoff.packets_sent"]
COST = ["cost.low", "cost.high", "cost.assumptions"]


def supplier_fact(supplier_id: int | str, name: str) -> str:
    if name not in SUPPLIER_FIELDS:
        raise KeyError(f"unknown supplier fact: {name}")
    return f"supplier.{supplier_id}.{name}"


ALL_STATIC = set(PATIENT + ORDER + NOTE + PCP + HANDOFF + COST)


def is_known(fact_id: str) -> bool:
    if fact_id in ALL_STATIC:
        return True
    parts = fact_id.split(".")
    return len(parts) == 3 and parts[0] == "supplier" and parts[1].isdigit() and parts[2] in SUPPLIER_FIELDS


# Canonical forms by fact kind (anything not listed is free text, e.g. a name or an address).
YES_NO = {
    "patient.identity_verified",
    "patient.consent_proceed",
    "patient.consent_share_with_suppliers",
    "patient.consent_calls_texts",
    "patient.in_home_use",
    "patient.prior_wheelchair_5y",
    "patient.helper_permission",
    "patient.medicare_id_ok_to_share",
    "patient.delivery_received",
    "patient.fit_ok",
    "patient.shown_how_to_use",
    "patient.has_repair_number",
    "patient.deceased",
    "order.received",
    "order.item_matches_k0001",
    "order.beneficiary_ok",
    "order.description_ok",
    "order.quantity_ok",
    "order.order_date_ok",
    "order.practitioner_ok",
    "order.signature_ok",
    "order.changes_ok",
    "order.elements_complete",
    "note.received",
    "note.criteria_accepted",
    "pcp.release_signed",
}
SUPPLIER_YES_NO = {
    "accepting_new_medicare",
    "bills_medicare_k0001",
    "k0001_in_stock",
    "delivers_to_zip",
    "accepts_assignment_all_months",
    "abn_required",
    "readback_confirmed",
    "collects_coinsurance_at_delivery",
    "call_permission",
    "packet_sent",
    "accepted",
    "home_assessment_recorded",
    "same_or_similar_clear",
}
DIGITS = {
    "patient.age",
    "patient.contact_attempts",
    "order.version",
    "note.version",
    "handoff.supplier",
    "handoff.step",
    "handoff.packets_sent",
    "pcp.supplier_notified",
}
ISO_DATES = {"earliest_delivery", "scheduled_date", "delivered_date"}  # supplier facts
CHOICES = {
    "patient.medicaid_or_msp": ("yes", "no", "not_sure"),
    "pcp.request_status": ("sent", "confirmed", "not_received", "docs_received"),
    **{f"note.criterion_{c}": ("documented", "missing", "contradictory") for c in "abcdefg"},
}
SUPPLIER_STATUSES = (
    "candidate",
    "calling",
    "hold_needs_info",
    "qualified",
    "primary",
    "backup",
    "skipped",
    "released",
    "dropped",
)


def canonical_problem(fact_id: str, value: str) -> str:
    """Why `value` is not the canonical form of `fact_id`, or "" if it is (unknown is always canonical)."""
    if not isinstance(value, str):
        return f"{fact_id} must be a string, got {type(value).__name__}"
    if value == "":
        return ""
    name = fact_id.split(".", 2)[2] if fact_id.startswith("supplier.") else ""
    if fact_id in YES_NO or name in SUPPLIER_YES_NO:
        return "" if value in ("yes", "no") else f"{fact_id} takes yes or no, got {value!r}"
    if fact_id in DIGITS:
        return "" if value.isascii() and value.isdigit() else f"{fact_id} takes digits, got {value!r}"
    if name in ISO_DATES:
        ok = bool(re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value))
        if ok:
            from datetime import date

            try:
                date.fromisoformat(value)
            except ValueError:
                ok = False
        return "" if ok else f"{fact_id} takes a date as YYYY-MM-DD, got {value!r}"
    if name == "status":
        return "" if value in SUPPLIER_STATUSES else f"{fact_id} takes one of {', '.join(SUPPLIER_STATUSES)}"
    if fact_id in CHOICES:
        return "" if value in CHOICES[fact_id] else f"{fact_id} takes one of {', '.join(CHOICES[fact_id])}"
    if fact_id in ("cost.low", "cost.high"):
        return "" if re.fullmatch(r"[0-9]+\.[0-9]{2}", value) else f"{fact_id} takes money as 123.45, got {value!r}"
    if fact_id == "note.weight_lb":
        return "" if re.fullmatch(r"[0-9]+(\.[0-9]+)?", value) else f"{fact_id} takes pounds as a number, got {value!r}"
    return ""


def as_int(value: str, default: int | None = 0) -> int | None:
    """A counter or id fact as an int, or `default` if it is not digits. Never raises."""
    return int(value) if isinstance(value, str) and value.isascii() and value.isdigit() else default
