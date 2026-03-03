from __future__ import annotations

from typing import Any, Dict, Tuple


def crear_registro(client, payload: Dict[str, Any]) -> Tuple[int, Any]:
    """Crea un registro (time entry) en Clockify.

    Clockify API v1:
      POST /v1/workspaces/{workspaceId}/time-entries

    Args:
        client: instancia de ClockifyClient
        payload: JSON del time entry (campos como start/end/description/projectId...)

    Returns:
        (status_code, response_json)
    """
    path = f"/workspaces/{client.workspace_id}/time-entries"
    status, data = client.request_json("POST", path, json_body=payload)
    return status, data
