"""Tests for noc_toolkit_gui.py — GUI launcher.

Tests cover all pure logic, data structures, process management, config I/O,
and per-tab logging without launching a real Tk window.  customtkinter is
mocked at import time so no display server is needed.
"""

import importlib
import io
import os
import queue
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest
import yaml

# ---------------------------------------------------------------------------
# Module import: mock customtkinter before loading noc_toolkit_gui
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Build a fake customtkinter module that satisfies all GUI-level references
_ctk_mock = MagicMock()
# CTk must be a real class so NOCToolkitGUI can inherit from it
_ctk_mock.CTk = type("CTk", (), {})
# StringVar / BooleanVar must be callable and return MagicMock instances
_ctk_mock.StringVar = MagicMock
_ctk_mock.BooleanVar = MagicMock
_ctk_mock.IntVar = MagicMock
# Widget types used in isinstance checks
_ctk_mock.CTkScrollableFrame = type("CTkScrollableFrame", (), {})

sys.modules["customtkinter"] = _ctk_mock

_spec = importlib.util.spec_from_file_location(
    "noc_toolkit_gui",
    _PROJECT_ROOT / "noc_toolkit_gui.py",
)
gui_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gui_mod)

# Convenience aliases
_CfgKey = gui_mod._CfgKey
_ToolTab = gui_mod._ToolTab
_TabState = gui_mod._TabState
CONFIG_KEYS = gui_mod.CONFIG_KEYS
TOOL_TABS = gui_mod.TOOL_TABS
_TOOL_CFG_KEYS = gui_mod._TOOL_CFG_KEYS
_LOG_BUFFER_MAX = gui_mod._LOG_BUFFER_MAX
NOCToolkitGUI = gui_mod.NOCToolkitGUI


def _make_gui(**overrides):
    """Create a NOCToolkitGUI stub without calling __init__ (no Tk window)."""
    gui = object.__new__(NOCToolkitGUI)
    gui._config = {}
    gui._tab_states = {}
    gui._tab_widgets = {}
    gui._active_tab = TOOL_TABS[0].label
    gui._master_queue = queue.Queue()
    gui._cfg_vars = {}
    gui._log_box = MagicMock()
    gui._output_label = MagicMock()
    gui._last_tool_tab = TOOL_TABS[0].label
    gui._cfg_button = MagicMock()
    gui._tab_buttons = []
    gui._tab_frames = {}
    for k, v in overrides.items():
        setattr(gui, k, v)
    return gui


# ═══════════════════════════════════════════════════════════════════════════
# 1. Pure data structures
# ═══════════════════════════════════════════════════════════════════════════


class TestCfgKey:
    """Tests for the _CfgKey registry class."""

    def test_defaults(self) -> None:
        ck = _CfgKey("tokens", "MY_KEY", "My Key")
        assert ck.section == "tokens"
        assert ck.key == "MY_KEY"
        assert ck.label == "My Key"
        assert ck.secret is False
        assert ck.tool is None
        assert ck.default == ""
        assert ck.desc == ""

    def test_secret_flag(self) -> None:
        ck = _CfgKey("tokens", "TOKEN", "Token", secret=True)
        assert ck.secret is True

    def test_tool_assignment(self) -> None:
        ck = _CfgKey("settings", "X", "X", tool="ticket-watch")
        assert ck.tool == "ticket-watch"

    def test_default_and_desc(self) -> None:
        ck = _CfgKey("settings", "K", "K", default="42", desc="A number")
        assert ck.default == "42"
        assert ck.desc == "A number"


class TestConfigKeys:
    """Tests for the CONFIG_KEYS master list."""

    def test_all_are_cfgkey_instances(self) -> None:
        for ck in CONFIG_KEYS:
            assert isinstance(ck, _CfgKey)

    def test_valid_sections(self) -> None:
        for ck in CONFIG_KEYS:
            assert ck.section in {"tokens", "settings"}, f"{ck.key} has bad section"

    def test_no_duplicate_keys(self) -> None:
        keys = [ck.key for ck in CONFIG_KEYS]
        assert len(keys) == len(set(keys)), f"Duplicate keys: {keys}"

    def test_sensitive_keys_are_secret(self) -> None:
        secret_names = {
            "PAGERDUTY_API_TOKEN", "JIRA_PERSONAL_ACCESS_TOKEN",
            "JIRA_API_TOKEN", "DATABRICKS_TOKEN", "GSHEET_API_KEY",
        }
        for ck in CONFIG_KEYS:
            if ck.key in secret_names:
                assert ck.secret is True, f"{ck.key} should be secret"

    def test_non_secret_keys_not_flagged(self) -> None:
        non_secret = {"JIRA_SERVER_URL", "DATABRICKS_HOST", "LOG_LEVEL"}
        for ck in CONFIG_KEYS:
            if ck.key in non_secret:
                assert ck.secret is False, f"{ck.key} should not be secret"


