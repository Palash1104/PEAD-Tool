"""
PEAD Score Tool — BSE + NSE Financial Results Monitor
=====================================================
Polls BSE and NSE every 30s → downloads result PDF → LLM extracts financials
→ PEAD score computed → Telegram alert if score >= PEAD_THRESHOLD

SETUP:
  pip install -r requirements.txt   (plus Tesseract + Poppler for OCR)

CONFIG (.env):
  AICREDITS_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  EXTRACTION_MODEL  (optional, default anthropic/claude-haiku-4-5)

RUN:
  python pead_tool.py          poll forever
  python pead_tool.py --dump   save one raw BSE and NSE response, then exit
"""
import os
from dotenv import load_dotenv
import argparse
import json
import re
import html
import math
import pytesseract
from pdf2image import convert_from_bytes
import csv
import time
import logging
from logging.handlers import RotatingFileHandler
import requests
import pdfplumber
import io
import queue
import threading
from datetime import datetime, date, timedelta
from urllib.parse import quote
from openai import OpenAI

load_dotenv()

pytesseract.pytesseract.tesseract_cmd = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe"
)
POPPLER_PATH = r"C:\poppler\Library\bin"

# ─────────────────────────────────────────────────────────────
# USER CONFIG
# ─────────────────────────────────────────────────────────────
AICREDITS_API_KEY  = os.getenv("AICREDITS_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
EXTRACTION_MODEL   = os.getenv("EXTRACTION_MODEL") or "anthropic/claude-haiku-4-5"

if not all([
    AICREDITS_API_KEY,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
]):
    raise ValueError("Missing environment variables in .env file")

PEAD_THRESHOLD     = 35
POLL_INTERVAL_SEC  = 30
MAX_RETRIES        = 3      # extra attempts per failed filing, one per poll
BLOCKED_ALERT_SEC  = 3600   # Telegram at most hourly about an exchange blocking us

# Growth factors are skipped when last year's base is this small (Rs. Cr)
SMALL_BASE_PAT_CR  = 1
SMALL_BASE_REV_CR  = 10

SEEN_FILE = "seen.json"
RESULTS_CSV = "pead_results.csv"
PROCESSED_SCRIPS_FILE = "processed_scrips.json"   # "{ISIN}_{quarter}" keys
RETRIES_FILE = "retry_counts.json"                # filing id → failed attempts
SCRIP_MASTER_FILE = "scrip_master.json"           # BSE code / NSE symbol → ISIN
# ─────────────────────────────────────────────────────────────

LOG_FORMAT  = "%(asctime)s  %(levelname)s  %(tag)s%(message)s"
LOG_DATEFMT = "%H:%M:%S"
LOG_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pead_tool.log")

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    datefmt=LOG_DATEFMT,
)

class ThreadTag(logging.Filter):
    """Tag log lines written by the ambiguous-outcome worker thread."""
    def filter(self, record):
        record.tag = "[checker] " if record.threadName == "checker" else ""
        return True

for _handler in logging.getLogger().handlers:
    _handler.addFilter(ThreadTag())

log = logging.getLogger(__name__)

def setup_file_logging(path: str = LOG_FILE) -> logging.Handler:
    """Also write every log line to a UTF-8 file, rotating at 5 MB with 3 backups.

    Called only when run as a script, so tests and compare_models.py that
    import this module don't write to the scanner's log.
    """
    handler = RotatingFileHandler(
        path,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
    handler.addFilter(ThreadTag())
    logging.getLogger().addHandler(handler)
    return handler

client = OpenAI(
    api_key=AICREDITS_API_KEY,
    base_url="https://api.aicredits.in/v1",
)

def load_json(path: str, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log.warning(f"Could not load {path}: {e}")

    return default

def save_json(path: str, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:
        log.warning(f"Could not save {path}: {e}")

def load_seen() -> set:
    # Ids are "BSE:<NEWSID>" / "NSE:<seq_id>"; bare ids predate NSE support
    return {
        fid if ":" in fid else f"BSE:{fid}"
        for fid in load_json(SEEN_FILE, [])
    }

def save_seen(seen: set):
    save_json(SEEN_FILE, list(seen))

def load_processed_scrips() -> set:
    return set(load_json(PROCESSED_SCRIPS_FILE, []))

def save_processed_scrips(data: set):
    save_json(PROCESSED_SCRIPS_FILE, sorted(data))

def load_retries() -> dict:
    return load_json(RETRIES_FILE, {})

def save_retries(retries: dict):
    save_json(RETRIES_FILE, retries)

CSV_HEADER = [
    "timestamp",
    "company",
    "scrip",
    "score",

    "revenue_cq",
    "revenue_pq",
    "revenue_ly",

    "pat_cq",
    "pat_pq",
    "pat_ly",

    "ebitda_cq",
    "ebitda_pq",
    "ebitda_ly",

    "eps_cq",
    "eps_ly",

    "exchange",
]

def initialize_csv():

    if not os.path.exists(RESULTS_CSV):
        with open(RESULTS_CSV, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_HEADER)
        return

    # Files from before NSE support lack the exchange column — add it
    try:
        with open(RESULTS_CSV, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))

        if not rows or "exchange" in rows[0]:
            return

        rows[0].append("exchange")

        for row in rows[1:]:
            if row:
                row.append("BSE")

        tmp = RESULTS_CSV + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)
        os.replace(tmp, RESULTS_CSV)

        log.info(f"Added exchange column to {RESULTS_CSV} (existing rows marked BSE)")

    except OSError as e:
        log.warning(f"Could not add exchange column to {RESULTS_CSV}: {e}")

def save_result_csv(filing, score, fin):

    def get3(key):
        v = fin.get(key) or []
        return (v + [None, None, None])[:3]

    rev = get3("revenue_from_operations")
    pat = get3("pat")
    ebitda = (estimate_ebitda(fin) + [None,None,None])[:3]
    eps = get3("basic_eps")

    try:
        with open(RESULTS_CSV, "a", newline="", encoding="utf-8") as f:

            writer = csv.writer(f)

            writer.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),

                filing["company"],
                filing["code"],
                score,

                *rev,
                *pat,
                *ebitda,

                eps[0],
                eps[2],

                filing["exchange"],
            ])
    except OSError as e:
        # e.g. CSV open in Excel — don't let logging block the alert
        log.warning(f"   Could not write CSV row: {e}")

# ── EXCHANGES ────────────────────────────────────────────────
#
# Both exchanges are normalised into the same "filing" dict:
#   exchange, id ("BSE:<NEWSID>" / "NSE:<seq_id>"), company, code (scrip code
#   or symbol), isin (NSE only), category ("Result" / "Board Meeting" / other),
#   headline, headline_fields, attachment_url, exchange_dt

class ExchangeBlocked(Exception):
    """Access Denied / 401 / 403 — must never be read as 'no announcements'."""

class SkipFiling(Exception):
    """The filing can never yield results (no results table, not a PDF):
    skip it for good — no model call, no retry."""

def is_blocked_response(r) -> bool:
    return r.status_code in (401, 403) or "access denied" in r.text[:2000].lower()

# BSE's API answers 403 Access Denied unless Origin/Accept are sent too
# (verified 2026-09-24; UA + Referer alone stopped working)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://www.bseindia.com/",
    "Origin": "https://www.bseindia.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

BSE_ANN_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
    "?pageno={page}&strCat=-1&strPrevDate={date}"
    "&strScrip=&strSearch=P&strToDate={date}"
    "&strType=C&subcategory=-1"
)
BSE_PDF_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
BSE_MASTER_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/ListofScripData/w"
    "?Group=&Scripcode=&industry=&segment=Equity&status=Active"
)
BSE_MAX_PAGES = 100       # safety cap; pages are 50 rows and rows carry TotalPageCnt
BSE_HEADLINE_FIELDS = ["NEWSSUB", "HEADLINE", "MORE", "SUBCATNAME"]
BSE_TIME_FIELDS = ["EXCHANGE_RECEIVED_TIME", "NEWS_DT", "DT_TM", "DTTM"]

