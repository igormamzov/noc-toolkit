# NOC Toolkit - Version Information

This document describes the versioning strategy for the NOC Toolkit and all its tools.

---

## Versioning Standard

We follow **Semantic Versioning (SemVer)**: `MAJOR.MINOR.PATCH`

- **MAJOR (X)** - Breaking changes, incompatible API changes
- **MINOR (Y)** - New features (backward compatible)
- **PATCH (Z)** - Bug fixes (backward compatible)

### Pre-1.0 Development

All components are currently in **0.x.x** version, indicating active development:
- API is not yet stable
- Breaking changes may occur between minor versions
- Version 1.0.0 will indicate production-ready, stable API

---

## Current Versions

| Component                  | Version | Status        | Description                                    |
|----------------------------|---------|---------------|------------------------------------------------|
| **noc-toolkit**            | 0.6.0   | Development   | Main toolkit launcher and orchestrator         |
| **pd-monitor**             | 0.1.3   | Development   | Auto-acknowledge triggered PagerDuty incidents |
| **pd-jira-tool**           | 0.3.1   | Development   | PagerDuty-Jira integration and sync tool       |
| **pagerduty-job-extractor**| 0.1.0   | Development   | Extract failed job names from PD incidents     |
| **pd-merge**               | 0.2.2   | Development   | Find and merge related PD incidents by job name|
| **pd-escalate**            | 0.1.0   | Development   | Post-DSSD escalation workflow automation       |
| **data-freshness**         | 0.1.0   | Development   | DACSCAN data freshness report via Databricks SQL|
| **noc-report-assistant**   | 0.1.1   | Development   | Sync Jira statuses into End-of-Shift Excel report|

---

## Version Storage

Each component stores its version in two places:

1. **Python file** - `VERSION = "X.Y.Z"` constant at the top of the main script
2. **README.md** - `**Version:** X.Y.Z` in the header section

### Accessing Version Information

**From Command Line:**
```bash
# NOC Toolkit
python3 noc-toolkit.py --help  # Version shown in help

# Individual Tools
python3 tools/pd-monitor/pd_monitor.py --version
```

**From Python Code:**
```python
# Import version from tool
from pd_monitor import VERSION
print(f"Version: {VERSION}")
```

---

## Version History

### NOC Toolkit v0.6.0 (2026-03-07)

**New tool — PD Escalation Tool (pd-escalate v0.1.0):**
- Automates post-DSSD escalation workflow: link DRGN→DSSD, transition DRGN to Escalated, post PD note, print Slack template
- Auto-detects DRGN ticket via PD Jira integration field (`external_references`) with notes fallback
- When DRGN is not found: shows PD incident URL with instruction to manually press "Create Jira Issue" button
- CLI: `--pd` (incident ID or URL), `--dssd` (required), `--drgn` (optional), `--dry-run`
- No new dependencies — reuses `pagerduty` + `jira` libs already in requirements.txt
- Registered as tool #7 in noc-toolkit menu

### pd-escalate v0.1.0 (2026-03-07)

**Initial release:**
- `EscalateTool` class with 8-step workflow: resolve PD user → fetch incident → detect DRGN → fetch DSSD → link Jira issues → transition DRGN → add PD note → print Slack template
- DRGN detection via `GET /incidents/{id}?include[]=external_references` (PD Jira integration field)
- Fallback DRGN detection by scanning PD incident notes for `DRGN-\d+` pattern
- Jira link creation: DRGN "is blocked by" DSSD via `create_issue_link(type="Blocks")`
- DRGN transition to "Escalated" status (transition ID 51)
- PD note with escalation summary + Jira URL
- Slack template output for #cds-ops-24x7-int with hyperlink instructions
- Dry-run mode for safe testing

### noc-report-assistant v0.1.1 (2026-03-07)

