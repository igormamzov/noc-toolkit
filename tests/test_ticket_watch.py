"""Tests for ticket-watch tool."""

import random
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from freezegun import freeze_time

from jira.exceptions import JIRAError

from ticket_watch import TicketWatch, PING_PHRASES, PING_KEYWORDS, VERSION


# ---------------------------------------------------------------------------
# Helpers to build mock Jira objects
# ---------------------------------------------------------------------------

def _make_comment(
    body: str,
    author_name: str = "SomeUser",
    created: str = "2026-03-15T10:00:00.000+0000",
) -> SimpleNamespace:
    """Create a mock Jira comment object."""
    return SimpleNamespace(
        body=body,
        author=SimpleNamespace(displayName=author_name),
        created=created,
    )


def _make_issue(
    key: str = "DSSD-1000",
    summary: str = "Test issue",
    status: str = "Open",
    assignee_name: str | None = None,
    created: str = "2026-03-20T06:00:00.000+0000",
    comments: list | None = None,
) -> SimpleNamespace:
    """Create a mock Jira issue object."""
    assignee = SimpleNamespace(displayName=assignee_name) if assignee_name else None
    comment_obj = SimpleNamespace(comments=comments or [])
    return SimpleNamespace(
        key=key,
        fields=SimpleNamespace(
            summary=summary,
            status=SimpleNamespace(__str__=lambda self: status),
            assignee=assignee,
            created=created,
            comment=comment_obj,
            reporter=SimpleNamespace(displayName="Igor Mamzov"),
        ),
    )


def _make_tool(
    dry_run: bool = True,
    no_comment: bool = False,
    unassigned_hours: float = 4.0,
    stale_days: int = 3,
) -> TicketWatch:
    """Create a TicketWatch instance with mocked Jira client."""
    with patch("noc_utils.JIRA"):
        tool = TicketWatch(
            jira_server_url="https://jira.example.com",
            jira_personal_access_token="fake-token",
            reporters=["Igor Mamzov", "Ilya Klimov"],
            project="DSSD",
            unassigned_hours=unassigned_hours,
            stale_days=stale_days,
            dry_run=dry_run,
            no_comment=no_comment,
        )
    tool.current_user = "NOC Bot"
    return tool


# ---------------------------------------------------------------------------
# Test: _parse_jira_datetime
# ---------------------------------------------------------------------------

