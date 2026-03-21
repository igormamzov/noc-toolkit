"""Tests for PagerDuty Incident Merge Tool (pd-merge)."""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest

from pd_merge import (
    ALERT_TYPE_LABELS,
    PD_BASE_URL,
    MergeGroup,
    MergeResult,
    PagerDutyMergeTool,
    ParsedIncident,
    _parse_iso_dt,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tool(dry_run: bool = True, verbose: bool = False) -> PagerDutyMergeTool:
    """Create a PagerDutyMergeTool with a mocked PD client."""
    with patch("noc_utils._pagerduty") as mock_pd:
        mock_pd.RestApiV2Client.return_value = MagicMock()
        tool = PagerDutyMergeTool(
            pagerduty_api_token="test-token",
            dry_run=dry_run,
            verbose=verbose,
        )
    return tool


def _make_incident(
    incident_id: str = "P001",
    title: str = "Databricks batch job jb_test_job failed",
    status: str = "triggered",
    created_at: str = "2026-03-10T10:00:00Z",
    alert_type: str = "databricks",
    alert_priority: int = 1,
    normalized_job_name: str = "jb_test_job",
    raw_job_name: str = "jb_test_job",
    jira_tickets: Optional[List[str]] = None,
    real_notes: Optional[List[str]] = None,
    notes_fetched: bool = False,
) -> ParsedIncident:
    """Build a ParsedIncident with sensible defaults."""
    return ParsedIncident(
        incident_id=incident_id,
        title=title,
        status=status,
        created_at=created_at,
        html_url=f"{PD_BASE_URL}/{incident_id}",
        alert_type=alert_type,
        alert_priority=alert_priority,
        normalized_job_name=normalized_job_name,
        raw_job_name=raw_job_name,
        jira_tickets=jira_tickets or [],
        real_notes=real_notes or [],
        notes_fetched=notes_fetched,
    )


# ===========================================================================
# _parse_iso_dt helper
# ===========================================================================

class TestParseIsoDt:
    def test_with_z_suffix(self):
        dt = _parse_iso_dt("2026-03-10T10:00:00Z")
        assert dt.year == 2026
        assert dt.month == 3
        assert dt.day == 10
        assert dt.tzinfo is not None

    def test_with_offset(self):
        dt = _parse_iso_dt("2026-03-10T10:00:00+00:00")
        assert dt.hour == 10

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            _parse_iso_dt("not-a-date")


# ===========================================================================
# Title parsing — _strip_prefix
# ===========================================================================

class TestStripPrefix:
    def setup_method(self):
        self.tool = _make_tool()

    def test_no_prefix(self):
        title, tickets = self.tool._strip_prefix("Databricks batch job jb_x failed")
        assert title == "Databricks batch job jb_x failed"
        assert tickets == []

    def test_single_dssd_prefix(self):
        title, tickets = self.tool._strip_prefix("DSSD-29001 Databricks batch job jb_x failed")
        assert "DSSD-29001" in tickets
        assert "DSSD-29001" not in title

    def test_stacked_prefixes(self):
        title, tickets = self.tool._strip_prefix("DSSD-100 DRGN-200 Databricks batch job jb_x failed")
        assert "DSSD-100" in tickets
        assert "DRGN-200" in tickets

    def test_marker_words_stripped(self):
        title, _ = self.tool._strip_prefix("Monitoring Databricks batch job jb_x failed")
        assert title.startswith("Databricks")

    def test_disabled_ignore_stripped(self):
        title, _ = self.tool._strip_prefix("Disabled. Ignore Databricks batch job jb_x failed")
        assert title.startswith("Databricks")

    def test_bracket_wrappers_stripped(self):
        title, _ = self.tool._strip_prefix("[ERROR] [DATABRICKS] batch job jb_x failed")
        assert not title.startswith("[")


# ===========================================================================
# Title parsing — parse_incident_title
# ===========================================================================

class TestParseIncidentTitle:
    def setup_method(self):
        self.tool = _make_tool()

    def test_databricks_pattern(self):
        name, atype, prio, raw, tickets = self.tool.parse_incident_title(
            "Databricks batch job jb_edw_sales_001 failed"
        )
        assert name == "jb_edw_sales_001"
        assert atype == "databricks"
        assert prio == 1

    def test_monitor_pattern(self):
        name, atype, prio, raw, _ = self.tool.parse_incident_title(
            "Monitor job 'jb_edw_sales_001_run_prod' failed"
        )
        assert name == "jb_edw_sales_001"
        assert atype == "monitor"
        assert prio == 2
        assert raw == "jb_edw_sales_001_run_prod"

    def test_monitor_airflow_prod_suffix(self):
        name, atype, _, raw, _ = self.tool.parse_incident_title(
            "Monitor job 'jb_edw_sales_airflow_prod' failed"
        )
        assert name == "jb_edw_sales"

    def test_airflow_consec_pattern(self):
        name, atype, prio, _, _ = self.tool.parse_incident_title(
            "Airflow DAG jb_edw_load has failed consecutively"
        )
        assert name == "jb_edw_load"
        assert atype == "airflow_consec"
        assert prio == 3

    def test_airflow_time_pattern(self):
        name, atype, prio, _, _ = self.tool.parse_incident_title(
            "Airflow DAG jb_edw_load exceeded expected run time"
        )
        assert name == "jb_edw_load"
        assert atype == "airflow_time"
        assert prio == 3

    def test_consequential_pattern(self):
        name, atype, prio, _, _ = self.tool.parse_incident_title(
            "Data delayed for downstream consumers"
        )
        assert atype == "consequential"
        assert prio == 99

    def test_step_not_started_consequential(self):
        _, atype, _, _, _ = self.tool.parse_incident_title(
            "Step not started on time for pipeline"
        )
        assert atype == "consequential"

    def test_unknown_pattern(self):
        _, atype, prio, _, _ = self.tool.parse_incident_title(
            "Something completely different happened"
        )
        assert atype == "unknown"
        assert prio == 99

    def test_jira_ticket_extracted_from_prefix(self):
        _, _, _, _, tickets = self.tool.parse_incident_title(
            "DSSD-29001 Databricks batch job jb_x failed"
        )
        assert "DSSD-29001" in tickets

    def test_jira_ticket_extracted_from_body(self):
        _, _, _, _, tickets = self.tool.parse_incident_title(
            "Databricks batch job jb_x failed (see DRGN-12345)"
        )
        assert "DRGN-12345" in tickets

    def test_multiple_tickets_deduped(self):
        _, _, _, _, tickets = self.tool.parse_incident_title(
            "DSSD-100 Databricks batch job jb_x failed DSSD-100"
        )
        assert tickets.count("DSSD-100") == 1


# ===========================================================================
# Note classification
# ===========================================================================

class TestClassifyNote:
    def test_working_on_it_is_ignore(self):
        assert PagerDutyMergeTool._classify_note("working on it") == "ignore"

    def test_working_on_it_case_insensitive(self):
        assert PagerDutyMergeTool._classify_note("Working On It now") == "ignore"

    def test_disabled_ignore_is_ignore(self):
        assert PagerDutyMergeTool._classify_note("Disabled. Ignore") == "ignore"

    def test_snooze_with_ticket_is_context(self):
        assert PagerDutyMergeTool._classify_note(
            "DSSD-29001 - Open - Unassigned. Snooze"
        ) == "context"

    def test_real_note(self):
        assert PagerDutyMergeTool._classify_note(
            "Restarted the job, monitoring now"
        ) == "real"

    def test_empty_note_is_real(self):
        assert PagerDutyMergeTool._classify_note("") == "real"


# ===========================================================================
# Static helpers
# ===========================================================================

class TestIsAssignedToUser:
    def test_assigned(self):
        incident = {
            "assignments": [{"assignee": {"id": "U001"}}]
        }
        assert PagerDutyMergeTool._is_assigned_to_user(incident, "U001") is True

    def test_not_assigned(self):
        incident = {
            "assignments": [{"assignee": {"id": "U002"}}]
        }
        assert PagerDutyMergeTool._is_assigned_to_user(incident, "U001") is False

    def test_empty_assignments(self):
        incident = {"assignments": []}
        assert PagerDutyMergeTool._is_assigned_to_user(incident, "U001") is False

    def test_missing_assignments_key(self):
        incident = {}
        assert PagerDutyMergeTool._is_assigned_to_user(incident, "U001") is False


class TestAllSameDay:
    def test_same_day(self):
        incidents = [
            _make_incident(created_at="2026-03-10T08:00:00Z"),
            _make_incident(created_at="2026-03-10T22:00:00Z"),
        ]
        assert PagerDutyMergeTool._all_same_day(incidents) is True

    def test_different_days(self):
        incidents = [
            _make_incident(created_at="2026-03-10T08:00:00Z"),
            _make_incident(created_at="2026-03-11T08:00:00Z"),
        ]
        assert PagerDutyMergeTool._all_same_day(incidents) is False

    def test_empty_list(self):
        assert PagerDutyMergeTool._all_same_day([]) is True

    def test_invalid_date(self):
        incidents = [_make_incident(created_at="invalid")]
        assert PagerDutyMergeTool._all_same_day(incidents) is False


class TestFormatDate:
    def test_valid_iso(self):
        assert PagerDutyMergeTool._format_date("2026-03-10T08:00:00Z") == "03-10"

    def test_invalid(self):
        assert PagerDutyMergeTool._format_date("bad") == "??-??"


class TestFormatTime:
    @patch("pd_merge._parse_iso_dt")
    @patch("pd_merge.datetime")
    def test_recent(self, mock_dt, mock_parse):
        mock_parse.return_value = datetime(2026, 3, 10, 10, 0, tzinfo=timezone.utc)
        mock_dt.now.return_value = datetime(2026, 3, 10, 12, 30, tzinfo=timezone.utc)
        result = PagerDutyMergeTool._format_time("2026-03-10T10:00:00Z")
        assert result == "00:02:30"

    def test_invalid(self):
        assert PagerDutyMergeTool._format_time("bad") == "??:??:??"


class TestMakeRow:
    def test_basic(self):
        result = PagerDutyMergeTool._make_row(["A", "BC"], [5, 5])
        assert result == "| A     | BC    |"


class TestMakeSeparator:
    def test_basic(self):
        result = PagerDutyMergeTool._make_separator([5, 5])
        assert result == "+-------+-------+"


# ===========================================================================
# Enrich incident
# ===========================================================================

class TestEnrichIncident:
    def setup_method(self):
        self.tool = _make_tool()

    def test_databricks_incident(self):
        raw = {
            "id": "P001",
            "title": "Databricks batch job jb_test_job failed",
            "status": "triggered",
            "created_at": "2026-03-10T10:00:00Z",
            "html_url": "https://pd.example.com/P001",
        }
        inc = self.tool.enrich_incident(raw)
        assert inc.incident_id == "P001"
        assert inc.normalized_job_name == "jb_test_job"
        assert inc.alert_type == "databricks"
        assert inc.alert_priority == 1

    def test_fallback_html_url(self):
        raw = {"id": "P002", "title": "test", "status": "triggered", "created_at": ""}
        inc = self.tool.enrich_incident(raw)
        assert inc.html_url == f"{PD_BASE_URL}/P002"


# ===========================================================================
# Grouping
# ===========================================================================

class TestGroupIncidents:
    def setup_method(self):
        self.tool = _make_tool()

    def test_groups_by_name(self):
        incidents = [
            _make_incident(incident_id="P001", normalized_job_name="jb_a"),
            _make_incident(incident_id="P002", normalized_job_name="jb_a"),
            _make_incident(incident_id="P003", normalized_job_name="jb_b"),
        ]
        groups = self.tool.group_incidents(incidents)
        assert "jb_a" in groups
        assert len(groups["jb_a"]) == 2
        assert "jb_b" not in groups  # only 1 incident

    def test_excludes_unknown(self):
        incidents = [
            _make_incident(incident_id="P001", normalized_job_name="x", alert_type="unknown"),
            _make_incident(incident_id="P002", normalized_job_name="x", alert_type="unknown"),
        ]
        groups = self.tool.group_incidents(incidents)
        assert len(groups) == 0

    def test_excludes_consequential(self):
        incidents = [
            _make_incident(incident_id="P001", normalized_job_name="x", alert_type="consequential"),
            _make_incident(incident_id="P002", normalized_job_name="x", alert_type="consequential"),
        ]
        groups = self.tool.group_incidents(incidents)
        assert len(groups) == 0


# ===========================================================================
# Classify group (Scenario A vs B)
# ===========================================================================

class TestClassifyGroup:
    def setup_method(self):
        self.tool = _make_tool()

    def test_same_day_scenario_a(self):
        incidents = [
            _make_incident(incident_id="P001", created_at="2026-03-10T08:00:00Z"),
            _make_incident(incident_id="P002", created_at="2026-03-10T14:00:00Z"),
        ]
        group = self.tool.classify_group("jb_test", incidents, None)
        assert group.scenario == "A"
        assert group.skip_reason is None

    def test_cross_date_no_tickets_skipped(self):
        incidents = [
            _make_incident(incident_id="P001", created_at="2026-03-09T08:00:00Z"),
            _make_incident(incident_id="P002", created_at="2026-03-10T08:00:00Z"),
        ]
        group = self.tool.classify_group("jb_test", incidents, None)
        assert group.skip_reason is not None
        assert "cannot validate" in group.skip_reason.lower()

    def test_cross_date_with_tickets_scenario_b(self):
        incidents = [
            _make_incident(incident_id="P001", created_at="2026-03-09T08:00:00Z", jira_tickets=["DSSD-100"]),
            _make_incident(incident_id="P002", created_at="2026-03-10T08:00:00Z"),
        ]
        # No jira_client → skip
        group = self.tool.classify_group("jb_test", incidents, None)
        assert group.scenario == "B"
        assert group.skip_reason is not None
        assert "Jira not configured" in group.skip_reason


# ===========================================================================
# Target selection
# ===========================================================================

class TestSelectTarget:
    def setup_method(self):
        self.tool = _make_tool()

    def test_single_with_real_notes_becomes_target(self):
        inc1 = _make_incident(incident_id="P001", real_notes=["Restarted"])
        inc2 = _make_incident(incident_id="P002")
        group = MergeGroup(group_key="test", incidents=[inc1, inc2])
        self.tool.select_target(group)
        assert group.target.incident_id == "P001"
        assert len(group.sources) == 1
        assert group.sources[0].incident_id == "P002"

    def test_no_notes_lowest_priority_wins(self):
        inc1 = _make_incident(incident_id="P001", alert_priority=2)
        inc2 = _make_incident(incident_id="P002", alert_priority=1)
        group = MergeGroup(group_key="test", incidents=[inc1, inc2])
        self.tool.select_target(group)
        assert group.target.incident_id == "P002"

    def test_same_priority_earliest_wins(self):
        inc1 = _make_incident(incident_id="P001", alert_priority=1, created_at="2026-03-10T12:00:00Z")
        inc2 = _make_incident(incident_id="P002", alert_priority=1, created_at="2026-03-10T08:00:00Z")
        group = MergeGroup(group_key="test", incidents=[inc1, inc2])
        self.tool.select_target(group)
        assert group.target.incident_id == "P002"

    def test_multiple_with_notes_uses_priority_tiebreak(self):
        inc1 = _make_incident(incident_id="P001", alert_priority=2, real_notes=["note"])
        inc2 = _make_incident(incident_id="P002", alert_priority=1, real_notes=["note"])
        group = MergeGroup(group_key="test", incidents=[inc1, inc2])
        self.tool.select_target(group)
        assert group.target.incident_id == "P002"


# ===========================================================================
# Find mass failure incident
# ===========================================================================

class TestFindMassFailureIncident:
    def setup_method(self):
        self.tool = _make_tool()

    def test_finds_mass_failure(self):
        inc = _make_incident(
            incident_id="PMASS",
            title="DSSD-100 Multiple batch jobs failing",
            jira_tickets=["DSSD-100"],
        )
        result = self.tool._find_mass_failure_incident([inc])
        assert result is not None
        assert result.incident_id == "PMASS"

    def test_returns_none_when_no_match(self):
        inc = _make_incident(title="Databricks batch job jb_x failed")
        result = self.tool._find_mass_failure_incident([inc])
        assert result is None

    def test_requires_jira_ticket(self):
        inc = _make_incident(
            title="Multiple batch jobs failing",
            jira_tickets=[],
        )
        result = self.tool._find_mass_failure_incident([inc])
        assert result is None

    def test_returns_most_recent(self):
        inc1 = _make_incident(
            incident_id="P1",
            title="DSSD-100 Multiple jobs failing",
            jira_tickets=["DSSD-100"],
            created_at="2026-03-09T10:00:00Z",
        )
        inc2 = _make_incident(
            incident_id="P2",
            title="DSSD-200 Multiple databricks batch jobs failing",
            jira_tickets=["DSSD-200"],
            created_at="2026-03-10T10:00:00Z",
        )
        result = self.tool._find_mass_failure_incident([inc1, inc2])
        assert result.incident_id == "P2"


# ===========================================================================
# Merge incident (dry run)
# ===========================================================================

class TestMergeIncidentDryRun:
    def setup_method(self):
        self.tool = _make_tool(dry_run=True)

    def test_dry_run_returns_success(self):
        result = self.tool.merge_incident("PTGT", "PSRC")
        assert result.success is True
        assert result.target_id == "PTGT"
        assert result.source_id == "PSRC"
        assert "DRY RUN" in result.error_message

    def test_execute_group_merge_dry_run(self, capsys):
        target = _make_incident(incident_id="PTGT")
        source = _make_incident(incident_id="PSRC")
        group = MergeGroup(
            group_key="test",
            incidents=[target, source],
            target=target,
            sources=[source],
        )
        results = self.tool.execute_group_merge(group)
        assert len(results) == 1
        assert results[0].success is True

    def test_execute_group_merge_no_target(self):
        group = MergeGroup(group_key="test", incidents=[])
        results = self.tool.execute_group_merge(group)
        assert results == []


# ===========================================================================
# Skip persistence
# ===========================================================================

class TestSkipPersistence:
    def test_load_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pd_merge.SKIP_FILE", tmp_path / "nonexistent.json")
        result = PagerDutyMergeTool.load_skipped_ids()
        assert result == set()

    def test_save_and_load(self, tmp_path, monkeypatch):
        skip_file = tmp_path / "skips.json"
        monkeypatch.setattr("pd_merge.SKIP_FILE", skip_file)

        ids = {"P001", "P002"}
        PagerDutyMergeTool.save_skipped_ids(ids)

        loaded = PagerDutyMergeTool.load_skipped_ids()
        assert loaded == ids

    def test_load_corrupt_file(self, tmp_path, monkeypatch):
        skip_file = tmp_path / "skips.json"
        skip_file.write_text("not json", encoding="utf-8")
        monkeypatch.setattr("pd_merge.SKIP_FILE", skip_file)

        result = PagerDutyMergeTool.load_skipped_ids()
        assert result == set()

    def test_save_creates_sorted_json(self, tmp_path, monkeypatch):
        skip_file = tmp_path / "skips.json"
        monkeypatch.setattr("pd_merge.SKIP_FILE", skip_file)

        PagerDutyMergeTool.save_skipped_ids({"P003", "P001", "P002"})
        data = json.loads(skip_file.read_text(encoding="utf-8"))
        assert data["skipped_incident_ids"] == ["P001", "P002", "P003"]
        assert "updated_at" in data


# ===========================================================================
# RDS Export detection
# ===========================================================================

class TestRdsExportDetection:
    def setup_method(self):
        self.tool = _make_tool()

    def test_rds_export_title_detected(self):
        title, _ = self.tool._strip_prefix("RDS export jb_rds_job is failed more than 30 minutes")
        from pd_merge import RDS_EXPORT_RE
        assert RDS_EXPORT_RE.match(title) is not None

    def test_rds_exports_plural_detected(self):
        title, _ = self.tool._strip_prefix("RDS exports - failed to start")
        from pd_merge import RDS_EXPORT_RE, RDS_FAILED_TO_START_RE
        assert RDS_EXPORT_RE.match(title) is not None
        assert RDS_FAILED_TO_START_RE.search(title) is not None


# ===========================================================================
# Regex patterns
# ===========================================================================

class TestRegexPatterns:
    def test_monitor_suffix_run_prod(self):
        from pd_merge import MONITOR_SUFFIX_RE
        assert MONITOR_SUFFIX_RE.sub("", "jb_test_run_prod") == "jb_test"

    def test_monitor_suffix_airflow_prod(self):
        from pd_merge import MONITOR_SUFFIX_RE
        assert MONITOR_SUFFIX_RE.sub("", "jb_test_airflow_prod") == "jb_test"

    def test_monitor_suffix_prod(self):
        from pd_merge import MONITOR_SUFFIX_RE
        assert MONITOR_SUFFIX_RE.sub("", "jb_test_prod") == "jb_test"

    def test_jira_ticket_re(self):
        from pd_merge import JIRA_TICKET_RE
        matches = JIRA_TICKET_RE.findall("DSSD-100 and DRGN-200 and FCR-300")
        assert set(m.upper() for m in matches) == {"DSSD-100", "DRGN-200", "FCR-300"}

    def test_mass_failure_re(self):
        from pd_merge import MASS_FAILURE_RE
        assert MASS_FAILURE_RE.search("Multiple batch jobs failing") is not None
        assert MASS_FAILURE_RE.search("Multiple databricks batch jobs failing") is not None
        assert MASS_FAILURE_RE.search("Single job failed") is None
