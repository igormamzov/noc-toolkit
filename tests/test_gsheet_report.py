"""Tests for gsheet_report.py (Google Sheets adapter)."""

import json
import pytest
from unittest.mock import patch, MagicMock

from gsheet_report import (
    GSheetClient,
    JiraClient,
    build_status_string,
    do_sync,
    do_add_row,
    do_start_shift,
    VERSION,
    SHEETS,
    STATUS_MAP,
    TICKET_REGEX,
    NOTE_REGEX,
)


# ===================================================================
# build_status_string
# ===================================================================

class TestBuildStatusString:

    def test_basic_status(self):
        result = build_status_string("Open", "John Doe", "")
        assert result == "OPEN John Doe"

    def test_work_in_progress_mapped(self):
        result = build_status_string("Work In Progress", "Alice", "")
        assert result == "IN PROGRESS Alice"

    def test_unassigned(self):
        result = build_status_string("Open", "Unassigned", "")
        assert result == "OPEN Unassigned"

    def test_preserves_parenthetical_note(self):
        result = build_status_string("Open", "Bob", "OPEN Alice (recurring)")
        assert result == "OPEN Bob (recurring)"

    def test_no_note_in_old_value(self):
        result = build_status_string("Done", "Alice", "IN PROGRESS Alice")
        assert result == "DONE Alice"

    def test_multiple_notes_preserves_first(self):
        result = build_status_string("Open", "Bob", "OPEN Alice (note1) (note2)")
        assert result == "OPEN Bob (note1)"

    def test_status_uppercased(self):
        result = build_status_string("resolved", "Jane", "")
        assert result == "RESOLVED Jane"

    def test_blocked_status(self):
        result = build_status_string("Blocked", "Dev Team", "")
        assert result == "BLOCKED Dev Team"


# ===================================================================
# TICKET_REGEX
# ===================================================================

class TestTicketRegex:

    def test_dssd_ticket(self):
        match = TICKET_REGEX.search("DSSD-29540")
        assert match and match.group(1) == "DSSD-29540"

    def test_coredata_ticket(self):
        match = TICKET_REGEX.search("COREDATA-5821")
        assert match and match.group(1) == "COREDATA-5821"

    def test_drgn_ticket(self):
        match = TICKET_REGEX.search("DRGN-50001")
        assert match and match.group(1) == "DRGN-50001"

    def test_ticket_in_url(self):
        match = TICKET_REGEX.search("https://jira.example.com/browse/DSSD-29137")
        assert match and match.group(1) == "DSSD-29137"

    def test_no_match(self):
        match = TICKET_REGEX.search("no ticket here")
        assert match is None

    def test_lowercase_no_match(self):
        match = TICKET_REGEX.search("dssd-123")
        assert match is None


# ===================================================================
# NOTE_REGEX
# ===================================================================

class TestNoteRegex:

    def test_finds_parenthetical(self):
        match = NOTE_REGEX.search("OPEN Alice (recurring)")
        assert match and match.group(0) == "(recurring)"

    def test_no_match(self):
        match = NOTE_REGEX.search("OPEN Alice")
        assert match is None

    def test_empty_parens(self):
        match = NOTE_REGEX.search("OPEN Alice ()")
        assert match and match.group(0) == "()"


# ===================================================================
# GSheetClient
# ===================================================================

# Sample API responses
SAMPLE_READ_RESPONSE = {
    "ok": True,
    "sheetName": "Night-Shift-NEW",
    "date": {"day": 15, "month": "Mar"},
    "layout": {
        "fromPrevRow": 8,
        "fromPrevEnd": 10,
        "ttmRow": 11,
        "ttmEnd": 11,
        "permalinksRow": 12,
    },
    "tickets": [
        {
            "row": 8,
            "summary": "Data export job failed",
            "ticketId": "DSSD-29001",
            "ticketHyperlink": "https://jira.example.com/browse/DSSD-29001",
            "status": "OPEN Unassigned",
            "slackText": "slack_link",
            "slackHyperlink": "https://company.slack.com/archives/C123/p111",
            "section": "fromPrev",
        },
        {
            "row": 9,
            "summary": "Batch job failing",
            "ticketId": "DSSD-29002",
            "ticketHyperlink": "https://jira.example.com/browse/DSSD-29002",
            "status": "IN PROGRESS John Doe",
            "slackText": "slack_link",
            "slackHyperlink": "https://company.slack.com/archives/C123/p222",
            "section": "fromPrev",
        },
        {
            "row": 10,
            "summary": "RDS export failed",
            "ticketId": "DSSD-29003",
            "ticketHyperlink": "https://jira.example.com/browse/DSSD-29003",
            "status": "OPEN Jane Smith (recurring)",
            "slackText": "slack_link",
            "slackHyperlink": "https://company.slack.com/archives/C123/p333",
            "section": "fromPrev",
        },
        {
            "row": 11,
            "summary": "New issue during shift",
            "ticketId": "DRGN-50001",
            "ticketHyperlink": "https://jira.example.com/browse/DRGN-50001",
            "status": "OPEN Unassigned",
            "slackText": "slack_link",
            "slackHyperlink": "https://company.slack.com/archives/C789/p666",
            "section": "ttm",
        },
    ],
}

