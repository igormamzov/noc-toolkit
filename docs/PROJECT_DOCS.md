# NOC Toolkit - Project Documentation

## 📋 Project Overview

**Project Name:** NOC Toolkit
**Version:** 0.6.0
**Created:** 2026-02-22
**Purpose:** Unified command-line toolkit for NOC operations, providing a centralized menu-driven interface for various operational tools and scripts.

---

## 🎯 Project Goals

1. **Consolidate Tools**: Provide a single entry point for multiple NOC operational tools
2. **Ease of Use**: Menu-driven interface for quick tool selection and execution
3. **Extensibility**: Easy addition of new tools without modifying core infrastructure
4. **Consistency**: Standardized execution environment and configuration management
5. **Documentation**: Comprehensive documentation for all integrated tools

---

## 🏗️ Architecture

### Directory Structure

```
noc-toolkit/
├── noc-toolkit.py              # Main menu script (entry point)
├── tools/                      # Directory containing all tools
│   ├── pd-jira-tool/          # PagerDuty-Jira integration tool
│   │   └── pagerduty_jira_tool.py
│   ├── pagerduty-job-extractor/  # PagerDuty job extractor
│   │   └── extract_jobs.py
│   ├── pd-monitor/            # PagerDuty monitor
│   │   └── pd_monitor.py
│   ├── pd-merge/              # PagerDuty incident merge
│   │   └── pd_merge.py
│   ├── data-freshness/        # DACSCAN data freshness report
│   │   └── data_freshness.py
│   ├── noc-report-assistant/  # End-of-Shift Excel report tool
│   │   └── noc_report_assistant.py
│   └── pd-escalate/           # Post-DSSD escalation workflow
│       └── pd_escalate.py
├── config/                     # Configuration files
│   ├── .env.example           # Example environment variables
│   └── tools.json             # Tool registry and metadata
├── docs/                       # Documentation
│   ├── PROJECT_DOCS.md        # This file
│   ├── PLAN.md                # Development plan and progress
│   └── tools/                 # Individual tool documentation
├── requirements.txt            # Python dependencies
└── README.md                   # User guide
```

### Design Principles

1. **Modular Design**: Each tool is self-contained in its own directory
2. **Simple Integration**: Tools are referenced via configuration, not hardcoded
3. **Environment Isolation**: Each tool can have its own configuration
4. **Error Handling**: Graceful error handling with informative messages
5. **User-Friendly**: Clear menu options and helpful descriptions

---

## 🔧 Integrated Tools

### 1. PagerDuty-Jira Tool

**Location:** `tools/pd-jira-tool/`
**Main Script:** `pagerduty_jira_tool.py`
**Purpose:** Synchronizes PagerDuty incidents with Jira issues

**Key Features:**
- Fetches PagerDuty incidents based on configured filters
- Auto-discovers Jira tickets from incident titles and comments
- Tracks Jira statuses and posts status-update comments
- Auto-snooze mode with configurable duration
- Auto-detects "ignore"/"disabled" keywords — posts short comment and snoozes
- 12-hour duplicate comment guard to prevent comment spam
- Progress bar with time estimation

**Configuration:**
- Requires PagerDuty API token
- Requires Jira credentials (PAT or email + API token)
- Configured via shared `.env` file from toolkit root

### 2. PagerDuty Job Extractor

**Location:** `tools/pagerduty-job-extractor/`
**Main Script:** `extract_jobs.py`
**Purpose:** Extracts and analyzes PagerDuty on-call schedules and job assignments

**Key Features:**
- Fetches PagerDuty schedules
- Extracts on-call rotation data
- Generates reports on job assignments
- Exports data in various formats (CSV, JSON)

**Configuration:**
- Requires PagerDuty API token
- Configured via environment variables or `.env` file

### 3. PagerDuty Monitor

**Location:** `tools/pd-monitor/`
**Main Script:** `pd_monitor.py`
**Purpose:** Automatically refreshes acknowledgments for PagerDuty incidents to prevent auto-resolve

