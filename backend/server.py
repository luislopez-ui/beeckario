import json
import os
from datetime import datetime, timezone as dt_timezone, timedelta
from typing import Optional, Dict, List
from pathlib import Path
import platform

from queue import Queue, Empty
from threading import Thread

from dotenv import load_dotenv, find_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pydantic_settings import BaseSettings

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


def _safe_tz(name: str):
    """Return tzinfo with safe fallbacks.

    Uses datetime.timezone.utc when zoneinfo keys are unavailable (common on Windows
    if tzdata is missing or when 'UTC' key is not present).
    """
    n = (name or '').strip()
    if n.upper() in ('CDMX', 'MEXICO_CITY', 'MEXICO CITY'):
        n = 'America/Mexico_City'
    if not n:
        return dt_timezone.utc
    if n.upper() in ('UTC', 'Z'):
        if ZoneInfo is not None:
            for key in ('Etc/UTC', 'UTC'):
                try:
                    return ZoneInfo(key)
                except Exception:
                    pass
        return dt_timezone.utc
    if ZoneInfo is None:
        return dt_timezone.utc
    try:
        return ZoneInfo(n)
    except Exception:
        return dt_timezone.utc



try:
    from langchain_core.tools import tool
except Exception:  # pragma: no cover
    try:
        from langchain.tools import tool  # type: ignore
    except Exception:  # pragma: no cover
        # Minimal fallback so the backend can run in USE_MOCK_MODEL=true without LangChain installed.
        def tool(name: str):  # type: ignore
            def _decorator(fn):
                setattr(fn, "__tool_name__", name)
                return fn
            return _decorator


@tool('clockify_request')
def clockify_request(user_request: str) -> str:
    """Crea/modifica/elimina registros (time entries) en Clockify.

    Entrada: texto libre del usuario (idealmente con pares key=value).
    Salida: JSON (string) con ok/action/status/response.
    """
    try:
        from funciones.clockify.main_clockify import handle_clockify_request
        result = handle_clockify_request(user_request)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False)


try:
    from langchain_core.messages import AIMessageChunk
except Exception:  # pragma: no cover
    AIMessageChunk = object  # type: ignore

# Load .env from project root
load_dotenv(find_dotenv(usecwd=True))


class Settings(BaseSettings):
    openai_api_key: Optional[str] = None
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-5.2")
    temperature: float = float(os.getenv("TEMPERATURE", "0.2"))
    max_output_tokens: int = int(os.getenv("MAX_OUTPUT_TOKENS", "2048"))
    use_responses_api: bool = str(os.getenv("USE_RESPONSES_API", "true")).lower() in ("1", "true", "yes")
    system_prompt: str = os.getenv(
        "SYSTEM_PROMPT",
        "Eres Beeckario, un asistente útil y directo.\n"
        "Si el usuario pide crear, modificar, eliminar o buscar/listar registros en Clockify, usa la herramienta clockify_request.\n"
        "Cuando la herramienta devuelva JSON, NO lo pegues tal cual; resume el resultado en lenguaje natural, indica si fue exitoso, y si hay múltiples coincidencias pide al usuario que elija uno.\n",
    )
    cors_origins: str = "*"  # local desktop
    use_mock_model: bool = str(os.getenv("USE_MOCK_MODEL", "true")).lower() in ("1", "true", "yes")

    class Config:
        env_file = find_dotenv(usecwd=True) or ".env"
        extra = "ignore"


settings = Settings()
OPENAI_API_KEY = settings.openai_api_key or os.getenv("OPENAI_API_KEY")


