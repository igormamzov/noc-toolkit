#!/usr/bin/env python3
"""
pd-create-drgn — programmatically create a DRGN Jira ticket for a PagerDuty
incident, the same way the PD UI's "More → Create Jira Issue" button does.

Why this exists: the PD↔Jira integration used by NOC has a *one-click*
button in the UI, but no documented public REST endpoint. This tool calls
the same internal endpoint the UI calls, with the same body shape, so
batching/automation becomes possible.

Auth: requires a PagerDuty UI session bearer token (`pdus+_...`) — the same
token Chrome sends as `Authorization: Bearer ...` on
app.pagerduty.com/integration-jira-service/*. It is NOT the same as a
classic PagerDuty REST API key. The UI bearer lives ~1 week and must be
captured from the browser by the user.

Workflow:
  1. fetch incident object via PD REST API (`Token token=...`)
  2. fetch alerts (so we can carry first-alert details in the body)
  3. POST the assembled payload to integration-jira-service via UI bearer
  4. extract DRGN key from the JSON response and print it
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

warnings.filterwarnings("ignore", message="Unverified HTTPS request")

# tools/common is added to sys.path by the launcher; for direct invocation,
# add it ourselves so `from noc_utils import ...` works.
_SCRIPT_DIR = Path(__file__).resolve().parent
_COMMON = _SCRIPT_DIR.parent / "common"
for _path in (_COMMON, _SCRIPT_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from noc_utils import load_env, require_env  # noqa: E402


VERSION = "0.1.0"

# Endpoint that the PD UI calls when the user clicks "Create Jira Issue".
# Captured via Chrome DevTools on 2026-05-31 from a successful manual run.
CREATE_ISSUE_URL = (
    "https://app.pagerduty.com/integration-jira-service/create_issue_from_pagerduty"
)
PD_API_BASE = "https://api.pagerduty.com"

# DRGN-specific integration constants (also captured from the UI request).
# accounts_mapping_id maps the PD account to the Jira project; project_key
# and issuetype_id are what the modal pre-selects for DRGN/Alert.
DEFAULT_ACCOUNTS_MAPPING_ID = "PVP6DLT"
DEFAULT_PROJECT_KEY = "DRGN"
DEFAULT_ISSUETYPE_ID = "21701"  # "Alert"

# Includes copied from the UI's incident-fetch behaviour. Without them the
# integration endpoint complains the body is missing fields it expects to
# pass through to Jira.
INCIDENT_INCLUDE_PARAMS = [
    "first_trigger_log_entry",
    "assignees",
    "acknowledgers",
    "teams",
    "services",
    "priorities",
    "escalation_policies",
    # external_references is a separate include — needed so we can short-
    # circuit when a DRGN is already linked, without an extra notes fetch.
    "external_references",
]

DEFAULT_REQUEST_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def extract_incident_id(incident_input: str) -> str:
    """Accept either a raw PD incident ID or a full incident URL."""
    if "incidents/" in incident_input:
        return incident_input.split("incidents/")[-1].strip("/")
    return incident_input.strip()


def fetch_pd_incident(
    incident_id: str,
    pagerduty_api_token: str,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
) -> Dict[str, Any]:
    """
    Fetch a PD incident with the same `include` set the UI uses.

    Uses the classic PD REST API (`Authorization: Token token=...`); the UI
    bearer token does NOT work for api.pagerduty.com.
    """
    response = requests.get(
        f"{PD_API_BASE}/incidents/{incident_id}",
        headers={
            "Authorization": f"Token token={pagerduty_api_token}",
            "Accept": "application/vnd.pagerduty+json;version=2",
        },
        params=[("include[]", inc) for inc in INCIDENT_INCLUDE_PARAMS],
        timeout=timeout,
        verify=False,
    )
    if response.status_code == 401:
        raise RuntimeError(
            f"PD REST API rejected PAGERDUTY_API_TOKEN for /incidents/{incident_id} (401). "
            f"Check the token in .env."
        )
    if response.status_code == 404:
        raise RuntimeError(f"Incident {incident_id} not found.")
    response.raise_for_status()
    return response.json()["incident"]


def fetch_pd_alerts(
    incident_id: str,
    pagerduty_api_token: str,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
) -> List[Dict[str, Any]]:
    """Fetch alerts attached to a PD incident."""
    response = requests.get(
        f"{PD_API_BASE}/incidents/{incident_id}/alerts",
        headers={
            "Authorization": f"Token token={pagerduty_api_token}",
            "Accept": "application/vnd.pagerduty+json;version=2",
        },
        timeout=timeout,
        verify=False,
    )
    response.raise_for_status()
    return response.json().get("alerts", [])


_DRGN_KEY_RE = re.compile(r"\b(DRGN-\d+)\b")


def find_existing_drgn_in_incident(incident: Dict[str, Any]) -> Optional[str]:
    """
    Return DRGN-NNNNN if the integration already created one for this PD
    incident; None otherwise.

    Checked in order:
    1. `external_references` — populated by PD↔Jira-Server integration v3
       when fetching with `include[]=external_references`. Sometimes empty
       even after a DRGN exists, which is why we fall through to step 2.
    2. (Caller responsibility) — fall back to `find_existing_drgn_in_notes`,
       which scans PD notes for the Jira-automation comment of the form
       "Jira issue: https://jira.livenation.com/browse/DRGN-NNNNN".
    """
    for reference in incident.get("external_references", []) or []:
        external_id = reference.get("external_id", "")
        if external_id.startswith("DRGN-"):
            return external_id
    return None


def find_existing_drgn_in_notes(
    incident_id: str,
    pagerduty_api_token: str,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
) -> Optional[str]:
    """
    Scan PD incident notes for a DRGN reference.

    The Jira automation that runs after a successful Create-Issue posts a
    note like::

        Comment from Jira - <ts> - JIRA Automation
        Jira issue: https://jira.livenation.com/browse/DRGN-17945

    Notes are also more reliable than `external_references` for the
    "endpoint says already-exists" recovery path.
    """
    response = requests.get(
        f"{PD_API_BASE}/incidents/{incident_id}/notes",
        headers={
            "Authorization": f"Token token={pagerduty_api_token}",
            "Accept": "application/vnd.pagerduty+json;version=2",
        },
        timeout=timeout,
        verify=False,
    )
    if response.status_code != 200:
        return None
    notes = response.json().get("notes", [])
    for note in notes:
        match = _DRGN_KEY_RE.search(note.get("content", "") or "")
        if match:
            return match.group(1)
    return None


# ---------------------------------------------------------------------------
# Payload assembly
# ---------------------------------------------------------------------------


def _alert_details_for_description(alert: Dict[str, Any]) -> Dict[str, str]:
    """
    Pull the kv-style `details` block out of an alert. Falls back to
    `body.cef_details.details` then `body.details` so we still get something
    if Prometheus/Alertmanager schemas drift.
    """
    body = alert.get("body") or {}
    cef = (body.get("cef_details") or {}) if isinstance(body, dict) else {}
    candidates = [
        body.get("details"),
        cef.get("details"),
    ]
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate:
            return {str(k): str(v) for k, v in candidate.items()}
    return {}


def build_issue_description(
    incident: Dict[str, Any],
    alerts: List[Dict[str, Any]],
) -> str:
    """
    Render the Jira description block, mimicking the format the UI fills in.

    Format (Jira wiki markup):
        [https://tmtoc.pagerduty.com/incidents/Q...|https://...]

        * key: value
        * key: value
        ...
    """
    pd_url = incident.get("html_url") or ""
    lines: List[str] = []
    if pd_url:
        lines.append(f"[{pd_url}|{pd_url}]")
        lines.append("")  # blank line for readability

    if alerts:
        details = _alert_details_for_description(alerts[0])
        for key in sorted(details):
            lines.append(f"* {key}: {details[key]}")

    return "\n".join(lines).rstrip() + "\n"


def build_request_body(
    incident: Dict[str, Any],
    alerts: List[Dict[str, Any]],
    accounts_mapping_id: str,
    project_key: str,
    issuetype_id: str,
) -> Dict[str, Any]:
    """
    Compose the body the integration-jira-service endpoint expects.

    The UI sends the entire `incident` object as-is plus a synthesized
    `issue.fields` block. We do the same — the endpoint pre-validates many
    inner fields, so trimming the incident invites 422s.
    """
    return {
        "accounts_mapping_id": accounts_mapping_id,
        "incident": incident,
        "issue": {
            "fields": {
                "summary": incident.get("title", "").rstrip("\n") + "\n",
                "description": build_issue_description(incident, alerts),
                "project": {"key": project_key},
                "issuetype": {"id": issuetype_id},
            }
        },
    }


# ---------------------------------------------------------------------------
# Endpoint call
# ---------------------------------------------------------------------------


class CreateDRGNError(RuntimeError):
    """Raised when the integration endpoint refuses the request."""


class IncidentAlreadyHasDRGNError(CreateDRGNError):
    """
    Raised when the endpoint refuses creation because a DRGN already exists.

    The endpoint returns 422 with message "PagerDuty create button, Incident
    already exist, stopping" — this is its server-side dedup check, more
    reliable than scanning external_references on the PD side. We translate
    that into a typed exception so callers (and pd-escalate) can fall back
    to fetching the existing DRGN instead of failing the workflow.
    """


def post_create_issue(
    request_body: Dict[str, Any],
    pd_ui_bearer_token: str,
    timeout: int = DEFAULT_REQUEST_TIMEOUT,
) -> Dict[str, Any]:
    """
    POST to integration-jira-service/create_issue_from_pagerduty.

    Auth: requires the PD UI bearer token (`pdus+_...`); a classic REST API
    key in this header returns 401.
    """
    response = requests.post(
        CREATE_ISSUE_URL,
        headers={
            "Authorization": f"Bearer {pd_ui_bearer_token}",
            "Content-Type": "application/json",
            "Accept": "*/*",
            # Origin matters: the UI sends it; without it some PD edges treat
            # the call as cross-origin and reject.
            "Origin": "https://app.pagerduty.com",
        },
        data=json.dumps(request_body),
        timeout=timeout,
        verify=False,
    )

    # The endpoint returns 401 when the bearer is missing/expired/invalid,
    # and 422 with structured errors when the body is wrong.
    if response.status_code == 401:
        raise CreateDRGNError(
            "401 Unauthorized — PD_UI_BEARER_TOKEN is missing, expired, or "
            "uses the wrong scheme. UI bearer tokens live ~1 week. Capture "
            "a fresh one from Chrome DevTools (any app.pagerduty.com XHR → "
            "Authorization header) and update .env."
        )

    try:
        response_payload = response.json()
    except ValueError:
        response_payload = {"raw": response.text}

    if response.status_code >= 400:
        error_block = response_payload.get("error") or {}
        error_message = error_block.get("message") or response_payload.get("raw") or "unknown error"
        error_details = error_block.get("errors") or []
        details_str = "; ".join(error_details) if error_details else ""

        # Endpoint's own dedup check fires when a DRGN is already linked to
        # this incident — surface as a typed error so pd-escalate can fall
        # back to "use the existing DRGN" instead of failing.
        if (
            response.status_code == 422
            and "already exist" in (error_message or "").lower()
        ):
            raise IncidentAlreadyHasDRGNError(error_message)

        raise CreateDRGNError(
            f"HTTP {response.status_code} from integration-jira-service: "
            f"{error_message}"
            + (f" ({details_str})" if details_str else "")
        )

    return response_payload


# ---------------------------------------------------------------------------
# Top-level workflow
# ---------------------------------------------------------------------------


def create_drgn_for_incident(
    incident_id: str,
    pagerduty_api_token: str,
    pd_ui_bearer_token: str,
    accounts_mapping_id: str = DEFAULT_ACCOUNTS_MAPPING_ID,
    project_key: str = DEFAULT_PROJECT_KEY,
    issuetype_id: str = DEFAULT_ISSUETYPE_ID,
    skip_if_exists: bool = True,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Create a DRGN ticket for the given PD incident.

    Returns a dict like:
        {"drgn_key": "DRGN-17946",
         "drgn_url": "https://jira.livenation.com/browse/DRGN-17946",
         "already_existed": False}

    If `skip_if_exists` and the incident already has a DRGN linked via
    external_references, returns that DRGN without re-posting.
    """
    incident = fetch_pd_incident(incident_id, pagerduty_api_token)

    # Cheap pre-check: if external_references already mentions a DRGN we can
    # skip both the alerts fetch and the POST.
    if skip_if_exists:
        existing_from_refs = find_existing_drgn_in_incident(incident)
        if existing_from_refs:
            return {
                "drgn_key": existing_from_refs,
                "drgn_url": f"https://jira.livenation.com/browse/{existing_from_refs}",
                "already_existed": True,
            }

    alerts = fetch_pd_alerts(incident_id, pagerduty_api_token)
    request_body = build_request_body(
        incident=incident,
        alerts=alerts,
        accounts_mapping_id=accounts_mapping_id,
        project_key=project_key,
        issuetype_id=issuetype_id,
    )

    if dry_run:
        return {
            "drgn_key": None,
            "drgn_url": None,
            "already_existed": False,
            "dry_run_payload": request_body,
        }

    try:
        response_payload = post_create_issue(request_body, pd_ui_bearer_token)
    except IncidentAlreadyHasDRGNError:
        # Endpoint's own dedup check fired. external_references was empty
        # (otherwise we'd have short-circuited above), so look in PD notes
        # for the Jira-automation comment that carries the DRGN URL.
        existing_from_notes = find_existing_drgn_in_notes(incident_id, pagerduty_api_token)
        if existing_from_notes:
            return {
                "drgn_key": existing_from_notes,
                "drgn_url": f"https://jira.livenation.com/browse/{existing_from_notes}",
                "already_existed": True,
            }
        # Endpoint said "exists" but we can't find the key — surface that so
        # the user investigates manually instead of silently succeeding.
        raise CreateDRGNError(
            f"Endpoint reports DRGN already exists for incident {incident_id}, "
            f"but no DRGN reference found in external_references or notes."
        )

    issue = response_payload.get("issue") or {}
    drgn_key = issue.get("key")
    drgn_url = issue.get("html_url")

    if not drgn_key:
        raise CreateDRGNError(
            f"Endpoint returned 200 but no issue.key in response: {response_payload}"
        )

    return {
        "drgn_key": drgn_key,
        "drgn_url": drgn_url,
        "already_existed": False,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point for the pd-create-drgn CLI."""
    load_env()

    parser = argparse.ArgumentParser(
        prog="pd_create_drgn.py",
        description=(
            "Create a DRGN Jira ticket for a PagerDuty incident "
            "by simulating the UI's 'Create Jira Issue' button."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s --pd Q1WPEMZKLQZGJF\n"
            "  %(prog)s --pd https://tmtoc.pagerduty.com/incidents/Q1WPEMZKLQZGJF\n"
            "  %(prog)s --pd Q1WPEMZKLQZGJF --dry-run     # show payload without POST\n"
            "  %(prog)s --pd Q1WPEMZKLQZGJF --json        # machine-readable output\n"
            "\n"
            "Required env:\n"
            "  PAGERDUTY_API_TOKEN  — REST API key (Token token=...)\n"
            "  PD_UI_BEARER_TOKEN   — UI session bearer (pdus+_...); ~1-week TTL,\n"
            "                         capture from Chrome DevTools any app.pagerduty.com XHR\n"
        ),
    )
    parser.add_argument(
        "--pd",
        required=True,
        help="PagerDuty incident ID or URL (required).",
    )
    parser.add_argument(
        "--accounts-mapping-id",
        default=DEFAULT_ACCOUNTS_MAPPING_ID,
        help=f"Override the PD↔Jira accounts mapping ID (default {DEFAULT_ACCOUNTS_MAPPING_ID}).",
    )
    parser.add_argument(
        "--project-key",
        default=DEFAULT_PROJECT_KEY,
        help=f"Jira project key (default {DEFAULT_PROJECT_KEY}).",
    )
    parser.add_argument(
        "--issuetype-id",
        default=DEFAULT_ISSUETYPE_ID,
        help=f"Jira issuetype ID (default {DEFAULT_ISSUETYPE_ID} = 'Alert').",
    )
    parser.add_argument(
        "--allow-duplicate",
        action="store_true",
        help=(
            "Force creation even if the incident already has a linked DRGN. "
            "Default behaviour returns the existing DRGN without re-posting."
        ),
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Build the request body and print it without POSTing.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output JSON {drgn_key, drgn_url, already_existed} on stdout.",
    )
    parser.add_argument(
        "--version",
        "-v",
        action="version",
        version=f"%(prog)s {VERSION}",
    )

    args = parser.parse_args()

    incident_id = extract_incident_id(args.pd)

    # PAGERDUTY_API_TOKEN is always needed (we read the incident either way).
    # PD_UI_BEARER_TOKEN is needed only when we'll actually POST — in dry-run
    # mode we still skip it so users can inspect the payload before hunting
    # down a fresh UI bearer.
    required_env_keys = ["PAGERDUTY_API_TOKEN"]
    if not args.dry_run:
        required_env_keys.append("PD_UI_BEARER_TOKEN")
    env_values = require_env(*required_env_keys)

    try:
        result = create_drgn_for_incident(
            incident_id=incident_id,
            pagerduty_api_token=env_values["PAGERDUTY_API_TOKEN"],
            pd_ui_bearer_token=env_values.get("PD_UI_BEARER_TOKEN", ""),
            accounts_mapping_id=args.accounts_mapping_id,
            project_key=args.project_key,
            issuetype_id=args.issuetype_id,
            skip_if_exists=not args.allow_duplicate,
            dry_run=args.dry_run,
        )
    except (CreateDRGNError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(result, indent=2))
        return

    if args.dry_run:
        print(f"[DRY-RUN] Would POST to {CREATE_ISSUE_URL}")
        print(f"[DRY-RUN] Payload size: {len(json.dumps(result['dry_run_payload']))} bytes")
        print(f"[DRY-RUN] Incident title: {result['dry_run_payload']['incident'].get('title','').rstrip()}")
        return

    drgn_key = result["drgn_key"]
    drgn_url = result["drgn_url"]
    if result["already_existed"]:
        print(f"{drgn_key} already exists for incident {incident_id}: {drgn_url}")
    else:
        print(f"Created {drgn_key}: {drgn_url}")


if __name__ == "__main__":
    main()
