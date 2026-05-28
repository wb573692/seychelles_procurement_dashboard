"""
normalise_orgs.py — Organisation Name Normalisation
=====================================================
Cleans and deduplicates procuring entity names across all four NTB
datasets using a two-pass approach:

Pass 1 — Rule-based canonicalisation
    Apply a lookup table of known abbreviations, typos, and variants
    to map them to a single canonical form.  Fast and deterministic.

Pass 2 — Fuzzy clustering
    For names that survive Pass 1 without a match, use token-sort
    ratio (handles word-order differences like "Authority Port Sey."
    vs "Sey. Port Authority") to cluster near-duplicates.
    The most frequent name in each cluster becomes the canonical.

The result is a mapping dict  {raw_name → canonical_name}  that is
applied to every DataFrame before charts are built.

A full audit log (raw → canonical, match method, score) is written
to  data/org_normalisation_log.csv  so you can review and override.

Usage
-----
    from normalise_orgs import build_org_mapping, apply_org_mapping

    # Build once from all four DataFrames
    mapping = build_org_mapping([df_awarded, df_minutes, df_eoi, df_adv])

    # Apply to each DataFrame in place
    for df in [df_awarded, df_minutes, df_eoi, df_adv]:
        apply_org_mapping(df, mapping)

Tuning
------
    FUZZY_THRESHOLD (default 88) — lower = more aggressive merging.
    Override mappings live in MANUAL_OVERRIDES at the top of this file.
"""

import re
import unicodedata
from collections import Counter

import pandas as pd

try:
    from thefuzz import fuzz, process as fuzz_process
    FUZZY_AVAILABLE = True
except ImportError:
    FUZZY_AVAILABLE = False
    print("  WARNING: thefuzz not installed — fuzzy matching disabled. "
          "Run: pip install thefuzz python-Levenshtein")

# ---------------------------------------------------------------------------
# Tuning
# ---------------------------------------------------------------------------

FUZZY_THRESHOLD = 88   # 0–100; raise to merge less, lower to merge more
MIN_NAME_LENGTH = 4    # ignore very short strings

# ---------------------------------------------------------------------------
# Manual overrides — highest priority, applied before fuzzy matching.
# Add entries here whenever the automated clustering gets something wrong.
# Keys are lowercase-stripped versions of the raw name.
# ---------------------------------------------------------------------------

