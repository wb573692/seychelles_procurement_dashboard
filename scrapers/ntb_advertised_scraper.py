"""
NTB Seychelles — Advertised Tenders Scraper
============================================
Crawls every pagination page of:
    https://www.ntb.sc/tenders?start=X
Fetches each detail page, downloads attached PDFs (where available),
and extracts every structured field into a flat CSV + Excel workbook.

Key difference from other NTB scrapers
---------------------------------------
Advertised tender PDFs are MOSTLY accessible (unlike awarded tenders
which mostly 404). PDFs contain richer fields not present on the HTML
page: source of finance, contractor class / eligibility, performance
period, dossier collection fee, pre-bid meeting date, place of
performance, and contact emails. The scraper extracts all of these.

Output columns
--------------
    title                – Tender title
    org                  – Procuring entity
    source_of_finance    – e.g. "Government of Seychelles" / "Public Utilities Corporation"
    project_title        – Formal project title (often same as title)
    eligibility          – Raw eligibility text
    contractor_class     – Extracted class level(s) e.g. "Class 1 & 2"
    performance_period   – e.g. "12 Calendar Weeks"
    place_of_performance – e.g. "Plaisance Primary School"
    dossier_fee          – e.g. "SR 350.00" / "Free"
    pre_bid_meeting      – Date/time of mandatory pre-bid meeting
    contact_email        – Contact email(s) from PDF
    tags                 – comma-separated site tags
    category             – inferred project category
    created_date         – e.g. "04 March 2026"
    submission_deadline  – ISO date e.g. "2026-03-26"
    submission_time      – e.g. "10:00"
    pdf_url              – URL of attached PDF (may be .pdf or .docx)
    pdf_accessible       – True/False
    description          – Full body text from the detail/listing page
    detail_url           – Canonical detail page URL

Requirements
------------
    pip install requests beautifulsoup4 pdfplumber pandas openpyxl

Usage
-----
    # Full scrape — all 153 pages, downloads every accessible PDF
    python ntb_advertised_scraper.py

    # Test run — first 5 pages only
    python ntb_advertised_scraper.py --max-pages 5

    # Re-use cached HTML, skip re-downloading PDFs
    python ntb_advertised_scraper.py --skip-fetch --cache-dir ./ntb_adv_cache

    # Skip PDF download/parsing entirely (much faster, HTML data only)
    python ntb_advertised_scraper.py --no-pdfs

    # Custom output
    python ntb_advertised_scraper.py --out advertised.csv
"""

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

BASE_URL      = "https://www.ntb.sc/tenders"
HEADERS       = {"User-Agent": "Mozilla/5.0 (compatible; NTB-Advertised-Scraper/1.0)"}
PAGE_SIZE     = 8
REQUEST_DELAY = 1.2
MAX_PAGES     = 200        # safety cap (~153 pages as of April 2026)
DEFAULT_OUT       = "ntb_advertised.csv"
DEFAULT_CACHE_DIR = "./ntb_adv_cache"
DEFAULT_PDF_DIR   = "./ntb_adv_pdfs"

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
    ("Housing",          r"housing|condo|condominium|affordable|residential|duplex|flat|apartment|bedroom|creche|kampala"),
    ("Roads & seawalls", r"road|seawall|parapet|footpath|lane|surfac|resurfac|roundabout|traffic|pavement|widening|drainage.*road|asphal|bollard|access road|motorable|sluice gate"),
    ("Infrastructure",   r"infrastructure|retaining wall|drain|culvert|bridge|slope|stabilisation|renovation|refurbish|office|headquarter|national house|state house|assembly|archive|substation|kiosk|perimeter|record centre|depot|workshop|shed|warehouse|fence|fencing|chainlink|building|toilet facil|carport"),
    ("Utilities",        r"sewerage|sewage|wastewater|desalination|pipeline|water supply|water pipe|water meter|pump|cable.*laying|duct|duct laying|hdpe|soda ash|sodium|poly.alumin|chlor"),
    ("Education",        r"school|education|university|textbook|stationar|furniture.*school|band set|britannica|learning|classroom|library|creche"),
    ("Health",           r"hospital|health centre|clinic|medical|health care|doctor|chicken meat|nutrition|dietary|maintenance.*health"),
    ("Transport",        r"transport|vehicle|bus|car|suv|pickup|motorbike|streetlight|tipper|crane|barge|forklift|lift truck|van|truck"),
    ("Energy",           r"solar|pv|genset|generator|power station|floating.*pv|energy meter|switchgear|substation.*power|electrical duct|battery charger|33kv|11kv|xlpe|overhead cable"),
    ("Fisheries",        r"fish|fisheries|fishing|ice machine|vms|transceiver"),
    ("Maritime",         r"port|quay|jetty|docking|tug|undersea cable|submarine cable|harbour|seaport|vessel|boat"),
    ("ICT",              r"ict|software|microsoft|cisco|server|laptop|computer|network|digital|erp|data|cybersecurity|iso.*27001|android|office 365|it system|technology|portal|website"),
    ("Environment",      r"landfill|waste.*bin|bin.*collection|waste collection|cleaning.*beach|beach.*cleaning|environmental|climate|carbon|energy audit|sustainability|ground maintenance|beautification"),
    ("Security",         r"security service|cctv|surveillance|screening|x-ray|detection|guard|patrol|protective equipment|fire"),
    ("Sports",           r"sport|stadium|swimming pool|pitch|field|mower|grass|arena"),
    ("Consultancy",      r"consultancy|consultant|advisory|feasibility study|impact assessment|audit|assessment|evaluation|survey|prequalification"),
    ("Goods & Services", r"procurement.*supply|supply.*procurement|printing|brochure|promotional|fertilizer|detergent|cleaning.*material|spare parts|fittings|compression|cable.*procurement|procurement.*cable|procurement.*vehicle|air.condition|maintenance.*service|maintenance service"),
    ("Other",            r".*"),
]

