import json
import pytest
from fastapi.testclient import TestClient

from server import app


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("USE_MOCK_MODEL", "true")
    return TestClient(app)


def parse_sse(text: str):
    events = []
    blocks = [b for b in text.split("\n\n") if b.strip()]
    for b in blocks:
        event = None
        data = None
        for ln in b.splitlines():
            if ln.startswith("event:"):
                event = ln.split(":", 1)[1].strip()
            if ln.startswith("data:"):
                data = json.loads(ln.split(":", 1)[1].strip())
        if event and data is not None:
            events.append((event, data))
    return events


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_stream_tokens_and_done(client):
    r = client.post("/api/chat/stream", json={"session_id": "s1", "message": "hola"})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers.get("content-type", "")
    ev = parse_sse(r.text)
    text = "".join(d["text"] for e, d in ev if e == "token")
    assert "Beeckario:" in text
    assert "hola" in text
    assert ev[-1][0] == "done"


def test_session_memory_increments(client):
    r1 = client.post("/api/chat/stream", json={"session_id": "s2", "message": "uno"})
    t1 = "".join(d["text"] for e, d in parse_sse(r1.text) if e == "token")
    assert "historial: 1" in t1

    r2 = client.post("/api/chat/stream", json={"session_id": "s2", "message": "dos"})
    t2 = "".join(d["text"] for e, d in parse_sse(r2.text) if e == "token")
    assert "historial: 2" in t2
