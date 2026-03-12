#!/usr/bin/env python3
"""
PagerDuty Monitor

Automatically monitors triggered PagerDuty incidents and acknowledges them
with appropriate comments. Runs continuously for 1 hour.
"""

import os
import sys
import json
import argparse
import random
import warnings
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set
from dotenv import load_dotenv

# Version information
VERSION = "0.1.4"

# Title patterns for silent acknowledge (ack only, no comment).
# If an incident title contains any of these substrings (case-insensitive),
# the monitor will acknowledge it without posting a comment.
SILENT_ACK_PATTERNS = [
    "Missing AUS & NZL",
    "Missing MSP Export",
    "Missing CANADA",
    "Missing Central",
    "Missing East",
    "Missing International",
    "Missing UK",
]

# Comment phrases for auto-acknowledge (to look like a real engineer)
COMMENTS_NORMAL = [
    "Working on it",
    "Monitoring now",
    "Working",
    "On it",
    "Checking job",
    "Checking now",
    "Checking status",
    "Looking into it",
    "Reviewing the alert",
    "Started working",
    "Investigating",
    "Checking",
    "Checking alert",
]

COMMENTS_TYPO = [
    "Investigaing",
    "Sarted working",
    "Reviewing alert",
    "Cheking status",
    "Chekcing",
    "Wokring",
    "Investagating",
    "Checkign",
    "Monitornig now",
    "Workign",
]

ALL_COMMENTS = COMMENTS_NORMAL + COMMENTS_TYPO

# Suppress pagination warnings from pagerduty package
warnings.filterwarnings('ignore', message='.*lacks a "more" property.*')

try:
    import pagerduty
except ImportError:
    print("Error: Missing required dependencies. Please run: pip install -r requirements.txt")
    sys.exit(1)


