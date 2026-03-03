# NOC Toolkit - Development Plan

**Project Start Date:** 2026-02-22
**Current Version:** 1.2.0
**Status:** ✅ Phase 1 Complete, Phase 1.5 Complete, data-freshness integrated

---

## 📊 Project Phases

### Phase 1: Foundation (v1.0.0) ✅ COMPLETED

**Goal:** Create basic toolkit infrastructure with initial tools

#### Tasks

| Task | Status | Completed Date | Notes |
|------|--------|----------------|-------|
| Create project directory structure | ✅ Done | 2026-02-22 | Created noc-toolkit with tools/, docs/, config/ |
| Write PROJECT_DOCS.md | ✅ Done | 2026-02-22 | Complete architecture documentation |
| Write PLAN.md | ✅ Done | 2026-02-22 | This file |
| Create main menu script | ✅ Done | 2026-02-22 | noc-toolkit.py with interactive menu |
| Integrate pd-jira-tool | ✅ Done | 2026-02-22 | Created symlink to existing tool |
| Integrate pagerduty-job-extractor | ✅ Done | 2026-02-22 | Created symlink to existing tool |
| Create requirements.txt | ✅ Done | 2026-02-22 | Consolidated all dependencies |
| Create README.md | ✅ Done | 2026-02-22 | Complete user-facing documentation |
| Create .env.example | ✅ Done | 2026-02-22 | Configuration template in config/ |
| Create tools.json config | ⏭️ Skipped | - | Using hardcoded tool definitions for now |
| Initial testing | ✅ Done | 2026-02-22 | Menu displays correctly, tools accessible |
| Create .gitignore | ✅ Done | 2026-02-22 | Comprehensive ignore rules |

**Phase 1 Progress:** ██████████ 100%

---

### Phase 2: Enhancement (v1.1.0) 📋 PLANNED

**Goal:** Improve user experience and add utility features

#### Planned Features

- [ ] **Colored output** - Add colorful, user-friendly terminal output
- [ ] **Logging system** - Centralized logging for all operations
- [ ] **Configuration wizard** - Interactive setup for first-time users
- [ ] **Tool versioning** - Display tool versions in menu
- [ ] **Command-line arguments** - Allow direct tool execution without menu
  - Example: `./noc-toolkit.py --tool pd-jira`
- [ ] **Help system** - Built-in help for each tool (`--help` flag)
- [ ] **Error reporting** - Better error messages and troubleshooting
- [ ] **Tool health check** - Verify dependencies and configuration

**Phase 2 Progress:** ░░░░░░░░░░ 0%

---

### Phase 3: Expansion (v1.2.0) 🔮 FUTURE

**Goal:** Add more tools and advanced features

#### Potential New Tools

- [ ] **PagerDuty Incident Reporter** - Generate incident reports and statistics
- [ ] **Confluence Documentation Generator** - Auto-generate docs from tickets
- [ ] **Databricks Cluster Monitor** - Check cluster status and usage
- [ ] **Alert Aggregator** - Consolidate alerts from multiple sources
- [ ] **On-Call Schedule Manager** - Manage and display on-call rotations
- [ ] **Backup Automation Tool** - Automated backups for configurations
- [ ] **Health Check Dashboard** - System health monitoring

#### Advanced Features

- [ ] **Plugin system** - Hot-load tools without restart
- [ ] **Scheduled tasks** - Cron-like scheduling within toolkit
- [ ] **Multi-user support** - User profiles and preferences
- [ ] **API server mode** - Run toolkit as REST API service
- [ ] **Web interface** - Browser-based UI for toolkit
- [ ] **Notification integration** - Slack/Teams notifications
- [ ] **Report generation** - Automated reports in PDF/HTML

**Phase 3 Progress:** ░░░░░░░░░░ 0%

---

## 🎯 Current Sprint

**Sprint:** Phase 1 - Foundation ✅ COMPLETED
**Start Date:** 2026-02-22
**Completion Date:** 2026-02-22

### Sprint Goals

1. ✅ Set up project structure
2. ✅ Create documentation
3. ✅ Build main menu interface
4. ✅ Integrate existing tools
5. ✅ Create user README

### Completed Today (2026-02-22)

- [x] Create directory structure
- [x] Write PROJECT_DOCS.md
- [x] Write PLAN.md
- [x] Create noc-toolkit.py with menu
- [x] Integrate pd-jira-tool
- [x] Integrate pagerduty-job-extractor
- [x] Test basic functionality
- [x] Create README.md
- [x] Create requirements.txt
- [x] Create .gitignore and .env.example

**Result:** All Phase 1 objectives achieved! 🎉

---

## 📝 Task Backlog

### High Priority

1. **Complete Phase 1 tasks** - Finish foundation work
2. **Testing** - Ensure all tools work correctly
3. **Documentation** - Complete README for end users

### Medium Priority

4. **Logging** - Add comprehensive logging
5. **Error handling** - Improve error messages
6. **Configuration management** - Simplify config setup

### Low Priority

