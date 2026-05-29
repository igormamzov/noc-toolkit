"""Read CDT dashboards: streaming jobs, batch jobs, SLA breaches.

Endpoints from /openapi.json:
    GET /streaming_dashboard?environment=<env>
    GET /batch_dashboard?environment=<env>
    GET /sla_monitor/breaches?environment=<env>
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from cdt_client import CDTClient


def streaming_dashboard(client: CDTClient, environment: str = "prod") -> List[Dict[str, Any]]:
    return client.get("/streaming_dashboard", environment=environment)


def batch_dashboard(client: CDTClient, environment: str = "prod") -> List[Dict[str, Any]]:
    return client.get("/batch_dashboard", environment=environment)


def sla_breaches(
    client: CDTClient,
    environment: str = "prod",
    *,
    job_type: Optional[str] = None,
    job_status: Optional[str] = None,
    name_pattern: Optional[str] = None,
    since_hours: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Fetch SLA breaches and apply client-side filters.

    Filters:
        job_type — e.g. 'batch', 'workflow', 'client_feed', 'sql_data_loader'
        job_status — 'failed', 'not started', 'skipped', 'success'
        name_pattern — regex against job_name (case-insensitive)
        since_hours — only breaches with created_at within last N hours
    """
    items = client.get("/sla_monitor/breaches", environment=environment)
    pat = re.compile(name_pattern, re.IGNORECASE) if name_pattern else None
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=since_hours)
              if since_hours else None)
    out: List[Dict[str, Any]] = []
    for it in items:
        if job_type and it.get("job_type") != job_type:
            continue
        if job_status and it.get("job_status") != job_status:
            continue
        if pat and not pat.search(it.get("job_name", "")):
            continue
        if cutoff:
            ts = it.get("created_at") or it.get("fire_ts")
            if not ts:
                continue
            try:
                # CDT returns ISO timestamps without timezone — assume UTC
                created = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            if created < cutoff:
                continue
        out.append(it)
    return out


def find_streaming_job(
    client: CDTClient,
    name_or_id: str,
    environment: str = "prod",
) -> List[Dict[str, Any]]:
    """Match by instance_id, instance_file_name, or service_name regex."""
    items = streaming_dashboard(client, environment)
    pat = re.compile(name_or_id, re.IGNORECASE)
    return [
        it for it in items
        if str(it.get("instance_id")) == name_or_id
        or pat.search(it.get("instance_file_name", "") or "")
        or pat.search(it.get("airflow_id", "") or "")
        or pat.search(it.get("service_name", "") or "")
    ]


def find_batch_job(
    client: CDTClient,
    name_or_id: str,
    environment: str = "prod",
) -> List[Dict[str, Any]]:
    """Match by job_id, file_name, or airflow_id (regex)."""
    items = batch_dashboard(client, environment)
    pat = re.compile(name_or_id, re.IGNORECASE)
    return [
        it for it in items
        if str(it.get("job_id")) == name_or_id
        or pat.search(it.get("file_name", "") or "")
        or pat.search(it.get("airflow_id", "") or "")
    ]
