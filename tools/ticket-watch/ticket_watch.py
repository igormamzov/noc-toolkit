#!/usr/bin/env python3
"""
Ticket Watch — Escalated Ticket Monitor

Monitors Jira escalation tickets (DSSD by default) and:
1. Reports tickets that remain Unassigned after a configurable threshold (default: 4h)
2. Pings assignees on stale tickets where no comment was posted for N days (default: 3)
3. Detects repeat pings and shows the last assignee response in the report
"""

import logging
import os
import random
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Version information
VERSION = "0.1.0"

try:
    from jira.exceptions import JIRAError
    from noc_utils import require_env, new_jira_client, setup_logging
except ImportError as import_error:
    logging.basicConfig()
    logging.error("Missing required dependencies. Please run: pip install -r requirements.txt")
    logging.error("Details: %s", import_error)
    sys.exit(1)

logger = setup_logging(name=__name__)

# Phrases for pinging assignees — randomly selected per ticket
PING_PHRASES: List[str] = [
    "Could you please provide an update on this ticket?",
    "Hi! Any progress on this issue?",
    "Just checking in — is there any update here?",
    "Could you share the current status of this ticket?",
    "Hi! Could you let us know how this is progressing?",
    "Any updates on this? Please let us know the current status.",
    "Could you please update us on the status of this issue?",
    "Checking in on this ticket — any updates?",
    "Hi! We'd appreciate an update on this when you get a chance.",
    "Any news on this ticket? An update would be appreciated.",
    "Following up — could you please provide a status update?",
]

# Keywords used to detect our own ping comments
PING_KEYWORDS: List[str] = [
    "update", "progress", "status", "checking in", "following up",
]


