"""
build.py — NTB Seychelles Procurement Dashboard
================================================
Reads the four scraped CSVs from ./data/ and generates a fully
self-contained ./docs/index.html that runs on GitHub Pages with
zero server requirements.

All chart data is embedded as JSON inside the HTML. The page uses
Plotly.js (CDN) and plain JavaScript — no Python needed at runtime.

Usage
-----
    python build.py                        # reads ./data/, writes ./docs/index.html
    python build.py --data-dir ./my_data   # custom CSV directory
    python build.py --out ./docs           # custom output directory
    python build.py --pretty               # pretty-print embedded JSON (larger file)

GitHub Pages setup
------------------
1. Push this repo to GitHub.
2. Go to Settings → Pages → Source → Deploy from branch → main → /docs.
3. Run: python build.py && git add docs/index.html && git commit -m "Rebuild dashboard" && git push
4. Your dashboard is live at https://YOUR_USERNAME.github.io/REPO_NAME/
"""

import argparse
import json
import math
from pathlib import Path

import pandas as pd

try:
    from normalise_orgs import normalise_all
    NORMALISE_AVAILABLE = True
except ImportError:
    NORMALISE_AVAILABLE = False

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_DATA_DIR = "./data"
DEFAULT_OUT_DIR  = "./docs"

CATEGORY_COLORS = {
    "Housing":          "#006994",
    "Infrastructure":   "#00A896",
    "Utilities":        "#854F0B",
    "Roads & seawalls": "#534AB7",
    "Education":        "#E05A3A",
    "Transport":        "#2D7A3A",
    "Health":           "#993556",
    "Energy":           "#6B8FA3",
    "Fisheries":        "#3C3489",
    "Maritime":         "#0C447C",
    "Environment":      "#63380A",
    "ICT":              "#1D9E75",
    "Sports":           "#D85A30",
    "Security":         "#888780",
    "Consultancy":      "#7F77DD",
    "Goods & Services": "#D4537E",
    "Other":            "#B4B2A9",
}

# ---------------------------------------------------------------------------
# Data loading (mirrors ntb_dashboard.py loaders)
# ---------------------------------------------------------------------------

def load_awarded(path: Path) -> pd.DataFrame:
    if not path.exists():
        print(f"  WARNING: {path} not found")
        return pd.DataFrame()
    df = pd.read_csv(path)
    df["source"] = "Awarded"
    # Normalise column names
    if "description" in df.columns and "title" not in df.columns:
        df = df.rename(columns={"description": "title"})
    df["sr_value"] = pd.to_numeric(df.get("sr_value", pd.Series(dtype=float)), errors="coerce")
    return df


def load_minutes(path: Path) -> pd.DataFrame:
    if not path.exists():
        print(f"  WARNING: {path} not found")
        return pd.DataFrame()
    df = pd.read_csv(path)
    df["source"] = "Minutes"
    if "tender_description" in df.columns and "title" not in df.columns:
        df = df.rename(columns={"tender_description": "title"})
    if "bidder_name" in df.columns and "winner" not in df.columns:
        df = df.rename(columns={"bidder_name": "winner"})
    if "bid_amount" in df.columns and "sr_value" not in df.columns:
        df = df.rename(columns={"bid_amount": "sr_value"})
    df["sr_value"] = pd.to_numeric(df.get("sr_value", pd.Series(dtype=float)), errors="coerce")
    return df


def load_eoi(path: Path) -> pd.DataFrame:
    if not path.exists():
        print(f"  WARNING: {path} not found")
        return pd.DataFrame()
    df = pd.read_csv(path)
    df["source"] = "EOI"
    df["sr_value"] = float("nan")
    return df


def load_advertised(path: Path) -> pd.DataFrame:
    if not path.exists():
        print(f"  WARNING: {path} not found")
        return pd.DataFrame()
    df = pd.read_csv(path)
    df["source"] = "Advertised"
    df["sr_value"] = float("nan")
    return df


# ---------------------------------------------------------------------------
# Date range extraction
# ---------------------------------------------------------------------------

def extract_date_range(dfs: dict) -> tuple[str, str]:
    """Return (earliest_date_str, latest_date_str) across all datasets."""
    all_dates = []
    date_cols = ["created_date", "opening_date", "period", "submission_deadline"]

    for df in dfs.values():
        if df.empty:
            continue
        for col in date_cols:
            if col not in df.columns:
                continue
            parsed = pd.to_datetime(df[col], errors="coerce", dayfirst=True)
            valid = parsed.dropna()
            if not valid.empty:
                all_dates.extend(valid.tolist())

    if not all_dates:
        return "2020", "2025"

    earliest = min(all_dates)
    latest   = max(all_dates)
    fmt = "%B %Y"
    return earliest.strftime(fmt), latest.strftime(fmt)


# ---------------------------------------------------------------------------
# Chart data builders
# ---------------------------------------------------------------------------

