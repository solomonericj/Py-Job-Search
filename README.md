# LinkedIn Job Match Finder

A self-contained Python script that **scrapes job postings from LinkedIn, Indeed, and ZipRecruiter** and automatically **scores each posting** against your skill/keyword profile — so the best matches bubble to the top.

---

## Quick Start

### 1. Install dependencies
```bash
pip install python-jobspy pandas tabulate
# Python >= 3.10 required
```

### 2. Run
```bash
python job_match_finder.py
```

### 3. View results
- Top 10 matches are printed to the terminal.
- All results are exported to **`job_matches.csv`** (sortable in Excel / Pandas).

---

## How It Works

```
Search Terms × Job Boards
        │
        ▼
  python-jobspy ──► LinkedIn, Indeed, ZipRecruiter  (concurrent)
        │
        ▼
  Deduplicate by job_url
        │
        ▼
  Keyword Scorer
    tier1 keywords × 3 pts  (Python, PySpark, Spark, SQL, ETL …)
    tier2 keywords × 2 pts  (Java, Kafka, Airflow, AWS, Azure …)
    tier3 keywords × 1 pt   (Kubernetes, Terraform, dbt …)
        │
        ▼
  match_score_pct = raw_score / max_possible × 100
        │
        ▼
  Sort descending → export job_matches.csv
```

---

## Customising Your Profile

Edit the `PROFILE` dict at the top of `job_match_finder.py`:

| Key | What to change |
|-----|---------------|
| `location` | Your city/state (e.g. `"Nashville, TN"`) |
| `distance_miles` | Search radius |
| `search_terms` | Job titles you want to match |
| `skills.tier1/2/3` | Keywords ranked by importance to you |
| `hours_old` | Only show jobs posted in the last N hours |
| `results_wanted` | How many results to fetch per site |
| `sites` | Which boards to query (linkedin, indeed, zip_recruiter, glassdoor, google) |

---

## Output Columns (`job_matches.csv`)

| Column | Description |
|--------|-------------|
| `site` | Source job board |
| `title` | Job title |
| `company` | Employer |
| `city` / `state` | Location |
| `match_score_pct` | Your % match (0–100) |
| `keyword_count` | Number of matching skills found |
| `matched_keywords` | Comma-separated list of matching skills |
| `min_amount` / `max_amount` | Salary range (when available) |
| `is_remote` | Remote flag |
| `job_url` | Direct link to the posting |
| `description` | Full job description (use for deeper NLP later) |

---

## Notes & Limitations

- **LinkedIn rate-limits** aggressive scrapers (~10 pages per IP). If you hit 429 errors,
  add the `proxies` parameter or wait ~1 hour between runs.
- The `linkedin_fetch_description=True` flag fetches full descriptions (more accurate scoring)
  but makes one extra HTTP request per LinkedIn posting.
- All data is **public** (unauthenticated), so LinkedIn login is not required.
- To avoid blocking, don't run more than once every 30–60 minutes against the same board.

---

## Extending the Script

**Add NLP scoring (OpenAI):**
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

**Schedule daily runs (cron):**
```bash
# runs every morning at 8 AM
0 8 * * * /usr/bin/python3 /path/to/job_match_finder.py >> ~/job_scraper.log 2>&1
```

**Filter only high matches in Pandas:**
```python
import pandas as pd
df = pd.read_csv("job_matches.csv")
strong = df[df["match_score_pct"] >= 50].sort_values("match_score_pct", ascending=False)
print(strong[["title", "company", "match_score_pct", "job_url"]])
```
