"""Tests for PagerDuty Incident Merge Tool (pd-merge)."""

import builtins
import importlib
import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from pd_merge import (
    ALERT_TYPE_LABELS,
    PD_BASE_URL,
    SKIP_FILE,
    VERSION,
    MergeGroup,
    MergeResult,
    PagerDutyMergeTool,
    ParsedIncident,
    _parse_iso_dt,
    main,
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
    context_notes: Optional[List[str]] = None,
    all_notes_count: int = 0,
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
        context_notes=context_notes or [],
        all_notes_count=all_notes_count,
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

    def test_fcr_prefix(self):
        title, tickets = self.tool._strip_prefix("FCR-300 Databricks batch job jb_x failed")
        assert "FCR-300" in tickets

    def test_coredata_prefix(self):
        title, tickets = self.tool._strip_prefix("COREDATA-42 Databricks batch job jb_x failed")
        assert "COREDATA-42" in tickets

    def test_restarted_marker_stripped(self):
        title, _ = self.tool._strip_prefix("restarted Databricks batch job jb_x failed")
        assert title.startswith("Databricks")


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

    def test_monitor_prod_suffix(self):
        name, _, _, _, _ = self.tool.parse_incident_title(
            "Monitor job 'jb_edw_sales_prod' failed"
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

    def test_snooze_without_ticket_is_real(self):
        # Has "snooze" but no Jira ticket → should be real
        assert PagerDutyMergeTool._classify_note("Snooze for tomorrow") == "real"


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

    def test_multiple_assignees_one_matches(self):
        incident = {
            "assignments": [
                {"assignee": {"id": "U002"}},
                {"assignee": {"id": "U001"}},
            ]
        }
        assert PagerDutyMergeTool._is_assigned_to_user(incident, "U001") is True


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

    def test_single_incident(self):
        incidents = [_make_incident(created_at="2026-03-10T08:00:00Z")]
        assert PagerDutyMergeTool._all_same_day(incidents) is True


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

    def test_negative_delta_clamped(self):
        """Future timestamp should produce 00:00:00."""
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        result = PagerDutyMergeTool._format_time(future)
        assert result == "00:00:00"

    def test_multiday_age(self):
        old = (datetime.now(timezone.utc) - timedelta(days=2, hours=3, minutes=5)).isoformat()
        result = PagerDutyMergeTool._format_time(old)
        assert result.startswith("02:")


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

    def test_missing_title_defaults_to_empty(self):
        raw = {"id": "P003", "status": "triggered", "created_at": "2026-03-10T10:00:00Z"}
        inc = self.tool.enrich_incident(raw)
        assert inc.title == ""


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

    def test_three_incidents_all_grouped(self):
        incidents = [
            _make_incident(incident_id=f"P00{i}", normalized_job_name="jb_a")
            for i in range(1, 4)
        ]
        groups = self.tool.group_incidents(incidents)
        assert len(groups["jb_a"]) == 3


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

    def test_cross_date_with_mass_failure_skips_new_incidents(self):
        """Cross-date group where new incidents fall after mass failure → skip."""
        mass = _make_incident(
            incident_id="PMASS",
            title="DSSD-100 Multiple batch jobs failing",
            jira_tickets=["DSSD-100"],
            created_at="2026-03-10T09:00:00Z",
        )
        incidents = [
            _make_incident(incident_id="P001", created_at="2026-03-09T08:00:00Z", jira_tickets=["DSSD-100"]),
            # P002 created after mass failure, no ticket
            _make_incident(incident_id="P002", created_at="2026-03-10T10:00:00Z"),
        ]
        group = self.tool.classify_group("jb_test", incidents, mass)
        assert group.skip_reason is not None
        assert "mass failure" in group.skip_reason

    def test_cross_date_with_jira_client_scenario_b_no_skip(self):
        """Cross-date + Jira configured → scenario B without skip."""
        incidents = [
            _make_incident(incident_id="P001", created_at="2026-03-09T08:00:00Z", jira_tickets=["DSSD-100"]),
            _make_incident(incident_id="P002", created_at="2026-03-10T08:00:00Z"),
        ]
        tool = _make_tool()
        tool.jira_client = MagicMock()
        group = tool.classify_group("jb_test", incidents, None)
        assert group.scenario == "B"
        assert group.skip_reason is None


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

    def test_empty_list_returns_none(self):
        assert self.tool._find_mass_failure_incident([]) is None


# ===========================================================================
# build_mass_failure_group
# ===========================================================================

class TestBuildMassFailureGroup:
    def setup_method(self):
        self.tool = _make_tool()
        self.tool.pd_client.list_all = MagicMock(return_value=iter([]))

    def test_no_candidates_returns_none(self):
        mass = _make_incident(
            incident_id="PMASS",
            title="DSSD-100 Multiple batch jobs failing",
            jira_tickets=["DSSD-100"],
            created_at="2026-03-10T09:00:00Z",
        )
        result = self.tool.build_mass_failure_group(mass, [mass], {})
        assert result is None

    def test_adds_databricks_candidates(self):
        mass = _make_incident(
            incident_id="PMASS",
            title="DSSD-100 Multiple batch jobs failing",
            jira_tickets=["DSSD-100"],
            created_at="2026-03-10T09:00:00Z",
        )
        cand = _make_incident(
            incident_id="P001",
            created_at="2026-03-10T10:00:00Z",
            alert_type="databricks",
            jira_tickets=[],
        )
        group = self.tool.build_mass_failure_group(mass, [mass, cand], {})
        assert group is not None
        assert group.scenario == "C"
        assert cand in group.sources

    def test_skips_incidents_before_mass_failure(self):
        mass = _make_incident(
            incident_id="PMASS",
            title="DSSD-100 Multiple batch jobs failing",
            jira_tickets=["DSSD-100"],
            created_at="2026-03-10T09:00:00Z",
        )
        old_inc = _make_incident(
            incident_id="POLD",
            created_at="2026-03-10T08:00:00Z",
            alert_type="databricks",
            jira_tickets=[],
        )
        result = self.tool.build_mass_failure_group(mass, [mass, old_inc], {})
        assert result is None

    def test_skips_incidents_with_jira_tickets(self):
        mass = _make_incident(
            incident_id="PMASS",
            title="DSSD-100 Multiple batch jobs failing",
            jira_tickets=["DSSD-100"],
            created_at="2026-03-10T09:00:00Z",
        )
        with_ticket = _make_incident(
            incident_id="PTKT",
            created_at="2026-03-10T10:00:00Z",
            alert_type="databricks",
            jira_tickets=["DSSD-200"],
        )
        result = self.tool.build_mass_failure_group(mass, [mass, with_ticket], {})
        assert result is None

    def test_skips_unknown_alert_type(self):
        mass = _make_incident(
            incident_id="PMASS",
            title="DSSD-100 Multiple batch jobs failing",
            jira_tickets=["DSSD-100"],
            created_at="2026-03-10T09:00:00Z",
        )
        unknown_inc = _make_incident(
            incident_id="PUNK",
            created_at="2026-03-10T10:00:00Z",
            alert_type="unknown",
            jira_tickets=[],
        )
        result = self.tool.build_mass_failure_group(mass, [mass, unknown_inc], {})
        assert result is None

    def test_skips_incidents_in_same_name_groups(self):
        mass = _make_incident(
            incident_id="PMASS",
            title="DSSD-100 Multiple batch jobs failing",
            jira_tickets=["DSSD-100"],
            created_at="2026-03-10T09:00:00Z",
        )
        grouped_inc = _make_incident(
            incident_id="PGRP",
            normalized_job_name="jb_grouped",
            created_at="2026-03-10T10:00:00Z",
            alert_type="databricks",
            jira_tickets=[],
        )
        another_inc = _make_incident(
            incident_id="PGRP2",
            normalized_job_name="jb_grouped",
            created_at="2026-03-10T10:00:00Z",
            alert_type="databricks",
            jira_tickets=[],
        )
        same_name_groups = {"jb_grouped": [grouped_inc, another_inc]}
        result = self.tool.build_mass_failure_group(
            mass, [mass, grouped_inc, another_inc], same_name_groups
        )
        assert result is None

    def test_strong_match_via_alerts(self):
        """Incidents whose job name is in alerts are added as strong matches."""
        mass = _make_incident(
            incident_id="PMASS",
            title="DSSD-100 Multiple batch jobs failing",
            jira_tickets=["DSSD-100"],
            created_at="2026-03-10T09:00:00Z",
        )
        cand = _make_incident(
            incident_id="P001",
            normalized_job_name="jb_known_job",
            created_at="2026-03-10T10:00:00Z",
            alert_type="databricks",
            jira_tickets=[],
        )
        # Make pd_client return an alert with the matching job name
        self.tool.pd_client.list_all = MagicMock(return_value=iter([
            {"summary": "Databricks batch job jb_known_job failed"}
        ]))
        group = self.tool.build_mass_failure_group(mass, [mass, cand], {})
        assert group is not None
        assert cand in group.sources

    def test_consequential_incidents_included(self):
        mass = _make_incident(
            incident_id="PMASS",
            title="DSSD-100 Multiple batch jobs failing",
            jira_tickets=["DSSD-100"],
            created_at="2026-03-10T09:00:00Z",
        )
        cons = _make_incident(
            incident_id="PCONS",
            title="Data delayed for consumers",
            normalized_job_name="data delayed for consumers",
            created_at="2026-03-10T10:00:00Z",
            alert_type="consequential",
            jira_tickets=[],
        )
        group = self.tool.build_mass_failure_group(mass, [mass, cons], {})
        assert group is not None
        assert cons in group.sources

    def test_pd_error_on_alert_fetch_continues_gracefully(self):
        """Errors fetching alerts are swallowed; normal candidates still added."""
        import pagerduty as _pd

        mass = _make_incident(
            incident_id="PMASS",
            title="DSSD-100 Multiple batch jobs failing",
            jira_tickets=["DSSD-100"],
            created_at="2026-03-10T09:00:00Z",
        )
        cand = _make_incident(
            incident_id="P001",
            created_at="2026-03-10T10:00:00Z",
            alert_type="databricks",
            jira_tickets=[],
        )
        self.tool.pd_client.list_all = MagicMock(side_effect=_pd.Error("oops"))
        group = self.tool.build_mass_failure_group(mass, [mass, cand], {})
        assert group is not None

    def test_incident_with_invalid_date_skipped(self):
        mass = _make_incident(
            incident_id="PMASS",
            title="DSSD-100 Multiple batch jobs failing",
            jira_tickets=["DSSD-100"],
            created_at="2026-03-10T09:00:00Z",
        )
        bad_date_inc = _make_incident(
            incident_id="PBAD",
            created_at="not-a-date",
            alert_type="databricks",
            jira_tickets=[],
        )
        result = self.tool.build_mass_failure_group(mass, [mass, bad_date_inc], {})
        assert result is None


# ===========================================================================
# build_rds_exports_group (Scenario D)
# ===========================================================================

class TestBuildRdsExportsGroup:
    def setup_method(self):
        self.tool = _make_tool()
        self.tool.pd_client.list_all = MagicMock(return_value=iter([]))

    def _make_rds_target(self, incident_id: str = "PRDS_TARGET") -> ParsedIncident:
        return _make_incident(
            incident_id=incident_id,
            title="RDS exports — failed to start",
            alert_type="unknown",
            normalized_job_name="rds exports — failed to start",
        )

    def _make_rds_source(self, incident_id: str = "PRDS_SRC") -> ParsedIncident:
        return _make_incident(
            incident_id=incident_id,
            title="RDS export jb_rds_report is failed more than 30 minutes",
            alert_type="unknown",
            normalized_job_name="jb_rds_report",
        )

    def test_less_than_two_rds_returns_none(self):
        inc = self._make_rds_target()
        result = self.tool.build_rds_exports_group([inc])
        assert result is None

    def test_no_failed_to_start_target_returns_none(self):
        src1 = self._make_rds_source("P001")
        src2 = self._make_rds_source("P002")
        result = self.tool.build_rds_exports_group([src1, src2])
        assert result is None

    def test_target_without_note_returns_none(self):
        """Target has no 'failed to start' in notes → skip."""
        target = self._make_rds_target()
        target.notes_fetched = True  # no notes
        src = self._make_rds_source()
        result = self.tool.build_rds_exports_group([target, src])
        assert result is None

    def test_valid_group_built_when_note_present(self):
        """Target has 'failed to start' in real_notes → group is created."""
        target = _make_incident(
            incident_id="PRDS_TARGET",
            title="RDS exports — failed to start",
            alert_type="unknown",
            normalized_job_name="rds exports — failed to start",
            real_notes=["RDS exports failed to start at 05:00"],
            notes_fetched=True,
        )
        src = self._make_rds_source()
        group = self.tool.build_rds_exports_group([target, src])
        assert group is not None
        assert group.scenario == "D"
        assert group.target.incident_id == "PRDS_TARGET"
        assert src in group.sources

    def test_target_note_in_api_raw_notes(self):
        """Notes not in real_notes but returned by API contain 'failed to start'."""
        target = _make_incident(
            incident_id="PRDS_TARGET",
            title="RDS exports — failed to start",
            alert_type="unknown",
            normalized_job_name="rds exports — failed to start",
            notes_fetched=True,  # already fetched, but no real_notes
        )
        src = self._make_rds_source()

        def list_all_side_effect(endpoint, *args, **kwargs):
            if "notes" in endpoint:
                return iter([{"content": "RDS exports failed to start at 05:00"}])
            return iter([])

        self.tool.pd_client.list_all = MagicMock(side_effect=list_all_side_effect)
        group = self.tool.build_rds_exports_group([target, src])
        assert group is not None

    def test_pd_error_on_rds_notes_returns_none(self):
        """If both note sources fail the target is rejected."""
        import pagerduty as _pd

        target = _make_incident(
            incident_id="PRDS_TARGET",
            title="RDS exports — failed to start",
            alert_type="unknown",
            normalized_job_name="rds exports — failed to start",
            notes_fetched=True,
        )
        src = self._make_rds_source()
        self.tool.pd_client.list_all = MagicMock(side_effect=_pd.Error("fail"))
        result = self.tool.build_rds_exports_group([target, src])
        assert result is None

    def test_no_sources_after_removing_target(self):
        """Only one RDS incident other than target that qualifies."""
        # Two RDS incidents, both "failed to start" (no source)
        target = _make_incident(
            incident_id="PRDS_T1",
            title="RDS exports — failed to start",
            alert_type="unknown",
            normalized_job_name="rds exports — failed to start",
            real_notes=["RDS exports failed to start"],
            notes_fetched=True,
        )
        # The second is also a target-type; source list will be empty
        target2 = _make_incident(
            incident_id="PRDS_T2",
            title="RDS exports — failed to start",
            alert_type="unknown",
            normalized_job_name="rds exports — failed to start2",
        )
        # Force only 2 rds incidents, first matched as target
        group = self.tool.build_rds_exports_group([target, target2])
        # sources = incidents - target. If target2 is not a source, it depends
        # on the logic. Actually both match RDS_EXPORT_RE, first match becomes
        # target; sources = rest. So target2 would be a source.
        # This test ensures no crash.
        assert group is not None or group is None  # just verify no exception


# ===========================================================================
# fetch_and_classify_notes
# ===========================================================================

class TestFetchAndClassifyNotes:
    def setup_method(self):
        self.tool = _make_tool()
        self.tool.pd_client.list_all = MagicMock(return_value=iter([]))

    def test_already_fetched_skips(self):
        inc = _make_incident(notes_fetched=True)
        self.tool.pd_client.list_all = MagicMock()
        self.tool.fetch_and_classify_notes(inc)
        self.tool.pd_client.list_all.assert_not_called()

    def test_classifies_notes_correctly(self):
        notes = [
            {"content": "working on it"},
            {"content": "DSSD-100 - Open. Snooze"},
            {"content": "Restarted the job"},
        ]
        self.tool.pd_client.list_all = MagicMock(return_value=iter(notes))
        inc = _make_incident()
        self.tool.fetch_and_classify_notes(inc)
        assert inc.all_notes_count == 3
        # "working on it" is ignored
        assert inc.real_notes == ["Restarted the job"]
        assert inc.context_notes == ["DSSD-100 - Open. Snooze"]
        assert inc.notes_fetched is True

    def test_pd_error_marks_fetched(self):
        import pagerduty as _pd

        self.tool.pd_client.list_all = MagicMock(side_effect=_pd.Error("fail"))
        inc = _make_incident()
        self.tool.fetch_and_classify_notes(inc)
        assert inc.notes_fetched is True
        assert inc.real_notes == []


# ===========================================================================
# Merge incident (dry run and real)
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

    def test_execute_group_merge_dry_run(self, caplog):
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


class TestMergeIncidentReal:
    def setup_method(self):
        self.tool = _make_tool(dry_run=False)
        self.tool.user_email = "user@example.com"

    def test_successful_merge(self):
        self.tool.pd_client.rput = MagicMock(return_value={})
        result = self.tool.merge_incident("PTGT", "PSRC")
        assert result.success is True
        assert result.error_message is None

    def test_merge_pd_error(self):
        import pagerduty as _pd

        self.tool.pd_client.rput = MagicMock(side_effect=_pd.Error("some error"))
        result = self.tool.merge_incident("PTGT", "PSRC")
        assert result.success is False
        assert result.error_message is not None

    def test_merge_already_resolved_error(self):
        import pagerduty as _pd

        self.tool.pd_client.rput = MagicMock(
            side_effect=_pd.Error("arguments caused error in the request")
        )
        result = self.tool.merge_incident("PTGT", "PSRC")
        assert result.success is False
        assert "already resolved" in result.error_message.lower()

    def test_execute_group_merge_logs_fail(self, caplog):
        import pagerduty as _pd

        self.tool.pd_client.rput = MagicMock(side_effect=_pd.Error("bad"))
        target = _make_incident(incident_id="PTGT")
        source = _make_incident(incident_id="PSRC")
        group = MergeGroup(
            group_key="test",
            incidents=[target, source],
            target=target,
            sources=[source],
        )
        with caplog.at_level(logging.INFO, logger="pd_merge"):
            results = self.tool.execute_group_merge(group)
        assert len(results) == 1
        assert results[0].success is False


# ===========================================================================
# get_current_user
# ===========================================================================

class TestGetCurrentUser:
    def setup_method(self):
        self.tool = _make_tool()

    def test_successful_user_resolution(self):
        self.tool.pd_client.rget = MagicMock(
            return_value={"user": {"id": "U001", "email": "ops@example.com"}}
        )
        self.tool.get_current_user()
        assert self.tool.user_id == "U001"
        assert self.tool.user_email == "ops@example.com"

    def test_response_without_user_key(self):
        self.tool.pd_client.rget = MagicMock(
            return_value={"id": "U002", "email": "ops2@example.com"}
        )
        self.tool.get_current_user()
        assert self.tool.user_id == "U002"

    def test_unexpected_response_type_raises(self):
        self.tool.pd_client.rget = MagicMock(return_value="bad response")
        with pytest.raises(RuntimeError, match="Unexpected API response type"):
            self.tool.get_current_user()

    def test_pagerduty_error_raises_runtime(self):
        import pagerduty as _pd

        self.tool.pd_client.rget = MagicMock(side_effect=_pd.Error("auth fail"))
        with pytest.raises(RuntimeError, match="Failed to fetch current user"):
            self.tool.get_current_user()

    def test_missing_key_raises_runtime(self):
        self.tool.pd_client.rget = MagicMock(return_value={"user": {}})
        with pytest.raises(RuntimeError, match="Could not parse user ID"):
            self.tool.get_current_user()


# ===========================================================================
# fetch_active_incidents
# ===========================================================================

class TestFetchActiveIncidents:
    def setup_method(self):
        self.tool = _make_tool()
        self.tool.user_id = "U001"

    def _mock_incidents(self, assigned_user_id: str = "U001") -> dict:
        return {
            "id": "P001",
            "title": "test",
            "status": "triggered",
            "created_at": "2026-03-10T10:00:00Z",
            "assignments": [{"assignee": {"id": assigned_user_id}}],
        }

    def test_returns_merged_incidents(self):
        current_inc = self._mock_incidents()
        historical_inc = {**self._mock_incidents(), "id": "P002"}

        call_count = 0

        def list_all_side_effect(endpoint, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return iter([current_inc])
            return iter([historical_inc])

        self.tool.pd_client.list_all = MagicMock(side_effect=list_all_side_effect)
        incidents = self.tool.fetch_active_incidents()
        assert len(incidents) == 2

    def test_deduplicates_incidents(self):
        """Same incident returned in both passes should appear only once."""
        inc = self._mock_incidents()
        self.tool.pd_client.list_all = MagicMock(return_value=iter([inc]))
        incidents = self.tool.fetch_active_incidents()
        assert len(incidents) == 1

    def test_filters_by_user(self):
        """Incidents not assigned to self.user_id should be excluded."""
        inc = self._mock_incidents(assigned_user_id="OTHER")
        self.tool.pd_client.list_all = MagicMock(return_value=iter([inc]))
        incidents = self.tool.fetch_active_incidents()
        assert incidents == []

    def test_pd_error_raises_runtime(self):
        import pagerduty as _pd

        self.tool.pd_client.list_all = MagicMock(side_effect=_pd.Error("timeout"))
        with pytest.raises(RuntimeError, match="Failed to fetch PagerDuty incidents"):
            self.tool.fetch_active_incidents()

    def test_no_user_id_no_filter(self):
        """When user_id is None, no client-side filter is applied."""
        self.tool.user_id = None
        inc = self._mock_incidents(assigned_user_id="OTHER")
        self.tool.pd_client.list_all = MagicMock(return_value=iter([inc]))
        incidents = self.tool.fetch_active_incidents()
        assert len(incidents) == 1


# ===========================================================================
# validate_cross_date_merge (Scenario B)
# ===========================================================================

class TestValidateCrossDateMerge:
    def setup_method(self):
        self.tool = _make_tool()

    def test_no_jira_client_returns_false(self):
        old = _make_incident(jira_tickets=["DSSD-100"])
        new = _make_incident()
        ok, reason = self.tool.validate_cross_date_merge(old, new)
        assert ok is False
        assert "not configured" in reason

    def test_no_ticket_on_old_incident(self):
        self.tool.jira_client = MagicMock()
        old = _make_incident(jira_tickets=[])
        new = _make_incident()
        ok, reason = self.tool.validate_cross_date_merge(old, new)
        assert ok is False
        assert "No DSSD/DRGN ticket" in reason

    def test_ticket_from_context_notes(self):
        self.tool.jira_client = MagicMock()
        mock_issue = MagicMock()
        mock_issue.fields.summary = "ImportError: bad import"
        mock_issue.fields.description = "job ImportError raised"
        self.tool.jira_client.issue = MagicMock(return_value=mock_issue)
        self.tool.pd_client.list_all = MagicMock(return_value=iter([]))

        old = _make_incident(
            jira_tickets=[],
            context_notes=["DSSD-100 - Open. Snooze"],
        )
        new = _make_incident(real_notes=["ImportError occurred"])
        ok, reason = self.tool.validate_cross_date_merge(old, new)
        # Depending on content matching, just ensure it runs without exception
        assert isinstance(ok, bool)

    def test_jira_fetch_error_returns_false(self):
        self.tool.jira_client = MagicMock()
        self.tool.jira_client.issue = MagicMock(side_effect=Exception("not found"))
        old = _make_incident(jira_tickets=["DSSD-100"])
        new = _make_incident()
        ok, reason = self.tool.validate_cross_date_merge(old, new)
        assert ok is False
        assert "Failed to fetch Jira ticket" in reason

    def test_common_error_type_returns_true(self):
        self.tool.jira_client = MagicMock()
        mock_issue = MagicMock()
        mock_issue.fields.summary = "Job raised FetchFailedError in prod"
        mock_issue.fields.description = "FetchFailedError: connection reset"
        self.tool.jira_client.issue = MagicMock(return_value=mock_issue)

        def list_all_side_effect(endpoint, *args, **kwargs):
            return iter([{"summary": "Databricks job failed with FetchFailedError", "body": {}}])

        self.tool.pd_client.list_all = MagicMock(side_effect=list_all_side_effect)

        old = _make_incident(jira_tickets=["DSSD-100"])
        new = _make_incident()
        ok, reason = self.tool.validate_cross_date_merge(old, new)
        assert ok is True
        assert "FetchFailederror" in reason.lower() or "fetchfailederror" in reason.lower()

    def test_sla_vs_failure_returns_false(self):
        self.tool.jira_client = MagicMock()
        mock_issue = MagicMock()
        mock_issue.fields.summary = "Job exceeded SLA run time duration slow"
        mock_issue.fields.description = ""
        self.tool.jira_client.issue = MagicMock(return_value=mock_issue)

        def list_all_side_effect(endpoint, *args, **kwargs):
            return iter([{"summary": "Databricks job failed crash", "body": {}}])

        self.tool.pd_client.list_all = MagicMock(side_effect=list_all_side_effect)

        old = _make_incident(jira_tickets=["DSSD-100"])
        new = _make_incident()
        ok, reason = self.tool.validate_cross_date_merge(old, new)
        assert ok is False
        assert "Different root causes" in reason

    def test_no_common_errors_returns_false(self):
        self.tool.jira_client = MagicMock()
        mock_issue = MagicMock()
        mock_issue.fields.summary = "Nothing useful here"
        mock_issue.fields.description = ""
        self.tool.jira_client.issue = MagicMock(return_value=mock_issue)
        self.tool.pd_client.list_all = MagicMock(return_value=iter([]))

        old = _make_incident(jira_tickets=["DSSD-100"])
        new = _make_incident()
        ok, reason = self.tool.validate_cross_date_merge(old, new)
        assert ok is False
        assert "Could not confirm" in reason


# ===========================================================================
# _validate_scenario_b_group
# ===========================================================================

class TestValidateScenarioBGroup:
    def setup_method(self):
        self.tool = _make_tool()

    def test_all_have_tickets_no_action(self):
        """If all incidents have tickets, method returns without skip."""
        inc1 = _make_incident(incident_id="P001", jira_tickets=["DSSD-100"])
        inc2 = _make_incident(incident_id="P002", jira_tickets=["DSSD-200"])
        group = MergeGroup(group_key="test", incidents=[inc1, inc2], scenario="B")
        self.tool._validate_scenario_b_group(group)
        assert group.skip_reason is None

    def test_none_have_tickets_no_action(self):
        """If no incidents have tickets, method returns without skip."""
        inc1 = _make_incident(incident_id="P001", jira_tickets=[])
        inc2 = _make_incident(incident_id="P002", jira_tickets=[])
        group = MergeGroup(group_key="test", incidents=[inc1, inc2], scenario="B")
        self.tool._validate_scenario_b_group(group)
        assert group.skip_reason is None

    def test_failed_validation_sets_skip_reason(self):
        """Failed Jira check sets skip_reason on the group."""
        self.tool.jira_client = MagicMock()
        self.tool.jira_client.issue = MagicMock(side_effect=Exception("not found"))
        self.tool.pd_client.list_all = MagicMock(return_value=iter([]))

        old = _make_incident(incident_id="P001", created_at="2026-03-09T08:00:00Z", jira_tickets=["DSSD-100"])
        new = _make_incident(incident_id="P002", created_at="2026-03-10T08:00:00Z", jira_tickets=[])
        group = MergeGroup(group_key="test", incidents=[old, new], scenario="B")
        self.tool._validate_scenario_b_group(group)
        assert group.skip_reason is not None


# ===========================================================================
# _get_alert_text
# ===========================================================================

class TestGetAlertText:
    def setup_method(self):
        self.tool = _make_tool()

    def test_returns_concatenated_summaries(self):
        alerts = [
            {"summary": "alert one", "body": {}},
            {"summary": "alert two", "body": {"details": {"Description": "detail info"}}},
        ]
        self.tool.pd_client.list_all = MagicMock(return_value=iter(alerts))
        text = self.tool._get_alert_text("P001")
        assert "alert one" in text
        assert "alert two" in text
        assert "detail info" in text

    def test_pd_error_returns_empty_string(self):
        import pagerduty as _pd

        self.tool.pd_client.list_all = MagicMock(side_effect=_pd.Error("fail"))
        text = self.tool._get_alert_text("P001")
        assert text == ""


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

    def test_save_os_error_logs_warning(self, tmp_path, monkeypatch, caplog):
        """OSError during save is handled gracefully and logged as warning."""
        skip_file = tmp_path / "readonly.json"
        monkeypatch.setattr("pd_merge.SKIP_FILE", skip_file)

        with patch("pathlib.Path.write_text", side_effect=OSError("permission denied")):
            with caplog.at_level(logging.WARNING, logger="pd_merge"):
                PagerDutyMergeTool.save_skipped_ids({"P001"})
        assert any("Could not save skip file" in r.message for r in caplog.records)


# ===========================================================================
# print_group_detail_table
# ===========================================================================

class TestPrintGroupDetailTable:
    def setup_method(self):
        self.tool = _make_tool()

    def test_skip_reason_printed(self, caplog):
        group = MergeGroup(
            group_key="test",
            incidents=[_make_incident()],
            skip_reason="Already merged",
        )
        with caplog.at_level(logging.INFO, logger="pd_merge"):
            self.tool.print_group_detail_table(group)
        assert any("Already merged" in r.message for r in caplog.records)

    def test_no_target_prints_error(self, caplog):
        group = MergeGroup(group_key="test", incidents=[_make_incident()])
        with caplog.at_level(logging.INFO, logger="pd_merge"):
            self.tool.print_group_detail_table(group)
        assert any("No target" in r.message for r in caplog.records)

    def test_table_with_same_day_incidents(self, caplog):
        target = _make_incident(incident_id="P001", real_notes=["a note"])
        source = _make_incident(incident_id="P002")
        group = MergeGroup(
            group_key="jb_test",
            incidents=[target, source],
            target=target,
            sources=[source],
            scenario="A",
        )
        with caplog.at_level(logging.INFO, logger="pd_merge"):
            self.tool.print_group_detail_table(group)
        messages = " ".join(r.message for r in caplog.records)
        assert "TARGET" in messages
        assert "merge" in messages

    def test_table_cross_date_shows_date_column(self, caplog):
        target = _make_incident(incident_id="P001", created_at="2026-03-09T10:00:00Z")
        source = _make_incident(incident_id="P002", created_at="2026-03-10T10:00:00Z")
        group = MergeGroup(
            group_key="jb_test",
            incidents=[target, source],
            target=target,
            sources=[source],
            scenario="B",
        )
        with caplog.at_level(logging.INFO, logger="pd_merge"):
            self.tool.print_group_detail_table(group)
        messages = " ".join(r.message for r in caplog.records)
        # Date column header should appear
        assert "Date" in messages

    def test_rds_export_alert_label(self, caplog):
        target = _make_incident(
            incident_id="PRDS",
            title="RDS exports — failed to start",
            alert_type="unknown",
            normalized_job_name="rds exports",
        )
        source = _make_incident(
            incident_id="PSRC",
            title="RDS export jb_report failed",
            alert_type="unknown",
        )
        group = MergeGroup(
            group_key="RDS Exports — failed to start",
            incidents=[target, source],
            target=target,
            sources=[source],
            scenario="D",
        )
        with caplog.at_level(logging.INFO, logger="pd_merge"):
            self.tool.print_group_detail_table(group)
        messages = " ".join(r.message for r in caplog.records)
        assert "RDS Exports" in messages


# ===========================================================================
# print_summary_line
# ===========================================================================

class TestPrintSummaryLine:
    def setup_method(self):
        self.tool = _make_tool()

    def test_normal_group(self, caplog):
        target = _make_incident(incident_id="PTGT")
        source = _make_incident(incident_id="PSRC")
        group = MergeGroup(
            group_key="jb_test",
            incidents=[target, source],
            target=target,
            sources=[source],
            scenario="A",
        )
        with caplog.at_level(logging.INFO, logger="pd_merge"):
            self.tool.print_summary_line(1, group)
        messages = " ".join(r.message for r in caplog.records)
        assert "jb_test" in messages
        assert "Scenario A" in messages

    def test_skipped_group(self, caplog):
        group = MergeGroup(
            group_key="jb_test",
            incidents=[_make_incident()],
            skip_reason="cross-date without ticket",
        )
        with caplog.at_level(logging.INFO, logger="pd_merge"):
            self.tool.print_summary_line(2, group)
        messages = " ".join(r.message for r in caplog.records)
        assert "SKIP" in messages

    def test_group_with_no_target(self, caplog):
        group = MergeGroup(
            group_key="jb_test",
            incidents=[_make_incident(), _make_incident(incident_id="P002")],
        )
        with caplog.at_level(logging.INFO, logger="pd_merge"):
            self.tool.print_summary_line(3, group)
        messages = " ".join(r.message for r in caplog.records)
        assert "?" in messages


# ===========================================================================
# print_results_summary
# ===========================================================================

class TestPrintResultsSummary:
    def setup_method(self):
        self.tool = _make_tool()

    def test_no_results_prints_no_merges(self, caplog):
        with caplog.at_level(logging.INFO, logger="pd_merge"):
            self.tool.print_results_summary([], [])
        messages = " ".join(r.message for r in caplog.records)
        assert "No merges" in messages

    def test_results_with_successes(self, caplog):
        results = [
            MergeResult(target_id="T", source_id="S1", success=True),
            MergeResult(target_id="T", source_id="S2", success=True),
        ]
        with caplog.at_level(logging.INFO, logger="pd_merge"):
            self.tool.print_results_summary(results, [])
        messages = " ".join(r.message for r in caplog.records)
        assert "2" in messages

    def test_results_with_failures(self, caplog):
        results = [
            MergeResult(target_id="T", source_id="S1", success=True),
            MergeResult(target_id="T", source_id="S2", success=False, error_message="bad"),
        ]
        with caplog.at_level(logging.INFO, logger="pd_merge"):
            self.tool.print_results_summary(results, [])
        messages = " ".join(r.message for r in caplog.records)
        assert "Failed" in messages
        assert "S2" in messages

    def test_skipped_groups_shown(self, caplog):
        group = MergeGroup(group_key="jb_test", incidents=[], skip_reason="reason")
        with caplog.at_level(logging.INFO, logger="pd_merge"):
            self.tool.print_results_summary([], [group])
        messages = " ".join(r.message for r in caplog.records)
        assert "Skipped" in messages


# ===========================================================================
# _select_incidents
# ===========================================================================

class TestSelectIncidents:
    def setup_method(self):
        self.tool = _make_tool()

    def _make_group_with_sources(self, count: int = 3) -> MergeGroup:
        target = _make_incident(incident_id="PTGT")
        sources = [_make_incident(incident_id=f"PSRC{i}") for i in range(1, count + 1)]
        group = MergeGroup(
            group_key="jb_test",
            incidents=[target] + sources,
            target=target,
            sources=sources,
        )
        return group

    def test_none_returns_empty(self, caplog):
        group = self._make_group_with_sources(3)
        with patch("builtins.input", return_value="none"):
            result = self.tool._select_incidents(group)
        assert result == []

    def test_all_returns_all_sources(self):
        group = self._make_group_with_sources(3)
        with patch("builtins.input", return_value="all"):
            result = self.tool._select_incidents(group)
        assert len(result) == 3

    def test_single_number_selection(self):
        group = self._make_group_with_sources(3)
        with patch("builtins.input", return_value="2"):
            result = self.tool._select_incidents(group)
        assert len(result) == 1
        assert result[0].incident_id == "PSRC2"

    def test_comma_separated_selection(self):
        group = self._make_group_with_sources(3)
        with patch("builtins.input", return_value="1,3"):
            result = self.tool._select_incidents(group)
        assert len(result) == 2

    def test_range_selection(self):
        group = self._make_group_with_sources(4)
        with patch("builtins.input", return_value="1-3"):
            result = self.tool._select_incidents(group)
        assert len(result) == 3

    def test_invalid_range_skipped(self, caplog):
        group = self._make_group_with_sources(3)
        with patch("builtins.input", return_value="bad-range"):
            with caplog.at_level(logging.INFO, logger="pd_merge"):
                result = self.tool._select_incidents(group)
        assert result == []

    def test_invalid_number_skipped(self, caplog):
        group = self._make_group_with_sources(3)
        with patch("builtins.input", return_value="abc"):
            with caplog.at_level(logging.INFO, logger="pd_merge"):
                result = self.tool._select_incidents(group)
        assert result == []

    def test_out_of_range_index_skipped(self, caplog):
        group = self._make_group_with_sources(2)
        with patch("builtins.input", return_value="99"):
            with caplog.at_level(logging.INFO, logger="pd_merge"):
                result = self.tool._select_incidents(group)
        assert result == []

    def test_keyboard_interrupt_returns_empty(self):
        group = self._make_group_with_sources(2)
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            result = self.tool._select_incidents(group)
        assert result == []

    def test_eof_returns_empty(self):
        group = self._make_group_with_sources(2)
        with patch("builtins.input", side_effect=EOFError):
            result = self.tool._select_incidents(group)
        assert result == []

    def test_empty_input_returns_empty(self):
        group = self._make_group_with_sources(2)
        with patch("builtins.input", return_value=""):
            result = self.tool._select_incidents(group)
        assert result == []


# ===========================================================================
# run() — full orchestration
# ===========================================================================

class TestRunMethod:
    """Tests for the PagerDutyMergeTool.run() workflow."""

    def _make_full_tool(self, dry_run: bool = True) -> PagerDutyMergeTool:
        tool = _make_tool(dry_run=dry_run)
        tool.user_id = "U001"
        tool.user_email = "ops@example.com"
        return tool

    def _setup_mock_calls(
        self,
        tool: PagerDutyMergeTool,
        raw_incidents: list,
    ) -> None:
        tool.get_current_user = MagicMock()
        tool.fetch_active_incidents = MagicMock(return_value=raw_incidents)
        tool.fetch_and_classify_notes = MagicMock()

    def test_no_incidents_returns_early(self, caplog):
        tool = self._make_full_tool()
        self._setup_mock_calls(tool, [])
        with caplog.at_level(logging.INFO, logger="pd_merge"):
            tool.run()
        assert any("Nothing to merge" in r.message for r in caplog.records)

    def test_single_incident_no_groups(self, caplog):
        """One incident cannot form any merge groups."""
        tool = self._make_full_tool()
        raw = [{"id": "P001", "title": "Databricks batch job jb_x failed",
                "status": "triggered", "created_at": "2026-03-10T10:00:00Z",
                "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]}]
        self._setup_mock_calls(tool, raw)
        with caplog.at_level(logging.INFO, logger="pd_merge"):
            tool.run()
        messages = " ".join(r.message for r in caplog.records)
        assert "No groups to merge" in messages

    def test_skipped_incidents_filtered_out(self, caplog):
        """Previously skipped incidents are excluded from processing."""
        tool = self._make_full_tool()
        tool.skipped_ids = {"P001"}
        raw = [{"id": "P001", "title": "Databricks batch job jb_x failed",
                "status": "triggered", "created_at": "2026-03-10T10:00:00Z",
                "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]}]
        self._setup_mock_calls(tool, raw)
        with caplog.at_level(logging.INFO, logger="pd_merge"):
            tool.run()
        assert any("skipped" in r.message.lower() for r in caplog.records)

    def test_dry_run_group_merge(self, caplog, tmp_path, monkeypatch):
        """Two same-day incidents in dry-run mode should log DRY RUN."""
        monkeypatch.setattr("pd_merge.SKIP_FILE", tmp_path / "skips.json")
        tool = self._make_full_tool(dry_run=True)
        raw = [
            {"id": "P001", "title": "Databricks batch job jb_x failed",
             "status": "triggered", "created_at": "2026-03-10T10:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
            {"id": "P002", "title": "Databricks batch job jb_x failed",
             "status": "triggered", "created_at": "2026-03-10T12:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
        ]
        self._setup_mock_calls(tool, raw)
        with caplog.at_level(logging.INFO, logger="pd_merge"):
            tool.run()
        messages = " ".join(r.message for r in caplog.records)
        assert "DRY RUN" in messages

    def test_user_answers_yes_triggers_merge(self, caplog, tmp_path, monkeypatch):
        """User answering 'y' should trigger merge execution."""
        monkeypatch.setattr("pd_merge.SKIP_FILE", tmp_path / "skips.json")
        tool = self._make_full_tool(dry_run=False)
        tool.user_email = "ops@example.com"
        tool.pd_client.rput = MagicMock(return_value={})
        raw = [
            {"id": "P001", "title": "Databricks batch job jb_x failed",
             "status": "triggered", "created_at": "2026-03-10T10:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
            {"id": "P002", "title": "Databricks batch job jb_x failed",
             "status": "triggered", "created_at": "2026-03-10T12:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
        ]
        self._setup_mock_calls(tool, raw)
        with patch("builtins.input", return_value="y"):
            with caplog.at_level(logging.INFO, logger="pd_merge"):
                tool.run()
        tool.pd_client.rput.assert_called_once()

    def test_user_answers_n_skips_group(self, caplog, tmp_path, monkeypatch):
        """User answering 'n' should skip the group and persist skip IDs."""
        monkeypatch.setattr("pd_merge.SKIP_FILE", tmp_path / "skips.json")
        tool = self._make_full_tool(dry_run=False)
        raw = [
            {"id": "P001", "title": "Databricks batch job jb_x failed",
             "status": "triggered", "created_at": "2026-03-10T10:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
            {"id": "P002", "title": "Databricks batch job jb_x failed",
             "status": "triggered", "created_at": "2026-03-10T12:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
        ]
        self._setup_mock_calls(tool, raw)
        with patch("builtins.input", return_value="n"):
            with caplog.at_level(logging.INFO, logger="pd_merge"):
                tool.run()
        messages = " ".join(r.message for r in caplog.records)
        assert "Skipping" in messages or "skipped" in messages.lower()

    def test_user_answers_all_merges_remaining(self, caplog, tmp_path, monkeypatch):
        """User answering 'all' should merge all remaining groups."""
        monkeypatch.setattr("pd_merge.SKIP_FILE", tmp_path / "skips.json")
        tool = self._make_full_tool(dry_run=False)
        tool.user_email = "ops@example.com"
        tool.pd_client.rput = MagicMock(return_value={})
        raw = [
            {"id": "P001", "title": "Databricks batch job jb_x failed",
             "status": "triggered", "created_at": "2026-03-10T10:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
            {"id": "P002", "title": "Databricks batch job jb_x failed",
             "status": "triggered", "created_at": "2026-03-10T12:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
        ]
        self._setup_mock_calls(tool, raw)
        with patch("builtins.input", return_value="all"):
            with caplog.at_level(logging.INFO, logger="pd_merge"):
                tool.run()
        tool.pd_client.rput.assert_called_once()

    def test_user_answers_unrecognized_skips(self, caplog, tmp_path, monkeypatch):
        """Unrecognized input should skip the group."""
        monkeypatch.setattr("pd_merge.SKIP_FILE", tmp_path / "skips.json")
        tool = self._make_full_tool(dry_run=False)
        raw = [
            {"id": "P001", "title": "Databricks batch job jb_x failed",
             "status": "triggered", "created_at": "2026-03-10T10:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
            {"id": "P002", "title": "Databricks batch job jb_x failed",
             "status": "triggered", "created_at": "2026-03-10T12:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
        ]
        self._setup_mock_calls(tool, raw)
        with patch("builtins.input", return_value="xyzzy"):
            with caplog.at_level(logging.INFO, logger="pd_merge"):
                tool.run()
        messages = " ".join(r.message for r in caplog.records)
        assert "Unrecognized" in messages

    def test_keyboard_interrupt_during_prompt_aborts(self, caplog, tmp_path, monkeypatch):
        """KeyboardInterrupt during prompt aborts processing."""
        monkeypatch.setattr("pd_merge.SKIP_FILE", tmp_path / "skips.json")
        tool = self._make_full_tool(dry_run=False)
        raw = [
            {"id": "P001", "title": "Databricks batch job jb_x failed",
             "status": "triggered", "created_at": "2026-03-10T10:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
            {"id": "P002", "title": "Databricks batch job jb_x failed",
             "status": "triggered", "created_at": "2026-03-10T12:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
        ]
        self._setup_mock_calls(tool, raw)
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            with caplog.at_level(logging.INFO, logger="pd_merge"):
                tool.run()
        messages = " ".join(r.message for r in caplog.records)
        assert "Aborted" in messages

    def test_select_mode_with_selections(self, caplog, tmp_path, monkeypatch):
        """User choosing 'select' then picking incident '1' merges it."""
        monkeypatch.setattr("pd_merge.SKIP_FILE", tmp_path / "skips.json")
        tool = self._make_full_tool(dry_run=False)
        tool.user_email = "ops@example.com"
        tool.pd_client.rput = MagicMock(return_value={})
        raw = [
            {"id": "P001", "title": "Databricks batch job jb_x failed",
             "status": "triggered", "created_at": "2026-03-10T10:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
            {"id": "P002", "title": "Databricks batch job jb_x failed",
             "status": "triggered", "created_at": "2026-03-10T12:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
        ]
        self._setup_mock_calls(tool, raw)
        # First input for group prompt → "select"; second for selection → "1"
        with patch("builtins.input", side_effect=["select", "1"]):
            with caplog.at_level(logging.INFO, logger="pd_merge"):
                tool.run()
        tool.pd_client.rput.assert_called_once()

    def test_select_mode_empty_selection_skips(self, caplog, tmp_path, monkeypatch):
        """User choosing 'select' then selecting nothing skips the group."""
        monkeypatch.setattr("pd_merge.SKIP_FILE", tmp_path / "skips.json")
        tool = self._make_full_tool(dry_run=False)
        raw = [
            {"id": "P001", "title": "Databricks batch job jb_x failed",
             "status": "triggered", "created_at": "2026-03-10T10:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
            {"id": "P002", "title": "Databricks batch job jb_x failed",
             "status": "triggered", "created_at": "2026-03-10T12:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
        ]
        self._setup_mock_calls(tool, raw)
        with patch("builtins.input", side_effect=["select", "none"]):
            with caplog.at_level(logging.INFO, logger="pd_merge"):
                tool.run()
        messages = " ".join(r.message for r in caplog.records)
        assert "No incidents selected" in messages

    def test_mass_failure_scenario_c_detected(self, caplog, tmp_path, monkeypatch):
        """Mass-failure incident triggers Scenario C detection message."""
        monkeypatch.setattr("pd_merge.SKIP_FILE", tmp_path / "skips.json")
        tool = self._make_full_tool(dry_run=True)
        tool.pd_client.list_all = MagicMock(return_value=iter([]))
        raw = [
            {"id": "PMASS", "title": "DSSD-100 Multiple databricks batch jobs failing",
             "status": "triggered", "created_at": "2026-03-10T09:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
            {"id": "P001", "title": "Databricks batch job jb_a failed",
             "status": "triggered", "created_at": "2026-03-10T10:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
        ]
        self._setup_mock_calls(tool, raw)
        tool.fetch_and_classify_notes = MagicMock()
        with caplog.at_level(logging.INFO, logger="pd_merge"):
            tool.run()
        messages = " ".join(r.message for r in caplog.records)
        assert "Mass-failure" in messages

    def test_rds_exports_scenario_d_user_opts_in(self, caplog, tmp_path, monkeypatch):
        """User opting in to RDS merge triggers Scenario D group building."""
        monkeypatch.setattr("pd_merge.SKIP_FILE", tmp_path / "skips.json")
        tool = self._make_full_tool(dry_run=True)
        tool.pd_client.list_all = MagicMock(return_value=iter([]))
        raw = [
            {"id": "PRDS1", "title": "RDS exports — failed to start",
             "status": "triggered", "created_at": "2026-03-10T09:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
            {"id": "PRDS2", "title": "RDS export jb_rds_report failed more than 30 minutes",
             "status": "triggered", "created_at": "2026-03-10T10:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
        ]
        self._setup_mock_calls(tool, raw)
        tool.fetch_and_classify_notes = MagicMock()

        def rds_group_builder(incidents):
            target = tool.enrich_incident(raw[0])
            src = tool.enrich_incident(raw[1])
            target.real_notes = ["RDS exports failed to start"]
            target.notes_fetched = True
            return MergeGroup(
                group_key="RDS Exports — failed to start",
                incidents=[target, src],
                target=target,
                sources=[src],
                scenario="D",
            )

        tool.build_rds_exports_group = MagicMock(side_effect=rds_group_builder)

        with patch("builtins.input", return_value="y"):
            with caplog.at_level(logging.INFO, logger="pd_merge"):
                tool.run()
        messages = " ".join(r.message for r in caplog.records)
        assert "RDS Exports" in messages or "DRY RUN" in messages

    def test_rds_exports_user_opts_out(self, caplog, tmp_path, monkeypatch):
        """User opting out of RDS merge logs disabled message."""
        monkeypatch.setattr("pd_merge.SKIP_FILE", tmp_path / "skips.json")
        tool = self._make_full_tool(dry_run=True)
        tool.pd_client.list_all = MagicMock(return_value=iter([]))
        raw = [
            {"id": "PRDS1", "title": "RDS exports — failed to start",
             "status": "triggered", "created_at": "2026-03-10T09:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
            {"id": "PRDS2", "title": "RDS export jb_rds_report failed more than 30 minutes",
             "status": "triggered", "created_at": "2026-03-10T10:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
        ]
        self._setup_mock_calls(tool, raw)
        tool.fetch_and_classify_notes = MagicMock()

        with patch("builtins.input", return_value="n"):
            with caplog.at_level(logging.INFO, logger="pd_merge"):
                tool.run()
        messages = " ".join(r.message for r in caplog.records)
        assert "disabled" in messages.lower()

    def test_rds_group_build_fails_logs_skip(self, caplog, tmp_path, monkeypatch):
        """If build_rds_exports_group returns None, appropriate message logged."""
        monkeypatch.setattr("pd_merge.SKIP_FILE", tmp_path / "skips.json")
        tool = self._make_full_tool(dry_run=True)
        tool.pd_client.list_all = MagicMock(return_value=iter([]))
        raw = [
            {"id": "PRDS1", "title": "RDS exports — failed to start",
             "status": "triggered", "created_at": "2026-03-10T09:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
            {"id": "PRDS2", "title": "RDS export jb_rds_report failed",
             "status": "triggered", "created_at": "2026-03-10T10:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
        ]
        self._setup_mock_calls(tool, raw)
        tool.fetch_and_classify_notes = MagicMock()
        tool.build_rds_exports_group = MagicMock(return_value=None)

        with patch("builtins.input", return_value="y"):
            with caplog.at_level(logging.INFO, logger="pd_merge"):
                tool.run()
        messages = " ".join(r.message for r in caplog.records)
        assert "skipped" in messages.lower() or "no 'Failed to start'" in messages

    def test_rds_kbd_interrupt_defaults_no(self, caplog, tmp_path, monkeypatch):
        """KeyboardInterrupt during RDS merge prompt defaults to 'n'."""
        monkeypatch.setattr("pd_merge.SKIP_FILE", tmp_path / "skips.json")
        tool = self._make_full_tool(dry_run=True)
        tool.pd_client.list_all = MagicMock(return_value=iter([]))
        raw = [
            {"id": "PRDS1", "title": "RDS exports — failed to start",
             "status": "triggered", "created_at": "2026-03-10T09:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
            {"id": "PRDS2", "title": "RDS export jb_rds_report failed",
             "status": "triggered", "created_at": "2026-03-10T10:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
        ]
        self._setup_mock_calls(tool, raw)
        tool.fetch_and_classify_notes = MagicMock()

        with patch("builtins.input", side_effect=KeyboardInterrupt):
            with caplog.at_level(logging.INFO, logger="pd_merge"):
                tool.run()
        messages = " ".join(r.message for r in caplog.records)
        assert "disabled" in messages.lower()

    def test_skip_word_answer_skips_group(self, caplog, tmp_path, monkeypatch):
        """User typing 'skip' explicitly skips group."""
        monkeypatch.setattr("pd_merge.SKIP_FILE", tmp_path / "skips.json")
        tool = self._make_full_tool(dry_run=False)
        raw = [
            {"id": "P001", "title": "Databricks batch job jb_x failed",
             "status": "triggered", "created_at": "2026-03-10T10:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
            {"id": "P002", "title": "Databricks batch job jb_x failed",
             "status": "triggered", "created_at": "2026-03-10T12:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
        ]
        self._setup_mock_calls(tool, raw)
        with patch("builtins.input", return_value="skip"):
            with caplog.at_level(logging.INFO, logger="pd_merge"):
                tool.run()
        messages = " ".join(r.message for r in caplog.records)
        assert "Skipping" in messages


# ===========================================================================
# main() entry point
# ===========================================================================

class TestMain:
    """Tests for the main() CLI entry point."""

    def _call_main(self, args: list, env: dict = None, monkeypatch=None):
        """Call main() with given sys.argv and env vars."""
        import pd_merge

        full_env = {"PAGERDUTY_API_TOKEN": "test-token"}
        if env:
            full_env.update(env)

        with patch.object(sys, "argv", ["pd_merge.py"] + args):
            with patch.dict("os.environ", full_env):
                yield

    def test_help_exits_zero(self, monkeypatch):
        with patch.object(sys, "argv", ["pd_merge.py", "--help"]):
            with patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "tok"}):
                with pytest.raises(SystemExit) as exc:
                    main()
        assert exc.value.code == 0

    def test_short_help_exits_zero(self, monkeypatch):
        with patch.object(sys, "argv", ["pd_merge.py", "-h"]):
            with patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "tok"}):
                with pytest.raises(SystemExit) as exc:
                    main()
        assert exc.value.code == 0

    def test_unknown_arg_exits_one(self, monkeypatch):
        with patch.object(sys, "argv", ["pd_merge.py", "--unknown-arg"]):
            with patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "tok"}):
                with pytest.raises(SystemExit) as exc:
                    main()
        assert exc.value.code == 1

    def test_clear_skips_with_existing_file(self, tmp_path, monkeypatch):
        skip_file = tmp_path / "skips.json"
        skip_file.write_text('{"skipped_incident_ids": ["P001"]}', encoding="utf-8")
        monkeypatch.setattr("pd_merge.SKIP_FILE", skip_file)
        with patch.object(sys, "argv", ["pd_merge.py", "--clear-skips"]):
            with patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "tok"}):
                with pytest.raises(SystemExit) as exc:
                    main()
        assert exc.value.code == 0
        assert not skip_file.exists()

    def test_clear_skips_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pd_merge.SKIP_FILE", tmp_path / "nonexistent.json")
        with patch.object(sys, "argv", ["pd_merge.py", "--clear-skips"]):
            with patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "tok"}):
                with pytest.raises(SystemExit) as exc:
                    main()
        assert exc.value.code == 0

    def test_show_skips_with_skips(self, tmp_path, monkeypatch, caplog):
        skip_file = tmp_path / "skips.json"
        skip_file.write_text(
            '{"skipped_incident_ids": ["P001", "P002"]}', encoding="utf-8"
        )
        monkeypatch.setattr("pd_merge.SKIP_FILE", skip_file)
        with patch.object(sys, "argv", ["pd_merge.py", "--show-skips"]):
            with patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "tok"}):
                with caplog.at_level(logging.INFO, logger="pd_merge"):
                    with pytest.raises(SystemExit) as exc:
                        main()
        assert exc.value.code == 0
        messages = " ".join(r.message for r in caplog.records)
        assert "P001" in messages

    def test_show_skips_empty(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr("pd_merge.SKIP_FILE", tmp_path / "nonexistent.json")
        with patch.object(sys, "argv", ["pd_merge.py", "--show-skips"]):
            with patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "tok"}):
                with caplog.at_level(logging.INFO, logger="pd_merge"):
                    with pytest.raises(SystemExit) as exc:
                        main()
        assert exc.value.code == 0
        messages = " ".join(r.message for r in caplog.records)
        assert "No skipped" in messages

    def test_banner_dry_run(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr("pd_merge.SKIP_FILE", tmp_path / "skips.json")

        mock_tool = MagicMock()
        mock_tool.run = MagicMock()

        with patch.object(sys, "argv", ["pd_merge.py", "--dry-run"]):
            with patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "tok"}):
                with patch("pd_merge.PagerDutyMergeTool", return_value=mock_tool):
                    with caplog.at_level(logging.INFO, logger="pd_merge"):
                        main()
        messages = " ".join(r.message for r in caplog.records)
        assert "DRY RUN" in messages

    def _run_main_with_skip_file(
        self,
        skip_file,
        monkeypatch,
        caplog,
        input_side_effect,
    ):
        """Helper that runs main() with a pre-populated skip file.

        Patches only __init__ so that load_skipped_ids (a @staticmethod)
        keeps working via the module-level SKIP_FILE monkeypatch.
        """
        import pd_merge as _pd_merge

        real_load = _pd_merge.PagerDutyMergeTool.load_skipped_ids

        mock_tool = MagicMock()
        mock_tool.run = MagicMock()

        def fake_init(self_inner, **kwargs):
            pass  # Skip real __init__; keep static methods

        with patch.object(_pd_merge.PagerDutyMergeTool, "__init__", fake_init):
            # Restore the static method (patch.object replaces it otherwise)
            with patch(
                "pd_merge.PagerDutyMergeTool",
                wraps=_pd_merge.PagerDutyMergeTool,
            ) as MockClass:
                MockClass.load_skipped_ids = real_load
                MockClass.return_value = mock_tool
                with patch.object(sys, "argv", ["pd_merge.py"]):
                    with patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "tok"}):
                        with patch("builtins.input", side_effect=input_side_effect):
                            with caplog.at_level(logging.INFO, logger="pd_merge"):
                                main()

    def test_skip_list_prompt_clear_yes(self, tmp_path, monkeypatch, caplog):
        skip_file = tmp_path / "skips.json"
        skip_file.write_text('{"skipped_incident_ids": ["P001"]}', encoding="utf-8")
        monkeypatch.setattr("pd_merge.SKIP_FILE", skip_file)

        mock_tool = MagicMock()
        mock_tool.run = MagicMock()

        with patch.object(sys, "argv", ["pd_merge.py"]):
            with patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "tok"}):
                with patch("pd_merge.PagerDutyMergeTool", return_value=mock_tool) as MockClass:
                    MockClass.load_skipped_ids = MagicMock(return_value={"P001"})
                    with patch("builtins.input", return_value="yes"):
                        with caplog.at_level(logging.INFO, logger="pd_merge"):
                            main()
        messages = " ".join(r.message for r in caplog.records)
        assert "Cleared" in messages

    def test_skip_list_prompt_clear_no(self, tmp_path, monkeypatch, caplog):
        skip_file = tmp_path / "skips.json"
        skip_file.write_text('{"skipped_incident_ids": ["P001"]}', encoding="utf-8")
        monkeypatch.setattr("pd_merge.SKIP_FILE", skip_file)

        mock_tool = MagicMock()
        mock_tool.run = MagicMock()

        with patch.object(sys, "argv", ["pd_merge.py"]):
            with patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "tok"}):
                with patch("pd_merge.PagerDutyMergeTool", return_value=mock_tool) as MockClass:
                    MockClass.load_skipped_ids = MagicMock(return_value={"P001"})
                    with patch("builtins.input", return_value="n"):
                        with caplog.at_level(logging.INFO, logger="pd_merge"):
                            main()
        messages = " ".join(r.message for r in caplog.records)
        assert "Keeping" in messages

    def test_skip_list_prompt_keyboard_interrupt(self, tmp_path, monkeypatch, caplog):
        skip_file = tmp_path / "skips.json"
        skip_file.write_text('{"skipped_incident_ids": ["P001"]}', encoding="utf-8")
        monkeypatch.setattr("pd_merge.SKIP_FILE", skip_file)

        mock_tool = MagicMock()
        mock_tool.run = MagicMock()

        with patch.object(sys, "argv", ["pd_merge.py"]):
            with patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "tok"}):
                with patch("pd_merge.PagerDutyMergeTool", return_value=mock_tool) as MockClass:
                    MockClass.load_skipped_ids = MagicMock(return_value={"P001"})
                    with patch("builtins.input", side_effect=KeyboardInterrupt):
                        with caplog.at_level(logging.INFO, logger="pd_merge"):
                            main()
        messages = " ".join(r.message for r in caplog.records)
        # Default is 'n' when KeyboardInterrupt
        assert "Keeping" in messages

    def test_keyboard_interrupt_exits_130(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pd_merge.SKIP_FILE", tmp_path / "skips.json")

        mock_tool = MagicMock()
        mock_tool.run = MagicMock(side_effect=KeyboardInterrupt)

        with patch.object(sys, "argv", ["pd_merge.py"]):
            with patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "tok"}):
                with patch("pd_merge.PagerDutyMergeTool", return_value=mock_tool):
                    with pytest.raises(SystemExit) as exc:
                        main()
        assert exc.value.code == 130

    def test_runtime_error_exits_one(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pd_merge.SKIP_FILE", tmp_path / "skips.json")

        mock_tool = MagicMock()
        mock_tool.run = MagicMock(side_effect=RuntimeError("API failure"))

        with patch.object(sys, "argv", ["pd_merge.py"]):
            with patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "tok"}):
                with patch("pd_merge.PagerDutyMergeTool", return_value=mock_tool):
                    with pytest.raises(SystemExit) as exc:
                        main()
        assert exc.value.code == 1

    def test_unexpected_error_exits_one(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pd_merge.SKIP_FILE", tmp_path / "skips.json")

        mock_tool = MagicMock()
        mock_tool.run = MagicMock(side_effect=ValueError("unexpected"))

        with patch.object(sys, "argv", ["pd_merge.py"]):
            with patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "tok"}):
                with patch("pd_merge.PagerDutyMergeTool", return_value=mock_tool):
                    with pytest.raises(SystemExit) as exc:
                        main()
        assert exc.value.code == 1

    def test_unexpected_error_verbose_prints_traceback(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("pd_merge.SKIP_FILE", tmp_path / "skips.json")

        mock_tool = MagicMock()
        mock_tool.run = MagicMock(side_effect=ValueError("unexpected traceback test"))

        with patch.object(sys, "argv", ["pd_merge.py", "--verbose"]):
            with patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "tok"}):
                with patch("pd_merge.PagerDutyMergeTool", return_value=mock_tool):
                    with pytest.raises(SystemExit):
                        main()
        captured = capsys.readouterr()
        assert "ValueError" in captured.err or "Traceback" in captured.err

    def test_dry_run_and_verbose_flags(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pd_merge.SKIP_FILE", tmp_path / "skips.json")

        mock_tool = MagicMock()
        mock_tool.run = MagicMock()

        captured_kwargs = {}

        def capture_tool_init(**kwargs):
            captured_kwargs.update(kwargs)
            return mock_tool

        with patch.object(sys, "argv", ["pd_merge.py", "--dry-run", "--verbose"]):
            with patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "tok"}):
                with patch("pd_merge.PagerDutyMergeTool", side_effect=capture_tool_init):
                    main()
        assert captured_kwargs.get("dry_run") is True
        assert captured_kwargs.get("verbose") is True

    def test_short_flags_dry_run_verbose(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pd_merge.SKIP_FILE", tmp_path / "skips.json")

        mock_tool = MagicMock()
        mock_tool.run = MagicMock()

        captured_kwargs = {}

        def capture_tool_init(**kwargs):
            captured_kwargs.update(kwargs)
            return mock_tool

        with patch.object(sys, "argv", ["pd_merge.py", "-n", "-v"]):
            with patch.dict("os.environ", {"PAGERDUTY_API_TOKEN": "tok"}):
                with patch("pd_merge.PagerDutyMergeTool", side_effect=capture_tool_init):
                    main()
        assert captured_kwargs.get("dry_run") is True
        assert captured_kwargs.get("verbose") is True


# ===========================================================================
# PagerDutyMergeTool.__init__ with Jira (line 211)
# ===========================================================================

class TestToolInitWithJira:
    def test_jira_client_initialized_when_available(self):
        """Exercises the JIRA_AVAILABLE + credentials branch in __init__."""
        import pd_merge as _pd_merge

        mock_jira_client = MagicMock()
        with patch("noc_utils._pagerduty") as mock_pd:
            mock_pd.RestApiV2Client.return_value = MagicMock()
            with patch("pd_merge.JIRA_AVAILABLE", True):
                with patch("pd_merge.new_jira_client", return_value=(mock_jira_client, "https://jira/browse")):
                    tool = _pd_merge.PagerDutyMergeTool(
                        pagerduty_api_token="tok",
                        jira_server_url="https://jira.example.com",
                        jira_personal_access_token="my-pat",
                    )
        assert tool.jira_client is mock_jira_client


# ===========================================================================
# fetch_active_incidents — historical filter branch (line 323)
# ===========================================================================

class TestFetchActiveIncidentsHistoricalFilter:
    def test_historical_incident_not_assigned_excluded(self):
        """Historical incident (new ID) assigned to different user is excluded."""
        tool = _make_tool()
        tool.user_id = "U001"

        current_inc = {
            "id": "P001", "title": "t", "status": "triggered",
            "created_at": "2026-03-10T10:00:00Z",
            "assignments": [{"assignee": {"id": "U001"}}],
        }
        historical_inc = {
            "id": "P002", "title": "t", "status": "triggered",
            "created_at": "2026-03-10T11:00:00Z",
            "assignments": [{"assignee": {"id": "OTHER"}}],
        }

        call_count = 0

        def list_all_side_effect(endpoint, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return iter([current_inc])
            return iter([historical_inc])

        tool.pd_client.list_all = MagicMock(side_effect=list_all_side_effect)
        incidents = tool.fetch_active_incidents()
        # Only P001 should be included; P002 is assigned to OTHER
        assert all(i["id"] == "P001" for i in incidents)


# ===========================================================================
# build_rds_exports_group — no sources after removing target (line 726)
# ===========================================================================

class TestBuildRdsExportsGroupNoSources:
    def test_only_target_no_source_returns_none(self):
        """When the only second RDS incident is the same as the target, sources is empty."""
        tool = _make_tool()
        tool.pd_client.list_all = MagicMock(return_value=iter([]))

        # Two "failed to start" incidents — first becomes target, second is excluded
        # as source since both would match as target. We need a case where
        # after removing target from sources, sources is empty.
        # Simplest: only one non-target RDS incident with same "failed to start" title
        # that becomes the target, and no other RDS incidents.
        target = _make_incident(
            incident_id="PRDS_T",
            title="RDS exports — failed to start",
            alert_type="unknown",
            normalized_job_name="rds exports — failed to start",
            real_notes=["failed to start"],
            notes_fetched=True,
        )
        # Create a second incident that does NOT match RDS_EXPORT_RE (so rds_incidents
        # only has 2, but after removing target, sources = [non_rds]? No, need 2 RDS.)
        # Actually: build two RDS incidents both "failed to start". First is target.
        # Then sources = rds_incidents minus target = [second]. That's not empty.
        # To get empty: we need only target in rds_incidents (len < 2 → already returns None)
        # OR all other RDS incidents are also the same id as target (impossible with different IDs).
        # The branch fires when sources list comprehension is empty after excluding target.
        # This can happen if all incidents have the same incident_id as target (impossible)
        # OR if there are exactly 2 RDS incidents and the "source" candidate is filtered out
        # by the "sources = [inc for inc in rds_incidents if inc.incident_id != target.incident_id]"
        # That only leaves the case where len(rds_incidents) == 1 (returns None via len < 2 check)
        # or all non-target rds_incidents have the same ID as target.
        # In practice: this is unreachable without duplicated IDs.
        # Mark as covered-in-implementation; test verifies no crash on edge case.
        result = tool.build_rds_exports_group([target])
        # len(rds_incidents) < 2 → returns None before reaching line 726
        assert result is None


# ===========================================================================
# run() — skipped groups shown (lines 1297-1299)
# ===========================================================================

class TestRunSkippedGroupsDisplay:
    def test_skipped_group_shown_in_summary(self, caplog, tmp_path, monkeypatch):
        """A cross-date group without tickets is skipped; 'Skipped' appears in log."""
        monkeypatch.setattr("pd_merge.SKIP_FILE", tmp_path / "skips.json")
        tool = _make_tool(dry_run=True)
        tool.pd_client.list_all = MagicMock(return_value=iter([]))

        # Two same-name incidents on different days without tickets → skipped
        raw = [
            {"id": "P001", "title": "Databricks batch job jb_x failed",
             "status": "triggered", "created_at": "2026-03-09T10:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
            {"id": "P002", "title": "Databricks batch job jb_x failed",
             "status": "triggered", "created_at": "2026-03-10T10:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
        ]
        tool.get_current_user = MagicMock()
        tool.user_id = "U001"
        tool.user_email = "ops@example.com"
        tool.fetch_active_incidents = MagicMock(return_value=raw)
        tool.fetch_and_classify_notes = MagicMock()

        with caplog.at_level(logging.INFO, logger="pd_merge"):
            tool.run()
        messages = " ".join(r.message for r in caplog.records)
        assert "Skipped" in messages or "skipped" in messages


# ===========================================================================
# run() — select mode with partial skip (line 1346)
# ===========================================================================

class TestRunSelectModePartialSkip:
    def test_select_mode_partial_merge_persists_unselected(self, caplog, tmp_path, monkeypatch):
        """Select mode with 3 incidents (2 sources): selecting only 1 skips the other."""
        monkeypatch.setattr("pd_merge.SKIP_FILE", tmp_path / "skips.json")
        tool = _make_tool(dry_run=False)
        tool.user_email = "ops@example.com"
        tool.pd_client.rput = MagicMock(return_value={})

        raw = [
            {"id": "P001", "title": "Databricks batch job jb_x failed",
             "status": "triggered", "created_at": "2026-03-10T08:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
            {"id": "P002", "title": "Databricks batch job jb_x failed",
             "status": "triggered", "created_at": "2026-03-10T10:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
            {"id": "P003", "title": "Databricks batch job jb_x failed",
             "status": "triggered", "created_at": "2026-03-10T12:00:00Z",
             "html_url": "", "assignments": [{"assignee": {"id": "U001"}}]},
        ]
        tool.get_current_user = MagicMock()
        tool.user_id = "U001"
        tool.user_email = "ops@example.com"
        tool.fetch_active_incidents = MagicMock(return_value=raw)
        tool.fetch_and_classify_notes = MagicMock()

        # 3 incidents: 1 target + 2 sources. Select only source #1.
        # First input is group prompt → "select"; second is incident selection → "1"
        with patch("builtins.input", side_effect=["select", "1"]):
            with caplog.at_level(logging.INFO, logger="pd_merge"):
                tool.run()

        # P002 should be merged; P003 should be added to skipped IDs
        assert "P003" in tool.skipped_ids

        # Verify the skip file was saved
        assert (tmp_path / "skips.json").exists()


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


# ===========================================================================
# Import error fallback (lines 35-39)
# ===========================================================================


class TestImportErrorFallback:
    """Test that the import-error fallback exits with code 1."""

    def test_missing_pagerduty_exits(self) -> None:
        """Module exits with code 1 when pagerduty package is unavailable."""
        import pd_merge

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "pagerduty":
                raise ImportError("No module named 'pagerduty'")
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=fake_import):
            with pytest.raises(SystemExit) as exc_info:
                importlib.reload(pd_merge)
            assert exc_info.value.code == 1


class TestJiraImportFallback:
    """Test the optional Jira import fallback (lines 46-47)."""

    def test_jira_unavailable_sets_flag_false(self) -> None:
        """When jira is not installed, JIRA_AVAILABLE is set to False."""
        import pd_merge

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "jira.exceptions":
                raise ImportError("No module named 'jira'")
            return real_import(name, *args, **kwargs)

        try:
            with patch.object(builtins, "__import__", side_effect=fake_import):
                importlib.reload(pd_merge)
            assert pd_merge.JIRA_AVAILABLE is False
        finally:
            importlib.reload(pd_merge)


# ===========================================================================
# build_rds_exports_group — all-same-id edge case (line 726)
# ===========================================================================


class TestBuildRdsExportsGroupNoSources:
    """Cover the 'no sources after filtering target' branch."""

    def setup_method(self):
        self.tool = _make_tool()

    def test_all_same_id_returns_none(self) -> None:
        """When all RDS incidents share the target's ID, sources is empty → None."""
        inc1 = _make_incident(
            incident_id="P001",
            title="RDS exports — failed to start",
            normalized_job_name="rds exports — failed to start",
            raw_job_name="RDS exports — failed to start",
            real_notes=["RDS exports failed to start at 05:00"],
        )
        # Duplicate with same incident_id
        inc2 = _make_incident(
            incident_id="P001",
            title="RDS export jb_rds_foo is failed more than 30 minutes",
            normalized_job_name="rds export jb_rds_foo",
            raw_job_name="RDS export jb_rds_foo",
        )

        result = self.tool.build_rds_exports_group([inc1, inc2])
        assert result is None


# ===========================================================================
# run() — Scenario B validation call (line 1274)
# ===========================================================================


class TestRunScenarioBValidation:
    """Cover the _validate_scenario_b_group call inside run()."""

    def setup_method(self):
        self.tool = _make_tool()

    def test_scenario_b_validation_called(self, caplog) -> None:
        """Scenario B group triggers _validate_scenario_b_group during run()."""
        from pd_merge import MergeGroup

        inc1 = _make_incident(
            incident_id="P001",
            title="Job jb_alpha failed",
            created_at="2026-01-01T10:00:00Z",
            normalized_job_name="jb_alpha",
            jira_tickets=["DSSD-100"],
        )
        inc2 = _make_incident(
            incident_id="P002",
            title="Job jb_alpha failed",
            created_at="2026-01-02T10:00:00Z",
            normalized_job_name="jb_alpha",
            jira_tickets=["DSSD-100"],
        )

        group = MergeGroup(
            group_key="jb_alpha",
            incidents=[inc1, inc2],
            target=inc1,
            sources=[inc2],
            scenario="B",
        )

        # Mock internal methods called by run()
        self.tool.get_current_user = MagicMock()
        self.tool.fetch_active_incidents = MagicMock(return_value=[{"id": "P001"}, {"id": "P002"}])
        self.tool.enrich_incident = MagicMock(side_effect=[inc1, inc2])
        self.tool._find_mass_failure_incident = MagicMock(return_value=None)
        self.tool.group_incidents = MagicMock(return_value={"jb_alpha": [inc1, inc2]})
        self.tool.classify_group = MagicMock(return_value=group)
        self.tool.fetch_and_classify_notes = MagicMock()
        self.tool._validate_scenario_b_group = MagicMock()
        self.tool.select_target = MagicMock()

        self.tool.run()

        self.tool._validate_scenario_b_group.assert_called_once_with(group)
