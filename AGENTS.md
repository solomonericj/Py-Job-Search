# OpenCode Context — Job Match Finder

## Project Overview
Scrapes job postings from LinkedIn/Indeed/ZipRecruiter/Glassdoor/Google via `python-jobspy`, scores them against a skill profile, caches results, tracks application status in SQLite, and sends email notifications.

## Key Files

| File | Purpose |
|------|---------|
| `job_match_finder.py` | Core engine + CLI (~800 lines); imported by the GUI as `jmf` |
| `gui.py` | CustomTkinter desktop app — Search / Results / Config pages |
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
- **CLI** — `argparse` with `--limit`, `--no-cache`, `--no-db`, `--no-notify`, `--verbose`, `--clear-cache`, `--clear-db`, `--config`
- **Entry point for the GUI** — `run(config, limit, no_cache, no_notify, no_db)` is the reusable function `gui.py` calls on a background thread

## Architecture (gui.py)

- Imports `job_match_finder` as `jmf`; reuses its `run()`, `load_config()`, `JobCache`, and path constants (`DEFAULT_CONFIG_PATH`, `DEFAULT_CACHE_PATH`, `DEFAULT_DB_PATH`)
- **JobMatchApp** — `ctk.CTk` root; sidebar nav stacks three page frames and raises the active one
- **SearchPage** — run options (limit / bypass cache / verbose) + live log; search runs on a daemon `threading.Thread`, logs piped back via a `queue.Queue` (`_QueueHandler` for the logger, `_QueueWriter` for `print`/stdout), drained by `after(100, …)`
- **ResultsPage** — `ttk.Treeview` reading `job_search.db` directly; client-side sort/filter (text, status, min-score slider); `DetailPanel` writes `status` + `notes` back via direct SQLite `UPDATE`
- **ConfigPage** — form bound to `config.yaml`; `_load()` reads via `jmf.load_config()`, `_save()` writes YAML with `yaml.dump`
- Status values include `ignored` in addition to the engine's `new` / `saved` / `applied` / `rejected`

## Style Conventions
- Python >= 3.10, uses `str | None` syntax and `from __future__ import annotations`
- Type hints on all functions
- Logging via `logging.getLogger("job_match_finder")` — no bare `print()` except for user-facing output
- `snake_case` for functions/variables, `PascalCase` for classes
- Section comments use `# ═══════════` separators

## Dependencies
- **Required**: python-jobspy, pandas, tabulate, pyyaml, customtkinter (GUI)
- **Optional**: rapidfuzz (faster fuzzy matching), installed via `pip install -r requirements.txt`

## Testing
- No formal test suite. Verify the CLI with `python job_match_finder.py --help`, `python job_match_finder.py --clear-cache`, and `python job_match_finder.py --clear-db`
- Run `python job_match_finder.py --limit 5 --no-cache` for a quick live test
- Launch the GUI with `python gui.py`; sanity-check by opening each page (Search / Results / Config)

## Common Tasks
- **Launch the GUI**: `python gui.py`
- **Change profile**: edit `config.yaml` (skills, location, search terms) — or use the GUI's Config page
- **Quick test**: `python job_match_finder.py --limit 10 --no-cache`
- **Bypass database**: `python job_match_finder.py --no-db`
- **View cached data**: check `job_cache.json`, clear with `--clear-cache`
- **Clear database**: `python job_match_finder.py --clear-db` (resets all scraped jobs, table stays)
- **Track applications**: edit `status` column in `job_matches.csv` or run SQL directly on `job_search.db`