def classify(text: str) -> str:
    tl = text.lower()
    for category, pattern in CATEGORY_RULES:
        if re.search(pattern, tl):
            return category
    return "Other"

# ---------------------------------------------------------------------------
# Org extractor
# ---------------------------------------------------------------------------

KNOWN_ORGS = [
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
    "Ministry of Local Government & Community Affairs",
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
    "Ministry of Lands and Housing",
    "Ministry of Lands & Housing",
    "Seychelles Heritage Foundation",
    "Development Bank of Seychelles",
    "Anti-Corruption Commission",
    "Financial Intelligence Unit",
    "National Information Sharing Coordination Centre",
    "Seychelles Maritime Safety Administration",
    "Seychelles National Parks Authority",
]

_ORG_RE = re.compile(
    r"(?i)(" + "|".join(re.escape(o) for o in KNOWN_ORGS) + r")"
)

def extract_org(title: str, description: str, pdf_text: str = "") -> str:
    for corpus in (title, description, pdf_text):
        m = _ORG_RE.search(corpus)
        if m:
            return m.group(1)
    return ""

# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _get(url: str, stream: bool = False, retries: int = 3):
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, stream=stream, timeout=30)
            if r.status_code == 200:
                return r
            log.warning("HTTP %s → %s (attempt %d)", r.status_code, url, attempt)
        except requests.RequestException as exc:
            log.warning("Request error: %s (attempt %d)", exc, attempt)
        time.sleep(REQUEST_DELAY * attempt)
    return None

# ---------------------------------------------------------------------------
# Step 1 — Parse one listing-page HTML block into a stub entry
# ---------------------------------------------------------------------------

def _extract_deadline_time(block) -> tuple[str, str]:
    deadline, sub_time = "", ""
    for li in block.find_all("li"):
        text = li.get_text(" ", strip=True)
        if re.search(r"submission deadline", text, re.I):
            m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
            if m:
                deadline = m.group(1)
        if re.search(r"submission time", text, re.I):
            m = re.search(r"(\d{1,2}:\d{2})", text)
            if m:
                sub_time = m.group(1)
    return deadline, sub_time