**Key Features:**
- Auto-acknowledges triggered incidents assigned to current user
- Randomized comment phrases (13 normal + 10 typo variants) to look like a real engineer
- 20% typo probability, 50% lowercase probability for natural variation
- Silent acknowledge (no comment) for "Missing" load-status incidents (AUS & NZL, MSP Export, CANADA, Central, East, International)
- Detects prior auto-comments across all phrase variants to avoid duplicates
- Continuous monitoring with configurable duration and check interval
- Custom `--pattern` override disables randomization (backward compatible)
- Dry-run mode for safe testing
- Verbose mode for debugging

**Configuration:**
- Requires PagerDuty API token (write access)
- Optional configuration via environment variables:
  - `MONITOR_ACKNOWLEDGE_THRESHOLD_HOURS` (default: 4.0)
  - `MONITOR_COMMENT_PATTERN` (default: "working on it")
  - `MONITOR_NEW_INCIDENT_THRESHOLD_HOURS` (default: 1.0)
  - `MONITOR_MAX_AUTO_REFRESHES` (default: 3)
  - `MONITOR_STATE_FILE` (default: ~/.pd-monitor-state.json)
  - `MONITOR_DRY_RUN` (default: false)
  - `MONITOR_VERBOSE` (default: false)

**Cron Integration:**
Designed to run every 10 minutes via cron:
```bash
*/10 * * * * cd /Users/master/pd-monitor && python3 pd_monitor.py >> /tmp/pd-monitor.log 2>&1
```

**Technical Details:**
- State persistence between runs via JSON file
- Automatic cleanup of state entries older than 7 days
- Case-insensitive pattern matching
- Timezone-aware datetime handling (UTC)
- Comprehensive error handling and logging

### 4. PagerDuty Incident Merge

**Location:** `tools/pd-merge/`
**Main Script:** `pd_merge.py`
**Version:** 0.2.2
**Purpose:** Find and merge related PagerDuty incidents that share the same root cause (same job/DAG name)

**Key Features:**
- Automatic grouping of incidents by normalized job name
- Four merge scenarios:
  - **Scenario A:** Same-day incidents — merge by alert priority
  - **Scenario B:** Cross-date with DSSD/DRGN ticket — validate via Jira before merging
  - **Scenario C:** Mass failure consolidation — merge standalone incidents into mass-failure DSSD
  - **Scenario D:** RDS exports "failed to start" — merge individual RDS export failures into umbrella incident (interactive opt-in)
- Deterministic target selection: real comments > alert priority (Databricks > Monitor > AirFlow) > earliest created
- Interactive per-group and per-incident confirmation before merging
- Skip persistence — skipped incidents remembered across runs via JSON file
- Dry-run mode for safe preview
- Detailed merge table with clickable PD links, incident titles, and dd:hh:mm age format
- Interactive skip list management at startup (clear without --clear-skips flag)

**Alert Type Priority:**

| Priority | Alert Type | Role |
|----------|-----------|------|
| 1 (highest) | Databricks batch job failed | Preferred TARGET |
| 2 | Monitor job failed | TARGET only if no Databricks exists |
| 3 (lowest) | AirFlow DAG failed/exceeded | TARGET only if no Databricks or Monitor |

**Title Normalization:**
- Strip DSSD/DRGN/FCR/COREDATA ticket prefixes from titles
- Strip `[ERROR]`, `[DATABRICKS]`, `[CRITICAL]`, `[AIRFLOW]` wrappers
- For Monitor jobs: strip `_prod` and `_airflow_prod` suffixes
- Group incidents by normalized job name

**Configuration:**
- Requires PagerDuty API token (write access for merges)
- Optional: Jira credentials for Scenario B cross-date validation
- Skip file stored at `tools/pd-merge/.pd_merge_skips.json`

**CLI Options:**
- `--dry-run, -n` — Simulate merges without API changes
- `--verbose, -v` — Show extra debug output
- `--clear-skips` — Clear the saved skip list
- `--show-skips` — Show currently skipped incidents

