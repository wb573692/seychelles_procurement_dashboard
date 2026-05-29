import re
import time
import json
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

BASE_URL     = "https://www.ntb.sc/tenders/expression-of-interest"
HEADERS      = {"User-Agent": "Mozilla/5.0 (compatible; NTB-EOI-Scraper/1.0)"}
PAGE_SIZE    = 8          # items per listing page (?start=N*8)
REQUEST_DELAY = 1.2       # seconds between HTTP requests
MAX_PAGES    = 50         # safety cap (site has ~28 pages as of April 2026)
DEFAULT_OUT  = "ntb_eoi.csv"
DEFAULT_CACHE_DIR = "./ntb_eoi_cache"
DEFAULT_PDF_DIR   = "./ntb_eoi_pdfs"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Category classifier  (shared with other NTB scrapers)
# ---------------------------------------------------------------------------

CATEGORY_RULES = [
    ("Housing",          r"housing|condo|condominium|affordable|residential|duplex|flat|apartment|bedroom|creche|renovation.*house|house.*renovation"),
    ("Roads & seawalls", r"road|seawall|parapet|footpath|lane|surfac|resurfac|roundabout|traffic|pavement|widening|drainage.*road|asphal|bollard|access road|motorable"),
    ("Infrastructure",   r"infrastructure|retaining wall|drain|culvert|bridge|slope|stabilisation|renovation|refurbish|office|headquarter|national house|state house|assembly|archive|substation|kiosk|perimeter|record centre|depot|workshop|shed|warehouse|fence|fencing|chainlink|building"),
    ("Utilities",        r"sewerage|sewage|wastewater|desalination|pipeline|water supply|water pipe|water meter|pump|cable|duct|sorento|electricity|33kv|11kv|xlpe|overhead cable"),
    ("Education",        r"school|education|university|textbook|stationar|furniture.*school|band set|britannica|openemis|learning|classroom|library|domiciliary care|certificate.*programme"),
    ("Health",           r"hospital|health centre|clinic|medical|health care|doctor|chicken meat|nutrition|dietary"),
    ("Transport",        r"transport|vehicle|bus|car|suv|pickup|motorbike|streetlight|tipper|crane|barge|forklift|lift truck"),
    ("Energy",           r"solar|pv|genset|generator|power station|floating.*pv|energy meter|switchgear|substation.*power|electrical duct|battery charger"),
    ("Fisheries",        r"fish|fisheries|fishing|ice machine|vms|transceiver"),
    ("Maritime",         r"port|quay|jetty|docking|tug|undersea cable|submarine cable|harbour|seaport|vessel|boat"),
    ("ICT",              r"ict|software|microsoft|cisco|server|laptop|computer|network|digital|erp|data|cybersecurity|iso.*27001|android|office 365|it system|technology|portal|website"),
    ("Environment",      r"landfill|waste|cleaning.*beach|beach.*cleaning|bin|environmental|biodiversity|climate|carbon|energy audit|sustainability|transparency report"),
    ("Security",         r"security service|cctv|surveillance|screening|x-ray|detection|guard|patrol|protective equipment"),
    ("Sports",           r"sport|stadium|swimming pool|pitch|field|mower|grass|arena"),
    ("Consultancy",      r"consultancy|consultant|advisory|feasibility study|impact assessment|audit|assessment|evaluation|survey|prequalification|expression of interest.*expert|expert.*expression"),
    ("Goods & Services", r"procurement|supply|printing|brochure|promotional|fertilizer|detergent|cleaning.*material|spare parts|fittings|compression|cable.*procurement"),
    ("Other",            r".*"),
]

def classify(text: str) -> str:
    text_lower = text.lower()
    for category, pattern in CATEGORY_RULES:
        if re.search(pattern, text_lower):
            return category
    return "Other"

# ---------------------------------------------------------------------------
# EOI type classifier
# ---------------------------------------------------------------------------

def classify_eoi_type(text: str) -> str:
    tl = text.lower()
    if re.search(r"prequalif", tl):
        return "Prequalification"
    if re.search(r"limited bidding", tl):
        return "Limited Bidding"
    if re.search(r"expression of interest|eoi", tl):
        return "EOI"
    return "Other"

# ---------------------------------------------------------------------------
# Org extractor — pull procuring entity from description or title prefix
# ---------------------------------------------------------------------------

