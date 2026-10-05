# PEAD Result Scanner

Post-Earnings Announcement Drift scanner for Indian stocks. It polls BSE and NSE corporate announcements, pulls quarterly-result PDFs, has an LLM (via AICredits) extract the financials as JSON, scores the result out of 50, logs it to CSV, and sends a Telegram alert when the score is at or above `PEAD_THRESHOLD`.

## Files

| File | What it is |
|---|---|
| [pead_tool.py](pead_tool.py) | The scanner. It is one procedural module with no package structure. |
| [compare_models.py](compare_models.py) | Runs a folder of saved PDFs through two models and prints the extracted values side by side. |
| [dashboard.py](dashboard.py) + [dashboard.html](dashboard.html) | Local read-only dashboard (`py dashboard.py`, http://127.0.0.1:8050). Standard library only. Reads the state files and `pead_tool.log`. Its one outbound request is `/filing` (below). |
| [test_pead.py](test_pead.py) | Offline tests. Run `python test_pead.py` and expect `FAILURES: 0`. Network, model and Telegram are mocked, and state files go to a temp dir. The PDF section uses the real pdfplumber, Poppler and Tesseract (about 15s of OCR). Keep it passing and extend it with any change. |

## Running

```
python pead_tool.py            # poll forever
python pead_tool.py --verbose  # same, with DEBUG detail in the terminal too
python pead_tool.py --dump     # save raw_bse_sample.json + raw_nse_sample.json, log field names and scrip-master counts, exit
python compare_models.py pdfs/ --model-b <aicredits-model-name>   # --model-a defaults to EXTRACTION_MODEL
python test_pead.py
```

- Dependencies are in `requirements.txt` (unpinned). The tests also use Pillow and pypdfium2, which come with pdfplumber/pdf2image.
- On this machine the Python 3.14 install (`py`) has the deps. The Python 3.10 on PATH does **not**. Needs Python 3.10+.
- Tesseract is expected at `C:\Program Files\Tesseract-OCR\tesseract.exe` and Poppler at `C:\poppler\Library\bin` (hardcoded).
- `.env` needs `AICREDITS_API_KEY`, `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`; the script raises at import if any is missing. `EXTRACTION_MODEL` is optional and defaults to `anthropic/claude-haiku-4-5`. Never print or commit `.env`.

## Pipeline

Each poll, `main()` calls `run_cycle()` and then `wait_for_checks()` until the next poll (`POLL_INTERVAL_SEC`, 30s). `run_cycle()`:
1. `poll_exchanges()`
2. Sorts filings by priority: clear results (`filing_kind` "results") first, then ambiguous outcomes, each oldest first.
3. `triage()` filters and pre-checks dedup; clear filings are handled synchronously on the main thread (`handle_clear` → `process_filing`).
4. `drain_checks()` applies finished ambiguous checks.
5. New ambiguous outcomes are queued on `AmbiguousChecker` (`handle_ambiguous`).

**Threads.**
- `AmbiguousChecker` is a single daemon thread named `checker`. It runs `obtain_financials(..., ambiguous=True)`: download, results-table check, and the model only if a table is found.
- It has its own `NseClient` and **never touches `ScannerState`**. Results go on `checker.done`, and the main thread applies them in `apply_ambiguous_result` (score, CSV, alert, retry bookkeeping), during `wait_for_checks` or `drain_checks`.
- `in_flight` stops the checker re-queueing a filing (NSE lists it every poll). Only the main thread touches it.
- Non-filing log lines from the checker thread are tagged `[checker]` (by `LineFormatter`).
- The POLL summary ends with "checker queue: N pending", where N is the ambiguous filings queued or being checked (`in_flight`).
- `main()` stops the checker in a `finally`.
- Keep all state changes on the main thread.

**Terminal log (LineFormatter).** Every line is `HH:MM:SS  STATUS  message`.
- Filing lines come from `report(status, filing, text)`: exchange, `short_company()` (Ltd/Limited/-$ stripped, padded to `COMPANY_WIDTH` 30), then the text. Each is built whole in one call, so threads can't interleave it. Every filing handled gets exactly one line:
  - `SKIP`: already scored, or not a PDF
  - `CHECK`: ambiguous outcome queued
  - `NONE`: no results table; for a "Result" filing the line includes its headline (160 chars)
  - `SCORE` / `ALERT`: `score/50  quarter  basis`, plus "Telegram sent/FAILED" on alerts
  - `OLD`: before `SCORE_FROM_QUARTER`
  - `RETRY`: reason · "retry n/3" or "gave up after n attempts"
  - `ERROR`: unexpected exception, logged at ERROR level; the traceback goes to DEBUG
- Each cycle has one `POLL` line with both fetch results ("BSE: N new announcements (fetch OK, P pages read) · NSE: …") and one `POLL` summary: "done · N result filings · N intimations skipped · N ambiguous queued · N not relevant · checker queue: N pending". There is no "Sleeping" line.
- Intimations and not-relevant filings are only counted. `triage()` returns a verdict (results / ambiguous / intimation / not relevant / duplicate / seen), and `run_cycle` counts it.
- Other lines use their level word: `INFO` (startup, migrations), `WARN`, `ERROR`, `DEBUG`. Warnings and errors (fetch failures, blocks, model/OCR/Telegram problems) always get their own line.
- ISIN, headline source and full headline, exchange time, PDF size, OCR/page-selection notes and the model's raw JSON are DEBUG.
  - The terminal (`console_handler`) shows INFO; `--verbose` lowers it to DEBUG.
  - `pead_tool.log` always gets DEBUG.
  - The `pead_tool` logger is at DEBUG and the root logger stays at WARNING, so libraries (httpx, pdfminer) stay quiet.
- `dashboard.py` parses this layout. If a phrase changes, update its regexes and the "dashboard log parser" tests:
  - the stage timings "download 0.5s", "page scan", "OCR", "model"
  - "exchange→alert/scored"
  - "BSE:/NSE: N new announcements" and "fetch FAILED"
  - "checker queue: N"
  - the `⏱` marker

**Timings.**
- `obtain_financials` records download time, `get_result_text` page-scan and OCR time, and `extract_from_text` model time, all in a `timings` dict.
- `format_timings` appends them to the filing's line, e.g. `⏱ download 1.2s · page scan 0.3s · OCR 6.1s · model 3.4s · exchange→alert 2m 13s` ("exchange→scored" when there is no alert).

1. **Fetch** (`poll_exchanges`)
   - **BSE**: `fetch_bse_filings(known_ids)` reads `AnnSubCategoryGetData` pages. It stops when a page has no NEWSIDs unseen on earlier polls, or at the row's `TotalPageCnt`, capped at `BSE_MAX_PAGES` (100). It returns only new rows, so `state.pending` re-queues filings awaiting retry.
     - Verified 2026-09-24: 50 rows per page, newest first, and an out-of-range page returns an empty `Table`. A busy day runs 30+ pages.
     - BSE returns **403 unless `HEADERS` includes `Origin` and `Accept`**. User-Agent and Referer alone stopped working, for both the API and the scrip master. PDF downloads work either way.
   - **NSE**: `fetch_nse_filings(NseClient)` calls `/api/corporate-announcements` for today. `NseClient` primes cookies from the NSE homepage and re-primes once on 401/403.
   - `fetch_bse_filings` returns `(new filings, pages read)`. A page-1 failure raises; a later page failing keeps the earlier pages.
   - Poll logs read "BSE: N new announcements (fetch OK, P pages read)" and "NSE: N new announcements (fetch OK, M today)". A failure logs "BSE/NSE: fetch FAILED (…)", so a quiet poll and a failed one look different.
   - An Access Denied, 401 or 403 response raises `ExchangeBlocked`. `warn_blocked()` then logs an error every poll and sends Telegram at most every `BLOCKED_ALERT_SEC` per exchange. A block is never treated as zero announcements.
   - Filings from both exchanges plus pending ones are sorted oldest first, so whichever exchange published first is processed.
2. **Normalise**: `normalise_bse` and `normalise_nse` produce one shared filing dict:
   - `exchange`, `id` (`BSE:<NEWSID>` / `NSE:<seq_id>`), `company`, `code` (scrip code or symbol), `isin` (NSE only)
   - `category`: NSE's `desc` is mapped to "Board Meeting" or "Result" by `nse_category`
   - `headline` plus the `headline_fields` it came from, `attachment_url`, `exchange_dt`
3. **Filter** (`triage`)
   - Skip filings already in `seen`. The category must be "Result" or "Board Meeting".
   - Board Meeting filings are classified by `board_meeting_kind()` on the **full** headline:
     - "intimation" ("intimation" and no "outcome"): skip
     - "results" (the word result(s)): process normally
     - "ambiguous" ("outcome" without a result word, e.g. "Outcome of Board Meeting held today"): download and process only if the PDF has a results table, with OCR capped at `AMBIGUOUS_OCR_PAGES` (8). Logged as "Ambiguous outcome → results table found / not found".
     - "other": skip
   - Headline sources are BSE `NEWSSUB` + `MORE` (the full text when set; `HEADLINE` is cut at about 190 chars with "....") + `SUBCATNAME`, and NSE `desc`/`attchmntText`. Which fields were used, and the full headline, go to DEBUG. The filter always reads the full text.
   - Verified against live data on 2026-09-24, as were `seq_id`, `sm_isin`, `exchdisstime` and BSE `NEWSID`/`DT_TM`. `test_pead.py` has real captured rows as fixtures.
   - NSE files results under desc "Outcome of Board Meeting", with the standard text "…has submitted to the Exchange, the financial results for the period ended…".
   - NSE's generic "…Outcome of Board Meeting held on <date>" filings are non-result outcomes, such as buybacks.
   - NSE `attchmntFile` is `-` when there is no attachment (treated as none). Some attachments are `.zip`.
4. **Dedup**
   - The ISIN comes from the NSE filing itself, or from the scrip master for BSE (`lookup_isin`).
   - The pre-check key is `{ISIN}_{quarter}`, with the quarter estimated from the filing date (`reporting_quarter`). This cheap dedup skip is the only use of the filing-date estimate; it never decides a score. While the ISIN is unknown the key is `{EXCHANGE}-{code}_{quarter}`.
   - If the key is already in `processed_scrips`, the filing is marked seen and skipped.
5. **`process_filing()`** = `obtain_financials()` + `score_filing()`. The first downloads (NSE via the session), extracts and validates; it touches no state, so the checker thread shares it. The second computes the real key, scores, writes the CSV and alerts, on the main thread only.
   - `obtain_financials` validates in this order:
     1. `has_core_values` (current revenue **and** PAT).
     2. The quarter: `pdf_quarter` takes it from the PDF's `period_end`, trusted only if it falls 0–400 days before the filing date. There is **no filing-date fallback**: without a usable `period_end` it raises `QuarterUnknown` ("quarter unknown (no period_end)" / "(period_end … implausible …)").
     3. The column dates: `column_dates_problem` checks that `prev_period_end` is 3 months and `ly_period_end` 12 months before `period_end`, by year and month. A mismatch or missing date raises `RetryFiling("column dates don't line up (…)")`. This catches half-year, nine-month or swapped columns.
   - If the PDF's quarter is before `SCORE_FROM_QUARTER` ("Q2FY27"), `score_filing` logs "<quarter>: old quarter, ignored" and raises `SkipFiling(model_called=True)`. The filing is marked seen, and it isn't scored, alerted, added to processed or retried.
   - If that key is already processed, the filing is a duplicate: no score and no alert.
   - Otherwise it scores, writes the CSV and alerts, and returns the key. It returns None on failure.
6. **Retry or skip**
   - `obtain_financials` raises `RetryFiling(reason)` on a retryable failure: no attachment, download failed, model/extraction failed, missing revenue or PAT, quarter unknown, or column dates that don't line up. A crash is treated the same way.
   - `QuarterUnknown` is a `RetryFiling` that carries the extraction. Only when the last attempt also fails does `record_retry` score it and write **one** CSV row with quarter `UNKNOWN`, never alerted or added to processed. The line reads "… · gave up after 4 attempts · saved as quarter UNKNOWN (x/50, no alert)". A retry that recovers leaves only the real row. The filing is retried on later polls, up to `MAX_RETRIES` (3) extra attempts. The counts persist in `retry_counts.json`, so restarts don't reset them.
   - `SkipFiling` is raised when retrying can't help: "no results table found", or an attachment that isn't a PDF. The filing is then logged, marked seen and never retried, and the model is not called.
   - Success, a skip, or giving up calls `state.finish()`, which marks the filing seen and clears its retry count.

## Extraction (`extract_financials` = `get_result_text` + `extract_from_text`)

- pdfplumber reads pages 1–`MAX_PDF_PAGES` (25).
- If the whole PDF has under `MIN_TEXT_CHARS` (500) of text, it is OCR'd.
- Otherwise, if no result table is found in the text layer, the pages under `LOW_TEXT_PAGE_CHARS` (200) are OCR'd. That handles a text cover letter with scanned result pages.
- `ocr_pages()` works one page at a time (Poppler at 250 dpi, then Tesseract) and stops once a consolidated table and its next page are readable. OCR takes about 2s per page.
- `find_table_pages()`:
  - A page is a results table if it shows at least 2 of the 5 `TABLE_MARKERS` kinds: income, expenses, profit before tax, net profit and EPS.
    - Each kind is a regex covering company, NBFC, broker and bank wording, e.g. "Total Revenue", "Profit/(Loss) before Tax", "Earning per equity share", "Interest earned/expended", "Operating profit before provisions".
    - Gowra Leasing (NBFC, 26 Sep 2026) matched only one of the old four fixed phrases and was wrongly skipped. Real noisy text layers, like ESDS and Purple Style, show just 2 kinds.
    - A page whose heading matches `NOT_TABLE_HEADINGS` (cash flow, assets and liabilities, balance sheet) is not a table unless the heading also names the results.
  - Its type comes from `RESULT_HEADINGS` in its first 20 lines, and a table with an unrecognised title is "untitled".
  - The first page in `TABLE_PREFERENCE` order (consolidated, standalone, generic, untitled) is sent, together with the next page.
  - There is **no keyword fallback**. If no page is a results table, even after OCR, `get_result_text` returns `(None, "no results table found")` and `extract_financials` raises `SkipFiling`.
- `extract_from_text(text, model)` sends `EXTRACTION_PROMPT` plus the page text with `max_tokens=1500` and no truncation. The JSON is sliced from the first `{` to the last `}`.
- `normalise_financials()` validates the reply:
  - Every `FIN_KEYS` entry becomes a 3-float list `[cq, pq, ly]`.
  - Amounts are converted to ₹ crore in code from `unit` (`UNIT_TO_CRORE`). EPS is never converted.
  - It keeps `basis`, `unit` and the three column dates `period_end`, `prev_period_end` and `ly_period_end` (ISO strings or None). An unknown unit fails the extraction.

## PEAD score (max 50)

Growth is `_pct(curr, prev) = (curr - prev) / abs(prev) * 100`. `band_score` awards the first band threshold reached, walking down from the highest.

| Factor | Pts | Input | Skipped as "small base" when |
|---|---|---|---|
| EPS surprise | 15 | basic EPS YoY % | \|last-year PAT\| < ₹1 Cr |
| PAT growth | 10 | PAT YoY % | \|last-year PAT\| < ₹1 Cr |
| Revenue growth | 10 | revenue YoY % | last-year revenue < ₹10 Cr |
| EBITDA margin expansion | 5 | margin delta (pp) vs same quarter last year | — |
| Revenue momentum | 5 | revenue QoQ % | last-quarter revenue < ₹10 Cr |
| Net margin quality | 5 | PAT / revenue % | — |

- **Hard reject**: the score is 0 if current PAT, EBITDA or EPS is negative.
- **Turnaround**: when last year's value was negative, that factor's EPS and PAT band scores are halved. A `"Turnaround"` row is added to `bd`, which also adds a 🔄 alert line. The row is omitted when the PAT small base applies.
- **EBITDA**: `estimate_ebitda()` uses the extracted EBITDA, or else PBT + finance cost + depreciation.

## State and output files (all relative, so run from the repo root)

| File | Purpose | Git |
|---|---|---|
| `seen.json` | Filing ids finished with. Bare legacy ids load as `BSE:` ids. | ignored |
| `processed_scrips.json` | `{ISIN}_{quarter}` keys (or the `{EXCHANGE}-{code}_{quarter}` fallback). `migrate_processed_keys()` upgrades legacy or fallback keys at startup and at the daily master refresh. It logs "Migrated N processed keys to ISIN format" and lists the keys still without an ISIN. | ignored (untracked 2026-09-26) |
| `retry_counts.json` | filing id → failed attempts | ignored |
| `scrip_master.json` | `{"bse": {code: ISIN}, "nse": {symbol: ISIN}, "updated"}` | ignored |
| `pead_results.csv` | One row per scored filing | ignored |
| `raw_bse_sample.json`, `raw_nse_sample.json` | `--dump` output | ignored |
| `pead_tool.log` (+ `.1`–`.3`) | The console lines plus DEBUG detail, UTF-8, rotating at 5 MB with 3 backups. `setup_file_logging()` is attached only in the `__main__` block, so tests and `compare_models.py` don't write to it. Always lives next to `pead_tool.py`. | ignored |

- **Scrip master**: built from BSE `ListofScripData` and NSE `EQUITY_L.csv` / `SME_EQUITY_L.csv`, refreshed daily. `updated` is only stamped when both downloads succeed, and failed downloads keep the cached mappings. NSE filings also teach symbol → ISIN (`learn_isin`).
- **CSV columns**: timestamp, company, scrip (code or symbol), score, revenue/pat/ebitda for cq, pq and ly, eps_cq, eps_ly, exchange, filing_url (the result PDF's `attachment_url`), period_end, quarter (e.g. `Q2FY27`, or `UNKNOWN`), basis, unit.
  - `initialize_csv()` appends any `CSV_ADDED_COLUMNS` an older file lacks:
    - `exchange`: old rows marked BSE
    - `filing_url`: blank for rows scored before 2026-09-26
    - `period_end`, `quarter`, `basis`, `unit`: blank for rows scored before 2026-10-05
  - `save_result_csv(filing, score, fin, quarter)` writes them.
- **Dashboard quarters**:
  - `dashboard.py` sends `SCORE_FROM_QUARTER` as `current_quarter`.
  - The results table has a Quarter column. The detail panel shows the quarter, the quarter-end date and the basis.
  - Rows whose quarter isn't the current one (UNKNOWN, blank or another quarter) get an amber ⚠ pill and a left stripe.
  - `alerted(r)` is score ≥ threshold and quarter not UNKNOWN. It drives "Alerts sent", "Alerts only", ▲, the badge and the skyline colours.
  - `dashboard.html` links the Exch. cell to `/filing?url=<filing_url>&exchange=&scrip=`, or straight to the company's exchange page (dotted underline) when `filing_url` is blank. It only accepts https links on bseindia.com / nseindia.com.
- **`/filing` in dashboard.py**: BSE moves result PDFs from `AttachLive` to `AttachHis` after a few months. Checked 2026-09-26: May filings 404 on AttachLive and load from AttachHis, mid-August ones are in both, and today's are only on AttachLive.
  - `resolve_filing()` checks AttachLive, then AttachHis, by fetching the first 8 bytes and looking for `%PDF`. It redirects (302) to the first one that works, else to the company page.
  - Results are cached for 6h. NSE archive links pass through unchecked. Any other URL is never fetched.
- **Data reset 2026-09-26**: the Q1FY27 run (results CSV, seen, processed, log) was moved to `archive/q1fy27/`, which is gitignored. `pead_results.csv` restarted empty with the current header. `scrip_master.json` was kept.

## Config (top of `pead_tool.py`)

- `PEAD_THRESHOLD = 35`, `POLL_INTERVAL_SEC = 30`, `MAX_RETRIES = 3`, `BLOCKED_ALERT_SEC = 3600`
- `SCORE_FROM_QUARTER = "Q2FY27"`: earlier quarters, by the PDF's period_end, are ignored, and a result without a usable period_end is never scored. `test_pead.py` sets it to "Q1FY20" for its Q1FY27 fixtures, and the cutoff tests restore the real value.
- `SMALL_BASE_PAT_CR = 1`, `SMALL_BASE_REV_CR = 10`
- The Telegram emoji tiers are hardcoded separately in `send_telegram` (🚀 ≥40, ✅ ≥30).
- Exchange constants sit in the EXCHANGES section, and extraction tuning sits next to `EXTRACTION_PROMPT`.

## Conventions

- The style is procedural: module-level functions and `log = logging.getLogger`. `ScannerState`, `NseClient`, `AmbiguousChecker` and `LineFormatter` are the only classes.
- Per-filing terminal output goes through `report()` only, once per filing. Put everything else about a filing in `log.debug`. Warnings have no leading indentation.
- Network and parse errors are caught, logged with `log.warning`, and the step returns None or `[]`. `handle_clear` catches `SkipFiling` (NONE/OLD/SKIP line, never retried), `RetryFiling` (RETRY line) and any other exception (ERROR line, counted as a failed attempt); the checker does the same around `obtain_financials`.
- After `normalise_financials()`, every `FIN_KEYS` value is a 3-element list of floats or None, in ₹ crore. Add new metrics to `FIN_KEYS` **and** the prompt.
- Telegram uses `parse_mode: HTML`, so HTML-escape anything dynamic. Company names and NSE symbols contain `&` (e.g. `M&M`).
- Both exchanges sit behind Akamai. Keep the browser-like headers and don't add request volume casually.
- Heredocs in the Bash tool mangle backslashes and `\n` in Python source. Write code with the Write or Edit tools, or with script files.

## Known open issues (2026-09-24)

- NSE SME announcements (`index=sme`) are not polled.
- `nse_category` maps any NSE desc containing "financial result" to "Result", but no such desc was seen on 2026-09-24; results came as "Outcome of Board Meeting".
- Ambiguous outcomes cost one PDF download each (no model call unless a table is found). In results season that could mean hundreds of downloads a day.
- Announcements are fetched for today only, so filings just before midnight can be missed on a restart.
- The emoji tiers are hardcoded, and `send_telegram` calls `estimate_ebitda` 7 times.
- The CSV lacks filing id, headline, basis, period and score-version columns. State writes aren't atomic, and `requirements.txt` is unpinned.
