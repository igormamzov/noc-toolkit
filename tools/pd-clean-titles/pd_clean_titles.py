#!/usr/bin/env python3
"""
pd-clean-titles — replace 📤/🔗 emojis in PD incident titles with " - ".

Background: when a Jira-Server integration links a DSSD/DRGN to a PD
incident, the integration prepends the title with `<DSSD-NNNNN> 📤 ` /
`<DRGN-NNNNN> 🔗 ` markers. The emojis don't render well in some
terminals/Slack clients and break grep-by-substring workflows. This tool
scans the user's open/acknowledged incidents and rewrites those two
specific emojis to a plain " - " separator.

Only two emojis are touched (📤 outbox tray, 🔗 link). Anything else in
the title is preserved verbatim.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

warnings.filterwarnings("ignore", message="Unverified HTTPS request")

# tools/common is added to sys.path by the launcher; for direct invocation
# we add it ourselves.
_SCRIPT_DIR = Path(__file__).resolve().parent
_COMMON = _SCRIPT_DIR.parent / "common"
for _path in (_COMMON, _SCRIPT_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from noc_utils import load_env, require_env, new_pd_client  # noqa: E402

try:
    import pagerduty
except ImportError as _import_error:
    print(f"Error: missing PagerDuty dependency: {_import_error}", file=sys.stderr)
    sys.exit(1)


VERSION = "0.1.0"

# The exact two emoji codepoints we replace. Keeping them named so the
# regex stays readable and any future "also strip 🚀" addition is one
# line away from this list.
EMOJI_OUTBOX_TRAY = "\U0001F4E4"  # 📤 — added by Jira integration on DSSD link
EMOJI_LINK = "\U0001F517"         # 🔗 — added by Jira integration on DRGN link

# Match either emoji, optionally surrounded by spaces. We capture the
# surrounding whitespace so we can collapse "DSSD-X 📤 [ERROR]" → "DSSD-X - [ERROR]"
# in one shot instead of leaving "DSSD-X  -  [ERROR]" with double spaces.
EMOJI_REPLACE_RE = re.compile(
    rf"\s*[{EMOJI_OUTBOX_TRAY}{EMOJI_LINK}]\s*"
)
EMOJI_DETECT_RE = re.compile(
    rf"[{EMOJI_OUTBOX_TRAY}{EMOJI_LINK}]"
)


# ---------------------------------------------------------------------------
# Title rewriting
# ---------------------------------------------------------------------------


def clean_title(original_title: str) -> str:
    """
    Apply the emoji-to-dash replacement, returning the new title.

    Idempotent: a title without the target emojis is returned unchanged.
    Trailing newlines (PD likes to append "\\n" sometimes) are preserved
    so we don't accidentally trigger spurious "title changed" diffs after
    a previous tool run.
    """
    if not original_title or not EMOJI_DETECT_RE.search(original_title):
        return original_title
    return EMOJI_REPLACE_RE.sub(" - ", original_title)


def title_diff(old_title: str, new_title: str) -> Tuple[str, str]:
    """
    Return a side-by-side preview pair (old_repr, new_repr) — newlines
    rendered as visible characters so the table doesn't blow up.
    """
    return (
        old_title.replace("\n", "\\n"),
        new_title.replace("\n", "\\n"),
    )


# ---------------------------------------------------------------------------
# PagerDuty I/O
# ---------------------------------------------------------------------------


def fetch_user_open_incidents(
    pagerduty_session: Any,
    user_id: str,
) -> List[Dict[str, Any]]:
    """
    Pull every open/acknowledged incident currently assigned to user_id.

    Uses list_all so we automatically follow PD's pagination — there are
    rarely more than ~50 acked at once, but a backlog day can spike.
    """
    return list(pagerduty_session.list_all(
        "incidents",
        params={
            "statuses[]": ["triggered", "acknowledged"],
            "user_ids[]": [user_id],
        },
    ))


def update_incident_title(
    pagerduty_session: Any,
    incident_id: str,
    new_title: str,
    from_email: str,
) -> None:
    """
    PUT /incidents/{id} with a title-only payload.

    The PD API requires the `type: incident` discriminator and a `From`
    header; using the existing pd-monitor convention keeps audit logs
    consistent ("Igor changed title").
    """
    pagerduty_session.rput(
        f"incidents/{incident_id}",
        json={"incident": {"type": "incident", "title": new_title}},
        headers={"From": from_email},
    )


def get_current_user(pagerduty_session: Any) -> Tuple[str, str]:
    """Resolve (user_id, user_email) from the API token."""
    response = pagerduty_session.rget("users/me")
    user_record = (
        response.get("user", response)
        if isinstance(response, dict) else response
    )
    return user_record["id"], user_record.get("email", "")


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def format_diff_table(
    rewrites: List[Tuple[str, str, str]],
) -> str:
    """
    Render the planned rewrites as an ASCII block.

    Each entry is (incident_id, old_title, new_title). Output gives the
    user enough context to spot a malformed title before pressing 'y'.
    """
    if not rewrites:
        return "  (no incidents need cleaning)"

    lines: List[str] = []
    for incident_id, old_title, new_title in rewrites:
        old_repr, new_repr = title_diff(old_title, new_title)
        lines.append(f"  {incident_id}")
        lines.append(f"    -  {old_repr}")
        lines.append(f"    +  {new_repr}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point — scan, preview, optionally apply."""
    load_env()

    parser = argparse.ArgumentParser(
        prog="pd_clean_titles.py",
        description=(
            "Strip 📤 and 🔗 emojis (added by Jira integration on DSSD/DRGN "
            "linkage) from open/acknowledged PD incident titles, replacing "
            "them with ' - '."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s                # scan, show diff, prompt y/n\n"
            "  %(prog)s --dry-run      # scan + diff, never call PUT\n"
            "  %(prog)s --yes          # apply without prompting\n"
        ),
    )
    parser.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="Show the planned rewrites and exit without calling PUT.",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip the confirmation prompt and apply all rewrites.",
    )
    parser.add_argument(
        "--version",
        "-v",
        action="version",
        version=f"%(prog)s {VERSION}",
    )

    args = parser.parse_args()

    env_values = require_env("PAGERDUTY_API_TOKEN")
    pagerduty_session = new_pd_client(env_values["PAGERDUTY_API_TOKEN"])

    user_id, user_email = get_current_user(pagerduty_session)
    print(f"Scanning open/acked incidents for {user_email} ({user_id})...")

    incidents = fetch_user_open_incidents(pagerduty_session, user_id)
    print(f"  Found {len(incidents)} open/acked incident(s).")

    rewrites: List[Tuple[str, str, str]] = []
    for incident in incidents:
        incident_id = incident.get("id", "")
        original_title = incident.get("title", "") or ""
        new_title = clean_title(original_title)
        if new_title != original_title:
            rewrites.append((incident_id, original_title, new_title))

    print(f"  Need cleaning: {len(rewrites)}")
    if not rewrites:
        print("Nothing to do.")
        return

    print()
    print("Planned rewrites:")
    print("=" * 70)
    print(format_diff_table(rewrites))
    print("=" * 70)

    if args.dry_run:
        print("\n[DRY RUN] no changes made.")
        return

    if not args.yes:
        try:
            answer = input(f"\nApply {len(rewrites)} rewrite(s)? [y/N]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            return
        if answer not in ("y", "yes"):
            print("Aborted.")
            return

    successes = 0
    failures: List[Tuple[str, str]] = []
    for incident_id, _old_title, new_title in rewrites:
        try:
            update_incident_title(
                pagerduty_session,
                incident_id=incident_id,
                new_title=new_title,
                from_email=user_email,
            )
            print(f"  ✓ {incident_id}")
            successes += 1
        except pagerduty.Error as error:
            failures.append((incident_id, str(error)))
            print(f"  ✗ {incident_id}: {error}")

    print()
    print(f"Done — {successes} updated, {len(failures)} failed.")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