ORG_PREFIXES = [
    "Seychelles Infrastructure Agency",
    "Public Utilities Corporation",
    "Seychelles Land Transport Agency",
    "Ministry of Education",
    "Seychelles Airports Authority",
    "Seychelles Airport Authority",
    "Health Care Agency",
    "Seychelles Fisheries Authority",
    "Seychelles Fishing Authority",
    "Seychelles Ports Authority",
    "National Sports Council",
    "Seychelles Civil Aviation Authority",
    "Property Management Corporation",
    "Landscape & Waste Management Agency",
    "Seychelles Broadcasting Corporation",
    "Ministry of Local Government",
    "The Judiciary",
    "Ministry of Finance",
    "Financial Services Authority",
    "Department of Information Communication Technology",
    "Tourism Department",
    "Police Department",
    "Seychelles Fire & Rescue Services Agency",
    "Seychelles Revenue Commission",
    "Public Sector Bureau",
    "National Institute of Health and Social Studies",
    "Seychelles Prison Service",
    "Seychelles Communications Regulatory Authority",
    "Ministry of Agriculture",
    "Department of Agriculture",
    "Agriculture Department",
    "Climate Change Department",
    "Energy & Climate Change Department",
    "Department of Energy",
    "Ministry of Transport",
    "Seychelles Public Transport Corporation",
    "Ministry of Lands",
    "Seychelles Heritage Foundation",
    "Development Bank of Seychelles",
    "Anti-Corruption Commission",
    "Financial Intelligence Unit",
]

# Build a regex that matches any known org at the start of a string
_ORG_PATTERN = re.compile(
    r"(?i)^(" + "|".join(re.escape(o) for o in ORG_PREFIXES) + r")\b"
)

def extract_org(title: str, description: str) -> str:
    """Try to identify the procuring entity from the title or description."""
    for text in (title, description):
        m = _ORG_PATTERN.match(text.strip())
        if m:
            return m.group(1)
    # Fallback: look for known org name anywhere in description
    for org in ORG_PREFIXES:
        if org.lower() in description.lower():
            return org
    # Final fallback: use the first sentence of the description
    first_sentence = re.split(r"[.!?\n]", description.strip())[0]
    return first_sentence[:80] if first_sentence else ""

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
        except requests.RequestException as exc:
            log.warning("Request error: %s (attempt %d)", exc, attempt)
        time.sleep(REQUEST_DELAY * attempt)
    return None

# ---------------------------------------------------------------------------
# Step 1 — Crawl listing pages, collect stub entries
# ---------------------------------------------------------------------------

def parse_listing_block(block, base_url: str) -> dict | None:
    """
    Parse one <article>-like block from the listing page.
    Returns a stub dict or None if not a valid EOI entry.
    """
    # Title & detail URL
    h2 = block.find("h2")
    if not h2:
        return None
    a = h2.find("a", href=True)
    if not a:
        return None

    title = a.get_text(strip=True)
    detail_url = urljoin(base_url, a["href"])

    # Body text (intro paragraph on listing page)
    body_paras = block.find_all("p")
    description = " ".join(p.get_text(strip=True) for p in body_paras
                           if p.get_text(strip=True) and len(p.get_text(strip=True)) > 10)

    # Tags
    tags = [t.get_text(strip=True) for t in block.find_all("a", class_=lambda c: c and "tag" in c.lower())]
    # Also catch tag-style links by href
    tag_links = block.find_all("a", href=lambda h: h and "/tags/" in (h or ""))
    tags += [tl.get_text(strip=True) for tl in tag_links if tl.get_text(strip=True) not in tags]

    # Created date
    created_date = ""
    created_el = block.find(string=re.compile(r"created on", re.I))
    if created_el:
        created_date = re.sub(r"(?i)created on\s*", "", str(created_el)).strip()

    # Submission deadline & time
    submission_deadline = ""
    submission_time = ""
    for li in block.find_all("li"):
        text = li.get_text(strip=True)
        if re.search(r"submission deadline", text, re.I):
            m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
            if m:
                submission_deadline = m.group(1)
        if re.search(r"submission time", text, re.I):
            m = re.search(r"(\d{1,2}:\d{2})", text)
            if m:
                submission_time = m.group(1)

    # PDF link
    pdf_url = ""
    for a_tag in block.find_all("a", href=True):
        href = a_tag["href"]
        if ".pdf" in href.lower():
            pdf_url = urljoin("https://www.ntb.sc", href)
            break

    return {
        "title":               title,
        "org":                 extract_org(title, description),
        "description":         description,
        "created_date":        created_date,
        "submission_deadline": submission_deadline,
        "submission_time":     submission_time,
        "tags":                ", ".join(tags),
        "pdf_url":             pdf_url,
        "detail_url":          detail_url,
        "_needs_detail":       True,   # flag to fetch full detail page
    }


