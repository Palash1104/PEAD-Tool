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

def parse_exchange_time(ann: dict) -> str:
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
                return datetime.strptime(s, fmt).strftime("%d %b %Y  %H:%M:%S")
            except ValueError:
                continue
        return s
    return "N/A"

# ── CLAUDE PDF EXTRACTION ─────────────────────────────────────

CLAUDE_PROMPT = """This is a quarterly financial result PDF filed by an Indian listed company on BSE/NSE.

Extract the following metrics from the STANDALONE QUARTERLY columns only (NOT year-to-date or full year columns).
The result table has 3 quarterly columns:
  Column 1: Current quarter (most recent)
  Column 2: Previous quarter (immediately preceding)
  Column 3: Same quarter last year (year-over-year)

Extract these values IN CRORES (Rs. Cr):
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

Return ONLY a JSON object, no explanation, no markdown:
{
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
- Plain floats only, no commas or symbols
- If values shown in lakhs, divide by 100 to convert to crores
- Negatives as negative floats e.g. -12.5
- Do NOT include annual/year-ended columns"""

RESULT_PAGE_PATTERNS = [
    "statement of audited consolidated financial results",
    "statement of audited standalone financial results",
    "statement of unaudited consolidated financial results",
    "statement of unaudited standalone financial results",
    "financial results for the quarter",
    "financial results for the half year",
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

def extract_text_ocr(pdf_bytes: bytes) -> str:
    try:
        log.info("    Running OCR fallback...")

        images = convert_from_bytes(
            pdf_bytes,
            dpi=250,
            first_page=2,
            last_page=10,
            poppler_path=POPPLER_PATH
        )

        ocr_text = ""

        for i, image in enumerate(images):
            text = pytesseract.image_to_string(image)

            if text.strip():
                ocr_text += f"\n\n--- OCR PAGE {i+1} ---\n{text}"

        return ocr_text

    except Exception as e:
        log.warning(f"    OCR extraction failed: {e}")
        return ""

def extract_financials_claude(pdf_bytes: bytes) -> dict | None:
    """Extract text from PDF locally, then send to Claude via AICredits."""
    try:
        # Step 1 — extract text locally with pdfplumber
        relevant_pages = []

        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:

            pages = pdf.pages[1:10]
            
            best_page = None
            best_score = -1

            for idx, page in enumerate(pages, start=2):

                page_text = page.extract_text() or ""

                lower = page_text.lower()
                
                page_score = 0

                for keyword, weight in PAGE_SCORE_KEYWORDS.items():

                    if keyword in lower:
                        page_score += weight

                if page_score > best_score:

                    best_score = page_score

                    best_page = {
                        "page_num": idx,
                        "text": page_text
                    }

                matched = any(
                    pattern in lower
                    for pattern in RESULT_PAGE_PATTERNS
                )

                if matched:

                    log.info(f"    Found result table on PAGE {idx}")

                    relevant_pages.append(
                        f"\n\n--- PAGE {idx} ---\n{page_text}"
                    )

                    # Include next page too (table continuation)

                    actual_next_page = idx

                    if actual_next_page < len(pdf.pages):

                        next_page_text = (
                            pdf.pages[actual_next_page].extract_text() or ""
                        )

                        relevant_pages.append(
                            f"\n\n--- PAGE {idx + 1} ---\n{next_page_text}"
                        )

                    break

        # No exact heading found -> use highest scoring page

        if not relevant_pages and best_page:

            log.info(
                f"    No exact result heading found. "
                f"Using PAGE {best_page['page_num']} "
                f"(score={best_score})"
            )

            relevant_pages.append(
                f"\n\n--- PAGE {best_page['page_num']} ---\n"
                f"{best_page['text']}"
            )

        text = "\n".join(relevant_pages)

        if not text.strip():

            log.warning("    No text layer found, attempting OCR...")

            text = extract_text_ocr(pdf_bytes)

            if not text.strip():
                log.warning("    OCR also failed")
                return None

        log.info(
            f"    Selected {len(relevant_pages)} relevant pages "
            f"({len(text)} chars)"
        )
        # Step 2 — send text to Claude via AICredits OpenAI-compatible API
        response = client.chat.completions.create(
            model=CLAUDE_MODEL,
            max_tokens=512,
            messages=[
                {
                    "role": "user",
                    "content": f"{CLAUDE_PROMPT}\n\n---\nPDF TEXT:\n{text[:12000]}"
                }
            ],
        )

        raw = response.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        data = json.loads(raw)
        log.info(f"    Claude extracted: {data}")
        return data

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

    # ─────────────────────────────────────────
    # 1. EPS Surprise (15 pts)
    # ─────────────────────────────────────────

    s = band_score(eps_yoy, [
        (100, 15),
        (70, 13),
        (50, 11),
        (30, 8),
        (15, 5),
        (5, 2),
    ])

    score += s

    bd["EPS Surprise"] = (
        f"{eps_yoy:.1f}%" if eps_yoy is not None else "N/A",
        f"{s:.1f}/15"
    )

    # ─────────────────────────────────────────
    # 2. PAT Growth YoY (10 pts)
    # ─────────────────────────────────────────

    s = band_score(pat_yoy, [
        (80, 10),
        (50, 8),
        (30, 6),
        (15, 4),
        (5, 2),
    ])

    score += s

    bd["PAT Growth YoY"] = (
        f"{pat_yoy:.1f}%" if pat_yoy is not None else "N/A",
        f"{s:.1f}/10"
    )

    # ─────────────────────────────────────────
    # 3. Revenue Growth YoY (10 pts)
    # ─────────────────────────────────────────

    s = band_score(rev_yoy, [
        (50, 10),
        (30, 8),
        (20, 6),
        (10, 4),
        (5, 2),
    ])

    score += s

    bd["Revenue Growth YoY"] = (
        f"{rev_yoy:.1f}%" if rev_yoy is not None else "N/A",
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

    s = band_score(rev_qoq, [
        (25, 5),
        (15, 4),
        (10, 3),
        (5, 2),
    ])

    score += s

    bd["Revenue QoQ"] = (
        f"{rev_qoq:.1f}%" if rev_qoq is not None else "N/A",
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
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",

        f"🏢 <b>{company}</b>",
        f"📌 BSE: <code>{scrip}</code>",

        "",

        f"📄 <b>Filing:</b> <code>{filing_type}</code>",
        f"🕐 <b>Exchange:</b> <code>{exchange_time}</code>",
        f"📲 <b>Alert:</b>   <code>{alert_time}</code>",
        f"⚡ <b>Delay:</b>   <code>{delay_text}</code>",

        "",

        "📊 <b>QUARTERLY FINANCIALS (₹ Cr)</b>",

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
            f"{k[:27]:<28}{val:>12}{pts:>10}"
            for k, (val, pts) in bd.items()
        )
        + "</pre>",

        "",

        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",

        f"🔗 https://www.bseindia.com/stock-share-price/x/x/{scrip}/",
    ]

    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": "\n".join(lines),
            "parse_mode": "HTML",
        },
        timeout=10,
    )

    if r.status_code == 200:
        log.info("  ✅ Telegram sent")
    else:
        log.warning(f"  Telegram error: {r.text}")


