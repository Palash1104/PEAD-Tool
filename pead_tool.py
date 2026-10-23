"""
PEAD Score Tool — BSE + NSE Financial Results Monitor
=====================================================
Polls BSE and NSE every 30s → downloads result PDF → LLM extracts financials
→ PEAD score computed → Telegram alert if score >= PEAD_THRESHOLD

SETUP:
  pip install -r requirements.txt   (plus Tesseract + Poppler for OCR)

CONFIG (.env):
  AICREDITS_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  EXTRACTION_EFFORT (optional: low / medium / high thinking for Claude 5.x;
                     unset = the model's own default)
  The model is fixed to Claude Haiku 5.5 (EXTRACTION_MODEL below); an
  EXTRACTION_MODEL line in .env is ignored.

RUN:
  python pead_tool.py          poll forever
  python pead_tool.py --dump   save one raw BSE and NSE response, then exit
"""
import os
from dotenv import load_dotenv
import argparse
import base64
import hashlib
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
import sqlite3
import sys
import threading
from datetime import datetime, date, timedelta
from urllib.parse import quote
from openai import OpenAI

load_dotenv()

# Windows install locations; elsewhere (e.g. Linux test runs) use whatever is on PATH
TESSERACT_EXE = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
if os.path.exists(TESSERACT_EXE):
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_EXE
POPPLER_PATH = r"C:\poppler\Library\bin" if os.path.isdir(r"C:\poppler\Library\bin") else None

# ─────────────────────────────────────────────────────────────
# USER CONFIG
# ─────────────────────────────────────────────────────────────
AICREDITS_API_KEY  = os.getenv("AICREDITS_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
# Haiku 5.5 only (17 Oct 2026). Not configurable from .env: Haiku 4.5 is
# used only if the user explicitly asks for it.
EXTRACTION_MODEL   = "anthropic/claude-haiku-5.5"
EXTRACTION_EFFORT  = (os.getenv("EXTRACTION_EFFORT") or "").strip().lower() or None

if not all([
    AICREDITS_API_KEY,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
]):
    raise ValueError("Missing environment variables in .env file")

PEAD_THRESHOLD     = 35
POLL_INTERVAL_SEC  = 30
MAX_RETRIES        = 3      # extra attempts per failed filing, one per poll
# ...except a failed PDF download, retried after 1, 5 and 15 minutes: the file
# may not be up yet or the server is struggling (19 Oct 2026: UltraTech's NSE
# PDF failed four times within 90 seconds). A PDF that is cut off waits the
# same, in case it is uploaded again.
DOWNLOAD_RETRY_DELAYS_SEC = (60, 300, 900)
WAIT_BEFORE_RETRY = ("PDF download failed", "PDF is cut off")
BLOCKED_ALERT_SEC  = 3600   # Telegram at most hourly about an exchange blocking us

# Only results whose PDF says which quarter they are get scored. An earlier
# quarter (by period_end) is logged "old quarter, ignored", marked seen, never
# scored or alerted. No usable period_end, or column dates that don't line
# up, count as a failed extraction and are retried.
SCORE_FROM_QUARTER = "Q2FY27"

# Growth factors are skipped when last year's base is this small (Rs. Cr)
SMALL_BASE_PAT_CR  = 1
SMALL_BASE_REV_CR  = 10

SEEN_FILE = "seen.json"
RESULTS_CSV = "pead_results.csv"
PROCESSED_SCRIPS_FILE = "processed_scrips.json"   # "{ISIN}_{quarter}" keys
RETRIES_FILE = "retry_counts.json"                # filing id → failed attempts
FLAGGED_FILE = "flagged_scrips.json"              # company-quarters saved with a failed sanity check
SCRIP_MASTER_FILE = "scrip_master.json"           # BSE code / NSE symbol → ISIN
CHECKPOINT_FILE = "scan_checkpoint.json"          # exchange → when its last completed poll started

# After a restart the scanner reads from the day it last read each exchange
# (stop at 9 pm, restart at 4 pm next day → yesterday's late filings are
# read too), but never more than this many days back
MAX_RESUME_DAYS = 7
# ─────────────────────────────────────────────────────────────

LOG_DATEFMT   = "%H:%M:%S"
LOG_FILE      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pead_tool.log")
TERMINAL_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pead_terminal.log")
COMPANY_WIDTH = 30     # company column in filing lines

class LineFormatter(logging.Formatter):
    """"HH:MM:SS  STATUS  message", one fixed-column line per record.

    Filing and poll lines pass a status word (POLL, SKIP, CHECK, NONE, SCORE,
    ALERT, RETRY, OLD, FLAG, ERROR) as extra={"status": ...}; other lines show their
    level (INFO, WARN, ERROR, DEBUG). Non-filing lines from the checker thread
    are tagged [checker]. dashboard.py parses this layout.
    """
    LEVELS = {
        logging.DEBUG: "DEBUG",
        logging.INFO: "INFO",
        logging.WARNING: "WARN",
        logging.ERROR: "ERROR",
        logging.CRITICAL: "ERROR",
    }

    def format(self, record):
        status = getattr(record, "status", None)
        tag = "[checker] " if record.threadName == "checker" and not status else ""
        line = (
            f"{self.formatTime(record, LOG_DATEFMT)}  "
            f"{status or self.LEVELS.get(record.levelno, record.levelname):<5}  "
            f"{tag}{record.getMessage()}"
        )
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line

# This module logs everything down to DEBUG; each handler picks what it shows.
# The root logger stays at WARNING so libraries (httpx, pdfminer…) stay quiet.
log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)          # --verbose lowers it to DEBUG
console_handler.setFormatter(LineFormatter())
logging.getLogger().addHandler(console_handler)

def setup_file_logging(path: str = LOG_FILE) -> logging.Handler:
    """Also write every line, DEBUG included, to a UTF-8 file rotating at 5 MB
    with 3 backups.

    Called only when run as a script, so tests and compare_models.py that
    import this module don't write to the scanner's log.
    """
    handler = RotatingFileHandler(
        path,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(LineFormatter())
    handler.addFilter(lambda record: record.name != CONSOLE.name)   # summaries are terminal-only
    logging.getLogger().addHandler(handler)
    return handler

# ── TERMINAL ──────────────────────────────────────────────────
#
# pead_tool.log keeps the full LineFormatter layout (dashboard.py parses it).
# The terminal shows a cleaner view of the same records:
#   - one short, colour-coded line per filing that matters, no timing detail
#   - a "caught up" summary after the first poll, then one POLL line per poll
#     (new announcements per exchange and what they were)
#   - records logged with extra={"console": False} stay out of the terminal
#     (queued checks, vague outcomes without results, per-filing warnings)
# --verbose switches the terminal back to the full file layout, DEBUG included.
# pead_terminal.log is a copy of the clean terminal (no colours), whatever
# --verbose says; the dashboard's Scanner log panel shows it.

CONSOLE = logging.getLogger("pead_console")   # terminal-only summary lines
CONSOLE.setLevel(logging.INFO)
QUIET = {"console": False}                    # extra= for file-only records
CONSOLE_COMPANY_WIDTH = 28

ANSI = {
    "reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m",
    "green": "\033[1;32m", "cyan": "\033[36m", "yellow": "\033[33m", "red": "\033[1;31m",
}
STATUS_COLOURS = {
    "ALERT": "green", "SCORE": "cyan",
    "FLAG": "yellow", "RETRY": "yellow", "WARN": "yellow", "MISS": "yellow",
    "ERROR": "red",
}
DIM_STATUSES = {"SKIP", "OLD", "NONE"}         # handled, nothing to act on

DELAY_IN_TIMINGS = re.compile(r"exchange→(alert|scored) ((?:\d+h )?(?:\d+m )?\d+s)( \(catch-up\))?")
CONSOLE_REWRITES = [
    (re.compile(r"^(Q[1-4]FY\d{2}): old quarter per headline, ignored.*$"), r"\1 result, skipped by its title"),
    (re.compile(r"^((?:Q[1-4]|H[12])FY\d{2}): old quarter, ignored.*$"), r"\1 result, ignored"),
    (re.compile(r"^\S+_(?:Q[1-4]|H[12])FY\d{2} already scored.*$"), "already scored this quarter"),
    (re.compile(r"numbers don't add up \(.*\) · no alert"), "numbers don't add up · not alerted"),
    (re.compile(r"^no results table found · headline: .*$"), "no results table in the PDF"),
    (re.compile(r"^attachment is not a PDF.*$"), "attachment isn't a PDF"),
    (re.compile(r"\s{2,}"), "  "),
]

def console_text(status: str, text: str) -> str:
    """The terminal's version of a filing line: no timing breakdown, shorter
    wording, and for an alert how long after the filing it went out."""
    main, _, timing = text.partition("  ⏱ ")
    for pattern, repl in CONSOLE_REWRITES:
        main = pattern.sub(repl, main)

    delay = DELAY_IN_TIMINGS.search(timing)
    if delay and status == "ALERT":
        main += f"  ·  {delay.group(2)} after filing"
    if delay and delay.group(3):
        main += "  (catch-up)"

    return main if len(main) <= 110 else main[:109] + "…"

class ConsoleFormatter(logging.Formatter):
    """HH:MM:SS  STATUS  EXCH  Company                       what happened"""

    def __init__(self, colour: bool = False):
        super().__init__()
        self.colour = colour

    def paint(self, text: str, *styles: str) -> str:
        if not self.colour or not styles:
            return text
        return "".join(ANSI[s] for s in styles) + text + ANSI["reset"]

    def format(self, record):
        stamp = self.paint(self.formatTime(record, LOG_DATEFMT), "dim")

        if record.name == CONSOLE.name:        # banner, summaries, POLL lines, rules
            styles = [s for s in [getattr(record, "style", None)] if s]
            word = getattr(record, "word", None)
            head = self.paint(f"{word:<5}", *styles) + "  " if word else ""
            return f"{stamp}  {head}" + self.paint(record.getMessage(), *styles)

        status = getattr(record, "status", None) or LineFormatter.LEVELS.get(record.levelno, record.levelname)
        colour = STATUS_COLOURS.get(status) or ("dim" if status in DIM_STATUSES else None)
        word = self.paint(f"{status:<5}", colour) if colour else f"{status:<5}"

        exchange = getattr(record, "exchange", None)
        if exchange:
            company = short_company(record.company, CONSOLE_COMPANY_WIDTH)
            line = f"{exchange:<3}  {company}  {console_text(status, record.text)}"
        else:
            line = record.getMessage()

        if status in DIM_STATUSES:
            line = self.paint(line, "dim")
        elif status == "ALERT":
            line = self.paint(line, "bold")

        return f"{stamp}  {word}  {line}"

def console_colours_supported(stream) -> bool:
    """ANSI colours if the terminal can show them (turns them on in Windows
    consoles); off when NO_COLOR is set or output is redirected."""
    if os.environ.get("NO_COLOR") or not getattr(stream, "isatty", lambda: False)():
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-12 if stream is sys.stderr else -11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))   # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        return False

def _console_visible(record) -> bool:
    return getattr(record, "console", True)

def _not_poll_summary(record) -> bool:
    """--verbose already prints the file's two POLL lines; skip the short one."""
    return not (record.name == CONSOLE.name and getattr(record, "word", None) == "POLL")

def configure_console(verbose: bool):
    """Clean terminal by default; --verbose shows the full log layout."""
    console_handler.removeFilter(_console_visible)
    console_handler.removeFilter(_not_poll_summary)
    if verbose:
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(LineFormatter())
        console_handler.addFilter(_not_poll_summary)
    else:
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(ConsoleFormatter(console_colours_supported(console_handler.stream)))
        console_handler.addFilter(_console_visible)

def setup_terminal_log(path: str = TERMINAL_LOG_FILE) -> logging.Handler:
    """Copy of the clean terminal, without colours, for the dashboard's
    Scanner log panel: same lines, same layout. UTF-8, 1 MB with 2 backups.
    Called only when run as a script, like setup_file_logging."""
    handler = RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8")
    handler.setLevel(logging.INFO)
    handler.setFormatter(ConsoleFormatter(False))
    handler.addFilter(_console_visible)
    logging.getLogger().addHandler(handler)
    return handler

def say(text: str, style: str | None = None, word: str | None = None):
    """A terminal-only line (never written to pead_tool.log; pead_terminal.log
    gets it). word fills the status column, e.g. "POLL"."""
    extra = {k: v for k, v in (("style", style), ("word", word)) if v}
    CONSOLE.info(text, extra=extra or None)

def when_text(moment: datetime, now: datetime | None = None) -> str:
    """"21:01 today", "21:01 yesterday", "21:01 on Mon 05 Oct"."""
    day = (now or datetime.now()).date()
    if moment.date() == day:
        return f"{moment:%H:%M} today"
    if moment.date() == day - timedelta(days=1):
        return f"{moment:%H:%M} yesterday"
    return f"{moment:%H:%M} on {moment:%a %d %b}"

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

def load_flagged() -> set:
    return set(load_json(FLAGGED_FILE, []))

def save_flagged(data: set):
    save_json(FLAGGED_FILE, sorted(data))

def load_retries() -> dict:
    return load_json(RETRIES_FILE, {})

def save_retries(retries: dict):
    save_json(RETRIES_FILE, retries)

def load_checkpoint() -> dict:
    """exchange → datetime its last completed poll started (bad entries dropped)."""
    marks = {}
    for exchange, value in (load_json(CHECKPOINT_FILE, {}) or {}).items():
        try:
            marks[exchange] = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            continue
    return marks

def save_checkpoint(marks: dict):
    save_json(CHECKPOINT_FILE, {ex: dt.isoformat(timespec="seconds") for ex, dt in marks.items()})

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
    "filing_url",   # the result PDF on the exchange

    "period_end",   # the current quarter column's date, from the PDF
    "quarter",      # e.g. Q2FY27, or UNKNOWN when the PDF never said
    "basis",        # consolidated / standalone
    "unit",         # the table's unit before conversion to crores
    "check",        # blank, or why the figures failed the sanity check (never alerted)
    "eps_pq",       # previous quarter's EPS (the two EPS columns above predate it)
]

# Columns added after the CSV existed, with the value older rows get
CSV_ADDED_COLUMNS = {
    "exchange": "BSE",      # every row before NSE support came from BSE
    "filing_url": "",       # not recorded before this column existed
    "period_end": "",       # the four below weren't recorded before 2026-10-05
    "quarter": "",
    "basis": "",
    "unit": "",
    "check": "",            # sanity check added 2026-10-07
    "eps_pq": "",           # extracted all along, but only saved since 2026-10-18
}

def initialize_csv():

    if not os.path.exists(RESULTS_CSV):
        with open(RESULTS_CSV, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_HEADER)
        return

    # Older files lack columns added since — append them to every row
    try:
        with open(RESULTS_CSV, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))

        if not rows:
            return

        missing = [c for c in CSV_ADDED_COLUMNS if c not in rows[0]]

        if not missing:
            return

        rows[0].extend(missing)

        for row in rows[1:]:
            if row:
                row.extend(CSV_ADDED_COLUMNS[c] for c in missing)

        tmp = RESULTS_CSV + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)
        os.replace(tmp, RESULTS_CSV)

        log.info(f"Added {', '.join(missing)} column{'' if len(missing) == 1 else 's'} to {RESULTS_CSV}")

    except OSError as e:
        log.warning(f"Could not add new columns to {RESULTS_CSV}: {e}")

