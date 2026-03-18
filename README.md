# NOC Toolkit

**Version:** 0.6.0
**A unified command-line toolkit for NOC operations**

---

## 📖 Overview

NOC Toolkit is a menu-driven command-line interface that provides easy access to various operational tools used by the NOC team. Instead of remembering multiple script locations and commands, simply launch the toolkit and select the tool you need from an interactive menu.

---

## ✨ Features

- 🎯 **Unified Interface** - Single entry point for all NOC tools
- 📋 **Interactive Menu** - Easy-to-use menu-driven navigation
- 🔧 **Multiple Tools** - Currently includes:
  - **PD Sync** - Sync PagerDuty incidents with Jira
  - **PD Jobs** - Extract job names from merged PagerDuty incidents
  - **PD Monitor** - Monitor and auto-acknowledge triggered incidents
  - **PD Merge** - Find and merge related incidents by job name
  - **Freshness** - DACSCAN data freshness report via Databricks SQL
  - **Shift Report** - Sync Jira statuses into shift report (Google Sheets / Excel)
  - **PD Escalate** - Automate post-DSSD escalation workflow (link DRGN→DSSD, transition, PD note, Slack template)
  - **PD Resolve** - Auto-resolve PD incidents where Airflow DAG runs recovered
- 🚀 **Extensible** - Easy to add new tools
- ✅ **Health Checks** - Automatically verifies tool availability

---

## 🚀 Quick Start

### Prerequisites

- Python 3.7 or higher
- pip (Python package manager)

### Installation

1. **Clone or navigate to the toolkit directory:**
   ```bash
   cd /Users/master/noc-toolkit
   ```

2. **Install dependencies:**
   ```bash
   pip3 install -r requirements.txt
   ```

3. **Configure environment variables:**

   Each tool may require its own configuration. See the tool-specific documentation for details.

   Example for PD Sync:
   ```bash
   cd tools/pd-sync
   cp .env.example .env
   # Edit .env with your credentials
   ```

### Usage

Simply run the toolkit:

```bash
python3 noc-toolkit.py
```

Or make it executable and run directly:

```bash
chmod +x noc-toolkit.py
./noc-toolkit.py
```

---

## 🎮 Using the Toolkit

When you launch the toolkit, you'll see an interactive menu:

```
╔════════════════════════════════════════════════════════╗
║                                                        ║
║              NOC Toolkit v0.6.0║
║                                                        ║
║         Unified NOC Operations Toolkit                 ║
║                                                        ║
╚════════════════════════════════════════════════════════╝

========================================================
Available Tools:
========================================================
  1. [✓] PD Sync
      Sync PagerDuty incidents with Jira issues

  2. [✓] PD Jobs
      Extract and analyze PagerDuty on-call schedules

  3. [✓] PD Monitor
      Monitor and auto-acknowledge triggered incidents

  4. [✓] PD Merge
      Find and merge related PagerDuty incidents by job name

  5. [✓] Freshness
      DACSCAN data freshness report with granular table checks

  6. [✓] Shift Report
      Sync Jira statuses into shift report (Google Sheets / Excel)

  7. [✓] PD Escalate
      Link DRGN→DSSD, transition to Escalated, post PD note

  8. [✓] PD Resolve
      Auto-resolve PD incidents where Airflow jobs recovered

--------------------------------------------------------
  0. Exit
========================================================

Select tool [0-8]:
```

### Menu Navigation

- Enter the **number** of the tool you want to run (e.g., `1` for PD Sync)
- Enter **0** to exit the toolkit
- Press **Ctrl+C** at any time to interrupt and return to the menu

### Tool Status Indicators

- **[✓]** - Tool is available and ready to use
- **[✗]** - Tool script not found (check configuration)

---

## 🔧 Available Tools

### 1. PD Sync

**Purpose:** Synchronizes PagerDuty incidents with Jira issues

