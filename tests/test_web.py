"""Website API end to end (no AI, no network)."""
import time

import pytest
from fastapi.testclient import TestClient

from conftest import CENTRAL_PDF, ROOT, STATE_PDF


@pytest.fixture
def client(tmp_db):
    import web
    return TestClient(web.app)


def wait_idle(client, timeout=30):
    end = time.time() + timeout
    while time.time() < end and client.get("/api/config").json()["check_running"]:
        time.sleep(0.2)


def test_full_flow(client):
    csv = (ROOT / "sample_data" / "inventory_sample.csv").read_text()
    assert client.post("/api/inventory/csv", content=csv, headers={"Content-Type": "text/csv"}).json() == {"added": 8, "skipped": 0}
    r = client.post("/api/lists/upload?title=state", content=STATE_PDF.read_bytes(), headers={"Content-Type": "application/pdf"}).json()
    assert r["status"] == "imported" and r["entries"] == 130
    wait_idle(client)
    r = client.post("/api/lists/upload?title=central", content=CENTRAL_PDF.read_bytes(), headers={"Content-Type": "application/pdf"}).json()
    assert r["entries"] == 55
    wait_idle(client)
    assert client.post("/api/lists/upload", content=CENTRAL_PDF.read_bytes()).json()["status"] == "duplicate"
    alerts = client.get("/api/alerts?status=new").json()
    by_batch = {a["entry"]["batch"]: a for a in alerts}
    assert by_batch["2511723"]["level"] == "HIGH" and by_batch["1-3098"]["level"] == "HIGH"
    assert by_batch["AI071L24"]["draft"]["severity"] == "CRITICAL"        # injection failed sterility
    assert len(alerts) == 13
    # pharmacist decides
    aid = by_batch["2511723"]["id"]
    assert client.post(f"/api/alerts/{aid}/decide", json={"status": "confirmed", "note": "returned"}).json()["status"] == "confirmed"
    assert len(client.get("/api/alerts?status=new").json()) == 12
    # re-check creates no duplicates
    client.post("/api/check?source=none"); wait_idle(client)
    assert len(client.get("/api/alerts").json()) == 13
    runs = client.get("/api/runs").json()
    assert runs[0]["status"] == "done" and any("match:" in l["msg"] for l in runs[0]["log"])


def test_public_search_and_validation(client):
    client.post("/api/lists/upload", content=STATE_PDF.read_bytes()); wait_idle(client)
    res = client.get("/api/search?batch=HT24686").json()
    assert res[0]["drug_name"].startswith("Pantrum-40") and res[0]["list_month"] == "2025-06"
    assert client.get("/api/search?name=ab").status_code == 422
    assert client.post("/api/lists/upload", content=b"hello").status_code == 422


def test_access_code_and_check_token(client, monkeypatch):
    import config
    monkeypatch.setattr(config, "_base_settings", config.settings.replace(access_code="s3cret", check_token="tok"))
    assert client.get("/api/inventory").status_code == 401
    assert client.get("/api/inventory", headers={"X-Access-Code": "s3cret"}).status_code == 200
    assert client.get("/api/search?name=Paracetamol").status_code == 200           # public
    assert client.post("/api/check?source=none").status_code == 401
    assert client.post("/api/check?source=none", headers={"X-Check-Token": "tok"}).json()["started"] is True
    wait_idle(client)


