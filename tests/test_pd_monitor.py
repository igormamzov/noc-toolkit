"""Tests for pd-monitor (pd_monitor.py)."""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from pd_monitor import (
    PagerDutyMonitor,
    SILENT_ACK_PATTERNS,
    COMMENTS_NORMAL,
    COMMENTS_TYPO,
    ALL_COMMENTS,
    VERSION,
    load_config,
    show_duration_menu,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_monitor(**overrides) -> PagerDutyMonitor:
    """Create a PagerDutyMonitor with mocked PagerDuty client."""
    with patch("noc_utils._pagerduty") as mock_pd:
        mock_session = MagicMock()
        mock_pd.RestApiV2Client.return_value = mock_session

        # Mock get() calls: first for _get_current_user_id (users/me),
        # then for _get_user_email (users/PUSER123)
        resp_user_id = MagicMock()
        resp_user_id.json.return_value = {"user": {"id": "PUSER123"}}
        resp_email = MagicMock()
        resp_email.json.return_value = {"user": {"id": "PUSER123", "email": "test@example.com"}}
        mock_session.get.side_effect = [resp_user_id, resp_email]

        defaults = dict(
            pagerduty_api_token="test-token",
            comment_pattern="working on it",
            check_interval_seconds=30,
            output_file="/tmp/test-pd-monitor.txt",
            dry_run=False,
            verbose=False,
            details=False,
            background=False,
        )
        defaults.update(overrides)
        monitor = PagerDutyMonitor(**defaults)
    return monitor


def _incident(incident_id: str = "P001", title: str = "Job failed", url: str = "") -> dict:
    """Build a minimal incident dict."""
    return {
        "id": incident_id,
        "title": title,
        "html_url": url or f"https://pd.example.com/incidents/{incident_id}",
    }


# ===========================================================================
# Version
# ===========================================================================

class TestVersion:
    def test_version_is_string(self):
        assert isinstance(VERSION, str)

    def test_version_format(self):
        parts = VERSION.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)


# ===========================================================================
# _is_silent_ack
# ===========================================================================

class TestIsSilentAck:
    def test_matching_pattern(self):
        assert PagerDutyMonitor._is_silent_ack("Missing AUS & NZL load-status") is True

    def test_case_insensitive(self):
        assert PagerDutyMonitor._is_silent_ack("missing east something") is True

    def test_no_match(self):
        assert PagerDutyMonitor._is_silent_ack("Databricks job failed") is False

    def test_empty_string(self):
        assert PagerDutyMonitor._is_silent_ack("") is False

    def test_all_patterns_match(self):
        for pattern in SILENT_ACK_PATTERNS:
            assert PagerDutyMonitor._is_silent_ack(f"Alert: {pattern} detected") is True

    def test_partial_match(self):
        assert PagerDutyMonitor._is_silent_ack("Missing UK data") is True

    def test_missing_without_suffix(self):
        """'Missing' alone should not match any pattern."""
        assert PagerDutyMonitor._is_silent_ack("Missing data") is False


# ===========================================================================
# _pick_random_comment
# ===========================================================================

class TestPickRandomComment:
    def test_returns_string(self):
        monitor = _make_monitor()
        comment = monitor._pick_random_comment()
        assert isinstance(comment, str)
        assert len(comment) > 0

    def test_comment_is_from_known_pool(self):
        """The returned comment (case-insensitive) should match one of ALL_COMMENTS."""
        monitor = _make_monitor()
        all_lower = [c.lower() for c in ALL_COMMENTS]
        for _ in range(50):
            comment = monitor._pick_random_comment()
            assert comment.lower() in all_lower

    @patch("pd_monitor.random")
    def test_typo_path(self, mock_random):
        """When random < 0.2, pick from typo list."""
        mock_random.random.side_effect = [0.1, 0.9]  # typo=yes, lowercase=no
        mock_random.choice.return_value = COMMENTS_TYPO[0]
        monitor = _make_monitor()
        comment = monitor._pick_random_comment()
        assert comment == COMMENTS_TYPO[0]

    @patch("pd_monitor.random")
    def test_lowercase_path(self, mock_random):
        """When second random < 0.5, first char is lowered."""
        mock_random.random.side_effect = [0.5, 0.1]  # typo=no, lowercase=yes
        mock_random.choice.return_value = "Working on it"
        monitor = _make_monitor()
        comment = monitor._pick_random_comment()
        assert comment[0] == "w"