**Features:**
- Fetches PagerDuty incidents based on filters
- Auto-discovers Jira tickets from incident titles and comments
- Tracks Jira statuses and posts status-update comments
- Auto-snooze mode with configurable duration
- Auto-detects "ignore"/"disabled" keywords — posts short comment and snoozes
- 12-hour duplicate comment guard to prevent spam
- Progress bar with time estimation

**Configuration:** Uses shared `.env` from toolkit root (PAGERDUTY_API_TOKEN + Jira credentials)

**Quick setup:**
```bash
cd tools/pd-sync
cp .env.example .env
# Edit .env with your PagerDuty and Jira credentials
```

---

### 2. PD Jobs

**Purpose:** Extracts and analyzes PagerDuty on-call schedules

**Features:**
- Fetches PagerDuty schedules
- Extracts on-call rotation data
- Generates reports on job assignments
- Exports data in various formats

**Configuration:** See [tools/pd-jobs/README.md](tools/pd-jobs/README.md)

**Quick setup:**
```bash
cd tools/pd-jobs
cp .env.example .env
# Edit .env with your PagerDuty API token
```

---

### 3. PD Monitor

**Purpose:** Automatically acknowledges triggered incidents and posts human-like comments

**Features:**
- Auto-acknowledges triggered incidents assigned to current user
- Randomized comment phrases (13 normal + 10 typo variants) to look like a real engineer
- 20% typo probability, 50% lowercase probability for natural variation
- Silent acknowledge (no comment) for "Missing" load-status incidents (AUS & NZL, MSP Export, CANADA, Central, East, International, UK)
- **Background mode** — run monitoring while using other tools, view output on demand, auto-stop on exit
- Detects prior auto-comments to avoid duplicates
- Continuous monitoring with configurable duration and check interval
- Dry-run mode for safe testing

**Configuration:** Uses shared `.env` from toolkit root (PAGERDUTY_API_TOKEN)

**Quick setup:**
```bash
cd tools/pd-monitor
# API token shared from noc-toolkit .env
python3 pd_monitor.py --dry-run --verbose  # Test first
```

**Cron setup (recommended):**
```bash
crontab -e
# Add: */10 * * * * cd /Users/master/pd-monitor && python3 pd_monitor.py >> /tmp/pd-monitor.log 2>&1
```

---

### 4. PD Merge

**Purpose:** Find and merge related PagerDuty incidents that share the same root cause (same job/DAG name)

**Features:**
- Automatic grouping of incidents by normalized job name
- Four merge scenarios: same-day (A), cross-date with Jira validation (B), mass failure consolidation (C), RDS exports "failed to start" (D)
- Deterministic target selection: real comments > alert priority (Databricks > Monitor > AirFlow) > earliest created
- Interactive per-group and per-incident confirmation before merging
- Skip persistence — skipped incidents remembered across runs, interactive clear at startup
- Detailed merge table with PD links, incident titles, and dd:hh:mm age
- Dry-run mode for safe preview

**Configuration:** Uses shared `.env` from toolkit root (PAGERDUTY_API_TOKEN + optional Jira credentials for Scenario B)

**Quick setup:**
```bash
python3 tools/pd-merge/pd_merge.py --dry-run    # Preview
python3 tools/pd-merge/pd_merge.py               # Live run
```

**CLI options:**
- `--dry-run, -n` — Simulate merges without API changes
- `--verbose, -v` — Show extra debug output
- `--clear-skips` — Clear the saved skip list
- `--show-skips` — Show currently skipped incidents

---

### 5. Freshness

**Purpose:** Automate the daily DACSCAN Data Freshness Report by querying Databricks SQL

**Features:**
- Main 15-row freshness report (DACSCAN, AGG, AUDIT, SUMMARY, BI-LOADER tables)
- Automatic granular checks for delayed tables (host-level for DACSCAN, max(update_ts) for aggregates)
- SALES_ORD_EVENT_OPT known issue handled (DSSD-29069 — fallback to update_ts)
- SLA countdown (5:30 PM UTC deadline)
- HTML report with color-coded status — open in browser for Slack screenshots
- Connects via Databricks SQL Statement Execution REST API (no heavy SDK)

