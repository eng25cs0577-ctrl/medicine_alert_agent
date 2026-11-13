"""Read the CDSCO Alerts page (standard library only).

Shared by the website (cdsco.py) and the laptop sync helper (sync.py), which must
run with a plain Python install - so nothing outside the standard library here.
"""

from __future__ import annotations

import html
import re
import urllib.parse
from dataclasses import dataclass
from html.parser import HTMLParser

USER_AGENT = ("Mozilla/5.0 (compatible; MedAlert-student-project/1.0; "
              "+https://github.com/eng25cs0577-ctrl/medicine_alert_agent)")
_LIST_RE = re.compile(r"\b(nsq|not\s+of\s+standard\s+quality|spurious|drug\s+alert)\b", re.I)
_NOT_A_LIST_RE = re.compile(r"\b(availability|new\s+link|corrigendum|theft|circular)\b", re.I)


_MOJIBAKE_RE = re.compile(r"[\u00c2-\u00f4][\u0080-\u00bf\u0152-\u2122]{1,3}")


def _cp1252_bytes(text: str) -> bytes:
    out = bytearray()
    for ch in text:
        try:
            out += ch.encode("cp1252")
        except UnicodeEncodeError:
            if ord(ch) > 255:
                raise
            out.append(ord(ch))                      # C1 control chars cp1252 has no glyph for
    return bytes(out)


def fix_text(text: str) -> str:
    """Repair text that was UTF-8 but got read as Windows-1252, as on some CDSCO titles:
    'January\u00e2\u0080\u0093 2025' -> 'January\u2013 2025'. Clean text is left alone."""
    def repair(m: re.Match) -> str:
        try:
            return _cp1252_bytes(m.group(0)).decode("utf-8")
        except (UnicodeDecodeError, UnicodeEncodeError):
            return m.group(0)
    return _MOJIBAKE_RE.sub(repair, text or "")


@dataclass
class AlertLink:
    title: str
    released: str
    url: str


class _TableParser(HTMLParser):
    """Collect table rows as (cells text, links)."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[tuple[list[str], list[str]]] = []
        self._cells: list[str] | None = None
        self._links: list[str] = []
        self._text: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self._cells, self._links = [], []
        elif tag in ("td", "th") and self._cells is not None:
            self._text = []
        elif tag == "a" and self._cells is not None and a.get("href"):
            self._links.append(a["href"])

    def handle_data(self, data):
        if self._text is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cells is not None and self._text is not None:
            self._cells.append(re.sub(r"\s+", " ", html.unescape("".join(self._text))).strip())
            self._text = None
        elif tag == "tr" and self._cells is not None:
            if self._cells:
                self.rows.append((self._cells, self._links))
            self._cells = None


def clean_url(url: str) -> str:
    """CDSCO's HTML wraps long links over several lines, so an href can contain line
    breaks or tabs; Python refuses such addresses. Drop control characters and encode
    any remaining spaces."""
    url = re.sub(r"[\x00-\x1f\x7f]", "", html.unescape(url or "")).strip()
    return url.replace(" ", "%20")


def parse_alerts_page(page_html: str, base_url: str) -> list[AlertLink]:
    parser = _TableParser()
    parser.feed(page_html)
    out = []
    for cells, links in parser.rows:
        if len(cells) < 3 or not links:
            continue
        title = fix_text(max(cells, key=len))
        if not _LIST_RE.search(title) or _NOT_A_LIST_RE.search(title):
            continue
        released = next((c for c in cells if re.match(r"^\d{4}-[A-Za-z]{3}-\d{1,2}$", c)), "")
        links = [clean_url(l) for l in links if clean_url(l) and not clean_url(l).lower().startswith("javascript:")]
        if not links:
            continue
        pdf = next((l for l in links if "download" in l.lower() or l.lower().endswith(".pdf")), links[0])
        out.append(AlertLink(title=title, released=released, url=urllib.parse.urljoin(base_url, pdf)))
    return out
