import pytest
import yaml

from dme import knowledge
from dme.models import ReaderName


def test_every_entry_has_a_citation_and_check_status():
    for e in knowledge.load()["entries"]:
        assert e["citation"].strip(), e["id"]
        assert e["verified"] in {"checked", "not_rechecked", "design"}, e["id"]
        assert e["topics"], e["id"]


def test_contents_the_rules_rely_on():
    assert len(knowledge.entry("swo_elements")["elements"]) == 6
    assert set(knowledge.entry("coverage_criteria")["criteria"]) == set("abcdefg")
    assert knowledge.entry("weight_threshold")["k0006_over_lb"] == 250
    assert knowledge.entry("weight_threshold")["verified"] == "checked"
    assert knowledge.entry("weight_threshold_k0007")["verified"] == "not_rechecked"
    wopd = knowledge.entry("wopd")["codes_on_list"]
    assert "K0001" not in wopd and {"K0002", "K0007", "E1161"} <= set(wopd)
    cost = knowledge.entry("rental_cost")
    assert cost["monthly_allowed_months_1_3"] == "34.27" and cost["rental_months"] == 13
    assert cost["part_b_deductible"][2027]["assumed"] is True


def test_code_table_lookup_is_exact():
    assert knowledge.code_entry(" k0001 ")["requested"] is True
    assert knowledge.code_entry("K0003")["requested"] is False
    for code in ("K0002", "K0003", "K0004", "K0005", "K0006", "K0007"):
        assert knowledge.code_entry(code)["relation"] == "different item (not what was requested)"
    assert knowledge.code_entry("KOOO1") is None  # OCR-like misread: unknown, ask the PCP


@pytest.mark.parametrize("reader", ReaderName.__args__)
def test_rule_slice_for_every_reader_is_cited_yaml(reader):
    text = knowledge.rule_slice(reader)
    entries = yaml.safe_load(text)
    assert entries and all(e["citation"] for e in entries)


def test_slices_are_scoped():
    assert "coverage_criteria" in knowledge.rule_slice("read_visit_note")
    assert "coverage_criteria" not in knowledge.rule_slice("read_supplier_call")
    assert "assignment" in knowledge.rule_slice("check_safety_and_conditions")
    with pytest.raises(KeyError):
        knowledge.rule_slice("read_tea_leaves")
