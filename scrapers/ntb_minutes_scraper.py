import re
import time
import argparse
import logging
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
import pdfplumber
import pandas as pd

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL    = "https://www.ntb.sc/tenders/minutes-of-tenders"
HEADERS     = {"User-Agent": "Mozilla/5.0 (compatible; NTB-Minutes-Scraper/1.0)"}
PAGE_SIZE   = 8          # items per listing page (site uses ?start=N*8)
REQUEST_DELAY = 1.2      # seconds between requests — be polite
MAX_PAGES   = 200        # safety cap (site has ~158 pages as of April 2026)
DEFAULT_OUT = "ntb_minutes.csv"
DEFAULT_PDF_DIR = "./ntb_minutes_pdfs"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Category classifier
# ---------------------------------------------------------------------------

CATEGORY_RULES = [
    ("Housing",          r"housing|condo|condominium|affordable|residential|duplex|flat|apartment|bedroom|units.*bedroom|bedroom.*unit|creche|kampala"),
    ("Roads & seawalls", r"road|seawall|parapet|footpath|lane|surfac|resurfac|roundabout|traffic|pavement|widening|drainage.*road|asphal|bollard|access road|motorable"),
    ("Infrastructure",   r"infrastructure|retaining wall|drain|culvert|bridge|slope|stabilisation|renovation|refurbish|office|headquarter|national house|state house|assembly|archive|substation|kiosk|perimeter|record centre|depot|workshop|shed|warehouse|fence|fencing|chainlink"),
    ("Utilities",        r"sewerage|sewage|wastewater|desalination|pipeline|water supply|water pipe|water meter|pump|cable.*laying|duct|sorento|pipeline replacement"),
    ("Education",        r"school|education|university|textbook|stationar|furniture.*school|band set|britannica|openemis|learning|classroom|library"),
    ("Health",           r"hospital|health centre|clinic|medical|health care|doctor"),
    ("Transport",        r"transport|vehicle|bus|car|suv|pickup|motorbike|streetlight|airport.*transport|procurement.*vehicle"),
    ("Energy",           r"solar|pv|genset|generator|power station|floating.*pv|energy meter|switchgear|substation.*power|electrical duct"),
    ("Fisheries",        r"fish|fisheries|fishing|ice machine|vms|transceiver"),
    ("Maritime",         r"port|quay|jetty|docking|tug|undersea cable|submarine cable|harbour|seaport"),
    ("ICT",              r"ict|software|microsoft|cisco|server|laptop|computer|network|digital|erp|data warehouse|cybersecurity|iso.*27001|kanban|android|office 365"),
    ("Environment",      r"landfill|waste|cleaning.*beach|beach.*cleaning|bin|environmental|biodiversity|biodiversity audit"),
    ("Security",         r"security service|cctv|surveillance|screening|x-ray|detection|guard|patrol"),
    ("Sports",           r"sport|stadium|swimming pool|pitch|field|mower|grass|arena"),
    ("Consultancy",      r"consultancy|consultant|advisory|feasibility study|impact assessment|audit"),
    ("Other",            r".*"),
]

def classify(description: str) -> str:
    desc_lower = description.lower()
    for category, pattern in CATEGORY_RULES:
        if re.search(pattern, desc_lower):
            return category
    return "Other"

# ---------------------------------------------------------------------------
# Amount / currency parsing
# ---------------------------------------------------------------------------

