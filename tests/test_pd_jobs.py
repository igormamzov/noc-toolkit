"""Tests for pagerduty-job-extractor (pd_jobs.py)."""

import builtins
import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest

from pd_jobs import PDJobs, extract_incident_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_extractor() -> PDJobs:
    """Create extractor with a mocked PagerDuty client."""
    with patch("noc_utils._pagerduty") as mock_pd:
        mock_pd.RestApiV2Client.return_value = MagicMock()
        extractor = PDJobs(pagerduty_api_token="test-token")
    return extractor


# ===========================================================================
# extract_incident_id
# ===========================================================================

class TestExtractIncidentId:
    def test_plain_id(self):
        assert extract_incident_id("Q1WPEMZKLQZGJF") == "Q1WPEMZKLQZGJF"

    def test_url(self):
        url = "https://yourcompany.pagerduty.com/incidents/Q1WPEMZKLQZGJF"
        assert extract_incident_id(url) == "Q1WPEMZKLQZGJF"

    def test_url_with_trailing_slash(self):
        url = "https://yourcompany.pagerduty.com/incidents/Q1WPEMZKLQZGJF/"
        assert extract_incident_id(url) == "Q1WPEMZKLQZGJF"

    def test_whitespace_stripped(self):
        assert extract_incident_id("  Q1WPEMZKLQZGJF  ") == "Q1WPEMZKLQZGJF"

    def test_url_with_subdomain(self):
        url = "https://other-team.pagerduty.com/incidents/ABC123"
        assert extract_incident_id(url) == "ABC123"


# ===========================================================================
# extract_jobs_from_text
# ===========================================================================

class TestExtractJobsFromText:
    def setup_method(self):
        self.extractor = _make_extractor()

    def test_single_job(self):
        result = self.extractor.extract_jobs_from_text("Failed: jb_load_data")
        assert result == ["jb_load_data"]

    def test_multiple_jobs(self):
        text = "Jobs jb_alpha and jb_beta_gamma failed"
        result = self.extractor.extract_jobs_from_text(text)
        assert result == ["jb_alpha", "jb_beta_gamma"]

    def test_no_jobs(self):
        result = self.extractor.extract_jobs_from_text("No jobs here")
        assert result == []

    def test_empty_string(self):
        result = self.extractor.extract_jobs_from_text("")
        assert result == []

    def test_none_input(self):
        result = self.extractor.extract_jobs_from_text(None)
        assert result == []

    def test_job_with_numbers(self):
        result = self.extractor.extract_jobs_from_text("jb_export_v2_2026")
        assert result == ["jb_export_v2_2026"]

    def test_job_at_word_boundary(self):
        result = self.extractor.extract_jobs_from_text("prefix_jb_foo")
        assert result == []  # jb_ must be at word boundary

    def test_multiple_occurrences_same_job(self):
        text = "jb_load failed, retrying jb_load"
        result = self.extractor.extract_jobs_from_text(text)
        assert result == ["jb_load", "jb_load"]  # findall returns all matches


# ===========================================================================
# extract_jobs_from_dict
# ===========================================================================

class TestExtractJobsFromDict:
    def setup_method(self):
        self.extractor = _make_extractor()

    def test_flat_dict(self):
        data = {"summary": "Failed: jb_export_daily", "status": "triggered"}
        result = self.extractor.extract_jobs_from_dict(data)
        assert result == {"jb_export_daily"}

    def test_nested_dict(self):
        data = {
            "body": {
                "details": {
                    "Description": "Job jb_inner_task failed"
                }
            }
        }
        result = self.extractor.extract_jobs_from_dict(data)
        assert result == {"jb_inner_task"}

    def test_list_of_strings(self):
        data = ["jb_one failed", "jb_two failed"]
        result = self.extractor.extract_jobs_from_dict(data)
        assert result == {"jb_one", "jb_two"}

    def test_list_of_dicts(self):
        data = [
            {"summary": "jb_alpha error"},
            {"summary": "jb_beta error"},
        ]
        result = self.extractor.extract_jobs_from_dict(data)
        assert result == {"jb_alpha", "jb_beta"}

    def test_empty_dict(self):
        result = self.extractor.extract_jobs_from_dict({})
        assert result == set()

    def test_empty_list(self):
        result = self.extractor.extract_jobs_from_dict([])
        assert result == set()

    def test_numeric_values(self):
        data = {"count": 42, "name": "jb_numeric_test"}
        result = self.extractor.extract_jobs_from_dict(data)
        assert result == {"jb_numeric_test"}

    def test_job_in_key(self):
        data = {"jb_key_name": "some value"}
        result = self.extractor.extract_jobs_from_dict(data)
        assert result == {"jb_key_name"}

    def test_deeply_nested(self):
        data = {"a": {"b": {"c": [{"d": "jb_deep"}]}}}
        result = self.extractor.extract_jobs_from_dict(data)
        assert result == {"jb_deep"}

    def test_no_jobs_in_dict(self):
        data = {"status": "triggered", "urgency": "high"}
        result = self.extractor.extract_jobs_from_dict(data)
        assert result == set()


