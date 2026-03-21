"""Tests for pd-jira-tool (pd_sync.py)."""

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from pd_sync import PDSync, _parse_iso_dt, save_summary_to_file


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tool(quiet_mode: bool = False) -> PDSync:
    """Create tool with mocked PagerDuty and Jira clients."""
    with patch("noc_utils._pagerduty") as mock_pd, \
         patch("pd_sync.JIRA") as mock_jira:
        mock_pd.RestApiV2Client.return_value = MagicMock()
        mock_jira.return_value = MagicMock()
        tool = PDSync(
            pagerduty_api_token="test-pd-token",
            jira_server_url="https://jira.example.com",
            jira_personal_access_token="test-jira-pat",
            quiet_mode=quiet_mode,
        )
    return tool


def _make_incident(
    incident_id: str = "P001",
    title: str = "Test incident",
    status: str = "triggered",
    url: str = "https://pd.example.com/incidents/P001",
    created_at: str = "2026-03-10T10:00:00Z",
    assignments: list | None = None,
) -> dict:
    """Build a minimal PD incident dict."""
    inc = {
        "id": incident_id,
        "title": title,
        "status": status,
        "html_url": url,
        "created_at": created_at,
        "assignments": assignments or [],
    }
    return inc


# ===========================================================================
# _parse_iso_dt helper
# ===========================================================================

class TestParseIsoDt:
    def test_with_z_suffix(self):
        dt = _parse_iso_dt("2026-03-10T10:00:00Z")
        assert dt.year == 2026
        assert dt.month == 3
        assert dt.tzinfo is not None

    def test_with_offset(self):
        dt = _parse_iso_dt("2026-03-10T10:00:00+00:00")
        assert dt.hour == 10

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            _parse_iso_dt("not-a-date")


# ===========================================================================
# _is_assigned_to_user
# ===========================================================================

class TestIsAssignedToUser:
    def test_assigned(self):
        inc = _make_incident(assignments=[{"assignee": {"id": "U1"}}])
        assert PDSync._is_assigned_to_user(inc, "U1") is True

    def test_not_assigned(self):
        inc = _make_incident(assignments=[{"assignee": {"id": "U2"}}])
        assert PDSync._is_assigned_to_user(inc, "U1") is False

    def test_no_assignments(self):
        inc = _make_incident(assignments=[])
        assert PDSync._is_assigned_to_user(inc, "U1") is False

    def test_multiple_assignees(self):
        inc = _make_incident(assignments=[
            {"assignee": {"id": "U2"}},
            {"assignee": {"id": "U1"}},
        ])
        assert PDSync._is_assigned_to_user(inc, "U1") is True

    def test_missing_assignee_key(self):
        inc = _make_incident(assignments=[{}])
        assert PDSync._is_assigned_to_user(inc, "U1") is False


# ===========================================================================
# __init__ — authentication modes
# ===========================================================================

class TestInit:
    def test_personal_access_token_auth(self):
        """PAT auth creates tool without error."""
        tool = _make_tool()
        assert tool.jira_client is not None

    def test_cloud_auth(self):
        """Email + API token auth creates tool without error."""
        with patch("noc_utils._pagerduty") as mock_pd, \
             patch("pd_sync.JIRA") as mock_jira:
            mock_pd.RestApiV2Client.return_value = MagicMock()
            mock_jira.return_value = MagicMock()
            tool = PDSync(
                pagerduty_api_token="tok",
                jira_server_url="https://jira.example.com",
                jira_email="user@example.com",
                jira_api_token="api-tok",
            )
        assert tool.jira_client is not None

    def test_missing_credentials_raises(self):
        """No Jira creds raises ValueError."""
        with patch("noc_utils._pagerduty") as mock_pd:
            mock_pd.RestApiV2Client.return_value = MagicMock()
            with pytest.raises(ValueError, match="Invalid Jira credentials"):
                PDSync(
                    pagerduty_api_token="tok",
                    jira_server_url="https://jira.example.com",
                )

    def test_quiet_mode_flag(self):
        tool = _make_tool(quiet_mode=True)
        assert tool.quiet_mode is True


# ===========================================================================
# get_current_user_id
# ===========================================================================