SR_PATTERN = re.compile(r"(?:SR|SCR)\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE)
FOREIGN_PATTERN = re.compile(
    r"(USD|EUR|GBP|AED|ZAR|CHF|SGD|AUD|INR|RAND)\s*([\d,]+(?:\.\d+)?)"
    r"|([\d,]+(?:\.\d+)?)\s*(USD|EUR|GBP|AED|ZAR|CHF|SGD|AUD|INR|RAND)",
    re.IGNORECASE,
)

def parse_amount(text: str):
    """
    Returns (currency_str, float_amount) from a raw amount cell.
    Tries SR first, then foreign currencies.
    Returns ("UNKNOWN", None) if nothing found.
    """
    text = (text or "").strip()
    m = SR_PATTERN.search(text)
    if m:
        return "SR", float(m.group(1).replace(",", ""))
    m = FOREIGN_PATTERN.search(text)
    if m:
        if m.group(1):
            return m.group(1).upper(), float(m.group(2).replace(",", ""))
        return m.group(4).upper(), float(m.group(3).replace(",", ""))
    # Last-ditch: bare number
    bare = re.sub(r"[^\d.]", "", text)
    if bare:
        try:
            return "SR", float(bare)
        except ValueError:
            pass
    return "UNKNOWN", None

# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _get(url: str, stream: bool = False, retries: int = 3):
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, stream=stream, timeout=30)
            if r.status_code == 200:
                return r
            log.warning("HTTP %s for %s (attempt %d)", r.status_code, url, attempt)
        except requests.RequestException as e:
            log.warning("Request error: %s (attempt %d)", e, attempt)
        time.sleep(REQUEST_DELAY * attempt)
    return None

# ---------------------------------------------------------------------------
# Step 1 — Crawl listing pages and collect (title, org, date, pdf_url, detail_url)
# ---------------------------------------------------------------------------

def collect_listing_entries(max_pages: int) -> list[dict]:
    """
    Crawl all pagination pages of /tenders/minutes-of-tenders.
    Returns a list of dicts:
        title, org, created_date, pdf_url, detail_url
    """
    entries = []
    page = 0

    while page < max_pages:
        url = BASE_URL if page == 0 else f"{BASE_URL}?start={page * PAGE_SIZE}"
        log.info("Listing page %d: %s", page + 1, url)
        resp = _get(url)
        if resp is None:
            log.warning("Failed to fetch listing page %d — stopping.", page + 1)
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        found_on_page = 0

        # Each article block has an h2 with a link + optional org paragraph + created date + PDF link
        # Strategy: find all <h2> tags that contain an anchor to a minutes detail page
        for h2 in soup.find_all("h2"):
            a_title = h2.find("a", href=True)
            if not a_title:
                continue
            detail_url = urljoin("https://www.ntb.sc", a_title["href"])
            title = a_title.get_text(strip=True)

            # Walk up to the containing block to find org and date
            block = h2.find_parent()  # usually a div or article

            org = ""
            created_date = ""
            pdf_url = ""

            if block:
                # Org: often in a <p> or direct text after h2, before the date
                # It appears as plain text or in <em>/<strong> tags
                for el in block.children:
                    text = el.get_text(strip=True) if hasattr(el, "get_text") else str(el).strip()
                    if not text or text == title:
                        continue
                    # "Created on" line
                    if "created on" in text.lower():
                        created_date = re.sub(r"(?i)created on\s*", "", text).strip()
                        continue
                    # Download link
                    if hasattr(el, "find"):
                        dl = el.find("a", href=lambda h: h and ".pdf" in h.lower())
                        if dl:
                            pdf_url = urljoin("https://www.ntb.sc", dl["href"])
                            continue
                    # Anything else short-ish is likely the org
                    if text and len(text) < 120 and "download" not in text.lower():
                        if not org:
                            org = text.strip(" -–—*")

            entries.append({
                "title":       title,
                "org":         org,
                "created_date": created_date,
                "pdf_url":     pdf_url,
                "detail_url":  detail_url,
            })
            found_on_page += 1

        # Also grab entries listed in "More Articles …" <ul>
        more_ul = soup.find("ul", class_=lambda c: c and "more" in c.lower()) \
                  or soup.find("h3", string=lambda s: s and "More Articles" in s)
        if more_ul and hasattr(more_ul, "find_next_sibling"):
            ul = more_ul.find_next_sibling("ul")
            if ul:
                for li_a in ul.find_all("a", href=True):
                    detail_url = urljoin("https://www.ntb.sc", li_a["href"])
                    if detail_url not in {e["detail_url"] for e in entries}:
                        entries.append({
                            "title":       li_a.get_text(strip=True),
                            "org":         "",
                            "created_date": "",
                            "pdf_url":     "",   # will resolve from detail page
                            "detail_url":  detail_url,
                        })
                        found_on_page += 1

        log.info("  → %d entries found on this page (total so far: %d)", found_on_page, len(entries))

        # Check if there's a next page
        has_next = any(
            f"start={( page + 1) * PAGE_SIZE}" in (a.get("href") or "")
            for a in soup.select("a[href*='start=']")
        )
        if not has_next:
            log.info("No next page — listing crawl complete.")
            break

        page += 1
        time.sleep(REQUEST_DELAY)

    # Deduplicate by detail_url
    seen = set()
    unique = []
    for e in entries:
        if e["detail_url"] not in seen:
            seen.add(e["detail_url"])
            unique.append(e)

    log.info("Total unique listings collected: %d", len(unique))
    return unique

