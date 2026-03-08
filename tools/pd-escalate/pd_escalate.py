#!/usr/bin/env python3
"""
PD Escalation Tool

Automates the post-DSSD-creation escalation workflow:
1. Link DRGN → "is blocked by" → DSSD in Jira
2. Transition DRGN to "Escalated" status
3. Add a PD note with escalation details
4. Print Slack template for #cds-ops-24x7-int
"""

import os
import re
import sys
import warnings
from typing import Any, Dict, Optional

# Version information
VERSION = "0.1.0"

# Suppress pagination warnings from pagerduty package
warnings.filterwarnings('ignore', message='.*lacks a "more" property.*')

try:
    import pagerduty
    from jira import JIRA
    from jira.exceptions import JIRAError
    from dotenv import load_dotenv
except ImportError as import_error:
    print(f"Error: Missing required dependencies. Please run: pip install -r requirements.txt")
    print(f"Details: {import_error}")
    sys.exit(1)

# Regex for detecting DRGN tickets in text (fallback)
DRGN_PATTERN = re.compile(r'\b(DRGN-\d+)\b')

# PagerDuty web UI base URL
PD_BASE_URL = "https://yourcompany.pagerduty.com/incidents"

# Jira web UI base URL — built from JIRA_SERVER_URL env var at runtime
JIRA_BASE_URL = None  # set in main() from os.environ['JIRA_SERVER_URL']

# Jira transition ID for "Escalated" status
ESCALATED_TRANSITION_ID = "51"


def extract_incident_id(incident_input: str) -> str:
    """
    Extract incident ID from URL or return as-is if already an ID.

    Args:
        incident_input: PagerDuty incident URL or ID

    Returns:
        Incident ID string
    """
    if 'incidents/' in incident_input:
        return incident_input.split('incidents/')[-1].strip('/')
    return incident_input.strip()