# ===========================================================================
# __init__
# ===========================================================================

class TestInit:
    def test_random_comments_default(self):
        monitor = _make_monitor()
        assert monitor.random_comments is True

    def test_random_comments_custom_pattern(self):
        monitor = _make_monitor(comment_pattern="custom phrase")
        assert monitor.random_comments is False

    def test_user_id_set(self):
        monitor = _make_monitor()
        assert monitor.user_id == "PUSER123"

    def test_processed_incidents_empty(self):
        monitor = _make_monitor()
        assert monitor.processed_incidents == set()

    def test_dry_run_flag(self):
        monitor = _make_monitor(dry_run=True)
        assert monitor.dry_run is True

    def test_background_flag(self):
        monitor = _make_monitor(background=True)
        assert monitor.background is True


# ===========================================================================
# _get_current_user_id
# ===========================================================================

class TestGetUserEmail:
    def test_success(self):
        monitor = _make_monitor()
        assert monitor.user_email == "test@example.com"

    def test_missing_email_raises(self):
        with patch("noc_utils._pagerduty") as mock_pd:
            mock_session = MagicMock()
            mock_pd.RestApiV2Client.return_value = mock_session
            resp_user_id = MagicMock()
            resp_user_id.json.return_value = {"user": {"id": "PUSER123"}}
            resp_no_email = MagicMock()
            resp_no_email.json.return_value = {"user": {"id": "PUSER123"}}
            mock_session.get.side_effect = [resp_user_id, resp_no_email]

            with pytest.raises(RuntimeError, match="Unable to get user email"):
                PagerDutyMonitor(pagerduty_api_token="test")


class TestGetCurrentUserId:
    def test_success(self):
        monitor = _make_monitor()
        assert monitor.user_id == "PUSER123"

    def test_missing_user_id_raises(self):
        with patch("noc_utils._pagerduty") as mock_pd:
            mock_session = MagicMock()
            mock_pd.RestApiV2Client.return_value = mock_session
            mock_response = MagicMock()
            mock_response.json.return_value = {"user": {}}
            mock_session.get.return_value = mock_response

            with pytest.raises(RuntimeError, match="Unable to get user ID"):
                PagerDutyMonitor(pagerduty_api_token="test")

    def test_pd_error_propagates(self):
        import pagerduty as real_pd
        with patch("noc_utils._pagerduty") as mock_pd:
            mock_pd.Error = real_pd.Error
            mock_session = MagicMock()
            mock_pd.RestApiV2Client.return_value = mock_session
            mock_session.get.side_effect = real_pd.Error("auth failed")

            with pytest.raises(real_pd.Error):
                PagerDutyMonitor(pagerduty_api_token="test")


# ===========================================================================
# get_triggered_incidents
# ===========================================================================

class TestGetTriggeredIncidents:
    def test_returns_list(self):
        monitor = _make_monitor()
        monitor.pagerduty_session.list_all.return_value = iter([_incident()])
        result = monitor.get_triggered_incidents()
        assert result == [_incident()]

    def test_empty_result(self):
        monitor = _make_monitor()
        monitor.pagerduty_session.list_all.return_value = iter([])
        result = monitor.get_triggered_incidents()
        assert result == []

    def test_pd_error_returns_empty(self):
        import pagerduty
        monitor = _make_monitor()
        monitor.pagerduty_session.list_all.side_effect = pagerduty.Error("timeout")
        result = monitor.get_triggered_incidents()
        assert result == []

    def test_params_include_user_id(self):
        monitor = _make_monitor()
        monitor.pagerduty_session.list_all.return_value = iter([])
        monitor.get_triggered_incidents()
        call_args = monitor.pagerduty_session.list_all.call_args
        params = call_args[1]["params"]
        assert params["user_ids[]"] == ["PUSER123"]
        assert params["statuses[]"] == ["triggered"]


# ===========================================================================
# get_incident_notes
# ===========================================================================

