"""
normalise_orgs.py — Entity Name Normalisation (Orgs + Bidders)
==============================================================
Cleans and deduplicates both procuring entity names (org column)
and bidder/winner names (winner / bidder_name columns) across all
four NTB datasets using a two-pass approach:

Pass 1 — Rule-based canonicalisation
    Lookup tables of known abbreviations, typos, and variants.
    Fast, deterministic, highest priority.

Pass 2 — Fuzzy clustering
    token_sort_ratio handles word-order differences ("Builder Gopinath"
    vs "Gopinath Builder") and minor typos ("Builders" vs "Builder").
    The most frequent name in each cluster becomes canonical.

Audit logs are written to data/ so you can review every decision.

Usage
-----
    from normalise_orgs import normalise_all

    dfs = normalise_all(dfs)   # modifies org + winner columns in place

Tuning
------
    ORG_THRESHOLD    (default 88) — fuzzy threshold for org names
    BIDDER_THRESHOLD (default 85) — slightly lower; bidder names are messier
    Add entries to MANUAL_OVERRIDES or BIDDER_OVERRIDES to fix mistakes.
"""

import os
import re
import unicodedata
from collections import Counter

import pandas as pd

try:
    from thefuzz import fuzz, process as fuzz_process
    FUZZY_AVAILABLE = True
except ImportError:
    FUZZY_AVAILABLE = False
    print("  WARNING: thefuzz not installed — fuzzy matching disabled.\n"
          "           Run: pip install thefuzz python-Levenshtein")

# ---------------------------------------------------------------------------
# Tuning
# ---------------------------------------------------------------------------

ORG_THRESHOLD    = 88
BIDDER_THRESHOLD = 85
MIN_NAME_LENGTH  = 4

# ---------------------------------------------------------------------------
# Procuring entity overrides
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
    "agriculture department":               "Seychelles Agricultural Agency",

    # FIU
    "financial intelligence unit":           "Financial Intelligence Unit",
    "fiu":                                   "Financial Intelligence Unit",

    # Technical section
    "technical section services":            "Technical Section Services",
    "technical section":                     "Technical Section Services",
    "tss":                                   "Technical Section Services",

    # Known typos
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
# Bidder / winner overrides
# Covers the most common variants seen in NTB minutes and awarded PDFs.
# The fuzzy pass handles the long tail automatically.
# ---------------------------------------------------------------------------

