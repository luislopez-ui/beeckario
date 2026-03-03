from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone, timedelta
from threading import Lock
from typing import Any, Dict, Optional, Tuple, Callable, List

import httpx

from .project_lookup import (
    resolve_project_id,
    projects_by_client,
    list_clients,
    list_projects,
    find_project_by_id,
    normalize_project_code,
    ProjectMatch,
)
from .time_entry_lookup import EntryCriteria, parse_spanish_date, parse_time_range, find_time_entries, pick_best_match


try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

try:
    # Package-relative imports (preferred)
    from .flows.crear_registro import crear_registro
    from .flows.modificar_registro import modificar_registro
    from .flows.eliminar_registro import eliminar_registro
except Exception:  # pragma: no cover
    # Fallback for running from project root where 'funciones' is a top-level package
    from funciones.clockify.flows.crear_registro import crear_registro
    from funciones.clockify.flows.modificar_registro import modificar_registro
    from funciones.clockify.flows.eliminar_registro import eliminar_registro



# --- Config / client ----------------------------------------------------------

@dataclass(frozen=True)
class ClockifyConfig:
    """Runtime configuration for Clockify API calls."""

    api_key: str
    workspace_id: str
    base_url: str = "https://api.clockify.me/api/v1"
    timeout_s: float = 30.0
    timezone: str = "America/Mexico_City"  # default for this project

    # Always apply this tag (default: Arquitectura). Must be a 24-hex Clockify tag id.
    default_tag_id: str = "61f0377393930f642ee65f80"

    # Template for the final description. Available placeholders:
    #   {cliente}/{client}, {proyecto}/{project}, {actividad}/{activity}
    description_template: str = "{cliente} | {proyecto} | {actividad}"

    # Optional: if you already know the userId, set it to skip /v1/user.
    user_id: Optional[str] = None

    @staticmethod
    def from_env() -> "ClockifyConfig":
        api_key = (os.getenv("CLOCKIFY_API_KEY") or "").strip()
        workspace_id = (os.getenv("CLOCKIFY_WORKSPACE_ID") or "").strip()
        base_url = (os.getenv("CLOCKIFY_BASE_URL") or "https://api.clockify.me/api/v1").strip()
        timeout_s = float(os.getenv("CLOCKIFY_TIMEOUT_S") or "30")
        timezone = (os.getenv("CLOCKIFY_TIMEZONE") or "America/Mexico_City").strip()

        default_tag_id = (os.getenv("CLOCKIFY_DEFAULT_TAG_ID") or "61f0377393930f642ee65f80").strip()
        description_template = (os.getenv("CLOCKIFY_DESCRIPTION_TEMPLATE") or "{cliente} | {proyecto} | {actividad}").strip()

        user_id = (os.getenv("CLOCKIFY_USER_ID") or "").strip() or None
        if not api_key:
            raise RuntimeError("CLOCKIFY_API_KEY no está configurado en .env")
        if not workspace_id:
            raise RuntimeError("CLOCKIFY_WORKSPACE_ID no está configurado en .env")
        return ClockifyConfig(
            api_key=api_key,
            workspace_id=workspace_id,
            base_url=base_url.rstrip("/"),
            timeout_s=timeout_s,
            timezone=timezone,
            default_tag_id=default_tag_id,
            description_template=description_template,
            user_id=user_id,
        )


class ClockifyClient:

    """Minimal Clockify v1 REST client.

    Auth: send X-Api-Key header.
    Base URL varies by region/subdomain; configurable through CLOCKIFY_BASE_URL.
    """

    def __init__(self, config: ClockifyConfig):
        self.config = config
        self.workspace_id = config.workspace_id
        self._client = httpx.Client(
            base_url=config.base_url,
            timeout=config.timeout_s,
            headers={
                "X-Api-Key": config.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

        # Cache for /v1/user response
        self._current_user_id: Optional[str] = None

    def get_current_user_id(self) -> Tuple[int, Optional[str], Any]:
        """Return the authenticated user's id (cached).

        Uses GET /v1/user. See Clockify API docs.
        """
        # If the workspace/user is fixed for this app, allow configuring it
        # explicitly so we can avoid an extra network call.
        if self.config.user_id:
            self._current_user_id = self.config.user_id
            return 200, self._current_user_id, None

        if self._current_user_id:
            return 200, self._current_user_id, None
        status, data = self.request_json("GET", "/user")
        if status != 200:
            return status, None, data
        if isinstance(data, dict) and isinstance(data.get("id"), str):
            self._current_user_id = data["id"]
            return status, self._current_user_id, data
        return status, None, data


    def close(self) -> None:
        self._client.close()

    def request_json(self, method: str, path: str, json_body: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None) -> Tuple[int, Any]:
        try:
            resp = self._client.request(method, path, json=json_body, params=params)
        except Exception as e:
            return 0, {"error": str(e)}

        # Some endpoints (DELETE) may return 204 with empty body
        if resp.status_code == 204:
            return 204, None

        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}

        return resp.status_code, data


# --- Request parsing ----------------------------------------------------------

_ACTION_CREATE = {"crear", "crea", "agregar", "añadir", "nuevo", "registrar"}
_ACTION_UPDATE = {"modificar", "modifica", "editar", "edita", "actualizar", "actualiza", "cambiar", "cambia"}
_ACTION_DELETE = {"eliminar", "elimina", "borrar", "borra", "quitar", "quita"}
_ACTION_SEARCH = {"buscar", "busca", "listar", "lista", "consultar", "consulta", "muéstrame", "muestrame"}


def _detect_action(text: str) -> Optional[str]:
    t = (text or "").lower()
    # Directory listing: "lista los proyectos", "proyectos del directorio", etc.
    if re.search(r"\b(lista|listar|mu[eé]strame|consulta|consultar)\b.*\bproyectos?\b", t) or (
        "directorio" in t and ("proyecto" in t or "proyectos" in t)
    ):
        return "listar_proyectos"
    # Search/list should win if explicitly requested
    if any(w in t for w in _ACTION_SEARCH):
        return "buscar"
    if any(w in t for w in _ACTION_DELETE):
        return "eliminar"
    if any(w in t for w in _ACTION_UPDATE):
        return "modificar"
    if any(w in t for w in _ACTION_CREATE):
        return "crear"
    return None


_KV_KEY_RE = re.compile(r"(?P<k>[a-zA-Z_áéíóúñÁÉÍÓÚÑ]+)\s*(?:=|:)")


def _parse_bool(v: Any) -> bool:
    """Parse truthy/falsey values coming from loose text.

    Users often write parameters inline separated by spaces, e.g.
    `billable=true end=...`. Even with a robust KV parser, values can still
    occasionally contain trailing text. We therefore only consider the first
    token.
    """
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    s = str(v).strip().lower()
    if not s:
        return False
    # keep only the first token (split on whitespace and common separators)
    token = re.split(r"[\s;,]+", s, maxsplit=1)[0].strip()
    truthy = {"1", "true", "t", "yes", "y", "si", "sí", "on", "facturable"}
    falsey = {"0", "false", "f", "no", "n", "off", "nofacturable", "no_facturable"}
    if token in truthy:
        return True
    if token in falsey:
        return False
    # Fallback: interpret common prefixes
    if token.startswith("tru"):
        return True
    if token.startswith("fal"):
        return False
    return False


def _strip_wrapping_quotes(v: str) -> str:
    vv = (v or "").strip()
    if len(vv) >= 2:
        pairs = [("\"", "\""), ("'", "'"), ("“", "”"), ("‘", "’")]
        for a, b in pairs:
            if vv.startswith(a) and vv.endswith(b):
                return vv[1:-1].strip()
    return vv


def _api_error_text(status: int, resp: Any) -> str:
    if status == 0:
        if isinstance(resp, dict) and resp.get("error"):
            return str(resp.get("error"))
        return "No pude conectar con Clockify (error de red)."
    if isinstance(resp, dict):
        core = resp.get("message") or resp.get("error") or resp.get("reason")
        code = resp.get("code")
        if core and code is not None:
            return f"{core} (code {code})"
        if core:
            return str(core)
    return f"Clockify API respondió HTTP {status}."


def _parse_kv(text: str) -> Dict[str, str]:
    """Parsea pares key=value o key: value de forma robusta.

    Problema clásico que resolvemos aquí:
    - Mensajes como: `start=2025-12-23 09:00 end=2025-12-23 10:00 description=...`
      antes se interpretaban como si `start` incluyera TODO lo siguiente.

    Estrategia:
    - Encontrar todas las posiciones de `key=`/`key:`
    - El valor de cada key es el texto entre esa posición y la siguiente key.
    """
    t = text or ""
    out: Dict[str, str] = {}
    matches = list(_KV_KEY_RE.finditer(t))
    if not matches:
        return out

    for i, m in enumerate(matches):
        k = (m.group("k") or "").strip().lower()
        if not k:
            continue
        v_start = m.end()
        v_end = matches[i + 1].start() if i + 1 < len(matches) else len(t)
        v_raw = t[v_start:v_end]
        # Trim common separators and whitespace
        v = v_raw.strip().strip(";,")
        v = v.strip(" \t\r\n;,")
        v = _strip_wrapping_quotes(v)
        if v:
            out[k] = v
    return out


def _normalize_keys(raw: Dict[str, str]) -> Dict[str, Any]:
    # Spanish-friendly aliases
    mapping = {
        "descripcion": "description",
        "descripción": "description",
        "desc": "description",
        "proyecto": "projectId",
        "proyectoid": "projectId",
        "project": "projectId",
        "projectid": "projectId",
        # For matching existing entries (modify/delete) without requiring time entry id:
        "proyecto_actual": "matchProject",
        "proyectoactual": "matchProject",
        "proyecto_origen": "matchProject",
        "proyectoorigen": "matchProject",
        "project_actual": "matchProject",
        "matchproject": "matchProject",
        "descripcion_actual": "matchDescription",
        "descripcionactual": "matchDescription",
        "matchdescription": "matchDescription",
        "fecha": "matchDate",
        "dia": "matchDate",
        "día": "matchDate",
        "horario": "matchTimeRange",
        "rango": "matchTimeRange",

        "tarea": "taskId",
        "task": "taskId",
        "taskid": "taskId",
        "etiquetas": "tagIds",
        "tags": "tagIds",
        "tagids": "tagIds",
        "inicio": "start",
        "start": "start",
        "fin": "end",
        "end": "end",
        "id": "id",
        "timeentryid": "id",
        "registro": "id",
        "billable": "billable",
        "facturable": "billable",
        "tipo": "type",
        "type": "type",
        "workspace": "workspaceId",
        "workspaceid": "workspaceId",
    }

    out: Dict[str, Any] = {}
    for k, v in (raw or {}).items():
        nk = mapping.get(k, k)
        out[nk] = v

    # Filtra claves desconocidas para evitar que texto accidental (ej. "error:")
    # termine enviándose al API.
    allowed = {
        "description",
        "projectId",
        "taskId",
        "tagIds",
        "start",
        "end",
        "billable",
        "type",
        "id",
        "workspaceId",
        # criterios de búsqueda/match
        "matchProject",
        "matchDescription",
        "matchDate",
        "matchTimeRange",
    }
    out = {k: v for k, v in out.items() if k in allowed}

    # booleans
    if "billable" in out:
        out["billable"] = _parse_bool(out["billable"])

    # tagIds can be comma-separated
    if "tagIds" in out and isinstance(out["tagIds"], str):
        out["tagIds"] = [x.strip() for x in out["tagIds"].split(",") if x.strip()]

    return out


_DT_ISO_LIKE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?([zZ]|[+-]\d{2}:?\d{2})?$")
_DT_YMD_HM = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(:(\d{2}))?$")

_TIME_ONLY_RE = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$", flags=re.IGNORECASE)


def _parse_time_token(value: str) -> Optional[Tuple[int, int]]:
    """Parse a time token like '14:00', '2', '2pm', '2 pm'.

    Returns (hour24, minute) or None.
    """
    v = (value or "").strip()
    if not v:
        return None
    m = _TIME_ONLY_RE.match(v)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2) or 0)
    ap = (m.group(3) or "").lower().strip() or None
    if ap == "am":
        hh = 0 if hh == 12 else hh
    elif ap == "pm":
        hh = hh if hh == 12 else hh + 12
    # Basic guardrails
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        return None
    return hh, mm


