"""Matching stock against the real June 2025 lists."""
import pytest

from conftest import CENTRAL_PDF, STATE_PDF
from matching import batch_variants, compare, find_matches, maker_similarity, name_similarity, search_entries
from models import InventoryItem, NSQEntry
from nsq_parser import parse_pdf

ENTRIES = parse_pdf(STATE_PDF).entries + parse_pdf(CENTRAL_PDF).entries


def item(name, maker="", batch=""):
    return InventoryItem(id=1, name=name, manufacturer=maker, batch=batch)


def levels(i):
    return sorted({m.level for m in find_matches([i], ENTRIES)})


def test_exact_batch_is_high():
    m = find_matches([item("Paracetamol Tablets IP 500 mg", "Karnataka Antibiotics & Pharmaceuticals", "2511723")], ENTRIES)
    assert m[0].level == "HIGH" and m[0].entry.batch == "2511723"


def test_same_product_other_batch_is_medium():
    m = find_matches([item("Methotrexate Injection 15 mg/ml (Folitrax-15)", "BDH Industrial Ltd", "ZZ999")], ENTRIES)
    assert {x.level for x in m} == {"MEDIUM"} and {x.entry.batch for x in m} == {"AI054J24", "AI071L24"}


def test_brand_only_name_matches():
    assert "HIGH" in levels(item("Pantrum-40", "Habitare Pharma", "HT24686"))


def test_different_strength_or_maker_is_not_a_match():
    assert levels(item("Paracetamol Tablets IP 650 mg", "Micro Labs Ltd", "X1")) == []
    # 'Pharmaceuticals' in both names must not make two makers look alike
    assert maker_similarity("Tam-Bran Pharmaceuticals", "M/s. Paschim Banga Pharmaceuticals, (A Unit)") == 0.0
    assert all(m.entry.batch == "1-3098" for m in
               find_matches([item("Dextrose Injection 5% (D5)", "Tam-Bran Pharmaceuticals", "1-3098")], ENTRIES))


def test_unknown_maker_is_low():
    assert levels(item("Sodium Chloride Injection IP 0.9%")) == ["LOW"]


def test_unrelated_medicine_no_match():
    assert levels(item("Atorvastatin Tablets IP 10 mg", "Sun Pharmaceutical Industries", "ATV1")) == []


def test_batch_variants_and_names():
    assert batch_variants("B.NO:IEW-1487C,IEW-1487B") == {"IEW1487C", "IEW1487B"}
    assert name_similarity("Folitrax-15", "Methotrexate Injection IP 15 mg/ml (Folitrax-15)") >= 0.9
    level, _, _ = compare(item("Dolo", "X", "Q1"), NSQEntry(drug_name="Paracetamol Tablets", batch="Z"))
    assert level is None


def test_public_search():
    hits = search_entries(ENTRIES, batch="ht24686")
    assert hits and hits[0][0].drug_name.startswith("Pantrum-40") and hits[0][1] == 1.0
    assert search_entries(ENTRIES, batch="NOTLISTED123") == []
    assert any("Lorazepam" in e.drug_name for e, _ in search_entries(ENTRIES, name="Lorazepam 2 mg"))
