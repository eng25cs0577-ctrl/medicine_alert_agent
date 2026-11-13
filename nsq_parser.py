"""Read CDSCO 'Not of Standard Quality' (NSQ) / spurious-drug PDFs.

The monthly NSQ lists are text PDFs with one table (8 columns):
  S.No | Product/Drug Name | Batch No. | Manufacturing Date | Expiry Date |
  Manufactured By | NSQ Result | Reported by (State / CDSCO) Laboratory
The spurious and "revised drug alert" lists use another layout (10 columns):
  S.No. | Name of Drugs/medical device/cosmetics | Batch No. | Date of Manufacture |
  Date of Expiry | Manufactured By | Reason for failure | Drawn By | Firm's reply | Remarks
with merged cells and drug names split over several table rows.
Both are read exactly with pdfplumber - no AI needed, no tokens spent.
"""

from __future__ import annotations

import re
from pathlib import Path

from config import get_logger
from models import NSQEntry, NSQList

log = get_logger("Parser")

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_FULL_MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
                "august": 8, "september": 9, "october": 10, "november": 11, "december": 12}

# Header words -> field, so columns are found even if their order changes.
_HEADER_FIELDS = [
    ("batch", "batch"), ("manufactur", "mfg_or_maker"), ("expir", "exp_date"),
    ("result", "result"), ("reason", "result"), ("reported", "reported_by"), ("drawn", "reported_by"),
    ("reply", "remarks"), ("remark", "remarks"), ("product", "drug_name"),
    ("drug", "drug_name"), ("name", "drug_name"), ("s.no", "sno"), ("sl", "sno"),
]
# "Manufactured By" on spurious lists: the real maker isn't known.
_UNKNOWN_MAKER = re.compile(r"^(under\s*investigation|not\s*known|unknown|n\.?\s*a\.?|nm|-+)$", re.I)
_DEFAULT_ORDER = ["sno", "drug_name", "batch", "mfg_date", "exp_date", "manufacturer",
                  "result", "reported_by"]


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("“", '"').replace("”", '"')).strip()


def normalize_date(text: str) -> str:
    """'Apr-24' / '01/2025' / '12-09-2024' / 'Sept. 2024' -> 'YYYY-MM' (else cleaned text)."""
    # The PDFs sometimes space out letters ('O c t - 2 7'), so drop all spaces first.
    t = re.sub(r"\s+", "", clean(text).lower().replace(".", " "))
    m = re.match(r"^([a-z]{3,9})[\-/,']*(\d{2,4})$", t)
    if m and m.group(1)[:3] in _MONTHS:
        year = int(m.group(2))
        year = year + 2000 if year < 100 else year
        return f"{year:04d}-{_MONTHS[m.group(1)[:3]]:02d}"
    m = re.match(r"^(\d{1,2})[\-/](\d{4})$", t)                       # 01/2025
    if m and 1 <= int(m.group(1)) <= 12:
        return f"{m.group(2)}-{int(m.group(1)):02d}"
    m = re.match(r"^(\d{1,2})[\-/](\d{1,2})[\-/](\d{2,4})$", t)       # 12-09-2024 (dd-mm-yyyy)
    if m and 1 <= int(m.group(2)) <= 12:
        year = int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)
        return f"{year:04d}-{int(m.group(2)):02d}"
    return clean(text)


def normalize_batch(text: str) -> str:
    """Batch numbers are often split across lines in the PDF: 'DXT2024120\\n40'."""
    return re.sub(r"\s+", "", text or "").strip(" .,;").upper()


def detect_kind(text: str, all_spurious: bool = False) -> str:
    """A "drug alert" heading can name several categories ("Not of Standard Quality/
    Spurious/Adulterated"); such a list counts as spurious only if every row is."""
    t = clean(text).lower()
    if "spurious" in t:
        mixed = "standard quality" in t or "adulterated" in t or "misbranded" in t
        if all_spurious or not mixed:
            return "spurious"
        t = t.replace("spurious", "")
    if "state lab" in t or "state drug" in t or "(state" in t:
        return "state_nsq"
    if "cdsco" in t or "central lab" in t:
        return "central_nsq"
    return "other"


def detect_month(text: str) -> str:
    """'... FOR THE MONTH OF JUNE– 2025 ...' -> '2025-06'."""
    t = text.lower()
    m = re.search(r"month\s+of\s+([a-z]+)\W*(\d{4})", t) or \
        re.search(r"\b(" + "|".join(_FULL_MONTHS) + r")\W{0,3}(\d{4})\b", t)
    if m and m.group(1) in _FULL_MONTHS:
        return f"{m.group(2)}-{_FULL_MONTHS[m.group(1)]:02d}"
    return ""