class TestParseJiraDatetime:
    """Tests for Jira datetime parsing."""

    def test_standard_format(self) -> None:
        result = TicketWatch._parse_jira_datetime("2026-03-20T08:30:15.123+0000")
        assert result.year == 2026
        assert result.month == 3
        assert result.day == 20
        assert result.hour == 8
        assert result.minute == 30

    def test_colon_in_offset(self) -> None:
        result = TicketWatch._parse_jira_datetime("2026-03-20T08:30:15.123+00:00")
        assert result.year == 2026
        assert result.tzinfo is not None

    def test_z_suffix(self) -> None:
        result = TicketWatch._parse_jira_datetime("2026-03-20T08:30:15.123Z")
        assert result.year == 2026
        assert result.tzinfo is not None

    def test_no_milliseconds(self) -> None:
        result = TicketWatch._parse_jira_datetime("2026-03-20T08:30:15+0000")
        assert result.second == 15

    def test_invalid_format_raises(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse"):
            TicketWatch._parse_jira_datetime("not-a-date")


# ---------------------------------------------------------------------------
# Test: classify_ticket
# ---------------------------------------------------------------------------

class TestClassifyTicket:
    """Tests for ticket classification logic."""

    def test_unassigned_old_ticket(self) -> None:
        """Unassigned ticket created > 4h ago → category 'unassigned'."""
        tool = _make_tool()
        old_time = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime(
            "%Y-%m-%dT%H:%M:%S.000+0000"
        )
        issue = _make_issue(assignee_name=None, created=old_time)
        result = tool.classify_ticket(issue)
        assert result["category"] == "unassigned"
        assert result["assignee"] is None

    def test_unassigned_new_ticket_is_ok(self) -> None:
        """Unassigned ticket created < 4h ago → category 'ok'."""
        tool = _make_tool()
        recent_time = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%S.000+0000"
        )
        issue = _make_issue(assignee_name=None, created=recent_time)
        result = tool.classify_ticket(issue)
        assert result["category"] == "ok"

    def test_assigned_fresh_comment_is_ok(self) -> None:
        """Assigned ticket with recent comment → category 'ok'."""
        tool = _make_tool()
        recent_comment_time = (datetime.now(timezone.utc) - timedelta(hours=12)).strftime(
            "%Y-%m-%dT%H:%M:%S.000+0000"
        )
        created_time = (datetime.now(timezone.utc) - timedelta(days=5)).strftime(
            "%Y-%m-%dT%H:%M:%S.000+0000"
        )
        comments = [_make_comment("Working on it", created=recent_comment_time)]
        issue = _make_issue(
            assignee_name="John Doe",
            created=created_time,
            comments=comments,
        )
        result = tool.classify_ticket(issue)
        assert result["category"] == "ok"
        assert result["last_comment_date"] is not None
        assert result["days_since_comment"] is not None
        assert result["days_since_comment"] < 3.0

    def test_assigned_stale_comment(self) -> None:
        """Assigned ticket with comment > 3d ago → category 'stale'."""
        tool = _make_tool()
        old_comment_time = (datetime.now(timezone.utc) - timedelta(days=5)).strftime(
            "%Y-%m-%dT%H:%M:%S.000+0000"
        )
        created_time = (datetime.now(timezone.utc) - timedelta(days=10)).strftime(
            "%Y-%m-%dT%H:%M:%S.000+0000"
        )
        comments = [_make_comment("Looking into it", author_name="John Doe", created=old_comment_time)]
        issue = _make_issue(
            assignee_name="John Doe",
            created=created_time,
            comments=comments,
        )
        result = tool.classify_ticket(issue)
        assert result["category"] == "stale"
        assert result["assignee"] == "John Doe"
        assert result["last_comment_date"] is not None
        assert result["days_since_comment"] is not None
        assert result["days_since_comment"] >= 5.0

    def test_assigned_no_comments_old_ticket(self) -> None:
        """Assigned ticket with no comments and old enough → category 'stale'."""
        tool = _make_tool()
        old_time = (datetime.now(timezone.utc) - timedelta(days=4)).strftime(
            "%Y-%m-%dT%H:%M:%S.000+0000"
        )
        issue = _make_issue(assignee_name="John Doe", created=old_time, comments=[])
        result = tool.classify_ticket(issue)
        assert result["category"] == "stale"

    def test_assigned_no_comments_recent_ticket(self) -> None:
        """Assigned ticket with no comments but recent → category 'ok'."""
        tool = _make_tool()
        recent_time = (datetime.now(timezone.utc) - timedelta(hours=12)).strftime(
            "%Y-%m-%dT%H:%M:%S.000+0000"
        )
        issue = _make_issue(assignee_name="John Doe", created=recent_time, comments=[])
        result = tool.classify_ticket(issue)
        assert result["category"] == "ok"

    def test_previously_pinged_ticket(self) -> None:
        """Ticket with our previous ping → category 'pinged'."""
        tool = _make_tool()
        old_ping_time = (datetime.now(timezone.utc) - timedelta(days=4)).strftime(
            "%Y-%m-%dT%H:%M:%S.000+0000"
        )
        created_time = (datetime.now(timezone.utc) - timedelta(days=10)).strftime(
            "%Y-%m-%dT%H:%M:%S.000+0000"
        )
        comments = [
            _make_comment(
                "[~John Doe] Could you please provide an update on this ticket?",
                author_name="NOC Bot",
                created=old_ping_time,
            ),
        ]
        issue = _make_issue(
            assignee_name="John Doe",
            created=created_time,
            comments=comments,
        )
        result = tool.classify_ticket(issue)
        assert result["category"] == "pinged"
        assert result["is_repeat_ping"] is True
        assert result["ping_count"] == 1

    def test_pinged_with_assignee_response(self) -> None:
        """Pinged ticket where assignee responded — last response captured."""
        tool = _make_tool()
        created_time = (datetime.now(timezone.utc) - timedelta(days=15)).strftime(
            "%Y-%m-%dT%H:%M:%S.000+0000"
        )
        ping_time = (datetime.now(timezone.utc) - timedelta(days=8)).strftime(
            "%Y-%m-%dT%H:%M:%S.000+0000"
        )
        response_time = (datetime.now(timezone.utc) - timedelta(days=5)).strftime(
            "%Y-%m-%dT%H:%M:%S.000+0000"
        )
        comments = [
            _make_comment(
                "[~John Doe] Could you please provide an update on this ticket?",
                author_name="NOC Bot",
                created=ping_time,
            ),
            _make_comment(
                "Waiting for vendor to provide access credentials for the staging environment.",
                author_name="John Doe",
                created=response_time,
            ),
        ]
        issue = _make_issue(
            assignee_name="John Doe",
            created=created_time,
            comments=comments,
        )
        result = tool.classify_ticket(issue)
        assert result["category"] == "pinged"
        assert result["last_assignee_response"] is not None
        assert "Waiting for vendor" in result["last_assignee_response"]["body"]

    def test_custom_thresholds(self) -> None:
        """Custom unassigned_hours and stale_days thresholds work."""
        tool = _make_tool(unassigned_hours=2.0, stale_days=1)
        # Unassigned, 3h old → should be flagged with 2h threshold
        old_time = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime(
            "%Y-%m-%dT%H:%M:%S.000+0000"
        )
        issue = _make_issue(assignee_name=None, created=old_time)
        result = tool.classify_ticket(issue)
        assert result["category"] == "unassigned"

    def test_unassigned_at_exact_threshold(self) -> None:
        """Ticket created exactly at unassigned_hours threshold → 'unassigned'."""
        tool = _make_tool(unassigned_hours=4.0)
        # Exactly 4h — should be flagged (>=, not >)
        exact_time = (datetime.now(timezone.utc) - timedelta(hours=4, minutes=1)).strftime(
            "%Y-%m-%dT%H:%M:%S.000+0000"
        )
        issue = _make_issue(assignee_name=None, created=exact_time)
        result = tool.classify_ticket(issue)
        assert result["category"] == "unassigned"

    def test_stale_at_exact_threshold_with_comments(self) -> None:
        """Comment at exactly stale_days threshold → 'stale'."""
        tool = _make_tool(stale_days=3)
        exact_comment = (datetime.now(timezone.utc) - timedelta(days=3, minutes=1)).strftime(
            "%Y-%m-%dT%H:%M:%S.000+0000"
        )
        created_time = (datetime.now(timezone.utc) - timedelta(days=10)).strftime(
            "%Y-%m-%dT%H:%M:%S.000+0000"
        )
        comments = [_make_comment("text", author_name="Jane", created=exact_comment)]
        issue = _make_issue(assignee_name="Jane", created=created_time, comments=comments)
        result = tool.classify_ticket(issue)
        assert result["category"] == "stale"

    def test_stale_at_exact_threshold_no_comments(self) -> None:
        """Assigned, no comments, age exactly at stale_days*24h → 'stale'."""
        tool = _make_tool(stale_days=3)
        exact_time = (datetime.now(timezone.utc) - timedelta(days=3, minutes=1)).strftime(
            "%Y-%m-%dT%H:%M:%S.000+0000"
        )
        issue = _make_issue(assignee_name="Jane", created=exact_time, comments=[])
        result = tool.classify_ticket(issue)
        assert result["category"] == "stale"
        assert result["days_since_comment"] is not None

    def test_classify_sets_all_result_fields(self) -> None:
        """Verify all expected fields are present in classify result."""
        tool = _make_tool()
        old_time = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime(
            "%Y-%m-%dT%H:%M:%S.000+0000"
        )
        issue = _make_issue(assignee_name=None, created=old_time)
        result = tool.classify_ticket(issue)
        expected_keys = {
            "key", "summary", "status", "assignee", "created",
            "age_hours", "category", "last_comment_date",
            "days_since_comment", "is_repeat_ping", "ping_count",
            "last_assignee_response",
        }
        assert set(result.keys()) == expected_keys

    def test_ok_ticket_defaults(self) -> None:
        """An 'ok' classified ticket has correct default values for ping fields."""
        tool = _make_tool()
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%S.000+0000"
        )
        issue = _make_issue(assignee_name=None, created=recent)
        result = tool.classify_ticket(issue)
        assert result["category"] == "ok"
        assert result["is_repeat_ping"] is False
        assert result["ping_count"] == 0
        assert result["last_assignee_response"] is None

    @freeze_time("2026-03-20T12:00:00+00:00")
    def test_unassigned_exactly_at_threshold_is_flagged(self) -> None:
        """Ticket age == unassigned_hours → must be flagged (>= not >)."""
        tool = _make_tool(unassigned_hours=4.0)
        # Created exactly 4h ago → age_hours == 4.0 exactly
        issue = _make_issue(assignee_name=None, created="2026-03-20T08:00:00.000+0000")
        result = tool.classify_ticket(issue)
        assert result["category"] == "unassigned"

    @freeze_time("2026-03-20T12:00:00+00:00")
    def test_unassigned_just_under_threshold_is_ok(self) -> None:
        """Ticket age just under threshold → 'ok'."""
        tool = _make_tool(unassigned_hours=4.0)
        # Created 3h59m ago → age_hours < 4.0
        issue = _make_issue(assignee_name=None, created="2026-03-20T08:01:00.000+0000")
        result = tool.classify_ticket(issue)
        assert result["category"] == "ok"

    @freeze_time("2026-03-20T12:00:00+00:00")
    def test_stale_exactly_at_threshold_is_flagged(self) -> None:
        """Comment age == stale_days → must be flagged (>= not >)."""
        tool = _make_tool(stale_days=3)
        # Comment exactly 3 days ago
        comments = [_make_comment("text", author_name="X", created="2026-03-17T12:00:00.000+0000")]
        issue = _make_issue(
            assignee_name="X", created="2026-03-10T12:00:00.000+0000", comments=comments,
        )
        result = tool.classify_ticket(issue)
        assert result["category"] == "stale"
        # days_since_comment == 3.0 exactly
        assert abs(result["days_since_comment"] - 3.0) < 0.001

    @freeze_time("2026-03-20T12:00:00+00:00")
    def test_stale_just_under_threshold_is_ok(self) -> None:
        """Comment age just under stale_days → 'ok'."""
        tool = _make_tool(stale_days=3)
        # Comment 2d23h59m ago → just under 3 days
        comments = [_make_comment("text", author_name="X", created="2026-03-17T12:01:00.000+0000")]
        issue = _make_issue(
            assignee_name="X", created="2026-03-10T12:00:00.000+0000", comments=comments,
        )
        result = tool.classify_ticket(issue)
        assert result["category"] == "ok"

    @freeze_time("2026-03-20T12:00:00+00:00")
    def test_stale_no_comments_exactly_at_threshold(self) -> None:
        """Assigned, no comments, age == stale_days*24h → must be 'stale' (>= not >)."""
        tool = _make_tool(stale_days=3)
        # Created exactly 72h (3 days) ago
        issue = _make_issue(assignee_name="X", created="2026-03-17T12:00:00.000+0000", comments=[])
        result = tool.classify_ticket(issue)
        assert result["category"] == "stale"
        # days_since_comment = 72h / 24 = 3.0 exactly
        assert abs(result["days_since_comment"] - 3.0) < 0.001

    @freeze_time("2026-03-20T12:00:00+00:00")
    def test_stale_no_comments_just_under_threshold(self) -> None:
        """Assigned, no comments, age just under stale_days*24h → 'ok'."""
        tool = _make_tool(stale_days=3)
        # Created 71h59m ago → just under 72h
        issue = _make_issue(assignee_name="X", created="2026-03-17T12:01:00.000+0000", comments=[])
        result = tool.classify_ticket(issue)
        assert result["category"] == "ok"

    @freeze_time("2026-03-20T12:00:00+00:00")
    def test_stale_no_comments_days_since_computed(self) -> None:
        """When no comments, days_since_comment = age_hours / 24."""
        tool = _make_tool(stale_days=3)
        # Created 120h (5 days) ago
        issue = _make_issue(assignee_name="X", created="2026-03-15T12:00:00.000+0000", comments=[])
        result = tool.classify_ticket(issue)
        assert result["category"] == "stale"
        # days_since_comment = 120h / 24 = 5.0 (not 120*24=2880 or 120/25=4.8)
        assert abs(result["days_since_comment"] - 5.0) < 0.001


