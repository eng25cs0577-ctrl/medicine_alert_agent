"""MedAlert website (FastAPI).

    uvicorn web:app --host 0.0.0.0 --port 7860

Public:      GET /, /api/config, /api/search (check a medicine), /healthz
Pharmacist:  inventory, lists, alerts, checks (protected by ACCESS_CODE if set)
Scheduler:   POST /api/check with header X-Check-Token: <CHECK_TOKEN> (daily GitHub Action)
"""

from __future__ import annotations

import csv
import io
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import storage
from config import get_logger, settings, setup_logging
from matching import search_entries
from models import InventoryItem

setup_logging()
log = get_logger("Web")
STATIC = Path(__file__).parent / "static"
MAX_PDF_BYTES = 15 * 1024 * 1024



@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:                       # titles/months that older versions stored garbled or empty
        if n := storage.repair_lists():
            log.info("repaired the title/month of %d stored list(s)", n)
    except Exception as exc:  # noqa: BLE001 - never block start-up
        log.warning("could not repair stored lists: %s", exc)
    yield


app = FastAPI(title="MedAlert", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
_running = threading.Event()


def _check_access(code: str | None) -> None:
    if settings.access_code and not secrets.compare_digest(code or "", settings.access_code):
        raise HTTPException(status_code=401, detail="Wrong access code.")


def _check_access_or_token(code: str | None, token: str | None) -> None:
    """Pharmacist (access code) or an automated helper (CHECK_TOKEN: daily job, laptop sync)."""
    if settings.check_token and token and secrets.compare_digest(token, settings.check_token):
        return
    _check_access(code)


def _start_check(source: str) -> dict:
    from pipeline import run_check

    if _running.is_set():
        raise HTTPException(status_code=409, detail="A check is already running. Please wait.")
    _running.set()

    def work():
        try:
            run_check(source)
        finally:
            _running.clear()
    threading.Thread(target=work, daemon=True, name="check").start()
    return {"started": True, "source": source}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/api/config")
def get_config() -> dict:
    return {"access_code_required": bool(settings.access_code), "ai_enabled": settings.llm_enabled,
            "model": settings.api_model if settings.llm_enabled else None,
            "backup_model": settings.fallback_model if settings.has_fallback else None,
            "storage": "permanent" if storage.is_permanent() else "temporary",
            "check_running": _running.is_set()}


# --------------------------------- public ---------------------------------- #
@app.get("/api/search")
def search(name: str = "", batch: str = "") -> list[dict]:
    """Check a medicine against every stored list (no access code needed)."""
    if len(name.strip()) < 3 and len(batch.strip()) < 2:
        raise HTTPException(status_code=422, detail="Type at least 3 letters of the name, or a batch number.")
    rows = storage.get_entries()
    models = [storage.entry_model(r) for r in rows]
    row_of = {id(m): r for m, r in zip(models, rows)}
    out = []
    for entry, score in search_entries(models, name, batch)[:50]:
        row = row_of[id(entry)]
        out.append({**entry.model_dump(), "score": score, "list_title": row["list_title"],
                    "list_month": row["list_month"], "list_kind": row["list_kind"]})
    return out


# ------------------------------- inventory --------------------------------- #
@app.get("/api/inventory")
def inventory(x_access_code: str | None = Header(default=None)) -> list[dict]:
    _check_access(x_access_code)
    return [i.model_dump() for i in storage.get_inventory()]


@app.post("/api/inventory")
def add_inventory(item: InventoryItem, x_access_code: str | None = Header(default=None)) -> dict:
    _check_access(x_access_code)
    return {"id": storage.add_item(item)}


@app.delete("/api/inventory/{item_id}")
def delete_inventory(item_id: int, x_access_code: str | None = Header(default=None)) -> dict:
    _check_access(x_access_code)
    if not storage.delete_item(item_id):
        raise HTTPException(status_code=404, detail="Not found.")
    return {"deleted": item_id}


@app.post("/api/inventory/csv")
async def import_inventory_csv(request: Request, x_access_code: str | None = Header(default=None)) -> dict:
    """Body: CSV text with columns name, manufacturer, batch, expiry, quantity, location."""
    _check_access(x_access_code)
    text = (await request.body()).decode("utf-8-sig", "replace")
    reader = csv.DictReader(io.StringIO(text))
    added, skipped = 0, 0
    for row in reader:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        try:
            qty = int(float(row.get("quantity") or 0))
            storage.add_item(InventoryItem(name=row.get("name", ""), manufacturer=row.get("manufacturer", ""),
                                           batch=row.get("batch", ""), expiry=row.get("expiry", ""),
                                           quantity=qty, location=row.get("location", "")))
            added += 1
        except Exception:  # noqa: BLE001 - skip bad rows, report the count
            skipped += 1
    return {"added": added, "skipped": skipped}


# --------------------------------- lists ----------------------------------- #
@app.get("/api/lists")
def lists(x_access_code: str | None = Header(default=None)) -> list[dict]:
    _check_access(x_access_code)
    return storage.get_lists()


@app.get("/api/lists/{list_id}/entries")
def list_entries(list_id: int, x_access_code: str | None = Header(default=None)) -> list[dict]:
    _check_access(x_access_code)
    return storage.get_entries(list_id)


@app.get("/api/lists/sources")
def list_sources(x_access_code: str | None = Header(default=None),
                 x_check_token: str | None = Header(default=None)) -> list[str]:
    """CDSCO links of the lists already stored, so the laptop sync skips them."""
    _check_access_or_token(x_access_code, x_check_token)
    return [l["source_url"] for l in storage.get_lists() if l.get("source_url")]


@app.delete("/api/lists/{list_id}")
def delete_list(list_id: int, x_access_code: str | None = Header(default=None)) -> dict:
    _check_access(x_access_code)
    if not storage.delete_list(list_id):
        raise HTTPException(status_code=404, detail="Not found.")
    return {"deleted": list_id}


@app.post("/api/lists/upload")
async def upload_list(request: Request, title: str = "", source_url: str = "", released: str = "",
                      check: bool = True, x_access_code: str | None = Header(default=None),
                      x_check_token: str | None = Header(default=None)) -> dict:
    """Body: the PDF file itself (Content-Type: application/pdf). Then re-checks the inventory
    (check=false lets the laptop sync upload several lists and start one check at the end)."""
    _check_access_or_token(x_access_code, x_check_token)
    from pipeline import import_pdf_bytes

    data = await request.body()
    if not data.startswith(b"%PDF"):
        raise HTTPException(status_code=422, detail="That file is not a PDF.")
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(status_code=413, detail="PDF is too large (max 15 MB).")
    result = import_pdf_bytes(data, title=title[:300], source_url=source_url[:500], released=released[:40])
    if result["status"] == "empty":
        raise HTTPException(status_code=422, detail="No table rows found - is this a CDSCO NSQ/spurious list?")
    if check and result["status"] == "imported" and not _running.is_set():
        _start_check("none")
    return result


# --------------------------------- alerts ---------------------------------- #
@app.get("/api/alerts")
def alerts(status: str | None = None, x_access_code: str | None = Header(default=None)) -> list[dict]:
    _check_access(x_access_code)
    return storage.get_alerts(status)


class Decision(BaseModel):
    status: Literal["confirmed", "dismissed", "new"]
    note: str = ""


@app.post("/api/alerts/{alert_id}/decide")
def decide(alert_id: int, d: Decision, x_access_code: str | None = Header(default=None)) -> dict:
    _check_access(x_access_code)
    if not storage.decide_alert(alert_id, d.status, d.note[:500]):
        raise HTTPException(status_code=404, detail="Not found.")
    return {"id": alert_id, "status": d.status}


# --------------------------------- checks ---------------------------------- #
@app.post("/api/check")
def check(source: Literal["cdsco", "none"] = "cdsco", x_access_code: str | None = Header(default=None),
          x_check_token: str | None = Header(default=None)) -> dict:
    """Start a check: from the website (access code) or the daily scheduler (check token)."""
    _check_access_or_token(x_access_code, x_check_token)
    return _start_check(source)


@app.get("/api/runs")
def runs(x_access_code: str | None = Header(default=None)) -> list[dict]:
    _check_access(x_access_code)
    return storage.get_runs()