def _column_map(header: list[str]) -> list[str]:
    """Map header cells to fields. 'Manufacturing Date' vs 'Manufactured By' are told
    apart by the word 'date'."""
    fields = []
    for cell in header:
        c = clean(cell).lower().replace(" ", "").replace("-", "")   # 'Manufa - cture'
        field = "skip"
        for key, name in _HEADER_FIELDS:
            if key.replace(" ", "") in c:
                field = name
                break
        if field == "mfg_or_maker":
            field = "mfg_date" if "date" in c else "manufacturer"
        fields.append(field)
    return fields


def _parse(rows: list[list[str | None]]) -> list[tuple[NSQEntry, bool]]:
    """Entries plus, for each, whether its row says the drug is spurious.

    - Columns come from the header row; cells under an empty (merged) header cell are
      ignored, and a table on the next page without those empty cells still lines up.
    - A row without a serial number continues the row above: long drug names run over
      several rows. Other cells are only filled if they were empty in the numbered row,
      so footnotes don't end up inside an entry.
    """
    columns, compact = _DEFAULT_ORDER, _DEFAULT_ORDER
    records: list[tuple[dict[str, list[str]], set[str]]] = []
    current = None
    for row in rows:
        cells = [c if c is not None else "" for c in row]
        joined = " ".join(clean(c) for c in cells).lower()
        numbered = bool(cells) and bool(re.match(r"^\s*\d+\s*\.?\s*$", clean(cells[0])))
        # Header row (data rows can mention "batch" and "drug" too, e.g. in the firm's reply)
        if not numbered and "batch" in joined and ("drug" in joined or "product" in joined):
            mapped = _column_map(cells)
            if "batch" in mapped and "drug_name" in mapped:
                compact = [f for f, c in zip(mapped, row) if c is not None]
                # a cell under a merged (empty) header cell spills over from the column on its left
                columns = list(mapped)
                for i in range(1, len(columns)):
                    if row[i] is None:
                        columns[i] = columns[i - 1]
            current = None
            continue
        cols = columns if len(cells) == len(columns) else compact if len(cells) == len(compact) else columns
        values: dict[str, list[str]] = {}
        for i, f in enumerate(cols):
            if i < len(cells) and f != "skip" and clean(cells[i]):
                values.setdefault(f, []).append(clean(cells[i]))
        if numbered:
            current = (values, set(values))          # fields filled by the numbered row
            records.append(current)
        elif current is not None:
            data, filled = current
            for f, parts in values.items():
                if f == "drug_name" or (f not in filled and f != "sno"):
                    data.setdefault(f, []).extend(parts)

    out = []
    for data, _ in records:
        get = lambda f: " ".join(data.get(f, []))   # noqa: E731
        name = get("drug_name")
        if not name:
            continue
        maker = get("manufacturer")
        out.append((NSQEntry(
            sno=int(re.sub(r"\D", "", get("sno")) or 0),
            drug_name=name,
            batch=normalize_batch(get("batch")),
            mfg_date=normalize_date(get("mfg_date")),
            exp_date=normalize_date(get("exp_date")),
            manufacturer="" if _UNKNOWN_MAKER.match(maker) else maker,
            result=get("result").strip('" '),
            reported_by=get("reported_by"),
        ), "spurious" in get("remarks").lower()))
    return out


def parse_rows(rows: list[list[str | None]]) -> list[NSQEntry]:
    """Turn raw table rows (header rows, data rows, footnotes) into entries."""
    return [entry for entry, _ in _parse(rows)]


def parse_pdf(path: str | Path, title: str = "", source_url: str = "", released: str = "") -> NSQList:
    import pdfplumber  # only needed here

    rows: list[list[str | None]] = []
    first_text = ""
    with pdfplumber.open(str(path)) as pdf:
        for i, page in enumerate(pdf.pages):
            if i == 0:
                first_text = page.extract_text() or ""
            for table in page.extract_tables():
                rows.extend(table)
    heading = clean(first_text.split("As part of")[0])[:200] or clean(first_text)[:200]
    parsed = _parse(rows)
    entries = [entry for entry, _ in parsed]
    all_spurious = bool(parsed) and all(sp for _, sp in parsed)
    lst = NSQList(title=title or heading or Path(path).stem,
                  kind=detect_kind(title + " " + first_text, all_spurious),
                  month=detect_month(title + " " + first_text), source_url=source_url,
                  released=released, entries=entries)
    log.info("parsed %r: %d entries (%s, %s)", lst.title[:70], len(entries), lst.kind, lst.month or "month?")
    if not entries:
        log.warning("no table rows found - the PDF may be a scanned image")
    return lst