# ---------------------------------------------------------------------------
# Test: _is_our_ping
# ---------------------------------------------------------------------------

class TestIsOurPing:
    """Tests for ping detection logic."""

    def test_our_ping_detected(self) -> None:
        tool = _make_tool()
        comment = _make_comment(
            "[~John] Could you please provide an update on this ticket?",
            author_name="NOC Bot",
        )
        assert tool._is_our_ping(comment) is True

    def test_other_user_comment_not_ping(self) -> None:
        tool = _make_tool()
        comment = _make_comment(
            "[~John] Could you please provide an update?",
            author_name="Other User",
        )
        assert tool._is_our_ping(comment) is False

    def test_regular_comment_not_ping(self) -> None:
        tool = _make_tool()
        comment = _make_comment(
            "I've fixed the issue, deploying now.",
            author_name="NOC Bot",
        )
        assert tool._is_our_ping(comment) is False

    def test_ping_without_known_user(self) -> None:
        """When current_user is None, match by pattern only."""
        tool = _make_tool()
        tool.current_user = None
        comment = _make_comment(
            "[~Jane] Any updates on this? Please let us know the current status.",
            author_name="Whoever",
        )
        assert tool._is_our_ping(comment) is True

    def test_all_phrases_detectable(self) -> None:
        """Verify all PING_PHRASES contain at least one PING_KEYWORD."""
        for phrase in PING_PHRASES:
            phrase_lower = phrase.lower()
            has_keyword = any(kw in phrase_lower for kw in PING_KEYWORDS)
            assert has_keyword, f"Phrase not detectable: {phrase}"