# ── MAIN ─────────────────────────────────────────────────────

def main():
    log.info("=" * 55)
    log.info("  PEAD Tool — BSE poller + Claude Haiku PDF reader")
    log.info(f"  Alert threshold : score >= {PEAD_THRESHOLD}")
    log.info("=" * 55)
    
    initialize_csv()

    seen: set = load_seen()
    processed_scrips = load_processed_scrips()

    log.info(f"Loaded {len(seen)} previously seen filings")
    log.info(f"Loaded {len(processed_scrips)} processed scrips")

    while True:
        filings = fetch_all_announcements()
        log.info(f"BSE: {len(filings)} announcements today")

        for ann in filings:
            fid     = str(ann.get("NEWSID") or ann.get("DT_TM") or "")
            company = ann.get("SLONGNAME") or ann.get("SNAME") or "Unknown"
            scrip   = str(ann.get("SCRIP_CD") or "")
            attach  = ann.get("ATTACHMENTNAME") or ""
            category = ann.get("CATEGORYNAME", "")
            
            if category not in {
                "Result",
                "Board Meeting"
            }:
                continue
            
            if scrip in processed_scrips:
                log.info(
                    f"   {scrip} already scanned before, skip"
                )
                continue
            
            if category == "Board Meeting":
                filing_type = "Board Meeting"
            else:
                filing_type = "Result"
            

            if not fid or fid in seen:
                continue

            seen.add(fid)
            save_seen(seen)

            log.info(f"→ New: {company} ({scrip})")
            if not attach:
                log.info("   No attachment, skip"); continue

            exchange_time = parse_exchange_time(ann)
            log.info(f"   Exchange time: {exchange_time}")
            
            delay_text = "N/A"

            try:

                exchange_dt = datetime.strptime(
                    exchange_time,
                    "%d %b %Y  %H:%M:%S"
                )

                delay_seconds = int(
                    (datetime.now() - exchange_dt).total_seconds()
                )

                mins, secs = divmod(delay_seconds, 60)

                delay_text = f"{mins}m {secs}s"

            except Exception:
                pass

            pdf = download_pdf(attach)
            if not pdf:
                log.info("   PDF download failed"); continue

            pdf_mb = len(pdf) / (1024 * 1024)
            log.info(f"   PDF: {pdf_mb:.1f} MB — extracting text…")
            fin = extract_financials_claude(pdf)
            if not fin:
                log.info("   Could not extract financials"); continue

            score, bd = compute_pead_score(fin)

            processed_scrips.add(scrip)
            save_processed_scrips(processed_scrips)

            save_result_csv(company, scrip, score, fin)
            log.info(f"   PEAD score: {score}")

            if score >= PEAD_THRESHOLD:
                
                log.info("   🚀 Above threshold! Sending alert…")
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

        log.info(f"Sleeping {POLL_INTERVAL_SEC}s…\n")
        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    main()