class PagerDutyMonitor:
    """Monitors triggered PagerDuty incidents and auto-acknowledges them."""

    def __init__(
        self,
        pagerduty_api_token: str,
        comment_pattern: str = "working on it",
        check_interval_seconds: int = 30,
        output_file: str = "~/pd-monitor-needs-attention.txt",
        dry_run: bool = False,
        verbose: bool = False,
        details: bool = False,
        background: bool = False
    ) -> None:
        """
        Initialize the PagerDuty monitor.

        Args:
            pagerduty_api_token: PagerDuty API token
            comment_pattern: Comment to add when acknowledging new incidents
            check_interval_seconds: How often to check for new incidents (seconds)
            output_file: Path to file for logging incidents needing attention
            dry_run: If True, don't make actual changes
            verbose: If True, print detailed output
            details: If True, show detailed check information
            background: If True, suppress interactive prompts and progress bar
        """
        self.pagerduty_session = pagerduty.RestApiV2Client(pagerduty_api_token)
        self.comment_pattern = comment_pattern
        self.random_comments = (comment_pattern.lower() == "working on it")
        self.check_interval_seconds = check_interval_seconds
        self.output_file = Path(output_file).expanduser()
        self.dry_run = dry_run
        self.verbose = verbose
        self.details = details
        self.background = background
        self.user_id = self._get_current_user_id()
        self.user_email = self._get_user_email()
        self.processed_incidents: Set[str] = set()

    def _get_current_user_id(self) -> str:
        """
        Get the current user ID from the API token.

        Returns:
            User ID string

        Raises:
            RuntimeError if unable to get user ID
        """
        response = self.pagerduty_session.get('users/me')
        data = response.json()
        user_id = data.get('user', {}).get('id')
        if not user_id:
            raise RuntimeError("Unable to get user ID from PagerDuty API")
        return user_id

    def _get_user_email(self) -> str:
        """Fetch and cache the current user's email for the From header.

        Raises:
            RuntimeError if unable to get user email
        """
        response = self.pagerduty_session.get(f'users/{self.user_id}')
        data = response.json()
        email = data.get('user', {}).get('email')
        if not email:
            raise RuntimeError("Unable to get user email from PagerDuty API")
        return email

    @staticmethod
    def _is_silent_ack(title: str) -> bool:
        """Check if the incident title matches a silent-acknowledge pattern.

        Returns True if the incident should be acknowledged without posting a comment.
        """
        title_lower = title.lower()
        return any(pattern.lower() in title_lower for pattern in SILENT_ACK_PATTERNS)

    def _pick_random_comment(self) -> str:
        """Pick a random comment phrase, with 20% typo chance and 50% lowercase chance."""
        if random.random() < 0.2:
            comment = random.choice(COMMENTS_TYPO)
        else:
            comment = random.choice(COMMENTS_NORMAL)
        # 50% chance to lowercase the first character
        if random.random() < 0.5:
            comment = comment[0].lower() + comment[1:]
        return comment

    def log_needs_attention(self, incident_id: str, incident_title: str, incident_url: str) -> None:
        """
        Log an incident that needs attention to the output file.

        Args:
            incident_id: Incident ID
            incident_title: Incident title
            incident_url: Incident URL
        """
        try:
            self.output_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.output_file, 'a') as f:
                timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
                f.write(f"[{timestamp}] need to look at: {incident_url}\n")
                f.write(f"  ID: {incident_id}\n")
                f.write(f"  Title: {incident_title}\n\n")
        except IOError as e:
            print(f"Warning: Failed to write to output file: {e}", file=sys.stderr)

    def get_triggered_incidents(self) -> List[Dict]:
        """
        Get all triggered incidents assigned to the current user.

        Returns:
            List of incident dictionaries
        """
        params = {
            'statuses[]': ['triggered'],
            'user_ids[]': [self.user_id],
            'sort_by': 'created_at:desc',
        }

        try:
            incidents = self.pagerduty_session.list_all('incidents', params=params)
            return list(incidents)
        except pagerduty.Error as e:
            print(f"Error fetching incidents: {e}", file=sys.stderr)
            return []

    def get_incident_notes(self, incident_id: str) -> List[Dict]:
        """
        Get all notes for an incident.

        Args:
            incident_id: Incident ID

        Returns:
            List of note dictionaries
        """
        try:
            notes = self.pagerduty_session.list_all(f'incidents/{incident_id}/notes')
            return list(notes)
        except pagerduty.Error as e:
            print(f"Error fetching notes for {incident_id}: {e}", file=sys.stderr)
            return []

    def check_has_comments(self, incident_id: str) -> bool:
        """
        Check if the incident has any comments.

        Args:
            incident_id: Incident ID

        Returns:
            True if incident has comments, False otherwise
        """
        notes = self.get_incident_notes(incident_id)
        return len(notes) > 0

    def check_has_working_comment(self, incident_id: str) -> bool:
        """
        Check if any comment contains a known auto-comment phrase (case-insensitive).

        When random_comments is active, checks all phrases from ALL_COMMENTS.
        Otherwise falls back to matching the single custom pattern.

        Args:
            incident_id: Incident ID

        Returns:
            True if pattern found in any comment, False otherwise
        """
        notes = self.get_incident_notes(incident_id)
        if not notes:
            return False

        if self.random_comments:
            # Check all possible auto-comment phrases
            phrases_lower = [p.lower() for p in ALL_COMMENTS]
            for note in notes:
                content = note.get('content', '').lower()
                for phrase in phrases_lower:
                    if phrase in content:
                        return True
        else:
            # Original behavior: match single custom pattern
            pattern_lower = self.comment_pattern.lower()
            for note in notes:
                content = note.get('content', '').lower()
                if pattern_lower in content:
                    return True
        return False

    def add_note_to_incident(self, incident_id: str, note_content: str) -> bool:
        """
        Add a note to an incident.

        Args:
            incident_id: Incident ID
            note_content: Note content

        Returns:
            True if successful, False otherwise
        """
        if self.dry_run:
            return True

        try:
            note_data = {'note': {'content': note_content}}
            self.pagerduty_session.rpost(f'incidents/{incident_id}/notes', json=note_data)
            return True
        except pagerduty.Error as e:
            print(f"Error adding note to {incident_id}: {e}", file=sys.stderr)
            return False

    def acknowledge_incident(self, incident_id: str) -> bool:
        """
        Acknowledge an incident.

        Args:
            incident_id: Incident ID

        Returns:
            True if successful, False otherwise
        """
        if self.dry_run:
            return True

        try:
            ack_data = {
                'incidents': [
                    {
                        'id': incident_id,
                        'type': 'incident_reference',
                        'status': 'acknowledged'
                    }
                ]
            }

            headers = {'From': self.user_email}
            self.pagerduty_session.rput('incidents', json=ack_data, headers=headers)
            return True
        except pagerduty.Error as e:
            print(f"Error acknowledging incident {incident_id}: {e}", file=sys.stderr)
            return False
        except Exception as e:
            print(f"Error acknowledging incident {incident_id}: {e}", file=sys.stderr)
            return False

    def process_incident(self, incident: Dict) -> Dict:
        """
        Process a triggered incident according to the monitoring logic.

        Args:
            incident: Incident dictionary

        Returns:
            Result dictionary: {'success': bool, 'action': str, 'message': str, 'url': str, 'comment_added': bool, 'logged_to_file': bool}
        """
        incident_id = incident['id']
        incident_title = incident.get('title', 'Unknown')
        incident_url = incident.get('html_url', '')

        # Check if we've already processed this incident in this run
        if incident_id in self.processed_incidents:
            return {
                'success': False,
                'action': 'already_processed',
                'message': f"Already processed {incident_id} in this run",
                'url': incident_url,
                'comment_added': False,
                'logged_to_file': False
            }

        # Check if incident has any comments
        has_comments = self.check_has_comments(incident_id)

        # Silent-ack incidents: acknowledge only, never post a comment
        silent = self._is_silent_ack(incident_title)

        if not has_comments and not silent:
            # No comments and not a silent-ack pattern — add comment and acknowledge
            comment_text = self._pick_random_comment() if self.random_comments else self.comment_pattern

            if self.dry_run:
                self.processed_incidents.add(incident_id)
                return {
                    'success': True,
                    'action': 'new_incident',
                    'message': f"[DRY RUN] Would add '{comment_text}' and acknowledge {incident_id}",
                    'url': incident_url,
                    'comment_added': True,
                    'logged_to_file': False
                }

            # Add comment
            if not self.add_note_to_incident(incident_id, comment_text):
                return {
                    'success': False,
                    'action': 'new_incident',
                    'message': f"Failed to add comment to {incident_id}",
                    'url': incident_url,
                    'comment_added': False,
                    'logged_to_file': False
                }

            # Acknowledge
            if not self.acknowledge_incident(incident_id):
                return {
                    'success': False,
                    'action': 'new_incident',
                    'message': f"Failed to acknowledge {incident_id}",
                    'url': incident_url,
                    'comment_added': True,
                    'logged_to_file': False
                }

            self.processed_incidents.add(incident_id)
            return {
                'success': True,
                'action': 'new_incident',
                'message': f"Added '{comment_text}' and acknowledged",
                'url': incident_url,
                'comment_added': True,
                'logged_to_file': False
            }
        elif not has_comments and silent:
            # Silent-ack pattern — acknowledge without posting a comment
            if self.dry_run:
                self.processed_incidents.add(incident_id)
                return {
                    'success': True,
                    'action': 'silent_ack',
                    'message': f"[DRY RUN] Would acknowledge {incident_id} (silent-ack pattern, no comment)",
                    'url': incident_url,
                    'comment_added': False,
                    'logged_to_file': False
                }

            if not self.acknowledge_incident(incident_id):
                return {
                    'success': False,
                    'action': 'silent_ack',
                    'message': f"Failed to acknowledge {incident_id}",
                    'url': incident_url,
                    'comment_added': False,
                    'logged_to_file': False
                }

            self.processed_incidents.add(incident_id)
            return {
                'success': True,
                'action': 'silent_ack',
                'message': f"Acknowledged (silent-ack, no comment)",
                'url': incident_url,
                'comment_added': False,
                'logged_to_file': False
            }
        else:
            # Has comments - check if "working on it" exists
            has_working = self.check_has_working_comment(incident_id)

            if has_working:
                # "working on it" exists - acknowledge and log to file
                if self.dry_run:
                    self.processed_incidents.add(incident_id)
                    return {
                        'success': True,
                        'action': 'needs_attention',
                        'message': f"[DRY RUN] Would acknowledge {incident_id} and log to file",
                        'url': incident_url,
                        'comment_added': False,
                        'logged_to_file': True
                    }

                # Acknowledge
                if not self.acknowledge_incident(incident_id):
                    return {
                        'success': False,
                        'action': 'needs_attention',
                        'message': f"Failed to acknowledge {incident_id}",
                        'url': incident_url,
                        'comment_added': False,
                        'logged_to_file': False
                    }

                # Log to file
                self.log_needs_attention(incident_id, incident_title, incident_url)

                self.processed_incidents.add(incident_id)
                return {
                    'success': True,
                    'action': 'needs_attention',
                    'message': f"Acknowledged and logged to file",
                    'url': incident_url,
                    'comment_added': False,
                    'logged_to_file': True
                }
            else:
                # Has comments but not "working on it" - only acknowledge (no comment)
                if self.dry_run:
                    self.processed_incidents.add(incident_id)
                    return {
                        'success': True,
                        'action': 'acknowledge_only',
                        'message': f"[DRY RUN] Would acknowledge {incident_id} (has other comments)",
                        'url': incident_url,
                        'comment_added': False,
                        'logged_to_file': False
                    }

                # Acknowledge only
                if not self.acknowledge_incident(incident_id):
                    return {
                        'success': False,
                        'action': 'acknowledge_only',
                        'message': f"Failed to acknowledge {incident_id}",
                        'url': incident_url,
                        'comment_added': False,
                        'logged_to_file': False
                    }

                self.processed_incidents.add(incident_id)
                return {
                    'success': True,
                    'action': 'acknowledge_only',
                    'message': f"Acknowledged (has other comments)",
                    'url': incident_url,
                    'comment_added': False,
                    'logged_to_file': False
                }

    def check_incidents_once(self) -> Dict:
        """
        Check triggered incidents once and process them.

        Returns:
            Summary dictionary with results
        """
        # Clear processed set so re-triggered incidents get re-acknowledged.
        # PagerDuty auto-un-acknowledges after ~30 min of inactivity,
        # flipping the incident back to "triggered".  Without this reset
        # the monitor would skip it forever as "already_processed".
        self.processed_incidents.clear()

        # Get triggered incidents
        incidents = self.get_triggered_incidents()

        if not incidents:
            return {
                'total': 0,
                'new_incidents': 0,
                'needs_attention': 0,
                'acknowledged': 0,
                'silent_ack': 0,
                'already_processed': 0,
                'errors': []
            }

        summary = {
            'total': len(incidents),
            'new_incidents': 0,
            'needs_attention': 0,
            'acknowledged': 0,
            'silent_ack': 0,
            'already_processed': 0,
            'errors': []
        }

        for incident in incidents:
            incident_id = incident['id']
            incident_title = incident.get('title', 'Unknown')

            if self.verbose:
                print(f"\n  Checking: {incident_title[:60]}...")
                print(f"  ID: {incident_id}")

            # Process incident
            result = self.process_incident(incident)

            if result['success']:
                # Print URL and action taken
                print(f"  {result['url']}")
                action_detail = []
                if result.get('comment_added'):
                    action_detail.append("comment added")
                if result.get('logged_to_file'):
                    action_detail.append("logged to file")
                if not result.get('comment_added') and not result.get('logged_to_file'):
                    action_detail.append("acknowledged")
                print(f"  → {result['message']} ({', '.join(action_detail)})")

                # Update summary
                if result['action'] == 'new_incident':
                    summary['new_incidents'] += 1
                elif result['action'] == 'needs_attention':
                    summary['needs_attention'] += 1
                elif result['action'] == 'silent_ack':
                    summary['silent_ack'] += 1
                elif result['action'] == 'acknowledge_only':
                    summary['acknowledged'] += 1
            else:
                if result['action'] == 'already_processed':
                    summary['already_processed'] += 1
                else:
                    summary['errors'].append(result['message'])
                    print(f"  {result['url']}")
                    print(f"  ❌ {result['message']}")

        return summary

    def _draw_progress_bar(self, elapsed: float, total: float, width: int = 30) -> str:
        """
        Draw a progress bar.

        Args:
            elapsed: Elapsed time in seconds
            total: Total duration in seconds
            width: Width of the progress bar

        Returns:
            Formatted progress bar string
        """
        percent = min(100, int((elapsed / total) * 100))
        filled = int((width * elapsed) / total)
        bar = '=' * filled + '>' + ' ' * (width - filled - 1)

        remaining_seconds = int(total - elapsed)
        remaining_minutes = remaining_seconds // 60
        remaining_secs = remaining_seconds % 60

        return f"[{bar}] {percent}% | {remaining_minutes}m {remaining_secs}s remaining"

    def monitor_continuously(self, duration_minutes: int = 60) -> None:
        """
        Monitor incidents continuously for specified duration.

        Args:
            duration_minutes: How long to run (default 60 minutes)
        """
        start_time = time.time()
        end_time = start_time + (duration_minutes * 60)
        total_duration = duration_minutes * 60
        check_count = 0

        print(f"Starting continuous monitoring for {duration_minutes} minutes...")
        print(f"Checking every {self.check_interval_seconds} seconds")
        print(f"User ID: {self.user_id}")
        print(f"Output file: {self.output_file}")
        print("=" * 60)
        print()  # Extra line before progress bar

        try:
            while time.time() < end_time:
                check_count += 1
                current_time = time.time()
                elapsed = current_time - start_time

                # Display progress (only in details mode)
                if self.details:
                    remaining_seconds = int(end_time - current_time)
                    remaining_minutes = remaining_seconds // 60
                    remaining_secs = remaining_seconds % 60
                    print(f"\n[Check #{check_count}] Time remaining: {remaining_minutes}m {remaining_secs}s")
                    print("-" * 60)

                # Check incidents
                summary = self.check_incidents_once()

                # Display summary
                if summary['total'] > 0:
                    # Clear progress bar line and show incident info
                    if not self.background:
                        print("\r" + " " * 80 + "\r", end='')  # Clear line
                    print(f"\n  Found {summary['total']} triggered incident(s)")
                    if summary['new_incidents'] > 0:
                        print(f"  ✓ New incidents (comment added): {summary['new_incidents']}")
                    if summary['silent_ack'] > 0:
                        print(f"  ✓ Silent ack (no comment): {summary['silent_ack']}")
                    if summary['acknowledged'] > 0:
                        print(f"  ✓ Acknowledged (no comment): {summary['acknowledged']}")
                    if summary['needs_attention'] > 0:
                        print(f"  ⚠️  Need attention (logged to file): {summary['needs_attention']}")
                    if summary['errors']:
                        print(f"  ❌ Errors: {len(summary['errors'])}")
                    print()  # Extra line after incident info
                else:
                    # Only show "no incidents" in details mode
                    if self.details:
                        print("  No triggered incidents found")

                # Wait for next check (unless we're at the end)
                if time.time() < end_time:
                    sleep_time = min(self.check_interval_seconds, end_time - time.time())
                    if sleep_time > 0:
                        if self.background:
                            # Background mode: plain sleep, no \r progress bar
                            time.sleep(sleep_time)
                        elif self.details:
                            # Details mode: show traditional countdown
                            for i in range(int(sleep_time), 0, -1):
                                mins = i // 60
                                secs = i % 60
                                print(f"\r  Next check in: {mins}m {secs}s  ", end='', flush=True)
                                time.sleep(1)
                            print()  # New line after countdown
                        else:
                            # Normal mode: show progress bar
                            for i in range(int(sleep_time), 0, -1):
                                current_elapsed = time.time() - start_time
                                progress_bar = self._draw_progress_bar(current_elapsed, total_duration)
                                print(f"\r{progress_bar} | Checking...  ", end='', flush=True)
                                time.sleep(1)

        except KeyboardInterrupt:
            print("\n\n⚠️  Monitoring interrupted by user")
            raise

        # Clear progress bar and show completion
        if not self.background:
            print("\r" + " " * 80 + "\r", end='')  # Clear line
        print("\n" + "=" * 60)
        print(f"Monitoring completed after {check_count} checks")
        print("=" * 60)


