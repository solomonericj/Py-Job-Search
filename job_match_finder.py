#!/usr/bin/env python3
"""
LinkedIn Job Match Finder
Scrapes job postings from LinkedIn (and other boards) using python-jobspy,
then scores each posting against a customizable skill/keyword profile.

Requirements:
    pip install python-jobspy pandas tabulate

Usage:
    python job_match_finder.py
"""

import csv
import re
import textwrap
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

# ── 1. USER PROFILE ──────────────────────────────────────────────────────────
# Edit this section to match your skills, preferred location, and job titles.

PROFILE = {
    "name": "Data Engineer – Nashville",
    "location": "Nashville, TN",          # city/state passed to every board
    "distance_miles": 50,                  # radius for boards that support it
    "search_terms": [
        "Data Analytics",
        "Director Analytics",
        "Business Intelligence",
        "Insights",
    ],
    # Keywords weighted by tier; higher tier = more impact on match score
    "skills": {
        "tier1": [                         # Must-have / core skills
            "python", "sql", "tableau", "Analytics", "etl",
            "data pipeline", "data governance",
        ],
        "tier2": [                         # Important / strong match
            "sox", "gcp", "power bi", "leadership", "mentoring",
            "snowflake","data warehouse",
        ],
        "tier3": [                         # Nice-to-have / bonus
            "data engineering", "data modeling", "data architecture", "cloud data"
        ],
    },
    "job_type": "fulltime",               # fulltime | parttime | contract | internship
    "hours_old": 168,                      # only jobs posted in the last 7 days
    "results_wanted": 25,                  # per site
    "sites": ["linkedin", "indeed", "zip_recruiter", "glassdoor", "google"],
}

# Scoring weights per tier
TIER_WEIGHTS = {"tier1": 3, "tier2": 2, "tier3": 1}
MAX_SCORE_POSSIBLE = (
    len(PROFILE["skills"]["tier1"]) * TIER_WEIGHTS["tier1"]
    + len(PROFILE["skills"]["tier2"]) * TIER_WEIGHTS["tier2"]
    + len(PROFILE["skills"]["tier3"]) * TIER_WEIGHTS["tier3"]
)


# ── 2. SCORING ENGINE ─────────────────────────────────────────────────────────

def score_job(row: pd.Series) -> dict:
    """Score a job row against the skill profile and return a match dict."""
    text = " ".join([
        str(row.get("title", "")),
        str(row.get("description", "")),
    ]).lower()

    raw_score = 0
    matched_keywords = []

    for tier, keywords in PROFILE["skills"].items():
        weight = TIER_WEIGHTS[tier]
        for kw in keywords:
            pattern = r"\b" + re.escape(kw) + r"\b"
            if re.search(pattern, text):
                raw_score += weight
                matched_keywords.append(kw)

    pct = round((raw_score / MAX_SCORE_POSSIBLE) * 100, 1) if MAX_SCORE_POSSIBLE else 0.0

    return {
        "match_score_pct": pct,
        "raw_score": raw_score,
        "matched_keywords": ", ".join(matched_keywords),
        "keyword_count": len(matched_keywords),
    }


# ── 3. SCRAPER ────────────────────────────────────────────────────────────────

def fetch_jobs() -> pd.DataFrame:
    """Fetch jobs from all configured job boards and return a combined DataFrame."""
    try:
        from jobspy import scrape_jobs
    except ImportError:
        raise ImportError(
            "\npython-jobspy is not installed. Run:\n"
            "    pip install python-jobspy\n"
        )

    all_frames = []

    for term in PROFILE["search_terms"]:
        print(f"  Searching: '{term}' in {PROFILE['location']} …")
        try:
            df = scrape_jobs(
                site_name=PROFILE["sites"],
                search_term=term,
                location=PROFILE["location"],
                distance=PROFILE["distance_miles"],
                job_type=PROFILE["job_type"],
                hours_old=PROFILE["hours_old"],
                results_wanted=PROFILE["results_wanted"],
                linkedin_fetch_description=True,  # full description from LinkedIn
                country_indeed="USA",
                verbose=0,
            )
            if not df.empty:
                df["search_term"] = term
                all_frames.append(df)
        except Exception as exc:
            print(f"    ⚠  Warning: could not fetch results for '{term}': {exc}")

    if not all_frames:
        return pd.DataFrame()

    combined = pd.concat(all_frames, ignore_index=True)

    # Deduplicate by job URL (same posting may appear under different search terms)
    if "job_url" in combined.columns:
        combined.drop_duplicates(subset=["job_url"], keep="first", inplace=True)

    return combined