**Configuration:** Uses `.env` from toolkit root (DATABRICKS_HOST, DATABRICKS_TOKEN, DATABRICKS_WAREHOUSE_ID)

**Quick setup:**
```bash
python3 tools/freshness/freshness.py --dry-run    # Preview SQL
python3 tools/freshness/freshness.py               # Run report
python3 tools/freshness/freshness.py --report      # Run + HTML report
```

**CLI options:**
- `--report, -r` — Generate HTML report and open in browser
- `--check-all` — Run granular checks for ALL tables (not just delayed)
- `--dry-run, -n` — Show SQL queries without executing
- `--verbose, -v` — Show API call details
- `--format csv/json` — Alternative output formats

---

### 6. Shift Report

**Purpose:** Automate shift handoff, sync Jira statuses, and add ticket rows to the shift report

**Modes:**
- **Online mode (Google Sheets)** [recommended] — reads/writes the shift report directly in Google Sheets via Apps Script Web App. No file downloads needed.
- **Local mode (Excel)** — works with a downloaded `.xlsx` file (legacy)

When Google Sheets is configured (`GSHEET_WEBAPP_URL` + `GSHEET_API_KEY`), the toolkit shows a sub-menu to choose Online or Local mode. If not configured, it shows setup instructions.

**Features:**
- **Start shift** — copy all tickets from previous shift, update date, sync Jira statuses
- **End shift (SYNC)** — update Jira statuses (column E) for all existing tickets
- **Add row** — insert a new ticket to "Things to monitor" section with Jira + Slack links
- Auto-detects section boundaries ("from previous shifts", "Things to monitor", "Permalinks")
- Handles insert/delete rows when ticket count differs between shifts
- Month boundary handling (e.g. Mar 31 → Apr 1)
- Auto-detects Jira and Slack links in any paste order
- Preserves all Excel formatting, merges, and hyperlinks (Local mode)
- Works with both Night-Shift-NEW and Day-Shift-NEW sheets

**Configuration:** Uses `.env` from toolkit root:
- `JIRA_SERVER_URL`, `JIRA_PERSONAL_ACCESS_TOKEN` (required for both modes)
- `GSHEET_WEBAPP_URL`, `GSHEET_API_KEY` (required for Online mode — request from toolkit maintainer)

**CLI options (Local mode):**
- `--dry-run, -n` — Show changes without saving
- `--verbose, -v` — Show API call details
- `--file PATH` — Custom Excel file path (default: `~/Downloads/NOC endshift report.xlsx`)

**CLI options (Online mode):**
- `--dry-run, -n` — Show changes without saving
- `--verbose, -v` — Show API call details

---

### 7. PD Escalate

**Purpose:** Automate the post-DSSD escalation workflow — link DRGN→DSSD in Jira, transition DRGN to Escalated, post PD note, print Slack template

**Features:**
- Auto-detects DRGN ticket via PD Jira integration field (`external_references`)
- Fallback: scans PD incident notes for DRGN-\d+ pattern
- When DRGN is not found: shows PD URL with instruction to press "Create Jira Issue" button
- Creates Jira link: DRGN "is blocked by" DSSD
- Transitions DRGN to "Escalated" status
- Posts PD note with escalation summary and Jira URL
- Prints ready-to-paste Slack template for #cds-ops-24x7-int
- Dry-run mode for safe testing

**Configuration:** Uses shared `.env` from toolkit root (PAGERDUTY_API_TOKEN + Jira credentials)

**Quick setup:**
```bash
python3 tools/pd-escalate/pd_escalate.py --pd Q33L5GALLQ3ESB --dssd DSSD-29386 --dry-run    # Preview
python3 tools/pd-escalate/pd_escalate.py --pd Q33L5GALLQ3ESB --dssd DSSD-29386               # Live run
```

**CLI options:**
- `--pd` — PagerDuty incident ID or URL (required)
- `--dssd` — DSSD ticket key, e.g. DSSD-29386 (required)
- `--drgn` — DRGN ticket key (optional, auto-detected)
- `--dry-run, -n` — Simulate without API mutations