NSE_HOME = "https://www.nseindia.com/"
NSE_ANN_URL = (
    "https://www.nseindia.com/api/corporate-announcements"
    "?index=equities&from_date={date}&to_date={date}"
)
NSE_MASTER_URLS = [
    "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
    "https://nsearchives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv",
]
NSE_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
}
NSE_HEADLINE_FIELDS = ["desc", "attchmntText"]
NSE_TIME_FIELDS = ["exchdisstime", "an_dt", "sort_date"]

ISIN_RE = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")

class NseClient:
    """requests session holding NSE cookies; re-primed from the homepage on 401/403."""

    def __init__(self):
        self.session = None

    def _prime(self):
        self.session = requests.Session()
        self.session.headers.update(NSE_HEADERS)
        self.session.get(NSE_HOME, timeout=15)

    def get(self, url: str, timeout: int = 15):
        if self.session is None:
            self._prime()

        r = self.session.get(url, timeout=timeout)

        if r.status_code in (401, 403):
            log.info(f"NSE returned {r.status_code}, refreshing session cookies")
            self._prime()
            r = self.session.get(url, timeout=timeout)

        return r

def parse_exchange_time(row: dict, fields: list) -> datetime | None:
    for field in fields:
        val = row.get(field)
        if not val:
            continue
        s = str(val).strip()
        for fmt in [
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%d/%m/%Y %H:%M:%S",
            "%Y%m%d%H%M%S",
            "%d-%m-%Y %H:%M:%S",
            "%d-%b-%Y %H:%M:%S",
        ]:
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
    return None

def headline_from(row: dict, fields: list) -> tuple:
    """Joined headline text plus the names of the fields it came from."""
    used = [f for f in fields if str(row.get(f) or "").strip()]
    return " ".join(str(row[f]).strip() for f in used), used

def normalise_bse(row: dict) -> dict:
    # HEADLINE is cut off at ~190 chars ("...."); MORE, when set, is the full text
    body = "MORE" if str(row.get("MORE") or "").strip() else "HEADLINE"
    headline, fields = headline_from(row, ["NEWSSUB", body, "SUBCATNAME"])
    raw_id = row.get("NEWSID") or row.get("DT_TM")
    attach = row.get("ATTACHMENTNAME") or ""

    return {
        "exchange": "BSE",
        "id": f"BSE:{raw_id}" if raw_id else "",
        "company": row.get("SLONGNAME") or row.get("SNAME") or "Unknown",
        "code": str(row.get("SCRIP_CD") or ""),
        "isin": None,
        "category": (row.get("CATEGORYNAME") or "").strip(),
        "headline": headline,
        "headline_fields": fields,
        "attachment_url": BSE_PDF_BASE + attach if attach else "",
        "exchange_dt": parse_exchange_time(row, BSE_TIME_FIELDS),
    }

def nse_category(desc: str) -> str:
    """Map NSE's subject line onto the BSE categories the filter understands."""
    lower = desc.lower()
    if "board meeting" in lower:
        return "Board Meeting"
    if "financial result" in lower:
        return "Result"
    return desc

def normalise_nse(row: dict) -> dict:
    headline, fields = headline_from(row, NSE_HEADLINE_FIELDS)
    raw_id = row.get("seq_id") or row.get("attchmntFile")
    isin = str(row.get("sm_isin") or "").strip()
    attachment = str(row.get("attchmntFile") or "")   # "-" when there is none

    return {
        "exchange": "NSE",
        "id": f"NSE:{raw_id}" if raw_id else "",
        "company": row.get("sm_name") or row.get("symbol") or "Unknown",
        "code": str(row.get("symbol") or ""),
        "isin": isin if ISIN_RE.fullmatch(isin) else None,
        "category": nse_category(str(row.get("desc") or "")),
        "headline": headline,
        "headline_fields": fields,
        "attachment_url": attachment if attachment.startswith("http") else "",
        "exchange_dt": parse_exchange_time(row, NSE_TIME_FIELDS),
    }

def fetch_bse_page(page: int, day: date) -> list:
    r = requests.get(
        BSE_ANN_URL.format(page=page, date=day.strftime("%Y%m%d")),
        headers=HEADERS,
        timeout=15
    )

    if is_blocked_response(r):
        raise ExchangeBlocked(f"HTTP {r.status_code} on announcements page {page}")

    r.raise_for_status()
    return r.json().get("Table") or []

def fetch_bse_filings(known_ids: set) -> tuple:
    """Today's new BSE announcements, newest first, paging until a page has
    no new rows or the last page (TotalPageCnt) is reached.
    Returns (new filings, pages read).

    known_ids holds the NEWSIDs fetched on earlier polls and is updated in
    place, so a normal poll reads page 1 and one page of already-known rows.
    A failure on page 1 raises (nothing was read); a later page failing keeps
    what the earlier pages returned.
    """
    today = date.today()
    filings = []

    for page in range(1, BSE_MAX_PAGES + 1):
        if page > 2:
            time.sleep(0.5)   # only a startup backlog reads this deep; go gently

        try:
            rows = fetch_bse_page(page, today)
        except ExchangeBlocked:
            raise
        except Exception as e:
            if page == 1:
                raise
            log.warning(f"BSE: page {page} failed ({e}); keeping {len(filings)} new announcements from earlier pages")
            page -= 1
            break

        new_rows = [
            row for row in rows
            if str(row.get("NEWSID") or row.get("DT_TM") or "") not in known_ids
        ]

        if not new_rows:
            break

        for row in new_rows:
            known_ids.add(str(row.get("NEWSID") or row.get("DT_TM") or ""))
            filings.append(normalise_bse(row))

        try:
            if page >= int(rows[0].get("TotalPageCnt") or BSE_MAX_PAGES):
                break
        except (TypeError, ValueError):
            pass
    else:
        log.warning(f"BSE: stopped at the {BSE_MAX_PAGES}-page cap, older filings may be missed")

    return filings, page

def fetch_nse_filings(nse: NseClient) -> list:
    r = nse.get(NSE_ANN_URL.format(date=date.today().strftime("%d-%m-%Y")))

    if is_blocked_response(r):
        raise ExchangeBlocked(f"HTTP {r.status_code} on announcements")

    r.raise_for_status()
    data = r.json()
    rows = data if isinstance(data, list) else data.get("data") or []
    return [normalise_nse(row) for row in rows]

def download_pdf(filing: dict, nse: NseClient):
    try:
        if filing["exchange"] == "NSE":
            r = nse.get(filing["attachment_url"], timeout=30)
        else:
            r = requests.get(filing["attachment_url"], headers=HEADERS, timeout=20)
        r.raise_for_status()
        return r.content
    except Exception as e:
        log.warning(f"PDF download failed: {e}")
        return None

_last_blocked_alert = {}   # exchange → time of last Telegram warning

def warn_blocked(exchange: str, detail: str):
    log.error(
        f"🚫 {exchange} BLOCKED ({detail}) — its announcements were NOT read "
        f"this poll; this is not 'zero announcements'"
    )

    now = time.time()

    if now - _last_blocked_alert.get(exchange, 0) >= BLOCKED_ALERT_SEC:
        _last_blocked_alert[exchange] = now
        send_telegram_text(
            f"🚫 <b>{exchange} is blocking the scanner</b>\n"
            f"<code>{html.escape(detail)}</code>\n"
            f"No {exchange} filings are being read until this clears. "
            f"(Repeats at most hourly.)"
        )

RESULT_WORD_RE = re.compile(r"\bresults?\b")

def board_meeting_kind(headline: str) -> str:
    """How to treat a Board Meeting filing, judged on its full headline text.

      "intimation" — notice of a future meeting (no "outcome"): skip
      "results"    — mentions results: process as a result filing
      "ambiguous"  — an outcome that doesn't say what was decided ("Outcome of
                     Board Meeting held today"): process only if the PDF has a
                     results table, and cap OCR at AMBIGUOUS_OCR_PAGES
      "other"      — neither an outcome nor results: skip
    """
    text = headline.lower()
    has_outcome = "outcome" in text

    if "intimation" in text and not has_outcome:
        return "intimation"
    if RESULT_WORD_RE.search(text):
        return "results"
    if has_outcome:
        return "ambiguous"
    return "other"

# ── QUARTERS, SCRIP MASTER & DEDUP KEYS ──────────────────────