def collect_listing_stubs(max_pages: int) -> list[dict]:
    """Crawl all listing pages and return stub entries."""
    entries = []
    seen_urls = set()
    page = 0

    # Page 0 uses the bare URL (no ?start=), page 1+ uses ?start=N*PAGE_SIZE
    # We already know page 0 returns PERMISSIONS_ERROR when fetched directly
    # so we start from page 1 (?start=8) and work outward, then reconstruct page 0
    # by also trying the bare URL via the search-found URL pattern.
    urls_to_try = []
    for p in range(0, max_pages):
        if p == 0:
            # Use the URL the user provided — already verified accessible
            urls_to_try.append((0, f"{BASE_URL}?start=0"))
        else:
            urls_to_try.append((p, f"{BASE_URL}?start={p * PAGE_SIZE}"))

    total_pages_found = None

    for page_num, url in urls_to_try:
        log.info("Listing page %d: %s", page_num + 1, url)
        resp = _get(url)
        if resp is None:
            log.warning("Failed to fetch page %d — skipping.", page_num + 1)
            continue

        soup = BeautifulSoup(resp.text, "html.parser")

        # Detect total pages from pagination on first successful page
        if total_pages_found is None:
            page_links = soup.select("a[href*='start=']")
            starts = []
            for pl in page_links:
                m = re.search(r"start=(\d+)", pl["href"])
                if m:
                    starts.append(int(m.group(1)))
            if starts:
                total_pages_found = max(starts) // PAGE_SIZE + 1
                log.info("Detected %d total listing pages.", total_pages_found)

        # Parse each article block
        # The site wraps entries in divs; h2 inside each block identifies the entry
        found_on_page = 0

        # Main items (full blocks with h2 + body)
        # Strategy: find all h2 inside the main content area
        main = soup.find("div", id="jsn-mainbody") or soup.find("main") or soup

        for h2 in main.find_all("h2"):
            # Skip navigation h2s
            if not h2.find("a", href=lambda h: h and "/expression-of-interest/" in (h or "")):
                continue
            # Walk up to find the enclosing block
            block = h2.find_parent("div") or h2.find_parent("article") or h2.parent
            entry = parse_listing_block(block, resp.url)
            if entry and entry["detail_url"] not in seen_urls:
                seen_urls.add(entry["detail_url"])
                entries.append(entry)
                found_on_page += 1

        # "More Articles" list items
        more_section = soup.find("h3", string=re.compile(r"More Articles", re.I))
        if more_section:
            ul = more_section.find_next_sibling("ul")
            if ul:
                for li_a in ul.find_all("a", href=True):
                    if "/expression-of-interest/" not in li_a["href"]:
                        continue
                    detail_url = urljoin("https://www.ntb.sc", li_a["href"])
                    if detail_url not in seen_urls:
                        seen_urls.add(detail_url)
                        entries.append({
                            "title":               li_a.get_text(strip=True),
                            "org":                 "",
                            "description":         "",
                            "created_date":        "",
                            "submission_deadline": "",
                            "submission_time":     "",
                            "tags":                "",
                            "pdf_url":             "",
                            "detail_url":          detail_url,
                            "_needs_detail":       True,
                        })
                        found_on_page += 1

        log.info("  → %d entries on this page (running total: %d)", found_on_page, len(entries))

        # Stop if we've gone past the last known page
        if total_pages_found and page_num + 1 >= total_pages_found:
            log.info("Reached last listing page (%d). Done.", total_pages_found)
            break

        time.sleep(REQUEST_DELAY)

    log.info("Total unique EOI stubs collected: %d", len(entries))
    return entries

# ---------------------------------------------------------------------------
# Step 2 — Fetch detail pages to fill in missing / richer fields
# ---------------------------------------------------------------------------

