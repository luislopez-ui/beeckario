from __future__ import annotations

import re
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


SPANISH_MONTHS = {
    "enero": 1, "ene": 1,
    "febrero": 2, "feb": 2,
    "marzo": 3, "mar": 3,
    "abril": 4, "abr": 4,
    "mayo": 5, "may": 5,
    "junio": 6, "jun": 6,
    "julio": 7, "jul": 7,
    "agosto": 8, "ago": 8,
    "septiembre": 9, "setiembre": 9, "sep": 9, "set": 9,
    "octubre": 10, "oct": 10,
    "noviembre": 11, "nov": 11,
    "diciembre": 12, "dic": 12,
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _tz(timezone: str):
    """Return tzinfo if available.

    - Accepts IANA names (e.g. America/Mexico_City)
    - Accepts 'UTC' / 'Z' as aliases (mapped safely)

    If tzdata is missing or the key is unknown, returns a safe fallback.
    - For CDMX (America/Mexico_City) we fall back to UTC-06:00.
    - Otherwise we return None so callers can fall back to local system time.
    """
    tz_name = (timezone or '').strip()
    if tz_name.upper() in ('CDMX', 'MEXICO_CITY', 'MEXICO CITY'):
        tz_name = 'America/Mexico_City'
    if not tz_name:
        return None

    if tz_name.upper() in ('UTC', 'Z'):
        if ZoneInfo is not None:
            for key in ('Etc/UTC', 'UTC'):
                try:
                    return ZoneInfo(key)
                except Exception:
                    pass
        return dt_timezone.utc

    if ZoneInfo is None:
        if tz_name == 'America/Mexico_City':
            return dt_timezone(timedelta(hours=-6))
        return None
    try:
        return ZoneInfo(tz_name)
    except Exception:
        if tz_name == 'America/Mexico_City':
            return dt_timezone(timedelta(hours=-6))
        return None



def _utc_iso_to_clockify_query_param(utc_iso_z: str, account_timezone: str) -> str:
    """Clockify quirk: query params `start`/`end` must be sent in *account timezone*.

    Clockify returns timestamps in UTC (with `Z`). However, per Clockify's own
    forum guidance, when using GET /workspaces/{workspaceId}/user/{userId}/time-entries
    with `start` / `end` query params, the values should be based on your account
    timezone, while responses are in UTC.

    Practically, the most reliable approach is:
      - parse the UTC timestamp
      - convert it to the account timezone
      - format it back as ISO with a trailing `Z` ("local time labeled as Z")

    This matches the behavior described by Clockify staff and fixes empty result
    sets when you send pure UTC values.
    """
    v = (utc_iso_z or "").strip()
    if not v:
        return v
    try:
        dt_utc = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except Exception:
        return v
    tz = _tz(account_timezone)
    if tz is None:
        return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    dt_local = dt_utc.astimezone(tz)
    return dt_local.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_spanish_date(text: str) -> Optional[datetime]:
    """Parse simple Spanish dates.

    Supports:
      - '26 de diciembre de 2025'
      - '26 de diciembre' (assumes current year)
      - '26/12/2025'
      - '26/12' (assumes current year)
    """
    t = _norm(text)

    tz_name = (os.getenv("CLOCKIFY_TIMEZONE") or "America/Mexico_City").strip() or "America/Mexico_City"
    tz = _tz(tz_name)
    now = datetime.now(tz=tz) if tz is not None else datetime.now()
    now = now.replace(tzinfo=None)
    if re.search(r"\b(hoy)\b", t):
        return datetime(now.year, now.month, now.day)
    if re.search(r"\b(ayer)\b", t):
        d = now - timedelta(days=1)
        return datetime(d.year, d.month, d.day)
    if re.search(r"\b(mañana|manana)\b", t):
        d = now + timedelta(days=1)
        return datetime(d.year, d.month, d.day)

    m = re.search(r"\b(?:este\s+|proximo\s+|pr[oó]ximo\s+)?(lunes|martes|mi[eé]rcoles|miercoles|jueves|viernes|s[aá]bado|sabado|domingo)\b", t)
    if m:
        w = m.group(1)
        w = w.replace("é", "e").replace("á", "a").replace("í", "i").replace("ó", "o").replace("ú", "u")
        weekdays = {"lunes": 0, "martes": 1, "miercoles": 2, "jueves": 3, "viernes": 4, "sabado": 5, "domingo": 6}
        target = weekdays.get(w)
        if target is not None:
            delta = (target - now.weekday()) % 7
            d = now + timedelta(days=delta)
            return datetime(d.year, d.month, d.day)

    now_year = now.year

    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t)
    if m:
        y, mo, d = map(int, m.groups())
        return datetime(y, mo, d)

    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", t)
    if m:
        d = int(m.group(1))
        mo = int(m.group(2))
        y = m.group(3)
        y_i = int(y) if y is not None else now_year
        if y_i < 100:
            y_i += 2000
        return datetime(y_i, mo, d)

    m = re.search(r"\b(\d{1,2})\s+de\s+([a-zñáéíóú]+)(?:\s+de\s+(\d{4}))?\b", t)
    if m:
        d = int(m.group(1))
        month_txt = _norm(m.group(2))
        y = int(m.group(3)) if m.group(3) else now_year
        mo = SPANISH_MONTHS.get(month_txt)
        if mo:
            return datetime(y, mo, d)
    return None


