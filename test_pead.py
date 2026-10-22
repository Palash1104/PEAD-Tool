"""
Offline tests for pead_tool.py — no network, no real Telegram / AICredits.

  python test_pead.py

Network, the model and Telegram are mocked; state files go to a temp folder.
The PDF tests use the real pdfplumber, Poppler and Tesseract (~30s of OCR).
"""
import base64
import calendar
import csv
import io
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
from datetime import date, datetime
from unittest import mock

# Dummy credentials so a missed mock can never reach the real bot or API
for var in ["AICREDITS_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]:
    os.environ[var] = "test"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pead_tool as pt
import compare_models
import dashboard

# pead_tool loaded .env; an old EXTRACTION_MODEL line there (ignored by the
# scanner) would add a startup warning to every banner the tests check
os.environ.pop("EXTRACTION_MODEL", None)

# Most fixtures are Q1FY27 results; the quarter-cutoff tests set the real value back
CONFIGURED_SCORE_FROM_QUARTER = pt.SCORE_FROM_QUARTER
pt.SCORE_FROM_QUARTER = "Q1FY20"

fails = 0

def check(name, cond, extra=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra else ""))
    fails += (not cond)

def section(title):
    print(f"\n── {title}")

def fresh_state_dir():
    d = tempfile.mkdtemp(prefix="pead_test_")
    pt.SEEN_FILE = os.path.join(d, "seen.json")
    pt.PROCESSED_SCRIPS_FILE = os.path.join(d, "processed.json")
    pt.RESULTS_CSV = os.path.join(d, "results.csv")
    pt.RETRIES_FILE = os.path.join(d, "retries.json")
    pt.SCRIP_MASTER_FILE = os.path.join(d, "master.json")
    pt.CHECKPOINT_FILE = os.path.join(d, "checkpoint.json")
    pt.FLAGGED_FILE = os.path.join(d, "flagged.json")
    pt.ANNOUNCEMENTS_DB = os.path.join(d, "announcements.db")
    pt._last_blocked_alert.clear()
    pt._pdf_memory.clear()
    pt._empty_answers.clear()
    pt._credits.update(out=False, since=None, next_try=0.0, detail="")
    return d

class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=None, url="http://x"):
        self.status_code = status_code
        self._json = json_data
        self.text = text if text is not None else json.dumps(json_data)
        self.content = self.text.encode()
        self.url = url

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise pt.requests.HTTPError(str(self.status_code))

class LogCapture(logging.Handler):
    """Collects pead_tool log records inside a `with` block."""
    def __enter__(self):
        self.messages, self.records = [], []
        pt.log.addHandler(self)
        return self
    def __exit__(self, *exc):
        pt.log.removeHandler(self)
    def emit(self, record):
        self.messages.append(record.getMessage())
        self.records.append((getattr(record, "status", None) or record.levelname, record.levelno, record.getMessage()))
    def has(self, text):
        return any(text in m for m in self.messages)
    def has_line(self, status, *texts):
        """A record with this status word whose message contains every text."""
        return any(st == status and all(t in m for t in texts) for st, _, m in self.records)
    def terminal(self):
        """What the terminal shows: (status, message) of INFO and above."""
        return [(st, m) for st, lvl, m in self.records if lvl >= logging.INFO]

ACCESS_DENIED = FakeResponse(403, text="<HTML><TITLE>Access Denied</TITLE>You don't have permission</HTML>")

def month_end_before(iso, months):
    """The month-end `months` before the month of an ISO date."""
    d = date.fromisoformat(iso)
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    return date(y, m + 1, calendar.monthrange(y, m + 1)[1]).isoformat()

def mk(**kw):
    """A model reply, normalised. Given only period_end, the previous-quarter and
    last-year column dates are filled in 3 and 12 months earlier, as a correct
    extraction has them; tests of the column check pass their own."""
    d = {"unit": "crores"}
    d.update(kw)
    if d.get("period_end") and "prev_period_end" not in d and "ly_period_end" not in d:
        d["prev_period_end"] = month_end_before(d["period_end"], 3)
        d["ly_period_end"] = month_end_before(d["period_end"], 12)
    return pt.normalise_financials(d)

GOOD = dict(revenue_from_operations=[150, 120, 100], pat=[30, 20, 10], basic_eps=[3, 2, 1],
            pbt=[40, 30, 15], finance_cost=[2, 2, 2], depreciation=[5, 5, 5])

# ─────────────────────────────────────────────────────────────
section("quarters")

for d, q in [(date(2026, 6, 30), "Q1FY27"), (date(2026, 9, 30), "Q2FY27"), (date(2026, 12, 31), "Q3FY27"),
             (date(2027, 3, 31), "Q4FY27"), (date(2026, 3, 31), "Q4FY26"), (date(2026, 4, 1), "Q1FY27")]:
    check(f"quarter_label {d}", pt.quarter_label(d) == q, pt.quarter_label(d))

for d, q in [(date(2026, 9, 24), "Q1FY27"), (date(2026, 7, 1), "Q1FY27"), (date(2026, 11, 14), "Q2FY27"),
             (date(2027, 2, 14), "Q3FY27"), (date(2026, 5, 30), "Q4FY26"), (date(2026, 6, 15), "Q4FY26"),
             (date(2026, 1, 5), "Q3FY26")]:
    check(f"reporting_quarter filed {d}", pt.reporting_quarter(d) == q, pt.reporting_quarter(d))

filed = date(2026, 7, 20)
check("pdf_quarter uses period_end (late Q4 filer)", pt.pdf_quarter({"period_end": "2026-03-31"}, filed) == "Q4FY26")
check("no period_end → no quarter (no filing-date fallback)", pt.pdf_quarter({"period_end": None}, filed) is None)
check("implausible period_end → no quarter", pt.pdf_quarter({"period_end": "2024-06-30"}, filed) is None)
check("future period_end → no quarter", pt.pdf_quarter({"period_end": "2026-09-30"}, filed) is None)
check("filing-date fallback is gone", not hasattr(pt, "filing_quarter"))
check("quarter unknown reason: missing",
      pt.quarter_unknown_reason({"period_end": None}, filed) == "quarter unknown (no period_end)")
check("quarter unknown reason: implausible",
      pt.quarter_unknown_reason({"period_end": "2024-06-30"}, filed)
      == "quarter unknown (period_end 2024-06-30 implausible for a filing on 2026-07-20)")

for cur, prev, ly, want in [
    ("2026-09-30", "2026-06-30", "2025-09-30", None),                      # Q2FY27
    ("2026-12-31", "2026-09-30", "2025-12-31", None),                      # Q3, previous in the same year
    ("2027-03-31", "2026-12-31", "2026-03-31", None),                      # Q4, previous across the year end
    ("2026-06-30", "2026-03-31", "2025-06-30", None),                      # Q1
    ("2026-09-29", "2026-06-27", "2025-09-28", None),                      # day of month doesn't matter
    ("2026-09-30", "2026-03-31", "2025-09-30", "previous column 2026-03-31 isn't 3 months before 2026-09-30"),
    ("2026-09-30", "2025-09-30", "2026-06-30", "previous column 2025-09-30 isn't 3 months before 2026-09-30"),  # swapped
    ("2026-09-30", "2026-06-30", "2024-09-30", "last-year column 2024-09-30 isn't 12 months before 2026-09-30"),
    ("2026-09-30", "2026-09-30", "2025-09-30", "previous column 2026-09-30 isn't 3 months before 2026-09-30"),  # half-year YTD column
    ("2026-09-30", None, "2025-09-30", "previous-quarter column date missing"),
    ("2026-09-30", None, None, "previous-quarter and last-year column date missing"),
]:
    got = pt.column_dates_problem({"period_end": cur, "prev_period_end": prev, "ly_period_end": ly})
    check(f"column dates {cur} / {prev} / {ly}", got == want, got)

# ─────────────────────────────────────────────────────────────
section("board meeting filter")

for h, want in [
    ("Board Meeting Outcome for Outcome Of Board Meeting - Unaudited Financial Results For Quarter Ended 30.06.2026", "results"),
    ("Intimation of Outcome of Board Meeting held today - Financial Results", "results"),
    ("Board Meeting Intimation for Considering And Approving Unaudited Financial Results", "intimation"),
    ("Transport Corporation of India Ltd - 532349 - Board Meeting Intimation for Prior Intimation Of The Meeting", "intimation"),
    ("Board Meeting Outcome for Fund Raising By Way Of QIP", "ambiguous"),
    ("Board Meeting Outcome for Declaration Of Interim Dividend", "ambiguous"),
    ("Board Meeting Outcome for Dividend And Financial Results", "results"),
    ("Outcome of Board Meeting - Date of AGM", "ambiguous"),
    ("Board Meeting - Financial Results for the quarter ended 30.06.2026", "results"),
    ("Closure of Trading Window", "other"),
    ("Outcome of Board Meeting Reliance Industries Limited has informed the Exchange that the Board approved the financial results", "results"),
    # Headlines from the 2026-09-24 live log that used to be skipped outright
    ("GACM Technologies Ltd - 531723 - Board Meeting Outcome for Meeting Held On Thursday, September 24, 2026 Outcome of Board Meeting", "ambiguous"),
    ("Saraswati Saree Depot Ltd - 544230 - Board Meeting Outcome for Outcome Of Board Meeting Held Today I.E Thursday, September 24, 2026", "ambiguous"),
    ("", "other"),
]:
    got = pt.board_meeting_kind(h)
    check(f"bm kind {want:<10} {h[:60]!r}", got == want, got)

# ─────────────────────────────────────────────────────────────
section("exchange normalisation")

bse_row = {"NEWSID": "abc-123", "SCRIP_CD": 500325, "SLONGNAME": "Reliance Industries Ltd", "CATEGORYNAME": "Board Meeting",
           "NEWSSUB": "Board Meeting Outcome for Financial Results", "HEADLINE": "", "SUBCATNAME": "Outcome of Board Meeting",
           "ATTACHMENTNAME": "x.pdf", "DT_TM": "2026-09-24T17:30:12.43"}
f = pt.normalise_bse(bse_row)
check("BSE id prefixed", f["id"] == "BSE:abc-123")
check("BSE code is str", f["code"] == "500325")
check("BSE attachment url", f["attachment_url"] == pt.BSE_PDF_BASE + "x.pdf")
check("BSE headline fields recorded (empty HEADLINE skipped)", f["headline_fields"] == ["NEWSSUB", "SUBCATNAME"], f["headline_fields"])
check("BSE time parsed", f["exchange_dt"] == datetime(2026, 9, 24, 17, 30, 12, 430000))
check("BSE no-attachment -> empty url", pt.normalise_bse({"NEWSID": "n"})["attachment_url"] == "")

nse_row = {"symbol": "M&M", "desc": "Outcome of Board Meeting", "attchmntText": "Mahindra has informed the Exchange ... financial results",
           "attchmntFile": "https://nsearchives.nseindia.com/corporate/MM_24092026.pdf", "sm_name": "Mahindra & Mahindra Limited",
           "sm_isin": "INE101A01026", "an_dt": "24-Sep-2026 17:31:00", "exchdisstime": "24-Sep-2026 17:31:05", "seq_id": "998877"}
g = pt.normalise_nse(nse_row)
check("NSE id", g["id"] == "NSE:998877")
check("NSE isin", g["isin"] == "INE101A01026")
check("NSE category mapped", g["category"] == "Board Meeting")
check("NSE headline fields", g["headline_fields"] == ["desc", "attchmntText"])
check("NSE time from exchdisstime", g["exchange_dt"] == datetime(2026, 9, 24, 17, 31, 5))
check("NSE headline -> results", pt.board_meeting_kind(g["headline"]) == "results")
check("NSE result desc -> Result", pt.nse_category("Financial Result Updates") == "Result")
check("NSE other desc unchanged", pt.nse_category("Change in Director(s)") == "Change in Director(s)")
check("NSE bad isin -> None", pt.normalise_nse({"sm_isin": "junk"})["isin"] is None)

# ─────────────────────────────────────────────────────────────
section("real exchange rows (captured 2026-09-24)")

REAL_BSE_OUTCOME = {'NEWSID': '1b933ea0-6848-4e29-9436-5b1b0dd40cb4', 'SCRIP_CD': 544898, 'XML_NAME': 'ANN_544898_1B933EA0-6848-4E29-9436-5B1B0DD40CB4', 'NEWSSUB': 'ESDS Software Solution Ltd - 544898 - Board Meeting Outcome for Outcome Of The Board Meeting', 'DT_TM': '2026-09-24T20:37:48.96', 'NEWS_DT': '2026-09-24T20:37:48.96', 'CRITICALNEWS': 0, 'ANNOUNCEMENT_TYPE': 'A', 'QUARTER_ID': None, 'FILESTATUS': 'N    ', 'ATTACHMENTNAME': 'b3f74873-bb3d-45f8-ae76-c90aca2297c0.pdf', 'MORE': '', 'HEADLINE': 'Unaudited Standalone and Consolidated Financial Results for the quarter ended June 30, 2026', 'CATEGORYNAME': 'Board Meeting', 'OLD': 1, 'RN': 1, 'PDFFLAG': 0, 'NSURL': 'https://www.bseindia.com/stock-share-price/esds-software-solution-ltd/esds/544898/', 'SLONGNAME': 'ESDS Software Solution Ltd', 'AGENDA_ID': 198, 'TotalPageCnt': 33, 'News_submission_dt': '2026-09-24T20:37:46', 'DissemDT': '2026-09-24T20:37:48.96', 'TimeDiff': '00:00:02', 'Fld_Attachsize': 7788222, 'SUBCATNAME': 'Outcome of Board Meeting', 'BSENEWSID': None, 'INVESTOR_PRESENTATION': None, 'RECORDID': None, 'DataInsDate': None, 'AUDIO_VIDEO_FILE': None}
REAL_BSE_INTIMATION = {'NEWSID': '36d890d0-cf3f-499d-85b2-0b7633d543e6', 'SCRIP_CD': 530973, 'NEWSSUB': 'Alfa Ica India Ltd - 530973 - Board Meeting Intimation for BM To Be Held On 03-10-2026', 'DT_TM': '2026-09-24T20:34:36.6', 'NEWS_DT': '2026-09-24T20:34:36.6', 'ATTACHMENTNAME': '4aefaad6-37bd-43b5-b01b-7e4577803868.pdf', 'MORE': 'Alfa Ica India Ltdhas informed BSE that the meeting of the Board of Directors of the Company is scheduled on 03/10/2026 ,inter alia, to consider and approve 1)Resignation of Company secretary \r\n2)Appointment of Company Secretary', 'HEADLINE': 'Alfa Ica India Ltdhas informed BSE that the meeting of the Board of Directors of the Company is scheduled on 03/10/2026 ,inter alia, to consider and approve 1)Resignation of Company secretary ....', 'CATEGORYNAME': 'Board Meeting', 'SLONGNAME': 'Alfa Ica India Ltd', 'TotalPageCnt': 33, 'SUBCATNAME': 'Board Meeting'}
REAL_NSE_RESULT = {'an_dt': '24-Sep-2026 20:30:02', 'attFileSize': '7.43 MB', 'attchmntFile': 'https://nsearchives.nseindia.com/corporate/ESDS_24092026202831_ESDS_BM_Outcome_BSE_NSE_Intimation.pdf', 'attchmntText': 'ESDS Software Solution Limited has submitted to the Exchange, the financial results for the period ended Jun 30, 2026.', 'bflag': None, 'csvName': None, 'desc': 'Outcome of Board Meeting', 'difference': '00:00:01', 'dt': '24092026203002', 'exchdisstime': '24-Sep-2026 20:30:03', 'fileSize': '7.43 MB', 'hasXbrl': True, 'old_new': None, 'orgid': None, 'seq_id': '106792297', 'smIndustry': None, 'sm_isin': 'INE0DRI01029', 'sm_name': 'ESDS Software Solution Limited', 'sort_date': '2026-09-24 20:30:02', 'symbol': 'ESDS'}
REAL_NSE_OTHER_OUTCOME = {'an_dt': '24-Sep-2026 12:36:57', 'attchmntFile': 'https://nsearchives.nseindia.com/corporate/GLOBAL_24092026123634_Outcoem_of_Baord_Meeting_24092026.pdf', 'attchmntText': 'Global Education Limited has informed the Exchange regarding Outcome of Board Meeting held on September 24, 2026.', 'desc': 'Outcome of Board Meeting', 'exchdisstime': '24-Sep-2026 12:36:58', 'seq_id': '106790850', 'sm_isin': 'INE291W01011', 'sm_name': 'Global Education Limited', 'sort_date': '2026-09-24 12:36:57', 'symbol': 'GLOBAL'}
REAL_NSE_NO_ATTACHMENT = {'an_dt': '24-Sep-2026 17:41:45', 'attchmntFile': '-', 'attchmntText': 'Significant increase in volume has been observed in Dc Infotech And Communication Limited.', 'desc': 'Spurt in Volume', 'exchdisstime': '24-Sep-2026 17:41:46', 'seq_id': '106791782', 'sm_isin': 'INE0A1101019', 'sm_name': 'DC Infotech and Communication Limited', 'symbol': 'DCI'}
# Both were skipped by headline in the live run; their PDFs (preferential allotment,
# interim dividend) indeed have no results table
REAL_BSE_GACM = {'NEWSID': '89e47c89-06e0-482d-9615-ae037068d044', 'SCRIP_CD': 531723, 'SLONGNAME': 'GACM Technologies Ltd', 'NEWSSUB': 'Board Meeting Outcome for Meeting Held On Thursday, September 24, 2026', 'HEADLINE': 'OUTCOME FOR MEETING OF THE BOARD OF DIRECTORS HELD ON THURSDAY, SEPTEMBER 24, 2026', 'MORE': '', 'SUBCATNAME': 'Outcome of Board Meeting', 'CATEGORYNAME': 'Board Meeting', 'DT_TM': '2026-09-24T13:38:56.833', 'ATTACHMENTNAME': '9c3460b1-c215-4933-a5d4-1e9ea79f2907.pdf'}
REAL_BSE_SARASWATI = {'NEWSID': '0eff5578-1c84-437f-83f8-d9be6c665d99', 'SCRIP_CD': 544230, 'SLONGNAME': 'Saraswati Saree Depot Ltd', 'NEWSSUB': 'Board Meeting Outcome for Outcome Of Board Meeting Held Today I.E Thursday, September 24, 2026', 'HEADLINE': 'The Board at its meeting held today declared and approved Interim dividend of Rs 3 (30%) per equity share of Rs 10 each for the financial year 2026-27.', 'MORE': '', 'SUBCATNAME': 'Outcome of Board Meeting', 'CATEGORYNAME': 'Board Meeting', 'DT_TM': '2026-09-24T16:40:36.453', 'ATTACHMENTNAME': '9a9b5888-bd45-4b49-9105-785ab453ffc5.pdf'}
REAL_BSE_MASTER_ROW = {'SCRIP_CD': '500002', 'Scrip_Name': 'ABB India Ltd', 'Status': 'Active', 'GROUP': 'A', 'FACE_VALUE': '2.00', 'ISIN_NUMBER': 'INE117A01022', 'INDUSTRY': None, 'scrip_id': 'ABB', 'Segment': 'Equity', 'NSURL': 'https://www.bseindia.com/stock-share-price/abb-india-ltd/abb/500002/', 'Issuer_Name': 'ABB India Limited', 'Mktcap': '150772.81'}

b = pt.normalise_bse(REAL_BSE_OUTCOME)
check("real BSE outcome: id/code/category", b["id"] == "BSE:1b933ea0-6848-4e29-9436-5b1b0dd40cb4" and b["code"] == "544898" and b["category"] == "Board Meeting")
check("real BSE outcome: headline fields", b["headline_fields"] == ["NEWSSUB", "HEADLINE", "SUBCATNAME"], b["headline_fields"])
check("real BSE outcome -> results", pt.board_meeting_kind(b["headline"]) == "results")
check("real BSE time (2-digit fraction)", b["exchange_dt"] == datetime(2026, 9, 24, 20, 37, 48, 960000))
alfa = pt.normalise_bse(REAL_BSE_INTIMATION)
check("real BSE intimation -> intimation", pt.board_meeting_kind(alfa["headline"]) == "intimation")
check("truncated HEADLINE replaced by full MORE text", alfa["headline_fields"] == ["NEWSSUB", "MORE", "SUBCATNAME"]
      and "Appointment of Company Secretary" in alfa["headline"] and "...." not in alfa["headline"], alfa["headline_fields"])
for real in (REAL_BSE_GACM, REAL_BSE_SARASWATI):
    rf = pt.normalise_bse(real)
    check(f"real {rf['company']} outcome -> ambiguous", pt.board_meeting_kind(rf["headline"]) == "ambiguous", rf["headline"])
check("BSE category whitespace stripped", pt.normalise_bse({"NEWSID": "x", "CATEGORYNAME": " Corp Action"})["category"] == "Corp Action")
n = pt.normalise_nse(REAL_NSE_RESULT)
check("real NSE result: id/isin/category/time", n["id"] == "NSE:106792297" and n["isin"] == "INE0DRI01029"
      and n["category"] == "Board Meeting" and n["exchange_dt"] == datetime(2026, 9, 24, 20, 30, 3))
check("real NSE result -> results", pt.board_meeting_kind(n["headline"]) == "results")
check("real NSE vague outcome -> ambiguous", pt.board_meeting_kind(pt.normalise_nse(REAL_NSE_OTHER_OUTCOME)["headline"]) == "ambiguous")
check("NSE '-' attachment treated as none", pt.normalise_nse(REAL_NSE_NO_ATTACHMENT)["attachment_url"] == "")
with mock.patch.object(pt.requests, "get", lambda *a, **k: FakeResponse(200, [REAL_BSE_MASTER_ROW])):
    check("real BSE master row parsed", pt.fetch_bse_master() == {"500002": "INE117A01022"})
check("BSE headers include Origin + Accept (403 without them)", pt.HEADERS.get("Origin") == "https://www.bseindia.com" and "Accept" in pt.HEADERS)

# ─────────────────────────────────────────────────────────────
section("blocked detection")

check("403 Access Denied is blocked", pt.is_blocked_response(ACCESS_DENIED))
check("200 with Access Denied page is blocked", pt.is_blocked_response(FakeResponse(200, text="<h1>Access Denied</h1>")))
check("401 is blocked", pt.is_blocked_response(FakeResponse(401, text="{}")))
check("normal JSON not blocked", not pt.is_blocked_response(FakeResponse(200, {"Table": []})))

fresh_state_dir()
sent_texts = []
with mock.patch.object(pt, "send_telegram_text", side_effect=lambda t: sent_texts.append(t) or True), \
     mock.patch.object(pt.time, "time", return_value=1_000_000.0):
    pt.warn_blocked("BSE", "HTTP 403")
    pt.warn_blocked("BSE", "HTTP 403")
    pt.warn_blocked("NSE", "HTTP 403")
with mock.patch.object(pt, "send_telegram_text", side_effect=lambda t: sent_texts.append(t) or True), \
     mock.patch.object(pt.time, "time", return_value=1_000_000.0 + 3601):
    pt.warn_blocked("BSE", "HTTP 403")
check("blocked Telegram at most hourly per exchange", len(sent_texts) == 3, len(sent_texts))
check("blocked Telegram names exchange", "BSE is blocking" in sent_texts[0])

# ─────────────────────────────────────────────────────────────
section("BSE pagination")

def bse_pages(pages):
    """Fake requests.get serving BSE announcement pages from a dict page → rows."""
    calls = []
    def get(url, headers=None, timeout=None):
        page = int(url.split("pageno=")[1].split("&")[0])
        calls.append(page)
        return FakeResponse(200, {"Table": pages.get(page, [])})
    return get, calls

row = lambda i: {"NEWSID": f"n{i}", "SCRIP_CD": i, "CATEGORYNAME": "Result", "DT_TM": "2026-09-24T10:00:00"}
known = set()
get, calls = bse_pages({1: [row(1), row(2), row(3)], 2: [row(4), row(5), row(6)], 3: []})
with mock.patch.object(pt.requests, "get", get), mock.patch.object(pt.time, "sleep"):
    got, pages = pt.fetch_bse_filings(known)
check("first poll reads until empty page", calls == [1, 2, 3] and len(got) == 6 and pages == 3, (calls, pages))

get, calls = bse_pages({1: [row(0), row(1), row(2)], 2: [row(3), row(4), row(5)], 3: [row(6)]})
with mock.patch.object(pt.requests, "get", get), mock.patch.object(pt.time, "sleep"):
    got, pages = pt.fetch_bse_filings(known)
check("later poll stops at first page with nothing new", calls == [1, 2] and [f["id"] for f in got] == ["BSE:n0"], calls)

get, calls = bse_pages({1: [row(0), row(1)]})
with mock.patch.object(pt.requests, "get", get):
    got, pages = pt.fetch_bse_filings(known)
check("quiet poll: 0 new, 1 page read", got == [] and pages == 1 and calls == [1])

counter = iter(range(100000))
def endless(url, headers=None, timeout=None):
    return FakeResponse(200, {"Table": [row(next(counter)) for _ in range(3)]})
with mock.patch.object(pt.requests, "get", endless), mock.patch.object(pt.time, "sleep"):
    got, pages = pt.fetch_bse_filings(set())
check("pagination capped", len(got) == 3 * pt.BSE_MAX_PAGES and pages == pt.BSE_MAX_PAGES, len(got))

paged = lambda i: dict(row(i), TotalPageCnt=2)
get, calls = bse_pages({1: [paged(1), paged(2)], 2: [paged(3), paged(4)], 3: [paged(5)]})
with mock.patch.object(pt.requests, "get", get), mock.patch.object(pt.time, "sleep"):
    got, pages = pt.fetch_bse_filings(set())
check("stops at TotalPageCnt without requesting further", calls == [1, 2] and len(got) == 4 and pages == 2, calls)