def quarter_label(period_end: date) -> str:
    """Indian FY quarter containing this date, e.g. 30 Jun 2026 → Q1FY27."""
    m, y = period_end.month, period_end.year
    if m >= 4:
        return f"Q{(m - 4) // 3 + 1}FY{(y + 1) % 100:02d}"
    return f"Q4FY{y % 100:02d}"

def reporting_quarter(filed_on: date) -> str:
    """Fallback when the PDF gives no period: results are filed within the
    three months after quarter end, e.g. filed Sep 2026 → Q1FY27."""
    m, y = filed_on.month - 3, filed_on.year
    if m < 1:
        m, y = m + 12, y - 1
    return quarter_label(date(y, m, 1))

def filing_quarter(fin: dict, filed_on: date) -> str:
    """Quarter from the PDF's period_end, else from the filing date."""
    period_end = fin.get("period_end")

    if period_end:
        ended = date.fromisoformat(period_end)

        if timedelta(0) <= filed_on - ended <= timedelta(days=400):
            return quarter_label(ended)

        log.warning(f"   period_end {period_end} implausible for a filing on {filed_on}, using filing date")

    return reporting_quarter(filed_on)

def fetch_bse_master() -> dict:
    r = requests.get(BSE_MASTER_URL, headers=HEADERS, timeout=60)

    if is_blocked_response(r):
        raise ExchangeBlocked(f"HTTP {r.status_code} on scrip master")

    r.raise_for_status()
    data = r.json()
    rows = data if isinstance(data, list) else data.get("Table") or []

    master = {}

    for row in rows:
        fields = {k.lower(): v for k, v in row.items()}
        code = str(fields.get("scrip_cd") or fields.get("scripcode") or "").strip()
        isin = str(fields.get("isin_number") or fields.get("isin") or "").strip()

        if code and ISIN_RE.fullmatch(isin):
            master[code] = isin

    return master

def fetch_nse_master(nse: NseClient) -> dict:
    master = {}

    for url in NSE_MASTER_URLS:
        try:
            r = nse.get(url, timeout=60)

            if is_blocked_response(r):
                raise ExchangeBlocked(f"HTTP {r.status_code}")

            r.raise_for_status()

            for row in csv.DictReader(io.StringIO(r.text)):
                row = {
                    (k or "").strip().upper(): (v or "").strip()
                    for k, v in row.items()
                }
                symbol, isin = row.get("SYMBOL"), row.get("ISIN NUMBER", "")

                if symbol and ISIN_RE.fullmatch(isin):
                    master[symbol] = isin

        except Exception as e:
            log.warning(f"NSE scrip list {url.rsplit('/', 1)[-1]} failed: {e}")

    return master

def load_scrip_master(nse: NseClient) -> dict:
    """{"bse": {code: ISIN}, "nse": {symbol: ISIN}, "updated": date}, refreshed daily.

    Failed downloads keep the cached mappings; "updated" is only stamped when
    both exchanges delivered, so a restart retries a failed refresh.
    """
    master = load_json(SCRIP_MASTER_FILE, {})
    master.setdefault("bse", {})
    master.setdefault("nse", {})

    if master.get("updated") == date.today().isoformat():
        return master

    complete = True

    for name, fetch in [
        ("bse", fetch_bse_master),
        ("nse", lambda: fetch_nse_master(nse)),
    ]:
        try:
            fresh = fetch()
        except Exception as e:
            log.warning(f"Scrip master: {name.upper()} download failed: {e}")
            fresh = {}

        if fresh:
            master[name].update(fresh)
        else:
            complete = False

        log.info(
            f"Scrip master: {len(fresh)} {name.upper()} codes downloaded, "
            f"{len(master[name])} known"
        )

    if complete:
        master["updated"] = date.today().isoformat()

    save_json(SCRIP_MASTER_FILE, master)
    return master

def lookup_isin(filing: dict, master: dict) -> str | None:
    return filing["isin"] or master[filing["exchange"].lower()].get(filing["code"])

def processed_key(filing: dict, isin: str | None, quarter: str) -> str:
    """"{ISIN}_{quarter}" so one company's BSE and NSE filings collide.
    Falls back to "{EXCHANGE}-{code}_{quarter}" while the ISIN is unknown."""
    if isin:
        return f"{isin}_{quarter}"
    return f"{filing['exchange']}-{filing['code']}_{quarter}"

KEY_RE = re.compile(r"(?:(BSE|NSE)-)?(.+)_(Q[1-4]FY\d{2})")

def migrate_processed_keys(processed: set, master: dict) -> set:
    """Upgrade pre-NSE "{scrip}_{q}" keys and "{EXCHANGE}-{code}_{q}" fallback
    keys to "{ISIN}_{q}" wherever the scrip master now knows the ISIN."""
    migrated = set()

    for key in processed:
        m = KEY_RE.fullmatch(key)

        if not m or (m.group(1) is None and ISIN_RE.fullmatch(m.group(2))):
            migrated.add(key)
            continue

        exchange, code, quarter = m.group(1) or "BSE", m.group(2), m.group(3)
        isin = master[exchange.lower()].get(code)
        migrated.add(f"{isin}_{quarter}" if isin else f"{exchange}-{code}_{quarter}")

    return migrated

# ── LLM PDF EXTRACTION ───────────────────────────────────────

EXTRACTION_PROMPT = """This text comes from a quarterly financial result PDF filed by an Indian listed company on BSE/NSE.

Use the CONSOLIDATED results if the text contains a consolidated results table; otherwise use the STANDALONE results.
Use only the individual quarter columns (NOT year-to-date, half-year, nine-month or full year columns).
The result table has 3 quarterly columns:
  Column 1: Current quarter (most recent)
  Column 2: Previous quarter (immediately preceding)
  Column 3: Same quarter last year (year-over-year)

Extract these values exactly as printed, in the table's own unit (do NOT convert units):
- revenue_from_operations: Revenue from operations / Net Sales (NOT total income)
- total_income: Total income including other income
- ebitda: EBITDA or Operating Profit if explicitly stated as a line item (else set null)
- finance_cost: Finance Cost / Borrowing Cost
- depreciation: Depreciation and Amortisation Expense
- employee_expense: Employee Benefit Expense
- other_expenses: Other Expenses
- total_expenses: Total Expenses
- pbt: Profit Before Tax
- pat: Profit After Tax for the quarter
- basic_eps: Basic Earnings Per Share in Rs.

Also report:
- basis: which results you used, "consolidated" or "standalone"
- unit: the unit the table states for amounts, one of "crores", "lakhs", "millions", "thousands", "rupees"
- period_end: the "quarter ended" date of the current quarter column, as YYYY-MM-DD (null if not shown)

Return ONLY a JSON object, no explanation, no markdown:
{
"basis":"consolidated|standalone",
"unit":"crores|lakhs|millions|thousands|rupees",
"period_end":"YYYY-MM-DD",
"revenue_from_operations":[cq,pq,ly],
"total_income":[cq,pq,ly],
"ebitda":[cq,pq,ly],
"finance_cost":[cq,pq,ly],
"depreciation":[cq,pq,ly],
"employee_expense":[cq,pq,ly],
"other_expenses":[cq,pq,ly],
"total_expenses":[cq,pq,ly],
"pbt":[cq,pq,ly],
"pat":[cq,pq,ly],
"basic_eps":[cq,pq,ly]
}

Rules:
- null for any value not found with confidence
- Plain numbers only, no commas or symbols
- Negatives as negative numbers e.g. -12.5 (values in brackets like (12.5) are negative)
- Do NOT include annual/year-ended columns"""

FIN_KEYS = [
    "revenue_from_operations",
    "total_income",
    "ebitda",
    "finance_cost",
    "depreciation",
    "employee_expense",
    "other_expenses",
    "total_expenses",
    "pbt",
    "pat",
    "basic_eps",
]
PER_SHARE_KEYS = {"basic_eps"}  # already in Rs., never unit-converted

UNIT_TO_CRORE = {
    "crores": 1,
    "lakhs": 0.01,
    "millions": 0.1,
    "thousands": 0.0001,
    "rupees": 0.0000001,
}
UNIT_ALIASES = {
    "crore": "crores", "cr": "crores",
    "lakh": "lakhs", "lacs": "lakhs", "lac": "lakhs",
    "million": "millions", "mn": "millions",
    "thousand": "thousands",
    "rupee": "rupees", "rs": "rupees", "inr": "rupees",
}
PERIOD_END_FORMATS = [
    "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%Y",
    "%d-%b-%Y", "%d %b %Y", "%d %B %Y", "%B %d, %Y", "%b %d, %Y",
]