def parse_time_range(text: str, base_date: Optional[datetime]) -> Tuple[Optional[datetime], Optional[datetime]]:
    """Parse time ranges and apply base_date.

    Supports:
      - '15:00-17:00' / '15:00 a 17:00'
      - '9 am a 10 am' / '9am-10am'
      - 'de 9 a 10' (assumes 24h or am if clearly morning)
    """
    if not base_date:
        return None, None
    t = _norm(text)

    # IMPORTANT: avoid accidentally parsing dates as time ranges.
    # Example bug: in text like "fecha=2026-02-19", the substring "02-19"
    # matched the generic "H-H" pattern and became 02:00–19:00.
    # We strip common date formats before attempting to parse time ranges.
    t = re.sub(r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b", " ", t)  # YYYY-MM-DD / YYYY/MM/DD
    t = re.sub(r"\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\b", " ", t)  # DD-MM-YYYY / DD/MM/YY
    t = re.sub(r"\b\d{1,2}\s+de\s+[a-zñáéíóú]+\s+de\s+\d{4}\b", " ", t)  # 26 de diciembre de 2025

    def _h24(h: int, ampm: Optional[str]) -> int:
        if not ampm:
            return h
        a = ampm.lower()
        if a == "am":
            return 0 if h == 12 else h
        if a == "pm":
            return h if h == 12 else h + 12
        return h

    # 1) HH:MM - HH:MM
    m = re.search(r"\b(\d{1,2}):(\d{2})\s*(?:-|a|–|—|hasta)\s*(\d{1,2}):(\d{2})\b", t)
    if m:
        h1, mi1, h2, mi2 = map(int, m.groups())
        start = base_date.replace(hour=h1, minute=mi1, second=0, microsecond=0)
        end = base_date.replace(hour=h2, minute=mi2, second=0, microsecond=0)
        if end <= start:
            end = end + timedelta(days=1)
        return start, end

    # 2) H[H][:MM] am/pm - H[H][:MM] am/pm
    m = re.search(
        r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:-|a|–|—|hasta)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
        t,
    )
    if not m:
        return None, None

    h1_s, mi1_s, ap1, h2_s, mi2_s, ap2 = m.groups()
    h1 = int(h1_s)
    h2 = int(h2_s)
    mi1 = int(mi1_s) if mi1_s else 0
    mi2 = int(mi2_s) if mi2_s else 0
    # If only one side specifies am/pm, apply it to both.
    if ap1 and not ap2:
        ap2 = ap1
    if ap2 and not ap1:
        ap1 = ap2

    h1 = _h24(h1, ap1)
    h2 = _h24(h2, ap2)
    start = base_date.replace(hour=h1, minute=mi1, second=0, microsecond=0)
    end = base_date.replace(hour=h2, minute=mi2, second=0, microsecond=0)
    if end <= start:
        end = end + timedelta(days=1)
    return start, end


