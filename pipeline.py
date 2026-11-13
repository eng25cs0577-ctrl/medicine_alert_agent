"""LangGraph = the orchestrator of a medicine check.

  START -> fetch_lists -> match --(no matches)--------------------------> save -> END
                               \\--(uncertain ones)--> review -> draft -> save
                                \\--(only certain ones)-------> draft -/

  fetch_lists  find + download new CDSCO lists, read them (code), store them
  match        compare the whole inventory with every stored list (code)
  review       AutoGen Matcher-vs-Sceptic debate on uncertain matches
  draft        CrewAI Risk Assessor + Notice Writer draft each alert
  save         store new alerts for the pharmacist to confirm or dismiss
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, TypedDict

from langgraph.graph import END, START, StateGraph

import storage
from config import get_logger, settings
from matching import compare
from models import InventoryItem

log = get_logger("LangGraph")
MAX_NEW_LISTS = 6           # per run, so the first run doesn't download years of lists
MAX_CANDIDATES = 5          # per inventory item in one AutoGen review


class CheckState(TypedDict, total=False):
    source: str             # "cdsco" (download new lists) or "none" (use stored lists only)
    cdsco_blocked: bool
    notes: list[str]
    imported: list[dict]
    matches: list[dict]
    saved: list[int]


def _note(state: CheckState, text: str) -> list[str]:
    log.info(text)
    return [*state.get("notes", []), text]


def import_pdf_bytes(data: bytes, title: str = "", source_url: str = "", released: str = "") -> dict:
    """Read a PDF and store it as a list (skipped if the same file was imported before)."""
    import tempfile

    from cdsco import file_hash
    from cdsco_page import fix_text
    from nsq_parser import parse_pdf

    title = fix_text(title)
    digest = file_hash(data)
    if storage.list_exists(digest):
        if source_url:
            storage.set_source_if_missing(digest, source_url, released)
        return {"status": "duplicate", "title": title}
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(data)
        f.flush()
        try:
            lst = parse_pdf(f.name, title=title, source_url=source_url, released=released)
        except Exception as exc:  # noqa: BLE001 - a damaged PDF is "unreadable", not a server error
            log.warning("could not read %r: %s", title[:60], exc)
            return {"status": "empty", "title": title}
    if not lst.entries:
        return {"status": "empty", "title": lst.title}
    list_id = storage.save_list(lst, digest)
    return {"status": "imported", "id": list_id, "title": lst.title, "kind": lst.kind,
            "month": lst.month, "entries": len(lst.entries)}


# ------------------------------- nodes ------------------------------------- #
def fetch_lists(state: CheckState) -> dict:
    if state.get("source") != "cdsco":
        return {"imported": [], "notes": _note(state, "fetch_lists: using stored lists only")}
    from cdsco import download_pdf, find_lists

    notes, imported = state.get("notes", []), []
    try:
        links = find_lists()
    except Exception as exc:  # noqa: BLE001 - e.g. the site blocks servers outside India
        notes = _note({"notes": notes}, f"fetch_lists: could not reach CDSCO ({str(exc)[:120]}). "
                      "Use the laptop sync helper (sync.py) or upload the PDF on the Lists page.")
        return {"imported": [], "notes": notes, "cdsco_blocked": True}
    for link in links[:MAX_NEW_LISTS * 3]:
        if len(imported) >= MAX_NEW_LISTS:
            break
        try:
            result = import_pdf_bytes(download_pdf(link), link.title, link.url, link.released)
        except Exception as exc:  # noqa: BLE001
            notes = _note({"notes": notes}, f"fetch_lists: skipped {link.title[:60]!r}: {str(exc)[:100]}")
            continue
        if result["status"] == "imported":
            imported.append(result)
            notes = _note({"notes": notes}, f"fetch_lists: imported {result['title'][:60]!r} "
                          f"({result['entries']} entries)")
    if not imported:
        notes = _note({"notes": notes}, "fetch_lists: no new lists on CDSCO")
    return {"imported": imported, "notes": notes}


def match(state: CheckState) -> dict:
    inventory = storage.get_inventory()
    rows = storage.get_entries()
    found = []
    for item in inventory:
        for row in rows:
            if storage_alert_known(item, row):
                continue
            entry = storage.entry_model(row)
            level, score, why = compare(item, entry)
            if level:
                found.append({"inventory": item.model_dump(), "entry": entry.model_dump(),
                              "entry_id": row["id"], "list_id": row["list_id"],
                              "list_title": row["list_title"], "spurious": row["list_kind"] == "spurious",
                              "level": level, "score": score, "why": why})
    counts = {lvl: sum(1 for m in found if m["level"] == lvl) for lvl in ("HIGH", "MEDIUM", "LOW")}
    return {"matches": found, "notes": _note(
        state, f"match: {len(inventory)} medicines x {len(rows)} listed entries -> "
               f"{counts['HIGH']} certain, {counts['MEDIUM']} same product/other batch, "
               f"{counts['LOW']} uncertain (new)")}


_known: set[tuple[int, int]] = set()
_known_lock = threading.Lock()


def storage_alert_known(item: InventoryItem, row: dict) -> bool:
    """Skip pairs that already have an alert (any status) - checked once per run."""
    with _known_lock:
        return (item.id, row["id"]) in _known


def review(state: CheckState) -> dict:
    from review_team import review_candidates

    matches = state["matches"]
    by_item: dict[int, list[dict]] = {}
    for m in matches:
        if m["level"] == "LOW":
            by_item.setdefault(m["inventory"]["id"], []).append(m)
    keep_ids, budget = set(), settings.max_reviews_per_run
    notes = state.get("notes", [])
    for item_id, group in by_item.items():
        group.sort(key=lambda m: -m["score"])
        top = group[:MAX_CANDIDATES]
        if budget <= 0:
            for m in group:
                m["review"] = {"decision": "UNSURE", "reasons": ["Not reviewed (AI budget for this run used)."]}
                keep_ids.add(id(m))
            continue
        budget -= 1
        item = InventoryItem(**top[0]["inventory"])
        from models import NSQEntry
        result = review_candidates(item, [NSQEntry(**m["entry"]) for m in top])
        for i, m in enumerate(top, 1):
            m["review"] = {"decision": result.decision if i in result.matched else "NOT_MATCH",
                           "reasons": result.reasons, "transcript": result.transcript}
            if i in result.matched:
                keep_ids.add(id(m))
        for m in group[MAX_CANDIDATES:]:
            m["review"] = {"decision": "UNSURE", "reasons": [
                "Not reviewed: too many candidates. Add the manufacturer and batch of this medicine "
                "to your inventory to get precise matches."]}
            keep_ids.add(id(m))
        notes = _note({"notes": notes}, f"review: {item.name!r} -> {result.decision} "
                      f"(kept {len(result.matched)} of {len(top)})")
    kept = [m for m in matches if m["level"] != "LOW" or id(m) in keep_ids]
    return {"matches": kept, "notes": notes}


def draft(state: CheckState) -> dict:
    from alert_crew import draft_alert
    from models import NSQEntry

    budget = settings.max_reviews_per_run
    for m in state["matches"]:           # HIGH first (list is sorted), so they get the AI first
        use_ai = budget > 0
        budget -= 1 if use_ai else 0
        reasons = (m.get("review") or {}).get("reasons")
        d = draft_alert(InventoryItem(**m["inventory"]), NSQEntry(**m["entry"]), m["level"],
                        spurious=m["spurious"], review_reasons=reasons, use_ai=use_ai)
        m["draft"] = d.model_dump()
    return {"matches": state["matches"], "notes": _note(state, f"draft: {len(state['matches'])} alert(s) written")}


def save(state: CheckState) -> dict:
    saved = []
    for m in state.get("matches", []):
        item = InventoryItem(**m["inventory"])
        if storage.alert_exists(item.id, m["entry_id"]):
            continue
        from models import NSQEntry
        saved.append(storage.save_alert(
            inventory=item, entry_id=m["entry_id"], entry=NSQEntry(**m["entry"]), list_id=m["list_id"],
            list_title=m["list_title"], level=m["level"], score=m["score"], why=m["why"],
            review=m.get("review"), draft=m.get("draft")))
    return {"saved": saved, "notes": _note(state, f"save: {len(saved)} new alert(s) for the pharmacist")}


def _after_match(state: CheckState) -> str:
    matches = state.get("matches", [])
    if not matches:
        return "save"
    return "review" if any(m["level"] == "LOW" for m in matches) else "draft"


def build_graph():
    g = StateGraph(CheckState)
    for name, fn in [("fetch_lists", fetch_lists), ("match", match), ("review", review),
                     ("draft", draft), ("save", save)]:
        g.add_node(name, fn)
    g.add_edge(START, "fetch_lists")
    g.add_edge("fetch_lists", "match")
    g.add_conditional_edges("match", _after_match, {"review": "review", "draft": "draft", "save": "save"})
    g.add_edge("review", "draft")
    g.add_edge("draft", "save")
    g.add_edge("save", END)
    return g.compile()


# ------------------------------ run a check -------------------------------- #
class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append({"t": time.strftime("%H:%M:%S", time.localtime(record.created)),
                           "tag": record.name.split(".", 1)[-1], "level": record.levelname,
                           "msg": record.getMessage()})


_run_lock = threading.Lock()


def run_check(source: str = "cdsco", on_line: Callable[[dict], None] | None = None) -> dict:
    """Run one full check. Only one check runs at a time."""
    if not _run_lock.acquire(blocking=False):
        raise RuntimeError("A check is already running.")
    capture = _Capture()
    root = logging.getLogger("med")
    root.addHandler(capture)
    run_id = storage.start_run()
    try:
        with _known_lock:
            _known.clear()
            _known.update((a["inventory_id"], a["entry_id"]) for a in storage.get_alerts())
        final = build_graph().invoke({"source": source, "notes": []})
        summary = (f"{len(final.get('imported', []))} new list(s), "
                   f"{len(final.get('saved', []))} new alert(s)")
        if final.get("cdsco_blocked"):
            summary += " - CDSCO can't be reached from this server: use the laptop sync or upload the PDF"
        storage.finish_run(run_id, "done", summary, capture.lines)
        return {"run_id": run_id, "status": "done", "summary": summary,
                "imported": final.get("imported", []), "new_alerts": final.get("saved", [])}
    except Exception as exc:  # noqa: BLE001
        from config import friendly_error
        log.error("check failed: %s", exc)
        storage.finish_run(run_id, "error", friendly_error(exc), capture.lines)
        return {"run_id": run_id, "status": "error", "summary": friendly_error(exc)}
    finally:
        root.removeHandler(capture)
        _run_lock.release()
