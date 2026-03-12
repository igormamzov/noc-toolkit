"""Tests for data-freshness tool (Data Freshness Checker v0.1.1)."""

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from data_freshness import (
    DatabricksSQL,
    DatabricksAPIError,
    DataFreshnessChecker,
    FreshnessRow,
    GranularResult,
    _html_escape,
    _is_fresh_date,
    _yesterday_str,
    _sla_status,
    build_dacscan_granular_query,
    build_non_dacscan_freshness_query,
    build_bi_loader_freshness_query,
    format_table,
    format_csv,
    format_json,
    DACSCAN_TABLE_MAP,
    NON_DACSCAN_TABLE_MAP,
    BI_LOADER_TABLE_MAP,
    EXPECTED_HOST_COUNT,
    DB_SCHEMA,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_row(
    group_name: str = "DACSCAN",
    db_name: str = "PFOCUSVW",
    table_name: str = "SALES_ORD",
    data_date: str = "2026-03-10",
    sla: str = "11:00 AM PDT",
    met: str = "Yes",
    comments: str = " ",
) -> FreshnessRow:
    return FreshnessRow(group_name, db_name, table_name, data_date, sla, met, comments)


def _make_checker() -> DataFreshnessChecker:
    """Create a DataFreshnessChecker with a mocked DatabricksSQL client."""
    mock_db = MagicMock(spec=DatabricksSQL)
    return DataFreshnessChecker(mock_db, verbose=False)


# ===========================================================================
# Tests: pure helper functions
# ===========================================================================


class TestHtmlEscape:

    def test_ampersand(self):
        assert _html_escape("A & B") == "A &amp; B"

    def test_angle_brackets(self):
        assert _html_escape("<script>") == "&lt;script&gt;"

    def test_double_quote(self):
        assert _html_escape('key="val"') == "key=&quot;val&quot;"

    def test_no_escape_needed(self):
        assert _html_escape("plain text") == "plain text"


class TestYesterdayStr:

    @patch("data_freshness.datetime")
    def test_returns_yesterday(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 11, 14, 0, 0, tzinfo=timezone.utc)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = _yesterday_str()
        assert result == "2026-03-10"

    @patch("data_freshness.datetime")
    def test_month_boundary(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = _yesterday_str()
        assert result == "2026-03-31"


class TestIsFreshDate:

    @patch("data_freshness._yesterday_str", return_value="2026-03-10")
    @patch("data_freshness.datetime")
    def test_today_is_fresh(self, mock_dt, _mock_yesterday):
        mock_dt.now.return_value = datetime(2026, 3, 11, 14, 0, 0, tzinfo=timezone.utc)
        assert _is_fresh_date("2026-03-11 08:00:00") is True

    @patch("data_freshness._yesterday_str", return_value="2026-03-10")
    @patch("data_freshness.datetime")
    def test_yesterday_is_fresh(self, mock_dt, _mock_yesterday):
        mock_dt.now.return_value = datetime(2026, 3, 11, 14, 0, 0, tzinfo=timezone.utc)
        assert _is_fresh_date("2026-03-10") is True

    @patch("data_freshness._yesterday_str", return_value="2026-03-10")
    @patch("data_freshness.datetime")
    def test_old_date_is_stale(self, mock_dt, _mock_yesterday):
        mock_dt.now.return_value = datetime(2026, 3, 11, 14, 0, 0, tzinfo=timezone.utc)
        assert _is_fresh_date("2026-03-08") is False

    @patch("data_freshness._yesterday_str", return_value="2026-03-10")
    @patch("data_freshness.datetime")
    def test_na_is_stale(self, mock_dt, _mock_yesterday):
        mock_dt.now.return_value = datetime(2026, 3, 11, 14, 0, 0, tzinfo=timezone.utc)
        assert _is_fresh_date("N/A") is False


class TestSlaStatus:

    @patch("data_freshness.datetime")
    def test_before_deadline(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 11, 15, 0, 0, tzinfo=timezone.utc)
        result = _sla_status()
        assert "until SLA deadline" in result
        assert "2h 30m" in result

    @patch("data_freshness.datetime")
    def test_after_deadline(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 11, 18, 0, 0, tzinfo=timezone.utc)
        result = _sla_status()
        assert "PASSED" in result
        assert "0h 30m" in result

    @patch("data_freshness.datetime")
    def test_exactly_at_deadline(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 11, 17, 30, 0, tzinfo=timezone.utc)
        result = _sla_status()
        assert "PASSED" in result
        assert "0h 0m" in result


# ===========================================================================
# Tests: query builders
# ===========================================================================


class TestBuildDacscanGranularQuery:

    def test_standard_table(self):
        sql = build_dacscan_granular_query("SALES_ORD", "pfocusdb_sales_ord")
        assert "pfocusdb_sales_ord" in sql
        assert "host_sys_cd" in sql
        assert "SALES_ORD" in sql
        assert DB_SCHEMA in sql
        # Should contain excluded hosts
        assert "TWB" in sql
        assert "CH8" in sql

    def test_event_opt_special_case(self):
        sql = build_dacscan_granular_query("SALES_ORD_EVENT_OPT", "pfocusdb_sales_ord_event_opt")
        assert "max(update_ts)" in sql
        assert "DSSD-29069" not in sql  # DSSD-29069 is in result detail, not query
        # Should NOT have host_sys_cd logic
        assert "count(distinct host_sys_cd)" not in sql

    def test_all_dacscan_tables_generate_valid_sql(self):
        for table_name, base_table in DACSCAN_TABLE_MAP.items():
            sql = build_dacscan_granular_query(table_name, base_table)
            assert len(sql) > 50
            assert base_table in sql


class TestBuildNonDacscanQuery:

    def test_basic(self):
        sql = build_non_dacscan_freshness_query("AUDIT_STAR", "pfocusdb_audit_star")
        assert "AUDIT_STAR" in sql
        assert "pfocusdb_audit_star" in sql
        assert "max(update_ts)" in sql

    def test_all_non_dacscan_tables(self):
        for table_name, base_table in NON_DACSCAN_TABLE_MAP.items():
            sql = build_non_dacscan_freshness_query(table_name, base_table)
            assert base_table in sql


class TestBuildBiLoaderQuery:

    def test_basic(self):
        sql = build_bi_loader_freshness_query(
            "BI_FACT_RESALE_ORDER_POSTING",
            "presaledb_bi_fact_resale_order_posting",
            "ord_dt",
        )
        assert "max(ord_dt)" in sql
        assert "presaledb_bi_fact_resale_order_posting" in sql

    def test_all_bi_loader_tables(self):
        for table_name, (base_table, date_col) in BI_LOADER_TABLE_MAP.items():
            sql = build_bi_loader_freshness_query(table_name, base_table, date_col)
            assert f"max({date_col})" in sql


# ===========================================================================
# Tests: DatabricksSQL._parse_result
# ===========================================================================


class TestParseResult:

    def test_normal_result(self):
        client = DatabricksSQL.__new__(DatabricksSQL)
        result = {
            "manifest": {
                "schema": {
                    "columns": [
                        {"name": "GroupName"},
                        {"name": "Met"},
                    ]
                }
            },
            "result": {
                "data_array": [
                    ["DACSCAN", "Yes"],
                    ["AGG", "No"],
                ]
            },
        }
        rows = client._parse_result(result)
        assert len(rows) == 2
        assert rows[0] == {"GroupName": "DACSCAN", "Met": "Yes"}
        assert rows[1] == {"GroupName": "AGG", "Met": "No"}

    def test_empty_result(self):
        client = DatabricksSQL.__new__(DatabricksSQL)
        result = {
            "manifest": {"schema": {"columns": [{"name": "col1"}]}},
            "result": {"data_array": []},
        }
        rows = client._parse_result(result)
        assert rows == []

    def test_missing_keys(self):
        client = DatabricksSQL.__new__(DatabricksSQL)
        result = {}
        rows = client._parse_result(result)
        assert rows == []


# ===========================================================================
# Tests: format functions
# ===========================================================================


class TestFormatTable:

    def test_basic_output(self):
        rows = [_make_row(met="Yes"), _make_row(table_name="TKT", met="No", comments="Delayed")]
        output = format_table(rows)
        assert "SALES_ORD" in output
        assert "TKT" in output
        assert "Delayed" in output
        # Should have table borders
        assert "+-" in output

    def test_with_granular_results(self):
        rows = [_make_row(table_name="TKT", met="No", comments="Delayed")]
        granular = [GranularResult("TKT", "host-level", "hosts=52/52", True)]
        output = format_table(rows, granular)
        assert "Fresh" in output
        assert "hosts=52/52" in output

    def test_empty_rows(self):
        output = format_table([])
        assert "GroupName" in output  # headers still present


class TestFormatCsv:

    def test_basic(self):
        rows = [_make_row(), _make_row(table_name="TKT")]
        output = format_csv(rows)
        lines = output.strip().split("\n")
        assert len(lines) == 3  # header + 2 data rows
        assert "GroupName" in lines[0]
        assert "SALES_ORD" in lines[1]
        assert "TKT" in lines[2]


class TestFormatJson:

    def test_basic(self):
        import json
        rows = [_make_row()]
        output = format_json(rows)
        data = json.loads(output)
        assert len(data) == 1
        assert data[0]["table_name"] == "SALES_ORD"
        assert data[0]["met"] == "Yes"


# ===========================================================================
# Tests: DataFreshnessChecker
# ===========================================================================


class TestGetDelayedTables:

    def test_filters_delayed(self):
        checker = _make_checker()
        rows = [
            _make_row(table_name="SALES_ORD", met="Yes"),
            _make_row(table_name="TKT", met="No"),
            _make_row(table_name="AUDIT_STAR", met="No"),
        ]
        delayed = checker.get_delayed_tables(rows)
        assert len(delayed) == 2
        assert all(r.met.strip().lower() == "no" for r in delayed)

    def test_none_delayed(self):
        checker = _make_checker()
        rows = [_make_row(met="Yes"), _make_row(met="Yes")]
        assert checker.get_delayed_tables(rows) == []

    def test_all_delayed(self):
        checker = _make_checker()
        rows = [_make_row(met="No"), _make_row(met="No")]
        assert len(checker.get_delayed_tables(rows)) == 2


class TestRunMainReport:

    def test_parses_rows(self):
        checker = _make_checker()
        checker.db.execute.return_value = [
            {
                "GroupName": "DACSCAN", "DBName": "PFOCUSVW",
                "TableName": "SALES_ORD", "DataDate": "2026-03-10",
                "SLA": "11:00 AM PDT", "Met": "Yes", "Comments": " ",
            },
            {
                "GroupName": "AGG", "DBName": "PFOCUSVW",
                "TableName": "AUDIT_STAR", "DataDate": "2026-03-09",
                "SLA": "11:00 AM PDT", "Met": "No", "Comments": "Delayed",
            },
        ]
        rows = checker.run_main_report()
        assert len(rows) == 2
        assert isinstance(rows[0], FreshnessRow)
        assert rows[0].table_name == "SALES_ORD"
        assert rows[1].met == "No"

    def test_empty_result(self):
        checker = _make_checker()
        checker.db.execute.return_value = []
        rows = checker.run_main_report()
        assert rows == []

    def test_api_error_propagates(self):
        checker = _make_checker()
        checker.db.execute.side_effect = DatabricksAPIError("timeout")
        with pytest.raises(DatabricksAPIError, match="timeout"):
            checker.run_main_report()


class TestCheckDacscanTable:

    @patch("data_freshness._is_fresh_date", return_value=True)
    def test_standard_fresh(self, _mock_fresh):
        checker = _make_checker()
        checker.db.execute.return_value = [
            {"host_sys_cd": "52", "filedate": "2026-03-10"},
        ]
        result = checker._check_dacscan_table("SALES_ORD")
        assert result.is_actually_fresh is True
        assert result.check_type == "host-level"
        assert "52/52" in result.detail

    @patch("data_freshness._is_fresh_date", return_value=True)
    def test_standard_delayed_low_hosts(self, _mock_fresh):
        checker = _make_checker()
        checker.db.execute.return_value = [
            {"host_sys_cd": "48", "filedate": "2026-03-10"},
        ]
        result = checker._check_dacscan_table("SALES_ORD")
        # Fresh date but not enough hosts → still delayed
        assert result.is_actually_fresh is False

    @patch("data_freshness._is_fresh_date", return_value=True)
    def test_event_opt_special(self, _mock_fresh):
        checker = _make_checker()
        checker.db.execute.return_value = [
            {"max_update": "2026-03-11 12:00:00", "max_data_dt": "2026-03-10"},
        ]
        result = checker._check_dacscan_table("SALES_ORD_EVENT_OPT")
        assert result.check_type == "update_ts"
        assert result.is_actually_fresh is True
        assert "DSSD-29069" in result.detail

    def test_no_data_returned(self):
        checker = _make_checker()
        checker.db.execute.return_value = []
        result = checker._check_dacscan_table("SALES_ORD")
        assert result.is_actually_fresh is False
        assert "No data" in result.detail


class TestCheckNonDacscanTable:

    @patch("data_freshness._is_fresh_date", return_value=True)
    def test_fresh(self, _mock_fresh):
        checker = _make_checker()
        checker.db.execute.return_value = [
            {"max_update": "2026-03-11 08:00:00"},
        ]
        result = checker._check_non_dacscan_table("AUDIT_STAR")
        assert result.is_actually_fresh is True
        assert result.check_type == "update_ts"

    @patch("data_freshness._is_fresh_date", return_value=False)
    def test_stale(self, _mock_fresh):
        checker = _make_checker()
        checker.db.execute.return_value = [
            {"max_update": "2026-03-08 22:00:00"},
        ]
        result = checker._check_non_dacscan_table("AUDIT_STAR")
        assert result.is_actually_fresh is False

    def test_no_data(self):
        checker = _make_checker()
        checker.db.execute.return_value = []
        result = checker._check_non_dacscan_table("AUDIT_STAR")
        assert result.is_actually_fresh is False


class TestCheckBiLoaderTable:

    @patch("data_freshness._is_fresh_date", return_value=True)
    def test_fresh(self, _mock_fresh):
        checker = _make_checker()
        checker.db.execute.return_value = [
            {"max_data_dt": "2026-03-10", "max_update": "2026-03-11 06:00:00"},
        ]
        result = checker._check_bi_loader_table("BI_FACT_RESALE_ORDER_POSTING")
        assert result.is_actually_fresh is True
        assert result.check_type == "bi-loader"

    def test_no_data(self):
        checker = _make_checker()
        checker.db.execute.return_value = []
        result = checker._check_bi_loader_table("BI_FACT_RESALE_ORDER_POSTING")
        assert result.is_actually_fresh is False


class TestRunGranularChecks:

    @patch("data_freshness._is_fresh_date", return_value=True)
    def test_dacscan_table_dispatched(self, _mock_fresh):
        checker = _make_checker()
        checker.db.execute.return_value = [
            {"host_sys_cd": "52", "filedate": "2026-03-10"},
        ]
        delayed = [_make_row(group_name="DACSCAN", table_name="SALES_ORD", met="No")]
        results = checker.run_granular_checks(delayed)

        assert len(results) == 1
        assert results[0].table_name == "SALES_ORD"

    def test_unknown_table_skipped(self):
        checker = _make_checker()
        delayed = [_make_row(group_name="UNKNOWN", table_name="MYSTERY", met="No")]
        results = checker.run_granular_checks(delayed)
        assert results == []

    def test_api_error_captured(self):
        checker = _make_checker()
        checker.db.execute.side_effect = DatabricksAPIError("connection refused")
        delayed = [_make_row(group_name="DACSCAN", table_name="SALES_ORD", met="No")]
        results = checker.run_granular_checks(delayed)
        assert len(results) == 1
        assert results[0].check_type == "error"
        assert results[0].is_actually_fresh is False

    @patch("data_freshness._is_fresh_date", return_value=True)
    def test_non_dacscan_dispatched(self, _mock_fresh):
        checker = _make_checker()
        checker.db.execute.return_value = [{"max_update": "2026-03-11 08:00:00"}]
        delayed = [_make_row(group_name="AUDIT", table_name="AUDIT_STAR", met="No")]
        results = checker.run_granular_checks(delayed)

        assert len(results) == 1
        assert results[0].check_type == "update_ts"

    @patch("data_freshness._is_fresh_date", return_value=True)
    def test_bi_loader_dispatched(self, _mock_fresh):
        checker = _make_checker()
        checker.db.execute.return_value = [
            {"max_data_dt": "2026-03-10", "max_update": "2026-03-11 06:00:00"},
        ]
        delayed = [_make_row(
            group_name="BI-LOADER",
            table_name="BI_FACT_RESALE_ORDER_POSTING",
            met="No",
        )]
        results = checker.run_granular_checks(delayed)

        assert len(results) == 1
        assert results[0].check_type == "bi-loader"
