#!/usr/bin/env python3
"""
NOC Toolkit - Unified command-line toolkit for NOC operations

This script provides a menu-driven interface for various operational tools.
"""

import os
import sys
import subprocess
import platform
import runpy
import threading
from collections import deque
from datetime import datetime
from typing import Deque, List, Dict, Optional
from pathlib import Path

# Determine key directories early (needed for .env, debug log, and sys.path setup)
_FROZEN = getattr(sys, 'frozen', False)
_EXE_DIR = Path(sys.executable).parent if _FROZEN else Path(__file__).parent.resolve()
_MEIPASS = getattr(sys, '_MEIPASS', None)

# Deferred: .env is loaded selectively during _load_config() based on
# which env-var references config.yaml actually contains.
_DOTENV_AVAILABLE = False
try:
    from dotenv import dotenv_values
    _DOTENV_AVAILABLE = True
except ImportError:  # pragma: no cover
    pass  # pragma: no cover

# Version information
VERSION = "0.6.1"
TOOLKIT_NAME = "NOC Toolkit"

# Directory paths — tools are bundled inside _MEIPASS, config is next to EXE
SCRIPT_DIR = Path(_MEIPASS) if _MEIPASS else Path(__file__).parent.resolve()
TOOLS_DIR = SCRIPT_DIR / "tools"
COMMON_DIR = TOOLS_DIR / "common"

# Ensure tools/common is importable so noc_utils can be resolved
_common_str = str(COMMON_DIR)
if _common_str not in sys.path:
    sys.path.insert(0, _common_str)  # pragma: no cover

from noc_utils import setup_logging, load_config, extract_env_references  # noqa: E402 — must follow sys.path setup

logger = setup_logging(name=__name__)


def _write_debug_log() -> None:
    """Write diagnostic log next to the EXE for troubleshooting."""
    log_path = _EXE_DIR / "noc-toolkit-debug.log"
    try:
        lines: List[str] = []
        lines.append(f"NOC Toolkit Debug Log — {datetime.now().isoformat()}")
        lines.append("=" * 60)

        # System info
        lines.append(f"Python:          {sys.version}")
        lines.append(f"Platform:        {platform.platform()}")
        lines.append(f"OS:              {os.name}")
        lines.append(f"CWD:             {os.getcwd()}")

        # PyInstaller info
        lines.append("")
        lines.append("--- PyInstaller ---")
        lines.append(f"sys.frozen:      {_FROZEN}")
        lines.append(f"sys.executable:  {sys.executable}")
        lines.append(f"sys._MEIPASS:    {_MEIPASS}")
        lines.append(f"__file__:        {__file__}")

        # Resolved directories
        lines.append("")
        lines.append("--- Paths ---")
        lines.append(f"EXE_DIR:         {_EXE_DIR}")
        lines.append(f"SCRIPT_DIR:      {SCRIPT_DIR}")
        lines.append(f"TOOLS_DIR:       {TOOLS_DIR}")
        lines.append(f"TOOLS_DIR exists:{TOOLS_DIR.exists()}")

        # Config / env info
        env_path = _EXE_DIR / ".env"
        config_path = _EXE_DIR / "config.yaml"
        lines.append("")
        lines.append("--- Configuration ---")
        lines.append(f"config.yaml:     {config_path} (exists: {config_path.exists()})")
        lines.append(f".env:            {env_path} (exists: {env_path.exists()})")

        # Check which credential env vars are set (masked)
        env_vars = [
            'PAGERDUTY_API_TOKEN', 'JIRA_SERVER_URL', 'JIRA_EMAIL',
            'JIRA_API_TOKEN', 'JIRA_PERSONAL_ACCESS_TOKEN',
        ]
        for var in env_vars:
            val = os.environ.get(var)
            if val:
                masked = val[:4] + '***' + val[-4:] if len(val) > 8 else '***'
                lines.append(f"  {var}: {masked}")
            else:
                lines.append(f"  {var}: NOT SET")

        # List tools directory contents
        lines.append("")
        lines.append("--- Tools Directory ---")
        if TOOLS_DIR.exists():
            for item in sorted(TOOLS_DIR.rglob("*")):
                rel = item.relative_to(TOOLS_DIR)
                kind = "DIR " if item.is_dir() else f"FILE ({item.stat().st_size}b)"
                lines.append(f"  {kind}: {rel}")
        else:
            lines.append("  TOOLS_DIR does not exist!")

        # List EXE directory contents
        lines.append("")
        lines.append("--- EXE Directory ---")
        for item in sorted(_EXE_DIR.iterdir()):
            kind = "DIR " if item.is_dir() else f"FILE ({item.stat().st_size}b)"
            lines.append(f"  {kind}: {item.name}")

        log_path.write_text("\n".join(lines), encoding="utf-8")
    except Exception as exc:
        # Debug log must never crash the toolkit — use logger as last resort
        logger.warning("debug log failed: %s", exc)


