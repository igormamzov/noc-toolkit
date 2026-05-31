#!/usr/bin/env python3
"""Scheduled recheck — wait for a job to recover, then auto-close the DRGN.

Use case:
    NOC ack-s a transient Databricks failure. The next scheduled run is
    expected to succeed in N minutes. Instead of writing a fresh check loop
    each time, schedule a recheck:

        scheduled_recheck.py schedule \\
            --drgn DRGN-17897 \\
            --pd Q2F39BSFK5L3V2 \\
            --job asra_split_trx_header_fact \\
            --fire-after-min 12 \\
            --on-success-resolution rvsp \\
            --on-success-reference DSSD-31131 \\
            --on-success-append "Igor performed manual repair at 05:11 UTC"

    Then, periodically:

        scheduled_recheck.py run-pending

    For each pending task whose fire_at has passed, this calls CDT, decides
    success/failure/still-pending, and either closes the DRGN (via the
    auto-close `close` library), notifies, or keeps the task pending.

Subcommands:
    schedule        Add a new scheduled recheck
    list            Show all tasks (filter with --status)
    show <id>       Show one task in detail
    cancel <id>     Mark a task cancelled
    run-pending     Process all pending tasks whose fire_at has passed
    install-launchd Install a launchd agent that calls run-pending every 5 min
    uninstall-launchd Remove the launchd agent

State is stored in `state.json` next to this script.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

warnings.filterwarnings("ignore", message="Unverified HTTPS request")

_SCRIPT_DIR = Path(__file__).resolve().parent
_TOOLS = _SCRIPT_DIR.parent
_COMMON = _TOOLS / "common"
_CDT = _TOOLS / "cdt"
_AUTO_CLOSE = _TOOLS / "auto-close"
for p in (_COMMON, _CDT, _AUTO_CLOSE, _SCRIPT_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from noc_utils import load_env

from cdt_client import CDTClient
from cdt_dashboards import find_batch_job

# Re-use the auto-close library (already battle-tested)
from closure import (
    close_drgn,
    SLA_NO, SLA_YES, SLA_UNKNOWN,
    RUNBOOK_UP_TO_DATE, RUNBOOK_MISSING,
    RESOLUTION_AUTO, RESOLUTION_STD_PROC,
    RESOLUTION_FALSE_POSITIVE, RESOLUTION_NOT_REQUIRED,
)

import state as state_mod  # noqa: E402

RESOLUTION_MAP = {
    "auto": (RESOLUTION_AUTO, "Resolved Automatically"),
    "rvsp": (RESOLUTION_STD_PROC, "Resolved via Standard Procedure"),
    "fp": (RESOLUTION_FALSE_POSITIVE, "False Positive"),
    "not-required": (RESOLUTION_NOT_REQUIRED, "Not Required"),
}


# ---- Time helpers -------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    s = s.strip()
    s = s.replace("Z", "+0000")
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        try:
            # tolerate fractional seconds
            return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f%z")
        except ValueError:
            return None


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- CDT recovery check -------------------------------------------------

def cdt_job_success_after(client: CDTClient, job_name: str, after_iso: str,
                           environment: str = "prod") -> tuple[str, str, Optional[Dict[str, Any]]]:
    """Check if `job_name` had a successful CDT run that started after `after_iso`.

    Returns (verdict, reason, latest_run) where verdict is one of:
        "success" — found a success run started after `after_iso`
        "failed"  — most recent run after `after_iso` is failed/cancelled
        "pending" — no run yet, or current run still running
        "error"   — CDT lookup failed

    `latest_run` is the most recent run dict, or None.
    """
    try:
        matches = find_batch_job(client, f"^{re.escape(job_name)}$", environment=environment)
    except Exception as e:
        return "error", f"CDT lookup failed: {e}", None
    if not matches:
        return "error", f"job '{job_name}' not found in CDT batch_dashboard", None

    job = matches[0]
    runs = (job.get("last_runs") or {}).get("runs") or []
    after_dt = _parse_iso(after_iso) if after_iso else None

    # Find the most recent run whose start_time is >= after_dt
    matching_runs: List[Dict[str, Any]] = []
    for r in runs:
        st = _parse_iso(r.get("start_time", ""))
        if not st or not after_dt:
            continue
        if st >= after_dt:
            matching_runs.append(r)

    if not matching_runs:
        # Nothing after the threshold yet
        latest = runs[0] if runs else None
        return "pending", f"no run after {after_iso}", latest

    # Most recent first (CDT returns newest-first already)
    latest = matching_runs[0]
    status = latest.get("status")
    if status == "success":
        return "success", f"run @ {latest.get('start_time')} succeeded", latest
    if status in ("failed", "cancelled", "canceled"):
        return "failed", f"run @ {latest.get('start_time')} {status}", latest
    # "running", "pending", anything else
    return "pending", f"run @ {latest.get('start_time')} status={status}", latest


# ---- Comment composition ------------------------------------------------

def _build_comment(task: Dict[str, Any], verdict_reason: str, latest_run: Optional[Dict[str, Any]]) -> str:
    on_success = task.get("on_success") or {}
    parts: List[str] = []

    base = on_success.get("comment")
    if base:
        parts.append(base)
    elif on_success.get("resolution") == "rvsp":
        parts.append("Manual repair was performed by NOC; subsequent run completed successfully.")
    else:
        parts.append("Job recovered without manual intervention; next run completed successfully.")

    if latest_run:
        st = latest_run.get("start_time", "?")
        et = latest_run.get("end_time", "?")
        parts.append(f"CDT run: {st} → {et} ({latest_run.get('status')}).")

    refs = on_success.get("reference") or []
    if refs:
        parts.append(f"Cross-reference: tracked under {', '.join(refs)}. No additional escalation needed.")

    append = on_success.get("append")
    if append:
        parts.append(append)

    return "\n\n".join(parts)


# ---- Subcommand: schedule -----------------------------------------------

def cmd_schedule(args) -> int:
    if args.fire_after_min is not None:
        fire_at = _now_utc() + timedelta(minutes=args.fire_after_min)
    elif args.fire_at:
        parsed = _parse_iso(args.fire_at)
        if not parsed:
            print(f"Invalid --fire-at ISO timestamp: {args.fire_at}", file=sys.stderr)
            return 1
        fire_at = parsed
    else:
        print("Must specify --fire-after-min N or --fire-at ISO", file=sys.stderr)
        return 1

    if args.after_iso:
        after_iso = args.after_iso
    elif args.after_now:
        after_iso = _iso_z(_now_utc())
    else:
        # Default: success run must start AFTER the moment scheduling happens
        after_iso = _iso_z(_now_utc())

    # Validate resolution flag
    res = args.on_success_resolution
    if res and res not in RESOLUTION_MAP:
        print(f"Invalid --on-success-resolution: {res}. Choose from {list(RESOLUTION_MAP)}", file=sys.stderr)
        return 1

    on_success: Dict[str, Any] = {
        "action": "close-drgn" if args.drgn else "notify",
        "resolution": res,
        "reference": list(args.on_success_reference or []),
        "append": args.on_success_append,
        "comment": args.on_success_comment,
        "runbook_url": args.on_success_runbook,
        "sla": args.on_success_sla,
    }
    on_failure: Dict[str, Any] = {
        "action": "notify",
        "message": args.on_failure_message or "next run still failed — escalation may be needed",
    }

    task = {
        "drgn": args.drgn,
        "pd": args.pd,
        "check_type": "cdt-job-success-after",
        "check_args": {
            "job_name": args.job,
            "after_iso": after_iso,
        },
        "fire_at_iso": _iso_z(fire_at),
        "on_success": on_success,
        "on_failure": on_failure,
    }
    saved = state_mod.add_task(task)

    print(f"Scheduled recheck {saved['id']}")
    print(f"  drgn: {saved.get('drgn')} | pd: {saved.get('pd')}")
    print(f"  job: {saved['check_args']['job_name']}")
    print(f"  fire_at: {saved['fire_at_iso']} (in {(fire_at - _now_utc()).total_seconds()/60:.1f} min)")
    print(f"  after_iso (run must start after): {saved['check_args']['after_iso']}")
    print(f"  on_success: action={on_success['action']}, resolution={res}")
    if on_success["reference"]:
        print(f"              references: {on_success['reference']}")
    return 0


# ---- Subcommand: list ---------------------------------------------------

def cmd_list(args) -> int:
    tasks = state_mod.list_tasks(status=args.status)
    if not tasks:
        print("(no tasks)")
        return 0
    print(f"{'ID':22s} {'STATUS':10s} {'FIRE_AT':21s} {'DRGN':14s} {'JOB':40s}")
    for t in tasks:
        print(f"{t.get('id',''):22s} {t.get('status',''):10s} "
              f"{t.get('fire_at_iso',''):21s} "
              f"{(t.get('drgn') or '-'):14s} "
              f"{t.get('check_args',{}).get('job_name',''):40s}")
    return 0


def cmd_show(args) -> int:
    t = state_mod.get_task(args.task_id)
    if not t:
        print(f"No such task: {args.task_id}", file=sys.stderr)
        return 1
    print(json.dumps(t, indent=2))
    return 0


def cmd_cancel(args) -> int:
    t = state_mod.get_task(args.task_id)
    if not t:
        print(f"No such task: {args.task_id}", file=sys.stderr)
        return 1
    if t.get("status") in ("done", "failed", "cancelled"):
        print(f"Task is already {t.get('status')}; nothing to cancel.")
        return 0
    state_mod.update_task(args.task_id, status="cancelled")
    print(f"Cancelled {args.task_id}")
    return 0


# ---- Subcommand: run-pending --------------------------------------------

def _fire_one(task: Dict[str, Any], client: CDTClient, dry_run: bool) -> str:
    """Execute one due task. Returns short status string for logging."""
    task_id = task["id"]
    check_type = task.get("check_type")
    if check_type != "cdt-job-success-after":
        state_mod.update_task(task_id, status="failed", last_result=f"unknown check_type: {check_type}",
                              last_attempt_at_iso=_iso_z(_now_utc()))
        return f"FAIL unknown check_type {check_type}"

    job = task["check_args"].get("job_name")
    after_iso = task["check_args"].get("after_iso")
    verdict, reason, latest = cdt_job_success_after(client, job, after_iso)

    now = _iso_z(_now_utc())
    attempts = task.get("attempts", 0) + 1

    if verdict == "success":
        on_success = task.get("on_success") or {}
        if on_success.get("action") == "close-drgn" and task.get("drgn"):
            res_flag = on_success.get("resolution")
            if not res_flag or res_flag not in RESOLUTION_MAP:
                state_mod.update_task(task_id, status="failed",
                                       last_result=f"invalid resolution flag: {res_flag}",
                                       last_attempt_at_iso=now, attempts=attempts)
                return f"FAIL invalid resolution flag {res_flag}"
            resolution_id, resolution_name = RESOLUTION_MAP[res_flag]
            sla_map = {"no": SLA_NO, "yes": SLA_YES, "unknown": SLA_UNKNOWN}
            sla = sla_map.get(on_success.get("sla", "no"), SLA_NO)
            runbook_url = on_success.get("runbook_url")
            runbook_status = RUNBOOK_UP_TO_DATE if runbook_url else RUNBOOK_MISSING
            comment = _build_comment(task, reason, latest)
            if dry_run:
                state_mod.update_task(task_id, last_attempt_at_iso=now, attempts=attempts,
                                       last_result=f"DRY-RUN would close as {resolution_name}")
                return f"DRY-RUN would close {task['drgn']} as {resolution_name}"
            try:
                code = close_drgn(
                    task["drgn"],
                    sla_violation=sla,
                    runbook_status=runbook_status,
                    runbook_link=runbook_url,
                    resolution=resolution_id,
                    comment=comment,
                )
                state_mod.update_task(task_id, status="done", last_attempt_at_iso=now,
                                       attempts=attempts,
                                       last_result=f"closed {task['drgn']} as {resolution_name} (HTTP {code})")
                return f"SUCCESS closed {task['drgn']} as {resolution_name}"
            except Exception as e:
                state_mod.update_task(task_id, last_attempt_at_iso=now, attempts=attempts,
                                       last_result=f"close failed: {e}")
                return f"ERROR close failed: {e}"
        else:
            # on_success.action == "notify" or no DRGN given
            state_mod.update_task(task_id, status="done", last_attempt_at_iso=now,
                                   attempts=attempts, last_result=f"success — {reason}")
            return f"SUCCESS notify-only — {reason}"

    if verdict == "failed":
        on_failure = task.get("on_failure") or {}
        msg = on_failure.get("message") or "run failed"
        state_mod.update_task(task_id, status="failed", last_attempt_at_iso=now,
                               attempts=attempts, last_result=f"{reason} | notify: {msg}")
        return f"FAIL {reason}"

    # pending — leave as-is, bump attempts so we can see how many times polled
    state_mod.update_task(task_id, last_attempt_at_iso=now, attempts=attempts,
                          last_result=f"still pending — {reason}")
    return f"PENDING {reason}"


def cmd_run_pending(args) -> int:
    due = state_mod.pending_due()
    if not due:
        if not args.quiet:
            print("(no pending tasks due)")
        return 0
    client = CDTClient()
    print(f"=== {len(due)} due task(s) at {_iso_z(_now_utc())} ===")
    for t in due:
        result = _fire_one(t, client, dry_run=args.dry_run)
        print(f"  [{t['id']}] {result}")
    return 0


# ---- Subcommand: install-launchd ----------------------------------------

LAUNCHD_LABEL = "com.master.noc-scheduled-recheck"

def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def cmd_install_launchd(args) -> int:
    """Generate and load a launchd agent that calls run-pending every N minutes."""
    if sys.platform != "darwin":
        print("install-launchd is for macOS only.", file=sys.stderr)
        return 1
    py = sys.executable
    script = str(_SCRIPT_DIR / "scheduled_recheck.py")
    plist_path = _launchd_plist_path()
    interval_sec = max(60, args.interval_min * 60)
    log_dir = _SCRIPT_DIR / "launchd-logs"
    log_dir.mkdir(exist_ok=True)
    stdout = str(log_dir / "stdout.log")
    stderr = str(log_dir / "stderr.log")

    # Read .env so launchd-spawned process has the same secrets
    env_path = _TOOLS.parent / ".env"
    env_lines: List[str] = []
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if not line.strip() or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env_lines.append(f"      <key>{k.strip()}</key>\n      <string>{v.strip()}</string>")
    env_block = "\n".join(env_lines) if env_lines else ""

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{py}</string>
    <string>{script}</string>
    <string>run-pending</string>
    <string>--quiet</string>
  </array>
  <key>StartInterval</key><integer>{interval_sec}</integer>
  <key>RunAtLoad</key><false/>
  <key>StandardOutPath</key><string>{stdout}</string>
  <key>StandardErrorPath</key><string>{stderr}</string>
  <key>EnvironmentVariables</key>
  <dict>
{env_block}
  </dict>
</dict>
</plist>
"""
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(plist)
    print(f"Wrote {plist_path}")
    # Reload
    os.system(f"launchctl unload {plist_path} 2>/dev/null")
    rc = os.system(f"launchctl load -w {plist_path}")
    if rc == 0:
        print(f"launchd agent loaded (interval {interval_sec}s)")
        print(f"  logs: {log_dir}")
    else:
        print(f"launchctl load returned non-zero: {rc}", file=sys.stderr)
        return 1
    return 0