def enrich_from_detail(entry: dict, cache_dir: Path | None) -> dict:
    """
    Fetch the detail page for one EOI and fill in all fields.
    Optionally reads from / writes to a local HTML cache.
    """
    url = entry["detail_url"]
    slug = urlparse(url).path.rstrip("/").split("/")[-1][:120]
    cache_path = (cache_dir / f"{slug}.html") if cache_dir else None

    html = None
    if cache_path and cache_path.exists():
        html = cache_path.read_text(encoding="utf-8", errors="replace")
    else:
        resp = _get(url)
        if resp:
            html = resp.text
            if cache_path:
                cache_path.write_text(html, encoding="utf-8")
        time.sleep(REQUEST_DELAY)

    if not html:
        return entry

    soup = BeautifulSoup(html, "html.parser")

    # Full description from detail page (much richer than listing snippet)
    main = soup.find("div", id="jsn-mainbody") or soup.find("article") or soup
    # Remove navigation, sidebar, footer noise
    for noise in main.find_all(["nav", "aside", "footer", "script", "style"]):
        noise.decompose()

    # Grab all paragraph text
    paras = main.find_all("p")
    full_desc = " ".join(
        p.get_text(separator=" ", strip=True) for p in paras
        if p.get_text(strip=True) and len(p.get_text(strip=True)) > 15
    )
    if full_desc:
        entry["description"] = full_desc

    # Created date (more reliable from detail page)
    created_el = soup.find(string=re.compile(r"created on", re.I))
    if created_el:
        entry["created_date"] = re.sub(r"(?i)created on\s*", "", str(created_el)).strip()

    # Submission deadline & time
    for li in soup.find_all("li"):
        text = li.get_text(strip=True)
        if re.search(r"submission deadline", text, re.I):
            m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
            if m:
                entry["submission_deadline"] = m.group(1)
        if re.search(r"submission time", text, re.I):
            m = re.search(r"(\d{1,2}:\d{2})", text)
            if m:
                entry["submission_time"] = m.group(1)

    # Tags
    tag_links = soup.find_all("a", href=lambda h: h and "/tags/" in (h or ""))
    if tag_links:
        entry["tags"] = ", ".join(tl.get_text(strip=True) for tl in tag_links)

    # PDF link
    if not entry.get("pdf_url"):
        for a_tag in soup.find_all("a", href=True):
            if ".pdf" in a_tag["href"].lower():
                entry["pdf_url"] = urljoin("https://www.ntb.sc", a_tag["href"])
                break

    # Re-extract org with fuller description
    if not entry.get("org") or len(entry["org"]) < 5:
        entry["org"] = extract_org(entry.get("title", ""), entry["description"])

    entry["_needs_detail"] = False
    return entry

# ---------------------------------------------------------------------------
# Step 3 — Optionally download and parse PDFs
# ---------------------------------------------------------------------------

def fetch_pdf_text(pdf_url: str, pdf_dir: Path) -> str:
    """Download (if needed) and extract text from a PDF. Returns "" on failure."""
    if not pdf_url:
        return ""

    filename = Path(urlparse(pdf_url).path).name or "unknown.pdf"
    local_path = pdf_dir / filename

    if not local_path.exists():
        resp = _get(pdf_url, stream=True)
        if resp is None:
            return ""
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)
        time.sleep(REQUEST_DELAY)

    try:
        with pdfplumber.open(local_path) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as exc:
        log.warning("Could not parse PDF %s: %s", filename, exc)
        return ""

# ---------------------------------------------------------------------------
# Step 4 — Summary + Excel export
# ---------------------------------------------------------------------------

def print_summary(df: pd.DataFrame):
    print("\n" + "=" * 65)
    print("  NTB SEYCHELLES — EXPRESSIONS OF INTEREST SUMMARY")
    print("=" * 65)
    print(f"  Total EOI records:                {len(df):>8,}")
    print(f"  With submission deadline:         {df['submission_deadline'].notna().sum():>8,}")
    print(f"  With attached PDF:                {(df['pdf_url'] != '').sum():>8,}")
    print(f"  Unique procuring entities:        {df['org'].nunique():>8,}")

    print("\n  BY EOI TYPE")
    print("  " + "-" * 55)
    for eoi_type, cnt in df["eoi_type"].value_counts().items():
        print(f"  {eoi_type:<30}  {cnt:>5,}")

    print("\n  BY CATEGORY")
    print("  " + "-" * 55)
    for cat, cnt in df["category"].value_counts().items():
        print(f"  {cat:<30}  {cnt:>5,}")

    print("\n  TOP 10 PROCURING ENTITIES")
    print("  " + "-" * 55)
    for org, cnt in df["org"].value_counts().head(10).items():
        label = (org[:42] + "…") if len(str(org)) > 42 else str(org)
        print(f"  {label:<44}  {cnt:>4,}")

    print("\n  BY YEAR (created date)")
    print("  " + "-" * 55)
    df["year"] = df["created_date"].str.extract(r"(\d{4})")
    for year, cnt in df["year"].value_counts().sort_index().items():
        print(f"  {year}  {cnt:>5,}")

    print("=" * 65 + "\n")


