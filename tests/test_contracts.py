"""The shared files load and the reader schemas work as structured-output schemas (closed, no nulls)."""

import json
from pathlib import Path

import yaml

from dme import models as m

ROOT = Path(__file__).resolve().parent.parent

READERS = [
    m.SupplierCallRead,
    m.PcpReplyRead,
    m.OrderDocumentRead,
    m.VisitNoteRead,
    m.PatientReplyRead,
    m.SafetyRead,
    m.PatientMessageDraft,
    m.Proposal,
]


def test_directory_has_the_twelve_given_suppliers():
    d = yaml.safe_load((ROOT / "scenarios/directory.yaml").read_text())["suppliers"]
    assert [s["id"] for s in d] == list(range(1, 13))
    for s in d:
        assert set(s) == {"id", "name", "phone", "address"}  # sparse on purpose


def test_case_seed_has_only_intake_facts():
    c = yaml.safe_load((ROOT / "scenarios/eleanor_case.yaml").read_text())
    case = m.Case(case_id=c["case_id"], facts=c["facts"])
    assert case.get("patient.zip") == ""
    assert case.get("order.hcpcs_expected") == "K0001"


def test_reader_schemas_have_no_nullable_fields():
    for model in READERS:
        schema = json.dumps(model.model_json_schema())
        assert '"null"' not in schema, model.__name__


def test_enum_and_status_casing_normalised():
    r = m.CallOutcomeRead(evidence_turn_id="t2", evidence_quote="x", status="FOUND", value="No_Answer")
    assert (r.status, r.value) == ("found", "no_answer")


def test_safety_and_supplier_conditions_share_names():
    for name in m.SUPPLIER_CONDITIONS:
        assert name in m.SupplierCallRead.model_fields
        assert name in m.SafetyRead.model_fields
    for name in m.SAFETY_SIGNALS:
        assert name in m.SafetyRead.model_fields


def test_fact_ids_and_supplier_helper():
    from dme import facts

    assert facts.supplier_fact(4, "k0001_in_stock") == "supplier.4.k0001_in_stock"
    assert facts.is_known("patient.medicare_id") and facts.is_known("supplier.12.status")
    assert not facts.is_known("supplier.4.made_up")
    case = __import__("yaml").safe_load((ROOT / "scenarios/eleanor_case.yaml").read_text())
    assert all(facts.is_known(k) for k in case["facts"])


def test_reader_fields_action_names_and_call_tags():
    assert "medicare_id" in m.PatientReplyRead.model_fields
    assert "item_confirmed" in m.PcpReplyRead.model_fields
    assert "proceed_with_pending" in m.ActionName.__args__
    from dme.protocols import CallContext

    a = CallContext(case_id="c", goal="g", facts={}, row_key="pcp|order_request|1")
    assert a.tag("read_pcp_reply", "x") == a.tag("read_pcp_reply", "x") != a.tag("read_order_document", "x")
    assert a.tag("read_pcp_reply", "x") != a.tag("read_pcp_reply", "y")