def parse_listing_block(block, base_url: str) -> dict | None:
    h2 = block.find("h2")
    if not h2:
        return None
    a = h2.find("a", href=True)
    if not a or "/tenders/" not in a["href"]:
        return None

    title = a.get_text(strip=True)
    detail_url = urljoin(base_url, a["href"])

    # Body text
    paras = [
        p.get_text(" ", strip=True)
        for p in block.find_all("p")
        if len(p.get_text(strip=True)) > 10
    ]
    description = " ".join(paras)

    # Tags
    tag_links = block.find_all("a", href=lambda h: h and "/tags/" in (h or ""))
    tags = ", ".join(tl.get_text(strip=True) for tl in tag_links)

    # Created date
    created_date = ""
    for el in block.find_all(string=re.compile(r"created on", re.I)):
        created_date = re.sub(r"(?i)created on\s*", "", str(el)).strip()
        break

    deadline, sub_time = _extract_deadline_time(block)

    # PDF / attachment link
    pdf_url = ""
    for a_tag in block.find_all("a", href=True):
        href = a_tag["href"]
        if re.search(r"\.(pdf|docx|doc|xlsx)$", href, re.I):
            pdf_url = urljoin("https://www.ntb.sc", href)
            break

    return {
        "title":               title,
        "org":                 "",   # filled later
        "source_of_finance":   "",
        "project_title":       "",
        "eligibility":         "",
        "contractor_class":    "",
        "performance_period":  "",
        "place_of_performance":"",
        "dossier_fee":         "",
        "pre_bid_meeting":     "",
        "contact_email":       "",
        "tags":                tags,
        "category":            "",   # filled later
        "created_date":        created_date,
        "submission_deadline": deadline,
        "submission_time":     sub_time,
        "pdf_url":             pdf_url,
        "pdf_accessible":      False,
        "description":         description,
        "detail_url":          detail_url,
    }

# ---------------------------------------------------------------------------
# Step 2 — Crawl all listing pages
# ---------------------------------------------------------------------------

def collect_listing_stubs(max_pages: int) -> list[dict]:
    entries = []
    seen = set()
    total_pages = None
    page = 0

    while page < max_pages:
        url = BASE_URL if page == 0 else f"{BASE_URL}?start={page * PAGE_SIZE}"
        log.info("Listing page %d: %s", page + 1, url)
        resp = _get(url)
        if resp is None:
            log.warning("Failed to fetch page %d — stopping.", page + 1)
            break

        soup = BeautifulSoup(resp.text, "html.parser")

        # Detect total pages from pagination
        if total_pages is None:
            starts = [
                int(m.group(1))
                for a in soup.select("a[href*='start=']")
                if (m := re.search(r"start=(\d+)", a.get("href", "")))
            ]
            if starts:
                total_pages = max(starts) // PAGE_SIZE + 1
                log.info("Detected %d total listing pages.", total_pages)

        main = soup.find("div", id="jsn-mainbody") or soup

        found = 0
        for h2 in main.find_all("h2"):
            if not h2.find("a", href=lambda h: h and "/tenders/" in (h or "") and h != "/tenders"):
                continue
            block = h2.find_parent("div") or h2.find_parent("article") or h2.parent
            entry = parse_listing_block(block, resp.url)
            if entry and entry["detail_url"] not in seen:
                seen.add(entry["detail_url"])
                entries.append(entry)
                found += 1

        # "More Articles" overflow list
        more_h3 = soup.find("h3", string=re.compile(r"More Articles", re.I))
        if more_h3:
            ul = more_h3.find_next_sibling("ul")
            if ul:
                for li_a in ul.find_all("a", href=True):
                    if "/tenders/" not in li_a["href"] or li_a["href"] in ("/tenders", "/tenders/"):
                        continue
                    durl = urljoin("https://www.ntb.sc", li_a["href"])
                    if durl not in seen:
                        seen.add(durl)
                        stub = {k: "" for k in [
                            "title","org","source_of_finance","project_title",
                            "eligibility","contractor_class","performance_period",
                            "place_of_performance","dossier_fee","pre_bid_meeting",
                            "contact_email","tags","category","created_date",
                            "submission_deadline","submission_time","pdf_url",
                            "description",
                        ]}
                        stub["title"] = li_a.get_text(strip=True)
                        stub["detail_url"] = durl
                        stub["pdf_accessible"] = False
                        entries.append(stub)
                        found += 1

        log.info("  → %d on this page (total: %d)", found, len(entries))

        if total_pages and page + 1 >= total_pages:
            log.info("Reached last page (%d). Listing crawl done.", total_pages)
            break

        page += 1
        time.sleep(REQUEST_DELAY)

    log.info("Total unique advertised tender stubs: %d", len(entries))
    return entries

# ---------------------------------------------------------------------------
# Step 3 — Enrich from detail pages
# ---------------------------------------------------------------------------