def _coerce_time_only_start_end(payload: Dict[str, Any], user_text: str, cfg: "ClockifyConfig", trace: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Coerce start/end values like '14:00' into 'YYYY-MM-DD HH:MM'.

    This fixes a common pattern produced by upstream planners:
      fecha=2026-02-19 inicio=14:00 fin=15:00

    Without coercion, _to_clockify_dt() rejects time-only values.
    """
    p = dict(payload or {})

    def _t(msg: str) -> None:
        if trace is None:
            return
        try:
            trace(msg)
        except Exception:
            pass

    raw_s = str(p.get("start") or "").strip()
    raw_e = str(p.get("end") or "").strip()
    if not raw_s or not raw_e:
        return p

    # If they are already full datetimes, do nothing.
    if re.search(r"\d{4}-\d{2}-\d{2}", raw_s) and re.search(r"\d{4}-\d{2}-\d{2}", raw_e):
        return p

    s_tok = _parse_time_token(raw_s)
    e_tok = _parse_time_token(raw_e)
    if not s_tok or not e_tok:
        return p

    base = parse_spanish_date(user_text) or datetime.now(tz=_tz(cfg.timezone)).replace(tzinfo=None)
    s_dt = base.replace(hour=s_tok[0], minute=s_tok[1], second=0, microsecond=0)
    e_dt = base.replace(hour=e_tok[0], minute=e_tok[1], second=0, microsecond=0)
    if e_dt <= s_dt:
        e_dt = e_dt + timedelta(days=1)

    p["start"] = s_dt.strftime("%Y-%m-%d %H:%M")
    p["end"] = e_dt.strftime("%Y-%m-%d %H:%M")
    _t(f"Normalizando horas (time-only -> datetime): start={raw_s!r} end={raw_e!r} -> {p['start']} – {p['end']}")
    return p


def _to_clockify_dt(value: str, tz_name: str) -> str:
    """Return yyyy-MM-ddTHH:MM:SSZ in UTC.

    Accepts:
      - ISO strings with Z / offset
      - 'YYYY-MM-DD HH:MM[:SS]' assumed local tz
      - 'YYYY-MM-DD' assumed 00:00 local
      - 'ahora'/'now'

    Clockify expects 'yyyy-MM-ddThh:mm:ssZ'.
    """
    v = (value or "").strip()
    if not v:
        raise ValueError("datetime vacío")

    if v.lower() in ("now", "ahora"):
        dt = datetime.now(tz=_tz(tz_name))
        return dt.astimezone(_tz("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- Formatos comunes en español (dd/mm/yyyy, etc.) ---------------------
    # Ej: 23/12/2025 09:00, 23-12-2025 09:00, 23/12 09:00
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?$", v)
    if m:
        d, mo, y_raw, hh, mm, ss = m.groups()
        year = int(y_raw) if y_raw else datetime.now(tz=_tz(tz_name)).year
        if year < 100:
            year += 2000
        dt = datetime(year, int(mo), int(d), int(hh), int(mm), int(ss or 0), tzinfo=_tz(tz_name))
        return dt.astimezone(_tz("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")

    m = re.match(r"^(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?$", v)
    if m:
        d, mo, y_raw = m.groups()
        year = int(y_raw) if y_raw else datetime.now(tz=_tz(tz_name)).year
        if year < 100:
            year += 2000
        dt = datetime(year, int(mo), int(d), 0, 0, 0, tzinfo=_tz(tz_name))
        return dt.astimezone(_tz("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Ej: "26 de diciembre de 2025 15:00"
    m = re.match(
        r"^(\d{1,2})\s+de\s+([a-zñáéíóú]+)\s+de\s+(\d{4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$",
        v,
        flags=re.IGNORECASE,
    )
    if m:
        # Reutiliza el parser existente (sin hora) y luego aplica hora si se dio.
        base = parse_spanish_date(f"{m.group(1)} de {m.group(2)} de {m.group(3)}")
        if base:
            hh = int(m.group(4) or 0)
            mm = int(m.group(5) or 0)
            ss = int(m.group(6) or 0)
            dt = base.replace(hour=hh, minute=mm, second=ss, microsecond=0, tzinfo=_tz(tz_name))
            return dt.astimezone(_tz("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")

    # --- ISO / YYYY-MM-DD HH:MM -------------------------------------------
    if not _DT_ISO_LIKE.match(v):
        raise ValueError(
            f"Formato de fecha/hora no reconocido: {value!r}. "
            "Acepto ISO (2025-12-28T10:00:00-06:00 / ...Z), "
            "YYYY-MM-DD HH:MM, o dd/mm/yyyy HH:MM."
        )

    # ISO with Z
    if v.endswith("Z") or v.endswith("z"):
        dt = datetime.fromisoformat(v[:-1] + "+00:00")
        return dt.astimezone(_tz("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ISO with offset
    if re.search(r"[+-]\d{2}:?\d{2}$", v):
        vv = v
        # handle +0000 form
        if re.search(r"[+-]\d{4}$", vv):
            vv = vv[:-5] + vv[-5:-2] + ":" + vv[-2:]
        dt = datetime.fromisoformat(vv)
        return dt.astimezone(_tz("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")

    # naive datetime
    m = _DT_YMD_HM.match(v)
    if m:
        date_part = m.group(1)
        time_part = m.group(2)
        sec = m.group(4) or "00"
        dt = datetime.fromisoformat(f"{date_part}T{time_part}:{sec}")
        dt = dt.replace(tzinfo=_tz(tz_name))
        return dt.astimezone(_tz("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")

    # date only
    dt = datetime.fromisoformat(v)
    dt = dt.replace(tzinfo=_tz(tz_name))
    return dt.astimezone(_tz("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tz(name: str):
    """Return a tzinfo object.

    IMPORTANT: On Windows/py>=3.9, zoneinfo may not include a 'UTC' key.
    Using datetime.timezone.utc avoids crashes like:
      ZoneInfoNotFoundError: 'No time zone found with key UTC'
    """
    n = (name or '').strip()
    if n.upper() in ('CDMX', 'MEXICO_CITY', 'MEXICO CITY'):
        n = 'America/Mexico_City'
    if not n:
        return dt_timezone.utc

    # Normalize common UTC aliases.
    if n.upper() in ('UTC', 'Z'):
        if ZoneInfo is not None:
            for key in ('Etc/UTC', 'UTC'):
                try:
                    return ZoneInfo(key)
                except Exception:
                    pass
        return dt_timezone.utc

    # Support fixed-offset syntaxes (handy when tzdata isn't available):
    #   - "+02:00" / "-0600"
    #   - "UTC-06" / "UTC-06:00"
    m = re.match(r"^(?:UTC)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", n, flags=re.IGNORECASE)
    if m:
        sign, hh, mm = m.groups()
        hours = int(hh)
        minutes = int(mm or 0)
        delta = timedelta(hours=hours, minutes=minutes)
        if sign == '-':
            delta = -delta
        return dt_timezone(delta)

    if ZoneInfo is None:
        # Pragmatic fallback for CDMX when tzdata is missing (common on Windows).
        if n == 'America/Mexico_City':
            return dt_timezone(timedelta(hours=-6))
        return dt_timezone.utc

    try:
        return ZoneInfo(n)
    except Exception:
        # Pragmatic fallback for CDMX when tzdata is missing.
        if n == 'America/Mexico_City':
            return dt_timezone(timedelta(hours=-6))
        # Fallback seguro: UTC (no rompemos por tzdata faltante).
        return dt_timezone.utc


# Serialize Clockify operations to avoid interleaving when multiple actions are
# requested in one message (or multiple UI requests arrive concurrently).
_CLOCKIFY_LOCK = Lock()


def _split_batch_requests(text: str) -> list[str]:
    """Split a single user message into multiple Clockify tasks.

    Heuristics are conservative: we only split when we see clear separators
    (numbered/bulleted list, or connectors like "y luego" before a new action).
    """
    t = (text or '').strip()
    if not t:
        return []

    def _has_action(s: str) -> bool:
        return _detect_action(s) is not None

    # Normalize newlines
    norm = t.replace('\r\n', '\n').replace('\r', '\n')
    lines = [ln.strip() for ln in norm.split('\n') if ln.strip()]

    # 1) Numbered/bulleted list (recommended by design)
    extracted: list[str] = []
    for ln in lines:
        m = re.match(r"^(?:\d+\s*[\).\]]\s+|[-•*]\s+)(.+)$", ln)
        extracted.append((m.group(1) if m else ln).strip())

    # Only treat as batch if we actually have multiple action lines.
    if len(extracted) >= 2 and sum(1 for x in extracted if _has_action(x)) >= 2:
        return [x for x in extracted if x]

    # 2) Single line with explicit connectors, e.g. "crea ... y luego modifica ..."
    # Split only when a connector is followed by a new action keyword.
    connector_split = re.split(
        r"\s+(?:y\s+luego|y\s+despu[eé]s|despu[eé]s|luego)\s+(?=(?:crea(?:r)?|registra(?:r)?|modifica(?:r)?|edita(?:r)?|actualiza(?:r)?|elimina(?:r)?|borra(?:r)?|buscar|busca|listar|lista)\b)",
        norm,
        flags=re.IGNORECASE,
    )
    connector_split = [p.strip() for p in connector_split if p.strip()]
    if len(connector_split) >= 2 and sum(1 for x in connector_split if _has_action(x)) >= 2:
        return connector_split

    # 3) Semicolon separated commands (power users)
    semi = [p.strip() for p in re.split(r";\s*(?=(?:crea(?:r)?|registra(?:r)?|modifica(?:r)?|elimina(?:r)?|borra(?:r)?|buscar|listar)\b)", norm, flags=re.IGNORECASE) if p.strip()]
    if len(semi) >= 2 and sum(1 for x in semi if _has_action(x)) >= 2:
        return semi

    return [t]



def _maybe_parse_json_payload(text: str) -> Optional[Dict[str, Any]]:
    t = (text or "").strip()
    if not t:
        return None
    # very naive: try find a JSON object in the message
    if "{" not in t or "}" not in t:
        return None
    start = t.find("{")
    end = t.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    blob = t[start : end + 1]
    try:
        obj = json.loads(blob)
        if isinstance(obj, dict):
            return obj
    except Exception:
        return None
    return None




def _maybe_resolve_project(payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """If payload contains projectId but it's a name, resolve it using directorios/clockify_proyectos.xlsx.

    Returns:
      (updated_payload, error_dict)
    """
    if not payload or "projectId" not in payload:
        return payload, None

    raw = payload.get("projectId")
    if raw is None:
        return payload, None

    raw_s = str(raw).strip()
    if not raw_s:
        payload.pop("projectId", None)
        return payload, None

    try:
        match, candidates = resolve_project_id(raw_s)
    except Exception as e:
        return None, {
            "ok": False,
            "error": f"Error leyendo clockify_proyectos.xlsx: {e}",
            "provided": raw_s,
        }
    if match:
        payload["projectId"] = match.project_id
        return payload, None

    if candidates:
        return None, {
            "ok": False,
            "error": "Proyecto ambiguo. Especifica mejor el nombre o usa el projectId directamente.",
            "provided": raw_s,
            "candidates": [{"name": c.project_name, "id": c.project_id, "match": c.match_type} for c in candidates],
        }

    return None, {
        "ok": False,
        "error": "No encontré el proyecto en directorios/clockify_proyectos.xlsx. Usa un nombre exacto o el projectId.",
        "provided": raw_s,
    }


# --- Orchestrator -------------------------------------------------------------



# --- Natural language helpers for modify/delete ----------------------------

_PROJECT_QUOTED_RE = re.compile(r'proyecto\s+["“](.+?)["”]', re.IGNORECASE)
_DESC_QUOTED_RE = re.compile(r'(?:llamado|llamada|descripci[oó]n)\s+["“](.+?)["”]', re.IGNORECASE)

def _explicit_update_fields(txt: str) -> set[str]:
    """Heurística conservadora: qué campos quiere *cambiar* el usuario en una modificación.

    Importante: para evitar efectos secundarios, NO actualizamos campos (especialmente `description`)
    si el usuario no lo pidió explícitamente.
    """
    t = txt or ""
    fields: set[str] = set()

    # Descripción
    if re.search(r"(descripci[oó]n|description)\s*[:=]", t, flags=re.IGNORECASE) or re.search(
        r"(la\s+descripci[oó]n\s+es|cambia(?:r)?|modifica(?:r)?|actualiza(?:r)?|pon(?:er)?|asigna(?:r)?|setea(?:r)?)\s*.{0,40}(descripci[oó]n|description)",
        t,
        flags=re.IGNORECASE,
    ):
        fields.add("description")

    # Horario (start/end)
    if re.search(r"(start|end|inicio|fin)\s*[:=]", t, flags=re.IGNORECASE):
        fields.update({"start", "end"})
    elif re.search(r"(cambia|ajusta|mueve|reprograma)\s*.{0,40}(horario|hora|inicio|fin)", t, flags=re.IGNORECASE) and re.search(
        r"\d{1,2}:\d{2}|\d{1,2}\s*(?:am|pm)",
        t,
        flags=re.IGNORECASE,
    ):
        fields.update({"start", "end"})

    # Facturable
    if re.search(r"(no\s+facturable|facturable|billable)", t, flags=re.IGNORECASE) or re.search(
        r"(billable|facturable)\s*[:=]",
        t,
        flags=re.IGNORECASE,
    ):
        fields.add("billable")

    # Proyecto (solo si se pide cambiar)
    if re.search(r"(proyecto|project)\s*[:=]", t, flags=re.IGNORECASE) or re.search(
        r"(cambiar|cambia|modificar|modifica|mover|mueve|pasar|pasa)\s*.{0,30}proyecto",
        t,
        flags=re.IGNORECASE,
    ):
        fields.add("projectId")

    # Tags
    if re.search(r"(tag|tags|etiqueta|etiquetas)", t, flags=re.IGNORECASE) or re.search(
        r"(arq|arquitectura)",
        t,
        flags=re.IGNORECASE,
    ) or re.search(r"hora\s*extra|overtime", t, flags=re.IGNORECASE):
        fields.add("tagIds")

    # Task
    if re.search(r"(tarea|task)", t, flags=re.IGNORECASE) or re.search(r"(taskid|tarea)\s*[:=]", t, flags=re.IGNORECASE):
        fields.add("taskId")

    # Type
    if re.search(r"(tipo|type)", t, flags=re.IGNORECASE) or re.search(r"(type|tipo)\s*[:=]", t, flags=re.IGNORECASE):
        fields.add("type")

    return fields


def _infer_tag_additions(txt: str, cfg: ClockifyConfig) -> list[str]:
    """Infiera tags a *agregar* en una modificación.

    Importante: en Clockify, mandar `tagIds: []` en PUT puede borrar tags
    existentes. Para solicitudes como "agrega el tag de horas extras",
    necesitamos *mergear* los tags actuales del registro con los tags nuevos.

    Esta función devuelve SOLO los IDs a agregar. El merge se realiza en
    `flows/modificar_registro.py`.
    """
    t = (txt or "")
    add: list[str] = []

    # Arquitectura / ARQ
    if re.search(r"\b(arq|arquitectura)\b", t, flags=re.IGNORECASE):
        if cfg.default_tag_id:
            add.append(cfg.default_tag_id)

    # Horas extras
    if re.search(r"\bhora\s*extra\b|\bhoras\s*extras\b|\bovertime\b", t, flags=re.IGNORECASE):
        if cfg.default_tag_id:
            add.append(cfg.default_tag_id)
        add.append(OVERTIME_TAG_ID)

    return _uniq(add)



def _extract_match_updates_from_text(user_text: str, payload: Dict[str, Any], action: str, cfg: ClockifyConfig) -> Tuple[EntryCriteria, Dict[str, Any], Optional[str]]:
    """Best-effort extraction of match criteria (existing entry) and updates (new values)."""
    txt = user_text or ""

    # --- Match date / time range
    base_date = None
    explicit_date = None
    if isinstance(payload.get("matchDate"), str):
        explicit_date = parse_spanish_date(payload["matchDate"])
    if explicit_date is None:
        explicit_date = parse_spanish_date(txt)

    # If the user did not mention any date at all, we will still need a base date
    # for parsing times. We'll default to "today" but later we may widen the
    # search window (lookback) to find the entry ID automatically.
    base_date = explicit_date
    if base_date is None:
        base_date = datetime.now(tz=_tz(cfg.timezone)).replace(tzinfo=None)

    # Build default day range if we have a date
    match_start_dt = None
    match_end_dt = None
    if base_date:
        match_start_dt = base_date.replace(hour=0, minute=0, second=0, microsecond=0)
        match_end_dt = base_date.replace(hour=23, minute=59, second=59, microsecond=0)

    # If a time range is provided, refine
    tr_raw = None
    if isinstance(payload.get("matchTimeRange"), str):
        tr_raw = payload["matchTimeRange"]
    else:
        # Attempt to detect "15:00-17:00" etc. in free text
        tr_raw = txt

    time_range_was_explicit = False
    if base_date:
        ts, te = parse_time_range(tr_raw, base_date)
        if ts and te:
            match_start_dt, match_end_dt = ts, te
            time_range_was_explicit = True

    # If the user did NOT mention a date/time range, widen the search window.
    # This is crucial to avoid asking the user for a time-entry ID.
    # Example: "modificar el registro del MIT.002" (no date/time) should still work.
    if explicit_date is None and not time_range_was_explicit and action in ("modificar", "eliminar", "buscar"):
        lookback_days = int(os.getenv("CLOCKIFY_LOOKBACK_DAYS") or "7")
        now_local = datetime.now(tz=_tz(cfg.timezone)).replace(tzinfo=None)
        lb = (now_local - timedelta(days=max(1, lookback_days))).replace(hour=0, minute=0, second=0, microsecond=0)
        ub = now_local.replace(hour=23, minute=59, second=59, microsecond=0)
        match_start_dt = lb
        match_end_dt = ub

    # Convert to ISO Z strings using the same function used for payload dates
    match_start = _to_clockify_dt(match_start_dt.strftime("%Y-%m-%d %H:%M"), cfg.timezone) if match_start_dt else None
    match_end = _to_clockify_dt(match_end_dt.strftime("%Y-%m-%d %H:%M"), cfg.timezone) if match_end_dt else None

    # --- Match description
    match_desc = None
    if isinstance(payload.get("matchDescription"), str):
        match_desc = payload["matchDescription"].strip() or None
    if match_desc is None:
        m = _DESC_QUOTED_RE.search(txt)
        if m:
            match_desc = m.group(1).strip()
        else:
            m2 = re.search(r"\bllamad[oa]\s+([^\n,.;]+)", txt, flags=re.IGNORECASE)
            if m2:
                match_desc = m2.group(1).strip(' "\'')

    # --- Match project (current)
    match_project_name = None
    if isinstance(payload.get("matchProject"), str):
        match_project_name = payload["matchProject"].strip() or None
    if match_project_name is None:
        # Prefer "cargado al proyecto X" / "en el proyecto X"
        m = re.search(r"(?:cargad[oa]\s+al\s+proyecto|en\s+el\s+proyecto)\s+([^\n,.;]+)", txt, flags=re.IGNORECASE)
        if m:
            match_project_name = m.group(1).strip(" \"'")
        else:
            # Handle: "modificar el registro del MIT.002" / "el registro de AER.MCC.004"
            m = re.search(
                r"\bregistro\s+(?:del|de)\s+("
                r"[A-Za-z]{2,6}\s*[\.\-]?\s*[A-Za-z]{2,6}\s*[\.\-]?\s*\d{2,4}"
                r"|[A-Za-z]{2,6}\s*[\.\-]?\s*\d{2,4}"
                r")\b",
                txt,
                flags=re.IGNORECASE,
            )
            if m:
                match_project_name = normalize_project_code(m.group(1))
            else:
                # Also handle unquoted project codes: "proyecto NYB.045" / "del proyecto AER.MCC.004"
                m = re.search(
                    r"\b(?:del\s+proyecto|para\s+el\s+proyecto|proyecto)\s+("
                    r"[A-Za-z]{2,6}\s*[\.\-]?\s*[A-Za-z]{2,6}\s*[\.\-]?\s*\d{2,4}"
                    r"|[A-Za-z]{2,6}\s*[\.\-]?\s*\d{2,4}"
                    r")\b",
                    txt,
                    flags=re.IGNORECASE,
                )
                if m:
                    match_project_name = normalize_project_code(m.group(1))
                else:
                    m = _PROJECT_QUOTED_RE.search(txt)
                    if m:
                        match_project_name = m.group(1).strip()

    match_project_id = None
    if match_project_name:
        mproj, cands = resolve_project_id(match_project_name)
        if mproj:
            match_project_id = mproj.project_id
        elif cands:
            return (
                EntryCriteria(start=match_start, end=match_end, description=match_desc, project_id=None),
                {},
                "Proyecto a buscar ambiguo. Especifica mejor el nombre.",
            )
        else:
            return (
                EntryCriteria(start=match_start, end=match_end, description=match_desc, project_id=None),
                {},
                "No encontré el proyecto a buscar en directorios/clockify_proyectos.xlsx.",
            )
    # --- Updates
    updates: Dict[str, Any] = {}

    # Para modificar, NO debemos actualizar campos que el usuario no pidió explícitamente.
    explicit = _explicit_update_fields(txt) if action == "modificar" else set()

    # Existing parsed payload already includes normalized keys (description/billable/projectId etc.)
    for k in ("description", "billable", "start", "end", "projectId", "tagIds", "taskId", "type"):
        if k in payload:
            if action != "modificar" or k in explicit:
                updates[k] = payload[k]

    # Nota: para modificaciones, NO forzamos `tagIds=[]` cuando el usuario menciona tags.
    # Eso termina borrando tags existentes y puede provocar 403/validaciones en Clockify.
    # Las operaciones de tags (agregar horas extra, etc.) se resuelven más adelante
    # haciendo *merge* con los tags actuales del time entry.

    if action == "modificar":
        # Detect billable from natural language if not explicit
        if "billable" not in updates:
            if re.search(r"\bno\s+facturable\b", txt, flags=re.IGNORECASE):
                updates["billable"] = False
            elif re.search(r"\bfacturable\b", txt, flags=re.IGNORECASE):
                updates["billable"] = True

        # Detect new project target ("cambiar ... a proyecto X")
        if "projectId" not in updates:
            m = re.search(r"(?:cambiar|modificar)\s+(?:el\s+)?proyecto\s+(?:a|por)\s+([^\n,.;]+)", txt, flags=re.IGNORECASE)
            if m:
                updates["projectId"] = m.group(1).strip(' "\'')

    # Resolve update projectId if user provided project name
    if isinstance(updates.get("projectId"), str):
        proj_raw = str(updates["projectId"]).strip()
        mproj, cands = resolve_project_id(proj_raw)
        if mproj:
            updates["projectId"] = mproj.project_id
        elif cands:
            return (
                EntryCriteria(start=match_start, end=match_end, description=match_desc, project_id=match_project_id),
                {},
                "Proyecto destino ambiguo. Especifica mejor el nombre.",
            )
        else:
            return (
                EntryCriteria(start=match_start, end=match_end, description=match_desc, project_id=match_project_id),
                {},
                "No encontré el proyecto destino en directorios/clockify_proyectos.xlsx.",
            )

    criteria = EntryCriteria(start=match_start, end=match_end, description=match_desc, project_id=match_project_id)
    return criteria, updates, None


class _SafeFormatDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _format_description_template(template: str, cliente: str, proyecto: str, actividad: str) -> str:
    tpl = (template or "").strip() or "{cliente} | {proyecto} | {actividad}"
    data = _SafeFormatDict(
        {
            "cliente": cliente or "",
            "client": cliente or "",
            "proyecto": proyecto or "",
            "project": proyecto or "",
            "actividad": actividad or "",
            "activity": actividad or "",
        }
    )
    try:
        out = tpl.format_map(data)
    except Exception:
        # If template is invalid (unbalanced braces), fallback to default.
        out = "{cliente} | {proyecto} | {actividad}".format_map(data)
    # Cleanup: collapse spaces around pipes
    out = re.sub(r"\s*\|\s*", " | ", out).strip()
    out = re.sub(r"\s+", " ", out).strip()
    return out


def _looks_like_full_template(desc: str) -> bool:
    d = (desc or "").strip()
    return d.count("|") >= 2


def _norm_simple(s: str) -> str:
    import unicodedata as _ud
    s = (s or "").lower().strip()
    s = _ud.normalize("NFKD", s)
    s = "".join(c for c in s if not _ud.combining(c))
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _infer_client_from_text(txt: str) -> Optional[str]:
    """Try to infer a client name mentioned in the text, using the Excel directory."""
    t = _norm_simple(txt)
    if not t:
        return None
    try:
        clients = list_clients()
    except Exception:
        return None
    hits = []
    for c in clients:
        nc = _norm_simple(c)
        if nc and nc in t:
            hits.append(c)
    if len(hits) == 1:
        return hits[0]
    return None


def _extract_project_hint_from_text(txt: str) -> Optional[str]:
    """Extract a project code from free text.

    Supports:
      - 2-part codes:  NYB.045, 'nyb 045', 'NYB-045'
      - 3-part codes: AER.MCC.004, 'AER MCC 004', 'AER-MCC-004'

    Important: do NOT drop the first prefix (fix for AER.MCC.004 becoming MCC.004).
    """
    t = txt or ""

    # Code patterns (letters segments + numeric suffix)
    CODE3 = r"[A-Za-z]{2,6}\s*[\.\-]?\s*[A-Za-z]{2,6}\s*[\.\-]?\s*\d{2,4}"
    CODE2 = r"[A-Za-z]{2,6}\s*[\.\-]?\s*\d{2,4}"

    def _ok(code: str) -> bool:
        if not code:
            return False
        head = code.split(".", 1)[0].upper().strip()
        # Avoid interpreting time markers like 'am 10' / 'pm 10' as project codes
        return head not in {"AM", "PM"}

    # Prefer explicit "proyecto X" (try 3-part first)
    for rgx in (
        rf"\bproyecto\s+({CODE3})\b",
        rf"\bproyecto\s+({CODE2})\b",
        rf"\b(?:del|sobre|en)\s+proyecto\s+({CODE3})\b",
        rf"\b(?:del|sobre|en)\s+proyecto\s+({CODE2})\b",
    ):
        m = re.search(rgx, t, flags=re.IGNORECASE)
        if m:
            code = normalize_project_code(m.group(1))
            if _ok(code):
                return code or None

    # Generic codes anywhere (try 3-part first, then 2-part)
    for rgx in (CODE3, CODE2):
        for m in re.finditer(rf"\b({rgx})\b", t):
            code = normalize_project_code(m.group(1))
            if _ok(code):
                return code or None

    if re.search(r"\bcomida\b", t, flags=re.IGNORECASE):
        return "Comida"

    return None



def _extract_duration_minutes(txt: str) -> Optional[int]:
    t = _norm_simple(txt)
    if not t:
        return None
    # 5 minutos / 5 min
    m = re.search(r"\b(\d+)\s*(minuto|minutos|min)\b", t)
    if m:
        return int(m.group(1))
    # 2 horas / 2 hrs / 2h
    m = re.search(r"\b(\d+)\s*(hora|horas|hr|hrs|h)\b", t)
    if m:
        return int(m.group(1)) * 60
    # media hora
    if re.search(r"\bmedia\s+hora\b", t):
        return 30
    return None


def _extract_start_time(txt: str, base_date: datetime) -> Optional[datetime]:
    t = _norm_simple(txt)
    if not t:
        return None

    def _h24(h: int, ampm: Optional[str]) -> int:
        if not ampm:
            return h
        a = ampm.lower()
        if a == "am":
            return 0 if h == 12 else h
        if a == "pm":
            return h if h == 12 else h + 12
        return h

    # "a las 8 am", "de las 8:15", "a las 08:00"
    m = re.search(r"\b(?:a\s+las|a|de\s+las|de|desde\s+las|desde)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", t)
    if not m:
        # "8 am" at end: "... a las 8am"
        m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", t)
    if not m:
        return None

    hh = int(m.group(1))
    mm = int(m.group(2) or 0)
    ap = m.group(3)
    hh = _h24(hh, ap)
    return base_date.replace(hour=hh, minute=mm, second=0, microsecond=0)


def _infer_activity_from_text(txt: str) -> str:
    """Best-effort activity label (short) from free text."""
    t = _norm_simple(txt)

    if re.search(r"\bdaily\b", t):
        return "Daily"
    if re.search(r"\bsdd\b", t):
        return "Elaboración de SDD"
    if re.search(r"\bpdd\b", t):
        return "Revisión de PDD"
    if re.search(r"\bdiagrama(s)?\b", t):
        return "Diagramas"
    if re.search(r"\bincidente(s)?\b", t):
        return "Revisión de incidentes"
    if re.search(r"\bsoporte\b", t):
        return "Soporte"
    if re.search(r"\b(reunion|reunión|meeting)\b", t):
        return "Reunión"
    if re.search(r"\b(atencion|atención)\b", t) and re.search(r"\b(devs?|desarroll)\b", t):
        return "Atención a devs"

    # Try to capture "hice X", "realicé X", etc.
    m = re.search(r"\b(?:realice|realice|realicé|hice|trabaje|trabajé|estuve|atendi|atendí)\s+(.+?)(?:\s+(?:durante|por|de|en|para|sobre|del|al)\b|$)", t)
    if m:
        cand = m.group(1).strip(" .,:;")
        # Keep it shortish
        if len(cand) > 60:
            cand = cand[:60].rstrip() + "…"
        # Capitalize first letter
        return cand[:1].upper() + cand[1:] if cand else "Trabajo"

    return "Trabajo"


def _infer_stage_from_text(txt: str, activity: Optional[str] = None) -> str:
    """Infer the company "stage" (task category) from text.

    Stages supported by the Excel directory:
      - Discovery  -> column ID_Discovery
      - Desarrollo -> column ID_Desarrollo
      - Deployment -> column ID_Deployment
      - Preventa   -> columns Farming / Hunting

    Policy: if ambiguous or missing, default to **Desarrollo** (per user request).
    """
    t = _norm_simple((txt or "") + " " + (activity or ""))

    # Preventa
    if re.search(r"\bfarming\b", t):
        return "farming"
    if re.search(r"\bhunting\b", t):
        return "hunting"
    if re.search(r"\bpreventa\b", t):
        return "preventa"

    # Deployment
    if re.search(r"\bdeployment\b|\bdeploy\b|\bdespliegue\b|\brelease\b|\bproducci[oó]n\b|\bprod\b|\bpipeline\b", t):
        return "deployment"

    # Discovery
    if re.search(r"\bdiscovery\b|\ban[aá]lisis\b|\barquitect\b|\bdise[nñ]o\b|\bdiagram\w*\b|\bpdd\b|\bsdd\b|\brefinamiento\b", t):
        return "discovery"

    # Desarrollo
    if re.search(r"\bdesarrollo\b|\bdevelopment\b|\bdevs?\b|\bimplement\w*\b|\bbug\b|\bfix\b|\bincidente\b|\bsoporte\b", t):
        return "desarrollo"

    return "desarrollo"


def _project_has_any_task_ids(pm: Optional[ProjectMatch]) -> bool:
    if not pm:
        return False
    return bool(pm.id_discovery or pm.id_desarrollo or pm.id_deployment or pm.farming or pm.hunting)


def _available_stage_choices(pm: ProjectMatch) -> list[str]:
    out: list[str] = []
    if pm.id_discovery:
        out.append("discovery")
    if pm.id_desarrollo:
        out.append("desarrollo")
    if pm.id_deployment:
        out.append("deployment")
    if pm.farming:
        out.append("farming")
    if pm.hunting:
        out.append("hunting")
    return out


def _task_id_for_stage(pm: ProjectMatch, stage: str) -> Optional[str]:
    s = (stage or "").strip().lower()
    if s in {"discovery"}:
        return pm.id_discovery
    if s in {"deployment", "deploy"}:
        return pm.id_deployment
    if s in {"farming"}:
        return pm.farming
    if s in {"hunting"}:
        return pm.hunting
    if s in {"preventa"}:
        # If the user says "preventa" but doesn't specify farming/hunting, we cannot guess.
        if pm.farming and not pm.hunting:
            return pm.farming
        if pm.hunting and not pm.farming:
            return pm.hunting
        return None
    # Default (policy): desarrollo
    return pm.id_desarrollo


def _infer_create_payload_from_text(user_text: str, cfg: ClockifyConfig) -> Tuple[Dict[str, Any], Optional[str]]:
    """Infer a Clockify create payload from Spanish natural language.

    Returns:
      (payload, error_text)

    This function tries to extract:
      - start/end (either a range, or a start + duration)
      - project hint (projectId as code/name)
      - billable intent (if explicitly mentioned)
      - activity/description (short; can be later templated)
    """
    txt = user_text or ""

    # Date (if omitted, assume today)
    base_date = parse_spanish_date(txt) or datetime.now(tz=_tz(cfg.timezone)).replace(tzinfo=None)

    # Time range first
    ts, te = parse_time_range(txt, base_date)

    # If no explicit range, try start + duration (e.g. "daily de 5 minutos ... a las 8 am")
    if not ts or not te:
        start = _extract_start_time(txt, base_date)
        dur_min = _extract_duration_minutes(txt)
        if start and dur_min:
            ts = start
            te = start + timedelta(minutes=dur_min)

    if not ts or not te:
        return {}, "No pude identificar el horario. Indica un rango (ej. 'de 9 am a 10 am') o un inicio + duración (ej. 'a las 8 am por 5 minutos')."

    # Activity / description (short). If user provided a quoted description, use it.
    desc = None
    m = _DESC_QUOTED_RE.search(txt)
    if m:
        desc = m.group(1).strip()
    else:
        m2 = re.search(r"[\"“](.+?)[\"”]", txt)
        if m2:
            desc = m2.group(1).strip()
        else:
            # Use heuristic label
            desc = _infer_activity_from_text(txt)

    # Project hint
    proj = _extract_project_hint_from_text(txt)

    # Billable intent (explicit)
    billable: Optional[bool] = None
    if re.search(r"\bno\s+facturable\b", txt, flags=re.IGNORECASE):
        billable = False
    elif re.search(r"\bfacturable\b", txt, flags=re.IGNORECASE):
        billable = True

    payload: Dict[str, Any] = {
        "start": _to_clockify_dt(ts.strftime("%Y-%m-%d %H:%M"), cfg.timezone),
        "end": _to_clockify_dt(te.strftime("%Y-%m-%d %H:%M"), cfg.timezone),
    }
    if desc:
        payload["description"] = desc
    if proj:
        payload["projectId"] = proj
    if billable is not None:
        payload["billable"] = billable
    return payload, None




# --- Bulk modify helpers ------------------------------------------------------

# Max number of time entries to modify/delete in one request.
# Safety valve to prevent accidental mass edits.
_BULK_MAX_DEFAULT = int(os.getenv("CLOCKIFY_BULK_MAX") or "25")


def _wants_bulk_apply(txt: str) -> bool:
    """Return True if the user explicitly implies *multiple* hours/entries."""
    t = (txt or "").lower()
    return bool(
        re.search(
            r"\b(todas|todos|todas\s+mis|todos\s+mis|mis\s+horas|las\s+horas|varias|varios|todos\s+los\s+registros|todas\s+las\s+horas|registros)\b",
            t,
            flags=re.IGNORECASE,
        )
    )


def _extract_time_ranges_all(txt: str, base_date: Optional[datetime]):
    """Extract *all* time ranges found in text (not just the first one).

    This is used for bulk modify/delete when the user mentions multiple ranges, e.g.
      "modifica de 9 a 10 y de 11 a 12"

    Returns:
      List[(start_dt, end_dt)] in naive datetimes (base_date applied).
    """
    if not base_date:
        return []

    t = (txt or "")

    # We reuse parse_time_range on each matched substring.
    patterns = [
        # HH:MM - HH:MM
        r"\b\d{1,2}:\d{2}\s*(?:-|a|–|—|hasta)\s*\d{1,2}:\d{2}\b",
        # H[:MM] am/pm - H[:MM] am/pm
        r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*(?:-|a|–|—|hasta)\s*\d{1,2}(?::\d{2})?\s*(?:am|pm)?\b",
    ]

    ranges = []
    seen = set()
    for pat in patterns:
        for m in re.finditer(pat, t, flags=re.IGNORECASE):
            chunk = m.group(0)
            ts, te = parse_time_range(chunk, base_date)
            if ts and te:
                key = (ts.isoformat(), te.isoformat())
                if key not in seen:
                    seen.add(key)
                    ranges.append((ts, te))

    return ranges


def _criteria_list_from_single(criteria: EntryCriteria, txt: str, payload: Dict[str, Any], cfg: ClockifyConfig):
    """If the text contains multiple time ranges, expand a single criteria into many."""
    # Only expand if the user didn't provide an explicit matchTimeRange in payload.
    base = None
    if isinstance(payload.get("matchDate"), str):
        base = parse_spanish_date(payload["matchDate"])
    if base is None:
        base = parse_spanish_date(txt)
    if base is None:
        base = datetime.now(tz=_tz(cfg.timezone)).replace(tzinfo=None)

    ranges = _extract_time_ranges_all(payload.get("matchTimeRange") if isinstance(payload.get("matchTimeRange"), str) else txt, base)

    # If we found 0 or 1 ranges, keep the single criteria (current behavior)
    if len(ranges) <= 1:
        return [criteria]

    out = []
    for ts, te in ranges:
        out.append(
            EntryCriteria(
                start=_to_clockify_dt(ts.strftime("%Y-%m-%d %H:%M"), cfg.timezone),
                end=_to_clockify_dt(te.strftime("%Y-%m-%d %H:%M"), cfg.timezone),
                description=criteria.description,
                project_id=criteria.project_id,
            )
        )
    return out


def _select_time_entries_for_action(
    client: "ClockifyClient",
    criteria_list,
    *,
    bulk: bool,
    max_bulk: int,
    user_text: Optional[str] = None,
):
    """Search time entries for each criteria, return (selected_entries, errors, debug)."""
    status, user_id, udata = client.get_current_user_id()
    if status != 200 or not user_id:
        return [], [{"error": "No pude obtener el userId con /v1/user", "status": status, "response": udata}], {"status": status}

    selected = []
    errors = []
    debug_all = []

    for idx, criteria in enumerate(criteria_list, start=1):
        matches, dbg = find_time_entries(client, user_id, criteria)
        debug_all.append({"i": idx, "criteria": criteria.__dict__, "debug": dbg})
        if not matches:
            errors.append({"i": idx, "error": "No encontré registros que coincidan con los criterios.", "debug": dbg, "criteria": criteria.__dict__})
            continue

        best, candidates = pick_best_match(matches, criteria)
        if best is not None:
            selected.append(best)
            continue

        # Ambiguous
        if bulk:
            # Safety: cap edits
            if len(candidates) > max_bulk:
                errors.append({
                    "i": idx,
                    "error": f"Encontré {len(candidates)} registros para modificar. Es demasiado para hacerlo de golpe (límite {max_bulk}). Acota por proyecto/horas o divide en varias solicitudes.",
                    "count": len(candidates),
                    "limit": max_bulk,
                    "criteria": criteria.__dict__,
                })
                continue
            selected.extend(candidates)
        else:
            # Heuristic: if the user asked for a SINGLE record ("el registro del MIT.002"),
            # auto-pick the most recent candidate to avoid asking for IDs.
            # This is intentionally conservative and can be disabled via env.
            auto_pick = (os.getenv("CLOCKIFY_AUTOPICK_MOST_RECENT") or "1").strip() not in ("0", "false", "False")
            asked_single = bool(re.search(r"\b(el|ese|este)\s+registro\b", (user_text or ""), flags=re.IGNORECASE))
            if auto_pick and asked_single and candidates:
                def _as_dt(entry: Dict[str, Any]):
                    ti = entry.get("timeInterval") or {}
                    s = ti.get("start")
                    if not isinstance(s, str) or not s:
                        return datetime.min.replace(tzinfo=dt_timezone.utc)
                    try:
                        return datetime.fromisoformat(s.replace("Z", "+00:00"))
                    except Exception:
                        return datetime.min.replace(tzinfo=dt_timezone.utc)

                best_recent = sorted(candidates, key=_as_dt, reverse=True)[0]
                selected.append(best_recent)
                debug_all.append({"i": idx, "auto_picked": True, "reason": "most_recent", "picked_id": best_recent.get("id")})
                continue

            # Return condensed candidates for UI/clarification
            summary = []
            for e in candidates[:10]:
                ti = e.get("timeInterval") or {}
                summary.append({
                    "id": e.get("id"),
                    "description": e.get("description"),
                    "projectId": e.get("projectId"),
                    "start": ti.get("start"),
                    "end": ti.get("end"),
                    "billable": e.get("billable"),
                })
            errors.append({
                "i": idx,
                "error": "Encontré múltiples registros posibles. Sé más específico (hora, descripción exacta o proyecto), o indica que aplique a todas las horas.",
                "candidates": summary,
                "criteria": criteria.__dict__,
                "debug": dbg,
            })

    # Deduplicate by id while keeping order
    seen = set()
    uniq = []
    for e in selected:
        eid = str(e.get("id"))
        if eid and eid not in seen:
            seen.add(eid)
            uniq.append(e)

    return uniq, errors, {"criteria_debug": debug_all}


def _lookup_time_entry_id(client: ClockifyClient, criteria: EntryCriteria) -> Tuple[Optional[str], Optional[Dict[str, Any]], Dict[str, Any]]:
    """Find a time entry id for the current user based on criteria."""
    status, user_id, data = client.get_current_user_id()
    if status != 200 or not user_id:
        return None, None, {"status": status, "error": "No pude obtener el userId con /v1/user", "response": data}

    matches, debug = find_time_entries(client, user_id, criteria)
    if not matches:
        return None, None, {"error": "No encontré registros que coincidan con los criterios.", "debug": debug}

    best, candidates = pick_best_match(matches, criteria)
    if best is None:
        # ambiguous; return candidates summary
        summary = []
        for e in candidates[:10]:
            ti = e.get("timeInterval") or {}
            summary.append({
                "id": e.get("id"),
                "description": e.get("description"),
                "projectId": e.get("projectId"),
                "start": ti.get("start"),
                "end": ti.get("end"),
                "billable": e.get("billable"),
            })
        return None, None, {"error": "Encontré múltiples registros posibles. Sé más específico (hora, descripción exacta o proyecto).", "candidates": summary, "debug": debug}

    return str(best.get("id")), best, {"debug": debug}


def procesar_solicitud_clockify(user_text: str, config: Optional[ClockifyConfig] = None, trace: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Interpreta la solicitud del usuario y ejecuta la operación en Clockify.

    Soporta:
      - Crear / modificar / eliminar / buscar
      - Key=Value (determinista) y lenguaje natural (creación)
      - Un "Clockify agent" opcional que puede emitir un plan JSON (action/payload/match)

    Devuelve:
      dict con `ok`, `action`, `request_json` y `response`/`error`.
    """
    cfg = config or ClockifyConfig.from_env()
    txt = user_text or ""

    def _t(msg: str) -> None:
        if trace is None:
            return
        try:
            trace(msg)
        except Exception:
            pass

    _t(f"Solicitud recibida: {txt[:200]}" + ("…" if len(txt) > 200 else ""))

    # 1) Direct JSON in message (power users)
    direct = _maybe_parse_json_payload(txt)

    # 2) Agent plan (best-effort). If it fails, we fall back to heuristics.
    plan = None
    if not direct:
        plan = _try_agent_plan(txt)

    # 3) Base parse (heuristic KV)
    action: Optional[str] = None
    payload: Dict[str, Any] = {}
    time_entry_id: Optional[str] = None

    if direct:
        action = (direct.get("action") or direct.get("accion") or "").strip().lower() or None
        payload = direct.get("payload") or direct.get("data") or {}
        if not isinstance(payload, dict):
            payload = {}
        time_entry_id = direct.get("id") or payload.get("id") or direct.get("timeEntryId") or direct.get("time_entry_id")
        if time_entry_id is not None:
            time_entry_id = str(time_entry_id).strip() or None
    else:
        kv = _parse_kv(txt)
        action = (kv.get("action") or kv.get("accion") or _detect_action(txt) or "").strip().lower() or None
        time_entry_id = kv.get("id") or kv.get("timeEntryId") or kv.get("time_entry_id") or kv.get("timeentryid")
        if time_entry_id is not None:
            time_entry_id = str(time_entry_id).strip() or None
        payload = _normalize_keys(kv)
        # Remove control keys
        for k in ("action", "accion", "id", "timeentryid", "time_entry_id", "timeentryid"):
            payload.pop(k, None)

    # 4) Merge agent plan if present
    # IMPORTANT POLICY:
    # - For modificar/eliminar/buscar we NEVER block on the agent asking for an explicit time-entry ID.
    #   We always attempt API lookup by criteria first.
    # - For crear, clarification can still be required (missing time range, etc.).
    if isinstance(plan, dict) and plan.get("action"):
        agent_action = str(plan.get("action")).strip().lower()

        if plan.get("needsClarification"):
            # If action isn't determined yet, take it from the agent.
            if not action:
                action = agent_action or None

            # For CREATE it's safer to ask for missing information.
            if (action or "").strip().lower() == "crear":
                return {
                    "ok": False,
                    "action": "crear",
                    "error": "Falta información para completar la solicitud.",
                    "question": plan.get("question"),
                    "notes": plan.get("notes") or [],
                }

            # For SEARCH/MODIFY/DELETE we continue and do lookup by criteria.
            _t(
                "Nota: el agente pidió aclaración, pero continuaré con búsqueda por criterios "
                "para resolver IDs automáticamente."
            )
            payload.setdefault("_agent_question", plan.get("question"))
            payload.setdefault("_agent_notes", plan.get("notes") or [])
        if not action:
            action = agent_action

        agent_payload = plan.get("payload") or {}
        if isinstance(agent_payload, dict):
            if agent_payload.get("projectQuery") and not agent_payload.get("projectId"):
                agent_payload["projectId"] = agent_payload.get("projectQuery")

            for k, v in agent_payload.items():
                if k == "projectQuery":
                    continue
                if v is None:
                    continue
                if isinstance(v, str) and not v.strip():
                    continue
                payload[k] = v

        # Allow agent match hints to help modify/delete search
        agent_match = plan.get("match") or {}
        if isinstance(agent_match, dict):
            ms = agent_match.get("start")
            me = agent_match.get("end")
            md = agent_match.get("date")
            mdesc = agent_match.get("description")
            mproj = agent_match.get("projectQuery")
            if md is not None and (not isinstance(md, str) or md.strip()):
                payload.setdefault("matchDate", md)
            if mdesc is not None and (not isinstance(mdesc, str) or mdesc.strip()):
                payload.setdefault("matchDescription", mdesc)
            if mproj is not None and (not isinstance(mproj, str) or mproj.strip()):
                payload.setdefault("matchProject", mproj)
            if isinstance(ms, str) and isinstance(me, str) and ms.strip() and me.strip() and not payload.get("matchTimeRange"):
                payload["matchTimeRange"] = f"{ms.strip()}-{me.strip()}"

    _t(f"Acción detectada: {action or 'N/A'}")

    if not action:
        return {
            "ok": False,
            "error": "No pude identificar la acción (crear/modificar/eliminar/buscar). Incluye palabras como 'crear', 'modificar', 'eliminar' o 'buscar' o manda JSON con action.",
            "hint": "Ejemplo: 'crear registro descripcion=Reunión; start=2025-12-28 10:00; end=2025-12-28 11:00; proyecto=...; billable=true'",
        }

    # Normalize action names
    if action in ("update", "editar"):
        action = "modificar"
    if action in ("delete", "borrar"):
        action = "eliminar"
    if action in ("create",):
        action = "crear"
    if action in ("list_projects", "listar_proyectos", "proyectos"):
        action = "listar_proyectos"
    if action in ("search", "listar", "list"):
        action = "buscar"

    # Helper: normalize start/end to Clockify ISO Z if they look like local times
    def _normalize_dt_fields(d: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(d or {})
        for k in ("start", "end"):
            v = out.get(k)
            if isinstance(v, str) and v.strip():
                vv = v.strip()
                # If already ISO with Z, keep
                if "T" in vv and vv.endswith("Z"):
                    continue
                try:
                    out[k] = _to_clockify_dt(vv, cfg.timezone)
                except Exception:
                    # leave as-is; downstream will error with a clear message
                    out[k] = vv
        return out

    client: Optional[ClockifyClient] = None
    try:
        # --------------------
        # LIST PROJECT DIRECTORY (no Clockify API call)
        # --------------------
        if action == "listar_proyectos":
            client_hint = _infer_client_from_text(txt)
            proj_hint = None

            # Prefer explicit projectQuery from the agent plan, if present.
            if isinstance(payload.get("projectQuery"), str) and payload.get("projectQuery").strip():
                proj_hint = payload.get("projectQuery").strip()
            elif isinstance(payload.get("projectId"), str) and payload.get("projectId").strip() and not re.fullmatch(
                r"[0-9a-f]{24}", payload.get("projectId").strip(), flags=re.IGNORECASE
            ):
                proj_hint = payload.get("projectId").strip()
            else:
                maybe = _extract_project_hint_from_text(txt)
                if maybe and maybe.lower() != "comida":
                    proj_hint = maybe

            projs = list_projects(client_query=client_hint, project_query=proj_hint, limit=200)
            return {
                "ok": True,
                "action": "listar_proyectos",
                "filters": {"client": client_hint, "projectQuery": proj_hint},
                "count": len(projs),
                "projects": [
                    {
                        "proyecto": p.project_name,
                        "id": p.project_id,
                        "cliente": p.client,
                        "facturable": p.billable_default,
                        "taskIds": {
                            "discovery": p.id_discovery,
                            "desarrollo": p.id_desarrollo,
                            "deployment": p.id_deployment,
                            "farming": p.farming,
                            "hunting": p.hunting,
                        },
                    }
                    for p in projs
                ],
            }

        client = ClockifyClient(cfg)

        # --------------------
        # CREATE
        # --------------------
        if action == "crear":
            # If the agent provided start/end but the user wrote a clear date+range in natural language,
            # prefer deterministic parsing so we don't inherit hallucinated years/dates.
            if isinstance(plan, dict) and plan.get("action"):
                bd = parse_spanish_date(txt) or datetime.now(tz=_tz(cfg.timezone)).replace(tzinfo=None)
                ts0, te0 = parse_time_range(txt, bd)
                if ts0 and te0:
                    payload["start"] = ts0.strftime("%Y-%m-%d %H:%M")
                    payload["end"] = te0.strftime("%Y-%m-%d %H:%M")

            # If start/end missing, try NL inference
            if not payload.get("start") or not payload.get("end"):
                inferred, err = _infer_create_payload_from_text(txt, cfg)
                if err:
                    return {"ok": False, "action": "crear", "error": err}
                # Merge inferred values (payload explicit overrides inferred)
                for k, v in inferred.items():
                    payload.setdefault(k, v)

            # Upstream planners sometimes send time-only values (e.g. start="14:00")
            # together with a date token in the text (e.g. "fecha=2026-02-19").
            # Coerce them into full datetimes before converting to UTC ISO.
            payload = _coerce_time_only_start_end(payload, txt, cfg, trace=_t)

            # Trace time normalization (local -> UTC ISO)
            _raw_start = str(payload.get('start') or '').strip()
            _raw_end = str(payload.get('end') or '').strip()
            if _raw_start and _raw_end:
                _t(f"Generando hora: {_raw_start} – {_raw_end} (tz={cfg.timezone})")
            payload = _normalize_dt_fields(payload)
            _iso_start = str(payload.get('start') or '').strip()
            _iso_end = str(payload.get('end') or '').strip()
            if _iso_start and _iso_end and (_iso_start != _raw_start or _iso_end != _raw_end):
                _t(f"Hora convertida a ISO (UTC): start={_iso_start} end={_iso_end}")

            # Workspace rules often require a project. If user says "Comida" and no project was set,
            # route it through the Excel resolver.
            if not payload.get("projectId") and re.search(r"\bcomida\b", txt, flags=re.IGNORECASE):
                payload["projectId"] = "Comida"

            if payload.get("projectQuery") and not payload.get("projectId"):
                payload["projectId"] = payload.get("projectQuery")

            # If still missing a project, try to infer it from the text (project code or client name).
            if not payload.get("projectId"):
                hint = _extract_project_hint_from_text(txt)
                if hint:
                    payload["projectId"] = hint
                else:
                    client_hint = _infer_client_from_text(txt)
                    if client_hint:
                        cprojs = projects_by_client(client_hint)
                        if len(cprojs) == 1:
                            payload["projectId"] = cprojs[0].project_name
                        elif len(cprojs) > 1:
                            return {
                                "ok": False,
                                "action": "crear",
                                "error": "Encontré varios proyectos para ese cliente. Indica el proyecto.",
                                "client": client_hint,
                                "candidates": [{"name": p.project_name, "id": p.project_id} for p in cprojs[:10]],
                            }

            # Resolve project (Excel). We also keep client + billable defaults for templating.
            resolved_project_code: Optional[str] = None
            resolved_client: Optional[str] = None
            resolved_billable_default: Optional[bool] = None
            resolved_project_match: Optional[ProjectMatch] = None

            if payload.get("projectId"):
                raw_proj = str(payload.get("projectId") or "").strip()
                try:
                    _t(f"Buscando proyecto en directorio: {raw_proj}")
                    pmatch, pcands = resolve_project_id(raw_proj)
                except Exception as e:
                    return {
                        "ok": False,
                        "action": "crear",
                        "error": f"Error leyendo clockify_proyectos.xlsx: {e}",
                        "provided": raw_proj,
                    }
                if pmatch:
                    # Keep the resolved match so we can also resolve taskId (etapa) via Excel columns.
                    resolved_project_match = pmatch

                    # When user passed an ID directly, project_name is that ID; try to enrich metadata
                    # from the directory (so we can still template + resolve tasks).
                    if pmatch.match_type == "id":
                        meta = find_project_by_id(pmatch.project_id)
                        if meta:
                            resolved_project_match = meta

                    # Only template with a *project code*, never with a raw 24-hex ID.
                    if resolved_project_match and not re.fullmatch(r"[0-9a-f]{24}", (resolved_project_match.project_name or "").strip(), flags=re.IGNORECASE):
                        resolved_project_code = resolved_project_match.project_name

                    resolved_client = resolved_project_match.client if resolved_project_match else pmatch.client
                    resolved_billable_default = resolved_project_match.billable_default if resolved_project_match else pmatch.billable_default
                    payload["projectId"] = pmatch.project_id
                    _bill = resolved_billable_default if resolved_billable_default is not None else pmatch.billable_default
                    _t(f"Proyecto encontrado: {(resolved_project_code or pmatch.project_name)} | {pmatch.project_id} | facturable={('Sí' if _bill else 'No')} | cliente={(resolved_client or pmatch.client or '').strip()}")
                elif pcands:
                    return {
                        "ok": False,
                        "action": "crear",
                        "error": "Proyecto ambiguo. Especifica mejor el nombre/código.",
                        "provided": raw_proj,
                        "candidates": [{"name": c.project_name, "id": c.project_id, "match": c.match_type} for c in pcands[:10]],
                    }
                else:
                    return {
                        "ok": False,
                        "action": "crear",
                        "error": "No encontré el proyecto en directorios/clockify_proyectos.xlsx. Indica el proyecto por nombre/código.",
                        "provided": raw_proj,
                    }

            # Normalize billable: explicit > text > Excel default > true
            if "billable" in payload:
                payload["billable"] = _parse_bool(payload.get("billable"))
            else:
                if re.search(r"\bno\s+facturable\b", txt, flags=re.IGNORECASE):
                    payload["billable"] = False
                elif re.search(r"\bfacturable\b", txt, flags=re.IGNORECASE):
                    payload["billable"] = True
                elif resolved_billable_default is not None:
                    payload["billable"] = bool(resolved_billable_default)
                else:
                    payload["billable"] = True

            # Build description with the configured template: "Cliente | Proyecto | Actividad"
            # We only template when we resolved the project through the Excel directory
            # (so we have a reliable client + project code).
            desc_raw = payload.get("description") if isinstance(payload.get("description"), str) else ""
            actividad_for_stage: Optional[str] = None
            comida_hit = bool(re.search(r"\bcomida\b", txt, flags=re.IGNORECASE))
            if comida_hit:
                payload["description"] = "Comida"
            elif resolved_project_code and not _looks_like_full_template(desc_raw):
                actividad = (desc_raw or "").strip() or _infer_activity_from_text(txt)
                actividad_for_stage = actividad
                cliente_final = (resolved_client or _infer_client_from_text(txt) or "").strip() or "Cliente"
                payload["description"] = _format_description_template(
                    cfg.description_template,
                    cliente_final,
                    resolved_project_code,
                    actividad,
                )
            elif isinstance(desc_raw, str) and desc_raw.strip():
                payload["description"] = desc_raw.strip()
                actividad_for_stage = desc_raw.strip()
            else:
                # If we can't template (missing project in Excel), keep a safe fallback.
                actividad_for_stage = _infer_activity_from_text(txt)
                payload["description"] = actividad_for_stage

            # If the user already provided a full template, try to extract the activity part
            # so stage inference (taskId) works reliably.
            if not actividad_for_stage and isinstance(payload.get("description"), str) and _looks_like_full_template(payload["description"]):
                parts = [p.strip() for p in (payload.get("description") or "").split("|")]
                if parts:
                    actividad_for_stage = parts[-1]

            # Resolve taskId (stage) from the Excel directory when the project provides stage IDs.
            # Policy: if ambiguous/missing, default to Desarrollo.
            if not payload.get("taskId") and _project_has_any_task_ids(resolved_project_match):
                stage = _infer_stage_from_text(txt, actividad_for_stage)
                task_id = _task_id_for_stage(resolved_project_match, stage)  # type: ignore[arg-type]
                if not task_id:
                    # Final fallback: Desarrollo (per policy)
                    task_id = (resolved_project_match.id_desarrollo if resolved_project_match else None)
                if not task_id:
                    choices = _available_stage_choices(resolved_project_match) if resolved_project_match else []
                    return {
                        "ok": False,
                        "action": "crear",
                        "error": "Este proyecto requiere taskId, pero el directorio no tiene IDs de etapa configurados para la etapa inferida.",
                        "project": getattr(resolved_project_match, "project_name", None),
                        "question": "¿Qué etapa debo usar (discovery, desarrollo, deployment, farming, hunting)?",
                        "choices": choices,
                    }
                payload["taskId"] = task_id

            # Ensure tagIds exists (business rules will always include default tag).
            if "tagIds" not in payload:
                payload["tagIds"] = []

            # Deterministic business rules (tags/comida/hora extra)
            payload, br_err = _apply_business_rules(payload, txt, action="crear", default_tag_id=cfg.default_tag_id)
            if br_err:
                br_err.setdefault("action", "crear")
                return br_err

            notes = payload.pop("notes", None)
            payload = {k: v for k, v in payload.items() if k in ("description", "start", "end", "billable", "projectId", "taskId", "tagIds", "type")}

            _t("Generando JSON (payload Clockify):\n```json\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n```")
            _t("Enviando API: POST /workspaces/{}/time-entries".format(client.workspace_id))
            status, resp = crear_registro(client, payload)
            _t(f"Respuesta API: HTTP {status}")
            ok = 200 <= status < 300
            if ok:
                _t("Resultado: ✅ Correcto")
            else:
                _t("Resultado: ❌ " + _api_error_text(status, resp))
            out = {
                "ok": ok,
                "action": "crear",
                "request_json": payload,
                "status": status,
                "response": resp,
                "notes": notes or [],
            }
            if not ok:
                out.setdefault("error", _api_error_text(status, resp))
            return out

        # --------------------
        # SEARCH/LIST
        # --------------------
        if action == "buscar":
            # Build criteria based on text and payload hints
            criteria, _updates, err = _extract_match_updates_from_text(txt, payload, "buscar", cfg)
            if err:
                return {"ok": False, "action": "buscar", "error": err}

            status, user_id, udata = client.get_current_user_id()
            if status != 200 or not user_id:
                return {"ok": False, "action": "buscar", "error": "No pude obtener el userId con /v1/user", "status": status, "response": udata}

            matches, debug = find_time_entries(client, user_id, criteria)
            if not matches:
                return {"ok": False, "action": "buscar", "error": "No encontré registros que coincidan con los criterios.", "debug": debug}

            best, candidates = pick_best_match(matches, criteria)
            # return a condensed list for UI buttons
            opts = []
            for e in candidates[:10]:
                ti = e.get("timeInterval") or {}
                opts.append({
                    "id": e.get("id"),
                    "description": e.get("description"),
                    "projectId": e.get("projectId"),
                    "start": ti.get("start"),
                    "end": ti.get("end"),
                    "billable": e.get("billable"),
                })
            return {
                "ok": True,
                "action": "buscar",
                "match": {"best": best.get("id") if isinstance(best, dict) else None},
                "results": opts,
                "debug": debug,
            }

        # --------------------
        # MODIFY / DELETE
        # --------------------
        if action in ("modificar", "eliminar"):
            criteria, updates, err = _extract_match_updates_from_text(txt, payload, action, cfg)
            _t("Extrayendo criterios de búsqueda para modificar/eliminar...")
            _t(f"Criterios: date={getattr(criteria, 'date', None)} start={getattr(criteria, 'start', None)} end={getattr(criteria, 'end', None)} project_id={getattr(criteria, 'project_id', None)} desc={getattr(criteria, 'description', None)}")
            if action == 'modificar':
                _t("Cambios solicitados: " + ", ".join(sorted(list(updates.keys()))) if updates else "Cambios solicitados: (ninguno)")
            if err:
                return {"ok": False, "action": action, "error": err}

            if action == "modificar" and not updates:
                return {
                    "ok": False,
                    "action": "modificar",
                    "error": "No detecté qué quieres cambiar. Indica el campo a modificar (horario, descripción, proyecto, facturable, tags).",
                    "question": "¿Qué quieres modificar?",
                    "choices": ["horario", "descripción", "proyecto", "facturable", "tags"],
                }

            if updates.get("projectQuery") and not updates.get("projectId"):
                updates["projectId"] = updates.get("projectQuery")

            # If the request includes a project change by name, resolve via Excel
            if updates.get("projectId"):
                up2, perr = _maybe_resolve_project(updates)
                if perr:
                    return perr
                updates = up2

            # Support upstream patterns like start="14:00" / end="16:00" in modify.
            # Coerce to full datetimes before converting to UTC ISO.
            updates = _coerce_time_only_start_end(updates, txt, cfg, trace=_t)

            updates = _normalize_dt_fields(updates)
            if "billable" in updates:
                updates["billable"] = _parse_bool(updates.get("billable"))

            # Apply deterministic business rules (especially tags)
            # En modificaciones NO aplicamos reglas de tags aquí para evitar enviar `tagIds: []`
            # o sobreescribir tags existentes. El merge de tags se hace más adelante por registro.
            updates, br_err = _apply_business_rules(
                updates,
                txt,
                action=action,
                default_tag_id=cfg.default_tag_id,
                apply_tags=False,
            )
            if br_err:
                br_err.setdefault("action", action)
                return br_err

            notes = updates.pop("notes", None)

            # --- Tags (modificar)
            # Si el usuario pide "agrega tag ..." (ej. horas extras), NO enviamos
            # `tagIds: []`. En lugar de eso, pasamos una operación interna
            # `_tag_add` para que `modificar_registro` haga merge con los tags
            # actuales del registro.
            if action == "modificar" and "tagIds" not in updates:
                if re.search(r"(tag|tags|etiqueta|etiquetas|hora\s*extra|horas\s*extras|overtime|arq|arquitectura)", txt, flags=re.IGNORECASE):
                    tag_add = _infer_tag_additions(txt, cfg)
                    if tag_add:
                        updates["_tag_add"] = tag_add
                        _t(f"Tags a agregar (merge): {tag_add}")

            # Safety: only send supported fields to Clockify
            updates = {
                k: v
                for k, v in updates.items()
                if k in (
                    "description",
                    "start",
                    "end",
                    "billable",
                    "projectId",
                    "taskId",
                    "tagIds",
                    "type",
                    # Internal tag operations (handled in modificar_registro)
                    "_tag_add",
                    "_tag_remove",
                    "_tag_set",
                )
            }


            # If no explicit id, lookup by criteria using the API first (no user-provided IDs required).
            # We can handle one or many matches depending on user intent ("todas mis horas", etc.).
            selected_entries = []
            matched = None
            debug = {}

            if not time_entry_id:
                # Expand criteria if the text contains multiple time ranges.
                criteria_list = _criteria_list_from_single(criteria, txt, payload, cfg)

                bulk = _wants_bulk_apply(txt) or len(criteria_list) > 1
                max_bulk = _BULK_MAX_DEFAULT

                selected_entries, lookup_errors, debug = _select_time_entries_for_action(
                    client,
                    criteria_list,
                    bulk=bulk,
                    max_bulk=max_bulk,
                    user_text=txt,
                )
                _t(f"Registros seleccionados: {len(selected_entries)}")
                if selected_entries:
                    _t("IDs seleccionados: " + ", ".join([str(e.get('id')) for e in selected_entries[:10]]))
                    for e in selected_entries[:5]:
                        ti = (e.get("timeInterval") or {}) if isinstance(e, dict) else {}
                        _t(f"- {e.get('id')} | {ti.get('start')}–{ti.get('end')} | {str(e.get('description') or '')[:60]}")

                if not selected_entries:
                    # Delete is idempotent: if nothing found, treat as already deleted.
                    if action == "eliminar" and lookup_errors and all("No encontré" in (e.get("error") or "") for e in lookup_errors):
                        return {
                            "ok": True,
                            "action": "eliminar",
                            "message": "No encontré el/los registros; probablemente ya estaban eliminados.",
                            "errors": lookup_errors,
                            "debug": debug,
                        }
                    return {"ok": False, "action": action, "errors": lookup_errors, "debug": debug}

                # Single match convenience for downstream logic
                if len(selected_entries) == 1:
                    matched = selected_entries[0]
                    time_entry_id = str(matched.get("id"))

                # If resolved to multiple entries, execute sequentially and return a summary.
                if len(selected_entries) > 1:
                    # Guard: do not apply start/end changes in bulk (too risky).
                    if action == "modificar" and ("start" in updates or "end" in updates):
                        return {
                            "ok": False,
                            "action": "modificar",
                            "error": "Detecté varios registros a modificar. Por seguridad no aplico cambios de horario (start/end) a múltiples registros en bloque. Divide la solicitud por rangos (uno por tarea) o especifica exactamente cada registro.",
                            "count": len(selected_entries),
                        }

                    results = []

                    stage_mentioned = False
                    if action == "modificar":
                        stage_mentioned = bool(re.search(r"\b(discovery|desarrollo|development|deployment|deploy|preventa|farming|hunting)\b", txt, flags=re.IGNORECASE))

                    for e in selected_entries:
                        eid = str(e.get("id") or "").strip()
                        if not eid:
                            results.append({"ok": False, "error": "Registro sin id en respuesta de Clockify", "entry": e})
                            continue

                        if action == "modificar":
                            per_updates = dict(updates)

                            # Resolve taskId per entry when the company directory provides stage IDs.
                            # Policy: only set taskId when the user changes project or mentions a stage.
                            if not per_updates.get("taskId") and ("projectId" in per_updates or stage_mentioned):
                                effective_project_id = per_updates.get("projectId") or e.get("projectId")
                                pm = find_project_by_id(str(effective_project_id)) if effective_project_id else None
                                if pm and _project_has_any_task_ids(pm):
                                    activity_hint = per_updates.get("description") if isinstance(per_updates.get("description"), str) else (e.get("description") if isinstance(e.get("description"), str) else None)
                                    stage = _infer_stage_from_text(txt, activity_hint)
                                    task_id = _task_id_for_stage(pm, stage) or pm.id_desarrollo
                                    if task_id:
                                        per_updates["taskId"] = task_id

                            _t("Generando JSON (updates Clockify):\n```json\n" + json.dumps(per_updates, ensure_ascii=False, indent=2) + "\n```")
                            _t("Enviando API: PUT /workspaces/{}/time-entries/{}".format(client.workspace_id, eid))
                            st, rp = modificar_registro(client, eid, per_updates)
                            _t(f"Respuesta API ({eid}): HTTP {st}")
                            ok_i = 200 <= st < 300
                            results.append({
                                "ok": ok_i,
                                "time_entry_id": eid,
                                "status": st,
                                "request_json": per_updates,
                                "response": rp,
                                **({"error": _api_error_text(st, rp)} if not ok_i else {}),
                            })
                        else:
                            st, rp = eliminar_registro(client, eid)
                            ok_i = st in (200, 204)
                            results.append({
                                "ok": ok_i,
                                "time_entry_id": eid,
                                "status": st,
                                "response": rp,
                                **({"error": _api_error_text(st, rp)} if not ok_i else {}),
                            })

                    ok_count = sum(1 for r in results if r.get("ok"))
                    fail_count = len(results) - ok_count
                    return {
                        "ok": fail_count == 0,
                        "action": action,
                        "batch": True,
                        "summary": {"total": len(results), "ok": ok_count, "failed": fail_count},
                        "results": results,
                        "debug": debug,
                        "notes": notes or [],
                    }

            # If we have an explicit ID (power users) and still need context (projectId), fetch the entry once.
            if time_entry_id and matched is None:
                st_get, cur = client.request_json("GET", f"/workspaces/{client.workspace_id}/time-entries/{str(time_entry_id).strip()}")
                if st_get == 200 and isinstance(cur, dict):
                    matched = cur
            # If we are modifying and the company directory provides stage IDs, resolve taskId automatically
            # when the user is changing the project or mentions a stage.
            if action == "modificar":
                stage_mentioned = bool(re.search(r"\b(discovery|desarrollo|development|deployment|deploy|preventa|farming|hunting)\b", txt, flags=re.IGNORECASE))
                if not updates.get("taskId") and ("projectId" in updates or stage_mentioned):
                    effective_project_id = updates.get("projectId") or (matched.get("projectId") if isinstance(matched, dict) else None)
                    pm = find_project_by_id(str(effective_project_id)) if effective_project_id else None

                    if pm and _project_has_any_task_ids(pm):
                        activity_hint = updates.get("description") if isinstance(updates.get("description"), str) else None
                        stage = _infer_stage_from_text(txt, activity_hint)
                        task_id = _task_id_for_stage(pm, stage) or pm.id_desarrollo
                        if not task_id:
                            return {
                                "ok": False,
                                "action": "modificar",
                                "error": "Este proyecto requiere taskId, pero el directorio no tiene IDs de etapa configurados.",
                                "project": pm.project_name,
                                "question": "¿Qué etapa debo usar (discovery, desarrollo, deployment, farming, hunting)?",
                                "choices": _available_stage_choices(pm),
                            }
                        updates["taskId"] = task_id
                    elif effective_project_id and ("projectId" in updates or stage_mentioned):
                        # We need a taskId, but the project isn't in the directory.
                        return {
                            "ok": False,
                            "action": "modificar",
                            "error": "No encontré el proyecto en el directorio para resolver taskId (etapa).",
                            "projectId": str(effective_project_id),
                        }

            if action == "modificar":
                _t("Generando JSON (updates Clockify):\n```json\n" + json.dumps(updates, ensure_ascii=False, indent=2) + "\n```")
                _t("Enviando API: PUT /workspaces/{}/time-entries/{}".format(client.workspace_id, str(time_entry_id).strip()))
                status, resp = modificar_registro(client, str(time_entry_id).strip(), updates)
                _t(f"Respuesta API: HTTP {status}")
                ok = 200 <= status < 300
                if ok:
                    _t("Resultado: ✅ Correcto")
                else:
                    _t("Resultado: ❌ " + _api_error_text(status, resp))
                out = {
                    "ok": ok,
                    "action": "modificar",
                    "time_entry_id": time_entry_id,
                    "request_json": updates,
                    "status": status,
                    "response": resp,
                    "notes": notes or [],
                }
                if not ok:
                    out.setdefault("error", _api_error_text(status, resp))
                return out

            # eliminar
            status, resp = eliminar_registro(client, str(time_entry_id).strip())
            ok = status in (200, 204)
            out = {
                "ok": ok,
                "action": "eliminar",
                "time_entry_id": time_entry_id,
                "status": status,
                "response": resp,
            }
            if not ok:
                out.setdefault("error", _api_error_text(status, resp))
            return out

        return {"ok": False, "action": action, "error": f"Acción no soportada: {action}"}

    finally:
        try:
            if client is not None:
                client.close()
        except Exception:
            pass

def handle_clockify_request(user_request: str, config: Optional[ClockifyConfig] = None, trace: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """High-level entry point for the Beeckario Clockify tool.

    Supports both single requests and *batch* requests in one message.

    Batch formats supported:
      1) Bullet/numbered list (recommended):
         - "1) crea ..."
         - "2) modifica ..."
      2) JSON: {"batch": [ {"action": "crear", "payload": {...}}, ... ]}

    All Clockify operations are executed sequentially under a global lock to
    avoid interleaving/ordering bugs (e.g., modify running before create).
    """

    txt = (user_request or '').strip()
    if not txt:
        return {"ok": False, "error": "Solicitud vacía."}

    trace_lines: List[str] = []

    def _t(msg: str) -> None:
        s = (msg or '').rstrip()
        if not s:
            return
        trace_lines.append(s)
        if trace is not None:
            try:
                trace(s)
            except Exception:
                # trace callbacks must never break core logic
                pass


    # JSON batch
    direct = _maybe_parse_json_payload(txt)
    if isinstance(direct, dict) and isinstance(direct.get('batch'), list):
        tasks = [t for t in direct.get('batch') if isinstance(t, dict)]
        if not tasks:
            return {"ok": False, "error": "Batch JSON vacío."}
        with _CLOCKIFY_LOCK:
            results = []
            for i, task in enumerate(tasks, start=1):
                try:
                    res = procesar_solicitud_clockify(json.dumps(task, ensure_ascii=False), config=config, trace=lambda m, _i=i: _t(f"[{_i}] {m}"))
                except Exception as e:
                    res = {"ok": False, "action": task.get("action"), "error": str(e)}
                results.append({"index": i, **(res or {})})
        ok_count = sum(1 for r in results if r.get('ok'))
        fail = [r for r in results if not r.get('ok')]
        return {
            "ok": len(fail) == 0,
            "action": "batch",
            "summary": {"total": len(results), "ok": ok_count, "failed": len(fail)},
            "results": results,
            "trace": trace_lines,
        }

    # Text batch
    parts = _split_batch_requests(txt)
    if len(parts) > 1:
        with _CLOCKIFY_LOCK:
            results = []
            for i, part in enumerate(parts, start=1):
                try:
                    res = procesar_solicitud_clockify(part, config=config, trace=lambda m, _i=i: _t(f"[{_i}] {m}"))
                except Exception as e:
                    res = {"ok": False, "action": _detect_action(part), "error": str(e)}
                results.append({"index": i, "request": part, **(res or {})})

        ok_count = sum(1 for r in results if r.get('ok'))
        fail = [r for r in results if not r.get('ok')]
        return {
            "ok": len(fail) == 0,
            "action": "batch",
            "summary": {"total": len(results), "ok": ok_count, "failed": len(fail)},
            "results": results,
            "trace": trace_lines,
        }

    # Single request
    with _CLOCKIFY_LOCK:
        res = procesar_solicitud_clockify(txt, config=config, trace=_t)
        # attach trace for callers that want the full internal process
        if isinstance(res, dict):
            res.setdefault("trace", trace_lines)
        return res



# ---------------------------
# Agent plan + business rules
# ---------------------------
import json as _json

ARCH_TAG_ID = (os.getenv("CLOCKIFY_DEFAULT_TAG_ID") or "61f0377393930f642ee65f80").strip() or "61f0377393930f642ee65f80"
OVERTIME_TAG_ID = "62656d0f5c72f44daa0ae1a8"
COMIDA_PROJECT_ID = "61d5aa2596aafe5141cd7413"

def _extract_json_object(s: str) -> dict | None:
    if not isinstance(s, str):
        return None
    # Try direct JSON
    try:
        return _json.loads(s)
    except Exception:
        pass
    # Try substring between first { and last }
    a = s.find("{")
    b = s.rfind("}")
    if a != -1 and b != -1 and b > a:
        try:
            return _json.loads(s[a:b+1])
        except Exception:
            return None
    return None

def _try_agent_plan(user_text: str) -> dict | None:
    # Use the agent if available; fall back if anything fails.
    try:
        from .clockify_agent import clockify_agent
    except Exception:
        return None
    try:
        # Provide current date context so the model can ground "hoy/ayer/mañana"
        # and weekdays reliably instead of hallucinating years/dates.
        tz_name = (os.getenv("CLOCKIFY_TIMEZONE") or "America/Mexico_City").strip() or "America/Mexico_City"
        now_local = datetime.now(tz=_tz(tz_name))
        today = now_local.strftime("%Y-%m-%d")
        now_hm = now_local.strftime("%H:%M")
        ctx = (
            f"Contexto de tiempo: hoy es {today} y la hora actual es {now_hm} en timezone {tz_name}. "
            f"Si el usuario dice 'hoy', usa {today}. Si dice un día de la semana (ej. 'viernes'), calcula la fecha a partir de {today}.\n\n"
        )
        res = clockify_agent.invoke({"request": ctx + (user_text or "")})
        decision = res.get("decision") if isinstance(res, dict) else None
        plan = _extract_json_object(decision)
        return plan if isinstance(plan, dict) else None
    except Exception:
        return None

def _uniq(lst: list) -> list:
    seen = set()
    out = []
    for x in lst:
        x = str(x).strip() if x is not None else ""
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out

def _apply_business_rules(
    payload: dict,
    user_text: str,
    action: str = "crear",
    default_tag_id: Optional[str] = None,
    apply_tags: bool = True,
) -> tuple[dict, dict | None]:
    """Apply deterministic business rules to minimize model mistakes.
    Returns: (payload, error_response_or_None)

    Rules covered:
      - Default tag (configurable) is always applied on CREATE.
      - For MODIFY, default tag is only enforced when the user explicitly changes tags.
      - Comida/Hora extra rules remain.
    """
    txt = user_text or ""
    p = dict(payload or {})

    default_tag = (default_tag_id or os.getenv("CLOCKIFY_DEFAULT_TAG_ID") or ARCH_TAG_ID).strip() or ARCH_TAG_ID

    # --- Tags (optional in modify)
    # Importante: para MODIFICAR, evitamos generar `tagIds: []` por defecto,
    # porque eso puede borrar tags existentes y disparar 403/validaciones.
    tags = None
    if apply_tags:
        tags = p.get("tagIds") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        if not isinstance(tags, list):
            tags = []
        tags = [str(t).strip() for t in tags if str(t).strip()]

        # Always apply the default tag on create.
        if (action or "").lower() == "crear":
            tags.append(default_tag)

        # If the user explicitly mentions architecture, enforce the default tag too.
        if re.search(r"\b(arq|arquitectura)\b", txt, flags=re.IGNORECASE):
            tags.append(default_tag)

        # Hora extra rule (solo agrega tags; el merge con tags existentes se hace en modificar_registro).
        if re.search(r"\bhora\s*extra\b|\bovertime\b", txt, flags=re.IGNORECASE):
            tags.append(default_tag)
            tags.append(OVERTIME_TAG_ID)
            desc = (p.get("description") or "").strip()
            if desc and not desc.lower().endswith("-hora extra"):
                p["description"] = desc + " -hora extra"

    # Comida rule (never billable)
    comida_hit = bool(
        re.search(r"\bcomida\b|\blunch\b", txt, flags=re.IGNORECASE)
        or (isinstance(p.get("projectId"), str) and (p.get("projectId") or "").lower() == "comida")
        or (isinstance(p.get("projectQuery"), str) and re.search(r"\bcomida\b", p.get("projectQuery"), flags=re.IGNORECASE))
    )
    if comida_hit:
        # En Clockify: comida nunca es facturable y debe llevar tag default
        tags.append(default_tag)
        p["billable"] = False

        # Asegura proyecto Comida si el usuario lo pidió explícitamente
        if not p.get("projectId") or (isinstance(p.get("projectId"), str) and (p.get("projectId") or "").lower() == "comida"):
            p["projectId"] = COMIDA_PROJECT_ID

        # Si no hay descripción explícita, usa "Comida"
        desc0 = (p.get("description") or "").strip()
        if not desc0:
            p["description"] = "Comida"

        notes = p.get("notes")
        if not isinstance(notes, list):
            notes = []
        notes.append("Nota: las horas de comida no son facturables. Se guardó billable=false.")
        p["notes"] = notes

    if apply_tags and tags is not None:
        # En CREATE siempre dejamos tagIds presente.
        if (action or "").lower() == "crear":
            p["tagIds"] = _uniq(tags)
        else:
            # En MODIFY, solo incluimos tagIds si realmente hay tags que aplicar
            # (si no, lo omitimos para no borrar tags existentes).
            tags_u = _uniq(tags)
            if tags_u:
                p["tagIds"] = tags_u
            elif "tagIds" in p:
                # Si el usuario pidió explícitamente modificar tags y quedó vacío,
                # lo dejamos para que Clockify valide (puede fallar si es requerido).
                p["tagIds"] = []
    return p, None