# ---------------------------------------------------------------------------
# Step 2 — Resolve missing PDF URLs from detail pages
# ---------------------------------------------------------------------------

def resolve_pdf_urls(entries: list[dict]) -> list[dict]:
    """For entries where pdf_url is empty, fetch the detail page to find it."""
    need_resolve = [e for e in entries if not e["pdf_url"]]
    log.info("Resolving PDF URLs for %d entries via detail pages…", len(need_resolve))

    for e in need_resolve:
        resp = _get(e["detail_url"])
        if resp is None:
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        dl = soup.find("a", href=lambda h: h and ".pdf" in h.lower() and "download" in (h + " ".join(soup.strings)).lower())
        if not dl:
            dl = soup.find("a", href=lambda h: h and ".pdf" in (h or "").lower())
        if dl:
            e["pdf_url"] = urljoin("https://www.ntb.sc", dl["href"])

        # Also grab org / date if still missing
        if not e["created_date"]:
            created_el = soup.find(string=re.compile(r"created on", re.I))
            if created_el:
                e["created_date"] = re.sub(r"(?i)created on\s*", "", created_el).strip()

        time.sleep(REQUEST_DELAY)

    return entries

# ---------------------------------------------------------------------------
# Step 3 — Download PDFs
# ---------------------------------------------------------------------------

def download_pdfs(entries: list[dict], pdf_dir: Path) -> dict[str, Path]:
    """
    Download each unique PDF. Returns mapping pdf_url → local_path.
    Already-downloaded files are skipped.
    """
    url_to_path = {}
    unique_urls = {e["pdf_url"] for e in entries if e["pdf_url"]}
    log.info("Downloading up to %d unique PDFs…", len(unique_urls))

    for pdf_url in unique_urls:
        filename = Path(urlparse(pdf_url).path).name or "unknown.pdf"
        local_path = pdf_dir / filename

        if local_path.exists():
            url_to_path[pdf_url] = local_path
            continue

        resp = _get(pdf_url, stream=True)
        if resp is None:
            log.warning("  Could not download: %s", pdf_url)
            continue

        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)

        url_to_path[pdf_url] = local_path
        log.info("  Downloaded: %s", filename)
        time.sleep(REQUEST_DELAY)

    return url_to_path

# ---------------------------------------------------------------------------
# Step 4 — Parse a single Minutes PDF
# ---------------------------------------------------------------------------

# Patterns to extract the structured fields
RE_DATE     = re.compile(r"date and time of tender opening[:\s]+(.+?)(?:\n|$)", re.I)
RE_ORG      = re.compile(r"minutes of tender opening\s+(.+?)\s+tender for", re.I | re.S)
RE_TENDER   = re.compile(r"tender for(?: the)?[:\s\-–]+(.+?)(?:\n\n|\d+\.)", re.I | re.S)
RE_N_BIDS   = re.compile(r"number of tender receipts[^\d]*(\d+)", re.I)

