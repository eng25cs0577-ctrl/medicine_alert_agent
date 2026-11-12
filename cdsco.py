"""Find and download new drug-alert lists from the CDSCO website.

The Alerts page (https://cdsco.gov.in/opencms/opencms/en/Alerts/) is a table:
  S.no | Title | Release Date | Download Pdf | Pdf Size
We keep the rows that are NSQ / spurious-drug lists and download their PDFs.

Indian government sites sometimes block servers outside India. If that happens the
website's "Upload a CDSCO PDF" button still works.
"""

from __future__ import annotations

import hashlib
import html
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser

from config import get_logger, settings, with_retries

log = get_logger("CDSCO")

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


@with_retries("CDSCO download", attempts=3, logger=log)
def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read()


def find_lists() -> list[AlertLink]:
    """All NSQ / spurious lists currently listed on the CDSCO alert page(s)."""
    links: list[AlertLink] = []
    for url in filter(None, [settings.cdsco_nsq_url, settings.cdsco_alerts_url]):
        page = _get(url).decode("utf-8", "replace")
        found = parse_alerts_page(page, url)
        log.info("%s: %d drug-alert lists listed", url, len(found))
        links.extend(found)
    seen, unique = set(), []
    for link in links:
        if link.url not in seen:
            seen.add(link.url)
            unique.append(link)
    return unique


def download_pdf(link: AlertLink) -> bytes:
    data = _get(link.url)
    if not data.startswith(b"%PDF"):
        raise ValueError(f"not a PDF: {link.url}")
    return data


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