MAX_PDF_PAGES       = 25
AMBIGUOUS_OCR_PAGES = 8     # OCR budget for outcomes whose headline names no results
MIN_TEXT_CHARS      = 500   # whole PDF below this → treat as scanned, OCR it all
LOW_TEXT_PAGE_CHARS = 200   # no table in text layer → OCR pages below this
HEADING_LINES       = 20    # a result table's title sits near the top of its page

# A page counts as a result table only if it has at least two of these
# (real text layers are noisy: ESDS and Purple Style tables matched just two)
TABLE_MARKERS = [
    "revenue from operations",
    "total income",
    "profit before tax",
    "earnings per share",
]

# Checked against the top of each table page, in order of preference
RESULT_HEADINGS = [
    ("consolidated", re.compile(
        r"(?:un)?audited\s+consolidated|consolidated\s+(?:un)?audited"
        r"|consolidated\s+(?:financial\s+)?results|consolidated\s+statement"
    )),
    ("standalone", re.compile(
        r"(?:un)?audited\s+standalone|standalone\s+(?:un)?audited"
        r"|standalone\s+(?:financial\s+)?results|standalone\s+statement"
    )),
    ("generic", re.compile(
        r"results\s+for\s+the\s+(?:quarter|half\s+year|period)"
        r"|(?:un)?audited\s+financial\s+results"
    )),
]

# Table pages whose title matched none of the above rank last, as "untitled"
TABLE_PREFERENCE = ["consolidated", "standalone", "generic", "untitled"]

def classify_result_page(text: str) -> str | None:
    lower = text.lower()

    if sum(marker in lower for marker in TABLE_MARKERS) < 2:
        return None

    heading = " ".join(lower.splitlines()[:HEADING_LINES])

    for basis, pattern in RESULT_HEADINGS:
        if pattern.search(heading):
            return basis

    return "untitled"

def with_next_page(idx: int, page_texts: list) -> list:
    return [i for i in (idx, idx + 1) if i < len(page_texts)]

def find_table_pages(page_texts: list) -> tuple:
    """Result table page plus its continuation, in TABLE_PREFERENCE order.
    Returns (indices, basis), or ([], None) if no page is a results table."""
    first_found = {}

    for idx, text in enumerate(page_texts):
        basis = classify_result_page(text)
        if basis and basis not in first_found:
            first_found[basis] = idx

    for basis in TABLE_PREFERENCE:
        if basis in first_found:
            return with_next_page(first_found[basis], page_texts), basis

    return [], None

def ocr_page(pdf_bytes: bytes, page_num: int) -> str:
    images = convert_from_bytes(
        pdf_bytes,
        dpi=250,
        first_page=page_num,
        last_page=page_num,
        poppler_path=POPPLER_PATH
    )
    return pytesseract.image_to_string(images[0]) if images else ""

def ocr_pages(pdf_bytes: bytes, page_texts: list, indices: list,
              timings: dict | None = None) -> list:
    """OCR the given pages one at a time, replacing their text.

    Stops early once a consolidated table and its next page are both readable.
    Adds the time spent to timings["ocr"].
    """
    log.info(f"    Running OCR on pages {[i + 1 for i in indices]}...")

    started = time.monotonic()
    page_texts = list(page_texts)
    pending = set(indices)

    try:
        for idx in indices:
            page_texts[idx] = ocr_page(pdf_bytes, idx + 1)
            pending.discard(idx)

            pages, basis = find_table_pages(page_texts)
            if basis == "consolidated" and not pending.intersection(pages):
                break

    except Exception as e:
        log.warning(f"    OCR extraction failed: {e}")

    if timings is not None:
        timings["ocr"] = timings.get("ocr", 0) + time.monotonic() - started

    return page_texts

def get_result_text(pdf_bytes: bytes, ocr_page_limit: int = MAX_PDF_PAGES,
                    timings: dict | None = None) -> tuple:
    """Text of the result table pages to send to the model: (text, reason).

    text is None when no page passes the results-table check, even after OCR.
    Only the first ocr_page_limit pages are ever OCR'd. Records
    timings["scan"] (text layer + table check) and timings["ocr"].
    """
    started = time.monotonic()
    timings = {} if timings is None else timings
    ocr_before = timings.get("ocr", 0)

    try:
        return _find_result_text(pdf_bytes, ocr_page_limit, timings)
    finally:
        ocr_spent = timings.get("ocr", 0) - ocr_before
        timings["scan"] = timings.get("scan", 0) + time.monotonic() - started - ocr_spent

def _find_result_text(pdf_bytes: bytes, ocr_page_limit: int, timings: dict) -> tuple:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page_count = min(len(pdf.pages), MAX_PDF_PAGES)
        page_texts = [
            page.extract_text() or ""
            for page in pdf.pages[:page_count]
        ]

    real_chars = sum(len(t.strip()) for t in page_texts)

    if real_chars < MIN_TEXT_CHARS:
        log.info(f"    Only {real_chars} chars of text layer, OCR-ing the whole PDF...")
        page_texts = ocr_pages(pdf_bytes, page_texts, list(range(min(page_count, ocr_page_limit))), timings)
        selected, basis = find_table_pages(page_texts)

    else:
        selected, basis = find_table_pages(page_texts)

        # Hybrid PDF: text cover letter, scanned result pages
        low_text = [
            i for i, t in enumerate(page_texts)
            if len(t.strip()) < LOW_TEXT_PAGE_CHARS and i < ocr_page_limit
        ]

        if not selected and low_text:
            log.info("    No result table in text layer, OCR-ing low-text pages...")
            page_texts = ocr_pages(pdf_bytes, page_texts, low_text, timings)
            selected, basis = find_table_pages(page_texts)

    if not selected:
        return None, "no results table found"

    reason = f"{basis} table"

    text = "\n".join(
        f"\n\n--- PAGE {i + 1} ---\n{page_texts[i]}"
        for i in selected
    )

    log.info(
        f"    Selected pages {[i + 1 for i in selected]} "
        f"({reason}, {len(text)} chars)"
    )

    return text, reason

def _to_number(value) -> float | None:
    if isinstance(value, bool):
        return None

    if isinstance(value, str):
        try:
            value = float(value.replace(",", "").strip())
        except ValueError:
            return None

    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)

    return None

def parse_period_end(value) -> str | None:
    """ISO date string, or None if the model gave nothing parseable."""
    if not isinstance(value, str) or not value.strip():
        return None

    for fmt in PERIOD_END_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            continue

    return None

def normalise_financials(data) -> dict | None:
    """Validate the model's JSON: numbers only, 3 values per metric, amounts in crores."""
    if not isinstance(data, dict):
        log.warning("    Model returned non-object JSON")
        return None

    unit = str(data.get("unit") or "").strip().lower()
    unit = UNIT_ALIASES.get(unit, unit)

    if unit not in UNIT_TO_CRORE:
        log.warning(f"    Unrecognised unit from model: {data.get('unit')!r}")
        return None

    factor = UNIT_TO_CRORE[unit]

    fin = {
        "basis": str(data.get("basis") or "unknown").strip().lower(),
        "unit": unit,
        "period_end": parse_period_end(data.get("period_end")),
    }

    for key in FIN_KEYS:
        values = data.get(key)

        if not isinstance(values, list):
            values = []

        numbers = [_to_number(v) for v in (values + [None, None, None])[:3]]

        if key not in PER_SHARE_KEYS:
            numbers = [
                round(n * factor, 4) if n is not None else None
                for n in numbers
            ]

        fin[key] = numbers

    return fin

