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

# Determine key directories early (needed for .env and debug log)
_FROZEN = getattr(sys, 'frozen', False)
_EXE_DIR = Path(sys.executable).parent if _FROZEN else Path(__file__).parent.resolve()
_MEIPASS = getattr(sys, '_MEIPASS', None)

# Load environment variables from centralized .env file
_ENV_LOADED = False
_ENV_MESSAGE = ""

try:
    from dotenv import load_dotenv
    # When running as PyInstaller EXE, look for .env next to the executable
    # (not in the temp extraction dir where __file__ points)
    env_path = _EXE_DIR / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        _ENV_LOADED = True
        _ENV_MESSAGE = f"Environment loaded from: {env_path.name}"
    else:
        _ENV_MESSAGE = "No .env file found (copy .env.example to .env and configure)"
except ImportError:
    _ENV_MESSAGE = "Warning: python-dotenv not installed (pip install python-dotenv)"

# Version information
VERSION = "0.7.0"
TOOLKIT_NAME = "NOC Toolkit"

# Directory paths — tools are bundled inside _MEIPASS, config is next to EXE
SCRIPT_DIR = Path(_MEIPASS) if _MEIPASS else Path(__file__).parent.resolve()
TOOLS_DIR = SCRIPT_DIR / "tools"
COMMON_DIR = TOOLS_DIR / "common"


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

        # .env info
        lines.append("")
        lines.append("--- Environment ---")
        lines.append(f"env_path:        {env_path}")
        lines.append(f"env_path exists: {env_path.exists()}")
        lines.append(f"ENV_LOADED:      {_ENV_LOADED}")
        lines.append(f"ENV_MESSAGE:     {_ENV_MESSAGE}")

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
        # Debug log must never crash the toolkit
        print(f"  (debug log failed: {exc})")


def _append_debug(message: str) -> None:
    """Append a timestamped line to the debug log."""
    log_path = _EXE_DIR / "noc-toolkit-debug.log"
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"\n[{datetime.now().isoformat()}] {message}\n")
    except Exception:
        pass