def enrich_from_detail(entry: dict, cache_dir: Path | None) -> dict:
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
    main = soup.find("div", id="jsn-mainbody") or soup

    # Full description
    paras = [
        p.get_text(" ", strip=True)
        for p in main.find_all("p")
        if len(p.get_text(strip=True)) > 15
    ]
    full_desc = " ".join(paras)
    if full_desc:
        entry["description"] = full_desc

    # Structured fields from HTML text
    text = main.get_text(" ", strip=True)
    entry = _parse_structured_fields(text, entry)

    # Dates
    for el in soup.find_all(string=re.compile(r"created on", re.I)):
        entry["created_date"] = re.sub(r"(?i)created on\s*", "", str(el)).strip()
        break

    deadline, sub_time = _extract_deadline_time(main)
    if deadline:
        entry["submission_deadline"] = deadline
    if sub_time:
        entry["submission_time"] = sub_time

    # Tags
    tag_links = soup.find_all("a", href=lambda h: h and "/tags/" in (h or ""))
    if tag_links:
        entry["tags"] = ", ".join(tl.get_text(strip=True) for tl in tag_links)

    # PDF link
    if not entry.get("pdf_url"):
        for a_tag in soup.find_all("a", href=True):
            if re.search(r"\.(pdf|docx|doc)$", a_tag["href"], re.I):
                entry["pdf_url"] = urljoin("https://www.ntb.sc", a_tag["href"])
                break

    return entry


def _parse_structured_fields(text: str, entry: dict) -> dict:
    """Extract structured fields from raw page text."""

    # Source of finance
    m = re.search(r"source of finance[:\s]+(.+?)(?:\n|project title|eligibility|subject)", text, re.I | re.S)
    if m:
        entry["source_of_finance"] = m.group(1).strip()[:150]

    # Project title
    m = re.search(r"project title[:\s]+(.+?)(?:\n|eligibility|source|subject)", text, re.I | re.S)
    if m:
        entry["project_title"] = m.group(1).strip()[:200]

    # Eligibility (raw)
    m = re.search(r"eligibility[:\s]+(.+?)(?:\n\n|subject|conditions|scope|collection)", text, re.I | re.S)
    if m:
        entry["eligibility"] = m.group(1).strip()[:300]

    # Contractor class
    m = re.search(r"class\s+(one|two|three|1|2|3|i|ii|iii)\b.*?(?:class\s+(?:one|two|three|1|2|3|i|ii|iii))?", text, re.I)
    if m:
        # Find all class references
        classes = re.findall(r"class\s+(?:one|two|three|1|2|3|i+)\b", text, re.I)
        entry["contractor_class"] = ", ".join(dict.fromkeys(c.strip() for c in classes[:4]))

    # Performance / completion period
    m = re.search(r"(?:performance|completion|contract)\s+period[:\s]+(.+?)(?:\n|access|submission|dossier)", text, re.I | re.S)
    if m:
        entry["performance_period"] = m.group(1).strip()[:100]
    else:
        m = re.search(r"(\d+)\s+(?:calendar\s+)?weeks?", text, re.I)
        if m:
            entry["performance_period"] = m.group(0).strip()

    # Place of performance
    m = re.search(r"place of performance[:\s]+(.+?)(?:\n|performance period|access|submission)", text, re.I | re.S)
    if m:
        entry["place_of_performance"] = m.group(1).strip()[:150]

    # Dossier fee
    m = re.search(r"(?:fee|non.refundable)[^\n]*?(?:SR|SCR|SCR\s)\s*([\d,]+\.?\d*)|free of charge|given.*free|free.*given", text, re.I)
    if m:
        if re.search(r"free", m.group(0), re.I):
            entry["dossier_fee"] = "Free"
        else:
            entry["dossier_fee"] = f"SR {m.group(1)}"

    # Pre-bid meeting
    m = re.search(
        r"pre.bid meeting[^\n]*?(\d{1,2}(?:st|nd|rd|th)?\s+\w+\s+\d{4}(?:\s+at\s+[\d:]+\s*(?:am|pm|hrs)?)?)",
        text, re.I
    )
    if m:
        entry["pre_bid_meeting"] = m.group(1).strip()[:100]

    # Contact emails
    emails = re.findall(r"[\w.\-+]+@[\w.\-]+\.\w{2,}", text)
    # Filter out spam-protected placeholders
    real_emails = [e for e in emails if "spambots" not in e and len(e) < 80]
    if real_emails:
        entry["contact_email"] = "; ".join(dict.fromkeys(real_emails[:5]))

    return entry

