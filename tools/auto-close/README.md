# auto-close — close transient Databricks failures

Closes DRGN tickets created from `[ERROR] [DATABRICKS] Databricks batch job
... failed` PD alerts when the job has already recovered on its own. PD
incidents auto-resolve when the linked DRGN closes, so we only act on Jira.

There are three subcommands:

| Subcommand | Use case |
|---|---|
| `scan` | Sweep all acknowledged PD incidents, find candidates by whitelist + CDT recovery, close in bulk |
| `check` | Inspect a single PD by URL/ID; if job not whitelisted, offer to add it |
| `close` | Close one DRGN/PD with **explicit flags** (bypasses whitelist; supports `--reference DSSD-NNNN` for cross-references) |

## How `scan` and `check` decide

A candidate is closed only when **all** of these hold:

1. PD incident is `acknowledged` and matches the Databricks-failure title pattern
2. `job_name` extracted from the title matches a regex in `whitelist.json`
3. CDT `batch_dashboard.last_runs[0].status == "success"`
4. That successful run's `start_time` is **after** the PD incident's `created_at`
5. A `DRGN-NNN` is referenced in the PD notes (auto-created by Jira)

Step 4 is what makes the check safe: an old success run doesn't count.

## Resolution and field selection (auto-detect, used by `scan`/`check`)

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

CDS Alert Category is always `ETL` (64520). Default comment for `scan`/`check`
is fixed: `repaired and next run succeeded`. The `close` subcommand builds a
richer comment from `--reference`, `--append`, and the auto-detected base.

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
to append a suggested pattern. Or use `close ... --auto` directly to skip
whitelist for one-off closures.

## Commands

### `scan` — bulk closure of acknowledged incidents

```bash
# Scan all acknowledged PD incidents, interactive prompts
python auto_close.py scan

# Same scan, no prompts (cron-friendly)
python auto_close.py scan --auto

# Show plan but don't change anything
python auto_close.py scan --dry-run
```

### `check` — inspect a single incident, add to whitelist if needed

```bash
# Check a single PD by URL or ID; if job not whitelisted, offer to add
python auto_close.py check https://tmtoc.pagerduty.com/incidents/Q13DBZIJBY5Z2V
python auto_close.py check Q13DBZIJBY5Z2V --dry-run
```

### `close` — explicit single-ticket closure

Closes one DRGN ticket with explicit resolution flags. Bypasses the whitelist
check, supports cross-reference comments to existing DSSD/COREDATA tickets
(useful for chronic-issue closures where the underlying RCA is already being
tracked elsewhere).

**Target** can be a PagerDuty ID/URL (DRGN is looked up automatically from PD
notes) or a bare `DRGN-NNNN` key (no PD lookup, flags drive everything).

```bash
# Close as Auto with a DSSD cross-reference
python auto_close.py close Q1NKRGE1Y1K5EL --auto --reference DSSD-31131

# Manual repair was performed → Resolved via Standard Procedure
python auto_close.py close Q0PC40933YQ7YT --rvsp --comment "Igor manual repair"

# False Positive (paused DAG, stale alert, etc.)
python auto_close.py close DRGN-17896 --fp --comment "DAG is paused, stale email alert"

# Auto-detect resolution (Auto vs RvSP) from PD title/notes; append a custom note
python auto_close.py close Q3EUAXVKFWS1QK --auto-detect --append "Tracked in DSSD-31259"

# Multiple cross-references (repeatable flag)
python auto_close.py close DRGN-17888 --auto --reference DSSD-31259 --reference DSSD-31225

# Dry-run + skip confirmation prompt
python auto_close.py close Q1NKRGE1Y1K5EL --auto --dry-run
python auto_close.py close Q1NKRGE1Y1K5EL --auto --yes  # close without prompt
```

**`close` flag reference:**

| Flag | What it does |
|---|---|
| `--auto` | Resolved Automatically (no manual intervention) |
| `--rvsp` | Resolved via Standard Procedure (manual repair was performed) |
| `--fp` | False Positive |
| `--not-required` | Not Required |
| `--auto-detect` | Pick Auto vs RvSP from PD title/notes (requires PD input) |
| `--reference KEY` | Cross-reference DSSD/COREDATA/FCR key (repeatable) |
| `--comment TEXT` | Override the default base comment |
| `--append TEXT` | Free-form text appended at the end of the comment |
| `--sla {no,yes,unknown}` | SLA Violation field (default `no`) |
| `--runbook-url URL` | Runbook URL — sets Runbook Status to Up-to-date |
| `--dry-run` | Show plan, do not close |
| `--yes` | Skip confirmation prompt |

If `--runbook-url` is omitted but a PD ID is given, the alert's runbook URL
(if not `Missing`) is used. With a bare DRGN target, no runbook lookup is
performed and Runbook Status defaults to Missing.

## Cron / scheduled use

```cron
*/30 * * * * cd /Users/master/noc-toolkit/tools/auto-close && python auto_close.py scan --auto >> /tmp/auto-close.log 2>&1
```

The `--auto` flag (on `scan`) uses safe defaults (SLA=No, runbook auto-detected)
and exits non-zero only on hard errors. If the whitelist is conservative and
the recovery check is solid, this is safe to run unattended.

## What it does NOT do

- It does not modify the PD incident directly. Jira → PD integration handles
  the cascade resolve.
- It does not close DRGNs whose linked PD has already been resolved
  manually — those PDs aren't returned by `?statuses[]=acknowledged`.
- It does not handle escalations. If a job needs a DSSD ticket, `scan`/`check`
  skip it (job_name won't be in whitelist). For deliberately closing a chronic
  ticket that's already escalated, use `close --reference DSSD-NNNN`.

## Files

- `auto_close.py` — CLI entry point (scan, check, close)
- `pd_helpers.py` — fetch PD incidents/notes/alerts, parse titles, find DRGN
- `closure.py` — Jira transition 61 with the right field IDs (used as a library
  by other tools too)
- `whitelist.json` — config (commit changes when adding patterns)