class TestGetCurrentUserId:
    def setup_method(self):
        self.tool = _make_tool()

    def test_user_wrapper(self):
        """Response has {user: {id: ...}} wrapper."""
        self.tool.pagerduty_session.rget.return_value = {"user": {"id": "PUSER1"}}
        assert self.tool.get_current_user_id() == "PUSER1"

    def test_flat_response(self):
        """Response has {id: ...} directly."""
        self.tool.pagerduty_session.rget.return_value = {"id": "PUSER2"}
        assert self.tool.get_current_user_id() == "PUSER2"

    def test_unexpected_keys_raises(self):
        """Response without 'id' or 'user' raises RuntimeError."""
        self.tool.pagerduty_session.rget.return_value = {"name": "foo"}
        with pytest.raises(RuntimeError, match="Unexpected API response structure"):
            self.tool.get_current_user_id()

    def test_unexpected_type_raises(self):
        """Non-dict response raises RuntimeError."""
        self.tool.pagerduty_session.rget.return_value = "string"
        with pytest.raises(RuntimeError, match="Unexpected API response type"):
            self.tool.get_current_user_id()

    def test_pd_error_raises(self):
        """PagerDuty API error wrapped in RuntimeError."""
        import pagerduty
        self.tool.pagerduty_session.rget.side_effect = pagerduty.Error("fail")
        with pytest.raises(RuntimeError, match="Failed to fetch current user"):
            self.tool.get_current_user_id()

    def test_key_error_raises(self):
        """Missing key in nested dict raises RuntimeError."""
        self.tool.pagerduty_session.rget.return_value = {"user": {}}
        with pytest.raises(RuntimeError, match="Could not parse user ID"):
            self.tool.get_current_user_id()


# ===========================================================================
# get_open_incidents
# ===========================================================================

class TestGetOpenIncidents:
    def setup_method(self):
        self.tool = _make_tool()

    def test_returns_current_incidents(self):
        """Basic case: current pass returns incidents."""
        inc = _make_incident()
        self.tool.pagerduty_session.list_all.side_effect = [
            iter([inc]),       # current
            iter([]),          # historical
        ]
        result = self.tool.get_open_incidents()
        assert len(result) == 1
        assert result[0]["id"] == "P001"

    def test_deduplication(self):
        """Same incident in both passes is not duplicated."""
        inc = _make_incident()
        self.tool.pagerduty_session.list_all.side_effect = [
            iter([inc]),
            iter([inc]),
        ]
        result = self.tool.get_open_incidents()
        assert len(result) == 1

    def test_historical_adds_new(self):
        """Historical pass adds incidents not in current pass."""
        inc1 = _make_incident(incident_id="P001")
        inc2 = _make_incident(incident_id="P002")
        self.tool.pagerduty_session.list_all.side_effect = [
            iter([inc1]),
            iter([inc2]),
        ]
        result = self.tool.get_open_incidents()
        assert len(result) == 2

    def test_user_filter_current(self):
        """Current pass filters by user_id assignment."""
        inc_mine = _make_incident(
            incident_id="P001",
            assignments=[{"assignee": {"id": "U1"}}],
        )
        inc_other = _make_incident(
            incident_id="P002",
            assignments=[{"assignee": {"id": "U2"}}],
        )
        self.tool.pagerduty_session.list_all.side_effect = [
            iter([inc_mine, inc_other]),
            iter([]),
        ]
        result = self.tool.get_open_incidents(user_id="U1")
        assert len(result) == 1
        assert result[0]["id"] == "P001"

    def test_user_filter_historical(self):
        """Historical pass filters by user_id too."""
        inc_mine = _make_incident(
            incident_id="P002",
            assignments=[{"assignee": {"id": "U1"}}],
        )
        inc_other = _make_incident(
            incident_id="P003",
            assignments=[{"assignee": {"id": "U2"}}],
        )
        self.tool.pagerduty_session.list_all.side_effect = [
            iter([]),
            iter([inc_mine, inc_other]),
        ]
        result = self.tool.get_open_incidents(user_id="U1")
        assert len(result) == 1
        assert result[0]["id"] == "P002"

    def test_pd_error_raises_runtime(self):
        """PagerDuty error wrapped in RuntimeError."""
        import pagerduty
        self.tool.pagerduty_session.list_all.side_effect = pagerduty.Error("fail")
        with pytest.raises(RuntimeError, match="Failed to fetch open PagerDuty incidents"):
            self.tool.get_open_incidents()

    def test_empty_results(self):
        """Both passes return empty lists."""
        self.tool.pagerduty_session.list_all.side_effect = [
            iter([]),
            iter([]),
        ]
        result = self.tool.get_open_incidents()
        assert result == []