def cmd_uninstall_launchd(args) -> int:
    if sys.platform != "darwin":
        print("uninstall-launchd is for macOS only.", file=sys.stderr)
        return 1
    plist_path = _launchd_plist_path()
    if plist_path.exists():
        os.system(f"launchctl unload {plist_path} 2>/dev/null")
        plist_path.unlink()
        print(f"Removed {plist_path}")
    else:
        print("No plist found.")
    return 0


# ---- Interactive menu (for noc-toolkit launcher) ------------------------

def _interactive_menu() -> List[str]:
    print()
    print("Scheduled Recheck — auto-close DRGN when next run succeeds")
    print("=" * 56)
    print("  1. List all tasks")
    print("  2. List pending tasks only")
    print("  3. Run pending now (process due tasks)")
    print("  4. Run pending now (dry-run)")
    print("  5. Schedule a new task (interactive prompts)")
    print("  6. Show task details")
    print("  7. Cancel a task")
    print("  8. Install launchd agent (Mac, every 5 min)")
    print("  9. Uninstall launchd agent")
    print("  0. Back")
    print("=" * 56)
    while True:
        choice = input("Select [0-9]: ").strip()
        if choice == "0":
            return []
        if choice == "1":
            return ["list"]
        if choice == "2":
            return ["list", "--status", "pending"]
        if choice == "3":
            return ["run-pending"]
        if choice == "4":
            return ["run-pending", "--dry-run"]
        if choice == "6":
            tid = input("Task ID: ").strip()
            return ["show", tid] if tid else []
        if choice == "7":
            tid = input("Task ID to cancel: ").strip()
            return ["cancel", tid] if tid else []
        if choice == "8":
            return ["install-launchd"]
        if choice == "9":
            return ["uninstall-launchd"]
        if choice == "5":
            drgn = input("DRGN key (e.g. DRGN-17897): ").strip()
            pd = input("PD ID (optional): ").strip()
            job = input("CDT job name (e.g. asra_split_trx_header_fact): ").strip()
            mins = input("Fire after how many minutes? (e.g. 30): ").strip() or "30"
            res = input("On success resolution [auto/rvsp/fp/not-required]: ").strip() or "auto"
            ref = input("Cross-reference key (e.g. DSSD-31131, blank to skip): ").strip()
            append = input("Append note (blank to skip): ").strip()
            argv = ["schedule",
                    "--drgn", drgn,
                    "--job", job,
                    "--fire-after-min", mins,
                    "--on-success-resolution", res]
            if pd: argv += ["--pd", pd]
            if ref: argv += ["--on-success-reference", ref]
            if append: argv += ["--on-success-append", append]
            return argv
        print("  invalid choice")