class EscalateTool:
    """Automates DRGN→DSSD linking, DRGN status transition, and PD note posting."""

    def __init__(
        self,
        pagerduty_api_token: str,
        jira_server_url: str,
        jira_personal_access_token: str,
        dry_run: bool = False,
    ) -> None:
        """
        Initialize with API credentials.

        Args:
            pagerduty_api_token: PagerDuty API token
            jira_server_url: Jira server URL
            jira_personal_access_token: Jira Server/DC PAT
            dry_run: If True, simulate without API mutations
        """
        self.dry_run = dry_run
        self.pd_client = pagerduty.RestApiV2Client(pagerduty_api_token)
        self.jira_client = JIRA(
            server=jira_server_url,
            token_auth=jira_personal_access_token,
        )
        self.user_email: str = ""
        self.user_id: str = ""

    def get_current_user(self) -> None:
        """
        Resolve PD user ID and email from the API token.

        Raises:
            RuntimeError: If unable to fetch current user information.
        """
        try:
            response = self.pd_client.rget('users/me')
            if isinstance(response, dict):
                user = response.get('user', response)
                self.user_id = user['id']
                self.user_email = user.get('email', '')
            else:
                raise RuntimeError(
                    f"Unexpected API response type: {type(response)}"
                )
        except pagerduty.Error as error:
            raise RuntimeError(
                f"Failed to fetch current user information: {error}"
            ) from error
        except (KeyError, TypeError) as error:
            raise RuntimeError(
                f"Could not parse user ID from API response: {error}"
            ) from error

    def fetch_incident(self, incident_id: str) -> Dict[str, Any]:
        """
        Fetch PD incident details including external_references (Jira integration field).

        Args:
            incident_id: PagerDuty incident ID

        Returns:
            Incident dict with keys: id, title, status, priority, incident_number,
            html_url, alert_count, drgn_key (from external_references or None)
        """
        try:
            response = self.pd_client.rget(
                f'incidents/{incident_id}',
                params={'include[]': 'external_references'},
            )
            if isinstance(response, dict):
                incident = response.get('incident', response)
            else:
                incident = response

            priority_summary = "—"
            if incident.get('priority'):
                priority_summary = incident['priority'].get('summary', '—')

            # Extract DRGN from external_references (Jira integration field)
            drgn_key: Optional[str] = None
            for ref in incident.get('external_references', []):
                external_id = ref.get('external_id', '')
                if external_id.startswith('DRGN-'):
                    drgn_key = external_id
                    break

            return {
                'id': incident['id'],
                'title': incident.get('title', ''),
                'status': incident.get('status', ''),
                'priority': priority_summary,
                'incident_number': incident.get('incident_number', ''),
                'html_url': incident.get('html_url', f'{PD_BASE_URL}/{incident_id}'),
                'alert_count': incident.get('alert_counts', {}).get('all', 0),
                'drgn_key': drgn_key,
            }
        except pagerduty.Error as error:
            raise RuntimeError(f"Failed to fetch incident {incident_id}: {error}") from error

    def detect_drgn_from_notes(self, incident_id: str) -> Optional[str]:
        """
        Fallback: scan PD incident notes for a DRGN-\\d+ ticket reference.

        Used only when external_references field is empty (DRGN not created via button).

        Args:
            incident_id: PagerDuty incident ID

        Returns:
            First DRGN ticket key found (e.g. "DRGN-15087"), or None
        """
        try:
            notes = list(self.pd_client.list_all(f'incidents/{incident_id}/notes'))
            for note in notes:
                content = note.get('content', '')
                match = DRGN_PATTERN.search(content)
                if match:
                    return match.group(1)
        except pagerduty.Error as error:
            print(f"  Warning: Could not fetch notes for {incident_id}: {error}")
        return None

    def fetch_jira_issue(self, issue_key: str) -> Dict[str, str]:
        """
        Fetch basic Jira issue details.

        Args:
            issue_key: Jira issue key (e.g. "DSSD-29386")

        Returns:
            Dict with keys: key, status, assignee, summary
        """
        try:
            issue = self.jira_client.issue(issue_key)
            assignee_name = "Unassigned"
            if issue.fields.assignee:
                assignee_name = issue.fields.assignee.displayName
            return {
                'key': issue.key,
                'status': str(issue.fields.status),
                'assignee': assignee_name,
                'summary': issue.fields.summary,
            }
        except JIRAError as error:
            raise RuntimeError(f"Failed to fetch Jira issue {issue_key}: {error}") from error

    def link_jira_issues(self, drgn_key: str, dssd_key: str) -> None:
        """
        Create a "Blocks" link: DRGN "is blocked by" DSSD.

        Args:
            drgn_key: DRGN issue key
            dssd_key: DSSD issue key
        """
        if self.dry_run:
            print(f"  [DRY-RUN] Would link {drgn_key} 'is blocked by' {dssd_key}")
            return

        try:
            self.jira_client.create_issue_link(
                type="Blocks",
                inwardIssue=drgn_key,
                outwardIssue=dssd_key,
            )
            print(f"  Linked: {drgn_key} 'is blocked by' {dssd_key}")
        except JIRAError as error:
            raise RuntimeError(
                f"Failed to link {drgn_key} → {dssd_key}: {error}"
            ) from error

    def transition_to_escalated(self, drgn_key: str) -> None:
        """
        Transition a DRGN ticket to "Escalated" status (transition ID 51).

        Args:
            drgn_key: DRGN issue key
        """
        if self.dry_run:
            print(f"  [DRY-RUN] Would transition {drgn_key} → Escalated (ID {ESCALATED_TRANSITION_ID})")
            return

        try:
            self.jira_client.transition_issue(drgn_key, ESCALATED_TRANSITION_ID)
            print(f"  Transitioned: {drgn_key} → Escalated")
        except JIRAError as error:
            raise RuntimeError(
                f"Failed to transition {drgn_key} to Escalated: {error}"
            ) from error

    def add_pd_note(
        self,
        incident_id: str,
        drgn_key: str,
        dssd_key: str,
        dssd_info: Dict[str, str],
    ) -> None:
        """
        Add a PD note with escalation details.

        Args:
            incident_id: PagerDuty incident ID
            drgn_key: DRGN issue key
            dssd_key: DSSD issue key
            dssd_info: DSSD issue details (status, assignee)
        """
        note_content = (
            f"Escalated to {dssd_key} - {dssd_info['status']} - {dssd_info['assignee']}\n"
            f"{JIRA_BASE_URL}/{dssd_key}\n"
            f"{drgn_key} linked \"is blocked by\" {dssd_key}, status → Escalated\n"
            f"Notified #cds-ops-24x7-int"
        )

        if self.dry_run:
            print(f"  [DRY-RUN] Would add PD note to {incident_id}:")
            for line in note_content.split('\n'):
                print(f"    {line}")
            return

        try:
            self.pd_client.rpost(
                f'incidents/{incident_id}/notes',
                json={'note': {'content': note_content}},
                headers={'From': self.user_email},
            )
            print(f"  PD note added to incident {incident_id}")
        except pagerduty.Error as error:
            raise RuntimeError(
                f"Failed to add PD note to {incident_id}: {error}"
            ) from error

    def print_slack_template(
        self,
        dssd_key: str,
        incident_title: str,
        error_summary: str,
    ) -> None:
        """
        Print a Slack notification template ready to paste into #cds-ops-24x7-int.

        Args:
            dssd_key: DSSD issue key
            incident_title: PD incident title
            error_summary: Brief error description for the blockquote
        """
        dssd_url = f"{JIRA_BASE_URL}/{dssd_key}"

        print("\n" + "=" * 60)
        print("Slack template for #cds-ops-24x7-int:")
        print("=" * 60)
        print(f"Hello @dataops,")
        print(f"Please have a look at {dssd_key}")
        print(f"({dssd_url})")
        print(f"{incident_title}")
        print(f"> {error_summary}")
        print(f"cc @noc")
        print("=" * 60)
        print(f"NOTE: Manually hyperlink '{dssd_key}' to {dssd_url} in Slack")
        print("=" * 60)

    def run(
        self,
        incident_id: str,
        dssd_key: str,
        drgn_key: Optional[str] = None,
    ) -> None:
        """
        Execute the full escalation workflow.

        Args:
            incident_id: PagerDuty incident ID
            dssd_key: DSSD ticket key (e.g. "DSSD-29386")
            drgn_key: DRGN ticket key (optional, auto-detected from PD notes)
        """
        mode_label = "[DRY-RUN] " if self.dry_run else ""
        print(f"\n{mode_label}PD Escalation Tool v{VERSION}")
        print("=" * 50)

        # Step 1: Resolve PD user email
        print("\n[1/8] Resolving PD user...")
        self.get_current_user()
        print(f"  User: {self.user_email}")

        # Step 2: Fetch PD incident
        print(f"\n[2/8] Fetching PD incident {incident_id}...")
        incident_info = self.fetch_incident(incident_id)
        print(f"  Title: {incident_info['title']}")
        print(f"  Status: {incident_info['status']} | Priority: {incident_info['priority']}")
        print(f"  Incident #{incident_info['incident_number']} | Alerts: {incident_info['alert_count']}")

        # Step 3: Auto-detect DRGN if not provided
        if not drgn_key:
            print(f"\n[3/8] Auto-detecting DRGN...")
            # Primary: check external_references (Jira integration field)
            drgn_key = incident_info.get('drgn_key')
            if drgn_key:
                print(f"  Found via Jira integration: {drgn_key}")
            else:
                # Fallback: scan PD notes
                print("  Not in Jira integration field, checking notes...")
                drgn_key = self.detect_drgn_from_notes(incident_id)
                if drgn_key:
                    print(f"  Found in notes: {drgn_key}")
                else:
                    pd_url = incident_info['html_url']
                    print(f"\n  ** No DRGN ticket linked to this incident **")
                    print(f"  Open PD incident and press 'Create Jira Issue' button:")
                    print(f"  {pd_url}")
                    print(f"\n  Then re-run this tool (DRGN will be auto-detected).")
                    sys.exit(1)
        else:
            print(f"\n[3/8] Using provided DRGN: {drgn_key}")

        # Step 4: Fetch DSSD status/assignee
        print(f"\n[4/8] Fetching DSSD issue {dssd_key}...")
        dssd_info = self.fetch_jira_issue(dssd_key)
        print(f"  Status: {dssd_info['status']} | Assignee: {dssd_info['assignee']}")
        print(f"  Summary: {dssd_info['summary']}")

        # Step 5: Link DRGN "is blocked by" DSSD
        print(f"\n[5/8] Linking {drgn_key} → {dssd_key}...")
        self.link_jira_issues(drgn_key, dssd_key)

        # Step 6: Transition DRGN → Escalated
        print(f"\n[6/8] Transitioning {drgn_key} to Escalated...")
        self.transition_to_escalated(drgn_key)

        # Step 7: Add PD note
        print(f"\n[7/8] Adding PD note...")
        self.add_pd_note(incident_id, drgn_key, dssd_key, dssd_info)

        # Step 8: Print Slack template
        print(f"\n[8/8] Slack template:")
        # Use incident title as-is; for error summary, extract a short form
        error_summary = incident_info['title']
        self.print_slack_template(dssd_key, incident_info['title'], error_summary)

        print(f"\n{'[DRY-RUN] ' if self.dry_run else ''}Escalation complete.")