# ── 4. MAIN PIPELINE ──────────────────────────────────────────────────────────

def run():
    print("\n🔍  LinkedIn Job Match Finder")
    print("=" * 60)
    print(f"Profile : {PROFILE['name']}")
    print(f"Location: {PROFILE['location']} (±{PROFILE['distance_miles']} mi)")
    print(f"Sites   : {', '.join(PROFILE['sites'])}")
    print(f"Posted  : last {PROFILE['hours_old']} hours")
    print("=" * 60)

    print("\n[1/3] Fetching job postings …")
    jobs_df = fetch_jobs()

    if jobs_df.empty:
        print("\n❌  No jobs found. Try broadening your search terms or increasing hours_old.")
        return

    print(f"      → {len(jobs_df)} unique postings retrieved.")

    print("\n[2/3] Scoring postings against your skill profile …")
    score_rows = [score_job(row) for _, row in jobs_df.iterrows()]
    scores_df = pd.DataFrame(score_rows)
    jobs_df = pd.concat([jobs_df.reset_index(drop=True), scores_df], axis=1)

    # Sort by match score descending
    jobs_df.sort_values("match_score_pct", ascending=False, inplace=True)
    jobs_df.reset_index(drop=True, inplace=True)

    print("\n[3/3] Results – Top Matches")
    print("=" * 60)

    display_cols = ["title", "company", "site", "city", "state",
                    "match_score_pct", "matched_keywords", "job_url"]
    display_cols = [c for c in display_cols if c in jobs_df.columns]

    # Pretty-print top 10
    top10 = jobs_df.head(10)[display_cols].copy()
    if "job_url" in top10.columns:
        top10["job_url"] = top10["job_url"].apply(
            lambda u: (str(u)[:60] + "…") if isinstance(u, str) and len(str(u)) > 60 else u
        )
    if "matched_keywords" in top10.columns:
        top10["matched_keywords"] = top10["matched_keywords"].apply(
            lambda k: textwrap.shorten(str(k), width=50, placeholder="…")
        )

    try:
        from tabulate import tabulate
        print(tabulate(top10, headers="keys", tablefmt="rounded_outline", showindex=True))
    except ImportError:
        print(top10.to_string())

    # ── Export ────────────────────────────────────────────────────────────────
    output_file = "job_matches.csv"
    export_cols = [c for c in [
        "site", "title", "company", "city", "state", "job_type",
        "date_posted", "match_score_pct", "keyword_count",
        "matched_keywords", "min_amount", "max_amount",
        "is_remote", "job_url", "description", "search_term",
    ] if c in jobs_df.columns]

    jobs_df[export_cols].to_csv(
        output_file,
        quoting=csv.QUOTE_NONNUMERIC,
        escapechar="\\",
        index=False,
    )
    print(f"\n✅  Full results exported to: {output_file}")
    print(f"    Scored {len(jobs_df)} jobs | Max possible score: {MAX_SCORE_POSSIBLE} pts")

    # ── Score distribution summary ────────────────────────────────────────────
    bins = [0, 25, 50, 75, 100]
    labels = ["<25%  (Weak)", "25–50% (Fair)", "50–75% (Good)", "75%+  (Strong)"]
    jobs_df["match_tier"] = pd.cut(
        jobs_df["match_score_pct"], bins=bins, labels=labels, include_lowest=True
    )
    dist = jobs_df["match_tier"].value_counts().sort_index()
    print("\n📊  Score Distribution:")
    for tier, count in dist.items():
        bar = "█" * count
        print(f"  {tier:25s} {count:3d}  {bar}")

    print()


if __name__ == "__main__":
    run()
