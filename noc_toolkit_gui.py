#!/usr/bin/env python3
"""
NOC Toolkit — GUI Launcher

A customtkinter-based graphical interface for launching and managing
NOC operational tools.  Reuses the same config/env infrastructure as
the CLI menu (noc-toolkit.py).
"""

import io
import os
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

import customtkinter as ctk

# ---------------------------------------------------------------------------
# Bootstrap paths (mirrors noc-toolkit.py)
# ---------------------------------------------------------------------------
_FROZEN = getattr(sys, "frozen", False)
_EXE_DIR = Path(sys.executable).parent if _FROZEN else Path(__file__).parent.resolve()
_MEIPASS = getattr(sys, "_MEIPASS", None)

SCRIPT_DIR = Path(_MEIPASS) if _MEIPASS else Path(__file__).parent.resolve()
TOOLS_DIR = SCRIPT_DIR / "tools"
COMMON_DIR = TOOLS_DIR / "common"

_common_str = str(COMMON_DIR)
if _common_str not in sys.path:
    sys.path.insert(0, _common_str)

from noc_utils import (  # noqa: E402
    extract_env_references,
    load_config,
    setup_logging,
)

_DOTENV_AVAILABLE = False
try:
    from dotenv import dotenv_values  # noqa: F401

    _DOTENV_AVAILABLE = True
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Version (kept in sync with noc-toolkit.py)
# ---------------------------------------------------------------------------
VERSION = "0.6.1"

logger = setup_logging(name=__name__)

# ---------------------------------------------------------------------------
# Appearance
# ---------------------------------------------------------------------------
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

_MONO = "Consolas" if sys.platform == "win32" else "Menlo"
_FONT = (_MONO, 13)
_FONT_BOLD = (_MONO, 13, "bold")
_FONT_HEADER = (_MONO, 14)

# Colours matching the dark screenshot
_BG = "#1e1e2e"
_BG_PANEL = "#252536"
_BG_INPUT = "#2a2a3d"
_FG = "#d0d0d0"
_FG_DIM = "#808090"
_GREEN = "#2fa572"
_RED = "#d9534f"
_BLUE = "#3b6bb5"

# Log directory for per-tool output files
LOGS_DIR = _EXE_DIR / "logs"

# Maximum lines kept in per-tab in-memory log buffer
_LOG_BUFFER_MAX = 5000


@dataclass
class _TabState:
    """Per-tool-tab runtime state for parallel execution."""

    process: Optional[subprocess.Popen] = None
    stdin_pipe: Optional[Any] = None
    log_buffer: List[str] = field(default_factory=list)
    log_fh: Optional[io.TextIOWrapper] = None
    launch_btn: Optional[ctk.CTkButton] = None
    stop_btn: Optional[ctk.CTkButton] = None
    status_label: Optional[ctk.CTkLabel] = None


# ═══════════════════════════════════════════════════════════════════════════
# Config key registry — master list of ALL config.yaml keys
# ═══════════════════════════════════════════════════════════════════════════
#
# Every key that can appear in config.yaml is listed here.  Each entry
# specifies:
#   section  – YAML section name ("tokens" or "settings")
#   key      – config key name
#   label    – human-readable label
#   secret   – True if the value should be masked in UI
#   tool     – tool_id that owns this key, or None for shared keys
#   default  – placeholder / example default
#   desc     – short description

class _CfgKey:
    """Metadata for a single config.yaml key."""

    __slots__ = ("section", "key", "label", "secret", "tool", "default", "desc")

    def __init__(
        self,
        section: str,
        key: str,
        label: str,
        secret: bool = False,
        tool: Optional[str] = None,
        default: str = "",
        desc: str = "",
    ) -> None:
        self.section = section
        self.key = key
        self.label = label
        self.secret = secret
        self.tool = tool
        self.default = default
        self.desc = desc


CONFIG_KEYS: List[_CfgKey] = [
    # --- tokens: shared credentials ---
    _CfgKey("tokens", "PAGERDUTY_API_TOKEN",
            "PagerDuty API token", secret=True,
            desc="Used by: pd-sync, pd-jobs, pd-monitor, pd-merge, pd-escalate, pd-resolve"),
    _CfgKey("tokens", "JIRA_SERVER_URL",
            "Jira Server URL",
            default="https://jira.example.com",
            desc="Jira Server / Data Center base URL"),
    _CfgKey("tokens", "JIRA_PERSONAL_ACCESS_TOKEN",
            "Jira PAT", secret=True,
            desc="Jira Server / Data Center Personal Access Token"),
    _CfgKey("tokens", "JIRA_EMAIL",
            "Jira Cloud email",
            desc="Jira Cloud authentication (alternative to PAT)"),
    _CfgKey("tokens", "JIRA_API_TOKEN",
            "Jira Cloud API token", secret=True,
            desc="Jira Cloud authentication (alternative to PAT)"),

    # --- tokens: Databricks (used by: freshness) ---
    _CfgKey("tokens", "DATABRICKS_HOST",
            "Databricks host URL",
            default="https://your-instance.cloud.databricks.com",
            desc="Used by: freshness — Databricks SQL endpoint"),
    _CfgKey("tokens", "DATABRICKS_TOKEN",
            "Databricks token", secret=True,
            desc="Used by: freshness — Databricks personal access token"),
    _CfgKey("tokens", "DATABRICKS_WAREHOUSE_ID",
            "Databricks warehouse ID",
            desc="Used by: freshness — SQL warehouse ID for queries"),

    # --- tokens: Google Sheets (used by: gsheet-report) ---
    _CfgKey("tokens", "GSHEET_WEBAPP_URL",
            "Google Sheets Web App URL",
            desc="Used by: gsheet-report — Apps Script deployment URL"),
    _CfgKey("tokens", "GSHEET_API_KEY",
            "Google Sheets API key", secret=True,
            desc="Used by: gsheet-report — API key for the Apps Script web app"),

    # --- settings: AWS / MWAA (used by: pd-resolve) ---
    _CfgKey("settings", "AWS_PROFILE",
            "AWS profile",
            desc="Used by: pd-resolve — AWS profile with MWAA access"),
    _CfgKey("settings", "MWAA_ENVIRONMENT_NAME",
            "MWAA environment",
            desc="Used by: pd-resolve — Airflow MWAA environment name"),
    _CfgKey("settings", "MWAA_REGION",
            "MWAA region",
            desc="Used by: pd-resolve — AWS region for MWAA"),

    # --- settings: tool-specific (non-sensitive, shown on tool tabs) ---
    _CfgKey("settings", "TICKET_WATCH_REPORTERS",
            "Reporter names", tool="ticket-watch",
            default="Name1,Name2,Name3",
            desc="Comma-separated Jira reporter display names"),
    _CfgKey("settings", "TICKET_WATCH_PROJECT",
            "Project key", tool="ticket-watch",
            default="DSSD",
            desc="Jira project key (default: DSSD)"),
    _CfgKey("settings", "NOC_REPORT_PATH",
            "Excel report path",
            default="~/Downloads/NOC endshift report.xlsx",
            desc="Path to local Excel shift report"),
    _CfgKey("settings", "LOG_LEVEL",
            "Log level",
            default="INFO",
            desc="Global log level (DEBUG, INFO, WARNING, ERROR)"),
]

# Pre-index: tool_id → list of _CfgKey for tool-specific non-secret keys
_TOOL_CFG_KEYS: Dict[str, List[_CfgKey]] = {}
for _ck in CONFIG_KEYS:
    if _ck.tool is not None:
        _TOOL_CFG_KEYS.setdefault(_ck.tool, []).append(_ck)


