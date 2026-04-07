"""Tests for noc-toolkit.py main launcher."""

import logging
import os
import sys
import subprocess
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# Add the project root to sys.path so we can import the launcher module.
# noc-toolkit.py has a hyphen, so we use importlib.
import importlib

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# Import via importlib since module name contains a hyphen
_spec = importlib.util.spec_from_file_location("noc_toolkit", _PROJECT_ROOT / "noc-toolkit.py")
noc_toolkit = importlib.util.module_from_spec(_spec)


def _load_module():
    """Load the noc-toolkit module with mocked env/dotenv."""
    with patch.dict(os.environ, {}, clear=False):
        _spec.loader.exec_module(noc_toolkit)
    return noc_toolkit


# Load once at module level (side-effects are safe: just dotenv + path constants)
_load_module()

ToolDefinition = noc_toolkit.ToolDefinition
MonitorBackground = noc_toolkit.MonitorBackground
NOCToolkit = noc_toolkit.NOCToolkit


# ===========================================================================
# ToolDefinition
# ===========================================================================


class TestToolDefinition:
    """Tests for the ToolDefinition class."""

    def test_init_stores_attributes(self) -> None:
        td = ToolDefinition(
            tool_id="test-tool",
            name="Test Tool",
            description="A test tool",
            script_path="tools/test/test.py",
        )
        assert td.tool_id == "test-tool"
        assert td.name == "Test Tool"
        assert td.description == "A test tool"
        assert td.enabled is True

    def test_disabled_tool(self) -> None:
        td = ToolDefinition(
            tool_id="disabled",
            name="Disabled",
            description="",
            script_path="tools/disabled.py",
            enabled=False,
        )
        assert td.enabled is False

    def test_get_full_path_relative(self) -> None:
        td = ToolDefinition(
            tool_id="t",
            name="T",
            description="",
            script_path="tools/test/test.py",
        )
        full_path = td.get_full_path()
        assert full_path.is_absolute()
        assert str(full_path).endswith("tools/test/test.py")

    def test_get_full_path_absolute(self, tmp_path: Path) -> None:
        abs_path = tmp_path / "tool.py"
        abs_path.touch()
        td = ToolDefinition(
            tool_id="t",
            name="T",
            description="",
            script_path=str(abs_path),
        )
        assert td.get_full_path() == abs_path

    def test_exists_true(self, tmp_path: Path) -> None:
        script = tmp_path / "tool.py"
        script.write_text("print('hello')")
        td = ToolDefinition(
            tool_id="t",
            name="T",
            description="",
            script_path=str(script),
        )
        assert td.exists() is True

    def test_exists_false(self) -> None:
        td = ToolDefinition(
            tool_id="t",
            name="T",
            description="",
            script_path="/nonexistent/path/tool.py",
        )
        assert td.exists() is False


# ===========================================================================
# MonitorBackground
# ===========================================================================