class TestGetIncidentNotes:
    def test_returns_notes(self):
        monitor = _make_monitor()
        notes = [{"content": "working on it"}]
        monitor.pagerduty_session.list_all.return_value = iter(notes)
        result = monitor.get_incident_notes("P001")
        assert result == notes

    def test_empty_notes(self):
        monitor = _make_monitor()
        monitor.pagerduty_session.list_all.return_value = iter([])
        result = monitor.get_incident_notes("P001")
        assert result == []

    def test_pd_error_returns_empty(self):
        import pagerduty
        monitor = _make_monitor()
        monitor.pagerduty_session.list_all.side_effect = pagerduty.Error("err")
        result = monitor.get_incident_notes("P001")
        assert result == []


# ===========================================================================
# check_has_comments
# ===========================================================================

class TestCheckHasComments:
    def test_has_comments(self):
        monitor = _make_monitor()
        monitor.pagerduty_session.list_all.return_value = iter([{"content": "hello"}])
        assert monitor.check_has_comments("P001") is True

    def test_no_comments(self):
        monitor = _make_monitor()
        monitor.pagerduty_session.list_all.return_value = iter([])
        assert monitor.check_has_comments("P001") is False


# ===========================================================================
# check_has_working_comment
# ===========================================================================

class TestCheckHasWorkingComment:
    def test_random_mode_matches_normal_phrase(self):
        monitor = _make_monitor()  # random_comments=True by default
        monitor.pagerduty_session.list_all.return_value = iter([
            {"content": "Working on it"}
        ])
        assert monitor.check_has_working_comment("P001") is True

    def test_random_mode_matches_typo_phrase(self):
        monitor = _make_monitor()
        monitor.pagerduty_session.list_all.return_value = iter([
            {"content": "Investigaing"}
        ])
        assert monitor.check_has_working_comment("P001") is True

    def test_random_mode_no_match(self):
        monitor = _make_monitor()
        monitor.pagerduty_session.list_all.return_value = iter([
            {"content": "Some unrelated comment"}
        ])
        assert monitor.check_has_working_comment("P001") is False

    def test_custom_pattern_matches(self):
        monitor = _make_monitor(comment_pattern="custom phrase")
        monitor.pagerduty_session.list_all.return_value = iter([
            {"content": "I said custom phrase here"}
        ])
        assert monitor.check_has_working_comment("P001") is True

    def test_custom_pattern_no_match(self):
        monitor = _make_monitor(comment_pattern="custom phrase")
        monitor.pagerduty_session.list_all.return_value = iter([
            {"content": "Working on it"}
        ])
        assert monitor.check_has_working_comment("P001") is False

    def test_case_insensitive(self):
        monitor = _make_monitor()
        monitor.pagerduty_session.list_all.return_value = iter([
            {"content": "WORKING ON IT"}
        ])
        assert monitor.check_has_working_comment("P001") is True

    def test_no_notes(self):
        monitor = _make_monitor()
        monitor.pagerduty_session.list_all.return_value = iter([])
        assert monitor.check_has_working_comment("P001") is False


# ===========================================================================
# add_note_to_incident
# ===========================================================================

class TestAddNoteToIncident:
    def test_success(self):
        monitor = _make_monitor()
        assert monitor.add_note_to_incident("P001", "test note") is True
        monitor.pagerduty_session.rpost.assert_called_once()

    def test_dry_run_skips_api(self):
        monitor = _make_monitor(dry_run=True)
        assert monitor.add_note_to_incident("P001", "test note") is True
        monitor.pagerduty_session.rpost.assert_not_called()

    def test_pd_error_returns_false(self):
        import pagerduty
        monitor = _make_monitor()
        monitor.pagerduty_session.rpost.side_effect = pagerduty.Error("err")
        assert monitor.add_note_to_incident("P001", "test note") is False


# ===========================================================================
# acknowledge_incident
# ===========================================================================