class TicketWatch:
    """Monitors Jira escalation tickets for unassigned and stale states."""

    def __init__(
        self,
        jira_server_url: str,
        jira_personal_access_token: str,
        reporters: List[str],
        project: str = "DSSD",
        unassigned_hours: float = 4.0,
        stale_days: int = 3,
        dry_run: bool = False,
        no_comment: bool = False,
    ) -> None:
        """
        Initialize with Jira credentials and configuration.

        Args:
            jira_server_url: Jira server URL
            jira_personal_access_token: Jira Server/DC PAT
            reporters: List of reporter display names to filter by
            project: Jira project key (default: DSSD)
            unassigned_hours: Hours before unassigned ticket is flagged (default: 4)
            stale_days: Days without comment before assignee is pinged (default: 3)
            dry_run: If True, don't post comments
            no_comment: If True, skip comment posting entirely
        """
        self.dry_run = dry_run
        self.no_comment = no_comment
        self.project = project
        self.reporters = reporters
        self.unassigned_hours = unassigned_hours
        self.stale_days = stale_days
        self.jira_client, self.jira_base_url = new_jira_client(
            jira_server_url, jira_personal_access_token,
        )
        # Resolve current user for detecting our own comments
        self.current_user: Optional[str] = None
        self._resolve_current_user()

    def _resolve_current_user(self) -> None:
        """Resolve the display name of the authenticated Jira user."""
        try:
            current_user = self.jira_client.myself()
            self.current_user = current_user.get("displayName", "")
        except JIRAError:
            # Non-critical — we can still function without it
            self.current_user = None

    def search_tickets(self) -> List[Any]:
        """
        Search Jira for escalation tickets matching criteria.

        Returns:
            List of Jira issue objects
        """
        # Build reporter filter — quote names for JQL
        reporter_list = ", ".join(f'"{name}"' for name in self.reporters)

        jql = (
            f"project = {self.project} "
            f"AND type = Escalation "
            f"AND reporter IN ({reporter_list}) "
            f"AND status NOT IN (Done, Closed, Resolved) "
            f"AND created >= -90d "
            f"ORDER BY created DESC"
        )

        try:
            issues = self.jira_client.search_issues(
                jql,
                fields="summary,status,assignee,created,comment,reporter",
                maxResults=200,
            )
            return list(issues)
        except JIRAError as error:
            raise RuntimeError(f"JQL search failed: {error}") from error

    def classify_ticket(self, issue: Any) -> Dict[str, Any]:
        """
        Classify a ticket into: unassigned, stale, pinged, or ok.

        Args:
            issue: Jira issue object

        Returns:
            Dict with classification details
        """
        now = datetime.now(timezone.utc)

        # Parse created timestamp
        created_str = issue.fields.created
        created_dt = self._parse_jira_datetime(created_str)
        age_hours = (now - created_dt).total_seconds() / 3600

        # Assignee check
        assignee = issue.fields.assignee
        assignee_name = assignee.displayName if assignee else None

        result: Dict[str, Any] = {
            "key": issue.key,
            "summary": issue.fields.summary,
            "status": str(issue.fields.status),
            "assignee": assignee_name,
            "created": created_dt,
            "age_hours": age_hours,
            "category": "ok",
            "last_comment_date": None,
            "days_since_comment": None,
            "is_repeat_ping": False,
            "ping_count": 0,
            "last_assignee_response": None,
        }

        # Category A: Unassigned and older than threshold
        if not assignee_name and age_hours >= self.unassigned_hours:
            result["category"] = "unassigned"
            return result

        # For assigned tickets — check comment staleness
        if assignee_name:
            comments = issue.fields.comment.comments if issue.fields.comment else []
            last_comment_date = self._get_last_comment_date(comments)
            result["last_comment_date"] = last_comment_date

            if last_comment_date:
                days_since = (now - last_comment_date).total_seconds() / 86400
                result["days_since_comment"] = days_since

                if days_since >= self.stale_days:
                    # Check if we already pinged
                    ping_count = self._count_our_pings(comments)
                    result["ping_count"] = ping_count

                    if ping_count > 0:
                        result["category"] = "pinged"
                        result["is_repeat_ping"] = True
                        last_response = self._get_last_assignee_response(
                            comments, assignee_name
                        )
                        result["last_assignee_response"] = last_response
                    else:
                        result["category"] = "stale"
            else:
                # No comments at all — check if ticket is old enough
                if age_hours >= self.stale_days * 24:
                    result["category"] = "stale"
                    result["days_since_comment"] = age_hours / 24

        return result

    def post_ping(self, issue_key: str, assignee_name: str) -> str:
        """
        Post a ping comment on a Jira ticket.

        Args:
            issue_key: Jira issue key
            assignee_name: Assignee display name for mention

        Returns:
            The phrase that was posted
        """
        phrase = random.choice(PING_PHRASES)
        # Use [~displayName] for Jira Server/DC mention
        comment_body = f"[~{assignee_name}] {phrase}"

        if self.dry_run:
            logger.info("  [DRY-RUN] Would comment on %s: %s", issue_key, comment_body)
            return phrase

        try:
            self.jira_client.add_comment(issue_key, comment_body)
            logger.info("  Commented on %s", issue_key)
            return phrase
        except JIRAError as error:
            logger.warning("  Warning: Failed to comment on %s: %s", issue_key, error)
            return phrase

    def search_chicken_curry(self) -> List[Any]:
        """
        Search for stale tickets assigned to a specific person across all projects.

        Returns:
            List of Jira issue objects
        """
        jql = (
            'assignee = "Basavaraj.Swamy" '
            "AND type != Epic "
            "AND status NOT IN (Done, Closed, Resolved) "
            "AND updated <= -3d "
            "ORDER BY updated ASC"
        )

        try:
            issues = self.jira_client.search_issues(
                jql,
                fields="summary,status,assignee,created,comment,reporter,project",
                maxResults=200,
            )
            return list(issues)
        except JIRAError as error:
            raise RuntimeError(f"Chicken curry search failed: {error}") from error

    def run_chicken_curry(self) -> List[Dict[str, Any]]:
        """
        Execute the chicken curry poop party mode.

        Returns:
            List of stale ticket results
        """
        mode_label = "[DRY-RUN] " if self.dry_run else ""
        logger.info("\n%sTicket Watch v%s", mode_label, VERSION)
        logger.info("=" * 50)
        logger.info("  CHICKEN CURRY MODE")
        logger.info("  Target: Basavaraj.Swamy | All projects | Stale > 3d")
        logger.info("=" * 50)

        logger.info("\nSearching all projects...")
        issues = self.search_chicken_curry()
        logger.info("Found %d stale tickets", len(issues))

        if not issues:
            logger.info("\nNo stale tickets found. Impressive!")
            return []

        results: List[Dict[str, Any]] = []
        for issue in issues:
            result = self.classify_ticket(issue)
            # In this mode, all returned tickets are stale by JQL — force category
            if result["category"] == "ok":
                result["category"] = "stale"
                result["days_since_comment"] = result["age_hours"] / 24
            results.append(result)

        # Post pings
        if not self.no_comment:
            for result in results:
                if result["assignee"]:
                    result["pinged_phrase"] = self.post_ping(
                        result["key"], result["assignee"]
                    )

        # Print report
        self._print_report(results)
        return results

    def run(self) -> List[Dict[str, Any]]:
        """
        Execute the full ticket watch workflow.

        Returns:
            List of classified ticket results
        """
        mode_label = "[DRY-RUN] " if self.dry_run else ""
        logger.info("\n%sTicket Watch v%s", mode_label, VERSION)
        logger.info("=" * 50)
        logger.info("Project: %s", self.project)
        logger.info("Reporters: %s", ", ".join(self.reporters))
        logger.info("Unassigned threshold: %sh", self.unassigned_hours)
        logger.info("Stale threshold: %s days", self.stale_days)

        # Step 1: Search tickets
        logger.info("\nSearching %s tickets...", self.project)
        issues = self.search_tickets()
        logger.info("Found %d open tickets", len(issues))

        if not issues:
            logger.info("\nNo tickets to report.")
            return []

        # Step 2: Classify each ticket
        results: List[Dict[str, Any]] = []
        for issue in issues:
            result = self.classify_ticket(issue)
            if result["category"] != "ok":
                results.append(result)

        # Step 3: Post pings for stale/pinged tickets
        if not self.no_comment:
            for result in results:
                if result["category"] in ("stale", "pinged") and result["assignee"]:
                    result["pinged_phrase"] = self.post_ping(
                        result["key"], result["assignee"]
                    )

        # Step 4: Generate report
        self._print_report(results)

        return results

    def _print_report(self, results: List[Dict[str, Any]]) -> None:
        """Print the formatted report to stdout."""
        unassigned = [r for r in results if r["category"] == "unassigned"]
        stale = [r for r in results if r["category"] == "stale"]
        pinged = [r for r in results if r["category"] == "pinged"]

        total = len(unassigned) + len(stale) + len(pinged)

        logger.info("\n%s", "=" * 50)
        logger.info("  Escalated Ticket Watch Report")
        logger.info("%s", "=" * 50)

        if total == 0:
            logger.info("\nAll tickets are in good shape. Nothing to report.")
            return

        counter = 0

        if unassigned:
            logger.info("\n--- UNASSIGNED (created > %sh ago) ---", self.unassigned_hours)
            for ticket in unassigned:
                counter += 1
                age_str = self._format_age(ticket["age_hours"])
                created_str = ticket["created"].strftime("%Y-%m-%d %H:%M UTC")
                logger.info("\n%d. %s/%s", counter, self.jira_base_url, ticket["key"])
                logger.info("   Summary: %s", ticket["summary"])
                logger.info("   Created: %s (%s)", created_str, age_str)
                logger.info("   Status: Unassigned")

        if stale:
            logger.info("\n--- STALE (no update > %s days) ---", self.stale_days)
            for ticket in stale:
                counter += 1
                days_str = self._format_days(ticket["days_since_comment"])
                logger.info("\n%d. %s/%s", counter, self.jira_base_url, ticket["key"])
                logger.info("   Summary: %s", ticket["summary"])
                logger.info("   Assignee: %s", ticket["assignee"])
                if ticket["last_comment_date"]:
                    comment_date = ticket["last_comment_date"].strftime("%Y-%m-%d")
                    logger.info("   Last comment: %s (%s)", comment_date, days_str)
                else:
                    logger.info("   Last comment: none (%s)", days_str)
                if not self.no_comment:
                    logger.info("   Action: Pinged assignee")

        if pinged:
            logger.info("\n--- REPEAT PING (previously pinged, still stale) ---")
            for ticket in pinged:
                counter += 1
                days_str = self._format_days(ticket["days_since_comment"])
                ping_label = f"{ticket['ping_count'] + 1}{'st' if ticket['ping_count'] == 0 else 'nd' if ticket['ping_count'] == 1 else 'rd' if ticket['ping_count'] == 2 else 'th'} ping"
                logger.info("\n%d. %s/%s", counter, self.jira_base_url, ticket["key"])
                logger.info("   Summary: %s", ticket["summary"])
                logger.info("   Assignee: %s", ticket["assignee"])
                if ticket["last_comment_date"]:
                    comment_date = ticket["last_comment_date"].strftime("%Y-%m-%d")
                    logger.info("   Last comment: %s (%s)", comment_date, days_str)
                else:
                    logger.info("   Last comment: none (%s)", days_str)
                if not self.no_comment:
                    logger.info("   Action: Repeat ping (%s)", ping_label)
                if ticket["last_assignee_response"]:
                    response_text = ticket["last_assignee_response"]["body"]
                    response_date = ticket["last_assignee_response"]["date"]
                    if len(response_text) > 70:
                        response_text = response_text[:70] + "..."
                    date_str = response_date.strftime("%Y-%m-%d")
                    logger.info("   Last assignee response (%s): \"%s\"", date_str, response_text)

        logger.info("\n%s", "=" * 50)
        logger.info("Total: %d ticket(s) need attention", total)
        logger.info("  Unassigned: %d", len(unassigned))
        logger.info("  Stale: %d", len(stale))
        logger.info("  Repeat ping: %d", len(pinged))

    def _get_last_comment_date(self, comments: List[Any]) -> Optional[datetime]:
        """Get the datetime of the most recent comment."""
        if not comments:
            return None
        last_comment = comments[-1]
        return self._parse_jira_datetime(last_comment.created)

    def _count_our_pings(self, comments: List[Any]) -> int:
        """Count how many of our ping comments exist on the ticket."""
        count = 0
        for comment in comments:
            if self._is_our_ping(comment):
                count += 1
        return count

    def _is_our_ping(self, comment: Any) -> bool:
        """
        Detect if a comment is one of our ping messages.

        Checks: authored by current user, starts with [~, and contains ping keywords.
        """
        author_name = getattr(comment, "author", None)
        if author_name:
            author_name = getattr(author_name, "displayName", str(author_name))

        # If we know current user, filter by author
        if self.current_user and author_name != self.current_user:
            return False

        body = getattr(comment, "body", "")
        if not body.startswith("[~"):
            return False

        body_lower = body.lower()
        return any(keyword in body_lower for keyword in PING_KEYWORDS)

    def _get_last_assignee_response(
        self, comments: List[Any], assignee_name: str
    ) -> Optional[Dict[str, Any]]:
        """
        Find the last comment by the assignee (excluding our pings).

        Returns:
            Dict with 'body' and 'date' keys, or None
        """
        for comment in reversed(comments):
            author = getattr(comment, "author", None)
            if author:
                author_display = getattr(author, "displayName", str(author))
                if author_display == assignee_name:
                    return {
                        "body": getattr(comment, "body", ""),
                        "date": self._parse_jira_datetime(comment.created),
                    }
        return None

    @staticmethod
    def _parse_jira_datetime(datetime_str: str) -> datetime:
        """
        Parse Jira datetime string to timezone-aware datetime.

        Handles formats: '2026-03-20T08:30:00.000+0000' and '2026-03-20T08:30:00.000+00:00'
        """
        # Normalize timezone offset: remove colon if present in offset
        clean = str(datetime_str)
        # Handle milliseconds + timezone like '2026-03-20T08:30:15.123+0000'
        if "+" in clean and clean.index("+") > 10:
            base, tz_part = clean.rsplit("+", 1)
            tz_part = tz_part.replace(":", "")
            clean = f"{base}+{tz_part}"
        elif clean.endswith("Z"):
            clean = clean[:-1] + "+0000"

        # Try parsing with milliseconds
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                return datetime.strptime(clean, fmt)
            except ValueError:
                continue

        raise ValueError(f"Cannot parse Jira datetime: {datetime_str}")

    @staticmethod
    def _format_age(hours: float) -> str:
        """Format age in hours to a human-readable string."""
        if hours < 1:
            minutes = int(hours * 60)
            return f"{minutes}m ago"
        if hours < 24:
            return f"{hours:.0f}h ago"
        days = hours / 24
        return f"{days:.0f}d {int(hours % 24)}h ago"

    @staticmethod
    def _format_days(days: Optional[float]) -> str:
        """Format days to a human-readable string."""
        if days is None:
            return "unknown"
        if days < 1:
            hours = int(days * 24)
            return f"{hours}h ago"
        return f"{days:.0f} days ago"