def build_spend_by_cat(aw: pd.DataFrame) -> list:
    if aw.empty or "currency" not in aw.columns:
        return []
    sr = aw[aw["currency"] == "SR"].copy()
    if sr.empty or "category" not in sr.columns:
        return []
    grouped = (
        sr.groupby("category")["sr_value"]
        .sum()
        .sort_values(ascending=False)
        .reset_index()
    )
    return [
        {"category": row["category"],
         "value_m": round(row["sr_value"] / 1e6, 2),
         "color": CATEGORY_COLORS.get(row["category"], "#B4B2A9")}
        for _, row in grouped.iterrows()
    ]


def build_spend_by_org(aw: pd.DataFrame) -> list:
    if aw.empty or "currency" not in aw.columns:
        return []
    sr = aw[aw["currency"] == "SR"].copy()
    if sr.empty or "org" not in sr.columns:
        return []
    grouped = (
        sr.groupby("org")["sr_value"]
        .sum()
        .sort_values(ascending=False)
        .head(10)
        .reset_index()
    )
    return [
        {"org": row["org"], "value_m": round(row["sr_value"] / 1e6, 2)}
        for _, row in grouped.iterrows()
    ]


def build_top_winners(aw: pd.DataFrame) -> list:
    if aw.empty or "currency" not in aw.columns:
        return []
    sr = aw[aw["currency"] == "SR"].copy()
    if sr.empty or "winner" not in sr.columns:
        return []
    sr["sr_value"] = pd.to_numeric(sr["sr_value"], errors="coerce")
    grouped = (
        sr.groupby("winner")["sr_value"]
        .agg(total_sr="sum", n_contracts="count")
        .sort_values("total_sr", ascending=False)
        .head(10)
        .reset_index()
    )
    return [
        {"winner": row["winner"],
         "total_sr_m": round(row["total_sr"] / 1e6, 2),
         "n_contracts": int(row["n_contracts"])}
        for _, row in grouped.iterrows()
    ]


def build_bunching_scatter(aw: pd.DataFrame) -> list:
    if aw.empty or "currency" not in aw.columns:
        return []
    sr = aw[aw["currency"] == "SR"].copy()
    sr["sr_value"] = pd.to_numeric(sr["sr_value"], errors="coerce")
    sr = sr.dropna(subset=["sr_value"])
    if sr.empty:
        return []

    # Normalise description column
    if "title" in sr.columns:
        sr["_label"] = sr["title"]
    elif "description" in sr.columns:
        sr["_label"] = sr["description"]
    else:
        sr["_label"] = ""

    import random
    random.seed(42)

    points = []
    for _, row in sr.iterrows():
        points.append({
            "x":        round(row["sr_value"] / 1e6, 4),
            "y":        round(random.uniform(0.1, 0.9), 4),
            "label":    str(row.get("_label", ""))[:60],
            "org":      str(row.get("org", ""))[:50],
            "winner":   str(row.get("winner", ""))[:50],
            "category": str(row.get("category", "Other")),
            "size":     round(min(row["sr_value"] / 1e6 * 1.5 + 5, 22), 1),
        })
    return points


def build_bids_dist(mn: pd.DataFrame) -> list:
    if mn.empty or "bid_number" not in mn.columns:
        return []
    counts = (
        mn["bid_number"]
        .dropna()
        .astype(int)
        .value_counts()
        .sort_index()
        .reset_index()
    )
    counts.columns = ["n_bids", "count"]
    return [{"n": int(r["n_bids"]), "count": int(r["count"])}
            for _, r in counts.iterrows() if r["n_bids"] <= 15]


def build_top_bidders(mn: pd.DataFrame) -> list:
    if mn.empty or "winner" not in mn.columns or "bid_number" not in mn.columns:
        return []
    top = (
        mn[mn["bid_number"] == 1]["winner"]
        .value_counts()
        .head(10)
        .reset_index()
    )
    top.columns = ["bidder", "count"]
    return [{"bidder": r["bidder"], "count": int(r["count"])}
            for _, r in top.iterrows()]


def build_org_stats(dfs: dict) -> dict:
    """Per-org stats across all 4 datasets."""
    aw = dfs.get("awarded", pd.DataFrame())
    mn = dfs.get("minutes", pd.DataFrame())
    eo = dfs.get("eoi", pd.DataFrame())
    ad = dfs.get("advertised", pd.DataFrame())

    all_orgs = set()
    for df in dfs.values():
        if not df.empty and "org" in df.columns:
            all_orgs.update(df["org"].dropna().unique())

    org_stats = {}
    for org in sorted(all_orgs):
        entry = {"awarded": 0, "minutes": 0, "eoi": 0, "advertised": 0,
                 "total_sr_m": 0, "cats": {}}

        if not aw.empty and "org" in aw.columns:
            aw_sub = aw[aw["org"] == org]
            entry["awarded"] = len(aw_sub)
            if "currency" in aw_sub.columns and "sr_value" in aw_sub.columns:
                sr_sub = aw_sub[aw_sub["currency"] == "SR"]["sr_value"]
                sr_sub = pd.to_numeric(sr_sub, errors="coerce")
                entry["total_sr_m"] = round(sr_sub.sum() / 1e6, 2)
            if "category" in aw_sub.columns:
                cats = aw_sub["category"].value_counts().head(6).to_dict()
                entry["cats"] = {k: int(v) for k, v in cats.items()}

        if not mn.empty and "org" in mn.columns:
            entry["minutes"] = len(mn[mn["org"] == org])
        if not eo.empty and "org" in eo.columns:
            entry["eoi"] = len(eo[eo["org"] == org])
        if not ad.empty and "org" in ad.columns:
            entry["advertised"] = len(ad[ad["org"] == org])

        org_stats[org] = entry

    return org_stats