**Technical Details:**
- Two-pass incident fetch (current triggered/acknowledged + historical since Jan 1)
- Note classification: "working on it" → ignore, DSSD/DRGN snooze → context, everything else → real
- Mass failure detection via DSSD incident alert count threshold
- Per-incident selection mode for partial group merges
- Merges executed one-at-a-time with error handling

### 5. Data Freshness Checker

**Location:** `tools/data-freshness/`
**Main Script:** `data_freshness.py`
**Version:** 0.1.0
**Purpose:** Automate the daily DACSCAN Data Freshness Report by querying Databricks SQL

**Key Features:**
- Main 15-row freshness report (DACSCAN, AGG, AUDIT, SUMMARY, BI-LOADER tables)
- Automatic granular checks for delayed tables:
  - Host-level checks for DACSCAN tables (52 hosts expected, excludes TWB/CH8/T43)
  - `max(update_ts)` checks for AGG/AUDIT/SUMMARY aggregate tables
  - Specific date-column checks for BI-LOADER tables
- SALES_ORD_EVENT_OPT known issue handled (DSSD-29069 — fallback to `max(update_ts)`)
- HTML report with color-coded rows (met/delayed/fresh) for Slack screenshots
- SLA countdown display (5:30 PM UTC deadline)
- Connects via Databricks SQL Statement Execution REST API (no heavy SDK)

**Configuration:**
- Requires Databricks credentials via environment variables:
  - `DATABRICKS_HOST` — Databricks workspace hostname
  - `DATABRICKS_TOKEN` — Personal access token
  - `DATABRICKS_WAREHOUSE_ID` — SQL warehouse ID

**CLI Options:**
- `--report, -r` — Generate HTML report and open in browser
- `--check-all` — Run granular checks for ALL tables (not just delayed)
- `--dry-run, -n` — Show SQL queries without executing
- `--verbose, -v` — Show API call details
- `--format csv/json` — Alternative output formats

**Technical Details:**
- `DatabricksSQL` REST client class with async polling (PENDING/RUNNING → SUCCEEDED/FAILED)
- Statement Execution API: `POST /api/2.0/sql/statements`
- 5-minute query timeout with configurable polling interval
- HTML report saved as `freshness-report-YYYY-MM-DD.html`, auto-opened via `webbrowser.open()`
- Three color states: met (white/green), delayed (red background), fresh-but-metadata-lagging (yellow)

### 6. NOC Report Assistant

**Location:** `tools/noc-report-assistant/`
**Main Script:** `noc_report_assistant.py`
**Version:** 0.1.1
**Purpose:** Sync Jira statuses and add ticket rows to the End-of-Shift Excel report

**Key Features:**
- Sync statuses — update Jira statuses (column E) for all existing tickets
- Add row — insert a new ticket to "Things to monitor" section with Jira + Slack links
- Auto-detects Jira and Slack links in any paste order
- Preserves all Excel formatting, merges, and hyperlinks
- Works with both Night-Shift-NEW and Day-Shift-NEW sheets

**Configuration:**
- Requires Jira credentials via environment variables:
  - `JIRA_SERVER_URL`
  - `JIRA_PERSONAL_ACCESS_TOKEN`

### 7. PD Escalation Tool

**Location:** `tools/pd-escalate/`
**Main Script:** `pd_escalate.py`
**Version:** 0.1.0
**Purpose:** Automate the post-DSSD escalation workflow — link DRGN→DSSD, transition DRGN to Escalated, post PD note, print Slack template

**Key Features:**
- 8-step workflow: resolve PD user → fetch incident → detect DRGN → fetch DSSD → link Jira issues → transition DRGN → add PD note → print Slack template
- DRGN auto-detection via PD Jira integration field (`GET /incidents/{id}?include[]=external_references`)
- Fallback: scans PD incident notes for `DRGN-\d+` pattern
- When DRGN is not found: shows PD incident URL with instruction to manually press "Create Jira Issue" button
- Jira link creation: DRGN "is blocked by" DSSD via `create_issue_link(type="Blocks")`
- DRGN transition to "Escalated" status (transition ID 51)
- PD note posting with `From` header (user email)
- Slack template output for #cds-ops-24x7-int

