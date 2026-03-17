#!/usr/bin/env python3
"""NOC Report Assistant — Google Sheets adapter.

Talks to the Apps Script Web App to read/write the shift report
directly in Google Sheets, without downloading an Excel file.
"""

import os
import sys
import re
import json
import ssl
import argparse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv() -> None:
        pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VERSION = "0.1.0"

SHEETS = ["Night-Shift-NEW", "Day-Shift-NEW"]
TICKET_REGEX = re.compile(r"([A-Z]+-\d+)")
NOTE_REGEX = re.compile(r"\(.*?\)")

STATUS_MAP = {
    "WORK IN PROGRESS": "IN PROGRESS",
}


# ---------------------------------------------------------------------------
# Google Sheets API client (via Apps Script)
# ---------------------------------------------------------------------------
class GSheetClient:
    """HTTP client for the Apps Script Web App."""

    def __init__(self, webapp_url: str, api_key: str) -> None:
        self.webapp_url = webapp_url.rstrip("/")
        self.api_key = api_key

    def read_sheet(self, sheet_name: str) -> Dict[str, Any]:
        """GET — read layout, tickets, and date from a sheet."""
        url = f"{self.webapp_url}?action=read&sheet={sheet_name}&key={self.api_key}"
        return self._get(url)

    def sync_statuses(self, sheet_name: str, updates: List[Dict[str, Any]]) -> Dict[str, Any]:
        """POST — batch update status cells (column E)."""
        return self._post({
            "action": "sync",
            "sheet": sheet_name,
            "key": self.api_key,
            "updates": updates,
        })

    def add_row(self, sheet_name: str, data: Dict[str, str]) -> Dict[str, Any]:
        """POST — insert a ticket row into the TTM section."""
        return self._post({
            "action": "addRow",
            "sheet": sheet_name,
            "key": self.api_key,
            "data": data,
        })

    def start_shift(self, sheet_name: str) -> Dict[str, Any]:
        """POST — full shift handoff."""
        return self._post({
            "action": "startShift",
            "sheet": sheet_name,
            "key": self.api_key,
        })

    # -- HTTP helpers --------------------------------------------------------

    def _get(self, url: str) -> Dict[str, Any]:
        """Send GET request, follow redirects, return parsed JSON."""
        request = Request(url, headers={"Accept": "application/json"})
        ctx = self._ssl_context()
        try:
            with urlopen(request, context=ctx) as response:
                return json.loads(response.read())
        except (URLError, HTTPError) as error:
            raise RuntimeError(f"Apps Script GET failed: {error}") from error

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Send POST request with JSON body, return parsed JSON."""
        data = json.dumps(payload).encode("utf-8")
        request = Request(
            self.webapp_url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        ctx = self._ssl_context()
        try:
            with urlopen(request, context=ctx) as response:
                return json.loads(response.read())
        except (URLError, HTTPError) as error:
            raise RuntimeError(f"Apps Script POST failed: {error}") from error

    @staticmethod
    def _ssl_context() -> ssl.SSLContext:
        return ssl.create_default_context()


# ---------------------------------------------------------------------------
# Jira API (reused from noc_report_assistant.py)
# ---------------------------------------------------------------------------
class JiraClient:
    """Minimal Jira REST client."""

    def __init__(self, jira_url: str, jira_token: str, verbose: bool = False) -> None:
        self.jira_url = jira_url.rstrip("/")
        self.jira_token = jira_token
        self.verbose = verbose

    def fetch_status(self, ticket_id: str) -> Tuple[Optional[str], Optional[str]]:
        """Returns (status, assignee) or (None, None) on failure."""
        url = f"{self.jira_url}/rest/api/2/issue/{ticket_id}?fields=status,assignee"
        data = self._get(url)
        if data is None:
            return None, None
        status = data["fields"]["status"]["name"]
        assignee_obj = data["fields"].get("assignee")
        assignee = assignee_obj["displayName"] if assignee_obj else "Unassigned"
        return status, assignee

    def fetch_full(self, ticket_id: str) -> Tuple[str, str, str]:
        """Returns (summary, status, assignee). Raises on failure."""
        url = f"{self.jira_url}/rest/api/2/issue/{ticket_id}?fields=status,assignee,summary"
        data = self._get(url)
        if data is None:
            raise RuntimeError(f"Failed to fetch Jira issue: {ticket_id}")
        summary = data["fields"].get("summary", "")
        status = data["fields"]["status"]["name"]
        assignee_obj = data["fields"].get("assignee")
        assignee = assignee_obj["displayName"] if assignee_obj else "Unassigned"
        return summary, status, assignee

    def _get(self, url: str) -> Optional[Dict[str, Any]]:
        request = Request(url, headers={
            "Authorization": f"Bearer {self.jira_token}",
            "Content-Type": "application/json",
        })
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            with urlopen(request, context=ctx) as response:
                return json.loads(response.read())
        except (URLError, HTTPError, KeyError) as error:
            if self.verbose:
                print(f"  [WARN] Jira request failed ({url}): {error}", file=sys.stderr)
            return None


# ---------------------------------------------------------------------------
# Business logic
# ---------------------------------------------------------------------------

def build_status_string(status: str, assignee: str, old_value: str) -> str:
    """Format: 'STATUS_UPPERCASE Assignee Name (optional note)'."""
    status_upper = status.upper()
    status_upper = STATUS_MAP.get(status_upper, status_upper)
    new_value = f"{status_upper} {assignee}"
    note_match = NOTE_REGEX.search(old_value)
    if note_match:
        new_value = f"{new_value} {note_match.group(0)}"
    return new_value


def do_sync(
    gsheet: GSheetClient,
    jira: JiraClient,
    sheet_name: str,
    dry_run: bool = False,
) -> List[Dict[str, str]]:
    """Read tickets from Google Sheet, fetch Jira statuses, push updates back."""
    print(f"  Reading {sheet_name} from Google Sheets...")
    sheet_data = gsheet.read_sheet(sheet_name)

    if not sheet_data.get("ok"):
        raise RuntimeError(f"Failed to read sheet: {sheet_data}")

    tickets = sheet_data["tickets"]
    print(f"  Found {len(tickets)} tickets")

    updates: List[Dict[str, Any]] = []
    changes: List[Dict[str, str]] = []

    for ticket in tickets:
        ticket_id_match = TICKET_REGEX.search(ticket["ticketId"])
        if not ticket_id_match:
            continue
        ticket_id = ticket_id_match.group(1)

        jira_status, jira_assignee = jira.fetch_status(ticket_id)
        if jira_status is None:
            continue

        old_value = ticket["status"]
        new_value = build_status_string(jira_status, jira_assignee, old_value)
        changed = old_value.strip() != new_value.strip()

        if changed:
            updates.append({"row": ticket["row"], "value": new_value})
            changes.append({
                "row": str(ticket["row"]),
                "ticket_id": ticket_id,
                "old": old_value,
                "new": new_value,
            })

    if updates and not dry_run:
        print(f"  Pushing {len(updates)} status updates to Google Sheets...")
        result = gsheet.sync_statuses(sheet_name, updates)
        if not result.get("ok"):
            raise RuntimeError(f"Sync failed: {result}")
        print(f"  Updated: {result.get('updated', 0)} cells")
    elif dry_run and updates:
        print(f"  [DRY RUN] Would update {len(updates)} cells")
    else:
        print("  All statuses up to date")

    return changes


def do_add_row(
    gsheet: GSheetClient,
    jira: JiraClient,
    sheet_name: str,
    jira_link: str,
    slack_link: str,
    dry_run: bool = False,
) -> Optional[int]:
    """Add a ticket row to the TTM section in Google Sheets."""
    ticket_id_match = TICKET_REGEX.search(jira_link)
    if not ticket_id_match:
        raise ValueError(f"Could not extract ticket ID from: {jira_link}")
    ticket_id = ticket_id_match.group(1)

    summary, jira_status, jira_assignee = jira.fetch_full(ticket_id)
    status_string = build_status_string(jira_status, jira_assignee, "")

    if dry_run:
        print(f"  [DRY RUN] Would insert:")
        print(f"    C: {summary}")
        print(f"    D: {ticket_id} -> {jira_link}")
        print(f"    E: {status_string}")
        print(f"    F: slack_link -> {slack_link}")
        return None

    result = gsheet.add_row(sheet_name, {
        "summary": summary,
        "ticketId": ticket_id,
        "jiraLink": jira_link,
        "status": status_string,
        "slackText": "slack_link",
        "slackLink": slack_link,
    })

    if not result.get("ok"):
        raise RuntimeError(f"Add row failed: {result}")

    inserted_row = result.get("insertedRow")
    print(f"  Row inserted at row {inserted_row}")
    return inserted_row


def do_start_shift(
    gsheet: GSheetClient,
    jira: JiraClient,
    sheet_name: str,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Start-of-shift handoff via Google Sheets."""
    opposite = "Day-Shift-NEW" if sheet_name == "Night-Shift-NEW" else "Night-Shift-NEW"
    print(f"  Source: {opposite}")
    print(f"  Target: {sheet_name}")

    if dry_run:
        source_data = gsheet.read_sheet(opposite)
        ticket_count = len(source_data.get("tickets", []))
        print(f"  [DRY RUN] Would copy {ticket_count} tickets from {opposite}")
        print(f"  [DRY RUN] Would update date and reset TTM")
        return {"tickets_copied": ticket_count, "dry_run": True}

    print("  Running shift handoff on Google Sheets...")
    result = gsheet.start_shift(sheet_name)

    if not result.get("ok"):
        raise RuntimeError(f"Start shift failed: {result}")

    tickets_copied = result.get("ticketsCopied", 0)
    date_day = result.get("dateDay")
    date_month = result.get("dateMonth")
    print(f"  Date   : {date_month} {date_day}")
    print(f"  Copied : {tickets_copied} ticket(s) from {opposite}")

    # Sync Jira statuses after handoff
    print()
    print("  Syncing Jira statuses...")
    changes = do_sync(gsheet, jira, sheet_name)

    return {
        "tickets_copied": tickets_copied,
        "date_day": date_day,
        "date_month": date_month,
        "sync_changes": len(changes),
    }


