# Job Match Finder

Scrapes job postings from LinkedIn, Indeed, and Google via **python-jobspy**, scores each posting against your customizable skill profile, caches results, tracks application status in SQLite, and optionally notifies you about high-scoring matches.

---

## Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt

# Optional (recommended for faster fuzzy matching):
pip install rapidfuzz
```

### 2. Configure your profile
Copy `config.yaml.example` to `config.yaml` and edit your location, search terms, and skills. `config.yaml` is gitignored to prevent credential leaks.

### 3. Run
```bash
python job_match_finder.py

# Quick test (10 results, bypass cache):
python job_match_finder.py --limit 10 --no-cache

# Use a custom config file:
python job_match_finder.py --config my_profile.yaml

# Debug logging:
python job_match_finder.py --verbose

# Clear cached results:
python job_match_finder.py --clear-cache

# Clear the database (jobs table):
python job_match_finder.py --clear-db
```

### 4. View results
- Top 10 matches printed to the terminal with a score distribution histogram.
- All results exported to **`job_matches.csv`** (edit the `status` column to track applications).
- Jobs persisted in **`job_search.db`** (SQLite) — status survives across runs.

---

## Features

| Feature | Description |
|---------|-------------|
| **External config** | `config.yaml` — edit profile without touching code |
| **Multi-site scraping** | LinkedIn, Indeed, Google (Glassdoor/ZipRecruiter blocked server-side) |
| **Tiered scoring** | Skills grouped into 3 tiers with configurable weights (3 / 2 / 1 pts) |
| **Fuzzy matching** | Catches near-miss keywords (e.g. "analytics" ≈ "analytical"). Uses `rapidfuzz` if installed, falls back to `difflib`. |
| **Title bonus** | Keywords found in job titles score extra percentage points |
| **Salary boost** | Jobs above a salary threshold get a % score bump |
| **Remote boost** | Remote/hybrid jobs get a % score bump |
| **Parallel fetching** | Search terms fetched concurrently via `ThreadPoolExecutor` (max 4 workers) |
| **Caching** | Results cached for 30 min (configurable); re-runs are instant |
| **Retry logic** | Automatic retry with exponential backoff on network errors |
| **SQLite database** | Tracks status (`new` → `saved` → `applied` → `rejected`), preserves across runs |
| **Email notifications** | Get alerted when high-scoring jobs appear |
| **CLI flags** | `--limit`, `--no-cache`, `--no-db`, `--no-notify`, `--verbose`, `--clear-cache`, `--config` |
| **Company normalizer** | Deduplicates "Acme Inc." and "Acme" |
| **Config template** | `config.yaml.example` tracked in git; `config.yaml` is gitignored |

---

## Configuration (`config.yaml`)

```yaml
profile:
  name: "Data Engineer - Nashville"
  location: "Nashville, TN"
  distance_miles: 50
  search_terms:
    - "Data Analytics"
    - "Director Analytics"
  skills:
    tier1: ["python", "sql", "tableau", ...]    # 3 pts each
    tier2: ["snowflake", "power bi", ...]        # 2 pts each
    tier3: ["data modeling", ...]                # 1 pt each
  job_type: "fulltime"
  hours_old: 168          # only postings from last 7 days
  results_wanted: 25      # per search term per site
  sites:
    - "linkedin"
    - "indeed"
    - "google"

scoring:
  tier_weights:
    tier1: 3
    tier2: 2
    tier3: 1
  title_bonus_per_match: 3        # extra % points per title keyword match
  salary_boost:
    enabled: true
    annual_threshold: 80000       # boost applies above this
    boost_pct: 10                 # e.g. 80% → 88%
  remote_boost_pct: 5             # boost for remote jobs
  fuzzy:
    enabled: true
    threshold: 80                 # 0-100 similarity threshold

notifications:
  email:
    enabled: false
    smtp_server: "smtp.gmail.com"
    smtp_port: 587
    sender_email: ""
    recipient_email: ""
    min_score: 70                 # only notify for matches ≥ 70%

cache:
  enabled: true
  ttl_minutes: 30

database:
  enabled: true
```

---

## Output

### `job_matches.csv`
Site, title, company, city, state, job type, date posted, match score, keyword count, matched keywords, salary range, remote flag, URL, search term, and **`status`** (new / saved / applied / rejected).

### `job_search.db`
SQLite database with a `jobs` table. Edit status directly:
```sql
UPDATE jobs SET status = 'applied' WHERE job_url = '...';
```

### `job_cache.json`
Cached API responses so re-runs don't hit the boards. Delete or use `--no-cache` to bypass.

---

## Email Notifications

1. Set `notifications.email.enabled: true` in `config.yaml`
2. Set your SMTP password as an environment variable:
   ```bash
   # Windows (PowerShell)
   $env:SMTP_PASSWORD = "your-app-password"

   # macOS / Linux
   export SMTP_PASSWORD="your-app-password"
   ```
3. For Gmail, use an [App Password](https://support.google.com/accounts/answer/185833).

---

## Notes & Limitations

- **LinkedIn rate-limits** aggressive scrapers (~10 pages per IP). Caching helps — you only fetch each term once per TTL.
- **Glassdoor (400) and ZipRecruiter (403)** are blocked server-side. LinkedIn, Indeed, and Google are the most reliable sources.
- The script always fetches full job descriptions (`linkedin_fetch_description=True`) for better scoring.
- All data is from **public** (unauthenticated) endpoints — no login required.
- Don't run more than once every 30–60 minutes against the same board.

---

## Extending

**Add AI scoring:**
```python
import openai
def ai_score(description, profile_summary):
    resp = openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{
            "role": "user",
            "content": f"Rate 0-100 how well this job matches: {profile_summary}\n\nJob:\n{description}"
        }]
    )
    return int(resp.choices[0].message.content.strip())
```

**Schedule daily (cron):**
```bash
0 8 * * * /usr/bin/python3 /path/to/job_match_finder.py >> ~/job_scraper.log 2>&1
```

**Filter high matches:**
```python
import pandas as pd
df = pd.read_csv("job_matches.csv")
strong = df[df["match_score_pct"] >= 50].sort_values("match_score_pct", ascending=False)
print(strong[["title", "company", "match_score_pct", "job_url"]])
```
