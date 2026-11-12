"""Match a medicine inventory against NSQ / spurious-drug lists (pure code, no AI).

Levels
  HIGH   - same batch number AND the same drug or maker. Almost certainly affected.
  MEDIUM - same drug (incl. strength) and same maker, but a different/unknown batch.
           The pharmacist should check the batch printed on the pack.
  LOW    - same drug name, maker unknown or unclear. Sent to the AutoGen review.
A known, clearly different maker or a different strength is NOT a match.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from models import InventoryItem, Match, NSQEntry

_PHARMACOPOEIA = {"ip", "i.p", "i.p.", "usp", "bp", "b.p", "ph", "eur", "nf", "vet"}
_FILLER = {"of", "and", "with", "for", "the", "&", "+", "only", "use", "im", "iv", "sr", "er", "ec"}
_FORMS = {"tablet", "tablets", "tab", "tabs", "capsule", "capsules", "cap", "caps", "injection", "inj",
          "syrup", "suspension", "oral", "solution", "cream", "ointment", "gel", "drops", "drop",
          "infusion", "powder", "sachet", "lotion", "spray", "shampoo", "film", "coated", "dispersible",
          "gastro", "resistant", "prolonged", "release", "extended", "sustained", "modified"}
_COMPANY = {"m/s", "ms", "mfg", "by", "pvt", "private", "ltd", "limited", "llp", "inc", "co", "company",
            "corporation", "corp", "india", "the", "and", "&", "unit", "a"}
# Words most drug makers share - ignored when comparing makers, or every
# "X Pharmaceuticals" would look like every "Y Pharmaceuticals".
_INDUSTRY = {"pharma", "pharmaceutical", "pharmaceuticals", "pharmaceutics", "laboratories", "laboratory",
             "labs", "lab", "healthcare", "health", "care", "industries", "industrial", "industry",
             "lifesciences", "life", "sciences", "science", "biotech", "bio", "formulation",
             "formulations", "remedies", "drugs", "drug", "medicines", "medicare", "medical", "meditek",
             "generics", "chemicals", "chemical", "biocare", "therapeutics", "injectables", "products"}
_STRENGTH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(mg|mcg|µg|g|gm|ml|iu|%)\b", re.I)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", text.lower())


def strengths(text: str) -> set[str]:
    """'Paracetamol 500 mg' -> {'500mg'}; used to tell 500 mg from 650 mg."""
    out = set()
    for num, unit in _STRENGTH_RE.findall(text):
        unit = {"gm": "g", "µg": "mcg"}.get(unit.lower(), unit.lower())
        out.add(f"{float(num):g}{unit}")
    return out


def name_variants(name: str) -> list[str]:
    """Generic part and each brand in brackets, as clean word strings.
    'Methotrexate Injection IP 15 mg/ml (Folitrax-15)' -> ['methotrexate 15', 'folitrax 15']"""
    brands = re.findall(r"\(([^)]*)\)", name)
    generic = re.sub(r"\([^)]*\)", " ", name)
    out = []
    for part in [generic, *brands]:
        words = [w for w in _tokens(part)
                 if w not in _PHARMACOPOEIA and w not in _FILLER and w not in _FORMS
                 and not re.fullmatch(r"(mg|mcg|ml|w|v|iu|g|gm)", w)]
        if words:
            out.append(" ".join(words))
    return out or [name.lower().strip()]


def name_similarity(a: str, b: str) -> float:
    """Best similarity between any variant of a and any variant of b (0..1)."""
    best = 0.0
    for x in name_variants(a):
        for y in name_variants(b):
            xs, ys = set(x.split()), set(y.split())
            overlap = len(xs & ys) / max(1, min(len(xs), len(ys)))
            seq = SequenceMatcher(None, x, y).ratio()
            first = 1.0 if x.split()[0] == y.split()[0] else 0.0   # main ingredient / brand word
            best = max(best, 0.45 * overlap + 0.35 * seq + 0.20 * first)
    return round(best, 3)


def maker_core(maker: str) -> str:
    """'M/s. Karnataka Antibiotics & Pharmaceuticals Ltd., Plot No.14 ...' -> 'karnataka antibiotics pharmaceuticals'"""
    text = re.sub(r"\(.*?\)", " ", maker or "")
    text = re.sub(r"^\s*(mfg\.?\s*by[-:\s]*)", "", text, flags=re.I)
    first = text.split(",")[0]
    words = [w for w in _tokens(first) if w not in _COMPANY]
    return " ".join(words)


def maker_similarity(a: str, b: str) -> float:
    """Compare only the distinctive words ('karnataka antibiotics', 'tam bran')."""
    def distinctive(m: str) -> str:
        core = maker_core(m)
        words = [w for w in core.split() if w not in _INDUSTRY]
        return " ".join(words) or core
    x, y = distinctive(a), distinctive(b)
    if not x or not y:
        return 0.0
    xs, ys = set(x.split()), set(y.split())
    overlap = len(xs & ys) / max(1, min(len(xs), len(ys)))
    return round(max(overlap, SequenceMatcher(None, x, y).ratio() if overlap else 0.0), 3)


def batch_variants(batch: str) -> set[str]:
    """'B.NO:IEW-1487C,IEW-1487B' -> {'IEW1487C', 'IEW1487B'} (letters and digits only)."""
    text = re.sub(r"^\s*(b\.?\s*no\.?|batch\s*(no\.?)?)\s*[:\-]?", "", batch or "", flags=re.I)
    parts = re.split(r"[,;/&]|\band\b", text, flags=re.I)
    return {re.sub(r"[^A-Z0-9]", "", p.upper()) for p in parts if re.sub(r"[^A-Z0-9]", "", p.upper())}


def compare(item: InventoryItem, entry: NSQEntry) -> tuple[str | None, float, str]:
    """Return (level or None, score, explanation)."""
    n_sim = name_similarity(item.name, entry.drug_name)
    m_sim = maker_similarity(item.manufacturer, entry.manufacturer) if item.manufacturer else None
    same_batch = bool(item.batch) and bool(batch_variants(item.batch) & batch_variants(entry.batch))
    s_item, s_entry = strengths(item.name), strengths(entry.drug_name)
    strength_clash = bool(s_item and s_entry and not (s_item & s_entry))

    why = [f"name {n_sim:.0%} similar"]
    if m_sim is not None:
        why.append(f"maker {m_sim:.0%} similar")
    if same_batch:
        why.append(f"same batch {entry.batch}")
    if strength_clash:
        why.append(f"different strength ({', '.join(sorted(s_item))} vs {', '.join(sorted(s_entry))})")
    explanation = "; ".join(why)

    if same_batch and (n_sim >= 0.5 or (m_sim or 0) >= 0.6):
        return "HIGH", round(0.6 + 0.4 * max(n_sim, m_sim or 0), 3), explanation
    if n_sim < 0.72 or strength_clash:
        return None, n_sim, explanation
    if m_sim is not None and m_sim >= 0.6:
        return "MEDIUM", round(0.5 * n_sim + 0.5 * m_sim, 3), explanation
    if m_sim is not None and m_sim < 0.35:
        return None, n_sim, explanation          # clearly a different maker: not affected
    return "LOW", round(0.7 * n_sim + 0.3 * (m_sim or 0), 3), explanation


def find_matches(inventory: list[InventoryItem], entries: list[NSQEntry],
                 list_title: str = "") -> list[Match]:
    matches = []
    for item in inventory:
        for entry in entries:
            level, score, why = compare(item, entry)
            if level:
                matches.append(Match(inventory=item, entry=entry, list_title=list_title,
                                     level=level, score=score, why=why))
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    return sorted(matches, key=lambda m: (order[m.level], -m.score))


def search_entries(entries: list[NSQEntry], name: str = "", batch: str = "") -> list[tuple[NSQEntry, float]]:
    """Public 'check a medicine' search: by batch (exact) and/or name (similar)."""
    wanted = batch_variants(batch) if batch.strip() else set()
    out = []
    for e in entries:
        if wanted and wanted & batch_variants(e.batch):
            out.append((e, 1.0))
        elif name.strip() and not wanted:
            sim = name_similarity(name, e.drug_name)
            if sim >= 0.65:
                out.append((e, sim))
    return sorted(out, key=lambda x: -x[1])