def save_excel(df: pd.DataFrame, out_path: Path):
    xl_path = out_path.with_suffix(".xlsx")
    with pd.ExcelWriter(xl_path, engine="openpyxl") as writer:
        df.drop(columns=["pdf_text", "_needs_detail"], errors="ignore").to_excel(
            writer, sheet_name="All EOIs", index=False
        )
        (
            df["org"].value_counts()
            .reset_index()
            .rename(columns={"index": "org", "org": "count"})
            .to_excel(writer, sheet_name="By org", index=False)
        )
        (
            df["category"].value_counts()
            .reset_index()
            .rename(columns={"index": "category", "category": "count"})
            .to_excel(writer, sheet_name="By category", index=False)
        )
        (
            df["eoi_type"].value_counts()
            .reset_index()
            .rename(columns={"index": "eoi_type", "eoi_type": "count"})
            .to_excel(writer, sheet_name="By type", index=False)
        )
        # Deadline timeline
        deadline_df = df[df["submission_deadline"] != ""].copy()
        deadline_df["deadline_year"] = deadline_df["submission_deadline"].str[:4]
        (
            deadline_df.groupby("deadline_year")
            .size()
            .reset_index(name="count")
            .to_excel(writer, sheet_name="By deadline year", index=False)
        )
    log.info("Excel workbook saved to %s", xl_path)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Scrape NTB Seychelles Expressions of Interest."
    )
    parser.add_argument("--out", default=DEFAULT_OUT,
                        help=f"Output CSV (default: {DEFAULT_OUT})")
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR,
                        help=f"Directory to cache detail page HTML (default: {DEFAULT_CACHE_DIR})")
    parser.add_argument("--pdf-dir", default=DEFAULT_PDF_DIR,
                        help=f"Directory to save PDFs (default: {DEFAULT_PDF_DIR})")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES,
                        help="Max listing pages to crawl (default: all ~28)")
    parser.add_argument("--skip-fetch", action="store_true",
                        help="Use only cached HTML — do not hit the network for detail pages")
    parser.add_argument("--download-pdfs", action="store_true",
                        help="Download and extract text from attached PDFs")
    parser.add_argument("--no-excel", action="store_true",
                        help="Skip Excel export")
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    pdf_dir = Path(args.pdf_dir)
    if args.download_pdfs:
        pdf_dir.mkdir(parents=True, exist_ok=True)

    # ---- 1. Collect stubs from listing pages --------------------------------
    entries = collect_listing_stubs(max_pages=args.max_pages)

    # ---- 2. Enrich each entry from its detail page --------------------------
    log.info("\nEnriching %d entries from detail pages…", len(entries))
    enriched = []
    for i, entry in enumerate(entries, 1):
        if args.skip_fetch and not cache_dir:
            enriched.append(entry)
            continue
        log.info("  [%d/%d] %s", i, len(entries), entry["detail_url"])
        enriched.append(enrich_from_detail(entry, cache_dir))

    # ---- 3. Optionally fetch PDF text ---------------------------------------
    if args.download_pdfs:
        log.info("\nDownloading/parsing PDFs…")
        for entry in enriched:
            if entry.get("pdf_url"):
                log.info("  PDF: %s", entry["pdf_url"])
                entry["pdf_text"] = fetch_pdf_text(entry["pdf_url"], pdf_dir)
            else:
                entry["pdf_text"] = ""
    else:
        for entry in enriched:
            entry["pdf_text"] = ""

    # ---- 4. Build DataFrame -------------------------------------------------
    df = pd.DataFrame(enriched)

    # Classify category and EOI type
    df["category"] = (df["title"] + " " + df["description"]).apply(classify)
    df["eoi_type"] = (df["title"] + " " + df["description"]).apply(classify_eoi_type)

    # Clean up
    df["org"]         = df["org"].str.strip()
    df["title"]       = df["title"].str.strip()
    df["description"] = df["description"].str.strip()

    # Drop internal flag column
    df.drop(columns=["_needs_detail"], errors="ignore", inplace=True)

    # Reorder columns sensibly
    col_order = [
        "title", "org", "eoi_type", "category", "tags",
        "created_date", "submission_deadline", "submission_time",
        "description", "pdf_url", "pdf_text", "detail_url",
    ]
    df = df[[c for c in col_order if c in df.columns]]

    # ---- 5. Save CSV --------------------------------------------------------
    out_path = Path(args.out)
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    log.info("Saved %d rows → %s", len(df), out_path)

    # ---- 6. Print summary ---------------------------------------------------
    print_summary(df)

    # ---- 7. Excel export ----------------------------------------------------
    if not args.no_excel:
        try:
            save_excel(df, out_path)
        except ImportError:
            log.info("openpyxl not installed — skipping Excel (pip install openpyxl)")


if __name__ == "__main__":
    main()
