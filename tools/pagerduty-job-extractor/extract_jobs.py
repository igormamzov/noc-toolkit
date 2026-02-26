#!/usr/bin/env python3
"""
PagerDuty Job Extractor

Extracts failed job names (jb_* pattern) from PagerDuty incident alerts.
"""

import os
import re
import sys
import warnings
from typing import Set, List
from dotenv import load_dotenv

# Version information
VERSION = "0.1.0"

# Suppress pagination warnings from pagerduty package
warnings.filterwarnings('ignore', message='.*lacks a "more" property.*')

try:
    import pagerduty
except ImportError:
    print("Error: Missing required dependencies. Please run: pip install -r requirements.txt")
    sys.exit(1)


class PagerDutyJobExtractor:
    """Extracts job names from PagerDuty incident alerts."""

    # Regex pattern to match job names starting with jb_
    JOB_PATTERN = re.compile(r'\b(jb_[a-zA-Z0-9_]+)\b')

    def __init__(self, pagerduty_api_token: str) -> None:
        """
        Initialize the extractor with PagerDuty API credentials.

        Args:
            pagerduty_api_token: PagerDuty API token
        """
        self.pagerduty_session = pagerduty.RestApiV2Client(pagerduty_api_token)

    def extract_jobs_from_text(self, text: str) -> List[str]:
        """
        Extract job names matching the pattern jb_* from text.

        Args:
            text: Text to search for job names

        Returns:
            List of unique job names found
        """
        if not text:
            return []
        return self.JOB_PATTERN.findall(text)

    def extract_jobs_from_dict(self, data: any) -> Set[str]:
        """
        Recursively extract jobs from nested dictionary/list structures.

        Args:
            data: Dictionary or list to search

        Returns:
            Set of unique job names found
        """
        jobs = set()

        if isinstance(data, dict):
            for key, value in data.items():
                # Check key itself
                key_jobs = self.extract_jobs_from_text(str(key))
                if key_jobs:
                    jobs.update(key_jobs)

                # Check value
                if isinstance(value, (str, int, float)):
                    value_jobs = self.extract_jobs_from_text(str(value))
                    if value_jobs:
                        jobs.update(value_jobs)
                elif isinstance(value, (dict, list)):
                    jobs.update(self.extract_jobs_from_dict(value))

        elif isinstance(data, list):
            for item in data:
                if isinstance(item, (str, int, float)):
                    item_jobs = self.extract_jobs_from_text(str(item))
                    if item_jobs:
                        jobs.update(item_jobs)
                elif isinstance(item, (dict, list)):
                    jobs.update(self.extract_jobs_from_dict(item))

        return jobs

    def get_jobs_from_incident(self, incident_id: str) -> List[str]:
        """
        Extract all job names from a PagerDuty incident.

        Args:
            incident_id: PagerDuty incident ID

        Returns:
            Sorted list of unique job names
        """
        all_jobs = set()

        try:
            # Get incident details
            incident = self.pagerduty_session.rget(f'incidents/{incident_id}')
            if isinstance(incident, dict) and 'incident' in incident:
                incident_data = incident['incident']
            else:
                incident_data = incident

            # Extract jobs from incident data itself
            incident_jobs = self.extract_jobs_from_dict(incident_data)
            if incident_jobs:
                all_jobs.update(incident_jobs)

            # Get alerts and extract jobs from each alert
            alerts = self.pagerduty_session.list_all(f'incidents/{incident_id}/alerts')
            alerts_list = list(alerts)

            for alert in alerts_list:
                alert_jobs = self.extract_jobs_from_dict(alert)
                if alert_jobs:
                    all_jobs.update(alert_jobs)

            # Get notes and extract jobs
            notes = self.pagerduty_session.list_all(f'incidents/{incident_id}/notes')
            notes_list = list(notes)

            for note in notes_list:
                content = note.get('content', '')
                note_jobs = self.extract_jobs_from_text(content)
                if note_jobs:
                    all_jobs.update(note_jobs)

        except pagerduty.Error as error:
            print(f"Error: Failed to fetch incident data: {error}", file=sys.stderr)
            sys.exit(1)
        except Exception as error:
            print(f"Error: An unexpected error occurred: {error}", file=sys.stderr)
            sys.exit(1)

        return sorted(all_jobs)


def extract_incident_id(incident_input: str) -> str:
    """
    Extract incident ID from URL or return as-is if already an ID.

    Args:
        incident_input: PagerDuty incident URL or ID

    Returns:
        Incident ID
    """
    if 'incidents/' in incident_input:
        # Extract ID from URL like https://tmtoc.pagerduty.com/incidents/Q1WPEMZKLQZGJF
        incident_id = incident_input.split('incidents/')[-1].strip('/')
    else:
        incident_id = incident_input.strip()

    return incident_id


def main() -> None:
    """Main entry point for the CLI tool."""
    # Load environment variables from .env file if present
    load_dotenv()

    # Check for .env in parent directory (pagerduty-jira-checker)
    parent_env = os.path.join(os.path.dirname(__file__), '..', 'pagerduty-jira-checker', '.env')
    if os.path.exists(parent_env):
        load_dotenv(dotenv_path=parent_env)

    # Get credentials from environment variables
    pagerduty_api_token = os.environ.get('PAGERDUTY_API_TOKEN')

    # Validate PagerDuty credentials
    if not pagerduty_api_token:
        print("Error: Missing required environment variable: PAGERDUTY_API_TOKEN", file=sys.stderr)
        print("\nPlease set this in your environment or create a .env file.", file=sys.stderr)
        print("See .env.example for the required format.", file=sys.stderr)
        sys.exit(1)

    # Parse command line arguments
    if len(sys.argv) < 2:
        print("Usage: python extract_jobs.py <INCIDENT_URL_OR_ID>", file=sys.stderr)
        print("\nExample:", file=sys.stderr)
        print("  python extract_jobs.py Q1WPEMZKLQZGJF", file=sys.stderr)
        print("  python extract_jobs.py https://tmtoc.pagerduty.com/incidents/Q1WPEMZKLQZGJF", file=sys.stderr)
        sys.exit(1)

    incident_input = sys.argv[1]
    incident_id = extract_incident_id(incident_input)

    # Create extractor instance and run
    try:
        extractor = PagerDutyJobExtractor(pagerduty_api_token=pagerduty_api_token)
        jobs = extractor.get_jobs_from_incident(incident_id)

        # Print results (just the list)
        if jobs:
            for job in jobs:
                print(job)
        else:
            print("No jobs matching jb_* pattern were found", file=sys.stderr)
            sys.exit(1)

    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