def _append_debug(message: str) -> None:
    """Append a timestamped line to the debug log."""
    log_path = _EXE_DIR / "noc-toolkit-debug.log"
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now().isoformat()}] {message}\n")
    except Exception:
        pass


class ToolDefinition:
    """Represents a tool available in the toolkit."""

    def __init__(
        self,
        tool_id: str,
        name: str,
        description: str,
        script_path: str,
        enabled: bool = True
    ):
        self.tool_id = tool_id
        self.name = name
        self.description = description
        self.script_path = Path(script_path)
        self.enabled = enabled

    def get_full_path(self) -> Path:
        """Get the full absolute path to the tool script."""
        if self.script_path.is_absolute():
            return self.script_path
        return SCRIPT_DIR / self.script_path

    def exists(self) -> bool:
        """Check if the tool script exists."""
        return self.get_full_path().exists()


class MonitorBackground:
    """Runs pd-monitor as a background subprocess with captured output."""

    MAX_LOG_LINES: int = 500

    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._output_buffer: Deque[str] = deque(maxlen=self.MAX_LOG_LINES)
        self._buffer_lock: threading.Lock = threading.Lock()
        self._new_line_count: int = 0
        self._start_time: Optional[datetime] = None
        self._duration_minutes: int = 60

    @property
    def is_running(self) -> bool:
        """True if the background process is alive."""
        return self._process is not None and self._process.poll() is None

    @property
    def new_lines(self) -> int:
        """Number of output lines not yet viewed by the user."""
        return self._new_line_count

    def start(self, tool_path: Path, duration_minutes: int,
              base_env: Optional[Dict[str, str]] = None) -> bool:
        """Launch pd-monitor as a background subprocess.

        Args:
            tool_path: Path to the pd_monitor.py script.
            duration_minutes: How long to run.
            base_env: Pre-built environment dict (from ``_tool_env()``).
                Falls back to ``os.environ.copy()`` when *None*.

        Returns True on success, False if already running or failed to start.
        """
        if self.is_running:
            return False

        self._duration_minutes = duration_minutes
        self._start_time = datetime.now()
        self._new_line_count = 0
        with self._buffer_lock:
            self._output_buffer.clear()

        # In frozen mode sys.executable is the EXE itself, not Python
        if _FROZEN:
            python_exe = 'python3'
        else:
            python_exe = sys.executable

        cmd = [
            python_exe, str(tool_path),
            '--duration', str(duration_minutes),
            '--background',
        ]

        env = base_env if base_env is not None else os.environ.copy()
        extra = str(COMMON_DIR)
        env["PYTHONPATH"] = extra + os.pathsep + env.get("PYTHONPATH", "")

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                cwd=str(tool_path.parent),
                env=env,
            )
        except FileNotFoundError as exc:
            logger.error("  Error starting background monitor: %s", exc)
            if _FROZEN:
                logger.error("  Note: Background mode requires python3 on PATH in EXE mode.")
            return False

        self._reader_thread = threading.Thread(
            target=self._read_output,
            daemon=True,
            name='pd-monitor-reader',
        )
        self._reader_thread.start()
        return True

    def stop(self) -> None:
        """Terminate the background subprocess."""
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()

    def get_output(self) -> List[str]:
        """Return all buffered output lines and reset the new-line counter."""
        with self._buffer_lock:
            self._new_line_count = 0
            return list(self._output_buffer)

    def status_line(self) -> str:
        """One-line status string for the menu banner."""
        if not self.is_running:
            if self._start_time is not None:
                return 'STOPPED'
            return 'OFF'

        elapsed = int((datetime.now() - self._start_time).total_seconds() // 60)
        new = self._new_line_count
        new_str = f' | {new} new' if new > 0 else ''
        return f'ACTIVE {elapsed}m/{self._duration_minutes}m{new_str}'

    def _read_output(self) -> None:
        """Daemon thread: reads lines from subprocess stdout into the ring buffer."""
        assert self._process is not None and self._process.stdout is not None
        try:
            for raw_line in iter(self._process.stdout.readline, ''):
                line = raw_line.rstrip('\n')
                if line:
                    with self._buffer_lock:
                        self._output_buffer.append(line)
                        self._new_line_count += 1
        except ValueError:
            pass  # Stream closed
        if self._process is not None:
            self._process.wait()


class NOCToolkit:
    """Main toolkit class for managing and running tools."""

    def __init__(self):
        self.tools: List[ToolDefinition] = []
        self._monitor_bg: MonitorBackground = MonitorBackground()
        self._config: Dict[str, str] = self._load_config()
        self._load_tools()

    def _load_config(self) -> Dict[str, str]:
        """Load config.yaml once at startup.

        Flow:
        1. Read config.yaml and extract ``${VAR}`` references.
        2. If any references exist **and** a ``.env`` file is present, load
           only the referenced variables from ``.env`` into ``os.environ``.
        3. Resolve the full config (env vars are now available).

        Resolved values are **not** written into ``os.environ``.  Instead
        they are injected into the subprocess environment by :meth:`run_tool`
        and :meth:`MonitorBackground.start` at launch time.

        Returns:
            Dict of resolved config key-value pairs.
        """
        config_path = _EXE_DIR / "config.yaml"
        config_str = str(config_path) if config_path.is_file() else None

        try:
            # Step 1 — identify which env vars the config references
            env_refs = extract_env_references(config_str)

            # Step 2 — selectively load only referenced vars from .env
            if env_refs and _DOTENV_AVAILABLE:
                env_file = _EXE_DIR / ".env"
                if env_file.is_file():
                    all_dotenv: Dict[str, Optional[str]] = dotenv_values(env_file)
                    for var_name in env_refs:
                        dotenv_val = all_dotenv.get(var_name)
                        if var_name not in os.environ and dotenv_val is not None:
                            os.environ[var_name] = dotenv_val

            # Step 3 — resolve config (reads os.environ for ${VAR} values)
            return load_config(config_str)
        except Exception as exc:
            logger.warning("Failed to load config.yaml: %s", exc)
            return {}

    def _tool_env(self) -> Dict[str, str]:
        """Build an environment dict for launching a tool subprocess.

        Starts from the current ``os.environ`` and overlays resolved config
        values.  Config values do **not** overwrite variables that are
        already set in the real environment.
        """
        env = os.environ.copy()
        for key, value in self._config.items():
            if key not in env or not env[key]:
                env[key] = value
        return env

    def _load_tools(self) -> None:
        """Load available tools."""
        # Define tools manually (can be moved to JSON config later)
        self.tools = [
            ToolDefinition(
                tool_id="pd-sync",
                name="PD Sync",
                description="Sync PagerDuty incidents with Jira issues",
                script_path="tools/pd-sync/pd_sync.py",
                enabled=True
            ),
            ToolDefinition(
                tool_id="pd-jobs",
                name="PD Jobs",
                description="Extract job names from merged PagerDuty incidents",
                script_path="tools/pd-jobs/pd_jobs.py",
                enabled=True
            ),
            ToolDefinition(
                tool_id="pd-monitor",
                name="PD Monitor",
                description="Monitor and auto-acknowledge triggered incidents",
                script_path="tools/pd-monitor/pd_monitor.py",
                enabled=True
            ),
            ToolDefinition(
                tool_id="pd-merge",
                name="PD Merge",
                description="Find and merge related PagerDuty incidents by job name",
                script_path="tools/pd-merge/pd_merge.py",
                enabled=True
            ),
            ToolDefinition(
                tool_id="freshness",
                name="Freshness",
                description="DACSCAN data freshness report with granular table checks",
                script_path="tools/freshness/freshness.py",
                enabled=True
            ),
            ToolDefinition(
                tool_id="shift-report",
                name="Shift Report",
                description="Sync Jira statuses into shift report (Google Sheets / Excel)",
                script_path="tools/shift-report/shift_report.py",
                enabled=True
            ),
            ToolDefinition(
                tool_id="pd-escalate",
                name="PD Escalate",
                description="Link DRGN→DSSD, transition to Escalated, post PD note",
                script_path="tools/pd-escalate/pd_escalate.py",
                enabled=True
            ),
            ToolDefinition(
                tool_id="pd-resolve",
                name="PD Resolve",
                description="Auto-resolve PD incidents where Airflow jobs recovered",
                script_path="tools/pd-resolve/pd_resolve.py",
                enabled=True
            ),
            ToolDefinition(
                tool_id="ticket-watch",
                name="Ticket Watch",
                description="Monitor escalation tickets for unassigned/stale states",
                script_path="tools/ticket-watch/ticket_watch.py",
                enabled=True
            ),
        ]

    def get_enabled_tools(self) -> List[ToolDefinition]:
        """Get list of enabled tools."""
        return [tool for tool in self.tools if tool.enabled]

    def display_banner(self) -> None:
        """Display the toolkit banner."""
        title_line = f"{TOOLKIT_NAME} v{VERSION}"
        banner = f"""
╔════════════════════════════════════════════════════════╗
║                                                        ║
║{title_line:^56}║
║                                                        ║
║         Unified NOC Operations Toolkit                 ║
║                                                        ║
╚════════════════════════════════════════════════════════╝
"""
        logger.info(banner)

        # Display config status
        if self._config:
            logger.info("✓ config.yaml: %d keys loaded", len(self._config))
        else:
            logger.info("⚠️  config.yaml: not found (copy config.yaml.example and configure)")

        # Display background monitor status
        monitor_status = self._monitor_bg.status_line()
        if self._monitor_bg.is_running:
            logger.info("▶ PD Monitor: %s", monitor_status)
        elif monitor_status != 'OFF':
            logger.info("  PD Monitor: %s", monitor_status)

        logger.info("")

    def display_menu(self) -> None:
        """Display the main menu."""
        logger.info("\n" + "=" * 56)
        logger.info("Available Tools:")
        logger.info("=" * 56)

        enabled_tools = self.get_enabled_tools()

        if not enabled_tools:
            logger.info("  No tools available.")
            return

        for idx, tool in enumerate(enabled_tools, start=1):
            status_icon = "✓" if tool.exists() else "✗"
            # Annotate pd-monitor entry when running in background
            running_tag = ""
            if tool.tool_id == "pd-monitor" and self._monitor_bg.is_running:
                new = self._monitor_bg.new_lines
                running_tag = f" [RUNNING{f', {new} new' if new else ''}]"
            logger.info("  %d. [%s] %s%s", idx, status_icon, tool.name, running_tag)
            logger.info("      %s", tool.description)
            if not tool.exists():
                logger.warning("      ⚠️  Warning: Script not found at %s", tool.get_full_path())
            logger.info("")

        logger.info("-" * 56)
        logger.info("  0. Exit")
        logger.info("=" * 56)

    def get_user_choice(self, max_choice: int) -> Optional[int]:
        """
        Get user's menu choice.

        Args:
            max_choice: Maximum valid choice number

        Returns:
            Selected choice number or None if invalid
        """
        try:
            choice = input(f"\nSelect tool [0-{max_choice}]: ").strip()
            choice_num = int(choice)

            if 0 <= choice_num <= max_choice:
                return choice_num
            else:
                logger.info("❌ Invalid choice. Please enter a number between 0 and %d.", max_choice)
                return None
        except ValueError:
            logger.info("❌ Invalid input. Please enter a number.")
            return None
        except KeyboardInterrupt:
            logger.info("\n\n👋 Interrupted by user.")
            return 0

    def run_tool(self, tool: ToolDefinition) -> int:
        """
        Run the specified tool.

        Args:
            tool: The tool to run

        Returns:
            Exit code from the tool
        """
        tool_path = tool.get_full_path()

        if not tool.exists():
            logger.error("❌ Error: Tool script not found at %s", tool_path)
            return 1

        logger.info("\n%s", "=" * 56)
        logger.info("🚀 Launching: %s", tool.name)
        logger.info("%s\n", "=" * 56)

        try:
            if _FROZEN:
                # In PyInstaller EXE, sys.executable is the EXE itself (not Python),
                # so subprocess would just re-launch the toolkit. Run in-process instead.
                _append_debug(f"Launching (in-process): {tool.name}\n  path: {tool_path}")
                saved_argv = sys.argv
                saved_cwd = os.getcwd()
                # Temporarily inject config into os.environ for in-process tools
                injected_keys: List[str] = []
                for key, value in self._config.items():
                    if key not in os.environ or not os.environ[key]:
                        os.environ[key] = value
                        injected_keys.append(key)
                # Ensure tools/common (noc_utils) is importable
                common_str = str(COMMON_DIR)
                if common_str not in sys.path:
                    sys.path.insert(0, common_str)
                try:
                    sys.argv = [str(tool_path)]
                    os.chdir(tool_path.parent)
                    runpy.run_path(str(tool_path), run_name='__main__')
                except SystemExit as exc:
                    # Tools may call sys.exit() — catch it so we return to menu
                    exit_code = exc.code if isinstance(exc.code, int) else 0
                    _append_debug(f"Finished (SystemExit): {tool.name} → code {exit_code}")
                    return exit_code
                except ImportError as exc:
                    _append_debug(f"IMPORT ERROR in {tool.name}: {exc}")
                    logger.error("\n❌ Missing package: %s", exc)
                    logger.error("This dependency was not bundled into the EXE.")
                    return 1
                finally:
                    sys.argv = saved_argv
                    os.chdir(saved_cwd)
                    for key in injected_keys:
                        os.environ.pop(key, None)
                _append_debug(f"Finished: {tool.name} → exit code 0")
                return 0
            else:
                # Running from source — use subprocess with Python interpreter
                cmd = [sys.executable, str(tool_path)]
                cwd = str(tool_path.parent)
                env = self._tool_env()
                # Ensure tools/common (noc_utils) is importable
                extra = str(COMMON_DIR)
                env["PYTHONPATH"] = extra + os.pathsep + env.get("PYTHONPATH", "")
                _append_debug(f"Launching (subprocess): {tool.name}\n  cmd: {cmd}\n  cwd: {cwd}")
                result = subprocess.run(cmd, cwd=cwd, env=env)
                _append_debug(f"Finished: {tool.name} → exit code {result.returncode}")
                return result.returncode
        except KeyboardInterrupt:
            logger.info("\n\n⚠️  Tool execution interrupted by user.")
            return 130
        except Exception as error:
            _append_debug(f"EXCEPTION running {tool.name}: {error}")
            logger.error("\n❌ Error running tool: %s", error)
            return 1

    def _run_pd_monitor_menu(self, tool: ToolDefinition) -> int:
        """Show pd-monitor sub-menu with background/foreground options.

        Returns exit code (0 = ok, used only for foreground run).
        """
        logger.info("\n%s", "=" * 56)
        logger.info("PagerDuty Monitor Options")
        logger.info("=" * 56)

        if self._monitor_bg.is_running:
            status = self._monitor_bg.status_line()
            logger.info("  Monitor is running in background (%s)\n", status)
            logger.info("  1. View background output")
            logger.info("  2. Stop background monitor")
            logger.info("  3. Run in FOREGROUND (stops background first)")
            logger.info("  0. Back to main menu")
            logger.info("=" * 56)

            try:
                choice = input("\nSelect option [0-3]: ").strip()
            except (KeyboardInterrupt, EOFError):
                return 0

            if choice == '1':
                return self._view_monitor_output()
            elif choice == '2':
                self._monitor_bg.stop()
                logger.info("\n  Background monitor stopped.")
                return 0
            elif choice == '3':
                self._monitor_bg.stop()
                logger.info("  Background monitor stopped.")
                return self.run_tool(tool)
            else:
                return 0
        else:
            logger.info("  1. Run in BACKGROUND (continue using other tools)")
            logger.info("  2. Run in FOREGROUND (interactive, blocks menu)")
            logger.info("  0. Back to main menu")
            logger.info("%s", '=' * 56)

            try:
                choice = input("\nSelect option [0-2]: ").strip()
            except (KeyboardInterrupt, EOFError):
                return 0

            if choice == '1':
                return self._start_background_monitor(tool)
            elif choice == '2':
                return self.run_tool(tool)
            else:
                return 0

    def _start_background_monitor(self, tool: ToolDefinition) -> int:
        """Ask for duration and launch pd-monitor in background."""
        logger.info("\nSelect monitoring duration:")
        logger.info("  1. 1 hour    [default]")
        logger.info("  2. 2 hours")
        logger.info("  3. 4 hours")
        logger.info("  4. 8 hours")
        logger.info("  0. Cancel")

        duration_map = {'': 60, '1': 60, '2': 120, '3': 240, '4': 480}

        try:
            choice = input("\nSelect [0-4, Enter=1]: ").strip()
        except (KeyboardInterrupt, EOFError):
            return 0

        if choice == '0':
            return 0

        duration = duration_map.get(choice)
        if duration is None:
            logger.info("  Invalid choice, using 1 hour.")
            duration = 60

        tool_path = tool.get_full_path()
        success = self._monitor_bg.start(tool_path, duration, base_env=self._tool_env())

        if success:
            logger.info("\n  PD Monitor started in background (%s min).", duration)
            logger.info("  Status shown in banner. Select PD Monitor to view output or stop.")
        else:
            if self._monitor_bg.is_running:
                logger.info("\n  Monitor is already running in background.")
            else:
                logger.info("\n  Failed to start background monitor.")
            return 1

        return 0

    def _run_shift_report_menu(self, tool: ToolDefinition) -> int:
        """Show Shift Report sub-menu: Online (Google Sheets) or Local (Excel).

        Returns exit code from the selected mode.
        """
        # Check both config.yaml and os.environ for Google Sheets credentials
        def _get(key: str) -> str:
            return (self._config.get(key) or os.environ.get(key, "")).strip()

        gsheet_configured = bool(_get("GSHEET_WEBAPP_URL") and _get("GSHEET_API_KEY"))

        logger.info("\n%s", '=' * 56)
        logger.info("Shift Report")
        logger.info("%s", '=' * 56)

        if gsheet_configured:
            logger.info("  1. Online mode  (Google Sheets)  [recommended]")
            logger.info("  2. Local mode   (Excel file)")
            logger.info("  0. Back to main menu")
            logger.info("%s", '=' * 56)

            try:
                choice = input("\nSelect option [0-2]: ").strip()
            except (KeyboardInterrupt, EOFError):
                return 0

            if choice == '1':
                gsheet_tool = ToolDefinition(
                    tool_id="shift-report-gsheet",
                    name="Shift Report (Google Sheets)",
                    description="",
                    script_path="tools/shift-report/gsheet_report.py",
                )
                return self.run_tool(gsheet_tool)
            elif choice == '2':
                return self.run_tool(tool)
            else:
                return 0
        else:
            logger.info("")
            logger.info("  Google Sheets mode is not configured.")
            logger.info("")
            logger.info("  To enable it, add these variables to your .env file:")
            logger.info("    GSHEET_WEBAPP_URL=<web app URL>")
            logger.info("    GSHEET_API_KEY=<api key>")
            logger.info("")
            logger.info("  Request these values from the toolkit maintainer.")
            logger.info("")
            logger.info("  1. Local mode   (Excel file)")
            logger.info("  0. Back to main menu")
            logger.info("%s", '=' * 56)

            try:
                choice = input("\nSelect option [0-1]: ").strip()
            except (KeyboardInterrupt, EOFError):
                return 0

            if choice == '1':
                return self.run_tool(tool)
            else:
                return 0

    def _view_monitor_output(self) -> int:
        """Display buffered pd-monitor output."""
        logger.info("\n%s", '=' * 56)
        logger.info("PD Monitor Output — %s", self._monitor_bg.status_line())
        logger.info("%s\n", '=' * 56)

        lines = self._monitor_bg.get_output()

        if not lines:
            logger.info("  (no output yet)")
        else:
            for line in lines:
                logger.info("  %s", line)

        logger.info("\n%s", '=' * 56)
        if self._monitor_bg.is_running:
            logger.info("  Monitor continues running in background.")

        try:
            input("\nPress Enter to return to main menu...")
        except (KeyboardInterrupt, EOFError):
            pass

        return 0

    def run_interactive_menu(self) -> None:
        """Run the main interactive menu loop."""
        while True:
            self.display_banner()
            self.display_menu()

            enabled_tools = self.get_enabled_tools()
            max_choice = len(enabled_tools)

            if max_choice == 0:
                logger.info("\n⚠️  No tools available. Exiting.")
                break

            choice = self.get_user_choice(max_choice)

            if choice is None:
                continue

            if choice == 0:
                if self._monitor_bg.is_running:
                    logger.info("\n  Stopping background PD Monitor...")
                    self._monitor_bg.stop()
                logger.info("\n👋 Exiting NOC Toolkit. Goodbye!")
                break

            # Get the selected tool (adjust index since menu starts at 1)
            selected_tool = enabled_tools[choice - 1]

            # Route pd-monitor through the background-capable sub-menu
            if selected_tool.tool_id == "pd-monitor":
                self._run_pd_monitor_menu(selected_tool)
                continue  # Sub-menu handles its own prompts

            # Route shift-report through Online/Local sub-menu
            if selected_tool.tool_id == "shift-report":
                self._run_shift_report_menu(selected_tool)
                continue

            # Run the tool
            exit_code = self.run_tool(selected_tool)

            # Show completion message
            logger.info("\n%s", '=' * 56)
            if exit_code == 0:
                logger.info("✅ %s completed successfully.", selected_tool.name)
            else:
                logger.info("⚠️  %s exited with code %s.", selected_tool.name, exit_code)
            logger.info("%s", '=' * 56)

            # Wait for user before returning to menu
            input("\nPress Enter to return to main menu...")


def main() -> int:
    """
    Main entry point for the toolkit.

    Returns:
        Exit code
    """
    toolkit: Optional[NOCToolkit] = None
    try:
        _write_debug_log()
        toolkit = NOCToolkit()
        toolkit.run_interactive_menu()
        return 0
    except KeyboardInterrupt:
        if toolkit is not None and toolkit._monitor_bg.is_running:
            toolkit._monitor_bg.stop()
        logger.info("\n\n👋 Interrupted by user. Exiting.")
        return 130
    except Exception as error:
        logger.error("\n❌ Unexpected error: %s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