class TestToolCfgKeysIndex:
    """Tests for _TOOL_CFG_KEYS derived dict."""

    def test_tool_specific_keys_indexed(self) -> None:
        assert "ticket-watch" in _TOOL_CFG_KEYS
        tw_keys = _TOOL_CFG_KEYS["ticket-watch"]
        names = {ck.key for ck in tw_keys}
        assert "TICKET_WATCH_REPORTERS" in names
        assert "TICKET_WATCH_PROJECT" in names

    def test_no_tool_keys_for_generic_tools(self) -> None:
        assert _TOOL_CFG_KEYS.get("pd-sync", []) == []
        assert _TOOL_CFG_KEYS.get("pd-jobs", []) == []

    def test_all_indexed_tool_ids_exist_in_tool_tabs(self) -> None:
        tab_ids = {t.tool_id for t in TOOL_TABS}
        for tool_id in _TOOL_CFG_KEYS:
            assert tool_id in tab_ids, f"{tool_id} not in TOOL_TABS"


class TestToolTab:
    """Tests for the _ToolTab namedtuple."""

    def test_script_path_property(self) -> None:
        tt = _ToolTab(
            tool_id="test", label="Test", script="tools/test/test.py",
            description="desc",
        )
        assert tt.script_path == gui_mod.SCRIPT_DIR / "tools/test/test.py"

    def test_options_default_empty(self) -> None:
        tt = _ToolTab(tool_id="x", label="X", script="x.py", description="d")
        assert tt.options == []


class TestToolTabs:
    """Tests for the TOOL_TABS list."""

    def test_count(self) -> None:
        assert len(TOOL_TABS) == 10

    def test_unique_tool_ids(self) -> None:
        ids = [t.tool_id for t in TOOL_TABS]
        assert len(ids) == len(set(ids))

    def test_unique_labels(self) -> None:
        labels = [t.label for t in TOOL_TABS]
        assert len(labels) == len(set(labels))

    def test_scripts_end_with_py(self) -> None:
        for tt in TOOL_TABS:
            assert tt.script.endswith(".py"), f"{tt.tool_id} script: {tt.script}"

    def test_every_option_has_kind(self) -> None:
        for tt in TOOL_TABS:
            for opt in tt.options:
                assert "kind" in opt, f"{tt.tool_id} option missing 'kind': {opt}"

    def test_radio_options_have_values_and_default(self) -> None:
        for tt in TOOL_TABS:
            for opt in tt.options:
                if opt.get("kind") == "radio":
                    assert "values" in opt, f"{tt.tool_id} radio missing 'values'"
                    assert "default" in opt, f"{tt.tool_id} radio missing 'default'"


class TestTabState:
    """Tests for the _TabState dataclass."""

    def test_defaults(self) -> None:
        state = _TabState()
        assert state.process is None
        assert state.stdin_pipe is None
        assert state.log_buffer == []
        assert state.log_fh is None
        assert state.launch_btn is None
        assert state.stop_btn is None
        assert state.status_label is None

    def test_log_buffer_not_shared(self) -> None:
        a = _TabState()
        b = _TabState()
        a.log_buffer.append("x")
        assert b.log_buffer == []


# ═══════════════════════════════════════════════════════════════════════════
# 2. Lookup methods
# ═══════════════════════════════════════════════════════════════════════════


class TestLabelToToolId:
    """Tests for _label_to_tool_id."""

    def test_valid_labels(self) -> None:
        gui = _make_gui()
        for tt in TOOL_TABS:
            assert gui._label_to_tool_id(tt.label) == tt.tool_id

    def test_config_tab_returns_none(self) -> None:
        gui = _make_gui()
        assert gui._label_to_tool_id("Config") is None

    def test_nonexistent_returns_none(self) -> None:
        gui = _make_gui()
        assert gui._label_to_tool_id("Nonexistent") is None


class TestToolTabById:
    """Tests for _tool_tab_by_id."""

    def test_valid_ids(self) -> None:
        gui = _make_gui()
        for tt in TOOL_TABS:
            result = gui._tool_tab_by_id(tt.tool_id)
            assert result is not None
            assert result.label == tt.label

    def test_invalid_returns_none(self) -> None:
        gui = _make_gui()
        assert gui._tool_tab_by_id("nonexistent") is None