# ===========================================================================
# get_recent_comments
# ===========================================================================

class TestGetRecentComments:
    def setup_method(self):
        self.tool = _make_tool()

    def test_returns_comments(self):
        notes = [
            {"content": "note1"},
            {"content": "note2"},
            {"content": "note3"},
            {"content": "note4"},
        ]
        self.tool.pagerduty_session.list_all.return_value = iter(notes)
        result = self.tool.get_recent_comments("P001", limit=3)
        assert result == ["note1", "note2", "note3"]

    def test_empty_notes(self):
        self.tool.pagerduty_session.list_all.return_value = iter([])
        result = self.tool.get_recent_comments("P001")
        assert result == []

    def test_skips_empty_content(self):
        notes = [{"content": ""}, {"content": "real note"}]
        self.tool.pagerduty_session.list_all.return_value = iter(notes)
        result = self.tool.get_recent_comments("P001")
        assert result == ["real note"]

    def test_pd_error_returns_empty(self):
        import pagerduty
        self.tool.pagerduty_session.list_all.side_effect = pagerduty.Error("fail")
        result = self.tool.get_recent_comments("P001")
        assert result == []


# ===========================================================================
# has_recent_comment_from_user
# ===========================================================================

class TestHasRecentCommentFromUser:
    def setup_method(self):
        self.tool = _make_tool(quiet_mode=True)

    def _note(self, user_id: str, hours_ago: float) -> dict:
        ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
        return {"user": {"id": user_id}, "created_at": ts}

    def test_recent_comment_returns_true(self):
        self.tool.pagerduty_session.list_all.return_value = iter([
            self._note("U1", hours_ago=2),
        ])
        assert self.tool.has_recent_comment_from_user("P001", "U1", hours_threshold=12.0) is True

    def test_old_comment_returns_false(self):
        self.tool.pagerduty_session.list_all.return_value = iter([
            self._note("U1", hours_ago=24),
        ])
        assert self.tool.has_recent_comment_from_user("P001", "U1", hours_threshold=12.0) is False

    def test_different_user_returns_false(self):
        self.tool.pagerduty_session.list_all.return_value = iter([
            self._note("U2", hours_ago=1),
        ])
        assert self.tool.has_recent_comment_from_user("P001", "U1") is False

    def test_no_notes_returns_false(self):
        self.tool.pagerduty_session.list_all.return_value = iter([])
        assert self.tool.has_recent_comment_from_user("P001", "U1") is False

    def test_pd_error_returns_false(self):
        import pagerduty
        self.tool.pagerduty_session.list_all.side_effect = pagerduty.Error("fail")
        assert self.tool.has_recent_comment_from_user("P001", "U1") is False

    def test_bad_timestamp_skipped(self):
        """Invalid timestamp is skipped, returns False."""
        note = {"user": {"id": "U1"}, "created_at": "not-a-date"}
        self.tool.pagerduty_session.list_all.return_value = iter([note])
        assert self.tool.has_recent_comment_from_user("P001", "U1") is False

    def test_z_suffix_timestamp(self):
        """Timestamp with 'Z' suffix is parsed correctly."""
        ts = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        note = {"user": {"id": "U1"}, "created_at": ts}
        self.tool.pagerduty_session.list_all.return_value = iter([note])
        assert self.tool.has_recent_comment_from_user("P001", "U1", hours_threshold=12.0) is True


# ===========================================================================
# extract_jira_ticket_numbers
# ===========================================================================