**Configuration:**
- Requires PagerDuty API token (write access for notes)
- Requires Jira credentials (PAT for link creation and transitions)
- Configured via shared `.env` file from toolkit root

**CLI Options:**
- `--pd` — PagerDuty incident ID or URL (required)
- `--dssd` — DSSD ticket key, e.g. DSSD-29386 (required)
- `--drgn` — DRGN ticket key (optional, auto-detected)
- `--dry-run, -n` — Simulate without API mutations
- `--version, -v` — Show version

**Technical Details:**
- `EscalateTool` class following patterns from `pd_merge.py` (PD client init, user resolution, note posting)
- Jira PAT auth pattern from `pagerduty_jira_tool.py`: `JIRA(server=url, token_auth=pat)`
- Incident ID extraction from URL pattern from `extract_jobs.py`
- `external_references` field requires `include[]` parameter on PD API call
- No new dependencies — reuses `pagerduty` + `jira` libs

---

## 🚀 Usage

### Starting the Toolkit

```bash
cd /Users/master/noc-toolkit
python3 noc-toolkit.py
```

### Menu Interface

The toolkit presents an interactive menu:

```
╔════════════════════════════════════════╗
║         NOC Toolkit v0.6.0             ║
╚════════════════════════════════════════╝

Available Tools:
  1. PagerDuty-Jira Tool
  2. PagerDuty Job Extractor
  3. PagerDuty Monitor
  4. PagerDuty Incident Merge
  5. Data Freshness Checker
  6. NOC Report Assistant
  7. PD Escalation Tool

  0. Exit

Select tool [0-7]:
```

### Adding New Tools

To add a new tool to the toolkit:

1. Create a new directory under `tools/`
2. Add your tool script(s)
3. Update `config/tools.json` with tool metadata
4. Add documentation to `docs/tools/`
5. Update `requirements.txt` if needed

---

## ⚙️ Configuration

### Centralized Environment Configuration

**Important:** NOC Toolkit uses a **centralized configuration approach**. All tools share a single `.env` file located in the toolkit root directory.

**Benefits:**
- ✅ Configure once, use everywhere
- ✅ No duplication of API tokens across tool directories
- ✅ Easier to manage and update credentials
- ✅ Reduced risk of using outdated tokens

### Environment Variables

The `.env` file in the toolkit root contains all environment variables for all tools:

```bash
# ============================================================================
# PagerDuty API Configuration
# ============================================================================
# Used by: pd-jira-tool, pagerduty-job-extractor, pd-monitor, pd-merge
PAGERDUTY_API_TOKEN=your_pd_token_here

# ============================================================================
# Jira Configuration
# ============================================================================
# Used by: pd-jira-tool, pd-merge (Scenario B)
JIRA_SERVER_URL=https://jira.livenation.com

# Option 1: Jira Server/Data Center
JIRA_PERSONAL_ACCESS_TOKEN=your_personal_access_token_here

# Option 2: Jira Cloud
# JIRA_EMAIL=your_email@example.com
# JIRA_API_TOKEN=your_jira_api_token_here

# ============================================================================
# Databricks SQL Configuration (for Data Freshness Checker)
# ============================================================================
# Used by: data-freshness
# DATABRICKS_HOST=ticketmaster-cds-analytics.cloud.databricks.com
# DATABRICKS_TOKEN=your_databricks_personal_access_token
# DATABRICKS_WAREHOUSE_ID=dbb3244d6fa2f0fc

# ============================================================================
# Optional Tool-Specific Settings
# ============================================================================
# SNOOZE_DURATION_HOURS=6.0
# LOG_LEVEL=INFO
# OUTPUT_DIR=./output
```

### Configuration Flow

1. User creates `.env` from `.env.example` in toolkit root
2. `noc-toolkit.py` automatically loads `.env` on startup using `python-dotenv`
3. Environment variables become available to all spawned tools
4. Tools access variables using standard `os.environ` or `dotenv.load_dotenv()`
5. Status of configuration loading is displayed in the menu banner

