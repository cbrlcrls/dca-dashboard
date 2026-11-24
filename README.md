# DCA Decision Dashboard mirror

A read-only, public mirror of my "DCA Decision Dashboard" Google Sheet, so AI tools
that can't open Google links can still read it. **The Google Sheet is the single
source of truth.** This repo only copies its published CSV. Market data only, no
holdings or account values.

## Where to read it

Replace `cbrlcrls` with the GitHub username.

| What | Link |
|---|---|
| HTML table | `https://USERNAME.github.io/dca-dashboard/` |
| Clean CSV (one header row, one row per ticker, plus a Section column) | `https://USERNAME.github.io/dca-dashboard/dashboard.csv` |
| JSON (numbers as numbers, `n/a` as null) | `https://USERNAME.github.io/dca-dashboard/dashboard.json` |
| CSV exactly as Google published it | `https://USERNAME.github.io/dca-dashboard/source.csv` |
| Short guide for AI readers | `https://USERNAME.github.io/dca-dashboard/llms.txt` |

Every file shows two times: when the Sheet last recalculated, and when this mirror
last copied it. If they're far apart, the mirror is behind.

## How it works

- `.github/workflows/mirror.yml` runs every 15 minutes on weekdays during US market
  hours, plus once each weekday evening. You can also run it by hand from the
  **Actions** tab ("Run workflow").
- `scripts/mirror.py` downloads the published CSV, drops the title, blank and section
  rows into proper fields, and writes the files above.
- GitHub Pages serves the result. Pages caches files for about 10 minutes, so total lag
  behind the Sheet is usually 15 to 30 minutes.

## Safety checks

If the download looks wrong (a Google error page, missing header, fewer than 15 rows,
SPY/VIX/HYG missing, or **column headers that don't match**), the run fails and
nothing is published, so the last good version stays online. The header check is
also a privacy guard: if the published link ever pointed at a different tab, it
would refuse to mirror it.

## Common changes

- **Renamed or added a column in the Sheet?** Update the `COLUMNS` list near the top
  of `scripts/mirror.py`. The failed run's log shows the expected vs. found headers.
- **Changed the published link?** Update `SHEET_CSV_URL` in `.github/workflows/mirror.yml`.
- **Different schedule?** Edit the `cron` lines in the workflow. Times are UTC, and
  GitHub can start scheduled runs a few minutes late or occasionally skip one.

## Testing locally

```
python3 scripts/mirror.py --input some-saved-copy.csv --out site
```