@dataclass(frozen=True)
class EntryCriteria:
    date: Optional[datetime] = None
    start: Optional[str] = None  # ISO Z, or None
    end: Optional[str] = None    # ISO Z, or None
    description: Optional[str] = None
    project_id: Optional[str] = None
    # if multiple entries match, choose best by scoring


def score_entry(entry: Dict[str, Any], criteria: EntryCriteria) -> int:
    score = 0
    desc = _norm(entry.get("description") or "")
    if criteria.description:
        q = _norm(criteria.description)
        if desc == q:
            score += 100
        elif q and q in desc:
            score += 60
    if criteria.project_id and entry.get("projectId") == criteria.project_id:
        score += 80

    ti = entry.get("timeInterval") or {}
    es = ti.get("start")
    ee = ti.get("end")
    if criteria.start and es == criteria.start:
        score += 30
    if criteria.end and ee == criteria.end:
        score += 30
    return score


def find_time_entries(
    client,
    user_id: str,
    criteria: EntryCriteria,
    *,
    page_size: int = 200,
    max_pages: int = 10,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Fetch time entries for a user and filter them in Python.

    Returns (matches, debug) where debug includes the query used.
    """
    params: Dict[str, Any] = {"page-size": page_size, "page": 1}
    # IMPORTANT: for this endpoint, Clockify expects start/end query params in
    # the account's timezone (even though it returns UTC in the response).
    # We convert our UTC criteria to "local-as-Z" for the query.
    tz_name = getattr(getattr(client, "config", None), "timezone", None) or "UTC"
    if criteria.start:
        params["start"] = _utc_iso_to_clockify_query_param(criteria.start, tz_name)
    if criteria.end:
        params["end"] = _utc_iso_to_clockify_query_param(criteria.end, tz_name)
    if criteria.description:
        # Clockify docs: 'description' is keywords search on server-side
        params["description"] = criteria.description
    if criteria.project_id:
        # Clockify docs: param is named 'project' and matches by project id string
        params["project"] = criteria.project_id

    path = f"/workspaces/{client.workspace_id}/user/{user_id}/time-entries"
    all_items: List[Dict[str, Any]] = []

    for page in range(1, max_pages + 1):
        params["page"] = page
        status, data = client.request_json("GET", path, params=params)
        if status != 200:
            return [], {"query": params, "status": status, "response": data}
        if not isinstance(data, list):
            return [], {"query": params, "status": status, "response": data, "error": "Unexpected response type"}
        all_items.extend(data)
        if len(data) < page_size:
            break

    # Apply exact filters client-side too (server filters can be substring-ish)
    matches = []
    for e in all_items:
        if criteria.project_id and e.get("projectId") != criteria.project_id:
            continue
        if criteria.description:
            q = _norm(criteria.description)
            if q and q not in _norm(e.get("description") or ""):
                continue
        matches.append(e)

    debug = {"query": params, "fetched": len(all_items), "filtered": len(matches)}
    return matches, debug


def pick_best_match(matches: List[Dict[str, Any]], criteria: EntryCriteria) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """Pick best entry by scoring; return (best, ordered_candidates)."""
    if not matches:
        return None, []
    scored = [(score_entry(e, criteria), e) for e in matches]
    scored.sort(key=lambda x: x[0], reverse=True)
    best_score = scored[0][0]
    # Candidates with same score
    top = [e for s, e in scored if s == best_score]
    return (top[0] if len(top) == 1 else None), top
