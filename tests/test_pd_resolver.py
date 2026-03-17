"""Tests for pd-resolver tool (PD Resolver v0.1.0)."""

from unittest.mock import MagicMock, patch, PropertyMock
from typing import Any, Dict, List, Optional

import pytest

# Must be importable via conftest.py sys.path setup
from pd_resolver import (
    PDResolver,
    AirflowRun,
    ResolveResult,
    extract_incident_id,
    DRGN_PATTERN,
    DAG_NAME_PATTERN,
    CONSECUTIVE_FAILURES_PATTERN,
    BATCH_DELAYED_PATTERN,
    COMMENT_PRESETS,
    CLOSE_TRANSITION_ID,
    ALERT_CATEGORY_ETL,
    SLA_VIOLATION_YES,
    SLA_VIOLATION_NO,
    SLA_VIOLATION_UNKNOWN,
    RUNBOOK_UP_TO_DATE,
    RUNBOOK_MISSING,
    RESOLUTION_AUTOMATICALLY,
    MWAA_VPCE,
)


# ---------------------------------------------------------------------------
# Helpers: build mock PDResolver without real API clients
# ---------------------------------------------------------------------------

def _make_resolver(dry_run: bool = False, no_confirm: bool = True) -> PDResolver:
    """Create a PDResolver with mocked PD, Jira, and boto3 clients."""
    with patch("pd_resolver.pagerduty.RestApiV2Client"), \
         patch("pd_resolver.JIRA"):
        resolver = PDResolver(
            pagerduty_api_token="fake-pd-token",
            jira_server_url="https://jira.example.com",
            jira_personal_access_token="fake-jira-token",
            jira_email="noc@example.com",
            dry_run=dry_run,
            no_confirm=no_confirm,
        )
    # Replace clients with fresh mocks for explicit control
    resolver.pd_client = MagicMock()
    resolver.jira_client = MagicMock()
    return resolver


