
#!/usr/bin/env python3
"""
Mirror the DCA Decision Dashboard (published Google Sheet CSV) into static files
for GitHub Pages.

The Google Sheet is the single source of truth. This script only reads its
published CSV and writes:

  site/index.html      plain HTML table (easy for people and AIs to read)
  site/dashboard.csv   clean CSV: one header row, one row per ticker, plus a Section column
  site/dashboard.json  same data as JSON, numbers as numbers, "n/a" as null
  site/source.csv      the CSV exactly as Google published it
  site/llms.txt        short plain-text guide to the files above, for AI readers

Safety checks: if the download looks wrong (an HTML error page, missing header,
unexpected columns, too few rows), the script exits with an error and nothing is
published, so the last good version stays online. The column check also acts as
a privacy guard: if the published link ever pointed at a different tab, the
headers would not match and nothing would be mirrored.

Uses only the Python standard library.
"""

import argparse
import csv
import html
import io
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    PACIFIC = ZoneInfo("America/Los_Angeles")
except Exception:  # tz database missing; fall back to UTC only
    PACIFIC = None

# The columns of the Dashboard tab, in order, with JSON key names.
# The first 16 (A-P) are required. Columns after that are optional and are
# mirrored when present, so new columns can be added to the Sheet at the end.
# If you rename or add a column in the Sheet, update this list to match.
COLUMNS = [
    ("Ticker", "ticker"),
    ("What we monitor", "what_we_monitor"),
    ("Data symbol", "data_symbol"),
    ("Price / level", "price"),
    ("Previous close", "previous_close"),
    ("Day change", "day_change"),
    ("Day change %", "day_change_pct"),
    ("Day change (bps)", "day_change_bps"),
    ("Intraday high", "intraday_high"),
    ("Intraday low", "intraday_low"),
    ("52-week high", "high_52w"),
    ("% below 52-week high", "pct_below_52w_high"),
    ("Last trade time", "last_trade_time"),
    ("Data delay (min)", "data_delay_min"),
    ("Status", "status"),
    ("Notes", "notes"),
    ("5-day change", "change_5d"),
    ("20-day change", "change_20d"),
]
REQUIRED_COLUMNS = 16
LABELS = [label for label, _ in COLUMNS]
KEYS = [key for _, key in COLUMNS]

# Columns that hold numbers. Text in these (like "Not reported") is kept as text.
NUMERIC_KEYS = {
    "price", "previous_close", "day_change", "day_change_pct", "day_change_bps",
    "intraday_high", "intraday_low", "high_52w", "pct_below_52w_high", "data_delay_min",
    "change_5d", "change_20d",
}
RIGHT_ALIGN_KEYS = NUMERIC_KEYS

MIN_DATA_ROWS = 15            # the Sheet currently has 21; fewer than 15 means something broke
REQUIRED_TICKERS = {"SPY", "VIX", "HYG"}
SECTION_RE = re.compile(r"^\d+\.\s")   # "1. Core equity market indicators"
NULL_VALUES = {"", "n/a", "na", "#n/a"}


# ----------------------------------------------------------------------------- fetch

def fetch(url: str, attempts: int = 3) -> str:
    """Download the published CSV, following Google's redirect. Retries on failure."""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "dca-dashboard-mirror/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
            return raw.decode("utf-8-sig")
        except Exception as exc:  # network hiccup, Google 5xx, etc.
            last_error = exc
            print(f"Fetch attempt {attempt} failed: {exc}", file=sys.stderr)
            if attempt < attempts:
                time.sleep(10 * attempt)
    raise SystemExit(f"Could not download the published CSV after {attempts} tries: {last_error}")


# ----------------------------------------------------------------------------- parse

def norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def to_number(value: str):
    """'-8.17' -> -8.17, '0' -> 0, 'n/a' -> None, 'Not reported' -> 'Not reported'."""
    v = value.strip()
    if v.lower() in NULL_VALUES:
        return None
    cleaned = v.replace(",", "").replace("%", "").replace("$", "")
    try:
        num = float(cleaned)
    except ValueError:
        return v
    return int(num) if re.fullmatch(r"-?\d+", cleaned) else num


def to_json_value(key: str, value: str):
    if key in NUMERIC_KEYS:
        return to_number(value)
    v = value.strip()
    return None if v.lower() in NULL_VALUES else v