**Hyperlink color fix:**
- Explicit Jira-blue (#0052CC) font color with underline for hyperlink cells (columns D and F)
- Previously link color depended on reference row styling in the Excel template
- New `_apply_hyperlink_font` helper ensures consistent link appearance regardless of template

### pd-monitor v0.1.3 (2026-03-07)

**Background mode — run pd-monitor while using other tools:**
- New `--background` CLI flag: skip interactive duration menu, suppress `\r` progress bar
- New `MonitorBackground` class in noc-toolkit.py: `subprocess.Popen` + daemon reader thread
- Sub-menu when selecting PD Monitor: background / foreground / view output / stop
- Banner status line: `▶ PD Monitor: ACTIVE 12m/60m | 5 new`
- Output ring buffer (500 lines) with view-on-demand
- Auto-stop on toolkit exit (choice 0 or Ctrl+C)

### pd-monitor v0.1.2 (2026-03-07)

**Silent acknowledge for "Missing" load-status incidents:**
- New `SILENT_ACK_PATTERNS` list: 6 title patterns (Missing AUS & NZL, Missing MSP Export, Missing CANADA, Missing Central, Missing East, Missing International)
- Incidents matching these patterns are acknowledged without posting a comment
- New `_is_silent_ack()` method for case-insensitive substring matching
- Separate `silent_ack` counter in summary output
- All other incidents processed as before (with comment)

### pd-monitor v0.1.1 (2026-03-04)

**Diversified auto-acknowledge comments:**
- Randomized comment phrases (13 normal + 10 typo variants) instead of single "working on it"
- 20% probability of typo variant for natural look
- 50% probability of lowercase first letter
- Matching logic updated to detect all phrase variants in existing comments
- Backward compatible: custom `--pattern` or `MONITOR_COMMENT_PATTERN` still works as before

### pd-merge v0.2.2 (2026-03-07)

**UI/UX improvements for merge table and skip list:**
- Table: incident ID replaced with full PagerDuty link (clickable in terminal)
- Table: new "Title" column showing stripped incident name
- Table: "Alert Type" shows "RDS Exports" instead of "Unknown" for RDS export incidents
- Table: "Age" column in dd:hh:mm format (time since creation) instead of creation time
- Interactive skip list clear: prompt to clear skip list at startup (no need for --clear-skips flag)

### pd-merge v0.2.1 (2026-03-06)

**RDS Export "failed to start" consolidation (Scenario D):**
- New interactive merge option for RDS export incidents
- Merges individual `RDS export <job> is failed more than 30 minutes` into `RDS export(s) - failed to start` umbrella
- Validates "Failed to start" in target's notes/comments before merging
- Interactive opt-in: option shown only when 2+ RDS export incidents detected
- Updated pd-merge-logic.md to v1.3

### pd-jira-tool v0.3.1 (2026-03-04)

**Auto-handle ignore/disabled incidents:**
- Detect "ignore" or "disabled" keywords in incident title and last 3 comments
- In --snooze mode: post "Ignore. Snooze" or "Disabled. Snooze" comment and snooze
- In --update mode: post "Ignore" or "Disabled" comment (no snooze)
- 12-hour duplicate comment guard; still re-snoozes if timer expired
- New summary category in output: "Auto-snoozed (ignore/disabled keyword)"

### NOC Toolkit v0.5.0 (2026-03-03)

**New tool — NOC Report Assistant (noc-report-assistant v0.1.0):**
- Sync Jira statuses (column E) for existing tickets in End-of-Shift Excel report
- Add new ticket rows to "Things to monitor" section with Jira + Slack links
- Auto-detect Jira/Slack links in any paste order
- Interactive sheet and action selection menus
- Robust openpyxl handling: 6 workarounds for insert_rows pitfalls (merge duplication, hyperlink corruption, fill loss)
- Preserves all Excel formatting, merges, and hyperlinks across inserts
- CLI: --dry-run, --verbose, --file
- Registered as tool #6 in noc-toolkit menu
- New dependency: openpyxl>=3.1.0

### noc-report-assistant v0.1.0 (2026-03-03)

**Initial release:**
- Two actions: sync statuses, add row
- Jira REST API integration via stdlib urllib (Bearer token, SSL disabled)
- Native cell hyperlinks (not =HYPERLINK formulas) to avoid merged cell corruption
- Style copying from reference rows for consistent formatting
- Permalink merge preservation after row insertions

### NOC Toolkit v0.4.0 (2026-02-27)

**New tool — Data Freshness Checker (data-freshness v0.1.0):**
- Automated DACSCAN 15-table freshness report via Databricks SQL REST API
- Granular host-level checks for DACSCAN tables (52 hosts expected)
- Simple max(update_ts) checks for AGG/AUDIT/SUMMARY and BI-LOADER tables
- SALES_ORD_EVENT_OPT known issue (DSSD-29069) handled with update_ts fallback
- HTML report with color-coded rows (met/delayed/fresh) for Slack posting
- SLA countdown display (5:30 PM UTC deadline)
- CLI: --report, --check-all, --dry-run, --verbose, --format csv/json
- Registered as tool #5 in noc-toolkit menu
- No new dependencies — uses requests (already bundled)

### data-freshness v0.1.0 (2026-02-27)

**Initial release:**
- DatabricksSQL REST API client (Statement Execution API with polling)
- Main freshness report query (15 rows from meta_load_status + BI-LOADER tables)
- 8 DACSCAN host-level granular queries + 4 aggregate queries + 3 BI-LOADER queries
- HTML report generation with webbrowser auto-open

### NOC Toolkit v0.3.0 (2026-02-26)

**New tool — PagerDuty Incident Merge (pd-merge v0.2.0):**
- Automated discovery and merging of related PD incidents by normalized job name
- Three merge scenarios: same-day (A), cross-date with Jira validation (B), mass failure consolidation (C)
- Deterministic target selection: real comments > alert priority > earliest
- Interactive per-group and per-incident confirmation
- Skip persistence across runs (.pd_merge_skips.json)
- CLI: --dry-run, --verbose, --clear-skips, --show-skips
- Registered as tool #4 in noc-toolkit menu

### pd-merge v0.2.0 (2026-02-26)

**Initial release:**
- v0.1.0: Core merge logic implementing pd-merge-logic.md v1.2
- v0.2.0: Added skip persistence (JSON file) and per-incident selection mode

### NOC Toolkit v0.2.0 (2026-02-25)

**PyInstaller EXE fixes — tools now work inside the compiled binary:**

**Bug fixes:**
- **Symlinks replaced with real files** — `tools/` contained symlinks to local dev directories (`/Users/master/pd-jira-tool/`, etc.) which don't exist on GitHub Actions runners. PyInstaller was bundling an empty `tools/` directory, so the EXE launched but showed "Script not found" for all 3 tools. Fixed by copying the actual Python scripts into the repository.
- **`.env` not found in EXE mode** — `Path(__file__).parent` in PyInstaller onefile mode points to the temp extraction directory (`_MEI...`), not the folder where the EXE lives. Fixed by using `Path(sys.executable).parent` when `sys.frozen` is True, so `.env` placed next to the EXE is correctly loaded.
- **Tools re-launched the toolkit instead of running** — `subprocess.run([sys.executable, tool_path])` was used to launch tools, but in PyInstaller mode `sys.executable` is `NOC-Toolkit.exe` (not Python). This caused the EXE to re-launch itself instead of running the tool script. Fixed by using `runpy.run_path()` to execute tools in-process when frozen.
- **Tool dependencies not bundled** — Tools import `pagerduty`, `jira`, `tqdm` at runtime, but PyInstaller only auto-detects imports from the main script. Since tools are loaded dynamically via `runpy`, these packages were not included in the EXE. Fixed by adding them to `hiddenimports` in `NOC-Toolkit.spec`.

**New features:**
- **Diagnostic debug log** — On every launch, writes `noc-toolkit-debug.log` next to the EXE with: Python/OS/platform info, PyInstaller paths (`frozen`, `_MEIPASS`, `executable`), `.env` location and load status, credential env vars (masked), full `tools/` directory listing, and tool launch commands with exit codes.

**Architecture notes (PyInstaller onefile mode):**
- `sys.executable` → the EXE itself, NOT a Python interpreter
- `sys._MEIPASS` → temp extraction dir where bundled files live
- `Path(__file__).parent` → inside `_MEIPASS`, not next to the EXE
- Config files (`.env`) must be resolved via `Path(sys.executable).parent`
- Bundled data files (tools) must be resolved via `Path(sys._MEIPASS)`
- Tool scripts cannot be run via `subprocess` (no Python interpreter available) — use `runpy.run_path()` instead
- Dynamic imports must be listed in `hiddenimports` in the `.spec` file

### NOC Toolkit v0.1.0 (2026-02-22)

**Initial unified release:**
- Unified launcher for all NOC tools
- Centralized configuration via shared `.env` file
- Standardized versioning across all tools
- Tools: pd-monitor (0.1.0), pd-jira-tool (0.3.0), pagerduty-job-extractor (0.1.0)

### pd-monitor v0.1.0 (2026-02-22)

**Initial release:**
- Monitor triggered incidents assigned to current user
- Automatic acknowledgment with smart comment logic
- Continuous monitoring mode with countdown timer
- Output file for incidents needing attention

### pd-jira-tool v0.3.0 (2026-02-22)

**Version standardization:**
- Formalized version number from previous informal "v3.2"
- Existing features: auto-discovery, status tracking, auto-snooze
- Progress bar with time estimation
- Smart filtering and duplicate prevention

### pagerduty-job-extractor v0.1.0 (2026-02-22)

**Initial versioned release:**
- Extract failed job names matching `jb_*` pattern
- Support for incident URLs and IDs
- Integration with NOC Toolkit

---

## Roadmap to v1.0.0

Before marking any component as 1.0.0 (production-ready), we will:

1. **Stabilize API** - No more breaking changes
2. **Complete Testing** - Comprehensive test coverage
3. **User Feedback** - Incorporate feedback from production use
4. **Documentation** - Complete documentation for all features
5. **Error Handling** - Robust error handling and recovery

Target: **Q2 2026**

---

## Version Update Process

When updating versions:

1. **Update Python file** - Change `VERSION` constant
2. **Update README.md** - Change version in header
3. **Update VERSION.md** - Add entry to version history
4. **Tag in Git** - Create version tag (if using git)
5. **Update Changelog** - Document changes in CHANGELOG.md (if exists)

---

**Last Updated:** 2026-03-07
**Maintained by:** NOC Team
