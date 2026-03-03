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
from datetime import datetime
from typing import List, Dict, Optional
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
VERSION = "0.4.0"
TOOLKIT_NAME = "NOC Toolkit"

# Directory paths — tools are bundled inside _MEIPASS, config is next to EXE
SCRIPT_DIR = Path(_MEIPASS) if _MEIPASS else Path(__file__).parent.resolve()
TOOLS_DIR = SCRIPT_DIR / "tools"


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


class NOCToolkit:
    """Main toolkit class for managing and running tools."""

    def __init__(self):
        self.tools: List[ToolDefinition] = []
        self._load_tools()

    def _load_tools(self) -> None:
        """Load available tools."""
        # Define tools manually (can be moved to JSON config later)
        self.tools = [
            ToolDefinition(
                tool_id="pd-jira-tool",
                name="PagerDuty-Jira Tool",
                description="Sync PagerDuty incidents with Jira issues",
                script_path="tools/pd-jira-tool/pagerduty_jira_tool.py",
                enabled=True
            ),
            ToolDefinition(
                tool_id="pagerduty-job-extractor",
                name="PagerDuty Job Extractor",
                description="Extract job names from merged PagerDuty incidents",
                script_path="tools/pagerduty-job-extractor/extract_jobs.py",
                enabled=True
            ),
            ToolDefinition(
                tool_id="pd-monitor",
                name="PagerDuty Monitor",
                description="Monitor and auto-acknowledge triggered incidents",
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
        print(banner)

        # Display environment configuration status
        env_icon = "✓" if _ENV_LOADED else "⚠️"
        print(f"{env_icon} Config: {_ENV_MESSAGE}")
        print(f"📋 Debug log: {_EXE_DIR / 'noc-toolkit-debug.log'}\n")

    def display_menu(self) -> None:
        """Display the main menu."""
        print("\n" + "=" * 56)
        print("Available Tools:")
        print("=" * 56)

        enabled_tools = self.get_enabled_tools()

        if not enabled_tools:
            print("  No tools available.")
            return

        for idx, tool in enumerate(enabled_tools, start=1):
            status_icon = "✓" if tool.exists() else "✗"
            print(f"  {idx}. [{status_icon}] {tool.name}")
            print(f"      {tool.description}")
            if not tool.exists():
                print(f"      ⚠️  Warning: Script not found at {tool.get_full_path()}")
            print()

        print("-" * 56)
        print("  0. Exit")
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
                _append_debug(f"Launching (subprocess): {tool.name}\n  cmd: {cmd}\n  cwd: {cwd}")
                result = subprocess.run(cmd, cwd=cwd)
                _append_debug(f"Finished: {tool.name} → exit code {result.returncode}")
                return result.returncode
        except KeyboardInterrupt:
            print("\n\n⚠️  Tool execution interrupted by user.")
            return 130
        except Exception as error:
            _append_debug(f"EXCEPTION running {tool.name}: {error}")
            print(f"\n❌ Error running tool: {error}")
            return 1

    def run_interactive_menu(self) -> None:
        """Run the main interactive menu loop."""
        while True:
            self.display_banner()
            self.display_menu()

            enabled_tools = self.get_enabled_tools()
            max_choice = len(enabled_tools)

            if max_choice == 0:
                print("\n⚠️  No tools available. Exiting.")
                break

            choice = self.get_user_choice(max_choice)

            if choice is None:
                continue

            if choice == 0:
                print("\n👋 Exiting NOC Toolkit. Goodbye!")
                break

            # Get the selected tool (adjust index since menu starts at 1)
            selected_tool = enabled_tools[choice - 1]

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


def main() -> int:
    """
    Main entry point for the toolkit.

    Returns:
        Exit code
    """
    try:
        _write_debug_log()
        toolkit = NOCToolkit()
        toolkit.run_interactive_menu()
        return 0
    except KeyboardInterrupt:
        print("\n\n👋 Interrupted by user. Exiting.")
        return 130
    except Exception as error:
        print(f"\n❌ Unexpected error: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