# Bidder row: T1 / T2 / ... | name | currency | amount
# Formats seen:
#   "T1 BENOITON CONSTRUCTION CO. LTD  SR  2,892,385.70  HSI"
#   "T1  BS CONSTRUCTION  SR  4,092,361.25  HSI"
#   "T3 EUODOO PTY LTD SR 2,299,577.00 SACOS"
RE_BIDDER = re.compile(
    r"T(\d+)\s+"                          # bid number
    r"(.+?)\s+"                           # bidder name (greedy up to currency)
    r"(SR|SCR|USD|EUR|GBP|AED|ZAR|CHF)\s+"  # currency
    r"([\d,]+(?:\.\d+)?)",               # amount
    re.IGNORECASE,
)


def parse_minutes_pdf(pdf_path: Path, entry: dict) -> list[dict]:
    """
    Parse one Minutes of Tender Opening PDF.
    Returns a list of dicts — one per bidder row — with keys:
        title, org, tender_description, opening_date, n_bids_declared,
        bid_number, bidder_name, currency, bid_amount, category,
        created_date, pdf_url, detail_url
    """
    rows = []

    try:
        with pdfplumber.open(pdf_path) as pdf:
            pages_text = []
            for page in pdf.pages:
                t = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
                pages_text.append(t)
            full_text = "\n".join(pages_text)
    except Exception as exc:
        log.warning("  Could not read %s: %s", pdf_path.name, exc)
        return rows

    # --- Extract header fields ---
    opening_date = ""
    m = RE_DATE.search(full_text)
    if m:
        opening_date = m.group(1).strip()

    org_from_pdf = entry.get("org", "")
    m = RE_ORG.search(full_text)
    if m:
        candidate = m.group(1).strip().replace("\n", " ")
        if len(candidate) < 120:
            org_from_pdf = candidate

    tender_desc = entry.get("title", "")
    m = RE_TENDER.search(full_text)
    if m:
        candidate = m.group(1).strip().replace("\n", " ").replace("  ", " ")
        if len(candidate) < 300:
            tender_desc = candidate

    n_bids_declared = None
    m = RE_N_BIDS.search(full_text)
    if m:
        try:
            n_bids_declared = int(m.group(1))
        except ValueError:
            pass

    # --- Extract bidder rows ---
    bidders = RE_BIDDER.findall(full_text)

    if not bidders:
        # Fallback: try table extraction row-by-row
        bidders = _extract_bidders_from_tables(pdf_path)

    category = classify(tender_desc)

    if bidders:
        for bid_num, bidder_name, currency, amount_str in bidders:
            try:
                amount = float(amount_str.replace(",", ""))
            except ValueError:
                amount = None

            rows.append({
                "title":             entry.get("title", ""),
                "org":               org_from_pdf,
                "tender_description": tender_desc,
                "opening_date":      opening_date,
                "n_bids_declared":   n_bids_declared,
                "bid_number":        int(bid_num),
                "bidder_name":       bidder_name.strip(),
                "currency":          currency.upper().replace("SCR", "SR"),
                "bid_amount":        amount,
                "category":          category,
                "created_date":      entry.get("created_date", ""),
                "pdf_url":           entry.get("pdf_url", ""),
                "detail_url":        entry.get("detail_url", ""),
            })
    else:
        # No bids found — still record the tender header with null bid fields
        rows.append({
            "title":             entry.get("title", ""),
            "org":               org_from_pdf,
            "tender_description": tender_desc,
            "opening_date":      opening_date,
            "n_bids_declared":   n_bids_declared,
            "bid_number":        None,
            "bidder_name":       None,
            "currency":          None,
            "bid_amount":        None,
            "category":          category,
            "created_date":      entry.get("created_date", ""),
            "pdf_url":           entry.get("pdf_url", ""),
            "detail_url":        entry.get("detail_url", ""),
        })

    return rows


