# NOC Skills for Claude Code — Setup Guide

## What's Inside

| File | Purpose |
|------|---------|
| `noc-engineer.md` | PagerDuty incidents, CDT/Airflow diagnostics, Jira escalation (DRGN/DSSD/COREDATA), Spark UI analysis |
| `noc-analytics.md` | DACSCAN data freshness report, Databricks table-level checks, SLA monitoring |
| `pd-merge-logic.md` | PagerDuty incident merge logic: grouping by job name, 4 scenarios (A/B/C/D), target selection rules, API reference |

## Quick Setup (2 minutes)

### 1. Copy skills to Claude's skill directory

```bash
mkdir -p ~/.claude/skills
cp skills/noc-engineer.md ~/.claude/skills/
cp skills/noc-analytics.md ~/.claude/skills/
cp skills/pd-merge-logic.md ~/.claude/skills/
```

### 2. Create `.env` file in the project root

```bash
cp .env.example .env
# Edit .env and fill in:
#   PAGERDUTY_API_TOKEN=your-token
#   JIRA_EMAIL=your.email@yourcompany.com
#   JIRA_API_TOKEN=your-jira-token
#   JIRA_BASE_URL=https://jira.yourcompany.com
```

### 3. (Optional) Connect Databricks MCP servers

For data freshness reports, add Databricks SQL MCP servers to Claude Code:
- `databricks-sql_analytics` — for production freshness queries
- `databricks-sql_nonprod` — backup

## How to Use

Just talk to Claude naturally. The skills activate automatically based on keywords:

| You say | Skill activates |
|---------|----------------|
| *paste PagerDuty incident link* | `noc-engineer` — fetches incident, diagnoses, suggests actions |
| "DAG delayed", "batch job stuck" | `noc-engineer` — runs Airflow/CDT/Spark diagnostic workflow |
| "create DSSD ticket", "escalate" | `noc-engineer` — generates portal-ready text with correct fields |
| "merge incidents" | `noc-engineer` + `pd-merge-logic` — merges PD incidents via API using grouping/priority rules |
| "data freshness", "DACSCAN" | `noc-analytics` — runs freshness SQL, checks all 15 tables |
| "run freshness report" | `noc-analytics` — full report + granular checks for delayed tables |

### Example Session

```
You: https://yourcompany.pagerduty.com/incidents/Q0JPEHL0SSIFFV

Claude: [fetches incident details, identifies DAG delay, checks Airflow,
         CDT Dashboard, Delta History, Spark UI, suggests merge/escalation]

You: escalate to DSSD

Claude: [generates portal-ready text with Summary, Error message,
         Performed steps (single-line, <255 chars), Runbook link]
```

## Key Things to Know

- **DSSD Portal:** https://jira.yourcompany.com/servicedesk/customer/portal/324/group/1476
- **"Performed steps"** field is single-line, max 255 chars — use `|` as separator
- **DRGN tickets** — create via PagerDuty button, then link "is blocked by" DSSD
- **PagerDuty notes** — after merging incidents, add notes to the **target** (surviving) incident
- **Data Freshness SLA** — 2:30 PM ART / 5:30 PM UTC / 9:30 AM PST
