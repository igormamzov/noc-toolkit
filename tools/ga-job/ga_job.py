#!/usr/bin/env python3
"""GoAnywhere Web Client read-only CLI.

Usage:
    python ga_job.py login                      # one-time OKTA login
    python ga_job.py find-job 1000006395396     # look up by Job Number
    python ga_job.py list-jobs --submitted-by API-GACMD --status Success
    python ga_job.py list-jobs --project copy_files --user teal.triangle
    python ga_job.py find-monitor 'jb_edw_resale_tnow.*'
    python ga_job.py list-monitors

All commands honour --json for machine-readable output.

Session cookies are cached at ~/.ga_session.json for ~30 min idle.
Re-run `login` to refresh.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from pathlib import Path
from typing import List

# Suppress urllib3 InsecureRequestWarning — corporate cert chains often
# don't validate from the local trust store, and verify=False is the
# pragmatic choice for an internal tool.
warnings.filterwarnings("ignore", message="Unverified HTTPS request")
try:
    from bs4 import XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except ImportError:
    pass

# When invoked via the noc-toolkit launcher, tools/common is already on
# sys.path. When run directly (python ga_job.py ...), add it ourselves.
_COMMON = Path(__file__).resolve().parents[1] / "common"
if str(_COMMON) not in sys.path:
    sys.path.insert(0, str(_COMMON))

from noc_utils import load_env

from ga_session import get_session, GA_BASE_URL_DEFAULT
from ga_jobs import find_job, filter_jobs, list_completed_jobs, CompletedJob
from ga_monitors import find_monitor, list_monitors, Monitor


def _print_jobs(jobs: List[CompletedJob], as_json: bool) -> None:
    if as_json:
        print(json.dumps([j.as_dict() for j in jobs], indent=2))
        return
    if not jobs:
        print("No matching jobs in the most recent 100.")
        return
    print(f"{'Job Number':<14} {'Status':<10} {'Project':<32} {'Submitted From':<20} {'Run User':<20} {'Start':<19}")
    print("-" * 120)
    for j in jobs:
        print(
            f"{j.job_number:<14} {j.status[:10]:<10} {j.project_name[:32]:<32} "
            f"{j.submitted_from[:20]:<20} {j.run_user[:20]:<20} {j.start_time[:19]:<19}"
        )


def _print_monitors(monitors: List[Monitor], as_json: bool) -> None:
    if as_json:
        print(json.dumps([m.as_dict() for m in monitors], indent=2))
        return
    if not monitors:
        print("No matching monitors.")
        return
    print(f"{'Name':<40} {'Last Run':<22} {'Next Run':<22} {'Runs':<6} {'Fired':<6}")
    print("-" * 100)
    for m in monitors:
        print(
            f"{m.name[:40]:<40} {m.last_run_time[:22]:<22} {m.next_run_time[:22]:<22} "
            f"{m.run_count[:6]:<6} {m.actions_fired[:6]:<6}"
        )


def cmd_login(args) -> int:
    sess = get_session(force_login=True)
    print(f"OK. Captured cookies: {list(sess.cookies.keys())}")
    return 0


def cmd_find_job(args) -> int:
    job = find_job(args.job_number)
    if not job:
        print(f"Job {args.job_number} not found in the most recent 100 completed jobs.")
        print("(For older jobs use the GoAnywhere UI directly.)")
        return 1
    _print_jobs([job], args.json)
    return 0


def cmd_list_jobs(args) -> int:
    jobs = filter_jobs(
        submitted_by=args.submitted_by,
        project_pattern=args.project,
        status=args.status,
        run_user=args.user,
    )
    _print_jobs(jobs, args.json)
    return 0


def cmd_find_monitor(args) -> int:
    monitors = find_monitor(args.pattern)
    _print_monitors(monitors, args.json)
    return 0 if monitors else 1


def cmd_list_monitors(args) -> int:
    _print_monitors(list_monitors(), args.json)
    return 0


def main(argv=None) -> int:
    load_env()
    parser = argparse.ArgumentParser(
        description="GoAnywhere read-only CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    # Parent parser so --json works on every subcommand
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="Output JSON")

    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", parents=[common], help="One-time OKTA login (refresh session)")

    p_find = sub.add_parser("find-job", parents=[common], help="Look up a job by Job Number")
    p_find.add_argument("job_number")

    p_list = sub.add_parser("list-jobs", parents=[common], help="List recent completed jobs with filters")
    p_list.add_argument("--submitted-by", help="Filter by Submitted From (substring)")
    p_list.add_argument("--project", help="Filter by Project Name (regex)")
    p_list.add_argument("--status", help="Filter by Status (substring)")
    p_list.add_argument("--user", help="Filter by Run User (substring)")

    p_mon = sub.add_parser("find-monitor", parents=[common], help="Search monitors by name regex")
    p_mon.add_argument("pattern")

    sub.add_parser("list-monitors", parents=[common], help="List all monitors")

    args = parser.parse_args(argv)

    handlers = {
        "login": cmd_login,
        "find-job": cmd_find_job,
        "list-jobs": cmd_list_jobs,
        "find-monitor": cmd_find_monitor,
        "list-monitors": cmd_list_monitors,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