class TestCurrentToolTab:
    """Tests for _current_tool_tab."""

    def test_returns_active_tab(self) -> None:
        gui = _make_gui(_active_tab="Ticket Watch")
        result = gui._current_tool_tab()
        assert result.tool_id == "ticket-watch"

    def test_fallback_on_invalid(self) -> None:
        gui = _make_gui(_active_tab="Nonexistent")
        result = gui._current_tool_tab()
        assert result == TOOL_TABS[0]


# ═══════════════════════════════════════════════════════════════════════════
# 3. _build_command
# ═══════════════════════════════════════════════════════════════════════════


class TestBuildCommand:
    """Tests for _build_command — argv construction from widget values."""

    def _var(self, value):
        """Create a mock variable with .get() returning *value*."""
        m = MagicMock()
        m.get.return_value = value
        return m

    def test_base_command(self) -> None:
        gui = _make_gui(_tab_widgets={"pd-sync": []})
        tt = gui._tool_tab_by_id("pd-sync")
        cmd = gui._build_command(tt)
        assert cmd[0] == sys.executable
        assert cmd[1] == str(tt.script_path)
        assert len(cmd) == 2

    def test_bool_true(self) -> None:
        gui = _make_gui()
        var = self._var(True)
        gui._tab_widgets = {"pd-sync": [
            ({"kind": "bool", "flag": "--dry-run"}, var),
        ]}
        tt = gui._tool_tab_by_id("pd-sync")
        cmd = gui._build_command(tt)
        assert "--dry-run" in cmd

    def test_bool_false(self) -> None:
        gui = _make_gui()
        var = self._var(False)
        gui._tab_widgets = {"pd-sync": [
            ({"kind": "bool", "flag": "--dry-run"}, var),
        ]}
        tt = gui._tool_tab_by_id("pd-sync")
        cmd = gui._build_command(tt)
        assert "--dry-run" not in cmd

    def test_radio_empty_skipped(self) -> None:
        gui = _make_gui()
        var = self._var("")
        gui._tab_widgets = {"pd-sync": [
            ({"kind": "radio", "flag": "", "label": "mode"}, var),
        ]}
        tt = gui._tool_tab_by_id("pd-sync")
        cmd = gui._build_command(tt)
        assert len(cmd) == 2  # only base

    def test_radio_flag_value(self) -> None:
        gui = _make_gui()
        var = self._var("--check")
        gui._tab_widgets = {"pd-sync": [
            ({"kind": "radio", "flag": "", "label": "mode"}, var),
        ]}
        tt = gui._tool_tab_by_id("pd-sync")
        cmd = gui._build_command(tt)
        assert "--check" in cmd

    def test_radio_with_flag_prefix(self) -> None:
        gui = _make_gui()
        var = self._var("table")
        gui._tab_widgets = {"pd-sync": [
            ({"kind": "radio", "flag": "--format", "label": "format"}, var),
        ]}
        tt = gui._tool_tab_by_id("pd-sync")
        cmd = gui._build_command(tt)
        assert cmd[-2:] == ["--format", "table"]

    def test_radio_value_no_flag(self) -> None:
        gui = _make_gui()
        var = self._var("myvalue")
        gui._tab_widgets = {"pd-sync": [
            ({"kind": "radio", "flag": "", "label": "x"}, var),
        ]}
        tt = gui._tool_tab_by_id("pd-sync")
        cmd = gui._build_command(tt)
        assert "myvalue" in cmd

    def test_entry_with_flag(self) -> None:
        gui = _make_gui()
        var = self._var("  60  ")
        gui._tab_widgets = {"pd-sync": [
            ({"kind": "entry", "flag": "--duration", "label": "dur"}, var),
        ]}
        tt = gui._tool_tab_by_id("pd-sync")
        cmd = gui._build_command(tt)
        assert cmd[-2:] == ["--duration", "60"]

    def test_entry_empty_skipped(self) -> None:
        gui = _make_gui()
        var = self._var("   ")
        gui._tab_widgets = {"pd-sync": [
            ({"kind": "entry", "flag": "--duration", "label": "dur"}, var),
        ]}
        tt = gui._tool_tab_by_id("pd-sync")
        cmd = gui._build_command(tt)
        assert "--duration" not in cmd

    def test_entry_no_flag(self) -> None:
        gui = _make_gui()
        var = self._var("some-id")
        gui._tab_widgets = {"pd-sync": [
            ({"kind": "entry", "flag": "", "label": "x"}, var),
        ]}
        tt = gui._tool_tab_by_id("pd-sync")
        cmd = gui._build_command(tt)
        assert "some-id" in cmd

    def test_choice_default_skipped(self) -> None:
        gui = _make_gui()
        var = self._var("table")
        gui._tab_widgets = {"pd-sync": [
            ({"kind": "choice", "flag": "--format", "label": "f",
              "default": "table", "values": ["table", "json"]}, var),
        ]}
        tt = gui._tool_tab_by_id("pd-sync")
        cmd = gui._build_command(tt)
        assert "--format" not in cmd

    def test_choice_non_default_with_flag(self) -> None:
        gui = _make_gui()
        var = self._var("json")
        gui._tab_widgets = {"pd-sync": [
            ({"kind": "choice", "flag": "--format", "label": "f",
              "default": "table", "values": ["table", "json"]}, var),
        ]}
        tt = gui._tool_tab_by_id("pd-sync")
        cmd = gui._build_command(tt)
        assert cmd[-2:] == ["--format", "json"]

    def test_choice_starts_with_dash(self) -> None:
        gui = _make_gui()
        var = self._var("--verbose")
        gui._tab_widgets = {"pd-sync": [
            ({"kind": "choice", "flag": "", "label": "x",
              "default": "", "values": ["", "--verbose"]}, var),
        ]}
        tt = gui._tool_tab_by_id("pd-sync")
        cmd = gui._build_command(tt)
        assert "--verbose" in cmd

    def test_multiple_options_combined(self) -> None:
        gui = _make_gui()
        gui._tab_widgets = {"pd-sync": [
            ({"kind": "bool", "flag": "--dry-run"}, self._var(True)),
            ({"kind": "entry", "flag": "--file", "label": "f"}, self._var("report.xlsx")),
            ({"kind": "radio", "flag": "", "label": "mode"}, self._var("--check")),
        ]}
        tt = gui._tool_tab_by_id("pd-sync")
        cmd = gui._build_command(tt)
        assert "--dry-run" in cmd
        assert "--file" in cmd
        assert "report.xlsx" in cmd
        assert "--check" in cmd


