# cfa — equity research data

`scraper/edgar_scraper.py` downloads one company's SEC EDGAR filings and earnings
call transcripts so they're ready to use in an equity research report.

## Setup

```bash
pip install -r scraper/requirements.txt
export SEC_USER_AGENT="Your Name you@email.com"   # SEC requires a name + email
export ALPHAVANTAGE_API_KEY=your_key              # optional; free key at alphavantage.co
```

## Usage

```bash
# Preview what will be downloaded
python scraper/edgar_scraper.py GRMN --years 10 --dry-run

# Download everything for the last 10 years into data/GRMN/
python scraper/edgar_scraper.py GRMN --years 10

# Another company, only 10-K/10-Q and earnings, from a fixed date, text only
python scraper/edgar_scraper.py AAPL --since 2020-01-01 --groups reports,earnings --format text
```

| Option | Meaning |
|---|---|
| `--years N` / `--since YYYY-MM-DD` | How far back to go (default 5 years) |
| `--groups` | Comma list of `reports, earnings, 8k, proxy, insider, ownership, transcripts` (default: all) |
| `--format` | `both` (original + `.txt`, default), `original`, or `text` |
| `--out DIR` | Output root (default `data/`) |
| `--cik N` | Use a CIK directly instead of looking up the ticker |
| `--dry-run` | List matching filings without downloading |

## What each group contains

| Folder | Forms |
|---|---|
| `reports/` | 10-K, 10-Q (and amendments; 20-F/40-F for foreign filers) |
| `earnings/` | 8-Ks reporting Item 2.02 + their EX-99 press-release exhibits |
| `8k/` | All other 8-Ks (exec changes, votes, buybacks, M&A, ...) |
| `proxy/` | DEF 14A / DEFA14A proxy statements (exec pay, governance) |
| `insider/` | Forms 3/4/5 (insider holdings and trades) |
| `ownership/` | Schedule 13D/13G (5%+ holders) |
| `transcripts/` | Earnings call transcripts from Alpha Vantage (`.json` + readable `.md`) |

Each folder holds files named `<filing date>_<form>_<accession>.<ext>`, each with a
clean `.txt` copy next to it. `manifest.csv` lists every document with its SEC URL.

## Notes

- Re-running is safe: files already on disk are skipped. That makes it easy to
  refresh after new filings, or to resume when Alpha Vantage's free tier (25 requests
  a day) cuts the transcript download short.
- SEC requests are throttled to stay under the SEC's 10 requests/second limit.
- A company's 10-K is its annual report. The glossy "annual report to shareholders"
  is usually only on the company's investor-relations site.
