"""
NOC Toolkit — Shared Utilities

Common helpers used across multiple NOC tools to eliminate code duplication.
"""

import logging
import os
import re
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


class _MaxLevelFilter(logging.Filter):
    """Allow only records below a given level (used to cap stdout at INFO)."""

    def __init__(self, max_level: int) -> None:
        super().__init__()
        self._max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < self._max_level


def setup_logging(verbose: bool = False, name: Optional[str] = None) -> logging.Logger:
    """
    Configure structured logging for a NOC CLI tool.

    Two handlers are installed so that output mirrors the previous ``print()``
    behaviour while adding structured logging:

    * **stdout** — ``INFO`` messages only, formatted as bare ``%(message)s``
      (looks identical to ``print()``).
    * **stderr** — ``WARNING`` / ``ERROR`` / ``CRITICAL`` messages with a
      timestamp prefix.  When *verbose* is ``True``, ``DEBUG`` messages are
      also written to stderr.

    Args:
        verbose: If True, set level to DEBUG; otherwise INFO.
        name: Logger name (typically ``__name__`` of the calling module).

    Returns:
        Configured Logger instance.
    """
    logger = logging.getLogger(name)

    if not logger.handlers:
        # INFO → stdout, plain text (replaces print())
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setLevel(logging.INFO)
        stdout_handler.addFilter(_MaxLevelFilter(logging.WARNING))
        stdout_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(stdout_handler)

        # WARNING+ → stderr with timestamp
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setLevel(logging.WARNING)
        stderr_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(stderr_handler)

    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    return logger


# Optional imports — available when corresponding deps are installed
try:
    import pagerduty as _pagerduty
except ImportError:
    _pagerduty = None  # type: ignore[assignment]

try:
    from jira import JIRA
except ImportError:
    JIRA = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Config management — YAML-based configuration with env-var resolution
# ---------------------------------------------------------------------------

_ENV_VAR_PATTERN: re.Pattern[str] = re.compile(r"\$\{([^}]+)\}")


def _resolve_value(raw_value: Any) -> Optional[str]:
    """Resolve a single config value.

    * If the value is a string matching ``${VAR_NAME}``, return the env var
      value (or ``None`` if the var is unset/empty).
    * If the string *contains* ``${VAR}`` mixed with other text, substitute
      each reference in-place (unset vars become empty strings).
    * Non-string values (int, float, bool) are converted to ``str``.
    * ``None`` / empty strings pass through as ``None``.
    """
    if raw_value is None:
        return None

    if not isinstance(raw_value, str):
        return str(raw_value)

    text = raw_value.strip()
    if not text:
        return None

    # Full-match: entire value is a single ${VAR} reference
    full_match = _ENV_VAR_PATTERN.fullmatch(text)
    if full_match:
        env_val = os.environ.get(full_match.group(1), "")
        return env_val if env_val else None

    # Partial substitution: "https://${HOST}/path"
    if "${" in text:
        def _sub(m: re.Match[str]) -> str:
            return os.environ.get(m.group(1), "")
        return _ENV_VAR_PATTERN.sub(_sub, text)

    # Plain literal value
    return text


