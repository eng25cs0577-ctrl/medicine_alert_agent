"""MedAlert laptop sync: download new CDSCO lists on a computer in India, upload them
to your MedAlert website, then start one check of your stock.

Why: the CDSCO website often blocks servers outside India (like Render's), but it
opens fine from a laptop in India.

Needs only a normal Python 3.8+ install (standard library only - no pip install).

    python sync.py                 # first run asks for your site address and check token
    python sync.py --dry-run       # show what would be uploaded, upload nothing
    python sync.py --max 3         # upload at most 3 new lists this time

Settings are read from (first found): command-line options, environment variables
SITE_URL / CHECK_TOKEN, or the file sync.env next to this script.
"""

from __future__ import annotations

import argparse
import html as htmllib
from http.cookiejar import CookieJar
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from cdsco_page import USER_AGENT, AlertLink, clean_url, parse_alerts_page

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / "sync.env"
LOG_FILE = HERE / "sync.log"
STATE_FILE = HERE / "sync_state.json"     # CDSCO links already handled (duplicate / unreadable)
DEFAULT_PAGES = ["https://cdsco.gov.in/opencms/opencms/en/Alerts/"]


# ------------------------------- output ------------------------------------ #
def say(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ------------------------- lists already handled --------------------------- #
def load_state() -> dict[str, str]:
    """{CDSCO link: "duplicate" | "unreadable"}, so later runs move on to other lists."""
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.get("handled", {}).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def save_state(handled: dict[str, str]) -> None:
    try:
        STATE_FILE.write_text(json.dumps({"handled": handled}, indent=1), encoding="utf-8")
    except OSError as exc:
        say(f"(could not save {STATE_FILE.name}: {exc})")


# ------------------------------ settings ----------------------------------- #
def read_env_file(path: Path) -> dict:
    values = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.split(" #")[0].strip().strip('"').strip("'")
    return values


def load_settings(args) -> dict:
    file_values = read_env_file(ENV_FILE)
    site = args.site or os.getenv("SITE_URL") or file_values.get("SITE_URL", "")
    token = args.token or os.getenv("CHECK_TOKEN") or file_values.get("CHECK_TOKEN", "")
    pages = args.page or [p.strip() for p in
                          (os.getenv("CDSCO_PAGES") or file_values.get("CDSCO_PAGES", "")).split(",") if p.strip()]
    if (not site or not token) and sys.stdin.isatty() and not args.quiet:
        print("First-time setup (saved in sync.env next to this script).")
        site = site or input("Your MedAlert website address (e.g. https://medicine-alert-agent.onrender.com): ").strip()
        token = token or input("Your CHECK_TOKEN (the same as in Render's Environment): ").strip()
        if site and token:
            ENV_FILE.write_text(f"SITE_URL={site}\nCHECK_TOKEN={token}\n", encoding="utf-8")
            say(f"saved settings to {ENV_FILE.name}")
    if not site or not token:
        raise SystemExit("Missing SITE_URL or CHECK_TOKEN. Run once by double-clicking sync_cdsco.bat, "
                         "or create sync.env from sync.env.example.")
    return {"site": site.rstrip("/"), "token": token, "pages": pages or DEFAULT_PAGES}


# -------------------------------- HTTP ------------------------------------- #
def http(url: str, *, data: bytes | None = None, headers: dict | None = None, method: str | None = None,
         timeout: float = 60, context: ssl.SSLContext | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def site_call(cfg: dict, path: str, *, data: bytes | None = None, content_type: str = "",
              method: str = "GET", timeout: float = 120) -> tuple[int, dict | list]:
    headers = {"X-Check-Token": cfg["token"]}
    if content_type:
        headers["Content-Type"] = content_type
    code, body = http(cfg["site"] + path, data=data, headers=headers, method=method, timeout=timeout)
    try:
        return code, json.loads(body or b"null")
    except json.JSONDecodeError:
        return code, {"detail": body[:200].decode("utf-8", "replace")}


def wake_site(cfg: dict) -> None:
    """Free hosts sleep; the first request can take ~1 minute."""
    for attempt in range(1, 7):
        try:
            code, _ = http(cfg["site"] + "/healthz", timeout=60)
            if code == 200:
                return
        except (urllib.error.URLError, OSError) as exc:
            say(f"  website not answering yet ({exc.__class__.__name__}), attempt {attempt}/6")
        time.sleep(15)
    raise SystemExit(f"Your website {cfg['site']} did not answer. Check the address and that Render shows Live.")


class CDSCOClient:
    """Talks to CDSCO the way a browser does: keeps the session cookie from the Alerts
    page, sends a Referer, and follows a redirect page that wraps the PDF."""

    def __init__(self, insecure: bool = False) -> None:
        handlers: list = [urllib.request.HTTPCookieProcessor(CookieJar())]
        if insecure:   # only for public CDSCO files, and only when you pass --insecure-cdsco
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self.opener = urllib.request.build_opener(*handlers)
        self.referer = ""

    def get(self, url: str, accept: str = "*/*") -> tuple[bytes, str]:
        url = clean_url(url)
        headers = {"User-Agent": USER_AGENT, "Accept": accept, "Accept-Language": "en-IN,en;q=0.9"}
        if self.referer:
            headers["Referer"] = self.referer
        try:
            with self.opener.open(urllib.request.Request(url, headers=headers), timeout=90) as r:
                return r.read(), r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            raise OSError(f"HTTP {e.code}") from e

    def page(self, url: str) -> str:
        body, _ = self.get(url, "text/html,application/xhtml+xml,*/*;q=0.8")
        self.referer = url
        return body.decode("utf-8", "replace")

    def pdf(self, url: str) -> tuple[bytes | None, str, bytes]:
        """Return (pdf or None, content type, what was received)."""
        body, ctype = self.get(url, "application/pdf,*/*;q=0.8")
        for _ in range(2):                       # up to two wrapper pages
            if body.lstrip()[:5] == b"%PDF-" or body.startswith(b"%PDF"):
                return body, ctype, body
            nxt = inner_pdf_link(body.decode("utf-8", "replace"), url)
            if not nxt:
                break
            url = nxt
            body, ctype = self.get(url, "application/pdf,*/*;q=0.8")
        return None, ctype, body


def inner_pdf_link(page: str, base: str) -> str | None:
    """A page that wraps the file: meta refresh, iframe/embed/object, or a .pdf link."""
    patterns = [r'http-equiv=["\']?refresh["\']?[^>]*url=([^"\'>\s]+)',
                r'<(?:iframe|embed|frame)[^>]+src=["\']([^"\']+)',
                r'<object[^>]+data=["\']([^"\']+)',
                r'(?:window\.)?location(?:\.href)?\s*=\s*["\']([^"\']+)',
                r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']']
    for pat in patterns:
        m = re.search(pat, page, re.I)
        if m:
            return urllib.parse.urljoin(base, clean_url(htmllib.unescape(m.group(1))))
    return None


def save_debug(n: int, body: bytes, ctype: str) -> str:
    path = HERE / f"sync_debug_{n}.html"
    try:
        path.write_bytes(body[:200_000])
    except OSError:
        return ""
    return path.name


def describe(body: bytes, ctype: str) -> str:
    text = body[:4000].decode("utf-8", "replace")
    title = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()[:120]
    return (f"type '{ctype or '?'}', {len(body)} bytes"
            + (f", page title '{title.group(1).strip()[:80]}'" if title else "")
            + (f", text: {snippet!r}" if snippet else ""))


# --------------------------------- main ------------------------------------ #
def find_new_lists(cfg: dict, known: set[str], client: CDSCOClient) -> list[AlertLink]:
    links: list[AlertLink] = []
    for page in cfg["pages"]:
        try:
            html_text = client.page(page)
        except ssl.SSLError as exc:
            raise SystemExit(f"Secure connection to CDSCO failed ({exc}). If cdsco.gov.in opens in your "
                             "browser, run again with --insecure-cdsco (only CDSCO downloads skip the check).")
        except (urllib.error.URLError, OSError) as exc:
            raise SystemExit(f"Could not open {page} ({exc}). Is this computer online, and does the page "
                             "open in your browser?")
        found = parse_alerts_page(html_text, page)
        say(f"CDSCO page {page}: {len(found)} drug-alert lists")
        links.extend(found)
    seen, new = set(known), []
    for link in links:
        if link.url not in seen:
            seen.add(link.url)
            new.append(link)
    return new


def start_check(cfg: dict) -> None:
    for attempt in range(1, 13):                      # wait up to ~6 minutes if one is running
        code, body = site_call(cfg, "/api/check?source=none", method="POST")
        if code == 200:
            say("Started a check of your stock - see the Alerts page in a few minutes.")
            return
        if code == 409:
            if attempt == 1:
                say("A check is already running on the website; waiting for it to finish...")
            time.sleep(30)
            continue
        raise SystemExit(f"Could not start the check (HTTP {code}): {body}")
    say("A check was still running after 6 minutes; the uploaded lists will be checked next time.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Download new CDSCO lists here and upload them to MedAlert.")
    p.add_argument("--site", help="your MedAlert address, e.g. https://medicine-alert-agent.onrender.com")
    p.add_argument("--token", help="your CHECK_TOKEN")
    p.add_argument("--page", action="append", help="CDSCO page to read (can repeat); default: the Alerts page")
    p.add_argument("--max", type=int, default=6, help="upload at most this many new lists per run (default 6)")
    p.add_argument("--retry-skipped", action="store_true",
                   help="try again lists that earlier runs found unreadable (e.g. after a website update)")
    p.add_argument("--dry-run", action="store_true", help="only show what would be uploaded")
    p.add_argument("--quiet", action="store_true", help="never ask questions (for Task Scheduler)")
    p.add_argument("--insecure-cdsco", action="store_true",
                   help="skip HTTPS certificate checks for CDSCO downloads only (if CDSCO's certificate fails)")
    args = p.parse_args(argv)

    cfg = load_settings(args)
    say(f"MedAlert sync -> {cfg['site']}")
    wake_site(cfg)
    code, known = site_call(cfg, "/api/lists/sources")
    if code == 401:
        raise SystemExit("The website rejected the CHECK_TOKEN. Copy it again from Render -> Environment "
                         "into sync.env.")
    if code != 200:
        raise SystemExit(f"Website error (HTTP {code}): {known}")
    say(f"website already has {len(known)} CDSCO list(s)")

    handled = load_state()
    skip = {url for url, why in handled.items() if not (args.retry_skipped and why == "unreadable")}
    if skip:
        say(f"skipping {len(skip)} list(s) earlier runs found duplicate/unreadable "
            f"(see {STATE_FILE.name}; --retry-skipped tries unreadable ones again)")
    client = CDSCOClient(args.insecure_cdsco)
    new = find_new_lists(cfg, set(known) | skip, client)
    if not new:
        say("No new lists on CDSCO. Nothing to do.")
        return 0
    todo = new[: max(1, args.max)]
    say(f"{len(new)} new list(s) on CDSCO; handling {len(todo)} now (newest first)")
    uploaded = 0
    for link in todo:
        say(f"- {link.title} ({link.released or 'no date'})")
        if args.dry_run:
            continue
        try:
            pdf, ctype, received = client.pdf(link.url)
        except Exception as exc:  # noqa: BLE001 - one bad link must not stop the other lists
            say(f"  download failed: {exc.__class__.__name__}: {str(exc)[:200]}")
            say(f"  link: {link.url!r}")
            continue
        if pdf is None:
            debug = save_debug(todo.index(link) + 1, received, ctype)
            say(f"  skipped: CDSCO sent {describe(received, ctype)}")
            if debug:
                say(f"  (saved what CDSCO sent as {debug} - send it to your developer if this repeats)")
            say(f"  link: {link.url}")
            continue
        query = urllib.parse.urlencode({"title": link.title, "source_url": link.url,
                                        "released": link.released, "check": "false"})
        code, result = site_call(cfg, f"/api/lists/upload?{query}", data=pdf,
                                 content_type="application/pdf", method="POST", timeout=180)
        if code == 200 and isinstance(result, dict):
            status = result.get("status")
            if status == "imported":
                uploaded += 1
                handled.pop(link.url, None)           # the website now lists it as a source
                say(f"  uploaded: {result.get('entries')} entries ({result.get('kind')}, {result.get('month') or '?'})")
            else:
                handled[link.url] = "duplicate"
                say(f"  {status}: already on the website")
        else:
            detail = result.get("detail") if isinstance(result, dict) else result
            if code == 422:                           # the website can't read this list: don't retry daily
                handled[link.url] = "unreadable"
            say(f"  not imported (HTTP {code}): {detail}")
        save_state(handled)
    if args.dry_run:
        say("Dry run: nothing was uploaded.")
        return 0
    if uploaded:
        start_check(cfg)
    say(f"Done: {uploaded} new list(s) uploaded.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
