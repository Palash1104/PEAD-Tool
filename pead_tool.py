"""
PEAD Score Tool — BSE Financial Results Monitor
================================================
Polls BSE every 30s → downloads PDF → Claude Haiku extracts financials
→ PEAD score computed → Telegram alert if score >= 30

SETUP:
  pip install requests openai pdfplumber

CONFIG:
  Fill in AICREDITS_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID below.
  Then run:  python pead_tool.py
"""
import os
from dotenv import load_dotenv
import json
import re
import html
import math
import pytesseract
from pdf2image import convert_from_bytes
import csv
import time
import logging
import requests
import pdfplumber
import io
from datetime import datetime, date
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

if not all([
    AICREDITS_API_KEY,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
]):
    raise ValueError("Missing environment variables in .env file")

PEAD_THRESHOLD     = 35
POLL_INTERVAL_SEC  = 30
CLAUDE_MODEL       = "anthropic/claude-haiku-4-5"
MAX_RETRIES        = 3      # extra attempts per failed filing, one per poll

# Growth factors are skipped when last year's base is this small (Rs. Cr)
SMALL_BASE_PAT_CR  = 1
SMALL_BASE_REV_CR  = 10

SEEN_FILE = "seen.json"
RESULTS_CSV = "pead_results.csv"
# ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

client = OpenAI(
    api_key=AICREDITS_API_KEY,
    base_url="https://api.aicredits.in/v1",
)

def load_seen() -> set:
    try:
        if os.path.exists(SEEN_FILE):
            with open(SEEN_FILE, "r") as f:
                data = json.load(f)
                return set(data)
    except Exception as e:
        log.warning(f"Could not load seen file: {e}")

    return set()


def save_seen(seen: set):
    try:
        with open(SEEN_FILE, "w") as f:
            json.dump(list(seen), f)
    except Exception as e:
        log.warning(f"Could not save seen file: {e}")

def initialize_csv():

    if os.path.exists(RESULTS_CSV):
        return

    with open(RESULTS_CSV, "w", newline="", encoding="utf-8") as f:

        writer = csv.writer(f)

        writer.writerow([
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
        ])

def save_result_csv(company, scrip, score, fin):

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

                company,
                scrip,
                score,

                *rev,
                *pat,
                *ebitda,

                eps[0],
                eps[2],
            ])
    except OSError as e:
        # e.g. CSV open in Excel — don't let logging block the alert
        log.warning(f"   Could not write CSV row: {e}")

# ── BSE ──────────────────────────────────────────────────────

BSE_ALL_ANN_URL = (
    "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
    "?pageno=1&strCat=-1&strPrevDate={date}"
    "&strScrip=&strSearch=P&strToDate={date}"
    "&strType=C&subcategory=-1"
)
BSE_PDF_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://www.bseindia.com/",
}

def fetch_all_announcements() -> list:

    today = date.today().strftime("%Y%m%d")

    try:

        r = requests.get(
            BSE_ALL_ANN_URL.format(date=today),
            headers=HEADERS,
            timeout=15
        )

        r.raise_for_status()

        return r.json().get("Table", [])

    except Exception as e:

        log.warning(
            f"All announcements fetch failed: {e}"
        )

        return []

def download_pdf(attachment_name: str):
    try:
        r = requests.get(BSE_PDF_BASE + attachment_name, headers=HEADERS, timeout=20)
        r.raise_for_status()
        return r.content
    except Exception as e:
        log.warning(f"PDF download failed: {e}")
        return None

def parse_exchange_time(ann: dict) -> datetime | None:
    for field in ["EXCHANGE_RECEIVED_TIME", "NEWS_DT", "DT_TM", "DTTM"]:
        val = ann.get(field)
        if not val:
            continue
        s = str(val).strip()
        for fmt in [
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%d/%m/%Y %H:%M:%S",
            "%Y%m%d%H%M%S",
            "%d-%m-%Y %H:%M:%S",
        ]:
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
    return None

