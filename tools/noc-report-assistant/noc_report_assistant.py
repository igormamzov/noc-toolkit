#!/usr/bin/env python3
"""NOC Report Assistant — syncs Jira statuses into End-of-Shift Excel report."""

import os
import sys
import re
import json
import ssl
import argparse
from copy import copy
from pathlib import Path
from dataclasses import dataclass
from typing import List, Optional, Tuple

from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv() -> None:
        pass

try:
    from openpyxl import load_workbook
    from openpyxl.styles import Font as _OpenpyxlFont
    from openpyxl.styles.colors import Color as _OpenpyxlColor
except ImportError:
    print("[ERROR] openpyxl required: pip install openpyxl")
    sys.exit(1)

# Jira-style link color (#0052CC)
_HYPERLINK_COLOR = _OpenpyxlColor(rgb="FF0052CC")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VERSION = "0.1.1"

DEFAULT_REPORT_PATH = "~/Downloads/NOC endshift report.xlsx"
SHEETS = ["Night-Shift-NEW", "Day-Shift-NEW"]

TICKET_START_ROW = 8
TICKET_COLUMN = 4       # D
STATUS_COLUMN = 5       # E

TICKET_REGEX = re.compile(r'([A-Z]+-\d+)')
NOTE_REGEX = re.compile(r'\(.*?\)')
SLACK_REGEX = re.compile(r'https?://[^/]*slack\.com/')
JIRA_LINK_REGEX = re.compile(r'https?://jira\.[^/]+/')

# Jira returns "Work In Progress" — we write "IN PROGRESS"
STATUS_MAP = {
    "WORK IN PROGRESS": "IN PROGRESS",
}