def _extract_bidders_from_tables(pdf_path: Path) -> list[tuple]:
    """
    Fallback table extractor — returns list of (bid_num, name, currency, amount_str).
    """
    results = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                for table in (page.extract_tables() or []):
                    for row in table:
                        if not row or len(row) < 3:
                            continue
                        cells = [(c or "").strip() for c in row]
                        # Check if first cell looks like "T1", "T2", ...
                        bid_m = re.match(r"^T(\d+)$", cells[0], re.I)
                        if not bid_m:
                            continue
                        bid_num = bid_m.group(1)
                        name = cells[1] if len(cells) > 1 else ""
                        # Find currency + amount in remaining cells
                        rest = " ".join(cells[2:])
                        cur, amt = parse_amount(rest)
                        if amt is not None:
                            results.append((bid_num, name, cur, str(amt)))
    except Exception:
        pass
    return results

# ---------------------------------------------------------------------------
# Step 5 — Summary + Excel export
# ---------------------------------------------------------------------------

def print_summary(df: pd.DataFrame):
    sr = df[df["currency"] == "SR"].copy()
    total_tenders = df["detail_url"].nunique()
    total_bids    = df["bid_number"].notna().sum()
    total_sr      = sr["bid_amount"].sum()

    print("\n" + "=" * 65)
    print("  NTB SEYCHELLES — MINUTES OF TENDERS SUMMARY")
    print("=" * 65)
    print(f"  Unique tenders parsed:            {total_tenders:>8,}")
    print(f"  Total bids (all currencies):      {total_bids:>8,}")
    print(f"  SR-denominated bids:              {sr['bid_number'].notna().sum():>8,}")
    print(f"  Total SR value bid:               SR {total_sr:>18,.2f}")
    print(f"  Unique bidding companies:         {df['bidder_name'].nunique():>8,}")
    print(f"  Unique procuring entities:        {df['org'].nunique():>8,}")

    print("\n  TENDERS BY CATEGORY (count)")
    print("  " + "-" * 55)
    cat_counts = df.groupby("category")["detail_url"].nunique().sort_values(ascending=False)
    for cat, cnt in cat_counts.items():
        print(f"  {cat:<30}  {cnt:>5,} tenders")

    print("\n  TOP 10 MOST ACTIVE PROCURING ENTITIES")
    print("  " + "-" * 55)
    top_orgs = df.groupby("org")["detail_url"].nunique().sort_values(ascending=False).head(10)
    for org, cnt in top_orgs.items():
        label = (org[:40] + "…") if len(org) > 40 else org
        print(f"  {label:<42}  {cnt:>4,} tenders")

    print("\n  TOP 10 MOST FREQUENT BIDDERS")
    print("  " + "-" * 55)
    top_bidders = df.groupby("bidder_name")["detail_url"].nunique().sort_values(ascending=False).head(10)
    for bidder, cnt in top_bidders.items():
        label = (bidder[:40] + "…") if len((bidder or "")) > 40 else (bidder or "")
        print(f"  {label:<42}  {cnt:>4,} tenders bid on")

    print("=" * 65 + "\n")