def build_kpis(dfs: dict) -> dict:
    aw = dfs.get("awarded", pd.DataFrame())
    mn = dfs.get("minutes", pd.DataFrame())
    eo = dfs.get("eoi", pd.DataFrame())
    ad = dfs.get("advertised", pd.DataFrame())

    total_sr = 0
    n_awarded = 0
    if not aw.empty and "currency" in aw.columns and "sr_value" in aw.columns:
        sr = aw[aw["currency"] == "SR"].copy()
        sr["sr_value"] = pd.to_numeric(sr["sr_value"], errors="coerce")
        total_sr = sr["sr_value"].sum()
        n_awarded = len(sr)

    n_tenders = 0
    avg_bids = 0
    if not mn.empty:
        if "detail_url" in mn.columns:
            n_tenders = mn["detail_url"].nunique()
        elif "bid_number" in mn.columns:
            n_tenders = int((mn["bid_number"] == 1).sum())
        if "bid_number" in mn.columns and n_tenders > 0:
            max_bids = mn.groupby(
                mn.get("detail_url", mn.index.astype(str)))["bid_number"].max()
            avg_bids = round(max_bids.mean(), 1)

    unique_orgs = set()
    for df in dfs.values():
        if not df.empty and "org" in df.columns:
            unique_orgs.update(df["org"].dropna().unique())

    return {
        "total_sr_m": round(total_sr / 1e6, 1),
        "n_awarded": n_awarded,
        "n_tenders": n_tenders,
        "avg_bids": avg_bids,
        "n_eoi": len(eo),
        "n_advertised": len(ad),
        "n_orgs": len(unique_orgs),
    }


