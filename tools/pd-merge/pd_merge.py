#!/usr/bin/env python3
"""
PagerDuty Incident Merge Tool

Discovers and merges related PagerDuty incidents that share the same
root cause (same job/DAG name), using a deterministic priority system.

Merge logic based on pd-merge-logic.md v1.2:
- Scenario A: Same-day incidents with same job name
- Scenario B: Cross-date incidents with DSSD/DRGN ticket validation via Jira
- Scenario C: Mass failure consolidation into a DSSD incident
- Scenario D: RDS Export "failed to start" consolidation (interactive opt-in)
"""

import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Version information
VERSION = "0.2.4"

# Skip file: stores incident IDs that the user explicitly skipped,
# so they don't reappear on subsequent runs.
SKIP_FILE = Path(__file__).parent / ".pd_merge_skips.json"

try:
    import pagerduty
    from noc_utils import require_env, new_pd_client, parse_iso_dt as _parse_iso_dt, setup_logging
except ImportError as import_error:
    logging.basicConfig()
    logging.error("Missing required dependencies. Please run: pip install -r requirements.txt")
    logging.error("Details: %s", import_error)
    sys.exit(1)

# Optional Jira import — only needed for Scenario B
try:
    from jira.exceptions import JIRAError
    from noc_utils import new_jira_client
    JIRA_AVAILABLE = True
except ImportError:
    JIRA_AVAILABLE = False

logger = setup_logging(name=__name__)

# ---------------------------------------------------------------------------
# Regex constants
# ---------------------------------------------------------------------------

# Strip leading ticket prefixes from titles
PREFIX_STRIP_RE = re.compile(
    r'^\s*(?:DSSD-\d+|DRGN-\d+|FCR-\d+|COREDATA-\d+)\s*',
    re.IGNORECASE,
)

# Strip non-informative marker words
WORD_STRIP_RE = re.compile(
    r'^(?:monitoring|restarted|disabled\.?\s*ignore\.?)\s*',
    re.IGNORECASE,
)

# Alert-type patterns (applied in priority order)
DATABRICKS_RE = re.compile(
    r'databricks\s+batch\s+job\s+(.+?)\s+failed',
    re.IGNORECASE,
)
MONITOR_RE = re.compile(
    r"monitor\s+job\s+'?([^']+?)'?\s+failed",
    re.IGNORECASE,
)
AIRFLOW_CONSEC_RE = re.compile(
    r'airflow\s+dag\s+(.+?)\s+has\s+failed\s+consecutively',
    re.IGNORECASE,
)
AIRFLOW_TIME_RE = re.compile(
    r'airflow\s+dag\s+(.+?)\s+exceeded\s+expected\s+run\s+time',
    re.IGNORECASE,
)

# Monitor name suffix normalization
MONITOR_SUFFIX_RE = re.compile(r'(?:_airflow_prod|_run_prod|_prod)$', re.IGNORECASE)

# Jira ticket extraction
JIRA_TICKET_RE = re.compile(r'\b((?:DSSD|DRGN|FCR|COREDATA)-\d+)\b', re.IGNORECASE)

# Mass-failure DSSD title detection
MASS_FAILURE_RE = re.compile(
    r'multiple\s+(?:databricks\s+)?(?:batch\s+)?jobs?\s+failing',
    re.IGNORECASE,
)

# Consequential failure patterns (Scenario C)
CONSEQUENTIAL_RE = re.compile(
    r'(?:data\s+delayed|step\s+not\s+started\s+on\s+time)',
    re.IGNORECASE,
)

# RDS Export "failed to start" consolidation (Scenario D)
# Matches both "RDS export" and "RDS exports" (used interchangeably in titles)
RDS_EXPORT_RE = re.compile(r'^RDS\s+exports?\b', re.IGNORECASE)
RDS_FAILED_TO_START_RE = re.compile(r'failed\s+to\s+start', re.IGNORECASE)

# PagerDuty web UI base URL
PD_BASE_URL = "https://yourcompany.pagerduty.com/incidents"

