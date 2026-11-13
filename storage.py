"""Database: NSQ lists and entries, the medicine inventory, alerts and check runs.

PostgreSQL when DATABASE_URL is set (e.g. a free Neon database: permanent), else a
SQLite file in DATA_DIR (fine on your own computer; wiped on free cloud hosts).
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from config import settings
from models import InventoryItem, NSQEntry, NSQList

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lists (
    id {pk}, title TEXT NOT NULL, kind TEXT, month TEXT, source_url TEXT, released TEXT,
    file_hash TEXT UNIQUE, n_entries INTEGER DEFAULT 0, created {real} NOT NULL
);
CREATE TABLE IF NOT EXISTS entries (
    id {pk}, list_id INTEGER NOT NULL, sno INTEGER, drug_name TEXT NOT NULL, batch TEXT,
    mfg_date TEXT, exp_date TEXT, manufacturer TEXT, result TEXT, reported_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_entries_list ON entries(list_id);
CREATE TABLE IF NOT EXISTS inventory (
    id {pk}, name TEXT NOT NULL, manufacturer TEXT, batch TEXT, expiry TEXT,
    quantity INTEGER DEFAULT 0, location TEXT, created {real} NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id {pk}, created {real} NOT NULL, inventory_id INTEGER, entry_id INTEGER, list_id INTEGER,
    list_title TEXT, level TEXT, score {real}, why TEXT, inventory_json TEXT, entry_json TEXT,
    review_json TEXT, draft_json TEXT, status TEXT DEFAULT 'new', note TEXT, decided {real}
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_alert_unique ON alerts(inventory_id, entry_id);
CREATE TABLE IF NOT EXISTS runs (
    id {pk}, started {real} NOT NULL, finished {real}, status TEXT, summary TEXT, log_json TEXT
);
"""

_pg_ready: set[str] = set()


def backend() -> str:
    url = settings.database_url.strip().lower()
    return "postgres" if url.startswith(("postgres://", "postgresql://")) else "sqlite"


def is_permanent() -> bool:
    return backend() == "postgres"


def _statements(pk: str, real: str) -> list[str]:
    return [s.strip() for s in _SCHEMA.format(pk=pk, real=real).split(";") if s.strip()]


def _sqlite():
    path = Path(settings.data_dir)
    path.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path / "medalert.db", timeout=15)
    conn.row_factory = sqlite3.Row
    for stmt in _statements("INTEGER PRIMARY KEY AUTOINCREMENT", "REAL"):
        conn.execute(stmt)
    return conn


def _postgres():
    import psycopg
    from psycopg.rows import dict_row

    url = settings.database_url.strip()
    conn = psycopg.connect(url, row_factory=dict_row, connect_timeout=15)
    if url not in _pg_ready:
        with conn.cursor() as cur:
            for stmt in _statements("SERIAL PRIMARY KEY", "DOUBLE PRECISION"):
                cur.execute(stmt)
        conn.commit()
        _pg_ready.add(url)
    return conn


def _run(sql: str, args: tuple = (), *, fetch: str | None = None, commit: bool = False,
         returning_id: bool = False):
    """One statement on the active backend. SQL uses '?' placeholders."""
    if backend() == "postgres":
        if returning_id:
            sql += " RETURNING id"
        with closing(_postgres()) as conn:
            with conn.cursor() as cur:
                cur.execute(sql.replace("?", "%s"), args)
                if returning_id:
                    out = cur.fetchone()["id"]
                elif fetch == "all":
                    out = [dict(r) for r in cur.fetchall()]
                elif fetch == "one":
                    r = cur.fetchone()
                    out = dict(r) if r else None
                else:
                    out = cur.rowcount
            if commit:
                conn.commit()
            return out
    with closing(_sqlite()) as conn:
        cur = conn.execute(sql, args)
        if returning_id:
            out = cur.lastrowid
        elif fetch == "all":
            out = [dict(r) for r in cur.fetchall()]
        elif fetch == "one":
            r = cur.fetchone()
            out = dict(r) if r else None
        else:
            out = cur.rowcount
        if commit:
            conn.commit()
        return out


# ------------------------------- lists ------------------------------------- #
def list_exists(file_hash: str) -> bool:
    return _run("SELECT id FROM lists WHERE file_hash = ?", (file_hash,), fetch="one") is not None


def set_source_if_missing(file_hash: str, source_url: str, released: str = "") -> None:
    """A list uploaded by hand has no CDSCO link; record it when the laptop sync sends the
    same file, so the sync knows the list is already here."""
    _run("UPDATE lists SET source_url = ?, released = COALESCE(NULLIF(released, ''), ?)"
         " WHERE file_hash = ? AND (source_url IS NULL OR source_url = '')",
         (source_url, released, file_hash), commit=True)


def save_list(lst: NSQList, file_hash: str) -> int:
    list_id = _run("INSERT INTO lists (title, kind, month, source_url, released, file_hash, n_entries, created)"
                   " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (lst.title, lst.kind, lst.month, lst.source_url, lst.released, file_hash,
                    len(lst.entries), time.time()), commit=True, returning_id=True)
    for e in lst.entries:
        _run("INSERT INTO entries (list_id, sno, drug_name, batch, mfg_date, exp_date, manufacturer, result,"
             " reported_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
             (list_id, e.sno, e.drug_name, e.batch, e.mfg_date, e.exp_date, e.manufacturer, e.result,
              e.reported_by), commit=True)
    return list_id