### Initial Setup

```bash
cd /Users/master/noc-toolkit
cp .env.example .env
nano .env  # Edit with your credentials
```

### Tool Registry

Tools are currently registered directly in `noc-toolkit.py` in the `_load_tools()` method:

```python
def _load_tools(self) -> None:
    """Load available tools."""
    self.tools = [
        ToolDefinition(
            tool_id="pd-jira-tool",
            name="PagerDuty-Jira Tool",
            description="Sync PagerDuty incidents with Jira",
            script_path="tools/pd-jira-tool/pagerduty_jira_tool.py",
            enabled=True
        ),
        ToolDefinition(
            tool_id="pagerduty-job-extractor",
            name="PagerDuty Job Extractor",
            description="Extract and analyze PagerDuty on-call schedules",
            script_path="tools/pagerduty-job-extractor/extract_jobs.py",
            enabled=True
        ),
        ToolDefinition(
            tool_id="pd-monitor",
            name="PagerDuty Monitor",
            description="Auto-refresh incident acknowledgments",
            script_path="tools/pd-monitor/pd_monitor.py",
            enabled=True
        ),
        ToolDefinition(
            tool_id="pd-merge",
            name="PagerDuty Incident Merge",
            description="Find and merge related PagerDuty incidents by job name",
            script_path="tools/pd-merge/pd_merge.py",
            enabled=True
        ),
        ToolDefinition(
            tool_id="data-freshness",
            name="Data Freshness Checker",
            description="DACSCAN data freshness report with granular table checks",
            script_path="tools/data-freshness/data_freshness.py",
            enabled=True
        ),
        ToolDefinition(
            tool_id="noc-report-assistant",
            name="NOC Report Assistant",
            description="Sync Jira statuses into End-of-Shift Excel report",
            script_path="tools/noc-report-assistant/noc_report_assistant.py",
            enabled=True
        ),
        ToolDefinition(
            tool_id="pd-escalate",
            name="PD Escalation Tool",
            description="Link DRGN→DSSD, transition to Escalated, post PD note",
            script_path="tools/pd-escalate/pd_escalate.py",
            enabled=True
        ),
    ]
```

**Note:** Future versions may support external `config/tools.json` for dynamic tool registration.

---

## 🔐 Security Considerations

1. **Credentials**: Never commit `.env` files or credentials to version control
2. **API Tokens**: Store sensitive tokens in environment variables
3. **File Permissions**: Ensure configuration files have appropriate permissions
4. **Access Control**: Limit toolkit access to authorized personnel only

---

## 📦 Dependencies

### Python Version
- Python 3.7 or higher

### Core Dependencies
- `python-dotenv` - Environment variable management
- `requests` - HTTP client for API calls
- `colorama` - Cross-platform colored terminal output

### Tool-Specific Dependencies
See individual tool directories for additional requirements.

---

## 🐛 Troubleshooting

### Common Issues

**Issue:** "Module not found" error
**Solution:** Install dependencies: `pip3 install -r requirements.txt`

**Issue:** "Permission denied" when running scripts
**Solution:** Make scripts executable: `chmod +x noc-toolkit.py`

**Issue:** API authentication errors
**Solution:** Verify credentials in `.env` file and check token permissions

### Log Files

Logs are stored in the `logs/` directory (created automatically):
- `noc-toolkit.log` - Main toolkit log
- `tools/*/logs/` - Individual tool logs

---

## 📝 Development Guidelines

### Code Style
- Follow PEP 8 for Python code
- Use type hints for function parameters and return values
- Include docstrings for all functions and classes
- Keep functions focused and single-purpose

### Testing
- Test tools individually before integration
- Verify menu navigation works correctly
- Test error handling and edge cases
- Validate configuration loading

### Documentation
- Update PROJECT_DOCS.md for architectural changes
- Update PLAN.md when completing tasks
- Document new tools in docs/tools/
- Keep README.md user-focused and concise

---

## 🔄 Version History

### Version 0.6.0 (2026-03-07)