---

### 8. PD Resolve

**Purpose:** Auto-resolve PagerDuty incidents where Airflow DAG runs have recovered (subsequent runs succeeded)

**Features:**
- Extracts DAG name from PD incident title
- Checks Airflow REST API (via AWS MWAA) for recent successful runs
- Finds DRGN ticket from PD notes
- Searches Confluence DS space for runbook
- Interactive prompts for SLA violation and comment
- Closes DRGN ticket with proper transition fields
- Resolves PD incident with summary note

**Configuration:** Uses shared `.env` from toolkit root (PAGERDUTY_API_TOKEN + Jira credentials) plus optional AWS/MWAA settings:
- `AWS_PROFILE` — AWS profile with MWAA access
- `MWAA_ENVIRONMENT_NAME` — Airflow environment name
- `MWAA_REGION` — AWS region

**Quick setup:**
```bash
python3 tools/pd-resolve/pd_resolve.py --dry-run    # Preview
python3 tools/pd-resolve/pd_resolve.py               # Live run
```

**CLI options:**
- `--dry-run, -n` — Simulate without API mutations
- `--verbose, -v` — Show extra debug output

---

## 🔐 Security

### Important Security Notes

- **Never commit `.env` files** - They contain sensitive credentials
- **Keep API tokens secure** - Store them only in environment variables or `.env` files
- **Use read-only tokens when possible** - Limit token permissions to minimum required
- **Restrict access** - Only authorized personnel should have access to the toolkit

### File Permissions

Ensure configuration files have appropriate permissions:

```bash
chmod 600 tools/*/.env  # Read/write for owner only
```

---

## 🛠️ Troubleshooting

### Common Issues

#### "Module not found" error

**Problem:** Python dependencies not installed

**Solution:**
```bash
pip3 install -r requirements.txt
```

#### "Permission denied" when running toolkit

**Problem:** Script not executable

**Solution:**
```bash
chmod +x noc-toolkit.py
```

#### Tool shows [✗] in menu

**Problem:** Tool script not found or symlink broken

**Solution:**
```bash
# Verify tools directory
ls -la tools/

# Check tool scripts exist
ls tools/*/
```

#### API authentication errors

**Problem:** Invalid or missing credentials

**Solution:**
1. Verify `.env` file exists in tool directory
2. Check that API tokens are valid and not expired
3. Ensure tokens have necessary permissions

---

## 📚 Documentation

- **[PROJECT_DOCS.md](docs/PROJECT_DOCS.md)** - Complete architecture and technical documentation
- **[PLAN.md](docs/PLAN.md)** - Development plan and progress tracking
- **Tool-specific docs** - See individual tool directories

---

## 🤝 Adding New Tools

Want to add a new tool to the toolkit?

1. **Add your tool to the `tools/` directory:**
   ```bash
   cp -r /path/to/your/tool tools/my-new-tool
   # Or create a symlink:
   ln -s /path/to/your/tool tools/my-new-tool
   ```

2. **Edit `noc-toolkit.py`** and add your tool to the `_load_tools()` method:
   ```python
   ToolDefinition(
       tool_id="my-new-tool",
       name="My New Tool",
       description="Description of what it does",
       script_path="tools/my-new-tool/main.py",
       enabled=True
   ),
   ```

3. **Update dependencies** if your tool requires additional packages:
   ```bash
   echo "your-package>=1.0.0" >> requirements.txt
   ```

4. **Test it:**
   ```bash
   python3 noc-toolkit.py
   ```

---

## 📊 Project Structure