def build_data_summary_html(dfs: dict) -> str:
    """
    Build an HTML snippet describing each dataset: row count, date range, notes.
    Injected into DATA_SUMMARY_PLACEHOLDER in the template.
    """
    DATASET_META = {
        "awarded": {
            "label":    "Awarded Tenders",
            "date_cols": ["period", "created_date"],
            "note":     "SR contract values, winning bidders, procuring entities.",
        },
        "minutes": {
            "label":    "Minutes of Tenders",
            "date_cols": ["opening_date", "created_date"],
            "note":     "All competing bids per tender opening — bidder names and prices.",
        },
        "eoi": {
            "label":    "Expressions of Interest",
            "date_cols": ["created_date", "submission_deadline"],
            "note":     "EOI notices, limited bidding invitations, prequalifications.",
        },
        "advertised": {
            "label":    "Advertised Tenders",
            "date_cols": ["created_date", "submission_deadline"],
            "note":     "Full tender notices with eligibility, deadlines, dossier fees.",
        },
    }

    cards = []
    for key, meta in DATASET_META.items():
        df = dfs.get(key, pd.DataFrame())
        if df.empty:
            n_rows = 0
            period_str = "No data"
        else:
            n_rows = len(df)
            # Try to find earliest / latest date
            dates = []
            for col in meta["date_cols"]:
                if col in df.columns:
                    parsed = pd.to_datetime(df[col], errors="coerce", dayfirst=True)
                    dates.extend(parsed.dropna().tolist())
            if dates:
                earliest = min(dates).strftime("%b %Y")
                latest   = max(dates).strftime("%b %Y")
                period_str = f"{earliest} – {latest}"
            else:
                period_str = "Period unknown"

        cards.append(f"""
      <div class="ds-card">
        <div class="ds-card-title">{meta['label']}</div>
        <div class="ds-card-rows">{n_rows:,} records retrieved</div>
        <div class="ds-card-period">📅 {period_str}</div>
        <div style="font-size:11px;color:var(--muted);margin-top:5px">{meta['note']}</div>
      </div>""")

    return "\n".join(cards)


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Seychelles — Procurement Dashboard</title>
<script src="https://cdn.plot.ly/plotly-2.32.0.min.js"></script>
<style>
  :root {
    --hero-bg:   #003B5C;
    --body-bg:   #001F33;
    --surface:   #002A45;
    --surface2:  #00344F;
    --teal:      #00A896;
    --teal-dim:  #5DCAA5;
    --teal-glow: rgba(0,168,150,0.2);
    --aqua:      #378ADD;
    --muted:     rgba(232,244,250,0.45);
    --ink:       #E8F4FA;
    --ink-dim:   rgba(232,244,250,0.65);
    --border:    rgba(0,168,150,0.18);
    --border2:   rgba(0,168,150,0.1);
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    background: var(--body-bg); color: var(--ink); font-size: 14px;
  }
  .wrap { max-width: 1120px; margin: 0 auto; padding: 0 1.2rem 3rem; }

  /* Hero */
  .hero {
    background: var(--hero-bg);
    border-bottom: 2px solid var(--teal);
    padding: 1.4rem 1.8rem;
    margin: 1rem 0 1.2rem;
    border-radius: 12px;
    display: flex; justify-content: space-between; align-items: center; gap: 12px;
  }
  .hero h1 { color: var(--ink); font-size: 21px; font-weight: 500; margin-bottom: 3px; }
  .hero p  { color: var(--ink-dim); font-size: 12px; }
  .hero-right { display: flex; flex-direction: column; align-items: flex-end; gap: 5px; }
  .hero-badge {
    font-size: 11px; color: var(--teal-dim);
    background: var(--teal-glow);
    border: 0.5px solid rgba(0,168,150,0.3);
    padding: 3px 10px; border-radius: 20px; white-space: nowrap;
  }

  /* KPIs */
  .kpi-grid { display: grid; grid-template-columns: repeat(4,1fr); gap: 10px; margin-bottom: 1.2rem; }
  .kpi {
    background: var(--surface); border-radius: 10px;
    border: 0.5px solid var(--border);
    padding: 1rem 1.1rem; display: flex; align-items: center; gap: 12px;
  }
  .kpi-bar { width: 3px; border-radius: 0; height: 36px; flex-shrink: 0; }
  .kpi-label { font-size: 10px; color: var(--teal-dim); font-weight: 500;
                text-transform: uppercase; letter-spacing: .05em; margin-bottom: 3px; }
  .kpi-val   { font-size: 20px; font-weight: 500; color: var(--ink); line-height: 1.1; }
  .kpi-sub   { font-size: 10px; color: var(--muted); margin-top: 1px; }

  /* Sections */
  .sec {
    border-left: 3px solid var(--teal); padding-left: 10px;
    margin: 2rem 0 .8rem; display: flex; align-items: center; gap: 7px;
  }
  .sec span { font-size: 11px; font-weight: 500; color: var(--teal-dim);
               text-transform: uppercase; letter-spacing: .06em; }
  .divider { height: 1px; background: var(--border2); margin: 1.4rem 0; }

  /* Chart cards */
  .card {
    background: var(--surface); border-radius: 10px;
    border: 0.5px solid var(--border); padding: 1rem;
  }
  .row2  { display: grid; grid-template-columns: 3fr 2fr; gap: 12px; }
  .row2b { display: grid; grid-template-columns: 2fr 1fr; gap: 12px; }

  /* Tables */
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th {
    padding: 7px 10px; text-align: left; font-size: 10px; font-weight: 600;
    color: var(--teal-dim); border-bottom: 1px solid var(--teal);
    background: var(--surface2); text-transform: uppercase; letter-spacing: .05em;
  }
  td {
    padding: 6px 10px; border-bottom: 1px solid var(--border2);
    color: var(--ink); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    max-width: 280px;
  }
  tr:nth-child(even) td { background: rgba(0,168,150,0.04); }

  /* Download panel */
  .dl-panel {
    background: var(--surface); border: 0.5px solid var(--border);
    border-radius: 10px; padding: 1.2rem 1.4rem;
  }
  .dl-grid { display: grid; grid-template-columns: 2fr 2fr 1fr; gap: 16px; align-items: end; }
  .dl-label { font-size: 10px; color: var(--teal-dim); font-weight: 600;
               text-transform: uppercase; letter-spacing: .05em; margin-bottom: 5px; }
  .dl-select {
    font-size: 13px; padding: 7px 10px;
    border: 0.5px solid rgba(0,168,150,0.3); border-radius: 8px;
    background: var(--surface2); color: var(--ink); width: 100%;
  }
  .fmt-row { display: flex; gap: 12px; margin-top: 4px; }
  .fmt-row label { font-size: 13px; cursor: pointer; display: flex; align-items: center; gap: 4px; color: var(--ink-dim); }
  .dl-btn {
    width: 100%; padding: 9px;
    background: var(--teal);
    color: #001F33; border: none; border-radius: 8px;
    font-size: 13px; font-weight: 600; cursor: pointer; letter-spacing: .02em;
  }
  .dl-btn:hover { opacity: .88; }
  .dl-status { font-size: 12px; color: var(--muted); margin-top: 8px; min-height: 16px; }

  /* Footer */
  .footer {
    text-align: center; font-size: 11px; color: var(--muted);
    padding: 1rem 0 .5rem;
    border-top: 1px solid var(--border2); margin-top: 1rem;
  }
  .footer a { color: var(--teal-dim); text-decoration: none; }

  /* Data summary */
  .data-summary {
    background: var(--surface); border-radius: 10px;
    border: 0.5px solid var(--border);
    padding: 1.2rem 1.4rem; margin-top: 1.5rem;
  }
  .ds-title {
    font-size: 12px; font-weight: 500; color: var(--teal-dim);
    text-transform: uppercase; letter-spacing: .06em;
    margin-bottom: 1rem;
  }
  .ds-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 12px; margin-bottom: 1rem;
  }
  .ds-card {
    background: var(--surface2); border-radius: 8px;
    border: 0.5px solid var(--border2); padding: .8rem 1rem;
  }
  .ds-card-title {
    font-size: 11px; font-weight: 600; color: var(--teal-dim);
    text-transform: uppercase; letter-spacing: .04em; margin-bottom: 6px;
  }
  .ds-card-rows { font-size: 12px; color: var(--ink); margin-bottom: 2px; }
  .ds-card-period { font-size: 11px; color: var(--muted); }
  .ds-note {
    font-size: 11px; color: var(--muted); line-height: 1.6;
    border-top: 1px solid var(--border2); padding-top: .7rem;
  }
  .ds-note a { color: var(--teal-dim); text-decoration: none; }

  @media (max-width: 700px) {
    .kpi-grid { grid-template-columns: repeat(2,1fr); }
    .row2, .row2b { grid-template-columns: 1fr; }
    .dl-grid { grid-template-columns: 1fr; }
    .hero { flex-direction: column; align-items: flex-start; }
  }