class TestAcknowledgeIncident:
    def test_success(self):
        monitor = _make_monitor()
        assert monitor.acknowledge_incident("P001") is True
        monitor.pagerduty_session.rput.assert_called_once()

    def test_uses_cached_email_in_header(self):
        monitor = _make_monitor()
        monitor.acknowledge_incident("P001")
        call_kwargs = monitor.pagerduty_session.rput.call_args[1]
        assert call_kwargs["headers"]["From"] == "test@example.com"

    def test_dry_run_skips_api(self):
        monitor = _make_monitor(dry_run=True)
        assert monitor.acknowledge_incident("P001") is True
        monitor.pagerduty_session.rput.assert_not_called()

    def test_pd_error_returns_false(self):
        import pagerduty
        monitor = _make_monitor()
        monitor.pagerduty_session.rput.side_effect = pagerduty.Error("err")
        assert monitor.acknowledge_incident("P001") is False

    def test_generic_error_returns_false(self):
        monitor = _make_monitor()
        monitor.pagerduty_session.rput.side_effect = RuntimeError("boom")
        assert monitor.acknowledge_incident("P001") is False


# ===========================================================================
# log_needs_attention
# ===========================================================================

class TestLogNeedsAttention:
    def test_writes_to_file(self, tmp_path):
        monitor = _make_monitor(output_file=str(tmp_path / "attention.txt"))
        monitor.log_needs_attention("P001", "Job failed", "https://pd.example.com/P001")
        content = (tmp_path / "attention.txt").read_text()
        assert "P001" in content
        assert "Job failed" in content
        assert "https://pd.example.com/P001" in content

    def test_appends_to_existing(self, tmp_path):
        out = tmp_path / "attention.txt"
        out.write_text("existing line\n")
        monitor = _make_monitor(output_file=str(out))
        monitor.log_needs_attention("P002", "Another", "https://pd.example.com/P002")
        content = out.read_text()
        assert "existing line" in content
        assert "P002" in content

    def test_creates_parent_dirs(self, tmp_path):
        out = tmp_path / "subdir" / "deep" / "attention.txt"
        monitor = _make_monitor(output_file=str(out))
        monitor.log_needs_attention("P003", "Title", "url")
        assert out.exists()


# ===========================================================================
# process_incident — core logic
# ===========================================================================