def main() -> None:
    """Main entry point for the CLI tool."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Ticket Watch — Monitor escalation tickets for unassigned/stale states",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s --dry-run\n"
            "  %(prog)s --no-comment\n"
            "  %(prog)s --project DSSD --dry-run\n"
        ),
    )
    parser.add_argument(
        '--dry-run', '-d',
        action='store_true',
        help='Simulate without posting comments',
    )
    parser.add_argument(
        '--no-comment',
        action='store_true',
        help='Skip comment posting entirely, only produce report',
    )
    parser.add_argument(
        '--project', '-p',
        default=os.environ.get('TICKET_WATCH_PROJECT', 'DSSD'),
        help='Jira project key (default: DSSD or TICKET_WATCH_PROJECT env)',
    )
    parser.add_argument(
        '--chicken-curry',
        action='store_true',
        help='[DANGEROUS] Chicken curry poop party mode',
    )
    parser.add_argument(
        '--version', '-v',
        action='version',
        version=f'%(prog)s {VERSION}',
    )

    args = parser.parse_args()

    # Validate environment
    env = require_env('JIRA_SERVER_URL', 'JIRA_PERSONAL_ACCESS_TOKEN', 'TICKET_WATCH_REPORTERS')

    # Parse reporters — comma-separated, strip whitespace
    reporters = [name.strip() for name in env['TICKET_WATCH_REPORTERS'].split(",") if name.strip()]

    # Parse optional thresholds from env
    unassigned_hours = float(os.environ.get('TICKET_WATCH_UNASSIGNED_HOURS', '4'))
    stale_days = int(os.environ.get('TICKET_WATCH_STALE_DAYS', '3'))

    try:
        tool = TicketWatch(
            jira_server_url=env['JIRA_SERVER_URL'],
            jira_personal_access_token=env['JIRA_PERSONAL_ACCESS_TOKEN'],
            reporters=reporters,
            project=args.project,
            unassigned_hours=unassigned_hours,
            stale_days=stale_days,
            dry_run=args.dry_run,
            no_comment=args.no_comment,
        )
        if args.chicken_curry:
            tool.run_chicken_curry()
        else:
            tool.run()
    except RuntimeError as error:
        logger.error(str(error))
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("\nAborted by user.")
        sys.exit(130)


if __name__ == '__main__':
    main()