def save_result_csv(filing, score, fin, quarter):

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
                filing["attachment_url"],

                fin.get("period_end") or "",
                quarter,
                fin.get("basis") or "",
                fin.get("unit") or "",
                fin.get("check") or "",
                eps[1],
            ])
    except OSError as e:
        # e.g. CSV open in Excel — don't let logging block the alert
        log.warning(f"Could not write CSV row: {e}")

# ── EXCHANGES ────────────────────────────────────────────────
#
# Both exchanges are normalised into the same "filing" dict:
#   exchange, id ("BSE:<NEWSID>" / "NSE:<seq_id>"), company, code (scrip code
#   or symbol), isin (NSE only), category ("Result" / "Board Meeting" / other),
#   headline, headline_fields, attachment_url, exchange_dt

class ExchangeBlocked(Exception):
    """Access Denied / 401 / 403 — must never be read as 'no announcements'."""

class SkipFiling(Exception):
    """The filing can never yield a score (no results table, not a PDF, old
    quarter, figures the model couldn't find): skip it for good, no retry.
    status is its terminal status word (NONE for no results table, OLD for an
    old quarter, MISS when the model answered without revenue or PAT, else
    SKIP); model_called says whether the model had already read it."""

    def __init__(self, reason: str, model_called: bool = False, status: str = "SKIP"):
        super().__init__(reason)
        self.model_called = model_called
        self.status = status

class RetryFiling(Exception):
    """A failure worth retrying on a later poll: the download or the model call
    failed, the reply wasn't JSON, or the column dates don't line up. Not when
    the model answered without the figures: the same PDF reads the same."""

class QuarterUnknown(RetryFiling):
    """The PDF never said which quarter it is (no usable period_end). Retried like
    any failed extraction; if the last attempt is no better, the figures are saved
    with quarter UNKNOWN and never alerted. fin is the extraction."""

    def __init__(self, reason: str, fin: dict):
        super().__init__(reason)
        self.fin = fin

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
    "?pageno={page}&strCat=-1&strPrevDate={from_date}"
    "&strScrip=&strSearch=P&strToDate={to_date}"
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
    "?index=equities&from_date={from_date}&to_date={to_date}"
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
            log.debug(f"NSE returned {r.status_code}, refreshing session cookies")
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

def fetch_bse_page(page: int, from_day: date, to_day: date | None = None) -> list:
    to_day = to_day or from_day
    r = requests.get(
        BSE_ANN_URL.format(page=page, from_date=from_day.strftime("%Y%m%d"), to_date=to_day.strftime("%Y%m%d")),
        headers=HEADERS,
        timeout=15
    )

    if is_blocked_response(r):
        raise ExchangeBlocked(f"HTTP {r.status_code} on announcements page {page}")

    r.raise_for_status()
    return r.json().get("Table") or []

def empty_day_is_wrong(day: date, now: datetime | None = None) -> bool:
    """Whether an exchange answering "no announcements" for `day` can't be
    true: any past weekday, or today on a weekday from 10 am. Both exchanges
    get hundreds a day. BSE's API sometimes answers with an empty list (7 Oct
    2026 21:12, 9 Oct 2026 20:27); taken at face value on 9 Oct it moved the
    checkpoint past all of 8 Oct, which was never read."""
    now = now or datetime.now()
    if day.weekday() >= 5:          # weekends can be genuinely quiet
        return False
    return day < now.date() or now.hour >= 10

EMPTY_ANSWERS_BELIEVED = 10     # consecutive empty answers for one day before it's taken as quiet (a holiday)
_empty_answers = {}             # (exchange, day) → consecutive empty answers

def unbelievable_empty(exchange: str, day: date, wrong: bool) -> bool:
    """An empty answer (for the range starting `day`) that should fail the
    read: `wrong` per empty_day_is_wrong, and not yet EMPTY_ANSWERS_BELIEVED
    times in a row (~5 minutes of polls). After that the day is taken as
    genuinely quiet, so a holiday can't stall the catch-up for good."""
    if not wrong:
        return False
    n = _empty_answers.get((exchange, day), 0) + 1
    _empty_answers[(exchange, day)] = n
    if n == EMPTY_ANSWERS_BELIEVED:
        log.warning(f"{exchange}: {day:%d %b} came back empty {n} times in a row; taking it as a quiet day (holiday?)")
    return n < EMPTY_ANSWERS_BELIEVED

def fetch_bse_filings(known_ids: set, since: date | None = None) -> tuple:
    """New BSE announcements from `since` (default today) to today, one day
    at a time (each day ~25 pages at most, so a long catch-up never hits the
    page cap). Returns (new filings, pages read).

    known_ids holds the NEWSIDs fetched on earlier polls; it is updated only
    if every page was read, so a failed read is repeated in full next poll.
    Raises if any page fails, or a day's first page comes back empty when it
    can't be (empty_day_is_wrong).
    """
    today = date.today()
    day = min(since or today, today)
    fresh_ids, filings, pages = set(), [], 0

    while day <= today:
        got, read = fetch_bse_day(day, known_ids, fresh_ids)
        filings += got
        pages += read
        day += timedelta(days=1)

    known_ids.update(fresh_ids)
    return filings, pages

def fetch_bse_day(day: date, known_ids: set, fresh_ids: set) -> tuple:
    """One day's new BSE announcements, newest first, paging until a page has
    no new rows or the last page (TotalPageCnt). New NEWSIDs are added to
    fresh_ids. Any page failing raises: keeping the pages before it would let
    the checkpoint move past the rows after it, which would never be read."""
    filings = []

    for page in range(1, BSE_MAX_PAGES + 1):
        if page > 2:
            time.sleep(0.5)   # only a startup backlog reads this deep; go gently

        try:
            rows = fetch_bse_page(page, day, day)
        except ExchangeBlocked:
            raise
        except Exception as e:
            if page == 1:
                raise
            raise RuntimeError(f"{day:%d %b} page {page} failed: {e}") from e

        if page == 1:
            if not rows and unbelievable_empty("BSE", day, empty_day_is_wrong(day)):
                raise RuntimeError(f"empty answer for {day:%d %b}, not believable")
            if rows:
                _empty_answers.pop(("BSE", day), None)

        new_rows = []
        for row in rows:
            rid = str(row.get("NEWSID") or row.get("DT_TM") or "")
            if rid not in known_ids and rid not in fresh_ids:
                fresh_ids.add(rid)
                new_rows.append(row)

        if not new_rows:
            break

        filings += [normalise_bse(row) for row in new_rows]

        try:
            if page >= int(rows[0].get("TotalPageCnt") or BSE_MAX_PAGES):
                break
        except (TypeError, ValueError):
            pass
    else:
        log.warning(f"BSE: stopped at the {BSE_MAX_PAGES}-page cap for {day:%d %b}, older filings may be missed")

    return filings, page

def fetch_nse_filings(nse: NseClient, since: date | None = None) -> list:
    """NSE equity announcements from `since` (default today) to today."""
    today = date.today()
    first_day = min(since or today, today)
    r = nse.get(NSE_ANN_URL.format(from_date=first_day.strftime("%d-%m-%Y"), to_date=today.strftime("%d-%m-%Y")))

    if is_blocked_response(r):
        raise ExchangeBlocked(f"HTTP {r.status_code} on announcements")

    r.raise_for_status()
    data = r.json()
    rows = data if isinstance(data, list) else data.get("data") or []

    if rows:
        _empty_answers.pop(("NSE", first_day), None)
    else:
        wrong = any(empty_day_is_wrong(first_day + timedelta(days=n)) for n in range((today - first_day).days + 1))
        if unbelievable_empty("NSE", first_day, wrong):
            raise RuntimeError(f"empty answer from {first_day:%d %b}, not believable")

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
        log.warning(f"PDF download failed: {e}", extra=QUIET)
        return None

_last_blocked_alert = {}   # exchange → time of last Telegram warning