SAMPLE_SYNC_RESPONSE = {"ok": True, "updated": 2}
SAMPLE_ADD_ROW_RESPONSE = {"ok": True, "insertedRow": 12}
SAMPLE_START_SHIFT_RESPONSE = {
    "ok": True,
    "ticketsCopied": 3,
    "dateDay": 16,
    "dateMonth": "Mar",
}


def _mock_urlopen(response_data):
    """Create a mock for urlopen that returns the given JSON data."""
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(response_data).encode("utf-8")
    mock_response.__enter__ = lambda self: self
    mock_response.__exit__ = MagicMock(return_value=False)
    return mock_response


class TestGSheetClient:

    def test_init(self):
        client = GSheetClient("https://example.com/exec/", "mykey")
        assert client.webapp_url == "https://example.com/exec"
        assert client.api_key == "mykey"

    def test_init_strips_trailing_slash(self):
        client = GSheetClient("https://example.com/exec///", "key")
        assert client.webapp_url == "https://example.com/exec"

    @patch("gsheet_report.urlopen")
    def test_read_sheet(self, mock_urlopen_fn):
        mock_urlopen_fn.return_value = _mock_urlopen(SAMPLE_READ_RESPONSE)
        client = GSheetClient("https://example.com/exec", "key123")
        result = client.read_sheet("Night-Shift-NEW")
        assert result["ok"] is True
        assert len(result["tickets"]) == 4
        assert result["date"]["day"] == 15

    @patch("gsheet_report.urlopen")
    def test_sync_statuses(self, mock_urlopen_fn):
        mock_urlopen_fn.return_value = _mock_urlopen(SAMPLE_SYNC_RESPONSE)
        client = GSheetClient("https://example.com/exec", "key123")
        updates = [{"row": 8, "value": "DONE Alice"}, {"row": 9, "value": "BLOCKED Bob"}]
        result = client.sync_statuses("Night-Shift-NEW", updates)
        assert result["ok"] is True
        assert result["updated"] == 2

    @patch("gsheet_report.urlopen")
    def test_add_row(self, mock_urlopen_fn):
        mock_urlopen_fn.return_value = _mock_urlopen(SAMPLE_ADD_ROW_RESPONSE)
        client = GSheetClient("https://example.com/exec", "key123")
        result = client.add_row("Night-Shift-NEW", {
            "summary": "Test",
            "ticketId": "DSSD-99999",
            "jiraLink": "https://jira.example.com/browse/DSSD-99999",
            "status": "OPEN Unassigned",
            "slackText": "slack_link",
            "slackLink": "https://company.slack.com/archives/C123/p999",
        })
        assert result["ok"] is True
        assert result["insertedRow"] == 12

    @patch("gsheet_report.urlopen")
    def test_start_shift(self, mock_urlopen_fn):
        mock_urlopen_fn.return_value = _mock_urlopen(SAMPLE_START_SHIFT_RESPONSE)
        client = GSheetClient("https://example.com/exec", "key123")
        result = client.start_shift("Night-Shift-NEW")
        assert result["ok"] is True
        assert result["ticketsCopied"] == 3

    @patch("gsheet_report.urlopen")
    def test_read_sheet_includes_key_in_url(self, mock_urlopen_fn):
        mock_urlopen_fn.return_value = _mock_urlopen(SAMPLE_READ_RESPONSE)
        client = GSheetClient("https://example.com/exec", "secret")
        client.read_sheet("Night-Shift-NEW")
        call_args = mock_urlopen_fn.call_args
        request = call_args[0][0]
        assert "key=secret" in request.full_url

    @patch("gsheet_report.urlopen")
    def test_sync_includes_key_in_body(self, mock_urlopen_fn):
        mock_urlopen_fn.return_value = _mock_urlopen(SAMPLE_SYNC_RESPONSE)
        client = GSheetClient("https://example.com/exec", "secret")
        client.sync_statuses("Night-Shift-NEW", [])
        call_args = mock_urlopen_fn.call_args
        request = call_args[0][0]
        body = json.loads(request.data)
        assert body["key"] == "secret"

    @patch("gsheet_report.urlopen")
    def test_get_network_error(self, mock_urlopen_fn):
        from urllib.error import URLError
        mock_urlopen_fn.side_effect = URLError("connection refused")
        client = GSheetClient("https://example.com/exec", "key")
        with pytest.raises(RuntimeError, match="Apps Script GET failed"):
            client.read_sheet("Night-Shift-NEW")

    @patch("gsheet_report.urlopen")
    def test_post_network_error(self, mock_urlopen_fn):
        from urllib.error import URLError
        mock_urlopen_fn.side_effect = URLError("connection refused")
        client = GSheetClient("https://example.com/exec", "key")
        with pytest.raises(RuntimeError, match="Apps Script POST failed"):
            client.sync_statuses("Night-Shift-NEW", [])


