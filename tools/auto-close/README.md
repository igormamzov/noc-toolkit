# auto-close — close transient Databricks failures

Closes DRGN tickets created from `[ERROR] [DATABRICKS] Databricks batch job
... failed` PD alerts when the job has already recovered on its own. PD
incidents auto-resolve when the linked DRGN closes, so we only act on Jira.

## How it decides

A candidate is closed only when **all** of these hold:

1. PD incident is `acknowledged` and matches the Databricks-failure title pattern
2. `job_name` extracted from the title matches a regex in `whitelist.json`
3. CDT `batch_dashboard.last_runs[0].status == "success"`
4. That successful run's `start_time` is **after** the PD incident's `created_at`
5. A `DRGN-NNN` is referenced in the PD notes (auto-created by Jira)

Step 4 is what makes the check safe: an old success run doesn't count.

## Resolution and field selection

| Condition | Resolution |
|---|---|
| Title starts with `repaired` OR a note says `repaired` | **Resolved via Standard Procedure** (12903) |
| Otherwise | **Resolved Automatically** (12901) |

| Condition | Runbook Status |
|---|---|
| Alert has runbook URL (not `Missing`) | Up-to-date (64530) |
| Alert says `runbook: Missing` | Missing (64532) |

| Condition | SLA Violation |
|---|---|
| `job_name` matches `high_frequency_jobs` | No (64528) — silent |
| Interactive mode, other jobs | Prompt user |
| Auto mode, other jobs | No (default) |

CDS Alert Category is always `ETL` (64520). Comment is fixed:
`repaired and next run succeeded`.

## Whitelist (`whitelist.json`)

```json
{
  "patterns": ["^asra_.*$"],
  "high_frequency_jobs": ["^asra_split_trx_.*_fact$"]
}
```

- `patterns` — regex list, anchored. ANY match makes the job eligible.
- `high_frequency_jobs` — subset that also auto-defaults SLA Violation = No.

To add a new job, the easiest way is to use `check <PD>` on an active
incident — when the job is recovered but not whitelisted, the tool offers
to append a suggested pattern.

## Commands

```bash
# Scan all acknowledged PD incidents, interactive prompts
python auto_close.py scan

# Same scan, no prompts (cron-friendly)
python auto_close.py scan --auto

# Show plan but don't change anything
python auto_close.py scan --dry-run

# Check a single PD by URL or ID; if job not whitelisted, offer to add
python auto_close.py check https://tmtoc.pagerduty.com/incidents/Q13DBZIJBY5Z2V
python auto_close.py check Q13DBZIJBY5Z2V --dry-run
```

## Cron / scheduled use

```cron
*/30 * * * * cd /Users/master/noc-toolkit/tools/auto-close && python auto_close.py scan --auto >> /tmp/auto-close.log 2>&1
```

The `--auto` flag uses safe defaults (SLA=No, runbook auto-detected) and
exits non-zero only on hard errors. If the whitelist is conservative and
the recovery check is solid, this is safe to run unattended.

## What it does NOT do

- It does not modify the PD incident directly. Jira → PD integration handles
  the cascade resolve.
- It does not close DRGNs whose linked PD has already been resolved
  manually — those PDs aren't returned by `?statuses[]=acknowledged`.
- It does not handle escalations. If a job needs a DSSD ticket, the tool
  skips it (job_name won't be in whitelist).

## Files

- `auto_close.py` — CLI entry point (scan, check)
- `pd_helpers.py` — fetch PD incidents/notes/alerts, parse titles, find DRGN
- `closure.py` — Jira transition 61 with the right field IDs
- `whitelist.json` — config (commit changes when adding patterns)