STOP_MARKERS = ("Things to monitor", "Permalinks")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class TicketUpdate:
    """Result of a single ticket status check."""
    row: int
    ticket_id: str
    old_value: str
    new_value: str
    changed: bool


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------
class NOCReportAssistant:
    """Reads an End-of-Shift Excel report, fetches Jira statuses, and updates cells."""

    def __init__(
        self,
        jira_url: str,
        jira_token: str,
        dry_run: bool = False,
        verbose: bool = False,
    ) -> None:
        self.jira_url = jira_url.rstrip("/")
        self.jira_token = jira_token
        self.dry_run = dry_run
        self.verbose = verbose

    # -- public: sync statuses -----------------------------------------------

    def run(self, file_path: Path, sheet_name: str) -> List[TicketUpdate]:
        """Open Excel, walk ticket rows, update statuses, save."""
        workbook = load_workbook(file_path)
        worksheet = workbook[sheet_name]
        updates: List[TicketUpdate] = []

        for row_number in range(TICKET_START_ROW, worksheet.max_row + 1):
            # Stop when we hit a section marker
            cell_a_value = str(worksheet.cell(row=row_number, column=1).value or "")
            if row_number > TICKET_START_ROW + 1 and any(
                marker in cell_a_value for marker in STOP_MARKERS
            ):
                break

            # Extract ticket ID from column D (plain text or =HYPERLINK formula)
            ticket_id = self._extract_ticket_id(
                worksheet.cell(row=row_number, column=TICKET_COLUMN)
            )
            if not ticket_id:
                continue

            # Fetch status from Jira
            jira_status, jira_assignee = self._fetch_jira_status(ticket_id)
            if jira_status is None:
                continue

            # Build new value for column E
            old_value = str(
                worksheet.cell(row=row_number, column=STATUS_COLUMN).value or ""
            )
            new_value = self._build_status_string(jira_status, jira_assignee, old_value)
            changed = old_value.strip() != new_value.strip()

            if not self.dry_run:
                worksheet.cell(row=row_number, column=STATUS_COLUMN).value = new_value

            updates.append(
                TicketUpdate(row_number, ticket_id, old_value, new_value, changed)
            )

        if not self.dry_run:
            workbook.save(file_path)

        return updates

    # -- public: add row -----------------------------------------------------

    def add_row(
        self,
        file_path: Path,
        sheet_name: str,
        jira_link: str,
        slack_link: str,
    ) -> int:
        """Add a ticket to the 'Things to monitor' section.

        - If the 'Things to monitor' row has empty C-F, write directly into it.
        - If already occupied, insert a new row before Permalinks and write there.
        - Expand the 'Things to monitor' A:B merge to cover all ticket rows.
        - Preserve the Permalinks full-width A:F merge.
        - Copy formatting (fill, font, alignment) from the last ticket row above.
        """
        workbook = load_workbook(file_path)
        worksheet = workbook[sheet_name]

        # Find "Things to monitor" row
        ttm_row: Optional[int] = None
        for row_number in range(TICKET_START_ROW + 2, worksheet.max_row + 1):
            cell_a_value = str(worksheet.cell(row=row_number, column=1).value or "")
            if "Things to monitor" in cell_a_value:
                ttm_row = row_number
                break

        if ttm_row is None:
            raise RuntimeError("Could not find 'Things to monitor' section")

        # Find "Permalinks" row (next section after "Things to monitor")
        permalinks_row: Optional[int] = None
        for row_number in range(ttm_row + 1, worksheet.max_row + 1):
            cell_a_value = str(worksheet.cell(row=row_number, column=1).value or "")
            if "Permalinks" in cell_a_value:
                permalinks_row = row_number
                break

        if permalinks_row is None:
            raise RuntimeError("Could not find 'Permalinks' section")

        # Reference row for formatting: last ticket row above "Things to monitor"
        reference_row = ttm_row - 1

        # Determine target row
        ttm_has_data = worksheet.cell(row=ttm_row, column=TICKET_COLUMN).value
        if not ttm_has_data:
            # First ticket — write directly into "Things to monitor" row
            target_row = ttm_row
        else:
            # Insert new row just before Permalinks
            worksheet.insert_rows(permalinks_row)
            target_row = permalinks_row
            new_permalinks_row = permalinks_row + 1

            # insert_rows may duplicate merges onto the new row — remove them
            for merge_range in list(worksheet.merged_cells.ranges):
                if (merge_range.min_row == target_row
                        and merge_range.max_row == target_row
                        and merge_range.max_col > 2):
                    worksheet.merged_cells.ranges.remove(merge_range)

            # Expand "Things to monitor" A:B merge to cover all ticket rows.
            # Remove the old A:B merge that starts at ttm_row.
            for merge_range in list(worksheet.merged_cells.ranges):
                if (merge_range.min_row == ttm_row
                        and merge_range.min_col == 1
                        and merge_range.max_col == 2):
                    worksheet.merged_cells.ranges.remove(merge_range)
                    break
            worksheet.merge_cells(
                start_row=ttm_row, start_column=1,
                end_row=target_row, end_column=2,
            )

            # Fix Permalinks full-width merge (A:F) at its new position.
            # Remove ALL merges on the Permalinks row — insert_rows can leave
            # orphaned partial merges (e.g. C:F from the row that shifted through).
            for merge_range in list(worksheet.merged_cells.ranges):
                if (merge_range.min_row == new_permalinks_row
                        and merge_range.max_row == new_permalinks_row):
                    worksheet.merged_cells.ranges.remove(merge_range)
            worksheet.merge_cells(
                start_row=new_permalinks_row, start_column=1,
                end_row=new_permalinks_row, end_column=6,
            )

            # Remove orphaned hyperlinks inside the Permalinks merge area.
            # insert_rows can duplicate hyperlinks from shifted rows (e.g. dashboard
            # URLs) onto the new Permalinks position, causing load_workbook to fail
            # with "'MergedCell' object attribute 'hyperlink' is read-only".
            _remove_hyperlinks_in_range(
                worksheet, new_permalinks_row, 2, new_permalinks_row, 6,
            )

            # Ensure all permalink rows below Permalinks retain their A:B and C:F
            # merges.  insert_rows can drop merges from the bottom rows.
            _ensure_permalink_merges(worksheet, new_permalinks_row)

        # Extract ticket ID from link
        ticket_id_match = TICKET_REGEX.search(jira_link)
        if not ticket_id_match:
            raise ValueError(f"Could not extract ticket ID from: {jira_link}")
        ticket_id = ticket_id_match.group(1)

        # Fetch full ticket data from Jira
        summary, jira_status, jira_assignee = self._fetch_jira_full(ticket_id)

        # C: summary
        worksheet.cell(row=target_row, column=3).value = summary
        _copy_cell_style(
            worksheet.cell(row=reference_row, column=3),
            worksheet.cell(row=target_row, column=3),
        )

        # D: ticket ID with hyperlink (native Hyperlink, not =HYPERLINK formula)
        cell_d = worksheet.cell(row=target_row, column=4)
        cell_d.value = ticket_id
        cell_d.hyperlink = jira_link
        _copy_cell_style(
            worksheet.cell(row=reference_row, column=4), cell_d,
        )
        _apply_hyperlink_font(cell_d)

        # E: STATUS Assignee
        status_string = self._build_status_string(jira_status, jira_assignee, "")
        worksheet.cell(row=target_row, column=5).value = status_string
        _copy_cell_style(
            worksheet.cell(row=reference_row, column=5),
            worksheet.cell(row=target_row, column=5),
        )

        # F: slack_link with hyperlink (native Hyperlink)
        cell_f = worksheet.cell(row=target_row, column=6)
        cell_f.value = "slack_link"
        cell_f.hyperlink = slack_link
        _copy_cell_style(
            worksheet.cell(row=reference_row, column=6), cell_f,
        )
        _apply_hyperlink_font(cell_f)

        # Columns G+ : copy fill from reference row so inserted rows don't
        # appear as black bars in Numbers/Excel (insert_rows leaves them empty).
        for extra_col in range(7, worksheet.max_column + 1):
            _copy_cell_style(
                worksheet.cell(row=reference_row, column=extra_col),
                worksheet.cell(row=target_row, column=extra_col),
            )

        workbook.save(file_path)
        return target_row

    # -- internal: Jira API --------------------------------------------------

    def _fetch_jira_status(self, ticket_id: str) -> Tuple[Optional[str], Optional[str]]:
        """GET /rest/api/2/issue/{ID}?fields=status,assignee — returns (status, assignee)."""
        url = f"{self.jira_url}/rest/api/2/issue/{ticket_id}?fields=status,assignee"
        data = self._jira_get(url)
        if data is None:
            return None, None

        jira_status = data["fields"]["status"]["name"]
        assignee_object = data["fields"].get("assignee")
        jira_assignee = assignee_object["displayName"] if assignee_object else "Unassigned"
        return jira_status, jira_assignee

    def _fetch_jira_full(self, ticket_id: str) -> Tuple[str, str, str]:
        """GET Jira — returns (summary, status, assignee). Raises on failure."""
        url = f"{self.jira_url}/rest/api/2/issue/{ticket_id}?fields=status,assignee,summary"
        data = self._jira_get(url)
        if data is None:
            raise RuntimeError(f"Failed to fetch Jira issue: {ticket_id}")

        summary = data["fields"].get("summary", "")
        jira_status = data["fields"]["status"]["name"]
        assignee_object = data["fields"].get("assignee")
        jira_assignee = assignee_object["displayName"] if assignee_object else "Unassigned"
        return summary, jira_status, jira_assignee

    def _jira_get(self, url: str) -> Optional[dict]:
        """Low-level Jira GET with Bearer token and SSL verification disabled."""
        request = Request(url, headers={
            "Authorization": f"Bearer {self.jira_token}",
            "Content-Type": "application/json",
        })
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        try:
            with urlopen(request, context=ssl_context) as response:
                return json.loads(response.read())
        except (URLError, HTTPError, KeyError) as error:
            if self.verbose:
                print(f"  [WARN] Jira request failed ({url}): {error}", file=sys.stderr)
            return None

    # -- internal: helpers ---------------------------------------------------

    def _extract_ticket_id(self, cell) -> Optional[str]:
        """Extract ticket ID from cell value (plain text or =HYPERLINK formula)."""
        raw_value = str(cell.value or "").strip()
        match = TICKET_REGEX.search(raw_value)
        return match.group(1) if match else None

    def _build_status_string(
        self, status: str, assignee: str, old_value: str
    ) -> str:
        """Format: 'STATUS_UPPERCASE Assignee Name (optional note)'."""
        status_upper = status.upper()
        # Map verbose Jira statuses to short form
        status_upper = STATUS_MAP.get(status_upper, status_upper)
        new_value = f"{status_upper} {assignee}"
        # Preserve parenthetical notes from old value
        note_match = NOTE_REGEX.search(old_value)
        if note_match:
            new_value = f"{new_value} {note_match.group(0)}"
        return new_value


