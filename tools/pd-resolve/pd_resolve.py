#!/usr/bin/env python3
"""
PD Resolver Tool

Automates the resolution of PagerDuty Airflow incidents where subsequent
DAG runs succeeded:
1. Fetch PD incident -> extract DAG name from title
2. Check Airflow REST API -> verify recent runs are all success
3. Find DRGN ticket from PD notes
4. Find runbook on Confluence (search DS space)
5. Interactive prompts: SLA Violation, Comment
6. Close DRGN with proper transition fields
7. Resolve PD incident + add note
"""

import os
import re
import sys
import warnings
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Version information
VERSION = "0.1.2"

# Suppress pagination warnings from pagerduty package
warnings.filterwarnings('ignore', message='.*lacks a "more" property.*')

try:
    import pagerduty
    import requests
    import boto3
    from jira import JIRA
    from jira.exceptions import JIRAError
    from dotenv import load_dotenv
except ImportError as import_error:
    print(f"Error: Missing required dependencies. Please run: pip install -r requirements.txt")
    print(f"Details: {import_error}")
    sys.exit(1)

# Regex for detecting DRGN tickets in text
DRGN_PATTERN = re.compile(r'\b(DRGN-\d+)\b')

# Regex for extracting DAG name from incident title
# Matches patterns like: "DAG <dag_name> has failed" or "AirFlow DAG <dag_name> has failed"
DAG_NAME_PATTERN = re.compile(r'DAG\s+(\S+)\s+has\s+failed', re.IGNORECASE)

# Airflow MWAA VPCE endpoint
MWAA_VPCE = "6a5c7525-46ab-47b5-a32f-65dc179ca140-vpce.c16.us-west-2.airflow.amazonaws.com"

# DRGN Close transition configuration
CLOSE_TRANSITION_ID = "61"

# CDS Alert Category field values
ALERT_CATEGORY_ETL = "64520"
ALERT_CATEGORY_DATA_EXPORT = "64521"

# SLA Violation field values
SLA_VIOLATION_YES = "64527"
SLA_VIOLATION_NO = "64528"
SLA_VIOLATION_UNKNOWN = "64529"

# Runbook Status field values
RUNBOOK_UP_TO_DATE = "64530"
RUNBOOK_MISSING = "64532"

# Resolution field values
RESOLUTION_AUTOMATICALLY = "12901"

# Alert type classification patterns
CONSECUTIVE_FAILURES_PATTERN = re.compile(
    r'consecutive.?failures|failed consecutively', re.IGNORECASE,
)
BATCH_DELAYED_PATTERN = re.compile(
    r'batch.?job.?delayed|delayed.?flag', re.IGNORECASE,
)

# Comment presets for DRGN closure
COMMENT_PRESETS = [
    "Subsequent runs succeeded",
    "Subsequent runs completed successfully, no action needed",
    "Job recovered automatically, all recent runs passed",
    "Resolved -- DAG runs healthy after transient failures",
]


@dataclass
class AirflowRun:
    """Represents a single Airflow DAG run."""

    dag_run_id: str
    state: str
    start_date: str
    end_date: Optional[str]


@dataclass
class ResolveResult:
    """Result of the resolve operation."""

    incident_id: str
    incident_title: str
    dag_name: str
    alert_type: str
    runs_checked: int
    recent_successes: int
    recovered: bool
    drgn_key: Optional[str]
    runbook_url: Optional[str]
    drgn_closed: bool
    pd_resolved: bool
    errors: List[str] = field(default_factory=list)


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