class TestExtractJiraTicketNumbers:
    def setup_method(self):
        self.tool = _make_tool()

    def test_single_ticket(self):
        assert self.tool.extract_jira_ticket_numbers("DSSD-12345 failed") == ["DSSD-12345"]

    def test_multiple_tickets(self):
        result = self.tool.extract_jira_ticket_numbers("DSSD-100 and COREDATA-200")
        assert result == ["DSSD-100", "COREDATA-200"]

    def test_drgn_filtered(self):
        """DRGN tickets are excluded."""
        result = self.tool.extract_jira_ticket_numbers("DRGN-999 DSSD-100")
        assert result == ["DSSD-100"]

    def test_only_drgn(self):
        result = self.tool.extract_jira_ticket_numbers("DRGN-999")
        assert result == []

    def test_deduplication(self):
        result = self.tool.extract_jira_ticket_numbers("DSSD-100 then DSSD-100 again")
        assert result == ["DSSD-100"]

    def test_empty_string(self):
        assert self.tool.extract_jira_ticket_numbers("") == []

    def test_none_input(self):
        assert self.tool.extract_jira_ticket_numbers(None) == []

    def test_no_tickets(self):
        assert self.tool.extract_jira_ticket_numbers("no tickets here") == []

    def test_preserves_order(self):
        result = self.tool.extract_jira_ticket_numbers("FCR-3 DSSD-1 COREDATA-2")
        assert result == ["FCR-3", "DSSD-1", "COREDATA-2"]

    def test_ticket_in_url(self):
        result = self.tool.extract_jira_ticket_numbers("https://jira.example.com/browse/DSSD-123")
        assert result == ["DSSD-123"]


# ===========================================================================
# _check_ignore_disabled
# ===========================================================================

class TestCheckIgnoreDisabled:
    def setup_method(self):
        self.tool = _make_tool()

    def test_ignore_in_title(self):
        assert self.tool._check_ignore_disabled("ignore this", []) == "Ignore"

    def test_disabled_in_title(self):
        assert self.tool._check_ignore_disabled("Disabled job", []) == "Disabled"

    def test_ignore_in_comment(self):
        assert self.tool._check_ignore_disabled("Normal title", ["please ignore"]) == "Ignore"

    def test_disabled_in_second_comment(self):
        assert self.tool._check_ignore_disabled("Title", ["ok", "disabled now"]) == "Disabled"

    def test_no_keyword(self):
        assert self.tool._check_ignore_disabled("Normal title", ["all good"]) is None

    def test_case_insensitive(self):
        assert self.tool._check_ignore_disabled("IGNORE THIS", []) == "Ignore"

    def test_empty_comments(self):
        assert self.tool._check_ignore_disabled("Normal", []) is None

    def test_title_checked_first(self):
        """Title keyword takes priority over comment keyword."""
        result = self.tool._check_ignore_disabled("ignore it", ["disabled"])
        assert result == "Ignore"


# ===========================================================================
# get_jira_ticket_status
# ===========================================================================

class TestGetJiraTicketStatus:
    def setup_method(self):
        self.tool = _make_tool()

    def test_returns_ticket_info(self):
        mock_issue = MagicMock()
        mock_issue.key = "DSSD-100"
        mock_issue.fields.summary = "Test summary"
        mock_issue.fields.status.name = "Open"
        mock_issue.fields.assignee.displayName = "John Doe"
        mock_issue.fields.priority.name = "High"
        self.tool.jira_client.issue.return_value = mock_issue
        self.tool.jira_client.server_url = "https://jira.example.com"

        result = self.tool.get_jira_ticket_status("DSSD-100")
        assert result["key"] == "DSSD-100"
        assert result["status"] == "Open"
        assert result["assignee"] == "John Doe"
        assert result["priority"] == "High"
        assert "DSSD-100" in result["url"]

    def test_unassigned(self):
        mock_issue = MagicMock()
        mock_issue.key = "DSSD-200"
        mock_issue.fields.summary = "Summary"
        mock_issue.fields.status.name = "Open"
        mock_issue.fields.assignee = None
        mock_issue.fields.priority.name = "Medium"
        self.tool.jira_client.issue.return_value = mock_issue
        self.tool.jira_client.server_url = "https://jira.example.com"

        result = self.tool.get_jira_ticket_status("DSSD-200")
        assert result["assignee"] == "Unassigned"

    def test_no_priority(self):
        mock_issue = MagicMock()
        mock_issue.key = "DSSD-300"
        mock_issue.fields.summary = "Summary"
        mock_issue.fields.status.name = "Open"
        mock_issue.fields.assignee.displayName = "Jane"
        mock_issue.fields.priority = None
        self.tool.jira_client.issue.return_value = mock_issue
        self.tool.jira_client.server_url = "https://jira.example.com"

        result = self.tool.get_jira_ticket_status("DSSD-300")
        assert result["priority"] == "None"

    def test_404_returns_none(self):
        from jira.exceptions import JIRAError
        self.tool.jira_client.issue.side_effect = JIRAError(status_code=404, text="Not found")
        result = self.tool.get_jira_ticket_status("DSSD-999")
        assert result is None

    def test_other_jira_error_returns_none(self):
        from jira.exceptions import JIRAError
        self.tool.jira_client.issue.side_effect = JIRAError(status_code=500, text="Server error")
        result = self.tool.get_jira_ticket_status("DSSD-999")
        assert result is None