def announcement_headline(ann: dict) -> str:
    return " ".join(
        str(ann.get(field) or "")
        for field in ["NEWSSUB", "HEADLINE", "SUBCATNAME"]
    ).strip()

def is_result_board_meeting(headline: str) -> bool:
    """Board Meeting filings only count when they are an outcome with results.

    Intimations, trading window, AGM and dividend notices are dropped unless
    they also mention results, which the result-keyword requirement already
    enforces ("Intimation of outcome ... financial results" is a real result).
    """
    text = headline.lower()
    return "outcome" in text and re.search(r"\bresults?\b", text) is not None

def reporting_quarter(filed_on: date) -> str:
    """Quarter a result filed on this date reports, e.g. Sep 2026 → Q1FY27.

    Results are filed in the three months after quarter end, and the
    Indian financial year runs April–March (FY27 = Apr 2026 – Mar 2027).
    """
    m, y = filed_on.month, filed_on.year
    if m in (7, 8, 9):
        q, fy = 1, y + 1
    elif m in (10, 11, 12):
        q, fy = 2, y + 1
    elif m in (1, 2, 3):
        q, fy = 3, y
    else:
        q, fy = 4, y
    return f"Q{q}FY{fy % 100:02d}"

# ── CLAUDE PDF EXTRACTION ─────────────────────────────────────

