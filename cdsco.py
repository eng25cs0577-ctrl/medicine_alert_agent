"""Find and download new drug-alert lists from the CDSCO website.

The Alerts page (https://cdsco.gov.in/opencms/opencms/en/Alerts/) is a table:
  S.no | Title | Release Date | Download Pdf | Pdf Size
We keep the rows that are NSQ / spurious-drug lists and download their PDFs.

Indian government sites often block servers outside India (Render's servers are in
Singapore/US). Then use the laptop sync helper (sync.py), which downloads the lists
on a computer in India and uploads them, or the website's "Upload a CDSCO PDF" button.
"""

from __future__ import annotations

import hashlib
import urllib.request

from cdsco_page import USER_AGENT, AlertLink, parse_alerts_page  # noqa: F401 (re-exported)
from config import get_logger, settings, with_retries

log = get_logger("CDSCO")


def _get(url: str, timeout: float = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


@with_retries("CDSCO download", attempts=2, logger=log)
def _get_pdf(url: str) -> bytes:
    return _get(url, timeout=45)


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
    data = _get_pdf(link.url)
    if not data.startswith(b"%PDF"):
        raise ValueError(f"not a PDF: {link.url}")
    return data


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