# ===========================================================================
# add_incident_note
# ===========================================================================

class TestAddIncidentNote:
    def setup_method(self):
        self.tool = _make_tool(quiet_mode=True)

    def test_success(self):
        assert self.tool.add_incident_note("P001", "test note") is True
        self.tool.pagerduty_session.rpost.assert_called_once()

    def test_failure(self):
        import pagerduty
        self.tool.pagerduty_session.rpost.side_effect = pagerduty.Error("fail")
        assert self.tool.add_incident_note("P001", "test") is False

    def test_note_payload(self):
        """Verify the note payload structure."""
        self.tool.add_incident_note("P001", "hello")
        call_args = self.tool.pagerduty_session.rpost.call_args
        assert call_args[0][0] == "incidents/P001/notes"
        assert call_args[1]["json"]["note"]["content"] == "hello"


# ===========================================================================
# snooze_incident
# ===========================================================================

class TestSnoozeIncident:
    def setup_method(self):
        self.tool = _make_tool(quiet_mode=True)

    def test_success(self):
        assert self.tool.snooze_incident("P001", 3600) is True
        self.tool.pagerduty_session.rpost.assert_called_once()

    def test_default_duration(self):
        self.tool.snooze_incident("P001")
        call_args = self.tool.pagerduty_session.rpost.call_args
        assert call_args[1]["json"]["duration"] == 21600

    def test_failure(self):
        import pagerduty
        self.tool.pagerduty_session.rpost.side_effect = pagerduty.Error("fail")
        assert self.tool.snooze_incident("P001") is False

    def test_snooze_payload(self):
        self.tool.snooze_incident("P001", 7200)
        call_args = self.tool.pagerduty_session.rpost.call_args
        assert call_args[0][0] == "incidents/P001/snooze"
        assert call_args[1]["json"]["duration"] == 7200


# ===========================================================================
# print_verbose
# ===========================================================================

class TestPrintVerbose:
    def test_prints_when_not_quiet(self, capsys):
        tool = _make_tool(quiet_mode=False)
        tool.print_verbose("hello")
        assert "hello" in capsys.readouterr().out

    def test_silent_when_quiet(self, capsys):
        tool = _make_tool(quiet_mode=True)
        tool.print_verbose("hello")
        assert capsys.readouterr().out == ""


# ===========================================================================
# process_and_update_incidents — integration-level tests
# ===========================================================================