# ═══════════════════════════════════════════════════════════════════════════
# 4. _tool_env
# ═══════════════════════════════════════════════════════════════════════════


class TestToolEnv:
    """Tests for _tool_env — subprocess environment overlay."""

    def test_config_keys_added(self) -> None:
        gui = _make_gui(_config={"MY_TOKEN": "secret123"})
        with patch.dict(os.environ, {}, clear=True):
            env = gui._tool_env()
        assert env["MY_TOKEN"] == "secret123"

    def test_existing_env_not_overwritten(self) -> None:
        gui = _make_gui(_config={"MY_TOKEN": "from_config"})
        with patch.dict(os.environ, {"MY_TOKEN": "from_env"}, clear=True):
            env = gui._tool_env()
        assert env["MY_TOKEN"] == "from_env"

    def test_empty_env_var_overwritten_by_config(self) -> None:
        gui = _make_gui(_config={"MY_TOKEN": "from_config"})
        with patch.dict(os.environ, {"MY_TOKEN": ""}, clear=True):
            env = gui._tool_env()
        assert env["MY_TOKEN"] == "from_config"

    def test_returns_copy(self) -> None:
        gui = _make_gui(_config={})
        env = gui._tool_env()
        env["INJECTED"] = "yes"
        assert "INJECTED" not in os.environ


# ═══════════════════════════════════════════════════════════════════════════
# 5. Config I/O
# ═══════════════════════════════════════════════════════════════════════════


class TestReadRawYaml:
    """Tests for _read_raw_yaml."""

    def test_missing_file(self, tmp_path) -> None:
        gui = _make_gui()
        with patch.object(gui_mod, "_EXE_DIR", tmp_path):
            result = gui._read_raw_yaml()
        assert result == {}

    def test_valid_yaml_dict(self, tmp_path) -> None:
        (tmp_path / "config.yaml").write_text("tokens:\n  KEY: val\n")
        gui = _make_gui()
        with patch.object(gui_mod, "_EXE_DIR", tmp_path):
            result = gui._read_raw_yaml()
        assert result == {"tokens": {"KEY": "val"}}

    def test_non_dict_root(self, tmp_path) -> None:
        (tmp_path / "config.yaml").write_text("- item1\n- item2\n")
        gui = _make_gui()
        with patch.object(gui_mod, "_EXE_DIR", tmp_path):
            result = gui._read_raw_yaml()
        assert result == {}

    def test_empty_file(self, tmp_path) -> None:
        (tmp_path / "config.yaml").write_text("")
        gui = _make_gui()
        with patch.object(gui_mod, "_EXE_DIR", tmp_path):
            result = gui._read_raw_yaml()
        assert result == {}