```
noc-toolkit/
├── noc-toolkit.py              # Main entry point
├── tools/                      # All tools
│   ├── pd-sync/          # PD-Jira sync
│   ├── pd-jobs/  # Extract job names from PD
│   ├── pd-monitor/            # Auto-acknowledge monitor
│   ├── pd-merge/              # Incident merge tool
│   ├── freshness/        # Data freshness report
│   ├── shift-report/  # Shift report (Google Sheets / Excel)
│   ├── pd-escalate/           # Post-DSSD escalation workflow
│   └── pd-resolve/           # Auto-resolve recovered incidents
├── config/                     # Configuration files
├── docs/                       # Documentation
│   ├── PROJECT_DOCS.md        # Architecture docs
│   └── PLAN.md                # Development plan
├── requirements.txt            # Python dependencies
└── README.md                   # This file
```

---

## 🔄 Version History

### pd-resolve v0.1.2 (2026-03-17)

- ✅ **Bug fix:** Recovery check now requires last 2 consecutive successes instead of all 15

### gsheet_report v0.1.1 (2026-03-17)

- ✅ **Fix:** Merge A:B for "from previous shifts" section with vertical align middle
- ✅ **Fix:** Text wrap on ticket data (C:F) in startShift and addRow

### gsheet_report v0.1.0 (2026-03-16)

- ✅ **New:** Google Sheets adapter for Shift Report via Apps Script Web App
- ✅ Three operations: sync statuses, add row, start shift — same as Excel mode
- ✅ Sub-menu in toolkit: Online (Google Sheets) / Local (Excel) mode selection
- ✅ API key authentication for Apps Script endpoint
- ✅ 57 unit tests added (pytest)

### shift-report v0.1.6 (2026-03-13)

- ✅ **Bug fix:** TTM row gets A:F merge instead of A:B after start_shift
- ✅ **Bug fix:** overlapping merge cells corrupt XLSX (v0.1.6)

### pd-resolve v0.1.1 (2026-03-16)

- ✅ **Bug fix:** Interactive prompt when launched from toolkit menu (no args)
- ✅ **Bug fix:** Auto-detect AWS profile from `~/.aws/credentials` for MWAA access

### pd-resolve v0.1.0 (2026-03-16)

- ✅ **New tool:** Auto-resolve PD incidents where Airflow DAG runs recovered
- ✅ Airflow REST API integration via AWS MWAA web login token
- ✅ DRGN Close transition with proper field IDs
- ✅ Confluence runbook search via DS space
- ✅ Interactive SLA violation and comment prompts
- ✅ 87 unit tests added (pytest)
- ✅ Registered as tool #8 in noc-toolkit menu

### pd-monitor v0.1.4 (2026-03-12)

- ✅ **Bug fix:** `processed_incidents` set was never cleared between check cycles — after PagerDuty auto-un-acknowledges (~30 min), re-triggered incidents were permanently skipped as "already processed"
- ✅ Cached user email at init — eliminates redundant `GET /users/{id}` call on every acknowledge
- ✅ Removed `sys.exit(1)` from `_get_current_user_id()` — raises `RuntimeError` instead
- ✅ 97 unit tests added (pytest)

### pd-sync v0.3.2 (2026-03-12)

- ✅ Extracted `_parse_iso_dt()` and `_is_assigned_to_user()` helpers — deduplicates ISO parsing and user-filter logic
- ✅ Removed `sys.exit(1)` from `check_incidents()` and `process_and_update_incidents()` — exceptions propagate to caller
- ✅ 96 unit tests added (pytest)

### pd-jobs v0.1.1 (2026-03-12)

- ✅ Removed `sys.exit(1)` from business logic — exceptions propagate to caller
- ✅ Fixed `any` → `Any` type hint, removed intermediate `list()` calls on iterators
- ✅ 39 unit tests added (pytest)

### pd-merge v0.2.4 (2026-03-12)

- ✅ Extracted `_parse_iso_dt()` helper — deduplicates 7 repeated ISO datetime parsing patterns
- ✅ 70 unit tests added (pytest)

### freshness v0.1.1 (2026-03-12)

- ✅ Extracted `_is_fresh_date()` helper — deduplicates 5 repeated freshness date checks
- ✅ Fixed `timedelta` import (was inside function body)
- ✅ 48 unit tests added (pytest)

### pd-escalate v0.1.1 (2026-03-11)

