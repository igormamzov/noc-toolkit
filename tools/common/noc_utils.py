"""
NOC Toolkit — Shared Utilities

Common helpers used across multiple NOC tools to eliminate code duplication.
"""

import os
import sys
import warnings
from datetime import datetime, timezone
from typing import Dict, List, Optional

from dotenv import load_dotenv

# Optional imports — available when corresponding deps are installed
try:
    import pagerduty as _pagerduty
except ImportError:
    _pagerduty = None  # type: ignore[assignment]

try:
    from jira import JIRA
except ImportError:
    JIRA = None  # type: ignore[assignment,misc]


def load_env() -> None:
    """
    Load environment variables from .env files.

    Loads the local .env first (tool directory), then the parent toolkit .env
    as a fallback. Variables from the first file take precedence.
    """
    load_dotenv()

    # Also check parent .env (for noc-toolkit layout: tools/<tool>/<script>.py)
    # Walk up from the caller's directory to find the toolkit root .env
    caller_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    parent_env = os.path.join(caller_dir, '..', '..', '.env')
    if os.path.exists(parent_env):
        load_dotenv(dotenv_path=parent_env)

    # Also try relative to this file (common/ is inside tools/)
    common_parent_env = os.path.join(os.path.dirname(__file__), '..', '..', '.env')
    if os.path.exists(common_parent_env):
        load_dotenv(dotenv_path=common_parent_env)


def require_env(*var_names: str) -> Dict[str, str]:
    """
    Validate that required environment variables are set.

    Args:
        *var_names: Names of required environment variables.

    Returns:
        Dict mapping variable names to their values.

    Raises:
        SystemExit: If any required variables are missing.
    """
    values: Dict[str, str] = {}
    missing: List[str] = []

    for name in var_names:
        value = os.environ.get(name, "")
        if value:
            values[name] = value
        else:
            missing.append(name)

    if missing:
        print(
            f"Error: Missing required environment variables: {', '.join(missing)}",
            file=sys.stderr,
        )
        print("\nPlease set these in your environment or .env file.", file=sys.stderr)
        print("See .env.example for the required format.", file=sys.stderr)
        sys.exit(1)

    return values


def new_pd_client(api_token: str):
    """
    Create a PagerDuty REST API v2 client with warnings suppressed.

    Args:
        api_token: PagerDuty API token.

    Returns:
        pagerduty.RestApiV2Client instance.
    """
    warnings.filterwarnings('ignore', message='.*lacks a "more" property.*')
    return _pagerduty.RestApiV2Client(api_token)


def new_jira_client(server_url: str, personal_access_token: str):
    """
    Create a Jira client using Server/DC PAT authentication.

    Args:
        server_url: Jira server URL (e.g. https://jira.example.com).
        personal_access_token: Jira Personal Access Token.

    Returns:
        Tuple of (JIRA client, browse_url) where browse_url is
        e.g. "https://jira.example.com/browse".
    """
    client = JIRA(
        server=server_url,
        token_auth=personal_access_token,
    )
    browse_url = server_url.rstrip("/") + "/browse"
    return client, browse_url


def parse_iso_dt(iso_str: str) -> datetime:
    """
    Parse an ISO 8601 datetime string into a timezone-aware datetime.

    Handles the common 'Z' suffix by replacing it with '+00:00'.

    Args:
        iso_str: ISO 8601 datetime string.

    Returns:
        Timezone-aware datetime object.
    """
    return datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