7. **Code cleanup** - Refactor and optimize
8. **Performance** - Optimize menu rendering
9. **Advanced features** - Consider future enhancements

---

## 🐛 Known Issues

*No known issues at this time*

---

## 💡 Ideas & Suggestions

### Tool Ideas

- **Incident Timeline Generator** - Visual timeline of incident events
- **Team Statistics Dashboard** - Response times, incident counts, etc.
- **Configuration Backup Tool** - Backup and restore tool configs
- **Batch Operations Tool** - Execute operations on multiple items
- **Report Scheduler** - Schedule automated reports

### UX Improvements

- Arrow key navigation in menu
- Tool favorites/recent tools
- Search functionality for tools
- Quick launch shortcuts
- Auto-completion for commands

### Integration Ideas

- Integrate with Slack for notifications
- Connect to Confluence for documentation
- Link with JIRA for ticket management
- Connect to monitoring systems (Datadog, Prometheus)
- Integration with configuration management tools

---

## 📈 Success Metrics

### Phase 1 Success Criteria

- ✅ Project structure established
- ✅ All existing tools integrated and functional
- ✅ Menu system working correctly
- ✅ Documentation complete
- ⏳ Successfully tested by at least one user (pending real-world usage)

### Long-term Metrics

- Number of integrated tools: 5 (target 5+ by v1.2.0 ✅ achieved)
- User adoption rate: Track active users
- Time saved: Measure efficiency gains
- Error rate: Minimize runtime errors
- User satisfaction: Collect feedback

---

## 🔄 Change Log

### 2026-02-27 (data-freshness Tool Integration)

**✅ Completed — Data Freshness Checker (data-freshness v0.1.0):**
- Created `tools/data-freshness/data_freshness.py` — automated DACSCAN 15-table freshness report
- `DatabricksSQL` REST API client (Statement Execution API with async polling)
- Main report query from `skills/noc-analytics.md` (15 rows from meta_load_status + BI-LOADER)
- 8 DACSCAN host-level granular queries (52 hosts expected, excludes TWB/CH8/T43)
- 4 aggregate queries (`max(update_ts)` for AGG/AUDIT/SUMMARY/BI_FACT_EVENT_DAILY)
- 3 BI-LOADER queries (ord_dt and collection_dt freshness)
- SALES_ORD_EVENT_OPT known issue (DSSD-29069) handled with `update_ts` fallback
- HTML report with color-coded rows: met (white/green), delayed (red), fresh (yellow)
- SLA countdown display (5:30 PM UTC deadline) in console output
- CLI: `--report`, `--check-all`, `--dry-run`, `--verbose`, `--format csv/json`
- Registered as tool #5 in noc-toolkit menu
- No new dependencies — uses `requests` (already bundled)
- Updated all documentation: README.md, README_RU.md, VERSION.md, PROJECT_DOCS.md, PLAN.md, CONTEXT.md, noc-analytics.md

**Technical Details:**
- Connection: Databricks SQL Statement Execution REST API (`POST /api/2.0/sql/statements`)
- Auth: Bearer token via `DATABRICKS_HOST`, `DATABRICKS_TOKEN`, `DATABRICKS_WAREHOUSE_ID` env vars
- Polling: PENDING/RUNNING → SUCCEEDED/FAILED with 5-minute timeout
- HTML: inline CSS, single self-contained file, `webbrowser.open()` auto-display

**Tested:**
- Live query against Databricks Analytics: 15 rows, 1 Met / 14 Delayed (metadata lagging)
- Granular checks confirmed 5 tables actually fresh despite delayed metadata
- HTML report generated and opened in browser
- `--dry-run` displays SQL queries without execution
- Syntax check passed (`py_compile`)

### 2026-02-26 (pd-merge Tool Integration)

**✅ Completed — PagerDuty Incident Merge Tool (pd-merge v0.2.0):**
- Created `tools/pd-merge/pd_merge.py` — standalone tool implementing `skills/pd-merge-logic.md` v1.2
- Single class `PagerDutyMergeTool` with full merge workflow
- Three merge scenarios: same-day (A), cross-date with Jira validation (B), mass failure consolidation (C)
- Deterministic target selection: real comments > alert priority (Databricks > Monitor > AirFlow) > earliest
- Title normalization with regex for 4 alert types + consequential patterns
- Note classification: "working on it" → ignore, DSSD/DRGN snooze → context, real → real
- Interactive per-group confirmation (y/n/all/select/skip)
- Per-incident selection mode for partial group merges
- Skip persistence via `.pd_merge_skips.json` (--clear-skips, --show-skips)
- Dry-run mode (--dry-run) and verbose mode (--verbose)
- Registered as tool #4 in noc-toolkit menu
- Updated all documentation: README.md, README_RU.md, PROJECT_DOCS.md, VERSION.md, PLAN.md, CONTEXT.md, SETUP.md, .env.example