MANUAL_OVERRIDES: dict[str, str] = {
    # Ports / maritime
    "seychelles ports authority":            "Seychelles Ports Authority",
    "seychelles port authority":             "Seychelles Ports Authority",
    "sey. ports authority":                  "Seychelles Ports Authority",
    "sey. port authority":                   "Seychelles Ports Authority",
    "spa":                                   "Seychelles Ports Authority",

    # Infrastructure
    "seychelles infrastructure agency":      "Seychelles Infrastructure Agency",
    "sia":                                   "Seychelles Infrastructure Agency",

    # Utilities
    "public utilities corporation":          "Public Utilities Corporation",
    "puc":                                   "Public Utilities Corporation",
    "public utility corporation":            "Public Utilities Corporation",

    # Land transport
    "seychelles land transport agency":      "Seychelles Land Transport Agency",
    "slta":                                  "Seychelles Land Transport Agency",
    "land transport agency":                 "Seychelles Land Transport Agency",

    # Airports
    "seychelles airports authority":         "Seychelles Airports Authority",
    "seychelles airport authority":          "Seychelles Airports Authority",
    "saa":                                   "Seychelles Airports Authority",

    # Civil aviation
    "seychelles civil aviation authority":   "Seychelles Civil Aviation Authority",
    "scaa":                                  "Seychelles Civil Aviation Authority",

    # Fisheries
    "seychelles fisheries authority":        "Seychelles Fisheries Authority",
    "seychelles fishing authority":          "Seychelles Fisheries Authority",
    "sfa":                                   "Seychelles Fisheries Authority",

    # Health
    "health care agency":                    "Health Care Agency",
    "healthcare agency":                     "Health Care Agency",
    "hca":                                   "Health Care Agency",

    # Education
    "ministry of education":                 "Ministry of Education",
    "moe":                                   "Ministry of Education",
    "ministry of education and human resource development": "Ministry of Education",

    # Property
    "property management corporation":       "Property Management Corporation",
    "pmc":                                   "Property Management Corporation",

    # Landscape & Waste
    "landscape & waste management agency":   "Landscape & Waste Management Agency",
    "landscape and waste management agency": "Landscape & Waste Management Agency",
    "lwma":                                  "Landscape & Waste Management Agency",

    # Broadcasting
    "seychelles broadcasting corporation":   "Seychelles Broadcasting Corporation",
    "sbc":                                   "Seychelles Broadcasting Corporation",

    # Local government
    "ministry of local government":          "Ministry of Local Government",
    "ministry of local government & community affairs": "Ministry of Local Government",
    "ministry of local government and community affairs": "Ministry of Local Government",

    # Finance
    "ministry of finance":                   "Ministry of Finance",
    "ministry of finance, national planning and trade": "Ministry of Finance",
    "ministry of finance and trade":         "Ministry of Finance",

    # Lands
    "ministry of lands and housing":         "Ministry of Lands & Housing",
    "ministry of lands & housing":           "Ministry of Lands & Housing",

    # Sports
    "national sports council":               "National Sports Council",
    "nsc":                                   "National Sports Council",

    # Revenue
    "seychelles revenue commission":         "Seychelles Revenue Commission",
    "src":                                   "Seychelles Revenue Commission",

    # FSA
    "financial services authority":          "Financial Services Authority",
    "fsa":                                   "Financial Services Authority",

    # Public sector
    "public sector bureau":                  "Public Sector Bureau",
    "psb":                                   "Public Sector Bureau",

    # Police
    "police department":                     "Seychelles Police Force",
    "seychelles police force":               "Seychelles Police Force",
    "seychelles police":                     "Seychelles Police Force",

    # Transport corporation
    "seychelles public transport corporation": "Public Transport Corporation",
    "public transport corporation":            "Public Transport Corporation",
    "sptc":                                    "Public Transport Corporation",

    # Tourism
    "tourism department":                    "Tourism Department",
    "department of tourism":                 "Tourism Department",

    # Fire
    "seychelles fire & rescue services agency": "Seychelles Fire & Rescue Services Agency",
    "seychelles fire and rescue services agency": "Seychelles Fire & Rescue Services Agency",
    "sfrsa":                                 "Seychelles Fire & Rescue Services Agency",

    # Heritage
    "seychelles heritage foundation":        "Seychelles Heritage Foundation",
    "shf":                                   "Seychelles Heritage Foundation",

    # ICT
    "department of information communication technology": "Department of ICT",
    "dept. of information communication technology":      "Department of ICT",
    "dict":                                  "Department of ICT",

    # Anti-corruption
    "anti-corruption commission":            "Anti-Corruption Commission",
    "anti corruption commission":            "Anti-Corruption Commission",
    "acc":                                   "Anti-Corruption Commission",

    # Judiciary
    "the judiciary":                         "The Judiciary",
    "judiciary":                             "The Judiciary",

    # Communications
    "seychelles communications regulatory authority": "Seychelles Communications Regulatory Authority",
    "scra":                                  "Seychelles Communications Regulatory Authority",

    # Agriculture
    "seychelles agricultural agency":        "Seychelles Agricultural Agency",
    "department of agriculture":             "Seychelles Agricultural Agency",
    "agriculture department":                "Seychelles Agricultural Agency",

    # FIU
    "financial intelligence unit":           "Financial Intelligence Unit",
    "fiu":                                   "Financial Intelligence Unit",

    # Technical section
    "technical section services":            "Technical Section Services",
    "technical section":                     "Technical Section Services",
    "tss":                                   "Technical Section Services",

    # Known typos seen in NTB data
    "seychelles land transport agnecy":      "Seychelles Land Transport Agency",
    "seychelles land transport agengy":      "Seychelles Land Transport Agency",
    "seychelles revenue commision":          "Seychelles Revenue Commission",
    "seychelles revenue comission":          "Seychelles Revenue Commission",
    "seychelles infrastucture agency":       "Seychelles Infrastructure Agency",
    "seychelles infrastructure agnecy":      "Seychelles Infrastructure Agency",
    "public utilities corporaton":           "Public Utilities Corporation",
    "public utilities corportation":         "Public Utilities Corporation",
    "seychelles fisheries authoirty":        "Seychelles Fisheries Authority",
    "health care agnecy":                    "Health Care Agency",
    "national sports coucil":               "National Sports Council",
    "seychelles civil aviation authoirty":   "Seychelles Civil Aviation Authority",
    "seychelles airports authoirty":         "Seychelles Airports Authority",
}

# ---------------------------------------------------------------------------
# Step 1 — Text pre-processing
# ---------------------------------------------------------------------------

def _normalise_key(name: str) -> str:
    """Lowercase, strip punctuation noise, collapse whitespace."""
    if not isinstance(name, str):
        return ""
    # Unicode normalise
    name = unicodedata.normalize("NFKC", name)
    # Lower
    name = name.lower().strip()
    # Remove leading/trailing punctuation noise
    name = name.strip(" -–—*•·")
    # Collapse multiple spaces
    name = re.sub(r"\s+", " ", name)
    return name


def _apply_manual(raw: str) -> str | None:
    """Return canonical if raw matches a manual override, else None."""
    key = _normalise_key(raw)
    if key in MANUAL_OVERRIDES:
        return MANUAL_OVERRIDES[key]
    # Strip common prefixes and retry
    for prefix in ("the ", "govt. ", "government of seychelles - "):
        if key.startswith(prefix):
            stripped = key[len(prefix):]
            if stripped in MANUAL_OVERRIDES:
                return MANUAL_OVERRIDES[stripped]
    return None

# ---------------------------------------------------------------------------
# Step 2 — Fuzzy clustering
# ---------------------------------------------------------------------------