class TestProcessIncident:
    def _make_processing_monitor(self, **kw) -> PagerDutyMonitor:
        """Monitor with mocked check/ack helpers for process_incident tests."""
        monitor = _make_monitor(**kw)
        # Default: no comments, ack succeeds, note succeeds
        monitor.check_has_comments = MagicMock(return_value=False)
        monitor.check_has_working_comment = MagicMock(return_value=False)
        monitor.acknowledge_incident = MagicMock(return_value=True)
        monitor.add_note_to_incident = MagicMock(return_value=True)
        monitor.log_needs_attention = MagicMock()
        return monitor

    # --- already processed ---
    def test_already_processed_skipped(self):
        monitor = self._make_processing_monitor()
        monitor.processed_incidents.add("P001")
        result = monitor.process_incident(_incident("P001"))
        assert result["action"] == "already_processed"
        assert result["success"] is False

    # --- new incident (no comments, not silent) ---
    def test_new_incident_comment_and_ack(self):
        monitor = self._make_processing_monitor()
        result = monitor.process_incident(_incident("P001"))
        assert result["action"] == "new_incident"
        assert result["success"] is True
        assert result["comment_added"] is True
        monitor.add_note_to_incident.assert_called_once()
        monitor.acknowledge_incident.assert_called_once_with("P001")
        assert "P001" in monitor.processed_incidents

    def test_new_incident_dry_run(self):
        monitor = self._make_processing_monitor(dry_run=True)
        result = monitor.process_incident(_incident("P001"))
        assert result["action"] == "new_incident"
        assert result["success"] is True
        assert "DRY RUN" in result["message"]
        monitor.add_note_to_incident.assert_not_called()
        monitor.acknowledge_incident.assert_not_called()

    def test_new_incident_comment_fails(self):
        monitor = self._make_processing_monitor()
        monitor.add_note_to_incident.return_value = False
        result = monitor.process_incident(_incident("P001"))
        assert result["success"] is False
        assert result["action"] == "new_incident"
        assert result["comment_added"] is False

    def test_new_incident_ack_fails(self):
        monitor = self._make_processing_monitor()
        monitor.acknowledge_incident.return_value = False
        result = monitor.process_incident(_incident("P001"))
        assert result["success"] is False
        assert result["comment_added"] is True  # comment was added before ack

    # --- silent ack (no comments, silent pattern) ---
    def test_silent_ack(self):
        monitor = self._make_processing_monitor()
        inc = _incident("P002", title="Missing AUS & NZL load-status")
        result = monitor.process_incident(inc)
        assert result["action"] == "silent_ack"
        assert result["success"] is True
        assert result["comment_added"] is False
        monitor.add_note_to_incident.assert_not_called()
        monitor.acknowledge_incident.assert_called_once_with("P002")

    def test_silent_ack_dry_run(self):
        monitor = self._make_processing_monitor(dry_run=True)
        inc = _incident("P002", title="Missing UK data alert")
        result = monitor.process_incident(inc)
        assert result["action"] == "silent_ack"
        assert "DRY RUN" in result["message"]

    def test_silent_ack_fails(self):
        monitor = self._make_processing_monitor()
        monitor.acknowledge_incident.return_value = False
        inc = _incident("P002", title="Missing Central data")
        result = monitor.process_incident(inc)
        assert result["success"] is False
        assert result["action"] == "silent_ack"

    # --- has comments + working comment → needs attention ---
    def test_needs_attention(self):
        monitor = self._make_processing_monitor()
        monitor.check_has_comments.return_value = True
        monitor.check_has_working_comment.return_value = True
        result = monitor.process_incident(_incident("P003"))
        assert result["action"] == "needs_attention"
        assert result["success"] is True
        assert result["logged_to_file"] is True
        monitor.log_needs_attention.assert_called_once()

    def test_needs_attention_dry_run(self):
        monitor = self._make_processing_monitor(dry_run=True)
        monitor.check_has_comments.return_value = True
        monitor.check_has_working_comment.return_value = True
        result = monitor.process_incident(_incident("P003"))
        assert result["action"] == "needs_attention"
        assert "DRY RUN" in result["message"]

    def test_needs_attention_ack_fails(self):
        monitor = self._make_processing_monitor()
        monitor.check_has_comments.return_value = True
        monitor.check_has_working_comment.return_value = True
        monitor.acknowledge_incident.return_value = False
        result = monitor.process_incident(_incident("P003"))
        assert result["success"] is False
        assert result["logged_to_file"] is False

    # --- has comments, no working comment → acknowledge only ---
    def test_acknowledge_only(self):
        monitor = self._make_processing_monitor()
        monitor.check_has_comments.return_value = True
        monitor.check_has_working_comment.return_value = False
        result = monitor.process_incident(_incident("P004"))
        assert result["action"] == "acknowledge_only"
        assert result["success"] is True
        assert result["comment_added"] is False
        monitor.add_note_to_incident.assert_not_called()

    def test_acknowledge_only_dry_run(self):
        monitor = self._make_processing_monitor(dry_run=True)
        monitor.check_has_comments.return_value = True
        monitor.check_has_working_comment.return_value = False
        result = monitor.process_incident(_incident("P004"))
        assert result["action"] == "acknowledge_only"
        assert "DRY RUN" in result["message"]

    def test_acknowledge_only_fails(self):
        monitor = self._make_processing_monitor()
        monitor.check_has_comments.return_value = True
        monitor.check_has_working_comment.return_value = False
        monitor.acknowledge_incident.return_value = False
        result = monitor.process_incident(_incident("P004"))
        assert result["success"] is False


# ===========================================================================
# check_incidents_once
# ===========================================================================

