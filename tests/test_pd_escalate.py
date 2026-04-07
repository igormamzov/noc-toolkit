"""Tests for pd-escalate tool (PD Escalation Tool v0.1.1)."""

import logging
import sys
from unittest.mock import MagicMock, patch, call
from typing import Any, Dict, List, Optional

import pytest

# Must be importable via conftest.py sys.path setup
from pd_escalate import EscalateTool, extract_incident_id, DRGN_PATTERN


# ---------------------------------------------------------------------------
# Helpers: build mock EscalateTool without real API clients
# ---------------------------------------------------------------------------

def _make_tool(dry_run: bool = False) -> EscalateTool:
    """Create an EscalateTool with mocked PD and Jira clients."""
    with patch("noc_utils._pagerduty.RestApiV2Client"), \
         patch("noc_utils.JIRA"):
        tool = EscalateTool(
            pagerduty_api_token="fake-pd-token",
            jira_server_url="https://jira.example.com",
            jira_personal_access_token="fake-jira-token",
            dry_run=dry_run,
        )
    # Replace clients with fresh mocks for explicit control
    tool.pd_client = MagicMock()
    tool.jira_client = MagicMock()
    return tool


def _pd_incident_response(
    incident_id: str = "ABC123",
    title: str = "Test Incident",
    status: str = "triggered",
    priority_summary: Optional[str] = "P2",
    incident_number: int = 42,
    drgn_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a mock PD incident API response."""
    external_references: List[Dict[str, str]] = []
    if drgn_key:
        external_references.append({"external_id": drgn_key})

    priority = {"summary": priority_summary} if priority_summary else None

    return {
        "incident": {
            "id": incident_id,
            "title": title,
            "status": status,
            "priority": priority,
            "incident_number": incident_number,
            "html_url": f"https://pd.example.com/incidents/{incident_id}",
            "alert_counts": {"all": 3},
            "external_references": external_references,
        }
    }


def _jira_issue_mock(
    key: str = "DSSD-29386",
    status: str = "Open",
    assignee: Optional[str] = "John Doe",
    summary: str = "Test summary",
) -> MagicMock:
    """Build a mock Jira issue object."""
    issue = MagicMock()
    issue.key = key
    issue.fields.status = MagicMock(__str__=lambda self: status)
    if assignee:
        issue.fields.assignee = MagicMock()
        issue.fields.assignee.displayName = assignee
    else:
        issue.fields.assignee = None
    issue.fields.summary = summary
    return issue


# ===========================================================================
# Tests
# ===========================================================================


class TestExtractIncidentId:
    """Test extract_incident_id() pure function."""

    def test_plain_id(self):
        assert extract_incident_id("Q33L5GALLQ3ESB") == "Q33L5GALLQ3ESB"

    def test_url(self):
        url = "https://yourcompany.pagerduty.com/incidents/Q33L5GALLQ3ESB"
        assert extract_incident_id(url) == "Q33L5GALLQ3ESB"

    def test_url_with_trailing_slash(self):
        url = "https://yourcompany.pagerduty.com/incidents/Q33L5GALLQ3ESB/"
        assert extract_incident_id(url) == "Q33L5GALLQ3ESB"

    def test_whitespace_stripped(self):
        assert extract_incident_id("  Q33L5GALLQ3ESB  ") == "Q33L5GALLQ3ESB"


class TestDrgnPattern:
    """Test DRGN_PATTERN regex."""

    def test_matches_drgn(self):
        match = DRGN_PATTERN.search("Escalated to DRGN-15087")
        assert match and match.group(1) == "DRGN-15087"

    def test_no_match(self):
        assert DRGN_PATTERN.search("No ticket here") is None

    def test_word_boundary(self):
        assert DRGN_PATTERN.search("XDRGN-123") is None


class TestGetCurrentUser:
    """Test EscalateTool.get_current_user()."""

    def test_success(self):
        tool = _make_tool()
        tool.pd_client.rget.return_value = {
            "user": {"id": "USER123", "email": "test@example.com"}
        }
        tool.get_current_user()
        assert tool.user_id == "USER123"
        assert tool.user_email == "test@example.com"

    def test_flat_response(self):
        """API may return user dict without 'user' wrapper."""
        tool = _make_tool()
        tool.pd_client.rget.return_value = {
            "id": "USER456", "email": "flat@example.com"
        }
        tool.get_current_user()
        assert tool.user_id == "USER456"
        assert tool.user_email == "flat@example.com"

    def test_api_error_raises(self):
        tool = _make_tool()
        import pagerduty
        tool.pd_client.rget.side_effect = pagerduty.Error("API failure")
        with pytest.raises(RuntimeError, match="Failed to fetch current user"):
            tool.get_current_user()

    def test_unexpected_type_raises(self):
        tool = _make_tool()
        tool.pd_client.rget.return_value = "not a dict"
        with pytest.raises(RuntimeError, match="Unexpected API response type"):
            tool.get_current_user()


class TestFetchIncident:
    """Test EscalateTool.fetch_incident()."""

    def test_with_drgn_in_external_refs(self):
        tool = _make_tool()
        tool.pd_client.rget.return_value = _pd_incident_response(
            drgn_key="DRGN-15087",
        )
        result = tool.fetch_incident("ABC123")
        assert result["id"] == "ABC123"
        assert result["drgn_key"] == "DRGN-15087"
        assert result["title"] == "Test Incident"
        assert result["alert_count"] == 3

    def test_without_drgn(self):
        tool = _make_tool()
        tool.pd_client.rget.return_value = _pd_incident_response()
        result = tool.fetch_incident("ABC123")
        assert result["drgn_key"] is None

    def test_no_priority(self):
        tool = _make_tool()
        tool.pd_client.rget.return_value = _pd_incident_response(
            priority_summary=None,
        )
        result = tool.fetch_incident("ABC123")
        assert result["priority"] == "—"

    def test_api_error_raises(self):
        tool = _make_tool()
        import pagerduty
        tool.pd_client.rget.side_effect = pagerduty.Error("timeout")
        with pytest.raises(RuntimeError, match="Failed to fetch incident"):
            tool.fetch_incident("ABC123")


class TestDetectDrgnFromNotes:
    """Test EscalateTool.detect_drgn_from_notes()."""

    def test_found_in_notes(self):
        tool = _make_tool()
        tool.pd_client.list_all.return_value = [
            {"content": "Working on it"},
            {"content": "Escalated to DRGN-15087, linked DSSD-29386"},
        ]
        result = tool.detect_drgn_from_notes("ABC123")
        assert result == "DRGN-15087"

    def test_not_found(self):
        tool = _make_tool()
        tool.pd_client.list_all.return_value = [
            {"content": "Just a regular note"},
        ]
        assert tool.detect_drgn_from_notes("ABC123") is None

    def test_empty_notes(self):
        tool = _make_tool()
        tool.pd_client.list_all.return_value = []
        assert tool.detect_drgn_from_notes("ABC123") is None

    def test_api_error_returns_none(self):
        tool = _make_tool()
        import pagerduty
        tool.pd_client.list_all.side_effect = pagerduty.Error("fail")
        assert tool.detect_drgn_from_notes("ABC123") is None


class TestFetchJiraIssue:
    """Test EscalateTool.fetch_jira_issue()."""

    def test_with_assignee(self):
        tool = _make_tool()
        tool.jira_client.issue.return_value = _jira_issue_mock(
            key="DSSD-29386", status="Open", assignee="John Doe",
        )
        result = tool.fetch_jira_issue("DSSD-29386")
        assert result["key"] == "DSSD-29386"
        assert result["status"] == "Open"
        assert result["assignee"] == "John Doe"

    def test_unassigned(self):
        tool = _make_tool()
        tool.jira_client.issue.return_value = _jira_issue_mock(assignee=None)
        result = tool.fetch_jira_issue("DSSD-29386")
        assert result["assignee"] == "Unassigned"

    def test_jira_error_raises(self):
        tool = _make_tool()
        from jira.exceptions import JIRAError
        tool.jira_client.issue.side_effect = JIRAError("Not found")
        with pytest.raises(RuntimeError, match="Failed to fetch Jira issue"):
            tool.fetch_jira_issue("DSSD-99999")


class TestLinkJiraIssues:
    """Test EscalateTool.link_jira_issues()."""

    def test_normal_mode(self):
        tool = _make_tool(dry_run=False)
        tool.link_jira_issues("DRGN-15087", "DSSD-29386")
        tool.jira_client.create_issue_link.assert_called_once_with(
            type="Blocks",
            inwardIssue="DRGN-15087",
            outwardIssue="DSSD-29386",
        )

    def test_dry_run_no_api_call(self):
        tool = _make_tool(dry_run=True)
        tool.link_jira_issues("DRGN-15087", "DSSD-29386")
        tool.jira_client.create_issue_link.assert_not_called()

    def test_jira_error_raises(self):
        tool = _make_tool(dry_run=False)
        from jira.exceptions import JIRAError
        tool.jira_client.create_issue_link.side_effect = JIRAError("fail")
        with pytest.raises(RuntimeError, match="Failed to link"):
            tool.link_jira_issues("DRGN-15087", "DSSD-29386")


class TestTransitionToEscalated:
    """Test EscalateTool.transition_to_escalated()."""

    def test_normal_mode(self):
        tool = _make_tool(dry_run=False)
        tool.transition_to_escalated("DRGN-15087")
        tool.jira_client.transition_issue.assert_called_once_with(
            "DRGN-15087", "51",
        )

    def test_dry_run_no_api_call(self):
        tool = _make_tool(dry_run=True)
        tool.transition_to_escalated("DRGN-15087")
        tool.jira_client.transition_issue.assert_not_called()

    def test_jira_error_raises(self):
        tool = _make_tool(dry_run=False)
        from jira.exceptions import JIRAError
        tool.jira_client.transition_issue.side_effect = JIRAError("bad transition")
        with pytest.raises(RuntimeError, match="Failed to transition"):
            tool.transition_to_escalated("DRGN-15087")


class TestAddPdNote:
    """Test EscalateTool.add_pd_note()."""

    def test_normal_mode(self):
        tool = _make_tool(dry_run=False)
        tool.user_email = "test@example.com"
        dssd_info = {"status": "Open", "assignee": "John Doe"}

        tool.add_pd_note("ABC123", "DRGN-15087", "DSSD-29386", dssd_info)

        tool.pd_client.rpost.assert_called_once()
        call_args = tool.pd_client.rpost.call_args
        assert "incidents/ABC123/notes" in call_args[0][0]
        note_content = call_args[1]["json"]["note"]["content"]
        assert "DSSD-29386" in note_content
        assert "DRGN-15087" in note_content
        assert call_args[1]["headers"]["From"] == "test@example.com"

    def test_dry_run_no_api_call(self):
        tool = _make_tool(dry_run=True)
        dssd_info = {"status": "Open", "assignee": "John Doe"}
        tool.add_pd_note("ABC123", "DRGN-15087", "DSSD-29386", dssd_info)
        tool.pd_client.rpost.assert_not_called()

    def test_api_error_raises(self):
        tool = _make_tool(dry_run=False)
        tool.user_email = "test@example.com"
        import pagerduty
        tool.pd_client.rpost.side_effect = pagerduty.Error("fail")
        with pytest.raises(RuntimeError, match="Failed to add PD note"):
            tool.add_pd_note("ABC123", "DRGN-15087", "DSSD-29386",
                             {"status": "Open", "assignee": "X"})


class TestPrintSlackTemplate:
    """Test EscalateTool.print_slack_template()."""

    def test_contains_dssd_key(self, caplog: pytest.LogCaptureFixture) -> None:
        tool = _make_tool()
        with caplog.at_level(logging.INFO):
            tool.print_slack_template("DSSD-29386", "Incident Title", "Error desc")
        combined = "\n".join(caplog.messages)
        assert "DSSD-29386" in combined
        assert "Incident Title" in combined
        assert "Error desc" in combined
        assert "@dataops" in combined
        assert "@noc" in combined


class TestRunWorkflow:
    """Test EscalateTool.run() orchestrator."""

    def _setup_full_mocks(self, tool: EscalateTool, drgn_in_refs: bool = True):
        """Set up mocks for a full successful workflow."""
        # Step 1: get_current_user
        tool.pd_client.rget.side_effect = [
            # users/me
            {"user": {"id": "USER1", "email": "noc@example.com"}},
            # incidents/ABC123
            _pd_incident_response(
                drgn_key="DRGN-15087" if drgn_in_refs else None,
            ),
        ]

        # Step 4: fetch_jira_issue (DSSD)
        tool.jira_client.issue.return_value = _jira_issue_mock(
            key="DSSD-29386", status="Open", assignee="John Doe",
            summary="Data export failed",
        )

    def test_full_flow_with_provided_drgn(self):
        tool = _make_tool(dry_run=False)
        self._setup_full_mocks(tool, drgn_in_refs=False)

        tool.run("ABC123", "DSSD-29386", drgn_key="DRGN-15087")

        # Jira link created
        tool.jira_client.create_issue_link.assert_called_once_with(
            type="Blocks",
            inwardIssue="DRGN-15087",
            outwardIssue="DSSD-29386",
        )
        # Transition called
        tool.jira_client.transition_issue.assert_called_once_with(
            "DRGN-15087", "51",
        )
        # PD note posted
        tool.pd_client.rpost.assert_called_once()

    def test_auto_detect_drgn_from_external_refs(self):
        tool = _make_tool(dry_run=False)
        self._setup_full_mocks(tool, drgn_in_refs=True)

        tool.run("ABC123", "DSSD-29386")

        # Should have used DRGN-15087 from external_references
        tool.jira_client.create_issue_link.assert_called_once_with(
            type="Blocks",
            inwardIssue="DRGN-15087",
            outwardIssue="DSSD-29386",
        )

    def test_auto_detect_drgn_from_notes_fallback(self):
        tool = _make_tool(dry_run=False)

        # users/me + incident (no DRGN in refs)
        tool.pd_client.rget.side_effect = [
            {"user": {"id": "USER1", "email": "noc@example.com"}},
            _pd_incident_response(drgn_key=None),
        ]
        # Notes fallback
        tool.pd_client.list_all.return_value = [
            {"content": "Created DRGN-15099 for this"},
        ]
        tool.jira_client.issue.return_value = _jira_issue_mock()

        tool.run("ABC123", "DSSD-29386")

        # Should have used DRGN-15099 from notes
        tool.jira_client.create_issue_link.assert_called_once_with(
            type="Blocks",
            inwardIssue="DRGN-15099",
            outwardIssue="DSSD-29386",
        )

    def test_no_drgn_found_raises(self):
        tool = _make_tool(dry_run=False)

        tool.pd_client.rget.side_effect = [
            {"user": {"id": "USER1", "email": "noc@example.com"}},
            _pd_incident_response(drgn_key=None),
        ]
        tool.pd_client.list_all.return_value = []  # no DRGN in notes either

        with pytest.raises(RuntimeError, match="No DRGN ticket linked"):
            tool.run("ABC123", "DSSD-29386")

    def test_dry_run_no_mutations(self):
        tool = _make_tool(dry_run=True)
        self._setup_full_mocks(tool, drgn_in_refs=True)

        tool.run("ABC123", "DSSD-29386")

        # No mutations should have happened
        tool.jira_client.create_issue_link.assert_not_called()
        tool.jira_client.transition_issue.assert_not_called()
        tool.pd_client.rpost.assert_not_called()


# ===========================================================================
# Missing coverage: import-error fallback (lines 27-30)
# ===========================================================================


class TestImportErrorFallback:
    """Test that the import-error fallback emits logging and exits."""

    def test_missing_dependency_exits(self) -> None:
        """Module exits with code 1 when pagerduty package is unavailable."""
        import builtins
        import importlib
        import pd_escalate

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "pagerduty":
                raise ImportError("No module named 'pagerduty'")
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=fake_import):
            with pytest.raises(SystemExit) as exc_info:
                importlib.reload(pd_escalate)
            assert exc_info.value.code == 1


# ===========================================================================
# Missing coverage: get_current_user KeyError/TypeError (line 103)
# ===========================================================================


class TestGetCurrentUserKeyError:
    """Test EscalateTool.get_current_user() KeyError/TypeError branch."""

    def test_missing_id_key_raises(self) -> None:
        """Dict response missing 'id' key → KeyError → RuntimeError."""
        tool = _make_tool()
        # Return a user dict that has no 'id' — triggers KeyError on user['id']
        tool.pd_client.rget.return_value = {"user": {"email": "noc@example.com"}}
        with pytest.raises(RuntimeError, match="Could not parse user ID"):
            tool.get_current_user()

    def test_none_user_raises(self) -> None:
        """user value is None → TypeError on user['id'] → RuntimeError."""
        tool = _make_tool()
        tool.pd_client.rget.return_value = {"user": None}
        with pytest.raises(RuntimeError, match="Could not parse user ID"):
            tool.get_current_user()


# ===========================================================================
# Missing coverage: fetch_incident non-dict response (line 126)
# ===========================================================================


class TestFetchIncidentNonDict:
    """Test EscalateTool.fetch_incident() when response is not a dict."""

    def test_non_dict_response_used_directly(self) -> None:
        """When rget returns a non-dict, incident is used as-is via else branch."""
        tool = _make_tool()
        # Simulate a list-style response (non-dict): the else branch assigns it directly
        # and subsequent .get() calls must not raise — use a MagicMock to absorb them.
        fake_incident = MagicMock()
        fake_incident.get.side_effect = lambda key, default=None: {
            'priority': None,
            'external_references': [],
            'title': 'Mock Title',
            'status': 'triggered',
            'incident_number': 1,
            'html_url': 'https://pd.example.com/incidents/X',
            'id': 'X',
            'alert_counts': {'all': 0},
        }.get(key, default)
        fake_incident.__getitem__ = lambda self, key: {
            'id': 'X',
            'priority': None,
        }[key]

        tool.pd_client.rget.return_value = fake_incident  # not a dict → else branch
        result = tool.fetch_incident("X")
        # The else: incident = response branch was exercised
        assert result['drgn_key'] is None


# ===========================================================================
# Missing coverage: main() function (lines 403-478)
# ===========================================================================


class TestMain:
    """Tests for the main() CLI entry point."""

    def _env_values(self) -> Dict[str, str]:
        return {
            'PAGERDUTY_API_TOKEN': 'fake-pd-token',
            'JIRA_SERVER_URL': 'https://jira.example.com',
            'JIRA_PERSONAL_ACCESS_TOKEN': 'fake-jira-token',
        }

    @patch("pd_escalate.EscalateTool")
    @patch("pd_escalate.require_env")
    def test_main_basic_run(
        self,
        mock_require_env: MagicMock,
        mock_cls: MagicMock,
    ) -> None:
        """main() creates EscalateTool and calls run() with correct args."""
        from pd_escalate import main

        mock_require_env.return_value = self._env_values()
        mock_instance = MagicMock()
        mock_cls.return_value = mock_instance

        with patch("sys.argv", ["pd_escalate.py", "--pd", "Q33L5GALLQ3ESB", "--dssd", "DSSD-29386"]):
            main()

        mock_cls.assert_called_once_with(
            pagerduty_api_token="fake-pd-token",
            jira_server_url="https://jira.example.com",
            jira_personal_access_token="fake-jira-token",
            dry_run=False,
        )
        mock_instance.run.assert_called_once_with(
            incident_id="Q33L5GALLQ3ESB",
            dssd_key="DSSD-29386",
            drgn_key=None,
        )

    @patch("pd_escalate.EscalateTool")
    @patch("pd_escalate.require_env")
    def test_main_with_drgn_flag(
        self,
        mock_require_env: MagicMock,
        mock_cls: MagicMock,
    ) -> None:
        """--drgn flag is normalized to uppercase and passed to run()."""
        from pd_escalate import main

        mock_require_env.return_value = self._env_values()
        mock_instance = MagicMock()
        mock_cls.return_value = mock_instance

        with patch("sys.argv", [
            "pd_escalate.py", "--pd", "INC123", "--dssd", "dssd-100", "--drgn", "drgn-99",
        ]):
            main()

        mock_instance.run.assert_called_once_with(
            incident_id="INC123",
            dssd_key="DSSD-100",
            drgn_key="DRGN-99",
        )

    @patch("pd_escalate.EscalateTool")
    @patch("pd_escalate.require_env")
    def test_main_dry_run_flag(
        self,
        mock_require_env: MagicMock,
        mock_cls: MagicMock,
    ) -> None:
        """--dry-run flag propagates to EscalateTool constructor."""
        from pd_escalate import main

        mock_require_env.return_value = self._env_values()
        mock_instance = MagicMock()
        mock_cls.return_value = mock_instance

        with patch("sys.argv", [
            "pd_escalate.py", "--pd", "INC123", "--dssd", "DSSD-100", "--dry-run",
        ]):
            main()

        call_kwargs = mock_cls.call_args[1]
        assert call_kwargs["dry_run"] is True

    @patch("pd_escalate.EscalateTool")
    @patch("pd_escalate.require_env")
    def test_main_pd_url_parsed(
        self,
        mock_require_env: MagicMock,
        mock_cls: MagicMock,
    ) -> None:
        """PagerDuty URL is correctly parsed to an incident ID."""
        from pd_escalate import main

        mock_require_env.return_value = self._env_values()
        mock_instance = MagicMock()
        mock_cls.return_value = mock_instance

        pd_url = "https://company.pagerduty.com/incidents/Q33L5GALLQ3ESB"
        with patch("sys.argv", ["pd_escalate.py", "--pd", pd_url, "--dssd", "DSSD-29386"]):
            main()

        mock_instance.run.assert_called_once_with(
            incident_id="Q33L5GALLQ3ESB",
            dssd_key="DSSD-29386",
            drgn_key=None,
        )

    @patch("pd_escalate.EscalateTool")
    @patch("pd_escalate.require_env")
    def test_main_runtime_error_exits_1(
        self,
        mock_require_env: MagicMock,
        mock_cls: MagicMock,
    ) -> None:
        """RuntimeError in tool.run() → sys.exit(1)."""
        from pd_escalate import main

        mock_require_env.return_value = self._env_values()
        mock_instance = MagicMock()
        mock_instance.run.side_effect = RuntimeError("something went wrong")
        mock_cls.return_value = mock_instance

        with patch("sys.argv", ["pd_escalate.py", "--pd", "INC123", "--dssd", "DSSD-100"]):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 1

    @patch("pd_escalate.EscalateTool")
    @patch("pd_escalate.require_env")
    def test_main_keyboard_interrupt_exits_130(
        self,
        mock_require_env: MagicMock,
        mock_cls: MagicMock,
    ) -> None:
        """KeyboardInterrupt during run() → sys.exit(130)."""
        from pd_escalate import main

        mock_require_env.return_value = self._env_values()
        mock_instance = MagicMock()
        mock_instance.run.side_effect = KeyboardInterrupt()
        mock_cls.return_value = mock_instance

        with patch("sys.argv", ["pd_escalate.py", "--pd", "INC123", "--dssd", "DSSD-100"]):
            with pytest.raises(SystemExit) as exc_info:
                main()

        assert exc_info.value.code == 130
