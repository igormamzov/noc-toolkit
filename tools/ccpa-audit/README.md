# ccpa-audit — CCPA ERASE progress block

Renders a multi-day progress summary for `privacy_request.ERASE` requests
in the CCPA Audit dashboard, in the exact format the on-call wants to paste
into a PD note + Slack reminder:

```
May 31, 2026    57% (4 of 7)
May 30, 2026    56% (9 of 16)
May 29, 2026    93% (40 of 43)
```

Equivalent UI view:
[CDT CCPA Audit dashboard](https://controlpanel-ui.prd2971.prod9.us-east-1.tktm.io/tt/ccpa/audit?status=New%2CCompleted%2CProcessing%2CFailed%20to%20send&request_type=privacy_request.ERASE)

Runbook reference:
[CCPA Runbook](https://confluence.livenation.com/spaces/DS/pages/346433860/CCPA+Runbook)

## What it does

1. `GET {CDT_BASE_URL}/ccpa_audit/{env}/ccpa_request_summary` — pulls daily
   counts for every CCPA request type and date the API has on hand.
2. Filters down to `privacy_request.ERASE` and the target window.
3. **Window logic**: starts with `--days` (default 3) ending at "today";
   auto-extends one day at a time (up to `--max-days`, default 14) as long
   as the *oldest* day in the window is still < 100% complete. This way a
   slow-draining backlog stays visible in the comment instead of getting
   silently truncated off the top.
4. Prints the formatted block to stdout. With `--pd`, also posts it as a
   PD incident note (same author/From-header convention as `pd-escalate`
   and `auto-close`).

## Usage

```bash
# Default — 3-day block to stdout, copy/paste into PD/Slack manually
python ccpa_audit.py

# Wider initial window (auto-extension still applies)
python ccpa_audit.py --days 7

# Auto-post as PD note
python ccpa_audit.py --pd Q1WPEMZKLQZGJF

# Preview the PD note without posting
python ccpa_audit.py --pd Q1WPEMZKLQZGJF --dry-run

# Pin 'today' for repro / backfill
python ccpa_audit.py --today 2026-05-31
```

## Environment

Required:

- `CDT_API_TOKEN` — bearer token from CDT UI → Platform → Auth Tokens
- `CDT_BASE_URL` — defaults to `https://controlpanel.prd2971.prod9.us-east-1.tktm.io`

Required *only* when `--pd` is used:

- `PAGERDUTY_API_TOKEN`

## Files

- `ccpa_audit.py` — CLI