class TestProcessAndUpdateIncidents:
    def setup_method(self):
        self.tool = _make_tool(quiet_mode=True)

    def _setup_incidents(self, incidents: list):
        """Configure tool to return given incidents list from get_open_incidents."""
        self.tool.get_open_incidents = MagicMock(return_value=incidents)

    def test_no_incidents(self, capsys):
        self._setup_incidents([])
        result = self.tool.process_and_update_incidents()
        assert "No open incidents" in result

    def test_keyword_ignore_snooze(self):
        """Incident with 'ignore' keyword is auto-handled with snooze."""
        inc = _make_incident(title="Ignore this job alert")
        self._setup_incidents([inc])
        self.tool.get_recent_comments = MagicMock(return_value=[])
        self.tool.has_recent_comment_from_user = MagicMock(return_value=False)
        self.tool.add_incident_note = MagicMock(return_value=True)
        self.tool.snooze_incident = MagicMock(return_value=True)

        result = self.tool.process_and_update_incidents(
            user_id="U1", enable_snooze=True, snooze_duration_hours=6.0,
        )
        # Should post "Ignore. Snooze" and snooze
        self.tool.add_incident_note.assert_called_once_with("P001", "Ignore. Snooze")
        self.tool.snooze_incident.assert_called_once()
        assert "ignore/disabled" in result.lower()

    def test_keyword_disabled_no_snooze(self):
        """Incident with 'disabled' keyword — no snooze mode posts just 'Disabled'."""
        inc = _make_incident(title="Disabled job alert")
        self._setup_incidents([inc])
        self.tool.get_recent_comments = MagicMock(return_value=[])
        self.tool.has_recent_comment_from_user = MagicMock(return_value=False)
        self.tool.add_incident_note = MagicMock(return_value=True)

        self.tool.process_and_update_incidents(user_id="U1", enable_snooze=False)
        self.tool.add_incident_note.assert_called_once_with("P001", "Disabled")

    def test_keyword_already_commented(self):
        """Keyword detected but user already commented recently — skip note."""
        inc = _make_incident(title="Ignore this")
        self._setup_incidents([inc])
        self.tool.get_recent_comments = MagicMock(return_value=[])
        self.tool.has_recent_comment_from_user = MagicMock(return_value=True)
        self.tool.add_incident_note = MagicMock()
        self.tool.snooze_incident = MagicMock(return_value=True)

        self.tool.process_and_update_incidents(
            user_id="U1", enable_snooze=True,
        )
        self.tool.add_incident_note.assert_not_called()
        # Still snoozes even if already commented
        self.tool.snooze_incident.assert_called_once()

    def test_no_jira_ticket(self):
        """Incident with no Jira ticket in title or comments."""
        inc = _make_incident(title="Something failed")
        self._setup_incidents([inc])
        self.tool.get_recent_comments = MagicMock(return_value=[])

        result = self.tool.process_and_update_incidents()
        assert "without Jira tickets" in result

    def test_resolved_ticket_not_snoozed(self):
        """Incident with resolved Jira ticket — comment posted, no snooze."""
        inc = _make_incident(title="DSSD-100 batch job")
        self._setup_incidents([inc])
        self.tool.get_recent_comments = MagicMock(return_value=[])
        self.tool.has_recent_comment_from_user = MagicMock(return_value=False)
        self.tool.get_jira_ticket_status = MagicMock(return_value={
            "key": "DSSD-100",
            "summary": "Batch job",
            "status": "Done",
            "assignee": "John",
            "priority": "High",
            "url": "https://jira.example.com/browse/DSSD-100",
        })
        self.tool.add_incident_note = MagicMock(return_value=True)
        self.tool.snooze_incident = MagicMock()

        result = self.tool.process_and_update_incidents(enable_snooze=True)
        # Comment should NOT end with ". Snooze"
        note_arg = self.tool.add_incident_note.call_args[0][1]
        assert "Snooze" not in note_arg
        assert "Done" in note_arg
        self.tool.snooze_incident.assert_not_called()
        assert "Done/Resolved" in result

    def test_normal_ticket_snoozed(self):
        """Open Jira ticket — comment + snooze."""
        inc = _make_incident(title="DSSD-200 issue")
        self._setup_incidents([inc])
        self.tool.get_recent_comments = MagicMock(return_value=[])
        self.tool.has_recent_comment_from_user = MagicMock(return_value=False)
        self.tool.get_jira_ticket_status = MagicMock(return_value={
            "key": "DSSD-200",
            "summary": "Issue",
            "status": "Open",
            "assignee": "Jane",
            "priority": "Medium",
            "url": "https://jira.example.com/browse/DSSD-200",
        })
        self.tool.add_incident_note = MagicMock(return_value=True)
        self.tool.snooze_incident = MagicMock(return_value=True)

        result = self.tool.process_and_update_incidents(enable_snooze=True)
        note_arg = self.tool.add_incident_note.call_args[0][1]
        assert "DSSD-200" in note_arg
        assert "Open" in note_arg
        assert "Snooze" in note_arg
        self.tool.snooze_incident.assert_called_once()

    def test_skipped_recent_comment(self):
        """Incident where user already commented within 12h is skipped."""
        inc = _make_incident(title="DSSD-300 alert")
        self._setup_incidents([inc])
        self.tool.get_recent_comments = MagicMock(return_value=[])
        self.tool.has_recent_comment_from_user = MagicMock(return_value=True)
        self.tool.get_jira_ticket_status = MagicMock(return_value={
            "key": "DSSD-300",
            "summary": "Alert",
            "status": "Open",
            "assignee": "Bob",
            "priority": "Low",
            "url": "https://jira.example.com/browse/DSSD-300",
        })
        self.tool.add_incident_note = MagicMock()

        result = self.tool.process_and_update_incidents(user_id="U1")
        self.tool.add_incident_note.assert_not_called()
        assert "already commented" in result.lower()

    def test_limit_parameter(self):
        """Limit restricts number of processed incidents."""
        incidents = [_make_incident(incident_id=f"P{i}") for i in range(5)]
        self._setup_incidents(incidents)
        self.tool.get_recent_comments = MagicMock(return_value=[])

        result = self.tool.process_and_update_incidents(limit=2)
        assert "Total incidents processed: 2" in result

    def test_jira_fetch_failure(self):
        """When Jira ticket can't be fetched, incident goes to no_jira_tickets."""
        inc = _make_incident(title="DSSD-400 problem")
        self._setup_incidents([inc])
        self.tool.get_recent_comments = MagicMock(return_value=[])
        self.tool.get_jira_ticket_status = MagicMock(return_value=None)

        result = self.tool.process_and_update_incidents()
        assert "without Jira tickets" in result

    def test_ticket_from_comments(self):
        """Jira ticket found in comments, not title."""
        inc = _make_incident(title="Some failure alert")
        self._setup_incidents([inc])
        self.tool.get_recent_comments = MagicMock(return_value=["DSSD-500 - Open - John. Snooze"])
        self.tool.has_recent_comment_from_user = MagicMock(return_value=False)
        self.tool.get_jira_ticket_status = MagicMock(return_value={
            "key": "DSSD-500",
            "summary": "Failure",
            "status": "In Progress",
            "assignee": "John",
            "priority": "High",
            "url": "https://jira.example.com/browse/DSSD-500",
        })
        self.tool.add_incident_note = MagicMock(return_value=True)

        result = self.tool.process_and_update_incidents(enable_snooze=False)
        self.tool.add_incident_note.assert_called_once()
        note_arg = self.tool.add_incident_note.call_args[0][1]
        assert "DSSD-500" in note_arg


