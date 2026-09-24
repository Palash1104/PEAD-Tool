# PEAD Result Scanner

Post-Earnings Announcement Drift scanner for Indian stocks. Polls BSE corporate announcements, pulls quarterly-result PDFs, has Claude Haiku extract the financials as JSON, scores the result out of 50, logs it to CSV, and sends a Telegram alert when the score is at or above `PEAD_THRESHOLD`.

Everything lives in one file, [pead_tool.py](pead_tool.py). It is a long-running script with no tests, no package structure and no CLI arguments.

## Running

```
python pead_tool.py
```

- Dependencies are in `requirements.txt` (unpinned): openai, requests, pdfplumber, pdf2image, pytesseract, python-dotenv.
- On this machine the Python 3.14 install (`C:\Users\tralp\AppData\Local\Python\bin\python.exe`, i.e. `py`) has the deps. The Python 3.10 install on PATH does **not**. Needs Python 3.10+ (`dict | None` syntax).
- External binaries use hardcoded Windows paths: Tesseract at `C:\Program Files\Tesseract-OCR\tesseract.exe`, Poppler at `C:\poppler\Library\bin`.
- `.env` must define `AICREDITS_API_KEY`, `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. The script raises at import if any is missing. Never print or commit `.env`.

## Pipeline (one poll cycle in `main()`)

1. **Fetch**: `fetch_all_announcements()` GETs BSE `AnnSubCategoryGetData` for today's date, `pageno=1` only, with a browser User-Agent and Referer. Returns `json["Table"]`, or `[]` on any error, including an Akamai "Access Denied" HTML page.
2. **Filter** each announcement in `main()`:
   - Skip if `NEWSID` is already in `seen`.
   - `CATEGORYNAME` must be in `{"Result", "Board Meeting"}`. Board Meeting is included because Board Meeting Outcome PDFs often carry results and are published earlier.
   - Board Meeting filings must also pass `is_result_board_meeting()`: "outcome" plus the word result(s) in `NEWSSUB`/`HEADLINE`/`SUBCATNAME`. Those field names are not verified against a live response. Filings that fail the filter are logged and marked seen.
   - The block key is `"{scrip}_{quarter}"`, e.g. `532540_Q1FY27`. `reporting_quarter()` derives the quarter from the filing date: Jul–Sep gives Q1, Oct–Dec Q2, Jan–Mar Q3 and Apr–Jun Q4, on an April–March FY. If the key is already in `processed_scrips`, the filing is marked seen and skipped.
3. **`process_filing()`**: downloads `BSE_PDF_BASE + ATTACHMENTNAME`, extracts, scores, writes the CSV and alerts. It returns True only if extraction succeeded, meaning current-quarter `revenue_from_operations` **and** `pat` are both present (`has_core_values`).
   - `main()` wraps it in try/except.
   - On success the quarter key goes into `processed_scrips` and the NEWSID into `seen`.
   - On failure the NEWSID is retried on later polls, up to `MAX_RETRIES` (3) extra attempts, then marked seen. The attempt counter is in-memory only, so it resets on restart.
4. **Page texts** in `extract_financials_claude()`: pdfplumber reads pages 1–`MAX_PDF_PAGES` (25). If the combined stripped text is under `MIN_TEXT_CHARS` (500), the PDF is treated as a scan. `extract_pages_ocr()` then OCRs one page at a time (Poppler at 250 dpi, then Tesseract) and stops early once a consolidated table page and its next page are read. OCR takes about 2s per page.
5. **Page selection** in `select_result_pages()`:
   - A page is a result table if it has at least 2 `TABLE_MARKERS`.
   - Its type comes from `RESULT_HEADINGS` matched in its first `HEADING_LINES` (20) lines.
   - Preference order: the first consolidated table, then standalone, then generic. That page and the next one are sent.
   - Fallback is the best `PAGE_SCORE_KEYWORDS` page plus the next one. If every page scores 0, the filing fails.
6. **LLM extraction**: the OpenAI SDK points at `https://api.aicredits.in/v1` with model `anthropic/claude-haiku-4-5` and `max_tokens=1500`. The selected pages are sent in full, with no truncation. The JSON is sliced from the first `{` to the last `}`.
   - `normalise_financials()` validates the reply. Every `FIN_KEYS` entry becomes a 3-float list `[cq, pq, ly]`: non-numbers become None and the list is padded or trimmed.
   - It converts amounts to ₹ crore **in code** using `unit` and `UNIT_TO_CRORE`. `basic_eps` is never converted.
   - It keeps `basis` (consolidated or standalone) and `unit` as extra keys. An unknown unit fails the extraction.
7. **Score**: `compute_pead_score(fin)` returns `(score, breakdown_dict)`.
8. **Log and alert**: `save_result_csv()` appends to `pead_results.csv`; an OSError is logged and doesn't block the alert. If `score >= PEAD_THRESHOLD`, `send_telegram()` posts an HTML message.
   - The company name and scorecard rows are HTML-escaped.
   - The post is guarded with try/except.
   - Delay is measured at alert time.
