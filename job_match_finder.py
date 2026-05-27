#!/usr/bin/env python3
"""
LinkedIn Job Match Finder v2
Scrapes jobs from LinkedIn / Indeed / ZipRecruiter / Glassdoor / Google via
python-jobspy, scores each posting against your customizable skill profile,
caches results, tracks application status in SQLite, and can email you about
high-scoring matches.

Usage:
    python job_match_finder.py
    python job_match_finder.py --limit 10 --no-cache
    python job_match_finder.py --help
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import sqlite3
import smtplib
import ssl
import sys
import textwrap
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import pandas as pd



# ═══════════════════════════════════════════════════════════════════════════════
# Optional Dependencies (graceful degradation)
# ═══════════════════════════════════════════════════════════════════════════════

try:
    import yaml
except ImportError:
    yaml = None

try:
    from rapidfuzz import fuzz as rapidfuzz_fuzz
except ImportError:
    rapidfuzz_fuzz = None

try:
    from tabulate import tabulate
except ImportError:
    tabulate = None

# ═══════════════════════════════════════════════════════════════════════════════
# Logging
# ═══════════════════════════════════════════════════════════════════════════════

logger = logging.getLogger("job_match_finder")


def setup_logging(verbose: bool = False) -> None:
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    )
    # prevent duplicate handlers on repeated calls
    if not logger.handlers:
        logger.addHandler(handler)


# ═══════════════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_CONFIG_PATH = Path("config.yaml")
DEFAULT_CACHE_PATH = Path("job_cache.json")
DEFAULT_DB_PATH = Path("job_search.db")

DEFAULT_PROFILE: dict[str, Any] = {
    "name": "Data Engineer - Nashville",
    "location": "Nashville, TN",
    "distance_miles": 50,
    "search_terms": [
        "Data Analytics",
        "Director Analytics",
        "Business Intelligence",
        "Insights",
    ],
    "skills": {
        "tier1": [
            "python", "sql", "tableau", "Analytics", "etl",
            "data pipeline", "data governance",
        ],
        "tier2": [
            "sox", "gcp", "power bi", "leadership", "mentoring",
            "snowflake", "data warehouse",
        ],
        "tier3": [
            "data engineering", "data modeling", "data architecture", "cloud data",
        ],
    },
    "job_type": "fulltime",
    "hours_old": 168,
    "results_wanted": 25,
    "sites": ["linkedin", "indeed", "google"],
}

DEFAULT_CONFIG: dict[str, Any] = {
    "profile": DEFAULT_PROFILE,
    "scoring": {
        "tier_weights": {"tier1": 3, "tier2": 2, "tier3": 1},
        "title_bonus_per_match": 3,
        "salary_boost": {
            "enabled": True,
            "annual_threshold": 80000,
            "boost_pct": 10,
        },
        "remote_boost_pct": 5,
        "fuzzy": {"enabled": True, "threshold": 80},
    },
    "notifications": {
        "email": {
            "enabled": False,
            "smtp_server": "smtp.gmail.com",
            "smtp_port": 587,
            "sender_email": "",
            "recipient_email": "",
            "min_score": 70,
        },
    },
    "cache": {"enabled": True, "ttl_minutes": 30},
    "database": {"enabled": True},
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config(config_path: str | None = None) -> dict[str, Any]:
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH

    if path.exists():
        if yaml is None:
            logger.warning("PyYAML not installed (pip install pyyaml). Using defaults.")
            return dict(DEFAULT_CONFIG)
        logger.info("Loading config from %s", path)
        with open(path) as f:
            cfg: dict[str, Any] = yaml.safe_load(f) or {}
        return _deep_merge(DEFAULT_CONFIG, cfg)

    logger.info("No config file found -- using built-in defaults.")
    return dict(DEFAULT_CONFIG)


# ═══════════════════════════════════════════════════════════════════════════════
# Retry helper
# ═══════════════════════════════════════════════════════════════════════════════

def retry(
    max_retries: int = 3,
    base_delay: float = 2.0,
    backoff: float = 2.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
):
    def decorator(func):
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    if attempt < max_retries - 1:
                        delay = base_delay * (backoff**attempt)
                        logger.warning(
                            "Retry %d/%d for %s after %.1fs -- %s",
                            attempt + 1, max_retries, func.__name__, delay, e,
                        )
                        time.sleep(delay)
            logger.error("All %d retries failed for %s: %s", max_retries, func.__name__, last_exc)
            if last_exc is not None:
                raise last_exc  # noqa: TRY201
            raise RuntimeError(f"No retry attempts configured for {func.__name__}")

        return wrapper

    return decorator


# ═══════════════════════════════════════════════════════════════════════════════
# Company name normalizer
# ═══════════════════════════════════════════════════════════════════════════════

_COMPANY_SUFFIXES = (
    ", inc", ", llc", ", ltd", ", corp", ", incorporated",
    " inc", " llc", " ltd", " corp", " incorporated",
    ".inc", ".llc", ".ltd", ".corp",
)


def normalize_company(name: str) -> str:
    if not isinstance(name, str):
        return ""
    name = name.strip().lower().rstrip(".").strip()
    changed = True
    while changed:
        changed = False
        for suffix in _COMPANY_SUFFIXES:
            if name.endswith(suffix):
                name = name[: -len(suffix)].strip().rstrip(".").strip()
                changed = True
                break
    return name


# ═══════════════════════════════════════════════════════════════════════════════
# Cache
# ═══════════════════════════════════════════════════════════════════════════════

class JobCache:
    def __init__(self, cache_path: str = "job_cache.json", ttl_minutes: int = 30):
        self.cache_path = Path(cache_path)
        self.ttl = timedelta(minutes=ttl_minutes)
        self._lock = threading.Lock()
        self._data: dict[str, Any] = self._load()
        self._dirty = False

    def _load(self) -> dict[str, Any]:
        if self.cache_path.exists():
            try:
                with open(self.cache_path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                logger.warning("Cache file corrupt -- starting fresh.")
        return {}

    def _save(self) -> None:
        if self._dirty:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_path, "w") as f:
                json.dump(self._data, f, indent=2, default=self._json_fallback)
            self._dirty = False

    @staticmethod
    def _json_fallback(o: Any) -> str:
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")

    @staticmethod
    def _make_key(search_term: str, location: str, hours_old: int, sites: list[str]) -> str:
        raw = json.dumps(
            {"term": search_term, "loc": location, "hours": hours_old, "sites": sorted(sites)},
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def get(self, search_term: str, location: str, hours_old: int, sites: list[str]) -> list[dict[str, Any]] | None:
        with self._lock:
            key = self._make_key(search_term, location, hours_old, sites)
            entry = self._data.get(key)
            if not entry:
                return None
            cached_at = datetime.fromisoformat(entry["cached_at"])
            if datetime.now(timezone.utc) - cached_at > self.ttl:
                del self._data[key]
                self._dirty = True
                self._save()
                return None
            logger.debug("Cache hit for '%s'", search_term)
            return entry["data"]

    def set(self, search_term: str, location: str, hours_old: int, sites: list[str], data: list[dict[str, Any]]) -> None:
        with self._lock:
            key = self._make_key(search_term, location, hours_old, sites)
            self._data[key] = {
                "cached_at": datetime.now(timezone.utc).isoformat(),
                "data": data,
            }
            self._dirty = True
            self._save()

    def clear(self) -> None:
        with self._lock:
            self._data = {}
            self._dirty = True
            self._save()


# ═══════════════════════════════════════════════════════════════════════════════
# Database
# ═══════════════════════════════════════════════════════════════════════════════

class JobDatabase:
    def __init__(self, db_path: str = "job_search.db"):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                job_url         TEXT UNIQUE,
                site            TEXT,
                title           TEXT,
                company         TEXT,
                city            TEXT,
                state           TEXT,
                job_type        TEXT,
                date_posted     TEXT,
                description     TEXT,
                min_amount      REAL,
                max_amount      REAL,
                is_remote       INTEGER DEFAULT 0,
                search_term     TEXT,
                match_score_pct REAL,
                keyword_count   INTEGER DEFAULT 0,
                matched_keywords TEXT,
                status          TEXT DEFAULT 'new',
                first_seen      TEXT,
                last_seen       TEXT,
                notes           TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_url  ON jobs(job_url);
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
        """)
        self.conn.commit()

    def upsert_job(self, job_data: dict[str, Any]) -> str:
        url = job_data.get("job_url", "")
        if not url:
            return "new"

        existing = self.conn.execute(
            "SELECT status FROM jobs WHERE job_url = ?", (url,)
        ).fetchone()

        now = datetime.now(timezone.utc).isoformat()

        if existing:
            self.conn.execute(
                """
                UPDATE jobs SET
                    match_score_pct = ?, keyword_count = ?, matched_keywords = ?,
                    min_amount = ?, max_amount = ?, last_seen = ?
                WHERE job_url = ?
                """,
                (
                    job_data.get("match_score_pct"),
                    job_data.get("keyword_count", 0),
                    job_data.get("matched_keywords", ""),
                    job_data.get("min_amount"),
                    job_data.get("max_amount"),
                    now,
                    url,
                ),
            )
            return existing["status"]

        date_posted = job_data.get("date_posted")
        if isinstance(date_posted, (date, datetime)):
            date_posted = date_posted.isoformat()

        self.conn.execute(
            """
            INSERT INTO jobs
                (job_url, site, title, company, city, state, job_type,
                 date_posted, description, min_amount, max_amount, is_remote,
                 search_term, match_score_pct, keyword_count, matched_keywords,
                 status, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
            """,
            (
                url,
                job_data.get("site"),
                job_data.get("title"),
                job_data.get("company"),
                job_data.get("city"),
                job_data.get("state"),
                job_data.get("job_type"),
                date_posted,
                job_data.get("description"),
                job_data.get("min_amount"),
                job_data.get("max_amount"),
                1 if job_data.get("is_remote") else 0,
                job_data.get("search_term"),
                job_data.get("match_score_pct"),
                job_data.get("keyword_count", 0),
                job_data.get("matched_keywords", ""),
                now,
                now,
            ),
        )
        return "new"

    def clear(self) -> None:
        self.conn.execute("DELETE FROM jobs;")
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Scoring Engine
# ═══════════════════════════════════════════════════════════════════════════════