</style>
</head>
<body>
<div class="wrap">

  <!-- Hero -->
  <div class="hero">
    <div>
      <h1>🏝 Seychelles — Procurement Dashboard</h1>
      <p>National Tender Board · Awarded · Minutes · EOI · Advertised</p>
    </div>
    <div class="hero-right">
      <span class="hero-badge">ntb.sc · open data</span>
    </div>
  </div>

  <!-- KPIs -->
  <div class="kpi-grid">
    <div class="kpi">
      <div class="kpi-bar" style="background:#00A896"></div>
      <div>
        <div class="kpi-label">Total SR awarded</div>
        <div class="kpi-val" id="kpi-sr"></div>
        <div class="kpi-sub" id="kpi-contracts"></div>
      </div>
    </div>
    <div class="kpi">
      <div class="kpi-bar" style="background:#5DCAA5"></div>
      <div>
        <div class="kpi-label">Tender openings</div>
        <div class="kpi-val" id="kpi-tenders"></div>
        <div class="kpi-sub" id="kpi-avgbids"></div>
      </div>
    </div>
    <div class="kpi">
      <div class="kpi-bar" style="background:#378ADD"></div>
      <div>
        <div class="kpi-label">Expressions of interest</div>
        <div class="kpi-val" id="kpi-eoi"></div>
        <div class="kpi-sub">market sounding notices</div>
      </div>
    </div>
    <div class="kpi">
      <div class="kpi-bar" style="background:#006994"></div>
      <div>
        <div class="kpi-label">Advertised tenders</div>
        <div class="kpi-val" id="kpi-adv"></div>
        <div class="kpi-sub" id="kpi-orgs"></div>
      </div>
    </div>
  </div>

  <!-- Awarded spend -->
  <div class="divider"></div>
  <div class="sec"><span>💰</span><span>Awarded spend</span></div>
  <div class="row2" style="margin-bottom:12px">
    <div class="card"><div id="chart-spend-cat" style="height:260px"></div></div>
    <div class="card"><div id="chart-spend-org" style="height:260px"></div></div>
  </div>

  <!-- Top winners -->
  <div class="divider"></div>
  <div class="sec"><span>🏆</span><span>Top 10 winning bidders by SR value</span></div>
  <div class="card"><div id="chart-winners" style="height:380px"></div></div>

  <!-- Bunching scatter -->
  <div class="divider"></div>
  <div class="sec"><span>📊</span><span>Contract value distribution — bunching analysis</span></div>
  <p style="font-size:12px;color:var(--muted);margin-bottom:10px">
    Each dot = one contract. Size = contract value.
    Vertical lines mark round SR million thresholds — clusters near these suggest possible bunching.
    Hover for details.
  </p>
  <div class="card"><div id="chart-scatter" style="height:360px"></div></div>

  <!-- Bidding market -->
  <div class="divider"></div>
  <div class="sec"><span>⚖️</span><span>Bidding market</span></div>
  <div class="row2b">
    <div class="card">
      <p style="font-size:12px;color:var(--muted);margin-bottom:8px">Bid count per tender</p>
      <div id="chart-bids" style="height:220px"></div>
    </div>
    <div class="card">
      <p style="font-size:12px;color:var(--muted);margin-bottom:8px">Top first-position bidders</p>
      <table id="tbl-bidders">
        <thead><tr><th>Bidder</th><th style="text-align:right;width:70px">Times T1</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <!-- Download -->
  <div class="divider"></div>
  <div class="sec"><span>⬇️</span><span>Download raw data</span></div>
  <div class="dl-panel">
    <div class="dl-grid">
      <div>
        <div class="dl-label">Dataset</div>
        <select class="dl-select" id="dl-dataset">
          <option value="master">All datasets combined (master)</option>
          <option value="awarded">Awarded tenders</option>
          <option value="minutes">Minutes of tenders</option>
          <option value="eoi">Expressions of interest</option>
          <option value="advertised">Advertised tenders</option>
        </select>
      </div>
      <div>
        <div class="dl-label">Format</div>
        <div class="fmt-row">
          <label><input type="radio" name="fmt" value="csv" checked> CSV</label>
          <label><input type="radio" name="fmt" value="json"> JSON</label>
        </div>
      </div>
      <div>
        <button class="dl-btn" onclick="doDownload()">⬇ Download</button>
      </div>
    </div>
    <div class="dl-status" id="dl-status"></div>
  </div>

  <div class="divider"></div>

  <!-- Data summary -->
  <div class="data-summary">
    <h3 class="ds-title">📋 About this data</h3>
    <div class="ds-grid">DATA_SUMMARY_PLACEHOLDER</div>
    <p class="ds-note">
      Data scraped from public records on <a href="https://ntb.sc" target="_blank">ntb.sc</a>.
      Some historical PDF files are no longer accessible on the NTB server and are excluded.
      All values are in Seychellois Rupees (SR) unless otherwise stated.
    </p>
  </div>

  <div class="footer">
    Data sourced from <a href="https://ntb.sc" target="_blank">ntb.sc</a> ·
    Independent open-source tool · Not affiliated with the NTB ·
    <a href="https://github.com/YOUR_USERNAME/ntb-seychelles" target="_blank">GitHub</a>
  </div>