# ===================================================================
# JiraClient
# ===================================================================

SAMPLE_JIRA_STATUS_RESPONSE = {
    "fields": {
        "status": {"name": "In Progress"},
        "assignee": {"displayName": "Alice Wonder"},
    }
}

SAMPLE_JIRA_FULL_RESPONSE = {
    "fields": {
        "status": {"name": "Open"},
        "assignee": {"displayName": "Bob Builder"},
        "summary": "Batch job failing on step 3",
    }
}

SAMPLE_JIRA_UNASSIGNED_RESPONSE = {
    "fields": {
        "status": {"name": "Open"},
        "assignee": None,
    }
}


class TestJiraClient:

    @patch("gsheet_report.urlopen")
    def test_fetch_status(self, mock_urlopen_fn):
        mock_urlopen_fn.return_value = _mock_urlopen(SAMPLE_JIRA_STATUS_RESPONSE)
        jira = JiraClient("https://jira.example.com", "token123")
        status, assignee = jira.fetch_status("DSSD-29001")
        assert status == "In Progress"
        assert assignee == "Alice Wonder"

    @patch("gsheet_report.urlopen")
    def test_fetch_status_unassigned(self, mock_urlopen_fn):
        mock_urlopen_fn.return_value = _mock_urlopen(SAMPLE_JIRA_UNASSIGNED_RESPONSE)
        jira = JiraClient("https://jira.example.com", "token123")
        status, assignee = jira.fetch_status("DSSD-29001")
        assert status == "Open"
        assert assignee == "Unassigned"

    @patch("gsheet_report.urlopen")
    def test_fetch_status_failure(self, mock_urlopen_fn):
        from urllib.error import URLError
        mock_urlopen_fn.side_effect = URLError("timeout")
        jira = JiraClient("https://jira.example.com", "token123")
        status, assignee = jira.fetch_status("DSSD-29001")
        assert status is None
        assert assignee is None

    @patch("gsheet_report.urlopen")
    def test_fetch_full(self, mock_urlopen_fn):
        mock_urlopen_fn.return_value = _mock_urlopen(SAMPLE_JIRA_FULL_RESPONSE)
        jira = JiraClient("https://jira.example.com", "token123")
        summary, status, assignee = jira.fetch_full("DSSD-29002")
        assert summary == "Batch job failing on step 3"
        assert status == "Open"
        assert assignee == "Bob Builder"

    @patch("gsheet_report.urlopen")
    def test_fetch_full_failure_raises(self, mock_urlopen_fn):
        from urllib.error import URLError
        mock_urlopen_fn.side_effect = URLError("timeout")
        jira = JiraClient("https://jira.example.com", "token123")
        with pytest.raises(RuntimeError, match="Failed to fetch Jira issue"):
            jira.fetch_full("DSSD-29002")

    def test_init_strips_url(self):
        jira = JiraClient("https://jira.example.com/", "tok")
        assert jira.jira_url == "https://jira.example.com"

    @patch("gsheet_report.urlopen")
    def test_uses_bearer_auth(self, mock_urlopen_fn):
        mock_urlopen_fn.return_value = _mock_urlopen(SAMPLE_JIRA_STATUS_RESPONSE)
        jira = JiraClient("https://jira.example.com", "my_secret_token")
        jira.fetch_status("DSSD-29001")
        call_args = mock_urlopen_fn.call_args
        request = call_args[0][0]
        assert request.get_header("Authorization") == "Bearer my_secret_token"