class PDResolve:
    """Resolves PD Airflow incidents where subsequent DAG runs succeeded."""

    def __init__(
        self,
        pagerduty_api_token: str,
        jira_server_url: str,
        jira_personal_access_token: str,
        jira_email: str,
        mwaa_env_name: str = "prd2612-prod-airflow",
        mwaa_region: str = "us-west-2",
        aws_profile: Optional[str] = None,
        dry_run: bool = False,
        verbose: bool = False,
        no_confirm: bool = False,
    ) -> None:
        """
        Initialize with API credentials.

        Args:
            pagerduty_api_token: PagerDuty API token
            jira_server_url: Jira server URL
            jira_personal_access_token: Jira Server/DC PAT
            jira_email: Email for PD 'From' header
            mwaa_env_name: MWAA environment name
            mwaa_region: AWS region for MWAA
            aws_profile: AWS profile name (auto-detected if None)
            dry_run: If True, simulate without API mutations
            verbose: If True, print detailed output
            no_confirm: If True, skip interactive confirmation before mutations
        """
        self.dry_run = dry_run
        self.verbose = verbose
        self.no_confirm = no_confirm
        self.jira_email = jira_email
        self.jira_server_url = jira_server_url.rstrip("/")
        self.mwaa_env_name = mwaa_env_name
        self.mwaa_region = mwaa_region
        self.aws_profile = aws_profile or self._detect_aws_profile()

        self.pd_client = pagerduty.RestApiV2Client(pagerduty_api_token)
        self.jira_client = JIRA(
            server=jira_server_url,
            token_auth=jira_personal_access_token,
        )

    # ------------------------------------------------------------------
    # PagerDuty methods
    # ------------------------------------------------------------------

    def fetch_incident(self, incident_id: str) -> Dict[str, Any]:
        """
        Fetch PD incident details including external_references.

        Args:
            incident_id: PagerDuty incident ID

        Returns:
            Incident dict with id, title, status, incident_number, html_url, drgn_key

        Raises:
            RuntimeError: If unable to fetch incident
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
                'incident_number': incident.get('incident_number', ''),
                'html_url': incident.get('html_url', ''),
                'drgn_key': drgn_key,
            }
        except pagerduty.Error as error:
            raise RuntimeError(
                f"Failed to fetch incident {incident_id}: {error}"
            ) from error

    def find_drgn_from_notes(self, incident_id: str) -> Optional[str]:
        """
        Scan PD incident notes for a DRGN-\\d+ ticket reference.

        Args:
            incident_id: PagerDuty incident ID

        Returns:
            First DRGN ticket key found, or None
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

    def resolve_pd_incident(self, incident_id: str, note_text: str) -> None:
        """
        Resolve a PD incident and add a note.

        Args:
            incident_id: PagerDuty incident ID
            note_text: Note content to add before resolving
        """
        if self.dry_run:
            print(f"  [DRY-RUN] Would resolve PD incident {incident_id}")
            print(f"  [DRY-RUN] Would add PD note: {note_text}")
            return

        # Add note first
        try:
            self.pd_client.rpost(
                f'incidents/{incident_id}/notes',
                json={'note': {'content': note_text}},
                headers={'From': self.jira_email},
            )
            print(f"  PD note added")
        except pagerduty.Error as error:
            raise RuntimeError(
                f"Failed to add PD note to {incident_id}: {error}"
            ) from error

        # Resolve incident
        try:
            self.pd_client.rput(
                'incidents',
                json={
                    'incidents': [{
                        'id': incident_id,
                        'type': 'incident_reference',
                        'status': 'resolved',
                    }],
                },
                headers={'From': self.jira_email},
            )
            print(f"  PD incident resolved")
        except pagerduty.Error as error:
            raise RuntimeError(
                f"Failed to resolve PD incident {incident_id}: {error}"
            ) from error

    # ------------------------------------------------------------------
    # AWS helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_aws_profile() -> Optional[str]:
        """Auto-detect AWS profile with Airflow access from ~/.aws/credentials."""
        credentials_path = os.path.expanduser('~/.aws/credentials')
        if not os.path.exists(credentials_path):
            return None
        try:
            import configparser
            config = configparser.ConfigParser()
            config.read(credentials_path)
            for section in config.sections():
                lower_section = section.lower()
                if 'airflow' in lower_section or 'mwaa' in lower_section:
                    return section
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Airflow methods
    # ------------------------------------------------------------------

    def get_airflow_session(self) -> requests.Session:
        """
        Create authenticated Airflow REST API session via MWAA web login token.

        Returns:
            Authenticated requests.Session

        Raises:
            RuntimeError: If unable to create session
        """
        try:
            session_kwargs: Dict[str, Any] = {'region_name': self.mwaa_region}
            if self.aws_profile:
                session_kwargs['profile_name'] = self.aws_profile
            boto_session = boto3.Session(**session_kwargs)
            client = boto_session.client('mwaa', region_name=self.mwaa_region)
            web_token = client.create_web_login_token(Name=self.mwaa_env_name)
            session = requests.Session()
            login_response = session.post(
                f'https://{MWAA_VPCE}/aws_mwaa/login',
                data={'token': web_token['WebToken']},
            )
            login_response.raise_for_status()
            return session
        except Exception as error:
            raise RuntimeError(
                f"Failed to create Airflow session: {error}"
            ) from error

    def check_airflow_runs(
        self, dag_name: str, limit: int = 15,
    ) -> List[AirflowRun]:
        """
        Fetch recent DAG runs via Airflow REST API.

        Args:
            dag_name: Airflow DAG ID
            limit: Number of recent runs to fetch

        Returns:
            List of AirflowRun ordered by start_date descending

        Raises:
            RuntimeError: If unable to fetch DAG runs
        """
        try:
            session = self.get_airflow_session()
            response = session.get(
                f'https://{MWAA_VPCE}/api/v1/dags/{dag_name}/dagRuns',
                params={'limit': limit, 'order_by': '-start_date'},
            )
            response.raise_for_status()
            data = response.json()

            runs: List[AirflowRun] = []
            for dag_run in data.get('dag_runs', []):
                runs.append(AirflowRun(
                    dag_run_id=dag_run.get('dag_run_id', ''),
                    state=dag_run.get('state', ''),
                    start_date=dag_run.get('start_date', ''),
                    end_date=dag_run.get('end_date'),
                ))
            return runs
        except requests.exceptions.HTTPError as error:
            if error.response is not None and error.response.status_code == 404:
                raise RuntimeError(
                    f"DAG '{dag_name}' not found in Airflow"
                ) from error
            raise RuntimeError(
                f"Failed to fetch DAG runs for '{dag_name}': {error}"
            ) from error
        except Exception as error:
            raise RuntimeError(
                f"Failed to fetch DAG runs for '{dag_name}': {error}"
            ) from error

    # ------------------------------------------------------------------
    # Classification and extraction
    # ------------------------------------------------------------------

    @staticmethod
    def classify_alert(title: str) -> str:
        """
        Classify alert type from incident title.

        Args:
            title: PD incident title

        Returns:
            Alert type string: 'consecutive_failures', 'batch_delayed', or 'unknown'
        """
        if CONSECUTIVE_FAILURES_PATTERN.search(title):
            return "consecutive_failures"
        if BATCH_DELAYED_PATTERN.search(title):
            return "batch_delayed"
        return "unknown"

    @staticmethod
    def extract_dag_name(title: str) -> Optional[str]:
        """
        Extract DAG name from PD incident title.

        Args:
            title: PD incident title

        Returns:
            DAG name string, or None if not found
        """
        match = DAG_NAME_PATTERN.search(title)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def evaluate_recovery(runs: List[AirflowRun], min_consecutive: int = 2) -> bool:
        """
        Check if the most recent runs are successful (indicating recovery).

        Runs must be ordered by start_date descending (newest first).
        Recovery = the last `min_consecutive` runs are all 'success'.

        Args:
            runs: List of recent AirflowRun objects (newest first)
            min_consecutive: Minimum consecutive successes required from the latest run

        Returns:
            True if the last min_consecutive runs are 'success', False otherwise
        """
        if len(runs) < min_consecutive:
            return False
        return all(run.state == 'success' for run in runs[:min_consecutive])

    # ------------------------------------------------------------------
    # Jira / DRGN methods
    # ------------------------------------------------------------------

    def get_drgn_status(self, drgn_key: str) -> str:
        """
        Get the current status of a DRGN ticket.

        Args:
            drgn_key: DRGN issue key

        Returns:
            Status string (e.g. 'Open', 'Closed')

        Raises:
            RuntimeError: If unable to fetch issue
        """
        try:
            issue = self.jira_client.issue(drgn_key)
            return str(issue.fields.status)
        except JIRAError as error:
            raise RuntimeError(
                f"Failed to fetch DRGN ticket {drgn_key}: {error}"
            ) from error

    def close_drgn(
        self,
        drgn_key: str,
        sla_violation_id: str,
        runbook_url: Optional[str],
        comment: str,
    ) -> None:
        """
        Close a DRGN ticket via transition 61 with required fields.

        Args:
            drgn_key: DRGN issue key
            sla_violation_id: SLA Violation field value ID
            runbook_url: Confluence runbook URL (or None if missing)
            comment: Comment to add during closure
        """
        # Determine runbook status based on whether URL was found
        runbook_status_id = RUNBOOK_UP_TO_DATE if runbook_url else RUNBOOK_MISSING

        if self.dry_run:
            print(f"  [DRY-RUN] Would close {drgn_key} (transition {CLOSE_TRANSITION_ID})")
            print(f"  [DRY-RUN]   CDS Alert Category: ETL ({ALERT_CATEGORY_ETL})")
            print(f"  [DRY-RUN]   SLA Violation: {sla_violation_id}")
            print(f"  [DRY-RUN]   Runbook Status: {runbook_status_id}")
            print(f"  [DRY-RUN]   Runbook link: {runbook_url or '(none)'}")
            print(f"  [DRY-RUN]   Resolution: Resolved Automatically ({RESOLUTION_AUTOMATICALLY})")
            print(f"  [DRY-RUN]   Comment: {comment}")
            return

        try:
            self.jira_client.transition_issue(
                drgn_key,
                CLOSE_TRANSITION_ID,
                customfield_45201={'id': ALERT_CATEGORY_ETL},
                customfield_45202={'id': sla_violation_id},
                customfield_45203={'id': runbook_status_id},
                customfield_38218=runbook_url or '',
                resolution={'id': RESOLUTION_AUTOMATICALLY},
                comment=comment,
            )
            print(f"  {drgn_key} -> Closed")
        except JIRAError as error:
            raise RuntimeError(
                f"Failed to close {drgn_key}: {error}"
            ) from error

    # ------------------------------------------------------------------
    # Confluence / Runbook search
    # ------------------------------------------------------------------

    def find_runbook(self, dag_name: str) -> Optional[str]:
        """
        Search Confluence for a runbook matching the DAG name.

        Uses the Jira REST API's Confluence content search endpoint.

        Args:
            dag_name: Airflow DAG ID

        Returns:
            Confluence page URL, or None if not found
        """
        try:
            confluence_base = self.jira_server_url.replace(
                'jira.', 'confluence.',
            )
            search_url = f"{confluence_base}/rest/api/content"
            response = requests.get(
                search_url,
                params={
                    'spaceKey': 'DS',
                    'title': f'Runbook - {dag_name}',
                    'limit': 1,
                },
                headers={
                    'Authorization': f'Bearer {self.jira_client._session.headers.get("Authorization", "").replace("Bearer ", "")}',
                },
                verify=False,
                timeout=15,
            )
            if response.status_code == 200:
                results = response.json().get('results', [])
                if results:
                    page_id = results[0]['id']
                    return f"{confluence_base}/pages/viewpage.action?pageId={page_id}"
        except Exception as error:
            if self.verbose:
                print(f"  Warning: Confluence search failed: {error}")
        return None

    # ------------------------------------------------------------------
    # Interactive prompts
    # ------------------------------------------------------------------

    @staticmethod
    def prompt_sla_violation() -> str:
        """
        Prompt user to select SLA Violation value.

        Returns:
            SLA Violation field value ID
        """
        print("\nSLA Violation?")
        print("  1. Yes")
        print("  2. No")
        print("  3. Unknown")

        while True:
            choice = input("Select [1-3]: ").strip()
            if choice == '1':
                return SLA_VIOLATION_YES
            elif choice == '2':
                return SLA_VIOLATION_NO
            elif choice == '3':
                return SLA_VIOLATION_UNKNOWN
            else:
                print("  Invalid choice. Enter 1, 2, or 3.")

    @staticmethod
    def prompt_comment() -> str:
        """
        Prompt user to select or enter a comment.

        Returns:
            Comment text string
        """
        print("\nComment:")
        for index, preset in enumerate(COMMENT_PRESETS, start=1):
            print(f"  {index}. {preset}")
        print(f"  {len(COMMENT_PRESETS) + 1}. Custom (enter your own)")

        max_choice = len(COMMENT_PRESETS) + 1
        while True:
            choice = input(f"Select [1-{max_choice}]: ").strip()
            if choice.isdigit():
                choice_number = int(choice)
                if 1 <= choice_number <= len(COMMENT_PRESETS):
                    return COMMENT_PRESETS[choice_number - 1]
                elif choice_number == max_choice:
                    custom_comment = input("Enter comment: ").strip()
                    if custom_comment:
                        return custom_comment
                    print("  Comment cannot be empty.")
                    continue
            print(f"  Invalid choice. Enter 1-{max_choice}.")

    @staticmethod
    def prompt_drgn_key() -> Optional[str]:
        """
        Prompt user to enter DRGN key manually.

        Returns:
            DRGN key string, or None if user skips
        """
        print("\n  No DRGN ticket found in PD notes or external references.")
        drgn_input = input("  Enter DRGN key (or press Enter to skip): ").strip().upper()
        if drgn_input and drgn_input.startswith('DRGN-'):
            return drgn_input
        return None

    # ------------------------------------------------------------------
    # Main resolve workflow
    # ------------------------------------------------------------------

    def resolve(self, incident_input: str) -> ResolveResult:
        """
        Execute the full resolution workflow.

        Args:
            incident_input: PagerDuty incident URL or ID

        Returns:
            ResolveResult with details of what was done

        Raises:
            RuntimeError: On unrecoverable errors
        """
        mode_label = "[DRY-RUN] " if self.dry_run else ""
        print(f"\n{mode_label}PD Resolver v{VERSION}")
        print("-" * 50)

        # Step 1: Fetch incident
        incident_id = extract_incident_id(incident_input)
        print(f"\nIncident: {incident_id}")
        incident_info = self.fetch_incident(incident_id)
        print(f"  {incident_info['title']}")
        print(f"  Status: {incident_info['status']}")

        title = incident_info['title']

        # Step 2: Classify alert type
        alert_type = self.classify_alert(title)
        print(f"\nAlert type: {alert_type}")

        # Step 3: Extract DAG name
        dag_name = self.extract_dag_name(title)
        if not dag_name:
            raise RuntimeError(
                f"Could not extract DAG name from title: {title}"
            )
        print(f"DAG: {dag_name}")

        # Step 4: Check Airflow runs
        print("\nChecking Airflow runs...")
        runs = self.check_airflow_runs(dag_name)
        success_count = sum(1 for run in runs if run.state == 'success')
        total_count = len(runs)
        print(f"  {success_count}/{total_count} recent runs: SUCCESS")

        # Show failed runs if any
        failed_runs = [run for run in runs if run.state != 'success']
        if failed_runs:
            for failed_run in failed_runs[:3]:
                print(f"  FAILED: {failed_run.dag_run_id} ({failed_run.state}) at {failed_run.start_date}")

        min_consecutive = 2
        recovered = self.evaluate_recovery(runs, min_consecutive=min_consecutive)
        if not recovered:
            # Count consecutive successes from the latest run
            consec = 0
            for run in runs:
                if run.state == 'success':
                    consec += 1
                else:
                    break
            print(f"\n  NOT recovered -- last {consec} consecutive successes "
                  f"(need {min_consecutive})")
            print("  Cannot auto-resolve. Exiting.")
            return ResolveResult(
                incident_id=incident_id,
                incident_title=title,
                dag_name=dag_name,
                alert_type=alert_type,
                runs_checked=total_count,
                recent_successes=success_count,
                recovered=False,
                drgn_key=None,
                runbook_url=None,
                drgn_closed=False,
                pd_resolved=False,
            )
        print(f"  Recovery confirmed (last {min_consecutive}+ runs succeeded)")

        # Step 5: Find DRGN ticket
        drgn_key = incident_info.get('drgn_key')
        if drgn_key:
            print(f"\nDRGN ticket: {drgn_key} (from Jira integration)")
        else:
            print("\nSearching PD notes for DRGN ticket...")
            drgn_key = self.find_drgn_from_notes(incident_id)
            if drgn_key:
                print(f"  Found in notes: {drgn_key}")
            else:
                drgn_key = self.prompt_drgn_key()
                if not drgn_key:
                    print("  Skipping DRGN closure (no ticket found).")

        # Check DRGN status if found
        drgn_closed = False
        if drgn_key:
            drgn_status = self.get_drgn_status(drgn_key)
            print(f"  {drgn_key} status: {drgn_status}")
            if drgn_status.lower() in ('closed', 'done', 'resolved'):
                print(f"  {drgn_key} already closed. Skipping.")
                drgn_key = None
                drgn_closed = True

        # Step 6: Find runbook
        runbook_url: Optional[str] = None
        if drgn_key:
            print(f"\nSearching for runbook...")
            runbook_url = self.find_runbook(dag_name)
            if runbook_url:
                print(f"  Runbook: {runbook_url}")
            else:
                print("  Runbook not found (will set status = Missing)")

        # Step 7: Interactive prompts (SLA + Comment)
        sla_violation_id: str = SLA_VIOLATION_UNKNOWN
        comment_text: str = COMMENT_PRESETS[0]

        if drgn_key and not self.no_confirm:
            sla_violation_id = self.prompt_sla_violation()
            comment_text = self.prompt_comment()
        elif drgn_key and self.no_confirm:
            # Default values for non-interactive mode
            sla_violation_id = SLA_VIOLATION_UNKNOWN
            comment_text = COMMENT_PRESETS[0]

        # Step 8: Confirm actions
        if drgn_key or not drgn_closed:
            sla_label = {
                SLA_VIOLATION_YES: "Yes",
                SLA_VIOLATION_NO: "No",
                SLA_VIOLATION_UNKNOWN: "Unknown",
            }.get(sla_violation_id, "Unknown")

            print("\nActions:")
            action_number = 1
            if drgn_key:
                print(f"  {action_number}. Close {drgn_key} (Resolution: Resolved Automatically, SLA: {sla_label})")
                action_number += 1
            print(f"  {action_number}. Resolve PD #{incident_info['incident_number']} + add note")
            action_number += 1
            if drgn_key:
                print(f"  {action_number}. Comment: \"{comment_text}\"")

            if not self.no_confirm and not self.dry_run:
                confirm = input("\nProceed? [Y/n] ").strip().lower()
                if confirm not in ('', 'y', 'yes'):
                    print("Aborted by user.")
                    return ResolveResult(
                        incident_id=incident_id,
                        incident_title=title,
                        dag_name=dag_name,
                        alert_type=alert_type,
                        runs_checked=total_count,
                        recent_successes=success_count,
                        recovered=True,
                        drgn_key=drgn_key,
                        runbook_url=runbook_url,
                        drgn_closed=False,
                        pd_resolved=False,
                    )

        # Step 9: Execute mutations
        print()
        errors: List[str] = []
        final_drgn_closed = drgn_closed

        # Close DRGN
        if drgn_key:
            try:
                self.close_drgn(drgn_key, sla_violation_id, runbook_url, comment_text)
                final_drgn_closed = True
            except RuntimeError as error:
                errors.append(str(error))
                print(f"  Error closing DRGN: {error}")

        # Resolve PD incident
        pd_resolved = False
        try:
            pd_note = comment_text
            if drgn_key:
                pd_note = f"{comment_text}\n{drgn_key} -> Closed"
            self.resolve_pd_incident(incident_id, pd_note)
            pd_resolved = True
        except RuntimeError as error:
            errors.append(str(error))
            print(f"  Error resolving PD incident: {error}")

        # Summary
        print(f"\n{'[DRY-RUN] ' if self.dry_run else ''}Done.")
        if errors:
            print(f"  Errors: {len(errors)}")
            for err in errors:
                print(f"    - {err}")

        return ResolveResult(
            incident_id=incident_id,
            incident_title=title,
            dag_name=dag_name,
            alert_type=alert_type,
            runs_checked=total_count,
            recent_successes=success_count,
            recovered=True,
            drgn_key=drgn_key,
            runbook_url=runbook_url,
            drgn_closed=final_drgn_closed,
            pd_resolved=pd_resolved,
            errors=errors,
        )