def warn_blocked(exchange: str, detail: str, console: bool = True):
    log.error(
        f"🚫 {exchange} BLOCKED ({detail}) — its announcements were NOT read "
        f"this poll; this is not 'zero announcements'",
        extra={"console": console},
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

# Quarter-end dates spelled out in a headline ("31.03.2025", "30th June 2026",
# "September 30th, 2026", "30-Jun-2026"), so an old quarter can be skipped
# before the PDF is downloaded or the model is called
_MONTH = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
          r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
MONTH_NUMBERS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
HEADLINE_DATE_RES = [
    ("dmy", re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})\b")),
    ("ymd", re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")),
    ("d-mon-y", re.compile(rf"\b(\d{{1,2}})\s*(?:st|nd|rd|th)?[\s.-]*(?:of\s+)?{_MONTH}\b[\s,.-]*(\d{{4}})\b")),
    ("mon-d-y", re.compile(rf"\b{_MONTH}[\s.-]*(\d{{1,2}})\s*(?:st|nd|rd|th)?\b[\s,.-]*(\d{{4}})\b")),
]
QUARTER_ENDS = {(3, 31), (6, 30), (9, 30), (12, 31)}

def headline_period_ends(headline: str) -> list:
    """Every quarter-end date (31 Mar, 30 Jun, 30 Sep, 31 Dec) written in the headline."""
    text = (headline or "").lower()
    found = set()

    for kind, pattern in HEADLINE_DATE_RES:
        for groups in pattern.findall(text):
            try:
                if kind == "dmy":
                    d, m, y = int(groups[0]), int(groups[1]), int(groups[2])
                elif kind == "ymd":
                    y, m, d = int(groups[0]), int(groups[1]), int(groups[2])
                elif kind == "d-mon-y":
                    d, m, y = int(groups[0]), MONTH_NUMBERS[groups[1][:3]], int(groups[2])
                else:
                    m, d, y = MONTH_NUMBERS[groups[0][:3]], int(groups[1]), int(groups[2])
                if y < 100:
                    y += 2000
                if (m, d) in QUARTER_ENDS:
                    found.add(date(y, m, d))
            except (ValueError, KeyError):
                continue

    return sorted(found)

def headline_old_quarter(headline: str) -> str | None:
    """The quarter a headline names, if it is before SCORE_FROM_QUARTER.

    Uses the latest quarter-end date in the headline, so one that also
    mentions the current quarter (or a 30 Sep meeting date) is never skipped.
    None when the headline names no quarter-end date or a current one.
    """
    ends = headline_period_ends(headline)
    if not ends:
        return None

    quarter = quarter_label(ends[-1])
    return quarter if quarter_index(quarter) < quarter_index(SCORE_FROM_QUARTER) else None

# ── QUARTERS, SCRIP MASTER & DEDUP KEYS ──────────────────────

def quarter_label(period_end: date) -> str:
    """Indian FY quarter containing this date, e.g. 30 Jun 2026 → Q1FY27."""
    m, y = period_end.month, period_end.year
    if m >= 4:
        return f"Q{(m - 4) // 3 + 1}FY{(y + 1) % 100:02d}"
    return f"Q4FY{y % 100:02d}"

# SME companies report every six months (Poojaa Precision, 22 Oct 2026: half
# years to Sep 2026, Mar 2026 and Sep 2025, no quarter anywhere). Their
# results are scored like a quarter's, with the previous half year in the
# previous-quarter slot, under a half-year label: H1 = Apr–Sep, H2 = Oct–Mar.

def half_label(period_end: date) -> str:
    """Indian FY half year ending in this date's quarter: 30 Sep 2026 → H1FY27,
    31 Mar 2027 → H2FY27."""
    quarter = quarter_label(period_end)
    return ("H1" if quarter[1] in "12" else "H2") + quarter[2:]

def half_ending(quarter: str) -> str | None:
    """The half year that ends with this quarter: Q2FY27 → H1FY27, Q4FY27 →
    H2FY27; None for Q1 and Q3."""
    half = {"Q2": "H1", "Q4": "H2"}.get(quarter[:2])
    return half + quarter[2:] if half else None

def half_yearly(fin: dict | None) -> bool:
    """The model read half-year columns (an SME company's results)."""
    return bool(fin) and fin.get("period_months") == 6

def period_label(period_end: date, fin: dict | None) -> str:
    """Q2FY27, or H1FY27 for half-yearly results."""
    return half_label(period_end) if half_yearly(fin) else quarter_label(period_end)

def reporting_quarter(filed_on: date) -> str:
    """Estimate from the filing date (results are filed within three months of
    quarter end, e.g. filed Sep 2026 → Q1FY27). Only the cheap dedup pre-check
    in triage uses it; scoring never does."""
    m, y = filed_on.month - 3, filed_on.year
    if m < 1:
        m, y = m + 12, y - 1
    return quarter_label(date(y, m, 1))

def pdf_quarter(fin: dict, filed_on: date) -> str | None:
    """Quarter from the PDF's period_end (a half year such as H1FY27 for
    half-yearly results), or None if missing or implausible (it must fall
    0–400 days before the filing date)."""
    period_end = fin.get("period_end")

    if period_end:
        ended = date.fromisoformat(period_end)

        if timedelta(0) <= filed_on - ended <= timedelta(days=400):
            return period_label(ended, fin)

    return None

def old_quarter(period_end: str | None, filed_on: date) -> str | None:
    """The quarter of period_end if it is before SCORE_FROM_QUARTER, else None.

    Unlike pdf_quarter there is no 400-day limit: a result for the year ended
    31.03.2025 filed in Oct 2026 is old, not "quarter unknown". A date after
    the filing date is implausible and never counts as old.
    """
    if not period_end:
        return None

    ended = date.fromisoformat(period_end)
    if ended > filed_on:
        return None

    quarter = quarter_label(ended)
    return quarter if quarter_index(quarter) < quarter_index(SCORE_FROM_QUARTER) else None

def quarter_unknown_reason(fin: dict, filed_on: date) -> str:
    period_end = fin.get("period_end")
    if not period_end:
        return "quarter unknown (no period_end)"
    return f"quarter unknown (period_end {period_end} implausible for a filing on {filed_on})"

def column_dates_problem(fin: dict) -> str | None:
    """Why the three quarterly columns aren't current / 3 months earlier / 12
    months earlier (6 / 12 for half-yearly results), or None if they line up.
    Compares year and month only, so 30 vs 31 of the month doesn't matter.
    period_end must already be set."""
    current, prev, ly = fin.get("period_end"), fin.get("prev_period_end"), fin.get("ly_period_end")
    step, previous = (6, "previous-half-year") if half_yearly(fin) else (3, "previous-quarter")

    missing = [name for name, value in [(previous, prev), ("last-year", ly)] if not value]
    if missing:
        return f"{' and '.join(missing)} column date missing"

    def months(iso: str) -> int:
        d = date.fromisoformat(iso)
        return d.year * 12 + d.month

    if months(current) - months(prev) != step:
        return f"previous column {prev} isn't {step} months before {current}"

    if months(current) - months(ly) != 12:
        return f"last-year column {ly} isn't 12 months before {current}"

    return None

def quarter_index(label: str) -> int:
    """Sortable number for "Q2FY27"-style labels. A half year counts as the
    quarter it ends with: H1FY27 = Q2FY27, H2FY27 = Q4FY27."""
    m = re.fullmatch(r"Q([1-4])FY(\d{2})|H([12])FY(\d{2})", label)
    if not m:
        raise ValueError(f"bad quarter label {label!r}")
    if m.group(3):
        return int(m.group(4)) * 4 + int(m.group(3)) * 2
    return int(m.group(2)) * 4 + int(m.group(1))

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
            f"{len(master[name])} known",
            extra=QUIET,
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

# A company's BSE and NSE copies normally collide on the ISIN key. When the
# ISIN lookup misses for one copy (17 Oct 2026: E2E Networks and Shoppers Stop
# were scored once from NSE and again from BSE), the name catches it: both
# exchanges print the same name apart from Ltd/Limited, "The" and punctuation.
_NAME_NOISE = re.compile(r"\b(?:limited|ltd|the)\b")
QUARTER_RE = re.compile(r"Q[1-4]FY\d{2}|H[12]FY\d{2}")     # a quarter, or an SME company's half year

def company_key(name: str) -> str:
    """A company's name as both exchanges print it: "E2E Networks Ltd" and
    "E2E Networks Limited" give the same key, "e2e networks"."""
    text = re.sub(r"[^a-z0-9]+", " ", (name or "").lower().replace("&", " and "))
    return " ".join(_NAME_NOISE.sub(" ", text).split())

def scored_names_from_csv() -> set:
    """(company_key, quarter) of every company-quarter in RESULTS_CSV that was
    scored: rows whose numbers didn't add up (FLAG) and UNKNOWN quarters leave
    the company-quarter open, as they do for the ISIN key."""
    names = set()
    try:
        with open(RESULTS_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                company, quarter = company_key(row.get("company")), (row.get("quarter") or "").strip()
                if company and QUARTER_RE.fullmatch(quarter) and not (row.get("check") or "").strip():
                    names.add((company, quarter))
    except OSError:
        pass
    return names

KEY_RE = re.compile(r"(?:(BSE|NSE)-)?(.+)_(Q[1-4]FY\d{2}|H[12]FY\d{2})")

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

EXTRACTION_PROMPT = """These pages come from a quarterly financial result PDF filed by an Indian listed company on BSE/NSE. Each page is given either as extracted text or, for scanned pages, as an image; read the figures from whichever form you get.

Use the CONSOLIDATED results if the text contains a consolidated results table; otherwise use the STANDALONE results.
Use only the individual quarter columns (NOT year-to-date, half-year, nine-month or full year columns), except for a half-yearly table (see HALF-YEARLY RESULTS).
The result table has 3 quarterly columns:
  Column 1: Current quarter (most recent)
  Column 2: Previous quarter (immediately preceding)
  Column 3: Same quarter last year (year-over-year)

HALF-YEARLY RESULTS: companies on the SME platform report every six months, so their table has half-year columns and NO quarter columns at all. Only for such a table (never when the table has quarter columns), use the half-year columns instead:
  Column 1: Current half year (the most recent six months)
  Column 2: Previous half year (the six months immediately before it)
  Column 3: Same half year last year
and read every "quarter" below as "half year".

Extract these values exactly as printed, in the table's own unit (do NOT convert units):
- revenue_from_operations: Revenue from operations / Net Sales (NOT total income)
- total_income: Total income including other income
- ebitda: EBITDA or Operating Profit if explicitly stated as a line item (else set null)
- finance_cost: Finance Cost / Borrowing Cost
- depreciation: Depreciation and Amortisation Expense
- employee_expense: Employee Benefit Expense
- other_expenses: Other Expenses
- total_expenses: Total Expenses
- exceptional_items: whatever the table prints between Total Expenses and Profit Before Tax — exceptional or extraordinary items, share of profit/(loss) of associates and joint ventures (add them up if there are several); a gain positive, a loss negative; null if there is none
- pbt: Profit Before Tax
- pat: Profit After Tax for the quarter
- basic_eps: Basic Earnings Per Share in Rs.

Also report:
- basis: which results you used, "consolidated" or "standalone"
- unit: the unit the table states for amounts, one of "crores", "lakhs", "millions", "thousands", "rupees"
- period_end: the "quarter ended" date of the current quarter column (Column 1), as YYYY-MM-DD
- prev_period_end: the "quarter ended" date of the previous quarter column (Column 2), as YYYY-MM-DD
- ly_period_end: the "quarter ended" date of the same quarter last year column (Column 3), as YYYY-MM-DD
  (use null for any of these dates the table doesn't show)
- period_months: 3 for quarter columns, 6 for half-year columns (HALF-YEARLY RESULTS)

BANKS: a bank's results table has no "Revenue from operations"; it shows Interest earned, Interest expended, Operating profit before provisions and contingencies, and Provisions and contingencies. For a bank's table use:
- revenue_from_operations: Interest earned (its total)
- total_income: Total income
- ebitda: Operating profit before provisions and contingencies
- finance_cost: Interest expended
- depreciation: null unless the table prints it as its own line
- employee_expense: Payments to and provisions for employees
- other_expenses: Other operating expenses
- total_expenses: Total expenditure (interest expended plus operating expenses), NOT including provisions and contingencies
- exceptional_items: Provisions (other than tax) and contingencies as a negative number, plus any exceptional items printed before profit before tax
- pbt: Profit from ordinary activities before tax
- pat: Net profit for the quarter (consolidated results: after minority interest and share of associates, if the table shows them)
- basic_eps: Basic EPS for the quarter, often listed under Analytical Ratios (not annualised)

Return ONLY a JSON object, no explanation, no markdown:
{
"basis":"consolidated|standalone",
"unit":"crores|lakhs|millions|thousands|rupees",
"period_end":"YYYY-MM-DD",
"prev_period_end":"YYYY-MM-DD",
"ly_period_end":"YYYY-MM-DD",
"period_months":3,
"revenue_from_operations":[cq,pq,ly],
"total_income":[cq,pq,ly],
"ebitda":[cq,pq,ly],
"finance_cost":[cq,pq,ly],
"depreciation":[cq,pq,ly],
"employee_expense":[cq,pq,ly],
"other_expenses":[cq,pq,ly],
"total_expenses":[cq,pq,ly],
"exceptional_items":[cq,pq,ly],
"pbt":[cq,pq,ly],
"pat":[cq,pq,ly],
"basic_eps":[cq,pq,ly]
}

Rules:
- null for any value not found with confidence
- A line the table prints with a dash (- or –) or "nil" in a column is 0 there, not null
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
    "exceptional_items",
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

MAX_PDF_PAGES       = 25    # read first (text layer, OCR where needed)
LONG_PDF_PAGES      = 60    # no titled results table in those → read on up to here
AMBIGUOUS_OCR_PAGES = 8     # OCR budget for outcomes whose headline names no results
MIN_TEXT_CHARS      = 500   # whole PDF below this → treat as scanned, OCR it all
LOW_TEXT_PAGE_CHARS = 200   # no table in text layer → OCR pages below this
SCAN_IMAGE_COVER    = 0.85  # a page this much covered by one image is a scan, whatever text it carries
HEADING_LINES       = 20    # a result table's title sits in its page's first non-empty lines

# Scanned pages. OCR only has to find which pages hold the results table
# (keywords, not exact digits), so it runs at a low resolution. The selected
# scanned pages then go to the model as images, which it reads far more
# accurately than Tesseract's digits (7 Oct 2026: Tiaan Consumer and Golkonda
# Aluminium came back with impossible figures from OCR text).
LOCATE_OCR_DPI       = 150   # page finding only
FALLBACK_OCR_DPI     = 250   # full-quality OCR text, used only when images can't be sent
IMAGE_DPI            = 150   # scanned result pages rendered for the model
IMAGE_MAX_EDGE       = 1568  # Claude scales larger images down to this anyway
SEND_SCANS_AS_IMAGES = True  # False → send Tesseract text instead (the old behaviour)
_images_rejected     = False # set if the provider refuses images; then text for the session

# A page counts as a results table if it shows at least two of these kinds
# of line item. Each kind accepts the wording different formats use:
# companies ("Revenue from operations", "Profit before tax"), NBFCs ("Total
# Revenue", "Profit/(Loss) before Tax", "Earning per equity share" — Gowra
# Leasing, 26 Sep 2026, matched only one of the old fixed phrases) and banks
# ("Interest earned", "Interest expended", "Operating profit before
# provisions"). Real text layers are noisy: ESDS and Purple Style show two.
TABLE_MARKERS = {
    "income": re.compile(
        r"revenue\s+from\s+operations|[il1]ncome\s+from\s+operations|total\s+[il1]ncome"
        r"|total\s+revenue|interest\s+(?:earned|income)|net\s+sales"
    ),
    "expenses": re.compile(
        r"total\s+expenses|finance\s+costs?|interest\s+expended|operating\s+expenses"
    ),
    "profit before tax": re.compile(
        r"(?:profit|loss)\s*(?:/\s*\(?\s*(?:profit|loss)\s*\)?\s*)?"
        r"(?:from\s+ordinary\s+activities\s+)?before\s+(?:[a-z&]+\s+){0,3}tax"
        r"|operating\s+profit\s+before\s+provisions"
    ),
    "net profit": re.compile(
        r"net\s+profit|profit\s*(?:/\s*\(?\s*loss\s*\)?\s*)?(?:after\s+tax|for\s+the\s+(?:period|quarter|year))"
    ),
    "eps": re.compile(r"earnings?\s+per\s+(?:equity\s+)?share|\beps\b"),
}

# A page headed like this is a cash flow statement or balance sheet, not
# the results table, unless its heading also says "results". Checked before
# RESULT_HEADINGS: "Unaudited Consolidated Statement of Cash Flows" matches
# the consolidated pattern (Tinna Rubber, 22 Oct 2026: both cash flow
# statements were taken for results tables, the real ones never sent).
NOT_TABLE_HEADINGS = re.compile(r"cash\s+flow|assets\s+and\s+liabilities|balance\s+sheet")

# An auditor's review report quotes a few figures in its prose ("total
# revenues of Rs. 1,123.67 lakhs ... net profit after tax"), enough to pass
# the line-item check (Tinna Rubber, page 13). A page with the auditor in its
# heading and fewer than AUDITOR_PAGE_AMOUNTS amounts is such a report; a
# results table carrying an auditor's stamp still has a grid of figures.
AUDITOR_HEADINGS = re.compile(
    r"chartered\s+accountants|independent\s+auditor|review\s+report|emphasis\s+of\s+matter|other\s+matters?\b"
    r"|reviewed\s+by\s+us|the\s+statement\s+includes"      # a report's later pages (Bansal Wire, 23 Oct 2026)
)
AMOUNT_RE = re.compile(r"\d[\d,]*\.\d+|\d{1,3}(?:,\d{2,3})+")
AUDITOR_PAGE_AMOUNTS = 30

# Pages about the results rather than the statement itself (23 Oct 2026):
# notes, whose title names the results (Coforge's segment table on page 15
# and Le Travenues' notes on page 8 were sent as the consolidated table), and
# press releases (Dr. Reddy's and Emmvee, page 2). The first title-like line
# of a notes page starts with "Notes" / "Explanatory notes", that of a
# segment page with "(Consolidated unaudited) Segment …" (Emmvee, page 44).
TITLE_LINE = re.compile(r"statement|results|notes?\b|segment")
NOTES_OR_SEGMENT_TITLE = re.compile(
    r"^\W*(?:selected\s+)?(?:explanatory\s+)?notes?\b"
    r"|^\W*(?:(?:un[-\s]?)?audited\s+|c?onsolidated\s+|s?tandalone\s+)*segment"
)
PRESS_RELEASE_HEADINGS = re.compile(
    r"today\s+announced|(?:press|media|earnings|investor)\s+release|\bhighlights\b"
)

# Checked against the top of each table page, in order of preference.
# "Un-audited" is often hyphenated (Tinna Rubber's "CONSOLIDATED UN-AUDITED"),
# and OCR can lose a title's first letter against a table border (Coforge's
# "onsolidated Financial Results").
RESULT_HEADINGS = [
    ("consolidated", re.compile(
        r"(?:un[-\s]?)?audited\s+c?onsolidated|c?onsolidated\s+(?:un[-\s]?)?audited"
        r"|c?onsolidated\s+(?:financial\s+)?results|c?onsolidated\s+statement"
    )),
    ("standalone", re.compile(
        r"(?:un[-\s]?)?audited\s+s?tandalone|s?tandalone\s+(?:un[-\s]?)?audited"
        r"|s?tandalone\s+(?:financial\s+)?results|s?tandalone\s+statement"
    )),
    ("generic", re.compile(
        r"results\s+for\s+the\s+(?:quarter|half\s+year|period)"
        r"|(?:un[-\s]?)?audited\s+financial\s+results"
    )),
]

# Table pages whose title matched none of the above rank last, as "untitled"
TABLE_PREFERENCE = ["consolidated", "standalone", "generic", "untitled"]

# When the model answers with no figures at all, the page finder probably
# picked the wrong table (Adani Green, 21 Oct 2026: two scanned pages taken for
# the consolidated table came back empty; the standalone table was on page 4).
# The next-best candidate tables are then tried at once, at most this many,
# never resending a table already sent. Only then is it a MISS.
MAX_EXTRA_TABLE_TRIES = 2

def heading_lines(text: str) -> list:
    """A page's first HEADING_LINES non-empty lines. OCR text is full of blank
    lines (Emmvee, 23 Oct 2026: page 41's title came after line 20 counting them)."""
    return [line for line in text.splitlines() if line.strip()][:HEADING_LINES]

def classify_result_page(text: str) -> str | None:
    lower = text.lower()

    if sum(1 for pattern in TABLE_MARKERS.values() if pattern.search(lower)) < 2:
        return None

    lines = heading_lines(lower)
    heading = " ".join(lines)
    statement = NOT_TABLE_HEADINGS.search(heading)

    if statement and not RESULT_WORD_RE.search(heading):
        return None

    if AUDITOR_HEADINGS.search(heading) and len(AMOUNT_RE.findall(lower)) < AUDITOR_PAGE_AMOUNTS:
        return None

    title = next((line for line in lines if TITLE_LINE.search(line)), "")
    if NOTES_OR_SEGMENT_TITLE.search(title) or PRESS_RELEASE_HEADINGS.search(heading):
        return None

    for basis, pattern in RESULT_HEADINGS:
        if pattern.search(heading):
            return basis

    if statement:
        return None

    return "untitled"

def with_next_page(idx: int, page_texts: list) -> list:
    return [i for i in (idx, idx + 1) if i < len(page_texts)]

def table_candidates(page_texts: list) -> list:
    """Every results-table page as (index, basis), best first: TABLE_PREFERENCE
    order, then page order. find_table_pages sends the first one."""
    found = [(i, basis) for i, text in enumerate(page_texts) if (basis := classify_result_page(text))]
    return sorted(found, key=lambda c: (TABLE_PREFERENCE.index(c[1]), c[0]))

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

def ocr_page(pdf_bytes: bytes, page_num: int, dpi: int = LOCATE_OCR_DPI) -> str:
    images = convert_from_bytes(
        pdf_bytes,
        dpi=dpi,
        first_page=page_num,
        last_page=page_num,
        poppler_path=POPPLER_PATH
    )
    return pytesseract.image_to_string(images[0]) if images else ""

def ocr_pages(pdf_bytes: bytes, page_texts: list, indices: list,
              timings: dict | None = None, ocred: set | None = None) -> list:
    """OCR the given pages one at a time, in the order given, replacing their text.

    Stops early once a consolidated table and its next page are both readable
    (a consolidated table's next page is OCR'd next). Adds the time spent to
    timings["ocr"]; adds each OCR'd page index to ocred.
    """
    log.debug(f"running OCR on pages {[i + 1 for i in indices]}")

    started = time.monotonic()
    page_texts = list(page_texts)
    order = list(indices)
    pending = set(indices)

    try:
        while order:
            idx = order.pop(0)
            page_texts[idx] = ocr_page(pdf_bytes, idx + 1)
            pending.discard(idx)
            if ocred is not None:
                ocred.add(idx)

            pages, basis = find_table_pages(page_texts)
            if basis == "consolidated":
                waiting = [i for i in pages if i in pending]
                if not waiting:
                    break
                order = waiting + [i for i in order if i not in waiting]

    except Exception as e:
        log.warning(f"OCR extraction failed: {e}", extra=QUIET)

    if timings is not None:
        timings["ocr"] = timings.get("ocr", 0) + time.monotonic() - started

    return page_texts

def find_result_pages(pdf_bytes: bytes, ocr_page_limit: int = LONG_PDF_PAGES,
                      timings: dict | None = None) -> dict:
    """Locate the result table pages: {"pages", "basis", "reason", "texts", "ocr"}.

    pages is [] when no page passes the results-table check, even after OCR.
    texts holds every page's text (OCR text for scanned pages); ocr is the set
    of page indices whose text came from OCR. The first MAX_PDF_PAGES pages
    are read; only if they hold no titled table, the rest up to
    LONG_PDF_PAGES. Only pages below ocr_page_limit are ever OCR'd. Records
    timings["scan"] (text layer + table check) and timings["ocr"].
    """
    started = time.monotonic()
    timings = {} if timings is None else timings
    ocr_before = timings.get("ocr", 0)

    try:
        return _find_result_pages(pdf_bytes, ocr_page_limit, timings)
    finally:
        ocr_spent = timings.get("ocr", 0) - ocr_before
        timings["scan"] = timings.get("scan", 0) + time.monotonic() - started - ocr_spent

def is_scanned_page(page) -> bool:
    """One image covers (nearly) the whole page: a scan. Its text layer, if
    any, came from the scanner's own OCR and can't be trusted."""
    try:
        area = float(page.width) * float(page.height)
        return area > 0 and any(
            (float(im["x1"]) - float(im["x0"])) * (float(im["bottom"]) - float(im["top"])) >= SCAN_IMAGE_COVER * area
            for im in page.images
        )
    except Exception:
        return False

def table_hints(text: str) -> int:
    """How much a page's text looks like a results table, for OCR order:
    one point per kind of line item, plus 3 for a consolidated results title
    (2 for any other results title)."""
    lower = text.lower()
    heading = " ".join(heading_lines(lower))
    points = sum(1 for pattern in TABLE_MARKERS.values() if pattern.search(lower))
    for basis, pattern in RESULT_HEADINGS:
        if pattern.search(heading):
            return points + (3 if basis == "consolidated" else 2)
    return points

def read_text_layer(pdf_bytes: bytes, start: int, end: int) -> tuple:
    """Text of pages start..end-1 (0-based), the indices among them that are
    scans, and the PDF's page count."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        pages = pdf.pages[start:end]
        texts = [page.extract_text() or "" for page in pages]
        scans = {start + i for i, page in enumerate(pages) if is_scanned_page(page)}
        return texts, scans, len(pdf.pages)

def ocr_unread(pdf_bytes: bytes, page_texts: list, scans: set, ocred: set, limit: int,
               timings: dict) -> tuple:
    """OCR the pages below limit not read properly yet, while no titled table
    is found: near-empty pages, then scans. Returns (texts, pages, basis)."""
    selected, basis = find_table_pages(page_texts)

    # Hybrid PDF: text cover letter, scanned result pages
    low_text = [
        i for i, t in enumerate(page_texts)
        if len(t.strip()) < LOW_TEXT_PAGE_CHARS and i < limit and i not in ocred
    ]

    # An untitled "table" in the text layer can be a press release's
    # summary box while the real statement is a scan (Automobile Corp of
    # Goa, 21 Oct 2026: page 10's highlights were sent, the scanned
    # statement on page 6 never read). Read the unread pages first; a
    # titled table found there ranks above the untitled one.
    if (not selected or basis == "untitled") and low_text:
        log.debug(
            "no result table in text layer, OCR-ing low-text pages" if not selected
            else "only an untitled table in the text layer, OCR-ing low-text pages first"
        )
        page_texts = ocr_pages(pdf_bytes, page_texts, low_text, timings, ocred)
        selected, basis = find_table_pages(page_texts)

    # Scans that carry the scanner's own text layer: that layer can be too
    # garbled to show the table (GM Breweries, 8 Oct 2026: row names but
    # no figures). OCR them ourselves, the most table-like first.
    rescan = [i for i in sorted(scans) if i < limit and i not in ocred]
    if (not selected or basis == "untitled") and rescan:
        rescan.sort(key=lambda i: -table_hints(page_texts[i]))      # stable: page order on ties
        log.debug("no result table in the scans' own text layer, OCR-ing the scanned pages")
        page_texts = ocr_pages(pdf_bytes, page_texts, rescan, timings, ocred)
        selected, basis = find_table_pages(page_texts)

    return page_texts, selected, basis

def _find_result_pages(pdf_bytes: bytes, ocr_page_limit: int, timings: dict) -> dict:
    page_texts, scans, page_count = read_text_layer(pdf_bytes, 0, MAX_PDF_PAGES)
    first_limit = min(ocr_page_limit, MAX_PDF_PAGES)
    real_chars = sum(len(t.strip()) for t in page_texts)
    ocred = set()

    if real_chars < MIN_TEXT_CHARS:
        log.debug(f"only {real_chars} chars of text layer, OCR-ing the whole PDF")
        page_texts = ocr_pages(pdf_bytes, page_texts, list(range(min(len(page_texts), first_limit))), timings, ocred)
        selected, basis = find_table_pages(page_texts)
    else:
        page_texts, selected, basis = ocr_unread(pdf_bytes, page_texts, scans, ocred, first_limit, timings)

    # Long PDFs can put a press release and an investor deck first (23 Oct
    # 2026: Dr. Reddy's statements came after page 25 of 36, Emmvee's on pages
    # 41 and 48 of 51). With no titled table so far, read on.
    if (not selected or basis == "untitled") and page_count > MAX_PDF_PAGES:
        more, more_scans, _ = read_text_layer(pdf_bytes, MAX_PDF_PAGES, LONG_PDF_PAGES)
        log.debug(f"no titled results table in the first {MAX_PDF_PAGES} pages; reading pages "
                  f"{MAX_PDF_PAGES + 1}–{MAX_PDF_PAGES + len(more)}")
        page_texts = page_texts + more
        scans = scans | more_scans
        page_texts, selected, basis = ocr_unread(pdf_bytes, page_texts, scans, ocred, ocr_page_limit, timings)

    found = {
        "pages": selected,
        "basis": basis,
        "reason": f"{basis} table" if selected else "no results table found",
        "texts": page_texts,
        "ocr": ocred,
        "scans": scans,
        "ocr_limit": ocr_page_limit,
    }

    if selected:
        scanned = [i + 1 for i in selected if i in ocred or i in scans]
        log.debug(
            f"selected pages {[i + 1 for i in selected]} ({found['reason']}"
            f"{f', scanned: {scanned}' if scanned else ''})"
        )

    return found

def page_span(pages: list) -> str:
    """[9, 10] → "10–11" (1-based)."""
    pages = sorted(pages)
    return f"{pages[0] + 1}" if len(pages) == 1 else f"{pages[0] + 1}–{pages[-1] + 1}"

def next_candidate(pdf_bytes: bytes, found: dict, sent: set, timings: dict | None = None) -> dict | None:
    """The next-best results table whose first page hasn't been sent yet, as a
    `found` dict for it, or None.

    If every table already found was sent, the pages not read properly yet
    (scans and near-empty pages that weren't OCR'd, within the OCR limit) are
    OCR'd one at a time, most table-like first, until a new table and its
    next page are readable.
    """
    texts = list(found["texts"])
    ocred = set(found["ocr"])

    def pick():
        for idx, basis in table_candidates(texts):
            if idx not in sent:
                return {**found, "texts": texts, "ocr": set(ocred), "pages": with_next_page(idx, texts),
                        "basis": basis, "reason": f"{basis} table"}
        return None

    following = pick()
    if following:
        return following

    limit = found.get("ocr_limit", LONG_PDF_PAGES)
    unread = [
        i for i, t in enumerate(texts)
        if i < limit and i not in ocred
        and (i in found.get("scans", ()) or len(t.strip()) < LOW_TEXT_PAGE_CHARS)
    ]
    unread.sort(key=lambda i: -table_hints(texts[i]))     # stable: page order on ties
    if unread:
        log.debug(f"no other table found yet; OCR-ing unread pages {[i + 1 for i in unread]} until one turns up")

    started = time.monotonic()
    try:
        while unread:
            idx = unread.pop(0)
            texts[idx] = ocr_page(pdf_bytes, idx + 1)
            ocred.add(idx)
            following = pick()
            if following:
                waiting = [i for i in following["pages"] if i in unread]
                if not waiting:
                    return following
                unread = waiting + [i for i in unread if i not in waiting]     # its next page first
    except Exception as e:
        log.warning(f"OCR extraction failed: {e}", extra=QUIET)
    finally:
        if timings is not None:
            timings["ocr"] = timings.get("ocr", 0) + time.monotonic() - started

    return pick()

def current_figures(raw_out: dict) -> dict:
    """The answer's current-quarter figures by FIN_KEYS key (empty if none)."""
    data = raw_out.get("data")
    if not isinstance(data, dict):
        return {}
    figures = {}
    for key in FIN_KEYS:
        values = data.get(key)
        number = _to_number(values[0]) if isinstance(values, list) and values else None
        if number is not None:
            figures[key] = number
    return figures

def answered_without_figures(raw_out: dict) -> bool:
    """The model answered, but with no current-quarter figure for any line item."""
    return "data" in raw_out and not current_figures(raw_out)

def partial_answer(raw_out: dict) -> bool:
    """Figures, but neither EPS nor total expenses this quarter: a segment
    table, press release or highlights box rather than the results statement
    (Coforge, 23 Oct 2026: page 15's segment table gave revenue, PBT and PAT)."""
    figures = current_figures(raw_out)
    return bool(figures) and "basic_eps" not in figures and "total_expenses" not in figures

def pages_as_text(found: dict) -> str:
    return "\n".join(
        f"\n\n--- PAGE {i + 1} ---\n{found['texts'][i]}"
        for i in found["pages"]
    )

def is_scan(found: dict, i: int) -> bool:
    """Page i is a scan: OCR'd by us, or an image with the scanner's text layer.
    Scans go to the model as images (their text is not trustworthy)."""
    return i in found["ocr"] or i in found.get("scans", ())

def page_image_data_url(pdf_bytes: bytes, page_num: int) -> str:
    """One PDF page as a grayscale PNG data URL, long edge at most IMAGE_MAX_EDGE."""
    images = convert_from_bytes(
        pdf_bytes,
        dpi=IMAGE_DPI,
        first_page=page_num,
        last_page=page_num,
        grayscale=True,
        poppler_path=POPPLER_PATH,
    )
    if not images:
        raise ValueError(f"could not render page {page_num}")

    image = images[0]
    longest = max(image.size)
    if longest > IMAGE_MAX_EDGE:
        scale = IMAGE_MAX_EDGE / longest
        image = image.resize((round(image.width * scale), round(image.height * scale)))

    buf = io.BytesIO()
    image.save(buf, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

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
        log.warning("Model returned non-object JSON", extra=QUIET)
        return None

    unit = str(data.get("unit") or "").strip().lower()
    unit = UNIT_ALIASES.get(unit, unit)

    if unit not in UNIT_TO_CRORE:
        log.warning(f"Unrecognised unit from model: {data.get('unit')!r}", extra=QUIET)
        return None

    factor = UNIT_TO_CRORE[unit]

    fin = {
        "basis": str(data.get("basis") or "unknown").strip().lower(),
        "unit": unit,
        "period_end": parse_period_end(data.get("period_end")),
        "prev_period_end": parse_period_end(data.get("prev_period_end")),
        "ly_period_end": parse_period_end(data.get("ly_period_end")),
        "period_months": 6 if _to_number(data.get("period_months")) == 6 else 3,
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

class ModelCallFailed(Exception):
    """The API call itself failed (not a bad answer). Only raised for image
    calls, so extract_financials can fall back to OCR text."""

# ── OUT OF CREDITS ────────────────────────────────────────────
#
# When the AICredits wallet is empty every model call fails. Treated as an
# ordinary failure, each result would burn its retries within two minutes and
# be dropped. Instead the terminal says so once, results that need the model
# wait in `pending` without using up retries, and one model call is tried
# every CREDITS_RECHECK_SEC to see whether credits are back.

class CreditsExhausted(Exception):
    """The model provider refused for lack of credits (or we're still waiting
    for them). Never a retry: the filing is held until credits are back."""

CREDITS_RECHECK_SEC = 300
CREDIT_ERROR_WORDS = ("insufficient", "credit", "balance", "quota", "billing", "payment",
                      "top up", "top-up", "recharge", "funds", "wallet")
_credits = {"out": False, "since": None, "next_try": 0.0, "detail": ""}
_credits_lock = threading.Lock()

def is_credit_error(error: BaseException) -> bool:
    """An API error that means the account has no credits left: HTTP 402, or
    a 400/401/403/429 whose message talks about credits, balance or quota."""
    status = getattr(error, "status_code", None)
    text = str(error).lower()
    if status == 402:
        return True
    if status in (400, 401, 403, 429):
        return any(w in text for w in CREDIT_ERROR_WORDS)
    return any(w in text for w in ("insufficient credit", "insufficient balance", "credit balance", "out of credits"))

def credits_waiting() -> bool:
    """Credits ran out and it isn't time to try again yet (read-only)."""
    with _credits_lock:
        return _credits["out"] and time.monotonic() < _credits["next_try"]

def _credits_call_allowed() -> bool:
    """Before a model call: allowed unless credits are out; when they are,
    one call is let through every CREDITS_RECHECK_SEC to see if they're back."""
    with _credits_lock:
        if not _credits["out"]:
            return True
        if time.monotonic() >= _credits["next_try"]:
            _credits["next_try"] = time.monotonic() + CREDITS_RECHECK_SEC
            return True
        return False

def _note_credits_out(error: BaseException):
    with _credits_lock:
        first = not _credits["out"]
        _credits.update(out=True, next_try=time.monotonic() + CREDITS_RECHECK_SEC, detail=shorten(str(error), 120))
        if first:
            _credits["since"] = datetime.now()
    if first:
        log.error(f"AICredits: out of credits ({shorten(str(error), 200)}) · results wait, checking every "
                  f"{CREDITS_RECHECK_SEC // 60} min", extra=QUIET)
        say(f"AICredits is out of credits — top up. Results wait and are read when credits are back "
            f"(checked every {CREDITS_RECHECK_SEC // 60} min)", "red")

def _note_credits_ok():
    with _credits_lock:
        if not _credits["out"]:
            return
        since = _credits["since"]
        _credits.update(out=False, since=None, next_try=0.0, detail="")
    log.info(f"AICredits: credits are back (out since {since:%H:%M})" if since else "AICredits: credits are back", extra=QUIET)
    say("AICredits credits are back — reading the results that waited", "green")

# Claude 3.x / 4.x answer straight away: temperature 0 keeps two reads of the
# same page the same (9 Oct 2026: they differed), and 1500 tokens is ample.
# Claude 5.x thinks before it answers (on by default), refuses any temperature
# but its default, and counts the thinking toward max_tokens, so it gets no
# temperature and room to think. Any model that isn't Claude 3/4 is treated
# like 5.x. EXTRACTION_EFFORT is passed only to those.
LEGACY_MAX_TOKENS   = 1500
THINKING_MAX_TOKENS = 8000

def model_call_params(model: str) -> dict:
    """The chat.completions.create settings for this model, besides model and messages."""
    version = re.search(r"claude-(?:(?:haiku|sonnet|opus)-)?(\d+)", model.lower())

    if version and int(version.group(1)) <= 4:
        return {"max_tokens": LEGACY_MAX_TOKENS, "temperature": 0}

    params = {"max_tokens": THINKING_MAX_TOKENS}
    if EXTRACTION_EFFORT:
        params["extra_body"] = {"reasoning_effort": EXTRACTION_EFFORT}
    return params

_THINKING = re.compile(r"<(think|thinking)>.*?(?:</\1>|\Z)", re.S | re.I)

def answer_text(message) -> str:
    """The model's answer without its thinking. content is a string, or a list
    of parts when a gateway passes Claude's thinking blocks through."""
    content = getattr(message, "content", None)

    if isinstance(content, list):
        def field(part, key):
            return part.get(key) if isinstance(part, dict) else getattr(part, key, None)
        content = "".join(
            field(part, "text") or ""
            for part in content
            if field(part, "type") in (None, "text", "output_text")
        )

    return _THINKING.sub("", content or "").strip()

def parse_answer(text: str) -> dict:
    """The extraction JSON in the model's answer. Normally the answer is just
    the object; if the model wrote anything around it, the last object holding
    figure keys is used. Raises json.JSONDecodeError when there is none."""
    try:
        data = json.loads(text[text.find("{"):text.rfind("}") + 1])
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    decoder, found = json.JSONDecoder(), None
    start = text.find("{")

    while start != -1:
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue
        if isinstance(obj, dict) and any(key in obj for key in FIN_KEYS):
            found = obj
        start = text.find("{", end)

    if found is None:
        raise json.JSONDecodeError("no JSON object with figures in the answer" if text else "empty answer", text, 0)
    return found

usage_totals = {}                # model → {"calls", "in", "out", "thinking"} tokens this run
answered_by = {}                 # model asked for → names AICredits says answered

def note_usage(model: str, response, finish) -> None:
    """Add the call's token counts to usage_totals, the answering model's name
    (as AICredits reports it) to answered_by, and both to the debug log."""
    served = getattr(response, "model", None)
    if isinstance(served, str) and served:
        answered_by.setdefault(model, set()).add(served)
        log.debug(f"{model} answered by {served}")

    usage = getattr(response, "usage", None)
    if usage is None:
        return

    details = getattr(usage, "completion_tokens_details", None)
    tokens_in = getattr(usage, "prompt_tokens", 0) or 0
    tokens_out = getattr(usage, "completion_tokens", 0) or 0
    thinking = (getattr(details, "reasoning_tokens", 0) or 0) if details is not None else 0

    totals = usage_totals.setdefault(model, {"calls": 0, "in": 0, "out": 0, "thinking": 0})
    totals["calls"] += 1
    totals["in"] += tokens_in
    totals["out"] += tokens_out
    totals["thinking"] += thinking

    log.debug(
        f"{model} tokens: {tokens_in} in, {tokens_out} out"
        + (f" ({thinking} thinking)" if thinking else "")
        + f", finish {finish}"
    )

def _call_model(content, model: str, timings: dict | None = None,
                raw_out: dict | None = None, raise_api_errors: bool = False) -> dict | None:
    """One extraction call via AICredits; validated financials or None.

    content is the prompt string, or a list of text / image_url parts.
    raw_out, if given, receives the model's parsed JSON as raw_out["data"],
    even when it fails validation (obtain_financials reads period_end from it),
    and raw_out["cut_off"] when the token limit ended the reply before any JSON.
    Adds the call time to timings["model"].
    """
    if not _credits_call_allowed():
        raise CreditsExhausted("waiting for AICredits credits")

    started = time.monotonic()

    try:
        try:
            params = model_call_params(model)
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": content}],
                **params,
            )
        finally:
            if timings is not None:
                timings["model"] = timings.get("model", 0) + time.monotonic() - started

        if not getattr(response, "choices", None):
            # Some gateways answer 200 with an error body instead of choices
            extra = getattr(response, "model_extra", None) or {}
            raise RuntimeError(f"no answer from the model: {getattr(response, 'error', None) or extra.get('error') or extra}")
        _note_credits_ok()

        choice = response.choices[0]
        finish = getattr(choice, "finish_reason", None)
        note_usage(model, response, finish)

        try:
            data = parse_answer(answer_text(choice.message))
        except json.JSONDecodeError:
            if finish != "length":
                raise
            # Thinking counts toward max_tokens: it ran out before the JSON
            log.warning(
                f"{model} used all {params['max_tokens']} tokens before answering; will retry",
                extra=QUIET,
            )
            if raw_out is not None:
                raw_out["cut_off"] = True
            return None

        log.debug(f"{model} extracted: {data}")

        if raw_out is not None:
            raw_out["data"] = data

        fin = normalise_financials(data)

        if fin:
            log.debug(
                f"basis: {fin['basis']}, unit: {fin['unit']}, "
                f"column dates: {fin['period_end']} / {fin['prev_period_end']} / {fin['ly_period_end']}"
            )

        return fin

    except json.JSONDecodeError as e:
        log.warning(f"Model JSON parse error: {e}", extra=QUIET)
        return None
    except Exception as e:
        if is_credit_error(e):
            _note_credits_out(e)
            raise CreditsExhausted(str(e)) from e
        if raise_api_errors:
            raise ModelCallFailed(str(e)) from e
        log.warning(f"Model extraction failed: {e}", extra=QUIET)
        return None

def extract_from_text(text: str, model: str, timings: dict | None = None,
                      raw_out: dict | None = None) -> dict | None:
    """Send result-page text to the model; validated financials or None."""
    return _call_model(f"{EXTRACTION_PROMPT}\n\n---\nPDF TEXT:\n{text}", model, timings, raw_out)

def extract_from_page_images(pdf_bytes: bytes, found: dict, model: str,
                             timings: dict | None = None, raw_out: dict | None = None) -> dict | None:
    """Send the selected pages to the model: scanned pages as PNG images, text
    pages as text. Records timings["render"]. Raises ModelCallFailed if the API
    call fails, so the caller can fall back to OCR text."""
    started = time.monotonic()
    parts = [{"type": "text", "text": EXTRACTION_PROMPT}]

    for i in found["pages"]:
        if is_scan(found, i):
            parts.append({"type": "text", "text": f"--- PAGE {i + 1} (scanned page image) ---"})
            parts.append({"type": "image_url", "image_url": {"url": page_image_data_url(pdf_bytes, i + 1)}})
        else:
            parts.append({"type": "text", "text": f"--- PAGE {i + 1} ---\n{found['texts'][i]}"})

    if timings is not None:
        timings["render"] = timings.get("render", 0) + time.monotonic() - started

    return _call_model(parts, model, timings, raw_out, raise_api_errors=True)

def _note_image_failure(error: Exception):
    """Stop sending images for the rest of the run if the provider refuses them."""
    global _images_rejected
    text = str(error).lower()
    if "image" in text and any(w in text for w in ("media_type", "not supported", "unsupported", "invalid")):
        _images_rejected = True
        log.warning(f"Provider rejected page images ({error}); sending OCR text for scanned pages from now on")
    else:
        log.warning(f"Model call with page images failed ({error}); falling back to OCR text for this filing", extra=QUIET)

def extract_financials(pdf_bytes: bytes, model: str | None = None,
                       ambiguous: bool = False, timings: dict | None = None,
                       raw_out: dict | None = None) -> dict | None:
    """Pick the result pages locally (OCR if needed), then have the model read them.

    Scanned result pages go to the model as images (SEND_SCANS_AS_IMAGES);
    if that call fails, or images are off, they are re-OCR'd at
    FALLBACK_OCR_DPI and sent as text. ambiguous: a board meeting outcome
    whose headline names no results — OCR is capped at AMBIGUOUS_OCR_PAGES and
    the table check outcome is logged. Raises SkipFiling when the PDF has no
    results table (the model isn't called).

    If the model answers with no figures at all, or with figures but neither
    EPS nor total expenses (a segment table or press release, not the
    statement), the next-best candidate tables are tried straight away (up to
    MAX_EXTRA_TABLE_TRIES, never a table already sent), and the most complete
    reading is kept. raw_out["tried"] lists the page spans sent, e.g.
    ["10–11", "4–5"]; raw_out["data"] is the kept answer.
    """
    try:
        found = find_result_pages(
            pdf_bytes,
            AMBIGUOUS_OCR_PAGES if ambiguous else LONG_PDF_PAGES,
            timings
        )
    except Exception as e:
        log.warning(f"PDF text extraction failed: {e}", extra=QUIET)
        if b"%%EOF" not in pdf_bytes[-2048:]:
            # The file stops short (Tinna Rubber's NSE copy, 22 Oct 2026: NSE
            # serves 2.65 MB of a 6 MB PDF, the same on every download)
            raise RetryFiling(f"PDF is cut off ({len(pdf_bytes) / (1024 * 1024):.1f} MB, no end marker)")
        return None

    if ambiguous:
        log.debug(f"ambiguous outcome → results table {'found' if found['pages'] else 'not found'}")

    if not found["pages"]:
        raise SkipFiling(found["reason"], status="NONE")

    model = model or EXTRACTION_MODEL
    raw_out = {} if raw_out is None else raw_out
    sent, tried = set(), []
    best = None     # (rank, fin, attempt, span): the most complete reading so far

    for extra in range(MAX_EXTRA_TABLE_TRIES + 1):
        sent.update(found["pages"])
        tried.append(page_span(found["pages"]))
        attempt = {}
        fin = _extract_from_found(pdf_bytes, found, model, timings, attempt)

        # Validated first, then a statement (EPS or total expenses), then more
        # figures, then consolidated; between empty answers the latest stands
        figures = current_figures(attempt)
        rank = (fin is not None, bool(figures.keys() & {"basic_eps", "total_expenses"}), len(figures),
                (fin or {}).get("basis") == "consolidated")
        if best is None or rank > best[0] or not (figures or best[0][2]):
            best = (rank, fin, attempt, tried[-1])

        if answered_without_figures(attempt):
            missing = "no figures"
        elif partial_answer(attempt):
            missing = "no EPS or total expenses"
        else:
            break

        following = next_candidate(pdf_bytes, found, sent, timings) if extra < MAX_EXTRA_TABLE_TRIES else None
        if not following:
            break

        log.debug(
            f"{missing} from pages {tried[-1]} ({found['reason']}); "
            f"trying the {following['reason']} on pages {page_span(following['pages'])}"
        )
        found = following

    _, fin, attempt, span = best
    if span != tried[-1]:
        log.debug(f"kept the reading of pages {span}, the most complete")
    raw_out.clear()
    raw_out.update(attempt)
    raw_out["tried"] = tried
    return fin

def _extract_from_found(pdf_bytes: bytes, found: dict, model: str,
                        timings: dict | None, raw_out: dict) -> dict | None:
    """One model call for the pages in found: scans as images, else text."""
    scanned = [i for i in found["pages"] if is_scan(found, i)]

    if scanned and SEND_SCANS_AS_IMAGES and not _images_rejected:
        try:
            return extract_from_page_images(pdf_bytes, found, model, timings, raw_out)
        except CreditsExhausted:
            raise
        except ModelCallFailed as e:
            _note_image_failure(e)
        except Exception as e:      # rendering failed
            log.warning(f"Could not render page images ({e}); falling back to OCR text", extra=QUIET)

    if scanned:
        # Locating OCR was low resolution; re-read the chosen pages properly
        started = time.monotonic()
        texts = list(found["texts"])
        try:
            for i in scanned:
                texts[i] = ocr_page(pdf_bytes, i + 1, dpi=FALLBACK_OCR_DPI)
        except Exception as e:
            log.warning(f"Full-quality OCR failed ({e}); using the locating OCR text", extra=QUIET)
        if timings is not None:
            timings["ocr"] = timings.get("ocr", 0) + time.monotonic() - started
        found = {**found, "texts": texts}

    return extract_from_text(pages_as_text(found), model, timings, raw_out)

# ── SANITY CHECK ──────────────────────────────────────────────
#
# Catches figures that can't all be true together — typically digits misread
# from a scan. A failed check doesn't stop the row being saved, but it is
# flagged and never alerted.

CHECK_COLUMNS = [(0, "this {}"), (2, "year-ago {}")]

def numbers_problem(fin: dict) -> str | None:
    """Why the extracted figures don't add up, or None if they do (or there
    aren't enough figures to tell). Amounts are in crores."""

    def at(key, i):
        values = fin.get(key) or []
        return values[i] if i < len(values) else None

    period = "half year" if half_yearly(fin) else "quarter"

    for i, label in ((i, label.format(period)) for i, label in CHECK_COLUMNS):
        revenue, total_income = at("revenue_from_operations", i), at("total_income", i)
        total_expenses, pbt, pat = at("total_expenses", i), at("pbt", i), at("pat", i)

        if revenue is not None and total_income is not None and revenue > 0:
            if total_income < revenue * 0.98 - 0.01:
                return f"{label}: total income {total_income:,.2f} below revenue {revenue:,.2f}"

        income = total_income if total_income is not None else revenue

        if income is not None and total_expenses is not None:
            for key in ("employee_expense", "other_expenses", "finance_cost", "depreciation"):
                part = at(key, i)
                if part is not None and total_expenses >= 0 and part > total_expenses * 1.02 + 0.05:
                    return f"{label}: {key.replace('_', ' ')} {part:,.2f} above total expenses {total_expenses:,.2f}"

            # Exceptional items / associates sit between expenses and PBT
            # (Bridge Securities, 9 Oct 2026: a year-ago gain of ₹25 L
            # flagged a correct reading). Their sign is printed inconsistently,
            # so either direction counts as adding up.
            extra = at("exceptional_items", i) or 0
            implied = income - total_expenses

            if pbt is not None:
                actual, name = pbt, "PBT"
                tolerance = max(0.10 * abs(income), 0.25 * abs(pbt), 0.05)
            elif pat is not None:
                actual, name = pat, "PAT"
                tolerance = max(0.10 * abs(income), 0.5 * abs(pat), 0.05)   # tax sits between them
            else:
                continue

            if all(abs(implied + sign * extra - actual) > tolerance for sign in (1, -1)):
                plus = f" plus exceptional items {extra:,.2f}" if extra else ""
                return (
                    f"{label}: income {income:,.2f} minus expenses {total_expenses:,.2f}{plus} "
                    f"= {implied + extra:,.2f}, but {name} is {actual:,.2f}"
                )

    return None

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

    # A cost line blank in all three columns is one the company doesn't have
    # (no borrowings, nothing to depreciate): nothing to add back. 23 Oct
    # 2026: 20 of 307 scored rows had no EBITDA because a "–" came back null.
    def cost(key):
        values = fin.get(key) or [None, None, None]
        return [0.0, 0.0, 0.0] if all(v is None for v in values) else values

    fin_cost_vals = cost("finance_cost")
    dep_vals = cost("depreciation")

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
    # 5. Revenue QoQ Momentum (5 pts; half on half for half-yearly results)
    # ─────────────────────────────────────────

    s = 0.0 if rev_qoq_small else band_score(rev_qoq, [
        (25, 5),
        (15, 4),
        (10, 3),
        (5, 2),
    ])

    score += s

    bd["Revenue HoH" if half_yearly(fin) else "Revenue QoQ"] = (
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
        log.warning(f"Telegram send failed: {e}")
        return False

    if r.status_code == 200:
        return True

    log.warning(f"Telegram error: {r.text}")
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
    half = half_yearly(fin)
    company = filing["company"] + (f" {SME_TAG}" if half else "")
    figures, change, this, last = (("HALF-YEARLY", "HoH%", "CH", "PH") if half else
                                   ("QUARTERLY", "QoQ%", "CQ", "PQ"))

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

        f"🏢 <b>{html.escape(company)}</b>",
        f"📌 {filing['exchange']}: <code>{html.escape(filing['code'])}</code>",

        "",

        f"🏛 <b>Source:</b> <code>{filing['exchange']}</code>",
        f"📄 <b>Filing:</b> <code>{html.escape(filing['category'])}</code>",
        f"🗓 <b>Period:</b> <code>{html.escape(period)}</code>",
        f"🕐 <b>Exchange:</b> <code>{exchange_time}</code>",
        f"📲 <b>Alert:</b>   <code>{alert_time}</code>",
        f"⚡ <b>Delay:</b>   <code>{delay_text}</code>",

        "",

        f"📊 <b>{figures} FINANCIALS (₹ Cr, {html.escape(fin.get('basis', 'unknown'))})</b>",

        "<pre>"

        f"{'Metric':<12}"
        f"{change:>10}"
        f"{'YoY%':>10}"
        f"{this:>12}"
        f"{last:>12}"
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

    return send_telegram_text("\n".join(lines))


# ── MAIN ─────────────────────────────────────────────────────
#
# Each poll the main thread handles clear result filings first (oldest
# first). Ambiguous board meeting outcomes go to AmbiguousChecker, one
# background thread that downloads the PDF, checks it for a results table
# and runs the model if there is one. The checker never touches scanner
# state: the main thread applies its finished checks (score, CSV, alert)
# between polls, so a clear result never waits behind an ambiguous PDF.
#
# Terminal output: one POLL line when a cycle's fetches finish, one line per
# filing (built whole by report(), so threads can't interleave it) and one
# POLL summary at the end. Everything else about a filing goes to DEBUG.

def has_core_values(fin: dict) -> bool:
    return (
        fin["revenue_from_operations"][0] is not None and
        fin["pat"][0] is not None
    )

SCANNER_STARTED_AT: datetime | None = None   # set in main()

def is_catch_up(exchange_dt) -> bool:
    """Published before this run started: its delay measures downtime, not speed."""
    return bool(exchange_dt and SCANNER_STARTED_AT and exchange_dt < SCANNER_STARTED_AT)

def format_delay(since: datetime) -> str:
    hours, rest = divmod(int((datetime.now() - since).total_seconds()), 3600)
    mins, secs = divmod(rest, 60)
    text = f"{hours}h {mins}m {secs}s" if hours else f"{mins}m {secs}s"
    return text + (" (catch-up)" if is_catch_up(since) else "")

SME_TAG = "[SME]"     # after the company name when its results are half-yearly

def company_label(filing: dict) -> str:
    """The company's name, tagged [SME] once its results read as half-yearly
    (score_filing sets filing["sme"])."""
    return f"{filing['company']} {SME_TAG}" if filing.get("sme") else filing["company"]

def short_company(name: str, width: int = COMPANY_WIDTH) -> str:
    """Company name without Ltd/Limited/-$, cut or padded to a fixed width.
    A trailing [SME] tag survives the cut."""
    name, tag = (name or "Unknown").strip(), ""
    if name.endswith(SME_TAG):
        name, tag = name[:-len(SME_TAG)].strip(), " " + SME_TAG
    name = re.sub(r"\s*-\s*\$$", "", name)
    name = re.sub(r"[\s,]*\b(?:Ltd|Limited)\.?$", "", name, flags=re.I).strip() or "Unknown"
    room = width - len(tag)
    return ((name[:room - 1] + "…" if len(name) > room else name) + tag).ljust(width)

def report(status: str, filing: dict, text: str, level: int = logging.INFO, console: bool = True):
    """The one line for a filing: status, exchange, company, text. The log
    file gets it whole; the terminal a shorter version (console_text), or
    nothing when console=False."""
    company = company_label(filing)
    log.log(
        level,
        f"{filing['exchange']:<3}  {short_company(company)}  {text}",
        extra={"status": status, "exchange": filing["exchange"], "company": company,
               "text": text, "console": console},
    )

def format_timings(timings: dict, exchange_dt=None, outcome: str | None = None) -> str:
    """"⏱ download 0.5s · page scan 0.3s · OCR 6.1s · model 3.4s · exchange→alert 2m 13s"
    (dashboard.py parses these phrases)."""
    parts = [
        f"{label} {timings[key]:.1f}s"
        for key, label in [
            ("download", "download"),
            ("scan", "page scan"),
            ("ocr", "OCR"),
            ("render", "render"),
            ("model", "model"),
        ]
        if key in timings
    ]

    if outcome and exchange_dt:
        parts.append(f"exchange→{outcome} {format_delay(exchange_dt)}")

    return "⏱ " + " · ".join(parts) if parts else ""

def with_timings(text: str, timings: dict, exchange_dt=None, outcome: str | None = None) -> str:
    stamp = format_timings(timings, exchange_dt, outcome)
    return f"{text}  {stamp}" if stamp else text

def shorten(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"

def log_filing_details(filing: dict, isin):
    exchange_dt = filing["exchange_dt"]
    log.debug(
        f"{filing['exchange']} {filing['company']} ({filing['code']}): "
        f"ISIN {isin or 'unknown'}, {filing['category']}, "
        f"exchange time {exchange_dt.strftime('%d %b %Y %H:%M:%S') if exchange_dt else 'N/A'}, "
        f"headline from {'+'.join(filing['headline_fields']) or 'no headline field'}: {filing['headline']!r}"
    )

# The same PDF is often filed two to four times (BSE "Board Meeting" and
# "Result", and NSE). Byte-identical copies are read once per run: each PDF
# already handled is remembered with what came of it.
PDF_MEMORY_SIZE = 5000
_pdf_memory = {}                 # sha256 → "BSE 16:35 filing (MISS)"
_pdf_memory_lock = threading.Lock()

def remember_pdf(digest: str, filing: dict, outcome: str):
    when = filing.get("exchange_dt")
    label = f"{filing['exchange']}{f' {when:%d %b %H:%M}' if when else ''} filing ({outcome})"
    with _pdf_memory_lock:
        if len(_pdf_memory) >= PDF_MEMORY_SIZE:
            _pdf_memory.pop(next(iter(_pdf_memory)))
        _pdf_memory[digest] = label

def earlier_copy(digest: str) -> str | None:
    with _pdf_memory_lock:
        return _pdf_memory.get(digest)

def obtain_financials(filing: dict, nse: NseClient, ambiguous: bool = False,
                      timings: dict | None = None) -> dict:
    """Download the PDF and extract validated financials.

    Touches no scanner state, so the checker thread can run it too. Raises
    RetryFiling on a failure worth retrying (QuarterUnknown when the PDF gives
    no usable period_end), SkipFiling when retrying can't help — including a
    byte-identical copy of a PDF already handled this run.
    """
    timings = {} if timings is None else timings

    if not filing["attachment_url"]:
        raise RetryFiling("no attachment")

    started = time.monotonic()
    pdf = download_pdf(filing, nse)
    timings["download"] = time.monotonic() - started

    if not pdf:
        raise RetryFiling("PDF download failed")

    if not pdf.startswith(b"%PDF"):
        raise SkipFiling(f"attachment is not a PDF (starts {pdf[:8]!r})")

    log.debug(f"{filing['exchange']} {filing['company']}: PDF {len(pdf) / (1024 * 1024):.1f} MB")

    digest = hashlib.sha256(pdf).hexdigest()
    earlier = earlier_copy(digest)
    if earlier:
        raise SkipFiling(f"same PDF as the {earlier} · not read again")

    try:
        fin = _read_financials(pdf, filing, ambiguous, timings)
    except SkipFiling as e:
        remember_pdf(digest, filing, e.status)
        raise
    remember_pdf(digest, filing, "read")
    return fin

def tried_text(raw_out: dict) -> str:
    """" (pages 10–11, 4–5 tried)" when more than one table was sent, else ""."""
    tried = raw_out.get("tried") or []
    return f" (pages {', '.join(tried)} tried)" if len(tried) > 1 else ""

def _read_financials(pdf: bytes, filing: dict, ambiguous: bool, timings: dict) -> dict:
    """obtain_financials after the download: extract, then validate."""
    raw_out = {}
    fin = extract_financials(pdf, ambiguous=ambiguous, timings=timings, raw_out=raw_out)
    filed_on = (filing["exchange_dt"] or datetime.now()).date()

    # An old quarter is final, whatever else is wrong with the extraction:
    # retrying a result we'd ignore anyway only burns model calls. A date
    # more than 400 days old (e.g. a very late "year ended 31.03.2025") only
    # counts if all three column dates line up, so a misread year is retried.
    raw = raw_out.get("data") or {}
    dates = fin or {k: parse_period_end(raw.get(k)) for k in ("period_end", "prev_period_end", "ly_period_end")}
    old = old_quarter(dates.get("period_end"), filed_on)
    if old and (pdf_quarter(dates, filed_on) or not column_dates_problem(dates)):
        raise SkipFiling(
            f"{old}: old quarter, ignored (scoring from {SCORE_FROM_QUARTER})",
            model_called=True,
            status="OLD",
        )

    # The model answered but without usable figures (an insurer's or bank's
    # format, a shell company with no revenue, the wrong pages): the same PDF
    # reads the same, so no retry (9 Oct 2026: 52 of 59 model calls were such
    # retries). Only a failed call or a reply that isn't JSON is retried.
    answered = "data" in raw_out

    if not fin:
        if answered:
            raise SkipFiling(f"the model found no figures it could use{tried_text(raw_out)} · not retried",
                             model_called=True, status="MISS")
        if raw_out.get("cut_off"):
            raise RetryFiling("the model ran out of tokens before answering")
        raise RetryFiling("could not extract financials")

    if not has_core_values(fin):
        missing = " or ".join(
            name for key, name in (("revenue_from_operations", "revenue from operations"), ("pat", "PAT"))
            if fin[key][0] is None
        )
        raise SkipFiling(f"no current-quarter {missing} in the table{tried_text(raw_out)} · not retried",
                         model_called=True, status="MISS")

    if not pdf_quarter(fin, filed_on):
        raise QuarterUnknown(quarter_unknown_reason(fin, filed_on), fin)

    problem = column_dates_problem(fin)
    if problem:
        raise RetryFiling(f"column dates don't line up ({problem})")

    fin["check"] = numbers_problem(fin) or ""
    return fin

def score_filing(filing: dict, isin, fin: dict, processed: set, timings: dict,
                 flagged: set = frozenset(), scored_names: set = frozenset()) -> tuple:
    """Quarter key, duplicate check, score, CSV and alert.

    Returns (processed key, status word, line text); the key is returned too
    when the PDF's period shows the quarter was already scored. The quarter
    always comes from the PDF's period_end. Raises SkipFiling for a quarter
    before SCORE_FROM_QUARTER. flagged: company-quarters already saved with a
    failed sanity check; another failing copy saves no second row.
    scored_names: (company_key, quarter) already scored, which catches the
    other exchange's copy when the two ISIN keys differ.
    """
    exchange_dt = filing["exchange_dt"]
    filed_on = (exchange_dt or datetime.now()).date()
    quarter = pdf_quarter(fin, filed_on)
    filing["sme"] = half_yearly(fin)    # its line and alert carry [SME]

    if not quarter:      # obtain_financials already checks; never fall back to the filing date
        raise QuarterUnknown(quarter_unknown_reason(fin, filed_on), fin)

    if quarter_index(quarter) < quarter_index(SCORE_FROM_QUARTER):
        raise SkipFiling(
            f"{quarter}: old quarter, ignored (scoring from {SCORE_FROM_QUARTER})",
            model_called=True,
            status="OLD",
        )

    key = processed_key(filing, isin, quarter)

    if key in processed:
        return key, "SKIP", with_timings(f"{key} already scored (quarter from PDF)", timings)

    if (company_key(filing["company"]), quarter) in scored_names:
        return key, "SKIP", with_timings(
            f"{quarter} already scored from the other exchange (same company name)", timings)

    if fin.get("check") and key in flagged:
        # Another copy of a filing already saved as FLAG (Bridge Securities,
        # 9 Oct 2026, filed the same PDF twice on BSE: two identical rows)
        return key, "SKIP", with_timings(f"{key} already flagged, numbers still don't add up · no second row", timings)

    score, bd = compute_pead_score(fin)
    save_result_csv(filing, score, fin, quarter)
    summary = f"{score:4.1f}/50  {quarter}  {fin.get('basis', 'unknown')}"
    if "Rejected" in bd:
        reason = bd["Rejected"][0]
        summary += f"  rejected: {reason[:1].lower()}{reason[1:]}"

    if fin.get("check"):
        # Saved for reference, never alerted. The company-quarter stays open
        # (a different copy, e.g. the other exchange's, can still be scored);
        # it's recorded as flagged so a copy that fails too saves no second row.
        summary += f"  numbers don't add up ({fin['check']}) · no alert"
        return key, "FLAG", with_timings(summary, timings, exchange_dt, "scored")

    if score >= PEAD_THRESHOLD:
        sent = send_telegram(
            filing,
            score,
            bd,
            fin,
            quarter,
            datetime.now().strftime("%d %b %Y  %H:%M:%S"),
            format_delay(exchange_dt) if exchange_dt else "N/A"
        )
        summary += "  Telegram sent" if sent else "  Telegram FAILED"
        return key, "ALERT", with_timings(summary, timings, exchange_dt, "alert")

    return key, "SCORE", with_timings(summary, timings, exchange_dt, "scored")

def process_filing(filing: dict, isin, processed: set, nse: NseClient,
                   timings: dict | None = None, flagged: set = frozenset(),
                   scored_names: set = frozenset()) -> tuple:
    """Clear result filings, on the main thread: obtain financials, then score.

    Returns score_filing's (key, status, text). Raises RetryFiling or
    SkipFiling like obtain_financials, and SkipFiling for an old quarter.
    """
    timings = {} if timings is None else timings
    fin = obtain_financials(filing, nse, timings=timings)
    return score_filing(filing, isin, fin, processed, timings, flagged, scored_names)

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
        result = {"filing": filing, "fin": None, "skip": None, "retry": None, "error": None, "timings": timings}
        log.debug(f"checking {filing['exchange']} {filing['company']}: ambiguous outcome PDF")

        try:
            result["fin"] = obtain_financials(filing, self.nse, ambiguous=True, timings=timings)
        except CreditsExhausted as e:
            result["credits"] = e
        except SkipFiling as e:
            result["skip"] = e
        except RetryFiling as e:
            result["retry"] = e
        except Exception as e:
            result["error"] = e

        return result

    def stop(self):
        self.todo.put(None)
        self.thread.join(timeout=5)

class ScannerState:
    """Everything that survives between polls; persisted parts saved as they change.
    Only the main thread uses it."""

    from_day = None          # --from: this run's catch-up reads back to this day

    def __init__(self, nse: NseClient):
        self.nse = nse
        self.seen = load_seen()
        self.retries = load_retries()          # filing id → failed attempts
        self.flagged = load_flagged()          # company-quarters saved as FLAG
        self.scored_names = scored_names_from_csv()   # (company_key, quarter) already scored
        self.held = set()                      # filing ids waiting for model credits (in pending, no retry used)
        self.pending = {}                      # filing id → filing awaiting retry
        self.retry_after = {}                  # filing id → time.monotonic() before which it isn't retried
        self.bse_known_ids = set()             # BSE NEWSIDs fetched on earlier polls
        self.nse_known_ids = set()             # NSE filing ids fetched on earlier polls
        self.checker = AmbiguousChecker()
        self.failing = {}                      # exchange → True while its fetch fails
        self.fetched = {}                      # exchange → new announcements last poll (None = failed)
        self.window = {}                       # counts since the last terminal line
        self.caught_up = False                 # first poll's summary printed
        self.first_since = None                # where this run picks up (None = start of today)
        self.checkpoint = load_checkpoint()    # exchange → start of its last completed poll
        self.poll_marks = {}                   # this cycle's successful fetches, saved when it ends
        self.unpublished = []                  # this poll's new announcements, for BASIS after the alerts
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

        # Housekeeping detail: log file only, the terminal starts with the banner
        if to_isin:
            log.info(f"Migrated {len(to_isin)} processed key{'' if len(to_isin) == 1 else 's'} to ISIN format", extra=QUIET)
        if len(changed) > len(to_isin):
            log.info(f"Renamed {len(changed) - len(to_isin)} old-format keys to EXCHANGE-code format (ISIN unknown)", extra=QUIET)
        if without_isin:
            log.info(f"Processed keys still without ISIN: {', '.join(without_isin)}", extra=QUIET)

    def resume_from(self, exchange: str) -> date | None:
        """First day to fetch for this exchange: the day it was last read, if
        that's before today (at most MAX_RESUME_DAYS back). None = today only.
        Also covers midnight: the first poll after it still reads yesterday."""
        mark = self.checkpoint.get(exchange)
        if not mark:
            return None
        today = date.today()
        day = max(mark.date(), self.from_day or today - timedelta(days=MAX_RESUME_DAYS))
        return day if day < today else None

    def last_stop(self) -> datetime | None:
        """When the previous run last read the exchanges (the earlier of the
        two), or None if no run has finished a poll yet."""
        return min(self.checkpoint.values()) if self.checkpoint else None

    def resumed_since(self) -> datetime | None:
        """Where this run picks up from: the previous run's last poll, at most
        MAX_RESUME_DAYS back. A same-day restart re-reads today, but what came
        before this time was already handled. None = no checkpoint (start of today)."""
        stop = self.last_stop()
        if not stop:
            return None
        floor = datetime.combine(self.from_day or date.today() - timedelta(days=MAX_RESUME_DAYS), datetime.min.time())
        return max(stop, floor)

    def save_poll_marks(self):
        """End of a cycle: every exchange fetched successfully this cycle is
        read up to when its poll started. Saved only once the cycle's clear
        filings are handled, so a stop mid-cycle re-reads them next time."""
        if self.poll_marks:
            self.checkpoint.update(self.poll_marks)
            self.poll_marks = {}
            save_checkpoint(self.marks_to_save())

    def marks_to_save(self) -> dict:
        """The checkpoint as saved to disk: each exchange's mark, held back to
        its oldest filing still waiting (for credits or a retry). Those wait
        only in memory, so a restart must fetch their day again (16 Oct 2026:
        results held for credits from 10-15 Oct were never read, because the
        next run started from 16 Oct). The in-memory marks move on as usual,
        so polling stays light while the run lasts."""
        marks = dict(self.checkpoint)
        for filing in self.pending.values():
            exchange, when = filing.get("exchange"), filing.get("exchange_dt")
            if when and exchange in marks and when < marks[exchange]:
                marks[exchange] = when
        return marks

    def finish(self, fid: str):
        """Done with a filing for good: success, skip, or retries exhausted."""
        self.seen.add(fid)
        save_seen(self.seen)
        self.pending.pop(fid, None)
        self.retry_after.pop(fid, None)
        self.held.discard(fid)

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

def triage(filing: dict, state: ScannerState) -> str:
    """Filter and dedup pre-check. Returns what happened to the filing:

      "results" / "ambiguous" — process it now
      "intimation", "not relevant" — skipped quietly (counted in the POLL summary)
      "duplicate" — company-quarter already scored (a SKIP line)
      "old" — the headline names a quarter before SCORE_FROM_QUARTER (an OLD line)
      "seen" — already handled on an earlier poll
    """
    fid = filing["id"]

    if not fid or fid in state.seen:
        return "seen"

    if filing["category"] not in {
        "Result",
        "Board Meeting"
    }:
        return "not relevant"

    kind = filing_kind(filing)

    if kind in ("intimation", "other"):
        log.debug(
            f"skip {filing['exchange']} {filing['company']} board meeting ({kind}): "
            f"{filing['headline']!r} (headline from {'+'.join(filing['headline_fields']) or 'no headline field'})"
        )
        state.finish(fid)
        return "intimation" if kind == "intimation" else "not relevant"

    old = headline_old_quarter(filing["headline"])
    if old:
        log.debug(f"old quarter per headline: {filing['headline']!r}")
        report("OLD", filing, f"{old}: old quarter per headline, ignored (scoring from {SCORE_FROM_QUARTER}) · not downloaded")
        state.finish(fid)
        return "old"

    state.learn_isin(filing)
    isin = lookup_isin(filing, state.master)

    # Cheap pre-check with the filing-date quarter, and the half year ending
    # with it (an SME company's key); score_filing re-checks with the period
    # printed in the PDF
    filed_on = (filing["exchange_dt"] or datetime.now()).date()
    estimated = reporting_quarter(filed_on)

    for period in filter(None, [estimated, half_ending(estimated)]):
        estimated_key = processed_key(filing, isin, period)

        if estimated_key in state.processed:
            report("SKIP", filing, f"{estimated_key} already scored")
            state.finish(fid)
            return "duplicate"

        if (company_key(filing["company"]), period) in state.scored_names:
            report("SKIP", filing, f"{period} already scored from the other exchange (same company name)")
            state.finish(fid)
            return "duplicate"

    return kind

def record_success(filing: dict, state: ScannerState, key: str | None, status: str = "SCORE"):
    if key and status == "FLAG":       # saved, but the company-quarter stays open
        state.flagged.add(key)
        save_flagged(state.flagged)
    elif key and status != "SKIP":
        state.processed.add(key)
        save_processed_scrips(state.processed)
        state.scored_names.add((company_key(filing["company"]), key.rsplit("_", 1)[-1]))
    state.finish(filing["id"])

def report_result(status: str, filing: dict, text: str):
    """SCORE / ALERT / SKIP line; a FLAG (failed sanity check) is a warning."""
    report(status, filing, text, level=logging.WARNING if status == "FLAG" else logging.INFO)

def record_retry(filing: dict, state: ScannerState, reason: str, timings: dict,
                 error: BaseException | None = None, unknown_fin: dict | None = None):
    """A retryable failure: one RETRY (or ERROR) line, then retry later or give up.

    unknown_fin is the extraction from a QuarterUnknown failure: if this was
    the last attempt, it is saved with quarter UNKNOWN (scored, never alerted),
    so each filing leaves at most one row.
    """
    fid = filing["id"]
    state.held.discard(fid)
    attempts = state.retries.get(fid, 0) + 1

    if attempts > MAX_RETRIES:
        outcome = f"gave up after {attempts} attempts"

        if unknown_fin is not None:
            score, _ = compute_pead_score(unknown_fin)
            save_result_csv(filing, score, unknown_fin, "UNKNOWN")
            outcome += f" · saved as quarter UNKNOWN ({score:.1f}/50, no alert)"

        state.finish(fid)
    else:
        outcome = f"retry {attempts}/{MAX_RETRIES}"
        state.retries[fid] = attempts
        save_retries(state.retries)
        state.pending[fid] = filing

        if reason.startswith(WAIT_BEFORE_RETRY):
            delay = DOWNLOAD_RETRY_DELAYS_SEC[min(attempts, len(DOWNLOAD_RETRY_DELAYS_SEC)) - 1]
            state.retry_after[fid] = time.monotonic() + delay
            outcome += f" in {delay // 60} min" if delay >= 60 else f" in {delay} s"
        else:
            state.retry_after.pop(fid, None)

    if error is None:
        report("RETRY", filing, with_timings(f"{reason} · {outcome}", timings))
    else:
        report("ERROR", filing, with_timings(f"unexpected error: {type(error).__name__}: {error} · {outcome}", timings),
               level=logging.ERROR)
        log.debug(f"traceback for {filing['exchange']} {filing['company']}", exc_info=error)

def record_skip(filing: dict, state: ScannerState, skip: "SkipFiling", timings: dict,
                ambiguous: bool = False):
    """Skipped for good: one NONE / OLD / SKIP line, marked seen, never retried."""
    text = str(skip)

    if skip.status == "NONE":
        text = ("ambiguous outcome → " if ambiguous else "") + text
        if filing["category"] == "Result":
            # A "Result" filing without a table is worth a look at what it was
            text += f" · headline: {shorten(filing['headline'], 160)!r}"

    # A vague board meeting outcome with no results is the expected case:
    # counted in the terminal's summaries, not listed
    quiet = ambiguous and skip.status == "NONE"
    if quiet:
        state.window["outcomes without results"] = state.window.get("outcomes without results", 0) + 1
    report(skip.status, filing, with_timings(text, timings), console=not quiet)
    state.finish(filing["id"])

def hold_for_credits(filing: dict, state: ScannerState):
    """Keep a filing for when model credits are back: in pending, retry count
    untouched, a WAIT line in the log file only (the POLL line counts them)."""
    fid = filing["id"]
    state.pending[fid] = filing
    if fid not in state.held:
        state.held.add(fid)
        report("WAIT", filing, "waiting for AICredits credits · no retry used", console=False)

def handle_clear(filing: dict, state: ScannerState):
    if credits_waiting():
        hold_for_credits(filing, state)
        return

    isin = lookup_isin(filing, state.master)
    log_filing_details(filing, isin)
    timings = {}

    try:
        key, status, text = process_filing(filing, isin, state.processed, state.nse, timings,
                                           state.flagged, state.scored_names)
    except CreditsExhausted:
        hold_for_credits(filing, state)
        return
    except SkipFiling as e:
        record_skip(filing, state, e, timings)
        return
    except RetryFiling as e:
        record_retry(filing, state, str(e), timings, unknown_fin=getattr(e, "fin", None))
        return
    except Exception as e:
        record_retry(filing, state, "", timings, error=e)
        return

    report_result(status, filing, text)
    record_success(filing, state, key, status)

def handle_ambiguous(filing: dict, state: ScannerState) -> bool:
    """Queue an ambiguous outcome for the checker; True if newly queued."""
    if credits_waiting():
        hold_for_credits(filing, state)
        return False
    if not state.checker.submit(filing):
        return False

    log_filing_details(filing, lookup_isin(filing, state.master))
    report("CHECK", filing, "ambiguous outcome → queued for a results-table check", console=False)
    return True

def apply_ambiguous_result(result: dict, state: ScannerState):
    """Main thread: record a finished checker result and score it if it had results."""
    filing = result["filing"]
    fid = filing["id"]
    timings = result["timings"]
    state.checker.in_flight.discard(fid)

    if fid in state.seen:      # e.g. the company's clear filing was scored meanwhile
        return

    if result.get("credits"):
        hold_for_credits(filing, state)
        return

    if result["skip"]:
        record_skip(filing, state, result["skip"], timings, ambiguous=True)
        return

    if result["error"]:
        record_retry(filing, state, "", timings, error=result["error"])
        return

    if result["retry"]:
        retry = result["retry"]
        record_retry(filing, state, str(retry), timings, unknown_fin=getattr(retry, "fin", None))
        return

    try:
        key, status, text = score_filing(
            filing, lookup_isin(filing, state.master), result["fin"], state.processed, timings,
            state.flagged, state.scored_names
        )
    except SkipFiling as e:
        record_skip(filing, state, e, timings)
        return
    except RetryFiling as e:
        record_retry(filing, state, str(e), timings, unknown_fin=getattr(e, "fin", None))
        return
    except Exception as e:
        record_retry(filing, state, "", timings, error=e)
        return

    report_result(status, filing, text)
    record_success(filing, state, key, status)

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

def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"

def run_cycle(state: ScannerState):
    """One poll. Clear result filings are handled first (oldest first), then
    finished ambiguous checks are applied, then new ambiguous outcomes are
    queued for the checker (oldest first). Ends with the POLL summary line."""
    priority = {"results": 0, "ambiguous": 1}
    filings = sorted(
        poll_exchanges(state),     # already oldest first; sort is stable
        key=lambda f: priority.get(filing_kind(f), 2)
    )

    counts = {}
    ambiguous = []

    for filing in filings:
        retrying = filing["id"] in state.pending and filing["id"] not in state.held
        verdict = triage(filing, state)
        counts[verdict] = counts.get(verdict, 0) + 1
        if retrying and verdict in ("results", "ambiguous"):     # an earlier poll's failure, tried again
            counts[f"retried {verdict}"] = counts.get(f"retried {verdict}", 0) + 1

        if verdict == "results":
            handle_clear(filing, state)
        elif verdict == "ambiguous":
            ambiguous.append(filing)

    drain_checks(state)
    queued = sum(handle_ambiguous(filing, state) for filing in ambiguous)
    # Shared with BASIS only now, once this poll's alerts are sent, so a slow write can
    # never delay one. Before the poll marks are saved: a crash first re-reads them.
    publish_announcements(state.unpublished, state.master)
    state.unpublished = []
    state.save_poll_marks()

    retried = counts.get("retried results", 0) + counts.get("retried ambiguous", 0)
    log.info(
        " · ".join([
            "done",
            plural(counts.get("results", 0), "result filing"),
            plural(counts.get("intimation", 0), "intimation") + " skipped",
            *([f"{counts['old']} old quarter by headline"] if counts.get("old") else []),
            f"{queued} ambiguous queued",
            *([f"{retried} of them retries"] if retried else []),
            f"{counts.get('not relevant', 0)} not relevant",
            f"checker queue: {len(state.checker.in_flight)} pending",
        ]),
        extra={"status": "POLL", "console": False},
    )

    # The terminal counts new filings, retries and filings held for credits apart
    held_now = len(state.held)
    new_results = max(0, counts.get("results", 0) - counts.get("retried results", 0) - held_now)
    new_outcomes = max(0, queued - counts.get("retried ambiguous", 0))
    for key, n in [("results", new_results), ("intimations", counts.get("intimation", 0)),
                   ("old by title", counts.get("old", 0)), ("outcomes to check", new_outcomes),
                   ("retries", retried)]:
        state.window[key] = state.window.get(key, 0) + n
    state.window["waiting for credits"] = held_now

    terminal_summary(state)

SUMMARY_WORDS = [   # window key, singular, plural — the "Caught up" line
    ("results", "result filing", "result filings"),
    ("outcomes to check", "board meeting outcome to check", "board meeting outcomes to check"),
    ("outcomes without results", "outcome without results", "outcomes without results"),
    ("intimations", "intimation skipped", "intimations skipped"),
    ("old by title", "old result skipped by title", "old results skipped by title"),
    ("retries", "earlier failure retried", "earlier failures retried"),
    ("waiting for credits", "result waiting for AICredits credits", "results waiting for AICredits credits"),
]
POLL_WORDS = [      # shorter, for the per-poll line
    ("results", "result", "results"),
    ("outcomes to check", "outcome to check", "outcomes to check"),
    ("outcomes without results", "outcome without results", "outcomes without results"),
    ("intimations", "intimation skipped", "intimations skipped"),
    ("old by title", "old result skipped", "old results skipped"),
    ("retries", "retry", "retries"),
    ("waiting for credits", "waiting for credits", "waiting for credits"),
]

def summary_parts(window: dict, words: list = SUMMARY_WORDS) -> list:
    return [
        f"{window[key]:,} {one if window[key] == 1 else many}"
        for key, one, many in words
        if window.get(key)
    ]

def poll_line(state) -> tuple:
    """The terminal's line for one poll: (text, style).
    "BSE 3 new  ·  NSE 0 new  ·  1 result  ·  2 intimations skipped" """
    feeds = [
        f"{ex} not answering" if state.fetched.get(ex) is None else f"{ex} {state.fetched[ex]:,} new"
        for ex in ("BSE", "NSE")
    ]
    found = summary_parts(state.window, POLL_WORDS)
    if any(state.fetched.get(ex) is None for ex in ("BSE", "NSE")) or state.window.get("waiting for credits"):
        style = "yellow"
    else:
        style = None if found else "dim"
    return "  ·  ".join(feeds + (found or ["nothing relevant"])), style

def terminal_summary(state):
    """After the first poll: what the catch-up found, then a rule. After every
    later poll: one POLL line, so the terminal shows each poll as it happens."""
    if not state.caught_up:
        state.caught_up = True
        found = summary_parts(state.window) or ["nothing relevant"]
        since = state.first_since
        span = f"since {when_text(since)}" if since else "on today"
        say(f"Caught up {span}  ·  {state.window.get('BSE', 0):,} BSE + {state.window.get('NSE', 0):,} NSE "
            f"announcements  ·  " + "  ·  ".join(found))
        say("─" * 24 + f"  watching for new filings every {POLL_INTERVAL_SEC}s  " + "─" * 24, "dim")
    else:
        text, style = poll_line(state)
        say(text, style, word="POLL")

    state.window = {}

# ── SHARED ANNOUNCEMENTS (read by BASIS) ─────────────────────
# Every announcement a poll reads is also appended to this SQLite file, once that poll's
# alerts are sent (run_cycle), which BASIS (Downloads/news_scanner) opens read-only to
# follow BSE filings for its stock watchlist.
# Additive only: nothing here changes what is polled, scored or alerted, and a failure to
# write is logged to the file log and ignored. exchange_time is the exchange's own clock
# (IST), as the scanner parses it; fetched_at carries its UTC offset.
# BASIS reads by seq, the order rows were written in, never by exchange_time: a filing this
# tool backfills after a restart is written late but is still read. AUTOINCREMENT keeps seq
# from ever being reused or renumbered.
ANNOUNCEMENTS_DB = os.getenv("PEAD_ANNOUNCEMENTS_DB") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "announcements.db"
)

def publish_announcements(filings: list, master: dict):
    """Append `filings` to ANNOUNCEMENTS_DB, one row each, keyed by the scanner's own id, so
    a filing read twice is stored once. The ISIN comes from the scrip master, as for
    scoring. Never raises: building the rows is inside the guard too, and a filing that
    can't be turned into a row is skipped on its own, so it costs neither the poll nor the
    other rows."""
    try:
        fetched_at = datetime.now().astimezone().isoformat(timespec="seconds")
        rows = []
        for f in filings:
            try:
                if not f.get("id"):
                    continue
                try:
                    isin = lookup_isin(f, master)
                except (KeyError, AttributeError, TypeError):
                    isin = f.get("isin")
                when = f.get("exchange_dt")
                rows.append((
                    f["id"], f.get("exchange"), f.get("company"), f.get("code"), isin,
                    f.get("category"), f.get("headline"), f.get("attachment_url"),
                    when.isoformat() if when else None, fetched_at,
                ))
            except Exception as e:
                log.warning(f"announcement for BASIS skipped ({e})", extra=QUIET)
        if not rows:
            return
        conn = sqlite3.connect(ANNOUNCEMENTS_DB, timeout=2)
        try:
            conn.execute("PRAGMA journal_mode=WAL")   # BASIS reads while this writes
            conn.execute(
                "CREATE TABLE IF NOT EXISTS announcements (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                "id TEXT NOT NULL UNIQUE, exchange TEXT, company TEXT, code TEXT, isin TEXT, "
                "category TEXT, headline TEXT, attachment_url TEXT, exchange_time TEXT, "
                "fetched_at TEXT)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_announcements_time ON announcements (exchange_time)"
            )
            conn.executemany(
                "INSERT OR IGNORE INTO announcements (id, exchange, company, code, isin, category, "
                "headline, attachment_url, exchange_time, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"announcements for BASIS not written ({e})", extra=QUIET)

def poll_exchanges(state: ScannerState) -> list:
    """New filings from both exchanges plus pending retries, oldest first,
    so whichever exchange published a result first is the one processed.
    Logs the POLL line with both fetch results (file only; the terminal gets
    a failure once, and a line when the exchange recovers)."""
    fresh, parts = [], []
    poll_started = datetime.now()

    def failed(exchange: str, detail: str, blocked: bool):
        first = not state.failing.get(exchange)
        state.failing[exchange] = True
        state.fetched[exchange] = None
        if blocked:
            warn_blocked(exchange, detail, console=False)
        else:
            log.warning(f"{exchange}: fetch FAILED ({detail}) — announcements not read this poll", extra=QUIET)
        if first:      # the terminal says it once, and again when it recovers
            what = "is blocking the scanner (Access Denied) — Telegram warning sent" if blocked else \
                   f"isn't answering ({shorten(detail, 60)})"
            say(f"{exchange} {what}; retrying every poll, you'll see a line when it's back", "red")

    def succeeded(exchange: str, new: list):
        if state.failing.pop(exchange, None):
            say(f"{exchange} is answering again", "green")
        state.poll_marks[exchange] = poll_started
        # First poll: count for the terminal only what's newer than the last
        # run's read; a resumed range also returns the earlier part of that
        # day, already handled. Later polls only return new rows: count them all.
        mark = None if state.caught_up else state.checkpoint.get(exchange)
        cutoff = mark - timedelta(minutes=2) if mark else None
        count = sum(1 for f in new if not cutoff or not f["exchange_dt"] or f["exchange_dt"] >= cutoff)
        state.fetched[exchange] = count
        state.window[exchange] = state.window.get(exchange, 0) + count

    try:
        filings, pages = fetch_bse_filings(state.bse_known_ids, since=state.resume_from("BSE"))
        parts.append(
            f"BSE: {len(filings)} new announcements "
            f"(fetch OK, {pages} page{'' if pages == 1 else 's'} read)"
        )
        fresh += filings
        succeeded("BSE", filings)
    except ExchangeBlocked as e:
        failed("BSE", str(e), blocked=True)
        parts.append(f"BSE: fetch FAILED (blocked: {e})")
    except Exception as e:
        failed("BSE", str(e), blocked=False)
        parts.append(f"BSE: fetch FAILED ({e})")

    try:
        filings = fetch_nse_filings(state.nse, since=state.resume_from("NSE"))
        # NSE returns the whole day every poll; only unseen ones go on
        new = [f for f in filings if f["id"] not in state.nse_known_ids]
        state.nse_known_ids.update(f["id"] for f in filings)
        parts.append(f"NSE: {len(new)} new announcements (fetch OK, {len(filings)} today)")
        fresh += new
        succeeded("NSE", new)
    except ExchangeBlocked as e:
        failed("NSE", str(e), blocked=True)
        parts.append(f"NSE: fetch FAILED (blocked: {e})")
    except Exception as e:
        failed("NSE", str(e), blocked=False)
        parts.append(f"NSE: fetch FAILED ({e})")

    log.info(" · ".join(parts), extra={"status": "POLL", "console": False})
    state.unpublished = list(fresh)    # shared with BASIS by run_cycle, after the alerts

    by_id = {f["id"]: f for f in state.pending.values()}
    by_id.update({f["id"]: f for f in fresh})

    # A failed download waits out its delay (DOWNLOAD_RETRY_DELAYS_SEC)
    now = time.monotonic()
    due = [f for f in by_id.values() if state.retry_after.get(f["id"], 0) <= now]

    return sorted(due, key=lambda f: f["exchange_dt"] or datetime.max)

def dump_samples():
    """Save one raw announcements response per exchange to verify field names."""
    nse = NseClient()
    today = date.today()

    targets = [
        (
            "BSE", "raw_bse_sample.json", BSE_HEADLINE_FIELDS,
            lambda: requests.get(
                BSE_ANN_URL.format(page=1, from_date=today.strftime("%Y%m%d"), to_date=today.strftime("%Y%m%d")),
                headers=HEADERS,
                timeout=15
            ),
        ),
        (
            "NSE", "raw_nse_sample.json", NSE_HEADLINE_FIELDS,
            lambda: nse.get(NSE_ANN_URL.format(from_date=today.strftime("%d-%m-%Y"), to_date=today.strftime("%d-%m-%Y"))),
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
            log.info(f"{exchange}: {len(rows)} rows; fields: {sorted(rows[0].keys())}")
            present = [f for f in headline_fields if f in rows[0]]
            log.info(f"{exchange}: headline fields present: {present or 'NONE'} (expected {headline_fields})")

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
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show DEBUG detail in the terminal too (pead_tool.log always has it)",
    )
    parser.add_argument(
        "--from",
        dest="from_day",
        type=date.fromisoformat,
        metavar="YYYY-MM-DD",
        help="catch up from this day instead of where the last scan stopped (no "
             f"{MAX_RESUME_DAYS}-day limit); filings already handled are not read again",
    )
    args = parser.parse_args(argv)

    configure_console(args.verbose)

    if args.dump:
        dump_samples()
        return

    global SCANNER_STARTED_AT
    SCANNER_STARTED_AT = datetime.now()

    effort = (f" ({EXTRACTION_EFFORT} effort)"
              if EXTRACTION_EFFORT and "temperature" not in model_call_params(EXTRACTION_MODEL) else "")

    log.info(
        f"PEAD scanner · BSE + NSE · model {EXTRACTION_MODEL}{effort} · "
        f"alert at score ≥ {PEAD_THRESHOLD} · scoring from {SCORE_FROM_QUARTER}",
        extra=QUIET,
    )
    say(f"PEAD scanner  ·  {EXTRACTION_MODEL.split('/')[-1]}{effort}  ·  alerts at {PEAD_THRESHOLD}+  ·  "
        f"scoring {SCORE_FROM_QUARTER} results  ·  {SCANNER_STARTED_AT:%a %d %b %Y}", "bold")

    if (os.getenv("EXTRACTION_MODEL") or EXTRACTION_MODEL) != EXTRACTION_MODEL:
        log.warning(f"EXTRACTION_MODEL in .env is ignored; the scanner always uses {EXTRACTION_MODEL}")

    initialize_csv()

    state = ScannerState(NseClient())

    log.info(
        f"Loaded {len(state.seen)} seen filings · {len(state.processed)} scored company-quarters · "
        f"{len(state.retries)} awaiting retry",
        extra=QUIET,
    )
    say(f"{len(state.master['bse']):,} BSE + {len(state.master['nse']):,} NSE companies  ·  "
        f"{len(state.processed)} scored this quarter  ·  {len(state.retries)} awaiting retry", "dim")

    stop = state.last_stop()
    if args.from_day:
        state.from_day = args.from_day
        start = datetime.combine(args.from_day, datetime.min.time())
        state.checkpoint = {"BSE": start, "NSE": start}
    state.first_since = state.resumed_since()
    if args.from_day:
        say(f"Scanning from {when_text(state.first_since)}  ·  --from: every filing since then that wasn't "
            f"handled yet is read", "cyan")
    elif not state.first_since:
        say("Scanning from 00:00 today  ·  no earlier run to pick up from", "cyan")
    elif state.first_since > stop:
        say(f"Scanning from {when_text(state.first_since)}  ·  last scan was {when_text(stop)}, "
            f"catch-up goes back {MAX_RESUME_DAYS} days at most", "cyan")
    else:
        say(f"Scanning from {when_text(state.first_since)}  ·  where the last scan stopped", "cyan")
    if stop:
        log.info(f"Last scan {stop:%d %b %Y %H:%M:%S} · scanning from {state.first_since:%d %b %Y %H:%M:%S}", extra=QUIET)

    try:
        while True:
            if date.today() != state.master_day:
                state.refresh_master()

            run_cycle(state)
            wait_for_checks(state, POLL_INTERVAL_SEC)
    finally:
        state.checker.stop()

if __name__ == "__main__":
    setup_file_logging()
    setup_terminal_log()
    main()
