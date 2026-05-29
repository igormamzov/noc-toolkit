"""GoAnywhere session management.

Uses Selenium with a persistent Chrome profile to perform one-time login
through OKTA (push MFA approved on phone), then captures the ASESSIONID
cookie for subsequent HTTP-only API-style calls. Sessions are cached on
disk and reused until they expire (server returns the login page).
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

import requests

GA_BASE_URL_DEFAULT = "https://goanywhere.prod-tmaws.io"
GA_LOGIN_PATH = "/goanywhere/auth/Login.xhtml"
GA_DASHBOARD_PATH = "/goanywhere/Dashboard.xhtml"
OKTA_HOME_URL = "https://livenation.okta.com/app/UserHome?session_hint=AUTHENTICATED"

CACHE_FILE = Path.home() / ".ga_session.json"
CHROME_PROFILE_DIR = Path.home() / ".ga_chrome_profile"

LOGIN_TIMEOUT_SEC = 600  # 10 minutes — OKTA login + click GoAnywhere tile
SESSION_CHECK_TIMEOUT = 10


@dataclass
class GASession:
    base_url: str
    cookies: Dict[str, str] = field(default_factory=dict)
    captured_at: float = 0.0

    def to_json(self) -> dict:
        return {
            "base_url": self.base_url,
            "cookies": self.cookies,
            "captured_at": self.captured_at,
        }

    @classmethod
    def from_json(cls, data: dict) -> "GASession":
        return cls(
            base_url=data["base_url"],
            cookies=data.get("cookies", {}),
            captured_at=data.get("captured_at", 0.0),
        )

    def cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())


def _load_cached() -> Optional[GASession]:
    if not CACHE_FILE.exists():
        return None
    try:
        return GASession.from_json(json.loads(CACHE_FILE.read_text()))
    except (json.JSONDecodeError, KeyError):
        return None


def _save_cached(sess: GASession) -> None:
    CACHE_FILE.write_text(json.dumps(sess.to_json(), indent=2))
    CACHE_FILE.chmod(0o600)


def _is_session_alive(sess: GASession) -> bool:
    """Verify the session cookie still works by hitting Dashboard."""
    try:
        r = requests.get(
            sess.base_url + GA_DASHBOARD_PATH,
            cookies=sess.cookies,
            allow_redirects=False,
            timeout=SESSION_CHECK_TIMEOUT,
            verify=False,
        )
    except requests.RequestException:
        return False
    if r.status_code != 200:
        return False
    return "GoAnywhere" in r.text and "auth/Login" not in r.text[:2000]


def _selenium_login(base_url: str) -> GASession:
    """Open a visible Chrome window so user can complete OKTA push MFA."""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.support.ui import WebDriverWait
    except ImportError as exc:
        raise SystemExit(
            "Selenium is required. Install: pip install selenium\n"
            f"Original error: {exc}"
        )

    # Use a fresh isolated profile for each login. We don't reuse profile
    # because Chrome holds an exclusive Singleton lock — running Selenium
    # while a regular Chrome window is open would fail. Cookies are still
    # cached at ~/.ga_session.json, so the user only logs in when needed.
    options = Options()
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-features=Translate")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])

    print("Opening Chrome on OKTA UserHome...")
    print("→ Log in to OKTA, approve the push on your phone.")
    print("→ Click the GoAnywhere tile to land on the Dashboard.")
    print(f"→ The cookie will be captured automatically once Dashboard loads. (Timeout: {LOGIN_TIMEOUT_SEC}s)")

    driver = webdriver.Chrome(options=options)
    try:
        driver.get(OKTA_HOME_URL)
        deadline = time.time() + LOGIN_TIMEOUT_SEC
        # Switch to the GoAnywhere tab/window once it loads
        while time.time() < deadline:
            for handle in driver.window_handles:
                driver.switch_to.window(handle)
                url = driver.current_url
                if "goanywhere" in url and "Dashboard.xhtml" in url and "auth/Login" not in url:
                    title = driver.title or ""
                    if "GoAnywhere" in title:
                        # Found the GA dashboard window
                        break
            else:
                time.sleep(2)
                continue
            break
        else:
            raise SystemExit("Login timed out. Please retry.")

        # Cookies must be read while on the GA domain
        if "goanywhere" not in driver.current_url:
            raise SystemExit("Did not land on GoAnywhere domain — cannot read cookie.")
        cookies = {c["name"]: c["value"] for c in driver.get_cookies()}
        if "ASESSIONID" not in cookies:
            raise SystemExit("ASESSIONID cookie not found — login may have failed.")

        sess = GASession(
            base_url=base_url,
            cookies=cookies,
            captured_at=time.time(),
        )
        _save_cached(sess)
        print(f"✓ Session captured. Cached at {CACHE_FILE}")
        return sess
    finally:
        driver.quit()


def _session_from_env(base_url: str) -> Optional[GASession]:
    """If GA_SESSION_COOKIE is set in env, build a session from it.

    Format: 'NAME1=VAL1; NAME2=VAL2' — same as Chrome 'Copy as cURL' Cookie header.
    Bypasses Selenium for ad-hoc testing or when login is done out-of-band.
    """
    raw = os.environ.get("GA_SESSION_COOKIE", "").strip()
    if not raw:
        return None
    cookies: Dict[str, str] = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    if "ASESSIONID" not in cookies:
        return None
    return GASession(base_url=base_url, cookies=cookies, captured_at=time.time())


def get_session(base_url: Optional[str] = None, force_login: bool = False) -> GASession:
    """Return a working GoAnywhere session.

    Resolution order:
      1. force_login=True → Selenium login (always)
      2. GA_SESSION_COOKIE env var → bypass Selenium entirely
      3. ~/.ga_session.json cache → reuse if alive
      4. Selenium login (interactive)
    """
    base_url = base_url or os.environ.get("GA_BASE_URL", GA_BASE_URL_DEFAULT)

    if force_login:
        return _selenium_login(base_url)

    env_sess = _session_from_env(base_url)
    if env_sess:
        if _is_session_alive(env_sess):
            return env_sess
        print("GA_SESSION_COOKIE in .env is expired or invalid.", file=sys.stderr)

    cached = _load_cached()
    if cached and cached.base_url == base_url:
        if _is_session_alive(cached):
            return cached
        print(f"Cached session at {CACHE_FILE} is expired.", file=sys.stderr)

    print("Launching Selenium for OKTA login...", file=sys.stderr)
    return _selenium_login(base_url)


def http_get(path: str, sess: Optional[GASession] = None, **kwargs) -> requests.Response:
    """GET helper that uses the session and adds standard browser headers."""
    sess = sess or get_session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": sess.base_url + GA_DASHBOARD_PATH,
        **kwargs.pop("headers", {}),
    }
    return requests.get(
        sess.base_url + path,
        cookies=sess.cookies,
        headers=headers,
        timeout=kwargs.pop("timeout", 30),
        verify=False,
        **kwargs,
    )
