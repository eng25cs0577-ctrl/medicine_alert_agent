"""Laptop sync helper end to end: a fake CDSCO site + the real website, over real HTTP."""
import http.server
import socket
import threading
import time

import pytest

from conftest import CENTRAL_PDF, SPURIOUS_PDF, STATE_PDF

ALERTS_HTML = """<table><tr><th>S.no</th><th>Title</th><th>Release Date</th><th>Download Pdf</th></tr>
<tr><td>3</td><td>Availability of Not Standard Quality (NSQ) Alert on New Link</td><td>2025-Aug-28</td><td><a href="/x.pdf">p</a></td></tr>
<tr><td>6</td><td>CDSCO NSQ ALERT FOR THE MONTH OF June 2025</td><td>2025-Jul-18</td><td><a href="/download?num_id=
central	">p</a></td></tr>
<tr><td>8</td><td>STATE NSQ ALERT FOR THE MONTH OF June 2025</td><td>2025-Jul-18</td><td><a href="/download?num_id=state">p</a></td></tr>
<tr><td>9</td><td>Revise list drug alert May-2025</td><td>2025-Jul-18</td><td><a href="/download?num_id=broken">p</a></td></tr>
<tr><td>10</td><td>List of spurious Drugs for the month of June-2025</td><td>2025-Jul-18</td><td><a href="/download?num_id=spurious">p</a></td></tr>
<tr><td>11</td><td>Spurious drug alert for the month of May-2025</td><td>2025-Jul-18</td><td><a href="/download?num_id=damaged">p</a></td></tr>
</table>"""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeCDSCO(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/alerts"):
            self.send_response(200)
            self.send_header("Set-Cookie", "JSESSIONID=abc; Path=/")
            self.end_headers()
            self.wfile.write(ALERTS_HTML.encode())
            return
        browser_like = "JSESSIONID=abc" in (self.headers.get("Cookie") or "") and \
            "/alerts" in (self.headers.get("Referer") or "")
        files = {"/download?num_id=central": CENTRAL_PDF.read_bytes(),
                 "/real/state.pdf": STATE_PDF.read_bytes(),
                 # like many government sites: a small page that points at the real file
                 "/download?num_id=state": b'<html><head><meta http-equiv="refresh" content="0;url=/real/state.pdf"></head></html>',
                 "/download?num_id=broken": b"<html><title>Error</title>Session expired</html>",
                 "/download?num_id=spurious": SPURIOUS_PDF.read_bytes(),
                 "/download?num_id=damaged": b"%PDF-1.4 damaged file"}
        body = files.get(self.path) if browser_like else b"<html><title>Access denied</title></html>"
        self.send_response(200 if body else 404)
        self.end_headers()
        self.wfile.write(body or b"")


@pytest.fixture
def servers(tmp_db, monkeypatch):
    import config
    import uvicorn
    monkeypatch.setattr(config, "_base_settings", config.settings.replace(check_token="tok123", access_code="code"))
    import web
    cdsco_port, site_port = free_port(), free_port()
    cdsco = http.server.ThreadingHTTPServer(("127.0.0.1", cdsco_port), FakeCDSCO)
    threading.Thread(target=cdsco.serve_forever, daemon=True).start()
    server = uvicorn.Server(uvicorn.Config(web.app, host="127.0.0.1", port=site_port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{site_port}", f"http://127.0.0.1:{cdsco_port}/alerts/"
    server.should_exit = True
    cdsco.shutdown()


def wait_idle(client):
    for _ in range(150):
        if not client.get("/api/config").json()["check_running"]:
            return
        time.sleep(0.2)


def test_sync_downloads_uploads_and_checks(servers, monkeypatch, tmp_path):
    import sync
    from fastapi.testclient import TestClient
    import web
    site, cdsco_page = servers
    monkeypatch.setattr(sync, "ENV_FILE", tmp_path / "sync.env")
    monkeypatch.setattr(sync, "LOG_FILE", tmp_path / "sync.log")
    monkeypatch.setattr(sync, "HERE", tmp_path)
    monkeypatch.setattr(sync, "STATE_FILE", tmp_path / "sync_state.json")
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    c = TestClient(web.app, headers={"X-Access-Code": "code"})
    c.post("/api/inventory", json={"name": "Pantrum-40 Tablets", "manufacturer": "Habitare Pharma", "batch": "HT24686"})
    c.post("/api/inventory", json={"name": "Taxim-O 200 Tablets", "manufacturer": "Alkem Laboratories", "batch": "24460967"})

    args = ["--site", site, "--token", "tok123", "--page", cdsco_page, "--quiet"]
    assert sync.main(args + ["--dry-run"]) == 0
    assert c.get("/api/lists").json() == []                         # dry run uploaded nothing

    assert sync.main(args) == 0
    lists = c.get("/api/lists").json()
    assert sorted(l["n_entries"] for l in lists) == [4, 55, 130]    # broken link skipped, notice row ignored
    assert {l["kind"] for l in lists} == {"central_nsq", "state_nsq", "spurious"}
    assert all(l["source_url"].startswith("http://127.0.0.1") and l["released"] == "2025-Jul-18" for l in lists)
    wait_idle(c)
    alerts = c.get("/api/alerts").json()
    assert any(a["entry"]["batch"] == "HT24686" and a["level"] == "HIGH" for a in alerts)
    fake = next(a for a in alerts if a["entry"]["batch"] == "24460967")
    assert fake["level"] == "HIGH" and fake["draft"]["severity"] == "CRITICAL"     # spurious = fake medicine
    log = (tmp_path / "sync.log").read_text()
    assert "uploaded: 130 entries" in log and "Started a check" in log
    assert "page title 'Error'" in log and "sync_debug_" in log          # diagnostics for a non-PDF
    assert "not imported (HTTP 422)" in log                         # the damaged PDF
    assert sync.load_state() == {f"{cdsco_page[:-len('/alerts/')]}/download?num_id=damaged": "unreadable"}

    (tmp_path / "sync.log").write_text("")
    assert sync.main(args) == 0                                     # second run: only the failed download
    log = (tmp_path / "sync.log").read_text()
    assert "website already has 3 CDSCO list(s)" in log and "skipping 1 list(s)" in log
    assert "1 new list(s) on CDSCO" in log and "Revise list" in log and "damaged" not in log

    (tmp_path / "sync.log").write_text("")
    assert sync.main(args + ["--retry-skipped"]) == 0               # unreadable ones tried again
    assert "Spurious drug alert for the month of May-2025" in (tmp_path / "sync.log").read_text()


def test_duplicate_upload_records_cdsco_link(servers, monkeypatch, tmp_path):
    """A list uploaded by hand has no CDSCO link; the sync's duplicate upload adds it,
    so the next run doesn't download it again."""
    import sync
    from fastapi.testclient import TestClient
    import web
    site, cdsco_page = servers
    for name in ("ENV_FILE", "LOG_FILE", "STATE_FILE"):
        monkeypatch.setattr(sync, name, tmp_path / name.lower())
    monkeypatch.setattr(sync, "HERE", tmp_path)
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    c = TestClient(web.app, headers={"X-Access-Code": "code"})
    assert c.post("/api/lists/upload?check=false", content=CENTRAL_PDF.read_bytes()).json()["status"] == "imported"
    assert c.get("/api/lists/sources").json() == []

    assert sync.main(["--site", site, "--token", "tok123", "--page", cdsco_page, "--quiet", "--max", "1"]) == 0
    assert "duplicate: already on the website" in (tmp_path / "log_file").read_text()
    [source] = c.get("/api/lists/sources").json()
    assert source.endswith("/download?num_id=central")


def test_sync_rejects_wrong_token(servers, monkeypatch, tmp_path):
    import sync
    site, cdsco_page = servers
    monkeypatch.setattr(sync, "LOG_FILE", tmp_path / "sync.log")
    with pytest.raises(SystemExit, match="rejected the CHECK_TOKEN"):
        sync.main(["--site", site, "--token", "wrong", "--page", cdsco_page, "--quiet"])


def test_env_file_settings(monkeypatch, tmp_path):
    import sync
    env = tmp_path / "sync.env"
    env.write_text("# comment\nSITE_URL=https://x.onrender.com/  # my site\nCHECK_TOKEN='abc'\n")
    monkeypatch.setattr(sync, "ENV_FILE", env)
    monkeypatch.delenv("SITE_URL", raising=False)
    monkeypatch.delenv("CHECK_TOKEN", raising=False)
    cfg = sync.load_settings(sync.argparse.Namespace(site=None, token=None, page=None, quiet=True))
    assert cfg == {"site": "https://x.onrender.com", "token": "abc", "pages": sync.DEFAULT_PAGES}


def test_upload_needs_code_or_token(servers):
    from fastapi.testclient import TestClient
    import web
    c = TestClient(web.app)
    pdf = CENTRAL_PDF.read_bytes()
    assert c.post("/api/lists/upload", content=pdf).status_code == 401
    assert c.post("/api/lists/upload?check=false", content=pdf, headers={"X-Check-Token": "tok123"}).json()["entries"] == 55
    assert c.get("/api/lists/sources", headers={"X-Check-Token": "tok123"}).status_code == 200


def test_inner_pdf_link():
    import sync
    base = "https://cdsco.gov.in/opencms/x.jsp?num_id=1"
    assert sync.inner_pdf_link('<meta http-equiv="refresh" content="0; url=/f/a.pdf">', base) == "https://cdsco.gov.in/f/a.pdf"
    assert sync.inner_pdf_link('<iframe src="viewer?file=b.pdf"></iframe>', base) == "https://cdsco.gov.in/opencms/viewer?file=b.pdf"
    assert sync.inner_pdf_link('<a href="docs/c.pdf">c</a>', base) == "https://cdsco.gov.in/opencms/docs/c.pdf"
    assert sync.inner_pdf_link("<p>nothing</p>", base) is None


def test_clean_url_removes_line_breaks():
    """Regression from the live CDSCO page: hrefs wrapped over lines crashed urllib."""
    from cdsco_page import clean_url, parse_alerts_page
    assert clean_url(" /x.jsp?num_id=MTI5\r\nMjc=\t ") == "/x.jsp?num_id=MTI5Mjc="
    assert clean_url("/a b.pdf") == "/a%20b.pdf"
    html = '<table><tr><td>1</td><td>CDSCO NSQ ALERT FOR THE MONTH OF July 2025</td><td>2025-Aug-01</td>' \
           '<td><a href="/opencms/download_file_division.jsp?num_id=\nMTI5Mjc">p</a></td></tr></table>'
    [link] = parse_alerts_page(html, "https://cdsco.gov.in/opencms/opencms/en/Alerts/")
    assert link.url == "https://cdsco.gov.in/opencms/download_file_division.jsp?num_id=MTI5Mjc"


def test_clean_url_encodes_long_dash():
    """Regression from the live CDSCO site: a file name with '–' crashed the download."""
    from cdsco_page import clean_url
    assert clean_url("/f/Spurious Nov–2024.pdf") == "/f/Spurious%20Nov%E2%80%932024.pdf"
    assert clean_url("/x.jsp?num_id=MTIyOTE=") == "/x.jsp?num_id=MTIyOTE="


def test_reports_are_not_lists():
    from cdsco_page import parse_alerts_page
    page = ("<table><tr><td>1</td><td>Report on countrywide survey for Spurious Drugs</td><td>2010-Feb-24</td>"
            "<td><a href='/r.pdf'>p</a></td></tr></table>")
    assert parse_alerts_page(page, "https://cdsco.gov.in/") == []


def test_upload_timeout_skips_list_and_continues(monkeypatch, tmp_path):
    import sync
    from cdsco_page import AlertLink
    for name in ("LOG_FILE", "STATE_FILE"):
        monkeypatch.setattr(sync, name, tmp_path / name.lower())
    links = [AlertLink("Big report spurious", "", "https://c/big"), AlertLink("NSQ list", "", "https://c/ok")]
    monkeypatch.setattr(sync, "load_settings", lambda a: {"site": "https://s", "token": "t", "pages": []})
    monkeypatch.setattr(sync, "wake_site", lambda cfg: None)
    monkeypatch.setattr(sync, "find_new_lists", lambda cfg, known, client: links)
    monkeypatch.setattr(sync, "start_check", lambda cfg: None)
    monkeypatch.setattr(sync.CDSCOClient, "pdf", lambda self, url: (b"%PDF-1.4", "application/pdf", b""))

    def site_call(cfg, path, **kw):
        if path == "/api/lists/sources":
            return 200, []
        if "big" in path:
            raise TimeoutError("The read operation timed out")
        return 200, {"status": "imported", "entries": 3, "kind": "central_nsq", "month": "2025-01"}
    monkeypatch.setattr(sync, "site_call", site_call)
    assert sync.main(["--quiet"]) == 0
    log = (tmp_path / "log_file").read_text()
    assert "upload timed out" in log and "Done: 1 new list(s) uploaded." in log
    assert sync.load_state() == {"https://c/big": "unreadable"}
