"""Close DRGN ticket via Jira transition 61."""

from __future__ import annotations

import json
import os
import ssl
import urllib.request
from typing import Optional

JIRA_BASE = "https://jira.livenation.com"
TRANSITION_CLOSE = "61"

# Field IDs from the noc-engineer skill (verified earlier)
CF_ALERT_CATEGORY = "customfield_45201"
CF_SLA_VIOLATION = "customfield_45202"
CF_RUNBOOK_STATUS = "customfield_45203"
CF_RUNBOOK_LINK = "customfield_38218"

ALERT_CATEGORY_ETL = "64520"
SLA_NO = "64528"
SLA_YES = "64527"
SLA_UNKNOWN = "64529"
RUNBOOK_UP_TO_DATE = "64530"
RUNBOOK_OUTDATED = "64531"
RUNBOOK_MISSING = "64532"

RESOLUTION_AUTO = "12901"          # Resolved Automatically
RESOLUTION_STD_PROC = "12903"      # Resolved via Standard Procedure

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def close_drgn(
    drgn_key: str,
    *,
    sla_violation: str = SLA_NO,
    runbook_status: str = RUNBOOK_MISSING,
    runbook_link: Optional[str] = None,
    resolution: str = RESOLUTION_AUTO,
    comment: str = "repaired and next run succeeded",
    alert_category: str = ALERT_CATEGORY_ETL,
) -> int:
    """Transition the DRGN to Closed. Returns HTTP status code."""
    fields = {
        CF_ALERT_CATEGORY: {"id": alert_category},
        CF_SLA_VIOLATION: {"id": sla_violation},
        CF_RUNBOOK_STATUS: {"id": runbook_status},
        "resolution": {"id": resolution},
    }
    if runbook_link:
        fields[CF_RUNBOOK_LINK] = runbook_link

    payload = {
        "transition": {"id": TRANSITION_CLOSE},
        "fields": fields,
        "update": {
            "comment": [{"add": {"body": comment}}],
        },
    }

    req = urllib.request.Request(
        f"{JIRA_BASE}/rest/api/2/issue/{drgn_key}/transitions",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {os.environ['JIRA_PERSONAL_ACCESS_TOKEN']}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        r = urllib.request.urlopen(req, context=_CTX, timeout=20)
        return r.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:600]
        raise RuntimeError(f"Jira transition failed for {drgn_key}: HTTP {e.code} — {body}")


def get_drgn_status(drgn_key: str) -> dict:
    """Return current status + resolution of the DRGN."""
    req = urllib.request.Request(
        f"{JIRA_BASE}/rest/api/2/issue/{drgn_key}?fields=summary,status,resolution",
        headers={
            "Authorization": f"Bearer {os.environ['JIRA_PERSONAL_ACCESS_TOKEN']}",
            "Accept": "application/json",
        },
    )
    return json.loads(urllib.request.urlopen(req, context=_CTX, timeout=15).read())["fields"]