BIDDER_OVERRIDES: dict[str, str] = {
    # ── Legal suffix normalisation helpers ───────────────────────────────────
    # (These are applied via _strip_suffix before fuzzy matching,
    #  so you rarely need to list every "(pty) ltd" variant explicitly.)

    # ── A ────────────────────────────────────────────────────────────────────
    "abhaye valabhji pty ltd":               "Abhaye Valabhji Pty Ltd",
    "abhaye valabhji":                       "Abhaye Valabhji Pty Ltd",
    "ace security services":                 "Ace Security Services",
    "ace security":                          "Ace Security Services",
    "active protection services":            "Active Protection Services",
    "active protection":                     "Active Protection Services",
    "adams construction":                    "Adams Construction",
    "adam & son":                            "Adam & Son",
    "adam and son":                          "Adam & Son",
    "adc consulting":                        "ADC Consulting",
    "allied builders (sey) ltd":             "Allied Builders",
    "allied builders sey ltd":               "Allied Builders",
    "allied builders":                       "Allied Builders",
    "all weather builders":                  "All Weather Builders",
    "all weather builder":                   "All Weather Builders",
    "allweather builders":                   "All Weather Builders",
    "amico builders":                        "AMICO Builders",
    "amico investment":                      "AMICO Builders",
    "amico":                                 "AMICO Builders",
    "ascent projects sey pty ltd":           "Ascent Projects",
    "ascent projects (sey) pty ltd":         "Ascent Projects",
    "ascent projects sey":                   "Ascent Projects",
    "ascent project":                        "Ascent Projects",
    "ascent projects":                       "Ascent Projects",
    "ascent engineering supplies":           "Ascent Engineering Supplies",
    "atom engineering":                      "Atom Engineering",

    # ── B ────────────────────────────────────────────────────────────────────
    "bambino agency":                        "Bambino Agency",
    "bb services":                           "BB Services",
    "b & n security":                        "B & N Security",
    "b and n security":                      "B & N Security",
    "benoiton construction co. ltd":         "Benoiton Construction",
    "benoiton construction co ltd":          "Benoiton Construction",
    "benoiton construction":                 "Benoiton Construction",
    "birger international":                  "Birger International",
    "bs construction":                       "BS Construction",

    # ── C ────────────────────────────────────────────────────────────────────
    "cat security":                          "CAT Security",
    "cat security services":                 "CAT Security",

    # ── D ────────────────────────────────────────────────────────────────────
    "ddl builders":                          "DDL Builders",
    "dean builders":                         "Dean Builders",
    "des iles environmental solutions":      "Des Iles Environmental Solutions",
    "des iles environmental":                "Des Iles Environmental Solutions",
    "divy construction":                     "Divy Construction",
    "diqqa":                                 "DIQQA",
    "dubai civil engineering & construction":"Dubai Civil Engineering",
    "dubai civil engineering and construction":"Dubai Civil Engineering",
    "dubai civil engineering":               "Dubai Civil Engineering",
    "dubai civil":                           "Dubai Civil Engineering",

    # ── E ────────────────────────────────────────────────────────────────────
    "earth development pty ltd":             "Earth Development",
    "earth development":                     "Earth Development",
    "elvis labrosse":                        "Elvis Labrosse",
    "era cleaning":                          "Era Cleaning",
    "esparon builders":                      "Esparon Builders",
    "excel motors":                          "Excel Motors",
    "executive logistics":                   "Executive Logistics",
    "executive motors":                      "Executive Motors",
    "executive security service":            "Executive Security",
    "executive security services":           "Executive Security",
    "executive security":                    "Executive Security",
    "express security agency":               "Express Security Agency",
    "express security":                      "Express Security Agency",

    # ── F ────────────────────────────────────────────────────────────────────
    "fabs co construction ltd":              "FABS Co Construction",
    "fabs co construction":                  "FABS Co Construction",
    "fabs construction":                     "FABS Co Construction",
    "fabs co":                               "FABS Co Construction",
    "fair builders construction":            "Fair Builders Construction",
    "fair builders":                         "Fair Builders Construction",
    "fortress enterprise":                   "Fortress Enterprise",
    "furui construction":                    "Furui Construction",

    # ── G ────────────────────────────────────────────────────────────────────
    "gc construction":                       "GC Construction",
    "gibb seychelles":                       "GIBB Seychelles",
    "gmb trading":                           "GMB Trading",
    "gopinath builder (pty) ltd":            "Gopinath Builder Pty Ltd",
    "gopinath builders (pty) ltd":           "Gopinath Builder Pty Ltd",
    "gopinath builder pty ltd":              "Gopinath Builder Pty Ltd",
    "gopinath builders pty ltd":             "Gopinath Builder Pty Ltd",
    "gopinath builder":                      "Gopinath Builder Pty Ltd",
    "gopinath builders":                     "Gopinath Builder Pty Ltd",
    "green island construction":             "Green Island Construction",
    "guard amour security":                  "Guard Amour Security",
    "guard amour":                           "Guard Amour Security",

    # ── H ────────────────────────────────────────────────────────────────────
    "h & r new design & build":              "H & R New Design & Build",
    "h and r new design and build":          "H & R New Design & Build",
    "hari builders":                         "Hari Builders",
    "henri fraise & fils":                   "Henri Fraise & Fils",
    "henri fraise and fils":                 "Henri Fraise & Fils",
    "hpc construction":                      "HPC Construction",

    # ── I ────────────────────────────────────────────────────────────────────
    "incontrol":                             "InControl",
    "infinite waters cleanway services":     "Infinite Waters Cleanway",
    "infinite waters cleanway":              "Infinite Waters Cleanway",
    "interbuild ltd master builders":        "Interbuild Ltd",
    "interbuild":                            "Interbuild Ltd",
    "itech computer services":              "ITECH Computer Services",

    # ── J ────────────────────────────────────────────────────────────────────
    "j & j agency":                          "J & J Agency",
    "j and j agency":                        "J & J Agency",
    "jpm construction":                      "JPM Construction",

    # ── K ────────────────────────────────────────────────────────────────────
    "kingsgate electronic services":         "Kingsgate Electronic Services",

    # ── L ────────────────────────────────────────────────────────────────────
    "la digue island security":              "La Digue Island Security",
    "ladouceur excavation":                  "Ladouceur Excavation",
    "larj enterprise":                       "Larj Enterprise",

    # ── M ────────────────────────────────────────────────────────────────────
    "m & e maintenance":                     "M & E Maintenance",
    "m and e maintenance":                   "M & E Maintenance",
    "mana's cleaning agency":               "Mana's Cleaning Agency",
    "marpol security":                       "Marpol Security",
    "metaluco sey":                          "Metaluco Sey",
    "mj construction":                       "MJ Construction",
    "modern construction":                   "Modern Construction",
    "music store":                           "Music Store",

    # ── N ────────────────────────────────────────────────────────────────────
    "nature solutions agency":               "Nature Solutions Agency",
    "neils construction":                    "Neils Construction",

    # ── O ────────────────────────────────────────────────────────────────────
    "o nivo construction pty ltd":           "O Nivo Construction",
    "o nivo construction":                   "O Nivo Construction",
    "oceanlift pty ltd":                     "Oceanlift",
    "oceanlift":                             "Oceanlift",

    # ── P ────────────────────────────────────────────────────────────────────
    "pintec press holding":                  "Pintec Press Holding",
    "pintec press":                          "Pintec Press Holding",
    "pro archives":                          "Pro Archives",
    "pro-guard security":                    "Pro-Guard Security",
    "pro guard security":                    "Pro-Guard Security",

    # ── Q ────────────────────────────────────────────────────────────────────
    "qingjian international":                "Qingjian International",

    # ── R ────────────────────────────────────────────────────────────────────
    "rafa builders":                         "Rafa Builders",
    "reliance engineering services ltd":     "Reliance Engineering Services",
    "reliance engineering services":         "Reliance Engineering Services",
    "reliance engineering":                  "Reliance Engineering Services",
    "rhs construction":                      "RHS Construction",
    "royal security services":               "Royal Security Services",
    "royal security":                        "Royal Security Services",

    # ── S ────────────────────────────────────────────────────────────────────
    "secure r u s":                          "Secure R U S",
    "seychelles land transport agency":      "Seychelles Land Transport Agency",  # sometimes listed as winner for road surfacing
    "sey-shells construction ltd":           "Seyshells Construction",
    "seyshells construction ltd":            "Seyshells Construction",
    "seyshells construction":                "Seyshells Construction",
    "sey-shells construction":               "Seyshells Construction",
    "sharp security agency":                 "Sharp Security Agency",
    "sharp security":                        "Sharp Security Agency",
    "shield security":                       "Shield Security",
    "sport studio":                          "Sport Studio",
    "storm alarm & security":                "Storm Alarm & Security",
    "storm alarm and security":              "Storm Alarm & Security",
    "storm alarm":                           "Storm Alarm & Security",
    "stronghold security":                   "Stronghold Security",
    "stronghold":                            "Stronghold Security",
    "sulljet":                               "Sulljet",
    "sun excavation pty ltd":                "Sun Excavation",
    "sun excavation":                        "Sun Excavation",
    "sun motors":                            "Sun Motors",
    "sunny ocean ltd":                       "Sunny Ocean Ltd",
    "sunny ocean":                           "Sunny Ocean Ltd",

    # ── T ────────────────────────────────────────────────────────────────────
    "taylor smith and co ltd":               "Taylor Smith",
    "taylor smith & co ltd":                 "Taylor Smith",
    "taylor smith":                          "Taylor Smith",
    "tch pty ltd":                           "TCH Pty Ltd",
    "tch":                                   "TCH Pty Ltd",
    "ted construction":                      "TED Construction",
    "thunder construction pty ltd":          "Thunder Construction",
    "thunder construction ltd":              "Thunder Construction",
    "thunder construction":                  "Thunder Construction",
    "trl construction":                      "TRL Construction",
    "turnkey solutions (sey) ltd":           "Turnkey Solutions",
    "turnkey solutions sey ltd":             "Turnkey Solutions",
    "turnkey solutions":                     "Turnkey Solutions",

    # ── U ────────────────────────────────────────────────────────────────────
    "unique engineering":                    "Unique Engineering",
    "united concrete products":              "United Concrete Products",

    # ── V ────────────────────────────────────────────────────────────────────
    "vcs pty ltd":                           "VCS Pty Ltd",
    "vcs computer services":                 "VCS Pty Ltd",
    "victoria computer services":            "Victoria Computer Services",
    "vijay construction":                    "Vijay Construction",

    # ── W ────────────────────────────────────────────────────────────────────
    "wellpoint development":                 "Wellpoint Development",
    "wer construction":                      "WER Construction",
    "woodshine project company":             "Woodshine Project Company",

    # ── X ────────────────────────────────────────────────────────────────────
    "xt design & build":                     "XT Design & Build",
    "xt design and build":                   "XT Design & Build",
    "xtreme security services":              "Xtreme Security Services",
    "xtreme security":                       "Xtreme Security Services",
    "xwo security":                          "XWO Security",

    # ── Z ────────────────────────────────────────────────────────────────────
    "all weather builders (sey) ltd":        "All Weather Builders",
    "adams construction pty ltd":            "Adams Construction",
    "am upkeep services":                    "AM Upkeep Services",
    "greentech consultants":                 "Greentech Consultants",
    "green tech consultants":                "Greentech Consultants",
    "dean builders pty ltd":                 "Dean Builders",
    "divy construction pty ltd":             "Divy Construction",
}

