#!/usr/bin/env python3
"""CDT Control Panel API CLI (read-only).

Usage:
    python cdt.py health
    python cdt.py streaming                                 # list streaming jobs in prod
    python cdt.py streaming --status running --env prod
    python cdt.py find-streaming jb_edw_resale_tnow         # name regex
    python cdt.py batch                                     # list batch jobs in prod
    python cdt.py batch --status failed
    python cdt.py find-batch <airflow_id_or_regex>
    python cdt.py sla                                       # all SLA breaches in prod
    python cdt.py sla --since-hours 24 --status failed
    python cdt.py sla --type batch --name 'jb_edw_dsn_sls'

All commands accept --json for machine-readable output.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from pathlib import Path
from typing import List, Dict, Any

warnings.filterwarnings("ignore", message="Unverified HTTPS request")

# tools/common is added to sys.path by the noc-toolkit launcher; for direct
# `python cdt.py` invocation, add it ourselves.
_COMMON = Path(__file__).resolve().parents[1] / "common"
if str(_COMMON) not in sys.path:
    sys.path.insert(0, str(_COMMON))

from noc_utils import load_env

from cdt_client import CDTClient, CDTAuthError
from cdt_dashboards import (
    streaming_dashboard, batch_dashboard, sla_breaches,
    find_streaming_job, find_batch_job,
)


def _fmt_streaming(items: List[Dict[str, Any]]) -> None:
    if not items:
        print("(no matches)")
        return
    print(f"{'ID':<8} {'Status':<10} {'Service':<35} {'File Name':<45} {'Started':<19}")
    print("-" * 120)
    for it in items:
        print(
            f"{str(it.get('instance_id',''))[:8]:<8} "
            f"{(it.get('instance_status') or '')[:10]:<10} "
            f"{(it.get('service_name') or '')[:35]:<35} "
            f"{(it.get('instance_file_name') or '')[:45]:<45} "
            f"{(it.get('start_time') or '')[:19]:<19}"
        )


def _fmt_batch(items: List[Dict[str, Any]]) -> None:
    if not items:
        print("(no matches)")
        return
    print(f"{'Job ID':<18} {'Status':<10} {'File Name':<55} {'Last Run End':<19}")
    print("-" * 110)
    for it in items:
        last = it.get("last_status") or {}
        print(
            f"{str(it.get('job_id',''))[:18]:<18} "
            f"{(last.get('ss_status') or '')[:10]:<10} "
            f"{(it.get('file_name') or '')[:55]:<55} "
            f"{(last.get('ss_end_time') or '')[:19]:<19}"
        )


def _fmt_sla(items: List[Dict[str, Any]]) -> None:
    if not items:
        print("(no matches)")
        return
    print(f"{'Type':<12} {'Status':<11} {'SLA':<4} {'Job Name':<55} {'Fire TS':<19}")
    print("-" * 110)
    for it in items:
        print(
            f"{(it.get('job_type') or '')[:12]:<12} "
            f"{(it.get('job_status') or '')[:11]:<11} "
            f"{str(it.get('sla') or '')[:4]:<4} "
            f"{(it.get('job_name') or '')[:55]:<55} "
            f"{(it.get('fire_ts') or '')[:19]:<19}"
        )


def _output(items, fmt_func, as_json: bool) -> None:
    if as_json:
        print(json.dumps(items, indent=2))
    else:
        fmt_func(items)


def cmd_health(args, client) -> int:
    print(f"health: {client.health()}")
    print(f"auth ok: {client.is_authorized()}")
    return 0


def cmd_streaming(args, client) -> int:
    items = streaming_dashboard(client, environment=args.env)
    if args.status:
        items = [i for i in items if i.get("instance_status") == args.status]
    _output(items, _fmt_streaming, args.json)
    return 0


def cmd_find_streaming(args, client) -> int:
    items = find_streaming_job(client, args.pattern, environment=args.env)
    _output(items, _fmt_streaming, args.json)
    return 0 if items else 1


def cmd_batch(args, client) -> int:
    items = batch_dashboard(client, environment=args.env)
    if args.status:
        items = [
            i for i in items
            if (i.get("last_status") or {}).get("ss_status") == args.status
        ]
    _output(items, _fmt_batch, args.json)
    return 0


def cmd_find_batch(args, client) -> int:
    items = find_batch_job(client, args.pattern, environment=args.env)
    _output(items, _fmt_batch, args.json)
    return 0 if items else 1


def cmd_sla(args, client) -> int:
    items = sla_breaches(
        client,
        environment=args.env,
        job_type=args.type,
        job_status=args.status,
        name_pattern=args.name,
        since_hours=args.since_hours,
    )
    _output(items, _fmt_sla, args.json)
    return 0


def main(argv=None) -> int:
    load_env()
    parser = argparse.ArgumentParser(
        description="CDT Control Panel read-only CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="Output JSON")
    common.add_argument("--env", default="prod",
                        help="Environment (prod, preprod, nonprod). Default: prod")

    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health", parents=[common], help="API health check + auth verification")

    p_str = sub.add_parser("streaming", parents=[common], help="List streaming jobs")
    p_str.add_argument("--status", help="Filter by instance_status (e.g. running, failed)")

    p_fs = sub.add_parser("find-streaming", parents=[common], help="Find streaming job by name/id regex")
    p_fs.add_argument("pattern")

    p_b = sub.add_parser("batch", parents=[common], help="List batch jobs")
    p_b.add_argument("--status", help="Filter by last status (success, failed, ...)")

    p_fb = sub.add_parser("find-batch", parents=[common], help="Find batch job by job_id/file_name/airflow_id")
    p_fb.add_argument("pattern")

    p_sla = sub.add_parser("sla", parents=[common], help="SLA breaches with filters")
    p_sla.add_argument("--type", help="job_type (batch, workflow, client_feed, sql_data_loader)")
    p_sla.add_argument("--status", help="job_status (failed, not started, skipped, success)")
    p_sla.add_argument("--name", help="job_name regex")
    p_sla.add_argument("--since-hours", type=float, help="Only breaches in last N hours")

    args = parser.parse_args(argv)

    try:
        client = CDTClient()
    except SystemExit:
        raise
    except Exception as e:
        print(f"Failed to init CDT client: {e}", file=sys.stderr)
        return 1

    handlers = {
        "health": cmd_health,
        "streaming": cmd_streaming,
        "find-streaming": cmd_find_streaming,
        "batch": cmd_batch,
        "find-batch": cmd_find_batch,
        "sla": cmd_sla,
    }
    try:
        return handlers[args.cmd](args, client)
    except CDTAuthError as e:
        print(f"Auth error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
