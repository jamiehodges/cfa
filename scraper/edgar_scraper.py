#!/usr/bin/env python3
"""Download SEC EDGAR filings and earnings call transcripts for one company.

Pulls 10-K/10-Q reports, 8-K earnings releases (with their EX-99 press-release
exhibits), other 8-Ks, proxy statements, insider (Forms 3/4/5) and beneficial
ownership (13D/13G) filings, plus earnings call transcripts from Alpha Vantage.
Each document is saved as the original file and as cleaned plain text, and a
manifest.csv indexes everything that was downloaded.

Example:
    export SEC_USER_AGENT="Jane Doe jane@example.com"
    export ALPHAVANTAGE_API_KEY=...        # optional, for call transcripts
    python scraper/edgar_scraper.py GRMN --years 10 --out data
"""

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

import warnings

import requests
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/{name}"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}"
ALPHAVANTAGE_URL = "https://www.alphavantage.co/query"

# SEC allows at most 10 requests/second; stay comfortably below it.
SEC_DELAY = 0.15

# Form groups selectable with --groups. "earnings" and "8k" are both drawn from
# 8-K filings and split on whether the filing reports Item 2.02 (results of
# operations and financial condition).
GROUPS = {
    "reports": {"10-K", "10-K/A", "10-KT", "10-Q", "10-Q/A", "20-F", "40-F"},
    "earnings": {"8-K", "8-K/A"},
    "8k": {"8-K", "8-K/A"},
    "proxy": {"DEF 14A", "DEFA14A", "DEF 14C", "PRE 14A"},
    "insider": {"3", "3/A", "4", "4/A", "5", "5/A"},
    "ownership": {"SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A",
                  "SCHEDULE 13D", "SCHEDULE 13D/A", "SCHEDULE 13G", "SCHEDULE 13G/A"},
}
ALL_GROUPS = ["reports", "earnings", "8k", "proxy", "insider", "ownership", "transcripts"]


class Client:
    def __init__(self, user_agent):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self._last = 0.0

    def get(self, url, **kwargs):
        for attempt in range(5):
            wait = SEC_DELAY - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                resp = self.session.get(url, timeout=60, **kwargs)
            except requests.RequestException as exc:
                err = exc
            else:
                if resp.status_code == 200:
                    return resp
                if resp.status_code not in (429, 500, 502, 503, 504):
                    resp.raise_for_status()
                err = requests.HTTPError(f"HTTP {resp.status_code} for {url}")
            time.sleep(2 ** (attempt + 1))
        raise err


def resolve_cik(client, ticker):
    data = client.get(SEC_TICKERS_URL).json()
    for row in data.values():
        if row["ticker"].upper() == ticker.upper():
            return int(row["cik_str"]), row["title"]
    sys.exit(f"Ticker {ticker!r} not found in SEC company_tickers.json; pass --cik instead.")


def iter_filings(client, cik):
    """Yield every filing in the company's submission history as a dict."""
    sub = client.get(SUBMISSIONS_URL.format(name=f"CIK{cik:010d}.json")).json()
    pages = [sub["filings"]["recent"]]
    for extra in sub["filings"].get("files", []):
        pages.append(client.get(SUBMISSIONS_URL.format(name=extra["name"])).json())
    for page in pages:
        keys = list(page.keys())
        for values in zip(*(page[k] for k in keys)):
            yield dict(zip(keys, values))


def classify(filing, groups):
    """Return the group a filing belongs to, or None if it is not wanted."""
    form = filing["form"]
    for group in groups:
        if group == "transcripts" or form not in GROUPS[group]:
            continue
        is_earnings = "2.02" in (filing.get("items") or "")
        if group == "earnings" and not is_earnings:
            continue
        if group == "8k" and is_earnings:
            continue
        return group
    return None


BLOCK_TAGS = ["p", "div", "tr", "li", "table", "h1", "h2", "h3", "h4", "h5", "h6", "title"]


