"""Data contracts shared by the parser, matcher, agents, storage and website.

The frameworks only exchange these plain Pydantic models (or their dicts), so no
CrewAI / AutoGen / LangGraph object leaks into another part of the system.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ListKind = Literal["central_nsq", "state_nsq", "spurious", "other"]
MatchLevel = Literal["HIGH", "MEDIUM", "LOW"]
AlertStatus = Literal["new", "confirmed", "dismissed"]
Severity = Literal["CRITICAL", "HIGH", "MODERATE", "LOW"]


class NSQEntry(BaseModel):
    """One row of a CDSCO 'Not of Standard Quality' / spurious-drug list."""
    sno: int = 0
    drug_name: str
    batch: str = ""
    mfg_date: str = ""          # normalised to YYYY-MM when possible
    exp_date: str = ""
    manufacturer: str = ""
    result: str = ""            # which test it failed ("Sterility", "Assay of ...")
    reported_by: str = ""       # lab / state that tested it


class NSQList(BaseModel):
    title: str
    kind: ListKind = "other"
    month: str = ""             # e.g. "2025-06"
    source_url: str = ""
    released: str = ""
    entries: list[NSQEntry] = Field(default_factory=list)


class InventoryItem(BaseModel):
    id: int | None = None
    name: str = Field(min_length=2, max_length=200)
    manufacturer: str = ""
    batch: str = ""
    expiry: str = ""
    quantity: int = 0
    location: str = ""          # shelf / ward / store


class Match(BaseModel):
    inventory: InventoryItem
    entry: NSQEntry
    list_title: str = ""
    level: MatchLevel
    score: float = 0.0
    why: str = ""               # what matched, in plain words


class Review(BaseModel):
    """Verdict of the AutoGen Matcher-vs-Sceptic debate on an uncertain match."""
    decision: Literal["MATCH", "NOT_MATCH", "UNSURE"]
    matched: list[int] = Field(default_factory=list)   # which candidates (1-based) match
    reasons: list[str] = Field(default_factory=list)
    transcript: str = ""


class AlertDraft(BaseModel):
    """Written by the CrewAI crew for the pharmacist to review."""
    severity: Severity = "HIGH"
    summary: str = ""           # 1-2 sentences: what is wrong
    actions: list[str] = Field(default_factory=list)   # recommended steps
    supplier_note: str = ""     # ready-to-send message to the supplier