</div><!-- /wrap -->

<script>
// ── Embedded data (injected by build.py) ────────────────────────────────────
const DATA = __DATA_JSON__;

// ── Plotly config ────────────────────────────────────────────────────────────
const PC = {
  displayModeBar: false,
  responsive: true,
};
const LAY_BASE = {
  paper_bgcolor: 'rgba(0,0,0,0)',
  plot_bgcolor:  'rgba(0,0,0,0)',
  font: { family: 'Inter, sans-serif', size: 11, color: 'rgba(232,244,250,0.55)' },
  margin: { t: 20, b: 50, l: 50, r: 20 },
};
const GRID_COLOR = 'rgba(0,168,150,0.12)';
const TICK_COLOR = 'rgba(232,244,250,0.55)';
const LABEL_COLOR = 'rgba(232,244,250,0.85)';

// ── KPIs ─────────────────────────────────────────────────────────────────────
const K = DATA.kpis;
document.getElementById('kpi-sr').textContent        = 'SR ' + K.total_sr_m + 'M';
document.getElementById('kpi-contracts').textContent = K.n_awarded.toLocaleString() + ' contracts';
document.getElementById('kpi-tenders').textContent   = K.n_tenders.toLocaleString();
document.getElementById('kpi-avgbids').textContent   = 'avg ' + K.avg_bids + ' bids each';
document.getElementById('kpi-eoi').textContent       = K.n_eoi.toLocaleString();
document.getElementById('kpi-adv').textContent       = K.n_advertised.toLocaleString();
document.getElementById('kpi-orgs').textContent      = K.n_orgs + ' procuring entities';

// ── Spend by category ────────────────────────────────────────────────────────
(function() {
  const d = DATA.spend_by_cat;
  Plotly.newPlot('chart-spend-cat', [{
    type: 'bar', x: d.map(r => r.category), y: d.map(r => r.value_m),
    marker: { color: d.map(r => r.color), cornerradius: 4 },
    hovertemplate: '<b>%{x}</b><br>SR %{y}M<extra></extra>',
  }], {
    ...LAY_BASE,
    xaxis: { gridcolor: 'rgba(0,0,0,0)', tickangle: -30,
             tickfont: { size: 10 }, fixedrange: true },
    yaxis: { gridcolor: GRID_COLOR, tickprefix: 'SR ', ticksuffix: 'M',
             tickfont: { size: 10 }, fixedrange: true },
  }, PC);
})();

// ── Spend by org ─────────────────────────────────────────────────────────────
(function() {
  const d = DATA.spend_by_org;
  const labels = d.map(r => r.org.replace('Seychelles ', 'Sey. ').substring(0, 28));
  Plotly.newPlot('chart-spend-org', [{
    type: 'bar', orientation: 'h',
    y: labels, x: d.map(r => r.value_m),
    marker: { color: 'rgba(0,168,150,0.75)', line: { color: '#00A896', width: 0.5 } },
    hovertemplate: '<b>%{y}</b><br>SR %{x}M<extra></extra>',
  }], {
    ...LAY_BASE,
    margin: { t: 10, b: 40, l: 150, r: 60 },
    xaxis: { gridcolor: GRID_COLOR, tickprefix: 'SR ', ticksuffix: 'M',
             tickfont: { size: 10, color: TICK_COLOR }, fixedrange: true },
    yaxis: { autorange: 'reversed', gridcolor: 'rgba(0,0,0,0)',
             tickfont: { size: 10, color: LABEL_COLOR }, fixedrange: true },
  }, PC);
})();

