# NOC Toolkit — Quick Guide

**Version:** 0.6.0 | **Launch:** `python3 noc-toolkit.py`

---

## 1. PagerDuty-Jira Tool

Sync PagerDuty incidents with Jira tickets.

- Fetches your PD incidents and auto-discovers linked Jira tickets (from title + comments)
- Posts Jira status updates as PD comments
- `--snooze` mode: auto-snooze after update (configurable timer)
- Detects "ignore" / "disabled" keywords → posts short comment + snooze
- 12-hour duplicate comment guard

**Modes:** `--update` (status only) | `--snooze` (status + snooze)

---

## 2. PagerDuty Job Extractor

Extract failed job names from merged PD incidents.

- Accepts incident URL or ID
- Parses merged alerts and extracts `jb_*` job names
- Outputs a clean list for further triage

---

## 3. PagerDuty Monitor

Auto-acknowledge triggered incidents.

- Acknowledges all triggered incidents assigned to you
- Posts a randomized human-like comment (13 phrases + 10 typo variants)
- **Silent ack** (no comment) for "Missing" load-status incidents:
  - Missing AUS & NZL, MSP Export, CANADA, Central, East, International, UK
- **Background mode** — monitoring runs while you use other tools:
  - Select PD Monitor → "Background" → pick duration → done
  - Banner shows status: `▶ PD Monitor: ACTIVE 12m/60m | 5 new`
  - Select PD Monitor again → "View output" or "Stop"
  - Auto-stops on toolkit exit
- Interactive duration menu (1h / 2h / 4h / 8h / 12h / custom)
- `--once` for single check, `--details` for verbose countdown

---

## 4. PagerDuty Incident Merge

Find and merge related PD incidents by job name.

- **Scenario A** — Same-day duplicates: same job name, same date → merge
- **Scenario B** — Cross-date with Jira: same job, different dates, Jira ticket exists → merge
- **Scenario C** — Mass failure: DSSD incident with 10+ merged alerts → merge related jobs into it
- **Scenario D** — RDS Exports: merge individual `RDS export <job> failed` into `RDS exports - failed to start`
- Interactive per-group and per-incident confirmation
- Skip list remembered across runs (interactive clear at startup)

**Flags:** `--dry-run` | `--verbose` | `--show-skips`

---

## 5. Data Freshness Checker

Daily DACSCAN freshness report via Databricks SQL.

- 15-row main report (DACSCAN, AGG, AUDIT, SUMMARY, BI-LOADER tables)
- Auto granular checks for delayed tables (host-level for DACSCAN, max(update_ts) for aggregates)
- SLA countdown (deadline 5:30 PM UTC)
- HTML report with color-coded rows — open in browser for Slack screenshots

**Flags:** `--report` (HTML) | `--check-all` | `--dry-run` | `--format csv/json`

---

## 6. NOC Report Assistant

Sync Jira statuses into End-of-Shift Excel report.

- **Sync statuses** — updates Jira status (column E) for all existing tickets
- **Add row** — inserts new ticket to "Things to monitor" with Jira + Slack links
- Works with Night-Shift-NEW and Day-Shift-NEW sheets
- Preserves all Excel formatting, merges, hyperlinks

**Flags:** `--dry-run` | `--file PATH` (default: `~/Downloads/NOC endshift report.xlsx`)

---

## 7. PD Escalation Tool

Automate post-DSSD escalation workflow.

- Auto-detects DRGN ticket from PD incident (via Jira integration field)
- Creates Jira link: DRGN "is blocked by" DSSD
- Transitions DRGN to "Escalated" status
- Posts PD note with escalation summary
- Prints ready-to-paste Slack template for #cds-ops-24x7-int

**Usage:** `--pd <incident_id> --dssd DSSD-XXXXX [--drgn DRGN-XXXXX] [--dry-run]`

---

## Environment Variables

All tools share a single `.env` file in the toolkit root:

| Variable | Used by | Description |
|----------|---------|-------------|
| `PAGERDUTY_API_TOKEN` | 1, 2, 3, 4, 7 | PagerDuty API token (write access) |
| `JIRA_SERVER_URL` | 1, 4, 6, 7 | Jira server URL |
| `JIRA_PERSONAL_ACCESS_TOKEN` | 1, 4, 6, 7 | Jira PAT (Bearer auth) |
| `DATABRICKS_HOST` | 5 | Databricks workspace URL |
| `DATABRICKS_TOKEN` | 5 | Databricks access token |
| `DATABRICKS_WAREHOUSE_ID` | 5 | SQL warehouse ID |

---

## Tips

- All tools support `--dry-run` for safe preview
- Launch the toolkit and press the tool number — no CLI args needed
- Press **Ctrl+C** to interrupt any tool and return to menu
- `.env` file is loaded automatically on startup
