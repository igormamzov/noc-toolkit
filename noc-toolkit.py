#!/usr/bin/env python3
"""
NOC Toolkit - Unified command-line toolkit for NOC operations

This script provides a menu-driven interface for various operational tools.
"""

import os
import sys
import subprocess
from typing import List, Dict, Optional
from pathlib import Path

# Load environment variables from centralized .env file
_ENV_LOADED = False
_ENV_MESSAGE = ""

try:
    from dotenv import load_dotenv
    # When running as PyInstaller EXE, look for .env next to the executable
    # (not in the temp extraction dir where __file__ points)
    if getattr(sys, 'frozen', False):
        env_path = Path(sys.executable).parent / ".env"
    else:
        env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        _ENV_LOADED = True
        _ENV_MESSAGE = f"Environment loaded from: {env_path.name}"
    else:
        _ENV_MESSAGE = "No .env file found (copy .env.example to .env and configure)"
except ImportError:
    _ENV_MESSAGE = "Warning: python-dotenv not installed (pip install python-dotenv)"

# Version information
VERSION = "0.1.0"
TOOLKIT_NAME = "NOC Toolkit"

# Directory paths
SCRIPT_DIR = Path(__file__).parent.resolve()
TOOLS_DIR = SCRIPT_DIR / "tools"


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
        print(f"{env_icon} Config: {_ENV_MESSAGE}\n")

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
            # Run the tool script
            result = subprocess.run(
                [sys.executable, str(tool_path)],
                cwd=tool_path.parent
            )
            return result.returncode
        except KeyboardInterrupt:
            print("\n\n⚠️  Tool execution interrupted by user.")
            return 130
        except Exception as error:
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
