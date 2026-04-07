"""Tests for noc_utils.py shared utilities."""

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from noc_utils import (
    _MaxLevelFilter,
    _resolve_value,
    extract_env_references,
    load_config,
    require_env,
    new_pd_client,
    new_jira_client,
    parse_iso_dt,
    setup_logging,
)


# ===========================================================================
# _MaxLevelFilter
# ===========================================================================


class TestMaxLevelFilter:
    """Tests for the _MaxLevelFilter helper."""

    def test_allows_below_max(self) -> None:
        filt = _MaxLevelFilter(logging.WARNING)
        record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)
        assert filt.filter(record) is True

    def test_blocks_at_max(self) -> None:
        filt = _MaxLevelFilter(logging.WARNING)
        record = logging.LogRecord("test", logging.WARNING, "", 0, "msg", (), None)
        assert filt.filter(record) is False

    def test_blocks_above_max(self) -> None:
        filt = _MaxLevelFilter(logging.WARNING)
        record = logging.LogRecord("test", logging.ERROR, "", 0, "msg", (), None)
        assert filt.filter(record) is False


# ===========================================================================
# setup_logging
# ===========================================================================


class TestSetupLogging:
    """Tests for the structured logging setup."""

    def test_returns_logger(self) -> None:
        logger = setup_logging(name="test_setup")
        assert isinstance(logger, logging.Logger)
        assert logger.name == "test_setup"

    def test_verbose_sets_debug_level(self) -> None:
        logger = setup_logging(verbose=True, name="test_verbose")
        assert logger.level == logging.DEBUG

    def test_non_verbose_sets_info_level(self) -> None:
        logger = setup_logging(verbose=False, name="test_info")
        assert logger.level == logging.INFO

    def test_stdout_handler_for_info(self) -> None:
        logger = setup_logging(name="test_stdout_handler")
        assert len(logger.handlers) >= 1
        stdout_handler = logger.handlers[0]
        assert isinstance(stdout_handler, logging.StreamHandler)
        assert stdout_handler.stream is sys.stdout

    def test_stderr_handler_for_warnings(self) -> None:
        logger = setup_logging(name="test_stderr_handler")
        assert len(logger.handlers) >= 2
        stderr_handler = logger.handlers[1]
        assert isinstance(stderr_handler, logging.StreamHandler)
        assert stderr_handler.stream is sys.stderr

    def test_does_not_duplicate_handlers(self) -> None:
        """Calling setup_logging twice should not add extra handlers."""
        logger = setup_logging(name="test_no_dup")
        handler_count = len(logger.handlers)
        setup_logging(name="test_no_dup")
        assert len(logger.handlers) == handler_count

    def test_stdout_formatter_is_plain(self) -> None:
        logger = setup_logging(name="test_stdout_fmt")
        stdout_handler = logger.handlers[0]
        fmt = stdout_handler.formatter
        assert fmt is not None
        assert "%(message)s" in fmt._fmt

    def test_stderr_formatter_has_timestamp(self) -> None:
        logger = setup_logging(name="test_stderr_fmt")
        stderr_handler = logger.handlers[1]
        fmt = stderr_handler.formatter
        assert fmt is not None
        assert "%(asctime)s" in fmt._fmt
        assert "%(levelname)s" in fmt._fmt

    def test_none_name_returns_root_derived(self) -> None:
        logger = setup_logging(name=None)
        assert logger.name == "root"

    def test_stdout_handler_has_max_level_filter(self) -> None:
        logger = setup_logging(name="test_max_filter")
        stdout_handler = logger.handlers[0]
        filters = stdout_handler.filters
        assert any(isinstance(f, _MaxLevelFilter) for f in filters)


# ===========================================================================
# _resolve_value
# ===========================================================================