def save_excel(df: pd.DataFrame, out_path: Path):
    xl_path = out_path.with_suffix(".xlsx")
    sr = df[df["currency"] == "SR"].copy()

    with pd.ExcelWriter(xl_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="All bids", index=False)

        # Summary: tenders per org
        (
            df.groupby("org")["detail_url"].nunique()
            .sort_values(ascending=False)
            .reset_index()
            .rename(columns={"detail_url": "tender_count"})
            .to_excel(writer, sheet_name="By org", index=False)
        )

        # Summary: tenders per category
        (
            df.groupby("category")["detail_url"].nunique()
            .sort_values(ascending=False)
            .reset_index()
            .rename(columns={"detail_url": "tender_count"})
            .to_excel(writer, sheet_name="By category", index=False)
        )

        # Most frequent bidders
        (
            df.groupby("bidder_name")["detail_url"].nunique()
            .sort_values(ascending=False)
            .reset_index()
            .rename(columns={"detail_url": "tenders_bid_on"})
            .to_excel(writer, sheet_name="Top bidders", index=False)
        )

        # SR bid value by org
        if not sr.empty:
            (
                sr.groupby("org")["bid_amount"].sum()
                .sort_values(ascending=False)
                .reset_index()
                .rename(columns={"bid_amount": "total_sr_bid"})
                .to_excel(writer, sheet_name="SR value by org", index=False)
            )

    log.info("Excel workbook saved to %s", xl_path)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Scrape NTB Seychelles Minutes of Tenders."
    )
    parser.add_argument("--out", default=DEFAULT_OUT,
                        help=f"Output CSV file (default: {DEFAULT_OUT})")
    parser.add_argument("--pdf-dir", default=DEFAULT_PDF_DIR,
                        help=f"Directory for PDFs (default: {DEFAULT_PDF_DIR})")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES,
                        help="Max listing pages to crawl (default: all)")
    parser.add_argument("--skip-download", action="store_true",
                        help="Parse only PDFs already in --pdf-dir")
    parser.add_argument("--sr-only", action="store_true",
                        help="Keep only SR-denominated bids in output")
    parser.add_argument("--no-excel", action="store_true",
                        help="Skip Excel export")
    args = parser.parse_args()

    pdf_dir = Path(args.pdf_dir)
    pdf_dir.mkdir(parents=True, exist_ok=True)

    # ---- 1. Collect listing entries ----------------------------------------
    if args.skip_download:
        log.info("--skip-download: building entries from local PDFs in %s", pdf_dir)
        entries = []
        for pdf_path in sorted(pdf_dir.glob("*.pdf")):
            entries.append({
                "title":        pdf_path.stem.replace("_", " ").replace("-", " ").title(),
                "org":          "",
                "created_date": "",
                "pdf_url":      "",   # not needed for local parse
                "detail_url":   str(pdf_path),  # used as unique key
                "_local_path":  pdf_path,
            })
        url_to_path = {e["detail_url"]: e["_local_path"] for e in entries}
    else:
        entries = collect_listing_entries(max_pages=args.max_pages)

        # ---- 2. Resolve missing PDF URLs ------------------------------------
        entries = resolve_pdf_urls(entries)

        # ---- 3. Download PDFs ----------------------------------------------
        url_to_path = download_pdfs(entries, pdf_dir)

    # ---- 4. Parse PDFs ------------------------------------------------------
    log.info("\nParsing %d PDF(s)…", len(entries))
    all_rows = []

    for entry in entries:
        if args.skip_download:
            local_path = entry.get("_local_path")
        else:
            local_path = url_to_path.get(entry.get("pdf_url", ""))

        if not local_path or not Path(local_path).exists():
            log.warning("  No local PDF for: %s — skipping", entry.get("title", "?"))
            continue

        log.info("  Parsing: %s", Path(local_path).name)
        rows = parse_minutes_pdf(Path(local_path), entry)
        all_rows.extend(rows)

    if not all_rows:
        log.error("No data extracted. Check your PDFs and parser.")
        return

    # ---- 5. Build and clean DataFrame ---------------------------------------
    df = pd.DataFrame(all_rows)
    df["bidder_name"] = df["bidder_name"].str.strip()
    df["org"]         = df["org"].str.strip()
    df["tender_description"] = df["tender_description"].str.strip()

    if args.sr_only:
        df = df[df["currency"] == "SR"].copy()
        log.info("SR-only filter applied: %d rows retained", len(df))

    # ---- 6. Save CSV --------------------------------------------------------
    out_path = Path(args.out)
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    log.info("Saved %d rows → %s", len(df), out_path)

    # ---- 7. Print summary ---------------------------------------------------
    print_summary(df)

    # ---- 8. Excel export ----------------------------------------------------
    if not args.no_excel:
        try:
            save_excel(df, out_path)
        except ImportError:
            log.info("openpyxl not installed — skipping Excel (pip install openpyxl)")


if __name__ == "__main__":
    main()