- ✅ Refactor: moved `JIRA_BASE_URL` from global to instance attribute, removed `PD_BASE_URL` global
- ✅ Replaced `sys.exit(1)` in `run()` with `RuntimeError` for cleaner error handling
- ✅ 37 unit tests added (pytest)

### Version 0.6.0 (2026-03-07)

- ✅ Integrated PD Escalate (pd-escalate v0.1.0)
- ✅ Automate post-DSSD escalation: link DRGN→DSSD, transition to Escalated, PD note, Slack template
- ✅ Auto-detect DRGN via PD Jira integration field (`external_references`)
- ✅ Registered as tool #7 in noc-toolkit menu

### shift-report v0.1.5 (2026-03-11)

- ✅ Refactor: unified layout scanning via `_scan_layout()` in all methods, removed `STOP_MARKERS`
- ✅ Single file I/O pass in `start_shift()` (was double load/save), reduced `_scan_layout()` calls from 6 to 2
- ✅ Extracted sub-methods: `_restructure_from_prev()`, `_reset_ttm()`, `_repair_permalinks()`
- ✅ 46 unit tests added (pytest)

### shift-report v0.1.4 (2026-03-11)

- ✅ Fix: handle missing "from the previous shifts" section header (fallback to row 8)

### shift-report v0.1.3 (2026-03-09)

- ✅ Shift handoff automation: "Start shift" copies tickets from previous shift, updates date, syncs
- ✅ Menu reordered: 1=Start shift, 2=End shift (SYNC), 3=Add row
- ✅ Month boundary handling (e.g. Mar 31 → Apr 1)

### shift-report v0.1.2 (2026-03-09)

- ✅ Bug fix: sync now processes tickets inside "Things to monitor" section (previously skipped)

### shift-report v0.1.1 (2026-03-07)

- ✅ Fixed hyperlink color: explicit Jira-blue (#0052CC) with underline for ticket and Slack links

### Version 0.5.0 (2026-03-03)

- ✅ Integrated Shift Report (shift-report v0.1.0)
- ✅ Sync Jira statuses for existing tickets in End-of-Shift Excel report
- ✅ Add new ticket rows to "Things to monitor" section with Jira + Slack links
- ✅ Robust openpyxl handling for merges, hyperlinks, and cell formatting

### Version 0.4.0 (2026-02-27)

- ✅ Integrated Freshness (freshness v0.1.0)
- ✅ Automated DACSCAN 15-table report via Databricks SQL REST API
- ✅ Granular table-level checks with host-count and update_ts verification
- ✅ HTML report generation with color-coded rows for Slack posting

### Version 0.3.0 (2026-02-26)

- ✅ Integrated PD Merge tool (pd-merge v0.2.0)
- ✅ Three merge scenarios: same-day, cross-date with Jira, mass failure consolidation
- ✅ Interactive per-incident selection and skip persistence

### Version 0.2.0 (2026-02-25)

- ✅ PyInstaller EXE fixes — tools now work inside compiled binary
- ✅ Diagnostic debug log on every launch

### Version 0.1.0 (2026-02-22)

**Initial Release**

- ✅ Menu-driven interface
- ✅ Integrated PD Sync, Job Extractor, PD Monitor
- ✅ Centralized configuration via shared `.env`
- ✅ Tool health checks

---

## 📝 License

Internal tool for organizational use only.

---

## 💬 Support

For questions or issues:

1. Check the troubleshooting section above
2. Consult tool-specific documentation
3. Review [PROJECT_DOCS.md](docs/PROJECT_DOCS.md) for technical details
4. Contact the NOC team

---

## 🎯 Future Enhancements

Planned features for future versions:

- 🎨 Colored terminal output
- 📊 Built-in logging system
- ⚙️ Configuration wizard
- 🔍 Tool search functionality
- 📈 Usage statistics
- 🔔 Notification integrations

See [PLAN.md](docs/PLAN.md) for the complete roadmap.

---

**Made with ❤️ for the NOC team**
