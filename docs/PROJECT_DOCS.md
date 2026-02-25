# NOC Toolkit - Project Documentation

## 📋 Project Overview

**Project Name:** NOC Toolkit
**Version:** 1.0.0
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
│   └── pd-monitor/            # PagerDuty monitor
│       └── pd_monitor.py
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
- Creates corresponding Jira issues
- Updates existing issues with incident status
- Configurable mapping between PagerDuty and Jira fields

**Configuration:**
- Requires PagerDuty API token
- Requires Jira credentials (email + API token)
- Configured via environment variables or `.env` file

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
- Monitors acknowledged incidents every 10 minutes (via cron)
- Auto-refreshes acknowledgments before 6-hour timeout
- Smart refresh logic with 4 action types:
  - `add_working_on_it` - Add "working on it" comment for new incidents
  - `silent_refresh` - Minimal timestamp comment for tracked incidents
  - `needs_update` - Flag incidents requiring manual engineer update
  - `skip` - Skip old incidents without tracking pattern
- State management via JSON file for tracking refresh counts
- Configurable thresholds and patterns
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
║         NOC Toolkit v1.0.0             ║
╚════════════════════════════════════════╝

Available Tools:
  1. PagerDuty-Jira Tool
  2. PagerDuty Job Extractor
  3. [Future Tool 3]

  0. Exit

Select tool [0-2]:
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
# Used by: pd-jira-tool, pagerduty-job-extractor
PAGERDUTY_API_TOKEN=your_pd_token_here

# ============================================================================
# Jira Configuration
# ============================================================================
# Used by: pd-jira-tool
JIRA_SERVER_URL=https://jira.livenation.com

# Option 1: Jira Server/Data Center
JIRA_PERSONAL_ACCESS_TOKEN=your_personal_access_token_here

# Option 2: Jira Cloud
# JIRA_EMAIL=your_email@example.com
# JIRA_API_TOKEN=your_jira_api_token_here

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

### Version 1.0.0 (2026-02-22)

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

**Last Updated:** 2026-02-22