# ===================================================================
# do_sync
# ===================================================================

class TestDoSync:

    def _make_gsheet_mock(self, read_response):
        gsheet = MagicMock(spec=GSheetClient)
        gsheet.read_sheet.return_value = read_response
        gsheet.sync_statuses.return_value = {"ok": True, "updated": 0}
        return gsheet

    def _make_jira_mock(self, responses):
        """responses: dict of ticket_id -> (status, assignee)"""
        jira = MagicMock(spec=JiraClient)
        def fetch_status(ticket_id):
            if ticket_id in responses:
                return responses[ticket_id]
            return None, None
        jira.fetch_status.side_effect = fetch_status
        return jira

    def test_no_changes(self):
        gsheet = self._make_gsheet_mock(SAMPLE_READ_RESPONSE)
        jira = self._make_jira_mock({
            "DSSD-29001": ("Open", "Unassigned"),
            "DSSD-29002": ("In Progress", "John Doe"),
            "DSSD-29003": ("Open", "Jane Smith"),
            "DRGN-50001": ("Open", "Unassigned"),
        })
        changes = do_sync(gsheet, jira, "Night-Shift-NEW")
        assert len(changes) == 0
        gsheet.sync_statuses.assert_not_called()

    def test_detects_changes(self):
        gsheet = self._make_gsheet_mock(SAMPLE_READ_RESPONSE)
        jira = self._make_jira_mock({
            "DSSD-29001": ("Done", "Alice"),       # changed
            "DSSD-29002": ("In Progress", "John Doe"),  # same
            "DSSD-29003": ("Open", "Jane Smith"),   # same (note preserved)
            "DRGN-50001": ("Blocked", "Bob"),       # changed
        })
        changes = do_sync(gsheet, jira, "Night-Shift-NEW")
        assert len(changes) == 2
        assert changes[0]["ticket_id"] == "DSSD-29001"
        assert changes[0]["new"] == "DONE Alice"
        assert changes[1]["ticket_id"] == "DRGN-50001"
        assert changes[1]["new"] == "BLOCKED Bob"

    def test_pushes_updates_to_gsheet(self):
        gsheet = self._make_gsheet_mock(SAMPLE_READ_RESPONSE)
        gsheet.sync_statuses.return_value = {"ok": True, "updated": 1}
        jira = self._make_jira_mock({
            "DSSD-29001": ("Done", "Alice"),
            "DSSD-29002": ("In Progress", "John Doe"),
            "DSSD-29003": ("Open", "Jane Smith"),
            "DRGN-50001": ("Open", "Unassigned"),
        })
        do_sync(gsheet, jira, "Night-Shift-NEW")
        gsheet.sync_statuses.assert_called_once()
        call_args = gsheet.sync_statuses.call_args
        assert call_args[0][0] == "Night-Shift-NEW"
        updates = call_args[0][1]
        assert len(updates) == 1
        assert updates[0]["row"] == 8
        assert updates[0]["value"] == "DONE Alice"

    def test_dry_run_no_push(self):
        gsheet = self._make_gsheet_mock(SAMPLE_READ_RESPONSE)
        jira = self._make_jira_mock({
            "DSSD-29001": ("Done", "Alice"),
            "DSSD-29002": ("Done", "Bob"),
            "DSSD-29003": ("Done", "Carol"),
            "DRGN-50001": ("Done", "Dave"),
        })
        changes = do_sync(gsheet, jira, "Night-Shift-NEW", dry_run=True)
        assert len(changes) > 0
        gsheet.sync_statuses.assert_not_called()

    def test_jira_failure_skips_ticket(self):
        gsheet = self._make_gsheet_mock(SAMPLE_READ_RESPONSE)
        jira = self._make_jira_mock({
            "DSSD-29001": (None, None),  # Jira failure
            "DSSD-29002": ("Done", "Bob"),
            "DSSD-29003": (None, None),
            "DRGN-50001": (None, None),
        })
        # fetch_status returns None for failures, so override side_effect
        def fetch_status(tid):
            mapping = {
                "DSSD-29001": (None, None),
                "DSSD-29002": ("Done", "Bob"),
                "DSSD-29003": (None, None),
                "DRGN-50001": (None, None),
            }
            return mapping.get(tid, (None, None))
        jira.fetch_status.side_effect = fetch_status
        changes = do_sync(gsheet, jira, "Night-Shift-NEW")
        assert len(changes) == 1
        assert changes[0]["ticket_id"] == "DSSD-29002"

    def test_read_failure_raises(self):
        gsheet = MagicMock(spec=GSheetClient)
        gsheet.read_sheet.return_value = {"error": "sheet not found"}
        jira = MagicMock(spec=JiraClient)
        with pytest.raises(RuntimeError, match="Failed to read sheet"):
            do_sync(gsheet, jira, "Night-Shift-NEW")

    def test_preserves_note_on_change(self):
        gsheet = self._make_gsheet_mock(SAMPLE_READ_RESPONSE)
        jira = self._make_jira_mock({
            "DSSD-29001": ("Open", "Unassigned"),
            "DSSD-29002": ("In Progress", "John Doe"),
            "DSSD-29003": ("Done", "Alice"),  # changed, old has (recurring)
            "DRGN-50001": ("Open", "Unassigned"),
        })
        changes = do_sync(gsheet, jira, "Night-Shift-NEW")
        assert len(changes) == 1
        assert changes[0]["ticket_id"] == "DSSD-29003"
        assert "(recurring)" in changes[0]["new"]

    def test_empty_ticket_list(self):
        empty_response = dict(SAMPLE_READ_RESPONSE)
        empty_response = {**SAMPLE_READ_RESPONSE, "tickets": []}
        gsheet = self._make_gsheet_mock(empty_response)
        jira = MagicMock(spec=JiraClient)
        changes = do_sync(gsheet, jira, "Night-Shift-NEW")
        assert len(changes) == 0
        jira.fetch_status.assert_not_called()