**Technical Details:**
- Two-pass fetch: current (triggered+acknowledged) + historical (since Jan 1)
- Mass failure detection via DSSD incident with >10 merged alerts
- Alert type priority: Databricks (P1) > Monitor (P2) > AirFlow (P3) > unknown (P99)
- Jira integration via `jira.JIRA` with `token_auth` for Scenario B validation
- PagerDuty API via `pagerduty.RestApiV2Client` (list_all, rget, rput)

**Tested:**
- `--dry-run` against live PD API: detected DSSD-29178 mass failure (175 alerts, 78 known jobs), found 3 Scenario C candidates, correctly rejected Scenario B cross-date group (different root causes)
- `--show-skips`, `--clear-skips`, `--help` all working
- Syntax check passed (py_compile)

### 2026-02-22 (Phase 1 Complete + Enhancements + pd-monitor Tool)

**✅ Completed - pd-monitor Tool Integration:**
- Created standalone pd-monitor tool for auto-refreshing incident acknowledgments
- Implemented PagerDutyMonitor class with 13+ methods
- Smart refresh logic with 4 action types (add_working_on_it, silent_refresh, needs_update, skip)
- State management via JSON file (~/.pd-monitor-state.json)
- Configurable thresholds and patterns
- CLI interface with argparse (--check, --dry-run, --verbose, etc.)
- Complete documentation (README.md, README_RU.md, cron.example, CHANGELOG.md)
- Integrated into noc-toolkit as third tool
- Updated all toolkit documentation to reflect new tool
- Cron integration examples for automated monitoring

**Technical Details:**
- Acknowledge threshold: 4.0 hours (2-hour safety buffer)
- Comment pattern detection: "working on it" (case-insensitive)
- Max auto-refreshes: 3 (prevents infinite loops)
- State cleanup: Automatic removal of entries older than 7 days
- Exit codes: 0 (success), 1 (needs update), 130 (interrupted)

### 2026-02-22 (Phase 1 Complete + Enhancements)

**✅ Completed - Phase 1 Foundation:**
- Initial project setup
- Created directory structure (tools/, docs/, config/)
- Created PROJECT_DOCS.md with complete architecture documentation
- Created PLAN.md (this file) with development roadmap
- Created noc-toolkit.py with interactive menu system
- Integrated pd-jira-tool via symbolic link
- Integrated pagerduty-job-extractor via symbolic link
- Created README.md with comprehensive user documentation (English)
- Created requirements.txt with all dependencies
- Created .gitignore for version control
- Initial testing completed successfully - menu displays correctly, both tools accessible

**✅ Completed - Configuration Enhancement:**
- **Centralized .env configuration** - Single .env file in root for all tools
- Updated .env.example with complete set of variables from all tools
- Modified noc-toolkit.py to auto-load .env on startup
- Added configuration status indicator in menu banner (✓ / ⚠️)
- Eliminated need to configure each tool separately

**✅ Completed - Documentation Enhancement:**
- **README_RU.md** - Complete Russian translation with detailed setup instructions
- **CONTEXT.md** - Communication log for tracking discussions and decisions
- Updated PROJECT_DOCS.md with centralized configuration approach
- Updated PLAN.md with all enhancements (this update)

**📊 Results:**
- Phase 1: 100% complete with enhancements
- All core functionality implemented and tested
- Centralized configuration working correctly
- Comprehensive documentation in both English and Russian
- Context tracking system in place
- Ready for production use

---

## 📚 Technical Decisions

### Decision Log

**Decision 1:** Tool Integration Method
- **Date:** 2026-02-22
- **Decision:** Use symbolic links or copy tools into tools/ directory
- **Rationale:** Keeps toolkit self-contained while allowing tools to be updated independently
- **Alternative Considered:** Direct execution from original locations
- **Status:** To be implemented

**Decision 2:** Configuration Approach
- **Date:** 2026-02-22
- **Decision:** Use JSON-based tool registry with .env for secrets
- **Rationale:** Separates tool metadata from sensitive credentials
- **Alternative Considered:** Single YAML config file
- **Status:** To be implemented

**Decision 3:** Menu System
- **Date:** 2026-02-22
- **Decision:** Simple numeric menu with subprocess execution
- **Rationale:** Easy to use, cross-platform compatible
- **Alternative Considered:** curses-based TUI, CLI arguments only
- **Status:** To be implemented

---

## 🎓 Lessons Learned

*Will be updated as development progresses*

---

## 🤝 Contributing

### How to Contribute

1. Add new tool to `tools/` directory
2. Update `config/tools.json` with tool metadata
3. Update this PLAN.md with tool description
4. Add tool documentation to `docs/tools/`
5. Test integration with menu system
6. Update requirements.txt if needed

### Development Workflow

1. Check PLAN.md for current tasks
2. Update task status when starting work
3. Complete task and mark as done with date
4. Update Change Log section
5. Add lessons learned if applicable

---

## 📞 Support & Questions

For questions or issues with the toolkit:
- Check PROJECT_DOCS.md for architecture details
- Review README.md for usage instructions
- Consult individual tool documentation

---

**Last Updated:** 2026-02-27 by Claude
**Next Review:** After Phase 2 planning
