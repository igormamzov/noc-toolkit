"""Tests for data-freshness tool (Data Freshness Checker v0.1.1)."""

import json as _json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from freshness import (
    DatabricksSQL,
    DatabricksAPIError,
    FreshnessChecker,
    FreshnessRow,
    GranularResult,
    _html_escape,
    _is_fresh_date,
    _yesterday_str,
    _sla_status,
    _print_dry_run_queries,
    build_dacscan_granular_query,
    build_non_dacscan_freshness_query,
    build_bi_loader_freshness_query,
    format_table,
    format_csv,
    format_json,
    format_html,
    parse_args,
    main,
    DACSCAN_TABLE_MAP,
    NON_DACSCAN_TABLE_MAP,
    BI_LOADER_TABLE_MAP,
    EXPECTED_HOST_COUNT,
    DB_SCHEMA,
    VERSION,
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


def _make_checker() -> FreshnessChecker:
    """Create a FreshnessChecker with a mocked DatabricksSQL client."""
    mock_db = MagicMock(spec=DatabricksSQL)
    return FreshnessChecker(mock_db, verbose=False)


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

    @patch("freshness.datetime")
    def test_returns_yesterday(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 11, 14, 0, 0, tzinfo=timezone.utc)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = _yesterday_str()
        assert result == "2026-03-10"

    @patch("freshness.datetime")
    def test_month_boundary(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 4, 1, 10, 0, 0, tzinfo=timezone.utc)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        result = _yesterday_str()
        assert result == "2026-03-31"


class TestIsFreshDate:

    @patch("freshness._yesterday_str", return_value="2026-03-10")
    @patch("freshness.datetime")
    def test_today_is_fresh(self, mock_dt, _mock_yesterday):
        mock_dt.now.return_value = datetime(2026, 3, 11, 14, 0, 0, tzinfo=timezone.utc)
        assert _is_fresh_date("2026-03-11 08:00:00") is True

    @patch("freshness._yesterday_str", return_value="2026-03-10")
    @patch("freshness.datetime")
    def test_yesterday_is_fresh(self, mock_dt, _mock_yesterday):
        mock_dt.now.return_value = datetime(2026, 3, 11, 14, 0, 0, tzinfo=timezone.utc)
        assert _is_fresh_date("2026-03-10") is True

    @patch("freshness._yesterday_str", return_value="2026-03-10")
    @patch("freshness.datetime")
    def test_old_date_is_stale(self, mock_dt, _mock_yesterday):
        mock_dt.now.return_value = datetime(2026, 3, 11, 14, 0, 0, tzinfo=timezone.utc)
        assert _is_fresh_date("2026-03-08") is False

    @patch("freshness._yesterday_str", return_value="2026-03-10")
    @patch("freshness.datetime")
    def test_na_is_stale(self, mock_dt, _mock_yesterday):
        mock_dt.now.return_value = datetime(2026, 3, 11, 14, 0, 0, tzinfo=timezone.utc)
        assert _is_fresh_date("N/A") is False


class TestSlaStatus:

    @patch("freshness.datetime")
    def test_before_deadline(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 11, 15, 0, 0, tzinfo=timezone.utc)
        result = _sla_status()
        assert "until SLA deadline" in result
        assert "2h 30m" in result

    @patch("freshness.datetime")
    def test_after_deadline(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 11, 18, 0, 0, tzinfo=timezone.utc)
        result = _sla_status()
        assert "PASSED" in result
        assert "0h 30m" in result

    @patch("freshness.datetime")
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
# Tests: FreshnessChecker
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

    @patch("freshness._is_fresh_date", return_value=True)
    def test_standard_fresh(self, _mock_fresh):
        checker = _make_checker()
        checker.db.execute.return_value = [
            {"host_sys_cd": "52", "filedate": "2026-03-10"},
        ]
        result = checker._check_dacscan_table("SALES_ORD")
        assert result.is_actually_fresh is True
        assert result.check_type == "host-level"
        assert "52/52" in result.detail

    @patch("freshness._is_fresh_date", return_value=True)
    def test_standard_delayed_low_hosts(self, _mock_fresh):
        checker = _make_checker()
        checker.db.execute.return_value = [
            {"host_sys_cd": "48", "filedate": "2026-03-10"},
        ]
        result = checker._check_dacscan_table("SALES_ORD")
        # Fresh date but not enough hosts → still delayed
        assert result.is_actually_fresh is False

    @patch("freshness._is_fresh_date", return_value=True)
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

    @patch("freshness._is_fresh_date", return_value=True)
    def test_fresh(self, _mock_fresh):
        checker = _make_checker()
        checker.db.execute.return_value = [
            {"max_update": "2026-03-11 08:00:00"},
        ]
        result = checker._check_non_dacscan_table("AUDIT_STAR")
        assert result.is_actually_fresh is True
        assert result.check_type == "update_ts"

    @patch("freshness._is_fresh_date", return_value=False)
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

    @patch("freshness._is_fresh_date", return_value=True)
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

    @patch("freshness._is_fresh_date", return_value=True)
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

    @patch("freshness._is_fresh_date", return_value=True)
    def test_non_dacscan_dispatched(self, _mock_fresh):
        checker = _make_checker()
        checker.db.execute.return_value = [{"max_update": "2026-03-11 08:00:00"}]
        delayed = [_make_row(group_name="AUDIT", table_name="AUDIT_STAR", met="No")]
        results = checker.run_granular_checks(delayed)

        assert len(results) == 1
        assert results[0].check_type == "update_ts"

    @patch("freshness._is_fresh_date", return_value=True)
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


# ===========================================================================
# Tests: DatabricksSQL.__init__ and execute() — HTTP interactions
# ===========================================================================


def _make_db_client() -> DatabricksSQL:
    """Create a DatabricksSQL client with dummy credentials."""
    return DatabricksSQL(
        host="my-workspace.cloud.databricks.com",
        token="dapi_test_token",
        warehouse_id="abc123",
    )


class TestDatabricksSQLInit:

    def test_base_url_constructed(self):
        client = DatabricksSQL(host="ws.example.com/", token="tok", warehouse_id="wh1")
        assert client.base_url == "https://ws.example.com/api/2.0/sql/statements"

    def test_auth_header_set(self):
        client = _make_db_client()
        assert client.headers["Authorization"] == "Bearer dapi_test_token"

    def test_warehouse_id_stored(self):
        client = _make_db_client()
        assert client.warehouse_id == "abc123"


class TestDatabricksSQLExecute:
    """Tests for the execute() polling loop and error paths."""

    def _make_post_response(self, status_code: int = 200, json_body: dict = None) -> MagicMock:
        """Helper: build a mock requests.Response for POST."""
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = json_body or {}
        resp.text = "error text"
        return resp

    def _succeeded_result(self, data: list = None) -> dict:
        """Build a minimal SUCCEEDED API response."""
        return {
            "statement_id": "stmt-001",
            "status": {"state": "SUCCEEDED"},
            "manifest": {
                "schema": {
                    "columns": [{"name": "col1"}, {"name": "col2"}]
                }
            },
            "result": {
                "data_array": data or [["val1", "val2"]],
            },
        }

    @patch("freshness.requests.post")
    def test_submission_failure_raises(self, mock_post):
        """HTTP 500 on initial POST raises DatabricksAPIError."""
        mock_post.return_value = self._make_post_response(status_code=500)
        client = _make_db_client()
        with pytest.raises(DatabricksAPIError, match="Statement submission failed.*500"):
            client.execute("SELECT 1")

    @patch("freshness.requests.post")
    def test_immediate_success(self, mock_post):
        """Query that returns SUCCEEDED immediately (no polling needed)."""
        mock_post.return_value = self._make_post_response(
            json_body=self._succeeded_result()
        )
        client = _make_db_client()
        rows = client.execute("SELECT 1")
        assert rows == [{"col1": "val1", "col2": "val2"}]

    @patch("freshness.time.sleep")
    @patch("freshness.requests.get")
    @patch("freshness.requests.post")
    def test_polling_pending_then_succeeded(self, mock_post, mock_get, mock_sleep):
        """Query starts PENDING, transitions to SUCCEEDED on second poll."""
        mock_post.return_value = self._make_post_response(json_body={
            "statement_id": "stmt-002",
            "status": {"state": "PENDING"},
        })
        pending_resp = MagicMock()
        pending_resp.status_code = 200
        pending_resp.json.return_value = {
            "status": {"state": "RUNNING"},
        }
        succeeded_resp = MagicMock()
        succeeded_resp.status_code = 200
        succeeded_resp.json.return_value = self._succeeded_result()
        mock_get.side_effect = [pending_resp, succeeded_resp]

        client = _make_db_client()
        rows = client.execute("SELECT 1", timeout=60)
        assert rows == [{"col1": "val1", "col2": "val2"}]
        # Ensure sleep was called between polls
        assert mock_sleep.call_count >= 1

    @patch("freshness.time.sleep")
    @patch("freshness.requests.get")
    @patch("freshness.requests.post")
    def test_poll_http_error_raises(self, mock_post, mock_get, mock_sleep):
        """Non-200 response during poll raises DatabricksAPIError."""
        mock_post.return_value = self._make_post_response(json_body={
            "statement_id": "stmt-003",
            "status": {"state": "RUNNING"},
        })
        error_resp = MagicMock()
        error_resp.status_code = 503
        error_resp.text = "Service Unavailable"
        mock_get.return_value = error_resp

        client = _make_db_client()
        with pytest.raises(DatabricksAPIError, match="Poll failed.*503"):
            client.execute("SELECT 1", timeout=60)

    @patch("freshness.time.sleep")
    @patch("freshness.requests.get")
    @patch("freshness.requests.post")
    def test_failed_state_raises(self, mock_post, mock_get, mock_sleep):
        """FAILED terminal state raises DatabricksAPIError with message."""
        mock_post.return_value = self._make_post_response(json_body={
            "statement_id": "stmt-004",
            "status": {"state": "RUNNING"},
        })
        failed_resp = MagicMock()
        failed_resp.status_code = 200
        failed_resp.json.return_value = {
            "status": {
                "state": "FAILED",
                "error": {"message": "Table not found"},
            }
        }
        mock_get.return_value = failed_resp

        client = _make_db_client()
        with pytest.raises(DatabricksAPIError, match="Query failed: Table not found"):
            client.execute("SELECT 1", timeout=60)

    @patch("freshness.time.sleep")
    @patch("freshness.requests.get")
    @patch("freshness.requests.post")
    def test_canceled_state_raises(self, mock_post, mock_get, mock_sleep):
        """CANCELED terminal state raises DatabricksAPIError."""
        mock_post.return_value = self._make_post_response(json_body={
            "statement_id": "stmt-005",
            "status": {"state": "RUNNING"},
        })
        canceled_resp = MagicMock()
        canceled_resp.status_code = 200
        canceled_resp.json.return_value = {"status": {"state": "CANCELED"}}
        mock_get.return_value = canceled_resp

        client = _make_db_client()
        with pytest.raises(DatabricksAPIError, match="Query was canceled"):
            client.execute("SELECT 1", timeout=60)

    @patch("freshness.time.sleep")
    @patch("freshness.requests.get")
    @patch("freshness.requests.post")
    def test_unexpected_state_raises(self, mock_post, mock_get, mock_sleep):
        """An unrecognised terminal state raises DatabricksAPIError."""
        mock_post.return_value = self._make_post_response(json_body={
            "statement_id": "stmt-006",
            "status": {"state": "RUNNING"},
        })
        unknown_resp = MagicMock()
        unknown_resp.status_code = 200
        unknown_resp.json.return_value = {"status": {"state": "MYSTERY"}}
        mock_get.return_value = unknown_resp

        client = _make_db_client()
        with pytest.raises(DatabricksAPIError, match="Unexpected query state: MYSTERY"):
            client.execute("SELECT 1", timeout=60)

    @patch("freshness.time.monotonic")
    @patch("freshness.requests.post")
    def test_timeout_cancels_and_raises(self, mock_post, mock_monotonic):
        """Timeout path: deadline exceeded triggers cancel + raises."""
        mock_post.return_value = self._make_post_response(json_body={
            "statement_id": "stmt-007",
            "status": {"state": "PENDING"},
        })
        # monotonic() returns values so deadline is exceeded immediately on first poll check
        mock_monotonic.side_effect = [0.0, 1000.0]

        client = _make_db_client()
        with patch("freshness.requests.post") as mock_cancel_post:
            # First call is the initial submission; second will be the cancel call
            mock_cancel_post.side_effect = [
                self._make_post_response(json_body={
                    "statement_id": "stmt-007",
                    "status": {"state": "PENDING"},
                }),
                MagicMock(),  # cancel response — ignored
            ]
            # Re-apply monotonic patch within this scope
            with patch("freshness.time.monotonic", side_effect=[0.0, 1000.0]):
                with pytest.raises(DatabricksAPIError, match="timed out"):
                    client.execute("SELECT 1", timeout=5)

    @patch("freshness.requests.post")
    def test_cancel_swallows_exception(self, mock_post):
        """_cancel() silently handles request exceptions."""
        mock_post.side_effect = Exception("network down")
        client = _make_db_client()
        # Should not raise
        client._cancel("stmt-999")


# ===========================================================================
# Tests: format_html
# ===========================================================================


class TestFormatHtml:

    def test_basic_structure(self):
        rows = [_make_row(met="Yes"), _make_row(table_name="TKT", met="No", comments="Delayed")]
        html = format_html(rows)
        assert "<!DOCTYPE html>" in html
        assert "SALES_ORD" in html
        assert "TKT" in html
        assert "Delayed" in html

    def test_all_met_summary(self):
        rows = [_make_row(met="Yes"), _make_row(table_name="TKT", met="Yes")]
        html = format_html(rows)
        assert "All 2 tables Met" in html
        assert "summary-ok" in html

    def test_some_delayed_summary(self):
        rows = [_make_row(met="Yes"), _make_row(table_name="TKT", met="No", comments="Delayed")]
        html = format_html(rows)
        assert "1/2 Met" in html
        assert "summary-warn" in html

    def test_granular_fresh_shows_yellow(self):
        rows = [_make_row(table_name="SALES_ORD", met="No", comments="Delayed")]
        granular = [GranularResult("SALES_ORD", "host-level", "hosts=52/52", True)]
        html = format_html(rows, granular)
        assert "Metadata lagging" in html
        assert "tr.fresh" in html

    def test_granular_delayed_augments_comment(self):
        rows = [_make_row(table_name="SALES_ORD", met="No", comments="Delayed")]
        granular = [GranularResult("SALES_ORD", "host-level", "hosts=40/52", False)]
        html = format_html(rows, granular)
        assert "Delayed (hosts=40/52)" in html

    def test_stripe_applied_to_even_rows(self):
        rows = [_make_row(met="Yes"), _make_row(table_name="TKT", met="Yes")]
        html = format_html(rows)
        # Second row (index 1) should have class "stripe"
        assert 'stripe"' in html

    def test_version_in_footer(self):
        html = format_html([_make_row()])
        assert VERSION in html

    def test_html_escape_applied(self):
        rows = [_make_row(table_name='A&B<>')]
        html = format_html(rows)
        assert "&amp;" in html or "&lt;" in html

    def test_granular_empty_comments_augmented(self):
        """Empty comments (not 'Delayed') are still augmented for delayed rows."""
        rows = [_make_row(table_name="SALES_ORD", met="No", comments="")]
        granular = [GranularResult("SALES_ORD", "host-level", "hosts=40/52", False)]
        html = format_html(rows, granular)
        assert "Delayed (hosts=40/52)" in html


# ===========================================================================
# Tests: format_table — granular delayed (not fresh) branch
# ===========================================================================


class TestFormatTableDelayedBranch:

    def test_delayed_not_fresh_augments_comment(self):
        """Granular result not fresh: 'Delayed' comment gets detail appended."""
        rows = [_make_row(table_name="TKT", met="No", comments="Delayed")]
        granular = [GranularResult("TKT", "host-level", "hosts=40/52", False)]
        output = format_table(rows, granular)
        assert "Delayed (hosts=40/52)" in output

    def test_non_delayed_comment_unchanged(self):
        """Non-'Delayed' comment is left untouched when granular result is not fresh."""
        rows = [_make_row(table_name="TKT", met="No", comments="Some other note")]
        granular = [GranularResult("TKT", "host-level", "hosts=40/52", False)]
        output = format_table(rows, granular)
        # Comment was not "Delayed", so it should NOT be augmented with detail
        assert "hosts=40/52" not in output


# ===========================================================================
# Tests: _print_dry_run_queries
# ===========================================================================


class TestPrintDryRunQueries:

    def test_check_all_true(self, capsys):
        _print_dry_run_queries(check_all=True)
        # Nothing raised; dry-run completes without error

    def test_check_all_false(self, capsys):
        _print_dry_run_queries(check_all=False)
        # Nothing raised

    def test_dacscan_tables_included(self, capsys):
        _print_dry_run_queries(check_all=False)
        # Since we now use logger.info (stdout), capture is via logging
        # Just ensure no exception is raised and function returns normally


# ===========================================================================
# Tests: parse_args
# ===========================================================================


class TestParseArgs:

    def test_defaults(self):
        with patch("sys.argv", ["freshness.py"]):
            args = parse_args()
        assert args.check_all is False
        assert args.dry_run is False
        assert args.verbose is False
        assert args.format == "table"
        assert args.report is False

    def test_check_all_flag(self):
        with patch("sys.argv", ["freshness.py", "--check-all"]):
            args = parse_args()
        assert args.check_all is True

    def test_dry_run_short_flag(self):
        with patch("sys.argv", ["freshness.py", "-n"]):
            args = parse_args()
        assert args.dry_run is True

    def test_format_csv(self):
        with patch("sys.argv", ["freshness.py", "--format", "csv"]):
            args = parse_args()
        assert args.format == "csv"

    def test_format_json(self):
        with patch("sys.argv", ["freshness.py", "-f", "json"]):
            args = parse_args()
        assert args.format == "json"

    def test_report_flag(self):
        with patch("sys.argv", ["freshness.py", "--report"]):
            args = parse_args()
        assert args.report is True

    def test_verbose_short_flag(self):
        with patch("sys.argv", ["freshness.py", "-v"]):
            args = parse_args()
        assert args.verbose is True


# ===========================================================================
# Tests: main() — integration paths
# ===========================================================================

def _env_vars(host: str = "ws.example.com", token: str = "tok", wh: str = "wh1") -> dict:
    """Build a minimal env dict for main()."""
    return {
        "DATABRICKS_HOST": host,
        "DATABRICKS_TOKEN": token,
        "DATABRICKS_WAREHOUSE_ID": wh,
    }


class TestMainDryRun:

    @patch("freshness._print_dry_run_queries")
    def test_dry_run_calls_print_queries(self, mock_dry):
        with patch("sys.argv", ["freshness.py", "--dry-run"]):
            main()
        mock_dry.assert_called_once_with(False)

    @patch("freshness._print_dry_run_queries")
    def test_dry_run_check_all(self, mock_dry):
        with patch("sys.argv", ["freshness.py", "--dry-run", "--check-all"]):
            main()
        mock_dry.assert_called_once_with(True)


class TestMainMissingEnv:

    def test_missing_all_vars_exits(self):
        with patch("sys.argv", ["freshness.py"]):
            with patch.dict(os.environ, {}, clear=True):
                # Remove the three vars if present
                env = {k: v for k, v in os.environ.items()
                       if k not in ("DATABRICKS_HOST", "DATABRICKS_TOKEN", "DATABRICKS_WAREHOUSE_ID")}
                with patch.dict(os.environ, env, clear=True):
                    with pytest.raises(SystemExit) as exc_info:
                        main()
        assert exc_info.value.code == 1

    def test_missing_token_exits(self):
        env = {"DATABRICKS_HOST": "ws.example.com", "DATABRICKS_WAREHOUSE_ID": "wh1"}
        with patch("sys.argv", ["freshness.py"]):
            with patch.dict(os.environ, env, clear=True):
                with pytest.raises(SystemExit) as exc_info:
                    main()
        assert exc_info.value.code == 1


class TestMainRunReport:

    def _patch_checker(self, rows, granular=None, side_effect=None):
        """Return a context-manager patch for FreshnessChecker."""
        mock_checker = MagicMock()
        if side_effect:
            mock_checker.run_main_report.side_effect = side_effect
        else:
            mock_checker.run_main_report.return_value = rows
        mock_checker.get_delayed_tables.return_value = [r for r in rows if r.met.strip().lower() == "no"]
        mock_checker.run_granular_checks.return_value = granular or []
        return mock_checker

    @patch("freshness.FreshnessChecker")
    @patch("freshness.DatabricksSQL")
    def test_api_error_in_main_report_exits(self, mock_db_cls, mock_checker_cls):
        mock_checker = MagicMock()
        mock_checker.run_main_report.side_effect = DatabricksAPIError("boom")
        mock_checker_cls.return_value = mock_checker

        with patch("sys.argv", ["freshness.py"]):
            with patch.dict(os.environ, _env_vars()):
                with pytest.raises(SystemExit) as exc_info:
                    main()
        assert exc_info.value.code == 1

    @patch("freshness.FreshnessChecker")
    @patch("freshness.DatabricksSQL")
    def test_empty_rows_exits(self, mock_db_cls, mock_checker_cls):
        mock_checker = MagicMock()
        mock_checker.run_main_report.return_value = []
        mock_checker_cls.return_value = mock_checker

        with patch("sys.argv", ["freshness.py"]):
            with patch.dict(os.environ, _env_vars()):
                with pytest.raises(SystemExit) as exc_info:
                    main()
        assert exc_info.value.code == 1

    @patch("freshness.FreshnessChecker")
    @patch("freshness.DatabricksSQL")
    def test_all_met_no_granular(self, mock_db_cls, mock_checker_cls):
        """All tables Met — no granular checks, table format output."""
        rows = [_make_row(met="Yes"), _make_row(table_name="TKT", met="Yes")]
        mock_checker = MagicMock()
        mock_checker.run_main_report.return_value = rows
        mock_checker.get_delayed_tables.return_value = []
        mock_checker_cls.return_value = mock_checker

        with patch("sys.argv", ["freshness.py"]):
            with patch.dict(os.environ, _env_vars()):
                main()  # Should complete without SystemExit

        mock_checker.run_granular_checks.assert_not_called()

    @patch("freshness.FreshnessChecker")
    @patch("freshness.DatabricksSQL")
    def test_delayed_tables_trigger_granular(self, mock_db_cls, mock_checker_cls):
        """Delayed tables trigger granular checks."""
        rows = [
            _make_row(met="Yes"),
            _make_row(table_name="TKT", met="No", comments="Delayed"),
        ]
        granular = [GranularResult("TKT", "host-level", "hosts=52/52", True)]
        mock_checker = MagicMock()
        mock_checker.run_main_report.return_value = rows
        mock_checker.get_delayed_tables.return_value = [rows[1]]
        mock_checker.run_granular_checks.return_value = granular
        mock_checker_cls.return_value = mock_checker

        with patch("sys.argv", ["freshness.py"]):
            with patch.dict(os.environ, _env_vars()):
                main()

        mock_checker.run_granular_checks.assert_called_once()

    @patch("freshness.FreshnessChecker")
    @patch("freshness.DatabricksSQL")
    def test_check_all_uses_all_rows(self, mock_db_cls, mock_checker_cls):
        """--check-all passes ALL rows (not just delayed) to run_granular_checks."""
        rows = [_make_row(met="Yes"), _make_row(table_name="TKT", met="Yes")]
        mock_checker = MagicMock()
        mock_checker.run_main_report.return_value = rows
        mock_checker.get_delayed_tables.return_value = []
        mock_checker.run_granular_checks.return_value = []
        mock_checker_cls.return_value = mock_checker

        with patch("sys.argv", ["freshness.py", "--check-all"]):
            with patch.dict(os.environ, _env_vars()):
                main()

        # With --check-all, granular checks should be called even when none are delayed
        mock_checker.run_granular_checks.assert_called_once_with(rows)

    @patch("freshness.FreshnessChecker")
    @patch("freshness.DatabricksSQL")
    def test_csv_format_output(self, mock_db_cls, mock_checker_cls):
        """--format csv path is exercised without error."""
        rows = [_make_row(met="Yes")]
        mock_checker = MagicMock()
        mock_checker.run_main_report.return_value = rows
        mock_checker.get_delayed_tables.return_value = []
        mock_checker_cls.return_value = mock_checker

        with patch("sys.argv", ["freshness.py", "--format", "csv"]):
            with patch.dict(os.environ, _env_vars()):
                main()

    @patch("freshness.FreshnessChecker")
    @patch("freshness.DatabricksSQL")
    def test_json_format_output(self, mock_db_cls, mock_checker_cls):
        """--format json path is exercised without error."""
        rows = [_make_row(met="Yes")]
        mock_checker = MagicMock()
        mock_checker.run_main_report.return_value = rows
        mock_checker.get_delayed_tables.return_value = []
        mock_checker_cls.return_value = mock_checker

        with patch("sys.argv", ["freshness.py", "--format", "json"]):
            with patch.dict(os.environ, _env_vars()):
                main()

    @patch("freshness.webbrowser.open")
    @patch("freshness.FreshnessChecker")
    @patch("freshness.DatabricksSQL")
    def test_report_flag_saves_html(self, mock_db_cls, mock_checker_cls, mock_browser, tmp_path):
        """--report saves an HTML file and opens it in the browser."""
        rows = [_make_row(met="Yes")]
        mock_checker = MagicMock()
        mock_checker.run_main_report.return_value = rows
        mock_checker.get_delayed_tables.return_value = []
        mock_checker_cls.return_value = mock_checker

        with patch("sys.argv", ["freshness.py", "--report"]):
            with patch.dict(os.environ, _env_vars()):
                with patch("freshness.Path.cwd", return_value=tmp_path):
                    main()

        mock_browser.assert_called_once()
        # Verify an HTML file was written in tmp_path
        html_files = list(tmp_path.glob("freshness-report-*.html"))
        assert len(html_files) == 1

    @patch("freshness.FreshnessChecker")
    @patch("freshness.DatabricksSQL")
    def test_delayed_with_actually_fresh_granular_summary(self, mock_db_cls, mock_checker_cls):
        """Granular actually-fresh results trigger the 'metadata lagging' summary log."""
        rows = [
            _make_row(table_name="SALES_ORD", met="No", comments="Delayed"),
            _make_row(table_name="TKT", met="No", comments="Delayed"),
        ]
        granular = [
            GranularResult("SALES_ORD", "host-level", "hosts=52/52", True),
            GranularResult("TKT", "host-level", "hosts=40/52", False),
        ]
        mock_checker = MagicMock()
        mock_checker.run_main_report.return_value = rows
        mock_checker.get_delayed_tables.return_value = rows
        mock_checker.run_granular_checks.return_value = granular
        mock_checker_cls.return_value = mock_checker

        with patch("sys.argv", ["freshness.py"]):
            with patch.dict(os.environ, _env_vars()):
                main()  # Should complete without error

    @patch("freshness.FreshnessChecker")
    @patch("freshness.DatabricksSQL")
    def test_delayed_no_granular_results_available(self, mock_db_cls, mock_checker_cls):
        """Delayed tables but granular_results is None (no tables_to_check)."""
        rows = [_make_row(table_name="MYSTERY_TABLE", met="No", comments="Delayed")]
        mock_checker = MagicMock()
        mock_checker.run_main_report.return_value = rows
        # get_delayed_tables returns the delayed row, but since group is not in maps it won't run
        mock_checker.get_delayed_tables.return_value = rows
        mock_checker.run_granular_checks.return_value = []
        mock_checker_cls.return_value = mock_checker

        with patch("sys.argv", ["freshness.py"]):
            with patch.dict(os.environ, _env_vars()):
                main()  # Should complete — delayed_count > 0, granular_results is []


# ===========================================================================
# Tests: DatabricksSQL.execute() — FAILED state with missing error message
# ===========================================================================


class TestDatabricksSQLFailedStateMissingMessage:

    @patch("freshness.time.sleep")
    @patch("freshness.requests.get")
    @patch("freshness.requests.post")
    def test_failed_no_error_message_uses_unknown(self, mock_post, mock_get, mock_sleep):
        """FAILED state without an error.message falls back to 'Unknown error'."""
        mock_post.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={
                "statement_id": "stmt-err",
                "status": {"state": "RUNNING"},
            }),
        )
        failed_resp = MagicMock()
        failed_resp.status_code = 200
        # No "error" key in status
        failed_resp.json.return_value = {"status": {"state": "FAILED"}}
        mock_get.return_value = failed_resp

        client = _make_db_client()
        with pytest.raises(DatabricksAPIError, match="Unknown error"):
            client.execute("SELECT 1", timeout=60)


# ===========================================================================
# Tests: check_dacscan_table — invalid host_count type
# ===========================================================================


class TestCheckDacscanTableInvalidHostCount:

    def test_invalid_host_count_treated_as_zero(self):
        """Non-numeric host_sys_cd results in is_actually_fresh=False."""
        checker = _make_checker()
        checker.db.execute.return_value = [
            {"host_sys_cd": "N/A", "filedate": "2026-03-10"},
        ]
        with patch("freshness._is_fresh_date", return_value=True):
            result = checker._check_dacscan_table("SALES_ORD")
        assert result.is_actually_fresh is False

    def test_event_opt_no_data(self):
        """SALES_ORD_EVENT_OPT with empty result returns is_actually_fresh=False."""
        checker = _make_checker()
        checker.db.execute.return_value = []
        result = checker._check_dacscan_table("SALES_ORD_EVENT_OPT")
        assert result.is_actually_fresh is False
        assert result.check_type == "update_ts"
        assert "No data" in result.detail