# ===========================================================================
# get_jobs_from_incident
# ===========================================================================

class TestGetJobsFromIncident:
    def setup_method(self):
        self.extractor = _make_extractor()

    def test_jobs_from_incident_data(self):
        """Jobs found in the incident dict itself."""
        self.extractor.pagerduty_session.rget.return_value = {
            "title": "Databricks batch job jb_main_export failed",
            "status": "triggered",
        }
        self.extractor.pagerduty_session.list_all.return_value = iter([])

        result = self.extractor.get_jobs_from_incident("P001")
        assert result == ["jb_main_export"]

    def test_jobs_from_alerts(self):
        """Jobs found in alert bodies."""
        self.extractor.pagerduty_session.rget.return_value = {
            "title": "Something failed",
            "status": "triggered",
        }
        alerts = [
            {"summary": "Alert: jb_alert_job failed"},
            {"body": {"details": {"Description": "jb_another_job error"}}},
        ]
        notes: list = []
        self.extractor.pagerduty_session.list_all.side_effect = [
            iter(alerts),
            iter(notes),
        ]

        result = self.extractor.get_jobs_from_incident("P002")
        assert "jb_alert_job" in result
        assert "jb_another_job" in result

    def test_jobs_from_notes(self):
        """Jobs found in incident notes."""
        self.extractor.pagerduty_session.rget.return_value = {
            "title": "Generic failure",
            "status": "triggered",
        }
        alerts: list = []
        notes = [{"content": "Root cause: jb_note_job timed out"}]
        self.extractor.pagerduty_session.list_all.side_effect = [
            iter(alerts),
            iter(notes),
        ]

        result = self.extractor.get_jobs_from_incident("P003")
        assert result == ["jb_note_job"]

    def test_combined_deduplication(self):
        """Same job in multiple sources is deduplicated and sorted."""
        self.extractor.pagerduty_session.rget.return_value = {
            "title": "jb_common failed",
        }
        alerts = [{"summary": "jb_common and jb_alpha"}]
        notes = [{"content": "jb_common again"}]
        self.extractor.pagerduty_session.list_all.side_effect = [
            iter(alerts),
            iter(notes),
        ]

        result = self.extractor.get_jobs_from_incident("P004")
        assert result == ["jb_alpha", "jb_common"]  # sorted, deduplicated

    def test_no_jobs_found(self):
        """No jb_* patterns in any source."""
        self.extractor.pagerduty_session.rget.return_value = {
            "title": "No job names here",
        }
        self.extractor.pagerduty_session.list_all.return_value = iter([])

        result = self.extractor.get_jobs_from_incident("P005")
        assert result == []

    def test_incident_with_wrapper(self):
        """API returns {incident: {...}} wrapper."""
        self.extractor.pagerduty_session.rget.return_value = {
            "incident": {
                "title": "Job jb_wrapped failed",
                "status": "triggered",
            }
        }
        self.extractor.pagerduty_session.list_all.return_value = iter([])

        result = self.extractor.get_jobs_from_incident("P006")
        assert result == ["jb_wrapped"]

    def test_pd_error_propagates(self):
        """PagerDuty API error propagates to caller."""
        import pagerduty
        self.extractor.pagerduty_session.rget.side_effect = pagerduty.Error("API error")

        with pytest.raises(pagerduty.Error):
            self.extractor.get_jobs_from_incident("P_ERR")

    def test_unexpected_error_propagates(self):
        """Unexpected error propagates to caller."""
        self.extractor.pagerduty_session.rget.side_effect = RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            self.extractor.get_jobs_from_incident("P_BOOM")


# ===========================================================================
# JOB_PATTERN regex edge cases
# ===========================================================================

