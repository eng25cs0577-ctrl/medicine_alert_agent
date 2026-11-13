"""Laptop sync helper end to end: a fake CDSCO site + the real website, over real HTTP."""
import http.server
import socket
import threading
import time

import pytest

from conftest import CENTRAL_PDF, STATE_PDF

ALERTS_HTML = """<table><tr><th>S.no</th><th>Title</th><th>Release Date</th><th>Download Pdf</th></tr>
<tr><td>3</td><td>Availability of Not Standard Quality (NSQ) Alert on New Link</td><td>2025-Aug-28</td><td><a href="/x.pdf">p</a></td></tr>
<tr><td>6</td><td>CDSCO NSQ ALERT FOR THE MONTH OF June 2025</td><td>2025-Jul-18</td><td><a href="/download?num_id=central">p</a></td></tr>
<tr><td>8</td><td>STATE NSQ ALERT FOR THE MONTH OF June 2025</td><td>2025-Jul-18</td><td><a href="/download?num_id=state">p</a></td></tr>
<tr><td>9</td><td>Revise list drug alert May-2025</td><td>2025-Jul-18</td><td><a href="/download?num_id=broken">p</a></td></tr>
</table>"""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeCDSCO(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        files = {"/download?num_id=central": CENTRAL_PDF.read_bytes(), "/download?num_id=state": STATE_PDF.read_bytes(),
                 "/download?num_id=broken": b"<html>error page</html>"}
        body = ALERTS_HTML.encode() if self.path.startswith("/alerts") else files.get(self.path)
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
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    c = TestClient(web.app, headers={"X-Access-Code": "code"})
    c.post("/api/inventory", json={"name": "Pantrum-40 Tablets", "manufacturer": "Habitare Pharma", "batch": "HT24686"})

    args = ["--site", site, "--token", "tok123", "--page", cdsco_page, "--quiet"]
    assert sync.main(args + ["--dry-run"]) == 0
    assert c.get("/api/lists").json() == []                         # dry run uploaded nothing

    assert sync.main(args) == 0
    lists = c.get("/api/lists").json()
    assert sorted(l["n_entries"] for l in lists) == [55, 130]       # broken link skipped, notice row ignored
    assert all(l["source_url"].startswith("http://127.0.0.1") and l["released"] == "2025-Jul-18" for l in lists)
    wait_idle(c)
    alerts = c.get("/api/alerts").json()
    assert any(a["entry"]["batch"] == "HT24686" and a["level"] == "HIGH" for a in alerts)
    log = (tmp_path / "sync.log").read_text()
    assert "uploaded: 130 entries" in log and "Started a check" in log

    assert sync.main(args) == 0                                     # second run: nothing new
    log = (tmp_path / "sync.log").read_text()
    assert "website already has 2 CDSCO list(s)" in log
    assert log.rstrip().endswith("No new lists on CDSCO. Nothing to do.") or "Done: 0 new list(s)" in log


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
