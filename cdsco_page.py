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


def parse_alerts_page(page_html: str, base_url: str) -> list[AlertLink]:
    parser = _TableParser()
    parser.feed(page_html)
    out = []
    for cells, links in parser.rows:
        if len(cells) < 3 or not links:
            continue
        title = max(cells, key=len)
        if not _LIST_RE.search(title) or _NOT_A_LIST_RE.search(title):
            continue
        released = next((c for c in cells if re.match(r"^\d{4}-[A-Za-z]{3}-\d{1,2}$", c)), "")
        pdf = next((l for l in links if "download" in l.lower() or l.lower().endswith(".pdf")), links[0])
        out.append(AlertLink(title=title, released=released, url=urllib.parse.urljoin(base_url, pdf)))
    return out
