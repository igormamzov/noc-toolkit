"""CDT Control Panel API client.

The UI host (controlpanel-ui.*) is a SPA that always returns HTML.
The actual API lives at controlpanel.* (no -ui suffix) and uses
OAuth2 Bearer tokens — get one from the UI: Platform → Auth Tokens → Create.

OpenAPI spec is unauthenticated at GET /openapi.json (~370KB, 278 paths).
"""

from __future__ import annotations

import os
from typing import Any, Optional

import requests

CDT_BASE_URL_DEFAULT = "https://controlpanel.prd2971.prod9.us-east-1.tktm.io"
DEFAULT_TIMEOUT = 30


class CDTAuthError(RuntimeError):
    """Raised on 401 — token expired or insufficient scopes."""


class CDTClient:
    """Thin wrapper over requests.Session with Bearer auth + JSON helpers."""

    def __init__(
        self,
        token: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.base_url = (base_url or os.environ.get("CDT_BASE_URL", CDT_BASE_URL_DEFAULT)).rstrip("/")
        self.token = token or os.environ.get("CDT_API_TOKEN", "")
        if not self.token:
            raise SystemExit(
                "CDT_API_TOKEN not set. Create a token in CDT UI: "
                "Platform → Auth Tokens → Create, then add to .env."
            )
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        })

    def get(self, path: str, **params) -> Any:
        url = f"{self.base_url}{path}"
        # Strip None values from params
        clean_params = {k: v for k, v in params.items() if v is not None}
        r = self.session.get(url, params=clean_params or None,
                             timeout=self.timeout, verify=False)
        if r.status_code == 401:
            raise CDTAuthError(
                f"401 Unauthorized for {path}. Token may be expired "
                f"or missing scopes. Re-create at CDT UI → Platform → Auth Tokens."
            )
        r.raise_for_status()
        return r.json()

    def health(self) -> str:
        return self.get("/health")

    def is_authorized(self) -> bool:
        try:
            return bool(self.get("/auth"))
        except Exception:
            return False
