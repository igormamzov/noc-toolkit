"""Parse GoAnywhere ListMonitors.xhtml.

Columns:
    [checkbox] [actions] Name | Description | Last Run Time | Next Run Time
    | Run Count | Action Last Run Time | Actions Fired

The page is paginated server-side (max 100 rows per page). To get all
monitors, we GET the page once to obtain the ViewState, then issue JSF
AJAX postbacks with `javax.faces.behavior.event=page` for each offset.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import List, Optional

import requests
from bs4 import BeautifulSoup

from ga_session import GASession, get_session, http_get

LIST_MONITORS_PATH = "/goanywhere/monitors/ListMonitors.xhtml"
PAGE_SIZE = 100  # max allowed by GA dropdown

COL_NAME = 2
COL_DESC = 3
COL_LAST_RUN = 4
COL_NEXT_RUN = 5
COL_RUN_COUNT = 6
COL_ACTION_LAST_RUN = 7
COL_ACTIONS_FIRED = 8


@dataclass
class Monitor:
    name: str
    description: str
    last_run_time: str
    next_run_time: str
    run_count: str
    action_last_run_time: str
    actions_fired: str

    def as_dict(self) -> dict:
        return asdict(self)


def _find_monitors_table(soup: BeautifulSoup):
    tables = soup.find_all("table")
    if not tables:
        return None
    return max(tables, key=lambda t: len(t.find_all("tr")))


def _cell_text(cells, idx: int) -> str:
    """Return cell text, with fallback to img title for icon-only cells."""
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


def _parse_rows(html_or_xml: str) -> List[Monitor]:
    """Parse monitor rows from either a full HTML page or a JSF partial-response XML.

    Partial AJAX responses wrap the table HTML in
    <update id="MonitorListForm:monitors"><![CDATA[...HTML...]]></update>.
    We unwrap it before handing to BeautifulSoup.
    """
    text = html_or_xml
    is_partial = False
    cdata_match = re.search(
        r'<update id="MonitorListForm:monitors"><!\[CDATA\[(.*?)\]\]></update>',
        text,
        re.DOTALL,
    )
    if cdata_match:
        text = cdata_match.group(1)
        is_partial = True

    soup = BeautifulSoup(text, "html.parser")
    if is_partial:
        # Partial response inner is bare <tr>...</tr> rows — wrap them
        rows = soup.find_all("tr", attrs={"data-rk": True})
    else:
        # Full page render — locate the largest table and use its tbody
        table = _find_monitors_table(soup)
        if not table:
            return []
        body = table.find("tbody")
        if not body:
            return []
        rows = body.find_all("tr")

    monitors: List[Monitor] = []
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < COL_ACTIONS_FIRED + 1:
            continue
        name = _cell_text(cells, COL_NAME)
        if not name:
            continue
        # The Name cell embeds the schedule info as plain text. Strip any
        # text from "Run " onward (covers "Run from ...", "Run every ...",
        # "Run on demand", etc.). Also strip "Never" trailing.
        clean_name = re.sub(r"\s*Run [a-z].*$", "", name, flags=re.IGNORECASE).strip()
        clean_name = re.sub(r"\s*Never\s*$", "", clean_name).strip()
        clean_desc = _cell_text(cells, COL_DESC)
        clean_desc = " ".join(clean_desc.split())
        monitors.append(Monitor(
            name=clean_name,
            description=clean_desc,
            last_run_time=_cell_text(cells, COL_LAST_RUN),
            next_run_time=_cell_text(cells, COL_NEXT_RUN),
            run_count=_cell_text(cells, COL_RUN_COUNT),
            action_last_run_time=_cell_text(cells, COL_ACTION_LAST_RUN),
            actions_fired=_cell_text(cells, COL_ACTIONS_FIRED),
        ))
    return monitors


def _extract_total(html: str) -> Optional[int]:
    """Extract the 'Showing X - Y of TOTAL' indicator."""
    m = re.search(r"Showing\s+\d+\s*-\s*\d+\s+of\s+(\d+)", html)
    return int(m.group(1)) if m else None


def _extract_viewstate_from_partial(xml: str) -> Optional[str]:
    """JSF partial-response refreshes the ViewState in an <update> tag."""
    m = re.search(
        r'<update id="javax\.faces\.ViewState[^"]*"><!\[CDATA\[([^\]]+)\]\]></update>',
        xml,
    )
    return m.group(1) if m else None


def list_monitors(
    sess: Optional[GASession] = None,
    *,
    fetch_all: bool = True,
) -> List[Monitor]:
    """Return all monitors. By default walks all pages (5 requests for ~456 monitors)."""
    sess = sess or get_session()
    url = sess.base_url + LIST_MONITORS_PATH

    # 1. GET initial page to grab ViewState + first 20 rows
    r = http_get(LIST_MONITORS_PATH, sess=sess)
    if r.status_code != 200:
        raise RuntimeError(f"GA returned HTTP {r.status_code} for ListMonitors")
    if "auth/Login" in r.text[:5000]:
        raise RuntimeError("GA session expired. Re-run `login` to refresh cookie.")

    soup = BeautifulSoup(r.text, "html.parser")
    vs_input = soup.find("input", {"name": "javax.faces.ViewState"})
    if not vs_input:
        # No ViewState — return whatever was on page 1
        return _parse_rows(r.text)
    view_state = vs_input.get("value")
    total = _extract_total(r.text) or 0

    if not fetch_all or total <= 20:
        return _parse_rows(r.text)

    # 2. Sync any new cookies set by GET (Set-Cookie may rotate things)
    cookies = {**sess.cookies}
    for c in r.cookies:
        cookies[c.name] = c.value

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "Referer": url,
        "Faces-Request": "partial/ajax",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/xml, text/xml, */*; q=0.01",
    }

    # 3. POST per page until we cover total
    monitors: List[Monitor] = []
    seen_names = set()
    for offset in range(0, total, PAGE_SIZE):
        data = {
            "javax.faces.partial.ajax": "true",
            "javax.faces.source": "MonitorListForm:monitors",
            "javax.faces.partial.execute": "MonitorListForm:monitors",
            "javax.faces.partial.render": "MonitorListForm:monitors",
            "javax.faces.behavior.event": "page",
            "javax.faces.partial.event": "page",
            "MonitorListForm:monitors_pagination": "true",
            "MonitorListForm:monitors_first": str(offset),
            "MonitorListForm:monitors_rows": str(PAGE_SIZE),
            "MonitorListForm:monitors_encodeFeature": "true",
            "MonitorListForm:monitors_skipChildren": "true",
            "MonitorListForm": "MonitorListForm",
            "MonitorListForm:monitors_selection": "",
            "MonitorListForm_SUBMIT": "1",
            "javax.faces.ViewState": view_state,
        }
        rp = requests.post(url, cookies=cookies, data=data, headers=headers,
                           verify=False, timeout=30)
        if rp.status_code != 200:
            raise RuntimeError(f"GA paginator POST failed: HTTP {rp.status_code}")

        for m in _parse_rows(rp.text):
            # Dedupe in case of overlap between pages
            if m.name not in seen_names:
                seen_names.add(m.name)
                monitors.append(m)

        new_vs = _extract_viewstate_from_partial(rp.text)
        if new_vs:
            view_state = new_vs

    return monitors


def find_monitor(
    name_pattern: str,
    sess: Optional[GASession] = None,
) -> List[Monitor]:
    """Return monitors whose name matches the given regex (case-insensitive)."""
    pat = re.compile(name_pattern, re.IGNORECASE)
    return [m for m in list_monitors(sess=sess) if pat.search(m.name)]