def main() -> None:
    """Main entry point for the CLI tool."""
    import argparse

    load_dotenv()

    # Also check parent .env (for noc-toolkit layout)
    parent_env = os.path.join(os.path.dirname(__file__), '..', '..', '.env')
    if os.path.exists(parent_env):
        load_dotenv(dotenv_path=parent_env)

    parser = argparse.ArgumentParser(
        description="PD Escalation Tool — Link DRGN→DSSD, transition to Escalated, post PD note",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s --pd Q33L5GALLQ3ESB --dssd DSSD-29386\n"
            "  %(prog)s --pd https://yourcompany.pagerduty.com/incidents/Q33L5GALLQ3ESB --dssd DSSD-29386 --dry-run\n"
            "  %(prog)s --pd Q33L5GALLQ3ESB --dssd DSSD-29386 --drgn DRGN-15087\n"
        ),
    )
    parser.add_argument(
        '--pd',
        required=True,
        help='PagerDuty incident ID or URL (required)',
    )
    parser.add_argument(
        '--dssd',
        required=True,
        help='DSSD ticket key, e.g. DSSD-29386 (required)',
    )
    parser.add_argument(
        '--drgn',
        default=None,
        help='DRGN ticket key (optional, auto-detected from PD Jira integration field)',
    )
    parser.add_argument(
        '--dry-run', '-n',
        action='store_true',
        help='Simulate without making API mutations',
    )
    parser.add_argument(
        '--version', '-v',
        action='version',
        version=f'%(prog)s {VERSION}',
    )

    args = parser.parse_args()

    # Validate environment
    pagerduty_api_token = os.environ.get('PAGERDUTY_API_TOKEN')
    jira_server_url = os.environ.get('JIRA_SERVER_URL')
    jira_personal_access_token = os.environ.get('JIRA_PERSONAL_ACCESS_TOKEN')

    missing_vars = []
    if not pagerduty_api_token:
        missing_vars.append('PAGERDUTY_API_TOKEN')
    if not jira_server_url:
        missing_vars.append('JIRA_SERVER_URL')
    if not jira_personal_access_token:
        missing_vars.append('JIRA_PERSONAL_ACCESS_TOKEN')

    if missing_vars:
        print(f"Error: Missing required environment variables: {', '.join(missing_vars)}", file=sys.stderr)
        print("\nPlease set these in your environment or .env file.", file=sys.stderr)
        print("See .env.example for the required format.", file=sys.stderr)
        sys.exit(1)

    # Set JIRA_BASE_URL from env var
    global JIRA_BASE_URL
    JIRA_BASE_URL = jira_server_url.rstrip('/') + "/browse"

    # Parse incident ID
    incident_id = extract_incident_id(args.pd)

    # Normalize DSSD key to uppercase
    dssd_key = args.dssd.strip().upper()

    # Normalize DRGN key if provided
    drgn_key: Optional[str] = None
    if args.drgn:
        drgn_key = args.drgn.strip().upper()

    try:
        tool = EscalateTool(
            pagerduty_api_token=pagerduty_api_token,
            jira_server_url=jira_server_url,
            jira_personal_access_token=jira_personal_access_token,
            dry_run=args.dry_run,
        )
        tool.run(
            incident_id=incident_id,
            dssd_key=dssd_key,
            drgn_key=drgn_key,
        )
    except RuntimeError as error:
        print(f"\nError: {error}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nAborted by user.")
        sys.exit(130)


if __name__ == '__main__':
    main()