9. Sleep `POLL_INTERVAL_SEC` (30s). Processing is sequential, so one slow OCR or LLM call delays everything behind it.

## PEAD score (max 50)

Growth is `_pct(curr, prev) = (curr - prev) / abs(prev) * 100`. Bands are applied by `band_score`: the first threshold reached, walking down from the highest.

| Factor | Pts | Input |
|---|---|---|
| EPS surprise | 15 | basic EPS YoY % |
| PAT growth | 10 | PAT YoY % |
| Revenue growth | 10 | revenue YoY % |
| EBITDA margin expansion | 5 | margin delta (pp) vs same quarter last year |
| Revenue momentum | 5 | revenue QoQ % |
| Net margin quality | 5 | PAT / revenue % |

- **Hard reject**: the score is 0 if current PAT, EBITDA or EPS is negative.
- **Small base**: all four growth factors (EPS, PAT YoY, revenue YoY, revenue QoQ) score 0, labelled "small base", if `abs(last-year PAT) < SMALL_BASE_PAT_CR` (1) or `last-year revenue < SMALL_BASE_REV_CR` (10). The remaining maximum is then 10, so these companies can't alert.
- **Turnaround**: the EPS and PAT growth factors are each halved when that metric's last-year value was negative. A `"Turnaround"` row is added to `bd`, which also adds a 🔄 header line to the alert. The row is omitted when small-base applies.
- **EBITDA**: `estimate_ebitda()` uses the extracted `ebitda` if `[0]` is non-null. Otherwise it computes PBT + finance cost + depreciation. Other income is not removed.
- Missing inputs score 0 for that factor. A fully null extraction therefore scores 0.0 rather than being treated as a failure.

## State and output files

| File | Purpose | Git |
|---|---|---|
| `seen.json` | NEWSIDs finished with (succeeded, skipped, or retries exhausted) | ignored |
| `processed_scrips.json` | `"{scrip}_{quarter}"` keys scored successfully. Bare scrip codes from the old format never match. | **tracked** |
| `pead_results.csv` | One row per scored filing | ignored |

CSV columns: timestamp, company, scrip, score, then revenue/pat/ebitda for cq, pq and ly, then eps_cq and eps_ly.

Older rows came from an earlier scoring version and include scores above 50. Rows before 2026-09-24 relied on the LLM to convert units, and many small caps look like unconverted lakhs; newer rows are converted in code. There is no version, filing ID, category, headline or basis column.

## Config constants (top of `pead_tool.py`)

- `PEAD_THRESHOLD = 35`. The module docstring says 30, which is stale.
- `MAX_RETRIES = 3`, `SMALL_BASE_PAT_CR = 1`, `SMALL_BASE_REV_CR = 10`
- Extraction tuning sits next to the prompt: `MAX_PDF_PAGES`, `MIN_TEXT_CHARS`, `HEADING_LINES`, `TABLE_MARKERS`, `RESULT_HEADINGS`, `UNIT_TO_CRORE`.
- The Telegram emoji tiers are hardcoded separately in `send_telegram`: 🚀 ≥40, ✅ ≥30, 🟡 otherwise.
- `POLL_INTERVAL_SEC = 30`
- `CLAUDE_MODEL = "anthropic/claude-haiku-4-5"` (AICredits model naming)
- `SEEN_FILE`, `RESULTS_CSV`, `PROCESSED_SCRIPS_FILE` are relative paths, so run from the repo root.

## Conventions

- The code style is procedural: module-level functions, a `log = logging.getLogger` logger, and f-string logs prefixed with spaces to indent sub-steps under `→ New: <company>`.
- Network and parse errors are generally caught, logged with `log.warning`, and the step returns `None` or `[]`. Keep that pattern. `main()` also wraps each `process_filing()` call in try/except, so a crash counts as a failed attempt instead of stopping the scanner.
- After `normalise_financials()`, every `FIN_KEYS` value is a 3-element list of floats or None, in ₹ crore (EPS in ₹). Add new metrics to both `FIN_KEYS` and the prompt.
- Telegram messages use `parse_mode: HTML`. Dynamic text must be HTML-escaped (company names contain `&`).
- The BSE API and PDF host sit behind Akamai. Keep browser-like headers and don't hammer them.

## Known issues (as of 2026-09-24; update as they are fixed)

Fixed on 2026-09-24:
- The per-quarter block
- The Board Meeting filter
- Retry on failure
- Pages 1–25 and a working OCR trigger
- Consolidated preference
- Unit conversion in code
- No truncation
- HTML escaping and exception guards

Still open (P1/P2):
- Only BSE `pageno=1` is fetched, and an Akamai "Access Denied" reply is indistinguishable from "no announcements".
- A hybrid PDF (text cover letter, scanned result pages) has more than 500 chars of text, so it isn't OCR'd. The table isn't found and the fallback sends the cover letter.
- The Telegram emoji tiers are hardcoded (40/30), and `send_telegram` calls `estimate_ebitda` 7 times.
- The CSV lacks filing ID, headline, basis and score-version columns. State-file writes aren't atomic. `requirements.txt` is unpinned, and the module docstring is stale.
