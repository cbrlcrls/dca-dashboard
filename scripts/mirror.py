
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
from datetime import datetime, timezone, time as dtime
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

SCHEMA_VERSION = "2.0"
SOURCE_TIMEZONE = "America/Los_Angeles"   # the Google Sheet's time zone
MARKET_TIMEZONE = "America/New_York"
MAX_QUOTE_AGE_MIN = 30                    # flag older quotes while the market is open
MAX_CBOE_QUOTE_AGE_MIN = 60               # Cboe index quotes (VIX, VIX3M, TNX) update less often in Google Finance
MAX_TREND_AGE_MIN = 30                    # flag older trend copies while the market is open

# NYSE full-day holidays and 1:00 pm early closes (source: nyse.com Holidays & Trading Hours).
# Add the next year's dates each December.
NYSE_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}
NYSE_EARLY_CLOSES = {"2026-11-27", "2026-12-24", "2027-11-26"}
LOCAL_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")


def zone(name):
    try:
        return ZoneInfo(name)
    except Exception:
        return None


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
    trend_as_of = None
    header_index = None

    for i, row in enumerate(rows):
        cells = [c.strip() for c in row]
        first = cells[0] if cells else ""
        if i == 0 and first:
            title = first
        elif norm(first) == "last updated":
            last_updated = cells[1] if len(cells) > 1 else None
            last_updated_note = cells[2] if len(cells) > 2 and cells[2] else None
            if len(cells) > 4 and norm(cells[3]) == "trend values copied":
                trend_as_of = cells[4] or None
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
        "trend_as_of": trend_as_of,
        "units_note": units_note,
        "sections": sections,
        "records": records,
        "labels": LABELS[:n],
        "keys": KEYS[:n],
    }


# ----------------------------------------------------------------------------- write

def mirror_times(now):
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