def extract_from_text(text: str, model: str, timings: dict | None = None) -> dict | None:
    """Send result-page text to the model via AICredits; validated financials or None.
    Records timings["model"]."""
    started = time.monotonic()

    try:
        try:
            response = client.chat.completions.create(
                model=model,
                max_tokens=1500,
                messages=[
                    {
                        "role": "user",
                        "content": f"{EXTRACTION_PROMPT}\n\n---\nPDF TEXT:\n{text}"
                    }
                ],
            )
        finally:
            if timings is not None:
                timings["model"] = time.monotonic() - started

        raw = response.choices[0].message.content or ""
        data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        log.info(f"    {model} extracted: {data}")

        fin = normalise_financials(data)

        if fin:
            log.info(
                f"    Basis: {fin['basis']}, unit: {fin['unit']}, "
                f"period end: {fin['period_end']}"
            )

        return fin

    except json.JSONDecodeError as e:
        log.warning(f"    Model JSON parse error: {e}")
        return None
    except Exception as e:
        log.warning(f"    Model extraction failed: {e}")
        return None

def extract_financials(pdf_bytes: bytes, model: str | None = None,
                       ambiguous: bool = False, timings: dict | None = None) -> dict | None:
    """Pick the result pages locally (OCR if needed), then have the model read them.

    ambiguous: a board meeting outcome whose headline names no results — OCR
    is capped at AMBIGUOUS_OCR_PAGES and the table check outcome is logged.
    Raises SkipFiling when the PDF has no results table (the model isn't called).
    """
    try:
        text, reason = get_result_text(
            pdf_bytes,
            AMBIGUOUS_OCR_PAGES if ambiguous else MAX_PDF_PAGES,
            timings
        )
    except Exception as e:
        log.warning(f"    PDF text extraction failed: {e}")
        return None

    if ambiguous:
        log.info(f"   Ambiguous outcome → results table {'found' if text else 'not found'}")

    if not text:
        raise SkipFiling(reason)

    return extract_from_text(text, model or EXTRACTION_MODEL, timings)

# ── PEAD SCORE ────────────────────────────────────────────────

def _pct(curr, prev):
    if curr is None or prev is None or prev == 0:
        return None
    return (curr - prev) / abs(prev) * 100

def estimate_ebitda(fin):

    # Use extracted EBITDA if available
    ebitda = fin.get("ebitda")

    if ebitda and ebitda[0] is not None:
        return ebitda

    pbt_vals = fin.get("pbt") or [None, None, None]
    fin_cost_vals = fin.get("finance_cost") or [None, None, None]
    dep_vals = fin.get("depreciation") or [None, None, None]

    estimated = []

    for pbt, fin_cost, dep in zip(
        pbt_vals,
        fin_cost_vals,
        dep_vals
    ):

        if None not in [pbt, fin_cost, dep]:

            estimated.append(
                pbt + fin_cost + dep
            )

        else:
            estimated.append(None)

    return estimated

def calc_margin(value, revenue):

    if (
        value is None or
        revenue is None or
        revenue == 0
    ):
        return None

    return (value / revenue) * 100

def band_score(value, bands):

    if value is None:
        return 0.0

    for threshold, score in bands:

        if value >= threshold:
            return score

    return 0.0

def compute_pead_score(fin: dict):

    bd = {}
    score = 0.0

    def get3(key):
        v = fin.get(key) or []
        return (v + [None, None, None])[:3]

    c_rev, q_rev, y_rev = get3("revenue_from_operations")
    c_pat, q_pat, y_pat = get3("pat")
    
    ebitda_vals = estimate_ebitda(fin)
    c_ebitda, q_ebitda, y_ebitda = (
        ebitda_vals + [None, None, None]
    )[:3]
    
    c_eps, _, y_eps = get3("basic_eps")

    # ─────────────────────────────────────────
    # Reject weak companies
    # ─────────────────────────────────────────

    reject_conditions = [
        ("Current PAT", c_pat),
        ("Current EBITDA", c_ebitda),
        ("Current EPS", c_eps),
    ]

    for label, value in reject_conditions:

        if value is not None and value < 0:

            bd["Rejected"] = (
                f"{label} is negative",
                "0/50"
            )

            return 0.0, bd

    # ─────────────────────────────────────────
    # Growth calculations
    # ─────────────────────────────────────────

    eps_yoy = _pct(c_eps, y_eps)
    pat_yoy = _pct(c_pat, y_pat)
    rev_yoy = _pct(c_rev, y_rev)
    rev_qoq = _pct(c_rev, q_rev)

    # A tiny base makes that growth % meaningless → skip just that factor.
    # abs() so a real loss (e.g. -5 Cr) still counts as a turnaround base.
    pat_small     = y_pat is not None and abs(y_pat) < SMALL_BASE_PAT_CR
    rev_yoy_small = y_rev is not None and y_rev < SMALL_BASE_REV_CR
    rev_qoq_small = q_rev is not None and q_rev < SMALL_BASE_REV_CR

    def growth_label(growth, small):
        if small:
            return "small base"
        return f"{growth:.1f}%" if growth is not None else "N/A"

    # Current negatives were rejected above, so a negative base = loss → profit
    eps_turnaround = y_eps is not None and y_eps < 0 and c_eps is not None
    pat_turnaround = y_pat is not None and y_pat < 0 and c_pat is not None

    # ─────────────────────────────────────────
    # 1. EPS Surprise (15 pts, half on turnaround)
    # ─────────────────────────────────────────

    s = 0.0 if pat_small else band_score(eps_yoy, [
        (100, 15),
        (70, 13),
        (50, 11),
        (30, 8),
        (15, 5),
        (5, 2),
    ])

    if eps_turnaround:
        s /= 2

    score += s

    bd["EPS Surprise"] = (
        growth_label(eps_yoy, pat_small),
        f"{s:.1f}/15"
    )

    # ─────────────────────────────────────────
    # 2. PAT Growth YoY (10 pts, half on turnaround)
    # ─────────────────────────────────────────

    s = 0.0 if pat_small else band_score(pat_yoy, [
        (80, 10),
        (50, 8),
        (30, 6),
        (15, 4),
        (5, 2),
    ])

    if pat_turnaround:
        s /= 2

    score += s

    bd["PAT Growth YoY"] = (
        growth_label(pat_yoy, pat_small),
        f"{s:.1f}/10"
    )

    # ─────────────────────────────────────────
    # 3. Revenue Growth YoY (10 pts)
    # ─────────────────────────────────────────

    s = 0.0 if rev_yoy_small else band_score(rev_yoy, [
        (50, 10),
        (30, 8),
        (20, 6),
        (10, 4),
        (5, 2),
    ])

    score += s

    bd["Revenue Growth YoY"] = (
        growth_label(rev_yoy, rev_yoy_small),
        f"{s:.1f}/10"
    )

    # ─────────────────────────────────────────
    # 4. EBITDA Margin Expansion (5 pts)
    # ─────────────────────────────────────────

    if all(v is not None for v in [
        c_rev, y_rev,
        c_ebitda, y_ebitda
    ]) and c_rev and y_rev:

        margin_curr = calc_margin(c_ebitda, c_rev)
        margin_ly   = calc_margin(y_ebitda, y_rev)

        delta = margin_curr - margin_ly

        s = band_score(delta, [
            (5, 5),
            (3, 4),
            (2, 3),
            (1, 2),
            (0.5, 1),
        ])

        score += s

        bd["EBITDA Margin Exp"] = (
            f"{delta:+.1f}pp",
            f"{s:.1f}/5"
        )

    else:

        bd["EBITDA Margin Exp"] = (
            "N/A",
            "0.0/5"
        )

    # ─────────────────────────────────────────
    # 5. Revenue QoQ Momentum (5 pts)
    # ─────────────────────────────────────────

    s = 0.0 if rev_qoq_small else band_score(rev_qoq, [
        (25, 5),
        (15, 4),
        (10, 3),
        (5, 2),
    ])

    score += s

    bd["Revenue QoQ"] = (
        growth_label(rev_qoq, rev_qoq_small),
        f"{s:.1f}/5"
    )

    # ─────────────────────────────────────────
    # 6. Profitability Bonus (5 pts)
    # ─────────────────────────────────────────

    if (
        c_pat is not None and
        c_rev is not None and
        c_rev > 0
    ):

        net_margin = (c_pat / c_rev) * 100

        s = band_score(net_margin, [
            (25, 5),
            (18, 4),
            (12, 3),
            (8, 2),
        ])

        score += s

        bd["Net Margin Quality"] = (
            f"{net_margin:.1f}%",
            f"{s:.1f}/5"
        )

    if (eps_turnaround or pat_turnaround) and not pat_small:
        bd["Turnaround"] = (
            "loss→profit",
            "½ PAT/EPS"
        )

    return round(score, 1), bd