class TestJobPattern:
    def setup_method(self):
        self.extractor = _make_extractor()

    def test_job_with_only_underscore_prefix(self):
        """jb_ alone (no suffix) should not match."""
        result = self.extractor.extract_jobs_from_text("jb_ is incomplete")
        assert result == []

    def test_job_with_special_chars_after(self):
        """Job name ends at non-word character."""
        result = self.extractor.extract_jobs_from_text("jb_test! done")
        assert result == ["jb_test"]

    def test_job_in_url(self):
        """Job name embedded in URL-like string."""
        result = self.extractor.extract_jobs_from_text(
            "https://example.com/jobs/jb_url_job/status"
        )
        assert result == ["jb_url_job"]


# ===========================================================================
# main() CLI tests
# ===========================================================================

class TestMain:
    @patch("pd_jobs.PDJobs")
    @patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "test-token"})
    def test_successful_run(self, mock_cls, caplog):
        """main() logs jobs at INFO level."""
        import logging
        mock_instance = MagicMock()
        mock_instance.get_jobs_from_incident.return_value = ["jb_one", "jb_two"]
        mock_cls.return_value = mock_instance

        with patch("sys.argv", ["pd_jobs.py", "P123"]):
            from pd_jobs import main
            with caplog.at_level(logging.INFO, logger="pd_jobs"):
                main()

        assert "jb_one" in caplog.text
        assert "jb_two" in caplog.text

    @patch("pd_jobs.PDJobs")
    @patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "test-token"})
    def test_no_jobs_exits(self, mock_cls):
        """main() exits with code 1 when no jobs found."""
        mock_instance = MagicMock()
        mock_instance.get_jobs_from_incident.return_value = []
        mock_cls.return_value = mock_instance

        with patch("sys.argv", ["pd_jobs.py", "P123"]):
            from pd_jobs import main
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    @patch.dict("os.environ", {}, clear=True)
    def test_missing_token_exits(self):
        """main() exits when PAGERDUTY_API_TOKEN is not set."""
        with patch("sys.argv", ["pd_jobs.py", "P123"]):
            from pd_jobs import main
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_no_args_exits(self):
        """main() exits when no incident ID is provided."""
        with patch("sys.argv", ["pd_jobs.py"]):
            with patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "test-token"}):
                from pd_jobs import main
                with pytest.raises(SystemExit) as exc_info:
                    main()
                assert exc_info.value.code == 1

    def test_url_argument(self):
        """main() correctly parses URL argument."""
        with patch("pd_jobs.PDJobs") as mock_cls:
            mock_instance = MagicMock()
            mock_instance.get_jobs_from_incident.return_value = ["jb_test"]
            mock_cls.return_value = mock_instance

            with patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "test-token"}):
                url = "https://co.pagerduty.com/incidents/PXYZ123"
                with patch("sys.argv", ["pd_jobs.py", url]):
                    from pd_jobs import main
                    main()

            mock_instance.get_jobs_from_incident.assert_called_once_with("PXYZ123")

    @patch("pd_jobs.PDJobs")
    @patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "test-token"})
    def test_pagerduty_error_exits(self, mock_cls):
        """main() exits with code 1 when pagerduty.Error is raised."""
        import pagerduty
        mock_instance = MagicMock()
        mock_instance.get_jobs_from_incident.side_effect = pagerduty.Error("API down")
        mock_cls.return_value = mock_instance

        with patch("sys.argv", ["pd_jobs.py", "P123"]):
            from pd_jobs import main
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    @patch("pd_jobs.PDJobs")
    @patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "test-token"})
    def test_unexpected_exception_exits(self, mock_cls):
        """main() exits with code 1 on unexpected exceptions."""
        mock_instance = MagicMock()
        mock_instance.get_jobs_from_incident.side_effect = RuntimeError("unexpected boom")
        mock_cls.return_value = mock_instance

        with patch("sys.argv", ["pd_jobs.py", "P123"]):
            from pd_jobs import main
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1


# ===========================================================================
# Import error fallback (lines 20-24)
# ===========================================================================

class TestImportErrorFallback:
    def test_missing_pagerduty_exits(self):
        """Module exits with code 1 when pagerduty package is unavailable."""
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "pagerduty":
                raise ImportError("No module named 'pagerduty'")
            return real_import(name, *args, **kwargs)

        # Remove cached module so importlib.reload re-executes module-level code
        import pd_jobs
        with patch.object(builtins, "__import__", side_effect=fake_import):
            with pytest.raises(SystemExit) as exc_info:
                importlib.reload(pd_jobs)
            assert exc_info.value.code == 1
