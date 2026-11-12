"""AutoGen review (scripted model replies), CrewAI drafting (rules + crew build)."""
from alert_crew import _crew, draft_alert, rule_severity
from models import InventoryItem, NSQEntry
from review_team import parse_review, review_candidates

STOCK = InventoryItem(id=1, name="Sodium Chloride Injection IP 0.9%")
CANDS = [NSQEntry(drug_name="Sodium Chloride Injection IP 0.9% w/v (NS)", batch="TI204A002",
                  manufacturer="M/s. Puniska Injectables", result="Sterility"),
         NSQEntry(drug_name="Sodium Chloride Injection I.P (NS)", batch="24EC721",
                  manufacturer="M/s. Pentagon Labs", result="Sterility")]


def test_autogen_debate_with_replay_client():
    from autogen_ext.models.replay import ReplayChatCompletionClient
    client = ReplayChatCompletionClient([
        "1 and 2 have the same drug and strength; our record has no maker or batch, so neither can be ruled out.",
        "Agreed - the pack must be checked.\nFINAL DECISION: UNSURE (candidates 1, 2)\nREASONS:\n"
        "- No manufacturer or batch recorded for our stock\n- Both failed sterility"])
    r = review_candidates(STOCK, CANDS, model_client=client)
    assert r.decision == "UNSURE" and r.matched == [1, 2]
    assert r.reasons == ["No manufacturer or batch recorded for our stock", "Both failed sterility"]
    assert "### Matcher" in r.transcript and "### Sceptic" in r.transcript


def test_parse_review_variants():
    assert parse_review([("Sceptic", "**FINAL DECISION:** NOT_MATCH\nREASONS:\n- different maker")], 2).decision == "NOT_MATCH"
    r = parse_review([("Sceptic", "FINAL DECISION: MATCH (candidates 2)")], 2)
    assert (r.decision, r.matched) == ("MATCH", [2])
    assert parse_review([("Matcher", "no decision")], 2).decision == "UNSURE"


def test_review_without_ai_key_is_unsure(tmp_db):
    r = review_candidates(STOCK, CANDS)
    assert r.decision == "UNSURE" and r.matched == [1, 2]


def test_rule_severity():
    inj = NSQEntry(drug_name="Methotrexate Injection IP", result="Sterility")
    tab = NSQEntry(drug_name="Paracetamol Tablets", result="Description")
    assay = NSQEntry(drug_name="Lorazepam Tablets", result="Assay for Lorazepam")
    assert rule_severity(inj)[0] == "CRITICAL" and rule_severity(tab)[0] == "MODERATE"
    assert rule_severity(assay)[0] == "HIGH" and rule_severity(tab, spurious=True)[0] == "CRITICAL"


def test_rule_draft_used_without_ai(tmp_db):
    d = draft_alert(InventoryItem(id=1, name="Pantrum-40", batch="HT24686"),
                    NSQEntry(drug_name="Pantrum-40 Tablets", batch="HT24686", result="Dissolution",
                             manufacturer="M/s. Habitare Pharma pvt. Ltd., Plot"), "HIGH")
    assert d.severity == "HIGH" and "quarantine" in " ".join(d.actions).lower()
    assert "HT24686" in d.supplier_note and "Habitare Pharma pvt. Ltd." in d.supplier_note


def test_crew_builds(tmp_db, monkeypatch):
    import config
    monkeypatch.setattr(config, "_base_settings", config._base_settings.__class__(api_key="x"))
    crew = _crew("Our stock: X")
    assert [a.role for a in crew.agents] == ["Pharmacovigilance Risk Assessor", "Pharmacy Notice Writer"]
    assert crew.tasks[-1].output_pydantic.__name__ == "AlertDraft"
