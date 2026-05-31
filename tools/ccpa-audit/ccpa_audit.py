#!/usr/bin/env python3
"""
CCPA Audit progress tool.

Pulls the daily CCPA ERASE summary from CDT
(GET /ccpa_audit/{env}/ccpa_request_summary), formats a multi-day progress
block (`May 31, 2026    57% (4 of 7)`) and optionally posts it as a
PagerDuty incident note. Output is intended to be pasted into PD/Slack
reminders so the on-call sees ERASE progress at a glance.

Default window: 3 days. If any of those days is < 100% complete the window
auto-extends one day at a time (up to MAX_WINDOW_DAYS) so the still-running
backlog stays visible.
"""

from __future__ import annotations

import argparse
import os
import sys
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

warnings.filterwarnings("ignore", message="Unverified HTTPS request")

# tools/common and tools/cdt are added to sys.path by the noc-toolkit launcher;
# for direct `python ccpa_audit.py ...` invocation, add them ourselves.
_SCRIPT_DIR = Path(__file__).resolve().parent
_COMMON = _SCRIPT_DIR.parent / "common"
_CDT_DIR = _SCRIPT_DIR.parent / "cdt"
for _path in (_COMMON, _CDT_DIR, _SCRIPT_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from noc_utils import load_env, require_env, new_pd_client  # noqa: E402
from cdt_client import CDTClient  # noqa: E402

try:
    import pagerduty  # noqa: F401  (re-exported by new_pd_client; needed for error type)
except ImportError as _import_error:
    print(f"Error: missing PagerDuty dependency: {_import_error}", file=sys.stderr)
    sys.exit(1)


VERSION = "0.1.0"

# Default audit window — overridable via --days. We always include "today"
# plus the previous (DEFAULT_WINDOW_DAYS - 1) calendar dates.
DEFAULT_WINDOW_DAYS = 3

# Hard cap on auto-extension when older days are still < 100% complete.
# Keeps the comment from growing without bound on a long backlog.
MAX_WINDOW_DAYS = 14

# CDT API path components
CCPA_ENV_DEFAULT = "prod"
ERASE_REQUEST_TYPE = "privacy_request.ERASE"


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------


def fetch_ccpa_summary(
    cdt_client: CDTClient,
    env_name: str = CCPA_ENV_DEFAULT,
) -> List[Dict[str, Any]]:
    """
    Pull the full CCPA request summary for the given environment.

    Returns the raw list of summary rows. Each row is a dict shaped like::

        {"date": "2026-05-31",
         "request_type": "privacy_request.ERASE",
         "total": 7, "completed": 4,
         "min_insert_ts": "2026-05-31T02:41:00"}

    The endpoint returns rows for ALL request types and all dates the API has
    on hand; filtering down to ERASE / target dates is the caller's job.
    """
    return cdt_client.get(f"/ccpa_audit/{env_name}/ccpa_request_summary")


def index_summary_by_date_and_type(
    summary_rows: List[Dict[str, Any]],
) -> Dict[tuple, Dict[str, Any]]:
    """Build a lookup keyed by (date_str, request_type) → row."""
    return {
        (row.get("date", ""), row.get("request_type", "")): row
        for row in summary_rows
    }


# ---------------------------------------------------------------------------
# Window logic
# ---------------------------------------------------------------------------


def compute_target_dates(
    today_date: datetime,
    base_window_days: int,
    summary_index: Dict[tuple, Dict[str, Any]],
    request_type: str = ERASE_REQUEST_TYPE,
    max_window_days: int = MAX_WINDOW_DAYS,
) -> List[datetime]:
    """
    Pick which calendar dates to include in the progress block.

    Start with `base_window_days` dates ending at `today_date`. Then keep
    walking backwards (up to `max_window_days`) as long as the *oldest* date
    in the window is still < 100% complete — those days are still draining a
    backlog and the on-call wants to see them too.

    Stop conditions:
        - oldest day in current window is 100% (or has no rows)
        - window has reached max_window_days
        - we run out of dates that have data

    Returned list is sorted newest → oldest (to match the visual style of
    the PD note example).
    """
    today = today_date.date()
    window_size = base_window_days

    # Inspect the current oldest day; if it's not yet complete, bump window
    # size by one and check again. We stop *before* exceeding max_window_days
    # so `--days N --max-days N` truly pins the window at N (no +1 surprise).
    while window_size < max_window_days:
        oldest_date_str = (today - timedelta(days=window_size - 1)).strftime("%Y-%m-%d")
        oldest_row = summary_index.get((oldest_date_str, request_type))

        # No data for the oldest day → no point extending further back.
        if oldest_row is None:
            break

        total_for_oldest = oldest_row.get("total", 0) or 0
        completed_for_oldest = oldest_row.get("completed", 0) or 0

        # Oldest is fully complete — done extending.
        if total_for_oldest > 0 and completed_for_oldest >= total_for_oldest:
            break

        # Still draining — pull one more day in and check again.
        window_size += 1

    final_dates = [today - timedelta(days=offset) for offset in range(window_size)]
    return [datetime.combine(d, datetime.min.time()) for d in final_dates]


# ---------------------------------------------------------------------------
# Comment formatting
# ---------------------------------------------------------------------------


def format_progress_line(
    date_label: str,
    completed: int,
    total: int,
) -> str:
    """Render one line of the progress block.

    Layout matches the example Igor showed::

        May 31, 2026    57% (4 of 7)

    The space between the date and percentage is a fixed 4-space gap so
    columns align when pasted into PD/Slack monospace blocks.
    """
    if total <= 0:
        return f"{date_label}    no ERASE rows"
    percent_complete = round(100 * completed / total)
    return f"{date_label}    {percent_complete}% ({completed} of {total})"


def build_comment(
    target_dates: List[datetime],
    summary_index: Dict[tuple, Dict[str, Any]],
    request_type: str = ERASE_REQUEST_TYPE,
) -> str:
    """
    Build the multi-line progress block (no surrounding header/blockquote
    markup — keep it paste-ready for both PD note + Slack reminder).
    """
    lines: List[str] = []
    for target_date in target_dates:
        date_str = target_date.strftime("%Y-%m-%d")
        date_label = target_date.strftime("%b %d, %Y")
        row = summary_index.get((date_str, request_type))
        if row is None:
            lines.append(f"{date_label}    no data")
            continue
        completed = int(row.get("completed", 0) or 0)
        total = int(row.get("total", 0) or 0)
        lines.append(format_progress_line(date_label, completed, total))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# PagerDuty note posting
# ---------------------------------------------------------------------------


def extract_incident_id(incident_input: str) -> str:
    """Accept either a raw PD incident ID or a full incident URL."""
    if "incidents/" in incident_input:
        return incident_input.split("incidents/")[-1].strip("/")
    return incident_input.strip()


def post_pd_note(
    pagerduty_api_token: str,
    incident_id: str,
    note_content: str,
    dry_run: bool = False,
) -> None:
    """
    Add a note to a PagerDuty incident.

    Uses the same client + From-header convention as pd-escalate /
    auto-close, so PD shows the note as authored by the toolkit user.
    """
    if dry_run:
        print(f"  [DRY-RUN] Would POST PD note to incident {incident_id}:")
        for line in note_content.splitlines():
            print(f"    {line}")
        return

    pd_client = new_pd_client(pagerduty_api_token)

    # Resolve the user email so PD attributes the note correctly.
    try:
        user_response = pd_client.rget("users/me")
        user_record = (
            user_response.get("user", user_response)
            if isinstance(user_response, dict)
            else user_response
        )
        from_email = user_record.get("email", "") if isinstance(user_record, dict) else ""
    except pagerduty.Error as error:
        raise RuntimeError(
            f"Failed to resolve current PagerDuty user before posting note: {error}"
        ) from error

    try:
        pd_client.rpost(
            f"incidents/{incident_id}/notes",
            json={"note": {"content": note_content}},
            headers={"From": from_email},
        )
    except pagerduty.Error as error:
        raise RuntimeError(
            f"Failed to post PD note to incident {incident_id}: {error}"
        ) from error

    print(f"  PD note posted to incident {incident_id}.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_today_argument(today_argument: Optional[str]) -> datetime:
    """Resolve --today YYYY-MM-DD (or default to system today, UTC)."""
    if not today_argument:
        return datetime.utcnow()
    try:
        return datetime.strptime(today_argument, "%Y-%m-%d")
    except ValueError as error:
        raise SystemExit(
            f"Invalid --today value '{today_argument}': expected YYYY-MM-DD."
        ) from error


def main() -> None:
    """Entry point for the CCPA Audit progress tool."""
    load_env()

    parser = argparse.ArgumentParser(
        prog="ccpa_audit.py",
        description=(
            "Render a CCPA ERASE progress block for the last N days "
            "and optionally post it as a PagerDuty incident note."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s                         # 3-day block to stdout\n"
            "  %(prog)s --days 7                # widen the window\n"
            "  %(prog)s --pd Q1WPEMZKLQZGJF     # also post as PD note\n"
            "  %(prog)s --today 2026-05-31      # pin 'today' for repro\n"
            "  %(prog)s --pd ... --dry-run      # preview the PD note\n"
        ),
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_WINDOW_DAYS,
        help=(
            f"Base window size in days (default {DEFAULT_WINDOW_DAYS}). "
            f"Window auto-extends up to {MAX_WINDOW_DAYS} days while the "
            "oldest day is still < 100%% complete."
        ),
    )
    parser.add_argument(
        "--max-days",
        type=int,
        default=MAX_WINDOW_DAYS,
        help=(
            f"Hard cap on auto-extended window size (default {MAX_WINDOW_DAYS})."
        ),
    )
    parser.add_argument(
        "--today",
        default=None,
        help="Override 'today' as YYYY-MM-DD (default: system UTC today).",
    )
    parser.add_argument(
        "--env",
        default=CCPA_ENV_DEFAULT,
        help=f"CDT environment path segment (default '{CCPA_ENV_DEFAULT}').",
    )
    parser.add_argument(
        "--pd",
        default=None,
        help="PagerDuty incident ID or URL — also post the block as a PD note.",
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Print what would happen but skip the PD POST.",
    )
    parser.add_argument(
        "--version",
        "-v",
        action="version",
        version=f"%(prog)s {VERSION}",
    )

    args = parser.parse_args()

    if args.days < 1:
        parser.error("--days must be >= 1")
    if args.max_days < args.days:
        parser.error("--max-days must be >= --days")

    today_dt = parse_today_argument(args.today)

    # CDT auth — surfaced consistently with auto-close / scheduled-recheck.
    cdt_client = CDTClient()

    summary_rows = fetch_ccpa_summary(cdt_client, env_name=args.env)
    summary_index = index_summary_by_date_and_type(summary_rows)

    target_dates = compute_target_dates(
        today_date=today_dt,
        base_window_days=args.days,
        summary_index=summary_index,
        max_window_days=args.max_days,
    )

    progress_block = build_comment(target_dates, summary_index)

    print(progress_block)

    if args.pd:
        # Only require the PD token if the user actually asked us to post.
        env_values = require_env("PAGERDUTY_API_TOKEN")
        incident_id = extract_incident_id(args.pd)
        post_pd_note(
            pagerduty_api_token=env_values["PAGERDUTY_API_TOKEN"],
            incident_id=incident_id,
            note_content=progress_block,
            dry_run=args.dry_run,
        )


if __name__ == "__main__":
    main()