# ---------------------------------------------------------------------------
# Step 4 — Download & parse PDF for richer fields
# ---------------------------------------------------------------------------

def enrich_from_pdf(entry: dict, pdf_dir: Path) -> dict:
    pdf_url = entry.get("pdf_url", "")
    if not pdf_url or not pdf_url.lower().endswith(".pdf"):
        return entry

    filename = Path(urlparse(pdf_url).path).name or "unknown.pdf"
    local_path = pdf_dir / filename

    if not local_path.exists():
        resp = _get(pdf_url, stream=True)
        if resp is None:
            entry["pdf_accessible"] = False
            return entry
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)
        time.sleep(REQUEST_DELAY)

    entry["pdf_accessible"] = True

    try:
        with pdfplumber.open(local_path) as pdf:
            pdf_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as exc:
        log.warning("Could not parse PDF %s: %s", filename, exc)
        return entry

    if not pdf_text.strip():
        return entry

    # Run structured field extractor on PDF text — often more complete than HTML
    pdf_entry = _parse_structured_fields(pdf_text, {
        k: entry[k] for k in entry  # copy current values as defaults
    })

    # Merge: prefer PDF values for fields that were empty from HTML
    for field in [
        "source_of_finance", "project_title", "eligibility",
        "contractor_class", "performance_period", "place_of_performance",
        "dossier_fee", "pre_bid_meeting", "contact_email",
    ]:
        if not entry.get(field) and pdf_entry.get(field):
            entry[field] = pdf_entry[field]

    # Org from PDF text (often in header)
    if not entry.get("org"):
        entry["org"] = extract_org(
            entry.get("title", ""), entry.get("description", ""), pdf_text
        )

    return entry

# ---------------------------------------------------------------------------
# Step 5 — Summary + Excel export
# ---------------------------------------------------------------------------

def print_summary(df: pd.DataFrame):
    print("\n" + "=" * 65)
    print("  NTB SEYCHELLES — ADVERTISED TENDERS SUMMARY")
    print("=" * 65)
    print(f"  Total tenders:                    {len(df):>8,}")
    print(f"  With submission deadline:         {(df['submission_deadline'] != '').sum():>8,}")
    print(f"  With attached file:               {(df['pdf_url'] != '').sum():>8,}")
    print(f"  PDFs accessible:                  {df['pdf_accessible'].sum():>8,}")
    print(f"  With pre-bid meeting:             {(df['pre_bid_meeting'] != '').sum():>8,}")
    print(f"  Unique procuring entities:        {df['org'].nunique():>8,}")

    print("\n  BY CATEGORY")
    print("  " + "-" * 55)
    for cat, cnt in df["category"].value_counts().items():
        bar = "█" * min(int(cnt / max(df["category"].value_counts()) * 30), 30)
        print(f"  {cat:<28} {bar:<32} {cnt:>4,}")

    print("\n  TOP 12 PROCURING ENTITIES")
    print("  " + "-" * 55)
    for org, cnt in df["org"].value_counts().head(12).items():
        label = (str(org)[:42] + "…") if len(str(org)) > 42 else str(org)
        print(f"  {label:<44}  {cnt:>4,}")

    print("\n  BY CONTRACTOR CLASS")
    print("  " + "-" * 55)
    for cls, cnt in df["contractor_class"].value_counts().head(10).items():
        if cls:
            print(f"  {str(cls):<40}  {cnt:>4,}")

    print("\n  DOSSIER FEES")
    print("  " + "-" * 55)
    for fee, cnt in df["dossier_fee"].value_counts().head(8).items():
        if fee:
            print(f"  {str(fee):<30}  {cnt:>4,}")

    print("\n  BY YEAR (created date)")
    print("  " + "-" * 55)
    df["year"] = df["created_date"].str.extract(r"(\d{4})")
    for year, cnt in df["year"].value_counts().sort_index().items():
        print(f"  {year}  {cnt:>5,}")

    print("=" * 65 + "\n")