def test_cdsco_page_parsing_and_blocked_site(client, monkeypatch):
    from cdsco import parse_alerts_page
    html = """<table><tr><th>S.no</th><th>Title</th><th>Release Date</th><th>Download Pdf</th></tr>
      <tr><td>3</td><td>Availability of Not Standard Quality (NSQ) Alert on New Link</td><td>2025-Aug-28</td><td><a href="/x?num_id=1">pdf</a></td></tr>
      <tr><td>6</td><td>CDSCO NSQ ALERT FOR THE MONTH OF June 2025</td><td>2025-Jul-18</td><td><a href="/opencms/download_file_division.jsp?num_id=MTI5Mjc">pdf</a></td></tr>
      <tr><td>7</td><td>List of spurious Drugs for the month of June-2025</td><td>2025-Jul-18</td><td><a href="/d?num_id=2">pdf</a></td></tr>
      <tr><td>2</td><td>Circular to all State/UT Licensing Authorities regarding GST</td><td>2025-Oct-08</td><td><a href="/c">pdf</a></td></tr></table>"""
    links = parse_alerts_page(html, "https://cdsco.gov.in/opencms/opencms/en/Alerts/")
    assert [l.title for l in links] == ["CDSCO NSQ ALERT FOR THE MONTH OF June 2025", "List of spurious Drugs for the month of June-2025"]
    assert links[0].released == "2025-Jul-18" and links[0].url.startswith("https://cdsco.gov.in/opencms/download_file_division.jsp")
    import cdsco
    def blocked(url):
        raise OSError("HTTP Error 403: Forbidden")
    monkeypatch.setattr(cdsco, "_get", blocked)
    client.post("/api/check?source=cdsco"); wait_idle(client)
    run = client.get("/api/runs").json()[0]
    assert run["status"] == "done" and any("could not reach CDSCO" in l["msg"] for l in run["log"])


def test_old_list_is_reread_and_spurious_rows_marked(tmp_db):
    """A list stored by an older version (no per-row spurious marks) is offered to the laptop
    sync again; re-uploading the same PDF marks the fake rows and re-drafts undecided alerts."""
    import storage
    from cdsco import file_hash
    from conftest import SPURIOUS_PDF
    from fastapi.testclient import TestClient
    from models import InventoryItem
    from nsq_parser import parse_pdf
    import web

    data = SPURIOUS_PDF.read_bytes()
    old = parse_pdf(SPURIOUS_PDF, source_url="https://cdsco.gov.in/x")
    old = old.model_copy(update={"kind": "other",
                                 "entries": [e.model_copy(update={"spurious": False}) for e in old.entries]})
    list_id = storage.save_list(old, file_hash(data))
    storage._run("UPDATE lists SET parser_version = 0 WHERE id = ?", (list_id,), commit=True)
    entry_id = storage.get_entries(list_id)[0]["id"]
    item = InventoryItem(id=storage.add_item(InventoryItem(name="Taxim-O 200", batch="24460967")),
                         name="Taxim-O 200", batch="24460967")
    alert_id = storage.save_alert(inventory=item, entry_id=entry_id, entry=storage.entry_model(
        storage.get_entries(list_id)[0]), list_id=list_id, list_title="x", level="HIGH", score=1,
        why="", review=None, draft={"severity": "HIGH"})

    c = TestClient(web.app)
    assert c.get("/api/lists/sources").json() == []              # offered to the sync again
    r = c.post("/api/lists/upload?check=false", content=data).json()
    assert r["status"] == "updated" and r["spurious"] == 4 and r["kind"] == "spurious"
    assert all(e["spurious"] for e in storage.get_entries(list_id))
    assert c.get("/api/lists/sources").json() == ["https://cdsco.gov.in/x"]
    assert alert_id not in [a["id"] for a in storage.get_alerts()]   # undecided alert re-drafted next check
    assert c.post("/api/lists/upload?check=false", content=data).json()["status"] == "duplicate"


def test_spurious_row_in_mixed_list_is_critical(tmp_db):
    import pipeline
    import storage
    from models import InventoryItem, NSQEntry, NSQList
    storage.save_list(NSQList(title="Drug Alert February 2024", kind="mixed", entries=[
        NSQEntry(sno=1, drug_name="Real Tablets 10 mg", batch="A1", result="Dissolution"),
        NSQEntry(sno=2, drug_name="Fake Tablets 10 mg", batch="B2", result="Identification", spurious=True)]),
        "hash-mixed")
    storage.add_item(InventoryItem(name="Real Tablets 10 mg", batch="A1"))
    storage.add_item(InventoryItem(name="Fake Tablets 10 mg", batch="B2"))
    pipeline.run_check("none")
    sev = {a["entry"]["batch"]: a["draft"]["severity"] for a in storage.get_alerts()}
    assert sev["B2"] == "CRITICAL" and sev["A1"] != "CRITICAL"