# ═══════════════════════════════════════════════════════════════════════════
# Tool descriptors — one per tab
# ═══════════════════════════════════════════════════════════════════════════
#
# Option kinds:
#   "section"  – section header label (no widget, just visual grouping)
#   "radio"    – horizontal radio button group; "values" is list of
#                (cli_value, display_label) tuples; "default" = initial value
#   "bool"     – checkbox
#   "entry"    – text input
#   "choice"   – dropdown combo box
#   "row"      – multiple sub-options rendered on a single row

class _ToolTab:
    """Declarative description of a tool tab and its CLI options."""

    def __init__(
        self,
        tool_id: str,
        label: str,
        script: str,
        options: Optional[List[Dict[str, Any]]] = None,
        description: str = "",
    ) -> None:
        self.tool_id = tool_id
        self.label = label
        self.script = script
        self.options: List[Dict[str, Any]] = options or []
        self.description = description

    @property
    def script_path(self) -> Path:
        return SCRIPT_DIR / self.script


TOOL_TABS: List[_ToolTab] = [
    # ------------------------------------------------------------------
    # PD-Jira  (pd-sync)
    # ------------------------------------------------------------------
    _ToolTab(
        tool_id="pd-sync",
        label="PD-Jira",
        script="tools/pd-sync/pd_sync.py",
        description="Sync PagerDuty incidents with Jira issues",
        options=[
            {"kind": "info", "text":
             "Auto-discovers Jira tickets from PD titles & comments, "
             "posts status updates, 12h duplicate guard."},
            {"kind": "section", "label": "Mode:"},
            {
                "kind": "radio",
                "flag": "",
                "label": "mode",
                "values": [
                    ("--check", "Check only"),
                    ("--update", "Update incidents"),
                    ("--snooze", "Update & snooze"),
                ],
                "default": "--check",
                "tips": {
                    "--check": "Read-only — show incidents + Jira status, no API changes",
                    "--update": "Post Jira status comments to PD incidents",
                    "--snooze": "Post comments + auto-snooze (default 6 hours)",
                },
            },
            {"kind": "section", "label": "Filter:"},
            {
                "kind": "radio",
                "flag": "",
                "label": "filter",
                "values": [
                    ("", "My incidents"),
                    ("--all", "All incidents"),
                ],
                "default": "",
            },
            {"kind": "section", "label": "Options:"},
            {"flag": "--check-jira", "label": "Check Jira ticket status", "kind": "bool"},
            {"flag": "--limit", "label": "Limit incidents:", "kind": "entry",
             "default": "", "placeholder": "0 = all"},
            {
                "kind": "row",
                "items": [
                    {"flag": "--details", "label": "Show details", "kind": "bool"},
                    {"flag": "--save-summary", "label": "Save summary to file", "kind": "bool"},
                ],
            },
        ],
    ),
    # ------------------------------------------------------------------
    # Job Extractor  (pd-jobs)
    # ------------------------------------------------------------------
    _ToolTab(
        tool_id="pd-jobs",
        label="Job Extractor",
        script="tools/pd-jobs/pd_jobs.py",
        description="Extract job names from merged PD incidents",
        options=[
            {"kind": "info", "text":
             "Parses merged alerts and extracts jb_* job names into a clean list."},
            {"kind": "section", "label": "Input:"},
            {"flag": "", "label": "Incident URL / ID:", "kind": "entry",
             "default": "", "placeholder": "paste PD URL or incident ID"},
        ],
    ),
    # ------------------------------------------------------------------
    # PD Monitor
    # ------------------------------------------------------------------
    _ToolTab(
        tool_id="pd-monitor",
        label="PD Monitor",
        script="tools/pd-monitor/pd_monitor.py",
        description="Auto-acknowledge triggered incidents",
        options=[
            {"kind": "info", "text":
             "Acknowledges triggered PD incidents, posts randomized human-like "
             "comments (13 phrases + 10 typo variants). Silent ack for "
             "\"Missing\" load-status incidents."},
            {"kind": "section", "label": "Run mode:"},
            {
                "kind": "radio",
                "flag": "",
                "label": "run_mode",
                "values": [
                    ("", "Continuous"),
                    ("--once", "Run once"),
                    ("--background", "Background"),
                ],
                "default": "",
                "tips": {
                    "": "Monitor with interactive duration menu",
                    "--once": "Check once and exit (no continuous monitoring)",
                    "--background": "Skip duration menu, suppress progress bar",
                },
            },
            {"kind": "section", "label": "Settings:"},
            {"flag": "--duration", "label": "Duration (min):", "kind": "entry",
             "default": "", "placeholder": "60"},
            {"flag": "--interval", "label": "Interval (sec):", "kind": "entry",
             "default": "", "placeholder": "30"},
            {"flag": "--pattern", "label": "Comment pattern:", "kind": "entry",
             "default": "", "placeholder": "working on it"},
            {"flag": "--output", "label": "Output file:", "kind": "entry",
             "default": "", "placeholder": "~/pd-monitor-needs-attention.txt"},
            {"kind": "section", "label": "Options:"},
            {
                "kind": "row",
                "items": [
                    {"flag": "--dry-run", "label": "Dry run", "kind": "bool"},
                    {"flag": "--verbose", "label": "Verbose", "kind": "bool"},
                    {"flag": "--details", "label": "Show details", "kind": "bool"},
                ],
            },
        ],
    ),
    # ------------------------------------------------------------------
    # PD Merge
    # ------------------------------------------------------------------
    _ToolTab(
        tool_id="pd-merge",
        label="PD Merge",
        script="tools/pd-merge/pd_merge.py",
        description="Find & merge related PD incidents by job name",
        options=[
            {"kind": "info", "text":
             "Scenarios: A) same-day duplicates, B) cross-date with Jira, "
             "C) mass failure (10+ alerts), D) RDS export consolidation. "
             "Interactive per-group confirmation, skip list persisted."},
            {"kind": "section", "label": "Action:"},
            {
                "kind": "radio",
                "flag": "",
                "label": "action",
                "values": [
                    ("", "Merge incidents"),
                    ("--clear-skips", "Clear skip list"),
                    ("--show-skips", "Show skipped IDs"),
                ],
                "default": "",
                "tips": {
                    "": "Interactive merge workflow with per-group confirmation",
                    "--clear-skips": "Clear the saved skip list and exit",
                    "--show-skips": "Show currently skipped incident IDs and exit",
                },
            },
            {"kind": "section", "label": "Options:"},
            {
                "kind": "row",
                "items": [
                    {"flag": "--dry-run", "label": "Dry run", "kind": "bool"},
                    {"flag": "--verbose", "label": "Verbose", "kind": "bool"},
                ],
            },
        ],
    ),
    # ------------------------------------------------------------------
    # Data Freshness
    # ------------------------------------------------------------------
    _ToolTab(
        tool_id="freshness",
        label="Data Freshness",
        script="tools/freshness/freshness.py",
        description="DACSCAN data freshness report via Databricks SQL",
        options=[
            {"kind": "info", "text":
             "15-row report (DACSCAN, AGG, AUDIT, SUMMARY, BI-LOADER). "
             "Auto granular checks for delayed tables. "
             "SLA countdown (deadline 5:30 PM UTC)."},
            {"kind": "section", "label": "Scope:"},
            {
                "kind": "radio",
                "flag": "",
                "label": "scope",
                "values": [
                    ("", "Delayed tables only"),
                    ("--check-all", "All tables"),
                ],
                "default": "",
                "tips": {
                    "": "Run granular checks only for delayed tables",
                    "--check-all": "Run granular checks for ALL tables, not just delayed",
                },
            },
            {"kind": "section", "label": "Output:"},
            {
                "kind": "radio",
                "flag": "--format",
                "label": "format",
                "values": [
                    ("table", "Table"),
                    ("csv", "CSV"),
                    ("json", "JSON"),
                ],
                "default": "table",
            },
            {"kind": "section", "label": "Options:"},
            {
                "kind": "row",
                "items": [
                    {"flag": "--dry-run", "label": "Dry run", "kind": "bool"},
                    {"flag": "--verbose", "label": "Verbose", "kind": "bool"},
                    {"flag": "--report", "label": "HTML report", "kind": "bool"},
                ],
            },
        ],
    ),
    # ------------------------------------------------------------------
    # NOC Report  (shift-report Excel)
    # ------------------------------------------------------------------
    _ToolTab(
        tool_id="shift-report",
        label="NOC Report",
        script="tools/shift-report/shift_report.py",
        description="Sync Jira statuses into shift report (Excel, local mode)",
        options=[
            {"kind": "info", "text":
             "Start shift: copy tickets from previous shift, update date, sync Jira. "
             "Sync: update column E statuses. "
             "Add row: insert new ticket with Jira + Slack links."},
            {"kind": "section", "label": "File:"},
            {"flag": "--file", "label": "Excel path:", "kind": "entry",
             "default": "", "placeholder": "~/Downloads/NOC endshift report.xlsx"},
            {"kind": "section", "label": "Options:"},
            {
                "kind": "row",
                "items": [
                    {"flag": "--dry-run", "label": "Dry run", "kind": "bool"},
                    {"flag": "--verbose", "label": "Verbose", "kind": "bool"},
                ],
            },
        ],
    ),
    # ------------------------------------------------------------------
    # GSheet Report
    # ------------------------------------------------------------------
    _ToolTab(
        tool_id="gsheet-report",
        label="GSheet Report",
        script="tools/shift-report/gsheet_report.py",
        description="Shift report sync (Google Sheets, online mode)",
        options=[
            {"kind": "info", "text":
             "Reads/writes the shift report directly in Google Sheets "
             "via Apps Script Web App. No file downloads needed. "
             "Requires GSHEET_WEBAPP_URL + GSHEET_API_KEY in config."},
            {"kind": "section", "label": "Options:"},
            {
                "kind": "row",
                "items": [
                    {"flag": "--dry-run", "label": "Dry run", "kind": "bool"},
                    {"flag": "--verbose", "label": "Verbose", "kind": "bool"},
                ],
            },
        ],
    ),
    # ------------------------------------------------------------------
    # PD Escalate
    # ------------------------------------------------------------------
    _ToolTab(
        tool_id="pd-escalate",
        label="PD Escalate",
        script="tools/pd-escalate/pd_escalate.py",
        description="Automate post-DSSD escalation workflow",
        options=[
            {"kind": "info", "text":
             "Auto-detects DRGN from PD incident. Creates Jira link "
             "DRGN \"is blocked by\" DSSD, transitions DRGN to Escalated, "
             "posts PD note, prints Slack template for #cds-ops-24x7-int."},
            {"kind": "section", "label": "Tickets:"},
            {"flag": "--pd", "label": "PD incident:", "kind": "entry",
             "default": "", "placeholder": "ID or URL (required)"},
            {"flag": "--dssd", "label": "DSSD ticket:", "kind": "entry",
             "default": "", "placeholder": "e.g. DSSD-29386 (required)"},
            {"flag": "--drgn", "label": "DRGN ticket:", "kind": "entry",
             "default": "", "placeholder": "optional, auto-detected from PD"},
            {"kind": "section", "label": "Options:"},
            {"flag": "--dry-run", "label": "Dry run", "kind": "bool"},
        ],
    ),
    # ------------------------------------------------------------------
    # PD Resolve
    # ------------------------------------------------------------------
    _ToolTab(
        tool_id="pd-resolve",
        label="PD Resolve",
        script="tools/pd-resolve/pd_resolve.py",
        description="Auto-resolve PD incidents where Airflow recovered",
        options=[
            {"kind": "info", "text":
             "Extracts DAG name from PD title, checks Airflow (MWAA) for "
             "recent success runs, finds DRGN ticket, searches Confluence "
             "for runbook. Closes DRGN + resolves PD with summary note."},
            {"kind": "section", "label": "Input:"},
            {"flag": "", "label": "Incident:", "kind": "entry",
             "default": "", "placeholder": "PD URL or ID (interactive if empty)"},
            {"kind": "section", "label": "Options:"},
            {
                "kind": "row",
                "items": [
                    {"flag": "--dry-run", "label": "Dry run", "kind": "bool"},
                    {"flag": "--verbose", "label": "Verbose", "kind": "bool"},
                    {"flag": "--no-confirm", "label": "Skip confirmation", "kind": "bool"},
                ],
            },
        ],
    ),
    # ------------------------------------------------------------------
    # Ticket Watch
    # ------------------------------------------------------------------
    _ToolTab(
        tool_id="ticket-watch",
        label="Ticket Watch",
        script="tools/ticket-watch/ticket_watch.py",
        description="Monitor escalation tickets for unassigned/stale states",
        options=[
            {"kind": "info", "text":
             "Flags unassigned tickets (4+ hours). Pings assignees on "
             "stale tickets (3+ days without comment). Tracks repeat pings "
             "with last assignee response."},
            {"kind": "section", "label": "Mode:"},
            {
                "kind": "radio",
                "flag": "",
                "label": "tw_mode",
                "values": [
                    ("", "Standard watch"),
                    ("--chicken-curry", "Stale tickets for user"),
                ],
                "default": "",
                "tips": {
                    "": "Check unassigned + stale tickets in configured project",
                    "--chicken-curry": "Search all projects for stale tickets of a specific user",
                },
            },
            {"kind": "section", "label": "Settings:"},
            {"flag": "--project", "label": "Project key:", "kind": "entry",
             "default": "", "placeholder": "DSSD"},
            {"kind": "section", "label": "Options:"},
            {
                "kind": "row",
                "items": [
                    {"flag": "--dry-run", "label": "Dry run", "kind": "bool"},
                    {"flag": "--no-comment", "label": "Skip Jira comments", "kind": "bool"},
                ],
            },
        ],
    ),
]


