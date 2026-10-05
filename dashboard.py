"""
PEAD Dashboard
==============
A local, read-only web dashboard for the PEAD scanner.

Put this file and dashboard.html in the same folder as pead_tool.py, then run:
    py dashboard.py                 -> opens http://127.0.0.1:8050
    py dashboard.py --no-browser
    py dashboard.py --port 8060

It only READS these files from this folder (it never writes anything):
    pead_results.csv        scored results
    archive/*/pead_results.csv   results of earlier runs (for the quarter selector)
    seen.json               filings already handled
    processed_scrips.json   company-quarters already scored
    retry_counts.json       filings awaiting retry
    pead_tool.log           optional: scanner log (live status, timings, log tail)
    pead_tool.py            only to read PEAD_THRESHOLD and SCORE_FROM_QUARTER

No extra packages needed. Standard library only.
The server listens on 127.0.0.1, so it is only reachable from this computer.

Its only outbound requests come from /filing, when you click an exchange link
in the results table: for a BSE filing it checks which folder still holds the
PDF (see resolve_filing) and redirects you there.
"""

import argparse
import csv
import json
import os
import re
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

RESULTS_CSV    = "pead_results.csv"
SEEN_FILE      = "seen.json"
PROCESSED_FILE = "processed_scrips.json"
RETRY_FILE     = "retry_counts.json"
LOG_FILE       = "pead_tool.log"
TOOL_FILE      = "pead_tool.py"
HTML_FILE      = "dashboard.html"

LOG_TAIL_BYTES    = 1_500_000   # only the end of a big log is read
LOG_LINES_SHOWN   = 60
DEFAULT_THRESHOLD = 35.0


def _p(name: str) -> str:
    return os.path.join(BASE_DIR, name)


# ── File readers ──────────────────────────────────────────────