def _keyword_match(keyword: str, text: str, fuzzy_enabled: bool, fuzzy_threshold: int) -> bool:
    left  = r"\b" if re.match(r"\w", keyword)   else r"(?<!\w)"
    right = r"\b" if re.search(r"\w$", keyword) else r"(?!\w)"
    if re.search(left + re.escape(keyword) + right, text, re.IGNORECASE):
        return True

    if fuzzy_enabled:
        if rapidfuzz_fuzz is not None:
            return rapidfuzz_fuzz.partial_ratio(keyword.lower(), text.lower()) >= fuzzy_threshold
        if keyword.lower() in text.lower():
            return True
        try:
            import difflib
            words = re.findall(r"\b\w+\b", text.lower())
            return bool(difflib.get_close_matches(keyword.lower(), words, n=1, cutoff=fuzzy_threshold / 100))
        except ImportError:
            pass
    return False


def _calculate_max_score(profile: dict[str, Any], tier_weights: dict[str, int]) -> int:
    total = 0
    for tier, keywords in profile.get("skills", {}).items():
        total += len(keywords) * tier_weights.get(tier, 1)
    return total


def score_job(row: pd.Series, config: dict[str, Any]) -> dict[str, Any]:
    profile = config["profile"]
    scoring = config.get("scoring", {})
    tier_weights = scoring.get("tier_weights", {"tier1": 3, "tier2": 2, "tier3": 1})
    fuzzy_cfg = scoring.get("fuzzy", {})
    fuzzy_enabled = fuzzy_cfg.get("enabled", False)
    fuzzy_threshold = fuzzy_cfg.get("threshold", 80)

    title = str(row.get("title", ""))
    description = str(row.get("description", ""))
    title_lower = title.lower()
    desc_lower = description.lower()

    raw_score = 0
    matched_keywords: list[str] = []
    title_match_count = 0

    for tier, keywords in profile.get("skills", {}).items():
        weight = tier_weights.get(tier, 1)
        for kw in keywords:
            kw_lower = kw.lower()
            in_title = _keyword_match(kw_lower, title_lower, fuzzy_enabled, fuzzy_threshold)
            in_desc = _keyword_match(kw_lower, desc_lower, fuzzy_enabled, fuzzy_threshold)

            if in_title:
                raw_score += weight
                title_match_count += 1
                matched_keywords.append(f"{kw} (title)")
            elif in_desc:
                raw_score += weight
                matched_keywords.append(kw)

    max_score = _calculate_max_score(profile, tier_weights)
    pct = round((raw_score / max_score) * 100, 1) if max_score else 0.0

    title_bonus = scoring.get("title_bonus_per_match", 3)
    if title_match_count > 0 and title_bonus > 0:
        pct = min(100.0, round(pct + (title_match_count * title_bonus), 1))

    salary_cfg = scoring.get("salary_boost", {})
    if salary_cfg.get("enabled", False):
        max_salary = max(
            float(row.get("max_amount") or 0),
            float(row.get("min_amount") or 0),
        )
        threshold = salary_cfg.get("annual_threshold", 80000)
        boost_pct = salary_cfg.get("boost_pct", 10)
        if max_salary >= threshold:
            pct = min(100.0, round(pct + boost_pct, 1))

    remote_boost_pct = scoring.get("remote_boost_pct", 0)
    if remote_boost_pct and str(row.get("is_remote", "")).lower() in ("true", "yes", "1", "t"):
        pct = min(100.0, round(pct + remote_boost_pct, 1))

    return {
        "match_score_pct": pct,
        "raw_score": raw_score,
        "matched_keywords": ", ".join(matched_keywords),
        "keyword_count": len(matched_keywords),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Scraper
# ═══════════════════════════════════════════════════════════════════════════════

@retry(max_retries=2, base_delay=3.0, exceptions=(ConnectionError, TimeoutError, OSError))
def _fetch_single_term(term: str, config: dict[str, Any], cache: JobCache | None) -> pd.DataFrame:
    profile = config["profile"]

    if cache:
        cached = cache.get(term, profile["location"], profile["hours_old"], profile["sites"])
        if cached is not None:
            logger.info("  [cached] '%s' -- %d jobs", term, len(cached))
            return pd.DataFrame(cached)

    logger.info("  Fetching '%s' ...", term)
    try:
        from jobspy import scrape_jobs
    except ImportError:
        raise ImportError(
            "python-jobspy is not installed. Run: pip install python-jobspy"
        ) from None

    scrape_kwargs: dict[str, Any] = dict(
        site_name=profile["sites"],
        search_term=term,
        location=profile["location"],
        distance=profile["distance_miles"],
        job_type=profile["job_type"],
        hours_old=profile["hours_old"],
        results_wanted=profile["results_wanted"],
        linkedin_fetch_description=True,
        country_indeed="USA",
        verbose=0,
    )
    if "user_agent" in config:
        scrape_kwargs["user_agent"] = config["user_agent"]
    if "proxies" in config:
        scrape_kwargs["proxies"] = config["proxies"]

    df = scrape_jobs(**scrape_kwargs)

    if not df.empty:
        df["search_term"] = term
        if cache:
            cache.set(term, profile["location"], profile["hours_old"], profile["sites"],
                      df.to_dict("records"))

    return df


def fetch_jobs(
    config: dict[str, Any], cache: JobCache | None, limit: int | None = None
) -> pd.DataFrame:
    profile = config["profile"]
    cache_cfg = config.get("cache", {})
    use_cache = cache is not None and cache_cfg.get("enabled", True)

    all_frames: list[pd.DataFrame] = []
    terms = profile["search_terms"]

    with ThreadPoolExecutor(max_workers=min(len(terms), 4)) as pool:
        fut_map = {
            pool.submit(
                _fetch_single_term, term, config, cache if use_cache else None
            ): term
            for term in terms
        }
        for future in as_completed(fut_map):
            term = fut_map[future]
            try:
                df = future.result()
                if not df.empty:
                    all_frames.append(df)
            except ImportError:
                raise
            except Exception as exc:
                logger.warning("    Failed for '%s': %s", term, exc)

    if not all_frames:
        return pd.DataFrame()

    combined = pd.concat(all_frames, ignore_index=True)

    if "job_url" in combined.columns:
        before = len(combined)
        combined.drop_duplicates(subset=["job_url"], keep="first", inplace=True)
        dupes = before - len(combined)
        if dupes:
            logger.info("  Removed %d duplicate(s) by URL", dupes)

    if limit is not None and len(combined) > limit:
        combined = combined.head(limit)

    return combined


# ═══════════════════════════════════════════════════════════════════════════════
# Notifications
# ═══════════════════════════════════════════════════════════════════════════════

def send_email_notification(jobs_df: pd.DataFrame, config: dict[str, Any]) -> None:
    email_cfg = config.get("notifications", {}).get("email", {})
    if not email_cfg.get("enabled"):
        return

    sender = email_cfg.get("sender_email", "")
    recipient = email_cfg.get("recipient_email", "")
    if not sender or not recipient:
        logger.warning("Email enabled but sender/recipient not configured.")
        return
    if "@" not in sender or "@" not in recipient:
        logger.warning("Invalid sender or recipient email address -- skipping.")
        return

    smtp_port = email_cfg.get("smtp_port", 587)
    if not isinstance(smtp_port, int) or not (1 <= smtp_port <= 65535):
        logger.warning("Invalid SMTP port %r -- skipping email.", smtp_port)
        return

    min_score = email_cfg.get("min_score", 70)
    high_scorers = jobs_df[jobs_df["match_score_pct"] >= min_score]

    if high_scorers.empty:
        logger.info("No jobs above score %d -- skipping notification.", min_score)
        return

    lines = [f"Top {len(high_scorers)} Job Matches (>= {min_score}%):\n"]
    for _, row in high_scorers.head(10).iterrows():
        lines.append(
            f"  \u2022 {row.get('match_score_pct', '?')}%  "
            f"{row.get('title', 'N/A')} @ {row.get('company', 'N/A')}\n"
            f"    {row.get('job_url', '')}"
        )

    msg = MIMEText("\n".join(lines))
    msg["Subject"] = f"Job Matches -- {len(high_scorers)} new high-scoring jobs"
    msg["From"] = sender
    msg["To"] = recipient

    password = os.environ.get("SMTP_PASSWORD", "")
    if not password:
        logger.warning("SMTP_PASSWORD env var not set -- skipping email.")
        return

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP(
            email_cfg.get("smtp_server", "smtp.gmail.com"),
            smtp_port,
            timeout=30,
        ) as server:
            server.starttls(context=ctx)
            server.login(sender, password)
            server.sendmail(sender, recipient, msg.as_string())
        logger.info("Email notification sent to %s", recipient)
    except Exception as exc:
        logger.error("Failed to send email notification: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════════
# Output & Export
# ═══════════════════════════════════════════════════════════════════════════════

DISPLAY_COLS = [
    "title", "company", "site", "city", "state",
    "match_score_pct", "matched_keywords", "job_url",
]

EXPORT_COLS = [
    "site", "title", "company", "city", "state", "job_type",
    "date_posted", "match_score_pct", "keyword_count",
    "matched_keywords", "min_amount", "max_amount",
    "is_remote", "job_url", "search_term", "status",
]


def display_results(jobs_df: pd.DataFrame) -> None:
    print("\n[3/3] Results - Top Matches")
    print("=" * 60)

    cols = [c for c in DISPLAY_COLS if c in jobs_df.columns]
    top10 = jobs_df.head(10)[cols].copy()

    if "job_url" in top10.columns:
        top10["job_url"] = top10["job_url"].apply(
            lambda u: (str(u)[:60] + "\u2026") if isinstance(u, str) and len(u) > 60 else u
        )
    if "matched_keywords" in top10.columns:
        top10["matched_keywords"] = top10["matched_keywords"].apply(
            lambda k: textwrap.shorten(str(k), width=50, placeholder="\u2026")
        )

    if tabulate is not None:
        print(tabulate(top10, headers="keys", tablefmt="simple", showindex=True))
    else:
        print(top10.to_string())

    bins = [0, 25, 50, 75, 100]
    labels = ["<25%  (Weak)", "25-50% (Fair)", "50-75% (Good)", "75%+  (Strong)"]
    jobs_df["match_tier"] = pd.cut(jobs_df["match_score_pct"], bins=bins, labels=labels, include_lowest=True)
    dist = jobs_df["match_tier"].value_counts().sort_index()
    print("\n[DIST] Score Distribution:")
    for tier, count in dist.items():
        bar = "#" * count
        print(f"  {tier:25s} {count:3d}  {bar}")
    print()


def export_results(jobs_df: pd.DataFrame, db: JobDatabase | None) -> None:
    output_file = "job_matches.csv"
    export_df = jobs_df.copy()

    if db is not None and "job_url" in export_df.columns:
        status_map: dict[str, str] = {}
        rows = db.conn.execute("SELECT job_url, status FROM jobs").fetchall()
        for r in rows:
            status_map[r["job_url"]] = r["status"]
        export_df["status"] = export_df["job_url"].apply(
            lambda u: status_map.get(str(u), "new")
        )
    else:
        export_df["status"] = "new"

    cols = [c for c in EXPORT_COLS if c in export_df.columns]
    export_df[cols].to_csv(
        output_file,
        quoting=csv.QUOTE_NONNUMERIC,
        escapechar="\\",
        index=False,
    )
    logger.info("Full results exported to: %s", output_file)


# ═══════════════════════════════════════════════════════════════════════════════
# Main Pipeline
# ═══════════════════════════════════════════════════════════════════════════════

def run(
    config: dict[str, Any],
    limit: int | None = None,
    no_cache: bool = False,
    no_notify: bool = False,
    no_db: bool = False,
) -> None:
    profile = config["profile"]

    print(f"\n>>> {profile.get('name', 'Job Match Finder')}")
    print("=" * 60)
    print(f"Location: {profile['location']} (+/-{profile['distance_miles']} mi)")
    print(f"Sites   : {', '.join(profile['sites'])}")
    print(f"Posted  : last {profile['hours_old']} hours")
    if limit:
        print(f"Limit   : {limit} results")
    print("=" * 60)

    cache_cfg = config.get("cache", {})
    cache: JobCache | None = None
    if not no_cache and cache_cfg.get("enabled", True):
        cache_path = cache_cfg.get("path", str(DEFAULT_CACHE_PATH))
        cache = JobCache(cache_path, cache_cfg.get("ttl_minutes", 30))

    db_cfg = config.get("database", {})
    db: JobDatabase | None = None
    if not no_db and db_cfg.get("enabled", True):
        db_path = db_cfg.get("path", str(DEFAULT_DB_PATH))
        db = JobDatabase(db_path)

    try:
        print("\n[1/3] Fetching job postings ...")
        jobs_df = fetch_jobs(config, cache, limit)

        if jobs_df.empty:
            print("\n[!] No jobs found. Try broadening search terms or increasing hours_old.")
            return

        print(f"      -> {len(jobs_df)} unique posting(s) retrieved.")

        max_score = _calculate_max_score(profile, config.get("scoring", {}).get("tier_weights", {}))
        print(f"      Max possible raw score: {max_score} pts")

        print("\n[2/3] Scoring postings ...")
        score_rows = [score_job(row, config) for _, row in jobs_df.iterrows()]
        scores_df = pd.DataFrame(score_rows)
        jobs_df = pd.concat([jobs_df.reset_index(drop=True), scores_df], axis=1)

        jobs_df.sort_values("match_score_pct", ascending=False, inplace=True)
        jobs_df.reset_index(drop=True, inplace=True)

        if db is not None:
            for _, row in jobs_df.iterrows():
                db.upsert_job(row.to_dict())
            db.conn.commit()

        display_results(jobs_df)
        export_results(jobs_df, db)

        if not no_notify:
            send_email_notification(jobs_df, config)
    finally:
        if db is not None:
            db.close()


# ═══════════════════════════════════════════════════════════════════════════════
# CLI Entry Point
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Job Match Finder - scrapes & scores jobs against your skill profile.",
    )
    parser.add_argument("--config", "-c", default=None,
                        help="Path to config.yaml (default: config.yaml)")
    parser.add_argument("--limit", "-l", type=int, default=None,
                        help="Max results (quick test)")
    parser.add_argument("--no-cache", action="store_true",
                        help="Bypass cache")
    parser.add_argument("--no-notify", action="store_true",
                        help="Skip email notifications")
    parser.add_argument("--no-db", action="store_true",
                        help="Skip database persistence")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Debug logging")
    parser.add_argument("--clear-cache", action="store_true",
                        help="Clear job cache and exit")
    parser.add_argument("--clear-db", action="store_true",
                        help="Clear job database and exit")

    args = parser.parse_args()
    setup_logging(args.verbose)

    config = load_config(args.config)

    if args.clear_cache:
        cache_path = config.get("cache", {}).get("path", str(DEFAULT_CACHE_PATH))
        JobCache(cache_path).clear()
        print("[OK] Cache cleared.")
        return

    if args.clear_db:
        db_path = config.get("database", {}).get("path", "job_search.db")
        db = JobDatabase(db_path)
        db.clear()
        db.close()
        print("[OK] Database cleared.")
        return

    run(config, limit=args.limit, no_cache=args.no_cache,
        no_notify=args.no_notify, no_db=args.no_db)


if __name__ == "__main__":
    main()