# ===================================================================
# do_add_row
# ===================================================================

class TestDoAddRow:

    def test_add_row_success(self):
        gsheet = MagicMock(spec=GSheetClient)
        gsheet.add_row.return_value = {"ok": True, "insertedRow": 15}

        jira = MagicMock(spec=JiraClient)
        jira.fetch_full.return_value = ("Batch job failed", "Open", "Unassigned")

        row = do_add_row(
            gsheet, jira, "Night-Shift-NEW",
            "https://jira.example.com/browse/DSSD-29999",
            "https://company.slack.com/archives/C123/p999",
        )
        assert row == 15
        gsheet.add_row.assert_called_once()
        call_data = gsheet.add_row.call_args[0][1]
        assert call_data["ticketId"] == "DSSD-29999"
        assert call_data["summary"] == "Batch job failed"
        assert call_data["status"] == "OPEN Unassigned"

    def test_add_row_dry_run(self):
        gsheet = MagicMock(spec=GSheetClient)
        jira = MagicMock(spec=JiraClient)
        jira.fetch_full.return_value = ("Test summary", "Open", "Bob")

        row = do_add_row(
            gsheet, jira, "Night-Shift-NEW",
            "https://jira.example.com/browse/DSSD-29999",
            "https://company.slack.com/archives/C123/p999",
            dry_run=True,
        )
        assert row is None
        gsheet.add_row.assert_not_called()

    def test_invalid_jira_link_raises(self):
        gsheet = MagicMock(spec=GSheetClient)
        jira = MagicMock(spec=JiraClient)
        with pytest.raises(ValueError, match="Could not extract ticket ID"):
            do_add_row(gsheet, jira, "Night-Shift-NEW", "not-a-link", "slack")

    def test_add_row_api_failure_raises(self):
        gsheet = MagicMock(spec=GSheetClient)
        gsheet.add_row.return_value = {"error": "something went wrong"}

        jira = MagicMock(spec=JiraClient)
        jira.fetch_full.return_value = ("Summary", "Open", "Unassigned")

        with pytest.raises(RuntimeError, match="Add row failed"):
            do_add_row(
                gsheet, jira, "Night-Shift-NEW",
                "https://jira.example.com/browse/DSSD-29999",
                "https://company.slack.com/archives/C123/p999",
            )

    def test_extracts_ticket_from_servicedesk_url(self):
        gsheet = MagicMock(spec=GSheetClient)
        gsheet.add_row.return_value = {"ok": True, "insertedRow": 20}
        jira = MagicMock(spec=JiraClient)
        jira.fetch_full.return_value = ("Summary", "Open", "Unassigned")

        do_add_row(
            gsheet, jira, "Night-Shift-NEW",
            "https://jira.example.com/servicedesk/customer/portal/324/DSSD-29540",
            "https://company.slack.com/archives/C123/p999",
        )
        jira.fetch_full.assert_called_with("DSSD-29540")


