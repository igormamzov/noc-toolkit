# NOC Toolkit

**Version:** 1.0.0
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
║              NOC Toolkit v1.0.0                        ║
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

--------------------------------------------------------
  0. Exit
========================================================

Select tool [0-2]:
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
- Creates corresponding Jira issues
- Updates existing issues with incident status
- Configurable field mapping

**Configuration:** See [tools/pd-jira-tool/README.md](tools/pd-jira-tool/README.md)

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

**Purpose:** Automatically refreshes incident acknowledgments to prevent auto-resolve

**Features:**
- Monitors acknowledged incidents every 10 minutes (via cron)
- Auto-refreshes acknowledgments before 6-hour timeout
- Smart logic to avoid comment spam
- Tracks refresh count per incident
- Alerts when manual updates needed (3+ refreshes)
- Dry-run mode for safe testing

**Configuration:** See [tools/pd-monitor/README.md](tools/pd-monitor/README.md)

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
├── tools/                      # All tools (symlinks or copies)
│   ├── pd-jira-tool/          # PagerDuty-Jira integration
│   └── pagerduty-job-extractor/  # Job extractor
├── config/                     # Configuration files
├── docs/                       # Documentation
│   ├── PROJECT_DOCS.md        # Architecture docs
│   └── PLAN.md                # Development plan
├── requirements.txt            # Python dependencies
└── README.md                   # This file
```

---

## 🔄 Version History

### Version 1.0.0 (2026-02-22)

**Initial Release**

- ✅ Menu-driven interface
- ✅ Integrated PagerDuty-Jira Tool
- ✅ Integrated PagerDuty Job Extractor
- ✅ Tool health checks
- ✅ Comprehensive documentation

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