def html_to_text(raw):
    soup = BeautifulSoup(raw, "lxml")
    # Inline XBRL filings carry a hidden header full of tagged facts; drop it.
    for tag in soup.find_all(["ix:header", "script", "style"]):
        tag.decompose()
    for tag in soup.find_all(style=re.compile(r"display:\s*none", re.I)):
        tag.decompose()
    # Source whitespace (including newlines) is insignificant in HTML, so mark
    # line and cell breaks with placeholders, collapse whitespace, then expand
    # them. Inline spans stay on one line; table rows become tab-separated.
    for tag in soup.find_all("br"):
        tag.replace_with("\x00")
    for tag in soup.find_all(["td", "th"]):
        tag.append("\x01")
    for tag in soup.find_all(BLOCK_TAGS):
        if tag.name == "tr" or not tag.find_parent(["td", "th"]):
            tag.append("\x00")
    text = re.sub(r"[\s\u200b]+", " ", soup.get_text(""))
    text = re.sub(r" *\x01[ \x01]*", "\t", text)
    text = re.sub(r"\t*( *\x00 *)", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def to_text(raw, name):
    if name.lower().endswith((".htm", ".html", ".xml")):
        return html_to_text(raw)
    return raw.decode("utf-8", errors="replace")


def exhibit_docs(client, cik, acc_nodash, acc):
    """Return (document, type) pairs for the EX-99 exhibits of a filing."""
    url = f"{ARCHIVE_URL.format(cik=cik, acc=acc_nodash)}/{acc}-index.htm"
    soup = BeautifulSoup(client.get(url).content, "lxml")
    docs = []
    for table in soup.find_all("table", class_="tableFile"):
        for row in table.find_all("tr")[1:]:
            cells = row.find_all("td")
            if len(cells) < 4 or not cells[2].a:
                continue
            doc_type = cells[3].get_text(strip=True)
            href = cells[2].a["href"]
            if doc_type.upper().startswith("EX-99"):
                # Links may point at the inline XBRL viewer (/ix?doc=...).
                docs.append((href.split("/")[-1], doc_type))
    return docs


def text_path(dest):
    return dest if dest.suffix == ".txt" else dest.with_name(dest.name + ".txt")


def save(raw, dest, fmt):
    """Write the original and/or the clean-text version of a document."""
    if fmt != "text":
        dest.write_bytes(raw)
    if fmt != "original" and text_path(dest) != dest:
        text_path(dest).write_text(to_text(raw, dest.name), encoding="utf-8")
    elif fmt == "text":
        dest.write_bytes(raw)


def safe(name):
    return re.sub(r"[^\w.\-]+", "_", name)


def download_filing(client, cik, filing, group, root, fmt, manifest):
    acc = filing["accessionNumber"]
    acc_nodash = acc.replace("-", "")
    base_url = ARCHIVE_URL.format(cik=cik, acc=acc_nodash)
    folder = root / group
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{filing['filingDate']}_{safe(filing['form'])}_{acc_nodash}"

    docs = [(filing["primaryDocument"], "primary")]
    if group == "earnings":
        docs += exhibit_docs(client, cik, acc_nodash, acc)

    for doc, kind in docs:
        if not doc:
            continue
        # Forms 3/4/5 list an XSL-rendered path like "xslF345X05/form4.xml";
        # keep that path so we get the human-readable HTML rendering.
        suffix = "" if kind == "primary" else "_" + safe(kind.lower())
        ext = Path(doc).suffix or ".txt"
        if doc.lower().endswith(".xml") and doc.startswith("xsl"):
            ext = ".html"
        dest = folder / f"{stem}{suffix}{ext}"
        if (dest if fmt != "text" else text_path(dest)).exists():
            status = "exists"
        else:
            raw = client.get(f"{base_url}/{doc}").content
            save(raw, dest, fmt)
            status = "downloaded"
        manifest.append({
            "group": group, "form": filing["form"], "filing_date": filing["filingDate"],
            "report_date": filing.get("reportDate", ""), "items": filing.get("items", ""),
            "accession": acc, "document": kind, "description": filing.get("primaryDocDescription", ""),
            "url": f"{base_url}/{doc}", "local_file": str(dest), "status": status,
        })


def quarters(start, today):
    """Yield "YYYYQn" labels from start's quarter up to the last quarter whose
    earnings call has likely happened (quarter end + 45 days)."""
    y, q = start.year, (start.month - 1) // 3 + 1
    while True:
        quarter_end = dt.date(y + (q == 4), 1 if q == 4 else 3 * q + 1, 1) - dt.timedelta(days=1)
        if quarter_end + dt.timedelta(days=45) > today:
            return
        yield f"{y}Q{q}"
        y, q = (y + 1, 1) if q == 4 else (y, q + 1)


def download_transcripts(ticker, api_key, start, root, manifest):
    folder = root / "transcripts"
    folder.mkdir(parents=True, exist_ok=True)
    for quarter in quarters(start, dt.date.today()):
        dest = folder / f"{ticker}_{quarter}_earnings_call.json"
        if dest.exists():
            manifest.append({"group": "transcripts", "form": "transcript", "report_date": quarter,
                             "local_file": str(dest), "status": "exists"})
            continue
        resp = requests.get(ALPHAVANTAGE_URL, timeout=60, params={
            "function": "EARNINGS_CALL_TRANSCRIPT", "symbol": ticker,
            "quarter": quarter, "apikey": api_key})
        resp.raise_for_status()
        data = resp.json()
        if "Information" in data or "Note" in data:
            print(f"  Alpha Vantage stopped at {quarter}: {data.get('Information') or data.get('Note')}")
            print("  Re-run later; existing transcripts are skipped.")
            return
        lines = data.get("transcript") or []
        if not lines:
            print(f"  no transcript for {quarter}")
            continue
        dest.write_text(json.dumps(data, indent=2), encoding="utf-8")
        md = [f"# {ticker} {quarter} earnings call\n"]
        for line in lines:
            title = f" ({line['title']})" if line.get("title") else ""
            md.append(f"**{line.get('speaker', '')}{title}:** {line.get('content', '').strip()}\n")
        dest.with_suffix(".md").write_text("\n".join(md), encoding="utf-8")
        manifest.append({"group": "transcripts", "form": "transcript", "report_date": quarter,
                         "url": "alphavantage EARNINGS_CALL_TRANSCRIPT",
                         "local_file": str(dest.with_suffix(".md")), "status": "downloaded"})
        print(f"  transcript {quarter}")
        time.sleep(1.2)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("ticker", help="Stock ticker, e.g. GRMN")
    p.add_argument("--cik", type=int, help="SEC CIK, skips the ticker lookup")
    p.add_argument("--years", type=float, default=5, help="How many years back to go (default 5)")
    p.add_argument("--since", help="Start date YYYY-MM-DD; overrides --years")
    p.add_argument("--groups", default=",".join(ALL_GROUPS),
                   help=f"Comma-separated subset of: {','.join(ALL_GROUPS)} (default: all)")
    p.add_argument("--format", choices=["both", "original", "text"], default="both",
                   help="Save original files, clean text, or both (default both)")
    p.add_argument("--out", default="data", help="Output root folder (default ./data)")
    p.add_argument("--user-agent", default=os.environ.get("SEC_USER_AGENT"),
                   help='SEC requires "Name email@domain"; or set SEC_USER_AGENT')
    p.add_argument("--av-key", default=os.environ.get("ALPHAVANTAGE_API_KEY"),
                   help="Alpha Vantage API key for transcripts; or set ALPHAVANTAGE_API_KEY")
    p.add_argument("--dry-run", action="store_true", help="List matching filings without downloading")
    args = p.parse_args()

    if not args.user_agent:
        sys.exit('Set --user-agent or SEC_USER_AGENT to "Your Name your@email.com" (SEC requirement).')
    groups = [g.strip() for g in args.groups.split(",") if g.strip()]
    unknown = set(groups) - set(ALL_GROUPS)
    if unknown:
        sys.exit(f"Unknown group(s): {', '.join(sorted(unknown))}")

    start = (dt.date.fromisoformat(args.since) if args.since
             else dt.date.today() - dt.timedelta(days=round(365.25 * args.years)))
    ticker = args.ticker.upper()
    client = Client(args.user_agent)
    cik, name = (args.cik, ticker) if args.cik else resolve_cik(client, ticker)
    root = Path(args.out) / ticker
    root.mkdir(parents=True, exist_ok=True)
    print(f"{name} (CIK {cik}) filings since {start} -> {root}")

    wanted = []
    for filing in iter_filings(client, cik):
        if filing["filingDate"] < start.isoformat():
            continue
        group = classify(filing, groups)
        if group:
            wanted.append((filing, group))
    wanted.sort(key=lambda fg: fg[0]["filingDate"])

    counts = {}
    for _, group in wanted:
        counts[group] = counts.get(group, 0) + 1
    print("Matched: " + ", ".join(f"{g}={n}" for g, n in sorted(counts.items())) if counts else "Matched: none")
    if args.dry_run:
        for filing, group in wanted:
            print(f"  {filing['filingDate']}  {group:<9} {filing['form']:<8} {filing.get('items', '')}")
        return

    manifest = []
    for i, (filing, group) in enumerate(wanted, 1):
        print(f"[{i}/{len(wanted)}] {filing['filingDate']} {filing['form']} {filing['accessionNumber']}")
        try:
            download_filing(client, cik, filing, group, root, args.format, manifest)
        except requests.RequestException as exc:
            print(f"  FAILED: {exc}")
            manifest.append({"group": group, "form": filing["form"], "filing_date": filing["filingDate"],
                             "accession": filing["accessionNumber"], "status": f"failed: {exc}"})

    if "transcripts" in groups:
        if args.av_key:
            print("Earnings call transcripts (Alpha Vantage)")
            download_transcripts(ticker, args.av_key, start, root, manifest)
        else:
            print("Skipping transcripts: set ALPHAVANTAGE_API_KEY or --av-key.")

    fields = ["group", "form", "filing_date", "report_date", "items", "accession",
              "document", "description", "url", "local_file", "status"]
    with open(root / "manifest.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(manifest)
    print(f"Done. {len(manifest)} documents indexed in {root / 'manifest.csv'}")


if __name__ == "__main__":
    main()