class TestOnSaveConfig:
    """Tests for _on_save_config — YAML dict assembly logic."""

    def test_empty_values_omitted(self, tmp_path) -> None:
        gui = _make_gui()
        var = MagicMock()
        var.get.return_value = ""
        gui._cfg_vars = {"PAGERDUTY_API_TOKEN": var}
        gui._log = MagicMock()
        gui._cfg_save_status = MagicMock()
        with patch.object(gui_mod, "_EXE_DIR", tmp_path):
            gui._on_save_config()
        content = yaml.safe_load((tmp_path / "config.yaml").read_text())
        # Empty values are skipped entirely
        assert content is None or "PAGERDUTY_API_TOKEN" not in content.get("tokens", {})

    def test_numeric_string_stored_as_int(self, tmp_path) -> None:
        gui = _make_gui()
        var = MagicMock()
        var.get.return_value = "12345"
        gui._cfg_vars = {"DATABRICKS_WAREHOUSE_ID": var}
        gui._log = MagicMock()
        gui._cfg_save_status = MagicMock()
        gui._on_reload_config = MagicMock()
        with patch.object(gui_mod, "_EXE_DIR", tmp_path):
            gui._on_save_config()
        content = yaml.safe_load((tmp_path / "config.yaml").read_text())
        # Find the value — it's under "settings" section for this key
        section = None
        for ck in CONFIG_KEYS:
            if ck.key == "DATABRICKS_WAREHOUSE_ID":
                section = ck.section
                break
        assert content[section]["DATABRICKS_WAREHOUSE_ID"] == 12345

    def test_string_value_preserved(self, tmp_path) -> None:
        gui = _make_gui()
        var = MagicMock()
        var.get.return_value = "https://example.com"
        gui._cfg_vars = {"JIRA_SERVER_URL": var}
        gui._log = MagicMock()
        gui._cfg_save_status = MagicMock()
        gui._on_reload_config = MagicMock()
        with patch.object(gui_mod, "_EXE_DIR", tmp_path):
            gui._on_save_config()
        content = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert content["tokens"]["JIRA_SERVER_URL"] == "https://example.com"

    def test_write_error_handled(self, tmp_path) -> None:
        gui = _make_gui()
        gui._cfg_vars = {}
        gui._log = MagicMock()
        gui._cfg_save_status = MagicMock()
        with patch.object(gui_mod, "_EXE_DIR", tmp_path):
            with patch("builtins.open", side_effect=PermissionError("denied")):
                gui._on_save_config()
        gui._cfg_save_status.configure.assert_called()
        # Verify error was logged
        gui._log.assert_called()
        assert "denied" in str(gui._log.call_args)


# ═══════════════════════════════════════════════════════════════════════════
# 6. Per-tab logging
# ═══════════════════════════════════════════════════════════════════════════


class TestLogToTab:
    """Tests for _log_to_tab — per-tab log dispatch."""

    def test_appends_to_buffer(self) -> None:
        gui = _make_gui()
        gui._tab_states = {"pd-sync": _TabState()}
        gui._log_to_tab("pd-sync", "hello\n")
        assert gui._tab_states["pd-sync"].log_buffer == ["hello\n"]

    def test_buffer_cap(self) -> None:
        gui = _make_gui()
        state = _TabState()
        state.log_buffer = [f"line-{i}\n" for i in range(_LOG_BUFFER_MAX)]
        gui._tab_states = {"pd-sync": state}
        gui._log_to_tab("pd-sync", "overflow\n")
        assert len(state.log_buffer) == _LOG_BUFFER_MAX
        assert state.log_buffer[-1] == "overflow\n"
        assert state.log_buffer[0] == "line-1\n"  # first line dropped

    def test_writes_to_file(self) -> None:
        gui = _make_gui()
        fh = io.StringIO()
        state = _TabState()
        state.log_fh = fh
        gui._tab_states = {"pd-sync": state}
        gui._log_to_tab("pd-sync", "file-data\n")
        assert fh.getvalue() == "file-data\n"

    def test_file_write_error_ignored(self) -> None:
        gui = _make_gui()
        fh = MagicMock()
        fh.write.side_effect = OSError("disk full")
        state = _TabState()
        state.log_fh = fh
        gui._tab_states = {"pd-sync": state}
        gui._log_to_tab("pd-sync", "data\n")  # should not raise
        assert state.log_buffer == ["data\n"]

    def test_updates_logbox_when_active(self) -> None:
        gui = _make_gui(_active_tab=TOOL_TABS[0].label)
        gui._tab_states = {TOOL_TABS[0].tool_id: _TabState()}
        gui._log_to_tab(TOOL_TABS[0].tool_id, "visible\n")
        gui._log_box.configure.assert_called()

    def test_skips_logbox_when_inactive(self) -> None:
        gui = _make_gui(_active_tab="Ticket Watch")
        gui._tab_states = {"pd-sync": _TabState()}
        gui._log_to_tab("pd-sync", "invisible\n")
        gui._log_box.configure.assert_not_called()

    def test_unknown_tool_id_ignored(self) -> None:
        gui = _make_gui()
        gui._tab_states = {}
        gui._log_to_tab("nonexistent", "data\n")  # should not raise


