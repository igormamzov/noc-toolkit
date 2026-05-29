"""Parse GoAnywhere CompletedJobs.xhtml.

The page renders the most recent 100 completed jobs as an HTML table with
columns:
    [checkbox] [actions] Job Number | Domain | Project Name | In Folder
    | Status | Run User | Start Time | End Time | Time (sec) | Submitted From

A typical tenant has ~25k historic jobs, but only the 100 most recent are
returned without a JSF postback. For older jobs, use the GoAnywhere UI
directly — that scenario is out of scope for NOC triage automation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import List, Optional

from bs4 import BeautifulSoup

from ga_session import GASession, http_get

COMPLETED_JOBS_PATH = "/goanywhere/jobs/CompletedJobs.xhtml"

# Column index in the data table (after [checkbox] [actions] columns)
COL_JOB_NUMBER = 2
COL_DOMAIN = 3
COL_PROJECT = 4
COL_FOLDER = 5
COL_STATUS = 6
COL_RUN_USER = 7
COL_START = 8
COL_END = 9
COL_DURATION = 10
COL_SUBMITTED_FROM = 11


@dataclass
class CompletedJob:
    job_number: str
    domain: str
    project_name: str
    in_folder: str
    status: str
    run_user: str
    start_time: str
    end_time: str
    duration_sec: str
    submitted_from: str

    def url(self, base_url: str) -> str:
        return f"{base_url}{COMPLETED_JOBS_PATH}"

    def as_dict(self) -> dict:
        return asdict(self)


def _find_jobs_table(soup: BeautifulSoup):
    """Return the largest table — that's the completed jobs grid."""
    tables = soup.find_all("table")
    if not tables:
        return None
    return max(tables, key=lambda t: len(t.find_all("tr")))


def _cell_text(cells, idx: int) -> str:
    """Return cell text, with fallback to img title (for icon-only cells like Status)."""
    if idx >= len(cells):
        return ""
    cell = cells[idx]
    txt = cell.get_text(strip=True)
    if txt:
        return txt
    img = cell.find("img")
    if img:
        return (img.get("title") or img.get("alt") or "").strip()
    return ""


def list_completed_jobs(sess: Optional[GASession] = None) -> List[CompletedJob]:
    """Fetch and parse the most recent 100 completed jobs."""
    r = http_get(COMPLETED_JOBS_PATH, sess=sess)
    if r.status_code != 200:
        raise RuntimeError(f"GA returned HTTP {r.status_code} for CompletedJobs")
    if "auth/Login" in r.text[:5000]:
        raise RuntimeError(
            "GA session expired. Re-run with --login to refresh cookie."
        )

    soup = BeautifulSoup(r.text, "html.parser")
    table = _find_jobs_table(soup)
    if not table:
        return []
    body = table.find("tbody")
    if not body:
        return []

    jobs: List[CompletedJob] = []
    for row in body.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < COL_SUBMITTED_FROM + 1:
            continue
        job_number = _cell_text(cells, COL_JOB_NUMBER)
        if not job_number.isdigit():
            continue
        jobs.append(CompletedJob(
            job_number=job_number,
            domain=_cell_text(cells, COL_DOMAIN),
            project_name=_cell_text(cells, COL_PROJECT),
            in_folder=_cell_text(cells, COL_FOLDER),
            status=_cell_text(cells, COL_STATUS),
            run_user=_cell_text(cells, COL_RUN_USER),
            start_time=_cell_text(cells, COL_START),
            end_time=_cell_text(cells, COL_END),
            duration_sec=_cell_text(cells, COL_DURATION),
            submitted_from=_cell_text(cells, COL_SUBMITTED_FROM),
        ))
    return jobs


def find_job(job_number: str, sess: Optional[GASession] = None) -> Optional[CompletedJob]:
    """Return the matching job from the most recent 100 (None if older)."""
    for j in list_completed_jobs(sess=sess):
        if j.job_number == job_number:
            return j
    return None


def filter_jobs(
    sess: Optional[GASession] = None,
    *,
    submitted_by: Optional[str] = None,
    project_pattern: Optional[str] = None,
    status: Optional[str] = None,
    run_user: Optional[str] = None,
) -> List[CompletedJob]:
    """Return jobs from the most recent 100 matching the given filters.

    Filters are case-insensitive substring matches, except project_pattern
    which is treated as a regex.
    """
    jobs = list_completed_jobs(sess=sess)
    out: List[CompletedJob] = []
    proj_re = re.compile(project_pattern, re.IGNORECASE) if project_pattern else None
    for j in jobs:
        if submitted_by and submitted_by.lower() not in j.submitted_from.lower():
            continue
        if status and status.lower() not in j.status.lower():
            continue
        if run_user and run_user.lower() not in j.run_user.lower():
            continue
        if proj_re and not proj_re.search(j.project_name):
            continue
        out.append(j)
    return out