def parse(text: str) -> dict:
    if text.lstrip().startswith("<") or "<html" in text[:2000].lower():
        raise SystemExit("Download was an HTML page, not CSV (Google error or sign-in page). Not publishing.")

    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise SystemExit("Downloaded CSV is empty. Not publishing.")

    title, last_updated, last_updated_note, units_note = "DCA Decision Dashboard", None, None, None
    header_index = None

    for i, row in enumerate(rows):
        cells = [c.strip() for c in row]
        first = cells[0] if cells else ""
        if i == 0 and first:
            title = first
        elif norm(first) == "last updated":
            last_updated = cells[1] if len(cells) > 1 else None
            last_updated_note = cells[2] if len(cells) > 2 and cells[2] else None
        elif first.lower().startswith("units:"):
            units_note = first
        elif norm(first) == "ticker":
            header_index = i
            break

    if header_index is None:
        raise SystemExit("Could not find the header row (a row starting with 'Ticker'). Not publishing.")

    header = [c.strip() for c in rows[header_index]]
    while header and not header[-1]:
        header.pop()
    n = len(header)
    if not (REQUIRED_COLUMNS <= n <= len(LABELS)) or [norm(h) for h in header] != [norm(l) for l in LABELS[:n]]:
        raise SystemExit(
            "Column headers do not match what the mirror expects, so nothing was published.\n"
            f"  Expected: {LABELS[:max(n, REQUIRED_COLUMNS)]}\n  Found:    {header}\n"
            "If you changed columns in the Sheet on purpose, update COLUMNS in scripts/mirror.py."
        )

    sections, records = [], []
    current_section = None
    for row in rows[header_index + 1:]:
        cells = [c.strip() for c in row]
        while cells and not cells[-1]:
            cells.pop()
        if not cells:
            continue                                   # blank spacer row
        if len(cells) == 1 and SECTION_RE.match(cells[0]):
            current_section = cells[0]                 # section label row
            sections.append(current_section)
            continue
        if len(cells) > n:
            raise SystemExit(f"Row has more columns than expected: {cells}. Not publishing.")
        cells += [""] * (n - len(cells))
        records.append({"section": current_section, "cells": cells})

    if len(records) < MIN_DATA_ROWS:
        raise SystemExit(f"Only {len(records)} data rows found (expected at least {MIN_DATA_ROWS}). Not publishing.")
    tickers = {r["cells"][0] for r in records}
    missing = REQUIRED_TICKERS - tickers
    if missing:
        raise SystemExit(f"Required tickers missing: {sorted(missing)}. Not publishing.")

    return {
        "title": title,
        "last_updated": last_updated,
        "last_updated_note": last_updated_note,
        "units_note": units_note,
        "sections": sections,
        "records": records,
        "labels": LABELS[:n],
        "keys": KEYS[:n],
    }


# ----------------------------------------------------------------------------- write

def mirror_times():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    utc_iso = now.isoformat().replace("+00:00", "Z")
    utc_text = now.strftime("%Y-%m-%d %H:%M UTC")
    if PACIFIC:
        local = now.astimezone(PACIFIC)
        local_text = local.strftime("%Y-%m-%d %H:%M ") + (local.tzname() or "Pacific")
    else:
        local_text = None
    return utc_iso, utc_text, local_text


def write_clean_csv(data: dict, path: Path):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["Section"] + data["labels"])
    for r in data["records"]:
        w.writerow([r["section"] or ""] + r["cells"])
    path.write_text(buf.getvalue(), encoding="utf-8")


