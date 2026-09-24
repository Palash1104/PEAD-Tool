"""
Compare two extraction models on a folder of saved result PDFs.

Each PDF's result pages are picked once (same logic and OCR as the scanner),
then sent to both models. Values are shown after unit conversion to Rs. Cr,
so a wrong unit shows up as a 100x / 10x mismatch.

  python compare_models.py pdfs/ --model-b google/gemini-2.5-flash
  python compare_models.py pdfs/ --model-a anthropic/claude-haiku-4-5 --model-b <other>

--model-a defaults to EXTRACTION_MODEL from .env. Model names are whatever
AICredits calls them.
"""
import argparse
import glob
import logging
import os
import time

import pead_tool as pt

META_KEYS = ["basis", "unit", "period_end"]

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

def compare_pdf(path: str, model_a: str, model_b: str) -> dict:
    print(f"\n{'=' * 100}\n{os.path.basename(path)}")

    with open(path, "rb") as f:
        pdf_bytes = f.read()

    text, reason = pt.get_result_text(pdf_bytes)

    if not text:
        print(f"  Skipped: {reason}")
        return {"mismatches": None}

    print(f"  Pages: {reason}, {len(text)} chars sent to each model")

    results, timings = {}, {}

    for model in (model_a, model_b):
        started = time.time()
        results[model] = pt.extract_from_text(text, model)
        timings[model] = time.time() - started

    fin_a, fin_b = results[model_a], results[model_b]

    print(f"\n  {'field':<26}{model_a[:34]:>36}{model_b[:34]:>36}")
    print(f"  {'-' * 98}")

    for model in (model_a, model_b):
        if results[model] is None:
            print(f"  {model}: EXTRACTION FAILED")

    mismatches = 0

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

    print(
        f"  Time: {model_a} {timings[model_a]:.1f}s, "
        f"{model_b} {timings[model_b]:.1f}s"
    )

    return {
        "mismatches": mismatches if fin_a and fin_b else None,
        "failed": [m for m in (model_a, model_b) if results[m] is None],
    }

def main():
    parser = argparse.ArgumentParser(description="Compare two extraction models on saved PDFs")
    parser.add_argument("folder", help="folder of result PDFs")
    parser.add_argument("--model-a", default=pt.EXTRACTION_MODEL)
    parser.add_argument("--model-b", required=True)
    args = parser.parse_args()

    # Keep the scanner's per-step logging out of the comparison table
    logging.getLogger(pt.__name__).setLevel(logging.WARNING)

    paths = sorted(glob.glob(os.path.join(args.folder, "*.pdf")))

    if not paths:
        print(f"No PDFs in {args.folder}")
        return

    summary = {path: compare_pdf(path, args.model_a, args.model_b) for path in paths}

    print(f"\n{'=' * 100}\nSUMMARY  ({args.model_a} vs {args.model_b})")

    for path, result in summary.items():
        name = os.path.basename(path)
        if result["mismatches"] is None:
            status = "failed: " + ", ".join(result.get("failed") or ["no results table found"])
        else:
            status = f"{result['mismatches']} differing fields"
        print(f"  {name:<50}{status}")

if __name__ == "__main__":
    main()