class TestCheckIncidentsOnce:
    def test_no_incidents(self):
        monitor = _make_monitor()
        monitor.get_triggered_incidents = MagicMock(return_value=[])
        summary = monitor.check_incidents_once()
        assert summary["total"] == 0

    def test_processes_all_incidents(self):
        monitor = _make_monitor()
        incidents = [_incident("P001"), _incident("P002")]
        monitor.get_triggered_incidents = MagicMock(return_value=incidents)
        monitor.process_incident = MagicMock(return_value={
            "success": True,
            "action": "new_incident",
            "message": "ok",
            "url": "http://x",
            "comment_added": True,
            "logged_to_file": False,
        })
        summary = monitor.check_incidents_once()
        assert summary["total"] == 2
        assert summary["new_incidents"] == 2

    def test_clears_processed_set_each_cycle(self):
        """Regression test: processed_incidents must be cleared at the start of each cycle."""
        monitor = _make_monitor()
        monitor.processed_incidents = {"P_OLD_1", "P_OLD_2"}
        monitor.get_triggered_incidents = MagicMock(return_value=[])
        monitor.check_incidents_once()
        assert monitor.processed_incidents == set()

    def test_re_triggered_incident_reprocessed(self):
        """An incident acknowledged in cycle 1 must be re-processed if it reappears in cycle 2."""
        monitor = _make_monitor()
        inc = _incident("P_RETRIG")
        monitor.get_triggered_incidents = MagicMock(return_value=[inc])
        monitor.process_incident = MagicMock(return_value={
            "success": True,
            "action": "new_incident",
            "message": "ok",
            "url": "http://x",
            "comment_added": True,
            "logged_to_file": False,
        })

        # Cycle 1
        monitor.check_incidents_once()
        assert monitor.process_incident.call_count == 1

        # Simulate PD auto-un-ack: incident comes back as triggered
        monitor.process_incident.reset_mock()
        monitor.check_incidents_once()
        assert monitor.process_incident.call_count == 1  # reprocessed, not skipped

    def test_summary_counts_errors(self):
        monitor = _make_monitor()
        monitor.get_triggered_incidents = MagicMock(return_value=[_incident("P001")])
        monitor.process_incident = MagicMock(return_value={
            "success": False,
            "action": "new_incident",
            "message": "Failed to add comment",
            "url": "http://x",
            "comment_added": False,
            "logged_to_file": False,
        })
        summary = monitor.check_incidents_once()
        assert len(summary["errors"]) == 1

    def test_summary_counts_already_processed(self):
        """Duplicate incident ID within same cycle → already_processed."""
        monitor = _make_monitor()
        inc = _incident("P001")
        # Two references to the same incident in one fetch
        monitor.get_triggered_incidents = MagicMock(return_value=[inc, inc])

        call_count = [0]
        def mock_process(incident):
            call_count[0] += 1
            if call_count[0] == 1:
                monitor.processed_incidents.add("P001")
                return {
                    "success": True, "action": "new_incident", "message": "ok",
                    "url": "http://x", "comment_added": True, "logged_to_file": False,
                }
            return {
                "success": False, "action": "already_processed",
                "message": "dup", "url": "http://x",
                "comment_added": False, "logged_to_file": False,
            }

        monitor.process_incident = mock_process
        summary = monitor.check_incidents_once()
        assert summary["already_processed"] == 1

    def test_silent_ack_counted(self):
        monitor = _make_monitor()
        monitor.get_triggered_incidents = MagicMock(return_value=[_incident("P001")])
        monitor.process_incident = MagicMock(return_value={
            "success": True,
            "action": "silent_ack",
            "message": "ok",
            "url": "http://x",
            "comment_added": False,
            "logged_to_file": False,
        })
        summary = monitor.check_incidents_once()
        assert summary["silent_ack"] == 1


# ===========================================================================
# _draw_progress_bar
# ===========================================================================

class TestDrawProgressBar:
    def test_zero_elapsed(self):
        monitor = _make_monitor()
        bar = monitor._draw_progress_bar(0, 3600)
        assert "0%" in bar
        assert "remaining" in bar

    def test_half_elapsed(self):
        monitor = _make_monitor()
        bar = monitor._draw_progress_bar(1800, 3600)
        assert "50%" in bar

    def test_full_elapsed(self):
        monitor = _make_monitor()
        bar = monitor._draw_progress_bar(3600, 3600)
        assert "100%" in bar
        assert "0m 0s remaining" in bar

    def test_custom_width(self):
        monitor = _make_monitor()
        bar = monitor._draw_progress_bar(100, 200, width=10)
        assert isinstance(bar, str)


# ===========================================================================
# load_config
# ===========================================================================