def write_json(data: dict, path: Path, source_url: str, mirrored_utc: str):
    rows = []
    for r in data["records"]:
        obj = {"section": r["section"]}
        for key, value in zip(data["keys"], r["cells"]):
            obj[key] = to_json_value(key, value)
        rows.append(obj)
    doc = {
        "title": data["title"],
        "description": "Read-only mirror of a Google Sheet used to guide dollar-cost averaging. Market data only.",
        "source_last_updated": data["last_updated"],
        "source_last_updated_note": data["last_updated_note"],
        "mirrored_at_utc": mirrored_utc,
        "source_csv_url": source_url,
        "units_note": data["units_note"],
        "conventions": {
            "null": "n/a in the Sheet (not applicable) or blank",
            "percent_fields": "day_change_pct and pct_below_52w_high are percent values (-0.85 means -0.85%)",
            "yields": "US2Y, US10Y and spread levels and day_change are in percentage points; day_change_bps is basis points",
            "last_trade_time": "exchange local time (Cboe indexes in US Central)",
            "status": "OK, Stale (no trade in 4+ days) or Unavailable",
            "trend_changes": "change_5d and change_20d compare the current level with the close 5 and 20 trading days earlier: percent for prices, basis points for yields",
            "decision_framework": "Section 5 rows hold text readings in the price field (for example HOLD or DEVELOPING); their notes explain the reasoning",
        },
        "columns": [{"key": k, "label": l} for l, k in zip(data["labels"], data["keys"])],
        "sections": data["sections"],
        "rows": rows,
    }
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def status_class(status: str) -> str:
    s = status.strip().lower()
    if s.startswith("ok"):
        return "ok"
    if s.startswith("stale"):
        return "stale"
    if s.startswith("unavail"):
        return "unavailable"
    return ""


def change_class(key: str, value: str) -> str:
    if key not in {"day_change", "day_change_pct", "day_change_bps", "change_5d", "change_20d"}:
        return ""
    n = to_number(value)
    if isinstance(n, (int, float)):
        return "neg" if n < 0 else ("pos" if n > 0 else "")
    return ""