# ═══════════════════════════════════════════════════════════════════════════
# 7. Process management
# ═══════════════════════════════════════════════════════════════════════════


class TestOnLaunch:
    """Tests for _on_launch — subprocess spawning."""

    def test_none_state_returns(self) -> None:
        gui = _make_gui(_tab_states={})
        gui._on_launch("nonexistent")  # should not raise

    def test_already_running(self) -> None:
        gui = _make_gui()
        proc = MagicMock()
        proc.poll.return_value = None  # still running
        state = _TabState(process=proc)
        gui._tab_states = {"pd-sync": state}
        gui._log_to_tab = MagicMock()
        gui._on_launch("pd-sync")
        gui._log_to_tab.assert_called_once()
        assert "Already running" in str(gui._log_to_tab.call_args)

    def test_script_not_found(self) -> None:
        gui = _make_gui()
        state = _TabState()
        gui._tab_states = {"pd-sync": state}
        gui._log_to_tab = MagicMock()
        gui._build_command = MagicMock(return_value=["python", "x.py"])
        gui._tool_env = MagicMock(return_value={})
        with patch.object(gui_mod, "LOGS_DIR", Path("/tmp/test_logs")):
            # Make script_path not exist
            with patch.object(
                _ToolTab, "script_path",
                new_callable=lambda: property(lambda self: Path("/nonexistent/x.py")),
            ):
                gui._on_launch("pd-sync")
        assert any("not found" in str(c) for c in gui._log_to_tab.call_args_list)

    def test_success_spawns_process(self, tmp_path) -> None:
        gui = _make_gui()
        state = _TabState()
        state.launch_btn = MagicMock()
        state.stop_btn = MagicMock()
        state.status_label = MagicMock()
        gui._tab_states = {"pd-sync": state}
        gui._log_to_tab = MagicMock()
        gui._build_command = MagicMock(return_value=["python", "test.py"])
        gui._tool_env = MagicMock(return_value={"PATH": "/usr/bin"})

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()

        logs_dir = tmp_path / "logs"
        with patch.object(gui_mod, "LOGS_DIR", logs_dir):
            with patch("subprocess.Popen", return_value=mock_proc):
                with patch("threading.Thread") as mock_thread:
                    mock_thread_inst = MagicMock()
                    mock_thread.return_value = mock_thread_inst
                    gui._on_launch("pd-sync")

        assert state.process is mock_proc
        assert state.stdin_pipe is mock_proc.stdin
        state.launch_btn.configure.assert_called_with(state="disabled")
        state.stop_btn.configure.assert_called_with(state="normal")
        mock_thread_inst.start.assert_called_once()
        assert logs_dir.is_dir()

    def test_popen_error_handled(self, tmp_path) -> None:
        gui = _make_gui()
        state = _TabState()
        gui._tab_states = {"pd-sync": state}
        gui._log_to_tab = MagicMock()
        gui._build_command = MagicMock(return_value=["python", "test.py"])
        gui._tool_env = MagicMock(return_value={})

        with patch.object(gui_mod, "LOGS_DIR", tmp_path / "logs"):
            with patch("subprocess.Popen", side_effect=FileNotFoundError("no python")):
                gui._on_launch("pd-sync")

        assert any("Failed to start" in str(c) for c in gui._log_to_tab.call_args_list)
        assert state.log_fh is None  # cleaned up