# ── TELEGRAM ─────────────────────────────────────────────────

def send_telegram_text(text: str) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
    except requests.RequestException as e:
        log.warning(f"  Telegram send failed: {e}")
        return False

    if r.status_code == 200:
        return True

    log.warning(f"  Telegram error: {r.text}")
    return False

def exchange_link(filing: dict) -> str:
    if filing["exchange"] == "NSE":
        return f"https://www.nseindia.com/get-quotes/equity?symbol={quote(filing['code'])}"
    return f"https://www.bseindia.com/stock-share-price/x/x/{quote(filing['code'])}/"

def send_telegram(filing,
    score,
    bd,
    fin,
    quarter,
    alert_time,
    delay_text):
    exchange_time = (
        filing["exchange_dt"].strftime("%d %b %Y  %H:%M:%S")
        if filing["exchange_dt"] else "N/A"
    )
    period = f"{quarter} (to {fin['period_end']})" if fin.get("period_end") else quarter

    emoji = (
        "🚀" if score >= 40 else
        "✅" if score >= 30 else
        "🟡"
    )

    def pct_fmt(curr, prev):
        p = _pct(curr, prev)
        return f"{p:.1f}" if p is not None else "N/A"

    lines = [

        f"{emoji} <b>PEAD ALERT • {score}/50</b>",
        *(["🔄 <b>TURNAROUND</b> (loss → profit)"] if "Turnaround" in bd else []),
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",

        f"🏢 <b>{html.escape(filing['company'])}</b>",
        f"📌 {filing['exchange']}: <code>{html.escape(filing['code'])}</code>",

        "",

        f"🏛 <b>Source:</b> <code>{filing['exchange']}</code>",
        f"📄 <b>Filing:</b> <code>{html.escape(filing['category'])}</code>",
        f"🗓 <b>Period:</b> <code>{html.escape(period)}</code>",
        f"🕐 <b>Exchange:</b> <code>{exchange_time}</code>",
        f"📲 <b>Alert:</b>   <code>{alert_time}</code>",
        f"⚡ <b>Delay:</b>   <code>{delay_text}</code>",

        "",

        f"📊 <b>QUARTERLY FINANCIALS (₹ Cr, {html.escape(fin.get('basis', 'unknown'))})</b>",

        "<pre>"

        f"{'Metric':<12}"
        f"{'QoQ%':>10}"
        f"{'YoY%':>10}"
        f"{'CQ':>12}"
        f"{'PQ':>12}"
        f"{'LY':>12}\n"

        f"{'-'*68}\n"

        f"{'Revenue':<12}"
        f"{(pct_fmt((fin.get('revenue_from_operations') or [None,None,None])[0], (fin.get('revenue_from_operations') or [None,None,None])[1])):>10}"
        f"{(pct_fmt((fin.get('revenue_from_operations') or [None,None,None])[0], (fin.get('revenue_from_operations') or [None,None,None])[2])):>10}"
        f"{(fin.get('revenue_from_operations') or [None,None,None])[0] or 'N/A':>12}"
        f"{(fin.get('revenue_from_operations') or [None,None,None])[1] or 'N/A':>12}"
        f"{(fin.get('revenue_from_operations') or [None,None,None])[2] or 'N/A':>12}\n"

        f"{'EBITDA':<12}"
        f"{(pct_fmt(estimate_ebitda(fin)[0], estimate_ebitda(fin)[1])):>10}"
        f"{(pct_fmt(estimate_ebitda(fin)[0], estimate_ebitda(fin)[2])):>10}"
        f"{estimate_ebitda(fin)[0] or 'N/A':>12}"
        f"{estimate_ebitda(fin)[1] or 'N/A':>12}"
        f"{estimate_ebitda(fin)[2] or 'N/A':>12}\n"

        f"{'PBT':<12}"
        f"{(pct_fmt((fin.get('pbt') or [None,None,None])[0], (fin.get('pbt') or [None,None,None])[1])):>10}"
        f"{(pct_fmt((fin.get('pbt') or [None,None,None])[0], (fin.get('pbt') or [None,None,None])[2])):>10}"
        f"{(fin.get('pbt') or [None,None,None])[0] or 'N/A':>12}"
        f"{(fin.get('pbt') or [None,None,None])[1] or 'N/A':>12}"
        f"{(fin.get('pbt') or [None,None,None])[2] or 'N/A':>12}\n"

        f"{'PAT':<12}"
        f"{(pct_fmt((fin.get('pat') or [None,None,None])[0], (fin.get('pat') or [None,None,None])[1])):>10}"
        f"{(pct_fmt((fin.get('pat') or [None,None,None])[0], (fin.get('pat') or [None,None,None])[2])):>10}"
        f"{(fin.get('pat') or [None,None,None])[0] or 'N/A':>12}"
        f"{(fin.get('pat') or [None,None,None])[1] or 'N/A':>12}"
        f"{(fin.get('pat') or [None,None,None])[2] or 'N/A':>12}\n"

        f"{'EPS':<12}"
        f"{(pct_fmt((fin.get('basic_eps') or [None,None,None])[0], (fin.get('basic_eps') or [None,None,None])[1])):>10}"
        f"{(pct_fmt((fin.get('basic_eps') or [None,None,None])[0], (fin.get('basic_eps') or [None,None,None])[2])):>10}"
        f"{(fin.get('basic_eps') or [None,None,None])[0] or 'N/A':>12}"
        f"{(fin.get('basic_eps') or [None,None,None])[1] or 'N/A':>12}"
        f"{(fin.get('basic_eps') or [None,None,None])[2] or 'N/A':>12}"

        "</pre>",

        "",

        "📈 <b>PEAD SCORECARD</b>",

        "<pre>"
        f"{'Factor':<28}{'Value':>12}{'Pts':>10}\n"
        f"{'-'*50}\n"
        + "\n".join(
            html.escape(f"{k[:27]:<28}{val:>12}{pts:>10}")
            for k, (val, pts) in bd.items()
        )
        + "</pre>",

        "",

        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",

        f"🔗 {html.escape(exchange_link(filing))}",
    ]

    if send_telegram_text("\n".join(lines)):
        log.info("  ✅ Telegram sent")


# ── MAIN ─────────────────────────────────────────────────────
#
# Each poll the main thread handles clear result filings first (oldest
# first). Ambiguous board meeting outcomes go to AmbiguousChecker, one
# background thread that downloads the PDF, checks it for a results table
# and runs the model if there is one. The checker never touches scanner
# state: the main thread applies its finished checks (score, CSV, alert)
# between polls, so a clear result never waits behind an ambiguous PDF.

def has_core_values(fin: dict) -> bool:
    return (
        fin["revenue_from_operations"][0] is not None and
        fin["pat"][0] is not None
    )

def format_delay(since: datetime) -> str:
    hours, rest = divmod(int((datetime.now() - since).total_seconds()), 3600)
    mins, secs = divmod(rest, 60)
    return f"{hours}h {mins}m {secs}s" if hours else f"{mins}m {secs}s"

def log_timings(timings: dict, exchange_dt=None, outcome: str | None = None):
    """One line per filing: where the time went, plus exchange time → outcome."""
    parts = [
        f"{label} {timings[key]:.1f}s"
        for key, label in [
            ("download", "download"),
            ("scan", "page scan"),
            ("ocr", "OCR"),
            ("model", "model"),
        ]
        if key in timings
    ]

    if outcome and exchange_dt:
        parts.append(f"exchange→{outcome} {format_delay(exchange_dt)}")

    if parts:
        log.info("   ⏱ " + " · ".join(parts))