def write_html(data: dict, path: Path, source_url: str, mirrored_utc_text: str, mirrored_local_text):
    e = html.escape
    mirrored = mirrored_utc_text + (f" ({mirrored_local_text})" if mirrored_local_text else "")
    updated = data["last_updated"] or "unknown"
    if data["last_updated_note"]:
        updated += f" {data['last_updated_note']}"

    head_cells = "".join(f'<th scope="col">{e(l)}</th>' for l in data["labels"])

    # Group records by section, keeping order.
    groups = []
    for r in data["records"]:
        if not groups or groups[-1][0] != r["section"]:
            groups.append((r["section"], []))
        groups[-1][1].append(r)

    bodies = []
    for section, recs in groups:
        lines = ["<tbody>"]
        if section:
            lines.append(f'<tr class="section"><th colspan="{len(data['labels'])}" scope="colgroup">{e(section)}</th></tr>')
        for r in recs:
            tds = []
            for i, (key, value) in enumerate(zip(data["keys"], r["cells"])):
                classes = []
                if key in RIGHT_ALIGN_KEYS and not isinstance(to_number(value), str):
                    classes.append("num")
                if key in ("what_we_monitor", "notes", "last_trade_time"):
                    classes.append(key.replace("_", "-"))
                cc = change_class(key, value)
                if cc:
                    classes.append(cc)
                if key == "status":
                    sc = status_class(value)
                    if sc:
                        classes.append("status " + sc)
                attr = f' class="{" ".join(classes)}"' if classes else ""
                tag = "th" if i == 0 else "td"
                scope = ' scope="row"' if i == 0 else ""
                tds.append(f"<{tag}{scope}{attr}>{e(value)}</{tag}>")
            lines.append("<tr>" + "".join(tds) + "</tr>")
        lines.append("</tbody>")
        bodies.append("\n".join(lines))

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(data['title'])}</title>
<meta name="description" content="Read-only mirror of the DCA Decision Dashboard Google Sheet. Market data only.">
<link rel="alternate" type="text/csv" href="dashboard.csv">
<link rel="alternate" type="application/json" href="dashboard.json">
<style>
  :root {{ color-scheme: light dark; --line: #8884; --muted: #777; }}
  body {{ font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; margin: 24px; }}
  h1 {{ font-size: 22px; margin: 0 0 6px; }}
  .meta {{ margin: 0 0 4px; }}
  .meta dt {{ display: inline; font-weight: 600; }}
  .meta dd {{ display: inline; margin: 0 16px 0 4px; }}
  .units, .footnote {{ color: var(--muted); max-width: 1100px; }}
  .links a {{ margin-right: 14px; }}
  .wrap {{ overflow-x: auto; }}
  table {{ border-collapse: collapse; margin-top: 14px; font-variant-numeric: tabular-nums; }}
  th, td {{ border-bottom: 1px solid var(--line); padding: 5px 9px; text-align: left; vertical-align: top; }}
  thead th {{ position: sticky; top: 0; background: Canvas; font-size: 12px; border-bottom: 2px solid var(--line); }}
  tbody th[scope=row] {{ white-space: nowrap; }}
  tr.section th {{ padding-top: 16px; font-size: 13px; text-transform: none; background: #8881; }}
  td.num {{ text-align: right; white-space: nowrap; }}
  td.neg {{ color: #c0392b; }}
  td.pos {{ color: #1e8449; }}
  td.what-we-monitor {{ min-width: 190px; }}
  td.notes {{ min-width: 300px; }}
  td.last-trade-time {{ white-space: nowrap; }}
  td.status {{ font-weight: 600; white-space: nowrap; }}
  td.status.ok {{ color: #1e8449; }}
  td.status.stale {{ color: #b9770e; }}
  td.status.unavailable {{ color: #c0392b; }}
</style>
</head>
<body>
<h1>{e(data['title'])}</h1>
<dl class="meta">
  <dt>Sheet last updated:</dt><dd>{e(updated)}</dd>
  <dt>Mirrored:</dt><dd>{e(mirrored)}</dd>
</dl>
<p class="units">{e(data['units_note'] or '')}</p>
<p class="links">
  <a href="dashboard.csv">Clean CSV</a>
  <a href="dashboard.json">JSON</a>
  <a href="source.csv">Original CSV as published</a>
  <a href="{e(source_url)}">Google Sheet (published CSV)</a>
</p>
<div class="wrap">
<table>
<caption class="footnote" style="caption-side: bottom; text-align: left; padding-top: 10px;">
  Read-only mirror. The Google Sheet is the source of truth; this page is refreshed about every 15 minutes during US market hours and can lag the Sheet. Market data only, no holdings.
</caption>
<thead><tr>{head_cells}</tr></thead>
{chr(10).join(bodies)}
</table>
</div>
</body>
</html>
"""
    path.write_text(page, encoding="utf-8")


def write_llms_txt(data: dict, path: Path, mirrored_utc: str):
    tickers = ", ".join(r["cells"][0] for r in data["records"])
    text = f"""# {data['title']} (read-only mirror)

Market-data dashboard used to guide dollar-cost averaging decisions.
Source of truth is a Google Sheet; this site mirrors its published CSV about
every 15 minutes during US market hours.

Sheet last updated: {data['last_updated'] or 'unknown'} {data['last_updated_note'] or ''}
Mirrored at (UTC): {mirrored_utc}

Files:
- index.html: HTML table, grouped by section
- dashboard.csv: one header row, one row per ticker, first column is Section
- dashboard.json: same data, numbers as numbers, n/a as null, with units conventions

Section 5 (Decision framework) gives the market opportunity, deployment
recommendation and proposed tranche, with reasons in the notes. Section 6 adds
trend context. The 5-day and 20-day change columns show direction.
- source.csv: the CSV exactly as Google published it

Rows: {tickers}

{data['units_note'] or ''}
"""
    path.write_text(text, encoding="utf-8")


# ----------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="Published CSV link of the Dashboard tab")
    ap.add_argument("--input", help="Read a local CSV file instead of downloading (for testing)")
    ap.add_argument("--out", default="site", help="Output folder (default: site)")
    args = ap.parse_args()
    if not args.url and not args.input:
        ap.error("give --url or --input")

    text = Path(args.input).read_text(encoding="utf-8-sig") if args.input else fetch(args.url)
    data = parse(text)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    source_url = args.url or "(local file)"
    mirrored_iso, mirrored_utc_text, mirrored_local_text = mirror_times()

    (out / "source.csv").write_text(text, encoding="utf-8")
    write_clean_csv(data, out / "dashboard.csv")
    write_json(data, out / "dashboard.json", source_url, mirrored_iso)
    write_html(data, out / "index.html", source_url, mirrored_utc_text, mirrored_local_text)
    write_llms_txt(data, out / "llms.txt", mirrored_iso)

    print(f"OK: {len(data['records'])} rows in {len(data['sections'])} sections, "
          f"sheet updated {data['last_updated']}, written to {out}/")


if __name__ == "__main__":
    main()