# ---------------------------------------------------------------------------
# Style helpers
# ---------------------------------------------------------------------------

def _copy_cell_style(source_cell, target_cell) -> None:
    """Copy fill, font, border, alignment and number format from source to target."""
    if source_cell.has_style:
        target_cell.font = copy(source_cell.font)
        target_cell.fill = copy(source_cell.fill)
        target_cell.border = copy(source_cell.border)
        target_cell.alignment = copy(source_cell.alignment)
        target_cell.number_format = source_cell.number_format


def _apply_hyperlink_font(cell) -> None:
    """Override font color to Jira-blue (#0052CC) with single underline for hyperlink cells."""
    old_font = cell.font
    cell.font = _OpenpyxlFont(
        name=old_font.name,
        size=old_font.size,
        bold=old_font.bold,
        italic=old_font.italic,
        underline="single",
        color=_HYPERLINK_COLOR,
        strikethrough=old_font.strikethrough,
        vertAlign=old_font.vertAlign,
    )


def _remove_hyperlinks_in_range(
    worksheet,
    min_row: int, min_col: int,
    max_row: int, max_col: int,
) -> None:
    """Remove hyperlinks whose ref falls within the given cell range.

    This is needed because insert_rows can duplicate hyperlinks from shifted
    rows into merged areas, causing openpyxl to fail on next load_workbook.
    """
    from openpyxl.utils.cell import coordinate_to_tuple

    to_keep = []
    for hyperlink in worksheet._hyperlinks:
        if hyperlink.ref:
            try:
                hl_row, hl_col = coordinate_to_tuple(hyperlink.ref)
                if (min_row <= hl_row <= max_row and min_col <= hl_col <= max_col):
                    continue  # skip — this hyperlink is inside the range
            except (ValueError, TypeError):
                pass
        to_keep.append(hyperlink)
    worksheet._hyperlinks = to_keep


