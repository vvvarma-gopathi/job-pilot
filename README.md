# JobPilot AI

JobPilot is a local-first job discovery and evaluation tool. It fetches public listings from Remotive and Arbeitnow, applies a deterministic entry-level relevance filter, stores jobs and application activity in SQLite, and can optionally evaluate eligible jobs with the Google Gemini Developer API. Browser-assisted application preparation and read-only Gmail response tracking are optional; review mode is the default, and automatic submission is disabled.

## Sources

- **Remotive:** public remote-jobs API. Listings retain their source link for attribution. Remotive advises no more than four API requests per day; the default interval limits calls to one every six hours. See https://remotive.com/remote-jobs/api.
- **Arbeitnow:** public paginated job-board API. See https://www.arbeitnow.com/api/job-board-api.
- **Greenhouse and Lever:** public board adapters fetch only validated identifiers discovered from career-page links. The adapters never guess identifiers or brute-force boards.

The adapters normalize source records to the shared Pydantic `Job` model, skip invalid records, deduplicate results, and isolate failures per source. Remotive and Arbeitnow are active immediately. Greenhouse and Lever become active only after their boards are found and validated.

Talivy query generation, query caching, and an injectable adapter interface are present, but no Talivy HTTP/API client is enabled: the workspace contains no Talivy source or API contract. The adapter expects a client returning result dictionaries with `result_type`, original `url`, and, for jobs, a complete `description` marked with `description_complete: true`. Search snippets are never treated as full descriptions. To enable Talivy, its real base URL, authentication, request/response schemas, pagination, rate limits, and original-posting retrieval rules must be supplied. The implementation intentionally does not guess them. Until then, `--source talivy` and `--discover-companies` report the missing contract.

Company career pages found by Talivy can be checked for Greenhouse/Lever links. Crawling a company page is skipped when `robots.txt` cannot be read or disallows access. Board identifiers are validated against the provider endpoint before persistence.

No additional API-key-dependent source (such as Adzuna) is enabled; its credentials and account-specific limits are not configured. USAJobs is excluded because its geography does not match the candidate's India/remote preference.

## Windows setup

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

Edit `config/job_search.json` to configure role aliases, skill keywords, relevance weights, preferences and exclusions. Skill weights influence keyword relevance only; they do not represent candidate proficiency. The optional Gemini API key is read from `.env` and is never stored in the database.

To configure your own candidate data, create the local data directory and copy the blank templates:

```powershell
New-Item -ItemType Directory -Force data | Out-Null
Copy-Item config/candidate_profile.example.json data/candidate_profile.json
Copy-Item config/verified_answers.example.json data/verified_answers.json
```

Edit those local files with accurate details only. Do not put credentials in the profile or verified-answer bank. Leave unknown personal details blank; the application workflow will ask you to review fields it cannot safely answer. Add `GEMINI_API_KEY` to `.env` only if you want AI evaluation. Install the optional integrations you plan to use:

```powershell
uv sync --group dev --extra gmail --extra notifications
```

For discovery and tests without Gmail or toast notifications, `uv sync --group dev` is enough. The Gmail extra is only needed for Gmail commands; the notifications extra enables Windows toast notifications. The browser dependency is installed with the base project. If Playwright's browser download is unavailable, install Chromium with `python -m playwright install chromium` or set `PLAYWRIGHT_BROWSER_CHANNEL` to an installed supported browser channel.

## Commands

```powershell
python main.py discover
python main.py discover --source greenhouse
python main.py discover --source lever
python main.py discover --source talivy
python main.py discover --discover-companies
python main.py evaluate
python main.py run
python main.py recent --limit 25
python main.py stats
python main.py rejected --limit 20
python main.py pending
python main.py companies
python main.py sources
python main.py discovery-stats
```

`run` performs discovery and then evaluation. With no `GEMINI_API_KEY`, evaluation is skipped and eligible jobs remain pending. A Gemini quota/network error also leaves jobs pending for a later retry. Gemini results are validated with Pydantic; Python recomputes the final score using 40% technical skills, 30% experience eligibility, 20% role relevance and 10% location/employment preferences. Decisions are `APPLY` (85+), `REVIEW` (70–84), or `REJECT` (<70), with ineligible or misleading roles rejected regardless of score.

## Application tracking and browser review

Create and inspect local application records:

```powershell
python main.py application add --job-id 12
python main.py applications --limit 25
python main.py application show --id 1
python main.py application stats
python main.py application export --output data\applications.csv
```

Prepare a supported application in a visible browser for manual review:

```powershell
python main.py application prepare --id 1
```

The browser fills only recognized fields using the local profile and verified answers. Review the employer, URL, and every filled field. A submission requires explicitly typing `SUBMIT`; otherwise nothing is submitted. The browser workflow stops for unknown mandatory fields and CAPTCHA, login, OTP, or other security challenges. A submission is considered successful only when an explicit confirmation is visible. If the browser stops after the submission action, the status is `submission_unverified`; check the employer site before taking any further action. Tests use mock forms only and never submit real applications.