**New Tool — PD Escalation Tool (pd-escalate v0.1.0):**
- Automates post-DSSD escalation workflow: link DRGN→DSSD, transition DRGN to Escalated, post PD note, print Slack template
- Auto-detects DRGN via PD Jira integration field (`external_references`) with notes fallback
- When DRGN not found: shows PD URL with instruction to manually press "Create Jira Issue" button
- CLI: `--pd`, `--dssd`, `--drgn` (optional), `--dry-run`
- Registered as tool #7 in noc-toolkit menu
- No new dependencies

### noc-report-assistant v0.1.1 (2026-03-07)

**Hyperlink color fix:**
- Explicit Jira-blue (#0052CC) font color with underline for hyperlink cells (columns D and F)
- New `_apply_hyperlink_font` helper ensures consistent link appearance regardless of Excel template

### Version 0.5.0 (2026-03-03)

**New Tool — NOC Report Assistant (noc-report-assistant v0.1.0):**
- Sync Jira statuses for existing tickets in End-of-Shift Excel report
- Add new ticket rows to "Things to monitor" section with Jira + Slack links
- Robust openpyxl handling for merges, hyperlinks, and cell formatting
- New dependency: openpyxl>=3.1.0
- Registered as tool #6 in noc-toolkit menu

### Version 0.4.0 (2026-02-27)

**New Tool — Data Freshness Checker (data-freshness v0.1.0):**
- Automated DACSCAN 15-table freshness report via Databricks SQL REST API
- Granular host-level checks for DACSCAN tables (52 hosts expected)
- Simple `max(update_ts)` checks for AGG/AUDIT/SUMMARY and BI-LOADER tables
- SALES_ORD_EVENT_OPT known issue (DSSD-29069) handled with `update_ts` fallback
- HTML report with color-coded rows (met/delayed/fresh) for Slack posting
- SLA countdown display (5:30 PM UTC deadline)
- CLI: `--report`, `--check-all`, `--dry-run`, `--verbose`, `--format csv/json`
- Registered as tool #5 in noc-toolkit menu
- No new dependencies — uses `requests` (already bundled)

### Version 0.3.0 (2026-02-26)

**New Tool — PagerDuty Incident Merge (pd-merge v0.2.0):**
- Automated discovery and merging of related PagerDuty incidents by normalized job name
- Four merge scenarios: same-day (A), cross-date with Jira validation (B), mass failure consolidation (C), RDS exports "failed to start" (D)
- Deterministic target selection: real comments > alert priority > earliest created
- Interactive per-group and per-incident confirmation
- Skip persistence across runs via JSON file
- CLI flags: --dry-run, --verbose, --clear-skips, --show-skips
- Implements logic documented in skills/pd-merge-logic.md v1.3

### Version 0.1.0 (2026-02-22)

**Core Features:**
- Initial project setup with complete directory structure
- Interactive menu-driven interface for tool selection
- Integrated pd-jira-tool via symbolic link
- Integrated pagerduty-job-extractor via symbolic link

**Configuration:**
- Centralized environment configuration (single .env file in root)
- Automatic environment loading via python-dotenv
- Configuration status display in menu banner
- Comprehensive .env.example with all tool variables

**Documentation:**
- Complete technical documentation (PROJECT_DOCS.md)
- Development plan with progress tracking (PLAN.md)
- User guide in English (README.md)
- User guide in Russian (README_RU.md)
- Communication context log (CONTEXT.md)

**Developer Experience:**
- Type-hinted Python code
- Modular architecture with ToolDefinition class
- Easy extensibility for adding new tools
- Comprehensive .gitignore
- Consolidated requirements.txt

---

## 👥 Contributors

- Project initiated and developed for NOC team operations

---

## 📄 License

Internal tool for organizational use only.

---

## 🔗 Related Resources

- [PagerDuty API Documentation](https://developer.pagerduty.com/)
- [Jira API Documentation](https://developer.atlassian.com/cloud/jira/)
- [Python Best Practices](https://docs.python-guide.org/)

---

**Last Updated:** 2026-03-07