def save_excel(df: pd.DataFrame, out_path: Path):
    xl_path = out_path.with_suffix(".xlsx")
    with pd.ExcelWriter(xl_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="All tenders", index=False)

        (df["org"].value_counts()
         .reset_index().rename(columns={"count": "tender_count"})
         .to_excel(writer, sheet_name="By org", index=False))

        (df["category"].value_counts()
         .reset_index().rename(columns={"count": "tender_count"})
         .to_excel(writer, sheet_name="By category", index=False))

        (df["contractor_class"].value_counts()
         .reset_index().rename(columns={"count": "tender_count"})
         .to_excel(writer, sheet_name="By contractor class", index=False))

        # Deadline timeline
        dl = df[df["submission_deadline"] != ""].copy()
        dl["deadline_year"] = dl["submission_deadline"].str[:4]
        (dl.groupby("deadline_year").size()
         .reset_index(name="count")
         .to_excel(writer, sheet_name="Deadline timeline", index=False))

        # Dossier fees
        (df[df["dossier_fee"] != ""]["dossier_fee"].value_counts()
         .reset_index().rename(columns={"count": "tender_count"})
         .to_excel(writer, sheet_name="Dossier fees", index=False))

    log.info("Excel workbook saved to %s", xl_path)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Scrape NTB Seychelles Advertised Tenders."
    )
    parser.add_argument("--out", default=DEFAULT_OUT,
                        help=f"Output CSV (default: {DEFAULT_OUT})")
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR,
                        help=f"Directory to cache detail page HTML (default: {DEFAULT_CACHE_DIR})")
    parser.add_argument("--pdf-dir", default=DEFAULT_PDF_DIR,
                        help=f"Directory to save PDFs (default: {DEFAULT_PDF_DIR})")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES,
                        help="Max listing pages to crawl (default: all ~153)")
    parser.add_argument("--skip-fetch", action="store_true",
                        help="Use only cached HTML — skip network calls for detail pages")
    parser.add_argument("--no-pdfs", action="store_true",
                        help="Skip PDF download and parsing entirely")
    parser.add_argument("--no-excel", action="store_true",
                        help="Skip Excel export")
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    pdf_dir = Path(args.pdf_dir)
    if not args.no_pdfs:
        pdf_dir.mkdir(parents=True, exist_ok=True)

    # ---- 1. Collect listing stubs ------------------------------------------
    entries = collect_listing_stubs(max_pages=args.max_pages)

    # ---- 2. Enrich from detail pages ---------------------------------------
    log.info("\nEnriching %d entries from detail pages…", len(entries))
    for i, entry in enumerate(entries, 1):
        if not args.skip_fetch:
            log.info("  [%d/%d] %s", i, len(entries),
                     entry["detail_url"].split("/")[-1][:60])
            entries[i - 1] = enrich_from_detail(entry, cache_dir)

    # ---- 3. Enrich from PDFs -----------------------------------------------
    if not args.no_pdfs:
        log.info("\nDownloading/parsing PDFs for %d entries…", len(entries))
        for i, entry in enumerate(entries, 1):
            if entry.get("pdf_url"):
                log.info("  [%d/%d] PDF: %s", i, len(entries),
                         Path(entry["pdf_url"]).name[:60])
                entries[i - 1] = enrich_from_pdf(entry, pdf_dir)

    # ---- 4. Build DataFrame ------------------------------------------------
    df = pd.DataFrame(entries)

    # Fill org from title+description where still empty
    mask = df["org"] == ""
    df.loc[mask, "org"] = df.loc[mask].apply(
        lambda r: extract_org(r["title"], r["description"]), axis=1
    )

    # Classify category
    df["category"] = (df["title"] + " " + df["description"]).apply(classify)

    # Clean text columns
    for col in ["title", "org", "description", "project_title", "eligibility"]:
        df[col] = df[col].str.strip()

    # Canonical column order
    col_order = [
        "title", "org", "category", "tags",
        "source_of_finance", "project_title", "eligibility", "contractor_class",
        "performance_period", "place_of_performance", "dossier_fee",
        "pre_bid_meeting", "contact_email",
        "created_date", "submission_deadline", "submission_time",
        "pdf_url", "pdf_accessible", "description", "detail_url",
    ]
    df = df[[c for c in col_order if c in df.columns]]

    # ---- 5. Save CSV -------------------------------------------------------
    out_path = Path(args.out)
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    log.info("Saved %d rows → %s", len(df), out_path)

    # ---- 6. Summary --------------------------------------------------------
    print_summary(df)

    # ---- 7. Excel ----------------------------------------------------------
    if not args.no_excel:
        try:
            save_excel(df, out_path)
        except ImportError:
            log.info("openpyxl not installed — skipping Excel (pip install openpyxl)")


if __name__ == "__main__":
    main()
