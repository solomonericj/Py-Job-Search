# OpenCode Context — Job Match Finder

## Project Overview
Scrapes job postings from LinkedIn/Indeed/ZipRecruiter/Glassdoor/Google via `python-jobspy`, scores them against a skill profile, caches results, tracks application status in SQLite, and sends email notifications.

## Key Files

| File | Purpose |
|------|---------|
| `job_match_finder.py` | Main script (~800 lines) |
| `config.yaml` | User profile, scoring, notification, cache & DB settings |
| `requirements.txt` | Python dependencies |
| `job_matches.csv` | Exported results (gitignored) |
| `job_search.db` | SQLite job history (gitignored) |
| `job_cache.json` | Cached API responses (gitignored) |

## Architecture (job_match_finder.py)
- **Config** — `load_config()` reads YAML; falls back to built-in defaults
- **Cache** — `JobCache` class, JSON file, SHA256-keyed entries, configurable TTL
- **Database** — `JobDatabase` class, SQLite, upserts preserve `status` field
- **Scoring** — `score_job()` with weighted tiers, fuzzy matching (`rapidfuzz`|`difflib`), title bonus, salary/remote boosts
- **Scraper** — `fetch_jobs()` parallelizes search terms via `ThreadPoolExecutor`, `_fetch_single_term()` has `@retry` decorator
- **Notifications** — `send_email_notification()` via SMTP, password from `SMTP_PASSWORD` env var
- **CLI** — `argparse` with `--limit`, `--no-cache`, `--no-db`, `--no-notify`, `--verbose`, `--clear-cache`, `--config`

## Style Conventions
- Python >= 3.10, uses `str | None` syntax and `from __future__ import annotations`
- Type hints on all functions
- Logging via `logging.getLogger("job_match_finder")` — no bare `print()` except for user-facing output
- `snake_case` for functions/variables, `PascalCase` for classes
- Section comments use `# ═══════════` separators

## Dependencies
- **Required**: python-jobspy, pandas, tabulate, pyyaml
- **Optional**: rapidfuzz (faster fuzzy matching), installed via `pip install -r requirements.txt`

## Testing
- No formal test suite. Verify with `python job_match_finder.py --help` and `python job_match_finder.py --clear-cache`
- Run `python job_match_finder.py --limit 5 --no-cache` for a quick live test

## Common Tasks
- **Change profile**: edit `config.yaml` (skills, location, search terms)
- **Quick test**: `python job_match_finder.py --limit 10 --no-cache`
- **Bypass database**: `python job_match_finder.py --no-db`
- **View cached data**: check `job_cache.json`, clear with `--clear-cache`
- **Track applications**: edit `status` column in `job_matches.csv` or run SQL directly on `job_search.db`