def repair_lists() -> int:
    """Fill in what older versions missed: garbled titles ('Januaryâ 2025') and months
    written short ('Feb-2025'). Runs at start-up; returns how many lists changed."""
    from cdsco_page import fix_text
    from nsq_parser import detect_month

    changed = 0
    for row in _run("SELECT id, title, month FROM lists", fetch="all"):
        title = fix_text(row["title"] or "")
        month = row["month"] or detect_month(title)
        if title != (row["title"] or "") or month != (row["month"] or ""):
            _run("UPDATE lists SET title = ?, month = ? WHERE id = ?", (title, month, row["id"]), commit=True)
            changed += 1
    return changed


def get_lists() -> list[dict]:
    return _run("SELECT id, title, kind, month, source_url, released, n_entries, created FROM lists"
                " ORDER BY month DESC, created DESC", fetch="all")


def get_entries(list_id: int | None = None) -> list[dict]:
    if list_id is None:
        return _run("SELECT e.*, l.title AS list_title, l.month AS list_month, l.kind AS list_kind"
                    " FROM entries e JOIN lists l ON l.id = e.list_id ORDER BY l.month DESC, e.sno",
                    fetch="all")
    return _run("SELECT * FROM entries WHERE list_id = ? ORDER BY sno", (list_id,), fetch="all")


def entry_model(row: dict) -> NSQEntry:
    return NSQEntry(**{k: row.get(k) or ("" if k != "sno" else 0) for k in NSQEntry.model_fields})


def delete_list(list_id: int) -> bool:
    _run("DELETE FROM entries WHERE list_id = ?", (list_id,), commit=True)
    _run("DELETE FROM alerts WHERE list_id = ? AND status = 'new'", (list_id,), commit=True)
    return _run("DELETE FROM lists WHERE id = ?", (list_id,), commit=True) > 0


# ------------------------------ inventory ---------------------------------- #
def add_item(item: InventoryItem) -> int:
    return _run("INSERT INTO inventory (name, manufacturer, batch, expiry, quantity, location, created)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (item.name.strip(), item.manufacturer.strip(), item.batch.strip(), item.expiry.strip(),
                 item.quantity, item.location.strip(), time.time()), commit=True, returning_id=True)


def get_inventory() -> list[InventoryItem]:
    rows = _run("SELECT * FROM inventory ORDER BY name", fetch="all")
    return [InventoryItem(id=r["id"], name=r["name"], manufacturer=r["manufacturer"] or "",
                          batch=r["batch"] or "", expiry=r["expiry"] or "", quantity=r["quantity"] or 0,
                          location=r["location"] or "") for r in rows]


def delete_item(item_id: int) -> bool:
    _run("DELETE FROM alerts WHERE inventory_id = ? AND status = 'new'", (item_id,), commit=True)
    return _run("DELETE FROM inventory WHERE id = ?", (item_id,), commit=True) > 0


# -------------------------------- alerts ----------------------------------- #
def alert_exists(inventory_id: int, entry_id: int) -> bool:
    return _run("SELECT id FROM alerts WHERE inventory_id = ? AND entry_id = ?",
                (inventory_id, entry_id), fetch="one") is not None


def save_alert(*, inventory: InventoryItem, entry_id: int, entry: NSQEntry, list_id: int, list_title: str,
               level: str, score: float, why: str, review: dict | None, draft: dict | None) -> int:
    return _run("INSERT INTO alerts (created, inventory_id, entry_id, list_id, list_title, level, score, why,"
                " inventory_json, entry_json, review_json, draft_json, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')",
                (time.time(), inventory.id, entry_id, list_id, list_title, level, score, why,
                 inventory.model_dump_json(), entry.model_dump_json(), json.dumps(review or {}),
                 json.dumps(draft or {})), commit=True, returning_id=True)


def _alert_row(r: dict) -> dict:
    for f in ("inventory_json", "entry_json", "review_json", "draft_json"):
        try:
            r[f.removesuffix("_json")] = json.loads(r.pop(f) or "{}")
        except (json.JSONDecodeError, TypeError):
            r[f.removesuffix("_json")] = {}
    return r


def get_alerts(status: str | None = None) -> list[dict]:
    order = "CASE level WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END, created DESC"
    if status:
        rows = _run(f"SELECT * FROM alerts WHERE status = ? ORDER BY {order}", (status,), fetch="all")
    else:
        rows = _run(f"SELECT * FROM alerts ORDER BY {order}", fetch="all")
    return [_alert_row(r) for r in rows]


def decide_alert(alert_id: int, status: str, note: str = "") -> bool:
    return _run("UPDATE alerts SET status = ?, note = ?, decided = ? WHERE id = ?",
                (status, note, time.time(), alert_id), commit=True) > 0


# --------------------------------- runs ------------------------------------ #
def start_run() -> int:
    return _run("INSERT INTO runs (started, status) VALUES (?, 'running')", (time.time(),),
                commit=True, returning_id=True)


def finish_run(run_id: int, status: str, summary: str, log: list) -> None:
    _run("UPDATE runs SET finished = ?, status = ?, summary = ?, log_json = ? WHERE id = ?",
         (time.time(), status, summary, json.dumps(log[-300:]), run_id), commit=True)


def get_runs(limit: int = 20) -> list[dict]:
    rows = _run("SELECT * FROM runs ORDER BY started DESC LIMIT ?", (limit,), fetch="all")
    for r in rows:
        r["log"] = json.loads(r.pop("log_json") or "[]")
    return rows