# ═══════════════════════════════════════════════════════════════════════════
# Main application window
# ═══════════════════════════════════════════════════════════════════════════

class NOCToolkitGUI(ctk.CTk):
    """Main GUI window for the NOC Toolkit."""

    def __init__(self) -> None:
        super().__init__()

        self.title(f"NOC Toolkit v{VERSION}")
        self.geometry("1060x760")
        self.minsize(860, 600)
        self.configure(fg_color=_BG)

        # Config
        self._config: Dict[str, str] = self._load_config()

        # Per-tab process state (populated in _build_tabs / _populate_tab)
        self._tab_states: Dict[str, _TabState] = {}
        # Single queue shared by all reader threads: (tool_id, line_or_None)
        self._master_queue: queue.Queue[Tuple[str, Optional[str]]] = queue.Queue()

        # Widget references for each tab's options
        self._tab_widgets: Dict[str, List[Tuple[Dict[str, Any], Any]]] = {}

        # Config editor: key → StringVar (populated by _build_config_tab and _add_tool_config_entries)
        self._cfg_vars: Dict[str, ctk.StringVar] = {}

        self._build_ui()
        self._log("Ready. Select a tool tab and configure options, then click Launch.\n")
        self._poll_output()

    # ------------------------------------------------------------------
    # Config loading (mirrors NOCToolkit._load_config)
    # ------------------------------------------------------------------

    def _load_config(self) -> Dict[str, str]:
        """Load config.yaml once at startup, identical to CLI launcher."""
        config_path = _EXE_DIR / "config.yaml"
        config_str = str(config_path) if config_path.is_file() else None

        try:
            env_refs = extract_env_references(config_str)

            if env_refs and _DOTENV_AVAILABLE:
                env_file = _EXE_DIR / ".env"
                if env_file.is_file():
                    all_dotenv: Dict[str, Optional[str]] = dotenv_values(env_file)
                    for var_name in env_refs:
                        dotenv_val = all_dotenv.get(var_name)
                        if var_name not in os.environ and dotenv_val is not None:
                            os.environ[var_name] = dotenv_val

            return load_config(config_str)
        except Exception as exc:
            logger.warning("Failed to load config.yaml: %s", exc)
            return {}

    def _tool_env(self) -> Dict[str, str]:
        """Build subprocess environment with config overlay."""
        env = os.environ.copy()
        for key, value in self._config.items():
            if key not in env or not env[key]:
                env[key] = value
        return env

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        """Assemble all widgets."""
        self.grid_columnconfigure(0, weight=0)  # sidebar
        self.grid_columnconfigure(1, weight=1)  # main content
        self.grid_rowconfigure(0, weight=0)      # header
        self.grid_rowconfigure(1, weight=1)      # sidebar + paned area

        self._build_header()       # row 0, spans both columns
        self._build_tabs()         # row 1: col 0 sidebar, col 1 paned
        self._bind_mousewheel()

    # --- Header ---

    def _build_header(self) -> None:
        """Top bar: version + config status + reload button."""
        header = ctk.CTkFrame(self, fg_color=_BG, corner_radius=0)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=15, pady=(10, 2))
        header.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            header,
            text=f"NOC Toolkit v{VERSION}",
            font=_FONT_HEADER,
            text_color=_FG,
        ).grid(row=0, column=0, sticky="w")

        self._cfg_status_label = ctk.CTkLabel(
            header, text="", font=_FONT, text_color=_GREEN,
        )
        self._cfg_status_label.grid(row=0, column=1, sticky="w", padx=(30, 0))
        self._update_config_status()

        self._cfg_button = ctk.CTkButton(
            header, text="Config", font=_FONT, width=70, height=24,
            corner_radius=4, fg_color=_BG_PANEL, hover_color=_BLUE,
            text_color="#e8a838", command=self._on_toggle_config,
        )
        self._cfg_button.grid(row=0, column=2, sticky="e", padx=(0, 6))

        ctk.CTkButton(
            header, text="Reload", font=_FONT, width=70, height=24,
            corner_radius=4, fg_color=_BG_PANEL, hover_color=_BLUE,
            text_color=_FG, command=self._on_reload_config,
        ).grid(row=0, column=3, sticky="e")

    def _update_config_status(self) -> None:
        """Refresh the config status label text and colour."""
        config_count = len(self._config)
        if config_count:
            text = f"Config: [OK] Loaded: config.yaml ({config_count} keys)"
            color = _GREEN
        else:
            text = "Config: [MISSING] config.yaml not found"
            color = "#e8a838"
        self._cfg_status_label.configure(text=text, text_color=color)

    # --- Tab buttons + paned content / log ---

    def _build_tabs(self) -> None:
        """Vertical sidebar + PanedWindow (tab content | buttons+log)."""
        import tkinter as tk

        # --- Left sidebar with vertical tab buttons ---
        sidebar = ctk.CTkFrame(self, fg_color=_BG_PANEL, corner_radius=6, width=130)
        sidebar.grid(row=1, column=0, sticky="ns", padx=(15, 4), pady=(5, 10))
        sidebar.grid_propagate(False)

        self._tab_buttons: List[ctk.CTkButton] = []
        self._tab_frames: Dict[str, ctk.CTkScrollableFrame] = {}
        self._active_tab: str = TOOL_TABS[0].label

        for idx, tool_tab in enumerate(TOOL_TABS):
            btn = ctk.CTkButton(
                sidebar,
                text=tool_tab.label,
                font=_FONT,
                width=118,
                height=28,
                corner_radius=4,
                anchor="w",
                fg_color=_BLUE if idx == 0 else "transparent",
                hover_color=_BLUE,
                text_color=_FG,
                command=lambda lbl=tool_tab.label: self._switch_tab(lbl),
            )
            btn.pack(fill="x", padx=4, pady=(4 if idx == 0 else 1, 0))
            self._tab_buttons.append(btn)

        # Track the last tool tab so Config toggle can return to it
        self._last_tool_tab: str = TOOL_TABS[0].label

        # --- Right side: vertical PanedWindow (tab content | buttons+log) ---
        paned = tk.PanedWindow(
            self, orient=tk.VERTICAL, sashwidth=6, sashrelief=tk.FLAT,
            bg="#3a3a50", borderwidth=0,
        )
        paned.grid(row=1, column=1, sticky="nsew", padx=(0, 15), pady=(5, 10))

        # Top pane: tab content container
        self._tab_container = ctk.CTkFrame(paned, fg_color=_BG_PANEL, corner_radius=6)
        self._tab_container.grid_columnconfigure(0, weight=1)
        self._tab_container.grid_rowconfigure(0, weight=1)
        paned.add(self._tab_container, minsize=120, stretch="always")

        # Bottom pane: buttons + log
        bottom_pane = ctk.CTkFrame(paned, fg_color=_BG, corner_radius=0)
        bottom_pane.grid_columnconfigure(0, weight=1)
        bottom_pane.grid_rowconfigure(1, weight=1)
        paned.add(bottom_pane, minsize=100, stretch="always")

        # Buttons inside bottom pane
        self._build_buttons(bottom_pane)

        # Log inside bottom pane
        self._log_box = ctk.CTkTextbox(
            bottom_pane, font=(_MONO, 12), state="disabled", wrap="word",
            fg_color=_BG_PANEL, text_color=_FG, corner_radius=4,
        )
        self._log_box.grid(row=1, column=0, sticky="nsew", padx=0, pady=(2, 0))

        # Input bar inside bottom pane
        self._build_input_bar(bottom_pane)

        # Build a frame for each tool
        for tool_tab in TOOL_TABS:
            frame = ctk.CTkScrollableFrame(
                self._tab_container, fg_color="transparent",
            )
            frame.grid_columnconfigure(1, weight=1)
            self._populate_tab(frame, tool_tab)
            self._tab_frames[tool_tab.label] = frame

        # Build config editor frame
        self._build_config_tab()

        # Show first tab
        self._tab_frames[TOOL_TABS[0].label].grid(row=0, column=0, sticky="nsew", padx=5, pady=5)

    def _switch_tab(self, label: str) -> None:
        """Show the selected tab, hide the rest."""
        if label == self._active_tab:
            return

        # Hide current
        self._tab_frames[self._active_tab].grid_forget()

        # Show new
        self._tab_frames[label].grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        self._active_tab = label

        # Remember last tool tab (not Config) for toggle-back
        if label != "Config":
            self._last_tool_tab = label

        # Update sidebar button colours
        for btn in self._tab_buttons:
            if btn.cget("text") == label:
                btn.configure(fg_color=_BLUE)
            else:
                btn.configure(fg_color="transparent")

        # Update header Config button highlight
        if label == "Config":
            self._cfg_button.configure(fg_color=_BLUE, text_color="white")
        else:
            self._cfg_button.configure(fg_color=_BG_PANEL, text_color="#e8a838")

        # Swap log display to the new tab's buffer
        self._repaint_logbox(label)

    # --- Config editor tab ---

    def _read_raw_yaml(self) -> Dict[str, Any]:
        """Read config.yaml into a raw dict (empty if missing/invalid)."""
        config_path = _EXE_DIR / "config.yaml"
        if not config_path.is_file():
            return {}
        with open(config_path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
        return raw if isinstance(raw, dict) else {}

    def _build_config_tab(self) -> None:
        """Build the Config tab showing shared/global keys from CONFIG_KEYS."""
        frame = ctk.CTkScrollableFrame(self._tab_container, fg_color="transparent")
        frame.grid_columnconfigure(1, weight=1)

        # Read current values from file
        raw = self._read_raw_yaml()

        # Header
        ctk.CTkLabel(
            frame, text="Edit config.yaml — Shared Credentials & Settings",
            font=_FONT_BOLD, text_color=_FG,
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=5, pady=(5, 0))

        config_path = _EXE_DIR / "config.yaml"
        ctk.CTkLabel(
            frame, text=str(config_path), font=(_MONO, 11), text_color=_FG_DIM,
        ).grid(row=1, column=0, columnspan=3, sticky="w", padx=5, pady=(0, 4))

        ctk.CTkLabel(
            frame,
            text="Tool-specific settings are on each tool's tab under \"Config:\"",
            font=(_MONO, 11), text_color=_FG_DIM,
        ).grid(row=2, column=0, columnspan=3, sticky="w", padx=5, pady=(0, 8))

        row_idx = 3
        current_section = ""

        # Show only shared keys (tool=None) on the Config tab
        shared_keys = [ck for ck in CONFIG_KEYS if ck.tool is None]

        for ck in shared_keys:
            # Section header
            if ck.section != current_section:
                current_section = ck.section
                ctk.CTkLabel(
                    frame, text=f"{current_section}:", font=_FONT_BOLD, text_color=_FG,
                ).grid(row=row_idx, column=0, columnspan=3, sticky="w", padx=5, pady=(10, 2))
                row_idx += 1

            # Current value from YAML (could be in any section)
            current_val = ""
            section_data = raw.get(ck.section, {})
            if isinstance(section_data, dict):
                v = section_data.get(ck.key)
                if v is not None:
                    current_val = str(v)

            row_idx = self._add_cfg_entry_row(frame, row_idx, ck, current_val)

        # Save / Reload buttons
        row_idx = self._add_cfg_save_buttons(frame, row_idx)

        self._tab_frames["Config"] = frame

    def _add_cfg_entry_row(
        self,
        parent: ctk.CTkScrollableFrame,
        row_idx: int,
        ck: _CfgKey,
        current_val: str,
    ) -> int:
        """Add a single config key editor row. Returns the next row index."""
        # Label
        ctk.CTkLabel(
            parent, text=ck.label, font=_FONT, text_color=_FG,
        ).grid(row=row_idx, column=0, sticky="w", padx=(15, 5), pady=2)

        # Entry
        entry_var = ctk.StringVar(value=current_val)
        entry = ctk.CTkEntry(
            parent, textvariable=entry_var, font=_FONT,
            fg_color=_BG_INPUT, text_color=_FG, width=400,
            placeholder_text=ck.default,
        )
        entry.grid(row=row_idx, column=1, sticky="ew", padx=5, pady=2)

        # Mask secrets
        if ck.secret:
            entry.configure(show="*")
            show_var = ctk.BooleanVar(value=False)

            def _make_toggle(ent: ctk.CTkEntry, svar: ctk.BooleanVar):
                def _toggle() -> None:
                    ent.configure(show="" if svar.get() else "*")
                return _toggle

            ctk.CTkCheckBox(
                parent, text="show", variable=show_var, font=(_MONO, 11),
                text_color=_FG_DIM, checkbox_width=16, checkbox_height=16,
                command=_make_toggle(entry, show_var),
            ).grid(row=row_idx, column=2, padx=5, pady=2)

        self._cfg_vars[ck.key] = entry_var
        row_idx += 1

        # Description line
        if ck.desc:
            ctk.CTkLabel(
                parent, text=ck.desc, font=(_MONO, 11),
                text_color=_FG_DIM, anchor="w",
            ).grid(row=row_idx, column=0, columnspan=3, sticky="w", padx=(15, 5), pady=(0, 2))
            row_idx += 1

        return row_idx

    def _add_cfg_save_buttons(self, parent: ctk.CTkScrollableFrame, row_idx: int) -> int:
        """Add Save + Reload buttons at the given row. Returns the next row."""
        btn_row = ctk.CTkFrame(parent, fg_color="transparent")
        btn_row.grid(row=row_idx, column=0, columnspan=3, sticky="w", padx=5, pady=(12, 5))

        ctk.CTkButton(
            btn_row, text="Save config.yaml", font=_FONT_BOLD, width=160, height=30,
            fg_color=_GREEN, hover_color="#258f5f", text_color="white",
            command=self._on_save_config,
        ).pack(side="left", padx=(0, 10))

        ctk.CTkButton(
            btn_row, text="Reload", font=_FONT_BOLD, width=100, height=30,
            fg_color=_BLUE, hover_color="#2d5594", text_color="white",
            command=self._on_reload_config,
        ).pack(side="left")

        self._cfg_save_status = ctk.CTkLabel(
            btn_row, text="", font=(_MONO, 11), text_color=_GREEN,
        )
        self._cfg_save_status.pack(side="left", padx=(10, 0))

        return row_idx + 1

    def _add_tool_config_entries(
        self,
        parent: ctk.CTkScrollableFrame,
        tool_id: str,
        row_idx: int,
    ) -> int:
        """Add tool-specific config entries to a tool tab. Returns next row."""
        tool_keys = _TOOL_CFG_KEYS.get(tool_id, [])
        if not tool_keys:
            return row_idx

        raw = self._read_raw_yaml()

        ctk.CTkLabel(
            parent, text="Config:", font=_FONT_BOLD, text_color=_FG, anchor="w",
        ).grid(row=row_idx, column=0, columnspan=2, sticky="w", padx=5, pady=(8, 2))
        row_idx += 1

        for ck in tool_keys:
            current_val = ""
            section_data = raw.get(ck.section, {})
            if isinstance(section_data, dict):
                v = section_data.get(ck.key)
                if v is not None:
                    current_val = str(v)
            row_idx = self._add_cfg_entry_row(parent, row_idx, ck, current_val)

        # Per-tool save button
        save_btn_frame = ctk.CTkFrame(parent, fg_color="transparent")
        save_btn_frame.grid(row=row_idx, column=0, columnspan=2, sticky="w", padx=5, pady=(4, 2))

        ctk.CTkButton(
            save_btn_frame, text="Save", font=_FONT, width=80, height=24,
            corner_radius=4, fg_color=_GREEN, hover_color="#258f5f",
            text_color="white", command=self._on_save_config,
        ).pack(side="left", padx=(10, 0))

        return row_idx + 1

    def _on_save_config(self) -> None:
        """Write ALL config entry values back to config.yaml."""
        config_path = _EXE_DIR / "config.yaml"

        # Rebuild YAML structure from the registry + current var values
        output: Dict[str, Dict[str, Any]] = {}
        for ck in CONFIG_KEYS:
            var = self._cfg_vars.get(ck.key)
            if var is None:
                continue
            value = var.get().strip()
            if not value:
                continue  # omit empty keys
            if ck.section not in output:
                output[ck.section] = {}
            # Try to preserve numeric types for warehouse IDs etc.
            if value.isdigit():
                output[ck.section][ck.key] = int(value)
            else:
                output[ck.section][ck.key] = value

        try:
            # Write with a header comment
            header = (
                "# NOC Toolkit — config.yaml\n"
                "# Managed by NOC Toolkit GUI. See config.yaml.example for reference.\n"
                "# IMPORTANT: Never commit this file with real credentials!\n\n"
            )
            with open(config_path, "w", encoding="utf-8") as fh:
                fh.write(header)
                yaml.dump(
                    output, fh, default_flow_style=False,
                    allow_unicode=True, sort_keys=False,
                )
            self._log(f"Config saved to {config_path}\n")
            self._cfg_save_status.configure(text="Saved", text_color=_GREEN)
            # Auto-reload after save
            self._on_reload_config()
        except Exception as exc:
            self._log(f"Failed to save config: {exc}\n")
            self._cfg_save_status.configure(text=f"Error: {exc}", text_color=_RED)

    def _on_toggle_config(self) -> None:
        """Toggle between Config tab and the last-used tool tab."""
        if self._active_tab == "Config":
            self._switch_tab(self._last_tool_tab)
        else:
            self._switch_tab("Config")

    def _on_reload_config(self) -> None:
        """Hot-reload config.yaml without restarting."""
        self._config = self._load_config()
        self._update_config_status()
        config_count = len(self._config)
        self._log(f"Config reloaded: {config_count} keys\n")

        # Refresh all config entry vars from file
        raw = self._read_raw_yaml()
        for ck in CONFIG_KEYS:
            var = self._cfg_vars.get(ck.key)
            if var is None:
                continue
            section_data = raw.get(ck.section, {})
            if isinstance(section_data, dict):
                v = section_data.get(ck.key)
                var.set(str(v) if v is not None else "")
            else:
                var.set("")

    # --- Tab content population ---

    def _populate_tab(self, parent: ctk.CTkScrollableFrame, tool_tab: _ToolTab) -> None:
        """Fill a single tool tab matching the screenshot layout."""
        widgets: List[Tuple[Dict[str, Any], Any]] = []

        # Tool description
        ctk.CTkLabel(
            parent, text=tool_tab.description, font=_FONT,
            text_color=_FG_DIM, anchor="w",
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=5, pady=(5, 0))

        # Status line
        status_color = _GREEN if tool_tab.script_path.exists() else _RED
        status_text = "Status: Available" if tool_tab.script_path.exists() else "Status: Script not found"
        ctk.CTkLabel(
            parent, text=status_text, font=_FONT,
            text_color=status_color, anchor="w",
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=5, pady=(0, 8))

        row_idx = 2
        for opt in tool_tab.options:
            kind = opt["kind"]

            if kind == "info":
                ctk.CTkLabel(
                    parent, text=opt["text"], font=(_MONO, 11),
                    text_color=_FG_DIM, anchor="w", wraplength=700, justify="left",
                ).grid(row=row_idx, column=0, columnspan=2, sticky="w", padx=5, pady=(0, 4))

            elif kind == "section":
                ctk.CTkLabel(
                    parent, text=opt["label"], font=_FONT_BOLD,
                    text_color=_FG, anchor="w",
                ).grid(row=row_idx, column=0, columnspan=2, sticky="w", padx=5, pady=(8, 2))

            elif kind == "radio":
                radio_var = ctk.StringVar(value=opt["default"])
                radio_frame = ctk.CTkFrame(parent, fg_color="transparent")
                radio_frame.grid(row=row_idx, column=0, columnspan=2, sticky="w", padx=5, pady=2)
                for val, display in opt["values"]:
                    rb = ctk.CTkRadioButton(
                        radio_frame, text=display, variable=radio_var,
                        value=val, font=_FONT, text_color=_FG,
                        radiobutton_width=18, radiobutton_height=18,
                    )
                    rb.pack(side="left", padx=(0, 20))
                # Show dynamic tip below radio group when tips are provided
                tips = opt.get("tips")
                if tips:
                    row_idx += 1
                    tip_label = ctk.CTkLabel(
                        parent, text=tips.get(opt["default"], ""),
                        font=(_MONO, 11), text_color=_FG_DIM,
                        anchor="w", wraplength=700, justify="left",
                    )
                    tip_label.grid(row=row_idx, column=0, columnspan=2, sticky="w", padx=20, pady=(0, 2))

                    def _make_tip_updater(var: ctk.StringVar, lbl: ctk.CTkLabel,
                                          tip_map: Dict[str, str]):
                        def _update(*_args: Any) -> None:
                            lbl.configure(text=tip_map.get(var.get(), ""))
                        return _update

                    radio_var.trace_add("write", _make_tip_updater(radio_var, tip_label, tips))
                widgets.append((opt, radio_var))

            elif kind == "bool":
                checkbox_var = ctk.BooleanVar(value=False)
                ctk.CTkCheckBox(
                    parent, text=opt["label"], variable=checkbox_var,
                    font=_FONT, text_color=_FG,
                    checkbox_width=18, checkbox_height=18,
                ).grid(row=row_idx, column=0, columnspan=2, sticky="w", padx=5, pady=2)
                widgets.append((opt, checkbox_var))

            elif kind == "entry":
                ctk.CTkLabel(
                    parent, text=opt["label"], font=_FONT, text_color=_FG,
                ).grid(row=row_idx, column=0, sticky="w", padx=5, pady=2)
                entry_var = ctk.StringVar(value=opt.get("default", ""))
                ctk.CTkEntry(
                    parent, textvariable=entry_var, width=240, font=_FONT,
                    fg_color=_BG_INPUT, text_color=_FG,
                    placeholder_text=opt.get("placeholder", ""),
                ).grid(row=row_idx, column=1, sticky="w", padx=5, pady=2)
                widgets.append((opt, entry_var))

            elif kind == "choice":
                ctk.CTkLabel(
                    parent, text=opt["label"], font=_FONT, text_color=_FG,
                ).grid(row=row_idx, column=0, sticky="w", padx=5, pady=2)
                combo_var = ctk.StringVar(value=opt.get("default", opt["values"][0]))
                ctk.CTkComboBox(
                    parent, values=opt["values"], variable=combo_var,
                    width=180, font=_FONT, fg_color=_BG_INPUT,
                    text_color=_FG,
                ).grid(row=row_idx, column=1, sticky="w", padx=5, pady=2)
                widgets.append((opt, combo_var))

            elif kind == "row":
                row_frame = ctk.CTkFrame(parent, fg_color="transparent")
                row_frame.grid(row=row_idx, column=0, columnspan=2, sticky="w", padx=5, pady=2)
                for sub_opt in opt["items"]:
                    sub_var = ctk.BooleanVar(value=False)
                    ctk.CTkCheckBox(
                        row_frame, text=sub_opt["label"], variable=sub_var,
                        font=_FONT, text_color=_FG,
                        checkbox_width=18, checkbox_height=18,
                    ).pack(side="left", padx=(0, 20))
                    widgets.append((sub_opt, sub_var))

            row_idx += 1

        # Tool-specific config entries (non-secret, tool-owned keys)
        row_idx = self._add_tool_config_entries(parent, tool_tab.tool_id, row_idx)

        self._tab_widgets[tool_tab.tool_id] = widgets

        # Per-tab Launch / Stop buttons and status indicator
        self._add_tab_launch_buttons(parent, tool_tab, row_idx)

    def _add_tab_launch_buttons(
        self,
        parent: ctk.CTkScrollableFrame,
        tool_tab: "_ToolTab",
        row_idx: int,
    ) -> None:
        """Add per-tab Launch / Stop buttons and a running-status label."""
        state = self._tab_states.setdefault(tool_tab.tool_id, _TabState())

        btn_frame = ctk.CTkFrame(parent, fg_color="transparent")
        btn_frame.grid(
            row=row_idx, column=0, columnspan=2, sticky="w", padx=5, pady=(12, 5),
        )

        state.launch_btn = ctk.CTkButton(
            btn_frame, text="Launch", font=_FONT_BOLD, width=110, height=30,
            fg_color=_GREEN, hover_color="#258f5f", text_color="white",
            command=lambda tid=tool_tab.tool_id: self._on_launch(tid),
        )
        state.launch_btn.pack(side="left", padx=(0, 8))

        state.stop_btn = ctk.CTkButton(
            btn_frame, text="Stop", font=_FONT_BOLD, width=110, height=30,
            fg_color=_RED, hover_color="#b52b27", text_color="white",
            command=lambda tid=tool_tab.tool_id: self._on_stop(tid),
            state="disabled",
        )
        state.stop_btn.pack(side="left", padx=(0, 12))

        state.status_label = ctk.CTkLabel(
            btn_frame, text="", font=_FONT, text_color=_GREEN,
        )
        state.status_label.pack(side="left")

    # --- Action buttons (shared: Clear Log + Output label) ---

    def _build_buttons(self, parent: ctk.CTkFrame) -> None:
        """Clear Log button + Output label (shared across tabs)."""
        btn_frame = ctk.CTkFrame(parent, fg_color=_BG, corner_radius=0)
        btn_frame.grid(row=0, column=0, sticky="ew", padx=0, pady=(0, 2))

        ctk.CTkButton(
            btn_frame, text="Clear Log", font=_FONT_BOLD, width=110, height=30,
            fg_color=_BLUE, hover_color="#2d5594", text_color="white",
            command=self._on_clear_log,
        ).pack(side="left", padx=(0, 12))

        self._output_label = ctk.CTkLabel(
            btn_frame, text="Output", font=_FONT, text_color=_FG_DIM,
        )
        self._output_label.pack(side="left")

    # --- Input bar ---

    def _build_input_bar(self, parent: ctk.CTkFrame) -> None:
        """Bottom input field + Send button for interactive tools."""
        bar = ctk.CTkFrame(parent, fg_color=_BG, corner_radius=0)
        bar.grid(row=2, column=0, sticky="ew", padx=0, pady=(0, 4))
        bar.grid_columnconfigure(0, weight=1)

        self._input_var = ctk.StringVar()
        self._input_entry = ctk.CTkEntry(
            bar, textvariable=self._input_var, font=_FONT,
            fg_color=_BG_INPUT, text_color=_FG,
            placeholder_text="Type input for interactive tools and press Enter...",
        )
        self._input_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._input_entry.bind("<Return>", lambda _e: self._on_send_input())

        ctk.CTkButton(
            bar, text="Send", font=_FONT_BOLD, width=80, height=30,
            fg_color=_BLUE, hover_color="#2d5594", text_color="white",
            command=self._on_send_input,
        ).grid(row=0, column=1)

    # --- Mouse-wheel scrolling ---

    def _bind_mousewheel(self) -> None:
        """Enable mouse-wheel / trackpad scrolling for tab frames.

        Tk 9.0 on macOS aqua does not generate ``<MouseWheel>`` events,
        so we use pyobjc ``NSEvent.addLocalMonitorForEventsMatchingMask``
        to capture native Cocoa scroll-wheel events and forward them to
        the active tab's canvas.  Falls back to Tk bindings on Linux/Win.
        """
        import platform
        system = platform.system()

        def _scroll_active(delta: int) -> None:
            frame = self._tab_frames.get(self._active_tab)
            if frame is not None and isinstance(frame, ctk.CTkScrollableFrame):
                frame._parent_canvas.yview_scroll(delta, "units")  # noqa: SLF001

        if system == "Darwin":
            try:
                from AppKit import NSEvent  # type: ignore[import-untyped]

                _MAX_SCROLL = 8  # clamp large trackpad momentum deltas

                def _native_scroll(event):
                    dy = event.scrollingDeltaY()
                    if abs(dy) > 0.1:
                        units = int(-1 * dy)
                        if units == 0:
                            units = -1 if dy > 0 else 1
                        units = max(-_MAX_SCROLL, min(_MAX_SCROLL, units))
                        _scroll_active(units)
                    return event

                # NSEventMaskScrollWheel = 1 << 22
                NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                    1 << 22, _native_scroll,
                )
            except ImportError:
                # pyobjc not installed — try Tk binding as fallback
                self.bind_all(
                    "<MouseWheel>",
                    lambda e: _scroll_active(int(-1 * e.delta)),
                    add="+",
                )
        elif system == "Linux":
            self.bind_all("<Button-4>", lambda _e: _scroll_active(-3), add="+")
            self.bind_all("<Button-5>", lambda _e: _scroll_active(3), add="+")
        else:
            self.bind_all(
                "<MouseWheel>",
                lambda e: _scroll_active(int(-1 * (e.delta // 120))),
                add="+",
            )

    # ------------------------------------------------------------------
    # Command building
    # ------------------------------------------------------------------

    def _current_tool_tab(self) -> _ToolTab:
        """Return the _ToolTab for the currently selected tab."""
        for tool_tab in TOOL_TABS:
            if tool_tab.label == self._active_tab:
                return tool_tab
        return TOOL_TABS[0]

    def _label_to_tool_id(self, label: str) -> Optional[str]:
        """Map a tab label to its tool_id (None for Config tab)."""
        for tool_tab in TOOL_TABS:
            if tool_tab.label == label:
                return tool_tab.tool_id
        return None

    def _tool_tab_by_id(self, tool_id: str) -> Optional[_ToolTab]:
        """Look up a _ToolTab by its tool_id."""
        for tool_tab in TOOL_TABS:
            if tool_tab.tool_id == tool_id:
                return tool_tab
        return None

    def _build_command(self, tool_tab: _ToolTab) -> List[str]:
        """Build the subprocess argv from the active tab's widget values."""
        cmd: List[str] = [sys.executable, str(tool_tab.script_path)]
        widgets = self._tab_widgets.get(tool_tab.tool_id, [])

        for opt, var in widgets:
            kind = opt["kind"]
            flag = opt.get("flag", "")

            if kind == "bool":
                if var.get():
                    cmd.append(flag)

            elif kind == "radio":
                value = var.get()
                if not value:
                    continue
                if value.startswith("--"):
                    cmd.append(value)
                elif flag:
                    cmd.extend([flag, value])
                else:
                    cmd.append(value)

            elif kind == "choice":
                value = var.get()
                default = opt.get("default", opt.get("values", [""])[0])
                if not value or value == default:
                    continue
                if value.startswith("--"):
                    cmd.append(value)
                elif flag:
                    cmd.extend([flag, value])
                else:
                    cmd.append(value)

            elif kind == "entry":
                value = var.get().strip()
                if value:
                    if flag:
                        cmd.extend([flag, value])
                    else:
                        cmd.append(value)

        return cmd

    # ------------------------------------------------------------------
    # Process management (per-tab parallel execution)
    # ------------------------------------------------------------------

    def _on_launch(self, tool_id: str) -> None:
        """Start a tool as a subprocess (each tab runs independently)."""
        state = self._tab_states.get(tool_id)
        if state is None:
            return

        if state.process is not None and state.process.poll() is None:
            self._log_to_tab(tool_id, "Already running. Stop it first.\n")
            return

        tool_tab = self._tool_tab_by_id(tool_id)
        if tool_tab is None:
            return

        if not tool_tab.script_path.exists():
            self._log_to_tab(tool_id, f"Script not found: {tool_tab.script_path}\n")
            return

        cmd = self._build_command(tool_tab)
        env = self._tool_env()
        env["PYTHONPATH"] = str(COMMON_DIR) + os.pathsep + env.get("PYTHONPATH", "")

        # Prepare log file
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        log_path = LOGS_DIR / f"{tool_id}_{timestamp}.log"
        state.log_fh = open(log_path, "w", encoding="utf-8")  # noqa: SIM115

        # Clear previous buffer
        state.log_buffer.clear()

        self._log_to_tab(tool_id, f"--- Launching {tool_tab.label} ---\n")
        self._log_to_tab(tool_id, f"$ {' '.join(cmd)}\n")
        self._log_to_tab(tool_id, f"Log file: {log_path}\n\n")

        try:
            state.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=str(tool_tab.script_path.parent),
                env=env,
            )
            state.stdin_pipe = state.process.stdin
        except FileNotFoundError as exc:
            self._log_to_tab(tool_id, f"Failed to start: {exc}\n")
            if state.log_fh:
                state.log_fh.close()
                state.log_fh = None
            return

        if state.launch_btn:
            state.launch_btn.configure(state="disabled")
        if state.stop_btn:
            state.stop_btn.configure(state="normal")
        if state.status_label:
            state.status_label.configure(text="Running...", text_color="#e8a838")

        reader = threading.Thread(
            target=self._read_process_output,
            args=(tool_id,),
            daemon=True,
            name=f"gui-reader-{tool_id}",
        )
        reader.start()

    def _on_stop(self, tool_id: str) -> None:
        """Terminate a running subprocess for a specific tool."""
        state = self._tab_states.get(tool_id)
        if state is None:
            return

        if state.process is not None and state.process.poll() is None:
            state.process.terminate()
            try:
                state.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                state.process.kill()
            self._log_to_tab(tool_id, "\n--- Stopped ---\n")

        state.stdin_pipe = None
        self._close_tab_log(tool_id)
        self._reset_tab_buttons(tool_id)

    def _on_clear_log(self) -> None:
        """Clear the active tab's log buffer and the log display."""
        tool_id = self._label_to_tool_id(self._active_tab)
        if tool_id:
            state = self._tab_states.get(tool_id)
            if state:
                state.log_buffer.clear()
        self._log_box.configure(state="normal")
        self._log_box.delete("1.0", "end")
        self._log_box.configure(state="disabled")

    def _on_send_input(self) -> None:
        """Send text from the input bar to the active tab's running process."""
        text = self._input_var.get()
        if not text:
            return
        tool_id = self._label_to_tool_id(self._active_tab)
        if tool_id is None:
            return
        state = self._tab_states.get(tool_id)
        if state and state.stdin_pipe is not None:
            try:
                state.stdin_pipe.write(text + "\n")
                state.stdin_pipe.flush()
                self._log_to_tab(tool_id, f"> {text}\n")
            except (BrokenPipeError, OSError):
                self._log_to_tab(tool_id, "(process stdin closed)\n")
        self._input_var.set("")

    def _read_process_output(self, tool_id: str) -> None:
        """Daemon thread: pipe subprocess stdout into the master queue."""
        state = self._tab_states[tool_id]
        proc = state.process
        if proc is None or proc.stdout is None:
            return
        try:
            for line in iter(proc.stdout.readline, ""):
                self._master_queue.put((tool_id, line))
        except ValueError:
            pass
        proc.wait()
        self._master_queue.put((tool_id, None))  # sentinel

    def _poll_output(self) -> None:
        """Tkinter after-loop: drain master queue, dispatch to per-tab buffers."""
        try:
            for _ in range(200):  # process up to 200 items per tick
                tool_id, item = self._master_queue.get_nowait()
                state = self._tab_states.get(tool_id)
                if state is None:
                    continue
                if item is None:
                    # Process finished
                    exit_code = (
                        state.process.returncode if state.process else 0
                    )
                    self._log_to_tab(
                        tool_id, f"\n--- Finished (exit code {exit_code}) ---\n",
                    )
                    state.stdin_pipe = None
                    self._close_tab_log(tool_id)
                    self._reset_tab_buttons(tool_id)
                else:
                    self._log_to_tab(tool_id, item)
        except queue.Empty:
            pass
        self.after(100, self._poll_output)

    # ------------------------------------------------------------------
    # Per-tab logging
    # ------------------------------------------------------------------

    def _log_to_tab(self, tool_id: str, text: str) -> None:
        """Append text to a tab's log buffer, file, and (if visible) the logbox."""
        state = self._tab_states.get(tool_id)
        if state is None:
            return

        # In-memory buffer (capped)
        state.log_buffer.append(text)
        if len(state.log_buffer) > _LOG_BUFFER_MAX:
            state.log_buffer = state.log_buffer[-_LOG_BUFFER_MAX:]

        # File log
        if state.log_fh is not None:
            try:
                state.log_fh.write(text)
                state.log_fh.flush()
            except (OSError, ValueError):
                pass

        # Update logbox only if this tab is currently visible
        if self._label_to_tool_id(self._active_tab) == tool_id:
            self._append_to_logbox(text)

    def _append_to_logbox(self, text: str) -> None:
        """Append text to the shared output textbox."""
        self._log_box.configure(state="normal")
        self._log_box.insert("end", text)
        self._log_box.see("end")
        self._log_box.configure(state="disabled")

    def _log(self, text: str) -> None:
        """App-level log message (not tied to any tool tab)."""
        self._append_to_logbox(text)

    def _repaint_logbox(self, label: str) -> None:
        """Replace logbox contents with the given tab's buffer."""
        self._log_box.configure(state="normal")
        self._log_box.delete("1.0", "end")
        tool_id = self._label_to_tool_id(label)
        if tool_id:
            state = self._tab_states.get(tool_id)
            if state and state.log_buffer:
                self._log_box.insert("end", "".join(state.log_buffer))
            # Update the Output label to show which tool's log is displayed
            self._output_label.configure(text=f"Output — {label}")
        else:
            self._output_label.configure(text="Output")
        self._log_box.see("end")
        self._log_box.configure(state="disabled")

    def _close_tab_log(self, tool_id: str) -> None:
        """Close the log file handle for a tab."""
        state = self._tab_states.get(tool_id)
        if state and state.log_fh is not None:
            try:
                state.log_fh.close()
            except OSError:
                pass
            state.log_fh = None

    def _reset_tab_buttons(self, tool_id: str) -> None:
        """Restore a tab's button states after the tool finishes."""
        state = self._tab_states.get(tool_id)
        if state is None:
            return
        if state.launch_btn:
            state.launch_btn.configure(state="normal")
        if state.stop_btn:
            state.stop_btn.configure(state="disabled")
        if state.status_label:
            state.status_label.configure(text="", text_color=_GREEN)


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════

def main() -> None:
    """Launch the GUI."""
    app = NOCToolkitGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