def _ensure_permalink_merges(worksheet, permalinks_row: int) -> None:
    """Ensure every permalink row below Permalinks has A:B and C:F merges.

    insert_rows can silently drop merges from the bottom rows of the sheet.
    This function scans all rows from permalinks_row+1 to the end of data
    and recreates any missing A:B or C:F merges.
    """
    existing_merges: set = set()
    for merge_range in worksheet.merged_cells.ranges:
        if merge_range.min_row == merge_range.max_row:
            existing_merges.add(
                (merge_range.min_row, merge_range.min_col, merge_range.max_col)
            )

    for row_number in range(permalinks_row + 1, worksheet.max_row + 1):
        cell_a_value = worksheet.cell(row=row_number, column=1).value
        cell_c_value = worksheet.cell(row=row_number, column=3).value
        if cell_a_value is None and cell_c_value is None:
            break  # past the last permalink row

        if (row_number, 1, 2) not in existing_merges:
            worksheet.merge_cells(
                start_row=row_number, start_column=1,
                end_row=row_number, end_column=2,
            )
        if (row_number, 3, 6) not in existing_merges:
            worksheet.merge_cells(
                start_row=row_number, start_column=3,
                end_row=row_number, end_column=6,
            )


# ---------------------------------------------------------------------------
# Interactive helpers
# ---------------------------------------------------------------------------

def collect_links() -> Tuple[str, str]:
    """Collect Jira and Slack links from user in any order. Returns (jira_link, slack_link)."""
    jira_link: Optional[str] = None
    slack_link: Optional[str] = None

    while not (jira_link and slack_link):
        remaining_types: List[str] = []
        if not jira_link:
            remaining_types.append("Jira")
        if not slack_link:
            remaining_types.append("Slack")

        prompt_text = f"Paste a link ({' or '.join(remaining_types)}): "
        link = input(prompt_text).strip()

        if not link:
            continue

        if SLACK_REGEX.search(link) and not slack_link:
            slack_link = link
            print("  \u2713 Slack link detected")
        elif JIRA_LINK_REGEX.search(link) and not jira_link:
            ticket_match = TICKET_REGEX.search(link)
            if ticket_match:
                jira_link = link
                print(f"  \u2713 Jira link detected: {ticket_match.group(1)}")
            else:
                print("  \u2717 Could not extract ticket ID from Jira link")
        else:
            print("  \u2717 Unrecognized link, try again")

    return jira_link, slack_link