def parse_origins(val: str):
    v = (val or "").strip()
    if not v or v == "*":
        return ["*"]
    return [x.strip() for x in v.split(",") if x.strip()]


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def extract_text(token) -> str:
    # Best-effort extraction across langchain versions
    if token is None:
        return ""
    if hasattr(token, "text") and isinstance(getattr(token, "text"), str) and getattr(token, "text"):
        return getattr(token, "text")
    if hasattr(token, "content"):
        c = getattr(token, "content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            out = []
            for b in c:
                if isinstance(b, dict) and isinstance(b.get("text"), str):
                    out.append(b["text"])
            return "".join(out)
    return ""




def _format_clockify_result(result: dict) -> str:
    """Render a user-friendly summary for Clockify operations (no raw secrets)."""
    sep = chr(10)

    if not isinstance(result, dict):
        return f"Resultado: {result}" + sep

    action = str(result.get("action") or "").strip() or "clockify"
    ok = bool(result.get("ok"))

    # Batch summary
    if action == "batch":
        summ = result.get("summary") or {}
        total = summ.get("total")
        okc = summ.get("ok")
        fail = summ.get("failed")

        lines = ["", f"Resultado: {'✅' if ok else '❌'} Batch (total={total}, ok={okc}, failed={fail})"]

        results = result.get("results") if isinstance(result.get("results"), list) else []
        bad = [r for r in results if isinstance(r, dict) and not r.get("ok")]
        if bad:
            lines.append("Fallos:")
            for r in bad[:10]:
                idx = r.get("index") or r.get("i")
                err = r.get("error") or (r.get("response") or {}).get("message")
                lines.append(f"- [{idx}] {err}")

        return sep.join(lines) + sep

    # Single operations
    if ok:
        if action == "crear":
            resp = result.get("response") or {}
            rid = resp.get("id") if isinstance(resp, dict) else None
            extra = f" (id={rid})" if rid else ""
            return sep.join(["", "Resultado: ✅ Registro creado" + extra]) + sep

        if action == "modificar":
            return sep.join(["", "Resultado: ✅ Registro modificado"]) + sep

        if action == "eliminar":
            return sep.join(["", "Resultado: ✅ Registro eliminado"]) + sep

        if action == "buscar":
            n = len(result.get("results") or []) if isinstance(result.get("results"), list) else 0
            return sep.join(["", f"Resultado: ✅ Búsqueda OK (coincidencias: {n})"]) + sep

        if action == "listar_proyectos":
            n = int(result.get("count") or 0)
            return sep.join(["", f"Resultado: ✅ Directorio cargado (proyectos: {n})"]) + sep

        return sep.join(["", "Resultado: ✅ OK"]) + sep

    # Fail
    err = result.get("error") or "Operación no exitosa."
    hint = result.get("hint")
    question = result.get("question")

    lines = ["", f"Resultado: ❌ {action}", f"Motivo: {err}"]

    if question:
        lines.append(f"Siguiente paso: {question}")
    if hint:
        lines.append(f"Sugerencia: {hint}")

    e = str(err).lower()
    if "timezone" in e or "zona horaria" in e or "utc" in e:
        lines.append("Sugerencia: revisa CLOCKIFY_TIMEZONE en tu .env (ej. America/Mexico_City).")

    if ("datetime" in e) or (("hora" in e) and ("start" in e or "end" in e)):
        lines.append("Sugerencia: intenta un rango claro: 'de 2 a 4 pm' o '14:00-16:00'.")

    return sep.join(lines) + sep


app = FastAPI(title="Beeckario Backend", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=parse_origins(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)




@app.on_event("startup")
def _bootstrap_runtime():
    """Best-effort bootstrap: cache projects doc and capture current local time.

    This makes first Clockify lookups faster and gives a consistent "hoy" reference
    for prompts/logging.
    """
    tz_name = (os.getenv('CLOCKIFY_TIMEZONE') or 'America/Mexico_City').strip() or 'America/Mexico_City'
    now_local = datetime.now(tz=_safe_tz(tz_name))
    app.state.tz_name = tz_name
    app.state.boot_time_local = now_local.isoformat(timespec='seconds')

    # Preload Excel project catalog into the LRU cache (fast subsequent lookups).
    try:
        from funciones.clockify import project_lookup
        app.state.projects_loaded = int(project_lookup.preload_projects())  # type: ignore
        app.state.projects_error = None
    except Exception as e:  # pragma: no cover
        app.state.projects_loaded = 0
        app.state.projects_error = str(e)


class ChatRequest(BaseModel):
    session_id: str
    message: str


@app.get("/health")
def health():
    return {"ok": True, "timezone": getattr(app.state, 'tz_name', None), "boot_time_local": getattr(app.state, 'boot_time_local', None), "projects_loaded": getattr(app.state, 'projects_loaded', None), "projects_error": getattr(app.state, 'projects_error', None)}



def _truncate_id(val: Optional[str]) -> Optional[str]:
    if not val:
        return val
    s = str(val)
    if len(s) <= 12:
        return s
    return s[:4] + "…" + s[-4:]


def _project_catalog_snapshot(limit_preview: int = 12) -> dict:
    """Return a snapshot of the in-memory project directory cache."""
    try:
        from funciones.clockify import project_lookup

        # Excel file metadata
        root = Path(__file__).resolve().parents[1]
        excel_path = root / "directorios" / "clockify_proyectos.xlsx"
        meta = {
            "path": str(excel_path),
            "exists": excel_path.exists(),
        }
        if excel_path.exists():
            st = excel_path.stat()
            meta.update(
                {
                    "mtime": datetime.fromtimestamp(st.st_mtime, tz=dt_timezone.utc).isoformat(timespec="seconds"),
                    "size_bytes": int(st.st_size),
                }
            )

        # Use the cached loader to reflect what the runtime is really using.
        rows = project_lookup._load_projects()  # type: ignore[attr-defined]

        def has_any_stage(r) -> bool:
            return bool(r.id_discovery or r.id_desarrollo or r.id_deployment)

        total = len(rows)
        missing_cliente = sum(1 for r in rows if not (r.client or "").strip())
        missing_fact = sum(1 for r in rows if r.billable_default is None)
        with_disc = sum(1 for r in rows if (r.id_discovery or "").strip())
        with_dev = sum(1 for r in rows if (r.id_desarrollo or "").strip())
        with_dep = sum(1 for r in rows if (r.id_deployment or "").strip())
        with_farm = sum(1 for r in rows if (r.farming or "").strip())
        with_hunt = sum(1 for r in rows if (r.hunting or "").strip())
        with_any_stage = sum(1 for r in rows if has_any_stage(r))

        clients = []
        seen = set()
        for r in rows:
            c = (r.client or "").strip()
            k = c.lower()
            if c and k not in seen:
                seen.add(k)
                clients.append(c)

        preview = []
        for r in rows[: max(0, int(limit_preview))]:
            preview.append(
                {
                    "proyecto": r.project_name,
                    "cliente": r.client,
                    "facturable": r.billable_default,
                    "ids": {
                        "ID_Discovery": _truncate_id(r.id_discovery),
                        "ID_Desarrollo": _truncate_id(r.id_desarrollo),
                        "ID_Deployment": _truncate_id(r.id_deployment),
                        "Farming": _truncate_id(r.farming),
                        "Hunting": _truncate_id(r.hunting),
                    },
                }
            )

        return {
            "ok": True,
            "meta": meta,
            "stats": {
                "total": total,
                "clients": clients,
                "missing_cliente": missing_cliente,
                "missing_facturable": missing_fact,
                "with_any_stage": with_any_stage,
                "with_ID_Discovery": with_disc,
                "with_ID_Desarrollo": with_dev,
                "with_ID_Deployment": with_dep,
                "with_Farming": with_farm,
                "with_Hunting": with_hunt,
            },
            "preview": preview,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/memory")
def memory_snapshot():
    """Debug endpoint: what the backend has loaded in memory.

    Intended for local troubleshooting (timezone + directory cache + key env config).
    """
    tz_name = (getattr(app.state, "tz_name", None) or os.getenv("CLOCKIFY_TIMEZONE") or "America/Mexico_City")
    tz = _safe_tz(str(tz_name))
    now_local = datetime.now(tz=tz)
    offset = now_local.utcoffset() or timedelta(0)
    offset_hours = int(offset.total_seconds() // 3600)
    offset_minutes = int((abs(offset.total_seconds()) % 3600) // 60)

    # Helpful env (avoid secrets)
    safe_env = {
        "CLOCKIFY_TIMEZONE": os.getenv("CLOCKIFY_TIMEZONE"),
        "CLOCKIFY_DESCRIPTION_TEMPLATE": os.getenv("CLOCKIFY_DESCRIPTION_TEMPLATE"),
        "CLOCKIFY_DEFAULT_TAG_ID": _truncate_id(os.getenv("CLOCKIFY_DEFAULT_TAG_ID")),
        "CLOCKIFY_BULK_MAX": os.getenv("CLOCKIFY_BULK_MAX"),
        "BACKEND_HOST": os.getenv("BACKEND_HOST"),
        "BACKEND_PORT": os.getenv("BACKEND_PORT"),
        "USE_MOCK_MODEL": os.getenv("USE_MOCK_MODEL"),
    }

    return {
        "ok": True,
        "backend": {
            "version": app.version,
            "boot_time_local": getattr(app.state, "boot_time_local", None),
            "timezone_name": tz_name,
            "timezone_resolved_type": type(tz).__name__,
            "timezone_offset": f"UTC{offset_hours:+03d}:{offset_minutes:02d}",
            "now_local": now_local.isoformat(timespec="seconds"),
            "now_utc": datetime.now(tz=dt_timezone.utc).isoformat(timespec="seconds"),
            "zoneinfo_available": bool(ZoneInfo is not None),
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "env": safe_env,
        "projects": _project_catalog_snapshot(),
        "projects_loaded": getattr(app.state, "projects_loaded", None),
        "projects_error": getattr(app.state, "projects_error", None),
    }


# ------------------------
# Mock mode (easy local run)
# ------------------------
_mock_memory: Dict[str, List[str]] = {}


def _looks_like_clockify_intent(text: str) -> bool:
    t = (text or "").lower()
    # If the user already sent an action JSON, assume it's meant for clockify.
    if '"action"' in t and ('modificar' in t or 'eliminar' in t or 'crear' in t or 'buscar' in t):
        return True
    keywords = [
        "clockify",
        "registro",
        "time entry",
        "cargar horas",
        "horas",
        "facturable",
        "proyecto",
    ]
    if any(k in t for k in keywords) and any(w in t for w in ("crear", "modificar", "eliminar", "borrar", "buscar", "listar", "cargar")):
        return True
    # Also accept very explicit patterns like 'billable=true'
    if "billable=" in t or "facturable=" in t:
        return True
    return False


def _mock_stream(session_id: str, user_message: str):
    history = _mock_memory.setdefault(session_id, [])
    history.append(user_message)
    response = f"Beeckario: {user_message}\n(historial: {len(history)})"
    for i in range(0, len(response), 10):
        yield response[i:i + 10]


# ------------------------
# Real mode (LangGraph agent)
# ------------------------
_agent = None
_checkpointer = None


def get_agent():
    global _agent, _checkpointer
    if _agent is not None:
        return _agent

    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY missing. Set it in .env or keep USE_MOCK_MODEL=true.")

    # Lazy imports so the backend can run in USE_MOCK_MODEL=true even if
    # LangChain/OpenAI packages are not installed yet.
    try:
        from langchain_openai import ChatOpenAI
        from langchain.agents import create_agent
        from langgraph.checkpoint.memory import InMemorySaver
    except Exception as e:  # pragma: no cover
        raise RuntimeError(
            'Faltan dependencias para modo real (LangChain/OpenAI). '
            'Instala requirements.txt o habilita USE_MOCK_MODEL=true.'
        ) from e

    llm = ChatOpenAI(
        model=settings.openai_model,
        temperature=settings.temperature,
        use_responses_api=settings.use_responses_api,
        api_key=OPENAI_API_KEY,
        max_tokens=settings.max_output_tokens,
    )

    _checkpointer = InMemorySaver()
    _agent = create_agent(
        model=llm,
        tools=[clockify_request],
        system_prompt=settings.system_prompt,
        checkpointer=_checkpointer,
    )
    return _agent


@app.post("/api/chat/stream")
def chat_stream(req: ChatRequest):
    """
    SSE streaming:
      - token: {"text": "..."}
      - done: {"ok": true}
      - error: {"message": "..."}
    """
    def gen():
        try:
            # Intercept Clockify intents to provide deterministic tracing output.
            if _looks_like_clockify_intent(req.message):
                trace_enabled = str(os.getenv('BEECKARIO_TRACE', '1')).lower() in ('1', 'true', 'yes')
                yield sse('token', {'text': 'Beeckario:\n'})

                q: Queue = Queue()

                def _trace_cb(line: str) -> None:
                    if not trace_enabled:
                        return
                    q.put(('trace', line))

                def _worker() -> None:
                    try:
                        from funciones.clockify.main_clockify import handle_clockify_request
                        res = handle_clockify_request(req.message, trace=_trace_cb)
                        q.put(('result', res))
                    except Exception as e:
                        q.put(('error', str(e)))
                    finally:
                        q.put(('done', None))

                Thread(target=_worker, daemon=True).start()

                result = None
                while True:
                    kind, payload = q.get()
                    if kind == 'trace':
                        yield sse('token', {'text': str(payload) + '\n'})
                        continue
                    if kind == 'result':
                        result = payload
                        continue
                    if kind == 'error':
                        yield sse('token', {'text': f"❌ Error: {payload}\n"})
                        break
                    if kind == 'done':
                        break

                if isinstance(result, dict):
                    yield sse('token', {'text': _format_clockify_result(result)})
                yield sse('done', {'ok': True})
                return

            if settings.use_mock_model:
                # In mock mode, still execute Clockify tool if the message looks like
                # a Clockify request (so the desktop app remains functional without
                # an OpenAI key).
                if _looks_like_clockify_intent(req.message):
                    out = clockify_request(req.message)
                    for i in range(0, len(out), 80):
                        yield sse("token", {"text": out[i:i + 80]})
                    yield sse("done", {"ok": True})
                    return

                for t in _mock_stream(req.session_id, req.message):
                    yield sse("token", {"text": t})
                yield sse("done", {"ok": True})
                return

            agent = get_agent()
            config = {"configurable": {"thread_id": req.session_id}}
            inputs = {"messages": [{"role": "user", "content": req.message}]}

            for mode, data in agent.stream(inputs, config, stream_mode=["messages"]):
                if mode != "messages":
                    continue
                token, _meta = data
                text = extract_text(token)
                if text:
                    yield sse("token", {"text": text})

            yield sse("done", {"ok": True})
        except Exception as e:
            yield sse("error", {"message": str(e)})

    return StreamingResponse(gen(), media_type="text/event-stream")
