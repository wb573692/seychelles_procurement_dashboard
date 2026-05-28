"""
NTB Seychelles Awarded Tenders Scraper
=======================================
Scrapes all awarded tender PDFs from https://www.ntb.sc/tenders/awarded-tenders,
extracts SR-denominated contract data, and outputs a clean CSV + summary stats.

Requirements:
    pip install requests beautifulsoup4 pdfplumber pandas

Usage:
    python ntb_seychelles_scraper.py
    python ntb_seychelles_scraper.py --out my_output.csv
    python ntb_seychelles_scraper.py --pdf-dir ./pdfs --skip-download
"""

import re
import io
import time
import argparse
import logging
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
import pdfplumber
import pandas as pd

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = "https://www.ntb.sc/tenders/awarded-tenders"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; NTB-Scraper/1.0; "
        "+https://github.com/your-repo)"
    )
}
REQUEST_DELAY = 1.5          # seconds between HTTP requests (be polite)
MAX_PAGES = 20               # safety cap on pagination
DEFAULT_OUTPUT = "ntb_tenders.csv"
DEFAULT_PDF_DIR = "./ntb_pdfs"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Category classifier  (keyword → category)
# ---------------------------------------------------------------------------

CATEGORY_RULES = [
    ("Housing",         r"housing|condo|condominium|affordable|residential|flat|apartment|units.*bedroom|bedroom.*unit"),
    ("Infrastructure",  r"infrastructure|retaining wall|drain|culvert|bridge|access road|slope|stabilisation|renovation|refurbish|office|headquarter|national house|state house|assembly|archive|substation|kiosk"),
    ("Roads & seawalls",r"road|seawall|parapet|footpath|lane|surfac|resurfac|roundabout|traffic|pavement|widening|drainage.*road|asphal"),
    ("Utilities",       r"sewerage|sewage|wastewater|desalination|pipeline|water supply|water pipe|water meter|pump|cable.*laying|duct"),
    ("Education",       r"school|education|university|textbook|stationar|furniture.*school|band set|britannica|openemis|learning"),
    ("Health",          r"hospital|health centre|clinic|medical|health care|doctor"),
    ("Transport",       r"transport|vehicle|bus|car|suv|pickup|motorbike|streetlight|airport.*transport"),
    ("Energy",          r"solar|pv|genset|generator|power station|floating.*pv|energy meter|switchgear|substation.*power"),
    ("Fisheries",       r"fish|fisheries|fishing|ice machine|vms|transceiver"),
    ("Maritime",        r"port|quay|jetty|docking|tug|undersea cable|submarine cable|harbour"),
    ("ICT",             r"ict|software|microsoft|cisco|server|laptop|computer|network|digital|erp|data warehouse|cybersecurity|iso.*27001|kanban|android"),
    ("Environment",     r"landfill|waste|cleaning.*beach|beach.*cleaning|bin|environmental|biodiversity|biodiversity audit"),
    ("Security",        r"security service|cctv|surveillance|screening|x-ray|detection|guard|patrol"),
    ("Sports",          r"sport|stadium|swimming pool|pitch|field|mower|grass"),
    ("Consultancy",     r"consultancy|consultant|advisory|feasibility study|impact assessment|audit"),
    ("Other",           r".*"),   # catch-all — must be last
]

def classify(description: str) -> str:
    desc_lower = description.lower()
    for category, pattern in CATEGORY_RULES:
        if re.search(pattern, desc_lower):
            return category
    return "Other"

# ---------------------------------------------------------------------------
# Currency / amount parsing
# ---------------------------------------------------------------------------