class TestLoadConfig:
    @patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "tok123"}, clear=True)
    def test_token_loaded(self):
        config = load_config()
        assert config["pagerduty_api_token"] == "tok123"

    @patch.dict("os.environ", {}, clear=True)
    def test_missing_token_is_none(self):
        config = load_config()
        assert config["pagerduty_api_token"] is None

    @patch.dict("os.environ", {
        "MONITOR_COMMENT_PATTERN": "custom",
        "MONITOR_CHECK_INTERVAL_SECONDS": "60",
        "MONITOR_OUTPUT_FILE": "/tmp/out.txt",
        "MONITOR_DRY_RUN": "true",
        "MONITOR_VERBOSE": "true",
    }, clear=True)
    def test_env_overrides(self):
        config = load_config()
        assert config["comment_pattern"] == "custom"
        assert config["check_interval_seconds"] == 60
        assert config["output_file"] == "/tmp/out.txt"
        assert config["dry_run"] is True
        assert config["verbose"] is True

    @patch.dict("os.environ", {}, clear=True)
    def test_defaults(self):
        config = load_config()
        assert config["comment_pattern"] == "working on it"
        assert config["check_interval_seconds"] == 30
        assert config["dry_run"] is False
        assert config["verbose"] is False
        assert config["details"] is False
        assert config["background"] is False


# ===========================================================================
# show_duration_menu
# ===========================================================================

class TestShowDurationMenu:
    @patch("builtins.input", return_value="1")
    def test_option_1(self, _):
        assert show_duration_menu() == 60

    @patch("builtins.input", return_value="2")
    def test_option_2(self, _):
        assert show_duration_menu() == 120

    @patch("builtins.input", return_value="3")
    def test_option_3(self, _):
        assert show_duration_menu() == 240

    @patch("builtins.input", return_value="4")
    def test_option_4(self, _):
        assert show_duration_menu() == 480

    @patch("builtins.input", return_value="5")
    def test_option_5(self, _):
        assert show_duration_menu() == 720

    @patch("builtins.input", side_effect=["6", "45"])
    def test_custom_duration(self, _):
        assert show_duration_menu() == 45

    @patch("builtins.input", side_effect=KeyboardInterrupt)
    def test_keyboard_interrupt(self, _):
        with pytest.raises(SystemExit) as exc_info:
            show_duration_menu()
        assert exc_info.value.code == 130


# ===========================================================================
# monitor_continuously
# ===========================================================================

class TestMonitorContinuously:
    def test_runs_until_duration_expires(self):
        monitor = _make_monitor(background=True)
        monitor.check_incidents_once = MagicMock(return_value={
            "total": 0, "new_incidents": 0, "needs_attention": 0,
            "acknowledged": 0, "silent_ack": 0, "already_processed": 0,
            "errors": [],
        })

        # Patch time.time to simulate: start → loop check → elapsed → end
        call_count = [0]
        start = 1000.0
        end = start + 60  # 1-minute run

        def fake_time():
            call_count[0] += 1
            if call_count[0] <= 2:
                return start  # start_time and end_time setup
            if call_count[0] <= 5:
                return start + 1  # first iteration: within duration
            return end + 1  # subsequent calls: past end

        with patch("pd_monitor.time.time", side_effect=fake_time), \
             patch("pd_monitor.time.sleep"):
            monitor.monitor_continuously(duration_minutes=1)
        assert monitor.check_incidents_once.call_count >= 1

    def test_keyboard_interrupt_propagates(self):
        monitor = _make_monitor(background=True)
        monitor.check_incidents_once = MagicMock(side_effect=KeyboardInterrupt)

        with pytest.raises(KeyboardInterrupt):
            monitor.monitor_continuously(duration_minutes=1)


# ===========================================================================
# main() CLI tests
# ===========================================================================