shifted = {1: [row(10), row(11)], 2: [row(11), row(12)], 3: []}   # a new filing pushed row 11 down
get, calls = bse_pages(shifted)
with mock.patch.object(pt.requests, "get", get), mock.patch.object(pt.time, "sleep"):
    got, pages = pt.fetch_bse_filings(set())
check("rows shifted between pages aren't duplicated", [f["id"] for f in got] == ["BSE:n10", "BSE:n11", "BSE:n12"])

def flaky(url, headers=None, timeout=None):
    page = int(url.split("pageno=")[1].split("&")[0])
    if page == 2:
        raise pt.requests.ConnectionError("reset")
    return FakeResponse(200, {"Table": [row(100 + page)]})
known_before = {"x"}
with mock.patch.object(pt.requests, "get", flaky), mock.patch.object(pt.time, "sleep"):
    try:
        pt.fetch_bse_filings(known_before)
        check("a later page failing fails the whole read", False)
    except RuntimeError as e:
        check("a later page failing fails the whole read (nothing half-read, ids untouched)",
              "page 2 failed" in str(e) and known_before == {"x"}, (str(e), known_before))

# One day at a time, oldest first; ids only remembered once every day was read
from datetime import timedelta
day_calls = []
def by_day(url, headers=None, timeout=None):
    day = url.split("strPrevDate=")[1].split("&")[0]
    day_calls.append((day, url.split("strToDate=")[1].split("&")[0], int(url.split("pageno=")[1].split("&")[0])))
    return FakeResponse(200, {"Table": [dict(row(int(day[-2:]) * 10 + i), TotalPageCnt=1) for i in range(2)]})
two_days_ago = date.today() - timedelta(days=2)
known_days = set()
with mock.patch.object(pt.requests, "get", by_day), mock.patch.object(pt.time, "sleep"):
    got, pages = pt.fetch_bse_filings(known_days, since=two_days_ago)
check("catch-up reads BSE one day at a time, oldest first",
      [(a, b) for a, b, _ in day_calls] == [(f"{two_days_ago + timedelta(days=n):%Y%m%d}",) * 2 for n in range(3)]
      and len(got) == 6 and pages == 3 and len(known_days) == 6, (day_calls, pages))

weekday = date(2026, 10, 8)      # a Thursday
check("an empty past weekday can't be right", pt.empty_day_is_wrong(weekday, datetime(2026, 10, 9, 20, 27)))
check("an empty weekend day can be", not pt.empty_day_is_wrong(date(2026, 10, 10), datetime(2026, 10, 12, 9, 0)))
check("today: empty before 10 am is fine, after it isn't",
      not pt.empty_day_is_wrong(weekday, datetime(2026, 10, 8, 7, 30)) and pt.empty_day_is_wrong(weekday, datetime(2026, 10, 8, 10, 0)))

def empty_middle_day(url, headers=None, timeout=None):
    day = url.split("strPrevDate=")[1].split("&")[0]
    if day == f"{two_days_ago + timedelta(days=1):%Y%m%d}":
        return FakeResponse(200, {"Table": []})
    return FakeResponse(200, {"Table": [dict(row(int(day[-2:]) * 10), TotalPageCnt=1)]})
known_days = set()
with mock.patch.object(pt.requests, "get", empty_middle_day), mock.patch.object(pt.time, "sleep"), \
     mock.patch.object(pt, "empty_day_is_wrong", lambda day, now=None: True):
    try:
        pt.fetch_bse_filings(known_days, since=two_days_ago)
        check("an impossible empty day fails the read", False)
    except RuntimeError as e:
        check("an impossible empty day fails the read (9 Oct 2026: 8 Oct was skipped)",
              "empty answer for" in str(e) and known_days == set(), (str(e), known_days))
with mock.patch.object(pt.requests, "get", empty_middle_day), mock.patch.object(pt.time, "sleep"), \
     mock.patch.object(pt, "empty_day_is_wrong", lambda day, now=None: False):
    got, pages = pt.fetch_bse_filings(set(), since=two_days_ago)
check("a believable empty day is just empty", len(got) == 2, got)

pt._empty_answers.clear()
outcomes = []
with mock.patch.object(pt.requests, "get", empty_middle_day), mock.patch.object(pt.time, "sleep"), \
     mock.patch.object(pt, "empty_day_is_wrong", lambda day, now=None: True):
    for _ in range(pt.EMPTY_ANSWERS_BELIEVED):
        try:
            outcomes.append(len(pt.fetch_bse_filings(set(), since=two_days_ago)[0]))
        except RuntimeError:
            outcomes.append("failed")
check("a day still empty after 10 polls is taken as a quiet day (a holiday can't stall the catch-up)",
      outcomes == ["failed"] * (pt.EMPTY_ANSWERS_BELIEVED - 1) + [2], outcomes)
pt._empty_answers.clear()

with mock.patch.object(pt.requests, "get", side_effect=pt.requests.ConnectionError("down")):
    try:
        pt.fetch_bse_filings(set())
        check("page-1 failure raises (not '0 new')", False)
    except pt.requests.ConnectionError:
        check("page-1 failure raises (not '0 new')", True)

with mock.patch.object(pt.requests, "get", lambda *a, **k: ACCESS_DENIED):
    try:
        pt.fetch_bse_filings(set())
        check("BSE Access Denied raises ExchangeBlocked", False)
    except pt.ExchangeBlocked:
        check("BSE Access Denied raises ExchangeBlocked", True)

# ─────────────────────────────────────────────────────────────
section("NSE session")

class FakeSession:
    created = 0
    script = []
    def __init__(self):
        FakeSession.created += 1
        self.headers = {}
    def get(self, url, timeout=None):
        if url == pt.NSE_HOME:
            return FakeResponse(200, text="<html>home</html>")
        return FakeSession.script.pop(0)

FakeSession.script = [FakeResponse(401, text="{}"), FakeResponse(200, [nse_row])]
with mock.patch.object(pt.requests, "Session", FakeSession):
    nse = pt.NseClient()
    got = pt.fetch_nse_filings(nse)
check("NSE refreshes cookies on 401 and retries", FakeSession.created == 2 and len(got) == 1, FakeSession.created)

FakeSession.created = 0
FakeSession.script = [FakeResponse(403, text="Access Denied"), ACCESS_DENIED]
with mock.patch.object(pt.requests, "Session", FakeSession):
    try:
        pt.fetch_nse_filings(pt.NseClient())
        check("NSE still denied after refresh raises ExchangeBlocked", False)
    except pt.ExchangeBlocked:
        check("NSE still denied after refresh raises ExchangeBlocked", True)

FakeSession.created = 0
FakeSession.script = [FakeResponse(200, {"data": [nse_row]})]
with mock.patch.object(pt.requests, "Session", FakeSession):
    got = pt.fetch_nse_filings(pt.NseClient())
check("NSE {'data': [...]} shape accepted", len(got) == 1)

# ─────────────────────────────────────────────────────────────
section("scrip master & dedup keys")

bse_master_json = [{"SCRIP_CD": 500325, "ISIN_NUMBER": "INE002A01018"}, {"SCRIP_CD": "999", "ISIN_NUMBER": ""}]
with mock.patch.object(pt.requests, "get", lambda *a, **k: FakeResponse(200, bse_master_json)):
    check("BSE master parsed", pt.fetch_bse_master() == {"500325": "INE002A01018"})

class FakeNse:
    def __init__(self, responses):
        self.responses = responses
    def get(self, url, timeout=15):
        return self.responses[url] if url in self.responses else FakeResponse(404, text="nf")

equity_csv = ("SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE\n"
              "RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995,10,1,INE002A01018,10\n"
              "M&M,Mahindra & Mahindra Limited,EQ,01-JAN-1990,5,1,INE101A01026,5\n")
fake_nse = FakeNse({pt.NSE_MASTER_URLS[0]: FakeResponse(200, text=equity_csv)})
check("NSE master parsed (padded headers, SME list 404 tolerated)",
      pt.fetch_nse_master(fake_nse) == {"RELIANCE": "INE002A01018", "M&M": "INE101A01026"})

fresh_state_dir()
calls = {"bse": 0, "nse": 0}
def fb():
    calls["bse"] += 1; return {"500325": "INE002A01018"}
def fn(nse):
    calls["nse"] += 1; return {"RELIANCE": "INE002A01018"}
with mock.patch.object(pt, "fetch_bse_master", fb), mock.patch.object(pt, "fetch_nse_master", fn):
    m1 = pt.load_scrip_master(None)
    m2 = pt.load_scrip_master(None)
check("master fetched once per day", calls == {"bse": 1, "nse": 1} and m2["updated"] == date.today().isoformat(), calls)
check("master content", m2["bse"]["500325"] == "INE002A01018" and m2["nse"]["RELIANCE"] == "INE002A01018")

fresh_state_dir()
pt.save_json(pt.SCRIP_MASTER_FILE, {"bse": {"1": "INE000000011"}, "nse": {}, "updated": "2000-01-01"})
def boom():
    raise pt.ExchangeBlocked("HTTP 403")
with mock.patch.object(pt, "fetch_bse_master", boom), mock.patch.object(pt, "fetch_nse_master", lambda nse: {}):
    m = pt.load_scrip_master(None)
check("failed refresh keeps cache and doesn't stamp today", m["bse"] == {"1": "INE000000011"} and m["updated"] == "2000-01-01")

master = {"bse": {"531694": "INE111A01011"}, "nse": {"BAJAJ-AUTO": "INE917I01010"}}
migrated = pt.migrate_processed_keys(
    {"531694_Q4FY26", "532386_Q1FY27", "NSE-BAJAJ-AUTO_Q1FY27", "INE002A01018_Q1FY27", "BSE-777_Q1FY27", "weird"}, master)
check("legacy BSE key -> ISIN", "INE111A01011_Q4FY26" in migrated)
check("legacy BSE key w/o ISIN -> BSE- fallback", "BSE-532386_Q1FY27" in migrated)
check("NSE fallback key -> ISIN", "INE917I01010_Q1FY27" in migrated)
check("ISIN key untouched", "INE002A01018_Q1FY27" in migrated)
check("unknown fallback kept", "BSE-777_Q1FY27" in migrated and "weird" in migrated)
check("no legacy keys left", "531694_Q4FY26" not in migrated and len(migrated) == 6, migrated)

check("processed_key with ISIN", pt.processed_key(g, "INE101A01026", "Q1FY27") == "INE101A01026_Q1FY27")
check("processed_key fallback", pt.processed_key(f, None, "Q1FY27") == "BSE-500325_Q1FY27")
check("lookup_isin from BSE master", pt.lookup_isin(f, {"bse": {"500325": "INE002A01018"}, "nse": {}}) == "INE002A01018")
check("lookup_isin prefers filing's own ISIN", pt.lookup_isin(g, {"bse": {}, "nse": {"M&M": "XX"}}) == "INE101A01026")

# ─────────────────────────────────────────────────────────────
section("page selection")

cover = "ABC Ltd\nTo BSE Ltd\nOutcome of board meeting\nThe Board approved the Unaudited Standalone and Consolidated Financial Results for the quarter ended June 30, 2026.\n" * 3
table = "Revenue from operations 100 90 80\nTotal income 105\nProfit before tax 20\nEarnings per share 2.1\n" + "row\n" * 30
standalone = "ABC LIMITED\nCIN L123\nStatement of Unaudited Standalone Financial Results for the quarter ended 30 June 2026\n" + table
consolidated = "ABC LIMITED\nCIN L123\nStatement of Unaudited Consolidated Financial Results for the quarter ended 30 June 2026\n" + table
notes = "Notes: 1. The above results were reviewed by the audit committee...\n"
generic = "XYZ LIMITED\nStatement of Unaudited Financial Results for the quarter ended 30 June 2026\n" + table

check("prefers consolidated", pt.find_table_pages([cover, standalone, notes, consolidated, notes]) == ([3, 4], "consolidated"))
check("falls back to standalone, cover skipped", pt.find_table_pages([cover, standalone, notes]) == ([1, 2], "standalone"))
check("generic table on single page", pt.find_table_pages([generic]) == ([0], "generic"))
check("cover letter alone isn't a table", pt.find_table_pages([cover]) == ([], None))
check("blank pages -> nothing", pt.find_table_pages(["", ""]) == ([], None))
untitled = "ABC LIMITED\n(Rs in lakhs)\nParticulars Q1 Q4 Q1\n" + table
check("table without a recognised title -> untitled, ranked last",
      pt.find_table_pages([untitled, standalone]) == ([1], "standalone") and pt.find_table_pages([cover, untitled]) == ([1], "untitled"))
check("'Statement of Standalone Results' heading", pt.classify_result_page("Statement of Standalone Results for the quarter\n" + table) == "standalone")
check("'Results for the quarter ended' heading", pt.classify_result_page("XYZ Ltd\nResults for the quarter ended 30.06.2026\n" + table) == "generic")

# Sector formats (lines from real filings). Gowra Leasing (NBFC, 26 Sep 2026)
# matched only one of the old fixed phrases and was skipped as "no results table".
NBFC_PAGE = """Gowra Leasing & Finance Limited
CIN: L65910TG1993PLC015349
Audited Financial Results for the Quarter ended 31.03.2026
(Rs. In Lakhs)
I Revenue from operations
Interest 295.22 286.49 212.15 1130.17 519.74
Total Revenue from Operations 303.81 286.58 214.96 1138.94 523.70
III Total Revenue (I + II) 320.64 289.10 305.95 1159.94 767.83
Finance costs 25.68 74.42 41.25 249.26 80.93
V Total Expenses 60.57 107.16 72.21 385.85 183.41
VI Profit/(Loss) before Tax (III-IV) 260.07 181.94 233.74 774.09 584.42
XII Earning per equity share
Basic 2.53 2.48 3.84 9.48 10.81"""
BANK_PAGE = """XYZ BANK LIMITED
Statement of Unaudited Standalone Financial Results for the quarter ended 30.09.2026
1 Interest earned (a)+(b)+(c)+(d) 1,234.5
2 Other Income 210.3
4 Interest Expended 678.9
5 Operating Expenses (i)+(ii) 345.6
7 Operating Profit before Provisions and Contingencies 420.1
11 Net Profit / (Loss) from Ordinary Activities after tax 250.2
Basic EPS 4.12"""
BROKER_PAGE = """ABC SECURITIES LIMITED
Statement of Audited Results for the Quarter ended 31.03.2026
Revenue from Operations 12.3
Total Income 13.1
Total Expenses 10.2
Profit / (Loss) before exceptional items and tax 2.9
Net Profit / (Loss) for the period 2.1
Earnings per equity share (Basic) 0.45"""
CASH_FLOW_PAGE = """Gowra Leasing & Finance Limited
CASH FLOW STATEMENT FOR THE YEAR ENDED 31ST MARCH 2026
A. Operating activities
Profit before tax 774.09 584.41
Adjustments for finance costs 0.00 0.00
Adjustments for interest income 0.00 0.00"""
CLARIFICATION_LETTER = """INANI SECURITIES LTD
Subject: Clarification regarding discrepancy in Cash Flow Statement and non-disclosure of EPS
the Basic and Diluted EPS figures were inadvertently reported as 0 in the relevant filing."""
check("NBFC table (Total Revenue, Profit/(Loss) before Tax, Earning per equity share)",
      pt.classify_result_page(NBFC_PAGE) == "generic", pt.classify_result_page(NBFC_PAGE))
check("bank table (Interest earned / expended, operating profit before provisions)",
      pt.classify_result_page(BANK_PAGE) == "standalone", pt.classify_result_page(BANK_PAGE))
check("broker table ('Statement of Audited Results', Profit / (Loss) before … tax)",
      pt.classify_result_page(BROKER_PAGE) is not None, pt.classify_result_page(BROKER_PAGE))
check("cash flow statement isn't a results table", pt.classify_result_page(CASH_FLOW_PAGE) is None)
check("clarification letter isn't a results table", pt.classify_result_page(CLARIFICATION_LETTER) is None)
check("results page preferred over its cash flow page",
      pt.find_table_pages([CLARIFICATION_LETTER, NBFC_PAGE, CASH_FLOW_PAGE]) == ([1, 2], "generic"))
check("a results heading beats a cash-flow word in it",
      pt.classify_result_page("Statement of Audited Financial Results and Cash Flow for the quarter\n" + table) == "generic")

# ─────────────────────────────────────────────────────────────
section("normalise_financials")

raw = {"basis": "Consolidated", "unit": "Lakhs", "period_end": "30.06.2026",
       "prev_period_end": "31-Mar-2026", "ly_period_end": "30/06/2025",
       "revenue_from_operations": [66258, "140,264", 15459],
       "pat": [41939, None, "abc"], "basic_eps": [0.68, 1.2, 0.03],
       "pbt": "oops", "ebitda": [1, 2], "finance_cost": [True, float("nan"), 3]}
fin = pt.normalise_financials(raw)
check("lakhs -> crores", fin["revenue_from_operations"] == [662.58, 1402.64, 154.59], fin["revenue_from_operations"])
check("EPS not unit-converted", fin["basic_eps"] == [0.68, 1.2, 0.03])
check("bad strings -> None", fin["pat"] == [419.39, None, None], fin["pat"])
check("non-list -> 3 Nones", fin["pbt"] == [None, None, None])
check("short list padded", fin["ebitda"] == [0.01, 0.02, None], fin["ebitda"])
check("bool/nan rejected", fin["finance_cost"] == [None, None, 0.03], fin["finance_cost"])
check("all keys present", all(len(fin[k]) == 3 for k in pt.FIN_KEYS))
check("basis lowercased", fin["basis"] == "consolidated")
check("period_end dd.mm.yyyy -> ISO", fin["period_end"] == "2026-06-30")
check("previous and last-year column dates parsed",
      fin["prev_period_end"] == "2026-03-31" and fin["ly_period_end"] == "2025-06-30", fin)
check("missing column dates -> None", pt.normalise_financials({"unit": "crores"})["prev_period_end"] is None)
check("prompt asks for all three column dates",
      all(f'"{k}":"YYYY-MM-DD"' in pt.EXTRACTION_PROMPT for k in ("period_end", "prev_period_end", "ly_period_end")))
for value, want in [("2026-06-30", "2026-06-30"), ("30-Jun-2026", "2026-06-30"), ("June 30, 2026", "2026-06-30"),
                    ("30/06/2026", "2026-06-30"), (None, None), ("", None), ("Q1 FY27", None), (20260630, None)]:
    check(f"parse_period_end {value!r}", pt.parse_period_end(value) == want, pt.parse_period_end(value))
check("unknown unit -> None", pt.normalise_financials({"unit": "bananas"}) is None)
check("missing unit -> None", pt.normalise_financials({}) is None)
check("millions", pt.normalise_financials({"unit": "mn", "pat": [10]})["pat"][0] == 1.0)
check("non-dict -> None", pt.normalise_financials([1, 2]) is None)
check("has_core_values false w/o revenue", not pt.has_core_values(mk(pat=[1])))

# ─────────────────────────────────────────────────────────────
section("scoring")

s, bd = pt.compute_pead_score(mk(**GOOD))
check("normal company scores growth", s >= 35 and "Turnaround" not in bd, f"{s} {bd}")

s_ta, bd_ta = pt.compute_pead_score(mk(revenue_from_operations=[150, 120, 100], pat=[30, 20, -10], basic_eps=[3, 2, -1],
                                       pbt=[40, 30, -5], finance_cost=[2, 2, 2], depreciation=[5, 5, 5]))
check("turnaround halves PAT/EPS", bd_ta["EPS Surprise"][1] == "7.5/15" and bd_ta["PAT Growth YoY"][1] == "5.0/10", bd_ta)
check("turnaround label", "Turnaround" in bd_ta)

_, bd1 = pt.compute_pead_score(mk(revenue_from_operations=[150, 120, 100], pat=[3, 2, 0.5], basic_eps=[3, 2, 0.5]))
check("small PAT base skips only PAT+EPS",
      bd1["EPS Surprise"][0] == "small base" and bd1["PAT Growth YoY"][0] == "small base"
      and bd1["Revenue Growth YoY"] == ("50.0%", "10.0/10") and bd1["Revenue QoQ"][0] == "25.0%", bd1)

_, bd2 = pt.compute_pead_score(mk(revenue_from_operations=[15, 12, 5], pat=[3, 2, 1.5], basic_eps=[3, 2, 1.5]))
check("small last-year revenue skips only revenue YoY",
      bd2["Revenue Growth YoY"] == ("small base", "0.0/10") and bd2["Revenue QoQ"][0] == "25.0%"
      and bd2["PAT Growth YoY"][0] == "100.0%", bd2)

_, bd3 = pt.compute_pead_score(mk(revenue_from_operations=[15, 5, 12], pat=[3, 2, 1.5], basic_eps=[3, 2, 1.5]))
check("small last-quarter revenue skips only revenue QoQ",
      bd3["Revenue QoQ"] == ("small base", "0.0/5") and bd3["Revenue Growth YoY"][0] == "25.0%", bd3)

_, bd4 = pt.compute_pead_score(mk(revenue_from_operations=[150, 120, 100], pat=[3, 2, -0.5], basic_eps=[3, 2, -0.5]))
check("tiny loss base = small base, no turnaround label", "Turnaround" not in bd4 and bd4["PAT Growth YoY"][0] == "small base", bd4)

s_rej, bd_rej = pt.compute_pead_score(mk(revenue_from_operations=[150, 120, 100], pat=[-3, 2, 1]))
check("negative PAT still rejected", s_rej == 0 and "Rejected" in bd_rej)

# ─────────────────────────────────────────────────────────────
section("telegram")

sent = {}
def fake_post(url, json=None, timeout=None):
    sent["text"] = json["text"]
    return FakeResponse(200, {"ok": True})

nse_filing = pt.normalise_nse(nse_row)
fin_ta = mk(revenue_from_operations=[150, 120, 100], pat=[30, 20, -10], basic_eps=[3, 2, -1], period_end="2026-06-30", basis="consolidated")
with mock.patch.object(pt.requests, "post", fake_post):
    pt.send_telegram(nse_filing, s_ta, bd_ta, fin_ta, "Q1FY27", "24 Sep 2026  17:32:00", "1m 0s")
t = sent["text"]
check("company & escaped", "Mahindra &amp; Mahindra" in t and "Mahindra & Mahindra" not in t)
check("symbol & escaped", "<code>M&amp;M</code>" in t)
check("source exchange shown", "Source:</b> <code>NSE</code>" in t)
check("period shown", "Q1FY27 (to 2026-06-30)" in t)
check("NSE quote link", "nseindia.com/get-quotes/equity?symbol=M%26M" in t)
check("turnaround header", "TURNAROUND" in t)
check("basis in header", "(₹ Cr, consolidated)" in t)
import re as _re
stripped = _re.sub(r"</?(b|pre|code)>", "", t)
check("no stray < > after escaping", "<" not in stripped and ">" not in stripped.replace("&gt;", ""))

with mock.patch.object(pt.requests, "post", side_effect=pt.requests.ConnectionError("down")):
    check("telegram network error guarded", pt.send_telegram_text("x") is False)

# ─────────────────────────────────────────────────────────────
section("CSV")

NEW_COLUMNS = ["exchange", "filing_url", "period_end", "quarter", "basis", "unit", "check", "eps_pq"]
check("CSV header ends with the added columns", pt.CSV_HEADER[-8:] == NEW_COLUMNS, pt.CSV_HEADER)

