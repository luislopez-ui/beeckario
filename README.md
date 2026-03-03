# Beeckario (Desktop Widget + Backend)

## Run
```bash
cd Beeckario
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

- Aparece un **botón widget** (abajo-derecha). Click para abrir/cerrar el chat.
- La ventana del chat tiene **minimizar y cerrar**.
- Al cerrar el chat (X) NO se cierra la app: solo se oculta (el widget sigue).
- Click derecho en el widget -> **Salir**.

## Backend manual
Si quieres levantar backend aparte:
```bash
uvicorn backend.server:app --reload --port 8000
```
y en `.env` pon `AUTOSTART_BACKEND=false`.

## Modelo
Por defecto `OPENAI_MODEL=gpt-5.2`. Si tu cuenta no lo tiene, cambia a `gpt-5.1` o `gpt-5` en `.env`.


## Funciones agregadas (UX)
- **Burbujas estilo chatbot** (usuario derecha, asistente izquierda).
- **Enter envía**, **Shift+Enter** agrega salto de línea.
- **Indicador "…"** mientras Beeckario responde.
- **Ctrl+K** enfoca el input.
- **Esc** oculta el chat.
- Botón **Limpiar**.
- El botón/widget se puede **arrastrar** y recuerda su posición.
- Al cerrar el chat (X) **no se cierra la app**, solo se oculta.

## Persistencia
Se guarda estado local (posición, tamaño, sesión y mensajes) en un `state.json` dentro de la carpeta de configuración del usuario.


## UI (estilo Messenger)
La vista del chat ahora es 100% nativa (Qt widgets), sin HTML/CSS. Esto evita el render raro de `QTextBrowser` y se ve más limpio/profesional.
