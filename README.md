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
  - **PagerDuty-Jira Tool** - Sync PagerDuty incidents with Jira
  - **PagerDuty Job Extractor** - Extract job names from merged PagerDuty incidents
  - **PagerDuty Monitor** - Monitor and auto-acknowledge triggered incidents
  - **PagerDuty Incident Merge** - Find and merge related incidents by job name
  - **Data Freshness Checker** - DACSCAN data freshness report via Databricks SQL
  - **NOC Report Assistant** - Sync Jira statuses into End-of-Shift Excel report
  - **PD Escalation Tool** - Automate post-DSSD escalation workflow (link DRGN→DSSD, transition, PD note, Slack template)
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

   Example for PagerDuty-Jira Tool:
   ```bash
   cd tools/pd-jira-tool
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
  1. [✓] PagerDuty-Jira Tool
      Sync PagerDuty incidents with Jira issues

  2. [✓] PagerDuty Job Extractor
      Extract and analyze PagerDuty on-call schedules

  3. [✓] PagerDuty Monitor
      Monitor and auto-acknowledge triggered incidents

  4. [✓] PagerDuty Incident Merge
      Find and merge related PagerDuty incidents by job name

  5. [✓] Data Freshness Checker
      DACSCAN data freshness report with granular table checks

  6. [✓] NOC Report Assistant
      Sync Jira statuses into End-of-Shift Excel report

  7. [✓] PD Escalation Tool
      Link DRGN→DSSD, transition to Escalated, post PD note

--------------------------------------------------------
  0. Exit
========================================================

Select tool [0-7]:
```

### Menu Navigation

- Enter the **number** of the tool you want to run (e.g., `1` for PagerDuty-Jira Tool)
- Enter **0** to exit the toolkit
- Press **Ctrl+C** at any time to interrupt and return to the menu

### Tool Status Indicators

- **[✓]** - Tool is available and ready to use
- **[✗]** - Tool script not found (check configuration)

---

## 🔧 Available Tools

### 1. PagerDuty-Jira Tool

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
cd tools/pd-jira-tool
cp .env.example .env
# Edit .env with your PagerDuty and Jira credentials
```

---

### 2. PagerDuty Job Extractor

**Purpose:** Extracts and analyzes PagerDuty on-call schedules

**Features:**
- Fetches PagerDuty schedules
- Extracts on-call rotation data
- Generates reports on job assignments
- Exports data in various formats

**Configuration:** See [tools/pagerduty-job-extractor/README.md](tools/pagerduty-job-extractor/README.md)

**Quick setup:**
```bash
cd tools/pagerduty-job-extractor
cp .env.example .env
# Edit .env with your PagerDuty API token
```

---

### 3. PagerDuty Monitor

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

### 4. PagerDuty Incident Merge

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

### 5. Data Freshness Checker

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
python3 tools/data-freshness/data_freshness.py --dry-run    # Preview SQL
python3 tools/data-freshness/data_freshness.py               # Run report
python3 tools/data-freshness/data_freshness.py --report      # Run + HTML report
```

**CLI options:**
- `--report, -r` — Generate HTML report and open in browser
- `--check-all` — Run granular checks for ALL tables (not just delayed)
- `--dry-run, -n` — Show SQL queries without executing
- `--verbose, -v` — Show API call details
- `--format csv/json` — Alternative output formats

---

### 6. NOC Report Assistant

**Purpose:** Automate shift handoff, sync Jira statuses, and add ticket rows to the End-of-Shift Excel report

**Features:**
- **Start shift** — copy all tickets from previous shift, update date, sync Jira statuses
- **End shift (SYNC)** — update Jira statuses (column E) for all existing tickets
- **Add row** — insert a new ticket to "Things to monitor" section with Jira + Slack links
- Auto-detects section boundaries ("from previous shifts", "Things to monitor", "Permalinks")
- Handles insert/delete rows when ticket count differs between shifts
- Month boundary handling (e.g. Mar 31 → Apr 1)
- Auto-detects Jira and Slack links in any paste order
- Preserves all Excel formatting, merges, and hyperlinks
- Works with both Night-Shift-NEW and Day-Shift-NEW sheets

**Configuration:** Uses `.env` from toolkit root (JIRA_SERVER_URL, JIRA_PERSONAL_ACCESS_TOKEN)

**Quick setup:**
```bash
python3 tools/noc-report-assistant/noc_report_assistant.py --dry-run    # Preview
python3 tools/noc-report-assistant/noc_report_assistant.py               # Live run
```

**CLI options:**
- `--dry-run, -n` — Show changes without saving
- `--verbose, -v` — Show API call details
- `--file PATH` — Custom Excel file path (default: `~/Downloads/NOC endshift report.xlsx`)

---

### 7. PD Escalation Tool

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