def iso_utc(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z") if dt else None


def local_to_utc(text, tz_name):
    """'2026-11-24 15:55' in the given zone -> aware UTC datetime, or None."""
    tz = zone(tz_name)
    if not tz or not isinstance(text, str) or not LOCAL_TIME_RE.match(text.strip()):
        return None
    return datetime.strptime(text.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=tz).astimezone(timezone.utc)


def market_session(now_utc):
    """Session of the US stock market at now_utc, using the NYSE calendar above."""
    tz = zone(MARKET_TIMEZONE)
    if not tz:
        return "unknown", False
    ny = now_utc.astimezone(tz)
    day = ny.date().isoformat()
    early = day in NYSE_EARLY_CLOSES
    if ny.weekday() >= 5:
        return "weekend", False
    if day in NYSE_HOLIDAYS:
        return "holiday", False
    close = dtime(13, 0) if early else dtime(16, 0)
    t = ny.time()
    if t < dtime(4, 0):
        return "closed", early
    if t < dtime(9, 30):
        return "premarket", early
    if t < close:
        return "open", early
    if t < dtime(20, 0):
        return "after_hours", early
    return "closed", early


def quote_zone(data_symbol):
    """Exchange time zone of a row's last trade time, or None for calculated/daily rows."""
    if not isinstance(data_symbol, str) or ":" not in data_symbol:
        return None
    return "America/Chicago" if data_symbol.upper().startswith("INDEXCBOE:") else MARKET_TIMEZONE


def is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def quality_flags(rows, session, now_utc, trend_utc):
    flags = []
    for r in rows:
        t = r.get("ticker")
        status = r.get("status")
        if isinstance(status, str) and not status.upper().startswith("OK"):
            flags.append({"ticker": t, "issue": f"Status is {status}"})
        if quote_zone(r.get("data_symbol")) and r.get("quote_timestamp_utc") is None:
            flags.append({"ticker": t, "issue": "No trade timestamp in the published data, so quote freshness cannot be verified"})
        age = r.get("quote_age_min")
        limit = MAX_CBOE_QUOTE_AGE_MIN if quote_zone(r.get("data_symbol")) == "America/Chicago" else MAX_QUOTE_AGE_MIN
        if session == "open" and is_num(age) and age > limit:
            flags.append({"ticker": t, "issue": f"Quote is {age} minutes old during market hours"})
        p, pc, dc, pct = r.get("price"), r.get("previous_close"), r.get("day_change"), r.get("day_change_pct")
        if is_num(p) and is_num(pc) and is_num(dc) and abs(p - pc - dc) > max(0.011, abs(p) * 0.0001):
            flags.append({"ticker": t, "issue": f"Day change {dc} does not match price {p} minus previous close {pc}"})
        if is_num(p) and is_num(pc) and is_num(pct) and pc != 0 and abs((p / pc - 1) * 100 - pct) > 0.02:
            flags.append({"ticker": t, "issue": f"Day change % {pct} does not match price and previous close"})
    if trend_utc is None:
        flags.append({"ticker": None, "issue": "Trend copy time unavailable, so trend columns may be stale"})
    elif session == "open":
        trend_age = int((now_utc - trend_utc).total_seconds() // 60)
        if trend_age > MAX_TREND_AGE_MIN:
            flags.append({"ticker": None, "issue": f"Trend values were copied {trend_age} minutes ago during market hours"})
    return flags


def write_json(data: dict, path: Path, source_url: str, mirrored_utc: str, now_utc=None):
    rows = []
    for r in data["records"]:
        obj = {"section": r["section"]}
        for key, value in zip(data["keys"], r["cells"]):
            obj[key] = to_json_value(key, value)
        qz = quote_zone(obj.get("data_symbol"))
        qt = local_to_utc(obj.get("last_trade_time"), qz) if qz else None
        obj["quote_timestamp_utc"] = iso_utc(qt)
        obj["quote_age_min"] = max(0, int((now_utc - qt).total_seconds() // 60)) if qt else None
        rows.append(obj)
    session, early = market_session(now_utc)
    trend_utc = local_to_utc(data.get("trend_as_of"), SOURCE_TIMEZONE)
    flags = quality_flags(rows, session, now_utc, trend_utc)
    doc = {
        "schema_version": SCHEMA_VERSION,
        "title": data["title"],
        "description": "Read-only mirror of a Google Sheet used to guide dollar-cost averaging. Market data only.",
        "source_last_updated": data["last_updated"],
        "source_last_updated_note": data["last_updated_note"],
        "source_last_updated_utc": iso_utc(local_to_utc(data["last_updated"], SOURCE_TIMEZONE)),
        "source_timezone": SOURCE_TIMEZONE,
        "mirrored_at_utc": mirrored_utc,
        "market_timezone": MARKET_TIMEZONE,
        "market_session": session,
        "early_close_today": early,
        "trend_as_of": data.get("trend_as_of"),
        "trend_as_of_utc": iso_utc(trend_utc),
        "data_quality": {
            "status": "OK" if not flags else "CHECK",
            "flag_count": len(flags),
            "flags": flags,
        },
        "source_csv_url": source_url,
        "units_note": data["units_note"],
        "conventions": {
            "null": "n/a in the Sheet (not applicable) or blank",
            "percent_fields": "day_change_pct and pct_below_52w_high are percent values (-0.85 means -0.85%)",
            "yields": "US2Y, US10Y and spread levels and day_change are in percentage points; day_change_bps is basis points",
            "last_trade_time": "exchange local time (Cboe indexes in US Central)",
            "status": "OK, Stale (no trade in 4+ days) or Unavailable",
            "trend_changes": "change_5d and change_20d compare the current level with the close 5 and 20 trading days earlier: percent for prices, basis points for yields",
            "quote_timestamp_utc": "last_trade_time converted to UTC (NYSE and Nasdaq rows from US Eastern, Cboe rows from US Central); null when the published data has no trade time",
            "quote_age_min": "minutes between the trade time and mirrored_at_utc",
            "market_session": "open, premarket, after_hours, closed, weekend or holiday (US Eastern, NYSE calendar)",
            "data_quality": "flags list stale or unverifiable quotes, non-OK statuses, prices that do not reconcile, and stale trend copies; status OK means no flags",
            "decision_framework": "Section 5 rows hold text readings in the price field (for example HOLD or DEVELOPING); their notes explain the reasoning",
        },
        "columns": [{"key": k, "label": l} for l, k in zip(data["labels"], data["keys"])],
        "sections": data["sections"],
        "rows": rows,
    }
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return doc


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


def write_html(data: dict, path: Path, source_url: str, mirrored_utc_text: str, mirrored_local_text, doc=None):
    e = html.escape
    mirrored = mirrored_utc_text + (f" ({mirrored_local_text})" if mirrored_local_text else "")
    updated = data["last_updated"] or "unknown"
    if data["last_updated_note"]:
        updated += f" {data['last_updated_note']}"

    dq = (doc or {}).get("data_quality", {})
    quality_text = "OK" if dq.get("status") == "OK" else f"{dq.get('flag_count', 0)} item(s) to check"
    flag_items = "".join(f"<li>{e((f.get('ticker') or 'General') + ': ' + f['issue'])}</li>" for f in dq.get("flags", []))
    flag_list = f'\n<ul class="flags">{flag_items}</ul>' if flag_items else ""
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
  ul.flags {{ margin: 4px 0 8px; color: #b9770e; }}
</style>
</head>
<body>
<h1>{e(data['title'])}</h1>
<dl class="meta">
  <dt>Sheet last updated:</dt><dd>{e(updated)}</dd>
  <dt>Mirrored:</dt><dd>{e(mirrored)}</dd>
  <dt>Trend values copied:</dt><dd>{e((data.get('trend_as_of') or 'unknown') + ' (sheet time zone)')}</dd>
  <dt>Market session:</dt><dd>{e((doc or {}).get('market_session', 'unknown'))}</dd>
  <dt>Data quality:</dt><dd>{e(quality_text)}</dd>
</dl>{flag_list}
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
- dashboard.json: same data, numbers as numbers, n/a as null, with units
  conventions, quote timestamps in UTC, market session and data-quality flags
  (schema version {SCHEMA_VERSION})
- source.csv: the CSV exactly as Google published it

Section 5 (Decision framework) gives the market opportunity, deployment
recommendation and proposed tranche, with reasons in the notes. Section 6 adds
trend context. The 5-day and 20-day change columns show direction.

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
    ap.add_argument("--now", help="Pretend the current time is this UTC time, e.g. 2026-11-24T19:00:00Z (for testing)")
    args = ap.parse_args()
    if not args.url and not args.input:
        ap.error("give --url or --input")

    text = Path(args.input).read_text(encoding="utf-8-sig") if args.input else fetch(args.url)
    data = parse(text)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    source_url = args.url or "(local file)"
    now = (datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now
           else datetime.now(timezone.utc)).replace(microsecond=0)
    mirrored_iso, mirrored_utc_text, mirrored_local_text = mirror_times(now)

    (out / "source.csv").write_text(text, encoding="utf-8")
    write_clean_csv(data, out / "dashboard.csv")
    doc = write_json(data, out / "dashboard.json", source_url, mirrored_iso, now)
    write_html(data, out / "index.html", source_url, mirrored_utc_text, mirrored_local_text, doc)
    write_llms_txt(data, out / "llms.txt", mirrored_iso)

    print(f"OK: {len(data['records'])} rows in {len(data['sections'])} sections, "
          f"sheet updated {data['last_updated']}, market {doc['market_session']}, "
          f"data quality {doc['data_quality']['status']} ({doc['data_quality']['flag_count']} flags), written to {out}/")


if __name__ == "__main__":
    main()
