# cdt — CDT Control Panel API CLI

Read-only access to the CDT (Core Data Tools / Teal Triangle) Control Panel
API at `controlpanel.prd2971.prod9.us-east-1.tktm.io` for NOC alert triage.

Unlike GoAnywhere, CDT exposes a **proper REST API** with OAuth2 Bearer
tokens issued from the UI — no Selenium login or session scraping needed.

## Setup

1. Open https://controlpanel-ui.prd2971.prod9.us-east-1.tktm.io/tt/platform/tokens
2. Click **+ Create**
3. Fill in: name (`noc-toolkit-readonly`), description, expiration (max ~2 months)
4. Save → copy the token immediately (cannot be retrieved later)
5. Add to `.env`:

```
CDT_BASE_URL=https://controlpanel.prd2971.prod9.us-east-1.tktm.io
CDT_API_TOKEN=<paste>
CDT_API_TOKEN_EXPIRES=2026-07-16
```

> **Note**: Use the API host (`controlpanel.*`), NOT the UI host
> (`controlpanel-ui.*`) — the UI host is a SPA that always returns HTML.

## Commands

```bash
# Sanity check
python cdt.py health

# Streaming jobs (110 in prod)
python cdt.py streaming
python cdt.py streaming --status running
python cdt.py find-streaming 'jb_edw_resale_tnow'

# Batch jobs (532 in prod)
python cdt.py batch
python cdt.py batch --status failed
python cdt.py find-batch 'talend-sfmc-pns'

# SLA breaches
python cdt.py sla
python cdt.py sla --type batch --status failed --since-hours 24
python cdt.py sla --name 'jb_edw_dsn_sls'

# Other environments
python cdt.py streaming --env preprod
python cdt.py streaming --env nonprod

# JSON output
python cdt.py find-batch 'talend-sfmc-pns' --json
```

## Architecture

Three small modules:

- `cdt_client.py` — thin `CDTClient` wrapper over `requests.Session` with
  Bearer auth. Raises `CDTAuthError` on 401 with a hint to re-create token.
- `cdt_dashboards.py` — dashboard fetchers and filters (`streaming_dashboard`,
  `batch_dashboard`, `sla_breaches`, `find_streaming_job`, `find_batch_job`).
- `cdt.py` — CLI entry point.

## OpenAPI spec

The full API spec is unauthenticated:
```bash
curl -sk https://controlpanel.prd2971.prod9.us-east-1.tktm.io/openapi.json | jq .
```
278 paths total — this CLI exposes only the read-only NOC subset
(streaming/batch dashboards + SLA breaches). For exploration:

```bash
# Health and auth
GET /health             # public, returns "OK"
GET /auth               # requires token, returns true

# Dashboards
GET /streaming_dashboard?environment=<env>
GET /batch_dashboard?environment=<env>

# SLA
GET /sla_monitor/breaches?environment=<env>
GET /sla_monitor/breaches/batch
GET /sla_monitor/breaches/client_feed

# Service inventory
GET /service_instances
GET /service_types
GET /service_groups

# CCPA, RDS, latency_reports, etc.
```

## Token rotation

Tokens are short-lived (max ~2 months by org policy). The tool will return
HTTP 401 with a clear message when the token expires. Re-create at
`/tt/platform/tokens` and update `.env`.

## Comparison with ga-job

| Aspect | ga-job (GoAnywhere) | cdt (Control Panel) |
|---|---|---|
| API | None (HTML scraping) | Proper REST + OpenAPI |
| Auth | Session cookie via OKTA + Selenium | Bearer token from UI |
| Setup time | ~5 min (one-time login) | ~30 sec (paste token) |
| Token lifetime | ~30 min idle | ~2 months |
| Coverage | Jobs + Monitors | Streaming + Batch + SLA + 270 more endpoints |
