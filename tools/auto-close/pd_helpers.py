"""PagerDuty + DRGN extraction helpers for auto-close."""

from __future__ import annotations

import os
import re
import ssl
import json
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Optional

PD_BASE = "https://api.pagerduty.com"
JIRA_BASE = "https://jira.livenation.com"

# Build SSL context once
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

# ---- PD ----------------------------------------------------------------

def _pd_headers() -> dict:
    return {
        "Authorization": f"Token token={os.environ['PAGERDUTY_API_TOKEN']}",
        "Accept": "application/vnd.pagerduty+json;version=2",
    }


def pd_incident(incident_id: str) -> dict:
    req = urllib.request.Request(f"{PD_BASE}/incidents/{incident_id}", headers=_pd_headers())
    return json.loads(urllib.request.urlopen(req, context=_CTX, timeout=20).read())["incident"]


def pd_notes(incident_id: str) -> List[dict]:
    req = urllib.request.Request(f"{PD_BASE}/incidents/{incident_id}/notes",
                                  headers=_pd_headers())
    return json.loads(urllib.request.urlopen(req, context=_CTX, timeout=20).read()).get("notes", [])


def pd_alerts(incident_id: str) -> List[dict]:
    req = urllib.request.Request(f"{PD_BASE}/incidents/{incident_id}/alerts",
                                  headers=_pd_headers())
    return json.loads(urllib.request.urlopen(req, context=_CTX, timeout=20).read()).get("alerts", [])


# CDS-OPS-24x7 service ID. Found by GET /services?query=CDS-OPS-24x7.
# This is the service that auto-creates DRGN tickets via Jira integration.
CDS_OPS_SERVICE_ID = "PF1L8SI"


def pd_list_acknowledged(service_id: Optional[str] = CDS_OPS_SERVICE_ID,
                          limit: int = 100) -> List[dict]:
    """List currently acknowledged PD incidents.

    Defaults: filter to CDS-OPS-24x7 service, sort newest first. Without
    these defaults the API returns the 100 OLDEST acknowledged incidents
    across all services — useless for detecting fresh failures.
    """
    params = [
        "statuses[]=acknowledged",
        "sort_by=created_at:desc",
        f"limit={limit}",
    ]
    if service_id:
        params.append(f"service_ids[]={service_id}")
    qs = "&".join(params)
    req = urllib.request.Request(f"{PD_BASE}/incidents?{qs}", headers=_pd_headers())
    return json.loads(urllib.request.urlopen(req, context=_CTX, timeout=30).read()).get("incidents", [])


# ---- Title parsing ------------------------------------------------------

# "[ERROR] [DATABRICKS] Databricks batch job <job_name> failed"
# The 'repaired ' prefix is added by Prometheus when alert auto-resolves.
_TITLE_RE = re.compile(
    r"(?:repaired\s+)?\[ERROR\]\s+\[DATABRICKS\]\s+Databricks\s+batch\s+job\s+(\S+)\s+failed",
    re.IGNORECASE,
)


@dataclass
class IncidentSummary:
    pd_id: str
    pd_number: int
    title: str
    status: str
    priority: str
    created_at: str
    job_name: Optional[str]
    is_repaired: bool  # title starts with "repaired"
    drgn_key: Optional[str]
    dssd_key: Optional[str]  # set if PD title references a DSSD escalation
    runbook: Optional[str]


def extract_job_name(title: str) -> Optional[str]:
    m = _TITLE_RE.search(title)
    return m.group(1) if m else None


def is_repaired(title: str) -> bool:
    return title.lower().lstrip().startswith("repaired ")


_DRGN_RE = re.compile(r"\b(DRGN-\d+)\b")
_DSSD_RE = re.compile(r"\b(DSSD-\d+)\b")


def find_dssd_in_title(title: str) -> Optional[str]:
    """A DSSD-XXXX in the PD title means the incident is already escalated."""
    m = _DSSD_RE.search(title or "")
    return m.group(1) if m else None


def find_drgn_in_notes(notes: List[dict]) -> Optional[str]:
    """Return the DRGN key for THIS PD incident.

    PD notes are returned in created-desc order. We prefer a DRGN found in a
    Jira-automation note ("Jira issue has been created for the incident: ...DRGN-NNN")
    because that's deterministically the DRGN auto-created from this PD. If no
    such note exists, fall back to any DRGN reference (oldest first), since
    older notes are less likely to mention unrelated DRGN keys.
    """
    # Pass 1: prefer Jira-automation note
    for n in notes:
        author = ((n.get("user") or {}).get("summary") or "").lower()
        content = n.get("content", "") or ""
        if ("jira" in author or "responder" in author) and "jira issue has been created" in content.lower():
            m = _DRGN_RE.search(content)
            if m:
                return m.group(1)
    # Pass 2: fallback — oldest note with a DRGN reference
    for n in reversed(notes):
        m = _DRGN_RE.search(n.get("content", "") or "")
        if m:
            return m.group(1)
    return None


def find_repaired_in_notes(notes: List[dict]) -> bool:
    return any("repaired" in (n.get("content", "") or "").lower() for n in notes)


def first_alert_runbook(alerts: List[dict]) -> Optional[str]:
    for a in alerts:
        det = (a.get("body", {}) or {}).get("cef_details", {}) or {}
        rb = (det.get("details") or {}).get("runbook")
        if rb and rb != "Missing":
            return rb
    return None


def summarize_incident(pd_id: str) -> IncidentSummary:
    inc = pd_incident(pd_id)
    notes = pd_notes(pd_id)
    alerts = pd_alerts(pd_id)
    title = inc.get("title", "")
    return IncidentSummary(
        pd_id=pd_id,
        pd_number=inc["incident_number"],
        title=title,
        status=inc["status"],
        priority=(inc.get("priority") or {}).get("summary", ""),
        created_at=inc["created_at"],
        job_name=extract_job_name(title),
        is_repaired=is_repaired(title) or find_repaired_in_notes(notes),
        drgn_key=find_drgn_in_notes(notes),
        dssd_key=find_dssd_in_title(title),
        runbook=first_alert_runbook(alerts),
    )


# ---- URL parsing --------------------------------------------------------

_PD_URL_RE = re.compile(r"/incidents/([A-Z0-9]+)")


def parse_pd_id(s: str) -> Optional[str]:
    """Accept either bare PD ID or full PD URL."""
    s = s.strip()
    if not s:
        return None
    m = _PD_URL_RE.search(s)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Z0-9]{14}", s):
        return s
    return None


# ---- Time helpers -------------------------------------------------------

def parse_iso_naive(ts: Optional[str]) -> Optional[datetime]:
    """Parse ISO timestamp; return UTC-aware datetime. CDT returns naive UTC."""
    if not ts:
        return None
    try:
        # Strip timezone (Z or +00:00) for consistency, then mark UTC
        ts = ts.replace("Z", "")
        if "+" in ts:
            ts = ts.split("+")[0]
        return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