# ---------------------------------------------------------------------------
# Legal suffix stripping — normalise before fuzzy comparison
# e.g. "Gopinath Builder (Pty) Ltd" → "Gopinath Builder"
# This dramatically improves fuzzy scores for suffix variants.
# ---------------------------------------------------------------------------

_SUFFIX_RE = re.compile(
    r"""
    \s*
    (?:
        \(pty\)[\s.]*ltd |   # (Pty) Ltd
        \(pty\.?\)       |   # (Pty) or (Pty.)
        pty[\s.]*ltd     |   # Pty Ltd
        \(ltd\.?\)       |   # (Ltd) or (Ltd.)
        \bltd\.?         |   # Ltd
        \bco\.?\s*ltd\.? |   # Co. Ltd
        \bco\.?          |   # Co.
        \binc\.?         |   # Inc.
        \bcorp\.?        |   # Corp.
        \s+sey\.?        |   # Sey / Sey.  (suffix form)
        \s+seychelles    |   # Seychelles (suffix)
    )
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

def _strip_suffix(name: str) -> str:
    """Remove legal entity suffixes for fuzzy comparison purposes only."""
    prev = None
    result = name.strip()
    while result != prev:
        prev = result
        result = _SUFFIX_RE.sub("", result).strip(" ,.")
    return result


# ---------------------------------------------------------------------------
# Core helpers (shared by both org and bidder normalisation)
# ---------------------------------------------------------------------------

def _normalise_key(name: str) -> str:
    """Lowercase, strip punctuation noise, collapse whitespace."""
    if not isinstance(name, str):
        return ""
    name = unicodedata.normalize("NFKC", name)
    name = name.lower().strip().strip(" -–—*•·")
    name = re.sub(r"\s+", " ", name)
    return name


def _apply_table(raw: str, table: dict[str, str]) -> str | None:
    """Return canonical if raw matches the lookup table, else None."""
    key = _normalise_key(raw)
    if key in table:
        return table[key]
    for prefix in ("the ", "govt. ", "government of seychelles - "):
        if key.startswith(prefix):
            if key[len(prefix):] in table:
                return table[key[len(prefix):]]
    return None


def _fuzzy_cluster(
    names: list[str],
    counts: dict[str, int],
    threshold: int,
    strip_suffix: bool = False,
) -> dict[str, str]:
    """
    Cluster names by fuzzy similarity.
    Returns {name → canonical_name}.
    Canonical = most frequent member of each cluster.
    strip_suffix: if True, compare stripped forms but keep original as canonical.
    """
    if not FUZZY_AVAILABLE or not names:
        return {n: n for n in names}

    sorted_names = sorted(names, key=lambda n: -counts.get(n, 0))
    mapping: dict[str, str] = {}
    pool: list[str] = []         # canonical representatives
    pool_stripped: list[str] = []  # stripped forms for comparison

    for name in sorted_names:
        if name in mapping:
            continue

        name_cmp = _strip_suffix(name) if strip_suffix else name

        if not pool:
            pool.append(name)
            pool_stripped.append(name_cmp)
            mapping[name] = name
            continue

        best_match, best_score = fuzz_process.extractOne(
            name_cmp, pool_stripped, scorer=fuzz.token_sort_ratio
        )
        idx = pool_stripped.index(best_match)

        if best_score >= threshold:
            canonical = pool[idx]
            if counts.get(name, 0) > counts.get(canonical, 0):
                # Current name is more frequent — promote it
                pool[idx] = name
                pool_stripped[idx] = name_cmp
                for k in list(mapping):
                    if mapping[k] == canonical:
                        mapping[k] = name
                canonical = name
            mapping[name] = canonical
        else:
            pool.append(name)
            pool_stripped.append(name_cmp)
            mapping[name] = name

    return mapping


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_mapping(
    dataframes: list[pd.DataFrame],
    col: str,
    manual_table: dict[str, str],
    threshold: int,
    strip_suffix: bool = False,
    log_path: str | None = None,
    label: str = "names",
) -> dict[str, str]:
    """
    Generic mapping builder for any name column.

    Parameters
    ----------
    dataframes    : list of DataFrames containing `col`
    col           : column name to normalise (e.g. "org" or "winner")
    manual_table  : lookup table of {lowercased_raw → canonical}
    threshold     : fuzzy similarity threshold (0–100)
    strip_suffix  : whether to strip legal suffixes before fuzzy comparison
    log_path      : path to write audit CSV (None = skip)
    label         : human label for print output

    Returns
    -------
    dict {raw_name → canonical_name}
    """
    all_names: list[str] = []
    for df in dataframes:
        if df is not None and not df.empty and col in df.columns:
            all_names.extend(df[col].dropna().astype(str).tolist())

    counts = Counter(all_names)
    unique_raw = [n for n in counts if len(n) >= MIN_NAME_LENGTH]

    print(f"  {label}: {len(unique_raw)} unique raw names found")

    final_mapping: dict[str, str] = {}
    unresolved: list[str] = []
    log_rows: list[dict] = []

    # Pass 1 — manual table
    for raw in unique_raw:
        canonical = _apply_table(raw, manual_table)
        if canonical:
            final_mapping[raw] = canonical
            log_rows.append({
                "col": col, "raw": raw, "canonical": canonical,
                "method": "manual", "score": 100, "frequency": counts[raw],
            })
        else:
            unresolved.append(raw)

    # Pass 2 — fuzzy clustering
    fuzzy_map = _fuzzy_cluster(
        unresolved, counts, threshold, strip_suffix=strip_suffix
    )
    for raw, canonical in fuzzy_map.items():
        method = "fuzzy" if raw != canonical else "identity"
        score  = (fuzz.token_sort_ratio(_strip_suffix(raw), _strip_suffix(canonical))
                  if FUZZY_AVAILABLE and raw != canonical else 100)
        final_mapping[raw] = canonical
        log_rows.append({
            "col": col, "raw": raw, "canonical": canonical,
            "method": method, "score": score, "frequency": counts[raw],
        })

    n_merged = sum(1 for r in log_rows if r["raw"] != r["canonical"])
    print(f"  {label}: {n_merged} names merged "
          f"({len(unique_raw) - n_merged} remain distinct)")

    if log_path and log_rows:
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        log_df = (
            pd.DataFrame(log_rows)
            .sort_values(["method", "raw"], ascending=[False, True])
        )
        log_df.to_csv(log_path, index=False, encoding="utf-8-sig")
        print(f"  Audit log → {log_path}")

    return final_mapping


def build_org_mapping(
    dataframes: list[pd.DataFrame],
    log_path: str | None = "data/org_normalisation_log.csv",
) -> dict[str, str]:
    return build_mapping(
        dataframes, col="org",
        manual_table=MANUAL_OVERRIDES,
        threshold=ORG_THRESHOLD,
        strip_suffix=False,
        log_path=log_path,
        label="Org names",
    )


def build_bidder_mapping(
    dataframes: list[pd.DataFrame],
    log_path: str | None = "data/bidder_normalisation_log.csv",
) -> dict[str, str]:
    """
    Build a mapping for bidder/winner name columns.
    Uses suffix-stripping before fuzzy comparison so "Builder (Pty) Ltd"
    and "Builders Pty Ltd" compare as near-identical.
    """
    # Collect from both 'winner' (awarded/minutes) and 'bidder_name' columns
    return build_mapping(
        dataframes, col="winner",
        manual_table=BIDDER_OVERRIDES,
        threshold=BIDDER_THRESHOLD,
        strip_suffix=True,
        log_path=log_path,
        label="Bidder names",
    )


def apply_mapping(
    df: pd.DataFrame,
    mapping: dict[str, str],
    col: str,
) -> pd.DataFrame:
    """Apply a name mapping to a specific column in a DataFrame."""
    if df is None or df.empty or col not in df.columns:
        return df
    df[col] = df[col].astype(str).map(lambda x: mapping.get(x, x))
    return df


def normalise_all(
    dataframes: dict[str, pd.DataFrame],
    org_log: str | None = "data/org_normalisation_log.csv",
    bidder_log: str | None = "data/bidder_normalisation_log.csv",
) -> dict[str, pd.DataFrame]:
    """
    Normalise both org names and bidder/winner names across all DataFrames.

    Parameters
    ----------
    dataframes : dict {name → DataFrame} as returned by ntb_dashboard.combine()
    org_log    : path for org audit CSV
    bidder_log : path for bidder audit CSV

    Returns
    -------
    Same dict, modified in place.
    """
    df_list = [df for df in dataframes.values() if df is not None and not df.empty]

    print("\n  Building org name mapping…")
    org_mapping = build_org_mapping(df_list, log_path=org_log)

    print("  Building bidder name mapping…")
    bidder_mapping = build_bidder_mapping(df_list, log_path=bidder_log)

    print("  Applying mappings…")
    for df in df_list:
        apply_mapping(df, org_mapping, "org")
        apply_mapping(df, bidder_mapping, "winner")
        # Minutes uses 'bidder_name' in some versions
        if "bidder_name" in df.columns:
            apply_mapping(df, bidder_mapping, "bidder_name")

    return dataframes
