"""The delivery gate, in two parts. Delivery can only happen after both pass.

Each check returns the list of unmet conditions; an empty list means it passes.
The packet gate runs before handoff step 1; acceptance runs at handoff step 4.
"""

from __future__ import annotations

from dme import knowledge
from dme.facts import supplier_fact as sf
from dme.models import Case

SUPPLIER_MUST_SAY_YES = (
    "accepting_new_medicare",
    "bills_medicare_k0001",
    "k0001_in_stock",
    "delivers_to_zip",
    "accepts_assignment_all_months",
)
NOTE_REQUIRED = ("a", "b", "d", "e")  # plus F or G; C may be met at acceptance instead


def found(field, value: str = "yes") -> bool:
    """Whether the reader found this value; an ambiguous or contradictory "yes" is not a yes."""
    return field.status == "found" and field.value == value


def criteria_gaps(case: Case) -> list[str]:
    """Criteria the visit note does not document (C excluded: checked at acceptance)."""
    gaps = [f"criterion_{c}" for c in NOTE_REQUIRED if case.get(f"note.criterion_{c}") != "documented"]
    if "documented" not in (case.get("note.criterion_f"), case.get("note.criterion_g")):
        gaps.append("criterion_f_or_g")
    return gaps


def weight_ok(case: Case) -> bool:
    weight = case.get("note.weight_lb")
    limit = knowledge.entry("weight_threshold")["k0006_over_lb"]
    try:
        return float(weight) <= limit
    except ValueError:  # "" or anything that is not one number
        return False


ELIGIBLE_STATUSES = ("primary", "backup", "qualified")  # a supplier we may still hand off to


def supplier_qualified(case: Case, supplier_id: int) -> bool:
    """Its answers qualify it (read-back confirmed, every must-have "yes")."""
    return case.get(sf(supplier_id, "readback_confirmed")) == "yes" and all(
        case.get(sf(supplier_id, f)) == "yes" for f in SUPPLIER_MUST_SAY_YES
    )


def supplier_eligible(case: Case, supplier_id: int) -> bool:
    """Qualified and still in play: a released, skipped or dropped supplier's old answers never count."""
    return case.get(sf(supplier_id, "status")) in ELIGIBLE_STATUSES and supplier_qualified(case, supplier_id)


def document_gaps(case: Case) -> list[str]:
    """What the order and visit note still lack (the documents half of the packet gate)."""
    gaps = []
    if case.get("order.elements_complete") != "yes":
        gaps.append("signed order with all six elements")
    if case.get("order.item_matches_k0001") != "yes":
        gaps.append("item on the order is K0001")
    if case.get("order.changes_ok") != "yes":
        gaps.append("order changes initialled and dated")
    gaps += [f"visit note: {gap}" for gap in criteria_gaps(case)]
    if not weight_ok(case):
        gaps.append("weight stated and not over 250 lb")
    return gaps


def packet_gate(case: Case, supplier_id: int) -> list[str]:
    """Order, visit note, supplier and patient checks before any handoff."""
    unmet = document_gaps(case)
    if case.get("note.criteria_accepted") != "yes" or case.thread_state.get("criteria_accepted_version") != case.get(
        "note.version"
    ):
        unmet.append("criteria reading of the current visit note accepted")
    if not supplier_eligible(case, supplier_id):
        unmet.append(f"supplier {supplier_id} qualified")
    for fact in ("patient.identity_verified", "patient.consent_proceed", "patient.consent_share_with_suppliers"):
        if case.get(fact) != "yes":
            unmet.append(fact)
    if not case.get("patient.address"):
        unmet.append("patient.address")
    if case.get("patient.medicaid_or_msp") != "no":
        unmet.append("Medicaid/Savings Program answer is no")
    return unmet


def acceptance_gate(case: Case, supplier_id: int) -> list[str]:
    """Call permission for this supplier, criterion C, a clear same-or-similar check, and the packet sent."""
    unmet = []
    if case.get(sf(supplier_id, "call_permission")) != "yes":
        unmet.append(f"written permission for supplier {supplier_id} to call her")
    c_in_note = case.get("note.criterion_c") == "documented"
    if not c_in_note and case.get(sf(supplier_id, "home_assessment_recorded")) != "yes":
        unmet.append("criterion C: in the note or the supplier's home assessment")
    if case.get(sf(supplier_id, "same_or_similar_clear")) != "yes":
        unmet.append("same-or-similar check confirmed clear")
    if case.get(sf(supplier_id, "packet_sent")) != "yes":
        unmet.append("packet sent")
    return unmet
