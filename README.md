# Job Match Finder v2

Scrapes job postings from LinkedIn, Indeed, ZipRecruiter, Glassdoor, and Google via **python-jobspy**, then scores each posting against your skill profile. v2 adds fuzzy matching, caching, SQLite tracking, parallel fetching, email notifications, and more.

---

## Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt

# Optional (recommended for faster fuzzy matching):
pip install rapidfuzz
```

### 2. Edit your profile
Open **`config.yaml`** and set your location, search terms, and skills.

### 3. Run
```bash
python job_match_finder.py

# Quick test (10 results, bypass cache):
python job_match_finder.py --limit 10 --no-cache

# Debug logging:
python job_match_finder.py --verbose

# Clear cached results:
python job_match_finder.py --clear-cache
```

### 4. View results
- Top 10 matches are printed to the terminal.
- All results exported to **`job_matches.csv`** (with a `status` column you can edit to track applications).
- Jobs are persisted in **`job_search.db`** (SQLite) — status survives across runs.

---

## What's New in v2

| Feature | Description |
|---------|-------------|
| **External config** | `config.yaml` — edit your profile without touching code |
| **Fuzzy matching** | Catches near-miss keywords (e.g. "analytics" ≈ "analytical"). Uses `rapidfuzz` if installed, falls back to `difflib`. |
| **Title bonus** | Keywords found in the job title score extra points |
| **Salary boost** | Jobs above a salary threshold get a % score bump |
| **Remote boost** | Remote/hybrid jobs get a % score bump |
| **Parallel fetching** | Search terms are fetched concurrently via `ThreadPoolExecutor` |
| **Caching** | Results cached for 30 min (configurable) so re-runs are instant |
| **Retry logic** | Automatic retry with backoff on network errors |
| **SQLite database** | Tracks status (`new` → `saved` → `applied` → `rejected`), preserves across runs |
| **Email notifications** | Get alerted when high-scoring jobs appear |
| **CLI flags** | `--limit`, `--no-cache`, `--no-db`, `--no-notify`, `--verbose`, `--clear-cache` |
| **Logging** | Structured `logging` output instead of bare `print()` |
| **Company normalizer** | Deduplicates "Acme Inc." and "Acme" |
| **`.gitignore`** | Excludes generated CSVs, cache, and DB files |

---

## Configuration (`config.yaml`)

```yaml
profile:
  location: "Nashville, TN"
  search_terms:
    - "Data Analytics"
    - "Director Analytics"
  skills:
    tier1: ["python", "sql", "tableau", ...]   # 3 pts each
    tier2: ["snowflake", "power bi", ...]       # 2 pts each
    tier3: ["data modeling", ...]               # 1 pt each

scoring:
  title_bonus_per_match: 3       # extra % points per title keyword match
  salary_boost:
    enabled: true
    annual_threshold: 80000      # boost applies above this
    boost_pct: 10                # e.g. 80% → 88%
  remote_boost_pct: 5            # boost for remote jobs
  fuzzy:
    enabled: true
    threshold: 80                # 0-100 similarity threshold

notifications:
  email:
    enabled: false
    smtp_server: "smtp.gmail.com"
    sender_email: "your@email.com"
    recipient_email: "your@email.com"
    min_score: 70                # only notify for matches ≥ 70%
    # Password goes in SMTP_PASSWORD env var (never in config!)
```

---

## Output

### `job_matches.csv`
All columns from v1 plus **`status`** (new / saved / applied / rejected — edit in any spreadsheet).

### `job_search.db`
SQLite database persists every job you've ever seen. Edit the `status` column directly:
```sql
UPDATE jobs SET status = 'applied' WHERE job_url = '...';
```

### `job_cache.json`
Cached API responses so re-runs don't hit the boards. Delete or use `--no-cache` to bypass.

---

## Email Notifications

1. Set `notifications.email.enabled: true` in `config.yaml`
2. Set your SMTP password as an environment variable (never in config):
   ```bash
   # Windows (PowerShell)
   $env:SMTP_PASSWORD = "your-app-password"
   
   # macOS / Linux
   export SMTP_PASSWORD="your-app-password"
   ```
3. For Gmail, use an [App Password](https://support.google.com/accounts/answer/185833).

---

## Notes & Limitations

- **LinkedIn rate-limits** aggressive scrapers (~10 pages per IP). Cache helps — you only fetch each term once per TTL.
- The `linkedin_fetch_description=True` flag fetches full descriptions (better scoring) but makes one extra HTTP request per LinkedIn posting.
- All data is **public** (unauthenticated), no login required.
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
