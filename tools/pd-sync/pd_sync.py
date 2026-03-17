#!/usr/bin/env python3
"""
PagerDuty Jira Integration Tool

Unified tool for checking PagerDuty incidents and their associated Jira tickets.
Supports read-only mode, update mode with snooze, and interactive menu.
"""

import os
import re
import sys
import warnings
from typing import List, Optional, Dict, Any, TextIO
from datetime import datetime, timedelta, timezone

# Version information
VERSION = "0.3.2"

# Suppress pagination warnings from pagerduty package
warnings.filterwarnings('ignore', message='.*lacks a "more" property.*')

try:
    import pagerduty
    from jira import JIRA
    from jira.exceptions import JIRAError
    from dotenv import load_dotenv
    from tqdm import tqdm
except ImportError as import_error:
    print(f"Error: Missing required dependencies. Please run: pip install -r requirements.txt")
    print(f"Details: {import_error}")
    sys.exit(1)


def _parse_iso_dt(iso_str: str) -> datetime:
    """Parse an ISO 8601 datetime string (with optional trailing 'Z') into a timezone-aware datetime."""
    return datetime.fromisoformat(iso_str.replace('Z', '+00:00'))


class PDSync:
    """Main class for PagerDuty-Jira integration."""

    # Regex pattern to match Jira ticket numbers (e.g., PROJ-123, ABC-4567)
    JIRA_TICKET_PATTERN = re.compile(r'\b([A-Z][A-Z0-9]+-\d+)\b')

    # Keywords that signal an incident can be auto-snoozed without Jira lookup
    IGNORE_DISABLED_PATTERN = re.compile(r'\b(ignore|disabled)\b', re.IGNORECASE)

    def __init__(
        self,
        pagerduty_api_token: str,
        jira_server_url: str,
        jira_email: Optional[str] = None,
        jira_api_token: Optional[str] = None,
        jira_personal_access_token: Optional[str] = None,
        quiet_mode: bool = False
    ) -> None:
        """
        Initialize the tool with API credentials.

        Supports two Jira authentication methods:
        1. Jira Cloud: email + API token (basic auth)
        2. Jira Server/Data Center: Personal Access Token (bearer token)

        Args:
            pagerduty_api_token: PagerDuty API token
            jira_server_url: Jira server URL
            jira_email: Email for Jira Cloud authentication (optional)
            jira_api_token: Jira Cloud API token (optional)
            jira_personal_access_token: Jira Server/Data Center PAT (optional)
            quiet_mode: If True, suppress detailed processing output (default: False)
        """
        self.pagerduty_session = pagerduty.RestApiV2Client(pagerduty_api_token)
        self.quiet_mode = quiet_mode

        # Determine authentication method based on provided credentials
        if jira_personal_access_token:
            # Jira Server/Data Center with Personal Access Token
            self.jira_client = JIRA(
                server=jira_server_url,
                token_auth=jira_personal_access_token
            )
        elif jira_email and jira_api_token:
            # Jira Cloud with email and API token
            self.jira_client = JIRA(
                server=jira_server_url,
                basic_auth=(jira_email, jira_api_token)
            )
        else:
            raise ValueError(
                "Invalid Jira credentials. Provide either:\n"
                "  - JIRA_PERSONAL_ACCESS_TOKEN (for Jira Server/Data Center), or\n"
                "  - JIRA_EMAIL and JIRA_API_TOKEN (for Jira Cloud)"
            )

    @staticmethod
    def _is_assigned_to_user(incident: Dict[str, Any], user_id: str) -> bool:
        """Check if an incident is assigned to the given user."""
        return any(
            assignment.get('assignee', {}).get('id') == user_id
            for assignment in incident.get('assignments', [])
        )

    def print_verbose(self, message: str) -> None:
        """Print message only if not in quiet mode."""
        if not self.quiet_mode:
            print(message)

    def get_current_user_id(self) -> str:
        """
        Get the current authenticated user's ID from PagerDuty.

        Returns:
            Current user's PagerDuty ID

        Raises:
            RuntimeError: If unable to fetch current user information
        """
        try:
            current_user_response = self.pagerduty_session.rget('users/me')

            if isinstance(current_user_response, dict):
                if 'user' in current_user_response:
                    return current_user_response['user']['id']
                elif 'id' in current_user_response:
                    return current_user_response['id']
                else:
                    raise RuntimeError(f"Unexpected API response structure. Keys found: {list(current_user_response.keys())}")
            else:
                raise RuntimeError(f"Unexpected API response type: {type(current_user_response)}")
        except pagerduty.Error as error:
            raise RuntimeError(f"Failed to fetch current user information: {error}") from error
        except (KeyError, TypeError) as error:
            raise RuntimeError(f"Could not parse user ID from API response: {error}") from error

    def get_open_incidents(
        self,
        days_back: int = 60,
        user_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Fetch open PagerDuty incidents (triggered, acknowledged, or snoozed).
        Uses a two-step approach to capture both recently created and older reassigned incidents.

        Args:
            days_back: Number of days to look back for older incidents (default: 60)
            user_id: Filter incidents assigned to this user ID (optional)

        Returns:
            List of open incident dictionaries

        Raises:
            RuntimeError: If PagerDuty API request fails
        """
        try:
            # Step 1: Get current open incidents (no 'since' parameter)
            params_current = {
                'statuses[]': ['triggered', 'acknowledged'],
                'sort_by': 'created_at:desc',
                'include[]': ['assignees']
            }

            current_incidents = self.pagerduty_session.list_all('incidents', params=params_current)
            current_list = list(current_incidents)

            # Client-side filtering for user assignment
            if user_id:
                current_list = [
                    inc for inc in current_list
                    if self._is_assigned_to_user(inc, user_id)
                ]

            # Create a set of incident IDs for deduplication
            incident_ids = {incident['id'] for incident in current_list}
            all_incidents = list(current_list)

            # Step 2: Query with 'since' parameter to find older incidents
            current_year = datetime.now(timezone.utc).year
            since_date = datetime(current_year, 1, 1)
            params_historical = {
                'statuses[]': ['triggered', 'acknowledged'],
                'since': since_date.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'sort_by': 'created_at:desc',
                'include[]': ['assignees']
            }

            historical_incidents = self.pagerduty_session.list_all('incidents', params=params_historical)
            historical_list = list(historical_incidents)

            # Filter and add incidents assigned to the user that aren't already in our list
            for incident in historical_list:
                if incident['id'] not in incident_ids:
                    if user_id and not self._is_assigned_to_user(incident, user_id):
                        continue
                    all_incidents.append(incident)
                    incident_ids.add(incident['id'])

            return all_incidents
        except pagerduty.Error as error:
            raise RuntimeError(f"Failed to fetch open PagerDuty incidents: {error}") from error

    def get_recent_comments(self, incident_id: str, limit: int = 3) -> List[str]:
        """
        Get the most recent comments/notes from a PagerDuty incident.

        Args:
            incident_id: PagerDuty incident ID
            limit: Maximum number of comments to retrieve (default: 3)

        Returns:
            List of recent comment texts (newest first)
        """
        try:
            notes = self.pagerduty_session.list_all(
                f'incidents/{incident_id}/notes'
            )

            notes_list = list(notes)
            if not notes_list:
                return []

            # Notes are returned in reverse chronological order by default
            recent_notes = notes_list[:limit]
            return [note.get('content', '') for note in recent_notes if note.get('content')]

        except pagerduty.Error as error:
            self.print_verbose(f"Warning: Could not fetch notes for incident {incident_id}: {error}")
            return []

    def has_recent_comment_from_user(
        self,
        incident_id: str,
        user_id: str,
        hours_threshold: float = 12.0
    ) -> bool:
        """
        Check if there's a recent comment from the specified user on the incident.

        Args:
            incident_id: PagerDuty incident ID
            user_id: User ID to check for recent comments
            hours_threshold: Number of hours to consider as "recent" (default: 12.0)

        Returns:
            True if user has commented within the threshold, False otherwise
        """
        try:
            notes = self.pagerduty_session.list_all(
                f'incidents/{incident_id}/notes'
            )

            notes_list = list(notes)
            if not notes_list:
                return False

            # Calculate the threshold time (make naive for comparison with parsed timestamps)
            threshold_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours_threshold)

            # Check each note for matching user and recent timestamp
            for note in notes_list:
                # Check if note is from the specified user
                note_user = note.get('user', {})
                note_user_id = note_user.get('id', '')

                if note_user_id == user_id:
                    # Check if note is recent
                    created_at_str = note.get('created_at', '')
                    if created_at_str:
                        try:
                            created_at = _parse_iso_dt(created_at_str)
                            created_at_naive = created_at.replace(tzinfo=None)

                            if created_at_naive > threshold_time:
                                return True
                        except (ValueError, AttributeError) as error:
                            self.print_verbose(f"Warning: Could not parse timestamp '{created_at_str}': {error}")
                            continue

            return False

        except pagerduty.Error as error:
            self.print_verbose(f"Warning: Could not fetch notes for incident {incident_id}: {error}")
            return False

    def extract_jira_ticket_numbers(self, text: str) -> List[str]:
        """
        Extract Jira ticket numbers from text using regex.
        Filters out DRGN tickets as they should be ignored.

        Args:
            text: Text to search for Jira ticket numbers

        Returns:
            List of unique Jira ticket numbers found (excludes DRGN tickets)
        """
        if not text:
            return []

        matches = self.JIRA_TICKET_PATTERN.findall(text)
        # Filter out DRGN tickets - they should be ignored
        filtered_matches = [ticket for ticket in matches if not ticket.startswith('DRGN-')]
        # Return unique ticket numbers while preserving order
        return list(dict.fromkeys(filtered_matches))

    def _check_ignore_disabled(self, title: str, comments: List[str]) -> Optional[str]:
        """Check title and recent comments for 'ignore' or 'disabled' keywords.

        Searches title first, then comments in order.  Returns the first
        matched keyword capitalized ('Ignore' or 'Disabled'), or None.
        """
        for text in [title] + comments:
            match = self.IGNORE_DISABLED_PATTERN.search(text)
            if match:
                return match.group(1).capitalize()
        return None

    def get_jira_ticket_status(self, ticket_key: str) -> Optional[Dict[str, str]]:
        """
        Get the status of a Jira ticket.

        Args:
            ticket_key: Jira ticket key (e.g., 'PROJ-123')

        Returns:
            Dictionary with ticket information or None if ticket not found
        """
        try:
            issue = self.jira_client.issue(ticket_key)
            return {
                'key': issue.key,
                'summary': issue.fields.summary,
                'status': issue.fields.status.name,
                'assignee': issue.fields.assignee.displayName if issue.fields.assignee else 'Unassigned',
                'priority': issue.fields.priority.name if issue.fields.priority else 'None',
                'url': f"{self.jira_client.server_url}/browse/{issue.key}"
            }
        except JIRAError as error:
            if error.status_code == 404:
                self.print_verbose(f"Warning: Jira ticket {ticket_key} not found")
                return None
            else:
                self.print_verbose(f"Warning: Error fetching Jira ticket {ticket_key}: {error}")
                return None

    def add_incident_note(self, incident_id: str, note_content: str) -> bool:
        """
        Add a note/comment to a PagerDuty incident.

        Args:
            incident_id: PagerDuty incident ID
            note_content: The text content to add as a note

        Returns:
            True if successful, False otherwise
        """
        try:
            note_data = {
                'note': {
                    'content': note_content
                }
            }
            self.pagerduty_session.rpost(f'incidents/{incident_id}/notes', json=note_data)
            self.print_verbose(f"✓ Successfully added note to incident {incident_id}")
            return True
        except pagerduty.Error as error:
            self.print_verbose(f"✗ Failed to add note to incident {incident_id}: {error}")
            return False

    def snooze_incident(self, incident_id: str, duration_seconds: int = 21600) -> bool:
        """
        Snooze a PagerDuty incident for a specified duration.

        Args:
            incident_id: PagerDuty incident ID
            duration_seconds: Duration to snooze in seconds (default: 21600 = 6 hours)

        Returns:
            True if successful, False otherwise
        """
        try:
            snooze_data = {
                'duration': duration_seconds
            }
            self.pagerduty_session.rpost(f'incidents/{incident_id}/snooze', json=snooze_data)
            hours = duration_seconds / 3600
            self.print_verbose(f"✓ Successfully snoozed incident {incident_id} for {hours} hours")
            return True
        except pagerduty.Error as error:
            self.print_verbose(f"✗ Failed to snooze incident {incident_id}: {error}")
            return False

    def check_incidents(
        self,
        user_id: Optional[str] = None,
        check_jira: bool = False
    ) -> str:
        """
        Check and display all open incidents (read-only mode).

        Args:
            user_id: Filter incidents assigned to this user ID (optional)
            check_jira: Whether to check Jira status (default: False)

        Returns:
            Summary text suitable for display or saving to file
        """
        if user_id:
            print("Fetching your open PagerDuty incidents...")
        else:
            print("Fetching open PagerDuty incidents...")

        incidents = self.get_open_incidents(user_id=user_id)

        if not incidents:
            summary = "\n✓ No open incidents found. You're all clear!"
            print(summary)
            return summary

        print(f"\nFound {len(incidents)} open incident(s):\n")
        print(f"{'='*80}")

        summary_lines = [
            f"\nОткрыто инцидентов: {len(incidents)}",
            f"Дата проверки: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
            f"{'='*80}\n"
        ]

        for idx, incident in enumerate(tqdm(incidents, desc="Checking incidents", unit="incident"), 1):
            incident_id = incident['id']
            incident_title = incident['title']
            incident_status = incident['status']
            incident_url = incident['html_url']
            created_at = incident.get('created_at', 'Unknown')

            # Display incident information
            output = f"\n{idx}. Incident: {incident_title}\n"
            output += f"   Status: {incident_status}\n"
            output += f"   Created: {created_at}\n"
            output += f"   URL: {incident_url}\n"

            summary_lines.append(output)

            # Check incident title for Jira tickets
            all_jira_tickets = []
            title_tickets = self.extract_jira_ticket_numbers(incident_title)
            if title_tickets:
                all_jira_tickets.extend(title_tickets)
                jira_info = f"   Jira in title: {', '.join(title_tickets)}\n"
                output += jira_info
                summary_lines.append(jira_info)

            # Get last 3 comments
            recent_comments = self.get_recent_comments(incident_id, limit=3)

            if recent_comments:
                # Check all recent comments for Jira tickets
                for comment in recent_comments:
                    tickets = self.extract_jira_ticket_numbers(comment)
                    all_jira_tickets.extend(tickets)

                # Remove duplicates while preserving order
                jira_ticket_numbers = list(dict.fromkeys(all_jira_tickets))

                if jira_ticket_numbers:
                    tickets_info = f"   Jira Ticket(s): {', '.join(jira_ticket_numbers)}\n"
                    output += tickets_info
                    summary_lines.append(tickets_info)

                    # Check Jira status if requested
                    if check_jira:
                        for ticket_number in jira_ticket_numbers[:1]:  # Only show first ticket
                            ticket_info = self.get_jira_ticket_status(ticket_number)
                            if ticket_info:
                                status_info = f"   Jira Status: {ticket_info['status']} (Assigned to: {ticket_info['assignee']})\n"
                                output += status_info
                                summary_lines.append(status_info)
                else:
                    if not title_tickets:
                        comment_preview = f"   Latest comment: {recent_comments[0][:80]}...\n"
                        output += comment_preview
            else:
                if not title_tickets:
                    no_comments = "   No comments yet\n"
                    output += no_comments

            print(output.rstrip())

        print(f"\n{'='*80}")
        summary_footer = f"\nTotal: {len(incidents)} open incident(s)"
        print(summary_footer)
        summary_lines.append(f"\n{'='*80}")
        summary_lines.append(summary_footer)

        return ''.join(summary_lines)

    def process_and_update_incidents(
        self,
        user_id: Optional[str] = None,
        enable_snooze: bool = False,
        snooze_duration_hours: float = 6.0,
        limit: Optional[int] = None
    ) -> str:
        """
        Find all open incidents, check Jira status, and post update comments.

        Args:
            user_id: Filter incidents assigned to this user ID (optional)
            enable_snooze: Whether to snooze incidents after updating (default: False)
            snooze_duration_hours: Duration to snooze in hours (default: 6.0)
            limit: Maximum number of incidents to process (optional, for testing)

        Returns:
            Summary text suitable for display or saving to file
        """
        if user_id:
            print(f"Processing your assigned open PagerDuty incidents...")
        else:
            print(f"Processing open PagerDuty incidents...")

        incidents = self.get_open_incidents(user_id=user_id)

        if not incidents:
            summary = "\n✓ No open incidents found. You're all clear!"
            print(summary)
            return summary

        # Apply limit if specified
        total_incidents = len(incidents)
        if limit and limit < total_incidents:
            incidents = incidents[:limit]
            print(f"Found {total_incidents} open incident(s). Processing first {limit} for testing...\n")
        else:
            print(f"Found {len(incidents)} open incident(s). Processing...\n")

        # Track incidents by category
        processed_and_snoozed = []
        resolved_tickets = []
        no_jira_tickets = []
        skipped_recent_comment = []
        auto_handled_keywords = []

        for incident in tqdm(incidents, desc="Processing incidents", unit="incident", disable=not self.quiet_mode):
            incident_id = incident['id']
            incident_title = incident['title']
            incident_status = incident['status']
            incident_url = incident['html_url']

            self.print_verbose(f"{'='*80}")
            self.print_verbose(f"Processing: {incident_title}")
            self.print_verbose(f"Status: {incident_status}")
            self.print_verbose(f"URL: {incident_url}")

            # Get last 3 comments
            recent_comments = self.get_recent_comments(incident_id, limit=3)

            # Check for ignore/disabled keywords before Jira lookup
            keyword = self._check_ignore_disabled(incident_title, recent_comments)
            if keyword:
                already_commented = (
                    user_id
                    and self.has_recent_comment_from_user(incident_id, user_id, hours_threshold=12.0)
                )
                should_snooze_keyword = enable_snooze

                if should_snooze_keyword:
                    comment = f"{keyword}. Snooze"
                else:
                    comment = keyword

                if not already_commented:
                    self.print_verbose(f"Keyword '{keyword}' detected — posting: {comment}")
                    self.add_incident_note(incident_id, comment)
                else:
                    self.print_verbose(f"Keyword '{keyword}' detected — already commented recently")

                if should_snooze_keyword:
                    snooze_seconds = int(snooze_duration_hours * 3600)
                    self.snooze_incident(incident_id, snooze_seconds)

                auto_handled_keywords.append({
                    'title': incident_title,
                    'url': incident_url,
                    'keyword': keyword,
                })
                self.print_verbose("")
                continue

            # Check incident title for Jira tickets
            all_jira_tickets = []
            title_tickets = self.extract_jira_ticket_numbers(incident_title)
            if title_tickets:
                all_jira_tickets.extend(title_tickets)
                self.print_verbose(f"Found Jira ticket(s) in title: {', '.join(title_tickets)}")

            if recent_comments:
                # Check all recent comments for Jira tickets
                for comment in recent_comments:
                    tickets = self.extract_jira_ticket_numbers(comment)
                    all_jira_tickets.extend(tickets)

            # Remove duplicates while preserving order
            jira_ticket_numbers = list(dict.fromkeys(all_jira_tickets))

            if not jira_ticket_numbers:
                if recent_comments:
                    self.print_verbose(f"Latest comment: {recent_comments[0][:80]}...")
                    self.print_verbose("No Jira tickets found in title or comments.")
                else:
                    self.print_verbose("No comments found on this incident.")
                    self.print_verbose("No Jira tickets found in title.")
                no_jira_tickets.append({
                    'title': incident_title,
                    'url': incident_url,
                    'reason': 'No Jira ticket in title or comments'
                })
                self.print_verbose("")
                continue

            if title_tickets:
                self.print_verbose(f"Found Jira ticket(s): {', '.join(jira_ticket_numbers)} (from title and/or comments)")

            # Process the first Jira ticket
            ticket_number = jira_ticket_numbers[0]
            ticket_info = self.get_jira_ticket_status(ticket_number)

            if not ticket_info:
                self.print_verbose(f"Could not fetch Jira ticket info for {ticket_number}")
                no_jira_tickets.append({
                    'title': incident_title,
                    'url': incident_url,
                    'reason': f'Could not fetch {ticket_number}'
                })
                self.print_verbose("")
                continue

            self.print_verbose(f"Jira Ticket: {ticket_info['key']}")
            self.print_verbose(f"Status: {ticket_info['status']}")
            self.print_verbose(f"Assignee: {ticket_info['assignee']}")

            # Check if ticket is resolved/done
            ticket_status_lower = ticket_info['status'].lower()
            is_resolved = ticket_status_lower in ['done', 'resolved', 'closed']

            # Check if we already posted a recent comment (within last 12 hours)
            if user_id and self.has_recent_comment_from_user(incident_id, user_id, hours_threshold=12.0):
                self.print_verbose("⏭ Skipping - already commented within last 12 hours")
                self.print_verbose("")
                skipped_recent_comment.append({
                    'title': incident_title,
                    'url': incident_url,
                    'ticket': ticket_info['key'],
                    'status': ticket_info['status']
                })
                continue

            # Create the update comment
            update_comment = f"{ticket_info['key']} - {ticket_info['status']} - {ticket_info['assignee']}"

            # Only add ".Snooze" if ticket is not resolved and snooze is enabled
            should_snooze = enable_snooze and not is_resolved
            if should_snooze:
                update_comment += ". Snooze"

            self.print_verbose(f"Posting comment: {update_comment}")

            # Add the note to PagerDuty
            success = self.add_incident_note(incident_id, update_comment)

            if success:
                if is_resolved:
                    resolved_tickets.append({
                        'title': incident_title,
                        'url': incident_url,
                        'ticket': ticket_info['key'],
                        'status': ticket_info['status']
                    })
                    self.print_verbose(f"✓ Ticket is {ticket_info['status']} - not snoozing")
                elif should_snooze:
                    snooze_seconds = int(snooze_duration_hours * 3600)
                    if self.snooze_incident(incident_id, snooze_seconds):
                        processed_and_snoozed.append({
                            'title': incident_title,
                            'url': incident_url,
                            'ticket': ticket_info['key'],
                            'status': ticket_info['status']
                        })
                else:
                    processed_and_snoozed.append({
                        'title': incident_title,
                        'url': incident_url,
                        'ticket': ticket_info['key'],
                        'status': ticket_info['status']
                    })

            self.print_verbose("")

        # Build summary
        summary_lines = [
            f"\n{'='*80}",
            "\nSUMMARY",
            f"\n{'='*80}\n"
        ]

        if processed_and_snoozed:
            summary_lines.append(f"\n✓ Processed and {'snoozed' if enable_snooze else 'updated'}: {len(processed_and_snoozed)} incident(s)\n")
            for item in processed_and_snoozed:
                summary_lines.append(f"  - {item['title']}\n")
                summary_lines.append(f"    Ticket: {item['ticket']} ({item['status']})\n")
                summary_lines.append(f"    URL: {item['url']}\n")
                summary_lines.append("\n")

        if resolved_tickets:
            summary_lines.append(f"\n✓ Incidents with Done/Resolved Jira tickets (not snoozed): {len(resolved_tickets)}\n")
            for item in resolved_tickets:
                summary_lines.append(f"  - {item['title']}\n")
                summary_lines.append(f"    Ticket: {item['ticket']} ({item['status']})\n")
                summary_lines.append(f"    URL: {item['url']}\n")
                summary_lines.append("\n")

        if skipped_recent_comment:
            summary_lines.append(f"\n⏭ Skipped (already commented within 12 hours): {len(skipped_recent_comment)}\n")
            for item in skipped_recent_comment:
                summary_lines.append(f"  - {item['title']}\n")
                summary_lines.append(f"    Ticket: {item['ticket']} ({item['status']})\n")
                summary_lines.append(f"    URL: {item['url']}\n")
                summary_lines.append("\n")

        if no_jira_tickets:
            summary_lines.append(f"\n⚠ Incidents without Jira tickets: {len(no_jira_tickets)}\n")
            for item in no_jira_tickets:
                summary_lines.append(f"  - {item['title']}\n")
                summary_lines.append(f"    Reason: {item['reason']}\n")
                summary_lines.append(f"    URL: {item['url']}\n")
                summary_lines.append("\n")

        if auto_handled_keywords:
            action_label = "snoozed" if enable_snooze else "commented"
            summary_lines.append(f"\n🔇 Auto-{action_label} (ignore/disabled keyword): {len(auto_handled_keywords)} incident(s)\n")
            for item in auto_handled_keywords:
                summary_lines.append(f"  - {item['title']}\n")
                summary_lines.append(f"    Keyword: {item['keyword']}\n")
                summary_lines.append(f"    URL: {item['url']}\n")
                summary_lines.append("\n")

        summary_lines.append(f"\n{'='*80}\n")
        summary_lines.append(f"Total incidents processed: {len(incidents)}\n")

        summary_text = ''.join(summary_lines)
        print(summary_text)

        return summary_text


def save_summary_to_file(summary: str, filename: Optional[str] = None) -> None:
    """
    Save summary to a file.

    Args:
        summary: Summary text to save
        filename: Output filename (optional, defaults to pagerduty_summary.txt)
    """
    if not filename:
        filename = "pagerduty_summary.txt"

    try:
        # Remove old file if exists
        if os.path.exists(filename):
            os.remove(filename)

        with open(filename, 'w', encoding='utf-8') as f:
            f.write(summary)
        print(f"\n✓ Summary saved to: {filename}")
    except IOError as error:
        print(f"\n✗ Failed to save summary: {error}")


def show_interactive_menu() -> Dict[str, Any]:
    """
    Display interactive menu and collect user choices.

    Returns:
        Dictionary with user choices
    """
    print("\n" + "="*80)
    print("PagerDuty-Jira Integration Tool - Interactive Menu")
    print("="*80)

    # Mode selection
    print("\n1. Select mode:")
    print("   [1] Check only (read-only)")
    print("   [2] Update incidents (post comments)")
    print("   [3] Update and snooze incidents")

    mode_choice = input("\nYour choice (1-3): ").strip()

    if mode_choice not in ['1', '2', '3']:
        print("Invalid choice. Defaulting to check mode.")
        mode_choice = '1'

    # User filter
    print("\n2. Filter by user:")
    print("   [1] Only my incidents (default)")
    print("   [2] All incidents")

    user_filter = input("\nYour choice (1-2): ").strip()
    skip_user_filter = (user_filter == '2')

    # Jira status check
    check_jira = False
    if mode_choice == '1':
        jira_check = input("\n3. Check Jira status? (y/n): ").strip().lower()
        check_jira = (jira_check == 'y')

    # Snooze duration
    snooze_hours = 6.0
    if mode_choice == '3':
        snooze_input = input("\n3. Snooze duration in hours (default 6): ").strip()
        if snooze_input:
            try:
                snooze_hours = float(snooze_input)
            except ValueError:
                print("Invalid input. Using default 6 hours.")

    # Incident limit
    limit_input = input("\n4. Limit number of incidents to process (leave empty for all): ").strip()
    incident_limit = None
    if limit_input:
        try:
            incident_limit = int(limit_input)
        except ValueError:
            print("Invalid input. Processing all incidents.")

    # Details mode (for update/snooze, ask if want detailed output)
    show_details = True
    if mode_choice in ['2', '3']:
        details_input = input("\n5. Show detailed progress for each incident? (y/n, default: n): ").strip().lower()
        show_details = (details_input == 'y')
    else:
        # Check mode shows details by default
        show_details = True

    # Save summary
    save_input = input("\n6. Save summary to file? (y/n): ").strip().lower()
    save_summary = (save_input == 'y')

    return {
        'mode': mode_choice,
        'skip_user_filter': skip_user_filter,
        'check_jira': check_jira,
        'enable_snooze': (mode_choice == '3'),
        'snooze_hours': snooze_hours,
        'limit': incident_limit,
        'show_details': show_details,
        'save_summary': save_summary
    }


def main() -> None:
    """Main entry point for the CLI tool."""
    # Load environment variables from .env file if present
    load_dotenv()

    # Get credentials from environment variables
    pagerduty_api_token = os.environ.get('PAGERDUTY_API_TOKEN')
    jira_server_url = os.environ.get('JIRA_SERVER_URL')
    jira_email = os.environ.get('JIRA_EMAIL')
    jira_api_token = os.environ.get('JIRA_API_TOKEN')
    jira_personal_access_token = os.environ.get('JIRA_PERSONAL_ACCESS_TOKEN')

    # Validate PagerDuty credentials
    if not pagerduty_api_token:
        print("Error: Missing required environment variable: PAGERDUTY_API_TOKEN")
        print("\nPlease set this in your environment or create a .env file.")
        print("See .env.example for the required format.")
        sys.exit(1)

    # Validate Jira credentials
    if not jira_server_url:
        print("Error: Missing required environment variable: JIRA_SERVER_URL")
        print("\nPlease set this in your environment or create a .env file.")
        print("See .env.example for the required format.")
        sys.exit(1)

    # Check for valid Jira authentication method
    has_personal_access_token = bool(jira_personal_access_token)
    has_cloud_credentials = bool(jira_email and jira_api_token)

    if not has_personal_access_token and not has_cloud_credentials:
        print("Error: Missing Jira authentication credentials.")
        print("\nFor Jira Server/Data Center, set:")
        print("  - JIRA_PERSONAL_ACCESS_TOKEN")
        print("\nFor Jira Cloud, set:")
        print("  - JIRA_EMAIL")
        print("  - JIRA_API_TOKEN")
        print("\nSee .env.example for the required format.")
        sys.exit(1)

    # Parse command line arguments
    if len(sys.argv) == 1:
        # No arguments - show interactive menu
        choices = show_interactive_menu()
        mode = choices['mode']
        skip_user_filter = choices['skip_user_filter']
        check_jira = choices['check_jira']
        enable_snooze = choices['enable_snooze']
        snooze_hours = choices['snooze_hours']
        incident_limit = choices['limit']
        show_details = choices['show_details']
        save_summary = choices['save_summary']
    else:
        # Parse command line arguments
        mode = '1'  # Default to check mode
        skip_user_filter = False
        check_jira = False
        enable_snooze = False
        snooze_hours = 6.0
        incident_limit = None
        show_details = None  # Will be set based on mode if not explicitly specified
        save_summary = False

        i = 1
        while i < len(sys.argv):
            arg = sys.argv[i]

            if arg == '--check':
                mode = '1'
            elif arg == '--update':
                mode = '2'
            elif arg == '--snooze':
                mode = '3'
                enable_snooze = True
                # Check if next argument is a number (snooze hours)
                if i + 1 < len(sys.argv):
                    try:
                        snooze_hours = float(sys.argv[i + 1])
                        i += 1
                    except ValueError:
                        pass
            elif arg == '--all':
                skip_user_filter = True
            elif arg == '--check-jira':
                check_jira = True
            elif arg == '--limit':
                if i + 1 < len(sys.argv):
                    try:
                        incident_limit = int(sys.argv[i + 1])
                        i += 1
                    except ValueError:
                        print(f"Error: --limit requires an integer argument")
                        sys.exit(1)
            elif arg == '--details' or arg == '--verbose' or arg == '-v':
                show_details = True
            elif arg == '--quiet' or arg == '-q':
                # Deprecated but kept for backwards compatibility
                show_details = False
            elif arg == '--save-summary':
                save_summary = True
            elif arg == '--help' or arg == '-h':
                print("Usage: python pagerduty_jira_tool.py [OPTIONS]")
                print("\nOptions:")
                print("  --check           Check mode (read-only, default, shows details)")
                print("  --update          Update mode (post comments, quiet by default)")
                print("  --snooze [HOURS]  Update and snooze mode (quiet by default, default: 6 hours)")
                print("  --all             Show all incidents (not just yours)")
                print("  --check-jira      Check Jira status in check mode")
                print("  --limit N         Process only first N incidents")
                print("  --details, -v     Show detailed progress for each incident")
                print("  --save-summary    Save summary to file")
                print("  --help, -h        Show this help message")
                print("\nDefault behavior:")
                print("  • --check mode:  Shows detailed output")
                print("  • --update mode: Quiet (only summary)")
                print("  • --snooze mode: Quiet (only summary)")
                print("  • Use --details to see progress for update/snooze modes")
                print("\nExamples:")
                print("  python pagerduty_jira_tool.py")
                print("    (interactive menu)")
                print("  python pagerduty_jira_tool.py --check --check-jira")
                print("    (check with Jira status, detailed output)")
                print("  python pagerduty_jira_tool.py --snooze")
                print("    (snooze mode, quiet output by default)")
                print("  python pagerduty_jira_tool.py --snooze --details")
                print("    (snooze mode with detailed progress)")
                print("  python pagerduty_jira_tool.py --update --limit 3 --details")
                print("    (update first 3 incidents with detailed output)")
                print("\nNote: --quiet/-q flag still works for backwards compatibility.")
                sys.exit(0)
            else:
                print(f"Error: Unknown argument '{arg}'. Use --help for usage.")
                sys.exit(1)

            i += 1

        # Set default show_details based on mode if not explicitly specified
        if show_details is None:
            if mode == '1':
                # Check mode: show details by default
                show_details = True
            else:
                # Update/Snooze modes: quiet by default
                show_details = False

    # Create tool instance
    try:
        # quiet_mode is inverse of show_details
        quiet_mode = not show_details

        tool = PDSync(
            pagerduty_api_token=pagerduty_api_token,
            jira_server_url=jira_server_url,
            jira_email=jira_email,
            jira_api_token=jira_api_token,
            jira_personal_access_token=jira_personal_access_token,
            quiet_mode=quiet_mode
        )

        # Get current user ID
        if skip_user_filter:
            print("Skipping user filter - showing ALL incidents.\n")
            current_user_id = None
        else:
            print("Getting your PagerDuty user information...")
            try:
                current_user_id = tool.get_current_user_id()
                print(f"Filtering incidents assigned to you (User ID: {current_user_id})")
            except RuntimeError as error:
                print(f"Warning: {error}")
                print("Continuing without user filter (will show all incidents).")
                current_user_id = None

        # Show output mode info
        if mode in ['2', '3']:  # Update or Snooze modes
            if quiet_mode:
                print("Output: Quiet mode (only summary will be shown)")
            else:
                print("Output: Detailed mode (progress for each incident will be shown)")
        print()

        # Execute based on mode
        summary = ""
        if mode == '1':
            # Check mode
            summary = tool.check_incidents(
                user_id=current_user_id,
                check_jira=check_jira
            )
        else:
            # Update or snooze mode
            summary = tool.process_and_update_incidents(
                user_id=current_user_id,
                enable_snooze=enable_snooze,
                snooze_duration_hours=snooze_hours,
                limit=incident_limit
            )

        # Save summary if requested
        if save_summary:
            save_summary_to_file(summary)

    except ValueError as error:
        print(f"Error: {error}")
        sys.exit(1)
    except Exception as error:
        print(f"Error: An unexpected error occurred: {error}")
        sys.exit(1)


if __name__ == '__main__':
    main()