`APPLICATION_MODE=review` and `AUTO_APPLY_ENABLED=false` are the safe defaults. Do not enable automatic submission until you have personally verified the profile, answers, and supported employer workflows. The automated path is restricted by eligibility, AI score, duplicate checks, configured daily limits, recognized forms, and explicit confirmation detection; it does not apply on your behalf in the default configuration.

## Optional Gmail response tracking

Install the Gmail extra, create a local OAuth desktop client JSON from Google Cloud Console, and point `GMAIL_OAUTH_CLIENT_FILE` to that file. Then authorize and sync:

```powershell
python main.py gmail auth
```

Set `GMAIL_ENABLED=true` in `.env` to enable scheduled/manual sync, then run `python main.py gmail sync` and inspect results with `python main.py recruiter-emails`. OAuth requests only Gmail's `gmail.readonly` scope; refresh credentials are kept in Windows Credential Manager, not SQLite. Email bodies are processed in memory and are not persisted. Gemini email classification is disabled by default; enabling it sends message content to the configured Gemini API, so only opt in if that external processing is acceptable.

## Local background agent

The local agent schedules discovery and evaluation using the `AGENT_DISCOVERY_INTERVAL_HOURS` and `AGENT_EVALUATION_INTERVAL_HOURS` settings. It also queues eligible jobs in the application dashboard; Gmail polling is scheduled only when enabled. The persistent task table supports recovery after restarts, while interrupted submission attempts require manual verification and are never automatically retried.

Run one cycle or start the persistent agent directly:

```powershell
python main.py agent run-once
python main.py agent run
python main.py agent status
```

For Windows Task Scheduler integration, use these scripts from the repository root:

```powershell
.\scripts\install_agent.ps1
.\scripts\start_agent.ps1
.\scripts\status_agent.ps1
.\scripts\stop_agent.ps1
```

The scheduled task starts at the signed-in user's logon and runs with that user's limited privileges. `stop_agent.ps1` requests graceful shutdown and waits for the process lock to be released; it does not force-terminate the process. Agent logs are written to `logs\agent.log`. Discovery, Gemini evaluation, browser automation, OAuth authorization, and notifications still depend on network access, configured credentials, and the optional packages/browser noted above.

`discover --source greenhouse` and `discover --source lever` fetch from already validated boards. `companies` lists known companies/boards; `sources` shows which integrations are ready; `discovery-stats` summarizes prior runs. With no Talivy contract, company discovery and Talivy search cannot run yet.

## Configuration

The project reads `.env` from the current project directory. Example settings:

```dotenv
JOBPILOT_DB_PATH=data/jobpilot.db
JOBPILOT_SEARCH_CONFIG=config/job_search.json
HTTP_TIMEOUT_SECONDS=25
HTTP_MAX_RETRIES=3
HTTP_USER_AGENT=JobPilotAI/0.2 (personal local job discovery)
REMOTIVE_MIN_INTERVAL_HOURS=6
ARBEITNOW_MIN_INTERVAL_HOURS=6
ATS_MIN_INTERVAL_HOURS=24
ARBEITNOW_MAX_PAGES=10
MIN_JOB_MATCH_SCORE=40
GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.5-flash
GEMINI_MAX_RETRIES=3
```

Create a key in Google AI Studio if you want Gemini evaluation; free-tier availability and quotas depend on Google's current account and model limits. No key is required for discovery or deterministic filtering.

## Filtering and persistence

- Configurable title aliases and technical keywords allow title variants without requiring an exact title.
- Senior/managerial titles, unpaid/volunteer roles, mismatching locations, insecure/shortened application links, and explicit mandatory 2+ year requirements are rejected before Gemini.
- Unknown experience requirements remain eligible for review by default, not asserted as fresher-eligible.
- Rejections and their reasons are stored for inspection. Every source record also retains its original payload for traceability.
- SQLite migrations add columns/tables without dropping existing data. Jobs deduplicate using source IDs, canonical URLs and the existing normalized fingerprint. The SHA-256 description hash and evaluation cache prevent repeated Gemini analysis of unchanged content.
- API calls use bounded timeouts/retries. Both sources have configurable fetch intervals; Remotive's default six-hour interval respects its four-fetches-per-day guidance. Arbeitnow page traversal follows its pagination links up to `ARBEITNOW_MAX_PAGES`.

Database defaults to `data/jobpilot.db`. Remotive attribution is displayed as the original listing URL. Relevance scores are prioritization heuristics, not a probability of getting hired.

## Tests

```powershell
uv sync --group dev
uv run python -m pytest -q
```

Tests use local fixtures/mocks and never make applications or require live job APIs/Gemini credentials. Live discovery requires network access; AI evaluation additionally requires a valid Gemini API key and available quota.

## Example output

```text
[OK] arbeitnow: fetched=120, relevant=4, inserted=4, updated=0, filtered=116
Shortlisted jobs in database: 4
AI evaluation: evaluated=4, pending=0, failed=0
```
