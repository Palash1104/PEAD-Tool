"""
Run the scanner's model on a folder of saved result PDFs, or compare two models.

Each PDF's result pages are picked once (same logic and OCR as the scanner),
then each model reads them exactly as the scanner would send them: text pages
as text, scanned pages as images. Values are shown after unit conversion to
Rs. Cr, so a wrong unit shows up as a 100x / 10x mismatch. The summary gives
each model's tokens, the model AICredits says answered, and for models in
PRICES the cost.

  python compare_models.py pdfs/                          the scanner's model (Haiku 5.5) alone
  python compare_models.py pdfs/ --effort low             the same, thinking less
  python compare_models.py pdfs/ --model-b <other model>  side by side with another model

--model-a defaults to EXTRACTION_MODEL (Haiku 5.5). A second model is only
called when --model-b names one. --effort overrides EXTRACTION_EFFORT
(Claude 5.x thinking; Claude 3/4 ignore it). Model names are whatever
AICredits calls them.
"""
import argparse
import glob
import logging
import os
import time

import pead_tool as pt

META_KEYS = ["basis", "unit", "period_end", "prev_period_end", "ly_period_end", "period_months"]

# USD per million tokens (input, output) on AICredits, Oct 2026. Thinking is
# billed as output. Haiku 5.5's price is for prompts under 100k tokens.
PRICES = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-4.5": (1.00, 5.00),
    "claude-haiku-5.5": (0.10, 0.50),
    "claude-haiku-5-5": (0.10, 0.50),
}

def fmt(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)

def values_match(a, b) -> bool:
    if a is None or b is None or isinstance(a, str) or isinstance(b, str):
        return a == b
    return abs(a - b) <= max(0.01, 0.005 * max(abs(a), abs(b)))

def compare_pdf(path: str, model_a: str, model_b: str | None = None) -> dict:
    """Read one PDF with model_a (and model_b, if given) and print the values.
    Returns {"mismatches": differing fields, or None unless both models read
    it, "failed": models that got nothing, "fin": model_a's figures}."""
    models = [model_a] + ([model_b] if model_b else [])
    print(f"\n{'=' * 100}\n{os.path.basename(path)}")

    with open(path, "rb") as f:
        pdf_bytes = f.read()

    found = pt.find_result_pages(pdf_bytes)

    if not found["pages"]:
        print(f"  Skipped: {found['reason']}")
        return {"mismatches": None, "failed": [], "fin": None}

    scanned = [i for i in found["pages"] if pt.is_scan(found, i)]
    print(
        f"  Pages: {found['reason']}, pages {', '.join(str(i + 1) for i in found['pages'])}"
        + (f" (scanned: {', '.join(str(i + 1) for i in scanned)}"
           + (", sent as images)" if pt.SEND_SCANS_AS_IMAGES else ", sent as OCR text)") if scanned else "")
    )

    results, timings, cut_off = {}, {}, {}

    # Each model gets the pages found above instead of locating them again
    real_find = pt.find_result_pages
    pt.find_result_pages = lambda *args, **kwargs: found
    try:
        for model in models:
            spent, raw_out = {}, {}
            started = time.time()
            results[model] = pt.extract_financials(pdf_bytes, model, timings=spent, raw_out=raw_out)
            timings[model] = spent.get("model", time.time() - started)
            cut_off[model] = raw_out.get("cut_off", False)
    finally:
        pt.find_result_pages = real_find

    fin_a, fin_b = results[model_a], results.get(model_b)

    print("\n  " + f"{'field':<26}" + "".join(f"{m[:34]:>36}" for m in models))
    print(f"  {'-' * (26 + 36 * len(models))}")

    for model in models:
        if results[model] is None:
            print(f"  {model}: EXTRACTION FAILED" + (" (ran out of tokens before answering)" if cut_off[model] else ""))

    mismatches = 0

    if fin_a and not model_b:
        for key in META_KEYS + pt.FIN_KEYS:
            value = fin_a.get(key)
            cell = fmt(value) if key in META_KEYS else " / ".join(fmt(v) for v in value)
            print(f"  {key:<26}{cell:>36}")

        print("\n  (metric rows are current / previous quarter / same quarter last year, Rs. Cr; EPS in Rs.)")

    if fin_a and fin_b:
        for key in META_KEYS + pt.FIN_KEYS:
            a, b = fin_a.get(key), fin_b.get(key)

            if key in META_KEYS:
                same = values_match(a, b)
                cell_a, cell_b = fmt(a), fmt(b)
            else:
                same = all(values_match(x, y) for x, y in zip(a, b))
                cell_a = " / ".join(fmt(v) for v in a)
                cell_b = " / ".join(fmt(v) for v in b)

            mismatches += not same
            print(f"  {key:<26}{cell_a:>36}{cell_b:>36}  {'' if same else '<-- differs'}")

        print("\n  (metric rows are current / previous quarter / same quarter last year, Rs. Cr; EPS in Rs.)")

    print("  Time: " + ", ".join(f"{m} {timings[m]:.1f}s" for m in models))

    return {
        "mismatches": mismatches if fin_a and fin_b else None,
        "failed": [m for m in models if results[m] is None],
        "fin": fin_a,
    }