def read_json(name, default):
    try:
        with open(_p(name), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def read_threshold() -> float:
    env = os.environ.get("PEAD_THRESHOLD")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    try:
        with open(_p(TOOL_FILE), "r", encoding="utf-8") as f:
            m = re.search(r"^PEAD_THRESHOLD\s*=\s*([\d.]+)", f.read(), re.M)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return DEFAULT_THRESHOLD


def read_current_quarter() -> str:
    """SCORE_FROM_QUARTER from pead_tool.py: the quarter being scored now."""
    try:
        with open(_p(TOOL_FILE), "r", encoding="utf-8") as f:
            m = re.search(r'^SCORE_FROM_QUARTER\s*=\s*"(Q[1-4]FY\d{2})"', f.read(), re.M)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


def read_results(name: str = RESULTS_CSV):
    """Return (rows, error). Rows are dicts of raw strings keyed by CSV header."""
    path = _p(name)
    if not os.path.exists(path):
        return [], None
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            rows = []
            for row in csv.DictReader(f):
                rows.append({
                    k.strip(): (v.strip() if isinstance(v, str) else v)
                    for k, v in row.items()
                    if k  # drops overflow columns (key None)
                })
        return rows, None
    except Exception as e:
        return [], f"Could not read {name}: {e}"


# ── Quarters ──────────────────────────────────────────────────
#
# The quarter selector covers pead_results.csv and every archived run
# (archive/<name>/pead_results.csv). Each row's quarter is, in order:
#   1. its quarter column (Q2FY27, or UNKNOWN when the PDF never said)
#   2. else its period_end
#   3. else its timestamp, mapped like pead_tool.reporting_quarter: results
#      are filed in the three months after quarter end (filed Sep → Q1)
# Indian FY quarters, as in pead_tool.py: Q1 = Apr–Jun, FY27 = Apr 2026 – Mar 2027.
# Rows scoring above 50 come from the old 100-point scoring and are dropped,
# counted per quarter so the page can say how many were hidden.

ARCHIVE_DIR   = "archive"
OLD_SCALE_MAX = 50
QUARTER_RE    = re.compile(r"^Q([1-4])FY(\d{2})$")
QUARTER_MONTHS = ["Apr–Jun", "Jul–Sep", "Oct–Dec", "Jan–Mar"]


def quarter_label(d) -> str:
    """Indian FY quarter containing a date, e.g. 30 Jun 2026 → Q1FY27."""
    if d.month >= 4:
        return f"Q{(d.month - 4) // 3 + 1}FY{(d.year + 1) % 100:02d}"
    return f"Q4FY{d.year % 100:02d}"


def reporting_quarter(filed_on) -> str:
    """The quarter a result filed on this date reports (filed Sep 2026 → Q1FY27)."""
    m, y = filed_on.month - 3, filed_on.year
    if m < 1:
        m, y = m + 12, y - 1
    return quarter_label(datetime(y, m, 1).date())


def quarter_index(q: str):
    m = QUARTER_RE.match(q or "")
    return int(m.group(2)) * 4 + int(m.group(1)) if m else None


def quarter_name(q: str) -> str:
    """"Q2FY27" → "Q2 FY27 · Jul–Sep 2026"; anything else → "Quarter unknown"."""
    m = QUARTER_RE.match(q or "")
    if not m:
        return "Quarter unknown"
    n, fy = int(m.group(1)), int(m.group(2))
    year = 2000 + fy - (0 if n == 4 else 1)
    return f"Q{n} FY{fy:02d} · {QUARTER_MONTHS[n - 1]} {year}"


def _date(text):
    try:
        return datetime.strptime((text or "").strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _num(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def row_quarter(row: dict) -> tuple:
    """(quarter, where it came from): "csv", "period_end", "filed" or "unknown"."""
    q = (row.get("quarter") or "").strip().upper()
    if q == "UNKNOWN" or QUARTER_RE.match(q):
        return q, "csv"

    period_end = _date(row.get("period_end"))
    if period_end:
        return quarter_label(period_end), "period_end"

    filed = _date(row.get("timestamp"))
    if filed:
        return reporting_quarter(filed), "filed"

    return "UNKNOWN", "unknown"


def is_old_scale(row: dict) -> bool:
    score = _num(row.get("score"))
    return score is not None and score > OLD_SCALE_MAX


def is_result(row: dict) -> bool:
    """A scored row with figures (the dashboard hides empty extractions)."""
    return _num(row.get("score")) is not None and any(
        _num(row.get(k)) is not None for k in ("revenue_cq", "pat_cq", "eps_cq")
    )


def read_all_results():
    """Rows from pead_results.csv and archive/*/pead_results.csv, each tagged
    with _quarter, _quarter_from and _source, minus old-scale rows.

    Returns (rows, {quarter: old-scale rows hidden}, error or None).
    """
    sources = [(RESULTS_CSV, RESULTS_CSV)]
    archive = _p(ARCHIVE_DIR)
    if os.path.isdir(archive):
        for name in sorted(os.listdir(archive)):
            rel = os.path.join(ARCHIVE_DIR, name, RESULTS_CSV)
            if os.path.isfile(_p(rel)):
                sources.append((rel, f"{ARCHIVE_DIR}/{name}"))

    rows, hidden, errors = [], {}, []

    # Archives first, so the live file's rows come last (newest at the end)
    for rel, source in sources[1:] + sources[:1]:
        found, error = read_results(rel)
        if error:
            errors.append(error)
        for row in found:
            quarter, origin = row_quarter(row)
            if is_old_scale(row):
                hidden[quarter] = hidden.get(quarter, 0) + 1
                continue
            row.update({"_quarter": quarter, "_quarter_from": origin, "_source": source})
            rows.append(row)

    return rows, hidden, " ".join(errors) or None


def group_quarters(rows: list, hidden: dict, current: str) -> list:
    """Quarter-selector options: newest first, the UNKNOWN group last, the
    current quarter always present. count is the scored results the page
    lists for the quarter; hidden_old_scale the >50 rows dropped from it."""
    counts = {}
    for row in rows:
        counts.setdefault(row["_quarter"], 0)
        if is_result(row):
            counts[row["_quarter"]] += 1

    keys = set(counts) | set(hidden) | ({current} if current else set())
    known = sorted((q for q in keys if quarter_index(q) is not None), key=quarter_index, reverse=True)
    ordered = known + (["UNKNOWN"] if "UNKNOWN" in keys else [])

    options = []
    for q in ordered:
        n = counts.get(q, 0)
        options.append({
            "value": q,
            "name": quarter_name(q),
            "label": f"{quarter_name(q)} ({n} result{'' if n == 1 else 's'})",
            "count": n,
            "hidden_old_scale": hidden.get(q, 0),
            "current": q == current,
        })
    return options


# ── Log parsing ───────────────────────────────────────────────

# Scanner log lines look like
#   22:48:33  POLL   BSE: 3 new announcements (fetch OK, 2 pages read) · NSE: 1 new announcements (fetch OK, 931 today)
#   22:48:35  ALERT  BSE  ESDS Software Solution          41.0/50  Q2FY27  consolidated  Telegram sent  ⏱ download 0.5s · page scan 0.3s · model 3.4s · exchange→alert 2m 13s
#   22:48:40  POLL   done · 2 result filings · … · checker queue: 1 pending
# The word after the time is a status (POLL, SCORE, ALERT, …) or a level
# (INFO, WARN, ERROR, DEBUG). Older logs had one exchange per fetch line and
# timings on their own "⏱" line; both still parse.
LINE_RE    = re.compile(r"^(?:(\d{4}-\d{2}-\d{2})\s+)?(\d{2}:\d{2}:\d{2})\s+([A-Z]+)\s+(.*)$")
FILING_RE  = re.compile(r"^(BSE|NSE)\s{2}(\S.*?)\s{2,}")      # exchange + padded company
STAGE_RE   = re.compile(r"(download|page scan|OCR|model)\s+([\d.]+)s")
DELAY_RE   = re.compile(r"exchange→(alert|scored)\s+((?:\d+h\s*)?(?:\d+m\s*)?(?:\d+s)?)")
FETCH_RE   = re.compile(r"\b(BSE|NSE):\s+(fetch FAILED[^·]*|\d+ new announcements[^·]*|\d+ announcements fetched[^·]*)")
BLOCKED_RE = re.compile(r"\b(BSE|NSE)\b[^·]*?(?:Access Denied|BLOCKED|blocked:)")
QUEUE_RE   = re.compile(r"checker queue:\s*(\d+)")


def duration_to_seconds(text: str):
    parts = re.findall(r"(\d+)\s*([hms])", text or "")
    if not parts:
        return None
    return sum(int(v) * {"h": 3600, "m": 60, "s": 1}[u] for v, u in parts)


def read_log():
    path = _p(LOG_FILE)
    if not os.path.exists(path):
        return {"available": False}
    try:
        size  = os.path.getsize(path)
        mtime = os.path.getmtime(path)
        with open(path, "rb") as f:
            head = f.read(2)
            utf16 = head in (b"\xff\xfe", b"\xfe\xff")  # PowerShell Tee-Object writes UTF-16
            start = max(0, size - LOG_TAIL_BYTES)
            if utf16:
                start = max(2, start - (start % 2))
            f.seek(start)
            raw = f.read()
        if utf16:
            enc = "utf-16-le" if head == b"\xff\xfe" else "utf-16-be"
            text = raw.decode(enc, errors="replace")
        else:
            text = raw.decode("utf-8", errors="replace")
        if start > 0 and "\n" in text:
            text = text.split("\n", 1)[1]  # drop the partial first line
    except Exception as e:
        return {"available": False, "error": str(e)}

    lines = [ln.rstrip("\r") for ln in text.split("\n") if ln.strip()]

    timings, exchanges, shown = [], {}, []
    queue, context, status = None, None, None

    for line in lines:
        m      = LINE_RE.match(line)
        t      = m.group(2) if m else None
        status = m.group(3) if m else status      # traceback lines follow their record
        msg    = m.group(4) if m else line

        # pead_tool.log carries DEBUG detail; the panel shows what the terminal shows
        if status == "DEBUG":
            continue
        shown.append(line)

        filing = FILING_RE.match(msg)
        if filing:
            context = f"{status} {filing.group(1)} {filing.group(2)}"
        elif "→" in msg and "exchange→" not in msg:          # older log layout
            context = msg.strip()[:140]

        if "⏱" in msg:
            entry = {"time": t, "context": context, "status": status}
            for stage, val in STAGE_RE.findall(msg):
                entry[stage.replace(" ", "_").lower()] = float(val)
            d = DELAY_RE.search(msg)
            if d:
                entry["kind"]  = d.group(1)
                entry["delay"] = duration_to_seconds(d.group(2))
            timings.append(entry)

        for f in FETCH_RE.finditer(msg):
            exchanges[f.group(1)] = {
                "time": t,
                "message": f.group(2).strip(),
                "ok": "FAILED" not in f.group(2),
            }
        for b in BLOCKED_RE.finditer(msg):
            exchanges[b.group(1)] = {"time": t, "message": "blocked (Access Denied)", "ok": False}

        q = QUEUE_RE.search(msg)
        if q:
            queue = int(q.group(1))

    return {
        "available": True,
        "mtime": mtime,
        "age_sec": max(0.0, time.time() - mtime),
        "lines": shown[-LOG_LINES_SHOWN:],
        "timings": timings[-200:],
        "exchanges": exchanges,
        "checker_queue": queue,
    }


# ── Payload ───────────────────────────────────────────────────

def _count(obj) -> int:
    return len(obj) if isinstance(obj, (list, dict)) else 0


def build_payload():
    rows, hidden, csv_error = read_all_results()
    current   = read_current_quarter()
    seen      = read_json(SEEN_FILE, [])
    processed = read_json(PROCESSED_FILE, [])
    retry     = read_json(RETRY_FILE, {})

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "threshold":    read_threshold(),
        "current_quarter": current,
        "quarters":     group_quarters(rows, hidden, current),
        "results":      rows,
        "csv_error":    csv_error,
        "seen_count":   _count(seen),
        "processed":    list(processed) if isinstance(processed, (list, dict)) else [],
        "retry_pending": _count(retry),
        "log":          read_log(),
    }


# ── Filing links ──────────────────────────────────────────────
#
# BSE serves new result PDFs from AttachLive and later moves them to
# AttachHis (checked 2026-09-26: May filings 404 on AttachLive but load from
# AttachHis; mid-August ones are in both; today's only in AttachLive). The
# scanner records the AttachLive URL, so /filing finds where the PDF is now.

BSE_FILING_RE = re.compile(
    r"^https://www\.bseindia\.com/xml-data/corpfiling/(?:AttachLive|AttachHis)/([A-Za-z0-9._-]+\.pdf)$",
    re.I,
)
BSE_FOLDERS = ("AttachLive", "AttachHis")
NSE_FILING_HOSTS = {"nsearchives.nseindia.com", "archives.nseindia.com", "www.nseindia.com"}
SCRIP_RE = re.compile(r"^[A-Za-z0-9&._-]{1,20}$")

BSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
    "Accept": "application/pdf,*/*",
}

FILING_CACHE_SEC = 6 * 3600          # a PDF can move from AttachLive later
_filing_cache = {}                   # attachment name → (url, checked at)


def company_page(exchange: str, scrip: str) -> str:
    """The company's page on its exchange; the last resort for a filing link."""
    if not SCRIP_RE.match(scrip or ""):
        return "/"
    if (exchange or "").upper() == "NSE":
        return f"https://www.nseindia.com/get-quotes/equity?symbol={quote(scrip)}"
    return f"https://www.bseindia.com/stock-share-price/x/x/{quote(scrip)}/"


def is_pdf(url: str) -> bool:
    """True if the URL answers with a PDF (only the first bytes are fetched)."""
    req = urllib.request.Request(url, headers={**BSE_HEADERS, "Range": "bytes=0-7"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.read(8).startswith(b"%PDF")
    except Exception:
        return False


def resolve_filing(url: str, exchange: str, scrip: str) -> str:
    """Where a recorded filing link should go now.

    BSE: AttachLive, then AttachHis, then the company page. NSE archive links
    are passed through. Anything else goes to the company page.
    """
    m = BSE_FILING_RE.match(url or "")

    if m:
        name = m.group(1)
        cached = _filing_cache.get(name)

        if cached and time.time() - cached[1] < FILING_CACHE_SEC:
            return cached[0]

        for folder in BSE_FOLDERS:
            candidate = f"https://www.bseindia.com/xml-data/corpfiling/{folder}/{name}"
            if is_pdf(candidate):
                _filing_cache[name] = (candidate, time.time())
                return candidate

        return company_page(exchange, scrip)

    parts = urlsplit(url or "")
    if parts.scheme == "https" and parts.hostname in NSE_FILING_HOSTS:
        return url

    return company_page(exchange, scrip)


# ── HTTP server ───────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        route = self.path.split("?", 1)[0]

        if route in ("/", "/index.html"):
            try:
                with open(_p(HTML_FILE), "rb") as f:
                    body = f.read()
            except FileNotFoundError:
                self._send(404, b"dashboard.html not found next to dashboard.py",
                           "text/plain; charset=utf-8")
                return
            self._send(200, body, "text/html; charset=utf-8")

        elif route == "/api/data":
            try:
                body = json.dumps(build_payload(), ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode("utf-8")
                self._send(500, body, "application/json; charset=utf-8")

        elif route == "/filing":
            # /filing?url=<recorded filing_url>&exchange=BSE&scrip=532386
            q = parse_qs(urlsplit(self.path).query)
            first = lambda k: (q.get(k) or [""])[0]
            target = resolve_filing(first("url"), first("exchange"), first("scrip"))
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        elif route == "/favicon.ico":
            self._send(204, b"", "text/plain")

        else:
            self._send(404, b"Not found", "text/plain; charset=utf-8")

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, *args):
        pass  # keep the console quiet


def main():
    parser = argparse.ArgumentParser(description="PEAD scanner dashboard")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PEAD_DASHBOARD_PORT", 8050)))
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"PEAD dashboard running at {url}  (Ctrl+C to stop)")
    print(f"Reading data from: {BASE_DIR}")
    if not os.path.exists(_p(LOG_FILE)):
        print(f"Note: {LOG_FILE} not found, so live status and timings will be hidden.")

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