def load_config() -> Dict:
    """Load configuration from environment variables."""
    config = {
        'pagerduty_api_token': os.environ.get('PAGERDUTY_API_TOKEN'),
        'comment_pattern': os.environ.get('MONITOR_COMMENT_PATTERN', 'working on it'),
        'check_interval_seconds': int(os.environ.get('MONITOR_CHECK_INTERVAL_SECONDS', '30')),
        'output_file': os.environ.get('MONITOR_OUTPUT_FILE', '~/pd-monitor-needs-attention.txt'),
        'dry_run': os.environ.get('MONITOR_DRY_RUN', 'false').lower() == 'true',
        'verbose': os.environ.get('MONITOR_VERBOSE', 'false').lower() == 'true',
        'details': False,
        'background': False,
    }

    return config


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='PagerDuty Monitor - Auto-acknowledge triggered incidents',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run for 1 hour (default)
  %(prog)s

  # Run for 30 minutes
  %(prog)s --duration 30

  # Dry run (simulate without changes)
  %(prog)s --dry-run --verbose

  # Check once and exit
  %(prog)s --once

  # Override check interval
  %(prog)s --interval 60
        """
    )

    parser.add_argument(
        '--version',
        action='version',
        version=f'%(prog)s {VERSION}'
    )
    parser.add_argument(
        '-n', '--dry-run',
        action='store_true',
        help='Dry run (simulate without making changes)'
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Verbose output'
    )
    parser.add_argument(
        '-d', '--duration',
        type=int,
        default=None,
        help='How long to run in minutes (default: interactive menu)'
    )
    parser.add_argument(
        '-i', '--interval',
        type=int,
        help='Override check interval (seconds)'
    )
    parser.add_argument(
        '-p', '--pattern',
        type=str,
        help='Override comment pattern (default: "working on it")'
    )
    parser.add_argument(
        '-o', '--output',
        type=str,
        help='Override output file path'
    )
    parser.add_argument(
        '--once',
        action='store_true',
        help='Check once and exit (no continuous monitoring)'
    )
    parser.add_argument(
        '--details',
        action='store_true',
        help='Show detailed check information (default: minimal output)'
    )
    parser.add_argument(
        '--background',
        action='store_true',
        help='Background mode: skip duration menu, suppress progress bar (used by noc-toolkit)'
    )

    return parser.parse_args()


def show_duration_menu() -> int:
    """
    Show interactive menu for duration selection.

    Returns:
        Selected duration in minutes
    """
    print("\n" + "=" * 60)
    print("Select Monitoring Duration")
    print("=" * 60)
    print("  1. 1 hour (60 minutes)")
    print("  2. 2 hours (120 minutes)")
    print("  3. 4 hours (240 minutes)")
    print("  4. 8 hours (480 minutes)")
    print("  5. 12 hours (720 minutes)")
    print("  6. Custom (enter minutes)")
    print("=" * 60)

    while True:
        try:
            choice = input("\nSelect option [1-6]: ").strip()

            if choice == '1':
                return 60
            elif choice == '2':
                return 120
            elif choice == '3':
                return 240
            elif choice == '4':
                return 480
            elif choice == '5':
                return 720
            elif choice == '6':
                # Custom duration
                while True:
                    try:
                        custom = input("Enter duration in minutes (1-720): ").strip()
                        duration = int(custom)
                        if 1 <= duration <= 720:
                            return duration
                        else:
                            print("❌ Please enter a value between 1 and 720 minutes.")
                    except ValueError:
                        print("❌ Invalid input. Please enter a number.")
            else:
                print("❌ Invalid choice. Please enter a number between 1 and 6.")

        except KeyboardInterrupt:
            print("\n\n⚠️  Interrupted by user.")
            sys.exit(130)


def main() -> None:
    """Main entry point."""
    # Load environment variables
    load_dotenv()

    # Parse arguments
    args = parse_args()

    # Load config
    config = load_config()

    # Validate API token
    if not config['pagerduty_api_token']:
        print("Error: PAGERDUTY_API_TOKEN not found in environment", file=sys.stderr)
        print("\nPlease set this in your .env file or environment.", file=sys.stderr)
        sys.exit(1)

    # Override config with CLI arguments
    if args.pattern:
        config['comment_pattern'] = args.pattern
    if args.interval:
        config['check_interval_seconds'] = args.interval
    if args.output:
        config['output_file'] = args.output
    if args.dry_run:
        config['dry_run'] = True
    if args.verbose:
        config['verbose'] = True
    if args.details:
        config['details'] = True
    else:
        config['details'] = False
    if args.background:
        config['background'] = True

    # Create monitor instance
    try:
        monitor = PagerDutyMonitor(**config)
    except (RuntimeError, pagerduty.Error) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # If duration not specified and not single-check mode, show menu
    if args.duration is None and not args.once:
        if args.background:
            args.duration = 60  # Default for background mode
        else:
            args.duration = show_duration_menu()

    # Print header
    print("=" * 60)
    print("PagerDuty Monitor - Triggered Incident Handler")
    print("=" * 60)
    if config['comment_pattern'].lower() == 'working on it':
        print(f"Comment mode: randomized ({len(COMMENTS_NORMAL)} phrases + {len(COMMENTS_TYPO)} typo variants)")
    else:
        print(f"Comment pattern: \"{config['comment_pattern']}\"")
    print(f"Mode: {'DRY RUN' if config['dry_run'] else 'LIVE'}")

    if args.once:
        print(f"Mode: Single check")
    else:
        print(f"Duration: {args.duration} minutes")
        print(f"Check interval: {config['check_interval_seconds']} seconds")

    print("=" * 60)

    # Run monitor
    try:
        if args.once:
            # Single check mode
            print("\nPerforming single check...")
            summary = monitor.check_incidents_once()

            # Print summary
            print("\n" + "=" * 60)
            print("Summary")
            print("=" * 60)
            print(f"Total triggered incidents: {summary['total']}")
            print(f"New incidents (comment added): {summary['new_incidents']}")
            print(f"Silent ack (no comment): {summary['silent_ack']}")
            print(f"Acknowledged (no comment): {summary['acknowledged']}")
            print(f"Need attention (logged to file): {summary['needs_attention']}")
            print(f"Errors: {len(summary['errors'])}")

            if summary['errors']:
                print("\n❌ Errors:")
                for error in summary['errors']:
                    print(f"  - {error}")

            print("=" * 60)
        else:
            # Continuous monitoring mode
            monitor.monitor_continuously(duration_minutes=args.duration)

    except KeyboardInterrupt:
        print("\n\nInterrupted by user", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
