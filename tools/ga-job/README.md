# ga-job — GoAnywhere Web Client read-only CLI

Read-only access to GoAnywhere MFT (Completed Jobs, Monitors) for NOC alert
triage. Wraps the JSF web UI behind session-cookie scraping because this
tenant does not expose a public REST API for read operations.

## Why session cookies and not API keys

GoAnywhere "Admin User API Keys" (Users → Admin User API Keys) only authorize
**Submit Job** calls — they do not unlock the GAS Web Services REST endpoints
on this deployment. All paths under `/goanywhere/rest/...` return HTTP 500
regardless of the auth header used. The Web Client UI uses Spring + JSF and
requires a session cookie obtained through OKTA login.

Until InfraOps provisions a service account or enables the REST module, the
fastest working approach is:

1. Log in once via Selenium (visible Chrome, OKTA push approved on phone)
2. Cache the `ASESSIONID` cookie at `~/.ga_session.json` (chmod 600)
3. Reuse it for HTTP requests until it expires (~30 min idle)

## Setup

```bash
# Install deps (selenium needs Chrome installed at /Applications/Google Chrome.app)
pip install -r ../../requirements.txt

# First time — OKTA push login
python ga_job.py login
# → Chrome opens, log in via OKTA, approve push on phone
# → Once Dashboard appears, the cookie is captured automatically
```

## Commands

```bash
# Look up a specific Job Number (only the most recent 100 are visible)
python ga_job.py find-job 1000006395396

# List recent jobs filtered by Submitted From / Project / Status / Run User
python ga_job.py list-jobs --submitted-by API-GACMD --status Success
python ga_job.py list-jobs --project 'copy_files'
python ga_job.py list-jobs --user teal.triangle

# Find monitors by name regex
python ga_job.py find-monitor 'jb_edw_resale_tnow.*'

# List all monitors
python ga_job.py list-monitors

# Output as JSON (for piping into other tools)
python ga_job.py find-job 1000006395396 --json
```

## Limitations

- **Recent 100 jobs only**: the Completed Jobs page renders the last 100 jobs
  without a JSF postback. For older jobs, use the GoAnywhere UI directly.
- **No log content**: full job log requires a JSF AJAX postback to populate
  the detail panel — not implemented yet. Look up the Job Number, then open
  the UI for full logs.
- **Session expires**: re-run `login` when the tool reports
  `GA session expired`.

## Bypass Selenium for ad-hoc testing

Set `GA_SESSION_COOKIE` in `.env` (copy from Chrome DevTools → Application →
Cookies, or paste the `Cookie:` header from `Copy as cURL`). The tool will
use it instead of opening Chrome:

```
GA_SESSION_COOKIE=ASESSIONID=ABC...; admin_language=en
```

## Files

- `ga_session.py` — Selenium login + session cache + HTTP helper
- `ga_jobs.py` — `CompletedJobs.xhtml` parser (find_job, filter_jobs, list_completed_jobs)
- `ga_monitors.py` — `ListMonitors.xhtml` parser (find_monitor, list_monitors)
- `ga_job.py` — CLI entry point

## Future work

- JSF postback to fetch full job log on demand
- Service account auth (requires InfraOps FCR)
- Wire into noc-toolkit launcher (`noc-toolkit.py` → ToolDefinition)