def select_sheet() -> str:
    """Interactive sheet selection menu. No default — user must pick a number."""
    print("Select sheet:")
    for index, sheet_name in enumerate(SHEETS, 1):
        print(f"  {index}. {sheet_name}")
    while True:
        try:
            choice = int(input("Enter number: "))
            if 1 <= choice <= len(SHEETS):
                return SHEETS[choice - 1]
        except (ValueError, EOFError):
            pass
        print(f"  Invalid choice. Enter 1-{len(SHEETS)}.")


def select_action() -> int:
    """Interactive action selection menu. No default — user must pick a number."""
    print("Select action:")
    print("  1. Sync statuses  \u2014 update Jira statuses for existing tickets")
    print("  2. Add row        \u2014 add new ticket row to the report")
    while True:
        try:
            choice = int(input("Enter number: "))
            if 1 <= choice <= 2:
                return choice
        except (ValueError, EOFError):
            pass
        print("  Invalid choice. Enter 1-2.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="NOC Report Assistant — sync Jira statuses into End-of-Shift Excel report"
    )
    parser.add_argument(
        "--file", "-f",
        help=f"Path to Excel report (default: {DEFAULT_REPORT_PATH})",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Show what would change without saving",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show warnings and debug info",
    )
    return parser.parse_args()


def main() -> None:
    """Entry point."""
    load_dotenv()
    args = parse_args()

    # Required env vars
    jira_url = os.environ.get("JIRA_SERVER_URL", "").strip()
    jira_token = os.environ.get("JIRA_PERSONAL_ACCESS_TOKEN", "").strip()
    missing_vars: List[str] = []
    if not jira_url:
        missing_vars.append("JIRA_SERVER_URL")
    if not jira_token:
        missing_vars.append("JIRA_PERSONAL_ACCESS_TOKEN")
    if missing_vars:
        print(f"[ERROR] Missing env vars: {', '.join(missing_vars)}")
        sys.exit(1)

    # Resolve file path
    file_path = Path(
        args.file or os.environ.get("NOC_REPORT_PATH", DEFAULT_REPORT_PATH)
    )
    file_path = file_path.expanduser()
    if not file_path.exists():
        print(f"[ERROR] File not found: {file_path}")
        sys.exit(1)

    # Banner
    print(f"  NOC Report Assistant v{VERSION}")
    print(f"  File: {file_path.name}")
    print()

    # Sheet selection
    sheet_name = select_sheet()
    print(f"  Sheet: {sheet_name}")
    print()

    # Action selection
    action = select_action()
    print()

    assistant = NOCReportAssistant(jira_url, jira_token, args.dry_run, args.verbose)

    if action == 1:
        # Sync statuses
        updates = assistant.run(file_path, sheet_name)
        changed_count = sum(1 for update in updates if update.changed)
        print(f"\nProcessed: {len(updates)} tickets, Changed: {changed_count}")
        if args.dry_run:
            print("[DRY RUN] No changes saved.")
        for update in updates:
            if update.changed:
                print(f"  Row {update.row}: {update.ticket_id}")
                print(f"    - {update.old_value}")
                print(f"    + {update.new_value}")

    elif action == 2:
        # Add row
        jira_link, slack_link = collect_links()
        ticket_id_match = TICKET_REGEX.search(jira_link)
        if not ticket_id_match:
            print("[ERROR] Could not extract ticket ID from Jira link")
            sys.exit(1)
        ticket_id = ticket_id_match.group(1)
        print(f"\nAdding {ticket_id} to {sheet_name}...")

        if args.dry_run:
            summary, jira_status, jira_assignee = assistant._fetch_jira_full(ticket_id)
            print("  [DRY RUN] Would insert row:")
            print(f"    C: {summary}")
            print(f"    D: {ticket_id} (hyperlink)")
            print(f"    E: {assistant._build_status_string(jira_status, jira_assignee, '')}")
            print(f"    F: slack_link (hyperlink)")
        else:
            inserted_row = assistant.add_row(file_path, sheet_name, jira_link, slack_link)
            print(f"  \u2713 Row inserted at row {inserted_row}")
            print(f"  File saved: {file_path.name}")


if __name__ == "__main__":
    main()