class ToolDefinition:
    """Represents a tool available in the toolkit.

    `category` can be:
      - "main" — shown in the top-level menu (default)
      - "more" — shown only when the user opens the "More tools…" submenu
    """

    def __init__(
        self,
        tool_id: str,
        name: str,
        description: str,
        script_path: str,
        enabled: bool = True,
        category: str = "main",
    ):
        self.tool_id = tool_id
        self.name = name
        self.description = description
        self.script_path = Path(script_path)
        self.enabled = enabled
        self.category = category

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

    def start(self, tool_path: Path, duration_minutes: int) -> bool:
        """Launch pd-monitor as a background subprocess.

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

        env = os.environ.copy()
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
            print(f"  Error starting background monitor: {exc}")
            if _FROZEN:
                print("  Note: Background mode requires python3 on PATH in EXE mode.")
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
        self._load_tools()

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
                # Folded into pd-merge as `jobs` subcommand on 2026-05-31.
                # Standalone kept for one release as a safety net; will be removed later.
                enabled=False,
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
                enabled=True,
                category="more",
            ),
            ToolDefinition(
                tool_id="pd-escalate",
                name="PD Escalate",
                description="Link DRGN→DSSD, transition to Escalated, post PD note",
                script_path="tools/pd-escalate/pd_escalate.py",
                enabled=True,
                category="more",
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
            ToolDefinition(
                tool_id="ga-job",
                name="GoAnywhere Lookup",
                description="Read-only GoAnywhere CLI: find jobs and monitors",
                script_path="tools/ga-job/ga_job.py",
                enabled=True
            ),
            ToolDefinition(
                tool_id="cdt",
                name="CDT Control Panel",
                description="Read-only CDT API: streaming/batch dashboards, SLA breaches",
                script_path="tools/cdt/cdt.py",
                enabled=True
            ),
            ToolDefinition(
                tool_id="auto-close",
                name="Auto-Close Transient Failures",
                description="Close DRGN tickets for Databricks jobs that already recovered",
                script_path="tools/auto-close/auto_close.py",
                enabled=True
            ),
            ToolDefinition(
                tool_id="scheduled-recheck",
                name="Scheduled Recheck",
                description="Wait N minutes, check CDT, auto-close DRGN if next run succeeded",
                script_path="tools/scheduled-recheck/scheduled_recheck.py",
                enabled=True
            ),
            ToolDefinition(
                tool_id="ccpa-audit",
                name="CCPA Audit",
                description="Render CCPA ERASE progress block; optional PD note posting",
                script_path="tools/ccpa-audit/ccpa_audit.py",
                enabled=True,
                category="more",
            ),
        ]

    def get_enabled_tools(self) -> List[ToolDefinition]:
        """Get list of enabled tools (any category)."""
        return [tool for tool in self.tools if tool.enabled]

    def get_main_tools(self) -> List[ToolDefinition]:
        """Tools shown in the top-level menu."""
        return [t for t in self.tools if t.enabled and t.category == "main"]

    def get_more_tools(self) -> List[ToolDefinition]:
        """Tools shown in the 'More tools…' submenu."""
        return [t for t in self.tools if t.enabled and t.category == "more"]

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
        print(banner)

        # Display environment configuration status
        env_icon = "✓" if _ENV_LOADED else "⚠️"
        print(f"{env_icon} Config: {_ENV_MESSAGE}")

        # Display background monitor status
        monitor_status = self._monitor_bg.status_line()
        if self._monitor_bg.is_running:
            print(f"▶ PD Monitor: {monitor_status}")
        elif monitor_status != 'OFF':
            print(f"  PD Monitor: {monitor_status}")

        print()

    def display_menu(self) -> None:
        """Display the main menu (main-category tools + 'More tools' link)."""
        print("\n" + "=" * 56)
        print("Available Tools:")
        print("=" * 56)

        main_tools = self.get_main_tools()

        if not main_tools and not self.get_more_tools():
            print("  No tools available.")
            return

        for idx, tool in enumerate(main_tools, start=1):
            status_icon = "✓" if tool.exists() else "✗"
            running_tag = ""
            if tool.tool_id == "pd-monitor" and self._monitor_bg.is_running:
                new = self._monitor_bg.new_lines
                running_tag = f" [RUNNING{f', {new} new' if new else ''}]"
            print(f"  {idx}. [{status_icon}] {tool.name}{running_tag}")
            print(f"      {tool.description}")
            if not tool.exists():
                print(f"      ⚠️  Warning: Script not found at {tool.get_full_path()}")
            print()

        if self.get_more_tools():
            print(f"  99. More tools…  ({len(self.get_more_tools())} less-used)")
            print()

        print("-" * 56)
        print("  0. Exit")
        print("=" * 56)

    def display_more_menu(self) -> None:
        """Display the 'More tools…' submenu."""
        print("\n" + "=" * 56)
        print("More Tools (less-used):")
        print("=" * 56)

        more_tools = self.get_more_tools()
        if not more_tools:
            print("  (no less-used tools)")
        else:
            for idx, tool in enumerate(more_tools, start=1):
                status_icon = "✓" if tool.exists() else "✗"
                print(f"  {idx}. [{status_icon}] {tool.name}")
                print(f"      {tool.description}")
                if not tool.exists():
                    print(f"      ⚠️  Warning: Script not found at {tool.get_full_path()}")
                print()

        print("-" * 56)
        print("  0. Back to main menu")
        print("=" * 56)

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
                print(f"❌ Invalid choice. Please enter a number between 0 and {max_choice}.")
                return None
        except ValueError:
            print("❌ Invalid input. Please enter a number.")
            return None
        except KeyboardInterrupt:
            print("\n\n👋 Interrupted by user.")
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
            print(f"❌ Error: Tool script not found at {tool_path}")
            return 1

        print(f"\n{'=' * 56}")
        print(f"🚀 Launching: {tool.name}")
        print(f"{'=' * 56}\n")

        try:
            if _FROZEN:
                # In PyInstaller EXE, sys.executable is the EXE itself (not Python),
                # so subprocess would just re-launch the toolkit. Run in-process instead.
                _append_debug(f"Launching (in-process): {tool.name}\n  path: {tool_path}")
                saved_argv = sys.argv
                saved_cwd = os.getcwd()
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
                    print(f"\n❌ Missing package: {exc}")
                    print("This dependency was not bundled into the EXE.")
                    return 1
                finally:
                    sys.argv = saved_argv
                    os.chdir(saved_cwd)
                _append_debug(f"Finished: {tool.name} → exit code 0")
                return 0
            else:
                # Running from source — use subprocess with Python interpreter
                cmd = [sys.executable, str(tool_path)]
                cwd = str(tool_path.parent)
                env = os.environ.copy()
                # Ensure tools/common (noc_utils) is importable
                extra = str(COMMON_DIR)
                env["PYTHONPATH"] = extra + os.pathsep + env.get("PYTHONPATH", "")
                _append_debug(f"Launching (subprocess): {tool.name}\n  cmd: {cmd}\n  cwd: {cwd}")
                result = subprocess.run(cmd, cwd=cwd, env=env)
                _append_debug(f"Finished: {tool.name} → exit code {result.returncode}")
                return result.returncode
        except KeyboardInterrupt:
            print("\n\n⚠️  Tool execution interrupted by user.")
            return 130
        except Exception as error:
            _append_debug(f"EXCEPTION running {tool.name}: {error}")
            print(f"\n❌ Error running tool: {error}")
            return 1

    def _run_pd_monitor_menu(self, tool: ToolDefinition) -> int:
        """Show pd-monitor sub-menu with background/foreground options.

        Returns exit code (0 = ok, used only for foreground run).
        """
        print(f"\n{'=' * 56}")
        print("PagerDuty Monitor Options")
        print(f"{'=' * 56}")

        if self._monitor_bg.is_running:
            status = self._monitor_bg.status_line()
            print(f"  Monitor is running in background ({status})\n")
            print("  1. View background output")
            print("  2. Stop background monitor")
            print("  3. Run in FOREGROUND (stops background first)")
            print("  0. Back to main menu")
            print(f"{'=' * 56}")

            try:
                choice = input("\nSelect option [0-3]: ").strip()
            except (KeyboardInterrupt, EOFError):
                return 0

            if choice == '1':
                return self._view_monitor_output()
            elif choice == '2':
                self._monitor_bg.stop()
                print("\n  Background monitor stopped.")
                return 0
            elif choice == '3':
                self._monitor_bg.stop()
                print("  Background monitor stopped.")
                return self.run_tool(tool)
            else:
                return 0
        else:
            print("  1. Run in BACKGROUND (continue using other tools)")
            print("  2. Run in FOREGROUND (interactive, blocks menu)")
            print("  0. Back to main menu")
            print(f"{'=' * 56}")

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
        print("\nSelect monitoring duration:")
        print("  1. 1 hour    [default]")
        print("  2. 2 hours")
        print("  3. 4 hours")
        print("  4. 8 hours")
        print("  0. Cancel")

        duration_map = {'': 60, '1': 60, '2': 120, '3': 240, '4': 480}

        try:
            choice = input("\nSelect [0-4, Enter=1]: ").strip()
        except (KeyboardInterrupt, EOFError):
            return 0

        if choice == '0':
            return 0

        duration = duration_map.get(choice)
        if duration is None:
            print("  Invalid choice, using 1 hour.")
            duration = 60

        tool_path = tool.get_full_path()
        success = self._monitor_bg.start(tool_path, duration)

        if success:
            print(f"\n  PD Monitor started in background ({duration} min).")
            print("  Status shown in banner. Select PD Monitor to view output or stop.")
        else:
            if self._monitor_bg.is_running:
                print("\n  Monitor is already running in background.")
            else:
                print("\n  Failed to start background monitor.")
            return 1

        return 0

    def _view_monitor_output(self) -> int:
        """Display buffered pd-monitor output."""
        print(f"\n{'=' * 56}")
        print(f"PD Monitor Output — {self._monitor_bg.status_line()}")
        print(f"{'=' * 56}\n")

        lines = self._monitor_bg.get_output()

        if not lines:
            print("  (no output yet)")
        else:
            for line in lines:
                print(f"  {line}")

        print(f"\n{'=' * 56}")
        if self._monitor_bg.is_running:
            print("  Monitor continues running in background.")

        try:
            input("\nPress Enter to return to main menu...")
        except (KeyboardInterrupt, EOFError):
            pass

        return 0

    def _select_more_tool(self) -> Optional[ToolDefinition]:
        """Show the 'More tools…' submenu and return the selected tool, or None."""
        while True:
            self.display_more_menu()
            more_tools = self.get_more_tools()
            if not more_tools:
                input("\nPress Enter to return to main menu...")
                return None
            try:
                choice = input(f"\nSelect tool [0-{len(more_tools)}]: ").strip()
                choice_num = int(choice)
            except ValueError:
                print("❌ Invalid input. Please enter a number.")
                continue
            except KeyboardInterrupt:
                print("\n\n👋 Returning to main menu.")
                return None
            if choice_num == 0:
                return None
            if 1 <= choice_num <= len(more_tools):
                return more_tools[choice_num - 1]
            print(f"❌ Invalid choice. Please enter a number between 0 and {len(more_tools)}.")

    def _run_selected_tool(self, selected_tool: ToolDefinition) -> None:
        """Common path for running a chosen tool, with pd-monitor sub-menu routing."""
        # Route pd-monitor through the background-capable sub-menu
        if selected_tool.tool_id == "pd-monitor":
            self._run_pd_monitor_menu(selected_tool)
            return  # Sub-menu handles its own prompts

        # Run the tool
        exit_code = self.run_tool(selected_tool)

        # Show completion message
        print(f"\n{'=' * 56}")
        if exit_code == 0:
            print(f"✅ {selected_tool.name} completed successfully.")
        else:
            print(f"⚠️  {selected_tool.name} exited with code {exit_code}.")
        print(f"{'=' * 56}")

        # Wait for user before returning to menu
        input("\nPress Enter to return to main menu...")

    def run_interactive_menu(self) -> None:
        """Run the main interactive menu loop."""
        while True:
            self.display_banner()
            self.display_menu()

            main_tools = self.get_main_tools()
            more_tools = self.get_more_tools()

            if not main_tools and not more_tools:
                print("\n⚠️  No tools available. Exiting.")
                break

            # Build the choice prompt — we accept 1..N for main tools, 99 for the
            # 'More tools…' submenu (if present), and 0 to exit.
            max_main = len(main_tools)
            choice_label = f"[0-{max_main}{', 99' if more_tools else ''}]"

            try:
                raw = input(f"\nSelect tool {choice_label}: ").strip()
                choice_num = int(raw)
            except ValueError:
                print("❌ Invalid input. Please enter a number.")
                continue
            except KeyboardInterrupt:
                print("\n\n👋 Interrupted by user.")
                choice_num = 0

            if choice_num == 0:
                if self._monitor_bg.is_running:
                    print("\n  Stopping background PD Monitor...")
                    self._monitor_bg.stop()
                print("\n👋 Exiting NOC Toolkit. Goodbye!")
                break

            if choice_num == 99 and more_tools:
                more_choice = self._select_more_tool()
                if more_choice is None:
                    continue
                self._run_selected_tool(more_choice)
                continue

            if 1 <= choice_num <= max_main:
                self._run_selected_tool(main_tools[choice_num - 1])
                continue

            valid_range = f"between 0 and {max_main}" + (" (or 99 for more tools)" if more_tools else "")
            print(f"❌ Invalid choice. Please enter a number {valid_range}.")


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
        print("\n\n👋 Interrupted by user. Exiting.")
        return 130
    except Exception as error:
        print(f"\n❌ Unexpected error: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
