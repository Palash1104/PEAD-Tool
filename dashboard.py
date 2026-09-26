"""
PEAD Dashboard
==============
A local, read-only web dashboard for the PEAD scanner.

Put this file and dashboard.html in the same folder as pead_tool.py, then run:
    py dashboard.py                 -> opens http://127.0.0.1:8050
    py dashboard.py --no-browser
    py dashboard.py --port 8060

It only READS these files from this folder (it never writes anything):
    pead_results.csv        scored results
    seen.json               filings already handled
    processed_scrips.json   company-quarters already scored
    retry_counts.json       filings awaiting retry
    pead_tool.log           optional: scanner log (live status, timings, log tail)
    pead_tool.py            only to read PEAD_THRESHOLD

No extra packages needed. Standard library only.
The server listens on 127.0.0.1, so it is only reachable from this computer.
"""

import argparse
import csv
import json
import os
import re
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

RESULTS_CSV    = "pead_results.csv"
SEEN_FILE      = "seen.json"
PROCESSED_FILE = "processed_scrips.json"
RETRY_FILE     = "retry_counts.json"
LOG_FILE       = "pead_tool.log"
TOOL_FILE      = "pead_tool.py"
HTML_FILE      = "dashboard.html"

LOG_TAIL_BYTES    = 1_500_000   # only the end of a big log is read
LOG_LINES_SHOWN   = 60
DEFAULT_THRESHOLD = 35.0


def _p(name: str) -> str:
    return os.path.join(BASE_DIR, name)


# ── File readers ──────────────────────────────────────────────