# ---------------------------------------------------------------------------
# Test: _count_our_pings
# ---------------------------------------------------------------------------

class TestCountOurPings:
    """Tests for counting our ping comments."""

    def test_no_pings(self) -> None:
        tool = _make_tool()
        comments = [_make_comment("Just a normal comment", author_name="Other")]
        assert tool._count_our_pings(comments) == 0

    def test_multiple_pings(self) -> None:
        tool = _make_tool()
        comments = [
            _make_comment("[~John] Could you please provide an update?", author_name="NOC Bot"),
            _make_comment("I'll look into it", author_name="John"),
            _make_comment("[~John] Checking in on this ticket — any updates?", author_name="NOC Bot"),
        ]
        assert tool._count_our_pings(comments) == 2


# ---------------------------------------------------------------------------
# Test: _get_last_assignee_response
# ---------------------------------------------------------------------------

class TestGetLastAssigneeResponse:
    """Tests for finding last assignee response."""

    def test_finds_last_response(self) -> None:
        tool = _make_tool()
        comments = [
            _make_comment("First response", author_name="John Doe", created="2026-03-10T10:00:00.000+0000"),
            _make_comment("[~John Doe] ping", author_name="NOC Bot", created="2026-03-12T10:00:00.000+0000"),
            _make_comment("Second response", author_name="John Doe", created="2026-03-14T10:00:00.000+0000"),
        ]
        result = tool._get_last_assignee_response(comments, "John Doe")
        assert result is not None
        assert result["body"] == "Second response"

    def test_no_assignee_response(self) -> None:
        tool = _make_tool()
        comments = [
            _make_comment("[~John Doe] ping", author_name="NOC Bot"),
        ]
        result = tool._get_last_assignee_response(comments, "John Doe")
        assert result is None

    def test_empty_comments(self) -> None:
        tool = _make_tool()
        result = tool._get_last_assignee_response([], "John Doe")
        assert result is None


# ---------------------------------------------------------------------------
# Test: post_ping
# ---------------------------------------------------------------------------