def _pd_incident_response(
    incident_id: str = "Q1HR5H5BXCILO3",
    title: str = "[CRITICAL] [AIRFLOW] AirFlow DAG discovery-event has failed consecutively",
    status: str = "triggered",
    incident_number: int = 3057534,
    drgn_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a mock PD incident API response."""
    external_references: List[Dict[str, str]] = []
    if drgn_key:
        external_references.append({"external_id": drgn_key})

    return {
        "incident": {
            "id": incident_id,
            "title": title,
            "status": status,
            "incident_number": incident_number,
            "html_url": f"https://tmtoc.pagerduty.com/incidents/{incident_id}",
            "external_references": external_references,
        }
    }


def _make_airflow_runs(
    count: int = 15, all_success: bool = True, failed_indices: Optional[List[int]] = None,
) -> List[AirflowRun]:
    """Build a list of mock AirflowRun objects."""
    runs = []
    failed_set = set(failed_indices or [])
    for i in range(count):
        state = "failed" if (not all_success and i in failed_set) else "success"
        runs.append(AirflowRun(
            dag_run_id=f"scheduled__2026-03-13T{10+i:02d}:00:00+00:00",
            state=state,
            start_date=f"2026-03-13T{10+i:02d}:00:00+00:00",
            end_date=f"2026-03-13T{10+i:02d}:30:00+00:00",
        ))
    return runs


# ===========================================================================
# Tests: Pure Functions and Patterns
# ===========================================================================


class TestExtractIncidentId:
    """Test extract_incident_id() pure function."""

    def test_plain_id(self):
        assert extract_incident_id("Q1HR5H5BXCILO3") == "Q1HR5H5BXCILO3"

    def test_url(self):
        url = "https://tmtoc.pagerduty.com/incidents/Q1HR5H5BXCILO3"
        assert extract_incident_id(url) == "Q1HR5H5BXCILO3"

    def test_url_with_trailing_slash(self):
        url = "https://tmtoc.pagerduty.com/incidents/Q1HR5H5BXCILO3/"
        assert extract_incident_id(url) == "Q1HR5H5BXCILO3"

    def test_whitespace_stripped(self):
        assert extract_incident_id("  Q1HR5H5BXCILO3  ") == "Q1HR5H5BXCILO3"


class TestDrgnPattern:
    """Test DRGN_PATTERN regex."""

    def test_matches_drgn(self):
        match = DRGN_PATTERN.search("Created DRGN-15254 for this incident")
        assert match and match.group(1) == "DRGN-15254"

    def test_no_match(self):
        assert DRGN_PATTERN.search("No ticket here") is None

    def test_word_boundary(self):
        assert DRGN_PATTERN.search("XDRGN-123") is None

    def test_multiple_matches_returns_first(self):
        match = DRGN_PATTERN.search("DRGN-111 then DRGN-222")
        assert match and match.group(1) == "DRGN-111"


class TestDagNamePattern:
    """Test DAG_NAME_PATTERN regex."""

    def test_consecutive_failures_title(self):
        title = "[CRITICAL] [AIRFLOW] AirFlow DAG discovery-event has failed consecutively"
        match = DAG_NAME_PATTERN.search(title)
        assert match and match.group(1) == "discovery-event"

    def test_batch_delayed_title(self):
        title = "[WARNING] [AIRFLOW] DAG etl_daily_load has failed consecutively"
        match = DAG_NAME_PATTERN.search(title)
        assert match and match.group(1) == "etl_daily_load"

    def test_no_match(self):
        assert DAG_NAME_PATTERN.search("Some other alert") is None

    def test_dag_with_underscores(self):
        title = "DAG my_complex_dag_name has failed"
        match = DAG_NAME_PATTERN.search(title)
        assert match and match.group(1) == "my_complex_dag_name"


class TestAlertPatterns:
    """Test alert classification patterns."""

    def test_consecutive_failures(self):
        assert CONSECUTIVE_FAILURES_PATTERN.search("has failed consecutively")
        assert CONSECUTIVE_FAILURES_PATTERN.search("consecutive_failures_flag")

    def test_batch_delayed(self):
        assert BATCH_DELAYED_PATTERN.search("batch_job_delayed_flag")
        assert BATCH_DELAYED_PATTERN.search("batch job delayed")

    def test_no_match(self):
        assert CONSECUTIVE_FAILURES_PATTERN.search("normal alert") is None
        assert BATCH_DELAYED_PATTERN.search("normal alert") is None


# ===========================================================================
# Tests: Classification and Extraction
# ===========================================================================


class TestClassifyAlert:
    """Test PDResolver.classify_alert() static method."""

    def test_consecutive_failures(self):
        title = "[CRITICAL] [AIRFLOW] AirFlow DAG discovery-event has failed consecutively"
        assert PDResolver.classify_alert(title) == "consecutive_failures"

    def test_batch_delayed(self):
        title = "batch_job_delayed_flag: some_dag"
        assert PDResolver.classify_alert(title) == "batch_delayed"

    def test_unknown(self):
        assert PDResolver.classify_alert("Something else entirely") == "unknown"


class TestExtractDagName:
    """Test PDResolver.extract_dag_name() static method."""

    def test_standard_title(self):
        title = "[CRITICAL] [AIRFLOW] AirFlow DAG discovery-event has failed consecutively"
        assert PDResolver.extract_dag_name(title) == "discovery-event"

    def test_no_dag_name(self):
        assert PDResolver.extract_dag_name("No DAG here") is None

    def test_complex_dag_name(self):
        title = "DAG etl_sfdc_daily_load has failed"
        assert PDResolver.extract_dag_name(title) == "etl_sfdc_daily_load"


class TestEvaluateRecovery:
    """Test PDResolver.evaluate_recovery() static method."""

    def test_all_success(self):
        runs = _make_airflow_runs(15, all_success=True)
        assert PDResolver.evaluate_recovery(runs) is True

    def test_latest_two_success_with_older_failures(self):
        """Oldest runs failed but latest 2 succeeded — recovered."""
        runs = _make_airflow_runs(15, all_success=False, failed_indices=[5, 10])
        assert PDResolver.evaluate_recovery(runs) is True

    def test_latest_run_failed(self):
        """Most recent run failed — not recovered."""
        runs = _make_airflow_runs(15, all_success=False, failed_indices=[0])
        assert PDResolver.evaluate_recovery(runs) is False

    def test_second_latest_run_failed(self):
        """Second most recent run failed — not recovered."""
        runs = _make_airflow_runs(15, all_success=False, failed_indices=[1])
        assert PDResolver.evaluate_recovery(runs) is False

    def test_both_latest_failed(self):
        runs = _make_airflow_runs(15, all_success=False, failed_indices=[0, 1])
        assert PDResolver.evaluate_recovery(runs) is False

    def test_empty_runs(self):
        assert PDResolver.evaluate_recovery([]) is False

    def test_single_run_not_enough(self):
        """One successful run is below min_consecutive=2 threshold."""
        runs = _make_airflow_runs(1, all_success=True)
        assert PDResolver.evaluate_recovery(runs) is False

    def test_single_failure(self):
        runs = _make_airflow_runs(1, all_success=False, failed_indices=[0])
        assert PDResolver.evaluate_recovery(runs) is False

    def test_exactly_two_successes(self):
        """Exactly 2 successful runs — meets the threshold."""
        runs = _make_airflow_runs(2, all_success=True)
        assert PDResolver.evaluate_recovery(runs) is True

    def test_custom_min_consecutive(self):
        """Custom min_consecutive=3 requires 3 latest successes."""
        runs = _make_airflow_runs(5, all_success=False, failed_indices=[3])
        assert PDResolver.evaluate_recovery(runs, min_consecutive=3) is True
        runs_bad = _make_airflow_runs(5, all_success=False, failed_indices=[2])
        assert PDResolver.evaluate_recovery(runs_bad, min_consecutive=3) is False


# ===========================================================================
# Tests: PagerDuty Methods
# ===========================================================================


class TestFetchIncident:
    """Test PDResolver.fetch_incident()."""

    def test_with_drgn_in_external_refs(self):
        resolver = _make_resolver()
        resolver.pd_client.rget.return_value = _pd_incident_response(
            drgn_key="DRGN-15254",
        )
        result = resolver.fetch_incident("Q1HR5H5BXCILO3")
        assert result["id"] == "Q1HR5H5BXCILO3"
        assert result["drgn_key"] == "DRGN-15254"
        assert "discovery-event" in result["title"]

    def test_without_drgn(self):
        resolver = _make_resolver()
        resolver.pd_client.rget.return_value = _pd_incident_response()
        result = resolver.fetch_incident("Q1HR5H5BXCILO3")
        assert result["drgn_key"] is None

    def test_api_error_raises(self):
        resolver = _make_resolver()
        import pagerduty
        resolver.pd_client.rget.side_effect = pagerduty.Error("timeout")
        with pytest.raises(RuntimeError, match="Failed to fetch incident"):
            resolver.fetch_incident("Q1HR5H5BXCILO3")

    def test_flat_response(self):
        """API may return incident dict without 'incident' wrapper."""
        resolver = _make_resolver()
        resolver.pd_client.rget.return_value = {
            "id": "ABC123",
            "title": "Test",
            "status": "triggered",
            "incident_number": 42,
            "html_url": "https://pd.example.com/incidents/ABC123",
            "external_references": [],
        }
        result = resolver.fetch_incident("ABC123")
        assert result["id"] == "ABC123"


class TestFindDrgnFromNotes:
    """Test PDResolver.find_drgn_from_notes()."""

    def test_found_in_notes(self):
        resolver = _make_resolver()
        resolver.pd_client.list_all.return_value = [
            {"content": "Working on it"},
            {"content": "Created DRGN-15254 via Jira automation"},
        ]
        assert resolver.find_drgn_from_notes("Q1HR5H5BXCILO3") == "DRGN-15254"

    def test_not_found(self):
        resolver = _make_resolver()
        resolver.pd_client.list_all.return_value = [
            {"content": "Just a regular note"},
        ]
        assert resolver.find_drgn_from_notes("Q1HR5H5BXCILO3") is None

    def test_empty_notes(self):
        resolver = _make_resolver()
        resolver.pd_client.list_all.return_value = []
        assert resolver.find_drgn_from_notes("Q1HR5H5BXCILO3") is None

    def test_api_error_returns_none(self):
        resolver = _make_resolver()
        import pagerduty
        resolver.pd_client.list_all.side_effect = pagerduty.Error("fail")
        assert resolver.find_drgn_from_notes("Q1HR5H5BXCILO3") is None


class TestResolvePdIncident:
    """Test PDResolver.resolve_pd_incident()."""

    def test_normal_mode(self):
        resolver = _make_resolver(dry_run=False)
        resolver.resolve_pd_incident("Q1HR5H5BXCILO3", "Subsequent runs succeeded")

        # Note should be posted
        note_call = resolver.pd_client.rpost.call_args
        assert "incidents/Q1HR5H5BXCILO3/notes" in note_call[0][0]
        assert note_call[1]["json"]["note"]["content"] == "Subsequent runs succeeded"
        assert note_call[1]["headers"]["From"] == "noc@example.com"

        # Incident should be resolved
        resolve_call = resolver.pd_client.rput.call_args
        incidents = resolve_call[1]["json"]["incidents"]
        assert incidents[0]["id"] == "Q1HR5H5BXCILO3"
        assert incidents[0]["status"] == "resolved"

    def test_dry_run_no_api_calls(self):
        resolver = _make_resolver(dry_run=True)
        resolver.resolve_pd_incident("Q1HR5H5BXCILO3", "test note")
        resolver.pd_client.rpost.assert_not_called()
        resolver.pd_client.rput.assert_not_called()

    def test_note_error_raises(self):
        resolver = _make_resolver(dry_run=False)
        import pagerduty
        resolver.pd_client.rpost.side_effect = pagerduty.Error("fail")
        with pytest.raises(RuntimeError, match="Failed to add PD note"):
            resolver.resolve_pd_incident("Q1HR5H5BXCILO3", "note")

    def test_resolve_error_raises(self):
        resolver = _make_resolver(dry_run=False)
        import pagerduty
        resolver.pd_client.rput.side_effect = pagerduty.Error("fail")
        with pytest.raises(RuntimeError, match="Failed to resolve PD incident"):
            resolver.resolve_pd_incident("Q1HR5H5BXCILO3", "note")


# ===========================================================================
# Tests: Airflow Methods
# ===========================================================================


class TestGetAirflowSession:
    """Test PDResolver.get_airflow_session()."""

    def test_creates_session(self):
        resolver = _make_resolver()
        mock_mwaa_client = MagicMock()
        mock_mwaa_client.create_web_login_token.return_value = {
            "WebToken": "fake-token",
            "WebServerHostname": MWAA_VPCE,
        }
        mock_boto_session = MagicMock()
        mock_boto_session.client.return_value = mock_mwaa_client

        with patch("pd_resolver.boto3.Session", return_value=mock_boto_session), \
             patch("pd_resolver.requests.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session.post.return_value = MagicMock(status_code=200)
            mock_session.post.return_value.raise_for_status = MagicMock()
            mock_session_cls.return_value = mock_session

            session = resolver.get_airflow_session()

            mock_mwaa_client.create_web_login_token.assert_called_once_with(
                Name="prd2612-prod-airflow",
            )
            mock_session.post.assert_called_once()
            assert session is mock_session

    def test_boto_error_raises(self):
        resolver = _make_resolver()
        with patch("pd_resolver.boto3.Session", side_effect=Exception("no creds")):
            with pytest.raises(RuntimeError, match="Failed to create Airflow session"):
                resolver.get_airflow_session()


class TestCheckAirflowRuns:
    """Test PDResolver.check_airflow_runs()."""

    def test_returns_runs(self):
        resolver = _make_resolver()
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "dag_runs": [
                {
                    "dag_run_id": "run_1",
                    "state": "success",
                    "start_date": "2026-03-13T10:00:00+00:00",
                    "end_date": "2026-03-13T10:30:00+00:00",
                },
                {
                    "dag_run_id": "run_2",
                    "state": "failed",
                    "start_date": "2026-03-13T09:00:00+00:00",
                    "end_date": "2026-03-13T09:30:00+00:00",
                },
            ],
        }
        mock_response.raise_for_status = MagicMock()
        mock_session.get.return_value = mock_response

        with patch.object(resolver, "get_airflow_session", return_value=mock_session):
            runs = resolver.check_airflow_runs("discovery-event", limit=2)

        assert len(runs) == 2
        assert runs[0].dag_run_id == "run_1"
        assert runs[0].state == "success"
        assert runs[1].state == "failed"

    def test_dag_not_found_raises(self):
        resolver = _make_resolver()
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.raise_for_status.side_effect = (
            __import__("requests").exceptions.HTTPError(response=mock_response)
        )
        mock_session.get.return_value = mock_response

        with patch.object(resolver, "get_airflow_session", return_value=mock_session):
            with pytest.raises(RuntimeError, match="not found in Airflow"):
                resolver.check_airflow_runs("nonexistent_dag")

    def test_empty_response(self):
        resolver = _make_resolver()
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {"dag_runs": []}
        mock_response.raise_for_status = MagicMock()
        mock_session.get.return_value = mock_response

        with patch.object(resolver, "get_airflow_session", return_value=mock_session):
            runs = resolver.check_airflow_runs("empty_dag")

        assert runs == []


# ===========================================================================
# Tests: DRGN / Jira Methods
# ===========================================================================


class TestGetDrgnStatus:
    """Test PDResolver.get_drgn_status()."""

    def test_returns_status(self):
        resolver = _make_resolver()
        mock_issue = MagicMock()
        mock_issue.fields.status = MagicMock(__str__=lambda self: "Open")
        resolver.jira_client.issue.return_value = mock_issue

        assert resolver.get_drgn_status("DRGN-15254") == "Open"

    def test_jira_error_raises(self):
        resolver = _make_resolver()
        from jira.exceptions import JIRAError
        resolver.jira_client.issue.side_effect = JIRAError("Not found")
        with pytest.raises(RuntimeError, match="Failed to fetch DRGN ticket"):
            resolver.get_drgn_status("DRGN-99999")


class TestCloseDrgn:
    """Test PDResolver.close_drgn()."""

    def test_normal_mode_with_runbook(self):
        resolver = _make_resolver(dry_run=False)
        resolver.close_drgn(
            "DRGN-15254",
            sla_violation_id=SLA_VIOLATION_NO,
            runbook_url="https://confluence.example.com/pages/viewpage.action?pageId=12345",
            comment="Subsequent runs succeeded",
        )

        resolver.jira_client.transition_issue.assert_called_once_with(
            "DRGN-15254",
            CLOSE_TRANSITION_ID,
            customfield_45201={"id": ALERT_CATEGORY_ETL},
            customfield_45202={"id": SLA_VIOLATION_NO},
            customfield_45203={"id": RUNBOOK_UP_TO_DATE},
            customfield_38218="https://confluence.example.com/pages/viewpage.action?pageId=12345",
            resolution={"id": RESOLUTION_AUTOMATICALLY},
            comment="Subsequent runs succeeded",
        )

    def test_normal_mode_no_runbook(self):
        resolver = _make_resolver(dry_run=False)
        resolver.close_drgn(
            "DRGN-15254",
            sla_violation_id=SLA_VIOLATION_UNKNOWN,
            runbook_url=None,
            comment="Job recovered",
        )

        call_kwargs = resolver.jira_client.transition_issue.call_args[1]
        assert call_kwargs["customfield_45203"] == {"id": RUNBOOK_MISSING}
        assert call_kwargs["customfield_38218"] == ""

    def test_dry_run_no_api_call(self):
        resolver = _make_resolver(dry_run=True)
        resolver.close_drgn(
            "DRGN-15254",
            sla_violation_id=SLA_VIOLATION_NO,
            runbook_url="https://example.com",
            comment="test",
        )
        resolver.jira_client.transition_issue.assert_not_called()

    def test_jira_error_raises(self):
        resolver = _make_resolver(dry_run=False)
        from jira.exceptions import JIRAError
        resolver.jira_client.transition_issue.side_effect = JIRAError("bad transition")
        with pytest.raises(RuntimeError, match="Failed to close"):
            resolver.close_drgn("DRGN-15254", SLA_VIOLATION_NO, None, "test")

    def test_sla_violation_yes(self):
        resolver = _make_resolver(dry_run=False)
        resolver.close_drgn("DRGN-100", SLA_VIOLATION_YES, None, "test")
        call_kwargs = resolver.jira_client.transition_issue.call_args[1]
        assert call_kwargs["customfield_45202"] == {"id": SLA_VIOLATION_YES}


# ===========================================================================
# Tests: Confluence / Runbook Search
# ===========================================================================


class TestFindRunbook:
    """Test PDResolver.find_runbook()."""

    def test_found(self):
        resolver = _make_resolver()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "results": [{"id": "370419580", "title": "Runbook - discovery-event"}],
        }

        with patch("pd_resolver.requests.get", return_value=mock_response):
            url = resolver.find_runbook("discovery-event")

        assert url is not None
        assert "370419580" in url

    def test_not_found(self):
        resolver = _make_resolver()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"results": []}

        with patch("pd_resolver.requests.get", return_value=mock_response):
            assert resolver.find_runbook("nonexistent_dag") is None

    def test_api_error_returns_none(self):
        resolver = _make_resolver()
        with patch("pd_resolver.requests.get", side_effect=Exception("connection error")):
            assert resolver.find_runbook("some_dag") is None

    def test_non_200_returns_none(self):
        resolver = _make_resolver()
        mock_response = MagicMock()
        mock_response.status_code = 403

        with patch("pd_resolver.requests.get", return_value=mock_response):
            assert resolver.find_runbook("some_dag") is None


# ===========================================================================
# Tests: Interactive Prompts
# ===========================================================================


class TestPromptSlaViolation:
    """Test PDResolver.prompt_sla_violation()."""

    def test_yes(self):
        with patch("builtins.input", return_value="1"):
            assert PDResolver.prompt_sla_violation() == SLA_VIOLATION_YES

    def test_no(self):
        with patch("builtins.input", return_value="2"):
            assert PDResolver.prompt_sla_violation() == SLA_VIOLATION_NO

    def test_unknown(self):
        with patch("builtins.input", return_value="3"):
            assert PDResolver.prompt_sla_violation() == SLA_VIOLATION_UNKNOWN

    def test_invalid_then_valid(self):
        with patch("builtins.input", side_effect=["x", "4", "2"]):
            assert PDResolver.prompt_sla_violation() == SLA_VIOLATION_NO


class TestPromptComment:
    """Test PDResolver.prompt_comment()."""

    def test_preset_1(self):
        with patch("builtins.input", return_value="1"):
            assert PDResolver.prompt_comment() == COMMENT_PRESETS[0]

    def test_preset_4(self):
        with patch("builtins.input", return_value="4"):
            assert PDResolver.prompt_comment() == COMMENT_PRESETS[3]

    def test_custom(self):
        with patch("builtins.input", side_effect=["5", "My custom comment"]):
            assert PDResolver.prompt_comment() == "My custom comment"

    def test_custom_empty_then_valid(self):
        with patch("builtins.input", side_effect=["5", "", "5", "Valid comment"]):
            assert PDResolver.prompt_comment() == "Valid comment"

    def test_invalid_then_valid(self):
        with patch("builtins.input", side_effect=["99", "1"]):
            assert PDResolver.prompt_comment() == COMMENT_PRESETS[0]


class TestPromptDrgnKey:
    """Test PDResolver.prompt_drgn_key()."""

    def test_valid_drgn(self):
        with patch("builtins.input", return_value="DRGN-15254"):
            assert PDResolver.prompt_drgn_key() == "DRGN-15254"

    def test_lowercase_converted(self):
        with patch("builtins.input", return_value="drgn-15254"):
            assert PDResolver.prompt_drgn_key() == "DRGN-15254"

    def test_empty_returns_none(self):
        with patch("builtins.input", return_value=""):
            assert PDResolver.prompt_drgn_key() is None

    def test_invalid_prefix_returns_none(self):
        with patch("builtins.input", return_value="DSSD-123"):
            assert PDResolver.prompt_drgn_key() is None


# ===========================================================================
# Tests: ResolveResult Dataclass
# ===========================================================================


class TestResolveResult:
    """Test ResolveResult dataclass."""

    def test_default_errors(self):
        result = ResolveResult(
            incident_id="ABC",
            incident_title="Test",
            dag_name="test_dag",
            alert_type="unknown",
            runs_checked=0,
            recent_successes=0,
            recovered=False,
            drgn_key=None,
            runbook_url=None,
            drgn_closed=False,
            pd_resolved=False,
        )
        assert result.errors == []

    def test_with_errors(self):
        result = ResolveResult(
            incident_id="ABC",
            incident_title="Test",
            dag_name="test_dag",
            alert_type="unknown",
            runs_checked=15,
            recent_successes=15,
            recovered=True,
            drgn_key="DRGN-100",
            runbook_url=None,
            drgn_closed=False,
            pd_resolved=False,
            errors=["Jira timeout"],
        )
        assert len(result.errors) == 1


# ===========================================================================
# Tests: AirflowRun Dataclass
# ===========================================================================


class TestAirflowRun:
    """Test AirflowRun dataclass."""

    def test_creation(self):
        run = AirflowRun(
            dag_run_id="run_1",
            state="success",
            start_date="2026-03-13T10:00:00",
            end_date="2026-03-13T10:30:00",
        )
        assert run.state == "success"
        assert run.end_date == "2026-03-13T10:30:00"

    def test_none_end_date(self):
        run = AirflowRun(
            dag_run_id="run_2",
            state="running",
            start_date="2026-03-13T10:00:00",
            end_date=None,
        )
        assert run.end_date is None


# ===========================================================================
# Tests: Resolve Workflow (Integration)
# ===========================================================================


class TestResolveWorkflow:
    """Test PDResolver.resolve() orchestrator."""

    def _setup_full_mocks(
        self,
        resolver: PDResolver,
        drgn_in_refs: bool = False,
        all_runs_success: bool = True,
        drgn_status: str = "Open",
        runbook_found: bool = True,
    ):
        """Set up mocks for the full workflow."""
        # fetch_incident
        resolver.pd_client.rget.return_value = _pd_incident_response(
            drgn_key="DRGN-15254" if drgn_in_refs else None,
        )

        # find_drgn_from_notes (fallback)
        if not drgn_in_refs:
            resolver.pd_client.list_all.return_value = [
                {"content": "Created DRGN-15254 via Jira automation"},
            ]

        # check_airflow_runs
        runs = _make_airflow_runs(
            15,
            all_success=all_runs_success,
            failed_indices=[] if all_runs_success else [0, 1],
        )
        mock_session = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "dag_runs": [
                {
                    "dag_run_id": r.dag_run_id,
                    "state": r.state,
                    "start_date": r.start_date,
                    "end_date": r.end_date,
                }
                for r in runs
            ],
        }
        mock_response.raise_for_status = MagicMock()
        mock_session.get.return_value = mock_response

        # get_drgn_status
        mock_issue = MagicMock()
        mock_issue.fields.status = MagicMock(__str__=lambda self: drgn_status)
        resolver.jira_client.issue.return_value = mock_issue

        return mock_session

    def test_full_flow_success(self):
        resolver = _make_resolver(dry_run=False, no_confirm=True)
        mock_session = self._setup_full_mocks(
            resolver, drgn_in_refs=True, all_runs_success=True,
        )

        with patch.object(resolver, "get_airflow_session", return_value=mock_session), \
             patch.object(resolver, "find_runbook", return_value="https://confluence.example.com/page/123"):
            result = resolver.resolve("Q1HR5H5BXCILO3")

        assert result.recovered is True
        assert result.drgn_closed is True
        assert result.pd_resolved is True
        assert result.dag_name == "discovery-event"
        assert result.errors == []
        resolver.jira_client.transition_issue.assert_called_once()

    def test_not_recovered_skips_closure(self):
        resolver = _make_resolver(dry_run=False, no_confirm=True)
        mock_session = self._setup_full_mocks(
            resolver, drgn_in_refs=True, all_runs_success=False,
        )

        with patch.object(resolver, "get_airflow_session", return_value=mock_session):
            result = resolver.resolve("Q1HR5H5BXCILO3")

        assert result.recovered is False
        assert result.drgn_closed is False
        assert result.pd_resolved is False
        resolver.jira_client.transition_issue.assert_not_called()
        resolver.pd_client.rput.assert_not_called()

    def test_drgn_already_closed_skips(self):
        resolver = _make_resolver(dry_run=False, no_confirm=True)
        mock_session = self._setup_full_mocks(
            resolver, drgn_in_refs=True, drgn_status="Closed",
        )

        with patch.object(resolver, "get_airflow_session", return_value=mock_session), \
             patch.object(resolver, "find_runbook", return_value=None):
            result = resolver.resolve("Q1HR5H5BXCILO3")

        # DRGN should be marked as already closed, no transition call
        assert result.drgn_closed is True
        resolver.jira_client.transition_issue.assert_not_called()
        # PD should still be resolved
        assert result.pd_resolved is True

    def test_dry_run_no_mutations(self):
        resolver = _make_resolver(dry_run=True, no_confirm=True)
        mock_session = self._setup_full_mocks(
            resolver, drgn_in_refs=True,
        )

        with patch.object(resolver, "get_airflow_session", return_value=mock_session), \
             patch.object(resolver, "find_runbook", return_value="https://example.com/runbook"):
            result = resolver.resolve("Q1HR5H5BXCILO3")

        # No mutations should happen in dry-run
        resolver.jira_client.transition_issue.assert_not_called()
        resolver.pd_client.rput.assert_not_called()
        # But the result should still show intent
        assert result.recovered is True

    def test_drgn_from_notes_fallback(self):
        resolver = _make_resolver(dry_run=False, no_confirm=True)
        mock_session = self._setup_full_mocks(
            resolver, drgn_in_refs=False,
        )

        with patch.object(resolver, "get_airflow_session", return_value=mock_session), \
             patch.object(resolver, "find_runbook", return_value=None):
            result = resolver.resolve("Q1HR5H5BXCILO3")

        assert result.drgn_key == "DRGN-15254"
        assert result.drgn_closed is True

    def test_no_drgn_found_prompts_user(self):
        resolver = _make_resolver(dry_run=False, no_confirm=True)
        mock_session = self._setup_full_mocks(
            resolver, drgn_in_refs=False,
        )
        # Override notes to return no DRGN
        resolver.pd_client.list_all.return_value = []

        with patch.object(resolver, "get_airflow_session", return_value=mock_session), \
             patch.object(resolver, "find_runbook", return_value=None), \
             patch.object(PDResolver, "prompt_drgn_key", return_value=None):
            result = resolver.resolve("Q1HR5H5BXCILO3")

        assert result.drgn_key is None
        assert result.drgn_closed is False
        # PD should still be resolved even without DRGN
        assert result.pd_resolved is True

    def test_user_aborts_at_confirm(self):
        resolver = _make_resolver(dry_run=False, no_confirm=False)
        mock_session = self._setup_full_mocks(
            resolver, drgn_in_refs=True,
        )

        with patch.object(resolver, "get_airflow_session", return_value=mock_session), \
             patch.object(resolver, "find_runbook", return_value="https://example.com/runbook"), \
             patch.object(PDResolver, "prompt_sla_violation", return_value=SLA_VIOLATION_NO), \
             patch.object(PDResolver, "prompt_comment", return_value="test comment"), \
             patch("builtins.input", return_value="n"):
            result = resolver.resolve("Q1HR5H5BXCILO3")

        assert result.drgn_closed is False
        assert result.pd_resolved is False
        resolver.jira_client.transition_issue.assert_not_called()

    def test_jira_error_during_close_adds_error(self):
        resolver = _make_resolver(dry_run=False, no_confirm=True)
        mock_session = self._setup_full_mocks(
            resolver, drgn_in_refs=True,
        )
        from jira.exceptions import JIRAError
        resolver.jira_client.transition_issue.side_effect = JIRAError("bad transition")

        with patch.object(resolver, "get_airflow_session", return_value=mock_session), \
             patch.object(resolver, "find_runbook", return_value=None):
            result = resolver.resolve("Q1HR5H5BXCILO3")

        assert result.drgn_closed is False
        assert len(result.errors) >= 1
        assert any("Failed to close" in e for e in result.errors)

    def test_runbook_missing_sets_status(self):
        resolver = _make_resolver(dry_run=False, no_confirm=True)
        mock_session = self._setup_full_mocks(
            resolver, drgn_in_refs=True,
        )

        with patch.object(resolver, "get_airflow_session", return_value=mock_session), \
             patch.object(resolver, "find_runbook", return_value=None):
            result = resolver.resolve("Q1HR5H5BXCILO3")

        # Verify transition was called with RUNBOOK_MISSING
        call_kwargs = resolver.jira_client.transition_issue.call_args[1]
        assert call_kwargs["customfield_45203"] == {"id": RUNBOOK_MISSING}
        assert call_kwargs["customfield_38218"] == ""


# ===========================================================================
# Tests: Constants
# ===========================================================================


class TestConstants:
    """Test module-level constants are correct."""

    def test_close_transition_id(self):
        assert CLOSE_TRANSITION_ID == "61"

    def test_sla_violation_ids(self):
        assert SLA_VIOLATION_YES == "64527"
        assert SLA_VIOLATION_NO == "64528"
        assert SLA_VIOLATION_UNKNOWN == "64529"

    def test_runbook_status_ids(self):
        assert RUNBOOK_UP_TO_DATE == "64530"
        assert RUNBOOK_MISSING == "64532"

    def test_resolution_id(self):
        assert RESOLUTION_AUTOMATICALLY == "12901"

    def test_alert_category_etl(self):
        assert ALERT_CATEGORY_ETL == "64520"

    def test_comment_presets_count(self):
        assert len(COMMENT_PRESETS) == 4

    def test_mwaa_vpce(self):
        assert "airflow.amazonaws.com" in MWAA_VPCE