def obtain_financials(filing: dict, nse: NseClient, ambiguous: bool = False,
                      timings: dict | None = None) -> dict | None:
    """Download the PDF and extract validated financials.

    Touches no scanner state, so the checker thread can run it too. Returns
    None on a retryable failure; raises SkipFiling when retrying can't help.
    """
    timings = {} if timings is None else timings
    exchange_dt = filing["exchange_dt"]

    log.info(
        f"   Exchange time: "
        f"{exchange_dt.strftime('%d %b %Y  %H:%M:%S') if exchange_dt else 'N/A'}"
    )

    if not filing["attachment_url"]:
        log.info("   No attachment"); return None

    started = time.monotonic()
    pdf = download_pdf(filing, nse)
    timings["download"] = time.monotonic() - started

    if not pdf:
        log.info("   PDF download failed"); return None

    if not pdf.startswith(b"%PDF"):
        raise SkipFiling(f"attachment is not a PDF (starts {pdf[:8]!r})")

    pdf_mb = len(pdf) / (1024 * 1024)
    log.info(f"   PDF: {pdf_mb:.1f} MB — extracting text…")
    fin = extract_financials(pdf, ambiguous=ambiguous, timings=timings)
    if not fin:
        log.info("   Could not extract financials"); return None

    if not has_core_values(fin):
        log.info("   Missing current-quarter revenue or PAT"); return None

    return fin

def score_filing(filing: dict, isin, fin: dict, processed: set, timings: dict) -> str:
    """Quarter key, duplicate check, score, CSV and alert. Returns the
    processed key — also when the PDF's period shows it was already scored."""
    exchange_dt = filing["exchange_dt"]
    quarter = filing_quarter(fin, (exchange_dt or datetime.now()).date())
    key = processed_key(filing, isin, quarter)

    if key in processed:
        log.info(f"   {key} already scored (quarter from PDF), no alert")
        log_timings(timings)
        return key

    score, bd = compute_pead_score(fin)

    save_result_csv(filing, score, fin)
    log.info(f"   PEAD score: {score} ({key})")

    if score >= PEAD_THRESHOLD:

        log.info("   🚀 Above threshold! Sending alert…")

        send_telegram(
            filing,
            score,
            bd,
            fin,
            quarter,
            datetime.now().strftime("%d %b %Y  %H:%M:%S"),
            format_delay(exchange_dt) if exchange_dt else "N/A"
        )
        log_timings(timings, exchange_dt, "alert")
    else:
        log.info(f"   Below {PEAD_THRESHOLD}, no alert")
        log_timings(timings, exchange_dt, "scored")

    return key

def process_filing(filing: dict, isin, processed: set, nse: NseClient) -> str | None:
    """Clear result filings, on the main thread: obtain financials, then score.

    Returns the processed key, or None on a retryable failure. Raises
    SkipFiling when retrying can't help.
    """
    timings = {}

    try:
        fin = obtain_financials(filing, nse, timings=timings)
    except SkipFiling:
        log_timings(timings)
        raise

    if not fin:
        log_timings(timings)
        return None

    return score_filing(filing, isin, fin, processed, timings)

class AmbiguousChecker:
    """One background thread for ambiguous board meeting outcomes.

    submit() queues a filing; the thread runs obtain_financials (download,
    results-table check, model only if a table is found) and puts a result on
    `done` for the main thread to apply. in_flight is only touched by the
    main thread.
    """

    def __init__(self):
        self.todo = queue.Queue()
        self.done = queue.Queue()
        self.in_flight = set()     # filing ids queued or being checked
        self.nse = NseClient()     # own session: requests sessions aren't thread-safe
        self.thread = threading.Thread(target=self._run, name="checker", daemon=True)
        self.thread.start()

    def submit(self, filing: dict) -> bool:
        if filing["id"] in self.in_flight:
            return False
        self.in_flight.add(filing["id"])
        self.todo.put(filing)
        return True

    def _run(self):
        while True:
            filing = self.todo.get()
            try:
                if filing is None:
                    return
                self.done.put(self._check(filing))
            finally:
                self.todo.task_done()

    def _check(self, filing: dict) -> dict:
        timings = {}
        result = {"filing": filing, "fin": None, "skip": None, "timings": timings}
        log.info(f"→ Checking {filing['exchange']} {filing['company']}: ambiguous outcome PDF")

        try:
            result["fin"] = obtain_financials(filing, self.nse, ambiguous=True, timings=timings)
        except SkipFiling as e:
            result["skip"] = str(e)
        except Exception:
            log.exception("   Unexpected error checking ambiguous outcome")

        return result

    def stop(self):
        self.todo.put(None)
        self.thread.join(timeout=5)

class ScannerState:
    """Everything that survives between polls; persisted parts saved as they change.
    Only the main thread uses it."""

    def __init__(self, nse: NseClient):
        self.nse = nse
        self.seen = load_seen()
        self.retries = load_retries()          # filing id → failed attempts
        self.pending = {}                      # filing id → filing awaiting retry
        self.bse_known_ids = set()             # BSE NEWSIDs fetched on earlier polls
        self.nse_known_ids = set()             # NSE filing ids fetched on earlier polls
        self.checker = AmbiguousChecker()
        self.refresh_master()

    def refresh_master(self):
        self.master = load_scrip_master(self.nse)
        self.master_day = date.today()

        before = load_processed_scrips()
        self.processed = migrate_processed_keys(before, self.master)
        save_processed_scrips(self.processed)

        changed = self.processed - before
        to_isin = [k for k in changed if not k.startswith(("BSE-", "NSE-"))]
        without_isin = sorted(k for k in self.processed if k.startswith(("BSE-", "NSE-")))

        if to_isin:
            log.info(f"Migrated {len(to_isin)} processed key{'' if len(to_isin) == 1 else 's'} to ISIN format")
        if len(changed) > len(to_isin):
            log.info(f"Renamed {len(changed) - len(to_isin)} old-format keys to EXCHANGE-code format (ISIN unknown)")
        if without_isin:
            log.info(f"Processed keys still without ISIN: {', '.join(without_isin)}")

    def finish(self, fid: str):
        """Done with a filing for good: success, skip, or retries exhausted."""
        self.seen.add(fid)
        save_seen(self.seen)
        self.pending.pop(fid, None)

        if self.retries.pop(fid, None) is not None:
            save_retries(self.retries)

    def learn_isin(self, filing: dict):
        """NSE filings carry the ISIN — keep the symbol mapping for later."""
        if filing["exchange"] == "NSE" and filing["isin"]:
            if self.master["nse"].get(filing["code"]) != filing["isin"]:
                self.master["nse"][filing["code"]] = filing["isin"]
                save_json(SCRIP_MASTER_FILE, self.master)

def filing_kind(filing: dict) -> str:
    """"results", "ambiguous", "intimation" or "other" — headline only, no side effects."""
    if filing["category"] == "Board Meeting":
        return board_meeting_kind(filing["headline"])
    return "results"

def triage(filing: dict, state: ScannerState) -> str | None:
    """Filter and dedup pre-check. Returns "results" or "ambiguous" for a
    filing to process now, or None when it is skipped or already handled."""
    fid = filing["id"]

    if not fid or fid in state.seen:
        return None

    if filing["category"] not in {
        "Result",
        "Board Meeting"
    }:
        return None

    exchange, company = filing["exchange"], filing["company"]
    kind = filing_kind(filing)

    if kind in ("intimation", "other"):
        # The filter reads the full headline; only this log line is shortened
        shown = filing["headline"]
        shown = shown if len(shown) <= 120 else shown[:120] + "…"
        source = "+".join(filing["headline_fields"]) or "no headline field"
        log.info(
            f"→ Skip {exchange} board meeting ({kind}): {company} — "
            f"{shown!r} (headline from {source})"
        )
        state.finish(fid)
        return None

    state.learn_isin(filing)
    isin = lookup_isin(filing, state.master)

    # Cheap pre-check with the filing-date quarter; score_filing re-checks
    # with the quarter printed in the PDF
    filed_on = (filing["exchange_dt"] or datetime.now()).date()
    estimated_key = processed_key(filing, isin, reporting_quarter(filed_on))

    if estimated_key in state.processed:
        log.info(f"→ Skip {exchange} {company}: {estimated_key} already scored")
        state.finish(fid)
        return None

    return kind

def describe(filing: dict, isin) -> str:
    source = "+".join(filing["headline_fields"]) or "no headline field"
    return (
        f"{filing['exchange']}: {filing['company']} ({filing['code']}, "
        f"ISIN {isin or 'unknown'}, {filing['category']}; headline from {source})"
    )

