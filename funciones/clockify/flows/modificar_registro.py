from __future__ import annotations

from typing import Any, Dict, Tuple


_ALLOWED_UPDATE_KEYS = {
    "billable",
    "description",
    "start",
    "end",
    "projectId",
    "tagIds",
    "taskId",
    "type",
    "customFields",
    "customAttributes",
    # Internal operations (handled before PUT)
    "_tag_add",
    "_tag_remove",
    "_tag_set",
}


def _uniq(lst: list) -> list:
    seen = set()
    out = []
    for x in lst or []:
        x = str(x).strip() if x is not None else ""
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _build_put_payload_from_current(current: Dict[str, Any]) -> Dict[str, Any]:
    """Build a safe PUT payload from a GET time entry response.

    Clockify GET returns timeInterval.start/end; PUT expects start/end.
    """
    time_interval = current.get("timeInterval") or {}
    payload = {
        "billable": current.get("billable", False),
        "description": current.get("description") or "",
        "projectId": current.get("projectId"),
        "tagIds": current.get("tagIds") or [],
        "taskId": current.get("taskId"),
        "type": current.get("type"),
        "start": time_interval.get("start"),
        "end": time_interval.get("end"),
    }

    # Si el registro no trae tags (lista vacía) y la org requiere tags,
    # algunos workspaces rechazan el update. En Beecker, el tag default
    # (Arquitectura) es seguro, así que lo aplicamos como fallback.
    try:
        import os

        default_tag = (os.getenv("CLOCKIFY_DEFAULT_TAG_ID") or "").strip()
        if default_tag and isinstance(payload.get("tagIds"), list) and not payload["tagIds"]:
            payload["tagIds"] = [default_tag]
    except Exception:
        pass
    # remove Nones to avoid overwriting with null unless explicitly asked
    return {k: v for k, v in payload.items() if v is not None}


def modificar_registro(client, time_entry_id: str, updates: Dict[str, Any]) -> Tuple[int, Any]:
    """Modifica un registro (time entry) existente.

    Estrategia:
      1) GET el registro actual
      2) Construir un payload completo (más seguro para PUT)
      3) Aplicar solo los campos provistos en `updates`
      4) PUT al endpoint

    Clockify API v1:
      GET    /v1/workspaces/{workspaceId}/time-entries/{id}
      PUT    /v1/workspaces/{workspaceId}/time-entries/{id}

    Returns:
      (status_code, response_json)
    """
    # 1) Read current
    get_path = f"/workspaces/{client.workspace_id}/time-entries/{time_entry_id}"
    status, current = client.request_json("GET", get_path)
    if status >= 400:
        return status, current

    if not isinstance(current, dict):
        return 500, {"error": "Respuesta inesperada de Clockify al obtener el time entry."}

    # 2) Build safe PUT payload
    payload = _build_put_payload_from_current(current)

    # 3) Apply updates (only allowed keys)
    upd = dict(updates or {})

    # --- Tag operations (merge)
    # IMPORTANTE: tagIds=[] borra tags. Para solicitudes como "agrega horas extras",
    # el orquestador manda `_tag_add` y aquí hacemos merge con los tags actuales.
    tag_set = upd.pop("_tag_set", None)
    tag_add = upd.pop("_tag_add", None)
    tag_remove = upd.pop("_tag_remove", None)

    if tag_set is not None:
        # Set exact tag list (power users)
        payload["tagIds"] = _uniq(tag_set if isinstance(tag_set, list) else [tag_set])
    else:
        cur_tags = payload.get("tagIds") or []
        if not isinstance(cur_tags, list):
            cur_tags = []
        cur_tags = [str(t).strip() for t in cur_tags if str(t).strip()]

        if tag_add:
            add_list = tag_add if isinstance(tag_add, list) else [tag_add]
            cur_tags.extend([str(t).strip() for t in add_list if str(t).strip()])
        if tag_remove:
            rm_list = set([str(t).strip() for t in (tag_remove if isinstance(tag_remove, list) else [tag_remove]) if str(t).strip()])
            cur_tags = [t for t in cur_tags if t not in rm_list]

        if tag_add or tag_remove:
            payload["tagIds"] = _uniq(cur_tags)

    for k, v in upd.items():
        if k not in _ALLOWED_UPDATE_KEYS:
            continue
        payload[k] = v

    # 4) Update
    put_path = f"/workspaces/{client.workspace_id}/time-entries/{time_entry_id}"
    status, data = client.request_json("PUT", put_path, json_body=payload)

    # Optional debug: attach the final PUT payload (no secrets) when tracing.
    try:
        import os

        if str(os.getenv("BEECKARIO_TRACE") or "").strip() in {"1", "true", "True", "yes", "YES"}:
            if isinstance(data, dict):
                data["_debug_put_payload"] = payload
    except Exception:
        pass
    return status, data
