from __future__ import annotations

from typing import Any, Dict, Tuple


def eliminar_registro(client, time_entry_id: str) -> Tuple[int, Dict[str, Any]]:
    """Elimina un registro (time entry) en Clockify.

    Clockify API v1:
      DELETE /v1/workspaces/{workspaceId}/time-entries/{id}

    Returns:
      (status_code, response_json)

    Nota: Clockify responde 204 No Content si fue exitoso.
    """
    path = f"/workspaces/{client.workspace_id}/time-entries/{time_entry_id}"
    status, data = client.request_json("DELETE", path)
    if status == 204:
        return status, {"ok": True}
    # algunos proxies devuelven body vacío; normalizamos
    if data is None:
        data = {}
    return status, data