d = fresh_state_dir()
with open(pt.RESULTS_CSV, "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(pt.CSV_HEADER[:15])                         # pre-NSE file: none of the added columns
    w.writerow(["2026-05-27 23:40:34", "Old Co", "540026", "0.0"] + [""] * 11)
pt.initialize_csv()
pt.save_result_csv(nse_filing, 40.0, mk(period_end="2026-09-30", basis="consolidated", **GOOD), "Q2FY27")
rows = list(csv.reader(open(pt.RESULTS_CSV, encoding="utf-8")))
check("pre-NSE CSV gains all eight added columns",
      rows[0][-8:] == NEW_COLUMNS and rows[1][-8:] == ["BSE", "", "", "", "", "", "", ""], rows[1])
check("new row records exchange, filing link, period_end, quarter, basis, unit, blank check, last quarter's EPS",
      rows[2][-8:] == ["NSE", nse_filing["attachment_url"], "2026-09-30", "Q2FY27", "consolidated", "crores", "", "2.0"]
      and len(rows[2]) == len(pt.CSV_HEADER), rows[2])
saved = list(csv.DictReader(open(pt.RESULTS_CSV, encoding="utf-8")))[-1]
check("all three EPS columns saved: this quarter, last quarter, a year ago",
      (saved["eps_cq"], saved["eps_pq"], saved["eps_ly"]) == ("3.0", "2.0", "1.0"), saved)
pt.initialize_csv()
check("migration is idempotent", list(csv.reader(open(pt.RESULTS_CSV, encoding="utf-8")))[0] == pt.CSV_HEADER)

d = fresh_state_dir()
with open(pt.RESULTS_CSV, "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(pt.CSV_HEADER[:17])                         # the file as it was until 2026-10-05
    w.writerow(["2026-09-24 21:31:06", "Purple Style Labs Limited", "PERNIASPOP", "0.0"] + [""] * 11
               + ["NSE", "https://nsearchives.nseindia.com/corporate/x.pdf"])
pt.initialize_csv()
rows = list(csv.reader(open(pt.RESULTS_CSV, encoding="utf-8")))
check("existing columns kept, only period_end/quarter/basis/unit/check/eps_pq added",
      rows[0] == pt.CSV_HEADER and rows[1][-8:] == ["NSE", "https://nsearchives.nseindia.com/corporate/x.pdf", "", "", "", "", "", ""], rows)

d = fresh_state_dir()
with open(pt.RESULTS_CSV, "w", newline="", encoding="utf-8") as fh:
    csv.writer(fh).writerow(pt.CSV_HEADER[:-2])            # the file as it was until 2026-10-07
pt.initialize_csv()
flagged = mk(period_end="2026-09-30", **GOOD)
flagged["check"] = "this quarter: x"
pt.save_result_csv(nse_filing, 12.0, flagged, "Q2FY27")
rows = list(csv.reader(open(pt.RESULTS_CSV, encoding="utf-8")))
check("check column added and written", rows[0] == pt.CSV_HEADER and rows[1][-2] == "this quarter: x", rows)

d = fresh_state_dir()
with open(pt.RESULTS_CSV, "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(pt.CSV_HEADER[:-1])                         # the file as it was until 2026-10-18
    w.writerow(["2026-10-17 22:49:00", "E2E Networks Limited", "E2E", "37.5"] + [""] * 11
               + ["NSE", "https://nsearchives.nseindia.com/corporate/e.pdf", "2026-09-30", "Q2FY27", "consolidated", "crores", ""])
pt.initialize_csv()
rows = list(csv.reader(open(pt.RESULTS_CSV, encoding="utf-8")))
check("eps_pq added to an existing file, blank for its old rows",
      rows[0] == pt.CSV_HEADER and len(rows[1]) == len(pt.CSV_HEADER) and rows[1][-1] == "" and rows[1][-2] == "", rows)

# ─────────────────────────────────────────────────────────────
section("main loop")

class Stop(Exception):
    pass

fetch_since = []      # (exchange, since) each run_main fetch was asked for

def run_main(polls, bse=lambda known: [], nse=lambda client: [], extract=None, master=None,
             download=None, on_wait=None, argv=(), events=None):
    """Run pt.main() for a number of polls with everything external mocked.

    Between polls the default waits for queued ambiguous checks to finish and
    applies them, so runs are deterministic; on_wait(state, poll) replaces that.
    `events`, when given, gets "alert" each time an alert is sent, in order.
    """
    counter = {"n": 0}
    def fake_wait(state, seconds):
        counter["n"] += 1
        if on_wait:
            on_wait(state, counter["n"])
        else:
            state.checker.todo.join()
            pt.drain_checks(state)
        if counter["n"] >= polls:
            raise Stop()
    alerts = []
    def fake_send(filing, *a):
        alerts.append(filing["exchange"] + ":" + filing["company"])
        if events is not None:
            events.append("alert")
        return True
    def fake_bse(known, since=None):
        fetch_since.append(("BSE", since))
        return bse(known), 1
    def fake_nse(client, since=None):
        fetch_since.append(("NSE", since))
        return nse(client)
    with mock.patch.object(pt, "fetch_bse_filings", fake_bse), \
         mock.patch.object(pt, "fetch_nse_filings", fake_nse), \
         mock.patch.object(pt, "fetch_bse_master", lambda: dict((master or {}).get("bse", {}))), \
         mock.patch.object(pt, "fetch_nse_master", lambda client: dict((master or {}).get("nse", {}))), \
         mock.patch.object(pt, "download_pdf", download or (lambda filing, client: b"%PDF-" + filing["attachment_url"].encode())), \
         mock.patch.object(pt, "extract_financials", extract), \
         mock.patch.object(pt, "send_telegram", fake_send), \
         mock.patch.object(pt, "send_telegram_text", lambda text: True), \
         mock.patch.object(pt, "wait_for_checks", fake_wait):
        try:
            pt.main(list(argv))
        except Stop:
            pass
    return alerts

ambiguous_calls = []

def counting_extract(behaviour):
    calls = {}
    def extract(pdf, ambiguous=False, timings=None, **kw):
        name = pdf.decode()[len("%PDF-"):]
        calls[name] = calls.get(name, 0) + 1
        if ambiguous:
            ambiguous_calls.append(name)
        result = behaviour[name]
        if isinstance(result, Exception):
            raise result
        return result
    return extract, calls

good_fin = mk(period_end="2026-06-30", **GOOD)
bse_raw = lambda nid, scrip, cat, when, sub="", attach=None: {
    "NEWSID": nid, "SCRIP_CD": scrip, "SLONGNAME": f"Co {scrip}", "CATEGORYNAME": cat, "NEWSSUB": sub,
    "ATTACHMENTNAME": attach or f"{nid}.pdf", "DT_TM": when}
nse_raw = lambda seq, sym, isin, when, desc="Financial Results": {
    "seq_id": seq, "symbol": sym, "sm_name": f"Co {sym}", "sm_isin": isin, "desc": desc, "attchmntText": "",
    "attchmntFile": f"https://nse/{seq}.pdf", "exchdisstime": when}

# Cross-exchange dedup: NSE published first
fresh_state_dir()
bse_f = pt.normalise_bse(bse_raw("b1", 500001, "Result", "2026-09-24T10:02:00"))
nse_f = pt.normalise_nse(nse_raw("s1", "ABC", "INE000A01011", "24-Sep-2026 10:00:00"))
extract, calls = counting_extract({bse_f["attachment_url"]: good_fin, nse_f["attachment_url"]: good_fin})
alerts = run_main(1, bse=lambda k: [bse_f], nse=lambda c: [nse_f], extract=extract,
                  master={"bse": {"500001": "INE000A01011"}, "nse": {}})
processed = set(json.load(open(pt.PROCESSED_SCRIPS_FILE)))
seen = set(json.load(open(pt.SEEN_FILE)))
check("earlier NSE filing processed, BSE duplicate skipped", list(calls) == [nse_f["attachment_url"]] and alerts == ["NSE:Co ABC"], (calls, alerts))
check("ISIN quarter key stored", processed == {"INE000A01011_Q1FY27"}, processed)
check("both filings marked seen", {"BSE:b1", "NSE:s1"} <= seen)

# ...and BSE first
fresh_state_dir()
bse_f = pt.normalise_bse(bse_raw("b1", 500001, "Result", "2026-09-24T09:58:00"))
extract, calls = counting_extract({bse_f["attachment_url"]: good_fin, nse_f["attachment_url"]: good_fin})
alerts = run_main(1, bse=lambda k: [bse_f], nse=lambda c: [nse_f], extract=extract,
                  master={"bse": {"500001": "INE000A01011"}, "nse": {}})
check("earlier BSE filing wins", list(calls) == [bse_f["attachment_url"]] and alerts == ["BSE:Co 500001"], (calls, alerts))

# PDF period catches a duplicate the filing date can't (late Q4 filer)
fresh_state_dir()
pt.save_processed_scrips({"INE000A01011_Q4FY26"})
late = pt.normalise_nse(nse_raw("s2", "ABC", "INE000A01011", "05-Jul-2026 10:00:00"))
extract, calls = counting_extract({late["attachment_url"]: mk(period_end="2026-03-31", **GOOD)})
alerts = run_main(1, nse=lambda c: [late], extract=extract)
check("period_end duplicate: extracted once, no alert, seen", calls == {late["attachment_url"]: 1} and alerts == []
      and "NSE:s2" in set(json.load(open(pt.SEEN_FILE))), (calls, alerts))

# Retries persist across restarts; board meeting intimation skipped; crash survived
fresh_state_dir()
failing = pt.normalise_bse(bse_raw("f1", 111, "Result", "2026-09-24T10:00:00"))
crashing = pt.normalise_bse(bse_raw("c1", 444, "Result", "2026-09-24T10:00:00"))
intimation = pt.normalise_bse(bse_raw("i1", 333, "Board Meeting", "2026-09-24T10:00:00", sub="Board Meeting Intimation for Financial Results"))
outcome = pt.normalise_bse(bse_raw("o1", 222, "Board Meeting", "2026-09-24T10:00:00", sub="Board Meeting Outcome for Financial Results"))
behaviour = {failing["attachment_url"]: None,                          # model call failed → retried
             crashing["attachment_url"]: RuntimeError("kaboom"),
             outcome["attachment_url"]: good_fin}
extract, calls = counting_extract(behaviour)
feed = lambda k: [failing, crashing, intimation, outcome]
run_main(2, bse=feed, extract=extract)
retries = json.load(open(pt.RETRIES_FILE))
check("retry counts persisted", retries == {"BSE:f1": 2, "BSE:c1": 2}, retries)
run_main(3, bse=feed, extract=extract)       # "restart"
seen = set(json.load(open(pt.SEEN_FILE)))
check("failing filing: 1 + 3 retries across restart, then given up",
      calls.get(failing["attachment_url"]) == 4 and "BSE:f1" in seen, calls)
check("crashing filing retried and loop survived", calls.get(crashing["attachment_url"]) == 4 and "BSE:c1" in seen)
check("retry file emptied after giving up", json.load(open(pt.RETRIES_FILE)) == {})
check("intimation skipped & seen", intimation["attachment_url"] not in calls and "BSE:i1" in seen)
check("outcome processed once", calls.get(outcome["attachment_url"]) == 1)
check("no ISIN -> BSE fallback key", "BSE-222_Q1FY27" in set(json.load(open(pt.PROCESSED_SCRIPS_FILE))))

# No results table / not a PDF: skipped once, no retry
fresh_state_dir()
no_table = pt.normalise_bse(bse_raw("t1", 666, "Result", "2026-09-24T10:00:00"))
zipped = pt.normalise_nse(nse_raw("z1", "ZIPCO", "INE777A01017", "24-Sep-2026 10:00:00"))
extract, calls = counting_extract({no_table["attachment_url"]: pt.SkipFiling("no results table found", status="NONE")})
def download(filing, client):
    if filing["exchange"] == "NSE":
        return b"PK\x03\x04zipdata"
    return b"%PDF-" + filing["attachment_url"].encode()
run_main(3, bse=lambda k: [no_table], nse=lambda c: [zipped], extract=extract, download=download)
seen = set(json.load(open(pt.SEEN_FILE)))
no_retries = not os.path.exists(pt.RETRIES_FILE) or json.load(open(pt.RETRIES_FILE)) == {}
check("no-table filing: one attempt, seen, no retry", calls == {no_table["attachment_url"]: 1} and "BSE:t1" in seen and no_retries, calls)
check("non-PDF attachment skipped, model never called", "NSE:z1" in seen and zipped["attachment_url"] not in calls)
check("skipped filings not processed", not set(json.load(open(pt.PROCESSED_SCRIPS_FILE))))

# Ambiguous outcomes: PDF checked for a results table; model only if found
fresh_state_dir()
ambiguous_calls.clear()
gacm = pt.normalise_bse(REAL_BSE_GACM)
saraswati = pt.normalise_bse(REAL_BSE_SARASWATI)
vague_nse = pt.normalise_nse(REAL_NSE_OTHER_OUTCOME)
alfa = pt.normalise_bse(REAL_BSE_INTIMATION)
extract, calls = counting_extract({
    gacm["attachment_url"]: pt.SkipFiling("no results table found", status="NONE"),        # like the real PDF
    saraswati["attachment_url"]: pt.SkipFiling("no results table found", status="NONE"),   # like the real PDF
    vague_nse["attachment_url"]: good_fin,                                   # results inside
})
with LogCapture() as logs:
    alerts = run_main(2, bse=lambda k: [gacm, saraswati, alfa], nse=lambda c: [vague_nse], extract=extract)
seen = set(json.load(open(pt.SEEN_FILE)))
check("ambiguous outcomes are downloaded and checked, flagged ambiguous",
      sorted(ambiguous_calls) == sorted([gacm["attachment_url"], saraswati["attachment_url"], vague_nse["attachment_url"]]), ambiguous_calls)
check("ambiguous without table: once, seen, no retry",
      calls[gacm["attachment_url"]] == 1 and calls[saraswati["attachment_url"]] == 1
      and {gacm["id"], saraswati["id"]} <= seen and not os.path.exists(pt.RETRIES_FILE))
check("ambiguous with table: scored and alerted", alerts == ["NSE:Global Education Limited"], alerts)
check("intimation still skipped by headline", alfa["attachment_url"] not in calls and alfa["id"] in seen)
check("CHECK line when an ambiguous outcome is queued",
      logs.has_line("CHECK", "GACM Technologies", "ambiguous outcome → queued for a results-table check"))
check("NONE line once the check finds no table",
      logs.has_line("NONE", "GACM Technologies", "ambiguous outcome → no results table found", "⏱ download"))
check("ambiguous NONE line has no headline (only Result filings get one)",
      not logs.has_line("NONE", "GACM Technologies", "headline:"))
check("intimation not printed, only counted",
      not any("Alfa Ica" in m for _, m in logs.terminal())
      and logs.has_line("DEBUG", "Alfa Ica India Ltd board meeting (intimation)")
      and logs.has_line("POLL", "1 intimation skipped"), logs.terminal())

# Priority: clear results first (oldest first), ambiguous outcomes after, on the checker thread
fresh_state_dir()
order = []
def ordered_extract(pdf, ambiguous=False, timings=None, **kw):
    order.append((pdf.decode()[len("%PDF-"):].rsplit("/", 1)[-1], threading.current_thread().name))
    return good_fin
amb_old = pt.normalise_bse(bse_raw("a1", 701, "Board Meeting", "2026-09-24T09:00:00", sub="Board Meeting Outcome for Meeting Held Today"))
amb_new = pt.normalise_bse(bse_raw("a2", 702, "Board Meeting", "2026-09-24T09:15:00", sub="Outcome of Board Meeting held today"))
clear_new = pt.normalise_bse(bse_raw("c1", 703, "Result", "2026-09-24T10:00:00"))
clear_old = pt.normalise_nse(nse_raw("c0", "CLR", "INE703A01010", "24-Sep-2026 09:30:00"))
alerts = run_main(1, bse=lambda k: [amb_new, clear_new, amb_old], nse=lambda c: [clear_old], extract=ordered_extract)
check("clear results first (oldest first), then ambiguous (oldest first)",
      [name for name, _ in order] == ["c0.pdf", "c1.pdf", "a1.pdf", "a2.pdf"], order)
check("clear filings run on the main thread, ambiguous on the checker",
      [thread for _, thread in order] == ["MainThread", "MainThread", "checker", "checker"], order)
check("alerts follow the same priority", alerts == ["NSE:Co CLR", "BSE:Co 703", "BSE:Co 701", "BSE:Co 702"], alerts)

# A slow ambiguous check doesn't hold up a clear result from the next poll
fresh_state_dir()
started, release = threading.Event(), threading.Event()
slow_amb = pt.normalise_nse(nse_raw("s50", "SLOW", "INE050A01010", "24-Sep-2026 09:00:00", desc="Outcome of Board Meeting"))
next_clear = pt.normalise_bse(bse_raw("c50", 750, "Result", "2026-09-24T10:00:00"))
amb_calls = []
def slow_extract(pdf, ambiguous=False, timings=None, **kw):
    if ambiguous:
        amb_calls.append(1)
        started.set()
        release.wait(10)
    return good_fin
seen_during = {}
def hold_then_release(state, poll):
    if poll == 1:
        started.wait(10)                 # checker is now busy with the ambiguous PDF
    else:
        seen_during["processed"] = sorted(state.processed)
        seen_during["in_flight"] = slow_amb["id"] in state.checker.in_flight
        release.set()
        state.checker.todo.join()
        pt.drain_checks(state)
polls = {"n": 0}
def bse_second_poll(known):
    polls["n"] += 1
    return [next_clear] if polls["n"] == 2 else []
alerts = run_main(2, bse=bse_second_poll, nse=lambda c: [slow_amb], extract=slow_extract, on_wait=hold_then_release)
check("clear result scored while the ambiguous check was still running",
      seen_during == {"processed": ["BSE-750_Q1FY27"], "in_flight": True}, seen_during)
check("clear alert first, ambiguous alert once its check finished", alerts == ["BSE:Co 750", "NSE:Co SLOW"], alerts)
check("in-flight ambiguous filing not resubmitted when NSE lists it again", len(amb_calls) == 1, amb_calls)

# Quarter cutoff: results the PDF dates before SCORE_FROM_QUARTER are ignored
check("configured cutoff is Q2FY27", CONFIGURED_SCORE_FROM_QUARTER == "Q2FY27", CONFIGURED_SCORE_FROM_QUARTER)
check("quarter_index orders quarters",
      pt.quarter_index("Q4FY26") < pt.quarter_index("Q1FY27") < pt.quarter_index("Q2FY27") < pt.quarter_index("Q1FY28"))
try:
    pt.quarter_index("Q5FY27")
    check("bad quarter label rejected", False)
except ValueError:
    check("bad quarter label rejected", True)

pt.SCORE_FROM_QUARTER = "Q2FY27"
try:
    fresh_state_dir()
    old_q = pt.normalise_bse(bse_raw("q1", 801, "Result", "2026-09-26T10:00:00"))
    old_amb = pt.normalise_nse(nse_raw("q2", "OLDQ", "INE802A01010", "26-Sep-2026 10:05:00", desc="Outcome of Board Meeting"))
    new_q = pt.normalise_bse(bse_raw("q3", 803, "Result", "2026-10-20T10:00:00"))
    no_period = pt.normalise_bse(bse_raw("q4", 804, "Result", "2026-09-26T11:00:00"))
    extract, calls = counting_extract({
        old_q["attachment_url"]: mk(period_end="2026-06-30", **GOOD),        # Q1FY27
        old_amb["attachment_url"]: mk(period_end="2026-06-30", **GOOD),      # Q1FY27, via the checker
        new_q["attachment_url"]: mk(period_end="2026-09-30", **GOOD),        # Q2FY27
        no_period["attachment_url"]: mk(**GOOD),                             # no period_end
    })
    with LogCapture() as logs:
        alerts = run_main(1, bse=lambda k: [old_q, new_q, no_period], nse=lambda c: [old_amb], extract=extract)
    seen = set(json.load(open(pt.SEEN_FILE)))
    processed = set(json.load(open(pt.PROCESSED_SCRIPS_FILE)))
    csv_scrips = [r["scrip"] for r in csv.DictReader(open(pt.RESULTS_CSV, encoding="utf-8"))]
    check("old quarter logged as ignored", logs.has("Q1FY27: old quarter, ignored (scoring from Q2FY27)"), logs.messages)
    check("OLD line with the model's timing on it",
          logs.has_line("OLD", "Co 801", "Q1FY27: old quarter, ignored (scoring from Q2FY27)")
          and logs.has_line("OLD", "OLDQ", "old quarter, ignored") and not logs.has("not calling the model"), logs.terminal())
    retries = json.load(open(pt.RETRIES_FILE)) if os.path.exists(pt.RETRIES_FILE) else {}
    check("old-quarter filings marked seen, not scored, not processed, no retry",
          {old_q["id"], old_amb["id"]} <= seen and "801" not in csv_scrips and "OLDQ" not in csv_scrips
          and not any("801" in k or "INE802A01010" in k for k in processed)
          and old_q["id"] not in retries and old_amb["id"] not in retries, (seen, processed, csv_scrips, retries))
    check("only the Q2FY27 result is scored and alerted",
          alerts == ["BSE:Co 803"] and processed == {"BSE-803_Q2FY27"} and csv_scrips == ["803"], (alerts, processed, csv_scrips))
    check("no period_end: no alert, no row yet, RETRY 'quarter unknown'",
          logs.has_line("RETRY", "Co 804", "quarter unknown (no period_end) · retry 1/3")
          and retries.get(no_period["id"]) == 1 and no_period["id"] not in seen, logs.terminal())

    # Every attempt without a period_end: one UNKNOWN row after the last, never an alert
    fresh_state_dir()
    unknown = pt.normalise_bse(bse_raw("u1", 821, "Result", "2026-10-20T10:00:00"))
    extract, calls = counting_extract({unknown["attachment_url"]: mk(basis="standalone", **GOOD)})
    alerts = run_main(2, bse=lambda k: [unknown], extract=extract)
    rows_before = list(csv.DictReader(open(pt.RESULTS_CSV, encoding="utf-8")))
    with LogCapture() as logs:
        alerts += run_main(3, bse=lambda k: [unknown], extract=extract)          # restart: counts persist
    rows = list(csv.DictReader(open(pt.RESULTS_CSV, encoding="utf-8")))
    check("quarter unknown: no row while retries remain", rows_before == [], rows_before)
    check("quarter unknown: 4 attempts, then one UNKNOWN row with the figures",
          calls[unknown["attachment_url"]] == 4 and len(rows) == 1 and rows[0]["quarter"] == "UNKNOWN"
          and rows[0]["period_end"] == "" and rows[0]["basis"] == "standalone" and rows[0]["unit"] == "crores"
          and float(rows[0]["score"]) >= 35, (calls, rows))
    check("quarter unknown: never alerted, not processed, marked seen",
          alerts == [] and json.load(open(pt.PROCESSED_SCRIPS_FILE)) == []
          and unknown["id"] in set(json.load(open(pt.SEEN_FILE))), alerts)
    check("quarter unknown: give-up line says it was saved",
          logs.has_line("RETRY", "Co 821", "quarter unknown (no period_end) · gave up after 4 attempts · saved as quarter UNKNOWN (", "/50, no alert)"),
          logs.terminal())

    # A retry that gets the period_end right leaves only the real row
    fresh_state_dir()
    recovers = pt.normalise_bse(bse_raw("u2", 822, "Result", "2026-10-20T10:00:00"))
    replies = [mk(**GOOD), mk(period_end="2026-09-30", **GOOD)]
    def flaky_extract(pdf, ambiguous=False, timings=None, **kw):
        return replies.pop(0)
    alerts = run_main(2, bse=lambda k: [recovers], extract=flaky_extract)
    rows = list(csv.DictReader(open(pt.RESULTS_CSV, encoding="utf-8")))
    check("unknown on the first try, Q2FY27 on the retry: one Q2FY27 row, one alert",
          [(r["scrip"], r["quarter"], r["period_end"]) for r in rows] == [("822", "Q2FY27", "2026-09-30")]
          and alerts == ["BSE:Co 822"], (rows, alerts))

    # An implausibly old period_end whose column dates don't line up is a quarter unknown
    fresh_state_dir()
    stale = pt.normalise_bse(bse_raw("u3", 823, "Result", "2026-10-20T10:00:00"))
    extract, calls = counting_extract({stale["attachment_url"]: mk(period_end="2024-09-30", prev_period_end="2026-06-30",
                                                                   ly_period_end="2025-09-30", **GOOD)})
    with LogCapture() as logs:
        alerts = run_main(1, bse=lambda k: [stale], extract=extract)
    check("implausible period_end: RETRY 'quarter unknown', no alert",
          logs.has_line("RETRY", "Co 823", "quarter unknown (period_end 2024-09-30 implausible for a filing on 2026-10-20) · retry 1/3")
          and alerts == [], logs.terminal())

    # ...but with consistent column dates it's a very late filing of an old quarter: OLD, no retry
    fresh_state_dir()
    late = pt.normalise_bse(bse_raw("u4", 824, "Result", "2026-10-20T10:00:00"))
    extract, calls = counting_extract({late["attachment_url"]: mk(period_end="2025-03-31", **GOOD)})
    with LogCapture() as logs:
        alerts = run_main(2, bse=lambda k: [late], extract=extract)
    check("very late old result (columns line up): OLD once, not retried",
          logs.has_line("OLD", "Co 824", "Q4FY25: old quarter, ignored") and calls == {late["attachment_url"]: 1}
          and not logs.has_line("RETRY", "Co 824") and alerts == [] and "BSE:u4" in pt.load_seen(), logs.terminal())

    # Column dates that don't line up: retried, never scored, no row on giving up
    fresh_state_dir()
    shifted = pt.normalise_bse(bse_raw("c1", 831, "Result", "2026-10-20T10:00:00"))
    missing = pt.normalise_bse(bse_raw("c2", 832, "Result", "2026-10-20T10:00:00"))
    extract, calls = counting_extract({
        shifted["attachment_url"]: mk(period_end="2026-09-30", prev_period_end="2026-03-31", ly_period_end="2025-09-30", **GOOD),
        missing["attachment_url"]: mk(period_end="2026-09-30", prev_period_end="2026-06-30", ly_period_end=None, **GOOD),
    })
    with LogCapture() as logs:
        alerts = run_main(4, bse=lambda k: [shifted, missing], extract=extract)
    check("column mismatch: RETRY 'column dates don't line up'",
          logs.has_line("RETRY", "Co 831",
                        "column dates don't line up (previous column 2026-03-31 isn't 3 months before 2026-09-30) · retry 1/3"),
          logs.terminal())
    check("missing column date: RETRY 'column dates don't line up'",
          logs.has_line("RETRY", "Co 832", "column dates don't line up (last-year column date missing) · retry 1/3"))
    check("column mismatch: 4 attempts, given up, no row, no alert",
          calls[shifted["attachment_url"]] == 4 and logs.has_line("RETRY", "Co 831", "gave up after 4 attempts")
          and not logs.has_line("RETRY", "Co 831", "saved as quarter UNKNOWN")
          and list(csv.DictReader(open(pt.RESULTS_CSV, encoding="utf-8"))) == [] and alerts == [], calls)

    # The ambiguous-outcome checker path saves an UNKNOWN row the same way
    fresh_state_dir()
    vague = pt.normalise_nse(nse_raw("u9", "VAGUE", "INE829A01010", "20-Oct-2026 10:00:00", desc="Outcome of Board Meeting"))
    extract, calls = counting_extract({vague["attachment_url"]: mk(**GOOD)})
    alerts = run_main(4, nse=lambda c: [vague], extract=extract)
    rows = list(csv.DictReader(open(pt.RESULTS_CSV, encoding="utf-8")))
    check("checker path: quarter unknown retried, then one UNKNOWN row, no alert",
          calls[vague["attachment_url"]] == 4 and [(r["scrip"], r["quarter"]) for r in rows] == [("VAGUE", "UNKNOWN")]
          and alerts == [], (calls, rows))
finally:
    pt.SCORE_FROM_QUARTER = "Q1FY20"

# Timing line per processed filing
fresh_state_dir()
timed = pt.normalise_bse(bse_raw("t9", 909, "Result", "2026-09-24T10:00:00"))
extract, calls = counting_extract({timed["attachment_url"]: good_fin})
with LogCapture() as logs:
    run_main(1, bse=lambda k: [timed], extract=extract)
check("ALERT line carries score, quarter, basis and timing",
      logs.has_line("ALERT", "Co 909", "49.0/50  Q1FY27  unknown  Telegram sent  ⏱ download ", "exchange→alert"), logs.terminal())
check("exactly one terminal line per processed filing",
      sum("Co 909" in m for _, m in logs.terminal()) == 1, logs.terminal())
check("filing details go to DEBUG, not the terminal",
      logs.has_line("DEBUG", "BSE Co 909 (909): ISIN unknown, Result, exchange time 24 Sep 2026 10:00:00, headline from")
      and logs.has_line("DEBUG", "BSE Co 909: PDF")
      and not any("ISIN" in m or "headline from" in m or "MB" in m for _, m in logs.terminal()), logs.terminal())
check("POLL lines open and close the cycle",
      [st for st, _ in logs.terminal()][-3:] == ["POLL", "ALERT", "POLL"]
      and logs.has_line("POLL", "BSE: 1 new announcements (fetch OK, 1 page read) · NSE: 0 new announcements (fetch OK, 0 today)")
      and logs.has_line("POLL", "done · 1 result filing · 0 intimations skipped · 0 ambiguous queued · 0 not relevant · checker queue: 0 pending"),
      logs.terminal())
check("no 'Sleeping' lines", not logs.has("Sleeping"))

# Retry and error lines
fresh_state_dir()
flaky = pt.normalise_bse(bse_raw("r1", 911, "Result", "2026-09-24T10:00:00"))
boom_f = pt.normalise_bse(bse_raw("r2", 912, "Result", "2026-09-24T10:00:00"))
extract, calls = counting_extract({flaky["attachment_url"]: None, boom_f["attachment_url"]: RuntimeError("kaboom")})
with LogCapture() as logs:
    run_main(4, bse=lambda k: [flaky, boom_f], extract=extract)
check("RETRY line: reason and retry count",
      logs.has_line("RETRY", "Co 911", "could not extract financials · retry 1/3"), logs.terminal())
check("RETRY line when giving up", logs.has_line("RETRY", "Co 911", "gave up after 4 attempts"))
check("POLL summary counts the retries", logs.has_line("POLL", "done · 2 result filings", "2 of them retries"),
      [m for st, _, m in logs.records if st == "POLL"])

check("crash: one ERROR line at ERROR level, traceback only in DEBUG",
      any(st == "ERROR" and lvl == logging.ERROR and "Co 912" in m and "unexpected error: RuntimeError: kaboom · retry 1/3" in m
          for st, lvl, m in logs.records)
      and not any("Traceback" in m for _, m in logs.terminal()), logs.terminal())

# The model answered without revenue or PAT: one MISS line, never retried (9 Oct 2026: 52 of 59 calls were such retries)
fresh_state_dir()
no_rev = pt.normalise_bse(bse_raw("m1", 921, "Result", "2026-10-09T16:00:00"))
all_null = pt.normalise_bse(bse_raw("m2", 922, "Result", "2026-10-09T16:01:00"))
def answered_nothing(pdf, ambiguous=False, timings=None, raw_out=None, **kw):
    name = pdf.decode()[len("%PDF-"):]
    calls[name] = calls.get(name, 0) + 1
    if name == no_rev["attachment_url"]:
        return mk(period_end="2026-09-30", pat=[1, 1, 1], total_income=[9, 4, 1])
    if raw_out is not None:
        raw_out["data"] = {"basis": None, "unit": None, "revenue_from_operations": [None, None, None]}
    return None
calls = {}
with LogCapture() as logs:
    run_main(4, bse=lambda k: [no_rev, all_null], extract=answered_nothing)
check("no revenue: MISS, read once, not retried",
      logs.has_line("MISS", "Co 921", "no current-quarter revenue from operations in the table · not retried")
      and calls[no_rev["attachment_url"]] == 1 and "BSE:m1" in pt.load_seen(), (calls, logs.terminal()))
check("model answered with nothing usable: MISS, not retried",
      logs.has_line("MISS", "Co 922", "the model found no figures it could use") and calls[all_null["attachment_url"]] == 1
      and pt.load_retries() == {}, (calls, logs.terminal()))

# The same PDF filed again (BSE "Board Meeting" + "Result", or on NSE too) is read once
fresh_state_dir()
copy_a = pt.normalise_bse(bse_raw("d1", 931, "Result", "2026-10-09T16:35:00", sub="Financial Results", attach="same.pdf"))
copy_b = pt.normalise_bse(bse_raw("d2", 931, "Result", "2026-10-09T16:41:00", sub="Unaudited Financial Results", attach="same.pdf"))
calls = {}
with LogCapture() as logs:
    run_main(1, bse=lambda k: [copy_a, copy_b], extract=answered_nothing)
check("an identical PDF is not read again",
      sum(calls.values()) == 1 and logs.has_line("SKIP", "Co 931", "same PDF as the BSE 09 Oct 16:35 filing (MISS) · not read again"),
      (calls, logs.terminal()))
# A "Result" filing without a table names its headline
fresh_state_dir()
no_tab = pt.normalise_bse(bse_raw("n1", 913, "Result", "2026-09-26T17:32:26",
                                  sub="Results- Financial Results For 31St March, 2026 (31-03-2026)"))
extract, calls = counting_extract({no_tab["attachment_url"]: pt.SkipFiling("no results table found", status="NONE")})
with LogCapture() as logs:
    run_main(1, bse=lambda k: [no_tab], extract=extract)
check("NONE line for a Result filing shows its headline",
      logs.has_line("NONE", "Co 913", "no results table found · headline: 'Results- Financial Results For 31St March, 2026 (31-03-2026)'"),
      logs.terminal())

# --verbose shows DEBUG in the terminal
fresh_state_dir()
try:
    run_main(1, argv=["--verbose"])
    check("--verbose lowers the terminal to DEBUG", pt.console_handler.level == logging.DEBUG)
finally:
    pt.console_handler.setLevel(logging.INFO)
check("terminal shows INFO by default", pt.console_handler.level == logging.INFO)
check("libraries stay quiet (root logger at WARNING)",
      logging.getLogger("pdfminer").getEffectiveLevel() >= logging.WARNING
      and logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING)

# Poll logs distinguish "0 new" from a failed fetch
fresh_state_dir()
with LogCapture() as logs:
    run_main(1)
check("quiet poll logged as fetch OK", logs.has("BSE: 0 new announcements (fetch OK, 1 page read)")
      and logs.has("NSE: 0 new announcements (fetch OK, 0 today)"), logs.messages)
def bse_down(known):
    raise pt.requests.ConnectionError("connection reset")
with LogCapture() as logs:
    run_main(1, bse=bse_down)
check("failed BSE fetch logged as FAILED, not 0", logs.has("BSE: fetch FAILED (connection reset)")
      and not logs.has("BSE: 0 new"), logs.messages)
with LogCapture() as logs:
    run_main(2, nse=lambda c: [vague_nse], extract=lambda pdf, ambiguous=False, timings=None, **kw: None)
check("NSE counts only new filings on later polls",
      logs.has("NSE: 1 new announcements (fetch OK, 1 today)") and logs.has("NSE: 0 new announcements (fetch OK, 1 today)"))

# Startup reports processed-key migration
fresh_state_dir()
pt.save_processed_scrips({"531694_Q4FY26", "532386_Q1FY27", "BSE-509084_Q4FY26", "INE0DRI01029_Q1FY27"})
with LogCapture() as logs:
    run_main(1, master={"bse": {"531694": "INE111A01011", "532386": "INE222A01011"}, "nse": {}})
check("migration logged", logs.has("Migrated 2 processed keys to ISIN format"), logs.messages)
check("keys still without ISIN listed", logs.has("Processed keys still without ISIN: BSE-509084_Q4FY26"))

# Pending retries come back even when the exchange stops returning the filing
fresh_state_dir()
once = pt.normalise_bse(bse_raw("p1", 555, "Result", "2026-09-24T10:00:00"))
extract, calls = counting_extract({once["attachment_url"]: None})
fed = {"n": 0}
def feed_once(known):
    fed["n"] += 1
    return [once] if fed["n"] == 1 else []
run_main(5, bse=feed_once, extract=extract)
check("pending retry re-queued without re-fetch", calls.get(once["attachment_url"]) == 4, calls)

# Blocked exchange doesn't stop the other one
fresh_state_dir()
def blocked(known):
    raise pt.ExchangeBlocked("HTTP 403 on announcements page 1")
nse_ok = pt.normalise_nse(nse_raw("s9", "XYZ", "INE999A01019", "24-Sep-2026 11:00:00"))
extract, calls = counting_extract({nse_ok["attachment_url"]: good_fin})
warned = []
with mock.patch.object(pt, "warn_blocked", lambda ex, detail, **kw: warned.append(ex)):
    alerts = run_main(2, bse=blocked, nse=lambda c: [nse_ok], extract=extract)
check("blocked BSE warned each poll, NSE still processed", warned == ["BSE", "BSE"] and alerts == ["NSE:Co XYZ"], (warned, alerts))

# Startup migrates legacy processed keys
fresh_state_dir()
pt.save_processed_scrips({"531694_Q4FY26", "532386_Q1FY27"})
run_main(1, master={"bse": {"531694": "INE111A01011"}, "nse": {}})
check("startup migrates processed keys",
      set(json.load(open(pt.PROCESSED_SCRIPS_FILE))) == {"INE111A01011_Q4FY26", "BSE-532386_Q1FY27"})

# NSE filings teach the scrip master
fresh_state_dir()
extract, calls = counting_extract({nse_ok["attachment_url"]: good_fin})
run_main(1, nse=lambda c: [nse_ok], extract=extract)
check("NSE ISIN learned into master", json.load(open(pt.SCRIP_MASTER_FILE))["nse"].get("XYZ") == "INE999A01019")

# Checker queue size logged once per poll
fresh_state_dir()
with LogCapture() as logs:
    run_main(3)
check("checker queue logged once per poll, in the POLL summary",
      sum(st == "POLL" and m.endswith("checker queue: 0 pending") for st, _, m in logs.records) == 3,
      [m for m in logs.messages if "checker queue" in m])

started, release = threading.Event(), threading.Event()
def blocked_extract(pdf, ambiguous=False, timings=None, **kw):
    started.set()
    release.wait(10)
    return None
def count_then_release(state, poll):
    started.wait(10)
    release.set()
    state.checker.todo.join()
    pt.drain_checks(state)
fresh_state_dir()
with LogCapture() as logs:
    run_main(1, nse=lambda c: [pt.normalise_nse(REAL_NSE_OTHER_OUTCOME)], extract=blocked_extract, on_wait=count_then_release)
check("checker queue counts an in-flight ambiguous check", logs.has("checker queue: 1 pending"),
      [m for m in logs.messages if "checker queue" in m])

# ─────────────────────────────────────────────────────────────
section("file log")

log_dir = tempfile.mkdtemp(prefix="pead_log_")
log_path = os.path.join(log_dir, "pead_tool.log")
handler = pt.setup_file_logging(log_path)
try:
    pt.log.info("→ Skip NSE board meeting (intimation): Mahindra & Mahindra — ⏱ ₹ test")
    worker = threading.Thread(target=lambda: pt.log.info("   Ambiguous outcome → results table found"), name="checker")
    worker.start()
    worker.join()
    handler.flush()
finally:
    logging.getLogger().removeHandler(handler)
    handler.close()
lines = open(log_path, encoding="utf-8").read().splitlines()
check("file log gets console-format lines in UTF-8",
      len(lines) == 2 and lines[0][8:] == "  INFO   → Skip NSE board meeting (intimation): Mahindra & Mahindra — ⏱ ₹ test", lines)
check("file log keeps the [checker] tag", lines[1][8:] == "  INFO   [checker]    Ambiguous outcome → results table found", lines)

handler = pt.setup_file_logging(log_path)
try:
    pt.report("SCORE", nse_filing, "10.0/50  Q2FY27  consolidated")
    pt.log.debug("debug detail")
    worker = threading.Thread(target=lambda: pt.report("NONE", nse_filing, "no results table found"), name="checker")
    worker.start()
    worker.join()
    handler.flush()
finally:
    logging.getLogger().removeHandler(handler)
    handler.close()
lines = open(log_path, encoding="utf-8").read().splitlines()[2:]
check("filing line: time, status, exchange, padded company, text",
      re.fullmatch(r"\d{2}:\d{2}:\d{2}  SCORE  NSE  Mahindra & Mahindra {11}  10\.0/50  Q2FY27  consolidated", lines[0]) is not None, lines)
check("file log has DEBUG lines too", re.fullmatch(r"\d{2}:\d{2}:\d{2}  DEBUG  debug detail", lines[1]) is not None, lines)
check("checker-thread filing lines use their status, no [checker] tag",
      lines[2][8:] == f"  NONE   NSE  {pt.short_company('Mahindra & Mahindra Limited')}  no results table found", lines)
handler = pt.setup_file_logging(log_path)
check("file handler takes DEBUG", handler.level == logging.DEBUG)
logging.getLogger().removeHandler(handler)
handler.close()
check("file log rotates at 5 MB with 3 backups",
      handler.maxBytes == 5 * 1024 * 1024 and handler.backupCount == 3 and handler.encoding == "utf-8")
check("default log path is pead_tool.log next to the script",
      pt.LOG_FILE == os.path.join(os.path.dirname(os.path.abspath(pt.__file__)), "pead_tool.log"))
handler = pt.setup_file_logging(log_path)
try:
    pt.log.info("second run")
    handler.flush()
finally:
    logging.getLogger().removeHandler(handler)
    handler.close()
check("file log appends across restarts", open(log_path, encoding="utf-8").read().count("\n") == 6)

# ─────────────────────────────────────────────────────────────
section("dashboard /filing")

LIVE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
HIS = "https://www.bseindia.com/xml-data/corpfiling/AttachHis/"
old_name = "64c0e26e-8713-48ab-98a1-847f560b8ada.pdf"     # a May 2026 filing: gone from AttachLive
new_name = "49f77c3e-fb28-4a64-a1f6-084126c99675.pdf"     # a Sept 2026 filing: still in AttachLive

def fake_is_pdf(available):
    asked = []
    def is_pdf(url):
        asked.append(url)
        return url in available
    return is_pdf, asked

dashboard._filing_cache.clear()
is_pdf, asked = fake_is_pdf({HIS + old_name, LIVE + new_name})
with mock.patch.object(dashboard, "is_pdf", is_pdf):
    check("old BSE filing: AttachLive missing → AttachHis",
          dashboard.resolve_filing(LIVE + old_name, "BSE", "543273") == HIS + old_name and asked == [LIVE + old_name, HIS + old_name], asked)
    asked.clear()
    check("resolved link is cached", dashboard.resolve_filing(LIVE + old_name, "BSE", "543273") == HIS + old_name and asked == [])
    check("recent BSE filing stays on AttachLive", dashboard.resolve_filing(LIVE + new_name, "BSE", "544901") == LIVE + new_name)
    check("missing from both → company page",
          dashboard.resolve_filing(LIVE + "0000.pdf", "BSE", "544901") == "https://www.bseindia.com/stock-share-price/x/x/544901/")
    nse_url = "https://nsearchives.nseindia.com/corporate/ESDS_24092026202831_ESDS_BM_Outcome_BSE_NSE_Intimation.pdf"
    asked.clear()
    check("NSE archive link passed through unchecked", dashboard.resolve_filing(nse_url, "NSE", "ESDS") == nse_url and asked == [])
    check("NSE fallback is the NSE quote page, & escaped",
          dashboard.resolve_filing("", "NSE", "M&M") == "https://www.nseindia.com/get-quotes/equity?symbol=M%26M")
    asked.clear()
    check("non-exchange URL never fetched → company page",
          dashboard.resolve_filing("https://example.com/x.pdf", "BSE", "532386") == "https://www.bseindia.com/stock-share-price/x/x/532386/" and asked == [])
    check("BSE URL with a path trick rejected",
          dashboard.resolve_filing(LIVE + "../../etc/x.pdf", "BSE", "532386").startswith("https://www.bseindia.com/stock-share-price/"))
    check("bad scrip → dashboard home", dashboard.resolve_filing("", "BSE", "<script>") == "/")

import http.client
server = dashboard.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    dashboard._filing_cache.clear()
    is_pdf, asked = fake_is_pdf({HIS + old_name})
    with mock.patch.object(dashboard, "is_pdf", is_pdf):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        query = "url=" + dashboard.quote(LIVE + old_name, safe="") + "&exchange=BSE&scrip=543273"
        conn.request("GET", "/filing?" + query)
        resp = conn.getresponse()
        check("/filing answers 302 to where the PDF is now",
              resp.status == 302 and resp.getheader("Location") == HIS + old_name, (resp.status, resp.getheader("Location")))
        conn.close()
finally:
    server.shutdown()
    server.server_close()

# ─────────────────────────────────────────────────────────────
section("dashboard current quarter")

check("dashboard reads SCORE_FROM_QUARTER from pead_tool.py",
      dashboard.read_current_quarter() == CONFIGURED_SCORE_FROM_QUARTER, dashboard.read_current_quarter())
folder = tempfile.mkdtemp(prefix="pead_dash_")
with open(os.path.join(folder, dashboard.TOOL_FILE), "w", encoding="utf-8") as fh:
    fh.write('PEAD_THRESHOLD     = 35\nSCORE_FROM_QUARTER = "Q3FY27"\n')
with mock.patch.object(dashboard, "BASE_DIR", folder):
    check("…and sends it as current_quarter", dashboard.build_payload()["current_quarter"] == "Q3FY27")
with mock.patch.object(dashboard, "BASE_DIR", tempfile.mkdtemp(prefix="pead_dash_")):
    check("no pead_tool.py → empty current quarter", dashboard.read_current_quarter() == "")

# ─────────────────────────────────────────────────────────────
section("dashboard quarters")

months = [date(y, m, d) for y in (2025, 2026, 2027) for m in range(1, 13) for d in (1, 28)]
check("dashboard quarter maths matches pead_tool for every month 2025–2027",
      all(dashboard.quarter_label(x) == pt.quarter_label(x) and dashboard.reporting_quarter(x) == pt.reporting_quarter(x)
          for x in months))
for q, want in [("Q4FY26", "Apr–Jun 2026"), ("Q1FY27", "Jul–Sep 2026"), ("Q2FY27", "Oct–Dec 2026"),
                ("Q3FY27", "Jan–Mar 2027"), ("Q4FY27", "Apr–Jun 2027"), ("UNKNOWN", "")]:
    check(f"reported_window {q}", dashboard.reported_window(q) == want, dashboard.reported_window(q))
for q, want in [("Q1FY27", "Q1 FY27 · Apr–Jun 2026"), ("Q2FY27", "Q2 FY27 · Jul–Sep 2026"),
                ("Q3FY27", "Q3 FY27 · Oct–Dec 2026"), ("Q4FY26", "Q4 FY26 · Jan–Mar 2026"), ("UNKNOWN", "Quarter unknown")]:
    check(f"quarter_name {q}", dashboard.quarter_name(q) == want, dashboard.quarter_name(q))

# Old rows: the quarter column first, then period_end, then the filing-date mapping
for row, want in [
    ({"quarter": "Q2FY27", "period_end": "2026-06-30", "timestamp": "2026-05-29 12:00:00"}, ("Q2FY27", "csv")),
    ({"quarter": "q2fy27"}, ("Q2FY27", "csv")),
    ({"quarter": "UNKNOWN", "timestamp": "2026-10-20 10:00:00"}, ("UNKNOWN", "csv")),
    ({"quarter": "", "period_end": "2026-06-30", "timestamp": "2026-09-24 21:31:06"}, ("Q1FY27", "period_end")),
    ({"timestamp": "2026-05-29 12:38:10"}, ("Q4FY26", "filed")),     # the Q1 archive's Apr–Jun filings
    ({"timestamp": "2026-06-30 23:59:59"}, ("Q4FY26", "filed")),
    ({"timestamp": "2026-07-20 23:24:50"}, ("Q1FY27", "filed")),
    ({"timestamp": "2026-09-24 21:31:06"}, ("Q1FY27", "filed")),
    ({"timestamp": "2026-10-20 11:00:00"}, ("Q2FY27", "filed")),
    ({"timestamp": "2027-01-15 09:00:00"}, ("Q3FY27", "filed")),
    ({"timestamp": ""}, ("UNKNOWN", "unknown")),
]:
    check(f"row_quarter {row}", dashboard.row_quarter(row) == want, dashboard.row_quarter(row))

for score, want in [("90.0", True), ("71.7", True), ("50.1", True), ("50.0", False), ("35", False), ("", False), ("x", False)]:
    check(f"old 100-point scale: score {score!r} → {want}", dashboard.is_old_scale({"score": score}) == want)

folder = tempfile.mkdtemp(prefix="pead_dash_")
NEW_HEADER = pt.CSV_HEADER
OLD_HEADER = pt.CSV_HEADER[:17]                       # the archive's layout: no quarter columns
def csv_row(header, **kw):
    base = {"timestamp": "", "company": "Co", "scrip": "1", "score": "", "revenue_cq": "", "pat_cq": "", "eps_cq": ""}
    base.update(kw)
    return [base.get(col, "") for col in header]
with open(os.path.join(folder, dashboard.TOOL_FILE), "w", encoding="utf-8") as fh:
    fh.write('SCORE_FROM_QUARTER = "Q2FY27"\n')
with open(os.path.join(folder, dashboard.RESULTS_CSV), "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(NEW_HEADER)
    w.writerow(csv_row(NEW_HEADER, timestamp="2026-10-20 11:02:13", company="Live Q2", score="41.0", revenue_cq="182.4", quarter="Q2FY27"))
    w.writerow(csv_row(NEW_HEADER, timestamp="2026-10-20 12:00:00", company="Live empty", score="0.0", quarter="Q2FY27"))
    w.writerow(csv_row(NEW_HEADER, timestamp="2026-10-20 15:20:31", company="Live unknown", score="40.0", pat_cq="9", quarter="UNKNOWN"))
os.makedirs(os.path.join(folder, "archive", "pre-q2fy27"))
with open(os.path.join(folder, "archive", "pre-q2fy27", dashboard.RESULTS_CSV), "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(OLD_HEADER)
    w.writerow(csv_row(OLD_HEADER, timestamp="2026-05-27 23:50:45", company="GMR", score="90.0", revenue_cq="1580"))
    w.writerow(csv_row(OLD_HEADER, timestamp="2026-05-28 18:28:07", company="Superior", score="85.0", revenue_cq="422"))
    w.writerow(csv_row(OLD_HEADER, timestamp="2026-05-29 13:28:20", company="Sharda", score="45.0", revenue_cq="67.2"))
    w.writerow(csv_row(OLD_HEADER, timestamp="2026-06-01 11:48:36", company="Photon", score="50.0", revenue_cq="143"))
    w.writerow(csv_row(OLD_HEADER, timestamp="2026-07-20 23:24:50", company="California", score="40.0", revenue_cq="662"))
    w.writerow(csv_row(OLD_HEADER, timestamp="2026-09-24 21:31:06", company="Purple", score="0.0", revenue_cq="119"))
os.makedirs(os.path.join(folder, "archive", "extra"))
with open(os.path.join(folder, "archive", "extra", dashboard.RESULTS_CSV), "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(NEW_HEADER)
    w.writerow(csv_row(NEW_HEADER, timestamp="2026-09-30 10:00:00", company="Late filer", score="22", revenue_cq="80", period_end="2026-06-30"))
with open(os.path.join(folder, "archive", "notes.txt"), "w") as fh:
    fh.write("not a run folder")
with mock.patch.object(dashboard, "BASE_DIR", folder):
    payload = dashboard.build_payload()
rows = payload["results"]
by_company = {r["company"]: r for r in rows}
check("archive rows merged with the live file, archives first",
      [r["_source"] for r in rows] == ["archive/extra"] + ["archive/pre-q2fy27"] * 4 + ["pead_results.csv"] * 3,
      [r["_source"] for r in rows])
check(">50 rows dropped from the results", "GMR" not in by_company and "Superior" not in by_company
      and "Photon" in by_company, sorted(by_company))
check("old-row quarters: filing-date mapping and period_end",
      by_company["Sharda"]["_quarter"] == "Q4FY26" and by_company["Sharda"]["_quarter_from"] == "filed"
      and by_company["California"]["_quarter"] == "Q1FY27" and by_company["Purple"]["_quarter"] == "Q1FY27"
      and by_company["Late filer"]["_quarter"] == "Q1FY27" and by_company["Late filer"]["_quarter_from"] == "period_end")
options = payload["quarters"]
check("quarters newest first, the unknown group last",
      [o["value"] for o in options] == ["Q2FY27", "Q1FY27", "Q4FY26", "UNKNOWN"], [o["value"] for o in options])
check("labels: period covered, when reported, scored results (empty and >50 rows excluded)",
      [o["label"] for o in options] == ["Q2 FY27 · Jul–Sep 2026 quarter · reported Oct–Dec 2026 (1)",
                                        "Q1 FY27 · Apr–Jun 2026 quarter · reported Jul–Sep 2026 (3)",
                                        "Q4 FY26 · Jan–Mar 2026 quarter · reported Apr–Jun 2026 (2)",
                                        "Quarter unknown (1)"], [o["label"] for o in options])
check("old-scale rows counted per quarter",
      {o["value"]: o["hidden_old_scale"] for o in options} == {"Q2FY27": 0, "Q1FY27": 0, "Q4FY26": 2, "UNKNOWN": 0})
check("current quarter flagged", [o["value"] for o in options if o["current"]] == ["Q2FY27"])
check("the current quarter is offered even with no rows",
      [o["value"] for o in dashboard.group_quarters([], {}, "Q2FY27")] == ["Q2FY27"]
      and dashboard.group_quarters([], {}, "Q2FY27")[0]["label"] == "Q2 FY27 · Jul–Sep 2026 quarter · reported Oct–Dec 2026 (0)")

# ─────────────────────────────────────────────────────────────
section("dashboard log parser")

def parse_log(text):
    folder = tempfile.mkdtemp(prefix="pead_dash_")
    with open(os.path.join(folder, dashboard.LOG_FILE), "w", encoding="utf-8") as fh:
        fh.write(text)
    with mock.patch.object(dashboard, "BASE_DIR", folder):
        return dashboard.read_log()

NEW_LOG = """21:30:53  INFO   PEAD scanner · BSE + NSE · model anthropic/claude-haiku-4-5 · alert at score ≥ 35 · scoring from Q2FY27
21:31:01  POLL   BSE: 3 new announcements (fetch OK, 2 pages read) · NSE: 5 new announcements (fetch OK, 931 today)
21:31:01  DEBUG  BSE ESDS Software Solution Ltd (544898): ISIN INE0DRI01029, Board Meeting, headline from NEWSSUB+HEADLINE+SUBCATNAME: 'x'
21:31:05  ALERT  BSE  ESDS Software Solution          41.0/50  Q2FY27  consolidated  Telegram sent  ⏱ download 0.5s · page scan 0.3s · model 3.4s · exchange→alert 2m 13s
21:31:09  SCORE  NSE  Purple Style Labs               10.0/50  Q2FY27  consolidated  ⏱ download 1.2s · page scan 0.4s · OCR 6.1s · model 2.9s · exchange→scored 1h 2m 3s
21:31:10  CHECK  NSE  Global Education                ambiguous outcome → queued for a results-table check
21:31:12  NONE   NSE  Global Education                ambiguous outcome → no results table found  ⏱ download 0.4s · page scan 0.2s
21:31:12  DEBUG  traceback for BSE Co
Traceback (most recent call last):
  File "x.py", line 1, in <module>
21:31:13  POLL   done · 1 result filing · 3 intimations skipped · 1 ambiguous queued · 40 not relevant · checker queue: 1 pending
21:31:43  WARN   NSE: fetch FAILED (Read timed out) — announcements not read this poll
21:31:43  ERROR  🚫 NSE BLOCKED (HTTP 403 on announcements) — its announcements were NOT read this poll; this is not 'zero announcements'
21:31:43  POLL   BSE: 0 new announcements (fetch OK, 1 page read) · NSE: fetch FAILED (blocked: HTTP 403 on announcements)
21:31:44  POLL   done · 0 result filings · 0 intimations skipped · 0 ambiguous queued · 0 not relevant · checker queue: 0 pending
"""
parsed = parse_log(NEW_LOG)
check("both exchanges read from one POLL line",
      parsed["exchanges"]["BSE"]["ok"] and parsed["exchanges"]["BSE"]["message"] == "0 new announcements (fetch OK, 1 page read)", parsed["exchanges"])
check("blocked NSE shows as not OK", parsed["exchanges"]["NSE"]["ok"] is False, parsed["exchanges"]["NSE"])
check("an earlier POLL line parses per exchange",
      [f.group(1) + ": " + f.group(2).strip() for f in dashboard.FETCH_RE.finditer(NEW_LOG.splitlines()[1])]
      == ["BSE: 3 new announcements (fetch OK, 2 pages read)", "NSE: 5 new announcements (fetch OK, 931 today)"])
check("checker queue from the POLL summary", parsed["checker_queue"] == 0)
t = parsed["timings"]
check("three timing entries from filing lines", len(t) == 3, t)
check("ALERT timing: stages, kind, delay, context",
      t[0] == {"time": "21:31:05", "context": "ALERT BSE ESDS Software Solution", "status": "ALERT",
               "download": 0.5, "page_scan": 0.3, "model": 3.4, "kind": "alert", "delay": 133, "catchup": False}, t[0])
check("SCORE timing with OCR and an hour-long delay",
      t[1]["ocr"] == 6.1 and t[1]["kind"] == "scored" and t[1]["delay"] == 3723 and t[1]["context"] == "SCORE NSE Purple Style Labs", t[1])
check("NONE timing has stages but no delay", t[2]["download"] == 0.4 and "delay" not in t[2], t[2])
check("log panel hides DEBUG lines and their tracebacks",
      not any("DEBUG" in l or "Traceback" in l or "x.py" in l for l in parsed["lines"]) and len(parsed["lines"]) == 11, parsed["lines"])

check("without pead_terminal.log the panel shows the log file's lines", parsed["lines_source"] == "log")

def parse_with_terminal(log_text, terminal_text):
    folder = tempfile.mkdtemp(prefix="pead_dash_")
    with open(os.path.join(folder, dashboard.LOG_FILE), "w", encoding="utf-8") as fh:
        fh.write(log_text)
    with open(os.path.join(folder, dashboard.TERMINAL_LOG_FILE), "w", encoding="utf-8") as fh:
        fh.write(terminal_text)
    with mock.patch.object(dashboard, "BASE_DIR", folder):
        return dashboard.read_log()

TERMINAL = "".join(f"21:{m:02d}:00  POLL   BSE 0 new  ·  NSE {m} new  ·  nothing relevant\n" for m in range(0, 59)) + \
           "".join(f"22:{m:02d}:00  POLL   BSE 0 new  ·  NSE 0 new  ·  nothing relevant\n" for m in range(0, 59)) + \
           "22:59:30  ALERT  BSE  ESDS Software Solution        41.0/50  Q2FY27  consolidated  Telegram sent  ·  58s after filing\n"
parsed = parse_with_terminal(NEW_LOG, TERMINAL)
check("with pead_terminal.log the panel shows the terminal's last lines",
      parsed["lines_source"] == "terminal" and len(parsed["lines"]) == dashboard.LOG_LINES_SHOWN
      and parsed["lines"][-1].startswith("22:59:30  ALERT  BSE  ESDS") and parsed["lines"][0].startswith("21:19:00  POLL"), parsed["lines"][:2])
check("status, timings and queue still come from pead_tool.log",
      parsed["exchanges"]["NSE"]["ok"] is False and len(parsed["timings"]) == 3 and parsed["checker_queue"] == 0)

OLD_LOG = """21:30:55  ERROR  🚫 BSE BLOCKED (HTTP 403 on announcements page 1) — its announcements were NOT read this poll
21:31:01  INFO  NSE: 919 announcements fetched
21:31:05  INFO  → New NSE: ESDS Software Solution Limited (ESDS, ISIN INE0DRI01029, Board Meeting; headline from desc+attchmntText)
21:31:32  INFO     ⏱ download 1.1s · page scan 0.2s · model 2.0s · exchange→scored 1m 30s
21:31:40  INFO  checker queue: 2 pending
"""
parsed = parse_log(OLD_LOG)
check("older log layout still parses",
      parsed["exchanges"]["BSE"]["ok"] is False and parsed["exchanges"]["NSE"]["ok"]
      and parsed["checker_queue"] == 2 and parsed["timings"][0]["delay"] == 90
      and parsed["timings"][0]["context"].startswith("→ New NSE: ESDS"), parsed)

# ─────────────────────────────────────────────────────────────
section("--dump")

d = fresh_state_dir()
cwd = os.getcwd()
os.chdir(d)
bse_ann = {"Table": [bse_row], "Table1": [{"ROWCNT": 1}]}
def dump_get(url, headers=None, timeout=None):
    return FakeResponse(200, bse_ann if "AnnSubCategory" in url else bse_master_json, url=url)
def dump_nse_get(self, url, timeout=15):
    if "corporate-announcements" in url:
        return FakeResponse(200, [nse_row], url=url)
    return FakeResponse(200, text=equity_csv, url=url)
try:
    with mock.patch.object(pt.requests, "get", dump_get), mock.patch.object(pt.NseClient, "get", dump_nse_get):
        pt.main(["--dump"])
    check("raw_bse_sample.json saved", json.load(open("raw_bse_sample.json")) == bse_ann)
    check("raw_nse_sample.json saved", json.load(open("raw_nse_sample.json")) == [nse_row])
    with mock.patch.object(pt.requests, "get", lambda *a, **k: ACCESS_DENIED), \
         mock.patch.object(pt.NseClient, "get", lambda self, url, timeout=15: ACCESS_DENIED):
        pt.main(["--dump"])
    check("blocked dump saves the raw page", "Access Denied" in open("raw_bse_sample.json").read())
finally:
    os.chdir(cwd)

# ─────────────────────────────────────────────────────────────
section("PDF extraction (real pdfplumber / Poppler / Tesseract, mocked model)")

from PIL import Image, ImageDraw, ImageFont
import pypdfium2 as pdfium

COVER = ["ABC LIMITED", "To, BSE Limited", "Sub: Outcome of Board Meeting",
         "The Board approved the Unaudited Standalone and Consolidated Financial",
         "Results for the quarter ended June 30, 2026. Kindly take the same on record."] * 3
def table_lines(kind):
    return ["ABC LIMITED", "CIN: L12345MH1990PLC000000",
            f"Statement of Unaudited {kind} Financial Results for the quarter ended 30 June 2026",
            "(Rs. in Lakhs)", "Particulars  30.06.2026  31.03.2026  30.06.2025",
            "Revenue from operations  66258  60000  50000", "Total income  67000  61000  51000",
            "Profit before tax  9000  8000  6000", "Profit after tax  7000  6000  4000",
            "Earnings per share (Basic)  7.0  6.0  4.0"]
NOTES = ["Notes:", "1. The above results were reviewed by the Audit Committee and approved by the Board."]
PAGES = [COVER, table_lines("Standalone"), NOTES, table_lines("Consolidated"), NOTES]

def text_pdf(pages):
    """Minimal hand-built PDF with a real text layer."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for lines in pages:
        body = "BT /F1 10 Tf 14 TL 40 800 Td " + " ".join(
            "(" + l.replace("(", "\\(").replace(")", "\\)") + ") Tj T*" for l in lines) + " ET"
        objs.append(f"<< /Length {len(body)} >>\nstream\n{body}\nendstream")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                    f"/Resources << /Font << /F1 3 0 R >> >> /Contents {len(objs)} 0 R >>")
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out

def scanned_pdf(pages):
    for name in ("arial.ttf", "DejaVuSans.ttf"):      # DejaVu on Linux test machines
        try:
            font = ImageFont.truetype(name, 28)
            break
        except OSError:
            font = ImageFont.load_default()
    imgs = []
    for lines in pages:
        im = Image.new("RGB", (1654, 2339), "white")
        draw = ImageDraw.Draw(im)
        for i, l in enumerate(lines):
            draw.text((100, 120 + i * 48), l, fill="black", font=font)
        imgs.append(im)
    buf = io.BytesIO()
    imgs[0].save(buf, "PDF", save_all=True, append_images=imgs[1:], resolution=200)
    return buf.getvalue()

def hybrid_pdf():
    """Text-layer cover letter + scanned result pages."""
    doc = pdfium.PdfDocument.new()
    doc.import_pages(pdfium.PdfDocument(text_pdf([COVER])))
    doc.import_pages(pdfium.PdfDocument(scanned_pdf(PAGES[1:])))
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()

MODEL_JSON = ('Here you go:\n```json\n{"basis":"consolidated","unit":"lakhs","period_end":"2026-06-30",'
              '"prev_period_end":"2026-03-31","ly_period_end":"2025-06-30",'
              '"revenue_from_operations":[66258,60000,50000],"pat":[7000,6000,4000],"basic_eps":[7.0,6.0,4.0]}\n```')

class Msg:
    def __init__(self, content):
        self.content = content

class Resp:
    def __init__(self, content):
        self.choices = [type("Choice", (), {"message": Msg(content)})()]

def IMG(n):
    return f"--- PAGE {n} (scanned page image) ---"

def sent_page_labels(content):
    """Page labels in what was sent: text prompt lines, or text parts of an image message."""
    if isinstance(content, str):
        return [l for l in content.split("PDF TEXT:")[-1].splitlines() if l.startswith("--- PAGE")]
    return [p["text"].splitlines()[0] for p in content if p["type"] == "text" and p["text"].startswith("--- PAGE")]

sent_requests = []
def fake_create(**kw):
    sent_requests.append(kw)
    return Resp(MODEL_JSON)

with mock.patch.object(pt.client.chat.completions, "create", fake_create):
    for label, pdf, expect_pages in [
        ("text PDF", text_pdf(PAGES), ["--- PAGE 4 ---", "--- PAGE 5 ---"]),
        ("scanned PDF → page images", scanned_pdf(PAGES), [IMG(4), IMG(5)]),
        ("single-page scanned → page image", scanned_pdf([table_lines("Standalone")]), [IMG(1)]),
        ("hybrid: text cover + scanned tables → page images", hybrid_pdf(), [IMG(4), IMG(5)]),
    ]:
        sent_requests.clear()
        started = time.time()
        fin = pt.extract_financials(pdf)
        content = sent_requests[0]["messages"][0]["content"] if sent_requests else ""
        sent_pages = sent_page_labels(content)
        ok = (fin is not None and fin["revenue_from_operations"][0] == 662.58 and fin["period_end"] == "2026-06-30"
              and sent_requests[0]["max_tokens"] == pt.model_call_params(pt.EXTRACTION_MODEL)["max_tokens"]
              and sent_requests[0]["model"] == pt.EXTRACTION_MODEL
              and sent_pages == expect_pages)
        if isinstance(content, str) and len(expect_pages) == 2:
            ok = ok and "consolidated" in content.split("PDF TEXT:")[-1].split("--- PAGE 5")[0].lower()
        if isinstance(content, list):      # scanned pages: real PNGs, within Claude's size limit
            images = [p["image_url"]["url"] for p in content if p["type"] == "image_url"]
            sizes = [Image.open(io.BytesIO(base64.b64decode(u.split(",", 1)[1]))).size for u in images]
            ok = ok and len(images) == len(expect_pages) and all(u.startswith("data:image/png;base64,") for u in images) \
                and all(max(sz) <= pt.IMAGE_MAX_EDGE for sz in sizes) and pt.find_result_pages(pdf)["basis"] in ("consolidated", "standalone")
        check(f"{label} ({time.time() - started:.0f}s)", ok, sent_pages)

    t_text, t_scan = {}, {}
    pt.extract_financials(text_pdf(PAGES), timings=t_text)
    check("timings: text PDF records page scan + model, no OCR", set(t_text) == {"scan", "model"}, t_text)
    pt.extract_financials(scanned_pdf(PAGES[:2]), timings=t_scan)
    check("timings: scanned PDF records OCR and image rendering separately",
          set(t_scan) == {"scan", "ocr", "render", "model"} and t_scan["ocr"] > t_scan["scan"], t_scan)

    # Locating OCR runs at the low resolution
    dpis = []
    real_ocr_page = pt.ocr_page
    def spy_ocr(pdf_bytes, page_num, dpi=pt.LOCATE_OCR_DPI):
        dpis.append(dpi)
        return real_ocr_page(pdf_bytes, page_num, dpi)
    with mock.patch.object(pt, "ocr_page", spy_ocr):
        pt.extract_financials(scanned_pdf(PAGES[:2]))
    check("locating OCR at LOCATE_OCR_DPI only", dpis and set(dpis) == {pt.LOCATE_OCR_DPI}, dpis)

    # Provider refuses images → fall back to full-quality OCR text, and stop trying images
    def refuse_images(**kw):
        sent_requests.append(kw)
        if isinstance(kw["messages"][0]["content"], list):
            raise RuntimeError("Error code: 400 - messages.0.content.1.image.source.base64.media_type: image not supported")
        return Resp(MODEL_JSON)
    sent_requests.clear()
    dpis.clear()
    with mock.patch.object(pt.client.chat.completions, "create", refuse_images), \
         mock.patch.object(pt, "ocr_page", spy_ocr), LogCapture() as logs:
        fin = pt.extract_financials(scanned_pdf(PAGES[:2]))
        rejected_after_first = pt._images_rejected
        sent_requests_first = len(sent_requests)
        sent_requests.clear()
        pt.extract_financials(scanned_pdf(PAGES[:2]))
    check("images refused: falls back to OCR text, figures still extracted",
          fin is not None and fin["revenue_from_operations"][0] == 662.58 and sent_requests_first == 2
          and pt.FALLBACK_OCR_DPI in dpis and logs.has_line("WARNING", "Provider rejected page images"), (sent_requests_first, dpis))
    check("after a refusal, later scanned PDFs go straight to text", rejected_after_first and len(sent_requests) == 1
          and isinstance(sent_requests[0]["messages"][0]["content"], str))
    pt._images_rejected = False

    # A transient error falls back for that filing only
    def flaky_images(**kw):
        sent_requests.append(kw)
        if isinstance(kw["messages"][0]["content"], list):
            raise RuntimeError("Request timed out")
        return Resp(MODEL_JSON)
    sent_requests.clear()
    with mock.patch.object(pt.client.chat.completions, "create", flaky_images):
        fin = pt.extract_financials(scanned_pdf(PAGES[:2]))
    check("transient image-call error: text fallback, images stay on",
          fin is not None and len(sent_requests) == 2 and not pt._images_rejected)

    # Images can be switched off
    sent_requests.clear()
    with mock.patch.object(pt, "SEND_SCANS_AS_IMAGES", False):
        fin = pt.extract_financials(scanned_pdf(PAGES[:2]))
    check("SEND_SCANS_AS_IMAGES off: scanned pages sent as OCR text",
          fin is not None and len(sent_requests) == 1 and isinstance(sent_requests[0]["messages"][0]["content"], str))

    # raw_out carries the model's JSON even when it fails validation (no unit)
    def no_unit(**kw):
        return Resp('{"basis":"standalone","unit":null,"period_end":"2025-03-31","revenue_from_operations":[1,2,3]}')
    raw = {}
    with mock.patch.object(pt.client.chat.completions, "create", no_unit):
        fin = pt.extract_financials(text_pdf(PAGES), raw_out=raw)
    check("raw_out keeps period_end when the unit is missing", fin is None and raw["data"]["period_end"] == "2025-03-31")

    sent_requests.clear()
    pt.extract_financials(text_pdf(PAGES))
    check("text PDF still sent as text, no images", isinstance(sent_requests[0]["messages"][0]["content"], str))

    sent_requests.clear()
    pt.extract_financials(text_pdf(PAGES), model="google/gemini-test")
    check("model override passed through", sent_requests[0]["model"] == "google/gemini-test")

    sent_requests.clear()
    try:
        pt.extract_financials(text_pdf([COVER, NOTES]))
        check("no results table -> SkipFiling, model not called", False)
    except pt.SkipFiling as e:
        check("no results table -> SkipFiling, model not called", str(e) == "no results table found" and not sent_requests, str(e))


    sent_requests.clear()
    with LogCapture() as logs:
        pt.extract_financials(text_pdf(PAGES), ambiguous=True)
    check("ambiguous with table: logged found, model called", logs.has_line("DEBUG", "ambiguous outcome → results table found") and len(sent_requests) == 1)

    sent_requests.clear()
    with LogCapture() as logs:
        try:
            pt.extract_financials(text_pdf([COVER, NOTES]), ambiguous=True)
            skipped = False
        except pt.SkipFiling:
            skipped = True
    check("ambiguous without table: logged not found, skipped, no model call",
          skipped and logs.has_line("DEBUG", "ambiguous outcome → results table not found") and not sent_requests)

    ocr_seen = []
    def fake_ocr(pdf_bytes, page_num):
        ocr_seen.append(page_num)
        return ""
    blank12 = text_pdf([["."]] * 12)
    with mock.patch.object(pt, "ocr_page", fake_ocr):
        for ambiguous, want in [(True, list(range(1, 9))), (False, list(range(1, 13)))]:
            ocr_seen.clear()
            try:
                pt.extract_financials(blank12, ambiguous=ambiguous)
            except pt.SkipFiling:
                pass
            check(f"scanned PDF OCR pages ({'ambiguous, capped at 8' if ambiguous else 'normal'})", ocr_seen == want, ocr_seen)

        hybrid12 = text_pdf([COVER] + [["."]] * 11)      # enough text overall, 11 near-empty pages
        ocr_seen.clear()
        try:
            pt.extract_financials(hybrid12, ambiguous=True)
        except pt.SkipFiling:
            pass
        check("hybrid PDF low-text OCR also capped for ambiguous", ocr_seen == list(range(2, 9)), ocr_seen)

    sent_requests.clear()
    try:
        pt.extract_financials(scanned_pdf([COVER]))
        check("scanned PDF with no table after OCR -> SkipFiling", False)
    except pt.SkipFiling:
        check("scanned PDF with no table after OCR -> SkipFiling", not sent_requests)

def scan_with_text_layer(pages, layers):
    """Scanned pages that carry the scanner's own invisible text layer (like GM
    Breweries, 8 Oct 2026): a full-page JPEG plus `layers[i]` as text render mode 3."""
    scans = pdfium.PdfDocument(scanned_pdf(pages))
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    streams, kids = {}, []
    for i, lines in enumerate(layers):
        im = scans[i].render(scale=100 / 72).to_pil().convert("RGB")
        jpg = io.BytesIO()
        im.save(jpg, "JPEG", quality=85)
        objs.append(f"<< /Type /XObject /Subtype /Image /Width {im.width} /Height {im.height} /ColorSpace /DeviceRGB "
                    f"/BitsPerComponent 8 /Filter /DCTDecode /Length {len(jpg.getvalue())} >>")
        streams[len(objs)] = jpg.getvalue()
        img_ref = len(objs)
        text = " ".join("(" + l.replace("(", "\\(").replace(")", "\\)") + ") Tj T*" for l in lines)
        body = f"q 595 0 0 842 0 0 cm /Im1 Do Q BT 3 Tr /F1 9 Tf 12 TL 40 800 Td {text} ET"
        objs.append(f"<< /Length {len(body)} >>")
        streams[len(objs)] = body.encode("latin-1")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                    f"/Resources << /Font << /F1 3 0 R >> /XObject << /Im1 {img_ref} 0 R >> >> /Contents {len(objs)} 0 R >>")
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = b"%PDF-1.4\n", []
    for n, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{o}\n".encode("latin-1")
        if n in streams:
            out += b"stream\n" + streams[n] + b"\nendstream\n"
        out += b"endobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out

# The scanner's layer: row names without figures, one table marker, padded past LOW_TEXT_PAGE_CHARS
GARBLE = ["Begd. Office : Ganesh Niwas, S. Veer Savarkar Marg, Prabhadevi", "Particulars", "Finance Cost",
          "Depreciation and amortisation expenses", "Tax exoenses I Current & Deffered Taxes",
          "Loss on fair valuation oI investment in equiW shares (Net of"]
GARBLE_CONS = ["Consolidated Unaudited Financial results for the"] + GARBLE
layered = scan_with_text_layer(PAGES, [COVER, GARBLE, NOTES * 4, GARBLE_CONS, NOTES * 4])
ocr_order = []
real_ocr = pt.ocr_page
def spy_order(pdf_bytes, page_num, dpi=pt.LOCATE_OCR_DPI):
    ocr_order.append(page_num)
    return real_ocr(pdf_bytes, page_num, dpi)
with mock.patch.object(pt.client.chat.completions, "create", fake_create), mock.patch.object(pt, "ocr_page", spy_order):
    import pdfplumber as _pp
    with _pp.open(io.BytesIO(layered)) as _doc:
        layer_ok = all(len((pg.extract_text() or "").strip()) >= pt.LOW_TEXT_PAGE_CHARS for pg in _doc.pages) \
                   and all(pt.is_scanned_page(pg) for pg in _doc.pages)
        no_table_in_layer = pt.find_table_pages([pg.extract_text() or "" for pg in _doc.pages]) == ([], None)
    check("test PDF: scans with a full text layer that shows no table", layer_ok and no_table_in_layer)
    sent_requests.clear()
    fin = pt.extract_financials(layered)
    sent = sent_page_labels(sent_requests[0]["messages"][0]["content"]) if sent_requests else []
    check("scans with a garbled text layer are OCR'd, the consolidated-looking page first (GM Breweries)",
          fin is not None and sent == [IMG(4), IMG(5)] and ocr_order == [4, 5], (ocr_order, sent))
    with _pp.open(io.BytesIO(text_pdf(PAGES))) as _doc:
        check("a text PDF has no scanned pages", not any(pt.is_scanned_page(pg) for pg in _doc.pages))

    # A scan whose own layer does show the table: found without OCR, still sent as an image
    readable = scan_with_text_layer(PAGES, [COVER, table_lines("Standalone"), NOTES * 4, table_lines("Consolidated"), NOTES * 4])
    ocr_order.clear()
    sent_requests.clear()
    pt.extract_financials(readable)
    sent = sent_page_labels(sent_requests[0]["messages"][0]["content"]) if sent_requests else []
    check("scan with a usable layer: no OCR, but its pages go as images (its digits can't be trusted)",
          ocr_order == [] and sent == [IMG(4), IMG(5)], (ocr_order, sent))

check("OCR'd 'lncome' still counts as income",
      pt.classify_result_page("Sales / lncome from operations 2,97,657\nFinance Cost\n") == "untitled")
check("table hints: a consolidated title ranks first",
      pt.table_hints("Consolidated Unaudited Financial results for the\nFinance Cost") >
      pt.table_hints("Unaudited Financial results for the\nFinance Cost") > pt.table_hints("Finance Cost"))

# ─────────────────────────────────────────────────────────────
section("old quarter from the headline (7 Oct 2026 live run)")

SHIVOM = ("Shivom Investment & Consultancy Ltd - 539833 - Result For The Quarter & Financial Year Ended On 31.03.2025 "
          "Financial Results of the Quarter & Financial Year ended on 31.03.2025 Financial Results")
INDRAYANI = ("Indrayani Biotech Ltd - 526445 - Board Meeting Outcome for Board Meeting Outcome For Unaudited Standalone & "
             "Consolidated Financial Results For The Quarter Ended 30Th June 2026 Along With Limited Review Report.")
INDRAYANI_2 = "Results-Unaudited Standalone & Consolidated Financial Results For Quarter Ended June 30, 2026"
TIAAN = ("Submission Of Un-Audited Financial Results Of (Tiaan Consumer Limited) For The Quarter And Half Year "
         "Ended On September 30Th, 2026 And Along With Limited Review Report Thereon.")
ALSTONE = "Board Meeting Outcome for Outcome Of Meeting Of Board Of Directors Held On Wednesday, 07Th October, 2026"

check("dates: dd.mm.yyyy", pt.headline_period_ends(SHIVOM) == [date(2025, 3, 31)])
check("dates: 30Th June 2026 / June 30, 2026",
      pt.headline_period_ends(INDRAYANI) == [date(2026, 6, 30)] and pt.headline_period_ends(INDRAYANI_2) == [date(2026, 6, 30)])
check("dates: September 30Th, 2026", pt.headline_period_ends(TIAAN) == [date(2026, 9, 30)])
check("dates: meeting date isn't a quarter end", pt.headline_period_ends(ALSTONE) == [])
check("dates: 30-Jun-2026, 31/12/25, 2026-09-30",
      pt.headline_period_ends("results 30-Jun-2026") == [date(2026, 6, 30)]
      and pt.headline_period_ends("q3 ended 31/12/25") == [date(2025, 12, 31)]
      and pt.headline_period_ends("period 2026-09-30") == [date(2026, 9, 30)])
check("dates: impossible dates ignored", pt.headline_period_ends("31.06.2026 and 30.02.2026") == [])

with mock.patch.object(pt, "SCORE_FROM_QUARTER", "Q2FY27"):
    check("old by headline: Shivom (FY25) and Indrayani (Q1FY27)",
          pt.headline_old_quarter(SHIVOM) == "Q4FY25" and pt.headline_old_quarter(INDRAYANI) == "Q1FY27")
    check("not old: Tiaan (Sep quarter), Alstone (no date)",
          pt.headline_old_quarter(TIAAN) is None and pt.headline_old_quarter(ALSTONE) is None)
    check("latest date wins: Q1 results approved at a 30 Sep meeting aren't skipped by headline",
          pt.headline_old_quarter("Outcome of meeting held on 30.09.2026: results for quarter ended 30.06.2026") is None)

    fresh_state_dir()
    shivom = pt.normalise_bse(bse_raw("sh1", 539833, "Result", "2026-10-07T12:21:05", sub=SHIVOM))
    indra = pt.normalise_bse(bse_raw("in1", 526445, "Board Meeting", "2026-10-07T17:58:18", sub=INDRAYANI))
    fresh = pt.normalise_bse(bse_raw("ok1", 540108, "Result", "2026-10-07T19:25:17", sub=TIAAN))
    extract, calls = counting_extract({fresh["attachment_url"]: mk(period_end="2026-09-30", **GOOD)})
    downloads = []
    with LogCapture() as logs:
        run_main(1, bse=lambda k: [shivom, indra, fresh], extract=extract,
                 download=lambda f, c: downloads.append(f["id"]) or b"%PDF-" + f["attachment_url"].encode())
    seen = pt.load_seen()
    check("old-by-headline filings: OLD line, not downloaded, marked seen",
          logs.has_line("OLD", "Co 539833", "Q4FY25: old quarter per headline", "not downloaded")
          and logs.has_line("OLD", "Co 526445", "Q1FY27: old quarter per headline")
          and downloads == ["BSE:ok1"] and {"BSE:sh1", "BSE:in1"} <= seen, (downloads, logs.terminal()))
    check("POLL summary counts them", logs.has_line("POLL", "2 old quarter by headline"))

# ─────────────────────────────────────────────────────────────
section("sanity check on extracted figures")

def fin_with(**kw):
    f = mk(period_end="2026-09-30", **kw)
    return f

# Tiaan Consumer and Golkonda Aluminium, 7 Oct 2026 (OCR misreads), in crores
TIAAN_FIN = fin_with(revenue_from_operations=[88.0, 88.0522, 828.0316], total_income=[88.0, 88.0522, 828.0316],
                     total_expenses=[828.0371, 0.9311, 828.1064], other_expenses=[828.0021, 0.9237, None],
                     pbt=[-0.0371, -0.0377, -0.1017], pat=[-0.0371, -0.0377, -0.1017])
GOLKONDA_FIN = fin_with(revenue_from_operations=[39.9017, None, None], total_expenses=[1399.0338, None, 0.1388],
                        employee_expense=[0.0168, 0.0238, None], pat=[0.0288, 0.0471, -0.01848], basic_eps=[0.18, None, -0.06])
ALSTONE_FIN = fin_with(revenue_from_operations=[784.8842, None, None], total_income=[785.0515, None, None],
                       pbt=[-106.7225, None, None], pat=[-106.7225, None, None])
INDRAYANI_FIN = fin_with(revenue_from_operations=[37.7055, 37.7642, 29.1037], total_income=[39.2965, 38.6481, 30.1599],
                         total_expenses=[38.7076, 39.76, 29.9512], pbt=[0.5888, -1.1119, 0.2087], pat=[0.54, 0.976, 0.1565])

check("Tiaan: income minus expenses nowhere near PBT", "this quarter" in (pt.numbers_problem(TIAAN_FIN) or ""), pt.numbers_problem(TIAAN_FIN))
check("Golkonda: expenses 35x revenue with a profit", "PAT" in (pt.numbers_problem(GOLKONDA_FIN) or ""), pt.numbers_problem(GOLKONDA_FIN))
check("Alstone: too few figures to judge → passes", pt.numbers_problem(ALSTONE_FIN) is None)
check("real consistent result passes", pt.numbers_problem(INDRAYANI_FIN) is None, pt.numbers_problem(INDRAYANI_FIN))
check("total income below revenue fails",
      "below revenue" in (pt.numbers_problem(fin_with(revenue_from_operations=[100, 90, 80], total_income=[60, 91, 81])) or ""))
check("expense line above total expenses fails",
      "above total expenses" in (pt.numbers_problem(fin_with(revenue_from_operations=[100, 90, 80], total_income=[101, 91, 81],
                                                            total_expenses=[90, 80, 70], other_expenses=[500, 1, 1], pbt=[11, 11, 11])) or ""))
check("high-margin company with only PAT passes (tax gap allowed)",
      pt.numbers_problem(fin_with(revenue_from_operations=[200, 150, 100], total_expenses=[100, 80, 50], pat=[70, 50, 35])) is None)
check("Bridge Securities year-ago gain no longer flagged once the exceptional item is read",
      pt.numbers_problem(fin_with(revenue_from_operations=[0.171, 0.4231, 0.1715], total_income=[0.1715, 0.4231, 0.1715],
                                  total_expenses=[0.0289, 0.1287, 0.0306], exceptional_items=[None, None, 0.2516],
                                  pbt=[0.1426, -0.1287, 0.3925], pat=[0.143, -0.1283, 0.3929])) is None)
check("exceptional item printed as a positive loss still adds up",
      pt.numbers_problem(fin_with(revenue_from_operations=[100, 90, 80], total_income=[100, 90, 80], total_expenses=[60, 60, 60],
                                  exceptional_items=[30, None, None], pbt=[10, 30, 20])) is None)
check("without the exceptional item it's still caught, with the item named when there is one",
      "income 0.17 minus expenses 0.03 = 0.14, but PBT is 0.39" in (pt.numbers_problem(fin_with(
          revenue_from_operations=[0.171, 0.4231, 0.1715], total_income=[0.1715, 0.4231, 0.1715],
          total_expenses=[0.0289, 0.1287, 0.0306], pbt=[0.1426, -0.1287, 0.3925])) or "")
      and "plus exceptional items 1.00 = 41.00, but PBT is 10.00" in (pt.numbers_problem(fin_with(
          revenue_from_operations=[100, 90, 80], total_income=[100, 90, 80], total_expenses=[60, 60, 60],
          exceptional_items=[1, None, None], pbt=[10, 30, 20])) or ""))
check("exceptional item within 10% of income passes",
      pt.numbers_problem(fin_with(revenue_from_operations=[1000, 900, 800], total_income=[1000, 900, 800],
                                  total_expenses=[900, 820, 720], pbt=[80, 80, 80])) is None)

# Flagged result: saved, never alerted, company-quarter left open for the other exchange's copy
fresh_state_dir()
GARBLED = dict(GOOD, total_income=[155, 125, 105], total_expenses=[500, 90, 85])
garbled_bse = pt.normalise_bse(bse_raw("g1", 500777, "Result", "2026-10-07T10:00:00"))
clean_nse = pt.normalise_nse(nse_raw("g2", "GARB", "INE777A01011", "07-Oct-2026 10:05:00"))
extract, calls = counting_extract({garbled_bse["attachment_url"]: mk(period_end="2026-09-30", **GARBLED),
                                   clean_nse["attachment_url"]: mk(period_end="2026-09-30", **GOOD)})
with LogCapture() as logs:
    alerts = run_main(1, bse=lambda k: [garbled_bse], nse=lambda c: [clean_nse], extract=extract,
                      master={"bse": {"500777": "INE777A01011"}, "nse": {}})
rows = list(csv.DictReader(open(pt.RESULTS_CSV, encoding="utf-8")))
check("garbled copy: FLAG warning line, no alert", logs.has_line("FLAG", "Co 500777", "numbers don't add up", "no alert")
      and any(lvl == logging.WARNING and st == "FLAG" for st, lvl, _ in logs.records), logs.terminal())
check("garbled row saved with its check reason, clean copy alerted",
      [(r["scrip"], bool(r["check"])) for r in rows] == [("500777", True), ("GARB", False)] and alerts == ["NSE:Co GARB"], (rows, alerts))
check("only the clean copy closes the company-quarter",
      set(json.load(open(pt.PROCESSED_SCRIPS_FILE))) == {"INE777A01011_Q2FY27"} and {"BSE:g1", "NSE:g2"} <= pt.load_seen())

# Two copies that both fail the check: one FLAG row, not two (Bridge Securities, 9 Oct 2026)
fresh_state_dir()
garbled_2 = pt.normalise_bse(bse_raw("g3", 500777, "Result", "2026-10-07T10:06:00", attach="g3.pdf"))
extract, calls = counting_extract({garbled_bse["attachment_url"]: mk(period_end="2026-09-30", **GARBLED),
                                   garbled_2["attachment_url"]: mk(period_end="2026-09-30", **GARBLED)})
with LogCapture() as logs:
    alerts = run_main(2, bse=lambda k: [garbled_bse, garbled_2], extract=extract,
                      master={"bse": {"500777": "INE777A01011"}, "nse": {}})
rows = list(csv.DictReader(open(pt.RESULTS_CSV, encoding="utf-8")))
check("a second failing copy saves no second row",
      len(rows) == 1 and logs.has_line("SKIP", "Co 500777", "already flagged") and alerts == []
      and json.load(open(pt.FLAGGED_FILE)) == ["INE777A01011_Q2FY27"] and json.load(open(pt.PROCESSED_SCRIPS_FILE)) == [],
      (rows, logs.terminal()))

# ─────────────────────────────────────────────────────────────
section("catch-up filings")

pt.SCANNER_STARTED_AT = datetime(2026, 10, 7, 19, 51, 43)
check("published before start → catch-up", pt.is_catch_up(datetime(2026, 10, 7, 14, 0)) and "(catch-up)" in pt.format_delay(datetime(2026, 10, 7, 14, 0)))
check("published after start → not catch-up", not pt.is_catch_up(datetime(2026, 10, 7, 19, 52)) and not pt.is_catch_up(None))
pt.SCANNER_STARTED_AT = None
check("no start time (tests, compare_models) → never catch-up", not pt.is_catch_up(datetime(2020, 1, 1)))

fresh_state_dir()
old_filing = pt.normalise_bse(bse_raw("cu1", 500888, "Result", "2026-10-07T14:00:00"))
extract, calls = counting_extract({old_filing["attachment_url"]: mk(period_end="2026-09-30", **GOOD)})
with LogCapture() as logs:
    run_main(1, bse=lambda k: [old_filing], extract=extract)
check("scanner run tags a filing published before it started", logs.has_line("ALERT", "Co 500888", "(catch-up)"), logs.terminal())
pt.SCANNER_STARTED_AT = None

parsed = parse_log("""19:53:03  SCORE  BSE  Alstone Textiles (India)         0.0/50  Q2FY27  standalone  ⏱ download 1.0s · page scan 0.2s · OCR 8.9s · model 2.9s · exchange→scored 5h 53m 50s (catch-up)
19:58:10  ALERT  NSE  Example Tech                    41.0/50  Q2FY27  consolidated  Telegram sent  ⏱ download 0.4s · page scan 0.2s · model 2.8s · exchange→alert 48s
19:58:40  FLAG   BSE  Tiaan Consumer                   0.0/50  Q2FY27  standalone  numbers don't add up (x) · no alert  ⏱ download 1.0s · page scan 0.2s · OCR 1.2s · render 0.4s · model 3.0s · exchange→scored 31m 2s
""")
t = parsed["timings"]
check("dashboard parses catch-up flag", [e.get("catchup") for e in t] == [True, False, False] and t[0]["delay"] == 5 * 3600 + 53 * 60 + 50, t)
check("dashboard still reads stage timings around 'render'", t[2].get("ocr") == 1.2 and t[2].get("model") == 3.0, t[2])

# ─────────────────────────────────────────────────────────────
section("clean terminal view")

ct = pt.console_text
check("terminal: timing breakdown dropped",
      ct("SCORE", "12.0/50  Q2FY27  standalone  ⏱ download 0.5s · page scan 0.3s · model 2.9s · exchange→scored 1m 4s")
      == "12.0/50  Q2FY27  standalone")
check("terminal: alert shows how long after the filing",
      ct("ALERT", "41.0/50  Q2FY27  consolidated  Telegram sent  ⏱ download 0.4s · model 3.1s · exchange→alert 58s")
      == "41.0/50  Q2FY27  consolidated  Telegram sent  ·  58s after filing")
check("terminal: catch-up kept",
      ct("SCORE", " 0.0/50  Q2FY27  standalone  ⏱ model 2.9s · exchange→scored 6h 46m 2s (catch-up)").endswith("(catch-up)"))
check("terminal: old-by-title wording",
      ct("OLD", "Q1FY27: old quarter per headline, ignored (scoring from Q2FY27) · not downloaded") == "Q1FY27 result, skipped by its title")
check("terminal: duplicate wording", ct("SKIP", "INE0DRI01029_Q2FY27 already scored (quarter from PDF)") == "already scored this quarter")
check("terminal: sanity-check reason left to the log file",
      ct("FLAG", " 0.0/50  Q2FY27  standalone  numbers don't add up (this quarter: income 88.00 minus expenses 828.04 = -740.04, but PBT is -0.04) · no alert")
      == " 0.0/50  Q2FY27  standalone  numbers don't add up · not alerted")

rec = logging.LogRecord("pead_tool", logging.INFO, __file__, 1, "x", None, None)
rec.status, rec.exchange, rec.company, rec.text = "ALERT", "NSE", "Kaveri Agro Foods Limited", "41.0/50  Q2FY27  consolidated  Telegram sent"
plain = pt.ConsoleFormatter(False).format(rec)
coloured = pt.ConsoleFormatter(True).format(rec)
check("terminal line: status, exchange, short padded company", re.fullmatch(
    r"\d\d:\d\d:\d\d  ALERT  NSE  Kaveri Agro Foods {11}  41\.0/50  Q2FY27  consolidated  Telegram sent", plain) is not None, plain)
check("terminal colours only when enabled", "\033[" not in plain and "\033[1;32mALERT" in coloured)
check("no colours when output is redirected", pt.console_colours_supported(io.StringIO()) is False)

class TerminalCapture:
    """What the clean terminal would print during a run (the real console handler's filter + formatter)."""
    def __enter__(self):
        self.buf = io.StringIO()
        self.handler = logging.StreamHandler(self.buf)
        self.handler.setLevel(logging.INFO)
        self.handler.setFormatter(pt.ConsoleFormatter(False))
        self.handler.addFilter(pt._console_visible)
        logging.getLogger().addHandler(self.handler)
        return self
    def __exit__(self, *exc):
        logging.getLogger().removeHandler(self.handler)
    def lines(self):
        return [l[10:] for l in self.buf.getvalue().splitlines()]      # drop "HH:MM:SS  "

fresh_state_dir()
intim = pt.normalise_bse(bse_raw("t1", 510001, "Board Meeting", "2026-10-07T10:00:00", sub="Board Meeting Intimation for results"))
vague = pt.normalise_bse(bse_raw("t2", 510002, "Board Meeting", "2026-10-07T10:01:00", sub="Outcome of Board Meeting held today"))
good = pt.normalise_bse(bse_raw("t3", 510003, "Result", "2026-10-07T10:02:00", sub="Unaudited Financial Results for quarter ended 30.09.2026"))
extract, calls = counting_extract({vague["attachment_url"]: pt.SkipFiling("no results table found", status="NONE"),
                                   good["attachment_url"]: mk(period_end="2026-09-30", **GOOD)})
with TerminalCapture() as term:
    run_main(2, bse=lambda k: [intim, vague, good], extract=extract)
out = term.lines()
check("terminal: 3-line banner, the alert, the catch-up summary, a rule, then a POLL line",
      len(out) == 7 and out[0].startswith("PEAD scanner") and out[1].endswith("0 awaiting retry")
      and out[2] == "Scanning from 00:00 today  ·  no earlier run to pick up from"
      and out[3].startswith("ALERT  BSE  Co 510003")
      and out[4].startswith("Caught up on today") and set(out[5].replace("  watching for new filings every 30s  ", "")) == {"─"}
      and out[6].startswith("POLL "), out)
check("banner carries the date (the terminal's times don't)", out[0].endswith(f"{datetime.now():%a %d %b %Y}"), out[0])
check("catch-up summary counts what was skipped quietly",
      "1 result filing" in out[4] and "1 intimation skipped" in out[4] and "1 board meeting outcome to check" in out[4], out[4])
check("no CHECK, quiet NONE or DEBUG lines; POLL only as the short per-poll line",
      not any(l.startswith(("CHECK", "NONE", "DEBUG")) for l in out)
      and not any("announcements (fetch OK" in l or "checker queue" in l for l in out), out)

fresh_state_dir()
with TerminalCapture() as term:
    run_main(4, bse=lambda k: [], extract=extract)
polls = [l for l in term.lines() if l.startswith("POLL")]
check("a POLL line after every poll once caught up (3 of 4 polls)", len(polls) == 3, term.lines())
check("quiet POLL line: both exchanges and 'nothing relevant'",
      polls and polls[-1] == "POLL   BSE 0 new  ·  NSE 0 new  ·  nothing relevant", polls)

fresh_state_dir()
fed = {"n": 0}
def results_on_poll_2(known):
    fed["n"] += 1
    return [intim, good] if fed["n"] == 2 else []
extract, calls = counting_extract({good["attachment_url"]: mk(period_end="2026-09-30", **GOOD)})
with TerminalCapture() as term:
    run_main(3, bse=results_on_poll_2, extract=extract)
out = term.lines()
poll_2 = [l for l in out if l.startswith("POLL")]
check("POLL line counts that poll's announcements and what they were",
      poll_2[0] == "POLL   BSE 2 new  ·  NSE 0 new  ·  1 result  ·  1 intimation skipped"
      and poll_2[1] == "POLL   BSE 0 new  ·  NSE 0 new  ·  nothing relevant", poll_2)
check("the alert comes before its poll's POLL line",
      out.index(poll_2[0]) == next(i for i, l in enumerate(out) if l.startswith("ALERT")) + 1, out)

fake_state = type("S", (), {"fetched": {"BSE": 3, "NSE": 0}, "window": {"results": 1, "retries": 3}})()
check("POLL line counts retries apart from new results (9 Oct 2026: retries showed as '12 results')",
      pt.poll_line(fake_state) == ("BSE 3 new  ·  NSE 0 new  ·  1 result  ·  3 retries", None), pt.poll_line(fake_state))
check("MISS lines are yellow in the terminal", pt.STATUS_COLOURS.get("MISS") == "yellow")

line_rec = logging.LogRecord("pead_console", logging.INFO, __file__, 1, "BSE 0 new  ·  NSE 0 new  ·  nothing relevant", None, None)
line_rec.word, line_rec.style = "POLL", "dim"
check("POLL line coloured dim as a whole when enabled",
      pt.ConsoleFormatter(True).format(line_rec).endswith("\033[2mPOLL \033[0m  \033[2mBSE 0 new  ·  NSE 0 new  ·  nothing relevant\033[0m"))
check("--verbose skips the short POLL line (it shows the file's POLL lines)",
      pt._not_poll_summary(line_rec) is False and pt._not_poll_summary(rec) is True)

fresh_state_dir()
pt.save_processed_scrips({"BSE-509084_Q4FY26"})
with TerminalCapture() as term:
    run_main(1)
check("housekeeping lines (keys without ISIN) stay out of the terminal",
      term.lines()[0].startswith("PEAD scanner") and not any("Processed keys" in l for l in term.lines()), term.lines())

fresh_state_dir()
state_calls = {"n": 0}
def bse_down_then_up(known):
    state_calls["n"] += 1
    if state_calls["n"] <= 2:
        raise RuntimeError("connection reset")
    return []
with TerminalCapture() as term:
    run_main(3, bse=bse_down_then_up, extract=extract)
down = [l for l in term.lines() if "BSE isn't answering" in l]
check("exchange failure: said once, then once when it recovers",
      len(down) == 1 and sum("BSE is answering again" in l for l in term.lines()) == 1
      and not any("fetch FAILED" in l for l in term.lines()), term.lines())
check("POLL line while an exchange is down says so",
      "POLL   BSE not answering  ·  NSE 0 new  ·  nothing relevant" in term.lines(), term.lines())

# pead_terminal.log: the same lines as the terminal, no colours
mirror_path = os.path.join(tempfile.mkdtemp(prefix="pead_term_"), "pead_terminal.log")
mirror = pt.setup_terminal_log(mirror_path)
fresh_state_dir()
try:
    with TerminalCapture() as term:
        run_main(2, bse=lambda k: [intim, vague, good], extract=counting_extract(
            {vague["attachment_url"]: pt.SkipFiling("no results table found", status="NONE"),
             good["attachment_url"]: mk(period_end="2026-09-30", **GOOD)})[0])
        pt.log.debug("debug detail")
        pt.log.warning("per-filing warning", extra=pt.QUIET)
    mirror.flush()
finally:
    logging.getLogger().removeHandler(mirror)
    mirror.close()
mirrored = open(mirror_path, encoding="utf-8").read().splitlines()
check("pead_terminal.log has exactly the terminal's lines",
      [l[10:] for l in mirrored] == term.lines() and len(mirrored) == 7, (mirrored, term.lines()))
check("pead_terminal.log: no colours, no DEBUG, no file-only lines",
      not any("\033[" in l or "debug detail" in l or "per-filing warning" in l for l in mirrored))
check("pead_terminal.log rotates at 1 MB with 2 backups, UTF-8, next to the script",
      mirror.maxBytes == 1024 * 1024 and mirror.backupCount == 2 and mirror.encoding == "utf-8"
      and pt.TERMINAL_LOG_FILE == os.path.join(os.path.dirname(os.path.abspath(pt.__file__)), "pead_terminal.log"))

fresh_state_dir()
with LogCapture() as logs:
    run_main(1, bse=lambda k: [intim, vague, good], extract=extract)
check("log file still gets POLL and CHECK lines (dashboard parses them)",
      logs.has_line("POLL", "BSE: 3 new announcements") and logs.has_line("CHECK", "Co 510002"))

# ─────────────────────────────────────────────────────────────
section("resume from where the last run stopped")

from datetime import timedelta
today = date.today()
yesterday = today - timedelta(days=1)

def state_with(marks):
    st = pt.ScannerState.__new__(pt.ScannerState)
    st.checkpoint = marks
    return st
check("no checkpoint → today only", state_with({}).resume_from("BSE") is None)
check("read earlier today → today only", state_with({"BSE": datetime.combine(today, datetime.min.time())}).resume_from("BSE") is None)
night = datetime.combine(yesterday, datetime.min.time()).replace(hour=21)
check("stopped at 9 pm yesterday → reads from yesterday", state_with({"BSE": night}).resume_from("BSE") == yesterday)
check("stopped 20 days ago → reads the last 7 days only",
      state_with({"NSE": night - timedelta(days=19)}).resume_from("NSE") == today - timedelta(days=pt.MAX_RESUME_DAYS))
check("resume point shown in the terminal", state_with({"BSE": night, "NSE": night + timedelta(minutes=1)}).resumed_since() == night)
noon = datetime.combine(today, datetime.min.time()).replace(hour=12, minute=5)
check("same-day restart: scanning from the last scan, not midnight", state_with({"BSE": noon, "NSE": noon}).resumed_since() == noon)
check("no checkpoint: no resume point", state_with({}).resumed_since() is None and state_with({}).last_stop() is None)
check("stopped 20 days ago: scanning from 7 days back, last scan still known",
      state_with({"BSE": night - timedelta(days=19)}).resumed_since()
      == datetime.combine(today - timedelta(days=pt.MAX_RESUME_DAYS), datetime.min.time())
      and state_with({"BSE": night - timedelta(days=19)}).last_stop() == night - timedelta(days=19))
ref = datetime(2026, 10, 7, 16, 0)
check("times in words",
      pt.when_text(datetime(2026, 10, 7, 9, 5), ref) == "09:05 today"
      and pt.when_text(datetime(2026, 10, 6, 21, 0), ref) == "21:00 yesterday"
      and pt.when_text(datetime(2026, 10, 5, 21, 0), ref) == "21:00 on Mon 05 Oct")

urls = []
def record_bse(url, headers=None, timeout=None):
    urls.append(url)
    return FakeResponse(200, {"Table": []})
with mock.patch.object(pt.requests, "get", record_bse), mock.patch.object(pt, "empty_day_is_wrong", lambda d, now=None: False):
    pt.fetch_bse_filings(set(), since=yesterday)
    pt.fetch_bse_filings(set())
check("BSE asks for each day from the resume day",
      f"strPrevDate={yesterday:%Y%m%d}" in urls[0] and f"strToDate={yesterday:%Y%m%d}" in urls[0]
      and f"strPrevDate={today:%Y%m%d}" in urls[1] and f"strToDate={today:%Y%m%d}" in urls[1]
      and f"strPrevDate={today:%Y%m%d}" in urls[2] and len(urls) == 3, urls)
urls.clear()
class RecordingNse:
    def get(self, url, timeout=15):
        urls.append(url)
        return FakeResponse(200, [])
with mock.patch.object(pt, "empty_day_is_wrong", lambda d, now=None: False):
    pt.fetch_nse_filings(RecordingNse(), since=yesterday)
check("NSE asks for the date range",
      f"from_date={yesterday:%d-%m-%Y}" in urls[0] and f"to_date={today:%d-%m-%Y}" in urls[0], urls)

fresh_state_dir()
pt.save_checkpoint({"BSE": night, "NSE": night})
fetch_since.clear()
late = pt.normalise_bse(bse_raw("r1", 520001, "Result", (night + timedelta(minutes=30)).isoformat()))
extract, calls = counting_extract({late["attachment_url"]: mk(period_end="2026-09-30", **GOOD)})
with TerminalCapture() as term:
    alerts = run_main(2, bse=lambda k: [late], extract=extract)
marks = pt.load_checkpoint()
check("restart reads both exchanges from where it stopped", fetch_since[:2] == [("BSE", yesterday), ("NSE", yesterday)], fetch_since)
check("a filing from after last night's stop is processed", alerts == ["BSE:Co 520001"], alerts)
check("checkpoint moves to this run, so the next poll is today only",
      all(m.date() == today for m in marks.values()) and fetch_since[2:] == [("BSE", None), ("NSE", None)], (marks, fetch_since))
check("header says where it's scanning from",
      term.lines()[2] == "Scanning from 21:00 yesterday  ·  where the last scan stopped"
      and any(l.startswith("Caught up since 21:00 yesterday  ·  1 BSE + 0 NSE announcements") for l in term.lines()), term.lines())

fresh_state_dir()
pt.save_checkpoint({"BSE": noon, "NSE": noon + timedelta(minutes=1)})
with TerminalCapture() as term:
    run_main(1, extract=extract)
check("same-day restart: header shows the last scan's time",
      term.lines()[2] == "Scanning from 12:05 today  ·  where the last scan stopped"
      and term.lines()[3].startswith("Caught up since 12:05 today"), term.lines())

fresh_state_dir()
pt.save_checkpoint({"BSE": night - timedelta(days=19), "NSE": night - timedelta(days=19)})
with TerminalCapture() as term:
    run_main(1, extract=extract)
capped = today - timedelta(days=pt.MAX_RESUME_DAYS)
check("long gap: header says the catch-up is capped",
      term.lines()[2] == f"Scanning from 00:00 on {capped:%a %d %b}  ·  last scan was 21:00 on "
                         f"{yesterday - timedelta(days=19):%a %d %b}, catch-up goes back 7 days at most", term.lines())

fresh_state_dir()
pt.save_checkpoint({"BSE": night, "NSE": night})
def bse_fails(known):
    raise RuntimeError("down")
run_main(1, bse=bse_fails, extract=extract)
marks = pt.load_checkpoint()
check("a failed exchange keeps its old checkpoint (read again next time)",
      marks["BSE"] == night and marks["NSE"].date() == today, marks)

# ─────────────────────────────────────────────────────────────
section("out of AICredits credits")

class FakeAPIError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status_code = status

check("402 means out of credits", pt.is_credit_error(FakeAPIError(402, "Payment Required")))
check("400 'insufficient credits' means out of credits",
      pt.is_credit_error(FakeAPIError(400, "Insufficient credits. Please top up your wallet")))
check("429 'exceeded your current quota' means out of credits",
      pt.is_credit_error(FakeAPIError(429, "You exceeded your current quota, please check your plan and billing details")))
check("a plain rate limit is not", not pt.is_credit_error(FakeAPIError(429, "Rate limit reached for requests")))
check("a server error is not", not pt.is_credit_error(FakeAPIError(500, "Internal server error")))
check("an image refusal is not",
      not pt.is_credit_error(FakeAPIError(400, "messages.0.content.1.image.source.base64.media_type: image not supported")))

def reset_credits():
    pt._credits.update(out=False, since=None, next_try=0.0, detail="")

credit_pdf = text_pdf(PAGES)
held_f = pt.normalise_bse(bse_raw("cr1", 600001, "Result", "2026-07-20T10:00:00"))
model_calls, downloads = {"n": 0}, {"n": 0}
def credit_download(filing, client):
    downloads["n"] += 1
    return credit_pdf
def broke_then_topped_up(**kw):
    model_calls["n"] += 1
    if model_calls["n"] <= 2:
        raise FakeAPIError(402, "Insufficient credits. Please top up your wallet.")
    return Resp(MODEL_JSON)

fresh_state_dir(); reset_credits()
with mock.patch.object(pt.client.chat.completions, "create", broke_then_topped_up), \
     mock.patch.object(pt, "CREDITS_RECHECK_SEC", 0), TerminalCapture() as term:
    run_main(4, bse=lambda k: [held_f], extract=pt.extract_financials, download=credit_download)
out = term.lines()
check("terminal says once that credits ran out, and once that they're back",
      sum("AICredits is out of credits" in l for l in out) == 1 and sum("AICredits credits are back" in l for l in out) == 1, out)
check("POLL line counts the result waiting for credits",
      any(l.startswith("POLL") and "1 waiting for credits" in l for l in out), out)
rows = list(csv.DictReader(open(pt.RESULTS_CSV, encoding="utf-8")))
check("the result waited without using retries, then was scored once credits were back",
      model_calls["n"] == 3 and len(rows) == 1 and pt.load_retries() == {} and "BSE:cr1" in pt.load_seen(), (model_calls, rows))

fresh_state_dir(); reset_credits()
model_calls["n"], downloads["n"] = 0, 0
def always_broke(**kw):
    model_calls["n"] += 1
    raise FakeAPIError(402, "Insufficient credits")
with mock.patch.object(pt.client.chat.completions, "create", always_broke), TerminalCapture() as term:
    run_main(3, bse=lambda k: [held_f], extract=pt.extract_financials, download=credit_download)
check("while waiting for credits: no more model calls or downloads, no retry used, not marked seen",
      model_calls["n"] == 1 and downloads["n"] == 1 and pt.load_retries() == {} and "BSE:cr1" not in pt.load_seen(),
      (model_calls, downloads))
check("every POLL line keeps counting the result waiting for credits",
      sum(l.startswith("POLL") and "waiting for credits" in l for l in term.lines()) == 2, term.lines())

# A failed PDF download is retried after 1, 5 and 15 minutes, not every poll
dl_f = pt.normalise_nse(nse_raw("dl1", "ULTRACEMCO", "INE481G01011", "19-Oct-2026 13:46:00"))
dl_tries = {"n": 0}
def failing_download(filing, client):
    dl_tries["n"] += 1
    return None
extract, calls = counting_extract({dl_f["attachment_url"]: good_fin})
fresh_state_dir(); reset_credits()
with TerminalCapture() as term:
    run_main(4, nse=lambda c: [dl_f], extract=extract, download=failing_download)
check("a failed download waits a minute instead of retrying every poll",
      dl_tries["n"] == 1 and pt.load_retries() == {"NSE:dl1": 1}, (dl_tries, pt.load_retries()))
check("…and the line says when", any("PDF download failed · retry 1/3 in 1 min" in l for l in term.lines()), term.lines())

def let_time_pass(state, poll):           # each poll: every waiting download is due again
    for fid in state.retry_after:
        state.retry_after[fid] = 0
    state.checker.todo.join()
    pt.drain_checks(state)
fresh_state_dir(); reset_credits()
dl_tries["n"] = 0
with TerminalCapture() as term:
    run_main(5, nse=lambda c: [dl_f], extract=extract, download=failing_download, on_wait=let_time_pass)
lines = [l for l in term.lines() if "PDF download failed" in l]
check("the waits grow: 1, 5, then 15 minutes, then it gives up",
      dl_tries["n"] == 4 and any("retry 1/3 in 1 min" in l for l in lines) and any("retry 2/3 in 5 min" in l for l in lines)
      and any("retry 3/3 in 15 min" in l for l in lines) and any("gave up after 4 attempts" in l for l in lines)
      and "NSE:dl1" in pt.load_seen(), lines)

fresh_state_dir(); reset_credits()
dl_tries["n"] = 0
def download_once_ok(filing, client):
    dl_tries["n"] += 1
    return None if dl_tries["n"] == 1 else b"%PDF-" + filing["attachment_url"].encode()
extract, calls = counting_extract({dl_f["attachment_url"]: good_fin})
run_main(3, nse=lambda c: [dl_f], extract=extract, download=download_once_ok, on_wait=let_time_pass)
check("a download that works on the retry is scored",
      dl_tries["n"] == 2 and calls == {dl_f["attachment_url"]: 1} and "NSE:dl1" in pt.load_seen(), (dl_tries, calls))

retry_other = pt.normalise_bse(bse_raw("ro1", 600031, "Result", "2026-10-19T13:50:00"))
extract, calls = counting_extract({retry_other["attachment_url"]: None})
fresh_state_dir(); reset_credits()
run_main(3, bse=lambda k: [retry_other], extract=extract)
check("other failures still retry every poll", calls == {retry_other["attachment_url"]: 3}, calls)

# A filing waiting in memory holds the saved checkpoint back to its day, so a restart
# fetches it again (16 Oct 2026: results held for credits from 10-15 Oct were lost
# when the next run started from 16 Oct)
from datetime import timedelta as _td
two_days_ago = (datetime.now() - _td(days=2)).replace(hour=11, minute=0, second=0, microsecond=0)
old_held = pt.normalise_bse(bse_raw("cr9", 600009, "Result", two_days_ago.isoformat()))
fresh_state_dir(); reset_credits()
model_calls["n"] = 0
with mock.patch.object(pt.client.chat.completions, "create", always_broke):
    run_main(1, bse=lambda k: [old_held], extract=pt.extract_financials, download=credit_download)
marks = pt.load_checkpoint()
check("checkpoint saved at the waiting filing's time, not the poll's",
      marks.get("BSE") == two_days_ago and marks.get("NSE", datetime.min).date() == date.today(), marks)

reset_credits()
fetch_since.clear()
with mock.patch.object(pt.client.chat.completions, "create", lambda **kw: Resp(MODEL_JSON)):
    run_main(1, bse=lambda k: [old_held], extract=pt.extract_financials, download=credit_download)
marks = pt.load_checkpoint()
check("the restart fetches BSE from that day again, and reads the filing",
      fetch_since[0] == ("BSE", two_days_ago.date()) and "BSE:cr9" in pt.load_seen(), (fetch_since[:2], pt.load_seen()))
check("once it's handled the checkpoint moves on", marks.get("BSE", datetime.min).date() == date.today(), marks)

# The same for a filing waiting for a retry
fresh_state_dir(); reset_credits()
retry_f = pt.normalise_bse(bse_raw("rt9", 600019, "Result", two_days_ago.isoformat()))
extract, calls = counting_extract({retry_f["attachment_url"]: None})
run_main(1, bse=lambda k: [retry_f], extract=extract)
check("a filing waiting for a retry holds the checkpoint back too",
      pt.load_checkpoint().get("BSE") == two_days_ago and pt.load_retries().get("BSE:rt9") == 1, pt.load_checkpoint())

# --from: catch up from a given day, past the 7-day limit; handled filings aren't read again
fresh_state_dir(); reset_credits()
nine_days_ago = date.today() - _td(days=9)
pt.save_checkpoint({"BSE": datetime.now() - _td(hours=1), "NSE": datetime.now() - _td(hours=1)})
done_f = pt.normalise_bse(bse_raw("fd1", 600021, "Result", (datetime.now() - _td(days=8)).isoformat(timespec="seconds")))
todo_f = pt.normalise_bse(bse_raw("fd2", 600022, "Result", (datetime.now() - _td(days=8)).isoformat(timespec="seconds")))
pt.save_seen({"BSE:fd1"})
fetch_since.clear()
extract, calls = counting_extract({todo_f["attachment_url"]: good_fin, done_f["attachment_url"]: good_fin})
with TerminalCapture() as term:
    run_main(1, bse=lambda k: [done_f, todo_f], extract=extract, argv=["--from", nine_days_ago.isoformat()])
check("--from fetches both exchanges from that day, past the 7-day limit",
      fetch_since[:2] == [("BSE", nine_days_ago), ("NSE", nine_days_ago)], fetch_since[:2])
check("--from reads only what wasn't handled yet", list(calls) == [todo_f["attachment_url"]], calls)
check("--from says so in the header", any("--from" in l for l in term.lines()), term.lines())
check("--from is for that run only: the checkpoint then moves on",
      pt.load_checkpoint().get("BSE", datetime.min).date() == date.today(), pt.load_checkpoint())

fresh_state_dir(); reset_credits()
model_calls["n"] = 0
vague_cr = pt.normalise_bse(bse_raw("cr2", 600002, "Board Meeting", "2026-07-20T11:00:00", sub="Outcome of Board Meeting held today"))
with mock.patch.object(pt.client.chat.completions, "create", always_broke):
    run_main(1, bse=lambda k: [vague_cr], extract=pt.extract_financials, download=credit_download)
check("a vague outcome checked in the background waits for credits too",
      model_calls["n"] == 1 and pt.load_retries() == {} and "BSE:cr2" not in pt.load_seen(), model_calls)
reset_credits()

# ── An empty answer: try the next-best table at once (Adani Green, 21 Oct 2026)
EMPTY_JSON = ('{"basis":"consolidated","unit":null,"period_end":null,"prev_period_end":null,"ly_period_end":null,'
              '"revenue_from_operations":[null,null,null],"pat":[null,null,null],"basic_eps":[null,null,null]}')

check("page_span", pt.page_span([9, 10]) == "10–11" and pt.page_span([3]) == "4")
cand_texts = ["\n".join(p) for p in PAGES]
check("candidates ranked: consolidated first, then standalone",
      pt.table_candidates(cand_texts) == [(3, "consolidated"), (1, "standalone")], pt.table_candidates(cand_texts))
for raw, want in [({}, False), ({"data": json.loads(EMPTY_JSON)}, True), ({"data": [1, 2]}, True),
                  ({"data": {"unit": None, "revenue_from_operations": [1, 2, 3]}}, False),
                  ({"data": {"pat": ["12.5", None, None]}}, False), ({"data": {"pat": "n/a"}}, True)]:
    check(f"answered_without_figures {raw}", pt.answered_without_figures(raw) == want)

def replies(*answers):
    """create() mock: the given answers in order (the last one repeats), recording what was sent."""
    queue, seen = list(answers), []
    def create(**kw):
        seen.append(sent_page_labels(kw["messages"][0]["content"]))
        return Resp(queue.pop(0) if len(queue) > 1 else queue[0])
    return create, seen

create, seen_pages = replies(EMPTY_JSON, MODEL_JSON)
raw = {}
with mock.patch.object(pt.client.chat.completions, "create", create), LogCapture() as logs:
    fin = pt.extract_financials(text_pdf(PAGES), raw_out=raw)
check("empty answer from the consolidated table → standalone table tried at once, and read",
      seen_pages == [["--- PAGE 4 ---", "--- PAGE 5 ---"], ["--- PAGE 2 ---", "--- PAGE 3 ---"]]
      and fin is not None and fin["revenue_from_operations"][0] == 662.58 and raw["tried"] == ["4–5", "2–3"], (seen_pages, raw))
check("the switch is logged (DEBUG)",
      logs.has_line("DEBUG", "no figures from pages 4–5 (consolidated table); trying the standalone table on pages 2–3"))

filing_q = {"exchange_dt": datetime(2026, 7, 20, 10, 0), "exchange": "BSE", "company": "Co"}
create, seen_pages = replies(EMPTY_JSON)
with mock.patch.object(pt.client.chat.completions, "create", create):
    try:
        pt._read_financials(text_pdf(PAGES), filing_q, False, {})
        check("every table empty → MISS naming the pages tried", False)
    except pt.SkipFiling as e:
        check("every table empty → MISS naming the pages tried",
              e.status == "MISS" and str(e) == "the model found no figures it could use (pages 4–5, 2–3 tried) · not retried"
              and len(seen_pages) == 2, (str(e), seen_pages))

FOUR_TABLES = [table_lines("Consolidated"), NOTES, table_lines("Standalone"), NOTES,
               table_lines(""), NOTES, ["XYZ LIMITED"] + table_lines("")[3:], NOTES]
create, seen_pages = replies(EMPTY_JSON)
raw = {}
with mock.patch.object(pt.client.chat.completions, "create", create):
    pt.extract_financials(text_pdf(FOUR_TABLES), raw_out=raw)
check("at most 2 extra tables: 3 calls although 4 tables exist",
      pt.MAX_EXTRA_TABLE_TRIES == 2 and len(seen_pages) == 3 and raw["tried"] == ["1–2", "3–4", "5–6"], (seen_pages, raw))

for label, answer in [("figures but no revenue (insurer-like)", '{"unit":"crores","pat":[5,4,3]}'),
                      ("reply isn't JSON (retried later instead)", "sorry, no idea")]:
    create, seen_pages = replies(answer)
    with mock.patch.object(pt.client.chat.completions, "create", create):
        pt.extract_financials(text_pdf(PAGES))
    check(f"no extra table when {label}", len(seen_pages) == 1, seen_pages)

create, seen_pages = replies(EMPTY_JSON)
with mock.patch.object(pt.client.chat.completions, "create", create):
    pt.extract_financials(text_pdf([table_lines("Consolidated"), ["XYZ LIMITED"] + table_lines("")[3:]]))
check("a table already sent as the continuation page isn't sent again", len(seen_pages) == 1, seen_pages)

# No table left: unread scans are OCR'd (no early stop) and looked at once more
found = {"pages": [0, 1], "basis": "consolidated", "reason": "consolidated table",
         "texts": ["\n".join(table_lines("Consolidated")), "", "", "notes " * 60, ""],
         "ocr": set(), "scans": {1, 2, 4}, "ocr_limit": 25}
ocr_calls = []
def fake_ocr(pdf_bytes, page_num, dpi=None):
    ocr_calls.append(page_num)
    return "\n".join(table_lines("Standalone")) if page_num == 3 else "scan noise"
with mock.patch.object(pt, "ocr_page", fake_ocr):
    nxt = pt.next_candidate(b"%PDF-x", found, {0, 1})
check("no table left → unread scans OCR'd until one turns up: page 3's standalone table is next, page 5 never read",
      nxt is not None and nxt["pages"] == [2, 3] and nxt["basis"] == "standalone" and nxt["ocr"] == {1, 2}
      and ocr_calls == [2, 3] and found["ocr"] == set(), (nxt and nxt["pages"], nxt and nxt["ocr"], ocr_calls))
with mock.patch.object(pt, "ocr_page", fake_ocr):
    check("nothing unread and no table left → None",
          pt.next_candidate(b"%PDF-x", {**found, "scans": set(), "texts": found["texts"][:1] + ["x" * 300] * 3}, {0, 1}) is None)
check("OCR limit respected: an unread scan past it isn't read",
      pt.next_candidate(b"%PDF-x", {**found, "ocr_limit": 1}, {0, 1}) is None)

# ── An untitled text "table" doesn't stop the scans being read (Automobile Corp of Goa, 21 Oct 2026):
#    text cover letter, scanned statement, then a text press release with Total income / PBT / EPS
PRESS_RELEASE = ["21 October 2026", "Automobile Corporation of Goa Limited (ACGL)",
                 "announced its financial results for Q2 FY27 on 21st October 2026",
                 "Key Financial for Q2 FY27 Highlights:", "Total income : Rs. 270.94 crore, up by 28.76% QoQ",
                 "Profit Before Tax : Rs. 17.74 crore down by 9.80% QoQ", "Earnings Per Share (EPS)* : Rs 21.68",
                 "In Rs. Crores Q2-FY27 Q1-FY27 Q2-FY26", "Total income 270.94 252.71 210.43", "EPS 21.68 18.42 24.03"] * 2
SCANNED_STATEMENT = ["ACGL", "STATEMENT OF AUDITED RESULTS FOR THE QUARTER AND SIX MONTHS ENDED 30 SEPTEMBER 2026",
                     "Rs. In Lakhs", "Particulars  30.09.2026  30.06.2026  30.09.2025",
                     "Revenue from operations  26559.38  24894.40  20638.15", "Total income  27093.82  25271.20  21042.62",
                     "Total expenses  25320.15  23752.26  19076.19", "Profit from ordinary activities before tax  1773.67  1518.94  1966.43",
                     "Profit for the period  1319.89  1121.63  1463.61", "Basic Earnings per share  21.68  18.42  24.03"]
doc = pdfium.PdfDocument.new()
doc.import_pages(pdfium.PdfDocument(text_pdf([COVER])))
doc.import_pages(pdfium.PdfDocument(scanned_pdf([SCANNED_STATEMENT])))
doc.import_pages(pdfium.PdfDocument(text_pdf([PRESS_RELEASE])))
buf = io.BytesIO()
doc.save(buf)
acgl_like = buf.getvalue()
check("press release alone counts as an untitled table", pt.classify_result_page("\n".join(PRESS_RELEASE)) == "untitled")
with LogCapture() as logs:
    found = pt.find_result_pages(acgl_like, pt.AMBIGUOUS_OCR_PAGES)
check("untitled text table → scans OCR'd first, the scanned statement is chosen",
      found["pages"][:1] == [1] and found["basis"] == "generic" and 1 in found["ocr"]
      and logs.has_line("DEBUG", "only an untitled table in the text layer, OCR-ing low-text pages first"),
      (found["pages"], found["basis"], sorted(found["ocr"])))
found = pt.find_result_pages(text_pdf([COVER, PRESS_RELEASE]))
check("untitled table with nothing unread is still used, no OCR", found["pages"] == [1] and found["basis"] == "untitled"
      and not found["ocr"], (found["pages"], found["basis"]))

# ─────────────────────────────────────────────────────────────
section("compare_models.py")

check("values_match tolerance", compare_models.values_match(100.0, 100.4) and not compare_models.values_match(100.0, 102.0))
check("values_match None/str", compare_models.values_match(None, None) and not compare_models.values_match(None, 1.0)
      and compare_models.values_match("lakhs", "lakhs"))

d = tempfile.mkdtemp(prefix="pead_cmp_")
pdf_path = os.path.join(d, "sample.pdf")
open(pdf_path, "wb").write(text_pdf(PAGES))
def per_model_create(**kw):
    if kw["model"] == "model-b":
        return Resp(MODEL_JSON.replace('"unit":"lakhs"', '"unit":"crores"'))
    return Resp(MODEL_JSON)
out = io.StringIO()
with mock.patch.object(pt.client.chat.completions, "create", per_model_create), mock.patch("sys.stdout", out):
    result = compare_models.compare_pdf(pdf_path, "model-a", "model-b")
report = out.getvalue()
check("compare flags unit + converted value differences", result["mismatches"] == 3, result)  # unit, revenue, pat
check("compare prints side by side", "model-a" in report and "model-b" in report and "<-- differs" in report)

cover_path = os.path.join(d, "cover.pdf")
open(cover_path, "wb").write(text_pdf([COVER]))
out = io.StringIO()
with mock.patch.object(pt.client.chat.completions, "create", side_effect=AssertionError("model called")), mock.patch("sys.stdout", out):
    result = compare_models.compare_pdf(cover_path, "model-a", "model-b")
check("compare skips PDFs without a results table", result["mismatches"] is None and "no results table found" in out.getvalue())

called = []
def one_model_create(**kw):
    called.append(kw["model"])
    return Resp(MODEL_JSON)
out = io.StringIO()
with mock.patch.object(pt.client.chat.completions, "create", one_model_create), mock.patch("sys.stdout", out):
    result = compare_models.compare_pdf(pdf_path, "model-a")
report = out.getvalue()
check("one model alone: only that model is called", called == ["model-a"], called)
check("one model alone: its values are printed",
      result["fin"]["revenue_from_operations"][0] == 662.58 and "662.58 / 600.00 / 500.00" in report and not result["failed"], report[-600:])
check("one model alone: the summary line shows the figures",
      compare_models.summary_status(result, False) == "read · consolidated · revenue 662.58 Cr · PAT 70.00 Cr · quarter ended 2026-06-30",
      compare_models.summary_status(result, False))
check("summary for a PDF without a table",
      compare_models.summary_status({"mismatches": None, "failed": [], "fin": None}, False) == "skipped: no results table found")

section("announcements shared with BASIS (additive)")
import sqlite3

def shared_rows():
    if not os.path.exists(pt.ANNOUNCEMENTS_DB):
        return []
    conn = sqlite3.connect(pt.ANNOUNCEMENTS_DB)
    try:
        return conn.execute(
            "SELECT id, exchange, company, code, isin, category, exchange_time FROM announcements ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

fresh_state_dir()
bse_a = pt.normalise_bse(bse_raw("a1", 541154, "Company Update", "2026-10-07T11:02:00", sub="Clarification"))
nse_a = pt.normalise_nse(nse_raw("n1", "PFOCUS", "INE367G01020", "07-Oct-2026 11:05:31", desc="News Verification"))
master = {"bse": {"541154": "INE066F01012"}, "nse": {}}
pt.publish_announcements([bse_a, nse_a], master)
pt.publish_announcements([bse_a], master)          # read again on a later poll
rows = shared_rows()
check("every announcement read is stored once", [r[0] for r in rows] == ["BSE:a1", "NSE:n1"], rows)
check("BSE rows get their ISIN from the scrip master", rows[0][4] == "INE066F01012", rows)
check("NSE rows keep the ISIN NSE sends", rows[1][4] == "INE367G01020", rows)
check("the exchange's time is kept", rows[0][6] == "2026-10-07T11:02:00", rows)

# A filing backfilled after a restart is written later but filed earlier: BASIS reads by
# seq (write order), so it must come after everything already written.
bse_late = pt.normalise_bse(bse_raw("a0", 541154, "Company Update", "2026-10-07T02:00:00"))
pt.publish_announcements([bse_late], master)
conn = sqlite3.connect(pt.ANNOUNCEMENTS_DB)
seqs = dict(conn.execute("SELECT id, seq FROM announcements").fetchall())
stamps = [r[0] for r in conn.execute("SELECT fetched_at FROM announcements").fetchall()]
conn.close()
check("rows are numbered in the order they were written, not filed",
      seqs["BSE:a0"] > seqs["NSE:n1"] > seqs["BSE:a1"], seqs)
check("fetched_at carries its UTC offset",
      all(datetime.fromisoformat(s).utcoffset() is not None for s in stamps), stamps)

odd = dict(bse_a, id="BSE:odd", exchange_dt="07-Oct-2026 11:02")   # not a datetime
good = pt.normalise_bse(bse_raw("a2", 541154, "Company Update", "2026-10-07T12:00:00"))
try:
    pt.publish_announcements([odd, good], master)
    check("an odd filing never stops a poll", True)
except Exception as e:
    check("an odd filing never stops a poll", False, e)
ids = [r[0] for r in shared_rows()]
check("an odd filing is skipped on its own", "BSE:a2" in ids and "BSE:odd" not in ids, ids)

pt.ANNOUNCEMENTS_DB = os.path.join(tempfile.mkdtemp(prefix="pead_ann_"), "no", "such", "dir", "a.db")
try:
    pt.publish_announcements([bse_a], master)
    check("an unwritable file never stops a poll", True)
except Exception as e:
    check("an unwritable file never stops a poll", False, e)

# A whole run: the poll stores what it read, and alerts are exactly what they were.
fresh_state_dir()
bse_r = pt.normalise_bse(bse_raw("r1", 500001, "Result", "2026-09-24T10:02:00"))
nse_r = pt.normalise_nse(nse_raw("r2", "XYZ", "INE000B01011", "24-Sep-2026 10:05:00"))
extract, calls = counting_extract({bse_r["attachment_url"]: good_fin, nse_r["attachment_url"]: good_fin})
alerts = run_main(1, bse=lambda k: [bse_r], nse=lambda c: [nse_r], extract=extract,
                  master={"bse": {"500001": "INE000A01011"}, "nse": {}})
check("a poll shares both exchanges' announcements", [r[0] for r in shared_rows()] == ["BSE:r1", "NSE:r2"], shared_rows())
fresh_state_dir()
extract, calls = counting_extract({bse_r["attachment_url"]: good_fin, nse_r["attachment_url"]: good_fin})
with mock.patch.object(pt, "publish_announcements", lambda filings, master: None):
    without = run_main(1, bse=lambda k: [bse_r], nse=lambda c: [nse_r], extract=extract,
                       master={"bse": {"500001": "INE000A01011"}, "nse": {}})
check("alerts are the same with and without sharing", alerts == without and alerts, (alerts, without))

# The write comes after the poll's alerts, so a slow one can never delay an alert.
fresh_state_dir()
events = []
extract, calls = counting_extract({bse_r["attachment_url"]: good_fin, nse_r["attachment_url"]: good_fin})
with mock.patch.object(pt, "publish_announcements",
                       lambda filings, master: events.append(("publish", [f["id"] for f in filings]))):
    run_main(1, bse=lambda k: [bse_r], nse=lambda c: [nse_r], extract=extract,
             master={"bse": {"500001": "INE000A01011"}, "nse": {}}, events=events)
check("announcements are shared only after the poll's alerts are sent",
      events == ["alert", "alert", ("publish", ["BSE:r1", "NSE:r2"])], events)

# The ISIN lookup misses one copy (17 Oct 2026: E2E Networks scored from NSE, then again
# from BSE): the company name catches it, in the same run and after a restart
section("BSE and NSE copies matched by company name")
check("company_key: the two exchanges' spellings match",
      all(pt.company_key(a) == pt.company_key(b) for a, b in [
          ("E2E Networks Limited", "E2E Networks Ltd"), ("The Federal Bank Limited", "Federal Bank Ltd"),
          ("Indo Rama Synthetics (India) Limited", "INDO RAMA SYNTHETICS (INDIA) LTD."), ("Modison Limited", "Modison Ltd-$"),
          ("Aditya Birla Fashion and Retail Limited", "Aditya Birla Fashion & Retail Ltd")]))
check("company_key: different companies stay apart",
      len({pt.company_key(n) for n in ("HDFC Bank Ltd", "HDFC Life Insurance Company Limited", "Tata Motors Ltd",
                                       "Tata Motors Finance Ltd")}) == 4 and pt.company_key("") == "")

def named(raw, name, field):
    raw[field] = name
    return raw
e2e_nse = pt.normalise_nse(named(nse_raw("e1", "E2E", "INE255Z01020", "24-Sep-2026 10:00:00"), "E2E Networks Limited", "sm_name"))
e2e_bse = pt.normalise_bse(named(bse_raw("e2", 544021, "Result", "2026-09-24T10:05:00"), "E2E Networks Ltd", "SLONGNAME"))

fresh_state_dir()
extract, calls = counting_extract({e2e_nse["attachment_url"]: good_fin, e2e_bse["attachment_url"]: good_fin})
with TerminalCapture() as term:
    alerts = run_main(1, bse=lambda k: [e2e_bse], nse=lambda c: [e2e_nse], extract=extract,
                      master={"bse": {}, "nse": {}})         # BSE code unknown → no ISIN for the BSE copy
rows = list(csv.DictReader(open(pt.RESULTS_CSV, encoding="utf-8")))
check("same run: BSE copy without an ISIN is skipped by name, not downloaded or scored",
      list(calls) == [e2e_nse["attachment_url"]] and alerts == ["NSE:E2E Networks Limited"] and len(rows) == 1,
      (calls, alerts, len(rows)))
check("…with a SKIP line that says why",
      any("already scored from the other exchange" in l for l in term.lines()), term.lines())

extract, calls = counting_extract({e2e_bse["attachment_url"]: good_fin})
alerts = run_main(1, bse=lambda k: [e2e_bse], extract=extract, master={"bse": {}, "nse": {}})
check("after a restart too (names come from the results file)",
      not calls and not alerts and len(list(csv.DictReader(open(pt.RESULTS_CSV, encoding="utf-8")))) == 1, (calls, alerts))

# The quarter printed in the PDF is checked as well (the filing-date guess can differ)
fresh_state_dir()
pt.initialize_csv()
key, status, text = pt.score_filing(e2e_bse, None, dict(good_fin), set(), {},
                                    scored_names={("e2e networks", "Q1FY27")})
check("score_filing: same name and PDF quarter → SKIP, no row",
      status == "SKIP" and "other exchange" in text and len(list(csv.DictReader(open(pt.RESULTS_CSV, encoding="utf-8")))) == 0,
      (status, text))

# Only scored rows close a company-quarter: a FLAG row or an UNKNOWN quarter leaves it open
fresh_state_dir()
pt.initialize_csv()
pt.save_result_csv(e2e_nse, 30, dict(good_fin), "Q1FY27")
pt.save_result_csv(pt.normalise_nse(named(nse_raw("f1", "FLG", "INE111A01011", "24-Sep-2026 10:00:00"), "Flagged Co Ltd", "sm_name")),
                   30, dict(good_fin, check="Q1: income doesn't add up"), "Q1FY27")
pt.save_result_csv(pt.normalise_nse(named(nse_raw("u1", "UNK", "INE222A01011", "24-Sep-2026 10:00:00"), "Unknown Co Ltd", "sm_name")),
                   30, dict(good_fin), "UNKNOWN")
check("scored names from the results file: FLAG and UNKNOWN rows left out",
      pt.scored_names_from_csv() == {("e2e networks", "Q1FY27")}, pt.scored_names_from_csv())


section("Claude Haiku 5.5 (thinking models)")
from types import SimpleNamespace

class Resp5:
    """A reply as a thinking model sends it: finish_reason and token usage."""
    def __init__(self, content, finish="stop", usage=None):
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)]
        self.usage = usage

def usage(tokens_in, tokens_out, thinking):
    return SimpleNamespace(prompt_tokens=tokens_in, completion_tokens=tokens_out,
                           completion_tokens_details=SimpleNamespace(reasoning_tokens=thinking))

check("the scanner uses Haiku 5.5", pt.EXTRACTION_MODEL == "anthropic/claude-haiku-5.5", pt.EXTRACTION_MODEL)
import subprocess
probe = subprocess.run(
    [sys.executable, "-c", "import pead_tool as pt; print(pt.EXTRACTION_MODEL)"],
    cwd=os.path.dirname(os.path.abspath(__file__)), capture_output=True, text=True, timeout=120,
    env={**os.environ, "EXTRACTION_MODEL": "anthropic/claude-haiku-4-5"},
)
fresh_state_dir()
with mock.patch.dict(os.environ, {"EXTRACTION_MODEL": "anthropic/claude-haiku-4-5"}), TerminalCapture() as term:
    run_main(1)
check("an EXTRACTION_MODEL line in .env gets a startup warning",
      any("EXTRACTION_MODEL in .env is ignored" in l for l in term.lines()), term.lines())
fresh_state_dir()
with TerminalCapture() as term:
    run_main(1)
check("no warning without one", not any("EXTRACTION_MODEL" in l for l in term.lines()), term.lines())
check("an EXTRACTION_MODEL in the environment doesn't switch it to 4.5",
      probe.stdout.strip().splitlines()[-1:] == ["anthropic/claude-haiku-5.5"], (probe.stdout[-200:], probe.stderr[-300:]))

legacy = {"max_tokens": pt.LEGACY_MAX_TOKENS, "temperature": 0}
saved_effort = pt.EXTRACTION_EFFORT
pt.EXTRACTION_EFFORT = None
check("Claude 3/4 keep temperature 0 and 1500 tokens",
      all(pt.model_call_params(m) == legacy for m in
          ("anthropic/claude-haiku-4-5", "anthropic/claude-haiku-4.5", "anthropic/claude-sonnet-4",
           "anthropic/claude-3-5-haiku", "claude-3.7-sonnet", "anthropic/claude-opus-4-1")))
check("Claude 5.x: no temperature, room to think",
      all(pt.model_call_params(m) == {"max_tokens": pt.THINKING_MAX_TOKENS} for m in
          ("anthropic/claude-haiku-5.5", "anthropic/claude-haiku-5-5", "anthropic/claude-sonnet-5.5")))
check("other models get the thinking settings", pt.model_call_params("google/gemini-2.5-flash") == {"max_tokens": pt.THINKING_MAX_TOKENS})
pt.EXTRACTION_EFFORT = "low"
check("EXTRACTION_EFFORT goes to Claude 5.x only",
      pt.model_call_params("anthropic/claude-haiku-5.5").get("extra_body") == {"reasoning_effort": "low"}
      and pt.model_call_params("anthropic/claude-haiku-4-5") == legacy)
pt.EXTRACTION_EFFORT = None

sent = {}
def record_create(**kw):
    sent[kw["model"]] = kw
    return Resp(MODEL_JSON)
with mock.patch.object(pt.client.chat.completions, "create", record_create):
    pt.extract_from_text("PAGE TEXT", "anthropic/claude-haiku-5.5")
    pt.extract_from_text("PAGE TEXT", "anthropic/claude-haiku-4-5")
five, four = sent.get("anthropic/claude-haiku-5.5", {}), sent.get("anthropic/claude-haiku-4-5", {})
check("the 5.5 call sends no temperature", "temperature" not in five and five.get("max_tokens") == pt.THINKING_MAX_TOKENS, five.keys())
check("the 4.5 call is unchanged", four.get("temperature") == 0 and four.get("max_tokens") == 1500, four.keys())
check("no effort setting is sent unless asked", "extra_body" not in five)

# What comes back: thinking in the text, thinking blocks, notes around the JSON
FINAL = '{"basis":"standalone","unit":"crores","revenue_from_operations":[1,2,3],"pat":[4,5,6]}'
DRAFT = '{"basis":"consolidated","unit":"lakhs","revenue_from_operations":[9,9,9],"pat":[9,9,9]}'
check("<think> text is dropped",
      pt.parse_answer(pt.answer_text(SimpleNamespace(content=f"<think>Column 1 is {DRAFT}?</think>\n{FINAL}")))["basis"] == "standalone")
check("thinking blocks in a content list are dropped",
      pt.parse_answer(pt.answer_text(SimpleNamespace(content=[
          {"type": "thinking", "thinking": f"maybe {DRAFT}"},
          {"type": "text", "text": FINAL},
      ])))["unit"] == "crores")
check("an unfinished <think> leaves no answer", pt.answer_text(SimpleNamespace(content=f"<think>looking at {DRAFT}")) == "")
check("notes around the JSON: the last object with figures wins",
      pt.parse_answer(f"First guess: {DRAFT}. Checking the header again. {{\"note\": 1}} Final: {FINAL} done")["basis"] == "standalone")
check("a fenced JSON answer still parses", pt.parse_answer(f"```json\n{FINAL}\n```")["pat"] == [4, 5, 6])
check("a single object without figures is still the answer (→ MISS, as before)",
      pt.parse_answer('Sorry: {"note": "no table"}') == {"note": "no table"})
for label, text in [("an empty answer", ""), ("objects without figures", 'Saw {"page": 4} and {"page": 5}, no table')]:
    try:
        pt.parse_answer(text)
        check(f"{label} is a parse error", False)
    except json.JSONDecodeError:
        check(f"{label} is a parse error", True)

THINKING_REPLY = f"<think>The consolidated table is on page 5. {DRAFT}</think>\n" + MODEL_JSON
with mock.patch.object(pt.client.chat.completions, "create", lambda **kw: Resp5(THINKING_REPLY)):
    fin = pt.extract_from_text("PAGE TEXT", "anthropic/claude-haiku-5.5")
check("a reply with thinking still extracts the figures",
      fin is not None and fin["revenue_from_operations"][0] == 662.58 and fin["basis"] == "consolidated", fin)

# The token limit ended the reply before the JSON: a retry, never a MISS
fresh_state_dir()
raw_out = {}
with mock.patch.object(pt.client.chat.completions, "create",
                       lambda **kw: Resp5("<think>Reading every line of the table", finish="length")):
    fin = pt.extract_from_text("PAGE TEXT", "anthropic/claude-haiku-5.5", raw_out=raw_out)
check("cut off by the token limit → None, marked cut off, not answered",
      fin is None and raw_out.get("cut_off") and "data" not in raw_out, raw_out)
with mock.patch.object(pt.client.chat.completions, "create", lambda **kw: Resp5("", finish="length")):
    try:
        pt._read_financials(text_pdf(PAGES), {"exchange_dt": datetime(2026, 7, 20, 15, 0)}, False, {})
        check("a cut-off reply is retried, with the reason", False)
    except pt.RetryFiling as e:
        check("a cut-off reply is retried, with the reason", "ran out of tokens" in str(e), e)
    except pt.SkipFiling as e:
        check("a cut-off reply is retried, with the reason", False, f"skipped: {e}")
with mock.patch.object(pt.client.chat.completions, "create", lambda **kw: Resp5(MODEL_JSON, finish="length")):
    fin = pt.extract_from_text("PAGE TEXT", "anthropic/claude-haiku-5.5")
check("a complete JSON is used even when the limit was reached", fin is not None and fin["pat"][0] == 70.0, fin)

# Token counts, for checking the cost, and which model AICredits says answered
pt.usage_totals.clear()
pt.answered_by.clear()
def served_reply(**kw):
    reply = Resp5(MODEL_JSON, usage=usage(5200, 1400, 900))
    reply.model = "claude-haiku-5-5-20261007"
    return reply
with mock.patch.object(pt.client.chat.completions, "create", served_reply):
    pt.extract_from_text("PAGE TEXT", "anthropic/claude-haiku-5.5")
    pt.extract_from_text("PAGE TEXT", "anthropic/claude-haiku-5.5")
check("the answering model's name is kept",
      pt.answered_by == {"anthropic/claude-haiku-5.5": {"claude-haiku-5-5-20261007"}}, pt.answered_by)
check("token usage adds up per model",
      pt.usage_totals.get("anthropic/claude-haiku-5.5") == {"calls": 2, "in": 10400, "out": 2800, "thinking": 1800},
      pt.usage_totals)
line = compare_models.usage_line("anthropic/claude-haiku-5.5")
check("compare_models prices the tokens", "2 calls" in line and "$0.0024" in line and "1,800 thinking" in line, line)
check("compare_models shows who answered", line.endswith("answered by (as AICredits reports it): claude-haiku-5-5-20261007"), line)
check("compare_models without a price shows tokens only",
      compare_models.usage_line("x/unknown") == "  x/unknown: no token counts reported")
pt.usage_totals.clear()
pt.answered_by.clear()
pt.EXTRACTION_EFFORT = saved_effort

# ─────────────────────────────────────────────────────────────
section("half-yearly (SME) results (Poojaa Precision, 22 Oct 2026)")

saved_cutoff = pt.SCORE_FROM_QUARTER
pt.SCORE_FROM_QUARTER = CONFIGURED_SCORE_FROM_QUARTER      # Q2FY27, the real cutoff

for d, h in [(date(2026, 9, 30), "H1FY27"), (date(2027, 3, 31), "H2FY27"), (date(2026, 3, 31), "H2FY26"),
             (date(2026, 6, 30), "H1FY27"), (date(2026, 12, 31), "H2FY27")]:
    check(f"half_label {d}", pt.half_label(d) == h, pt.half_label(d))
for q, h in [("Q2FY27", "H1FY27"), ("Q4FY26", "H2FY26"), ("Q1FY27", None), ("Q3FY27", None)]:
    check(f"half_ending {q}", pt.half_ending(q) == h, pt.half_ending(q))
check("a half year sorts with the quarter it ends with",
      pt.quarter_index("H1FY27") == pt.quarter_index("Q2FY27") and pt.quarter_index("H2FY26") == pt.quarter_index("Q4FY26")
      and pt.quarter_index("H2FY26") < pt.quarter_index("Q1FY27") < pt.quarter_index("H1FY27") < pt.quarter_index("H2FY27"))
try:
    pt.quarter_index("H3FY27")
    check("H3 is not a half year", False)
except ValueError:
    check("H3 is not a half year", True)

check("prompt explains half-yearly tables and asks for period_months",
      "HALF-YEARLY RESULTS" in pt.EXTRACTION_PROMPT and '"period_months":3' in pt.EXTRACTION_PROMPT)
for value, want in [(6, 6), ("6", 6), (6.0, 6), (3, 3), (None, 3), ("half", 3), (12, 3)]:
    got = pt.normalise_financials({"unit": "lakhs", "period_months": value})["period_months"]
    check(f"period_months {value!r} → {want}", got == want, got)

# Poojaa's table: half years to Sep 2026, Mar 2026 and Sep 2025, in lakhs
HALF = dict(unit="lakhs", basis="standalone", period_months=6,
            period_end="30.09.2026", prev_period_end="31.03.2026", ly_period_end="30.09.2025",
            revenue_from_operations=[1500, 1200, 1000], total_income=[1510, 1210, 1005],
            total_expenses=[1100, 950, 860], pbt=[410, 260, 145], pat=[300, 190, 100],
            finance_cost=[20, 20, 20], depreciation=[50, 50, 50], basic_eps=[3, 1.9, 1])
half_fin = pt.normalise_financials(HALF)
check("half year: columns 6 and 12 months apart line up", pt.column_dates_problem(half_fin) is None,
      pt.column_dates_problem(half_fin))
check("half year: quarter-spaced columns don't",
      pt.column_dates_problem({**half_fin, "prev_period_end": "2026-06-30"})
      == "previous column 2026-06-30 isn't 6 months before 2026-09-30")
check("half year: a missing previous column is named",
      pt.column_dates_problem({**half_fin, "prev_period_end": None}) == "previous-half-year column date missing")
check("quarterly results still need 3 months",
      pt.column_dates_problem({**half_fin, "period_months": 3})
      == "previous column 2026-03-31 isn't 3 months before 2026-09-30")
check("pdf_quarter labels a half year", pt.pdf_quarter(half_fin, date(2026, 10, 22)) == "H1FY27")
problem = pt.numbers_problem({**half_fin, "pbt": [0.5, 2.6, 1.45]})
check("sanity check names the half year", (problem or "").startswith("this half year:"), problem)

s_half, bd_half = pt.compute_pead_score(half_fin)
check("half year scored like a quarter, momentum half on half",
      "Revenue HoH" in bd_half and "Revenue QoQ" not in bd_half and bd_half["Revenue HoH"][0] == "25.0%", bd_half)

tagged = pt.short_company("Tinna Rubber and Infrastructure Limited [SME]")
check("[SME] kept when a long name is cut", len(tagged) == pt.COMPANY_WIDTH and tagged.endswith("… [SME]"), tagged)
check("short name: Ltd dropped, [SME] kept",
      pt.short_company("Poojaa Precision Engg. Ltd [SME]") == "Poojaa Precision Engg. [SME]".ljust(pt.COMPANY_WIDTH))
check("no tag, cut as before", pt.short_company("Tinna Rubber and Infrastructure Limited") == "Tinna Rubber and Infrastructu…")

sme_filing = pt.normalise_bse(bse_raw("p1", 544844, "Result", "2026-10-22T14:57:17"))
with mock.patch.object(pt.requests, "post", fake_post):
    pt.send_telegram(sme_filing, s_half, bd_half, half_fin, "H1FY27", "22 Oct 2026  14:58:00", "43s")
t = sent["text"]
check("alert: [SME] after the name, half-yearly table",
      "<b>Co 544844 [SME]</b>" in t and "HALF-YEARLY FINANCIALS" in t and "HoH%" in t
      and "Revenue HoH" in t and "H1FY27 (to 2026-09-30)" in t and "QoQ" not in t, t)

# The scanner end to end: scored under its own key, then a later copy skipped before download
fresh_state_dir()
again = pt.normalise_bse(bse_raw("p2", 544844, "Result", "2026-10-23T11:00:00"))
extract, calls = counting_extract({sme_filing["attachment_url"]: half_fin, again["attachment_url"]: half_fin})
sme_master = {"bse": {"544844": "INE288301026"}, "nse": {}}
with LogCapture() as cap:
    run_main(1, bse=lambda k: [sme_filing], extract=extract, master=sme_master)
processed = set(json.load(open(pt.PROCESSED_SCRIPS_FILE)))
row = list(csv.DictReader(open(pt.RESULTS_CSV, encoding="utf-8")))[-1]
check("half-yearly result scored under its own key", processed == {"INE288301026_H1FY27"}, processed)
check("CSV row: quarter H1FY27, the previous half year in the pq columns, name untagged",
      row["quarter"] == "H1FY27" and row["revenue_pq"] == "12.0" and row["period_end"] == "2026-09-30"
      and row["company"] == "Co 544844", row)
check("its line carries [SME] and H1FY27",
      any(st in ("SCORE", "ALERT") and "Co 544844 [SME]" in m and "H1FY27" in m for st, _, m in cap.records), cap.records)
check("scored_names_from_csv keeps half years", ("co 544844", "H1FY27") in pt.scored_names_from_csv())

with LogCapture() as cap:
    run_main(1, bse=lambda k: [again], extract=extract, master=sme_master)      # restart
check("a later copy is skipped before download by the half-year key",
      again["attachment_url"] not in calls and cap.has_line("SKIP", "INE288301026_H1FY27 already scored"),
      (calls, cap.records))

old_half = pt.normalise_financials({**HALF, "period_end": "31.03.2026", "prev_period_end": "30.09.2025",
                                    "ly_period_end": "31.03.2025"})
try:
    pt.score_filing(pt.normalise_bse(bse_raw("p3", 544844, "Result", "2026-05-20T10:00:00")),
                    "INE288301026", old_half, set(), {})
    check("a half year before SCORE_FROM_QUARTER is old", False)
except pt.SkipFiling as e:
    check("a half year before SCORE_FROM_QUARTER is old", e.status == "OLD" and str(e).startswith("H2FY26: old quarter"), e)

check("half-year fallback keys move to the ISIN too",
      pt.migrate_processed_keys({"BSE-544844_H1FY27"}, sme_master) == {"INE288301026_H1FY27"})
check("terminal shortens a half-year duplicate",
      pt.console_text("SKIP", "INE288301026_H1FY27 already scored") == "already scored this quarter")
for dash_row, want in [({"quarter": "H1FY27", "period_end": "2026-09-30"}, ("Q2FY27", "csv")),
                       ({"quarter": "h2fy26"}, ("Q4FY26", "csv"))]:
    check(f"dashboard: half year {dash_row['quarter']} sits with {want[0]}",
          dashboard.row_quarter(dash_row) == want, dashboard.row_quarter(dash_row))

pt.SCORE_FROM_QUARTER = saved_cutoff


print(f"\nFAILURES: {fails}")
sys.exit(1 if fails else 0)
