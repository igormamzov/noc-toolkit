#!/usr/bin/env python3
"""Auto-close DRGN tickets for transient Databricks failures.

Usage:
    auto_close.py scan                          # find candidates, interactive
    auto_close.py scan --auto                   # close all without prompt
    auto_close.py scan --dry-run                # show only
    auto_close.py check <PD_URL_or_ID>          # single incident
                                                # offers to add to whitelist
                                                # if job_name not yet allowed
    auto_close.py close <PD_URL_or_ID> [flags]  # close one with explicit flags
                                                # bypasses whitelist if --force

Closure conditions for `scan` and `check` (all required):
    1. job_name matches whitelist pattern
    2. CDT batch_dashboard shows last_runs[0].status == 'success'
    3. that success run started after the PD incident.created_at

Resolution selection (auto-detect):
    - 'repaired' in title or notes  → Resolved via Standard Procedure
    - otherwise                     → Resolved Automatically

SLA Violation:
    - high_frequency_jobs match     → No (auto)
    - otherwise                     → prompt in interactive mode

The `close` subcommand:
    Closes a single PD/DRGN with explicit flags. Useful for scripting and for
    chronic-issue closures where you want to bypass whitelist + add a DSSD
    cross-reference. Examples:

        # explicit Auto with DSSD reference appended to comment
        auto_close.py close Q1NKRGE1Y1K5EL --auto --reference DSSD-31131

        # explicit RvSP (manual repair was performed)
        auto_close.py close Q0PC40933YQ7YT --rvsp --comment "Igor manual repair"

        # False Positive (paused DAG / stale alert)
        auto_close.py close DRGN-17896 --fp --comment "DAG paused"

        # auto-detect resolution + append a custom note
        auto_close.py close Q3EUAXVKFWS1QK --auto-detect --append "tracked in DSSD-31259"
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
from typing import List, Optional

warnings.filterwarnings("ignore", message="Unverified HTTPS request")

# tools/common is added to sys.path by noc-toolkit launcher; for direct
# `python auto_close.py ...` invocation, add it ourselves.
_SCRIPT_DIR = Path(__file__).resolve().parent
_COMMON = _SCRIPT_DIR.parent / "common"
_CDT = _SCRIPT_DIR.parent / "cdt"
for p in (_COMMON, _CDT, _SCRIPT_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from noc_utils import load_env

from cdt_client import CDTClient
from cdt_dashboards import find_batch_job

from pd_helpers import (
    IncidentSummary, summarize_incident, parse_pd_id,
    pd_list_acknowledged, parse_iso_naive,
)
from closure import (
    close_drgn, get_drgn_status,
    SLA_NO, SLA_YES, SLA_UNKNOWN,
    RUNBOOK_UP_TO_DATE, RUNBOOK_MISSING,
    RESOLUTION_AUTO, RESOLUTION_STD_PROC,
    RESOLUTION_FALSE_POSITIVE, RESOLUTION_NOT_REQUIRED,
)

# Map of CLI resolution flag → (resolution_id, human_name)
RESOLUTION_MAP = {
    "auto": (RESOLUTION_AUTO, "Resolved Automatically"),
    "rvsp": (RESOLUTION_STD_PROC, "Resolved via Standard Procedure"),
    "fp": (RESOLUTION_FALSE_POSITIVE, "False Positive"),
    "not-required": (RESOLUTION_NOT_REQUIRED, "Not Required"),
}

WHITELIST_PATH = _SCRIPT_DIR / "whitelist.json"

# CDS-OPS-24x7 service ID
CDS_OPS_SERVICE_ID = None  # filter at the title level instead


# ---- Whitelist ----------------------------------------------------------

def load_whitelist() -> dict:
    return json.loads(WHITELIST_PATH.read_text())


def save_whitelist(wl: dict) -> None:
    WHITELIST_PATH.write_text(json.dumps(wl, indent=2) + "\n")


def matches_any(name: str, patterns: List[str]) -> bool:
    return any(re.search(p, name) for p in patterns)


# ---- Recovery check (CDT) ----------------------------------------------

def has_recovered(client: CDTClient, job_name: str, incident_created_at: str,
                  environment: str = "prod") -> tuple[bool, Optional[dict], str]:
    """Return (recovered, last_run_dict_or_None, reason_string)."""
    matches = find_batch_job(client, f"^{re.escape(job_name)}$", environment=environment)
    if not matches:
        return False, None, f"job '{job_name}' not found in CDT batch_dashboard"
    job = matches[0]
    runs = (job.get("last_runs") or {}).get("runs") or []
    if not runs:
        return False, None, "no recent runs in CDT"
    latest = runs[0]
    if latest.get("status") != "success":
        return False, latest, f"latest run status is {latest.get('status')!r}"
    inc_time = parse_iso_naive(incident_created_at)
    run_start = parse_iso_naive(latest.get("start_time"))
    if not inc_time or not run_start:
        return False, latest, "could not parse timestamps"
    if run_start < inc_time:
        return False, latest, (
            f"latest success run started at {run_start.isoformat()} which is "
            f"before incident created at {inc_time.isoformat()}"
        )
    return True, latest, "OK"


# ---- Closure logic ------------------------------------------------------

def pick_resolution(summary: IncidentSummary) -> str:
    return RESOLUTION_STD_PROC if summary.is_repaired else RESOLUTION_AUTO


def pick_runbook_status(summary: IncidentSummary) -> str:
    return RUNBOOK_UP_TO_DATE if summary.runbook else RUNBOOK_MISSING


def pick_sla(summary: IncidentSummary, wl: dict, *, interactive: bool) -> Optional[str]:
    """Pick SLA Violation field.

    high_frequency_jobs → SLA No (auto, no prompt)
    interactive         → ask user
    auto mode           → default to No
    """
    if summary.job_name and matches_any(summary.job_name, wl.get("high_frequency_jobs", [])):
        return SLA_NO
    if not interactive:
        return SLA_NO
    while True:
        ans = input(f"  SLA Violation for {summary.job_name}? [n=No, y=Yes, u=Unknown, s=skip]: ").strip().lower()
        if ans in ("n", ""): return SLA_NO
        if ans == "y": return SLA_YES
        if ans == "u": return SLA_UNKNOWN
        if ans == "s": return None
        print("    invalid choice")


# ---- Candidate building -------------------------------------------------

def build_candidate(client: CDTClient, summary: IncidentSummary, wl: dict) -> dict:
    """Return a dict with candidate metadata + pre-computed verdict."""
    out = {
        "summary": summary,
        "in_whitelist": False,
        "recovered": False,
        "reason": None,
        "latest_run": None,
    }
    # Already escalated to DSSD — handled by a different tool (ticket-watch).
    # We never auto-close these, even if the underlying job has recovered.
    if summary.dssd_key:
        out["reason"] = f"already escalated to {summary.dssd_key} — skip (handled by ticket-watch)"
        return out
    if not summary.job_name:
        out["reason"] = "could not parse job_name from PD title"
        return out
    out["in_whitelist"] = matches_any(summary.job_name, wl.get("patterns", []))
    if not out["in_whitelist"]:
        out["reason"] = f"job '{summary.job_name}' not in whitelist"
        # still check recovery so user can decide whether to add
    recovered, latest, reason = has_recovered(client, summary.job_name, summary.created_at)
    out["recovered"] = recovered
    out["latest_run"] = latest
    if not recovered:
        out["reason"] = out["reason"] or reason
    elif out["in_whitelist"]:
        out["reason"] = "ELIGIBLE"
    return out


# ---- Output -------------------------------------------------------------

def print_candidate(c: dict) -> None:
    s: IncidentSummary = c["summary"]
    eligible = c["in_whitelist"] and c["recovered"]
    flag = "✓" if eligible else "✗"
    latest = c.get("latest_run") or {}
    print(f"  {flag} PD#{s.pd_number} {s.priority} [{s.status}]  {s.title[:80]}")
    print(f"      job={s.job_name}  drgn={s.drgn_key}  whitelisted={c['in_whitelist']}")
    print(f"      latest_run: {latest.get('status','?')} @ {latest.get('end_time','?')}")
    if c.get("reason") and c["reason"] != "ELIGIBLE":
        print(f"      reason: {c['reason']}")


def do_close(c: dict, wl: dict, *, interactive: bool, dry_run: bool) -> bool:
    """Close one candidate. Returns True if closed."""
    s: IncidentSummary = c["summary"]
    if not s.drgn_key:
        print(f"      SKIP: no DRGN key in PD notes")
        return False
    sla = pick_sla(s, wl, interactive=interactive)
    if sla is None:
        print(f"      SKIP: user chose to skip")
        return False
    runbook_status = pick_runbook_status(s)
    resolution = pick_resolution(s)
    res_name = "Resolved via Standard Procedure" if resolution == RESOLUTION_STD_PROC else "Resolved Automatically"
    if dry_run:
        print(f"      DRY-RUN: would close {s.drgn_key} as {res_name} (SLA={sla}, runbook={runbook_status})")
        return False
    code = close_drgn(
        s.drgn_key,
        sla_violation=sla,
        runbook_status=runbook_status,
        runbook_link=s.runbook,
        resolution=resolution,
    )
    print(f"      ✓ closed {s.drgn_key} (HTTP {code}, {res_name})")
    return True


# ---- Subcommands --------------------------------------------------------

def cmd_scan(args, client) -> int:
    wl = load_whitelist()
    print("Fetching acknowledged PD incidents...")
    incidents = pd_list_acknowledged()
    # Filter to Databricks batch failure pattern by title
    candidates = []
    for inc in incidents:
        title = inc.get("title", "")
        if "Databricks batch job" not in title or "failed" not in title:
            continue
        try:
            summary = summarize_incident(inc["id"])
        except Exception as e:
            print(f"  ! skip {inc['id']}: {e}")
            continue
        candidates.append(build_candidate(client, summary, wl))

    if not candidates:
        print("No matching incidents.")
        return 0

    eligible = [c for c in candidates if c["in_whitelist"] and c["recovered"]]
    other = [c for c in candidates if not (c["in_whitelist"] and c["recovered"])]

    print(f"\n=== Eligible for auto-close ({len(eligible)}) ===")
    for c in eligible:
        print_candidate(c)
    print(f"\n=== Skipped ({len(other)}) ===")
    for c in other:
        print_candidate(c)

    if args.dry_run:
        print("\n(dry-run, no changes made)")
        return 0
    if not eligible:
        return 0

    interactive = not args.auto
    closed = 0
    if interactive:
        print("\nInteractive mode. [a]ll/[y]es/[n]o/[q]uit per item")
        bulk = None
        for c in eligible:
            print_candidate(c)
            if bulk == "a":
                ans = "y"
            else:
                ans = input("    Close? [y/n/a=all/q=quit]: ").strip().lower() or "n"
            if ans == "q":
                break
            if ans == "a":
                bulk = "a"
                ans = "y"
            if ans == "y":
                if do_close(c, wl, interactive=True, dry_run=args.dry_run):
                    closed += 1
    else:
        for c in eligible:
            print_candidate(c)
            if do_close(c, wl, interactive=False, dry_run=args.dry_run):
                closed += 1

    print(f"\nClosed {closed}/{len(eligible)}")
    return 0


def build_comment(
    *,
    base: Optional[str] = None,
    summary: Optional[IncidentSummary] = None,
    references: Optional[List[str]] = None,
    append: Optional[str] = None,
    cdt_run_info: Optional[str] = None,
) -> str:
    """Compose a closure comment from optional parts.

    base: the main one-liner (default depends on context)
    summary: PD summary so we can mention "Igor performed manual repair" etc.
    references: list of DSSD/COREDATA/FCR keys to cross-reference
    append: free-form text appended at the end
    cdt_run_info: e.g. "Next scheduled run at 05:47 UTC completed successfully (5.5m, success)"
    """
    parts: List[str] = []
    if base:
        parts.append(base)
    elif summary and summary.is_repaired:
        parts.append("Manual repair was performed by NOC; subsequent run completed successfully.")
    elif summary:
        parts.append("Job recovered without manual intervention; next run completed successfully.")
    else:
        parts.append("Closing per NOC standard procedure.")

    if cdt_run_info:
        parts.append(cdt_run_info)

    if references:
        refs = ", ".join(references)
        parts.append(f"Cross-reference: tracked under {refs}. No additional escalation needed.")

    if append:
        parts.append(append)

    return "\n\n".join(parts)


def resolve_drgn_key(arg: str) -> Optional[str]:
    """Accept `DRGN-NNNN`, full Jira URL, or PD ID/URL.

    For PD inputs: looks up the linked DRGN via PD notes.
    Returns the DRGN key, or None if it can't be determined.
    """
    arg = arg.strip()
    # Direct DRGN-NNNN
    m = re.search(r"\b(DRGN-\d+)\b", arg)
    if m:
        return m.group(1)
    # Try as PD ID/URL
    pd_id = parse_pd_id(arg)
    if pd_id:
        try:
            summary = summarize_incident(pd_id)
            return summary.drgn_key
        except Exception:
            return None
    return None


def cmd_close(args, client) -> int:
    """Close a single DRGN with explicit flags.

    Accepts a PD ID/URL OR a DRGN key. With a PD ID, looks up the DRGN from PD
    notes and pulls IncidentSummary for auto-detection of resolution / SLA /
    runbook. With a bare DRGN, only closes (no auto-detection — flags must
    specify resolution).
    """
    target = args.target.strip()

    # Determine input type
    pd_id = parse_pd_id(target)
    drgn_key = None
    summary: Optional[IncidentSummary] = None

    if pd_id:
        try:
            summary = summarize_incident(pd_id)
        except Exception as e:
            print(f"Failed to fetch PD#{pd_id}: {e}", file=sys.stderr)
            return 1
        drgn_key = summary.drgn_key
        if not drgn_key:
            print(f"PD#{summary.pd_number} has no DRGN reference in notes — cannot close", file=sys.stderr)
            return 1
        print(f"PD#{summary.pd_number} [{summary.status}] → {drgn_key}")
        print(f"  title: {summary.title[:80]}")
        print(f"  job_name: {summary.job_name or '(none)'}")
        if summary.dssd_key:
            print(f"  dssd_key: {summary.dssd_key} (already escalated)")
    else:
        m = re.search(r"\b(DRGN-\d+)\b", target)
        if m:
            drgn_key = m.group(1)
            print(f"Direct DRGN target: {drgn_key} (no PD lookup; flags must drive closure)")
        else:
            print(f"Could not parse target (need PD ID/URL or DRGN-NNNN): {target}", file=sys.stderr)
            return 1

    # Determine resolution
    explicit_flags = [k for k in ("auto", "rvsp", "fp", "not_required") if getattr(args, k, False)]
    if len(explicit_flags) > 1:
        print(f"Conflicting resolution flags: {explicit_flags}", file=sys.stderr)
        return 1
    if explicit_flags:
        flag = explicit_flags[0].replace("_", "-")
        resolution_id, resolution_name = RESOLUTION_MAP[flag]
    elif args.auto_detect:
        if not summary:
            print("--auto-detect requires PD input (so we can read title/notes)", file=sys.stderr)
            return 1
        resolution_id = pick_resolution(summary)
        resolution_name = "Resolved via Standard Procedure" if resolution_id == RESOLUTION_STD_PROC else "Resolved Automatically"
        print(f"  auto-detected resolution: {resolution_name}")
    else:
        print("Must specify one of: --auto, --rvsp, --fp, --not-required, or --auto-detect", file=sys.stderr)
        return 1

    # Determine SLA Violation
    sla_map = {"no": SLA_NO, "yes": SLA_YES, "unknown": SLA_UNKNOWN}
    sla = sla_map.get(args.sla, SLA_NO)

    # Determine Runbook Status + URL
    if args.runbook_url:
        runbook_link = args.runbook_url
        runbook_status = RUNBOOK_UP_TO_DATE
    elif summary and summary.runbook:
        runbook_link = summary.runbook
        runbook_status = RUNBOOK_UP_TO_DATE
    else:
        runbook_link = None
        runbook_status = RUNBOOK_MISSING

    # Build comment
    references = list(args.reference or [])
    comment = build_comment(
        base=args.comment,
        summary=summary,
        references=references,
        append=args.append,
    )

    print(f"  resolution: {resolution_name}")
    print(f"  sla: {args.sla} | runbook_status: {'Up-to-date' if runbook_status == RUNBOOK_UP_TO_DATE else 'Missing'}")
    if references:
        print(f"  references: {', '.join(references)}")
    print(f"  comment ({len(comment)} chars): {comment[:200]}{'...' if len(comment) > 200 else ''}")

    if args.dry_run:
        print(f"\n(dry-run, no changes — would close {drgn_key} as {resolution_name})")
        return 0

    if not args.yes:
        ans = input(f"\nClose {drgn_key} as {resolution_name}? [y/N]: ").strip().lower()
        if ans != "y":
            print("Cancelled.")
            return 0

    code = close_drgn(
        drgn_key,
        sla_violation=sla,
        runbook_status=runbook_status,
        runbook_link=runbook_link,
        resolution=resolution_id,
        comment=comment,
    )
    print(f"\n✓ closed {drgn_key} (HTTP {code}, {resolution_name})")

    # Verify
    st = get_drgn_status(drgn_key)
    print(f"  verify: status={st['status']['name']}, resolution={(st.get('resolution') or {}).get('name')}")
    return 0


def cmd_check(args, client) -> int:
    wl = load_whitelist()
    pd_id = parse_pd_id(args.incident)
    if not pd_id:
        print(f"Could not parse PD ID/URL from: {args.incident}", file=sys.stderr)
        return 1

    summary = summarize_incident(pd_id)
    print(f"PD#{summary.pd_number} [{summary.status}] {summary.priority}")
    print(f"  title: {summary.title}")
    print(f"  job_name: {summary.job_name}")
    print(f"  drgn_key: {summary.drgn_key}")
    print(f"  dssd_key: {summary.dssd_key or '(none)'}")
    print(f"  is_repaired: {summary.is_repaired}")
    print(f"  runbook: {summary.runbook or '(missing)'}")

    if summary.dssd_key:
        print(f"✗ Already escalated to {summary.dssd_key}. "
              f"Auto-close skips escalated incidents — use ticket-watch tool.")
        return 1
    if not summary.job_name:
        print("✗ Cannot proceed: failed to extract job name from title")
        return 1
    if not summary.drgn_key:
        print("✗ Cannot proceed: no DRGN key found in PD notes")
        return 1

    c = build_candidate(client, summary, wl)
    print(f"  in_whitelist: {c['in_whitelist']}")
    latest = c.get("latest_run") or {}
    print(f"  latest_run: {latest.get('status')} @ {latest.get('start_time')} → {latest.get('end_time')}")
    if not c["recovered"]:
        print(f"✗ Not eligible: {c['reason']}")
        return 1

    if not c["in_whitelist"]:
        # Offer to add
        suggested = f"^{re.escape(summary.job_name)}$"
        print(f"\nJob '{summary.job_name}' is NOT in whitelist but has recovered.")
        print(f"Suggested pattern to add: {suggested}")
        if args.auto or args.dry_run:
            print("(skipping whitelist update — use interactive mode to add)")
            return 1
        ans = input("Add to whitelist? [y/N]: ").strip().lower()
        if ans == "y":
            wl["patterns"].append(suggested)
            save_whitelist(wl)
            print(f"  ✓ added {suggested} to whitelist.json")
        else:
            print("Skipped.")
            return 1

    # Now eligible — close it
    do_close(c, wl, interactive=not args.auto, dry_run=args.dry_run)
    if not args.dry_run and summary.drgn_key:
        st = get_drgn_status(summary.drgn_key)
        print(f"  verify: status={st['status']['name']}, resolution={(st.get('resolution') or {}).get('name')}")
    return 0


def _interactive_menu() -> List[str]:
    """When launched without arguments (via noc-toolkit launcher), show a menu."""
    print()
    print("Auto-Close — Transient Databricks Failure Closure")
    print("=" * 56)
    print("  1. Scan acknowledged PD incidents (interactive)")
    print("  2. Scan acknowledged PD incidents (dry-run)")
    print("  3. Scan acknowledged PD incidents (auto, no prompts)")
    print("  4. Check a single PD incident by URL or ID")
    print("  5. Check a single PD incident (dry-run)")
    print("  6. Close one DRGN/PD with explicit flags")
    print("  0. Back")
    print("=" * 56)
    while True:
        choice = input("Select [0-6]: ").strip()
        if choice == "0":
            return []
        if choice == "1":
            return ["scan"]
        if choice == "2":
            return ["scan", "--dry-run"]
        if choice == "3":
            return ["scan", "--auto"]
        if choice in ("4", "5"):
            inc = input("PD URL or ID: ").strip()
            if not inc:
                print("  cancelled")
                return []
            args = ["check", inc]
            if choice == "5":
                args.append("--dry-run")
            return args
        if choice == "6":
            target = input("PD URL/ID or DRGN-NNNN: ").strip()
            if not target:
                print("  cancelled")
                return []
            print("  Resolution? [a]uto / [r]vsp (manual repair) / [f]p / [n]ot-required / [d]etect: ", end="")
            res_choice = input().strip().lower() or "d"
            res_flag_map = {
                "a": "--auto",
                "auto": "--auto",
                "r": "--rvsp",
                "rvsp": "--rvsp",
                "f": "--fp",
                "fp": "--fp",
                "n": "--not-required",
                "d": "--auto-detect",
                "detect": "--auto-detect",
                "": "--auto-detect",
            }
            res_flag = res_flag_map.get(res_choice)
            if not res_flag:
                print("  invalid choice")
                continue
            ref = input("  Cross-reference key (e.g. DSSD-31131, blank to skip): ").strip()
            extra: List[str] = []
            if ref:
                extra += ["--reference", ref]
            note = input("  Append note (blank to skip): ").strip()
            if note:
                extra += ["--append", note]
            return ["close", target, res_flag] + extra
        print("  invalid choice")


def main(argv=None) -> int:
    load_env()
    parser = argparse.ArgumentParser(
        description="Auto-close DRGN tickets for transient Databricks failures",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--auto", action="store_true",
                        help="Skip prompts (suitable for cron)")
    common.add_argument("--dry-run", action="store_true",
                        help="Show plan but make no changes")

    sub = parser.add_subparsers(dest="cmd", required=False)
    sub.add_parser("scan", parents=[common],
                   help="Scan all acknowledged PD incidents")
    p_check = sub.add_parser("check", parents=[common],
                              help="Check single PD incident (URL or ID)")
    p_check.add_argument("incident")

    # `close` — explicit single-ticket closure with flags
    p_close = sub.add_parser(
        "close",
        help="Close one DRGN/PD with explicit flags (bypasses whitelist)",
        description=(
            "Close a single DRGN ticket with explicit flags. Target can be:\n"
            "  - a PagerDuty incident ID or URL (DRGN is looked up from PD notes)\n"
            "  - a bare DRGN-NNNN key\n\n"
            "One of the resolution flags must be given:\n"
            "  --auto / --rvsp / --fp / --not-required / --auto-detect"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_close.add_argument("target", help="PD ID/URL or DRGN-NNNN")
    res_group = p_close.add_mutually_exclusive_group()
    res_group.add_argument("--auto", action="store_true",
                            help="Resolved Automatically (no manual intervention)")
    res_group.add_argument("--rvsp", action="store_true",
                            help="Resolved via Standard Procedure (manual repair was performed)")
    res_group.add_argument("--fp", action="store_true",
                            help="False Positive")
    res_group.add_argument("--not-required", dest="not_required", action="store_true",
                            help="Not Required")
    res_group.add_argument("--auto-detect", action="store_true",
                            help="Pick resolution from PD title/notes (Auto vs RvSP)")
    p_close.add_argument("--reference", action="append", metavar="KEY",
                          help="Cross-reference key to mention in comment (e.g. DSSD-31131). "
                               "Repeatable.")
    p_close.add_argument("--comment", help="Custom base comment (overrides default)")
    p_close.add_argument("--append", help="Free-form text appended at the end of the comment")
    p_close.add_argument("--sla", choices=["no", "yes", "unknown"], default="no",
                          help="SLA Violation field (default: no)")
    p_close.add_argument("--runbook-url",
                          help="Runbook URL to set (Runbook Status becomes Up-to-date)")
    p_close.add_argument("--dry-run", action="store_true",
                          help="Show plan but do not close")
    p_close.add_argument("--yes", action="store_true",
                          help="Skip confirmation prompt")

    # Default to argv from sys.argv (or explicit caller argv).
    # When invoked via noc-toolkit launcher, sys.argv == [script_path] only,
    # so no subcommand is given — fall back to interactive menu.
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

    client = CDTClient()
    if args.cmd == "scan":
        return cmd_scan(args, client)
    if args.cmd == "check":
        return cmd_check(args, client)
    if args.cmd == "close":
        return cmd_close(args, client)
    return 1


if __name__ == "__main__":
    sys.exit(main())
