#!/usr/bin/env python3
"""
Data Freshness Checker — DACSCAN daily data freshness report

Automates the Daily Data Freshness Report (DACSCAN) by querying Databricks SQL
via the Statement Execution REST API. Runs the main 15-row freshness report and
optionally performs granular table-level checks for delayed tables.

SLA Deadline: All tables must show "Yes" (Met) by 9:30 AM PST / 5:30 PM UTC / 2:30 PM ART.
"""

import argparse
import csv
import io
import json
import os
import sys
import time
import warnings
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

warnings.filterwarnings("ignore", message=".*urllib3.*")

try:
    import requests
except ImportError:
    print("Error: Missing 'requests' library. Run: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

try:
    from dotenv import load_dotenv
except ImportError:
    print("Error: Missing 'python-dotenv' library. Run: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

VERSION = "0.1.1"

# ---------------------------------------------------------------------------
# SLA constants
# ---------------------------------------------------------------------------
SLA_HOUR_UTC = 17   # 5:30 PM UTC
SLA_MINUTE_UTC = 30

# ---------------------------------------------------------------------------
# SQL Queries
# ---------------------------------------------------------------------------

MAIN_FRESHNESS_QUERY = """
SELECT
  subject_area_name AS GroupName,
  db_nm AS DBName,
  table_nm AS TableName,
  date(data_dt) AS DataDate,
  '11:00 AM PDT' AS SLA,
  CASE WHEN date(data_dt) >= current_date() - interval 1 day THEN 'Yes' ELSE 'No' END AS Met,
  CASE WHEN date(data_dt) >= current_date() - interval 1 day THEN ' ' ELSE 'Delayed' END AS Comments
FROM dataservices_treasury.treasury_teradata_base.pfocuscntl_meta_load_status
WHERE subject_area_name IN ('AGG','AUDIT','SUMMARY','DACSCAN')
UNION ALL
SELECT 'BI-LOADER','PRESALEDB',UPPER('bi_fact_resale_order_posting'),
  max(ord_dt), '11:00 AM PDT',
  CASE WHEN max(ord_dt) >= current_date() - interval 1 day THEN 'Yes' ELSE 'No' END,
  CASE WHEN max(ord_dt) >= current_date() - interval 1 day THEN ' ' ELSE 'Delayed' END
FROM dataservices_treasury.treasury_teradata_base.presaledb_bi_fact_resale_order_posting
UNION ALL
SELECT 'BI-LOADER','PRESALEDB',UPPER('bi_fact_event_eligibility'),
  max(collection_dt), '11:00 AM PDT',
  CASE WHEN max(collection_dt) >= current_date() - interval 1 day THEN 'Yes' ELSE 'No' END,
  CASE WHEN max(collection_dt) >= current_date() - interval 1 day THEN ' ' ELSE 'Delayed' END
FROM dataservices_treasury.treasury_teradata_base.presaledb_bi_fact_event_eligibility
UNION ALL
SELECT 'BI-LOADER','PRESALEDB',UPPER('bi_fact_listing_eligibility'),
  max(collection_dt), '11:00 AM PDT',
  CASE WHEN max(collection_dt) >= current_date() - interval 1 day THEN 'Yes' ELSE 'No' END,
  CASE WHEN max(collection_dt) >= current_date() - interval 1 day THEN ' ' ELSE 'Delayed' END
FROM dataservices_treasury.treasury_teradata_base.presaledb_bi_fact_listing_eligibility
""".strip()

# DACSCAN tables: report name -> Databricks base table name
DACSCAN_TABLE_MAP: Dict[str, str] = {
    "SALES_ORD": "pfocusdb_sales_ord",
    "SALES_ORD_DELUXE_HDR": "pfocusdb_sales_ord_deluxe_hdr",
    "SALES_ORD_EVENT": "pfocusdb_sales_ord_event",
    "SALES_ORD_EVENT_OPT": "pfocusdb_sales_ord_event_opt",
    "SALES_ORD_EVENT_PMT": "pfocusdb_sales_ord_event_pmt",
    "SALES_ORD_OPT": "pfocusdb_sales_ord_opt",
    "SALES_ORD_TRAN": "pfocusdb_sales_ord_tran",
    "TKT": "pfocusdb_tkt",
}

# Non-DACSCAN aggregate tables: report name -> (Databricks base table, date column for max check)
NON_DACSCAN_TABLE_MAP: Dict[str, str] = {
    "AUDIT_STAR": "pfocusdb_audit_star",
    "SALES_SUMMARY": "pfocusdb_sales_summary",
    "AGGR_EVENT_CAP_DAILY": "pfocusdb_aggr_event_cap_comp_daily",
    "BI_FACT_EVENT_DAILY": "pbidimdb_bi_fact_event_daily",
}

# BI-LOADER tables: report name -> (Databricks base table, date column)
BI_LOADER_TABLE_MAP: Dict[str, tuple] = {
    "BI_FACT_RESALE_ORDER_POSTING": ("presaledb_bi_fact_resale_order_posting", "ord_dt"),
    "BI_FACT_EVENT_ELIGIBILITY": ("presaledb_bi_fact_event_eligibility", "collection_dt"),
    "BI_FACT_LISTING_ELIGIBILITY": ("presaledb_bi_fact_listing_eligibility", "collection_dt"),
}

EXCLUDED_HOST_SYSTEMS = ("TWB", "CH8", "T43")
EXPECTED_HOST_COUNT = 52
DB_SCHEMA = "dataservices_treasury.treasury_teradata_base"


# ---------------------------------------------------------------------------
# Databricks SQL REST API Client
# ---------------------------------------------------------------------------

class DatabricksAPIError(Exception):
    """Raised when a Databricks SQL API call fails."""


class DatabricksSQL:
    """Lightweight client for the Databricks SQL Statement Execution API.

    Uses only the ``requests`` library — no heavy SDK dependencies.
    Reference: https://docs.databricks.com/api/workspace/statementexecution
    """

    POLL_INTERVAL_SECONDS = 2
    DEFAULT_TIMEOUT_SECONDS = 300  # 5 minutes

    def __init__(self, host: str, token: str, warehouse_id: str, verbose: bool = False) -> None:
        self.base_url = f"https://{host.rstrip('/')}/api/2.0/sql/statements"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self.warehouse_id = warehouse_id
        self.verbose = verbose

    def execute(self, sql: str, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> List[Dict[str, Any]]:
        """Execute a SQL statement and return rows as a list of dicts.

        Submits the statement, polls until completion or timeout, and parses
        the columnar result into a list of ``{column_name: value}`` dicts.
        """
        payload = {
            "warehouse_id": self.warehouse_id,
            "statement": sql,
            "wait_timeout": "0s",  # async — we poll ourselves for better control
        }

        if self.verbose:
            print(f"\n  [SQL] Submitting query ({len(sql)} chars)...", file=sys.stderr)

        response = requests.post(self.base_url, headers=self.headers, json=payload, timeout=30)
        if response.status_code != 200:
            raise DatabricksAPIError(
                f"Statement submission failed (HTTP {response.status_code}): {response.text}"
            )

        result = response.json()
        statement_id: str = result.get("statement_id", "")
        status: str = result.get("status", {}).get("state", "UNKNOWN")

        if self.verbose:
            print(f"  [SQL] Statement ID: {statement_id}, initial state: {status}", file=sys.stderr)

        # Poll until terminal state
        deadline = time.monotonic() + timeout
        while status in ("PENDING", "RUNNING"):
            if time.monotonic() > deadline:
                self._cancel(statement_id)
                raise DatabricksAPIError(f"Query timed out after {timeout}s (statement {statement_id})")

            time.sleep(self.POLL_INTERVAL_SECONDS)

            poll_response = requests.get(
                f"{self.base_url}/{statement_id}",
                headers=self.headers,
                timeout=30,
            )
            if poll_response.status_code != 200:
                raise DatabricksAPIError(
                    f"Poll failed (HTTP {poll_response.status_code}): {poll_response.text}"
                )
            result = poll_response.json()
            status = result.get("status", {}).get("state", "UNKNOWN")

            if self.verbose:
                print(f"  [SQL] Polling... state: {status}", file=sys.stderr)

        if status == "FAILED":
            error_message = result.get("status", {}).get("error", {}).get("message", "Unknown error")
            raise DatabricksAPIError(f"Query failed: {error_message}")

        if status == "CANCELED":
            raise DatabricksAPIError("Query was canceled")

        if status != "SUCCEEDED":
            raise DatabricksAPIError(f"Unexpected query state: {status}")

        return self._parse_result(result)

    def _parse_result(self, result: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Convert Databricks columnar response into a list of row dicts."""
        manifest = result.get("manifest", {})
        columns = [col["name"] for col in manifest.get("schema", {}).get("columns", [])]
        data_array = result.get("result", {}).get("data_array", [])

        rows: List[Dict[str, Any]] = []
        for row_values in data_array:
            rows.append(dict(zip(columns, row_values)))
        return rows

    def _cancel(self, statement_id: str) -> None:
        """Best-effort cancel of a running statement."""
        try:
            requests.post(
                f"{self.base_url}/{statement_id}/cancel",
                headers=self.headers,
                timeout=10,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Granular query builders
# ---------------------------------------------------------------------------

def build_dacscan_granular_query(table_name: str, base_table: str) -> str:
    """Build the host-level granular check query for a DACSCAN table.

    For SALES_ORD_EVENT_OPT uses a simpler max(update_ts) check due to
    known issue DSSD-29069: this table never reaches 52 hosts/day.
    """
    if table_name == "SALES_ORD_EVENT_OPT":
        return f"""
SELECT 'DACSCAN' AS grp, '{table_name}' AS tbl,
  max(update_ts) AS max_update, max(host_acct_create_dt) AS max_data_dt
FROM {DB_SCHEMA}.{base_table}
WHERE update_ts >= current_date() - interval 7 day
""".strip()

    excluded_csv = ", ".join(f"'{h}'" for h in EXCLUDED_HOST_SYSTEMS)
    return f"""
SELECT
  subject_area_name, table_nm, db_nm,
  count(distinct host_sys_cd) AS host_sys_cd,
  min(file_date) AS filedate
FROM (
  SELECT 'DACSCAN' AS subject_area_name,
    '{table_name}' AS table_nm,
    'PFOCUSVW' AS db_nm,
    p.host_sys_cd AS host_sys_cd,
    (CASE WHEN max(p.host_acct_create_dt) > min(m.min_missing_date)
          THEN min(m.min_missing_date) ELSE max(p.host_acct_create_dt) END) AS file_date,
    'LOCAL' AS data_date_timezone,
    max(p.update_ts) AS table_max_td_update_ts,
    '{DB_SCHEMA}.{base_table}' AS db_table_nm,
    current_timestamp() AS insert_ts,
    current_timestamp() AS update_ts
  FROM {DB_SCHEMA}.{base_table} p
  LEFT JOIN (
    SELECT host_sys_cd, min(date_trunc('dd', host_acct_create_dt)) AS min_missing_date
    FROM {DB_SCHEMA}.{base_table}
    WHERE host_acct_create_dt BETWEEN current_date - 14 AND current_date
      AND host_sys_cd NOT IN ({excluded_csv})
      AND EXISTS (
        SELECT 1 FROM (
          SELECT date_trunc('dd', host_acct_create_dt) AS filedate
          FROM {DB_SCHEMA}.{base_table}
          WHERE host_acct_create_dt BETWEEN current_date - 14 AND current_date
            AND host_sys_cd NOT IN ({excluded_csv})
          GROUP BY 1 HAVING count(distinct host_sys_cd) <> {EXPECTED_HOST_COUNT}
        ) AS missing_files
        WHERE date_trunc('dd', host_acct_create_dt) = missing_files.filedate
      )
    GROUP BY host_sys_cd
  ) m ON upper(trim(p.host_sys_cd)) = upper(trim(m.host_sys_cd))
  WHERE p.host_acct_create_dt BETWEEN current_date - 14 AND current_date
    AND p.host_sys_cd NOT IN ({excluded_csv})
  GROUP BY p.host_sys_cd
) AS a
GROUP BY subject_area_name, table_nm, db_nm
""".strip()


def build_non_dacscan_freshness_query(table_name: str, base_table: str) -> str:
    """Build a simple max(update_ts) freshness check for aggregate tables."""
    return f"""
SELECT '{table_name}' AS tbl,
  max(update_ts) AS max_update, max(insert_ts) AS max_insert
FROM {DB_SCHEMA}.{base_table}
WHERE update_ts >= current_date() - interval 7 day
""".strip()


def build_bi_loader_freshness_query(table_name: str, base_table: str, date_column: str) -> str:
    """Build a freshness check for BI-LOADER tables."""
    return f"""
SELECT '{table_name}' AS tbl,
  max({date_column}) AS max_data_dt, max(update_ts) AS max_update
FROM {DB_SCHEMA}.{base_table}
WHERE update_ts >= current_date() - interval 7 day
""".strip()


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

@dataclass
class FreshnessRow:
    """One row from the main freshness report."""
    group_name: str
    db_name: str
    table_name: str
    data_date: str
    sla: str
    met: str
    comments: str


@dataclass
class GranularResult:
    """Result of a granular table-level check."""
    table_name: str
    check_type: str          # "host-level", "update_ts", "bi-loader"
    detail: str              # human-readable detail string
    is_actually_fresh: bool  # True = data is actually fresh despite report saying Delayed


def format_table(rows: List[FreshnessRow], granular_results: Optional[List[GranularResult]] = None) -> str:
    """Format freshness report as a fixed-width text table for Slack."""
    # Build granular lookup
    granular_map: Dict[str, GranularResult] = {}
    if granular_results:
        for granular_result in granular_results:
            granular_map[granular_result.table_name] = granular_result

    headers = ["GroupName", "DBName", "TableName", "DataDate", "SLA", "Met", "Comments"]
    table_data: List[List[str]] = []

    for row in rows:
        comments = row.comments.strip()
        # Augment comments with granular check results
        if row.table_name in granular_map:
            granular = granular_map[row.table_name]
            if granular.is_actually_fresh:
                comments = f"Fresh ({granular.detail})"
            elif comments == "Delayed":
                comments = f"Delayed ({granular.detail})"

        table_data.append([
            row.group_name, row.db_name, row.table_name,
            row.data_date, row.sla, row.met, comments,
        ])

    # Calculate column widths
    widths = [len(header) for header in headers]
    for data_row in table_data:
        for i, cell in enumerate(data_row):
            widths[i] = max(widths[i], len(cell))

    # Build formatted output
    separator = "+-" + "-+-".join("-" * width for width in widths) + "-+"
    header_line = "| " + " | ".join(header.ljust(width) for header, width in zip(headers, widths)) + " |"

    lines = [separator, header_line, separator]
    for data_row in table_data:
        line = "| " + " | ".join(cell.ljust(width) for cell, width in zip(data_row, widths)) + " |"
        lines.append(line)
    lines.append(separator)

    return "\n".join(lines)


def format_csv(rows: List[FreshnessRow]) -> str:
    """Format freshness report as CSV."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["GroupName", "DBName", "TableName", "DataDate", "SLA", "Met", "Comments"])
    for row in rows:
        writer.writerow([row.group_name, row.db_name, row.table_name,
                         row.data_date, row.sla, row.met, row.comments])
    return output.getvalue()


def format_json(rows: List[FreshnessRow]) -> str:
    """Format freshness report as JSON."""
    data = []
    for row in rows:
        data.append({
            "group_name": row.group_name,
            "db_name": row.db_name,
            "table_name": row.table_name,
            "data_date": row.data_date,
            "sla": row.sla,
            "met": row.met,
            "comments": row.comments,
        })
    return json.dumps(data, indent=2)


def format_html(
    rows: List[FreshnessRow],
    granular_results: Optional[List[GranularResult]] = None,
) -> str:
    """Generate a styled HTML report matching the Databricks notebook look."""
    granular_map: Dict[str, GranularResult] = {}
    if granular_results:
        for granular_result in granular_results:
            granular_map[granular_result.table_name] = granular_result

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Build table rows
    table_rows_html = ""
    for i, row in enumerate(rows):
        met_val = row.met.strip()
        comments = row.comments.strip()

        # Determine row status for coloring
        if met_val.lower() == "yes":
            row_class = "met"
        elif row.table_name in granular_map and granular_map[row.table_name].is_actually_fresh:
            row_class = "fresh"
            granular = granular_map[row.table_name]
            comments = f"Metadata lagging — data is fresh ({granular.detail})"
        else:
            row_class = "delayed"
            if row.table_name in granular_map:
                granular = granular_map[row.table_name]
                if comments == "Delayed" or comments == "":
                    comments = f"Delayed ({granular.detail})"

        stripe = " stripe" if i % 2 == 1 else ""
        table_rows_html += f"""      <tr class="{row_class}{stripe}">
        <td>{_html_escape(row.group_name)}</td>
        <td>{_html_escape(row.db_name)}</td>
        <td class="mono">{_html_escape(row.table_name)}</td>
        <td>{_html_escape(row.data_date)}</td>
        <td>{_html_escape(row.sla)}</td>
        <td class="met-cell">{_html_escape(met_val)}</td>
        <td class="comments">{_html_escape(comments)}</td>
      </tr>
"""

    met_count = sum(1 for r in rows if r.met.strip().lower() == "yes")
    total_count = len(rows)
    delayed_count = total_count - met_count
    if delayed_count == 0:
        summary_text = f"All {total_count} tables Met"
        summary_class = "summary-ok"
    else:
        summary_text = f"{met_count}/{total_count} Met, {delayed_count} Delayed"
        summary_class = "summary-warn"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Data Freshness Report — {now_utc}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: #f5f5f5;
    color: #1b1f23;
    padding: 24px;
  }}
  .header {{
    margin-bottom: 16px;
  }}
  .header h1 {{
    font-size: 18px;
    font-weight: 600;
    color: #24292e;
    margin-bottom: 4px;
  }}
  .header .subtitle {{
    font-size: 13px;
    color: #586069;
  }}
  .summary {{
    display: inline-block;
    padding: 6px 14px;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 600;
    margin-bottom: 12px;
  }}
  .summary-ok {{
    background: #dcffe4;
    color: #22863a;
    border: 1px solid #34d058;
  }}
  .summary-warn {{
    background: #fff5b1;
    color: #b08800;
    border: 1px solid #f9c513;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    background: #fff;
    border: 1px solid #e1e4e8;
    border-radius: 6px;
    overflow: hidden;
    font-size: 13px;
  }}
  thead th {{
    background: #f6f8fa;
    border-bottom: 2px solid #e1e4e8;
    padding: 10px 12px;
    text-align: left;
    font-weight: 600;
    color: #24292e;
    white-space: nowrap;
  }}
  tbody td {{
    padding: 8px 12px;
    border-bottom: 1px solid #eaecef;
    vertical-align: top;
  }}
  tr.stripe td {{
    background: #fafbfc;
  }}
  tr.met td {{
  }}
  tr.met .met-cell {{
    color: #22863a;
    font-weight: 600;
  }}
  tr.delayed td {{
    background: #ffeef0;
  }}
  tr.delayed.stripe td {{
    background: #fde0e4;
  }}
  tr.delayed .met-cell {{
    color: #cb2431;
    font-weight: 600;
  }}
  tr.fresh td {{
    background: #fff8c5;
  }}
  tr.fresh.stripe td {{
    background: #fef3b0;
  }}
  tr.fresh .met-cell {{
    color: #b08800;
    font-weight: 600;
  }}
  .mono {{
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    font-size: 12px;
  }}
  .comments {{
    font-size: 12px;
    color: #586069;
    max-width: 420px;
  }}
  .footer {{
    margin-top: 12px;
    font-size: 11px;
    color: #959da5;
  }}
</style>
</head>
<body>
  <div class="header">
    <h1>Daily Databricks Data Freshness Report</h1>
    <div class="subtitle">Generated: {now_utc}</div>
  </div>
  <div class="{summary_class} summary">{summary_text}</div>
  <table>
    <thead>
      <tr>
        <th>GroupName</th>
        <th>DBName</th>
        <th>TableName</th>
        <th>DataDate</th>
        <th>SLA</th>
        <th>Met</th>
        <th>Comments</th>
      </tr>
    </thead>
    <tbody>
{table_rows_html}    </tbody>
  </table>
  <div class="footer">
    NOC Toolkit — Data Freshness Checker v{VERSION}
  </div>
</body>
</html>"""


def _html_escape(text: str) -> str:
    """Escape HTML special characters."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# Main checker
# ---------------------------------------------------------------------------

class DataFreshnessChecker:
    """Runs the DACSCAN freshness report and optional granular checks."""

    def __init__(self, databricks_client: DatabricksSQL, verbose: bool = False) -> None:
        self.db = databricks_client
        self.verbose = verbose

    def run_main_report(self) -> List[FreshnessRow]:
        """Execute the main 15-row freshness report query."""
        print("Running main freshness report (15 tables)...")
        raw_rows = self.db.execute(MAIN_FRESHNESS_QUERY)

        rows: List[FreshnessRow] = []
        for raw_row in raw_rows:
            rows.append(FreshnessRow(
                group_name=str(raw_row.get("GroupName", "")),
                db_name=str(raw_row.get("DBName", "")),
                table_name=str(raw_row.get("TableName", "")),
                data_date=str(raw_row.get("DataDate", "")),
                sla=str(raw_row.get("SLA", "")),
                met=str(raw_row.get("Met", "")),
                comments=str(raw_row.get("Comments", "")),
            ))

        return rows

    def get_delayed_tables(self, rows: List[FreshnessRow]) -> List[FreshnessRow]:
        """Return only rows with Met = 'No' (delayed)."""
        return [row for row in rows if row.met.strip().lower() == "no"]

    def run_granular_checks(self, tables_to_check: List[FreshnessRow]) -> List[GranularResult]:
        """Run granular table-level checks for the specified delayed tables."""
        results: List[GranularResult] = []

        for table_row in tables_to_check:
            table_name = table_row.table_name.strip()
            group_name = table_row.group_name.strip()

            try:
                if group_name == "DACSCAN" and table_name in DACSCAN_TABLE_MAP:
                    result = self._check_dacscan_table(table_name)
                    results.append(result)
                elif group_name in ("AGG", "AUDIT", "SUMMARY") and table_name in NON_DACSCAN_TABLE_MAP:
                    result = self._check_non_dacscan_table(table_name)
                    results.append(result)
                elif group_name == "BI-LOADER" and table_name in BI_LOADER_TABLE_MAP:
                    result = self._check_bi_loader_table(table_name)
                    results.append(result)
                else:
                    if self.verbose:
                        print(f"  [SKIP] No granular check available for {group_name}/{table_name}",
                              file=sys.stderr)
            except DatabricksAPIError as error:
                print(f"  [ERROR] Granular check failed for {table_name}: {error}", file=sys.stderr)
                results.append(GranularResult(
                    table_name=table_name,
                    check_type="error",
                    detail=str(error),
                    is_actually_fresh=False,
                ))

        return results

    def _check_dacscan_table(self, table_name: str) -> GranularResult:
        """Run host-level granular check for a DACSCAN table."""
        base_table = DACSCAN_TABLE_MAP[table_name]
        sql = build_dacscan_granular_query(table_name, base_table)

        if self.verbose:
            print(f"  Checking DACSCAN table: {table_name}...", file=sys.stderr)

        rows = self.db.execute(sql)

        # SALES_ORD_EVENT_OPT: use max(update_ts) fallback
        if table_name == "SALES_ORD_EVENT_OPT":
            if rows:
                max_update = str(rows[0].get("max_update", "N/A"))
                max_data_dt = str(rows[0].get("max_data_dt", "N/A"))
                is_fresh = _is_fresh_date(max_update)
                return GranularResult(
                    table_name=table_name,
                    check_type="update_ts",
                    detail=f"max_update={max_update}, max_data_dt={max_data_dt} (DSSD-29069 known issue)",
                    is_actually_fresh=is_fresh,
                )
            return GranularResult(
                table_name=table_name, check_type="update_ts",
                detail="No data returned", is_actually_fresh=False,
            )

        # Standard DACSCAN: check host count and filedate
        if rows:
            host_count = rows[0].get("host_sys_cd", "0")
            filedate = str(rows[0].get("filedate", "N/A"))
            try:
                host_count_int = int(host_count)
            except (ValueError, TypeError):
                host_count_int = 0

            is_fresh = host_count_int >= EXPECTED_HOST_COUNT and _is_fresh_date(filedate)
            return GranularResult(
                table_name=table_name,
                check_type="host-level",
                detail=f"hosts={host_count_int}/{EXPECTED_HOST_COUNT}, filedate={filedate}",
                is_actually_fresh=is_fresh,
            )

        return GranularResult(
            table_name=table_name, check_type="host-level",
            detail="No data returned", is_actually_fresh=False,
        )

    def _check_non_dacscan_table(self, table_name: str) -> GranularResult:
        """Run max(update_ts) check for non-DACSCAN aggregate tables."""
        base_table = NON_DACSCAN_TABLE_MAP[table_name]
        sql = build_non_dacscan_freshness_query(table_name, base_table)

        if self.verbose:
            print(f"  Checking aggregate table: {table_name}...", file=sys.stderr)

        rows = self.db.execute(sql)
        if rows:
            max_update = str(rows[0].get("max_update", "N/A"))
            return GranularResult(
                table_name=table_name,
                check_type="update_ts",
                detail=f"max_update={max_update}",
                is_actually_fresh=_is_fresh_date(max_update),
            )

        return GranularResult(
            table_name=table_name, check_type="update_ts",
            detail="No data returned", is_actually_fresh=False,
        )

    def _check_bi_loader_table(self, table_name: str) -> GranularResult:
        """Run freshness check for BI-LOADER tables."""
        base_table, date_column = BI_LOADER_TABLE_MAP[table_name]
        sql = build_bi_loader_freshness_query(table_name, base_table, date_column)

        if self.verbose:
            print(f"  Checking BI-LOADER table: {table_name}...", file=sys.stderr)

        rows = self.db.execute(sql)
        if rows:
            max_data_dt = str(rows[0].get("max_data_dt", "N/A"))
            max_update = str(rows[0].get("max_update", "N/A"))
            return GranularResult(
                table_name=table_name,
                check_type="bi-loader",
                detail=f"max_data_dt={max_data_dt}, max_update={max_update}",
                is_actually_fresh=_is_fresh_date(max_data_dt),
            )

        return GranularResult(
            table_name=table_name, check_type="bi-loader",
            detail="No data returned", is_actually_fresh=False,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_fresh_date(date_str: str) -> bool:
    """Check if a date string contains today's or yesterday's date (UTC)."""
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday = _yesterday_str()
    return today_str in date_str or yesterday in date_str


def _yesterday_str() -> str:
    """Return yesterday's date as YYYY-MM-DD string (UTC)."""
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)
    return yesterday.strftime("%Y-%m-%d")


def _sla_status() -> str:
    """Return human-readable SLA status based on current UTC time."""
    now = datetime.now(timezone.utc)
    sla_minutes = SLA_HOUR_UTC * 60 + SLA_MINUTE_UTC
    current_minutes = now.hour * 60 + now.minute
    if current_minutes < sla_minutes:
        remaining = sla_minutes - current_minutes
        hours = remaining // 60
        minutes = remaining % 60
        return f"{hours}h {minutes}m until SLA deadline (5:30 PM UTC)"
    else:
        overdue = current_minutes - sla_minutes
        hours = overdue // 60
        minutes = overdue % 60
        return f"SLA deadline PASSED {hours}h {minutes}m ago (5:30 PM UTC)"


def _print_dry_run_queries(check_all: bool) -> None:
    """Print all SQL queries that would be executed without running them."""
    print("=" * 60)
    print("DRY RUN — Queries that would be executed:")
    print("=" * 60)
    print()
    print("--- Query 1: Main Freshness Report ---")
    print(MAIN_FRESHNESS_QUERY)
    print()

    if check_all:
        print("--- Granular Checks (--check-all): ALL tables ---")
    else:
        print("--- Granular Checks: only for Delayed tables ---")

    print()
    print("  DACSCAN tables:")
    for table_name, base_table in DACSCAN_TABLE_MAP.items():
        sql = build_dacscan_granular_query(table_name, base_table)
        print(f"\n  -- {table_name} --")
        print(f"  {sql[:120]}...")
    print()

    print("  Non-DACSCAN aggregate tables:")
    for table_name, base_table in NON_DACSCAN_TABLE_MAP.items():
        sql = build_non_dacscan_freshness_query(table_name, base_table)
        print(f"\n  -- {table_name} --")
        print(f"  {sql}")
    print()

    print("  BI-LOADER tables:")
    for table_name, (base_table, date_col) in BI_LOADER_TABLE_MAP.items():
        sql = build_bi_loader_freshness_query(table_name, base_table, date_col)
        print(f"\n  -- {table_name} --")
        print(f"  {sql}")
    print()
    print("=" * 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="DACSCAN Data Freshness Report — checks 15 tables against Databricks SQL",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python data_freshness.py                  # Run report, check delayed tables only
  python data_freshness.py --check-all      # Run report + granular checks for ALL tables
  python data_freshness.py --dry-run        # Show SQL queries without executing
  python data_freshness.py --format csv     # Output as CSV
  python data_freshness.py -v               # Verbose mode (show API calls)
""",
    )
    parser.add_argument("--check-all", action="store_true",
                        help="Run granular checks for ALL tables, not just delayed ones")
    parser.add_argument("--dry-run", "-n", action="store_true",
                        help="Show SQL queries without executing them")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show SQL queries and API responses")
    parser.add_argument("--format", "-f", choices=["table", "csv", "json"], default="table",
                        help="Output format (default: table)")
    parser.add_argument("--report", "-r", action="store_true",
                        help="Generate HTML report and open in browser")
    return parser.parse_args()


def main() -> None:
    """Main entry point."""
    load_dotenv()
    args = parse_args()

    print()
    print("=" * 60)
    print(f"  Data Freshness Checker v{VERSION}")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"  SLA: {_sla_status()}")
    print("=" * 60)
    print()

    # Dry run — just show queries
    if args.dry_run:
        _print_dry_run_queries(args.check_all)
        return

    # Validate environment
    databricks_host = os.environ.get("DATABRICKS_HOST", "").strip()
    databricks_token = os.environ.get("DATABRICKS_TOKEN", "").strip()
    warehouse_id = os.environ.get("DATABRICKS_WAREHOUSE_ID", "").strip()

    missing_vars: List[str] = []
    if not databricks_host:
        missing_vars.append("DATABRICKS_HOST")
    if not databricks_token:
        missing_vars.append("DATABRICKS_TOKEN")
    if not warehouse_id:
        missing_vars.append("DATABRICKS_WAREHOUSE_ID")

    if missing_vars:
        print(f"Error: Missing required environment variables: {', '.join(missing_vars)}", file=sys.stderr)
        print("", file=sys.stderr)
        print("Configure in .env file:", file=sys.stderr)
        print("  DATABRICKS_HOST=ticketmaster-cds-analytics.cloud.databricks.com", file=sys.stderr)
        print("  DATABRICKS_TOKEN=your_personal_access_token", file=sys.stderr)
        print("  DATABRICKS_WAREHOUSE_ID=your_warehouse_id", file=sys.stderr)
        sys.exit(1)

    # Initialize client
    databricks_client = DatabricksSQL(
        host=databricks_host,
        token=databricks_token,
        warehouse_id=warehouse_id,
        verbose=args.verbose,
    )
    checker = DataFreshnessChecker(databricks_client, verbose=args.verbose)

    # Step 1: Run main report
    try:
        freshness_rows = checker.run_main_report()
    except DatabricksAPIError as error:
        print(f"Error: Failed to run main report: {error}", file=sys.stderr)
        sys.exit(1)

    if not freshness_rows:
        print("Warning: Main report returned no rows.", file=sys.stderr)
        sys.exit(1)

    # Step 2: Identify delayed tables
    delayed_rows = checker.get_delayed_tables(freshness_rows)
    tables_to_check = freshness_rows if args.check_all else delayed_rows

    # Step 3: Run granular checks if needed
    granular_results: Optional[List[GranularResult]] = None
    if tables_to_check:
        check_label = "all" if args.check_all else f"{len(delayed_rows)} delayed"
        print(f"Running granular checks for {check_label} table(s)...")
        granular_results = checker.run_granular_checks(tables_to_check)

    # Step 4: Output results
    print()
    if args.format == "csv":
        print(format_csv(freshness_rows))
    elif args.format == "json":
        print(format_json(freshness_rows))
    else:
        print(format_table(freshness_rows, granular_results))

    # Step 5: Summary
    met_count = sum(1 for row in freshness_rows if row.met.strip().lower() == "yes")
    total_count = len(freshness_rows)
    delayed_count = total_count - met_count

    print()
    if delayed_count == 0:
        print(f"  ALL {total_count} tables MET SLA")
    else:
        print(f"  {met_count}/{total_count} tables Met, {delayed_count} DELAYED")

        # Show granular results summary if available
        if granular_results:
            actually_fresh = [r for r in granular_results if r.is_actually_fresh]
            if actually_fresh:
                print(f"  {len(actually_fresh)} table(s) show 'Delayed' in metadata but data is actually fresh:")
                for result in actually_fresh:
                    print(f"    - {result.table_name}: {result.detail}")

    print()
    print(f"  SLA: {_sla_status()}")
    print()

    # Step 6: HTML report
    if args.report:
        html_content = format_html(freshness_rows, granular_results)
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        report_path = Path.cwd() / f"freshness-report-{today_str}.html"
        report_path.write_text(html_content, encoding="utf-8")
        print(f"  Report saved: {report_path}")
        webbrowser.open(f"file://{report_path}")
        print(f"  Opened in browser.")


if __name__ == "__main__":
    main()
