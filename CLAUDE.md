# PEAD Result Scanner

Post-Earnings Announcement Drift scanner for Indian stocks. It polls BSE and NSE corporate announcements, pulls quarterly-result PDFs, has an LLM (via AICredits) extract the financials as JSON, scores the result out of 50, logs it to CSV, and sends a Telegram alert when the score is at or above `PEAD_THRESHOLD`.

## Files

| File | What it is |
|---|---|
| [pead_tool.py](pead_tool.py) | The scanner. It is one procedural module with no package structure. |
| [compare_models.py](compare_models.py) | Runs a folder of saved PDFs through two models and prints the extracted values side by side. |
| [dashboard.py](dashboard.py) + [dashboard.html](dashboard.html) | Local read-only dashboard (`py dashboard.py`, http://127.0.0.1:8050). Standard library only. Reads the state files, `archive/*/pead_results.csv`, `pead_tool.log` (live status, timings, exchange health) and `pead_terminal.log` (the Scanner log panel; falls back to `pead_tool.log` without DEBUG if it's missing). Its one outbound request is `/filing` (below). |
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

## Extraction (`extract_financials` = `find_result_pages` + model call)

- pdfplumber reads pages 1–`MAX_PDF_PAGES` (25).
- If the whole PDF has under `MIN_TEXT_CHARS` (500) of text, it is OCR'd.
- Otherwise, if no result table is found in the text layer, the pages under `LOW_TEXT_PAGE_CHARS` (200) are OCR'd. That handles a text cover letter with scanned result pages.
- `ocr_pages()` works one page at a time (Poppler at `LOCATE_OCR_DPI` = 150, then Tesseract) and stops once a consolidated table and its next page are readable. This OCR only *locates* the table (keywords), so low resolution is fine; it records which pages were OCR'd.
- `find_table_pages()`:
  - A page is a results table if it shows at least 2 of the 5 `TABLE_MARKERS` kinds: income, expenses, profit before tax, net profit and EPS.
    - Each kind is a regex covering company, NBFC, broker and bank wording, e.g. "Total Revenue", "Profit/(Loss) before Tax", "Earning per equity share", "Interest earned/expended", "Operating profit before provisions".
    - Gowra Leasing (NBFC, 26 Sep 2026) matched only one of the old four fixed phrases and was wrongly skipped. Real noisy text layers, like ESDS and Purple Style, show just 2 kinds.
    - A page whose heading matches `NOT_TABLE_HEADINGS` (cash flow, assets and liabilities, balance sheet) is not a table unless the heading also names the results.
  - Its type comes from `RESULT_HEADINGS` in its first 20 lines, and a table with an unrecognised title is "untitled".
  - The first page in `TABLE_PREFERENCE` order (consolidated, standalone, generic, untitled) is sent, together with the next page.
  - There is **no keyword fallback**. If no page is a results table, even after OCR, `get_result_text` returns `(None, "no results table found")` and `extract_financials` raises `SkipFiling`.
- `extract_from_text(text, model)` sends `EXTRACTION_PROMPT` plus the page text with `max_tokens=1500` and no truncation. The JSON is sliced from the first `{` to the last `}`.
- **Scanned result pages go to the model as images** (since 2026-10-07; Tiaan Consumer and Golkonda Aluminium came back with impossible figures from Tesseract digits). `extract_from_page_images()` sends the selected scanned pages as grayscale PNGs at `IMAGE_DPI` (150), long edge capped at `IMAGE_MAX_EDGE` (1568, Claude's limit), and text-layer pages as text. Rendering time is `timings["render"]`.
  - If the image call fails, the selected scanned pages are re-OCR'd at `FALLBACK_OCR_DPI` (250) and sent as text. A provider error that rejects images sets `_images_rejected` and text is used for the rest of the run; other errors fall back for that filing only. `SEND_SCANS_AS_IMAGES = False` restores the old text-only behaviour.
  - Not yet verified live: whether AICredits passes `image_url` PNG parts through to Haiku. The fallback covers a refusal; watch for a "Provider rejected page images" warning.
- `get_result_text()` still returns `(text, reason)` for `compare_models.py`; scanned pages there are locating-quality OCR text.
- `_call_model(..., raw_out=)` puts the model's raw JSON in `raw_out["data"]` even when validation fails, so `obtain_financials` can still see `period_end`.
- `normalise_financials()` validates the reply:
  - Every `FIN_KEYS` entry becomes a 3-float list `[cq, pq, ly]`.
  - Amounts are converted to ₹ crore in code from `unit` (`UNIT_TO_CRORE`). EPS is never converted.
  - It keeps `basis`, `unit` and the three column dates `period_end`, `prev_period_end` and `ly_period_end` (ISO strings or None). An unknown unit fails the extraction.

### After extraction (`obtain_financials`), in this order
1. **Old quarter first**: if `period_end` (from the validated result, or the raw JSON when validation failed, e.g. no unit) is before `SCORE_FROM_QUARTER`, the filing is `OLD` and never retried. A date over 400 days old only counts if all three column dates line up, so a misread year is retried instead.
2. Missing extraction / revenue / PAT → retry. 3. Quarter unknown → retry. 4. Column dates don't line up → retry.
5. **Sanity check** `numbers_problem()` on the current and year-ago columns: total income must not be below revenue, no expense line above total expenses, and income − total expenses must be near PBT (within max(10% of income, 25% of PBT)) or PAT (within max(10% of income, 50% of PAT)). A failure sets `fin["check"]`: the row is saved with that reason in the `check` CSV column, logged as a `FLAG` warning, **never alerted**, and the company-quarter is **not** marked processed, so the other exchange's copy or a corrected filing can still score. The dashboard treats flagged rows as not alerted.

### Before download
- `triage()` reads quarter-end dates written in the headline (`headline_period_ends`: 31.03.2025, 30th June 2026, September 30th, 2026, 30-Jun-2026, …). If the latest one is before `SCORE_FROM_QUARTER`, the filing is logged `OLD … per headline · not downloaded` and marked seen; counted as "N old quarter by headline" in the POLL summary. Using the latest date means a headline that also names a 30 Sep meeting is never skipped.

### Catch-up
- `SCANNER_STARTED_AT` is set in `main()`. Filings published before it get "(catch-up)" after their exchange→alert/scored delay (and in Telegram's Delay). `dashboard.py` sets `catchup` on those timing entries and the dashboard's delay median excludes them.

## Resuming after a stop
- `scan_checkpoint.json` holds, per exchange, when its last completed poll started. It is saved at the end of each cycle (after the cycle's clear filings are handled), and only for exchanges whose fetch succeeded.
- `ScannerState.resume_from(exchange)` returns the first day to fetch: the checkpoint's day if it's before today, at most `MAX_RESUME_DAYS` (7) back, else None (today only). `fetch_bse_filings(known, since=)` and `fetch_nse_filings(nse, since=)` request that date range (BSE `strPrevDate`/`strToDate`, NSE `from_date`/`to_date`).
- So a stop at 9 pm and a restart at 4 pm next day reads from 9 pm yesterday, and the first poll after midnight also re-reads yesterday (closes the midnight gap). `seen.json` stops anything already handled from being processed twice. On the first poll the terminal's counts only include announcements newer than the checkpoint (minus 2 min); later polls count every new row.
- `ScannerState.last_stop()` is the earlier of the two checkpoints; `resumed_since()` is that, floored at `MAX_RESUME_DAYS` back, also for a same-day restart (today is re-read, but what came before that time was already handled). None when there is no checkpoint. The banner's third line says it: "Scanning from 21:01 yesterday · where the last scan stopped", "Scanning from 00:00 today · no earlier run to pick up from", or the capped variant. `when_text()` gives "HH:MM today / yesterday / on Mon 05 Oct".

## Terminal vs log file
- `pead_tool.log` keeps the full `LineFormatter` layout with every POLL, CHECK and DEBUG line; `dashboard.py` parses it, so keep its phrases stable.
- The terminal (`configure_console`, `ConsoleFormatter`) shows a clean view of the same records:
  - a 3-line banner (model, threshold, quarter and the date; companies, scored, awaiting retry; "Scanning from …")
  - one colour-coded line per filing that matters (`console_text()` drops the ⏱ breakdown, shortens wording, adds "Ns after filing" to alerts)
  - after the first poll a "Caught up since … / on today" summary and a rule
  - after every later poll one line from `poll_line()`: "POLL   BSE 3 new  ·  NSE 0 new  ·  1 result  ·  2 intimations skipped" (`POLL_WORDS`); dim when nothing relevant, yellow and "BSE not answering" while an exchange fails
  - exchange failures also get their own line once, and again on recovery.
- Records logged with `extra={"console": False}` (`QUIET`) are file-only: queued CHECKs, vague outcomes without results, per-filing warnings, fetch details, scrip master and processed-key housekeeping. `say(text, style, word)` writes terminal-only lines via the `pead_console` logger, which the `pead_tool.log` handler drops; `word` fills the status column (the POLL line).
- `pead_terminal.log` (`setup_terminal_log()`, attached in `__main__` only) is a colourless copy of exactly what the clean terminal prints, whatever `--verbose` says. The dashboard's Scanner log panel shows its last 100 lines and colours them by the same status words.
- Colours use ANSI (turned on in Windows consoles via `SetConsoleMode`); off when `NO_COLOR` is set or output is redirected. `--verbose` restores the full file layout in the terminal, DEBUG included.

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
| `pead_terminal.log` (+ `.1`–`.2`) | Copy of the clean terminal, UTF-8, rotating at 1 MB with 2 backups. Read by the dashboard's Scanner log panel. | ignored |

- **Scrip master**: built from BSE `ListofScripData` and NSE `EQUITY_L.csv` / `SME_EQUITY_L.csv`, refreshed daily. `updated` is only stamped when both downloads succeed, and failed downloads keep the cached mappings. NSE filings also teach symbol → ISIN (`learn_isin`).
- **CSV columns**: timestamp, company, scrip (code or symbol), score, revenue/pat/ebitda for cq, pq and ly, eps_cq, eps_ly, exchange, filing_url (the result PDF's `attachment_url`), period_end, quarter (e.g. `Q2FY27`, or `UNKNOWN`), basis, unit.
  - `initialize_csv()` appends any `CSV_ADDED_COLUMNS` an older file lacks:
    - `exchange`: old rows marked BSE
    - `filing_url`: blank for rows scored before 2026-09-26
    - `period_end`, `quarter`, `basis`, `unit`: blank for rows scored before 2026-10-05
  - `save_result_csv(filing, score, fin, quarter)` writes them.
- **Dashboard quarter selector** (the grouping lives in `dashboard.py`, where `test_pead.py` covers it):
  - `read_all_results()` merges `archive/*/pead_results.csv` and `pead_results.csv` (archives first). Each row is tagged with `_quarter`, `_quarter_from` and `_source`.
  - `row_quarter()` takes the quarter column (incl. `UNKNOWN`), else `period_end`, else the timestamp via the filing-date mapping. `quarter_label` / `reporting_quarter` mirror `pead_tool.py`, and a test checks they match.
  - `archive/pre-q2fy27` (renamed from `archive/q1fy27` on 2026-10-05; any folder name works) is mostly March-quarter results reported Apr–Jun: Q4FY26 has 165 scored results. Only a few rows are Q1FY27.
  - Rows scoring above 50 come from the old 100-point scale. They are dropped and counted per quarter (`hidden_old_scale`); the count line says "N rows from the old scoring scale hidden".
  - `group_quarters()` builds the dropdown options: newest first, "Quarter unknown" last, and the current quarter (`SCORE_FROM_QUARTER`, sent as `current_quarter`) always present. Labels give the period covered and when it was reported (`reported_window`, the three months after quarter end), e.g. "Q4 FY26 · Jan–Mar 2026 quarter · reported Apr–Jun 2026 (165)". The count is non-empty scored rows.
  - The selected quarter filters the KPIs (results, alerts, average), skyline, detail panel, distribution and table.
  - **Current-quarter view** (selection = `SCORE_FROM_QUARTER`): a green "Current quarter" tag, plus the live parts, each tagged `live`: Exchange → alert, Awaiting retry, Filings handled, the timing chart and the scanner log.
  - **Past-quarter view** (`body.past-view`):
    - An amber "Past quarter" tag (or "Quarter unknown" / "Later quarter").
    - No `live` tags; the timing chart and log are hidden, and the distribution goes full width.
    - The three live tiles are replaced by `.past-only` tiles: Highest score (with company), Turnarounds (prior-year PAT < 0, current > 0; names in the tooltip) and Reporting window (first to last result, by when the scanner logged it, since the CSV has no filing date).
    - Today / 7 days / 30 days are disabled and the range resets to All.
  - The Scanner/BSE/NSE pills show in every view. Switching back to the current quarter redraws the timing chart and log.
  - The choice is kept in the URL (`?quarter=Q1FY27`, removed for the current quarter); an unknown value falls back to the current quarter.
  - There is no Quarter column in the table. The detail panel shows the quarter, the quarter-end date (or "quarter from filing date") and the basis.
  - `alerted(r)` is score ≥ threshold and quarter not UNKNOWN. It drives "Alerts sent", "Alerts only", ▲, the badge and the skyline colours.
  - `dashboard.html` links the Exch. cell to `/filing?url=<filing_url>&exchange=&scrip=`, or straight to the company's exchange page (dotted underline) when `filing_url` is blank. It only accepts https links on bseindia.com / nseindia.com.
- **`/filing` in dashboard.py**: BSE moves result PDFs from `AttachLive` to `AttachHis` after a few months. Checked 2026-09-26: May filings 404 on AttachLive and load from AttachHis, mid-August ones are in both, and today's are only on AttachLive.
  - `resolve_filing()` checks AttachLive, then AttachHis, by fetching the first 8 bytes and looking for `%PDF`. It redirects (302) to the first one that works, else to the company page.
  - Results are cached for 6h. NSE archive links pass through unchecked. Any other URL is never fetched.
- **Data reset 2026-09-26**: the run before Q2FY27 (results CSV, seen, processed, log) was moved to `archive/q1fy27/`, renamed `archive/pre-q2fy27/` on 2026-10-05; `archive/` is gitignored. `pead_results.csv` restarted empty with the current header. `scrip_master.json` was kept.

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