class TestOnStop:
    """Tests for _on_stop — process termination."""

    def test_terminates_running_process(self) -> None:
        gui = _make_gui()
        proc = MagicMock()
        proc.poll.return_value = None
        state = _TabState(process=proc, launch_btn=MagicMock(),
                          stop_btn=MagicMock(), status_label=MagicMock())
        gui._tab_states = {"pd-sync": state}
        gui._log_to_tab = MagicMock()
        gui._on_stop("pd-sync")
        proc.terminate.assert_called_once()
        assert state.stdin_pipe is None

    def test_kills_on_timeout(self) -> None:
        gui = _make_gui()
        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.side_effect = subprocess.TimeoutExpired(cmd="x", timeout=5)
        state = _TabState(process=proc, launch_btn=MagicMock(),
                          stop_btn=MagicMock(), status_label=MagicMock())
        gui._tab_states = {"pd-sync": state}
        gui._log_to_tab = MagicMock()
        gui._on_stop("pd-sync")
        proc.kill.assert_called_once()

    def test_already_stopped(self) -> None:
        gui = _make_gui()
        proc = MagicMock()
        proc.poll.return_value = 0  # already exited
        state = _TabState(process=proc, launch_btn=MagicMock(),
                          stop_btn=MagicMock(), status_label=MagicMock())
        gui._tab_states = {"pd-sync": state}
        gui._log_to_tab = MagicMock()
        gui._on_stop("pd-sync")
        proc.terminate.assert_not_called()

    def test_none_state(self) -> None:
        gui = _make_gui(_tab_states={})
        gui._on_stop("nonexistent")  # should not raise


class TestReadProcessOutput:
    """Tests for _read_process_output — thread reading stdout."""

    def test_reads_lines_into_queue(self) -> None:
        gui = _make_gui()
        proc = MagicMock()
        proc.stdout.readline.side_effect = ["line1\n", "line2\n", ""]
        proc.wait.return_value = 0
        state = _TabState(process=proc)
        gui._tab_states = {"pd-sync": state}

        gui._read_process_output("pd-sync")

        items = []
        while not gui._master_queue.empty():
            items.append(gui._master_queue.get())
        assert items == [
            ("pd-sync", "line1\n"),
            ("pd-sync", "line2\n"),
            ("pd-sync", None),
        ]

    def test_valueerror_ignored(self) -> None:
        gui = _make_gui()
        proc = MagicMock()
        proc.stdout.readline.side_effect = ValueError("closed")
        proc.wait.return_value = 1
        state = _TabState(process=proc)
        gui._tab_states = {"pd-sync": state}

        gui._read_process_output("pd-sync")

        items = []
        while not gui._master_queue.empty():
            items.append(gui._master_queue.get())
        assert items == [("pd-sync", None)]

    def test_none_process(self) -> None:
        gui = _make_gui()
        state = _TabState(process=None)
        gui._tab_states = {"pd-sync": state}
        gui._read_process_output("pd-sync")  # should return immediately
        assert gui._master_queue.empty()


class TestPollOutput:
    """Tests for _poll_output — queue drain loop."""

    def test_dispatches_lines(self) -> None:
        gui = _make_gui()
        gui._tab_states = {"pd-sync": _TabState()}
        gui._log_to_tab = MagicMock()
        gui.after = MagicMock()
        gui._master_queue.put(("pd-sync", "line1\n"))
        gui._master_queue.put(("pd-sync", "line2\n"))

        gui._poll_output()

        assert gui._log_to_tab.call_count == 2
        gui.after.assert_called_once_with(100, gui._poll_output)

    def test_sentinel_triggers_finish(self) -> None:
        gui = _make_gui()
        proc = MagicMock()
        proc.returncode = 0
        state = _TabState(process=proc, launch_btn=MagicMock(),
                          stop_btn=MagicMock(), status_label=MagicMock())
        gui._tab_states = {"pd-sync": state}
        gui._log_to_tab = MagicMock()
        gui.after = MagicMock()
        gui._master_queue.put(("pd-sync", None))

        gui._poll_output()

        assert any("Finished" in str(c) for c in gui._log_to_tab.call_args_list)
        assert state.stdin_pipe is None

    def test_unknown_tool_id_skipped(self) -> None:
        gui = _make_gui()
        gui._tab_states = {}
        gui._log_to_tab = MagicMock()
        gui.after = MagicMock()
        gui._master_queue.put(("unknown", "data\n"))

        gui._poll_output()
        gui._log_to_tab.assert_not_called()

    def test_always_reschedules(self) -> None:
        gui = _make_gui()
        gui.after = MagicMock()
        gui._poll_output()
        gui.after.assert_called_once_with(100, gui._poll_output)