// ── Top winners ──────────────────────────────────────────────────────────────
(function() {
  const d = DATA.top_winners;
  const n = d.length;
  const colors = d.map((_, i) =>
    `rgba(0, ${130 + Math.round(100*(i/Math.max(n-1,1)))}, ${168 - Math.round(40*(i/Math.max(n-1,1)))}, 0.88)`
  );
  Plotly.newPlot('chart-winners', [{
    type: 'bar', orientation: 'h',
    y: d.map(r => r.winner.length > 32 ? r.winner.substring(0,30)+'…' : r.winner),
    x: d.map(r => r.total_sr_m),
    marker: { color: colors },
    text: d.map(r => `SR ${r.total_sr_m}M  (${r.n_contracts} contracts)`),
    textposition: 'outside',
    textfont: { color: TICK_COLOR, size: 10 },
    hovertemplate: '<b>%{y}</b><br>SR %{x}M total<extra></extra>',
  }], {
    ...LAY_BASE,
    margin: { t: 10, b: 40, l: 220, r: 110 },
    xaxis: { gridcolor: GRID_COLOR, tickprefix: 'SR ', ticksuffix: 'M',
             tickfont: { size: 10, color: TICK_COLOR }, fixedrange: true },
    yaxis: { autorange: 'reversed', gridcolor: 'rgba(0,0,0,0)',
             tickfont: { size: 11, color: LABEL_COLOR }, fixedrange: true },
  }, PC);
})();

// ── Bunching scatter ─────────────────────────────────────────────────────────
(function() {
  const pts = DATA.bunching_scatter;
  const CAT_COLOR = __CAT_COLORS_JSON__;

  // Group by category for legend
  const cats = [...new Set(pts.map(p => p.category))];
  const traces = cats.map(cat => {
    const grp = pts.filter(p => p.category === cat);
    return {
      type: 'scatter', mode: 'markers', name: cat,
      x: grp.map(p => p.x), y: grp.map(p => p.y),
      marker: {
        color: CAT_COLOR[cat] || '#B4B2A9',
        size: grp.map(p => p.size),
        opacity: 0.72,
        line: { color: 'rgba(255,255,255,0.5)', width: 0.5 },
      },
      text: grp.map(p =>
        `<b>${p.label}</b><br>SR ${p.x.toFixed(2)}M<br>${p.org}<br>${p.winner}`
      ),
      hovertemplate: '%{text}<extra></extra>',
    };
  });

  // Vertical reference lines via shapes
  const shapes = [1,2,3,5,10,20].map(m => ({
    type: 'line', x0: m, x1: m, y0: 0, y1: 1,
    yref: 'paper',
    line: { color: 'rgba(0,168,150,0.3)', width: 1, dash: 'dot' },
  }));
  const annotations = [1,2,3,5,10,20].map(m => ({
    x: m, y: 1.02, yref: 'paper', xref: 'x',
    text: `SR ${m}M`, showarrow: false,
    font: { size: 9, color: 'rgba(0,168,150,0.65)' },
  }));

  Plotly.newPlot('chart-scatter', traces, {
    ...LAY_BASE,
    margin: { t: 30, b: 50, l: 20, r: 20 },
    shapes, annotations,
    xaxis: {
      title: { text: 'Contract value (SR millions)', font: { size: 11 } },
      gridcolor: GRID_COLOR, tickprefix: 'SR ', ticksuffix: 'M',
      tickfont: { size: 10 }, fixedrange: true,
    },
    yaxis: { visible: false, range: [0, 1], fixedrange: true },
    legend: {
      orientation: 'h', y: 1.08, x: 0,
      font: { size: 10, color: TICK_COLOR }, itemsizing: 'constant',
    },
    hovermode: 'closest',
  }, PC);
})();

// ── Bids distribution ────────────────────────────────────────────────────────
(function() {
  const d = DATA.bids_dist;
  Plotly.newPlot('chart-bids', [{
    type: 'bar',
    x: d.map(r => r.n.toString()), y: d.map(r => r.count),
    marker: { color: 'rgba(0,168,150,0.65)', line: { color: '#5DCAA5', width: 0.5 } },
    hovertemplate: '%{y} tenders received %{x} bids<extra></extra>',
  }], {
    ...LAY_BASE,
    margin: { t: 10, b: 40, l: 40, r: 10 },
    xaxis: { title: { text: 'Number of bids', font: { size: 11, color: TICK_COLOR } },
             gridcolor: 'rgba(0,0,0,0)', tickfont: { size: 10, color: TICK_COLOR }, fixedrange: true },
    yaxis: { gridcolor: GRID_COLOR, tickfont: { size: 10, color: TICK_COLOR }, fixedrange: true },
  }, PC);
})();

// ── Bidder table ──────────────────────────────────────────────────────────────
(function() {
  const tbody = document.querySelector('#tbl-bidders tbody');
  (DATA.top_bidders || []).forEach(r => {
    const tr = document.createElement('tr');
    const name = r.bidder.length > 28 ? r.bidder.substring(0,26)+'…' : r.bidder;
    tr.innerHTML = `<td title="${r.bidder}">${name}</td>
                    <td style="text-align:right;font-weight:500;color:#5DCAA5">${r.count}</td>`;
    tbody.appendChild(tr);
  });
})();

