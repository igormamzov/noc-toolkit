#!/usr/bin/env python3
"""
PD Escalation Tool

Automates the post-DSSD-creation escalation workflow:
1. Link DRGN → "is blocked by" → DSSD in Jira
2. Transition DRGN to "Escalated" status
3. Add a PD note with escalation details
4. Print Slack template for #cds-ops-24x7-int
"""

import logging
import os
import re
import sys
from typing import Any, Dict, Optional

# Version information
VERSION = "0.1.1"

try:
    import pagerduty
    from jira.exceptions import JIRAError
    from noc_utils import require_env, new_pd_client, new_jira_client, setup_logging
except ImportError as import_error:
    logging.basicConfig()
    logging.error("Missing required dependencies. Please run: pip install -r requirements.txt")
    logging.error("Details: %s", import_error)
    sys.exit(1)

logger = setup_logging(name=__name__)

# Regex for detecting DRGN tickets in text (fallback)
DRGN_PATTERN = re.compile(r'\b(DRGN-\d+)\b')

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
        self.pd_client = new_pd_client(pagerduty_api_token)
        self.jira_client, self.jira_base_url = new_jira_client(
            jira_server_url, jira_personal_access_token,
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
                'html_url': incident.get('html_url', ''),
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
            logger.warning("  Warning: Could not fetch notes for %s: %s", incident_id, error)
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
            logger.info("  [DRY-RUN] Would link %s 'is blocked by' %s", drgn_key, dssd_key)
            return

        try:
            self.jira_client.create_issue_link(
                type="Blocks",
                inwardIssue=drgn_key,
                outwardIssue=dssd_key,
            )
            logger.info("  Linked: %s 'is blocked by' %s", drgn_key, dssd_key)
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
            logger.info(
                "  [DRY-RUN] Would transition %s → Escalated (ID %s)",
                drgn_key, ESCALATED_TRANSITION_ID,
            )
            return

        try:
            self.jira_client.transition_issue(drgn_key, ESCALATED_TRANSITION_ID)
            logger.info("  Transitioned: %s → Escalated", drgn_key)
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
            f"{self.jira_base_url}/{dssd_key}\n"
            f"{drgn_key} linked \"is blocked by\" {dssd_key}, status → Escalated\n"
            f"Notified #cds-ops-24x7-int"
        )

        if self.dry_run:
            logger.info("  [DRY-RUN] Would add PD note to %s:", incident_id)
            for line in note_content.split('\n'):
                logger.info("    %s", line)
            return

        try:
            self.pd_client.rpost(
                f'incidents/{incident_id}/notes',
                json={'note': {'content': note_content}},
                headers={'From': self.user_email},
            )
            logger.info("  PD note added to incident %s", incident_id)
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
        dssd_url = f"{self.jira_base_url}/{dssd_key}"

        logger.info("\n" + "=" * 60)
        logger.info("Slack template for #cds-ops-24x7-int:")
        logger.info("=" * 60)
        logger.info("Hello @dataops,")
        logger.info("Please have a look at %s", dssd_key)
        logger.info("(%s)", dssd_url)
        logger.info("%s", incident_title)
        logger.info("> %s", error_summary)
        logger.info("cc @noc")
        logger.info("=" * 60)
        logger.info("NOTE: Manually hyperlink '%s' to %s in Slack", dssd_key, dssd_url)
        logger.info("=" * 60)

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
        logger.info("\n%sPD Escalation Tool v%s", mode_label, VERSION)
        logger.info("=" * 50)

        # Step 1: Resolve PD user email
        logger.info("\n[1/8] Resolving PD user...")
        self.get_current_user()
        logger.info("  User: %s", self.user_email)

        # Step 2: Fetch PD incident
        logger.info("\n[2/8] Fetching PD incident %s...", incident_id)
        incident_info = self.fetch_incident(incident_id)
        logger.info("  Title: %s", incident_info['title'])
        logger.info(
            "  Status: %s | Priority: %s",
            incident_info['status'], incident_info['priority'],
        )
        logger.info(
            "  Incident #%s | Alerts: %s",
            incident_info['incident_number'], incident_info['alert_count'],
        )

        # Step 3: Auto-detect DRGN if not provided
        if not drgn_key:
            logger.info("\n[3/8] Auto-detecting DRGN...")
            # Primary: check external_references (Jira integration field)
            drgn_key = incident_info.get('drgn_key')
            if drgn_key:
                logger.info("  Found via Jira integration: %s", drgn_key)
            else:
                # Fallback: scan PD notes
                logger.info("  Not in Jira integration field, checking notes...")
                drgn_key = self.detect_drgn_from_notes(incident_id)
                if drgn_key:
                    logger.info("  Found in notes: %s", drgn_key)
                else:
                    pd_url = incident_info['html_url']
                    raise RuntimeError(
                        f"No DRGN ticket linked to incident {incident_id}. "
                        f"Open PD incident and press 'Create Jira Issue' button: {pd_url} "
                        f"Then re-run this tool."
                    )
        else:
            logger.info("\n[3/8] Using provided DRGN: %s", drgn_key)

        # Step 4: Fetch DSSD status/assignee
        logger.info("\n[4/8] Fetching DSSD issue %s...", dssd_key)
        dssd_info = self.fetch_jira_issue(dssd_key)
        logger.info("  Status: %s | Assignee: %s", dssd_info['status'], dssd_info['assignee'])
        logger.info("  Summary: %s", dssd_info['summary'])

        # Step 5: Link DRGN "is blocked by" DSSD
        logger.info("\n[5/8] Linking %s → %s...", drgn_key, dssd_key)
        self.link_jira_issues(drgn_key, dssd_key)

        # Step 6: Transition DRGN → Escalated
        logger.info("\n[6/8] Transitioning %s to Escalated...", drgn_key)
        self.transition_to_escalated(drgn_key)

        # Step 7: Add PD note
        logger.info("\n[7/8] Adding PD note...")
        self.add_pd_note(incident_id, drgn_key, dssd_key, dssd_info)

        # Step 8: Print Slack template
        logger.info("\n[8/8] Slack template:")
        # Use incident title as-is; for error summary, extract a short form
        error_summary = incident_info['title']
        self.print_slack_template(dssd_key, incident_info['title'], error_summary)

        logger.info("\n%sEscalation complete.", "[DRY-RUN] " if self.dry_run else "")


def main() -> None:
    """Main entry point for the CLI tool."""
    import argparse

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
    env = require_env('PAGERDUTY_API_TOKEN', 'JIRA_SERVER_URL', 'JIRA_PERSONAL_ACCESS_TOKEN')

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
            pagerduty_api_token=env['PAGERDUTY_API_TOKEN'],
            jira_server_url=env['JIRA_SERVER_URL'],
            jira_personal_access_token=env['JIRA_PERSONAL_ACCESS_TOKEN'],
            dry_run=args.dry_run,
        )
        tool.run(
            incident_id=incident_id,
            dssd_key=dssd_key,
            drgn_key=drgn_key,
        )
    except RuntimeError as error:
        logger.error(str(error))
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("\nAborted by user.")
        sys.exit(130)


if __name__ == '__main__':
    main()