def summary_status(result: dict, compared: bool) -> str:
    """One SUMMARY line's status for a PDF."""
    if result["failed"]:
        return "failed: " + ", ".join(result["failed"])
    if compared and result["mismatches"] is not None:
        return f"{result['mismatches']} differing fields"
    fin = result.get("fin")
    if not fin:
        return "skipped: no results table found"
    return (f"read · {fin['basis']} · revenue {fmt(fin['revenue_from_operations'][0])} Cr · "
            f"PAT {fmt(fin['pat'][0])} Cr · quarter ended {fin['period_end'] or '?'}")

def usage_line(model: str) -> str:
    """Tokens a model used this run and, if its price is known, the cost."""
    used = pt.usage_totals.get(model)

    if not used or not used["calls"]:
        return f"  {model}: no token counts reported"

    line = f"  {model}: {used['calls']} calls, {used['in']:,} tokens in, {used['out']:,} out"
    if used["thinking"]:
        line += f" (of which {used['thinking']:,} thinking)"

    price = PRICES.get(model.split("/")[-1].lower())
    if price:
        cost = (used["in"] * price[0] + used["out"] * price[1]) / 1_000_000
        line += f"  ≈ ${cost:.4f}, ${cost / used['calls']:.5f} a call"

    served = pt.answered_by.get(model)
    line += "\n    answered by (as AICredits reports it): " + (", ".join(sorted(served)) if served else "not reported")
    return line

def main():
    parser = argparse.ArgumentParser(description="Run the extraction model on saved PDFs, or compare two models")
    parser.add_argument("folder", help="folder of result PDFs")
    parser.add_argument("--model-a", default=pt.EXTRACTION_MODEL)
    parser.add_argument("--model-b", help="a second model to compare with (none by default)")
    parser.add_argument("--effort", help="low / medium / high thinking for Claude 5.x (default: EXTRACTION_EFFORT)")
    args = parser.parse_args()

    if args.effort:
        pt.EXTRACTION_EFFORT = args.effort.strip().lower()

    # Keep the scanner's per-step logging out of the comparison table
    logging.getLogger(pt.__name__).setLevel(logging.WARNING)

    paths = sorted(glob.glob(os.path.join(args.folder, "*.pdf")))

    if not paths:
        print(f"No PDFs in {args.folder}")
        return

    summary = {path: compare_pdf(path, args.model_a, args.model_b) for path in paths}

    models = [args.model_a] + ([args.model_b] if args.model_b else [])
    print(f"\n{'=' * 100}\nSUMMARY  ({' vs '.join(models)})")

    for path, result in summary.items():
        print(f"  {os.path.basename(path):<50}{summary_status(result, bool(args.model_b))}")

    print("\nTOKENS")
    for model in models:
        print(usage_line(model))

if __name__ == "__main__":
    main()