// ── Download ──────────────────────────────────────────────────────────────────
function doDownload() {
  const dataset = document.getElementById('dl-dataset').value;
  const fmt = document.querySelector('input[name="fmt"]:checked').value;
  const status = document.getElementById('dl-status');

  const raw = DATA.raw[dataset];
  if (!raw || !raw.length) {
    status.textContent = 'No data available for this dataset.';
    return;
  }

  let content, mime, ext;
  if (fmt === 'csv') {
    const keys = Object.keys(raw[0]);
    const rows = [keys.join(','),
      ...raw.map(r => keys.map(k => {
        const v = r[k] == null ? '' : String(r[k]);
        return v.includes(',') || v.includes('"') || v.includes('\n')
          ? '"' + v.replace(/"/g, '""') + '"' : v;
      }).join(','))
    ];
    content = rows.join('\n');
    mime = 'text/csv'; ext = 'csv';
  } else {
    content = JSON.stringify(raw, null, 2);
    mime = 'application/json'; ext = 'json';
  }

  const blob = new Blob([content], { type: mime });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href = url; a.download = `ntb_${dataset}.${ext}`;
  a.click(); URL.revokeObjectURL(url);

  status.textContent =
    `✓ Downloaded ntb_${dataset}.${ext} (${raw.length.toLocaleString()} rows)`;
}
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Build function
# ---------------------------------------------------------------------------

def build(data_dir: Path, out_dir: Path, pretty: bool = False):
    print("\nNTB Seychelles — Building static dashboard")
    print("=" * 50)

    # Load datasets
    print("\nLoading CSVs…")
    dfs = {
        "awarded":    load_awarded(data_dir / "ntb_tenders.csv"),
        "minutes":    load_minutes(data_dir / "ntb_minutes.csv"),
        "eoi":        load_eoi(data_dir / "ntb_eoi.csv"),
        "advertised": load_advertised(data_dir / "ntb_advertised.csv"),
    }
    for key, df in dfs.items():
        print(f"  {key:<12}: {len(df):,} rows")

    # Normalise organisation names (fuzzy deduplication)
    print("\nNormalising organisation names…")
    if NORMALISE_AVAILABLE:
        dfs = normalise_all(
            dfs,
            org_log=str(data_dir / "org_normalisation_log.csv"),
            bidder_log=str(data_dir / "bidder_normalisation_log.csv"),
        )
    else:
        print("  (skipped — normalise_orgs.py not found or thefuzz not installed)")

    # Build chart data
    print("\nBuilding chart data…")
    indent = 2 if pretty else None

    # Raw data for download (truncate huge columns to keep HTML size sane)
    def safe_records(df: pd.DataFrame, max_cols=20) -> list:
        if df.empty:
            return []
        cols = list(df.columns)[:max_cols]
        return df[cols].fillna("").astype(str).to_dict(orient="records")

    # Master = all four combined
    master_frames = []
    for key, df in dfs.items():
        if not df.empty:
            sub = df.copy()
            sub["source"] = key
            master_frames.append(sub)
    master_df = pd.concat(master_frames, ignore_index=True, sort=False) if master_frames else pd.DataFrame()

    data = {
        "kpis":            build_kpis(dfs),
        "spend_by_cat":    build_spend_by_cat(dfs["awarded"]),
        "spend_by_org":    build_spend_by_org(dfs["awarded"]),
        "top_winners":     build_top_winners(dfs["awarded"]),
        "bunching_scatter":build_bunching_scatter(dfs["awarded"]),
        "bids_dist":       build_bids_dist(dfs["minutes"]),
        "top_bidders":     build_top_bidders(dfs["minutes"]),
        "raw": {
            "master":     safe_records(master_df),
            "awarded":    safe_records(dfs["awarded"]),
            "minutes":    safe_records(dfs["minutes"]),
            "eoi":        safe_records(dfs["eoi"]),
            "advertised": safe_records(dfs["advertised"]),
        },
    }

    data_json      = json.dumps(data, indent=indent, ensure_ascii=False)
    cat_color_json = json.dumps(CATEGORY_COLORS, indent=indent)
    summary_html   = build_data_summary_html(dfs)

    # Inject into template
    html = HTML_TEMPLATE
    html = html.replace("DATA_SUMMARY_PLACEHOLDER", summary_html)
    html = html.replace("__DATA_JSON__", data_json)
    html = html.replace("__CAT_COLORS_JSON__", cat_color_json)

    # Write output
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "index.html"
    out_file.write_text(html, encoding="utf-8")

    size_kb = out_file.stat().st_size / 1024
    print(f"\n  ✓ Written → {out_file}  ({size_kb:.0f} KB)")
    print(f"\n  Open locally:  open {out_file}")
    print(f"  GitHub Pages:  push and enable Pages → /docs")
    print("=" * 50 + "\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Build NTB Seychelles static dashboard")
    p.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                   help=f"Directory containing the four CSV files (default: {DEFAULT_DATA_DIR})")
    p.add_argument("--out", default=DEFAULT_OUT_DIR,
                   help=f"Output directory for index.html (default: {DEFAULT_OUT_DIR})")
    p.add_argument("--pretty", action="store_true",
                   help="Pretty-print embedded JSON (larger file, easier to debug)")
    args = p.parse_args()
    build(Path(args.data_dir), Path(args.out), args.pretty)