def read_json(name, default):
    try:
        with open(_p(name), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def read_threshold() -> float:
    env = os.environ.get("PEAD_THRESHOLD")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    try:
        with open(_p(TOOL_FILE), "r", encoding="utf-8") as f:
            m = re.search(r"^PEAD_THRESHOLD\s*=\s*([\d.]+)", f.read(), re.M)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return DEFAULT_THRESHOLD


def read_results():
    """Return (rows, error). Rows are dicts of raw strings keyed by CSV header."""
    path = _p(RESULTS_CSV)
    if not os.path.exists(path):
        return [], None
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            rows = []
            for row in csv.DictReader(f):
                rows.append({
                    k.strip(): (v.strip() if isinstance(v, str) else v)
                    for k, v in row.items()
                    if k  # drops overflow columns (key None)
                })
        return rows, None
    except Exception as e:
        return [], f"Could not read {RESULTS_CSV}: {e}"


# ── Log parsing ───────────────────────────────────────────────

LINE_RE  = re.compile(r"^(?:(\d{4}-\d{2}-\d{2})\s+)?(\d{2}:\d{2}:\d{2})\s+([A-Z]+)\s+(.*)$")
STAGE_RE = re.compile(r"(download|page scan|OCR|model)\s+([\d.]+)s")
DELAY_RE = re.compile(r"exchange→(alert|scored)\s+((?:\d+h\s*)?(?:\d+m\s*)?(?:\d+s)?)")
FETCH_RE = re.compile(r"\b(BSE|NSE):\s+(fetch FAILED.*|\d+ new announcements.*|\d+ announcements fetched.*)")
QUEUE_RE = re.compile(r"checker queue:\s*(\d+)")


def duration_to_seconds(text: str):
    parts = re.findall(r"(\d+)\s*([hms])", text or "")
    if not parts:
        return None
    return sum(int(v) * {"h": 3600, "m": 60, "s": 1}[u] for v, u in parts)


def read_log():
    path = _p(LOG_FILE)
    if not os.path.exists(path):
        return {"available": False}
    try:
        size  = os.path.getsize(path)
        mtime = os.path.getmtime(path)
        with open(path, "rb") as f:
            head = f.read(2)
            utf16 = head in (b"\xff\xfe", b"\xfe\xff")  # PowerShell Tee-Object writes UTF-16
            start = max(0, size - LOG_TAIL_BYTES)
            if utf16:
                start = max(2, start - (start % 2))
            f.seek(start)
            raw = f.read()
        if utf16:
            enc = "utf-16-le" if head == b"\xff\xfe" else "utf-16-be"
            text = raw.decode(enc, errors="replace")
        else:
            text = raw.decode("utf-8", errors="replace")
        if start > 0 and "\n" in text:
            text = text.split("\n", 1)[1]  # drop the partial first line
    except Exception as e:
        return {"available": False, "error": str(e)}

    lines = [ln.rstrip("\r") for ln in text.split("\n") if ln.strip()]

    timings, exchanges = [], {}
    queue, context = None, None

    for line in lines:
        m   = LINE_RE.match(line)
        t   = m.group(2) if m else None
        msg = m.group(4) if m else line

        if "→" in msg and "exchange→" not in msg:
            context = msg.strip()[:140]

        if "⏱" in msg:
            entry = {"time": t, "context": context}
            for stage, val in STAGE_RE.findall(msg):
                entry[stage.replace(" ", "_").lower()] = float(val)
            d = DELAY_RE.search(msg)
            if d:
                entry["kind"]  = d.group(1)
                entry["delay"] = duration_to_seconds(d.group(2))
            timings.append(entry)

        f = FETCH_RE.search(msg)
        if f:
            exchanges[f.group(1)] = {
                "time": t,
                "message": f.group(2).strip(),
                "ok": "FAILED" not in f.group(2),
            }
        if "Access Denied" in msg:
            for ex in ("BSE", "NSE"):
                if ex in msg:
                    exchanges[ex] = {"time": t, "message": "Access Denied", "ok": False}

        q = QUEUE_RE.search(msg)
        if q:
            queue = int(q.group(1))

    return {
        "available": True,
        "mtime": mtime,
        "age_sec": max(0.0, time.time() - mtime),
        "lines": lines[-LOG_LINES_SHOWN:],
        "timings": timings[-200:],
        "exchanges": exchanges,
        "checker_queue": queue,
    }


# ── Payload ───────────────────────────────────────────────────

def _count(obj) -> int:
    return len(obj) if isinstance(obj, (list, dict)) else 0


def build_payload():
    rows, csv_error = read_results()
    seen      = read_json(SEEN_FILE, [])
    processed = read_json(PROCESSED_FILE, [])
    retry     = read_json(RETRY_FILE, {})

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "threshold":    read_threshold(),
        "results":      rows,
        "csv_error":    csv_error,
        "seen_count":   _count(seen),
        "processed":    list(processed) if isinstance(processed, (list, dict)) else [],
        "retry_pending": _count(retry),
        "log":          read_log(),
    }


# ── HTTP server ───────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        route = self.path.split("?", 1)[0]

        if route in ("/", "/index.html"):
            try:
                with open(_p(HTML_FILE), "rb") as f:
                    body = f.read()
            except FileNotFoundError:
                self._send(404, b"dashboard.html not found next to dashboard.py",
                           "text/plain; charset=utf-8")
                return
            self._send(200, body, "text/html; charset=utf-8")

        elif route == "/api/data":
            try:
                body = json.dumps(build_payload(), ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode("utf-8")
                self._send(500, body, "application/json; charset=utf-8")

        elif route == "/favicon.ico":
            self._send(204, b"", "text/plain")

        else:
            self._send(404, b"Not found", "text/plain; charset=utf-8")

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, *args):
        pass  # keep the console quiet


def main():
    parser = argparse.ArgumentParser(description="PEAD scanner dashboard")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PEAD_DASHBOARD_PORT", 8050)))
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"PEAD dashboard running at {url}  (Ctrl+C to stop)")
    print(f"Reading data from: {BASE_DIR}")
    if not os.path.exists(_p(LOG_FILE)):
        print(f"Note: {LOG_FILE} not found, so live status and timings will be hidden.")

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