# ---- main ---------------------------------------------------------------

def main(argv=None) -> int:
    load_env()
    parser = argparse.ArgumentParser(
        description="Schedule a recheck of a CDT job and auto-close DRGN on success",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="cmd", required=False)

    # schedule
    p_sched = sub.add_parser("schedule", help="Add a new scheduled recheck")
    p_sched.add_argument("--drgn", required=True, help="DRGN ticket to close on success (e.g. DRGN-17897)")
    p_sched.add_argument("--pd", help="PD incident ID (optional, for traceability)")
    p_sched.add_argument("--job", required=True, help="CDT batch job name to check")
    when = p_sched.add_mutually_exclusive_group(required=True)
    when.add_argument("--fire-after-min", type=int, help="Fire N minutes from now")
    when.add_argument("--fire-at", help="Fire at ISO timestamp (e.g. 2026-05-31T05:58:00Z)")
    after = p_sched.add_mutually_exclusive_group()
    after.add_argument("--after-iso", help="Success run must START after this ISO timestamp (default: now)")
    after.add_argument("--after-now", action="store_true", help="Use now as the after threshold (default)")
    p_sched.add_argument("--on-success-resolution",
                          choices=list(RESOLUTION_MAP.keys()),
                          default="auto",
                          help="Resolution to use when auto-closing on success (default: auto)")
    p_sched.add_argument("--on-success-reference", action="append",
                          help="Cross-reference key (e.g. DSSD-31131). Repeatable.")
    p_sched.add_argument("--on-success-append", help="Free-form text appended to comment")
    p_sched.add_argument("--on-success-comment", help="Custom base comment (overrides default)")
    p_sched.add_argument("--on-success-runbook", help="Runbook URL — sets Runbook Status to Up-to-date")
    p_sched.add_argument("--on-success-sla", choices=["no", "yes", "unknown"], default="no",
                          help="SLA Violation field (default: no)")
    p_sched.add_argument("--on-failure-message", help="Message logged when run fails")

    # list
    p_list = sub.add_parser("list", help="Show all tasks")
    p_list.add_argument("--status", choices=["pending", "done", "failed", "cancelled"])

    # show
    p_show = sub.add_parser("show", help="Show one task in JSON")
    p_show.add_argument("task_id")

    # cancel
    p_cancel = sub.add_parser("cancel", help="Mark a task as cancelled")
    p_cancel.add_argument("task_id")

    # run-pending
    p_run = sub.add_parser("run-pending", help="Process due pending tasks")
    p_run.add_argument("--dry-run", action="store_true", help="Plan only, no Jira writes")
    p_run.add_argument("--quiet", action="store_true", help="Suppress 'no pending tasks' line (cron-friendly)")

    # install-launchd
    p_inst = sub.add_parser("install-launchd", help="Install a launchd agent that runs run-pending periodically")
    p_inst.add_argument("--interval-min", type=int, default=5, help="How often to run-pending (default: 5)")
    sub.add_parser("uninstall-launchd", help="Remove the launchd agent")

    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        argv = _interactive_menu()
        if not argv:
            return 0

    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 1

    if args.cmd == "schedule":
        return cmd_schedule(args)
    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "show":
        return cmd_show(args)
    if args.cmd == "cancel":
        return cmd_cancel(args)
    if args.cmd == "run-pending":
        return cmd_run_pending(args)
    if args.cmd == "install-launchd":
        return cmd_install_launchd(args)
    if args.cmd == "uninstall-launchd":
        return cmd_uninstall_launchd(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