# ===================================================================
# do_start_shift
# ===================================================================

class TestDoStartShift:

    def test_start_shift_success(self):
        gsheet = MagicMock(spec=GSheetClient)
        gsheet.start_shift.return_value = {
            "ok": True, "ticketsCopied": 5, "dateDay": 16, "dateMonth": "Mar",
        }
        gsheet.read_sheet.return_value = {**SAMPLE_READ_RESPONSE, "tickets": []}

        jira = MagicMock(spec=JiraClient)

        result = do_start_shift(gsheet, jira, "Night-Shift-NEW")
        assert result["tickets_copied"] == 5
        assert result["date_day"] == 16
        assert result["date_month"] == "Mar"
        gsheet.start_shift.assert_called_once_with("Night-Shift-NEW")

    def test_start_shift_dry_run(self):
        gsheet = MagicMock(spec=GSheetClient)
        gsheet.read_sheet.return_value = SAMPLE_READ_RESPONSE

        jira = MagicMock(spec=JiraClient)

        result = do_start_shift(gsheet, jira, "Night-Shift-NEW", dry_run=True)
        assert result["dry_run"] is True
        assert result["tickets_copied"] == 4
        gsheet.start_shift.assert_not_called()

    def test_start_shift_api_failure_raises(self):
        gsheet = MagicMock(spec=GSheetClient)
        gsheet.start_shift.return_value = {"error": "sheet not found"}

        jira = MagicMock(spec=JiraClient)

        with pytest.raises(RuntimeError, match="Start shift failed"):
            do_start_shift(gsheet, jira, "Night-Shift-NEW")

    def test_start_shift_opposite_sheet_night(self):
        gsheet = MagicMock(spec=GSheetClient)
        gsheet.start_shift.return_value = {
            "ok": True, "ticketsCopied": 3, "dateDay": 15, "dateMonth": "Mar",
        }
        gsheet.read_sheet.return_value = {**SAMPLE_READ_RESPONSE, "tickets": []}
        jira = MagicMock(spec=JiraClient)

        do_start_shift(gsheet, jira, "Night-Shift-NEW")
        gsheet.start_shift.assert_called_with("Night-Shift-NEW")

    def test_start_shift_opposite_sheet_day(self):
        gsheet = MagicMock(spec=GSheetClient)
        gsheet.start_shift.return_value = {
            "ok": True, "ticketsCopied": 3, "dateDay": 15, "dateMonth": "Mar",
        }
        gsheet.read_sheet.return_value = {**SAMPLE_READ_RESPONSE, "tickets": []}
        jira = MagicMock(spec=JiraClient)

        do_start_shift(gsheet, jira, "Day-Shift-NEW")
        gsheet.start_shift.assert_called_with("Day-Shift-NEW")

    def test_start_shift_runs_sync_after(self):
        gsheet = MagicMock(spec=GSheetClient)
        gsheet.start_shift.return_value = {
            "ok": True, "ticketsCopied": 2, "dateDay": 16, "dateMonth": "Mar",
        }
        # After start_shift, do_sync reads the sheet
        read_after = {**SAMPLE_READ_RESPONSE, "tickets": [
            {
                "row": 8, "summary": "Test", "ticketId": "DSSD-29001",
                "ticketHyperlink": None, "status": "OPEN Unassigned",
                "slackText": "", "slackHyperlink": None, "section": "fromPrev",
            },
        ]}
        gsheet.read_sheet.return_value = read_after
        gsheet.sync_statuses.return_value = {"ok": True, "updated": 1}

        jira = MagicMock(spec=JiraClient)
        jira.fetch_status.return_value = ("Done", "Alice")

        result = do_start_shift(gsheet, jira, "Night-Shift-NEW")
        assert result["sync_changes"] == 1
        gsheet.read_sheet.assert_called()


# ===================================================================
# STATUS_MAP
# ===================================================================

class TestStatusMap:

    def test_work_in_progress(self):
        assert STATUS_MAP["WORK IN PROGRESS"] == "IN PROGRESS"

    def test_no_other_mappings(self):
        assert len(STATUS_MAP) == 1


# ===================================================================
# Constants
# ===================================================================

class TestConstants:

    def test_version_format(self):
        parts = VERSION.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_sheets_list(self):
        assert SHEETS == ["Night-Shift-NEW", "Day-Shift-NEW"]