# ---------------------------------------------------------------------------
# Interactive helpers
# ---------------------------------------------------------------------------

SLACK_REGEX = re.compile(r"https?://[^/]*slack\.com/")
JIRA_LINK_REGEX = re.compile(r"https?://jira\.[^/]+/")


def collect_links() -> Tuple[str, str]:
    """Collect Jira and Slack links from user in any order."""
    jira_link: Optional[str] = None
    slack_link: Optional[str] = None

    while not (jira_link and slack_link):
        remaining: List[str] = []
        if not jira_link:
            remaining.append("Jira")
        if not slack_link:
            remaining.append("Slack")

        link = input(f"Paste a link ({' or '.join(remaining)}): ").strip()
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
    """Interactive sheet selection."""
    print("Select sheet:")
    for index, name in enumerate(SHEETS, 1):
        print(f"  {index}. {name}")
    while True:
        try:
            choice = int(input("Enter number: "))
            if 1 <= choice <= len(SHEETS):
                return SHEETS[choice - 1]
        except (ValueError, EOFError):
            pass
        print(f"  Invalid choice. Enter 1-{len(SHEETS)}.")


def select_action() -> int:
    """Interactive action selection."""
    print("Select action:")
    print("  1. Start shift       \u2014 copy tickets from previous shift, update date, sync")
    print("  2. End shift (SYNC)  \u2014 sync Jira statuses for all existing tickets")
    print("  3. Add row           \u2014 add new ticket row to the report")
    while True:
        try:
            choice = int(input("Enter number: "))
            if 1 <= choice <= 3:
                return choice
        except (ValueError, EOFError):
            pass
        print("  Invalid choice. Enter 1-3.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="NOC Report Assistant — Google Sheets edition"
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
    parent_env = Path(__file__).resolve().parent.parent.parent / ".env"
    if parent_env.exists():
        load_dotenv(parent_env)

    args = parse_args()

    # Required env vars
    webapp_url = os.environ.get("GSHEET_WEBAPP_URL", "").strip()
    api_key = os.environ.get("GSHEET_API_KEY", "").strip()
    jira_url = os.environ.get("JIRA_SERVER_URL", "").strip()
    jira_token = os.environ.get("JIRA_PERSONAL_ACCESS_TOKEN", "").strip()

    missing: List[str] = []
    if not webapp_url:
        missing.append("GSHEET_WEBAPP_URL")
    if not api_key:
        missing.append("GSHEET_API_KEY")
    if not jira_url:
        missing.append("JIRA_SERVER_URL")
    if not jira_token:
        missing.append("JIRA_PERSONAL_ACCESS_TOKEN")
    if missing:
        print(f"[ERROR] Missing env vars: {', '.join(missing)}")
        sys.exit(1)

    gsheet = GSheetClient(webapp_url, api_key)
    jira = JiraClient(jira_url, jira_token, args.verbose)

    # Banner
    print(f"  NOC Report Assistant (Google Sheets) v{VERSION}")
    print()

    sheet_name = select_sheet()
    print(f"  Sheet: {sheet_name}")
    print()

    action = select_action()
    print()

    if action == 1:
        result = do_start_shift(gsheet, jira, sheet_name, args.dry_run)
        if args.dry_run:
            print("\n[DRY RUN] No changes saved.")
        else:
            print(f"\n  Done!")

    elif action == 2:
        changes = do_sync(gsheet, jira, sheet_name, args.dry_run)
        changed_count = len(changes)
        print(f"\n  Changed: {changed_count} tickets")
        if args.dry_run:
            print("[DRY RUN] No changes saved.")
        for change in changes:
            print(f"  Row {change['row']}: {change['ticket_id']}")
            print(f"    - {change['old']}")
            print(f"    + {change['new']}")

    elif action == 3:
        jira_link, slack_link = collect_links()
        ticket_id_match = TICKET_REGEX.search(jira_link)
        if not ticket_id_match:
            print("[ERROR] Could not extract ticket ID from Jira link")
            sys.exit(1)
        print(f"\nAdding {ticket_id_match.group(1)} to {sheet_name}...")
        do_add_row(gsheet, jira, sheet_name, jira_link, slack_link, args.dry_run)
        if not args.dry_run:
            print(f"  Done!")


if __name__ == "__main__":
    main()