def _find_config_path(config_path: Optional[str]) -> Optional[Path]:
    """Locate the config file, returning *None* when it does not exist."""
    if config_path is not None:
        path = Path(config_path)
        if not path.is_file():
            raise FileNotFoundError(f"Config file not found: {path}")
        return path

    # Search order: next to entry-point, then CWD
    candidates = [
        Path(sys.argv[0]).resolve().parent / "config.yaml",
        Path.cwd() / "config.yaml",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _read_raw_config(path: Path) -> Optional[Dict[str, Any]]:
    """Read and return the raw YAML mapping (or *None* for empty/invalid)."""
    _logger = logging.getLogger(__name__)

    with open(path, encoding="utf-8") as fh:
        raw: Any = yaml.safe_load(fh)

    if raw is None:
        return None

    if not isinstance(raw, dict):
        _logger.warning("config.yaml root is not a mapping — ignoring.")
        return None

    return raw


def extract_env_references(config_path: Optional[str] = None) -> set[str]:
    """Return the set of ``${VAR}`` names referenced in *config.yaml*.

    This scans the raw YAML values **without** resolving them, so the
    caller can load only the required variables from ``.env`` before
    calling :func:`load_config`.

    Args:
        config_path: Explicit path, or *None* for auto-discovery.

    Returns:
        Set of environment-variable names (e.g. ``{"PAGERDUTY_API_TOKEN"}``).
        Empty set if no config file is found or no references exist.
    """
    path = _find_config_path(config_path)
    if path is None:
        return set()

    raw = _read_raw_config(path)
    if raw is None:
        return set()

    refs: set[str] = set()
    for section_value in raw.values():
        if isinstance(section_value, dict):
            for val in section_value.values():
                if isinstance(val, str):
                    refs.update(_ENV_VAR_PATTERN.findall(val))
        elif isinstance(section_value, str):
            refs.update(_ENV_VAR_PATTERN.findall(section_value))
    return refs


def load_config(config_path: Optional[str] = None) -> Dict[str, str]:
    """Load and resolve a YAML config file into a flat ``{KEY: value}`` dict.

    The config file is expected to have a ``tokens`` mapping at the top level,
    plus optional additional top-level mappings that are also flattened::

        tokens:
          PAGERDUTY_API_TOKEN: ${PAGERDUTY_API_TOKEN}
          JIRA_SERVER_URL: https://jira.example.com

        settings:
          LOG_LEVEL: INFO

    Values can be:

    * **Raw literals** — used as-is (``https://jira.example.com``).
    * **Env-var references** — ``${VAR_NAME}`` is resolved from the
      environment at load time.
    * **Mixed** — ``https://${HOST}/api`` substitutes inline.

    Keys whose resolved value is ``None`` (env var unset and no literal
    fallback) are **omitted** from the result so that tools fall back to
    their own ``require_env`` validation.

    Args:
        config_path: Path to the YAML file.  When *None*, looks for
            ``config.yaml`` next to the toolkit entry-point (``sys.argv[0]``)
            or the current working directory.

    Returns:
        Dict of resolved configuration key-value pairs.

    Raises:
        FileNotFoundError: If the resolved path does not exist.
        yaml.YAMLError: If the file is not valid YAML.
    """
    _logger = logging.getLogger(__name__)

    path = _find_config_path(config_path)
    if path is None:
        _logger.info("No config.yaml found — using environment variables only.")
        return {}

    _logger.info("Loading configuration from %s", path)

    raw = _read_raw_config(path)
    if raw is None:
        return {}

    resolved: Dict[str, str] = {}

    for section_key, section_value in raw.items():
        if isinstance(section_value, dict):
            # Flatten nested mapping (e.g., tokens:, settings:)
            for key, val in section_value.items():
                result = _resolve_value(val)
                if result is not None:
                    resolved[str(key)] = result
        else:
            # Top-level scalar
            result = _resolve_value(section_value)
            if result is not None:
                resolved[str(section_key)] = result

    _logger.info("Config loaded: %d keys resolved.", len(resolved))
    return resolved


def require_env(*var_names: str) -> Dict[str, str]:
    """
    Validate that required environment variables are set.

    Args:
        *var_names: Names of required environment variables.

    Returns:
        Dict mapping variable names to their values.

    Raises:
        SystemExit: If any required variables are missing.
    """
    _logger = logging.getLogger(__name__)

    values: Dict[str, str] = {}
    missing: List[str] = []

    for name in var_names:
        value = os.environ.get(name, "")
        if value:
            values[name] = value
        else:
            missing.append(name)

    if missing:
        _logger.error(
            "Missing required environment variables: %s", ", ".join(missing),
        )
        _logger.error("Please set these in config.yaml.")
        _logger.error("See config.yaml.example for the required format.")
        sys.exit(1)

    return values


def new_pd_client(api_token: str):
    """
    Create a PagerDuty REST API v2 client with warnings suppressed.

    Args:
        api_token: PagerDuty API token.

    Returns:
        pagerduty.RestApiV2Client instance.
    """
    warnings.filterwarnings('ignore', message='.*lacks a "more" property.*')
    return _pagerduty.RestApiV2Client(api_token)


def new_jira_client(server_url: str, personal_access_token: str):
    """
    Create a Jira client using Server/DC PAT authentication.

    Args:
        server_url: Jira server URL (e.g. https://jira.example.com).
        personal_access_token: Jira Personal Access Token.

    Returns:
        Tuple of (JIRA client, browse_url) where browse_url is
        e.g. "https://jira.example.com/browse".
    """
    client = JIRA(
        server=server_url,
        token_auth=personal_access_token,
    )
    browse_url = server_url.rstrip("/") + "/browse"
    return client, browse_url


def parse_iso_dt(iso_str: str) -> datetime:
    """
    Parse an ISO 8601 datetime string into a timezone-aware datetime.

    Handles the common 'Z' suffix by replacing it with '+00:00'.

    Args:
        iso_str: ISO 8601 datetime string.

    Returns:
        Timezone-aware datetime object.
    """
    return datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