class TestMonitorBackground:
    """Tests for the MonitorBackground class."""

    def test_init_defaults(self) -> None:
        mb = MonitorBackground()
        assert mb.is_running is False
        assert mb.new_lines == 0
        assert mb.MAX_LOG_LINES == 500

    def test_status_line_off(self) -> None:
        mb = MonitorBackground()
        assert mb.status_line() == "OFF"

    def test_status_line_stopped(self) -> None:
        mb = MonitorBackground()
        mb._start_time = datetime.now()
        assert mb.status_line() == "STOPPED"

    def test_get_output_empty(self) -> None:
        mb = MonitorBackground()
        assert mb.get_output() == []

    def test_get_output_resets_counter(self) -> None:
        mb = MonitorBackground()
        mb._new_line_count = 5
        mb._output_buffer.append("line1")
        output = mb.get_output()
        assert output == ["line1"]
        assert mb.new_lines == 0

    def test_stop_no_process(self) -> None:
        """Stop when no process is running should not crash."""
        mb = MonitorBackground()
        mb.stop()  # should be a no-op

    def test_start_already_running_returns_false(self) -> None:
        mb = MonitorBackground()
        # Simulate a running process
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mb._process = mock_proc
        result = mb.start(Path("/fake/tool.py"), 60)
        assert result is False

    @patch("subprocess.Popen")
    def test_start_success(self, mock_popen: MagicMock, tmp_path: Path) -> None:
        tool_path = tmp_path / "pd_monitor.py"
        tool_path.write_text("print('monitor')")

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.readline.return_value = ""  # Empty = EOF
        mock_popen.return_value = mock_proc

        mb = MonitorBackground()
        result = mb.start(tool_path, 60)
        assert result is True
        assert mb._start_time is not None
        assert mb._duration_minutes == 60
        mb.stop()

    @patch("subprocess.Popen", side_effect=FileNotFoundError("python3 not found"))
    def test_start_file_not_found(self, mock_popen: MagicMock, tmp_path: Path) -> None:
        tool_path = tmp_path / "pd_monitor.py"
        tool_path.write_text("")
        mb = MonitorBackground()
        result = mb.start(tool_path, 60)
        assert result is False

    @patch("subprocess.Popen", side_effect=FileNotFoundError("python3 not found"))
    def test_start_file_not_found_frozen_note(
        self, mock_popen: MagicMock, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """FileNotFoundError in frozen mode logs the python3-on-PATH note."""
        tool_path = tmp_path / "pd_monitor.py"
        tool_path.write_text("")
        mb = MonitorBackground()
        original_frozen = noc_toolkit._FROZEN
        noc_toolkit._FROZEN = True
        try:
            with caplog.at_level(logging.ERROR):
                result = mb.start(tool_path, 60)
        finally:
            noc_toolkit._FROZEN = original_frozen
        assert result is False
        assert any("python3" in r.message for r in caplog.records)

    def test_stop_terminates_process(self) -> None:
        mb = MonitorBackground()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mb._process = mock_proc
        mb.stop()
        mock_proc.terminate.assert_called_once()

    def test_stop_kills_on_timeout(self) -> None:
        mb = MonitorBackground()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.wait.side_effect = subprocess.TimeoutExpired("cmd", 5)
        mb._process = mock_proc
        mb.stop()
        mock_proc.kill.assert_called_once()

    def test_status_line_active(self) -> None:
        mb = MonitorBackground()
        mb._start_time = datetime.now()
        mb._duration_minutes = 60
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mb._process = mock_proc
        status = mb.status_line()
        assert "ACTIVE" in status
        assert "60m" in status

    def test_status_line_active_with_new_lines(self) -> None:
        """status_line includes 'new' count when there are unread lines."""
        mb = MonitorBackground()
        mb._start_time = datetime.now()
        mb._duration_minutes = 30
        mb._new_line_count = 5
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mb._process = mock_proc
        status = mb.status_line()
        assert "5 new" in status

    def test_read_output_populates_buffer(self) -> None:
        """_read_output appends non-empty lines to the ring buffer."""
        mb = MonitorBackground()
        mock_proc = MagicMock()
        # readline returns two lines then empty string (EOF)
        mock_proc.stdout.readline.side_effect = ["line one\n", "line two\n", ""]
        mb._process = mock_proc

        mb._read_output()

        assert list(mb._output_buffer) == ["line one", "line two"]
        assert mb._new_line_count == 2

    def test_read_output_ignores_blank_lines(self) -> None:
        """_read_output skips lines that are empty after stripping."""
        mb = MonitorBackground()
        mock_proc = MagicMock()
        mock_proc.stdout.readline.side_effect = ["\n", "real line\n", ""]
        mb._process = mock_proc

        mb._read_output()

        assert list(mb._output_buffer) == ["real line"]

    def test_read_output_handles_value_error(self) -> None:
        """_read_output silently handles ValueError (stream closed)."""
        mb = MonitorBackground()
        mock_proc = MagicMock()
        mock_proc.stdout.readline.side_effect = ValueError("I/O operation on closed file")
        mb._process = mock_proc

        mb._read_output()  # should not raise


# ===========================================================================
# NOCToolkit
# ===========================================================================


class TestNOCToolkit:
    """Tests for the NOCToolkit class."""

    def test_init_loads_tools(self) -> None:
        toolkit = NOCToolkit()
        assert len(toolkit.tools) > 0

    def test_init_loads_config(self) -> None:
        """_config is populated during __init__."""
        toolkit = NOCToolkit()
        assert isinstance(toolkit._config, dict)

    def test_load_config_populates_config_dict(self, tmp_path: Path) -> None:
        """Config values are stored in _config dict (not in os.environ)."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("tokens:\n  _TEST_CFG_KEY_12345: injected_value\n")
        original_exe_dir = noc_toolkit._EXE_DIR
        noc_toolkit._EXE_DIR = tmp_path
        try:
            toolkit = NOCToolkit()
            # Config stays in the dict, NOT in os.environ
            assert toolkit._config.get("_TEST_CFG_KEY_12345") == "injected_value"
            assert "_TEST_CFG_KEY_12345" not in os.environ
            # _tool_env() merges config into a subprocess env copy
            tool_env = toolkit._tool_env()
            assert tool_env["_TEST_CFG_KEY_12345"] == "injected_value"
        finally:
            noc_toolkit._EXE_DIR = original_exe_dir

    def test_tool_env_does_not_overwrite_existing_env(self, tmp_path: Path) -> None:
        """_tool_env() does not overwrite already-set env vars."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("tokens:\n  _TEST_CFG_EXISTING: from_config\n")
        original_exe_dir = noc_toolkit._EXE_DIR
        noc_toolkit._EXE_DIR = tmp_path
        try:
            with patch.dict(os.environ, {"_TEST_CFG_EXISTING": "from_env"}):
                toolkit = NOCToolkit()
                tool_env = toolkit._tool_env()
                assert tool_env["_TEST_CFG_EXISTING"] == "from_env"
        finally:
            noc_toolkit._EXE_DIR = original_exe_dir
            os.environ.pop("_TEST_CFG_EXISTING", None)

    def test_load_config_handles_exception(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """_load_config returns empty dict and logs warning on failure."""
        # Create a config.yaml that exists (so is_file() is True) but make
        # load_config raise an exception during parsing.
        config_file = tmp_path / "config.yaml"
        config_file.write_text("valid: yaml")
        original_exe_dir = noc_toolkit._EXE_DIR
        noc_toolkit._EXE_DIR = tmp_path
        try:
            with patch.object(noc_toolkit, "load_config", side_effect=RuntimeError("boom")):
                with caplog.at_level(logging.WARNING):
                    toolkit = NOCToolkit()
            assert toolkit._config == {}
            assert any("Failed to load config.yaml" in m for m in caplog.messages)
        finally:
            noc_toolkit._EXE_DIR = original_exe_dir

    def test_load_config_no_config_file(self) -> None:
        """When no config.yaml exists, _config is empty."""
        toolkit = NOCToolkit()
        # Default _EXE_DIR likely has no config.yaml
        assert isinstance(toolkit._config, dict)

    def test_load_config_selective_dotenv(self, tmp_path: Path) -> None:
        """Only env vars referenced in config.yaml are loaded from .env."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "tokens:\n"
            "  _SEL_WANTED: ${_SEL_WANTED}\n"
        )
        env_file = tmp_path / ".env"
        env_file.write_text("_SEL_WANTED=from_dotenv\n_SEL_UNWANTED=should_not_load\n")
        original_exe_dir = noc_toolkit._EXE_DIR
        noc_toolkit._EXE_DIR = tmp_path
        try:
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("_SEL_WANTED", None)
                os.environ.pop("_SEL_UNWANTED", None)
                toolkit = NOCToolkit()
                # Referenced var is loaded into os.environ (for resolution)
                assert os.environ.get("_SEL_WANTED") == "from_dotenv"
                # Unreferenced var is NOT loaded
                assert "_SEL_UNWANTED" not in os.environ
                assert toolkit._config["_SEL_WANTED"] == "from_dotenv"
        finally:
            noc_toolkit._EXE_DIR = original_exe_dir
            os.environ.pop("_SEL_WANTED", None)
            os.environ.pop("_SEL_UNWANTED", None)

    def test_load_config_dotenv_skips_existing_env(self, tmp_path: Path) -> None:
        """Selective .env loading does not overwrite already-set env vars."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("tokens:\n  _SEL_EXISTS: ${_SEL_EXISTS}\n")
        env_file = tmp_path / ".env"
        env_file.write_text("_SEL_EXISTS=from_dotenv\n")
        original_exe_dir = noc_toolkit._EXE_DIR
        noc_toolkit._EXE_DIR = tmp_path
        try:
            with patch.dict(os.environ, {"_SEL_EXISTS": "already_set"}):
                toolkit = NOCToolkit()
                assert os.environ["_SEL_EXISTS"] == "already_set"
                assert toolkit._config["_SEL_EXISTS"] == "already_set"
        finally:
            noc_toolkit._EXE_DIR = original_exe_dir
            os.environ.pop("_SEL_EXISTS", None)

    def test_load_config_no_dotenv_file(self, tmp_path: Path) -> None:
        """Config with env refs works when .env file does not exist."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("tokens:\n  _NO_DOTENV: ${_NO_DOTENV}\n  LIT: value\n")
        # No .env file created
        original_exe_dir = noc_toolkit._EXE_DIR
        noc_toolkit._EXE_DIR = tmp_path
        try:
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("_NO_DOTENV", None)
                toolkit = NOCToolkit()
                assert "_NO_DOTENV" not in toolkit._config
                assert toolkit._config["LIT"] == "value"
        finally:
            noc_toolkit._EXE_DIR = original_exe_dir

    def test_get_enabled_tools(self) -> None:
        toolkit = NOCToolkit()
        enabled = toolkit.get_enabled_tools()
        assert all(t.enabled for t in enabled)
        assert len(enabled) == len(toolkit.tools)  # all enabled by default

    def test_get_enabled_tools_filters_disabled(self) -> None:
        toolkit = NOCToolkit()
        toolkit.tools[0].enabled = False
        enabled = toolkit.get_enabled_tools()
        assert len(enabled) == len(toolkit.tools) - 1

    def test_known_tool_ids(self) -> None:
        toolkit = NOCToolkit()
        tool_ids = {t.tool_id for t in toolkit.tools}
        expected = {
            "pd-sync", "pd-jobs", "pd-monitor", "pd-merge",
            "freshness", "shift-report", "pd-escalate", "pd-resolve",
            "ticket-watch",
        }
        assert tool_ids == expected

    def test_display_banner(self, caplog: pytest.LogCaptureFixture) -> None:
        toolkit = NOCToolkit()
        with caplog.at_level(logging.INFO):
            toolkit.display_banner()
        full_output = "\n".join(caplog.messages)
        assert "NOC Toolkit" in full_output
        assert noc_toolkit.VERSION in full_output

    def test_display_banner_config_loaded(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Banner shows config.yaml key count when config is loaded."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("tokens:\n  A: val1\n  B: val2\n")
        original_exe_dir = noc_toolkit._EXE_DIR
        noc_toolkit._EXE_DIR = tmp_path
        try:
            toolkit = NOCToolkit()
            with caplog.at_level(logging.INFO):
                toolkit.display_banner()
            assert any("config.yaml" in m and "2 keys" in m for m in caplog.messages)
        finally:
            noc_toolkit._EXE_DIR = original_exe_dir
            os.environ.pop("A", None)
            os.environ.pop("B", None)

    def test_display_banner_no_config(self, caplog: pytest.LogCaptureFixture) -> None:
        """Banner shows 'not found' when no config is loaded."""
        toolkit = NOCToolkit()
        toolkit._config = {}
        with caplog.at_level(logging.INFO):
            toolkit.display_banner()
        assert any("not found" in m for m in caplog.messages)

    def test_display_banner_monitor_running(self, caplog: pytest.LogCaptureFixture) -> None:
        """Banner shows monitor status line when monitor is running."""
        toolkit = NOCToolkit()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        toolkit._monitor_bg._process = mock_proc
        toolkit._monitor_bg._start_time = datetime.now()
        toolkit._monitor_bg._duration_minutes = 60
        with caplog.at_level(logging.INFO):
            toolkit.display_banner()
        assert any("PD Monitor" in m for m in caplog.messages)

    def test_display_banner_monitor_stopped(self, caplog: pytest.LogCaptureFixture) -> None:
        """Banner shows STOPPED status when monitor has run but is no longer active."""
        toolkit = NOCToolkit()
        # Simulate stopped state: _start_time set but process not running
        toolkit._monitor_bg._start_time = datetime.now()
        with caplog.at_level(logging.INFO):
            toolkit.display_banner()
        assert any("PD Monitor" in m for m in caplog.messages)

    def test_display_menu(self, caplog: pytest.LogCaptureFixture) -> None:
        toolkit = NOCToolkit()
        with caplog.at_level(logging.INFO):
            toolkit.display_menu()
        full_output = "\n".join(caplog.messages)
        assert "Available Tools" in full_output
        assert "Exit" in full_output

    def test_display_menu_no_tools(self, caplog: pytest.LogCaptureFixture) -> None:
        toolkit = NOCToolkit()
        toolkit.tools = []
        with caplog.at_level(logging.INFO):
            toolkit.display_menu()
        assert any("No tools available" in m for m in caplog.messages)

    def test_display_menu_with_monitor_running(self, caplog: pytest.LogCaptureFixture) -> None:
        """Menu annotates pd-monitor entry when monitor is running in background."""
        toolkit = NOCToolkit()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        toolkit._monitor_bg._process = mock_proc
        toolkit._monitor_bg._start_time = datetime.now()
        with caplog.at_level(logging.INFO):
            toolkit.display_menu()
        assert any("RUNNING" in m for m in caplog.messages)

    def test_display_menu_missing_script_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Missing tool script emits a WARNING log."""
        toolkit = NOCToolkit()
        # Force first tool to point at a nonexistent path
        toolkit.tools[0].script_path = Path("/nonexistent/script.py")
        with caplog.at_level(logging.WARNING):
            toolkit.display_menu()
        assert any(
            "Warning" in r.message and r.levelno == logging.WARNING
            for r in caplog.records
        )

    @patch("builtins.input", return_value="0")
    def test_get_user_choice_exit(self, mock_input: MagicMock) -> None:
        toolkit = NOCToolkit()
        assert toolkit.get_user_choice(5) == 0

    @patch("builtins.input", return_value="3")
    def test_get_user_choice_valid(self, mock_input: MagicMock) -> None:
        toolkit = NOCToolkit()
        assert toolkit.get_user_choice(5) == 3

    @patch("builtins.input", return_value="99")
    def test_get_user_choice_out_of_range(self, mock_input: MagicMock) -> None:
        toolkit = NOCToolkit()
        assert toolkit.get_user_choice(5) is None

    @patch("builtins.input", return_value="abc")
    def test_get_user_choice_non_numeric(self, mock_input: MagicMock) -> None:
        toolkit = NOCToolkit()
        assert toolkit.get_user_choice(5) is None

    @patch("builtins.input", side_effect=KeyboardInterrupt)
    def test_get_user_choice_interrupt(self, mock_input: MagicMock) -> None:
        toolkit = NOCToolkit()
        assert toolkit.get_user_choice(5) == 0

    def test_run_tool_missing_script(self, caplog: pytest.LogCaptureFixture) -> None:
        toolkit = NOCToolkit()
        td = ToolDefinition(
            tool_id="missing",
            name="Missing",
            description="",
            script_path="/nonexistent/script.py",
        )
        with caplog.at_level(logging.ERROR):
            exit_code = toolkit.run_tool(td)
        assert exit_code == 1
        assert any("Error" in r.message for r in caplog.records)

    @patch("subprocess.run")
    def test_run_tool_subprocess_success(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        script = tmp_path / "tool.py"
        script.write_text("print('hello')")
        mock_run.return_value = MagicMock(returncode=0)

        toolkit = NOCToolkit()
        td = ToolDefinition(
            tool_id="test",
            name="Test",
            description="",
            script_path=str(script),
        )
        exit_code = toolkit.run_tool(td)
        assert exit_code == 0

    @patch("subprocess.run")
    def test_run_tool_subprocess_failure(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        script = tmp_path / "tool.py"
        script.write_text("import sys; sys.exit(1)")
        mock_run.return_value = MagicMock(returncode=1)

        toolkit = NOCToolkit()
        td = ToolDefinition(
            tool_id="test",
            name="Test",
            description="",
            script_path=str(script),
        )
        exit_code = toolkit.run_tool(td)
        assert exit_code == 1

    @patch("subprocess.run", side_effect=KeyboardInterrupt)
    def test_run_tool_keyboard_interrupt(
        self, mock_run: MagicMock, tmp_path: Path
    ) -> None:
        script = tmp_path / "tool.py"
        script.write_text("")
        toolkit = NOCToolkit()
        td = ToolDefinition(
            tool_id="test",
            name="Test",
            description="",
            script_path=str(script),
        )
        exit_code = toolkit.run_tool(td)
        assert exit_code == 130

    @patch("subprocess.run", side_effect=OSError("disk full"))
    def test_run_tool_generic_exception(
        self, mock_run: MagicMock, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Unexpected exceptions in run_tool return exit code 1 and log an error."""
        script = tmp_path / "tool.py"
        script.write_text("")
        toolkit = NOCToolkit()
        td = ToolDefinition(
            tool_id="test",
            name="Test",
            description="",
            script_path=str(script),
        )
        with caplog.at_level(logging.ERROR):
            exit_code = toolkit.run_tool(td)
        assert exit_code == 1
        assert any("Error running tool" in r.message for r in caplog.records)

    def test_run_tool_frozen_in_process(self, tmp_path: Path) -> None:
        """In frozen mode, run_tool executes the script in-process via runpy."""
        script = tmp_path / "tool.py"
        script.write_text("# no-op")
        toolkit = NOCToolkit()
        td = ToolDefinition(
            tool_id="test",
            name="Test",
            description="",
            script_path=str(script),
        )
        original_frozen = noc_toolkit._FROZEN
        noc_toolkit._FROZEN = True
        try:
            exit_code = toolkit.run_tool(td)
        finally:
            noc_toolkit._FROZEN = original_frozen
        assert exit_code == 0

    def test_run_tool_frozen_adds_common_to_sys_path(self, tmp_path: Path) -> None:
        """In frozen mode, COMMON_DIR is inserted into sys.path if absent."""
        script = tmp_path / "tool.py"
        script.write_text("# no-op")
        toolkit = NOCToolkit()
        td = ToolDefinition(
            tool_id="test",
            name="Test",
            description="",
            script_path=str(script),
        )
        original_frozen = noc_toolkit._FROZEN
        noc_toolkit._FROZEN = True
        common_str = str(noc_toolkit.COMMON_DIR)
        # Temporarily remove COMMON_DIR from sys.path to force the insert branch
        original_path = sys.path[:]
        sys.path = [p for p in sys.path if p != common_str]
        try:
            exit_code = toolkit.run_tool(td)
            assert common_str in sys.path
        finally:
            noc_toolkit._FROZEN = original_frozen
            sys.path[:] = original_path
        assert exit_code == 0

    def test_run_tool_frozen_sys_exit(self, tmp_path: Path) -> None:
        """In frozen mode, SystemExit from a tool is caught and its code returned."""
        script = tmp_path / "tool.py"
        script.write_text("import sys; sys.exit(42)")
        toolkit = NOCToolkit()
        td = ToolDefinition(
            tool_id="test",
            name="Test",
            description="",
            script_path=str(script),
        )
        original_frozen = noc_toolkit._FROZEN
        noc_toolkit._FROZEN = True
        try:
            exit_code = toolkit.run_tool(td)
        finally:
            noc_toolkit._FROZEN = original_frozen
        assert exit_code == 42

    def test_run_tool_frozen_import_error(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """In frozen mode, ImportError is caught and returns exit code 1."""
        script = tmp_path / "tool.py"
        script.write_text("import nonexistent_package_xyz")
        toolkit = NOCToolkit()
        td = ToolDefinition(
            tool_id="test",
            name="Test",
            description="",
            script_path=str(script),
        )
        original_frozen = noc_toolkit._FROZEN
        noc_toolkit._FROZEN = True
        try:
            with caplog.at_level(logging.ERROR):
                exit_code = toolkit.run_tool(td)
        finally:
            noc_toolkit._FROZEN = original_frozen
        assert exit_code == 1
        assert any("Missing package" in r.message for r in caplog.records)

    # ------------------------------------------------------------------
    # _run_pd_monitor_menu
    # ------------------------------------------------------------------

    @patch("builtins.input", return_value="0")
    def test_run_pd_monitor_menu_back(self, mock_input: MagicMock) -> None:
        """Choosing 0 returns 0 (back to menu) when monitor is not running."""
        toolkit = NOCToolkit()
        td = ToolDefinition(
            tool_id="pd-monitor", name="PD Monitor", description="",
            script_path="/fake/pd_monitor.py",
        )
        assert toolkit._run_pd_monitor_menu(td) == 0

    @patch("builtins.input", side_effect=EOFError)
    def test_run_pd_monitor_menu_eof_not_running(self, mock_input: MagicMock) -> None:
        """EOFError in option input returns 0 when monitor is not running."""
        toolkit = NOCToolkit()
        td = ToolDefinition(
            tool_id="pd-monitor", name="PD Monitor", description="",
            script_path="/fake/pd_monitor.py",
        )
        assert toolkit._run_pd_monitor_menu(td) == 0

    @patch("builtins.input", side_effect=EOFError)
    def test_run_pd_monitor_menu_eof_while_running(self, mock_input: MagicMock) -> None:
        """EOFError in option input returns 0 when monitor is running."""
        toolkit = NOCToolkit()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        toolkit._monitor_bg._process = mock_proc
        toolkit._monitor_bg._start_time = datetime.now()
        td = ToolDefinition(
            tool_id="pd-monitor", name="PD Monitor", description="",
            script_path="/fake/pd_monitor.py",
        )
        assert toolkit._run_pd_monitor_menu(td) == 0

    @patch("builtins.input", return_value="9")
    def test_run_pd_monitor_menu_unknown_choice_while_running(
        self, mock_input: MagicMock
    ) -> None:
        """Unrecognised option while monitor is running returns 0."""
        toolkit = NOCToolkit()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        toolkit._monitor_bg._process = mock_proc
        toolkit._monitor_bg._start_time = datetime.now()
        td = ToolDefinition(
            tool_id="pd-monitor", name="PD Monitor", description="",
            script_path="/fake/pd_monitor.py",
        )
        assert toolkit._run_pd_monitor_menu(td) == 0

    @patch("builtins.input", side_effect=(KeyboardInterrupt,))
    def test_run_pd_monitor_menu_interrupt(self, mock_input: MagicMock) -> None:
        """KeyboardInterrupt during option selection returns 0."""
        toolkit = NOCToolkit()
        td = ToolDefinition(
            tool_id="pd-monitor", name="PD Monitor", description="",
            script_path="/fake/pd_monitor.py",
        )
        assert toolkit._run_pd_monitor_menu(td) == 0

    @patch("builtins.input", return_value="2")
    def test_run_pd_monitor_menu_foreground(
        self, mock_input: MagicMock, tmp_path: Path
    ) -> None:
        """Choosing 2 (foreground) delegates to run_tool."""
        script = tmp_path / "pd_monitor.py"
        script.write_text("# no-op")
        toolkit = NOCToolkit()
        td = ToolDefinition(
            tool_id="pd-monitor", name="PD Monitor", description="",
            script_path=str(script),
        )
        with patch.object(toolkit, "run_tool", return_value=0) as mock_run:
            result = toolkit._run_pd_monitor_menu(td)
        assert result == 0
        mock_run.assert_called_once_with(td)

    @patch("builtins.input", return_value="1")
    def test_run_pd_monitor_menu_background(self, mock_input: MagicMock) -> None:
        """Choosing 1 (background) calls _start_background_monitor."""
        toolkit = NOCToolkit()
        td = ToolDefinition(
            tool_id="pd-monitor", name="PD Monitor", description="",
            script_path="/fake/pd_monitor.py",
        )
        with patch.object(toolkit, "_start_background_monitor", return_value=0) as mock_bg:
            result = toolkit._run_pd_monitor_menu(td)
        assert result == 0
        mock_bg.assert_called_once_with(td)

    @patch("builtins.input", return_value="1")
    def test_run_pd_monitor_menu_running_view(self, mock_input: MagicMock) -> None:
        """When monitor running, option 1 shows output."""
        toolkit = NOCToolkit()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        toolkit._monitor_bg._process = mock_proc
        toolkit._monitor_bg._start_time = datetime.now()
        td = ToolDefinition(
            tool_id="pd-monitor", name="PD Monitor", description="",
            script_path="/fake/pd_monitor.py",
        )
        with patch.object(toolkit, "_view_monitor_output", return_value=0) as mock_view:
            result = toolkit._run_pd_monitor_menu(td)
        assert result == 0
        mock_view.assert_called_once()

    @patch("builtins.input", return_value="2")
    def test_run_pd_monitor_menu_running_stop(
        self, mock_input: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When monitor running, option 2 stops it."""
        toolkit = NOCToolkit()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        toolkit._monitor_bg._process = mock_proc
        toolkit._monitor_bg._start_time = datetime.now()
        td = ToolDefinition(
            tool_id="pd-monitor", name="PD Monitor", description="",
            script_path="/fake/pd_monitor.py",
        )
        with caplog.at_level(logging.INFO):
            result = toolkit._run_pd_monitor_menu(td)
        assert result == 0
        mock_proc.terminate.assert_called()

    @patch("builtins.input", return_value="3")
    def test_run_pd_monitor_menu_running_foreground(
        self, mock_input: MagicMock
    ) -> None:
        """When monitor running, option 3 stops it and runs in foreground."""
        toolkit = NOCToolkit()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        toolkit._monitor_bg._process = mock_proc
        toolkit._monitor_bg._start_time = datetime.now()
        td = ToolDefinition(
            tool_id="pd-monitor", name="PD Monitor", description="",
            script_path="/fake/pd_monitor.py",
        )
        with patch.object(toolkit, "run_tool", return_value=0) as mock_run:
            result = toolkit._run_pd_monitor_menu(td)
        assert result == 0
        mock_run.assert_called_once_with(td)

    # ------------------------------------------------------------------
    # _start_background_monitor
    # ------------------------------------------------------------------

    @patch("builtins.input", return_value="0")
    def test_start_background_monitor_cancel(self, mock_input: MagicMock) -> None:
        toolkit = NOCToolkit()
        td = ToolDefinition(
            tool_id="pd-monitor", name="PD Monitor", description="",
            script_path="/fake/pd_monitor.py",
        )
        assert toolkit._start_background_monitor(td) == 0

    @patch("builtins.input", side_effect=KeyboardInterrupt)
    def test_start_background_monitor_interrupt(self, mock_input: MagicMock) -> None:
        toolkit = NOCToolkit()
        td = ToolDefinition(
            tool_id="pd-monitor", name="PD Monitor", description="",
            script_path="/fake/pd_monitor.py",
        )
        assert toolkit._start_background_monitor(td) == 0

    @patch("builtins.input", return_value="99")
    def test_start_background_monitor_invalid_choice_uses_default(
        self, mock_input: MagicMock
    ) -> None:
        """An unrecognised duration choice falls back to 60 minutes."""
        toolkit = NOCToolkit()
        td = ToolDefinition(
            tool_id="pd-monitor", name="PD Monitor", description="",
            script_path="/fake/pd_monitor.py",
        )
        with patch.object(toolkit._monitor_bg, "start", return_value=True):
            result = toolkit._start_background_monitor(td)
        assert result == 0

    @patch("builtins.input", return_value="2")
    def test_start_background_monitor_success(self, mock_input: MagicMock) -> None:
        toolkit = NOCToolkit()
        td = ToolDefinition(
            tool_id="pd-monitor", name="PD Monitor", description="",
            script_path="/fake/pd_monitor.py",
        )
        with patch.object(toolkit._monitor_bg, "start", return_value=True):
            result = toolkit._start_background_monitor(td)
        assert result == 0

    @patch("builtins.input", return_value="1")
    def test_start_background_monitor_already_running(
        self, mock_input: MagicMock
    ) -> None:
        """Returns 1 when monitor reports it is already running."""
        toolkit = NOCToolkit()
        td = ToolDefinition(
            tool_id="pd-monitor", name="PD Monitor", description="",
            script_path="/fake/pd_monitor.py",
        )
        # start() returns False and is_running is True → "already running" branch
        with patch.object(toolkit._monitor_bg, "start", return_value=False):
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            toolkit._monitor_bg._process = mock_proc
            toolkit._monitor_bg._start_time = datetime.now()
            result = toolkit._start_background_monitor(td)
        assert result == 1

    @patch("builtins.input", return_value="1")
    def test_start_background_monitor_failed(self, mock_input: MagicMock) -> None:
        """Returns 1 when monitor fails to start (not already running)."""
        toolkit = NOCToolkit()
        td = ToolDefinition(
            tool_id="pd-monitor", name="PD Monitor", description="",
            script_path="/fake/pd_monitor.py",
        )
        with patch.object(toolkit._monitor_bg, "start", return_value=False):
            result = toolkit._start_background_monitor(td)
        assert result == 1

    # ------------------------------------------------------------------
    # _run_shift_report_menu
    # ------------------------------------------------------------------

    @patch("builtins.input", return_value="0")
    def test_run_shift_report_menu_back_unconfigured(
        self, mock_input: MagicMock
    ) -> None:
        toolkit = NOCToolkit()
        td = ToolDefinition(
            tool_id="shift-report", name="Shift Report", description="",
            script_path="/fake/shift_report.py",
        )
        with patch.dict(os.environ, {"GSHEET_WEBAPP_URL": "", "GSHEET_API_KEY": ""}):
            result = toolkit._run_shift_report_menu(td)
        assert result == 0

    @patch("builtins.input", return_value="1")
    def test_run_shift_report_menu_local_unconfigured(
        self, mock_input: MagicMock
    ) -> None:
        toolkit = NOCToolkit()
        td = ToolDefinition(
            tool_id="shift-report", name="Shift Report", description="",
            script_path="/fake/shift_report.py",
        )
        with patch.dict(os.environ, {"GSHEET_WEBAPP_URL": "", "GSHEET_API_KEY": ""}):
            with patch.object(toolkit, "run_tool", return_value=0) as mock_run:
                result = toolkit._run_shift_report_menu(td)
        assert result == 0
        mock_run.assert_called_once_with(td)

    @patch("builtins.input", return_value="1")
    def test_run_shift_report_menu_gsheet_configured(
        self, mock_input: MagicMock
    ) -> None:
        """When GSheet is configured, option 1 runs the gsheet tool."""
        toolkit = NOCToolkit()
        td = ToolDefinition(
            tool_id="shift-report", name="Shift Report", description="",
            script_path="/fake/shift_report.py",
        )
        with patch.dict(
            os.environ,
            {"GSHEET_WEBAPP_URL": "https://example.com", "GSHEET_API_KEY": "key123"},
        ):
            with patch.object(toolkit, "run_tool", return_value=0) as mock_run:
                result = toolkit._run_shift_report_menu(td)
        assert result == 0
        # run_tool should be called with the gsheet ToolDefinition
        called_td = mock_run.call_args[0][0]
        assert called_td.tool_id == "shift-report-gsheet"

    @patch("builtins.input", return_value="2")
    def test_run_shift_report_menu_gsheet_local_mode(
        self, mock_input: MagicMock
    ) -> None:
        """When GSheet is configured, option 2 runs the local (Excel) tool."""
        toolkit = NOCToolkit()
        td = ToolDefinition(
            tool_id="shift-report", name="Shift Report", description="",
            script_path="/fake/shift_report.py",
        )
        with patch.dict(
            os.environ,
            {"GSHEET_WEBAPP_URL": "https://example.com", "GSHEET_API_KEY": "key123"},
        ):
            with patch.object(toolkit, "run_tool", return_value=0) as mock_run:
                result = toolkit._run_shift_report_menu(td)
        assert result == 0
        mock_run.assert_called_once_with(td)

    @patch("builtins.input", side_effect=KeyboardInterrupt)
    def test_run_shift_report_menu_interrupt(self, mock_input: MagicMock) -> None:
        toolkit = NOCToolkit()
        td = ToolDefinition(
            tool_id="shift-report", name="Shift Report", description="",
            script_path="/fake/shift_report.py",
        )
        with patch.dict(os.environ, {"GSHEET_WEBAPP_URL": "", "GSHEET_API_KEY": ""}):
            result = toolkit._run_shift_report_menu(td)
        assert result == 0

    @patch("builtins.input", side_effect=EOFError)
    def test_run_shift_report_menu_eof_unconfigured(self, mock_input: MagicMock) -> None:
        """EOFError in the unconfigured shift-report menu returns 0."""
        toolkit = NOCToolkit()
        td = ToolDefinition(
            tool_id="shift-report", name="Shift Report", description="",
            script_path="/fake/shift_report.py",
        )
        with patch.dict(os.environ, {"GSHEET_WEBAPP_URL": "", "GSHEET_API_KEY": ""}):
            result = toolkit._run_shift_report_menu(td)
        assert result == 0

    @patch("builtins.input", return_value="0")
    def test_run_shift_report_menu_back_configured(
        self, mock_input: MagicMock
    ) -> None:
        """Choosing 0 in the configured shift-report menu returns 0."""
        toolkit = NOCToolkit()
        td = ToolDefinition(
            tool_id="shift-report", name="Shift Report", description="",
            script_path="/fake/shift_report.py",
        )
        with patch.dict(
            os.environ,
            {"GSHEET_WEBAPP_URL": "https://example.com", "GSHEET_API_KEY": "key123"},
        ):
            result = toolkit._run_shift_report_menu(td)
        assert result == 0

    @patch("builtins.input", side_effect=EOFError)
    def test_run_shift_report_menu_eof_configured(self, mock_input: MagicMock) -> None:
        """EOFError in the configured shift-report menu returns 0."""
        toolkit = NOCToolkit()
        td = ToolDefinition(
            tool_id="shift-report", name="Shift Report", description="",
            script_path="/fake/shift_report.py",
        )
        with patch.dict(
            os.environ,
            {"GSHEET_WEBAPP_URL": "https://example.com", "GSHEET_API_KEY": "key123"},
        ):
            result = toolkit._run_shift_report_menu(td)
        assert result == 0

    # ------------------------------------------------------------------
    # _view_monitor_output
    # ------------------------------------------------------------------

    @patch("builtins.input", return_value="")
    def test_view_monitor_output_empty(
        self, mock_input: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        toolkit = NOCToolkit()
        with caplog.at_level(logging.INFO):
            result = toolkit._view_monitor_output()
        assert result == 0
        assert any("no output yet" in m for m in caplog.messages)

    @patch("builtins.input", return_value="")
    def test_view_monitor_output_with_lines(
        self, mock_input: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        toolkit = NOCToolkit()
        toolkit._monitor_bg._output_buffer.append("event one")
        toolkit._monitor_bg._output_buffer.append("event two")
        with caplog.at_level(logging.INFO):
            result = toolkit._view_monitor_output()
        assert result == 0
        full_output = "\n".join(caplog.messages)
        assert "event one" in full_output
        assert "event two" in full_output

    @patch("builtins.input", side_effect=KeyboardInterrupt)
    def test_view_monitor_output_interrupt(self, mock_input: MagicMock) -> None:
        toolkit = NOCToolkit()
        result = toolkit._view_monitor_output()
        assert result == 0

    @patch("builtins.input", return_value="")
    def test_view_monitor_output_monitor_still_running(
        self, mock_input: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When monitor is still running, a note is logged in _view_monitor_output."""
        toolkit = NOCToolkit()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        toolkit._monitor_bg._process = mock_proc
        toolkit._monitor_bg._start_time = datetime.now()
        with caplog.at_level(logging.INFO):
            result = toolkit._view_monitor_output()
        assert result == 0
        assert any("continues running" in m for m in caplog.messages)

    # ------------------------------------------------------------------
    # run_interactive_menu
    # ------------------------------------------------------------------

    @patch("builtins.input", return_value="")
    def test_run_interactive_menu_no_tools(
        self, mock_input: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Menu exits immediately when no tools are available."""
        toolkit = NOCToolkit()
        toolkit.tools = []
        with caplog.at_level(logging.INFO):
            toolkit.run_interactive_menu()
        assert any("No tools available" in m for m in caplog.messages)

    def test_run_interactive_menu_exit_choice(self) -> None:
        """Choosing 0 exits the menu loop cleanly."""
        toolkit = NOCToolkit()
        inputs = iter(["0"])
        with patch("builtins.input", side_effect=inputs):
            toolkit.run_interactive_menu()

    def test_run_interactive_menu_exit_stops_monitor(self) -> None:
        """Choosing 0 stops a running background monitor before exiting."""
        toolkit = NOCToolkit()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        toolkit._monitor_bg._process = mock_proc
        toolkit._monitor_bg._start_time = datetime.now()
        with patch("builtins.input", return_value="0"):
            toolkit.run_interactive_menu()
        mock_proc.terminate.assert_called()

    def test_run_interactive_menu_invalid_then_exit(self) -> None:
        """An invalid choice loops back; a subsequent 0 exits cleanly."""
        toolkit = NOCToolkit()
        inputs = iter(["99", "0"])
        with patch("builtins.input", side_effect=inputs):
            toolkit.run_interactive_menu()

    def test_run_interactive_menu_run_tool_and_back(self, tmp_path: Path) -> None:
        """Selecting a valid tool runs it and shows completion, then 0 exits."""
        script = tmp_path / "fake_tool.py"
        script.write_text("# no-op")
        toolkit = NOCToolkit()
        # Replace all tools with a single, always-present fake tool
        toolkit.tools = [
            ToolDefinition(
                tool_id="fake",
                name="Fake Tool",
                description="For testing",
                script_path=str(script),
            )
        ]
        inputs = iter(["1", "", "0"])  # select tool, press Enter after completion, exit
        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            with patch("builtins.input", side_effect=inputs):
                toolkit.run_interactive_menu()

    def test_run_interactive_menu_pd_monitor_routed(self) -> None:
        """Selecting pd-monitor routes through _run_pd_monitor_menu then exits."""
        toolkit = NOCToolkit()
        # Inputs: select pd-monitor (index depends on tool list position), then exit
        pd_monitor_idx = next(
            str(i + 1)
            for i, t in enumerate(toolkit.get_enabled_tools())
            if t.tool_id == "pd-monitor"
        )
        inputs = iter([pd_monitor_idx, "0", "0"])
        with patch.object(
            toolkit, "_run_pd_monitor_menu", return_value=0
        ) as mock_sub:
            with patch("builtins.input", side_effect=inputs):
                toolkit.run_interactive_menu()
        mock_sub.assert_called_once()

    def test_run_interactive_menu_shift_report_routed(self) -> None:
        """Selecting shift-report routes through _run_shift_report_menu then exits."""
        toolkit = NOCToolkit()
        shift_idx = next(
            str(i + 1)
            for i, t in enumerate(toolkit.get_enabled_tools())
            if t.tool_id == "shift-report"
        )
        inputs = iter([shift_idx, "0", "0"])
        with patch.object(
            toolkit, "_run_shift_report_menu", return_value=0
        ) as mock_sub:
            with patch("builtins.input", side_effect=inputs):
                toolkit.run_interactive_menu()
        mock_sub.assert_called_once()

    def test_run_interactive_menu_tool_failure_shown(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Non-zero exit code from a tool logs the failure message."""
        script = tmp_path / "bad_tool.py"
        script.write_text("# no-op")
        toolkit = NOCToolkit()
        toolkit.tools = [
            ToolDefinition(
                tool_id="bad",
                name="Bad Tool",
                description="",
                script_path=str(script),
            )
        ]
        inputs = iter(["1", "", "0"])
        with patch("subprocess.run", return_value=MagicMock(returncode=2)):
            with patch("builtins.input", side_effect=inputs):
                with caplog.at_level(logging.INFO):
                    toolkit.run_interactive_menu()
        assert any("exited with code" in m for m in caplog.messages)


# ===========================================================================
# _write_debug_log / _append_debug
# ===========================================================================


class TestDebugLog:
    """Tests for debug logging helpers."""

    @patch.object(noc_toolkit, '_EXE_DIR')
    def test_write_debug_log_creates_file(self, mock_dir: MagicMock, tmp_path: Path) -> None:
        # Temporarily override _EXE_DIR
        original = noc_toolkit._EXE_DIR
        noc_toolkit._EXE_DIR = tmp_path
        try:
            noc_toolkit._write_debug_log()
            log_path = tmp_path / "noc-toolkit-debug.log"
            assert log_path.exists()
            content = log_path.read_text()
            assert "NOC Toolkit Debug Log" in content
            assert "Python:" in content
        finally:
            noc_toolkit._EXE_DIR = original

    def test_write_debug_log_failure_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """_write_debug_log logs a WARNING (not crashing) when writing fails."""
        original = noc_toolkit._EXE_DIR
        # Point at a file (not a directory) so Path('/nonexistent') / "..." fails
        noc_toolkit._EXE_DIR = Path("/nonexistent/dir/that/does/not/exist")
        try:
            with caplog.at_level(logging.WARNING):
                noc_toolkit._write_debug_log()
        finally:
            noc_toolkit._EXE_DIR = original
        # Should have logged a warning instead of raising
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    def test_write_debug_log_masked_env_var(self, tmp_path: Path) -> None:
        """_write_debug_log masks env vars that are set, and lists EXE dir contents."""
        original = noc_toolkit._EXE_DIR
        noc_toolkit._EXE_DIR = tmp_path
        # Place a file so the EXE directory listing loop executes (lines 127-128)
        (tmp_path / "dummy.txt").write_text("x")
        try:
            with patch.dict(os.environ, {"PAGERDUTY_API_TOKEN": "abcd1234efgh"}):
                noc_toolkit._write_debug_log()
            content = (tmp_path / "noc-toolkit-debug.log").read_text()
            # Should contain masked value, not the raw token
            assert "abcd***efgh" in content
            # EXE directory listing should include the dummy file
            assert "dummy.txt" in content
        finally:
            noc_toolkit._EXE_DIR = original

    def test_write_debug_log_tools_dir_missing(self, tmp_path: Path) -> None:
        """_write_debug_log records TOOLS_DIR absence when it doesn't exist."""
        original_exe = noc_toolkit._EXE_DIR
        original_tools = noc_toolkit.TOOLS_DIR
        noc_toolkit._EXE_DIR = tmp_path
        noc_toolkit.TOOLS_DIR = tmp_path / "nonexistent_tools"
        try:
            noc_toolkit._write_debug_log()
            content = (tmp_path / "noc-toolkit-debug.log").read_text()
            assert "TOOLS_DIR does not exist" in content
        finally:
            noc_toolkit._EXE_DIR = original_exe
            noc_toolkit.TOOLS_DIR = original_tools

    def test_append_debug(self, tmp_path: Path) -> None:
        original = noc_toolkit._EXE_DIR
        noc_toolkit._EXE_DIR = tmp_path
        try:
            # Create initial log
            (tmp_path / "noc-toolkit-debug.log").write_text("initial\n")
            noc_toolkit._append_debug("test message")
            content = (tmp_path / "noc-toolkit-debug.log").read_text()
            assert "initial" in content
            assert "test message" in content
        finally:
            noc_toolkit._EXE_DIR = original

    def test_append_debug_no_crash_on_error(self) -> None:
        """_append_debug should silently handle write errors."""
        original = noc_toolkit._EXE_DIR
        noc_toolkit._EXE_DIR = Path("/nonexistent/path")
        try:
            noc_toolkit._append_debug("should not crash")
        finally:
            noc_toolkit._EXE_DIR = original


# ===========================================================================
# main()
# ===========================================================================


class TestMain:
    """Tests for the main entry point."""

    @patch.object(NOCToolkit, 'run_interactive_menu')
    @patch.object(noc_toolkit, '_write_debug_log')
    def test_main_success(
        self, mock_debug: MagicMock, mock_menu: MagicMock
    ) -> None:
        exit_code = noc_toolkit.main()
        assert exit_code == 0
        mock_menu.assert_called_once()

    @patch.object(NOCToolkit, 'run_interactive_menu', side_effect=KeyboardInterrupt)
    @patch.object(noc_toolkit, '_write_debug_log')
    def test_main_keyboard_interrupt(
        self, mock_debug: MagicMock, mock_menu: MagicMock
    ) -> None:
        exit_code = noc_toolkit.main()
        assert exit_code == 130

    @patch.object(NOCToolkit, 'run_interactive_menu', side_effect=KeyboardInterrupt)
    @patch.object(noc_toolkit, '_write_debug_log')
    def test_main_keyboard_interrupt_stops_monitor(
        self, mock_debug: MagicMock, mock_menu: MagicMock
    ) -> None:
        """KeyboardInterrupt in main() stops a running background monitor."""
        # We need the real NOCToolkit to be instantiated, then interrupt during menu
        # The easiest way: patch run_interactive_menu to set up a running monitor first
        def _set_running_and_interrupt(self_inner):  # noqa: ANN001
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            self_inner._monitor_bg._process = mock_proc
            self_inner._monitor_bg._start_time = datetime.now()
            raise KeyboardInterrupt

        with patch.object(NOCToolkit, 'run_interactive_menu', _set_running_and_interrupt):
            exit_code = noc_toolkit.main()
        assert exit_code == 130

    @patch.object(NOCToolkit, 'run_interactive_menu', side_effect=RuntimeError("boom"))
    @patch.object(noc_toolkit, '_write_debug_log')
    def test_main_exception(
        self, mock_debug: MagicMock, mock_menu: MagicMock
    ) -> None:
        exit_code = noc_toolkit.main()
        assert exit_code == 1


# ===========================================================================
# VERSION
# ===========================================================================


class TestVersion:
    """Tests for version string."""

    def test_version_is_string(self) -> None:
        assert isinstance(noc_toolkit.VERSION, str)

    def test_version_format(self) -> None:
        parts = noc_toolkit.VERSION.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)