def record_result(filing: dict, state: ScannerState, key: str | None):
    """Success → processed + seen; failure → retry on a later poll or give up."""
    fid = filing["id"]

    if key:
        state.processed.add(key)
        save_processed_scrips(state.processed)
        state.finish(fid)
        return

    attempts = state.retries.get(fid, 0) + 1

    if attempts > MAX_RETRIES:
        log.info(f"   Giving up after {attempts} attempts")
        state.finish(fid)
    else:
        state.retries[fid] = attempts
        save_retries(state.retries)
        state.pending[fid] = filing
        log.info(f"   Will retry next poll (retry {attempts}/{MAX_RETRIES})")

def record_skip(filing: dict, state: ScannerState, reason: str):
    log.info(f"   Skip: {reason} — not calling the model, not retrying")
    state.finish(filing["id"])

def handle_clear(filing: dict, state: ScannerState):
    isin = lookup_isin(filing, state.master)
    log.info(f"→ New {describe(filing, isin)}")

    try:
        key = process_filing(filing, isin, state.processed, state.nse)
    except SkipFiling as e:
        record_skip(filing, state, str(e))
        return
    except Exception:
        log.exception("   Unexpected error processing filing")
        key = None

    record_result(filing, state, key)

def handle_ambiguous(filing: dict, state: ScannerState):
    if state.checker.submit(filing):
        isin = lookup_isin(filing, state.master)
        log.info(f"→ New {describe(filing, isin)}; ambiguous outcome, queued for a PDF check")

def apply_ambiguous_result(result: dict, state: ScannerState):
    """Main thread: record a finished checker result and score it if it had results."""
    filing = result["filing"]
    fid = filing["id"]
    state.checker.in_flight.discard(fid)

    if fid in state.seen:      # e.g. the company's clear filing was scored meanwhile
        return

    isin = lookup_isin(filing, state.master)
    log.info(f"→ Checked ambiguous outcome {describe(filing, isin)}")

    if result["skip"]:
        log_timings(result["timings"])
        record_skip(filing, state, result["skip"])
        return

    key = None

    if result["fin"]:
        try:
            key = score_filing(filing, isin, result["fin"], state.processed, result["timings"])
        except Exception:
            log.exception("   Unexpected error scoring filing")
    else:
        log_timings(result["timings"])

    record_result(filing, state, key)

def drain_checks(state: ScannerState):
    """Apply every checker result that is ready, without waiting."""
    while True:
        try:
            result = state.checker.done.get_nowait()
        except queue.Empty:
            return
        apply_ambiguous_result(result, state)

def wait_for_checks(state: ScannerState, seconds: float):
    """Sleep until the next poll, applying checker results as they arrive."""
    deadline = time.monotonic() + seconds

    while True:
        remaining = deadline - time.monotonic()

        if remaining <= 0:
            return

        try:
            result = state.checker.done.get(timeout=remaining)
        except queue.Empty:
            return

        apply_ambiguous_result(result, state)

def run_cycle(state: ScannerState):
    """One poll. Clear result filings are handled first (oldest first), then
    finished ambiguous checks are applied, then new ambiguous outcomes are
    queued for the checker (oldest first)."""
    priority = {"results": 0, "ambiguous": 1}
    filings = sorted(
        poll_exchanges(state),     # already oldest first; sort is stable
        key=lambda f: priority.get(filing_kind(f), 2)
    )

    ambiguous = []

    for filing in filings:
        kind = triage(filing, state)

        if kind == "results":
            handle_clear(filing, state)
        elif kind == "ambiguous":
            ambiguous.append(filing)

    drain_checks(state)

    for filing in ambiguous:
        handle_ambiguous(filing, state)

def poll_exchanges(state: ScannerState) -> list:
    """New filings from both exchanges plus pending retries, oldest first,
    so whichever exchange published a result first is the one processed."""
    fresh = []

    try:
        filings, pages = fetch_bse_filings(state.bse_known_ids)
        log.info(
            f"BSE: {len(filings)} new announcements "
            f"(fetch OK, {pages} page{'' if pages == 1 else 's'} read)"
        )
        fresh += filings
    except ExchangeBlocked as e:
        warn_blocked("BSE", str(e))
    except Exception as e:
        log.warning(f"BSE: fetch FAILED ({e}) — announcements not read this poll")

    try:
        filings = fetch_nse_filings(state.nse)
        new = [f for f in filings if f["id"] not in state.nse_known_ids]
        state.nse_known_ids.update(f["id"] for f in filings)
        log.info(f"NSE: {len(new)} new announcements (fetch OK, {len(filings)} today)")
        fresh += filings
    except ExchangeBlocked as e:
        warn_blocked("NSE", str(e))
    except Exception as e:
        log.warning(f"NSE: fetch FAILED ({e}) — announcements not read this poll")

    by_id = {f["id"]: f for f in state.pending.values()}
    by_id.update({f["id"]: f for f in fresh})

    return sorted(by_id.values(), key=lambda f: f["exchange_dt"] or datetime.max)

def dump_samples():
    """Save one raw announcements response per exchange to verify field names."""
    nse = NseClient()
    today = date.today()

    targets = [
        (
            "BSE", "raw_bse_sample.json", BSE_HEADLINE_FIELDS,
            lambda: requests.get(
                BSE_ANN_URL.format(page=1, date=today.strftime("%Y%m%d")),
                headers=HEADERS,
                timeout=15
            ),
        ),
        (
            "NSE", "raw_nse_sample.json", NSE_HEADLINE_FIELDS,
            lambda: nse.get(NSE_ANN_URL.format(date=today.strftime("%d-%m-%Y"))),
        ),
    ]

    for exchange, path, headline_fields, fetch in targets:
        try:
            r = fetch()
        except Exception as e:
            log.error(f"{exchange}: request failed: {e}")
            continue

        try:
            body = r.json()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(body, f, indent=2, ensure_ascii=False)
        except ValueError:
            body = None
            with open(path, "w", encoding="utf-8") as f:
                f.write(r.text)

        log.info(
            f"{exchange}: HTTP {r.status_code}, saved to {path}"
            f"{' — ACCESS DENIED' if is_blocked_response(r) else ''}"
        )

        rows = body if isinstance(body, list) else (
            (body or {}).get("Table") or (body or {}).get("data") or []
        )

        if rows:
            log.info(f"   {len(rows)} rows; fields: {sorted(rows[0].keys())}")
            present = [f for f in headline_fields if f in rows[0]]
            log.info(f"   headline fields present: {present or 'NONE'} (expected {headline_fields})")

    for name, fetch in [
        ("BSE", fetch_bse_master),
        ("NSE", lambda: fetch_nse_master(nse)),
    ]:
        try:
            log.info(f"{name} scrip master: {len(fetch())} codes with ISIN")
        except Exception as e:
            log.error(f"{name} scrip master failed: {e}")

def main(argv=None):
    parser = argparse.ArgumentParser(description="PEAD result scanner for BSE + NSE")
    parser.add_argument(
        "--dump",
        action="store_true",
        help="save one raw BSE and NSE announcements response "
             "(raw_bse_sample.json, raw_nse_sample.json) and exit",
    )
    args = parser.parse_args(argv)

    if args.dump:
        dump_samples()
        return

    log.info("=" * 55)
    log.info("  PEAD Tool — BSE + NSE poller + LLM PDF reader")
    log.info(f"  Model           : {EXTRACTION_MODEL}")
    log.info(f"  Alert threshold : score >= {PEAD_THRESHOLD}")
    log.info("=" * 55)

    initialize_csv()

    state = ScannerState(NseClient())

    log.info(f"Loaded {len(state.seen)} previously seen filings")
    log.info(f"Loaded {len(state.processed)} processed company-quarters")
    log.info(f"Loaded {len(state.retries)} filings awaiting retry")

    try:
        while True:
            if date.today() != state.master_day:
                state.refresh_master()

            run_cycle(state)

            log.info(f"checker queue: {len(state.checker.in_flight)} pending")
            log.info(f"Sleeping {POLL_INTERVAL_SEC}s…\n")
            wait_for_checks(state, POLL_INTERVAL_SEC)
    finally:
        state.checker.stop()

if __name__ == "__main__":
    setup_file_logging()
    main()