def _fuzzy_cluster(names: list[str], counts: dict[str, int]) -> dict[str, str]:
    """
    Cluster name strings by fuzzy similarity.
    Returns {name → canonical_name} for the whole set.
    Canonical = most frequent member of each cluster.
    """
    if not FUZZY_AVAILABLE or not names:
        return {n: n for n in names}

    # Sort by frequency descending so we anchor on the most-seen variant
    sorted_names = sorted(names, key=lambda n: -counts.get(n, 0))

    mapping: dict[str, str] = {}
    canonical_pool: list[str] = []   # one representative per cluster

    for name in sorted_names:
        if name in mapping:
            continue   # already assigned

        if not canonical_pool:
            canonical_pool.append(name)
            mapping[name] = name
            continue

        # Compare against all existing cluster representatives
        best_match, best_score = fuzz_process.extractOne(
            name, canonical_pool,
            scorer=fuzz.token_sort_ratio,
        )

        if best_score >= FUZZY_THRESHOLD:
            # Assign to existing cluster — pick the more frequent as canonical
            canonical = best_match
            if counts.get(name, 0) > counts.get(canonical, 0):
                # This name is more frequent — it becomes the new canonical
                canonical_pool[canonical_pool.index(best_match)] = name
                # Re-map everything that pointed to old canonical
                for k in list(mapping):
                    if mapping[k] == best_match:
                        mapping[k] = name
                canonical = name
            mapping[name] = canonical
        else:
            # New cluster
            canonical_pool.append(name)
            mapping[name] = name

    return mapping

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_org_mapping(
    dataframes: list[pd.DataFrame],
    log_path: str | None = "data/org_normalisation_log.csv",
) -> dict[str, str]:
    """
    Build a {raw_name → canonical_name} mapping from all DataFrames.

    Parameters
    ----------
    dataframes  : list of DataFrames, each expected to have an 'org' column
    log_path    : if set, write an audit CSV showing every mapping decision

    Returns
    -------
    dict mapping every raw org name seen → its canonical form
    """
    # Collect all raw org names + frequency counts
    all_names: list[str] = []
    for df in dataframes:
        if df is not None and not df.empty and "org" in df.columns:
            all_names.extend(df["org"].dropna().astype(str).tolist())

    counts = Counter(all_names)
    unique_raw = [n for n in counts if len(n) >= MIN_NAME_LENGTH]

    print(f"  Org normalisation: {len(unique_raw)} unique raw names found")

    # Pass 1 — manual overrides
    final_mapping: dict[str, str] = {}
    unresolved: list[str] = []
    log_rows: list[dict] = []

    for raw in unique_raw:
        canonical = _apply_manual(raw)
        if canonical:
            final_mapping[raw] = canonical
            log_rows.append({
                "raw": raw,
                "canonical": canonical,
                "method": "manual",
                "score": 100,
                "frequency": counts[raw],
            })
        else:
            unresolved.append(raw)

    # Pass 2 — fuzzy clustering on the unresolved set
    fuzzy_map = _fuzzy_cluster(unresolved, counts)

    for raw, canonical in fuzzy_map.items():
        method = "fuzzy" if raw != canonical else "identity"
        score  = fuzz.token_sort_ratio(raw, canonical) if FUZZY_AVAILABLE and raw != canonical else 100
        final_mapping[raw] = canonical
        log_rows.append({
            "raw": raw,
            "canonical": canonical,
            "method": method,
            "score": score,
            "frequency": counts[raw],
        })

    # Summary
    n_merged = sum(1 for r in log_rows if r["raw"] != r["canonical"])
    print(f"  Org normalisation: {n_merged} names merged "
          f"({len(unique_raw) - n_merged} remain distinct)")

    # Write audit log
    if log_path and log_rows:
        import os
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        log_df = pd.DataFrame(log_rows).sort_values(
            ["method", "raw"], ascending=[False, True]
        )
        log_df.to_csv(log_path, index=False, encoding="utf-8-sig")
        print(f"  Audit log written → {log_path}")

    return final_mapping


def apply_org_mapping(df: pd.DataFrame, mapping: dict[str, str]) -> pd.DataFrame:
    """
    Apply the org mapping to a DataFrame in-place (modifies 'org' column).
    Returns the same DataFrame for chaining.
    """
    if df is None or df.empty or "org" not in df.columns:
        return df
    df["org"] = df["org"].astype(str).map(
        lambda x: mapping.get(x, x)
    )
    return df


def normalise_all(
    dataframes: dict[str, pd.DataFrame],
    log_path: str | None = "data/org_normalisation_log.csv",
) -> dict[str, pd.DataFrame]:
    """
    Convenience wrapper: build mapping from all DFs, apply to all, return.

    Parameters
    ----------
    dataframes : dict of {name → DataFrame}  e.g. from ntb_dashboard.combine()
    log_path   : audit log output path

    Returns
    -------
    Same dict with org columns normalised in place
    """
    df_list = [df for df in dataframes.values() if df is not None and not df.empty]
    mapping = build_org_mapping(df_list, log_path=log_path)
    for df in df_list:
        apply_org_mapping(df, mapping)
    return dataframes
