#!/usr/bin/env python3
"""
NOC Toolkit Site Deployer

Reads VERSION.md for tool versions and changelog, discovers build artifacts
in a release directory, renders an updated index.html, and deploys everything
to Netlify via the Deploy API.

Usage:
    python site/deploy.py --dry-run --verbose          # preview what would happen
    python site/deploy.py --output /tmp/preview.html   # render HTML to file
    python site/deploy.py --release-dir ./release      # full deploy
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from html import escape as html_escape
from pathlib import Path
from typing import Optional

VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# Tool-to-placeholder mapping
# ---------------------------------------------------------------------------
TOOL_PLACEHOLDERS: dict[str, str] = {
    "pd-jira-tool": "VERSION_PD_JIRA_TOOL",
    "pagerduty-job-extractor": "VERSION_PAGERDUTY_JOB_EXTRACTOR",
    "pd-monitor": "VERSION_PD_MONITOR",
    "pd-merge": "VERSION_PD_MERGE",
    "data-freshness": "VERSION_DATA_FRESHNESS",
    "noc-report-assistant": "VERSION_NOC_REPORT_ASSISTANT",
}

PLATFORM_META: dict[str, dict[str, str]] = {
    "windows": {"icon": "🪟", "label": "Windows", "subtitle": ".zip — Windows 10/11"},
    "macos": {"icon": "🍎", "label": "macOS", "subtitle": ".tar.gz — macOS 12+"},
    "linux": {"icon": "🐧", "label": "Linux", "subtitle": ".tar.gz — x86_64"},
}

ARTIFACT_PATTERN: re.Pattern[str] = re.compile(
    r"^noc-toolkit-(v?[\d.]+)-(windows|macos|linux)\.(zip|tar\.gz)$"
)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Artifact:
    """A discovered build artifact."""

    path: Path
    version: str
    platform: str
    filename: str
    size_bytes: int


@dataclass
class ChangelogEntry:
    """A single changelog entry parsed from VERSION.md."""

    heading: str
    date: str
    body_lines: list[str] = field(default_factory=list)


class DeployError(RuntimeError):
    """Raised for unrecoverable deploy errors."""


# ---------------------------------------------------------------------------
# VERSION.md parsing
# ---------------------------------------------------------------------------

def parse_versions(text: str) -> dict[str, str]:
    """Parse the 'Current Versions' table from VERSION.md.

    Returns a dict mapping component name (lowercase) to version string.
    Example: {"noc-toolkit": "0.5.0", "pd-monitor": "0.1.1"}
    """
    versions: dict[str, str] = {}
    in_table: bool = False
    for line in text.splitlines():
        if "| Component" in line and "| Version" in line:
            in_table = True
            continue
        if in_table and line.startswith("|---"):
            continue
        if in_table and line.startswith("|"):
            cols = [c.strip().strip("*") for c in line.strip("|").split("|")]
            if len(cols) >= 2:
                name: str = cols[0].lower().strip()
                version: str = cols[1].strip()
                if re.match(r"\d+\.\d+\.\d+", version):
                    versions[name] = version
        elif in_table:
            break
    return versions


def parse_changelog(text: str, limit: int = 6) -> list[ChangelogEntry]:
    """Parse 'Version History' section from VERSION.md.

    Returns the most recent *limit* changelog entries.
    """
    entries: list[ChangelogEntry] = []
    in_history: bool = False
    current: Optional[ChangelogEntry] = None

    for line in text.splitlines():
        stripped: str = line.strip()

        # Detect start of Version History section
        if stripped.startswith("## Version History"):
            in_history = True
            continue

        if not in_history:
            continue

        # Stop at next top-level section
        if stripped.startswith("## ") and "Version History" not in stripped:
            break

        # New changelog entry header: ### Component vX.Y.Z (YYYY-MM-DD)
        if stripped.startswith("### "):
            if current is not None:
                entries.append(current)
            heading_text: str = stripped[4:]
            date_match = re.search(r"\((\d{4}-\d{2}-\d{2})\)", heading_text)
            date_str: str = date_match.group(1) if date_match else ""
            # Remove the date part from the heading for cleaner display
            clean_heading: str = re.sub(r"\s*\(\d{4}-\d{2}-\d{2}\)\s*", "", heading_text).strip()
            current = ChangelogEntry(heading=clean_heading, date=date_str)
            continue

        # Body lines (bullet points and description lines)
        if current is not None and stripped:
            # Skip bold sub-headers like **Diversified auto-acknowledge comments:**
            if stripped.startswith("**") and stripped.endswith(":**"):
                continue
            # Collect bullet points
            if stripped.startswith("- "):
                current.body_lines.append(stripped[2:])

    if current is not None:
        entries.append(current)

    return entries[:limit]


# ---------------------------------------------------------------------------
# Artifact discovery
# ---------------------------------------------------------------------------

def _parse_semver(version_string: str) -> tuple[int, ...]:
    """Parse a semver string like '0.5.0' or 'v0.5.0' into a comparable tuple."""
    cleaned: str = version_string.lstrip("v")
    parts: list[str] = cleaned.split(".")
    return tuple(int(p) for p in parts if p.isdigit())


def discover_artifacts(release_dir: Path) -> list[Artifact]:
    """Scan release_dir for build artifacts matching the naming convention."""
    artifacts: list[Artifact] = []
    if not release_dir.exists():
        return artifacts
    for filepath in release_dir.iterdir():
        match = ARTIFACT_PATTERN.match(filepath.name)
        if match:
            artifacts.append(Artifact(
                path=filepath,
                version=match.group(1),
                platform=match.group(2),
                filename=filepath.name,
                size_bytes=filepath.stat().st_size,
            ))
    return artifacts


def get_latest_artifacts(artifacts: list[Artifact]) -> list[Artifact]:
    """Filter to only the highest-version artifacts."""
    if not artifacts:
        return []
    max_version: tuple[int, ...] = max(_parse_semver(a.version) for a in artifacts)
    return [a for a in artifacts if _parse_semver(a.version) == max_version]


def _format_file_size(size_bytes: int) -> str:
    """Format bytes as human-readable size string."""
    if size_bytes >= 1_048_576:
        return f"{size_bytes / 1_048_576:.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.0f} KB"
    return f"{size_bytes} B"


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

def render_download_section(latest_artifacts: list[Artifact]) -> str:
    """Generate HTML for the Downloads section."""
    if not latest_artifacts:
        return '<p class="download-empty">No downloads available yet. Check back soon.</p>'

    cards: list[str] = []
    for platform_key in ("windows", "macos", "linux"):
        matching: list[Artifact] = [a for a in latest_artifacts if a.platform == platform_key]
        if not matching:
            continue
        artifact: Artifact = matching[0]
        meta: dict[str, str] = PLATFORM_META[platform_key]
        size_str: str = _format_file_size(artifact.size_bytes)
        card_html: str = f"""            <div class="download-card">
                <div class="platform-icon">{meta['icon']}</div>
                <h3>{meta['label']}</h3>
                <div class="file-info">{meta['subtitle']} &middot; {size_str}</div>
                <a href="/{html_escape(artifact.filename)}" class="btn-download">Download</a>
            </div>"""
        cards.append(card_html)

    if not cards:
        return '<p class="download-empty">No downloads available yet. Check back soon.</p>'

    return '<div class="download-grid">\n' + "\n\n".join(cards) + "\n            </div>"


def render_changelog_section(entries: list[ChangelogEntry]) -> str:
    """Generate HTML for the Changelog section."""
    if not entries:
        return '<p class="download-empty">No changelog entries yet.</p>'

    items: list[str] = []
    for entry in entries:
        bullets: str = ""
        if entry.body_lines:
            li_items: str = "\n".join(
                f"                    <li>{html_escape(line)}</li>"
                for line in entry.body_lines
            )
            bullets = f"\n                <ul>\n{li_items}\n                </ul>"
        date_html: str = (
            f'\n                <div class="changelog-date">{html_escape(entry.date)}</div>'
            if entry.date else ""
        )
        item_html: str = f"""            <div class="changelog-entry">
                <h3>{html_escape(entry.heading)}</h3>{date_html}{bullets}
            </div>"""
        items.append(item_html)

    return '<div class="changelog-list">\n' + "\n\n".join(items) + "\n            </div>"


def render_html(
    template: str,
    versions: dict[str, str],
    latest_artifacts: list[Artifact],
    changelog_entries: list[ChangelogEntry],
) -> str:
    """Substitute all {{PLACEHOLDER}} tokens in the template and return final HTML."""
    html: str = template

    # Toolkit version
    toolkit_version: str = versions.get("noc-toolkit", "?.?.?")
    html = html.replace("{{TOOLKIT_VERSION}}", f"v{toolkit_version}")

    # Per-tool versions
    for tool_key, placeholder in TOOL_PLACEHOLDERS.items():
        version: str = versions.get(tool_key, "?.?.?")
        html = html.replace("{{" + placeholder + "}}", f"v{version}")

    # Download section
    html = html.replace("{{DOWNLOAD_SECTION}}", render_download_section(latest_artifacts))

    # Changelog section
    html = html.replace("{{CHANGELOG_SECTION}}", render_changelog_section(changelog_entries))

    return html


# ---------------------------------------------------------------------------
# Netlify Deploy API (stdlib urllib)
# ---------------------------------------------------------------------------

def _create_ssl_context() -> ssl.SSLContext:
    """Create a permissive SSL context for urllib."""
    ctx: ssl.SSLContext = ssl.create_default_context()
    return ctx


def netlify_api(
    method: str,
    url: str,
    token: str,
    data: Optional[bytes] = None,
    content_type: str = "application/json",
) -> dict:
    """Make a Netlify API request. Returns parsed JSON response."""
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Content-Type", content_type)
    ctx: ssl.SSLContext = _create_ssl_context()
    try:
        with urllib.request.urlopen(request, context=ctx, timeout=300) as response:
            body: bytes = response.read()
            if body:
                return json.loads(body)
            return {}
    except urllib.error.HTTPError as exc:
        error_body: str = exc.read().decode("utf-8", errors="replace")
        raise DeployError(
            f"Netlify API error: HTTP {exc.code} {exc.reason}\n{error_body}"
        ) from exc


def sha1_file(filepath: Path) -> str:
    """Compute SHA1 hex digest of a file."""
    hasher = hashlib.sha1()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def sha1_bytes(data: bytes) -> str:
    """Compute SHA1 hex digest of in-memory bytes."""
    return hashlib.sha1(data).hexdigest()


def deploy_to_netlify(
    site_id: str,
    token: str,
    file_manifest: dict[str, tuple[str, Path | bytes]],
    verbose: bool = False,
) -> str:
    """Deploy files to Netlify.

    Args:
        site_id: Netlify site ID.
        token: Netlify auth token.
        file_manifest: Mapping of site path -> (sha1, Path or bytes).
            If the value's second element is a Path, the file is read from disk.
            If bytes, the content is used directly (for rendered index.html).
        verbose: Print progress details.

    Returns:
        The deploy URL.
    """
    base_url: str = "https://api.netlify.com/api/v1"

    # Step 1: Build digest map
    digest_map: dict[str, str] = {}
    for site_path, (sha1, _source) in file_manifest.items():
        digest_map[site_path] = sha1

    if verbose:
        print(f"\n  Deploy manifest ({len(digest_map)} files):")
        for site_path, sha1 in sorted(digest_map.items()):
            print(f"    {site_path} -> {sha1[:12]}...")

    # Step 2: Create deploy
    deploy_payload: bytes = json.dumps({"files": digest_map}).encode("utf-8")
    print("\n  Creating deploy...")
    deploy_response: dict = netlify_api(
        "POST",
        f"{base_url}/sites/{site_id}/deploys",
        token,
        data=deploy_payload,
    )

    deploy_id: str = deploy_response["id"]
    required_hashes: set[str] = set(deploy_response.get("required", []))
    deploy_ssl_url: str = deploy_response.get("ssl_url", deploy_response.get("url", ""))

    if verbose:
        print(f"  Deploy ID: {deploy_id}")
        print(f"  Files to upload: {len(required_hashes)} (cached: {len(digest_map) - len(required_hashes)})")

    # Step 3: Upload required files
    # Build reverse map: sha1 -> (site_path, source)
    sha1_to_file: dict[str, tuple[str, Path | bytes]] = {}
    for site_path, (sha1, source) in file_manifest.items():
        if sha1 in required_hashes:
            sha1_to_file[sha1] = (site_path, source)

    uploaded_count: int = 0
    for sha1, (site_path, source) in sha1_to_file.items():
        if isinstance(source, bytes):
            file_data: bytes = source
        else:
            file_data = source.read_bytes()

        if verbose:
            size_str: str = _format_file_size(len(file_data))
            print(f"  Uploading {site_path} ({size_str})...")

        netlify_api(
            "PUT",
            f"{base_url}/deploys/{deploy_id}/files{site_path}",
            token,
            data=file_data,
            content_type="application/octet-stream",
        )
        uploaded_count += 1

    print(f"  Uploaded {uploaded_count} file(s).")

    # Step 4: Poll for completion
    print("  Waiting for deploy to go live...", end="", flush=True)
    for _attempt in range(60):
        time.sleep(2)
        status_response: dict = netlify_api(
            "GET",
            f"{base_url}/deploys/{deploy_id}",
            token,
        )
        state: str = status_response.get("state", "unknown")
        if state == "ready":
            print(" done!")
            return deploy_ssl_url
        if state in ("error", "failed"):
            raise DeployError(f"Deploy failed with state: {state}")
        print(".", end="", flush=True)

    raise DeployError("Deploy timed out after 120 seconds.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def resolve_repo_root() -> Path:
    """Find the repo root (parent of site/)."""
    script_dir: Path = Path(__file__).resolve().parent
    # deploy.py lives in site/, so repo root is one level up
    repo_root: Path = script_dir.parent
    if (repo_root / "VERSION.md").exists():
        return repo_root
    # Fallback: try current working directory
    cwd: Path = Path.cwd()
    if (cwd / "VERSION.md").exists():
        return cwd
    raise DeployError(
        "Cannot find repo root (VERSION.md not found). "
        "Run from the repo root or from site/."
    )


def main() -> None:
    """Entry point."""
    parser = argparse.ArgumentParser(
        description="Deploy NOC Toolkit site to Netlify",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python site/deploy.py --dry-run --verbose
  python site/deploy.py --output /tmp/preview.html
  python site/deploy.py --release-dir ./release --verbose
""",
    )
    parser.add_argument(
        "--release-dir",
        type=Path,
        default=None,
        help="Directory containing build artifacts (default: ./release)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Render HTML and show manifest, but do not deploy",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write rendered index.html to file (for previewing)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print detailed progress",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"site-deploy {VERSION}",
    )

    args = parser.parse_args()

    # Resolve paths
    repo_root: Path = resolve_repo_root()
    site_dir: Path = repo_root / "site"
    version_md_path: Path = repo_root / "VERSION.md"
    template_path: Path = site_dir / "index.html"
    release_dir: Path = args.release_dir if args.release_dir else repo_root / "release"

    # Load .env if available
    env_path: Path = repo_root / ".env"
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path)
        except ImportError:
            pass

    import os
    netlify_token: str = os.environ.get("NETLIFY_AUTH_TOKEN", "")
    netlify_site_id: str = os.environ.get("NETLIFY_SITE_ID", "")

    # ---- Step 1: Parse VERSION.md ----
    print("Reading VERSION.md...")
    version_md_text: str = version_md_path.read_text(encoding="utf-8")
    versions: dict[str, str] = parse_versions(version_md_text)
    changelog_entries: list[ChangelogEntry] = parse_changelog(version_md_text)

    if not versions:
        raise DeployError("No versions found in VERSION.md — is the table formatted correctly?")

    if args.verbose:
        print(f"  Found {len(versions)} tool version(s):")
        for name, ver in sorted(versions.items()):
            print(f"    {name}: {ver}")
        print(f"  Found {len(changelog_entries)} changelog entries")

    # ---- Step 2: Discover artifacts ----
    print(f"Scanning artifacts in {release_dir}...")
    all_artifacts: list[Artifact] = discover_artifacts(release_dir)
    latest_artifacts: list[Artifact] = get_latest_artifacts(all_artifacts)

    if latest_artifacts:
        latest_version: str = latest_artifacts[0].version
        print(f"  Found {len(latest_artifacts)} artifact(s) for version {latest_version}:")
        for artifact in sorted(latest_artifacts, key=lambda a: a.platform):
            size_str: str = _format_file_size(artifact.size_bytes)
            print(f"    {artifact.filename} ({size_str})")
            if artifact.size_bytes > 50_000_000:
                print(f"    WARNING: File exceeds 50 MB — close to Netlify limits")
    else:
        print("  No artifacts found (download section will show placeholder)")

    # ---- Step 3: Render HTML ----
    print("Rendering index.html...")
    template: str = template_path.read_text(encoding="utf-8")
    rendered_html: str = render_html(template, versions, latest_artifacts, changelog_entries)

    # Check for unresolved placeholders
    unresolved: list[str] = re.findall(r"\{\{[A-Z_]+\}\}", rendered_html)
    if unresolved:
        print(f"  WARNING: Unresolved placeholders: {', '.join(unresolved)}")

    # ---- Step 4: Output / Deploy ----
    if args.output:
        args.output.write_text(rendered_html, encoding="utf-8")
        print(f"\n  Rendered HTML written to: {args.output}")
        return

    if args.dry_run:
        print("\n  [DRY RUN] Would deploy the following files:")

        # Site files
        site_files: list[str] = ["index.html (rendered)", "style.css", "netlify.toml"]
        for name in site_files:
            print(f"    /{name}")

        # Artifacts
        for artifact in latest_artifacts:
            print(f"    /{artifact.filename}")

        print(f"\n  Total: {len(site_files) + len(latest_artifacts)} file(s)")
        print("  [DRY RUN] No changes made.")
        return

    # Real deploy — validate credentials
    if not netlify_token:
        raise DeployError(
            "NETLIFY_AUTH_TOKEN not set. "
            "Add it to .env or set it as an environment variable.\n"
            "Get a token at: https://app.netlify.com/user/applications#personal-access-tokens"
        )
    if not netlify_site_id:
        raise DeployError(
            "NETLIFY_SITE_ID not set. "
            "Add it to .env or set it as an environment variable.\n"
            "Find your site ID in Netlify Site Settings > General."
        )

    # Build file manifest: {site_path: (sha1, Path|bytes)}
    rendered_bytes: bytes = rendered_html.encode("utf-8")
    file_manifest: dict[str, tuple[str, Path | bytes]] = {
        "/index.html": (sha1_bytes(rendered_bytes), rendered_bytes),
    }

    # Add static site files
    for static_file in ("style.css", "netlify.toml"):
        filepath: Path = site_dir / static_file
        if filepath.exists():
            file_manifest[f"/{static_file}"] = (sha1_file(filepath), filepath)

    # Add artifacts
    for artifact in latest_artifacts:
        file_manifest[f"/{artifact.filename}"] = (sha1_file(artifact.path), artifact.path)

    # Deploy
    print(f"\nDeploying to Netlify (site: {netlify_site_id})...")
    deploy_url: str = deploy_to_netlify(
        site_id=netlify_site_id,
        token=netlify_token,
        file_manifest=file_manifest,
        verbose=args.verbose,
    )

    print(f"\n  Site live at: {deploy_url}")


if __name__ == "__main__":
    try:
        main()
    except DeployError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(130)