class TestPostPing:
    """Tests for posting ping comments."""

    def test_dry_run_does_not_post(self, capsys: pytest.CaptureFixture) -> None:
        tool = _make_tool(dry_run=True)
        phrase = tool.post_ping("DSSD-1000", "John Doe")
        assert phrase in PING_PHRASES
        output = capsys.readouterr().out
        assert "[DRY-RUN]" in output
        assert "DSSD-1000" in output

    def test_post_calls_add_comment(self) -> None:
        tool = _make_tool(dry_run=False)
        tool.jira_client = MagicMock()
        phrase = tool.post_ping("DSSD-1000", "John Doe")
        assert phrase in PING_PHRASES
        tool.jira_client.add_comment.assert_called_once()
        call_args = tool.jira_client.add_comment.call_args
        assert call_args[0][0] == "DSSD-1000"
        comment_body = call_args[0][1]
        assert comment_body.startswith("[~John Doe]")
        assert comment_body == f"[~John Doe] {phrase}"

    def test_post_handles_jira_error(self, capsys: pytest.CaptureFixture) -> None:
        tool = _make_tool(dry_run=False)
        tool.jira_client = MagicMock()
        tool.jira_client.add_comment.side_effect = JIRAError("Connection error")
        phrase = tool.post_ping("DSSD-1000", "John Doe")
        assert phrase in PING_PHRASES
        output = capsys.readouterr().out
        assert "Warning" in output


# ---------------------------------------------------------------------------
# Test: search_tickets
# ---------------------------------------------------------------------------

class TestSearchTickets:
    """Tests for JQL search."""

    def test_builds_correct_jql(self) -> None:
        tool = _make_tool()
        tool.jira_client = MagicMock()
        tool.jira_client.search_issues.return_value = []
        tool.search_tickets()

        call_args = tool.jira_client.search_issues.call_args
        jql = call_args[0][0]
        assert "project = DSSD" in jql
        assert "type = Escalation" in jql
        assert '"Igor Mamzov"' in jql
        assert '"Ilya Klimov"' in jql
        assert "status NOT IN (Done, Closed, Resolved)" in jql

    def test_search_jira_error(self) -> None:
        tool = _make_tool()
        tool.jira_client = MagicMock()
        tool.jira_client.search_issues.side_effect = JIRAError("Search failed")
        with pytest.raises(RuntimeError, match="JQL search failed"):
            tool.search_tickets()


# ---------------------------------------------------------------------------
# Test: run (integration)
# ---------------------------------------------------------------------------