# Matches amounts like "SR1,234,567.89" / "SR 1,234,567" / "SCR 999,000"
SR_PATTERN = re.compile(
    r"(?:SR|SCR)\s*([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)

# Generic amount for non-SR currencies
AMOUNT_PATTERN = re.compile(
    r"(USD|EUR|GBP|AED|ZAR|RAND|CHF|SGD|AUD|INR)\s*([\d,]+(?:\.\d+)?)"
    r"|([\d,]+(?:\.\d+)?)\s*(USD|EUR|GBP|AED|ZAR|RAND)",
    re.IGNORECASE,
)


def parse_sr(text: str):
    """Return float SR value or None."""
    m = SR_PATTERN.search(text)
    if m:
        return float(m.group(1).replace(",", ""))
    return None


def parse_foreign(text: str):
    """Return (currency, amount) tuple or (None, None)."""
    m = AMOUNT_PATTERN.search(text)
    if not m:
        return None, None
    if m.group(1):
        return m.group(1).upper(), float(m.group(2).replace(",", ""))
    if m.group(4):
        return m.group(4).upper(), float(m.group(3).replace(",", ""))
    return None, None

# ---------------------------------------------------------------------------
# Page listing scraper
# ---------------------------------------------------------------------------

def get_pdf_links() -> list[dict]:
    """
    Crawl all pagination pages of /tenders/awarded-tenders and collect
    every PDF download link plus its period label.
    Returns list of dicts: {period_label, pdf_url, page_url}
    """
    results = []
    page = 0

    while page < MAX_PAGES:
        url = BASE_URL if page == 0 else f"{BASE_URL}?start={page * 8}"
        log.info(f"Fetching listing page: {url}")
        resp = _get(url)
        if resp is None:
            break

        soup = BeautifulSoup(resp.text, "html.parser")

        # Each tender block is an <article> or identified by h2 + download link
        # The site wraps each entry in a div with a h2 headline + download link
        found_any = False
        for h2 in soup.select("h2"):
            period_label = h2.get_text(strip=True)
            # Look for download link nearby (sibling / parent)
            container = h2.find_parent() or h2
            for a in container.find_all("a", href=True):
                href = a["href"]
                if ".pdf" in href.lower() and "download" in a.get_text(strip=True).lower():
                    pdf_url = urljoin("https://www.ntb.sc", href)
                    results.append({
                        "period_label": period_label,
                        "pdf_url": pdf_url,
                        "page_url": url,
                    })
                    found_any = True

        # Check if there's a next page
        next_link = soup.select_one("a[href*='start=']")
        has_more = any(
            f"start={( page + 1) * 8}" in a["href"]
            for a in soup.select("a[href*='start=']")
        )
        if not has_more:
            log.info("No more pages found.")
            break

        page += 1
        time.sleep(REQUEST_DELAY)

    # Deduplicate by PDF URL
    seen = set()
    unique = []
    for r in results:
        if r["pdf_url"] not in seen:
            seen.add(r["pdf_url"])
            unique.append(r)

    log.info(f"Found {len(unique)} unique PDF links across {page + 1} page(s).")
    return unique

# ---------------------------------------------------------------------------
# PDF downloader
# ---------------------------------------------------------------------------

def download_pdf(pdf_url: str, pdf_dir: Path) -> Path | None:
    """Download PDF to disk, return local path or None on failure."""
    filename = pdf_url.split("/")[-1]
    local_path = pdf_dir / filename
    if local_path.exists():
        log.info(f"  Already downloaded: {filename}")
        return local_path

    log.info(f"  Downloading: {pdf_url}")
    resp = _get(pdf_url, stream=True)
    if resp is None:
        return None

    with open(local_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    time.sleep(REQUEST_DELAY)
    return local_path

# ---------------------------------------------------------------------------
# PDF parser
# ---------------------------------------------------------------------------

def parse_pdf(pdf_path: Path, period_label: str) -> list[dict]:
    """
    Extract contract rows from a tender award PDF.
    Returns list of dicts with keys:
        period, org, description, winner, sr_value,
        currency, foreign_amount, category
    """
    rows = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            full_text = "\n".join(
                page.extract_text() or "" for page in pdf.pages
            )
    except Exception as e:
        log.warning(f"  Could not read {pdf_path.name}: {e}")
        return rows

    # Try table extraction first (most PDFs have proper tables)
    rows = _parse_via_tables(pdf_path, period_label)
    if rows:
        log.info(f"  Extracted {len(rows)} rows via table parser")
        return rows

    # Fallback: line-by-line text heuristic
    rows = _parse_via_text(full_text, period_label)
    log.info(f"  Extracted {len(rows)} rows via text parser")
    return rows


def _parse_via_tables(pdf_path: Path, period_label: str) -> list[dict]:
    """Attempt structured table extraction with pdfplumber."""
    rows = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables()
                for table in tables:
                    for row in table:
                        if row is None or len(row) < 3:
                            continue
                        # Normalise cells
                        cells = [
                            (c or "").strip().replace("\n", " ")
                            for c in row
                        ]
                        # Skip header rows
                        if any(
                            kw in cells[0].lower()
                            for kw in ("organisation", "project", "procuring", "description")
                        ):
                            continue

                        # Identify columns by content heuristics
                        # Typical layout: ORG | DESCRIPTION | WINNER | AMOUNT
                        # Some PDFs flip org/description
                        if len(cells) >= 4:
                            org         = cells[0]
                            description = cells[1]
                            winner      = cells[2]
                            amount_raw  = " ".join(cells[3:])
                        elif len(cells) == 3:
                            org         = ""
                            description = cells[0]
                            winner      = cells[1]
                            amount_raw  = cells[2]
                        else:
                            continue

                        if not description or not winner:
                            continue

                        sr_val          = parse_sr(amount_raw)
                        currency, f_amt = parse_foreign(amount_raw)

                        rows.append({
                            "period":         period_label,
                            "org":            org,
                            "description":    description,
                            "winner":         winner,
                            "sr_value":       sr_val,
                            "currency":       currency if sr_val is None else "SR",
                            "foreign_amount": f_amt if sr_val is None else None,
                            "amount_raw":     amount_raw,
                            "category":       classify(description),
                        })
    except Exception as e:
        log.warning(f"  Table extraction failed: {e}")
    return rows


def _parse_via_text(text: str, period_label: str) -> list[dict]:
    """
    Heuristic line-based parser for PDFs where table extraction fails.
    Looks for lines containing an amount and tries to reconstruct the row.
    """
    rows = []
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    for i, line in enumerate(lines):
        # A data line typically ends with an amount
        sr_val = parse_sr(line)
        currency, f_amt = parse_foreign(line)

        if sr_val is None and currency is None:
            continue
        if len(line) < 20:
            continue  # too short to be a real row

        # Try to split into parts:  "ORG  DESCRIPTION  WINNER  AMOUNT"
        # Remove the amount suffix first
        amount_match = SR_PATTERN.search(line) or AMOUNT_PATTERN.search(line)
        core = line[: amount_match.start()].strip() if amount_match else line
        amount_raw = line[amount_match.start():].strip() if amount_match else ""

        # Heuristic: split on 2+ spaces
        parts = re.split(r"\s{2,}", core)
        if len(parts) >= 3:
            org, description, winner = parts[0], parts[1], parts[-1]
        elif len(parts) == 2:
            org, description, winner = "", parts[0], parts[1]
        else:
            org, description, winner = "", core, ""

        rows.append({
            "period":         period_label,
            "org":            org,
            "description":    description,
            "winner":         winner,
            "sr_value":       sr_val,
            "currency":       currency if sr_val is None else "SR",
            "foreign_amount": f_amt if sr_val is None else None,
            "amount_raw":     amount_raw,
            "category":       classify(description),
        })
    return rows

# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _get(url: str, stream: bool = False, retries: int = 3):
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, stream=stream, timeout=30)
            if r.status_code == 200:
                return r
            log.warning(f"  HTTP {r.status_code} for {url} (attempt {attempt})")
        except requests.RequestException as e:
            log.warning(f"  Request error: {e} (attempt {attempt})")
        time.sleep(REQUEST_DELAY * attempt)
    return None

# ---------------------------------------------------------------------------
# Summary printer
# ---------------------------------------------------------------------------

def print_summary(df: pd.DataFrame):
    sr_only = df[df["currency"] == "SR"].copy()
    total_sr = sr_only["sr_value"].sum()

    print("\n" + "=" * 60)
    print("  NTB SEYCHELLES — TENDER AWARDS SUMMARY")
    print("=" * 60)
    print(f"  Total contracts (all currencies): {len(df):>6,}")
    print(f"  SR-denominated contracts:         {len(sr_only):>6,}")
    print(f"  Total SR value awarded:           SR {total_sr:>16,.2f}")
    print(f"  Unique procuring entities:        {df['org'].nunique():>6}")
    print(f"  Unique winning bidders:           {df['winner'].nunique():>6}")

    print("\n  TOP 10 AGENCIES BY SR SPEND")
    print("  " + "-" * 50)
    top_orgs = (
        sr_only.groupby("org")["sr_value"]
        .sum()
        .sort_values(ascending=False)
        .head(10)
    )
    for org, val in top_orgs.items():
        label = (org[:38] + "…") if len(org) > 38 else org
        print(f"  {label:<40}  SR {val:>14,.0f}")

    print("\n  SPEND BY CATEGORY")
    print("  " + "-" * 50)
    top_cats = (
        sr_only.groupby("category")["sr_value"]
        .sum()
        .sort_values(ascending=False)
    )
    for cat, val in top_cats.items():
        pct = val / total_sr * 100
        print(f"  {cat:<25}  SR {val:>14,.0f}  ({pct:.1f}%)")

    print("\n  SPEND BY PERIOD")
    print("  " + "-" * 50)
    top_periods = (
        sr_only.groupby("period")["sr_value"]
        .sum()
        .sort_values(ascending=False)
    )
    for period, val in top_periods.items():
        label = (period[:38] + "…") if len(period) > 38 else period
        print(f"  {label:<40}  SR {val:>14,.0f}")

    print("=" * 60 + "\n")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Scrape NTB Seychelles awarded tender PDFs."
    )
    parser.add_argument(
        "--out", default=DEFAULT_OUTPUT,
        help=f"Output CSV file (default: {DEFAULT_OUTPUT})"
    )
    parser.add_argument(
        "--pdf-dir", default=DEFAULT_PDF_DIR,
        help=f"Directory to save downloaded PDFs (default: {DEFAULT_PDF_DIR})"
    )
    parser.add_argument(
        "--skip-download", action="store_true",
        help="Skip downloading PDFs; parse whatever is already in --pdf-dir"
    )
    parser.add_argument(
        "--sr-only", action="store_true",
        help="Only output rows with SR-denominated amounts"
    )
    args = parser.parse_args()

    pdf_dir = Path(args.pdf_dir)
    pdf_dir.mkdir(parents=True, exist_ok=True)

    # 1. Discover PDF links
    if args.skip_download:
        log.info("--skip-download set; using existing PDFs in %s", pdf_dir)
        pdf_links = [
            {"period_label": p.stem, "pdf_url": "", "page_url": ""}
            for p in sorted(pdf_dir.glob("*.pdf"))
        ]
        local_paths = {
            item["period_label"]: pdf_dir / (item["period_label"] + ".pdf")
            for item in pdf_links
        }
    else:
        pdf_links = get_pdf_links()
        if not pdf_links:
            log.error("No PDF links found. Exiting.")
            return

        # 2. Download PDFs
        log.info("\nDownloading %d PDF(s)…", len(pdf_links))
        local_paths = {}
        for item in pdf_links:
            lp = download_pdf(item["pdf_url"], pdf_dir)
            if lp:
                local_paths[item["period_label"]] = lp

    # 3. Parse PDFs
    log.info("\nParsing %d PDF(s)…", len(local_paths))
    all_rows = []
    for period_label, lp in local_paths.items():
        log.info("Parsing: %s", lp.name)
        rows = parse_pdf(lp, period_label)
        all_rows.extend(rows)

    if not all_rows:
        log.error("No data extracted. Check the PDFs manually.")
        return

    # 4. Build DataFrame & clean up
    df = pd.DataFrame(all_rows)
    df = df.dropna(subset=["description"])
    df = df[df["description"].str.len() > 5]
    df["description"] = df["description"].str.strip()
    df["org"]         = df["org"].str.strip()
    df["winner"]      = df["winner"].str.strip()

    if args.sr_only:
        df = df[df["currency"] == "SR"].copy()
        log.info("Filtered to SR-only rows: %d", len(df))

    # 5. Save CSV
    out_path = Path(args.out)
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    log.info("Saved %d rows to %s", len(df), out_path)

    # 6. Print summary
    print_summary(df)

    # 7. Also save a quick Excel with summary sheets if openpyxl available
    try:
        import openpyxl  # noqa: F401
        xl_path = out_path.with_suffix(".xlsx")
        sr_df = df[df["currency"] == "SR"].copy()
        with pd.ExcelWriter(xl_path, engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="All contracts", index=False)
            (
                sr_df.groupby("org")["sr_value"]
                .sum()
                .sort_values(ascending=False)
                .reset_index()
                .rename(columns={"sr_value": "total_sr"})
                .to_excel(writer, sheet_name="By agency", index=False)
            )
            (
                sr_df.groupby("category")["sr_value"]
                .sum()
                .sort_values(ascending=False)
                .reset_index()
                .rename(columns={"sr_value": "total_sr"})
                .to_excel(writer, sheet_name="By category", index=False)
            )
            (
                sr_df.groupby("period")["sr_value"]
                .sum()
                .sort_values(ascending=False)
                .reset_index()
                .rename(columns={"sr_value": "total_sr"})
                .to_excel(writer, sheet_name="By period", index=False)
            )
        log.info("Also saved Excel workbook to %s", xl_path)
    except ImportError:
        log.info("openpyxl not installed — skipping Excel export (pip install openpyxl)")


if __name__ == "__main__":
    main()
