# pd-create-drgn — programmatically create DRGN ticket from PD incident

Mimics the PD UI's **More → Create Jira Issue** button by POSTing to the
same internal endpoint the browser uses. Removes the manual click step so
batching / chained automation (`pd-escalate` etc.) is possible.

## Why a separate tool

The PagerDuty Jira-Server integration (PD↔DRGN at Live Nation) has a UI
button but **no documented public REST endpoint**. The `Create Jira Issue`
modal POSTs to:

```
https://app.pagerduty.com/integration-jira-service/create_issue_from_pagerduty
```

— which expects:

- `Authorization: Bearer <PD UI session token>` (the `pdus+_...` cookie/header
  Chrome sends; **NOT** a classic REST API key — REST keys return 401)
- the full PD incident object plus a synthesized Jira `issue.fields` block

Captured via DevTools on 2026-05-31 from a successful manual run.

## Usage

```bash
# Basic — auto-create DRGN for a PD incident
python pd_create_drgn.py --pd Q1WPEMZKLQZGJF

# URL form
python pd_create_drgn.py --pd https://tmtoc.pagerduty.com/incidents/Q1WPEMZKLQZGJF

# Inspect the request body without POSTing
python pd_create_drgn.py --pd Q1WPEMZKLQZGJF --dry-run

# Machine-readable output
python pd_create_drgn.py --pd Q1WPEMZKLQZGJF --json
```

Output forms:

```
Created DRGN-17946: https://jira.livenation.com/browse/DRGN-17946
```

```
DRGN-17945 already exists for incident Q0V16YZTZKJEMX: https://jira.livenation.com/browse/DRGN-17945
```

When the endpoint reports "already exists" but `external_references` is
empty, the tool falls back to scanning PD notes for the Jira-automation
comment (`Jira issue: https://jira.livenation.com/browse/DRGN-NNNNN`) so
the existing DRGN key is still recovered.

## Environment

Required:

- `PAGERDUTY_API_TOKEN` — classic PD REST API key (used to read the
  incident and notes via `Token token=...`)
- `PD_UI_BEARER_TOKEN` — UI session bearer (`pdus+_...`); ~1-week TTL,
  must be refreshed manually from Chrome:
  - Open any page on `app.pagerduty.com`
  - DevTools → Network → click any XHR request → Headers
  - Copy the `Authorization: Bearer pdus+_...` value
  - Paste into `.env` as `PD_UI_BEARER_TOKEN=pdus+_...`

The tool prints a clear error with a link to refresh instructions when
the bearer is invalid or expired.

## DRGN-specific defaults (overridable)

Captured from the Live Nation PD↔DRGN mapping:

- `--accounts-mapping-id PVP6DLT` (DRGN account mapping)
- `--project-key DRGN`
- `--issuetype-id 21701` (= "Alert" in DRGN)

Override these flags only if Live Nation reorganizes the integration.

## Integration with pd-escalate

`pd-escalate` (v0.3.0+) imports this module and auto-creates a DRGN when
none is linked to the PD incident. Disable with `--no-auto-create-drgn`.

## Files

- `pd_create_drgn.py` — CLI + library functions (`create_drgn_for_incident`)
- `__init__.py` — empty, for package imports