def main() -> None:
    """Main entry point for the CLI tool."""
    import argparse

    load_dotenv()

    # Also check parent .env (for noc-toolkit layout)
    parent_env = os.path.join(os.path.dirname(__file__), '..', '..', '.env')
    if os.path.exists(parent_env):
        load_dotenv(dotenv_path=parent_env)

    parser = argparse.ArgumentParser(
        description="PD Resolver -- Auto-resolve PD incidents where Airflow jobs recovered",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s https://tmtoc.pagerduty.com/incidents/Q1HR5H5BXCILO3\n"
            "  %(prog)s Q1HR5H5BXCILO3 --dry-run\n"
            "  %(prog)s Q1HR5H5BXCILO3 --no-confirm\n"
        ),
    )
    parser.add_argument(
        'incident',
        nargs='?',
        default=None,
        help='PagerDuty incident URL or ID',
    )
    parser.add_argument(
        '--dry-run', '-n',
        action='store_true',
        help='Simulate without making API mutations',
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Verbose output',
    )
    parser.add_argument(
        '--no-confirm',
        action='store_true',
        help='Skip interactive confirmation (use defaults)',
    )
    parser.add_argument(
        '--version',
        action='version',
        version=f'%(prog)s {VERSION}',
    )

    args = parser.parse_args()

    # Interactive prompt when launched from toolkit menu (no args)
    if not args.incident:
        try:
            args.incident = input("PagerDuty incident URL or ID: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(130)
        if not args.incident:
            print("Error: incident is required.", file=sys.stderr)
            sys.exit(1)

    # Validate environment
    pagerduty_api_token = os.environ.get('PAGERDUTY_API_TOKEN')
    jira_server_url = os.environ.get('JIRA_SERVER_URL')
    jira_personal_access_token = os.environ.get('JIRA_PERSONAL_ACCESS_TOKEN')
    jira_email = os.environ.get('JIRA_EMAIL', '')

    missing_vars: List[str] = []
    if not pagerduty_api_token:
        missing_vars.append('PAGERDUTY_API_TOKEN')
    if not jira_server_url:
        missing_vars.append('JIRA_SERVER_URL')
    if not jira_personal_access_token:
        missing_vars.append('JIRA_PERSONAL_ACCESS_TOKEN')

    if missing_vars:
        print(
            f"Error: Missing required environment variables: {', '.join(missing_vars)}",
            file=sys.stderr,
        )
        print("\nPlease set these in your environment or .env file.", file=sys.stderr)
        print("See .env.example for the required format.", file=sys.stderr)
        sys.exit(1)

    # MWAA config from env or defaults
    mwaa_env_name = os.environ.get('MWAA_ENVIRONMENT_NAME', 'prd2612-prod-airflow')
    mwaa_region = os.environ.get('MWAA_REGION', 'us-west-2')
    aws_profile = os.environ.get('AWS_PROFILE')

    try:
        resolver = PDResolve(
            pagerduty_api_token=pagerduty_api_token,
            jira_server_url=jira_server_url,
            jira_personal_access_token=jira_personal_access_token,
            jira_email=jira_email,
            mwaa_env_name=mwaa_env_name,
            mwaa_region=mwaa_region,
            aws_profile=aws_profile,
            dry_run=args.dry_run,
            verbose=args.verbose,
            no_confirm=args.no_confirm,
        )
        result = resolver.resolve(args.incident)

        # Exit with error code if not fully resolved
        if result.errors:
            sys.exit(1)

    except RuntimeError as error:
        print(f"\nError: {error}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nAborted by user.")
        sys.exit(130)


if __name__ == '__main__':
    main()