class TestMain:
    @patch("pd_monitor.PagerDutyMonitor")
    @patch("pd_monitor.load_env")
    @patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "test-token"})
    def test_once_mode(self, mock_dotenv, mock_cls):
        mock_instance = MagicMock()
        mock_instance.check_incidents_once.return_value = {
            "total": 0, "new_incidents": 0, "needs_attention": 0,
            "acknowledged": 0, "silent_ack": 0, "already_processed": 0,
            "errors": [],
        }
        mock_cls.return_value = mock_instance

        with patch("sys.argv", ["pd_monitor.py", "--once"]):
            from pd_monitor import main
            main()

        mock_instance.check_incidents_once.assert_called_once()

    @patch("pd_monitor.PagerDutyMonitor")
    @patch("pd_monitor.load_env")
    @patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "test-token"})
    def test_duration_mode(self, mock_dotenv, mock_cls):
        mock_instance = MagicMock()
        mock_cls.return_value = mock_instance

        with patch("sys.argv", ["pd_monitor.py", "--duration", "30"]):
            from pd_monitor import main
            main()

        mock_instance.monitor_continuously.assert_called_once_with(duration_minutes=30)

    @patch("pd_monitor.load_env")
    @patch.dict("os.environ", {}, clear=True)
    def test_missing_token_exits(self, mock_dotenv):
        with patch("sys.argv", ["pd_monitor.py", "--once"]):
            from pd_monitor import main
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    @patch("pd_monitor.PagerDutyMonitor")
    @patch("pd_monitor.load_env")
    @patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "test-token"})
    def test_dry_run_flag(self, mock_dotenv, mock_cls):
        mock_instance = MagicMock()
        mock_instance.check_incidents_once.return_value = {
            "total": 0, "new_incidents": 0, "needs_attention": 0,
            "acknowledged": 0, "silent_ack": 0, "already_processed": 0,
            "errors": [],
        }
        mock_cls.return_value = mock_instance

        with patch("sys.argv", ["pd_monitor.py", "--once", "--dry-run"]):
            from pd_monitor import main
            main()

        # Verify dry_run was passed to constructor
        call_kwargs = mock_cls.call_args[1]
        assert call_kwargs["dry_run"] is True

    @patch("pd_monitor.PagerDutyMonitor")
    @patch("pd_monitor.load_env")
    @patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "test-token"})
    def test_background_default_duration(self, mock_dotenv, mock_cls):
        mock_instance = MagicMock()
        mock_cls.return_value = mock_instance

        with patch("sys.argv", ["pd_monitor.py", "--background"]):
            from pd_monitor import main
            main()

        mock_instance.monitor_continuously.assert_called_once_with(duration_minutes=60)

    @patch("pd_monitor.PagerDutyMonitor")
    @patch("pd_monitor.load_env")
    @patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "test-token"})
    def test_verbose_and_details_flags(self, mock_dotenv, mock_cls):
        mock_instance = MagicMock()
        mock_instance.check_incidents_once.return_value = {
            "total": 0, "new_incidents": 0, "needs_attention": 0,
            "acknowledged": 0, "silent_ack": 0, "already_processed": 0,
            "errors": [],
        }
        mock_cls.return_value = mock_instance

        with patch("sys.argv", ["pd_monitor.py", "--once", "--verbose", "--details"]):
            from pd_monitor import main
            main()

        call_kwargs = mock_cls.call_args[1]
        assert call_kwargs["verbose"] is True
        assert call_kwargs["details"] is True

    @patch("pd_monitor.PagerDutyMonitor")
    @patch("pd_monitor.load_env")
    @patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "test-token"})
    def test_pattern_override(self, mock_dotenv, mock_cls):
        mock_instance = MagicMock()
        mock_instance.check_incidents_once.return_value = {
            "total": 0, "new_incidents": 0, "needs_attention": 0,
            "acknowledged": 0, "silent_ack": 0, "already_processed": 0,
            "errors": [],
        }
        mock_cls.return_value = mock_instance

        with patch("sys.argv", ["pd_monitor.py", "--once", "--pattern", "my custom"]):
            from pd_monitor import main
            main()

        call_kwargs = mock_cls.call_args[1]
        assert call_kwargs["comment_pattern"] == "my custom"

    @patch("pd_monitor.PagerDutyMonitor")
    @patch("pd_monitor.load_env")
    @patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "test-token"})
    def test_keyboard_interrupt_exits_130(self, mock_dotenv, mock_cls):
        mock_instance = MagicMock()
        mock_instance.check_incidents_once.side_effect = KeyboardInterrupt
        mock_cls.return_value = mock_instance

        with patch("sys.argv", ["pd_monitor.py", "--once"]):
            from pd_monitor import main
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 130