class TestRun:
    """Integration tests for the full run workflow."""

    def test_run_with_mixed_tickets(self, capsys: pytest.CaptureFixture) -> None:
        """Full run with unassigned, stale, and pinged tickets."""
        tool = _make_tool(dry_run=True, no_comment=False)

        now = datetime.now(timezone.utc)
        old_created = (now - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        very_old_created = (now - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        old_comment = (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        old_ping = (now - timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")

        issues = [
            # Unassigned
            _make_issue(key="DSSD-100", summary="Unassigned issue", assignee_name=None, created=old_created),
            # Stale
            _make_issue(
                key="DSSD-200",
                summary="Stale issue",
                assignee_name="Jane Smith",
                created=very_old_created,
                comments=[_make_comment("Looking into it", author_name="Jane Smith", created=old_comment)],
            ),
            # Previously pinged
            _make_issue(
                key="DSSD-300",
                summary="Pinged issue",
                assignee_name="Bob Wilson",
                created=very_old_created,
                comments=[
                    _make_comment(
                        "[~Bob Wilson] Could you please provide an update on this ticket?",
                        author_name="NOC Bot",
                        created=old_ping,
                    ),
                ],
            ),
        ]

        tool.jira_client = MagicMock()
        tool.jira_client.search_issues.return_value = issues

        results = tool.run()

        assert len(results) == 3
        categories = [r["category"] for r in results]
        assert "unassigned" in categories
        assert "stale" in categories
        assert "pinged" in categories

        # Verify stale/pinged tickets got ping phrases (dry-run)
        stale_result = [r for r in results if r["category"] == "stale"][0]
        assert "pinged_phrase" in stale_result
        assert stale_result["pinged_phrase"] in PING_PHRASES

        pinged_result = [r for r in results if r["category"] == "pinged"][0]
        assert "pinged_phrase" in pinged_result
        assert pinged_result["pinged_phrase"] in PING_PHRASES

        # Unassigned should NOT have a ping phrase
        unassigned_result = [r for r in results if r["category"] == "unassigned"][0]
        assert "pinged_phrase" not in unassigned_result

        output = capsys.readouterr().out
        assert "UNASSIGNED" in output
        assert "STALE" in output
        assert "REPEAT PING" in output
        assert "DSSD-100" in output
        assert "DSSD-200" in output
        assert "DSSD-300" in output

    def test_run_pings_only_stale_and_pinged(self, capsys: pytest.CaptureFixture) -> None:
        """Verify ping is called only for stale/pinged tickets, not unassigned."""
        tool = _make_tool(dry_run=True, no_comment=False)

        now = datetime.now(timezone.utc)
        old_created = (now - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        very_old_created = (now - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        old_comment = (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")

        issues = [
            _make_issue(key="DSSD-100", assignee_name=None, created=old_created),
            _make_issue(
                key="DSSD-200",
                assignee_name="Jane",
                created=very_old_created,
                comments=[_make_comment("text", author_name="Jane", created=old_comment)],
            ),
        ]
        tool.jira_client = MagicMock()
        tool.jira_client.search_issues.return_value = issues

        results = tool.run()
        output = capsys.readouterr().out

        # Only DSSD-200 should have DRY-RUN ping comment
        assert output.count("[DRY-RUN] Would comment") == 1
        assert "DSSD-200" in output
        # DSSD-100 is unassigned — no ping
        assert "Would comment on DSSD-100" not in output

    def test_run_no_issues(self, capsys: pytest.CaptureFixture) -> None:
        """Run with no matching tickets."""
        tool = _make_tool(dry_run=True)
        tool.jira_client = MagicMock()
        tool.jira_client.search_issues.return_value = []

        results = tool.run()
        assert results == []
        output = capsys.readouterr().out
        assert "No tickets to report" in output

    def test_run_all_ok(self, capsys: pytest.CaptureFixture) -> None:
        """Run where all tickets are in good shape."""
        tool = _make_tool(dry_run=True)
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        recent_comment = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")

        issues = [
            _make_issue(
                key="DSSD-400",
                assignee_name="Alice",
                created=recent,
                comments=[_make_comment("Working on it", author_name="Alice", created=recent_comment)],
            ),
        ]
        tool.jira_client = MagicMock()
        tool.jira_client.search_issues.return_value = issues

        results = tool.run()
        assert results == []
        output = capsys.readouterr().out
        assert "Nothing to report" in output

    def test_run_does_not_ping_unassigned_tickets(self, capsys: pytest.CaptureFixture) -> None:
        """Unassigned tickets must NEVER be pinged — only stale/pinged with assignees."""
        tool = _make_tool(dry_run=False, no_comment=False)
        tool.jira_client = MagicMock()

        now = datetime.now(timezone.utc)
        old = (now - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        issues = [_make_issue(key="DSSD-100", assignee_name=None, created=old)]
        tool.jira_client.search_issues.return_value = issues

        results = tool.run()
        assert len(results) == 1
        assert results[0]["category"] == "unassigned"
        # add_comment should NOT have been called at all
        tool.jira_client.add_comment.assert_not_called()

    def test_run_ok_ticket_with_assignee_not_pinged(self, capsys: pytest.CaptureFixture) -> None:
        """An 'ok' ticket (assigned, fresh) must not be pinged even with assignee."""
        tool = _make_tool(dry_run=False, no_comment=False)
        tool.jira_client = MagicMock()

        now = datetime.now(timezone.utc)
        recent = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        recent_comment = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        issues = [_make_issue(
            key="DSSD-100", assignee_name="Alice", created=recent,
            comments=[_make_comment("Working", author_name="Alice", created=recent_comment)],
        )]
        tool.jira_client.search_issues.return_value = issues

        results = tool.run()
        assert results == []
        tool.jira_client.add_comment.assert_not_called()

    def test_run_no_comment_mode(self, capsys: pytest.CaptureFixture) -> None:
        """Run with --no-comment skips pinging."""
        tool = _make_tool(dry_run=True, no_comment=True)
        now = datetime.now(timezone.utc)
        old_created = (now - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        old_comment = (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")

        issues = [
            _make_issue(
                key="DSSD-500",
                assignee_name="Jane",
                created=old_created,
                comments=[_make_comment("Checking", author_name="Jane", created=old_comment)],
            ),
        ]
        tool.jira_client = MagicMock()
        tool.jira_client.search_issues.return_value = issues

        results = tool.run()
        assert len(results) == 1
        assert results[0]["category"] == "stale"
        # No ping phrase should be set
        assert "pinged_phrase" not in results[0]


# ---------------------------------------------------------------------------
# Test: _format_age / _format_days
# ---------------------------------------------------------------------------

class TestFormatHelpers:
    """Tests for formatting helper methods."""

    def test_format_age_minutes(self) -> None:
        assert TicketWatch._format_age(0.5) == "30m ago"

    def test_format_age_hours(self) -> None:
        assert TicketWatch._format_age(5.0) == "5h ago"

    def test_format_age_days(self) -> None:
        result = TicketWatch._format_age(50.0)
        assert "2d" in result

    def test_format_days_none(self) -> None:
        assert TicketWatch._format_days(None) == "unknown"

    def test_format_days_hours(self) -> None:
        result = TicketWatch._format_days(0.5)
        assert "12h ago" == result

    def test_format_days_days(self) -> None:
        assert TicketWatch._format_days(5.0) == "5 days ago"


# ---------------------------------------------------------------------------
# Test: report output
# ---------------------------------------------------------------------------

class TestReportOutput:
    """Tests for report formatting."""

    def test_report_truncates_long_response(self, capsys: pytest.CaptureFixture) -> None:
        """Assignee response longer than 70 chars gets truncated with '...'."""
        tool = _make_tool(dry_run=True, no_comment=True)
        long_body = "A" * 100
        results = [{
            "key": "DSSD-999",
            "summary": "Test",
            "status": "Open",
            "assignee": "John",
            "created": datetime.now(timezone.utc) - timedelta(days=10),
            "age_hours": 240,
            "category": "pinged",
            "last_comment_date": datetime.now(timezone.utc) - timedelta(days=5),
            "days_since_comment": 5.0,
            "is_repeat_ping": True,
            "ping_count": 1,
            "last_assignee_response": {
                "body": long_body,
                "date": datetime.now(timezone.utc) - timedelta(days=4),
            },
        }]
        tool._print_report(results)
        output = capsys.readouterr().out
        assert "..." in output
        # Body should be truncated to 70 chars + "..."
        assert "A" * 71 not in output

    def test_report_shows_totals(self, capsys: pytest.CaptureFixture) -> None:
        tool = _make_tool(dry_run=True, no_comment=True)
        now = datetime.now(timezone.utc)
        results = [
            {
                "key": "DSSD-1",
                "summary": "Unassigned",
                "status": "Open",
                "assignee": None,
                "created": now - timedelta(hours=6),
                "age_hours": 6,
                "category": "unassigned",
                "last_comment_date": None,
                "days_since_comment": None,
                "is_repeat_ping": False,
                "ping_count": 0,
                "last_assignee_response": None,
            },
            {
                "key": "DSSD-2",
                "summary": "Stale",
                "status": "Open",
                "assignee": "Jane",
                "created": now - timedelta(days=10),
                "age_hours": 240,
                "category": "stale",
                "last_comment_date": now - timedelta(days=5),
                "days_since_comment": 5.0,
                "is_repeat_ping": False,
                "ping_count": 0,
                "last_assignee_response": None,
            },
        ]
        tool._print_report(results)
        output = capsys.readouterr().out
        assert "Total: 2 ticket(s)" in output
        assert "Unassigned: 1" in output
        assert "Stale: 1" in output

    def test_report_all_three_categories(self, capsys: pytest.CaptureFixture) -> None:
        """Report with all three categories shows correct totals."""
        tool = _make_tool(dry_run=True, no_comment=True)
        now = datetime.now(timezone.utc)
        results = [
            {
                "key": "DSSD-1", "summary": "U", "status": "Open", "assignee": None,
                "created": now - timedelta(hours=6), "age_hours": 6, "category": "unassigned",
                "last_comment_date": None, "days_since_comment": None,
                "is_repeat_ping": False, "ping_count": 0, "last_assignee_response": None,
            },
            {
                "key": "DSSD-2", "summary": "S", "status": "Open", "assignee": "Jane",
                "created": now - timedelta(days=10), "age_hours": 240, "category": "stale",
                "last_comment_date": now - timedelta(days=5), "days_since_comment": 5.0,
                "is_repeat_ping": False, "ping_count": 0, "last_assignee_response": None,
            },
            {
                "key": "DSSD-3", "summary": "P", "status": "Open", "assignee": "Bob",
                "created": now - timedelta(days=15), "age_hours": 360, "category": "pinged",
                "last_comment_date": now - timedelta(days=7), "days_since_comment": 7.0,
                "is_repeat_ping": True, "ping_count": 2, "last_assignee_response": None,
            },
        ]
        tool._print_report(results)
        output = capsys.readouterr().out
        assert "Total: 3 ticket(s)" in output
        assert "Unassigned: 1" in output
        assert "Stale: 1" in output
        assert "Repeat ping: 1" in output

    def test_report_shows_jira_url(self, capsys: pytest.CaptureFixture) -> None:
        """Report includes Jira browse URL for each ticket."""
        tool = _make_tool(dry_run=True, no_comment=True)
        now = datetime.now(timezone.utc)
        results = [{
            "key": "DSSD-42", "summary": "Test", "status": "Open", "assignee": None,
            "created": now - timedelta(hours=6), "age_hours": 6, "category": "unassigned",
            "last_comment_date": None, "days_since_comment": None,
            "is_repeat_ping": False, "ping_count": 0, "last_assignee_response": None,
        }]
        tool._print_report(results)
        output = capsys.readouterr().out
        assert "jira.example.com/browse/DSSD-42" in output


# ---------------------------------------------------------------------------
# Test: PING_PHRASES sanity
# ---------------------------------------------------------------------------

class TestPingPhrases:
    """Sanity checks for ping phrases."""

    def test_minimum_phrase_count(self) -> None:
        assert len(PING_PHRASES) >= 10

    def test_all_phrases_unique(self) -> None:
        assert len(PING_PHRASES) == len(set(PING_PHRASES))

    def test_phrases_not_empty(self) -> None:
        for phrase in PING_PHRASES:
            assert len(phrase.strip()) > 10


# ---------------------------------------------------------------------------
# Test: VERSION
# ---------------------------------------------------------------------------

class TestConstructor:
    """Tests for TicketWatch constructor and attribute storage."""

    def test_stores_all_config(self) -> None:
        tool = _make_tool(dry_run=True, no_comment=True, unassigned_hours=2.0, stale_days=5)
        assert tool.dry_run is True
        assert tool.no_comment is True
        assert tool.unassigned_hours == 2.0
        assert tool.stale_days == 5
        assert tool.project == "DSSD"
        assert tool.reporters == ["Igor Mamzov", "Ilya Klimov"]

    def test_jira_base_url(self) -> None:
        tool = _make_tool()
        assert tool.jira_base_url == "https://jira.example.com/browse"

    def test_jira_base_url_strips_trailing_slash(self) -> None:
        with patch("noc_utils.JIRA"):
            tool = TicketWatch(
                jira_server_url="https://jira.example.com/",
                jira_personal_access_token="fake",
                reporters=["Test"],
            )
        assert tool.jira_base_url == "https://jira.example.com/browse"


class TestSearchChickenCurry:
    """Tests for chicken curry mode."""

    def test_chicken_curry_jql(self) -> None:
        tool = _make_tool()
        tool.jira_client = MagicMock()
        tool.jira_client.search_issues.return_value = []
        tool.search_chicken_curry()
        jql = tool.jira_client.search_issues.call_args[0][0]
        assert "Basavaraj.Swamy" in jql
        assert "Epic" in jql
        assert "Done" in jql

    def test_chicken_curry_jql_not_none(self) -> None:
        """JQL must be a string, never None."""
        tool = _make_tool()
        tool.jira_client = MagicMock()
        tool.jira_client.search_issues.return_value = []
        tool.search_chicken_curry()
        jql = tool.jira_client.search_issues.call_args[0][0]
        assert isinstance(jql, str)
        assert len(jql) > 10

    def test_run_chicken_curry_dry_run(self, capsys: pytest.CaptureFixture) -> None:
        tool = _make_tool(dry_run=True)
        tool.jira_client = MagicMock()
        now = datetime.now(timezone.utc)
        old_time = (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        old_comment = (now - timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%S.000+0000")
        issues = [
            _make_issue(
                key="PROJ-100", summary="Test curry", assignee_name="Basavaraj.Swamy",
                created=old_time, comments=[_make_comment("text", created=old_comment)],
            ),
        ]
        tool.jira_client.search_issues.return_value = issues
        tool.run_chicken_curry()
        output = capsys.readouterr().out
        assert "CHICKEN CURRY" in output
        assert "PROJ-100" in output

    def test_run_chicken_curry_no_results(self, capsys: pytest.CaptureFixture) -> None:
        tool = _make_tool(dry_run=True)
        tool.jira_client = MagicMock()
        tool.jira_client.search_issues.return_value = []
        tool.run_chicken_curry()
        output = capsys.readouterr().out
        assert "No stale tickets" in output or "0" in output


class TestMain:
    """Tests for main() function and CLI argument parsing."""

    @patch("ticket_watch.TicketWatch")
    @patch("ticket_watch.load_env")
    def test_main_dry_run(self, mock_dotenv: MagicMock, mock_cls: MagicMock) -> None:
        from ticket_watch import main
        mock_instance = MagicMock()
        mock_instance.run.return_value = []
        mock_cls.return_value = mock_instance

        with patch("os.environ.get") as mock_env, \
             patch("sys.argv", ["ticket_watch.py", "--dry-run"]):
            mock_env.side_effect = lambda key, default="": {
                "JIRA_SERVER_URL": "https://jira.example.com",
                "JIRA_PERSONAL_ACCESS_TOKEN": "token",
                "TICKET_WATCH_REPORTERS": "Test User",
                "TICKET_WATCH_PROJECT": "DSSD",
                "TICKET_WATCH_UNASSIGNED_HOURS": "4",
                "TICKET_WATCH_STALE_DAYS": "3",
            }.get(key, default)
            main()

        # Verify TicketWatch was created with dry_run=True
        call_kwargs = mock_cls.call_args[1]
        assert call_kwargs["dry_run"] is True

    @patch("ticket_watch.load_env")
    def test_main_missing_env_exits(self, mock_dotenv: MagicMock) -> None:
        from ticket_watch import main
        with patch("os.environ.get", return_value=""), \
             patch("sys.argv", ["ticket_watch.py"]):
            with pytest.raises(SystemExit):
                main()

    @patch("ticket_watch.TicketWatch")
    @patch("ticket_watch.load_env")
    def test_main_reporters_parsed(self, mock_dotenv: MagicMock, mock_cls: MagicMock) -> None:
        from ticket_watch import main
        mock_instance = MagicMock()
        mock_instance.run.return_value = []
        mock_cls.return_value = mock_instance

        with patch("os.environ.get") as mock_env, \
             patch("sys.argv", ["ticket_watch.py", "--dry-run"]):
            mock_env.side_effect = lambda key, default="": {
                "JIRA_SERVER_URL": "https://jira.example.com",
                "JIRA_PERSONAL_ACCESS_TOKEN": "token",
                "TICKET_WATCH_REPORTERS": "Alice,Bob,Charlie",
                "TICKET_WATCH_PROJECT": "DSSD",
                "TICKET_WATCH_UNASSIGNED_HOURS": "4",
                "TICKET_WATCH_STALE_DAYS": "3",
            }.get(key, default)
            main()

        call_kwargs = mock_cls.call_args[1]
        assert call_kwargs["reporters"] == ["Alice", "Bob", "Charlie"]


class TestVersion:
    """Tests for version string."""

    def test_version_format(self) -> None:
        parts = VERSION.split(".")
        assert len(parts) == 3
        for part in parts:
            assert part.isdigit()

    def test_version_value(self) -> None:
        assert VERSION == "0.1.0"