CLAUDE_PROMPT = """This text comes from a quarterly financial result PDF filed by an Indian listed company on BSE/NSE.

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

Return ONLY a JSON object, no explanation, no markdown:
{
"basis":"consolidated|standalone",
"unit":"crores|lakhs|millions|thousands|rupees",
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

MAX_PDF_PAGES  = 25
MIN_TEXT_CHARS = 500   # less real text than this → treat PDF as scanned, OCR it
HEADING_LINES  = 20    # a result table's title sits near the top of its page

# A page counts as a result table only if it has at least two of these
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
        r"|consolidated\s+financial\s+results|consolidated\s+statement"
    )),
    ("standalone", re.compile(
        r"(?:un)?audited\s+standalone|standalone\s+(?:un)?audited"
        r"|standalone\s+financial\s+results|standalone\s+statement"
    )),
    ("generic", re.compile(
        r"financial\s+results\s+for\s+the\s+(?:quarter|half\s+year|period)"
        r"|(?:un)?audited\s+financial\s+results"
    )),
]

PAGE_SCORE_KEYWORDS = {
    "financial results": 5,
    "quarter ended": 4,
    "half year": 4,
    "year ended": 4,
    "revenue from operations": 5,
    "earnings per share": 5,
    "profit before tax": 4,
    "profit after tax": 4,
    "total income": 3,
    "standalone": 3,
    "consolidated": 3,
}

def classify_result_page(text: str) -> str | None:
    lower = text.lower()

    if sum(marker in lower for marker in TABLE_MARKERS) < 2:
        return None

    heading = " ".join(lower.splitlines()[:HEADING_LINES])

    for basis, pattern in RESULT_HEADINGS:
        if pattern.search(heading):
            return basis

    return None

def select_result_pages(page_texts: list) -> tuple:
    """Pick the result table page plus its continuation page.

    Prefers consolidated, then standalone, then an untitled result table,
    then the best keyword-scoring page. Returns (page indices, reason).
    """
    first_found = {}

    for idx, text in enumerate(page_texts):
        basis = classify_result_page(text)
        if basis and basis not in first_found:
            first_found[basis] = idx

    for basis in ["consolidated", "standalone", "generic"]:
        if basis in first_found:
            idx = first_found[basis]
            return [i for i in (idx, idx + 1) if i < len(page_texts)], f"{basis} table"

    scores = [
        sum(w for kw, w in PAGE_SCORE_KEYWORDS.items() if kw in text.lower())
        for text in page_texts
    ]

    if not scores or max(scores) == 0:
        return [], "no result page"

    idx = scores.index(max(scores))
    return [i for i in (idx, idx + 1) if i < len(page_texts)], f"best keyword score {max(scores)}"

def extract_pages_ocr(pdf_bytes: bytes, page_count: int) -> list:
    """OCR one page at a time, stopping once a consolidated table and its next page are read."""
    log.info("    Running OCR fallback...")

    page_texts = []

    try:
        for page_num in range(1, min(page_count, MAX_PDF_PAGES) + 1):

            images = convert_from_bytes(
                pdf_bytes,
                dpi=250,
                first_page=page_num,
                last_page=page_num,
                poppler_path=POPPLER_PATH
            )

            page_texts.append(
                pytesseract.image_to_string(images[0]) if images else ""
            )

            if (
                len(page_texts) >= 2 and
                classify_result_page(page_texts[-2]) == "consolidated"
            ):
                break

    except Exception as e:
        log.warning(f"    OCR extraction failed: {e}")

    return page_texts

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

def normalise_financials(data) -> dict | None:
    """Validate Claude's JSON: numbers only, 3 values per metric, amounts in crores."""
    if not isinstance(data, dict):
        log.warning("    Claude returned non-object JSON")
        return None

    unit = str(data.get("unit") or "").strip().lower()
    unit = UNIT_ALIASES.get(unit, unit)

    if unit not in UNIT_TO_CRORE:
        log.warning(f"    Unrecognised unit from Claude: {data.get('unit')!r}")
        return None

    factor = UNIT_TO_CRORE[unit]

    fin = {
        "basis": str(data.get("basis") or "unknown").strip().lower(),
        "unit": unit,
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

def extract_financials_claude(pdf_bytes: bytes) -> dict | None:
    """Extract text from PDF locally, then send to Claude via AICredits."""
    try:
        # Step 1 — extract text locally with pdfplumber, OCR if it's a scan
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            page_count = len(pdf.pages)
            page_texts = [
                page.extract_text() or ""
                for page in pdf.pages[:MAX_PDF_PAGES]
            ]

        real_chars = sum(len(t.strip()) for t in page_texts)

        if real_chars < MIN_TEXT_CHARS:
            log.info(f"    Only {real_chars} chars of text layer, attempting OCR...")
            page_texts = extract_pages_ocr(pdf_bytes, page_count)

        selected, reason = select_result_pages(page_texts)

        if not selected:
            log.warning("    No result table page found")
            return None

        text = "\n".join(
            f"\n\n--- PAGE {i + 1} ---\n{page_texts[i]}"
            for i in selected
        )

        log.info(
            f"    Selected pages {[i + 1 for i in selected]} "
            f"({reason}, {len(text)} chars)"
        )

        # Step 2 — send text to Claude via AICredits OpenAI-compatible API
        response = client.chat.completions.create(
            model=CLAUDE_MODEL,
            max_tokens=1500,
            messages=[
                {
                    "role": "user",
                    "content": f"{CLAUDE_PROMPT}\n\n---\nPDF TEXT:\n{text}"
                }
            ],
        )

        raw = response.choices[0].message.content or ""
        data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        log.info(f"    Claude extracted: {data}")

        fin = normalise_financials(data)

        if fin:
            log.info(f"    Basis: {fin['basis']}, unit: {fin['unit']}")

        return fin

    except json.JSONDecodeError as e:
        log.warning(f"    Claude JSON parse error: {e}")
        return None
    except Exception as e:
        log.warning(f"    Claude extraction failed: {e}")
        return None


PROCESSED_SCRIPS_FILE = "processed_scrips.json"

def load_processed_scrips():

    try:
        if os.path.exists(PROCESSED_SCRIPS_FILE):
            with open(PROCESSED_SCRIPS_FILE, "r") as f:
                return set(json.load(f))
    except:
        pass

    return set()

def save_processed_scrips(data):

    with open(
        PROCESSED_SCRIPS_FILE,
        "w"
    ) as f:

        json.dump(
            list(data),
            f
        )

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

    # Tiny prior-year base makes growth % meaningless → no growth points.
    # abs() so a real loss (e.g. -5 Cr) still counts as a turnaround base.
    small_base = (
        (y_pat is not None and abs(y_pat) < SMALL_BASE_PAT_CR) or
        (y_rev is not None and y_rev < SMALL_BASE_REV_CR)
    )

    def growth_label(growth):
        if small_base:
            return "small base"
        return f"{growth:.1f}%" if growth is not None else "N/A"

    # Current negatives were rejected above, so a negative base = loss → profit
    eps_turnaround = y_eps is not None and y_eps < 0 and c_eps is not None
    pat_turnaround = y_pat is not None and y_pat < 0 and c_pat is not None

    # ─────────────────────────────────────────
    # 1. EPS Surprise (15 pts, half on turnaround)
    # ─────────────────────────────────────────

    s = 0.0 if small_base else band_score(eps_yoy, [
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
        growth_label(eps_yoy),
        f"{s:.1f}/15"
    )

    # ─────────────────────────────────────────
    # 2. PAT Growth YoY (10 pts, half on turnaround)
    # ─────────────────────────────────────────

    s = 0.0 if small_base else band_score(pat_yoy, [
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
        growth_label(pat_yoy),
        f"{s:.1f}/10"
    )

    # ─────────────────────────────────────────
    # 3. Revenue Growth YoY (10 pts)
    # ─────────────────────────────────────────

    s = 0.0 if small_base else band_score(rev_yoy, [
        (50, 10),
        (30, 8),
        (20, 6),
        (10, 4),
        (5, 2),
    ])

    score += s

    bd["Revenue Growth YoY"] = (
        growth_label(rev_yoy),
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

    s = 0.0 if small_base else band_score(rev_qoq, [
        (25, 5),
        (15, 4),
        (10, 3),
        (5, 2),
    ])

    score += s

    bd["Revenue QoQ"] = (
        growth_label(rev_qoq),
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

    if (eps_turnaround or pat_turnaround) and not small_base:
        bd["Turnaround"] = (
            "loss→profit",
            "½ PAT/EPS"
        )

    return round(score, 1), bd

# ── TELEGRAM ─────────────────────────────────────────────────

def send_telegram(company,
    scrip,
    score,
    bd,
    fin,
    exchange_time,
    alert_time,
    filing_type,
    delay_text):
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
        f"📌 BSE: <code>{scrip}</code>",

        "",

        f"📄 <b>Filing:</b> <code>{filing_type}</code>",
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

        f"🔗 https://www.bseindia.com/stock-share-price/x/x/{scrip}/",
    ]

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": "\n".join(lines),
                "parse_mode": "HTML",
            },
            timeout=10,
        )
    except requests.RequestException as e:
        log.warning(f"  Telegram send failed: {e}")
        return

    if r.status_code == 200:
        log.info("  ✅ Telegram sent")
    else:
        log.warning(f"  Telegram error: {r.text}")


# ── MAIN ─────────────────────────────────────────────────────

def has_core_values(fin: dict) -> bool:
    return (
        fin["revenue_from_operations"][0] is not None and
        fin["pat"][0] is not None
    )

def process_filing(ann, company, scrip, filing_type, exchange_dt) -> bool:
    """Download, extract, score and alert. True only if financials were extracted."""
    attach = ann.get("ATTACHMENTNAME") or ""

    if not attach:
        log.info("   No attachment"); return False

    exchange_time = (
        exchange_dt.strftime("%d %b %Y  %H:%M:%S") if exchange_dt else "N/A"
    )
    log.info(f"   Exchange time: {exchange_time}")

    pdf = download_pdf(attach)
    if not pdf:
        log.info("   PDF download failed"); return False

    pdf_mb = len(pdf) / (1024 * 1024)
    log.info(f"   PDF: {pdf_mb:.1f} MB — extracting text…")
    fin = extract_financials_claude(pdf)
    if not fin:
        log.info("   Could not extract financials"); return False

    if not has_core_values(fin):
        log.info("   Missing current-quarter revenue or PAT"); return False

    score, bd = compute_pead_score(fin)

    save_result_csv(company, scrip, score, fin)
    log.info(f"   PEAD score: {score}")

    if score >= PEAD_THRESHOLD:

        log.info("   🚀 Above threshold! Sending alert…")

        delay_text = "N/A"

        if exchange_dt:
            delay_seconds = int((datetime.now() - exchange_dt).total_seconds())
            mins, secs = divmod(delay_seconds, 60)
            delay_text = f"{mins}m {secs}s"

        send_telegram(
            company,
            scrip,
            score,
            bd,
            fin,
            exchange_time,
            datetime.now().strftime("%d %b %Y  %H:%M:%S"),
            filing_type,
            delay_text
        )
    else:
        log.info(f"   Below {PEAD_THRESHOLD}, no alert")

    return True

def main():
    log.info("=" * 55)
    log.info("  PEAD Tool — BSE poller + Claude Haiku PDF reader")
    log.info(f"  Alert threshold : score >= {PEAD_THRESHOLD}")
    log.info("=" * 55)
    
    initialize_csv()

    seen: set = load_seen()
    processed_scrips = load_processed_scrips()   # "{scrip}_{quarter}" keys
    failed_attempts: dict = {}                   # fid → failures, this run only

    log.info(f"Loaded {len(seen)} previously seen filings")
    log.info(f"Loaded {len(processed_scrips)} processed scrip-quarters")

    while True:
        filings = fetch_all_announcements()
        log.info(f"BSE: {len(filings)} announcements today")

        for ann in filings:
            fid     = str(ann.get("NEWSID") or ann.get("DT_TM") or "")
            company = ann.get("SLONGNAME") or ann.get("SNAME") or "Unknown"
            scrip   = str(ann.get("SCRIP_CD") or "")
            category = ann.get("CATEGORYNAME", "")

            if not fid or fid in seen:
                continue

            if category not in {
                "Result",
                "Board Meeting"
            }:
                continue

            if category == "Board Meeting":
                headline = announcement_headline(ann)

                if not is_result_board_meeting(headline):
                    log.info(f"→ Skip board meeting without results: {company} — {headline[:100]}")
                    seen.add(fid)
                    save_seen(seen)
                    continue

            exchange_dt = parse_exchange_time(ann)
            quarter_key = f"{scrip}_{reporting_quarter((exchange_dt or datetime.now()).date())}"

            if quarter_key in processed_scrips:
                log.info(f"→ Skip {company}: {quarter_key} already scored")
                seen.add(fid)
                save_seen(seen)
                continue

            log.info(f"→ New: {company} ({quarter_key}, {category})")

            try:
                ok = process_filing(ann, company, scrip, category, exchange_dt)
            except Exception:
                log.exception("   Unexpected error processing filing")
                ok = False

            if ok:
                processed_scrips.add(quarter_key)
                save_processed_scrips(processed_scrips)
                seen.add(fid)
                save_seen(seen)
                failed_attempts.pop(fid, None)
                continue

            failed_attempts[fid] = failed_attempts.get(fid, 0) + 1

            if failed_attempts[fid] > MAX_RETRIES:
                log.info(f"   Giving up after {failed_attempts[fid]} attempts")
                seen.add(fid)
                save_seen(seen)
            else:
                log.info(
                    f"   Will retry next poll "
                    f"({failed_attempts[fid]}/{MAX_RETRIES} retries used)"
                )

        log.info(f"Sleeping {POLL_INTERVAL_SEC}s…\n")
        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    main()