class TestResolveValue:
    """Tests for the _resolve_value helper."""

    def test_none_returns_none(self) -> None:
        assert _resolve_value(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert _resolve_value("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert _resolve_value("   ") is None

    def test_literal_string(self) -> None:
        assert _resolve_value("https://jira.example.com") == "https://jira.example.com"

    def test_integer_converted_to_string(self) -> None:
        assert _resolve_value(42) == "42"

    def test_float_converted_to_string(self) -> None:
        assert _resolve_value(3.14) == "3.14"

    def test_bool_converted_to_string(self) -> None:
        assert _resolve_value(True) == "True"

    @patch.dict(os.environ, {"MY_TOKEN": "secret123"})
    def test_env_var_reference_resolved(self) -> None:
        assert _resolve_value("${MY_TOKEN}") == "secret123"

    @patch.dict(os.environ, {}, clear=True)
    def test_env_var_reference_unset_returns_none(self) -> None:
        assert _resolve_value("${NONEXISTENT_VAR}") is None

    @patch.dict(os.environ, {"EMPTY_VAR": ""}, clear=True)
    def test_env_var_reference_empty_returns_none(self) -> None:
        assert _resolve_value("${EMPTY_VAR}") is None

    @patch.dict(os.environ, {"HOST": "jira.example.com"})
    def test_mixed_env_var_substitution(self) -> None:
        assert _resolve_value("https://${HOST}/browse") == "https://jira.example.com/browse"

    @patch.dict(os.environ, {}, clear=True)
    def test_mixed_env_var_unset_becomes_empty(self) -> None:
        assert _resolve_value("https://${MISSING}/browse") == "https:///browse"

    @patch.dict(os.environ, {"A": "aaa", "B": "bbb"})
    def test_multiple_env_var_references(self) -> None:
        assert _resolve_value("${A}-${B}") == "aaa-bbb"


# ===========================================================================
# load_config
# ===========================================================================


# ===========================================================================
# extract_env_references
# ===========================================================================


class TestExtractEnvReferences:
    """Tests for extracting ${VAR} names from raw config."""

    def test_no_config_returns_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        with patch("sys.argv", [str(tmp_path / "fake.py")]):
            assert extract_env_references() == set()

    def test_explicit_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            extract_env_references(str(tmp_path / "missing.yaml"))

    def test_empty_yaml_returns_empty(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.yaml"
        cfg.write_text("")
        assert extract_env_references(str(cfg)) == set()

    def test_extracts_var_names(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "tokens:\n"
            "  PD: ${PAGERDUTY_API_TOKEN}\n"
            "  JIRA: https://${HOST}/browse\n"
            "  LITERAL: plain_value\n"
            "TOP_VAR: ${TOP_LEVEL}\n"
        )
        refs = extract_env_references(str(cfg))
        assert refs == {"PAGERDUTY_API_TOKEN", "HOST", "TOP_LEVEL"}

    def test_non_string_values_ignored(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.yaml"
        cfg.write_text("settings:\n  COUNT: 42\n  FLAG: true\n")
        assert extract_env_references(str(cfg)) == set()

    def test_non_dict_root_returns_empty(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.yaml"
        cfg.write_text("- item1\n- item2\n")
        assert extract_env_references(str(cfg)) == set()


# ===========================================================================
# load_config
# ===========================================================================


class TestLoadConfig:
    """Tests for the YAML config loader."""

    def test_no_config_file_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """When no config.yaml exists, returns an empty dict."""
        monkeypatch.chdir(tmp_path)
        with patch("sys.argv", [str(tmp_path / "fake.py")]):
            with caplog.at_level(logging.INFO, logger="noc_utils"):
                result = load_config()
        assert result == {}
        assert any("No config.yaml found" in m for m in caplog.messages)

    def test_explicit_path_not_found_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_config(str(tmp_path / "missing.yaml"))

    def test_empty_yaml_returns_empty(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("")
        result = load_config(str(config_file))
        assert result == {}

    def test_non_dict_root_returns_empty(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("- item1\n- item2\n")
        with caplog.at_level(logging.WARNING, logger="noc_utils"):
            result = load_config(str(config_file))
        assert result == {}
        assert any("not a mapping" in m for m in caplog.messages)

    @patch.dict(os.environ, {"PD_TOKEN": "pd-secret"})
    def test_tokens_section_resolved(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "tokens:\n"
            "  PAGERDUTY_API_TOKEN: ${PD_TOKEN}\n"
            "  JIRA_SERVER_URL: https://jira.example.com\n"
        )
        result = load_config(str(config_file))
        assert result["PAGERDUTY_API_TOKEN"] == "pd-secret"
        assert result["JIRA_SERVER_URL"] == "https://jira.example.com"

    @patch.dict(os.environ, {}, clear=True)
    def test_unset_env_vars_omitted(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "tokens:\n"
            "  MISSING_TOKEN: ${TOTALLY_MISSING}\n"
            "  PRESENT: literal_value\n"
        )
        result = load_config(str(config_file))
        assert "MISSING_TOKEN" not in result
        assert result["PRESENT"] == "literal_value"

    def test_top_level_scalar(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("LOG_LEVEL: INFO\n")
        result = load_config(str(config_file))
        assert result["LOG_LEVEL"] == "INFO"

    def test_settings_section(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "settings:\n"
            "  TICKET_WATCH_PROJECT: DSSD\n"
            "  RETRY_COUNT: 3\n"
        )
        result = load_config(str(config_file))
        assert result["TICKET_WATCH_PROJECT"] == "DSSD"
        assert result["RETRY_COUNT"] == "3"

    def test_auto_discover_next_to_argv(self, tmp_path: Path) -> None:
        """load_config() with no args discovers config.yaml next to sys.argv[0]."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("tokens:\n  KEY: value\n")
        with patch("sys.argv", [str(tmp_path / "main.py")]):
            result = load_config()
        assert result["KEY"] == "value"

    def test_auto_discover_cwd_fallback(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """load_config() falls back to CWD if config.yaml not next to argv[0]."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("tokens:\n  CWD_KEY: from_cwd\n")
        monkeypatch.chdir(tmp_path)
        with patch("sys.argv", ["/nonexistent/main.py"]):
            result = load_config()
        assert result["CWD_KEY"] == "from_cwd"

    def test_invalid_yaml_raises(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("tokens:\n  bad: [unterminated\n")
        import yaml
        with pytest.raises(yaml.YAMLError):
            load_config(str(config_file))

    def test_top_level_none_value_omitted(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("EMPTY_KEY:\n")
        result = load_config(str(config_file))
        assert "EMPTY_KEY" not in result


# ===========================================================================
# require_env
# ===========================================================================


class TestRequireEnv:
    """Tests for required environment variable validation."""

    @patch.dict(os.environ, {"VAR_A": "value_a", "VAR_B": "value_b"})
    def test_returns_values_when_set(self) -> None:
        result = require_env("VAR_A", "VAR_B")
        assert result == {"VAR_A": "value_a", "VAR_B": "value_b"}

    @patch.dict(os.environ, {"VAR_A": "value_a"}, clear=True)
    def test_exits_when_missing(self) -> None:
        with pytest.raises(SystemExit) as exc_info:
            require_env("VAR_A", "MISSING_VAR")
        assert exc_info.value.code == 1

    @patch.dict(os.environ, {"VAR_A": ""}, clear=True)
    def test_empty_string_treated_as_missing(self) -> None:
        with pytest.raises(SystemExit):
            require_env("VAR_A")

    @patch.dict(os.environ, {"X": "1", "Y": "2", "Z": "3"})
    def test_multiple_vars(self) -> None:
        result = require_env("X", "Y", "Z")
        assert len(result) == 3

    def test_no_args_returns_empty(self) -> None:
        result = require_env()
        assert result == {}

    @patch.dict(os.environ, {}, clear=True)
    def test_error_message_lists_missing_vars(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.ERROR, logger="noc_utils"):
            with pytest.raises(SystemExit):
                require_env("FOO", "BAR")
        assert "FOO" in caplog.text
        assert "BAR" in caplog.text


# ===========================================================================
# new_pd_client
# ===========================================================================


class TestNewPdClient:
    """Tests for PagerDuty client factory."""

    @patch("noc_utils._pagerduty")
    def test_returns_client(self, mock_pd: MagicMock) -> None:
        mock_pd.RestApiV2Client.return_value = MagicMock()
        client = new_pd_client("test-token")
        mock_pd.RestApiV2Client.assert_called_once_with("test-token")
        assert client is not None

    @patch("noc_utils._pagerduty")
    def test_suppresses_warnings(self, mock_pd: MagicMock) -> None:
        import warnings
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            new_pd_client("token")
        # The filterwarnings call should suppress "more" property warnings
        # (we just verify no crash)


# ===========================================================================
# new_jira_client
# ===========================================================================


class TestNewJiraClient:
    """Tests for Jira client factory."""

    @patch("noc_utils.JIRA")
    def test_returns_client_and_browse_url(self, mock_jira_cls: MagicMock) -> None:
        mock_jira_cls.return_value = MagicMock()
        client, browse_url = new_jira_client(
            "https://jira.example.com", "pat-token"
        )
        assert client is not None
        assert browse_url == "https://jira.example.com/browse"

    @patch("noc_utils.JIRA")
    def test_strips_trailing_slash(self, mock_jira_cls: MagicMock) -> None:
        mock_jira_cls.return_value = MagicMock()
        _, browse_url = new_jira_client(
            "https://jira.example.com/", "pat-token"
        )
        assert browse_url == "https://jira.example.com/browse"

    @patch("noc_utils.JIRA")
    def test_uses_token_auth(self, mock_jira_cls: MagicMock) -> None:
        new_jira_client("https://jira.example.com", "my-pat")
        call_kwargs = mock_jira_cls.call_args[1]
        assert call_kwargs["token_auth"] == "my-pat"
        assert call_kwargs["server"] == "https://jira.example.com"


# ===========================================================================
# parse_iso_dt
# ===========================================================================


class TestParseIsoDt:
    """Tests for ISO datetime parsing."""

    def test_z_suffix(self) -> None:
        result = parse_iso_dt("2026-03-20T08:30:15Z")
        assert result.year == 2026
        assert result.month == 3
        assert result.day == 20
        assert result.hour == 8
        assert result.minute == 30
        assert result.tzinfo is not None

    def test_offset_format(self) -> None:
        result = parse_iso_dt("2026-03-20T08:30:15+05:00")
        assert result.year == 2026
        assert result.tzinfo is not None

    def test_utc_offset(self) -> None:
        result = parse_iso_dt("2026-03-20T08:30:15+00:00")
        assert result.tzinfo is not None

    def test_negative_offset(self) -> None:
        result = parse_iso_dt("2026-03-20T08:30:15-03:00")
        assert result.hour == 8

    def test_z_replaced_with_utc(self) -> None:
        """Parsing 'Z' suffix should produce UTC-equivalent datetime."""
        result = parse_iso_dt("2026-01-01T00:00:00Z")
        assert result.tzinfo is not None
        assert result.utcoffset().total_seconds() == 0

    def test_invalid_format_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_iso_dt("not-a-date")

    def test_milliseconds_preserved(self) -> None:
        result = parse_iso_dt("2026-03-20T08:30:15.123456Z")
        assert result.microsecond == 123456


# ===========================================================================
# Optional import fallback branches
# ===========================================================================


class TestOptionalImports:
    """Cover the ImportError fallback branches for pagerduty / jira."""

    def test_pagerduty_import_fallback(self) -> None:
        """When pagerduty is not installed, _pagerduty falls back to None."""
        import importlib
        import noc_utils

        original_pd = noc_utils._pagerduty
        original_modules = sys.modules.copy()

        # Remove pagerduty from sys.modules and make import fail
        sys.modules.pop("pagerduty", None)
        real_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

        def failing_import(name, *args, **kwargs):
            if name == "pagerduty":
                raise ImportError("mocked")
            return real_import(name, *args, **kwargs)

        try:
            with patch("builtins.__import__", side_effect=failing_import):
                importlib.reload(noc_utils)
            assert noc_utils._pagerduty is None
        finally:
            # Restore
            sys.modules.update(original_modules)
            importlib.reload(noc_utils)

    def test_jira_import_fallback(self) -> None:
        """When jira is not installed, JIRA falls back to None."""
        import importlib
        import noc_utils

        original_modules = sys.modules.copy()

        sys.modules.pop("jira", None)
        real_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

        def failing_import(name, *args, **kwargs):
            if name == "jira":
                raise ImportError("mocked")
            return real_import(name, *args, **kwargs)

        try:
            with patch("builtins.__import__", side_effect=failing_import):
                importlib.reload(noc_utils)
            assert noc_utils.JIRA is None
        finally:
            sys.modules.update(original_modules)
            importlib.reload(noc_utils)