class TestOnClearLog:
    """Tests for _on_clear_log."""

    def test_clears_active_tab_buffer(self) -> None:
        gui = _make_gui(_active_tab=TOOL_TABS[0].label)
        state = _TabState()
        state.log_buffer = ["old data\n"]
        gui._tab_states = {TOOL_TABS[0].tool_id: state}
        gui._on_clear_log()
        assert state.log_buffer == []
        gui._log_box.configure.assert_called()

    def test_config_tab_clears_display_only(self) -> None:
        gui = _make_gui(_active_tab="Config")
        gui._tab_states = {}
        gui._on_clear_log()  # should not raise
        gui._log_box.configure.assert_called()


class TestOnSendInput:
    """Tests for _on_send_input."""

    def test_sends_to_active_process(self) -> None:
        gui = _make_gui(_active_tab=TOOL_TABS[0].label)
        stdin = MagicMock()
        state = _TabState(stdin_pipe=stdin)
        gui._tab_states = {TOOL_TABS[0].tool_id: state}
        gui._input_var = MagicMock()
        gui._input_var.get.return_value = "hello"
        gui._log_to_tab = MagicMock()

        gui._on_send_input()

        stdin.write.assert_called_once_with("hello\n")
        stdin.flush.assert_called_once()
        gui._input_var.set.assert_called_with("")

    def test_empty_input_ignored(self) -> None:
        gui = _make_gui()
        gui._input_var = MagicMock()
        gui._input_var.get.return_value = ""
        gui._on_send_input()
        gui._input_var.set.assert_not_called()

    def test_broken_pipe_handled(self) -> None:
        gui = _make_gui(_active_tab=TOOL_TABS[0].label)
        stdin = MagicMock()
        stdin.write.side_effect = BrokenPipeError()
        state = _TabState(stdin_pipe=stdin)
        gui._tab_states = {TOOL_TABS[0].tool_id: state}
        gui._input_var = MagicMock()
        gui._input_var.get.return_value = "hello"
        gui._log_to_tab = MagicMock()

        gui._on_send_input()  # should not raise
        assert any("closed" in str(c) for c in gui._log_to_tab.call_args_list)

    def test_config_tab_no_crash(self) -> None:
        gui = _make_gui(_active_tab="Config")
        gui._input_var = MagicMock()
        gui._input_var.get.return_value = "hello"
        gui._on_send_input()  # should return without error


# ═══════════════════════════════════════════════════════════════════════════
# 8. Helpers
# ═══════════════════════════════════════════════════════════════════════════


class TestResetTabButtons:
    """Tests for _reset_tab_buttons."""

    def test_restores_buttons(self) -> None:
        gui = _make_gui()
        state = _TabState(
            launch_btn=MagicMock(), stop_btn=MagicMock(),
            status_label=MagicMock(),
        )
        gui._tab_states = {"pd-sync": state}
        gui._reset_tab_buttons("pd-sync")
        state.launch_btn.configure.assert_called_with(state="normal")
        state.stop_btn.configure.assert_called_with(state="disabled")

    def test_none_state(self) -> None:
        gui = _make_gui(_tab_states={})
        gui._reset_tab_buttons("nonexistent")  # should not raise


class TestCloseTabLog:
    """Tests for _close_tab_log."""

    def test_closes_file(self) -> None:
        gui = _make_gui()
        fh = MagicMock()
        state = _TabState()
        state.log_fh = fh
        gui._tab_states = {"pd-sync": state}
        gui._close_tab_log("pd-sync")
        fh.close.assert_called_once()
        assert state.log_fh is None

    def test_already_none(self) -> None:
        gui = _make_gui()
        state = _TabState()
        gui._tab_states = {"pd-sync": state}
        gui._close_tab_log("pd-sync")  # should not raise

    def test_oserror_ignored(self) -> None:
        gui = _make_gui()
        fh = MagicMock()
        fh.close.side_effect = OSError("nfs stale")
        state = _TabState()
        state.log_fh = fh
        gui._tab_states = {"pd-sync": state}
        gui._close_tab_log("pd-sync")  # should not raise
        assert state.log_fh is None


class TestOnToggleConfig:
    """Tests for _on_toggle_config."""

    def test_switches_to_config(self) -> None:
        gui = _make_gui(_active_tab=TOOL_TABS[0].label)
        gui._switch_tab = MagicMock()
        gui._on_toggle_config()
        gui._switch_tab.assert_called_with("Config")

    def test_switches_back_to_last_tool(self) -> None:
        gui = _make_gui(_active_tab="Config", _last_tool_tab="Ticket Watch")
        gui._switch_tab = MagicMock()
        gui._on_toggle_config()
        gui._switch_tab.assert_called_with("Ticket Watch")
