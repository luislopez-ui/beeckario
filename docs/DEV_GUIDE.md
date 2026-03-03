# Beeckario – Guía para Desarrollo

Esta guía explica **cómo está armado Beeckario**, cuáles son los **puntos de entrada**, qué módulos procesan cada cosa y dónde tocar cuando necesites cambiar comportamiento.

## 1) Vista general
Beeckario es una app de escritorio (PySide6) que habla con un backend local (FastAPI) mediante **SSE** para tener streaming de tokens.

- **UI**: `Beeckario/main.py`
- **Backend**: `Beeckario/backend/server.py`
- **Clockify Tool**: `Beeckario/funciones/clockify/main_clockify.py`
- **Directorio (Excel)**: `Beeckario/directorios/clockify_proyectos.xlsx`

## 2) Estructura del repo
```
Beeckario/
  main.py                     # UI PySide6 (widget + chat)
  backend/server.py           # FastAPI (SSE) + Agent
  directorios/clockify_proyectos.xlsx
  funciones/
    storage.py                # persistencia local (posición/estado)
    clockify/
      main_clockify.py        # orquestador Clockify (tool)
      project_lookup.py       # lectura/búsqueda del Excel
      time_entry_lookup.py    # búsqueda de time entries para modificar/eliminar
      flows/                  # wrappers REST (crear/modificar/eliminar)
      prompts/system_clockify.txt
      clockify_agent.py       # parser semántico opcional (OpenAI)
```

## 3) Variables de entorno (.env)
Ver `.env.example`. Las principales:

- **Backend/UI**
  - `AUTOSTART_BACKEND=true|false`
  - `BACKEND_HOST`, `BACKEND_PORT`
  - `USE_MOCK_MODEL=true|false`

- **OpenAI / agente**
  - `OPENAI_API_KEY`
  - `OPENAI_MODEL`
  - `TEMPERATURE`, `MAX_OUTPUT_TOKENS`

- **Clockify**
  - `CLOCKIFY_API_KEY`
  - `CLOCKIFY_WORKSPACE_ID`
  - `CLOCKIFY_BASE_URL` (default Clockify v1)
  - `CLOCKIFY_TIMEZONE` (ej. `America/Mexico_City`)
  - `CLOCKIFY_DEFAULT_TAG_ID` (Arquitectura)
  - `CLOCKIFY_DESCRIPTION_TEMPLATE` (por defecto: `{cliente} | {proyecto} | {actividad}`)

## 4) Flujo completo (UI → Backend → Clockify)

### 4.1 UI (PySide6)
Archivo: `main.py`

Clases clave:
- `ChatInput`: input multiline (Enter envía, Shift+Enter nueva línea).
- `SSEClient`: ejecuta request SSE en un thread y emite señales Qt:
  - `token(str)`, `done()`, `error(str)`
- `Bubble`: render de un mensaje (user derecha, assistant izquierda).

Flujo:
1) Usuario envía texto
2) `SSEClient.start(session_id, message)` → POST `backend/server.py:/api/chat/stream`
3) Por cada `event: token` se va acumulando texto del assistant

### 4.2 Backend (FastAPI)
Archivo: `backend/server.py`

Endpoints:
- `GET /health`: diagnóstico (timezone, boot_time, projects_loaded)
- `POST /api/chat/stream`: SSE streaming

Modo mock (`USE_MOCK_MODEL=true`):
- Si detecta intención de Clockify → llama directo `clockify_request()`
- Si no → solo “eco” simulando tokens

Modo real (`USE_MOCK_MODEL=false`):
- Crea un agente (LangChain) con tool `clockify_request`
- El modelo decide cuándo llamar Clockify

### 4.3 Tool Clockify
Archivo: `funciones/clockify/main_clockify.py`

Entry point:
- `handle_clockify_request(user_request: str)`

Batch:
- Si el mensaje contiene varias tareas (lista numerada/bullets), las separa y las ejecuta **secuencialmente** bajo un lock global (`_CLOCKIFY_LOCK`).

Pipeline (single request):
1) `procesar_solicitud_clockify(texto)`
2) Intenta:
   - JSON directo
   - plan JSON del `clockify_agent` (opcional)
   - parsing determinista (key=value) + heurísticas
3) Resuelve proyecto via Excel (`project_lookup.resolve_project_id`)
4) Construye descripción final con template:
   - Cliente (Excel)
   - Proyecto (Excel)
   - Actividad (inferida o provista por usuario)
5) `billable`:
   - si usuario lo define, gana
   - si no, toma Excel (columna `Facturable`)
6) `taskId` por etapa:
   - discovery/desarrollo/deployment/farming/hunting
   - **default**: desarrollo
7) Aplica reglas deterministas (`_apply_business_rules`):
   - tag default (Arquitectura) en crear
   - comida (no facturable)
   - hora extra (tag extra + sufijo)
8) Ejecuta API Clockify:
   - create: `flows/crear_registro.py`
   - modify: `flows/modificar_registro.py`
   - delete: `flows/eliminar_registro.py`

## 5) Directorio Excel
Archivo: `directorios/clockify_proyectos.xlsx`

Columnas esperadas (exactas):
- `Proyecto`: código (ej. `AER.MCC.004`)
- `ID`: projectId de Clockify
- `Cliente`: nombre del cliente para la descripción
- `Facturable`: `Si/No`
- `ID_Discovery`, `ID_Desarrollo`, `ID_Deployment`: taskId por etapa
- `Farming`, `Hunting`: taskId para preventa

Ver también `docs/CLOCKIFY_DIRECTORY_SCHEMA.md`.

## 6) Dónde tocar para cambios comunes

### Cambiar formato de descripción
- `.env`: `CLOCKIFY_DESCRIPTION_TEMPLATE`
- Código: `main_clockify.py` (busca `description_template`)

### Agregar una nueva regla de negocio
- `main_clockify.py` → `_apply_business_rules()`

### Ajustar cómo se infiere etapa (discovery/desarrollo/deployment)
- `main_clockify.py` → `_infer_stage_from_text()`

### Ajustar resolución de proyectos
- `project_lookup.py` → `normalize_project_code()` y `resolve_project_id()`

### Agregar una nueva acción (ej. “pausar”, “duplicar”)
- `main_clockify.py` → `_detect_action()` + branch en `procesar_solicitud_clockify()`

## 7) Troubleshooting

- **Horas corridas 6h**: revisa `CLOCKIFY_TIMEZONE` y que `tzdata` esté instalado en Windows.
- **AER.MCC.004 se vuelve MCC.004**: revisa `normalize_project_code()` (ya soporta 3-part).
- **No encuentra proyectos**: verifica que el Excel esté en `directorios/clockify_proyectos.xlsx` y que las columnas coincidan.
- **Mock mode sin OpenAI**: el sistema sigue creando registros si el texto trae datos suficientes; el agente semántico puede no correr.
