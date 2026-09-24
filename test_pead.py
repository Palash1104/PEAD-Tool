"""
Offline tests for pead_tool.py — no network, no real Telegram / AICredits.

  python test_pead.py

Network, the model and Telegram are mocked; state files go to a temp folder.
The PDF tests use the real pdfplumber, Poppler and Tesseract (~30s of OCR).
"""
import csv
import io
import json
import logging
import os
import sys
import tempfile
import time
from datetime import date, datetime
from unittest import mock

# Dummy credentials so a missed mock can never reach the real bot or API
for var in ["AICREDITS_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]:
    os.environ[var] = "test"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pead_tool as pt
import compare_models

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
    pt._last_blocked_alert.clear()
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
    """Collects pead_tool log messages inside a `with` block."""
    def __enter__(self):
        self.messages = []
        pt.log.addHandler(self)
        return self
    def __exit__(self, *exc):
        pt.log.removeHandler(self)
    def emit(self, record):
        self.messages.append(record.getMessage())
    def has(self, text):
        return any(text in m for m in self.messages)

ACCESS_DENIED = FakeResponse(403, text="<HTML><TITLE>Access Denied</TITLE>You don't have permission</HTML>")

def mk(**kw):
    d = {"unit": "crores"}
    d.update(kw)
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
check("filing_quarter uses period_end (late Q4 filer)", pt.filing_quarter({"period_end": "2026-03-31"}, filed) == "Q4FY26")
check("filing_quarter falls back without period_end", pt.filing_quarter({"period_end": None}, filed) == "Q1FY27")
check("filing_quarter ignores implausible period_end", pt.filing_quarter({"period_end": "2024-06-30"}, filed) == "Q1FY27")
check("filing_quarter ignores future period_end", pt.filing_quarter({"period_end": "2026-09-30"}, filed) == "Q1FY27")

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
with mock.patch.object(pt.requests, "get", flaky), mock.patch.object(pt.time, "sleep"):
    got, pages = pt.fetch_bse_filings(set())
check("later page failing keeps earlier pages", [f["id"] for f in got] == ["BSE:n101"] and pages == 1, (got, pages))

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

# ─────────────────────────────────────────────────────────────
section("normalise_financials")

raw = {"basis": "Consolidated", "unit": "Lakhs", "period_end": "30.06.2026",
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

d = fresh_state_dir()
with open(pt.RESULTS_CSV, "w", newline="", encoding="utf-8") as fh:
    w = csv.writer(fh)
    w.writerow(pt.CSV_HEADER[:-1])
    w.writerow(["2026-05-27 23:40:34", "Old Co", "540026", "0.0"] + [""] * 11)
pt.initialize_csv()
pt.save_result_csv(nse_filing, 40.0, mk(**GOOD))
rows = list(csv.reader(open(pt.RESULTS_CSV, encoding="utf-8")))
check("legacy CSV gains exchange column", rows[0][-1] == "exchange" and rows[1][-1] == "BSE", rows[1])
check("new row records exchange", rows[2][-1] == "NSE" and len(rows[2]) == len(pt.CSV_HEADER), rows[2])
pt.initialize_csv()
check("migration is idempotent", list(csv.reader(open(pt.RESULTS_CSV, encoding="utf-8")))[0].count("exchange") == 1)

# ─────────────────────────────────────────────────────────────
section("main loop")

class Stop(Exception):
    pass

def run_main(polls, bse=lambda known: [], nse=lambda client: [], extract=None, master=None, download=None):
    """Run pt.main() for a number of polls with everything external mocked."""
    counter = {"n": 0}
    def fake_sleep(sec):
        counter["n"] += 1
        if counter["n"] >= polls:
            raise Stop()
    alerts = []
    with mock.patch.object(pt, "fetch_bse_filings", lambda known: (bse(known), 1)), \
         mock.patch.object(pt, "fetch_nse_filings", nse), \
         mock.patch.object(pt, "fetch_bse_master", lambda: dict((master or {}).get("bse", {}))), \
         mock.patch.object(pt, "fetch_nse_master", lambda client: dict((master or {}).get("nse", {}))), \
         mock.patch.object(pt, "download_pdf", download or (lambda filing, client: b"%PDF-" + filing["attachment_url"].encode())), \
         mock.patch.object(pt, "extract_financials", extract), \
         mock.patch.object(pt, "send_telegram", lambda filing, *a: alerts.append(filing["exchange"] + ":" + filing["company"])), \
         mock.patch.object(pt, "send_telegram_text", lambda text: True), \
         mock.patch.object(pt.time, "sleep", fake_sleep):
        try:
            pt.main([])
        except Stop:
            pass
    return alerts

ambiguous_calls = []

def counting_extract(behaviour):
    calls = {}
    def extract(pdf, ambiguous=False):
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
behaviour = {failing["attachment_url"]: mk(pat=[1, 1, 1]),            # no revenue → failure
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
extract, calls = counting_extract({no_table["attachment_url"]: pt.SkipFiling("no results table found")})
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
    gacm["attachment_url"]: pt.SkipFiling("no results table found"),        # like the real PDF
    saraswati["attachment_url"]: pt.SkipFiling("no results table found"),   # like the real PDF
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
check("log says ambiguous outcome is being checked", logs.has("ambiguous outcome, checking PDF for a results table"))
check("skip log names the reason", logs.has("→ Skip BSE board meeting (intimation): Alfa Ica India Ltd"))

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
    run_main(2, nse=lambda c: [vague_nse], extract=lambda pdf, ambiguous=False: None)
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
with mock.patch.object(pt, "warn_blocked", lambda ex, detail: warned.append(ex)):
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
    try:
        font = ImageFont.truetype("arial.ttf", 28)
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
              '"revenue_from_operations":[66258,60000,50000],"pat":[7000,6000,4000],"basic_eps":[7.0,6.0,4.0]}\n```')

class Msg:
    def __init__(self, content):
        self.content = content

class Resp:
    def __init__(self, content):
        self.choices = [type("Choice", (), {"message": Msg(content)})()]

sent_requests = []
def fake_create(**kw):
    sent_requests.append(kw)
    return Resp(MODEL_JSON)

with mock.patch.object(pt.client.chat.completions, "create", fake_create):
    for label, pdf, expect_pages in [
        ("text PDF", text_pdf(PAGES), ["--- PAGE 4 ---", "--- PAGE 5 ---"]),
        ("scanned PDF", scanned_pdf(PAGES), ["--- PAGE 4 ---", "--- PAGE 5 ---"]),
        ("single-page scanned", scanned_pdf([table_lines("Standalone")]), ["--- PAGE 1 ---"]),
        ("hybrid: text cover + scanned tables", hybrid_pdf(), ["--- PAGE 4 ---", "--- PAGE 5 ---"]),
    ]:
        sent_requests.clear()
        started = time.time()
        fin = pt.extract_financials(pdf)
        prompt = sent_requests[0]["messages"][0]["content"] if sent_requests else ""
        pdf_text = prompt.split("PDF TEXT:")[-1]
        sent_pages = [l for l in pdf_text.splitlines() if l.startswith("--- PAGE")]
        ok = (fin is not None and fin["revenue_from_operations"][0] == 662.58 and fin["period_end"] == "2026-06-30"
              and sent_requests[0]["max_tokens"] == 1500 and sent_requests[0]["model"] == pt.EXTRACTION_MODEL
              and sent_pages == expect_pages)
        if len(expect_pages) == 2:
            ok = ok and "consolidated" in pdf_text.split("--- PAGE 5")[0].lower()
        check(f"{label} ({time.time() - started:.0f}s)", ok, sent_pages)

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
    check("ambiguous with table: logged found, model called", logs.has("Ambiguous outcome → results table found") and len(sent_requests) == 1)

    sent_requests.clear()
    with LogCapture() as logs:
        try:
            pt.extract_financials(text_pdf([COVER, NOTES]), ambiguous=True)
            skipped = False
        except pt.SkipFiling:
            skipped = True
    check("ambiguous without table: logged not found, skipped, no model call",
          skipped and logs.has("Ambiguous outcome → results table not found") and not sent_requests)

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

print(f"\nFAILURES: {fails}")
sys.exit(1 if fails else 0)
