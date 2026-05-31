"""On-disk JSON state for scheduled-recheck.

State file lives next to the tool (`state.json`) so it travels with the
toolkit checkout. It is .gitignored so Igor's local pending checks don't
get committed.

Each scheduled task has shape:

    {
        "id": "src-DRGN-17897-1717123456",
        "drgn": "DRGN-17897",
        "pd": "Q2F39BSFK5L3V2",
        "check_type": "cdt-job-success-after",
        "check_args": {
            "job_name": "asra_split_trx_header_fact",
            "after_iso": "2026-05-31T05:11:00Z"
        },
        "fire_at_iso": "2026-05-31T05:58:00Z",
        "on_success": {
            "action": "close-drgn",
            "resolution": "rvsp",
            "reference": ["DSSD-31131"],
            "append": "Igor performed manual repair at 05:11 UTC",
            "runbook_url": null,
            "sla": "no"
        },
        "on_failure": {
            "action": "notify",
            "message": "next run still failed — pattern intensifying"
        },
        "created_at_iso": "2026-05-31T05:46:00Z",
        "status": "pending",
        "attempts": 0,
        "last_attempt_at_iso": null,
        "last_result": null
    }

Status values: pending / done / failed / cancelled.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_DIR = Path(__file__).resolve().parent
STATE_PATH = _DIR / "state.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {"tasks": []}
    try:
        return json.loads(STATE_PATH.read_text())
    except json.JSONDecodeError:
        # Don't blow up on a corrupt file; user can inspect it manually.
        return {"tasks": []}


def _save(data: Dict[str, Any]) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, STATE_PATH)


def list_tasks(status: Optional[str] = None) -> List[Dict[str, Any]]:
    tasks = _load().get("tasks", [])
    if status:
        tasks = [t for t in tasks if t.get("status") == status]
    return tasks


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    for t in _load().get("tasks", []):
        if t.get("id") == task_id:
            return t
    return None


def add_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """Insert a task. Generates id + timestamps if not provided."""
    data = _load()
    if not task.get("id"):
        suffix = uuid.uuid4().hex[:6]
        drgn = task.get("drgn") or "task"
        task["id"] = f"{drgn}-{suffix}"
    task.setdefault("created_at_iso", _now_iso())
    task.setdefault("status", "pending")
    task.setdefault("attempts", 0)
    task.setdefault("last_attempt_at_iso", None)
    task.setdefault("last_result", None)
    data["tasks"].append(task)
    _save(data)
    return task


def update_task(task_id: str, **changes) -> Optional[Dict[str, Any]]:
    data = _load()
    for t in data.get("tasks", []):
        if t.get("id") == task_id:
            t.update(changes)
            _save(data)
            return t
    return None


def remove_task(task_id: str) -> bool:
    data = _load()
    before = len(data.get("tasks", []))
    data["tasks"] = [t for t in data.get("tasks", []) if t.get("id") != task_id]
    after = len(data["tasks"])
    if before == after:
        return False
    _save(data)
    return True


def pending_due(now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Return pending tasks whose fire_at_iso is <= now (UTC)."""
    if now is None:
        now = datetime.now(timezone.utc)
    out: List[Dict[str, Any]] = []
    for t in list_tasks(status="pending"):
        fire = t.get("fire_at_iso")
        if not fire:
            continue
        try:
            fire_dt = datetime.strptime(fire.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            continue
        if fire_dt <= now:
            out.append(t)
    return out