# ===========================================================================
# save_summary_to_file
# ===========================================================================

class TestSaveSummaryToFile:
    def test_writes_file(self, tmp_path):
        filepath = str(tmp_path / "summary.txt")
        save_summary_to_file("test content", filename=filepath)
        assert (tmp_path / "summary.txt").read_text() == "test content"

    def test_overwrites_existing(self, tmp_path):
        filepath = str(tmp_path / "summary.txt")
        (tmp_path / "summary.txt").write_text("old")
        save_summary_to_file("new content", filename=filepath)
        assert (tmp_path / "summary.txt").read_text() == "new content"

    def test_default_filename(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        save_summary_to_file("content")
        assert (tmp_path / "pagerduty_summary.txt").read_text() == "content"

    def test_io_error(self, capsys):
        save_summary_to_file("content", filename="/nonexistent/dir/file.txt")
        assert "Failed to save" in capsys.readouterr().out


# ===========================================================================
# JIRA_TICKET_PATTERN regex edge cases
# ===========================================================================

class TestJiraTicketPattern:
    def setup_method(self):
        self.tool = _make_tool()

    def test_single_letter_project(self):
        """Single-letter project codes should not match (need 2+ chars)."""
        result = self.tool.extract_jira_ticket_numbers("A-123")
        assert result == []

    def test_lowercase_not_matched(self):
        """Lowercase project codes don't match the pattern."""
        result = self.tool.extract_jira_ticket_numbers("dssd-123")
        assert result == []

    def test_mixed_alphanum_project(self):
        """Project codes with numbers after first letter."""
        result = self.tool.extract_jira_ticket_numbers("AB2-999")
        assert result == ["AB2-999"]

    def test_long_issue_number(self):
        result = self.tool.extract_jira_ticket_numbers("DSSD-123456")
        assert result == ["DSSD-123456"]


# ===========================================================================
# main() CLI tests
# ===========================================================================

class TestMain:
    @patch("pd_sync.PDSync")
    @patch.dict("os.environ", {
        "PAGERDUTY_API_TOKEN": "tok",
        "JIRA_SERVER_URL": "https://jira.example.com",
        "JIRA_PERSONAL_ACCESS_TOKEN": "pat",
    })
    def test_check_mode(self, mock_cls):
        mock_instance = MagicMock()
        mock_instance.get_current_user_id.return_value = "U1"
        mock_instance.check_incidents.return_value = "summary"
        mock_cls.return_value = mock_instance

        with patch("sys.argv", ["prog", "--check"]):
            from pd_sync import main
            main()

        mock_instance.check_incidents.assert_called_once()

    @patch("pd_sync.PDSync")
    @patch.dict("os.environ", {
        "PAGERDUTY_API_TOKEN": "tok",
        "JIRA_SERVER_URL": "https://jira.example.com",
        "JIRA_PERSONAL_ACCESS_TOKEN": "pat",
    })
    def test_snooze_mode(self, mock_cls):
        mock_instance = MagicMock()
        mock_instance.get_current_user_id.return_value = "U1"
        mock_instance.process_and_update_incidents.return_value = "summary"
        mock_cls.return_value = mock_instance

        with patch("sys.argv", ["prog", "--snooze", "4"]):
            from pd_sync import main
            main()

        call_kwargs = mock_instance.process_and_update_incidents.call_args
        assert call_kwargs[1]["enable_snooze"] is True
        assert call_kwargs[1]["snooze_duration_hours"] == 4.0

    @patch.dict("os.environ", {}, clear=True)
    def test_missing_pd_token_exits(self):
        with patch("sys.argv", ["prog", "--check"]):
            with patch("pd_sync.load_env"):
                from pd_sync import main
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 1

    @patch.dict("os.environ", {
        "PAGERDUTY_API_TOKEN": "tok",
    }, clear=True)
    def test_missing_jira_url_exits(self):
        with patch("sys.argv", ["prog", "--check"]):
            with patch("pd_sync.load_env"):
                from pd_sync import main
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 1

    @patch.dict("os.environ", {
        "PAGERDUTY_API_TOKEN": "tok",
        "JIRA_SERVER_URL": "https://jira.example.com",
    }, clear=True)
    def test_missing_jira_creds_exits(self):
        with patch("sys.argv", ["prog", "--check"]):
            with patch("pd_sync.load_env"):
                from pd_sync import main
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 1

    @patch.dict("os.environ", {
        "PAGERDUTY_API_TOKEN": "tok",
        "JIRA_SERVER_URL": "https://jira.example.com",
        "JIRA_PERSONAL_ACCESS_TOKEN": "pat",
    })
    def test_help_exits_zero(self):
        with patch("sys.argv", ["prog", "--help"]):
            from pd_sync import main
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

    @patch.dict("os.environ", {
        "PAGERDUTY_API_TOKEN": "tok",
        "JIRA_SERVER_URL": "https://jira.example.com",
        "JIRA_PERSONAL_ACCESS_TOKEN": "pat",
    })
    def test_unknown_arg_exits(self):
        with patch("sys.argv", ["prog", "--bogus"]):
            from pd_sync import main
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    @patch("pd_sync.PDSync")
    @patch.dict("os.environ", {
        "PAGERDUTY_API_TOKEN": "tok",
        "JIRA_SERVER_URL": "https://jira.example.com",
        "JIRA_PERSONAL_ACCESS_TOKEN": "pat",
    })
    def test_all_flag_skips_user_filter(self, mock_cls):
        mock_instance = MagicMock()
        mock_instance.check_incidents.return_value = "summary"
        mock_cls.return_value = mock_instance

        with patch("sys.argv", ["prog", "--check", "--all"]):
            from pd_sync import main
            main()

        # check_incidents called with user_id=None
        call_kwargs = mock_instance.check_incidents.call_args
        assert call_kwargs[1]["user_id"] is None

    @patch("pd_sync.PDSync")
    @patch.dict("os.environ", {
        "PAGERDUTY_API_TOKEN": "tok",
        "JIRA_SERVER_URL": "https://jira.example.com",
        "JIRA_PERSONAL_ACCESS_TOKEN": "pat",
    })
    def test_update_mode_with_limit(self, mock_cls):
        mock_instance = MagicMock()
        mock_instance.get_current_user_id.return_value = "U1"
        mock_instance.process_and_update_incidents.return_value = "summary"
        mock_cls.return_value = mock_instance

        with patch("sys.argv", ["prog", "--update", "--limit", "5"]):
            from pd_sync import main
            main()

        call_kwargs = mock_instance.process_and_update_incidents.call_args
        assert call_kwargs[1]["limit"] == 5