# Recreate symlinks if needed
ln -sf /Users/master/pd-jira-tool tools/pd-jira-tool
ln -sf /Users/master/pagerduty-job-extractor tools/pagerduty-job-extractor
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
│   ├── pd-jira-tool/          # PagerDuty-Jira integration
│   ├── pagerduty-job-extractor/  # Job extractor
│   ├── pd-monitor/            # Auto-acknowledge monitor
│   ├── pd-merge/              # Incident merge tool
│   ├── data-freshness/        # DACSCAN freshness report
│   ├── noc-report-assistant/  # End-of-Shift Excel report tool
│   └── pd-escalate/           # Post-DSSD escalation workflow
├── config/                     # Configuration files
├── docs/                       # Documentation
│   ├── PROJECT_DOCS.md        # Architecture docs
│   └── PLAN.md                # Development plan
├── requirements.txt            # Python dependencies
└── README.md                   # This file
```

---

## 🔄 Version History

### pd-monitor v0.1.4 (2026-03-12)

- ✅ **Bug fix:** `processed_incidents` set was never cleared between check cycles — after PagerDuty auto-un-acknowledges (~30 min), re-triggered incidents were permanently skipped as "already processed"
- ✅ Cached user email at init — eliminates redundant `GET /users/{id}` call on every acknowledge
- ✅ Removed `sys.exit(1)` from `_get_current_user_id()` — raises `RuntimeError` instead
- ✅ 97 unit tests added (pytest)

### pd-jira-tool v0.3.2 (2026-03-12)

- ✅ Extracted `_parse_iso_dt()` and `_is_assigned_to_user()` helpers — deduplicates ISO parsing and user-filter logic
- ✅ Removed `sys.exit(1)` from `check_incidents()` and `process_and_update_incidents()` — exceptions propagate to caller
- ✅ 96 unit tests added (pytest)

### pagerduty-job-extractor v0.1.1 (2026-03-12)

- ✅ Removed `sys.exit(1)` from business logic — exceptions propagate to caller
- ✅ Fixed `any` → `Any` type hint, removed intermediate `list()` calls on iterators
- ✅ 39 unit tests added (pytest)

### pd-merge v0.2.4 (2026-03-12)

- ✅ Extracted `_parse_iso_dt()` helper — deduplicates 7 repeated ISO datetime parsing patterns
- ✅ 70 unit tests added (pytest)

### data-freshness v0.1.1 (2026-03-12)

- ✅ Extracted `_is_fresh_date()` helper — deduplicates 5 repeated freshness date checks
- ✅ Fixed `timedelta` import (was inside function body)
- ✅ 48 unit tests added (pytest)

### pd-escalate v0.1.1 (2026-03-11)

- ✅ Refactor: moved `JIRA_BASE_URL` from global to instance attribute, removed `PD_BASE_URL` global
- ✅ Replaced `sys.exit(1)` in `run()` with `RuntimeError` for cleaner error handling
- ✅ 37 unit tests added (pytest)

### Version 0.6.0 (2026-03-07)

- ✅ Integrated PD Escalation Tool (pd-escalate v0.1.0)
- ✅ Automate post-DSSD escalation: link DRGN→DSSD, transition to Escalated, PD note, Slack template
- ✅ Auto-detect DRGN via PD Jira integration field (`external_references`)
- ✅ Registered as tool #7 in noc-toolkit menu

### noc-report-assistant v0.1.5 (2026-03-11)

- ✅ Refactor: unified layout scanning via `_scan_layout()` in all methods, removed `STOP_MARKERS`
- ✅ Single file I/O pass in `start_shift()` (was double load/save), reduced `_scan_layout()` calls from 6 to 2
- ✅ Extracted sub-methods: `_restructure_from_prev()`, `_reset_ttm()`, `_repair_permalinks()`
- ✅ 46 unit tests added (pytest)

### noc-report-assistant v0.1.4 (2026-03-11)

- ✅ Fix: handle missing "from the previous shifts" section header (fallback to row 8)

### noc-report-assistant v0.1.3 (2026-03-09)

- ✅ Shift handoff automation: "Start shift" copies tickets from previous shift, updates date, syncs
- ✅ Menu reordered: 1=Start shift, 2=End shift (SYNC), 3=Add row
- ✅ Month boundary handling (e.g. Mar 31 → Apr 1)

### noc-report-assistant v0.1.2 (2026-03-09)

- ✅ Bug fix: sync now processes tickets inside "Things to monitor" section (previously skipped)

### noc-report-assistant v0.1.1 (2026-03-07)

- ✅ Fixed hyperlink color: explicit Jira-blue (#0052CC) with underline for ticket and Slack links

### Version 0.5.0 (2026-03-03)

- ✅ Integrated NOC Report Assistant (noc-report-assistant v0.1.0)
- ✅ Sync Jira statuses for existing tickets in End-of-Shift Excel report
- ✅ Add new ticket rows to "Things to monitor" section with Jira + Slack links
- ✅ Robust openpyxl handling for merges, hyperlinks, and cell formatting

### Version 0.4.0 (2026-02-27)

- ✅ Integrated Data Freshness Checker (data-freshness v0.1.0)
- ✅ Automated DACSCAN 15-table report via Databricks SQL REST API
- ✅ Granular table-level checks with host-count and update_ts verification
- ✅ HTML report generation with color-coded rows for Slack posting

### Version 0.3.0 (2026-02-26)

- ✅ Integrated PagerDuty Incident Merge tool (pd-merge v0.2.0)
- ✅ Three merge scenarios: same-day, cross-date with Jira, mass failure consolidation
- ✅ Interactive per-incident selection and skip persistence

### Version 0.2.0 (2026-02-25)

- ✅ PyInstaller EXE fixes — tools now work inside compiled binary
- ✅ Diagnostic debug log on every launch

### Version 0.1.0 (2026-02-22)

**Initial Release**

- ✅ Menu-driven interface
- ✅ Integrated PagerDuty-Jira Tool, Job Extractor, PD Monitor
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