# Alert type display names
ALERT_TYPE_LABELS: Dict[str, str] = {
    'databricks': 'Databricks batch job failed',
    'monitor': 'Monitor job failed',
    'airflow_consec': 'AirFlow DAG consec. failed',
    'airflow_time': 'AirFlow DAG exceeded time',
    'consequential': 'Consequential failure',
    'unknown': 'Unknown',
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ParsedIncident:
    """Enriched view of a PD incident after title parsing and note classification."""

    incident_id: str
    title: str
    status: str
    created_at: str
    html_url: str
    alert_type: str
    alert_priority: int
    normalized_job_name: str
    raw_job_name: str
    jira_tickets: List[str]
    real_notes: List[str] = field(default_factory=list)
    context_notes: List[str] = field(default_factory=list)
    all_notes_count: int = 0
    notes_fetched: bool = False


@dataclass
class MergeGroup:
    """A group of incidents sharing the same normalized job name."""

    group_key: str
    incidents: List[ParsedIncident]
    target: Optional[ParsedIncident] = None
    sources: List[ParsedIncident] = field(default_factory=list)
    scenario: str = "A"
    skip_reason: Optional[str] = None


@dataclass
class MergeResult:
    """Result of a single source-to-target merge API call."""

    target_id: str
    source_id: str
    success: bool
    error_message: Optional[str] = None


# ---------------------------------------------------------------------------
# Main tool class
# ---------------------------------------------------------------------------

class PagerDutyMergeTool:
    """
    Discovers and merges related PagerDuty incidents that share the same
    root cause (same job/DAG name), using a deterministic priority system.
    """

    def __init__(
        self,
        pagerduty_api_token: str,
        jira_server_url: Optional[str] = None,
        jira_personal_access_token: Optional[str] = None,
        dry_run: bool = False,
        verbose: bool = False,
    ) -> None:
        """
        Initialize the merge tool.

        Args:
            pagerduty_api_token: PagerDuty REST API token
            jira_server_url: Jira server URL (optional, for Scenario B)
            jira_personal_access_token: Jira PAT (optional, for Scenario B)
            dry_run: If True, simulate merges without API changes
            verbose: If True, show extra debug output
        """
        self.pd_client = new_pd_client(pagerduty_api_token)
        self.dry_run = dry_run
        self.verbose = verbose
        self.user_id: Optional[str] = None
        self.user_email: Optional[str] = None
        self.jira_client: Optional[Any] = None
        self.skipped_ids: Set[str] = self.load_skipped_ids()

        if JIRA_AVAILABLE and jira_server_url and jira_personal_access_token:
            self.jira_client, _ = new_jira_client(jira_server_url, jira_personal_access_token)

    # ------------------------------------------------------------------
    # Skip persistence
    # ------------------------------------------------------------------

    @staticmethod
    def load_skipped_ids() -> Set[str]:
        """Load previously skipped incident IDs from the skip file."""
        if not SKIP_FILE.exists():
            return set()
        try:
            data = json.loads(SKIP_FILE.read_text(encoding='utf-8'))
            return set(data.get('skipped_incident_ids', []))
        except (json.JSONDecodeError, OSError):
            return set()

    @staticmethod
    def save_skipped_ids(skipped_ids: Set[str]) -> None:
        """Persist skipped incident IDs to the skip file."""
        data = {
            'skipped_incident_ids': sorted(skipped_ids),
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }
        try:
            SKIP_FILE.write_text(
                json.dumps(data, indent=2) + '\n',
                encoding='utf-8',
            )
        except OSError as error:
            logger.warning("Could not save skip file: %s", error)

    # ------------------------------------------------------------------
    # Step 1: Authentication and fetch
    # ------------------------------------------------------------------

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

    def fetch_active_incidents(self) -> List[Dict[str, Any]]:
        """
        Fetch all triggered/acknowledged incidents for the current user.

        Uses a two-pass approach: current open incidents + historical since
        Jan 1 of the current year, to catch older reassigned incidents.

        Returns:
            List of raw PD incident dicts.

        Raises:
            RuntimeError: If PagerDuty API request fails.
        """
        try:
            # Pass 1: current open incidents
            params_current: Dict[str, Any] = {
                'statuses[]': ['triggered', 'acknowledged'],
                'sort_by': 'created_at:desc',
                'include[]': ['assignees'],
            }
            current_incidents = list(
                self.pd_client.list_all('incidents', params=params_current)
            )

            # Client-side user filter
            if self.user_id:
                current_incidents = [
                    inc for inc in current_incidents
                    if self._is_assigned_to_user(inc, self.user_id)
                ]

            incident_ids = {inc['id'] for inc in current_incidents}
            all_incidents = list(current_incidents)

            # Pass 2: historical since Jan 1 of current year
            current_year = datetime.now(timezone.utc).year
            since_date = datetime(current_year, 1, 1)
            params_historical: Dict[str, Any] = {
                'statuses[]': ['triggered', 'acknowledged'],
                'since': since_date.strftime('%Y-%m-%dT%H:%M:%SZ'),
                'sort_by': 'created_at:desc',
                'include[]': ['assignees'],
            }
            historical_incidents = list(
                self.pd_client.list_all('incidents', params=params_historical)
            )

            for inc in historical_incidents:
                if inc['id'] not in incident_ids:
                    if self.user_id and not self._is_assigned_to_user(inc, self.user_id):
                        continue
                    all_incidents.append(inc)
                    incident_ids.add(inc['id'])

            return all_incidents
        except pagerduty.Error as error:
            raise RuntimeError(
                f"Failed to fetch PagerDuty incidents: {error}"
            ) from error

    @staticmethod
    def _is_assigned_to_user(incident: Dict[str, Any], user_id: str) -> bool:
        """Check if an incident is assigned to the given user."""
        return any(
            assignment.get('assignee', {}).get('id') == user_id
            for assignment in incident.get('assignments', [])
        )

    # ------------------------------------------------------------------
    # Step 2: Title parsing and normalization
    # ------------------------------------------------------------------

    def _strip_prefix(self, title: str) -> Tuple[str, List[str]]:
        """
        Strip leading ticket prefixes (DSSD-NNNNN, DRGN-NNNNN, etc.)
        and marker words from the title.

        Returns:
            (stripped_title, list_of_found_ticket_keys)
        """
        tickets: List[str] = []

        # Extract and strip ticket prefixes (may be stacked)
        while True:
            match = PREFIX_STRIP_RE.match(title)
            if not match:
                break
            found = JIRA_TICKET_RE.findall(match.group())
            tickets.extend(t.upper() for t in found)
            title = title[match.end():]

        # Strip marker words
        title = WORD_STRIP_RE.sub('', title).strip()

        # Strip common bracket wrappers like [ERROR] [DATABRICKS]
        title = re.sub(
            r'^\s*(?:\[(?:ERROR|CRITICAL|WARNING|INFO)\]\s*)*'
            r'(?:\[(?:DATABRICKS|AIRFLOW)\]\s*)*',
            '', title, flags=re.IGNORECASE,
        ).strip()

        return title, tickets

    def parse_incident_title(
        self, title: str
    ) -> Tuple[str, str, int, str, List[str]]:
        """
        Parse an incident title to extract job name, alert type, and priority.

        Returns:
            (normalized_job_name, alert_type, alert_priority,
             raw_job_name, jira_tickets)
        """
        stripped_title, jira_tickets = self._strip_prefix(title)

        # Also extract tickets from the full original title
        for ticket in JIRA_TICKET_RE.findall(title):
            ticket_upper = ticket.upper()
            if ticket_upper not in jira_tickets:
                jira_tickets.append(ticket_upper)

        # Try patterns in priority order
        # Priority 1: Databricks batch job
        match = DATABRICKS_RE.search(stripped_title)
        if match:
            raw_name = match.group(1).strip()
            return raw_name, 'databricks', 1, raw_name, jira_tickets

        # Priority 2: Monitor job
        match = MONITOR_RE.search(stripped_title)
        if match:
            raw_name = match.group(1).strip()
            normalized_name = MONITOR_SUFFIX_RE.sub('', raw_name)
            return normalized_name, 'monitor', 2, raw_name, jira_tickets

        # Priority 3a: AirFlow DAG consecutive failure
        match = AIRFLOW_CONSEC_RE.search(stripped_title)
        if match:
            raw_name = match.group(1).strip()
            return raw_name, 'airflow_consec', 3, raw_name, jira_tickets

        # Priority 3b: AirFlow DAG exceeded run time
        match = AIRFLOW_TIME_RE.search(stripped_title)
        if match:
            raw_name = match.group(1).strip()
            return raw_name, 'airflow_time', 3, raw_name, jira_tickets

        # Check for consequential failure patterns
        if CONSEQUENTIAL_RE.search(stripped_title):
            return stripped_title, 'consequential', 99, stripped_title, jira_tickets

        # Unknown pattern
        return stripped_title, 'unknown', 99, stripped_title, jira_tickets

    def enrich_incident(self, raw: Dict[str, Any]) -> ParsedIncident:
        """Convert a raw PD API incident dict into a ParsedIncident."""
        title = raw.get('title', '')
        normalized_name, alert_type, priority, raw_name, tickets = (
            self.parse_incident_title(title)
        )

        return ParsedIncident(
            incident_id=raw['id'],
            title=title,
            status=raw.get('status', 'unknown'),
            created_at=raw.get('created_at', ''),
            html_url=raw.get('html_url', f"{PD_BASE_URL}/{raw['id']}"),
            alert_type=alert_type,
            alert_priority=priority,
            normalized_job_name=normalized_name,
            raw_job_name=raw_name,
            jira_tickets=tickets,
        )

    # ------------------------------------------------------------------
    # Step 3: Grouping and scenario classification
    # ------------------------------------------------------------------

    def group_incidents(
        self, incidents: List[ParsedIncident]
    ) -> Dict[str, List[ParsedIncident]]:
        """
        Group incidents by normalized_job_name.

        Returns only groups with 2 or more incidents.
        Incidents with alert_type 'unknown' or 'consequential' are excluded
        from same-name grouping (they participate in Scenario C separately).
        """
        groups: Dict[str, List[ParsedIncident]] = {}
        for inc in incidents:
            if inc.alert_type in ('unknown', 'consequential'):
                continue
            key = inc.normalized_job_name
            if key not in groups:
                groups[key] = []
            groups[key].append(inc)

        # Keep only groups with 2+ incidents
        return {k: v for k, v in groups.items() if len(v) >= 2}

    @staticmethod
    def _all_same_day(incidents: List[ParsedIncident]) -> bool:
        """Return True if all incidents share the same calendar date (UTC)."""
        dates = set()
        for inc in incidents:
            try:
                dt = _parse_iso_dt(inc.created_at)
                dates.add(dt.date())
            except (ValueError, AttributeError):
                return False
        return len(dates) <= 1

    def _find_mass_failure_incident(
        self, all_incidents: List[ParsedIncident]
    ) -> Optional[ParsedIncident]:
        """
        Scan all incidents for a Scenario C mass-failure DSSD target.

        Returns the most recently created mass-failure DSSD incident, or None.
        """
        candidates: List[ParsedIncident] = []
        for inc in all_incidents:
            if MASS_FAILURE_RE.search(inc.title) and inc.jira_tickets:
                candidates.append(inc)

        if not candidates:
            return None

        # Return most recently created
        candidates.sort(key=lambda i: i.created_at, reverse=True)
        return candidates[0]

    def classify_group(
        self,
        group_key: str,
        incidents: List[ParsedIncident],
        mass_failure_incident: Optional[ParsedIncident],
    ) -> MergeGroup:
        """
        Determine merge scenario for a group.

        Scenario A: same-day → mergeable.
        Scenario B: cross-date with DSSD/DRGN → needs Jira check.
        """
        merge_group = MergeGroup(group_key=group_key, incidents=incidents)

        if self._all_same_day(incidents):
            merge_group.scenario = "A"
            return merge_group

        # Cross-date: check for DSSD/DRGN tickets
        incidents_with_tickets = [
            inc for inc in incidents if inc.jira_tickets
        ]

        if not incidents_with_tickets:
            merge_group.skip_reason = (
                "Cross-date group without DSSD/DRGN ticket — cannot validate"
            )
            return merge_group

        # Check if any new incidents might belong to a mass failure
        if mass_failure_incident:
            mass_created = _parse_iso_dt(mass_failure_incident.created_at)
            new_incidents_in_mass_window = [
                inc for inc in incidents
                if inc.incident_id != mass_failure_incident.incident_id
                and not inc.jira_tickets
                and _parse_iso_dt(inc.created_at) >= mass_created
            ]
            if new_incidents_in_mass_window:
                merge_group.skip_reason = (
                    "Cross-date group — new incidents may belong to mass "
                    f"failure {mass_failure_incident.incident_id}; "
                    "review via Scenario C instead"
                )
                return merge_group

        # Scenario B: cross-date with DSSD/DRGN
        merge_group.scenario = "B"

        if not self.jira_client:
            merge_group.skip_reason = (
                "Cross-date group with DSSD/DRGN ticket — "
                "Jira not configured, cannot validate"
            )

        return merge_group

    def build_mass_failure_group(
        self,
        mass_failure_incident: ParsedIncident,
        all_incidents: List[ParsedIncident],
        same_name_groups: Dict[str, List[ParsedIncident]],
    ) -> Optional[MergeGroup]:
        """
        Build a Scenario C merge group: find all incidents that should be
        merged into the mass-failure DSSD incident.

        Checks:
        - Strong match: job name appears in mass-failure incident's alerts
        - Strong match: confirmed same error in notes
        - Likely: same time window, Databricks/Monitor/AirFlow, no own ticket
        - Consequential: data delayed / step not started on time
        """
        mass_created = _parse_iso_dt(mass_failure_incident.created_at)

        # Fetch alerts for the mass-failure incident to build known-jobs set
        known_jobs: set = set()
        try:
            alerts = list(self.pd_client.list_all(
                f"incidents/{mass_failure_incident.incident_id}/alerts"
            ))
            for alert in alerts:
                alert_summary = alert.get('summary', '') or alert.get('body', {}).get('details', {}).get('Description', '')
                # Extract job names from alert summaries
                for pattern in [DATABRICKS_RE, MONITOR_RE, AIRFLOW_CONSEC_RE, AIRFLOW_TIME_RE]:
                    match = pattern.search(alert_summary)
                    if match:
                        name = match.group(1).strip()
                        name = MONITOR_SUFFIX_RE.sub('', name)
                        known_jobs.add(name)
            logger.debug(f"Mass-failure incident has {len(alerts)} alerts, {len(known_jobs)} known jobs")
        except pagerduty.Error as error:
            logger.debug(f"Failed to fetch alerts for mass-failure incident: {error}")

        candidates: List[ParsedIncident] = []
        valid_types = {'databricks', 'monitor', 'airflow_consec', 'airflow_time', 'consequential'}

        for inc in all_incidents:
            # Skip the mass-failure incident itself
            if inc.incident_id == mass_failure_incident.incident_id:
                continue

            # Skip incidents already in a same-name merge group
            if inc.normalized_job_name in same_name_groups:
                group_incidents = same_name_groups[inc.normalized_job_name]
                if len(group_incidents) >= 2 and self._all_same_day(group_incidents):
                    continue

            # Skip incidents with their own DSSD/DRGN tickets
            if inc.jira_tickets:
                continue

            # Skip non-relevant alert types
            if inc.alert_type not in valid_types:
                continue

            # Skip incidents created before the mass failure
            try:
                inc_created = _parse_iso_dt(inc.created_at)
                if inc_created < mass_created:
                    continue
            except (ValueError, AttributeError):
                continue

            # Strong match: job name in known alerts
            if inc.normalized_job_name in known_jobs:
                logger.debug(f"Strong match (in alerts): {inc.incident_id} — {inc.normalized_job_name}")
                candidates.append(inc)
                continue

            # Consequential failure
            if inc.alert_type == 'consequential':
                logger.debug(f"Consequential: {inc.incident_id} — {inc.title}")
                candidates.append(inc)
                continue

            # Likely: same time window, valid type, no own ticket
            logger.debug(f"Likely match: {inc.incident_id} — {inc.normalized_job_name}")
            candidates.append(inc)

        if not candidates:
            return None

        group = MergeGroup(
            group_key=f"MASS: {mass_failure_incident.jira_tickets[0]}",
            incidents=[mass_failure_incident] + candidates,
            target=mass_failure_incident,
            sources=candidates,
            scenario="C",
        )
        return group

    # ------------------------------------------------------------------
    # Scenario D: RDS Export "failed to start" consolidation
    # ------------------------------------------------------------------

    def build_rds_exports_group(
        self,
        all_incidents: List[ParsedIncident],
    ) -> Optional[MergeGroup]:
        """
        Build a Scenario D merge group: find all RDS export incidents and
        merge individual failures into the "failed to start" umbrella incident.

        The target must have "Failed to start" confirmed in its notes.

        Returns:
            MergeGroup if a valid group was built, None otherwise.
        """
        # Find all RDS export incidents (including unknown alert types)
        rds_incidents: List[ParsedIncident] = []
        for inc in all_incidents:
            stripped_title, _ = self._strip_prefix(inc.title)
            if RDS_EXPORT_RE.match(stripped_title):
                rds_incidents.append(inc)

        if len(rds_incidents) < 2:
            return None

        # Find target: the "failed to start" incident
        target: Optional[ParsedIncident] = None
        for inc in rds_incidents:
            stripped_title, _ = self._strip_prefix(inc.title)
            if RDS_FAILED_TO_START_RE.search(stripped_title):
                target = inc
                break

        if target is None:
            logger.debug("Scenario D: no 'failed to start' target found among RDS export incidents")
            return None

        # Validate: target must have "Failed to start" in its notes
        self.fetch_and_classify_notes(target)
        has_failed_to_start_note = any(
            RDS_FAILED_TO_START_RE.search(note)
            for note in target.real_notes + target.context_notes
        )
        # Also check ignored notes (raw content) via all notes
        if not has_failed_to_start_note:
            try:
                notes = list(self.pd_client.list_all(
                    f"incidents/{target.incident_id}/notes"
                ))
                has_failed_to_start_note = any(
                    RDS_FAILED_TO_START_RE.search(note.get('content', ''))
                    for note in notes
                )
            except pagerduty.Error as error:
                logger.debug(f"Scenario D: failed to re-fetch notes for validation: {error}")

        if not has_failed_to_start_note:
            logger.debug(
                f"Scenario D: target {target.incident_id} has no "
                f"'Failed to start' in notes — skipping"
            )
            return None

        # Build sources: all other RDS export incidents
        sources = [inc for inc in rds_incidents if inc.incident_id != target.incident_id]

        if not sources:
            return None

        group = MergeGroup(
            group_key="RDS Exports — failed to start",
            incidents=[target] + sources,
            target=target,
            sources=sources,
            scenario="D",
        )
        logger.debug(
            f"Scenario D: built RDS exports group — "
            f"target={target.incident_id}, {len(sources)} source(s)"
        )
        return group

    # ------------------------------------------------------------------
    # Step 4: Notes fetch and classification
    # ------------------------------------------------------------------

    def fetch_and_classify_notes(self, incident: ParsedIncident) -> None:
        """
        Fetch and classify all notes for an incident.

        Classifications:
        - 'ignore': "working on it", "Disabled. Ignore"
        - 'context': DSSD/DRGN snooze notes
        - 'real': everything else
        """
        if incident.notes_fetched:
            return

        try:
            notes = list(self.pd_client.list_all(
                f"incidents/{incident.incident_id}/notes"
            ))
        except pagerduty.Error as error:
            logger.debug(f"Failed to fetch notes for {incident.incident_id}: {error}")
            incident.notes_fetched = True
            return

        incident.all_notes_count = len(notes)

        for note in notes:
            content = note.get('content', '')
            classification = self._classify_note(content)
            if classification == 'real':
                incident.real_notes.append(content)
            elif classification == 'context':
                incident.context_notes.append(content)

        incident.notes_fetched = True

    @staticmethod
    def _classify_note(content: str) -> str:
        """
        Classify a single note.

        Returns:
            'ignore', 'context', or 'real'
        """
        lower = content.strip().lower()

        # "working on it" in any form
        if 'working on it' in lower:
            return 'ignore'

        # "Disabled. Ignore"
        if 'disabled' in lower and 'ignore' in lower:
            return 'ignore'

        # DSSD/DRGN snooze note: "DSSD-NNNNN - Status - Name. Snooze"
        if JIRA_TICKET_RE.search(content) and 'snooze' in lower:
            return 'context'

        return 'real'

    # ------------------------------------------------------------------
    # Scenario B: Jira validation for cross-date merges
    # ------------------------------------------------------------------

    def validate_cross_date_merge(
        self,
        old_incident: ParsedIncident,
        new_incident: ParsedIncident,
    ) -> Tuple[bool, str]:
        """
        Scenario B: fetch the DSSD/DRGN Jira ticket from the old incident,
        compare its description to the new incident's alert content.

        Returns:
            (should_merge, reason)
        """
        if not self.jira_client:
            return False, "Jira client not configured"

        # Get the first ticket from the old incident
        ticket_key = None
        for ticket in old_incident.jira_tickets:
            if ticket.startswith(('DSSD-', 'DRGN-')):
                ticket_key = ticket
                break

        if not ticket_key:
            # Check context notes for ticket references
            for note in old_incident.context_notes:
                matches = JIRA_TICKET_RE.findall(note)
                for match in matches:
                    if match.upper().startswith(('DSSD-', 'DRGN-')):
                        ticket_key = match.upper()
                        break
                if ticket_key:
                    break

        if not ticket_key:
            return False, "No DSSD/DRGN ticket found on old incident"

        # Fetch Jira ticket
        try:
            issue = self.jira_client.issue(ticket_key)
            jira_summary = str(issue.fields.summary or '')
            jira_description = str(issue.fields.description or '')
        except Exception as error:
            return False, f"Failed to fetch Jira ticket {ticket_key}: {error}"

        # Fetch alerts for both incidents to compare error types
        old_alert_text = self._get_alert_text(old_incident.incident_id)
        new_alert_text = self._get_alert_text(new_incident.incident_id)

        # Simple heuristic: compare key error keywords
        jira_text = f"{jira_summary} {jira_description}".lower()
        new_text = f"{new_alert_text} {' '.join(new_incident.real_notes)}".lower()

        # Extract exception class names as error signatures
        exception_pattern = re.compile(r'(\w+(?:Error|Exception|Failure)\b)', re.IGNORECASE)
        jira_errors = set(e.lower() for e in exception_pattern.findall(jira_text))
        new_errors = set(e.lower() for e in exception_pattern.findall(new_text))

        common_errors = jira_errors & new_errors
        if common_errors:
            return True, f"Same error type confirmed: {', '.join(common_errors)}"

        # Check for common keywords indicating same root cause
        sla_keywords = {'exceeded', 'run time', 'sla', 'duration', 'slow'}
        failure_keywords = {'failed', 'crash', 'exception', 'error', 'oom', 'fetchfailed'}

        jira_is_sla = any(kw in jira_text for kw in sla_keywords)
        new_is_failure = any(kw in new_text for kw in failure_keywords)

        if jira_is_sla and new_is_failure:
            return False, (
                f"Different root causes: Jira {ticket_key} describes SLA/timing issue, "
                "new incident is a job failure"
            )

        return False, f"Could not confirm same root cause with Jira {ticket_key}"

    def _get_alert_text(self, incident_id: str) -> str:
        """Fetch and concatenate alert summaries for an incident."""
        try:
            alerts = list(self.pd_client.list_all(
                f"incidents/{incident_id}/alerts"
            ))
            texts = []
            for alert in alerts:
                summary = alert.get('summary', '')
                body = alert.get('body', {})
                if isinstance(body, dict):
                    details = body.get('details', {})
                    if isinstance(details, dict):
                        summary += ' ' + str(details.get('Description', ''))
                texts.append(summary)
            return ' '.join(texts)
        except pagerduty.Error:
            return ''

    # ------------------------------------------------------------------
    # Step 5: Target selection
    # ------------------------------------------------------------------

    def select_target(self, group: MergeGroup) -> None:
        """
        Apply the 3-rule target selection algorithm.

        Rule 1: Incident with real comments (if exactly one) -> target.
        Rule 2: Highest alert_priority among candidates -> target.
        Rule 3: Tiebreak by earliest created_at -> target.
        """
        incidents = group.incidents

        # Rule 1: real comments override
        with_comments = [i for i in incidents if len(i.real_notes) > 0]

        if len(with_comments) == 1:
            group.target = with_comments[0]
        elif len(with_comments) > 1:
            # Rule 2+3 among those with comments
            group.target = min(
                with_comments,
                key=lambda i: (i.alert_priority, i.created_at),
            )
        else:
            # Rule 2+3 among all
            group.target = min(
                incidents,
                key=lambda i: (i.alert_priority, i.created_at),
            )

        group.sources = [
            i for i in incidents if i.incident_id != group.target.incident_id
        ]

    # ------------------------------------------------------------------
    # Step 6: Execute merges
    # ------------------------------------------------------------------

    def merge_incident(
        self, target_id: str, source_id: str
    ) -> MergeResult:
        """
        Merge one source incident into the target.

        Args:
            target_id: The surviving incident ID.
            source_id: The incident to merge into the target.

        Returns:
            MergeResult with success status and any error message.
        """
        if self.dry_run:
            return MergeResult(
                target_id=target_id,
                source_id=source_id,
                success=True,
                error_message="[DRY RUN]",
            )

        try:
            payload = {
                'source_incidents': [
                    {'id': source_id, 'type': 'incident_reference'}
                ]
            }
            headers = {'From': self.user_email or ''}
            self.pd_client.rput(
                f'incidents/{target_id}/merge',
                json=payload,
                headers=headers,
            )
            return MergeResult(
                target_id=target_id,
                source_id=source_id,
                success=True,
            )
        except pagerduty.Error as error:
            error_msg = str(error)
            if 'arguments caused error' in error_msg.lower():
                error_msg = "Source already resolved — skipped"
            return MergeResult(
                target_id=target_id,
                source_id=source_id,
                success=False,
                error_message=error_msg,
            )

    def execute_group_merge(self, group: MergeGroup) -> List[MergeResult]:
        """Execute merges for a single group, one source at a time."""
        results: List[MergeResult] = []
        if not group.target:
            return results

        for source in group.sources:
            result = self.merge_incident(
                group.target.incident_id,
                source.incident_id,
            )
            status_text = "OK" if result.success else f"FAIL: {result.error_message}"
            logger.info("    %s -> %s: %s", source.incident_id, group.target.incident_id, status_text)
            results.append(result)

        return results

    # ------------------------------------------------------------------
    # UI: table formatting
    # ------------------------------------------------------------------

    @staticmethod
    def _format_time(iso_str: str) -> str:
        """Format incident age as dd:hh:mm from creation to now."""
        try:
            dt = _parse_iso_dt(iso_str)
            now = datetime.now(timezone.utc)
            delta = now - dt
            total_seconds = int(delta.total_seconds())
            if total_seconds < 0:
                total_seconds = 0
            days: int = total_seconds // 86400
            hours: int = (total_seconds % 86400) // 3600
            minutes: int = (total_seconds % 3600) // 60
            return f"{days:02d}:{hours:02d}:{minutes:02d}"
        except (ValueError, AttributeError):
            return '??:??:??'

    @staticmethod
    def _format_date(iso_str: str) -> str:
        """Extract MM-DD from ISO 8601 datetime string."""
        try:
            dt = _parse_iso_dt(iso_str)
            return dt.strftime('%m-%d')
        except (ValueError, AttributeError):
            return '??-??'

    @staticmethod
    def _make_row(cells: List[str], widths: List[int]) -> str:
        """Format a table row with fixed-width columns."""
        parts = [cell.ljust(w) for cell, w in zip(cells, widths)]
        return "| " + " | ".join(parts) + " |"

    @staticmethod
    def _make_separator(widths: List[int]) -> str:
        """Format a table separator line."""
        return "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def print_group_detail_table(self, group: MergeGroup) -> None:
        """Print the detailed ASCII table for one group before confirmation."""
        incident_count = len(group.incidents)
        logger.info("\nGroup: %s — %d incidents (Scenario %s)", group.group_key, incident_count, group.scenario)

        if group.skip_reason:
            logger.info("  SKIPPED: %s", group.skip_reason)
            return

        if not group.target:
            logger.info("  ERROR: No target selected")
            return

        # Determine if we need a Date column (cross-date groups)
        show_date = not self._all_same_day(group.incidents)

        # Build table data
        headers = ["Role", "Link", "Title", "Alert Type", "P", "Notes", "Age"]
        min_widths = [7, 4, 10, 30, 1, 5, 8]

        if show_date:
            headers.insert(6, "Date")
            min_widths.insert(6, 5)

        rows: List[List[str]] = []
        for inc in group.incidents:
            role = "TARGET" if inc.incident_id == group.target.incident_id else "merge"
            notes_str = str(inc.all_notes_count)
            if inc.real_notes:
                notes_str += f"({len(inc.real_notes)}r)"
            if inc.alert_type == 'unknown' and RDS_EXPORT_RE.match(
                self._strip_prefix(inc.title)[0]
            ):
                alert_label = 'RDS Exports'
            else:
                alert_label = ALERT_TYPE_LABELS.get(inc.alert_type, inc.alert_type)

            stripped_title, _ = self._strip_prefix(inc.title)
            row = [
                role,
                f"{PD_BASE_URL}/{inc.incident_id}",
                stripped_title,
                alert_label,
                str(inc.alert_priority),
                notes_str,
            ]
            if show_date:
                row.append(self._format_date(inc.created_at))
            row.append(self._format_time(inc.created_at))
            rows.append(row)

        # Compute column widths
        widths = list(min_widths)
        for i, header in enumerate(headers):
            widths[i] = max(widths[i], len(header))
        for row in rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], len(cell))

        # Print table
        separator = self._make_separator(widths)
        logger.info(separator)
        logger.info(self._make_row(headers, widths))
        logger.info(separator)
        for row in rows:
            logger.info(self._make_row(row, widths))
        logger.info(separator)


    def print_summary_line(self, idx: int, group: MergeGroup) -> None:
        """Print one-line summary for the all-groups overview."""
        source_count = len(group.sources) if group.sources else len(group.incidents) - 1
        target_id = group.target.incident_id if group.target else "?"
        status = f"target: {target_id}" if not group.skip_reason else f"SKIP: {group.skip_reason}"
        logger.info(
            "  [%d] %-45s | %d incidents | Scenario %s | %d to merge | %s",
            idx,
            group.group_key,
            len(group.incidents),
            group.scenario,
            source_count,
            status,
        )

    def print_results_summary(
        self,
        all_results: List[MergeResult],
        skipped_groups: List[MergeGroup],
    ) -> None:
        """Print final summary of merge operations."""
        if not all_results and not skipped_groups:
            logger.info("No merges performed.")
            return

        successes = sum(1 for r in all_results if r.success)
        failures = sum(1 for r in all_results if not r.success)

        logger.info("Merge Results Summary")
        logger.info("-" * 40)
        if all_results:
            logger.info("  Merged:  %d", successes)
            if failures:
                logger.info("  Failed:  %d", failures)
                for r in all_results:
                    if not r.success:
                        logger.info("    %s: %s", r.source_id, r.error_message)
        if skipped_groups:
            logger.info("  Skipped: %d group(s)", len(skipped_groups))

    # ------------------------------------------------------------------
    # Main orchestration
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Full interactive merge workflow."""
        # 1. Resolve current user
        logger.info("Fetching PagerDuty user info...")
        self.get_current_user()
        logger.info("  User: %s (%s)", self.user_email, self.user_id)

        # 2. Fetch active incidents
        logger.info("Fetching active incidents...")
        raw_incidents = self.fetch_active_incidents()
        logger.info("  Found %d active incident(s).", len(raw_incidents))
        if not raw_incidents:
            logger.info("Nothing to merge.")
            return

        # 3. Parse and enrich
        all_incidents = [self.enrich_incident(r) for r in raw_incidents]

        # 3b. Filter out previously skipped incidents
        if self.skipped_ids:
            before = len(all_incidents)
            all_incidents = [
                inc for inc in all_incidents
                if inc.incident_id not in self.skipped_ids
            ]
            filtered_count = before - len(all_incidents)
            if filtered_count:
                logger.info("  Filtered out %d previously skipped incident(s).", filtered_count)

        # 4. Detect mass-failure DSSD incident (Scenario C)
        mass_failure_incident = self._find_mass_failure_incident(all_incidents)
        if mass_failure_incident:
            logger.info(
                "  Mass-failure incident detected: %s (%s)",
                mass_failure_incident.incident_id,
                ", ".join(mass_failure_incident.jira_tickets),
            )

        # 5. Group by job name
        groups_dict = self.group_incidents(all_incidents)
        logger.debug(f"Found {len(groups_dict)} same-name group(s) with 2+ incidents")

        # 6. Classify each group (A, B, or skip)
        merge_groups: List[MergeGroup] = []
        for key, incidents in groups_dict.items():
            group = self.classify_group(key, incidents, mass_failure_incident)
            merge_groups.append(group)

        # 7. Build Scenario C group if mass-failure incident exists
        if mass_failure_incident:
            # Fetch notes for mass-failure incident first
            self.fetch_and_classify_notes(mass_failure_incident)
            mass_group = self.build_mass_failure_group(
                mass_failure_incident, all_incidents, groups_dict,
            )
            if mass_group:
                merge_groups.append(mass_group)

        # 7b. Detect RDS Exports merge opportunity (Scenario D)
        rds_candidates = [
            inc for inc in all_incidents
            if RDS_EXPORT_RE.match(self._strip_prefix(inc.title)[0])
        ]
        if len(rds_candidates) >= 2:
            # Check if there's a potential "failed to start" target
            rds_target_candidate = next(
                (
                    inc for inc in rds_candidates
                    if RDS_FAILED_TO_START_RE.search(
                        self._strip_prefix(inc.title)[0]
                    )
                ),
                None,
            )
            if rds_target_candidate:
                rds_source_count = len(rds_candidates) - 1
                logger.info("\n--- Options ---")
                logger.info("  [D] RDS Exports merge: %d RDS export incident(s) found", len(rds_candidates))
                logger.info(
                    "      Target: %s (%s)",
                    rds_target_candidate.incident_id,
                    self._strip_prefix(rds_target_candidate.title)[0],
                )
                logger.info("      Sources: %d individual RDS export failure(s)", rds_source_count)
                try:
                    rds_answer = input("  Enable RDS Exports merge? [y/n]: ").strip().lower()
                except (KeyboardInterrupt, EOFError):
                    rds_answer = 'n'

                if rds_answer in ('y', 'yes'):
                    rds_group = self.build_rds_exports_group(all_incidents)
                    if rds_group:
                        logger.info('  Validating target notes... "Failed to start" found in notes.')
                        merge_groups.append(rds_group)
                    else:
                        logger.info("  RDS Exports merge skipped: target has no 'Failed to start' in notes.")
                else:
                    logger.info("  RDS Exports merge disabled.")

        # 8. Fetch notes for all incidents in mergeable groups
        logger.info("Fetching incident notes...")
        notes_count = 0
        for group in merge_groups:
            if group.skip_reason is None:
                for inc in group.incidents:
                    if not inc.notes_fetched:
                        self.fetch_and_classify_notes(inc)
                        notes_count += 1
        logger.debug(f"Fetched notes for {notes_count} incident(s)")

        # 9. Scenario B: validate cross-date merges via Jira
        for group in merge_groups:
            if group.scenario == "B" and group.skip_reason is None:
                self._validate_scenario_b_group(group)

        # 10. Select targets for groups that don't have one yet
        for group in merge_groups:
            if group.skip_reason is None and group.target is None:
                self.select_target(group)

        # 11. Print overview
        actionable = [g for g in merge_groups if g.skip_reason is None]
        skipped = [g for g in merge_groups if g.skip_reason is not None]

        logger.info("\n%s", "=" * 70)
        logger.info(
            "Merge Plan: %d group(s) to merge, %d skipped",
            len(actionable),
            len(skipped),
        )
        logger.info("=" * 70)

        for idx, group in enumerate(actionable, start=1):
            self.print_summary_line(idx, group)

        if skipped:
            logger.info("\nSkipped (%d):", len(skipped))
            for group in skipped:
                logger.info("  - %s: %s", group.group_key, group.skip_reason)

        if not actionable:
            logger.info("\nNo groups to merge.")
            return

        # 12. Per-group confirmation and execution
        all_results: List[MergeResult] = []
        merge_all = False
        newly_skipped_ids: Set[str] = set()

        for idx, group in enumerate(actionable, start=1):
            logger.info("\n%s", "=" * 70)
            self.print_group_detail_table(group)

            if self.dry_run:
                logger.info("\n  [DRY RUN] Would merge the above group.")
                continue

            if not merge_all:
                prompt = (
                    f"\nMerge this group? [y/n/all/select/skip] "
                    f"({idx}/{len(actionable)}): "
                )
                try:
                    answer = input(prompt).strip().lower()
                except (KeyboardInterrupt, EOFError):
                    logger.info("\nAborted.")
                    break

                if answer == 'all':
                    merge_all = True
                elif answer in ('s', 'select'):
                    # Per-incident selection mode
                    selected_sources = self._select_incidents(group)
                    if not selected_sources:
                        logger.info("  No incidents selected, skipping group.")
                        # Skip the unselected sources
                        for src in group.sources:
                            newly_skipped_ids.add(src.incident_id)
                        group.skip_reason = "user skipped (select mode)"
                        skipped.append(group)
                        continue
                    # Skip the unselected, merge the selected
                    selected_ids = {s.incident_id for s in selected_sources}
                    for src in group.sources:
                        if src.incident_id not in selected_ids:
                            newly_skipped_ids.add(src.incident_id)
                    group.sources = selected_sources
                elif answer in ('n', 'skip', ''):
                    logger.info("  Skipping: %s", group.group_key)
                    for src in group.sources:
                        newly_skipped_ids.add(src.incident_id)
                    group.skip_reason = "user skipped"
                    skipped.append(group)
                    continue
                elif answer != 'y':
                    logger.info("  Unrecognized input, skipping.")
                    for src in group.sources:
                        newly_skipped_ids.add(src.incident_id)
                    group.skip_reason = "user skipped"
                    skipped.append(group)
                    continue

            logger.info(
                "\n  Merging %d incident(s) into %s...",
                len(group.sources),
                group.target.incident_id,
            )
            results = self.execute_group_merge(group)
            all_results.extend(results)

        # 13. Persist skipped incident IDs
        if newly_skipped_ids:
            self.skipped_ids.update(newly_skipped_ids)
            self.save_skipped_ids(self.skipped_ids)
            logger.info("\n  Saved %d skipped incident(s) to skip list.", len(newly_skipped_ids))

        # 14. Final summary
        logger.info("\n%s", "=" * 70)
        self.print_results_summary(all_results, skipped)
        logger.info("=" * 70)

    def _select_incidents(self, group: MergeGroup) -> List[ParsedIncident]:
        """
        Interactive per-incident selection within a group.

        Shows numbered list of source incidents and lets the user pick
        which ones to merge (comma-separated numbers or ranges).

        Returns:
            List of selected ParsedIncident sources to merge.
        """
        sources = group.sources
        logger.info("\n  Select incidents to merge into %s:", group.target.incident_id)
        logger.info("  (enter numbers separated by commas, e.g. '1,3' or 'all')\n")

        for i, src in enumerate(sources, start=1):
            alert_label = ALERT_TYPE_LABELS.get(src.alert_type, src.alert_type)
            time_str = self._format_time(src.created_at)
            logger.info("    %d. %s  %-30s  %s", i, src.incident_id, alert_label, time_str)

        try:
            answer = input(f"\n  Merge which? [1-{len(sources)}, all, none]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            logger.info("\n  Cancelled.")
            return []

        if answer in ('none', 'n', ''):
            return []
        if answer == 'all':
            return list(sources)

        # Parse comma-separated numbers and ranges (e.g. "1,3-5")
        selected: List[ParsedIncident] = []
        selected_indices: Set[int] = set()
        for part in answer.split(','):
            part = part.strip()
            if '-' in part:
                try:
                    start_str, end_str = part.split('-', 1)
                    start_val = int(start_str.strip())
                    end_val = int(end_str.strip())
                    for idx in range(start_val, end_val + 1):
                        selected_indices.add(idx)
                except ValueError:
                    logger.info("  Invalid range: '%s', skipping.", part)
            else:
                try:
                    selected_indices.add(int(part))
                except ValueError:
                    logger.info("  Invalid number: '%s', skipping.", part)

        for idx in sorted(selected_indices):
            if 1 <= idx <= len(sources):
                selected.append(sources[idx - 1])
            else:
                logger.info("  Index %d out of range, skipping.", idx)

        if selected:
            logger.info("  Selected %d of %d incidents.", len(selected), len(sources))

        return selected

    def _validate_scenario_b_group(self, group: MergeGroup) -> None:
        """Run Scenario B Jira validation for a cross-date group."""
        # Find old (with ticket) and new (without) incidents
        old_incidents = [i for i in group.incidents if i.jira_tickets]
        new_incidents = [i for i in group.incidents if not i.jira_tickets]

        if not old_incidents or not new_incidents:
            # All have tickets or none do — just use standard target selection
            return

        # Use the oldest incident with a ticket as reference
        old_ref = min(old_incidents, key=lambda i: i.created_at)

        for new_inc in new_incidents:
            should_merge, reason = self.validate_cross_date_merge(old_ref, new_inc)
            logger.debug(f"Scenario B: {new_inc.incident_id} vs {old_ref.incident_id}: {reason}")
            if not should_merge:
                group.skip_reason = reason
                return


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Entry point for the PagerDuty Incident Merge Tool."""

    env = require_env('PAGERDUTY_API_TOKEN')
    jira_server_url = os.environ.get('JIRA_SERVER_URL')
    jira_personal_access_token = os.environ.get('JIRA_PERSONAL_ACCESS_TOKEN')

    # Parse CLI arguments
    dry_run = False
    verbose = False

    idx = 1
    while idx < len(sys.argv):
        arg = sys.argv[idx]
        if arg in ('--dry-run', '-n'):
            dry_run = True
        elif arg in ('--verbose', '-v'):
            verbose = True
        elif arg == '--clear-skips':
            if SKIP_FILE.exists():
                skips = PagerDutyMergeTool.load_skipped_ids()
                SKIP_FILE.unlink()
                logger.info("Cleared %d skipped incident(s) from skip list.", len(skips))
            else:
                logger.info("No skip list found.")
            sys.exit(0)
        elif arg == '--show-skips':
            skips = PagerDutyMergeTool.load_skipped_ids()
            if skips:
                logger.info("Skipped incidents (%d):", len(skips))
                for sid in sorted(skips):
                    logger.info("  %s/%s", PD_BASE_URL, sid)
            else:
                logger.info("No skipped incidents.")
            sys.exit(0)
        elif arg in ('--help', '-h'):
            logger.info("PagerDuty Incident Merge Tool")
            logger.info("Version: %s", VERSION)
            logger.info("")
            logger.info("Usage: python pd_merge.py [OPTIONS]")
            logger.info("")
            logger.info("Options:")
            logger.info("  --dry-run, -n    Simulate merges without making API changes")
            logger.info("  --verbose, -v    Show extra debug output")
            logger.info("  --clear-skips    Clear the saved skip list and exit")
            logger.info("  --show-skips     Show currently skipped incident IDs and exit")
            logger.info("  --help, -h       Show this help message")
            logger.info("")
            logger.info("Interactive commands during merge:")
            logger.info("  y       Merge all incidents in this group")
            logger.info("  n/skip  Skip this group (remembered for future runs)")
            logger.info("  all     Merge all remaining groups without asking")
            logger.info("  select  Pick specific incidents to merge from this group")
            sys.exit(0)
        else:
            logger.error("Error: Unknown argument '%s'. Use --help for usage.", arg)
            sys.exit(1)
        idx += 1

    # Banner
    logger.info("=" * 70)
    logger.info("PagerDuty Incident Merge Tool v%s", VERSION)
    if dry_run:
        logger.info("Mode: DRY RUN (no changes will be made)")
    skipped_count = len(PagerDutyMergeTool.load_skipped_ids())
    if skipped_count:
        logger.info("Skip list: %d incident(s)", skipped_count)
        try:
            clear_answer = input("  Clear skip list before proceeding? [y/n]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            clear_answer = 'n'
        if clear_answer in ('y', 'yes'):
            SKIP_FILE.unlink()
            logger.info("  Cleared %d skipped incident(s).", skipped_count)
        else:
            logger.info("  Keeping skip list (%d incident(s)).", skipped_count)
    logger.info("=" * 70)
    logger.info("")

    try:
        tool = PagerDutyMergeTool(
            pagerduty_api_token=env['PAGERDUTY_API_TOKEN'],
            jira_server_url=jira_server_url,
            jira_personal_access_token=jira_personal_access_token or '',
            dry_run=dry_run,
            verbose=verbose,
        )
        tool.run()
    except KeyboardInterrupt:
        logger.info("\n\nInterrupted by user.")
        sys.exit(130)
    except RuntimeError as error:
        logger.error("\nError: %s", error)
        sys.exit(1)
    except Exception as error:
        logger.error("\nUnexpected error: %s", error)
        if verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
