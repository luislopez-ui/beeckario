import os
import json
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

import httpx
from dotenv import load_dotenv, find_dotenv

from PySide6.QtCore import Qt, QObject, Signal, QPoint, QTimer
from PySide6.QtGui import QIcon, QCursor, QKeySequence, QAction
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QMenu,
    QPlainTextEdit,
    QToolButton,
    QLabel,
    QScrollArea,
    QSizePolicy,
)

from funciones.storage import load_state, save_state, clamp_int

# Load root .env
load_dotenv(find_dotenv(usecwd=True))

BACKEND_HOST = os.getenv("BACKEND_HOST", "127.0.0.1")
BACKEND_PORT = int(os.getenv("BACKEND_PORT", "8000"))
BACKEND_URL = f"http://{BACKEND_HOST}:{BACKEND_PORT}"
AUTOSTART_BACKEND = os.getenv("AUTOSTART_BACKEND", "true").lower() in ("1", "true", "yes")

# Used only for UI formatting of timestamps coming from Clockify (which are in UTC, ISO Z)
LOCAL_TZ = os.getenv("CLOCKIFY_TIMEZONE", "America/Mexico_City")


@dataclass
class Msg:
    role: str  # "user" | "assistant"
    text: str


def start_backend_thread():
    from backend.server import app as fastapi_app
    import uvicorn

    def run():
        config = uvicorn.Config(
            fastapi_app,
            host=BACKEND_HOST,
            port=BACKEND_PORT,
            log_level="warning",
            reload=False,
        )
        uvicorn.Server(config).run()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def backend_is_up(timeout=0.5) -> bool:
    try:
        r = httpx.get(f"{BACKEND_URL}/health", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


class SSEClient(QObject):
    token = Signal(str)
    done = Signal()
    error = Signal(str)

    def __init__(self):
        super().__init__()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self, session_id: str, message: str):
        self.stop()
        self._stop.clear()

        def run():
            url = f"{BACKEND_URL}/api/chat/stream"
            payload = {"session_id": session_id, "message": message}
            try:
                with httpx.Client(timeout=None) as client:
                    with client.stream("POST", url, json=payload, headers={"Accept": "text/event-stream"}) as resp:
                        resp.raise_for_status()
                        buf = ""
                        for chunk in resp.iter_text():
                            if self._stop.is_set():
                                return
                            if not chunk:
                                continue
                            buf += chunk
                            while "\n\n" in buf:
                                block, buf = buf.split("\n\n", 1)
                                event_name = None
                                data_line = None
                                for ln in block.splitlines():
                                    ln = ln.strip()
                                    if ln.startswith("event:"):
                                        event_name = ln.split(":", 1)[1].strip()
                                    elif ln.startswith("data:"):
                                        data_line = ln.split(":", 1)[1].strip()

                                if not event_name or data_line is None:
                                    continue

                                try:
                                    data = httpx.Response(200, text=data_line).json()
                                except Exception:
                                    continue

                                if event_name == "token":
                                    self.token.emit(str(data.get("text", "")))
                                elif event_name == "done":
                                    self.done.emit()
                                    return
                                elif event_name == "error":
                                    self.error.emit(str(data.get("message", "unknown error")))
                                    return
                        self.done.emit()
            except Exception as e:
                self.error.emit(str(e))

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()


class ChatInput(QPlainTextEdit):
    """
    Multiline input:
      - Enter sends
      - Shift+Enter inserts newline
    """
    sendRequested = Signal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if event.modifiers() & Qt.ShiftModifier:
                super().keyPressEvent(event)
                return
            event.accept()
            self.sendRequested.emit()
            return
        super().keyPressEvent(event)


class Bubble(QWidget):
    """
    A single message row, styled like Messenger (no HTML/CSS dependency).
    """
    def __init__(self, role: str, text: str):
        super().__init__()
        self.role = role
        self._text = text

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        # User messages should appear on the RIGHT:
        # add stretch FIRST so the bubble is pushed to the right edge.
        if role == "user":
            row.addStretch(1)

        if role == "assistant":
            self.avatar = QLabel("B")
            self.avatar.setFixedSize(28, 28)
            self.avatar.setAlignment(Qt.AlignCenter)
            self.avatar.setStyleSheet(
                "QLabel{background:#CC02A7; color:#0b0f14; border-radius:10px; font-weight:900;}"
            )
            row.addWidget(self.avatar)

        # bubble container
        self.bubble = QWidget()
        b = QVBoxLayout(self.bubble)
        b.setContentsMargins(12, 10, 12, 10)
        b.setSpacing(6)

        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # Don't let the label expand horizontally beyond the viewport; we
        # control width via set_max_width() based on the scroll viewport.
        self.label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self.label.setStyleSheet("QLabel{font-size:14px; line-height:1.4;}")

        b.addWidget(self.label)

        # Optional actions (buttons) shown under the message (e.g., disambiguation choices)
        self._actions_wrap = QWidget()
        self._actions_row = QHBoxLayout(self._actions_wrap)
        self._actions_row.setContentsMargins(0, 0, 0, 0)
        self._actions_row.setSpacing(6)
        self._actions_wrap.hide()
        b.addWidget(self._actions_wrap)
        row.addWidget(self.bubble, 0)

        # For assistant messages, keep them left by adding stretch AFTER bubble
        if role == "assistant":
            row.addStretch(1)

        self.set_text(text)

        # Styles
        if role == "user":
            self.bubble.setStyleSheet(
                "QWidget{background:#0202CC; color:#ffffff; border-radius:18px; padding:0px;}"
            )
        else:
            self.bubble.setStyleSheet(
                "QWidget{background:#1f2937; color:#e8eef6; border-radius:18px; padding:0px;}"
            )

        self._max_width_px: Optional[int] = None

    def set_max_width(self, px: int):
        """Limit bubble width to keep it inside the scroll viewport."""
        try:
            px_i = int(px)
        except Exception:
            return
        px_i = max(220, px_i)
        if self._max_width_px == px_i:
            return
        self._max_width_px = px_i
        self.bubble.setMaximumWidth(px_i)
        # account for bubble padding (12+12) and some slack
        self.label.setMaximumWidth(max(180, px_i - 30))

    def set_text(self, text: str):
        self._text = text
        # Keep it clean: show as plain text (no markdown weird rendering)
        self.label.setText(text)

    def set_actions(self, actions: List[tuple]):
        """Render small action buttons below the message.

        actions: list of (label, callback)
        """
        # Clear existing
        while self._actions_row.count():
            item = self._actions_row.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        if not actions:
            self._actions_wrap.hide()
            return

        for label, cb in actions:
            btn = QPushButton(str(label))
            btn.setCursor(QCursor(Qt.PointingHandCursor))
            btn.setStyleSheet(
                "QPushButton{padding:6px 10px; border-radius:10px; background:rgba(255,255,255,0.10); color:#e8eef6; font-weight:700;}"
                "QPushButton:hover{background:rgba(255,255,255,0.18);}"
                "QPushButton:pressed{background:rgba(255,255,255,0.24);}"
            )
            btn.clicked.connect(cb)
            self._actions_row.addWidget(btn)

        self._actions_row.addStretch(1)
        self._actions_wrap.show()


class ChatThread(QWidget):
    def __init__(self):
        super().__init__()
        self.v = QVBoxLayout(self)
        self.v.setContentsMargins(0, 0, 0, 0)
        self.v.setSpacing(10)
        self.v.addStretch(1)  # spacer at bottom for nicer scroll behavior

        self._bubbles: List[Bubble] = []

    def clear_messages(self):
        # Remove all bubble widgets (keep bottom stretch)
        for b in self._bubbles:
            self.v.removeWidget(b)
            b.deleteLater()
        self._bubbles.clear()

    def add_bubble(self, role: str, text: str) -> Bubble:
        bubble = Bubble(role, text)
        # insert before the stretch spacer
        self.v.insertWidget(self.v.count() - 1, bubble)
        self._bubbles.append(bubble)
        return bubble


class ChatWindow(QMainWindow):
    def __init__(self, icon_path: str, state: dict):
        super().__init__()
        self._state = state
        self._state_dirty = False

        self.setWindowTitle("Beeckario — Asistente")
        self.setWindowFlags(
            Qt.Window
            | Qt.WindowStaysOnTopHint
            | Qt.CustomizeWindowHint
            | Qt.WindowTitleHint
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowCloseButtonHint
        )
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        # Session + messages
        self.session_id = state.get("session_id") or str(uuid.uuid4())
        state["session_id"] = self.session_id

        saved_msgs = state.get("messages") or []
        self.messages: List[Msg] = []
        if isinstance(saved_msgs, list) and saved_msgs:
            for m in saved_msgs[-300:]:
                if isinstance(m, dict) and "role" in m and "text" in m:
                    self.messages.append(Msg(str(m["role"]), str(m["text"])))
        if not self.messages:
            self.messages = [Msg("assistant", "Hola 👋 Soy Beeckario. ¿Qué necesitas hoy?")]

        self.stream = SSEClient()
        self._sending = False
        self._current_assistant_bubble: Optional[Bubble] = None

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # Header
        header = QHBoxLayout()
        title = QLabel("Beeckario")
        title.setStyleSheet("color:#e8eef6; font-weight:800; font-size:14px;")
        header.addWidget(title)
        header.addStretch(1)

        self.clear_btn = QToolButton()
        self.clear_btn.setText("Limpiar")
        self.clear_btn.setStyleSheet(
            "QToolButton{color:#e8eef6; background:rgba(255,255,255,0.06); padding:6px 10px; border-radius:10px;}"
            "QToolButton:hover{background:rgba(255,255,255,0.10);}"
        )
        header.addWidget(self.clear_btn)
        layout.addLayout(header)

        # Chat area (native widgets)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet(
            "QScrollArea{background:#0b0f14; border: 1px solid rgba(255,255,255,0.10); border-radius:14px;}"
            "QScrollBar:vertical{width:10px; background:transparent;}"
            "QScrollBar::handle:vertical{background:rgba(255,255,255,0.18); border-radius:5px; min-height:20px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
        )

        self.thread = ChatThread()
        self.thread.setStyleSheet("background:transparent;")
        self.scroll.setWidget(self.thread)
        layout.addWidget(self.scroll, 1)

        # Composer
        bottom = QHBoxLayout()
        self.input = ChatInput()
        self.input.setPlaceholderText("Escribe un mensaje… (Enter envía, Shift+Enter nueva línea)")
        self.input.setFixedHeight(64)
        self.input.setStyleSheet(
            "padding:10px 12px; border-radius: 12px; background:#0b1220; color:#e8eef6; "
            "border:1px solid rgba(255,255,255,0.16);"
        )

        self.send_btn = QPushButton("Enviar")
        self.send_btn.setStyleSheet(
            "padding:10px 14px; border-radius: 12px; background:#22c55e; color:#0b0f14; font-weight:800;"
        )

        bottom.addWidget(self.input, 1)
        bottom.addWidget(self.send_btn)
        layout.addLayout(bottom)

        # Signals
        self.send_btn.clicked.connect(self.on_send)
        self.input.sendRequested.connect(self.on_send)
        self.clear_btn.clicked.connect(self.on_clear)

        self.stream.token.connect(self.on_token)
        self.stream.done.connect(self.on_done)
        self.stream.error.connect(self.on_error)

        # Shortcuts with QAction (more compatible than QShortcut in some builds)
        act_focus = QAction(self)
        act_focus.setShortcut(QKeySequence("Ctrl+K"))
        act_focus.triggered.connect(lambda: self.input.setFocus())
        self.addAction(act_focus)

        act_hide = QAction(self)
        act_hide.setShortcut(QKeySequence("Esc"))
        act_hide.triggered.connect(self.hide)
        self.addAction(act_hide)

        # Autosave (debounced)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._flush_state)

        # Restore geometry
        geom = state.get("chat_geometry")
        if isinstance(geom, dict):
            w = clamp_int(geom.get("w"), 320, 900, 420)
            h = clamp_int(geom.get("h"), 420, 1000, 620)
            self.resize(w, h)
        else:
            self.resize(420, 620)

        self._render_full()

        # Debug: show what the backend has in memory on startup.
        #
        # IMPORTANT: the chat window is usually hidden at launch (only the
        # launcher bubble is shown). We still want the debug message to be
        # present as soon as the user opens the chat.
        #
        # Do NOT block the UI thread here. We fetch in a worker thread with
        # retries until the backend is ready.
        self._startup_fetcher = _StartupMemoryFetcher(
            backend_url=BACKEND_URL,
            autostart=AUTOSTART_BACKEND,
        )
        self._startup_fetcher.fetched.connect(self._on_startup_memory_fetched)
        self._startup_fetcher.failed.connect(self._on_startup_memory_failed)
        self._startup_fetcher.start()

    def _scroll_to_bottom(self):
        QTimer.singleShot(0, lambda: self.scroll.verticalScrollBar().setValue(self.scroll.verticalScrollBar().maximum()))

    def _update_bubble_widths(self):
        """Keep message bubbles within the current viewport width.

        Without this, QLabel can expand beyond the scroll viewport, causing
        messages to "run off" the screen.
        """
        try:
            vw = int(self.scroll.viewport().width())
        except Exception:
            return
        if vw <= 0:
            return
        # Leave space for padding + avatar and keep a messenger-like look.
        max_bubble = int(vw * 0.74)
        for b in getattr(self.thread, "_bubbles", []):
            try:
                b.set_max_width(max_bubble)
            except Exception:
                pass

    def move_to_bottom_right(self):
        screen = QApplication.primaryScreen().availableGeometry()
        x = screen.x() + screen.width() - self.width() - 16
        y = screen.y() + screen.height() - self.height() - 16
        self.move(x, y)

    def showEvent(self, event):
        super().showEvent(event)
        if not isinstance(self._state.get("chat_pos"), dict):
            self.move_to_bottom_right()
        else:
            pos = self._state["chat_pos"]
            self.move(int(pos.get("x", 0)), int(pos.get("y", 0)))

    def moveEvent(self, event):
        super().moveEvent(event)
        p = self.pos()
        self._state["chat_pos"] = {"x": int(p.x()), "y": int(p.y())}
        self._mark_dirty()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._state["chat_geometry"] = {"w": int(self.width()), "h": int(self.height())}
        self._update_bubble_widths()
        self._mark_dirty()

    def closeEvent(self, event):
        self.stream.stop()
        try:
            if hasattr(self, "_startup_fetcher"):
                self._startup_fetcher.stop()
        except Exception:
            pass
        event.ignore()
        self.hide()
        self._flush_state()

    def _mark_dirty(self):
        self._state_dirty = True
        self._save_timer.start(400)

    def _flush_state(self):
        if not self._state_dirty:
            return
        self._state["messages"] = [{"role": m.role, "text": m.text} for m in self.messages[-300:]]
        save_state(self._state)
        self._state_dirty = False

    def _render_full(self):
        self.thread.clear_messages()
        for m in self.messages:
            self.thread.add_bubble(m.role, m.text)
        self._current_assistant_bubble = None
        self._update_bubble_widths()
        self._scroll_to_bottom()
        self._mark_dirty()

    def set_sending(self, sending: bool):
        self._sending = sending
        self.send_btn.setDisabled(sending)

    def on_clear(self):
        self.messages = [Msg("assistant", "Listo. Empecemos de nuevo 🙂")]
        self._render_full()
        # When debugging issues (e.g., timezone/hour formatting), it's useful to
        # see the startup memory snapshot again after clearing.
        try:
            if hasattr(self, "_startup_fetcher"):
                self._startup_fetcher.start()
        except Exception:
            pass

    def ensure_backend_ready(self) -> bool:
        if backend_is_up():
            return True
        if AUTOSTART_BACKEND:
            start_backend_thread()
            for _ in range(25):
                if backend_is_up(timeout=0.4):
                    return True
                time.sleep(0.1)
        return backend_is_up(timeout=0.6)

    def on_send(self):
        text = self.input.toPlainText().strip()
        if not text:
            return
        self.input.clear()
        self._send_message(text)

    def _send_message(self, text: str):
        """Send a message (used by both the composer and action buttons)."""
        if self._sending:
            return

        if not self.ensure_backend_ready():
            self.messages.append(Msg("user", text))
            self.messages.append(Msg("assistant", "⚠️ No pude conectar al backend. Verifica que esté arriba."))
            self._render_full()
            return

        self.messages.append(Msg("user", text))
        self.messages.append(Msg("assistant", "…"))

        # Incremental UI update (no full re-render required for streaming)
        self.thread.add_bubble("user", text)
        self._current_assistant_bubble = self.thread.add_bubble("assistant", "…")
        self._update_bubble_widths()
        self._scroll_to_bottom()

        self.set_sending(True)
        self.stream.start(self.session_id, text)
        self._mark_dirty()

    def on_token(self, t: str):
        if not self.messages or self.messages[-1].role != "assistant":
            self.messages.append(Msg("assistant", ""))

        # Replace typing placeholder on first token
        if self.messages[-1].text.strip() == "…":
            self.messages[-1].text = ""
            if self._current_assistant_bubble:
                self._current_assistant_bubble.set_text("")

        self.messages[-1].text += t
        if self._current_assistant_bubble:
            self._current_assistant_bubble.set_text(self.messages[-1].text)
        # Keep bubbles within viewport as text grows
        self._update_bubble_widths()
        self._scroll_to_bottom()
        self._mark_dirty()

    # NOTE: _update_bubble_widths() and resizeEvent() are defined earlier in
    # this class. Do not re-define them below (it makes future debugging harder).

    def on_done(self):
        if self.messages and self.messages[-1].role == "assistant" and self.messages[-1].text.strip() == "…":
            self.messages[-1].text = "(sin respuesta)"
            if self._current_assistant_bubble:
                self._current_assistant_bubble.set_text(self.messages[-1].text)

        # Post-process structured Clockify tool results to:
        # 1) avoid dumping raw JSON in chat
        # 2) show disambiguation buttons when multiple matches exist
        self._postprocess_last_assistant_message()
        self.set_sending(False)
        self._mark_dirty()

    def _extract_json_obj(self, text: str):
        """Extract a JSON dict from assistant text.

        Supports both:
          A) Pure JSON: {"ok": true, ...}
          B) JSON prefix + text (common in SSE concatenation):
             {"ok": true, ...}Listo. ...
        """
        t = (text or "").strip()
        if not t:
            return None

        # Case A: pure JSON
        if t.startswith("{") and t.endswith("}"):
            try:
                obj = json.loads(t)
                return obj if isinstance(obj, dict) else None
            except Exception:
                return None

        # Case B: JSON prefix + trailing text
        split = self._split_json_prefix(t)
        if split and isinstance(split[0], dict):
            return split[0]

        return None

    def _split_json_prefix(self, text: str):
        """Extract the first JSON object prefix from a string.

        Returns (obj_dict, rest_text) or None.
        Uses brace-matching aware of JSON strings/escapes.
        """
        s = text or ""
        if "{" not in s:
            return None

        start = s.find("{")
        in_str = False
        esc = False
        depth = 0
        end = None

        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue

            if ch == '"':
                in_str = True
                continue
            if ch == "{":
                depth += 1
                continue
            if ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break

        if end is None:
            return None

        prefix = s[start:end + 1].strip()
        rest = s[end + 1:].strip()
        try:
            obj = json.loads(prefix)
            if isinstance(obj, dict):
                return obj, rest
        except Exception:
            return None

        return None

    def _fmt_iso_to_local_hm(self, iso_z: str) -> str:
        tz_name = os.getenv("CLOCKIFY_TIMEZONE", "America/Mexico_City")
        if str(tz_name).strip().upper() in ('CDMX', 'MEXICO_CITY', 'MEXICO CITY'):
            tz_name = 'America/Mexico_City'
        try:
            if not iso_z:
                return ""
            dt = None
            if iso_z.endswith("Z"):
                dt = datetime.fromisoformat(iso_z[:-1] + "+00:00")
            else:
                dt = datetime.fromisoformat(iso_z)
            if ZoneInfo:
                try:
                    key = 'Etc/UTC' if str(tz_name).strip().upper() in ('UTC', 'Z') else tz_name
                    dt = dt.astimezone(ZoneInfo(key))
                except Exception:
                    pass
            return dt.strftime("%H:%M")
        except Exception:
            return str(iso_z)

    def _postprocess_last_assistant_message(self):
        if not self.messages or self.messages[-1].role != "assistant":
            return
        raw = self.messages[-1].text
        rest_text = ""

        # Some backends append a human-friendly sentence after the JSON tool
        # envelope (e.g. "{...}Listo. ..."). Split and parse the JSON prefix.
        split = self._split_json_prefix((raw or "").strip())
        if split and isinstance(split[0], dict):
            obj, rest_text = split
        else:
            obj = self._extract_json_obj(raw)
        if not isinstance(obj, dict):
            return

        # Only handle the Clockify tool envelope
        if "action" not in obj and "ok" not in obj:
            return

        # Build a friendly message
        ok = bool(obj.get("ok"))
        action = str(obj.get("action") or "").strip()
        err = obj.get("error")
        msg_lines: List[str] = []

        # If enabled, show internal trace/debug steps produced by the backend.
        # This is UI-only and never affects the actual API calls.
        trace_enabled = str(os.getenv("BEECKARIO_TRACE", "1")).lower() in ("1", "true", "yes")
        trace_lines = obj.get("trace") if isinstance(obj.get("trace"), list) else []

        def _entry_summary(e: dict) -> str:
            ti = e.get("timeInterval") or {}
            start = self._fmt_iso_to_local_hm(ti.get("start") or e.get("start") or "")
            end = self._fmt_iso_to_local_hm(ti.get("end") or e.get("end") or "")
            desc = str(e.get("description") or "(sin descripción)")
            bill = "Sí" if e.get("billable") else "No"
            return f"{start}–{end} · {desc} · Facturable: {bill}".strip()

        if ok:
            if action in ("eliminar_registro", "eliminar"):
                if obj.get("message"):
                    msg_lines.append("✅ " + str(obj.get("message")))
                else:
                    msg_lines.append("✅ Registro eliminado.")
            elif action in ("buscar_registro", "buscar"):
                cnt = int(obj.get("count") or 0)
                msg_lines.append(f"🔎 Encontré {cnt} registro(s).")
                for e in (obj.get("matches") or [])[:6]:
                    if isinstance(e, dict):
                        msg_lines.append("• " + _entry_summary(e))
            else:
                # create/modify typically return the full entry in `response`
                resp = obj.get("response")
                if isinstance(resp, dict):
                    msg_lines.append("✅ " + _entry_summary(resp))
                else:
                    msg_lines.append("✅ Listo.")

            # When tracing is enabled, show HTTP status + request JSON even on success.
            if trace_enabled:
                status = obj.get("status")
                if status is not None and str(status).strip():
                    msg_lines.append(f"HTTP {status}")

                req = obj.get("request_json")
                if req is not None and req != "":
                    try:
                        req_pretty = (
                            json.dumps(req, ensure_ascii=False, indent=2)
                            if isinstance(req, (dict, list))
                            else str(req)
                        )
                    except Exception:
                        req_pretty = str(req)
                    msg_lines.append("\n🧾 JSON enviado:\n" + req_pretty)

                # Small response summary (avoid dumping huge payloads)
                resp = obj.get("response")
                if isinstance(resp, dict):
                    rid = resp.get("id")
                    ti = resp.get("timeInterval") or {}
                    s_iso = ti.get("start")
                    e_iso = ti.get("end")
                    dur = ti.get("duration")
                    parts = []
                    if rid:
                        parts.append(f"id={rid}")
                    if s_iso and e_iso:
                        parts.append(
                            f"intervalo_utc={s_iso} → {e_iso}" + (f" ({dur})" if dur else "")
                        )
                    if parts:
                        msg_lines.append("🧾 Respuesta (resumen): " + ", ".join(parts))
        else:
            if err:
                msg_lines.append("⚠️ " + str(err))
            else:
                msg_lines.append("⚠️ No pude completar la operación.")

            status = obj.get("status")
            if status is not None and str(status).strip():
                msg_lines.append(f"HTTP {status}")

            resp = obj.get("response")
            if resp is not None and resp != "":
                try:
                    if isinstance(resp, dict):
                        core = resp.get("message") or resp.get("error") or resp.get("reason")
                        code = resp.get("code")
                        if core and code:
                            msg_lines.append(f"Detalles: {core} (code {code})")
                        elif core:
                            msg_lines.append(f"Detalles: {core}")
                        else:
                            msg_lines.append("Detalles: " + json.dumps(resp, ensure_ascii=False))
                    else:
                        msg_lines.append("Detalles: " + str(resp))
                except Exception:
                    msg_lines.append("Detalles: " + str(resp))

            req = obj.get("request_json")
            if req is not None and req != "":
                try:
                    msg_lines.append("Envié: " + (json.dumps(req, ensure_ascii=False) if isinstance(req, (dict, list)) else str(req)))
                except Exception:
                    msg_lines.append("Envié: " + str(req))

            # If we have candidates, guide the user to pick one
            if obj.get("candidates"):
                msg_lines.append("Elige uno de los registros de abajo:")

        # If search has no matches, say it plainly
        if action in ("buscar_registro", "buscar") and ok and int(obj.get("count") or 0) == 0:
            msg_lines.append("No encontré registros con esos criterios.")

        # If we got trailing text after the JSON prefix, show it for debugging.
        if trace_enabled and rest_text:
            msg_lines.append("\n🗨️ Mensaje:")
            msg_lines.append(rest_text[:1500])

        # Append trace at the bottom so you can debug parsing/timezone/project
        # resolution without inspecting raw JSON.
        if trace_enabled and trace_lines:
            msg_lines.append("\n🧭 Proceso:")
            for ln in trace_lines[:250]:
                if not isinstance(ln, str):
                    continue
                s = ln.rstrip()
                if not s:
                    continue
                # Keep code fences / multi-line blocks intact
                if "\n" in s or s.strip().startswith("```"):
                    msg_lines.append(s)
                else:
                    msg_lines.append("• " + s)

        # Replace bubble text
        friendly = "\n".join(msg_lines).strip() or raw
        self.messages[-1].text = friendly
        if self._current_assistant_bubble:
            self._current_assistant_bubble.set_text(friendly)

        # Buttons for candidates (ambiguity) or matches (search listing)
        candidates = obj.get("candidates") or []
        matches = obj.get("matches") or []
        pending = obj.get("pending") or {}

        def make_label(e: dict) -> str:
            try:
                start = self._fmt_iso_to_local_hm(e.get("start") or "")
                end = self._fmt_iso_to_local_hm(e.get("end") or "")
                desc = str(e.get("description") or "(sin descripción)")
                bill = "💲" if e.get("billable") else ""
                return f"{start}–{end} · {desc} {bill}".strip()
            except Exception:
                return str(e.get("id") or "Seleccionar")

        actions = []
        if isinstance(candidates, list) and candidates:
            # Disambiguation: clicking a button resumes pending modify/delete using the selected id.
            p_action = str(pending.get("action") or "")
            p_payload = pending.get("payload") or {}
            for e in candidates[:8]:
                if not isinstance(e, dict) or not e.get("id"):
                    continue
                eid = str(e.get("id"))
                label = make_label(e)

                def _mk_cb(entry_id=eid, pa=p_action, pp=p_payload):
                    def _cb():
                        if pa == "modificar":
                            out = {"action": "modificar", "id": entry_id, "payload": pp}
                        elif pa == "eliminar":
                            out = {"action": "eliminar", "id": entry_id}
                        else:
                            out = {"action": pa or "modificar", "id": entry_id, "payload": pp}
                        self._send_message(json.dumps(out, ensure_ascii=False))
                    return _cb

                actions.append((label, _mk_cb()))

        elif action == "buscar_registro" and isinstance(matches, list) and matches:
            # Listing: provide a quick "Eliminar" button per entry (safe-ish, user requested a list).
            # To avoid accidental deletes, we only "pre-fill" the input with an ID-based command.
            for e in matches[:6]:
                if not isinstance(e, dict) or not e.get("id"):
                    continue
                eid = str(e.get("id"))
                label = make_label(e)

                def _mk_cb_prefill(entry_id=eid):
                    def _cb():
                        # Prefill a command skeleton (no need to type IDs manually)
                        self.input.setPlainText(f"modificar registro id={entry_id}; ")
                        self.input.setFocus()
                    return _cb

                actions.append((label, _mk_cb_prefill()))

        if actions and self._current_assistant_bubble:
            self._current_assistant_bubble.set_actions(actions)

    def on_error(self, msg: str):
        if not self.messages or self.messages[-1].role != "assistant":
            self.messages.append(Msg("assistant", ""))

        if self.messages[-1].text.strip() == "…":
            self.messages[-1].text = ""
            if self._current_assistant_bubble:
                self._current_assistant_bubble.set_text("")

        self.messages[-1].text += f"\n\n[ERROR] {msg}"
        if self._current_assistant_bubble:
            self._current_assistant_bubble.set_text(self.messages[-1].text)
        self.set_sending(False)
        self._scroll_to_bottom()
        self._mark_dirty()


    def _format_memory_snapshot(self, data: dict) -> str:
        """Build a readable startup debug message from /api/memory."""
        b = (data or {}).get("backend") or {}
        env = (data or {}).get("env") or {}
        proj = (data or {}).get("projects") or {}
        stats = (proj.get("stats") or {}) if isinstance(proj, dict) else {}

        tz_name = str(b.get("timezone_name") or "")
        tz_type = str(b.get("timezone_resolved_type") or "")
        tz_off = str(b.get("timezone_offset") or "")
        boot = str(b.get("boot_time_local") or "")
        zoneinfo_ok = b.get("zoneinfo_available")

        warn = ""
        if tz_name and "mexico" in tz_name.lower() and tz_off.startswith("UTC+00"):
            warn = (
                "\n⚠️ Ojo: tu timezone es CDMX pero el offset quedó UTC+00:00. "
                "Esto suele indicar que falta tzdata o que ZoneInfo no resolvió correctamente."
            )

        lines = []
        lines.append("🧠 Estado cargado (debug de arranque)")
        if boot:
            lines.append(f"• Boot: {boot}")
        if tz_name or tz_off:
            lines.append(f"• TZ: {tz_name} | {tz_off} | resolver={tz_type} | zoneinfo={zoneinfo_ok}")

        tpl = env.get("CLOCKIFY_DESCRIPTION_TEMPLATE")
        tag = env.get("CLOCKIFY_DEFAULT_TAG_ID")
        bulk = env.get("CLOCKIFY_BULK_MAX")
        if tpl:
            lines.append(f"• Template: {tpl}")
        if tag:
            lines.append(f"• Tag default: {tag}")
        if bulk:
            lines.append(f"• Bulk max: {bulk}")

        if isinstance(proj, dict) and proj.get("ok"):
            total = stats.get("total")
            miss_cli = stats.get("missing_cliente")
            miss_fact = stats.get("missing_facturable")
            with_stage = stats.get("with_any_stage")
            with_disc = stats.get("with_ID_Discovery")
            with_dev = stats.get("with_ID_Desarrollo")
            with_dep = stats.get("with_ID_Deployment")
            with_farm = stats.get("with_Farming")
            with_hunt = stats.get("with_Hunting")

            meta = proj.get("meta") or {}
            mtime = meta.get("mtime")
            sizeb = meta.get("size_bytes")
            lines.append(
                "• Directorio proyectos: "
                f"rows={total}, missing_cliente={miss_cli}, missing_facturable={miss_fact}, "
                f"stages_any={with_stage} (disc={with_disc}, dev={with_dev}, dep={with_dep}), "
                f"preventa(farm={with_farm}, hunt={with_hunt})"
            )
            if mtime or sizeb:
                lines.append(f"  - Excel mtime(UTC): {mtime} | size: {sizeb} bytes")

            preview = proj.get("preview") or []
            if isinstance(preview, list) and preview:
                lines.append("• Preview (primeros):")
                for r in preview[:5]:
                    if not isinstance(r, dict):
                        continue
                    pr = r.get("proyecto")
                    cl = r.get("cliente")
                    fa = r.get("facturable")
                    lines.append(f"  - {pr} | {cl} | facturable={fa}")
        else:
            lines.append("• Directorio proyectos: ERROR al cargar")
            if isinstance(proj, dict) and proj.get("error"):
                lines.append(f"  - {proj.get('error')}")

        if warn:
            lines.append(warn.strip())

        return "\n".join(lines).strip()

    def _on_startup_memory_fetched(self, data: dict):
        """UI-thread handler: append startup memory snapshot."""
        msg = self._format_memory_snapshot(data)
        self.messages.append(Msg("assistant", msg))
        self.thread.add_bubble("assistant", msg)
        self._update_bubble_widths()
        self._scroll_to_bottom()
        self._mark_dirty()

    def _on_startup_memory_failed(self, err: str):
        """UI-thread handler: backend memory snapshot could not be fetched."""
        msg = (
            "🧠 Estado cargado (debug de arranque)\n"
            "• No pude leer /api/memory del backend.\n"
            f"• Error: {err}\n\n"
            "Sugerencias:\n"
            "1) Verifica BACKEND_HOST/BACKEND_PORT en .env\n"
            "2) Si estás en Windows, instala tzdata (pip install tzdata)\n"
            "3) Revisa que el backend esté arrancando (endpoint /health)."
        )
        self.messages.append(Msg("assistant", msg))
        self.thread.add_bubble("assistant", msg)
        self._update_bubble_widths()
        self._scroll_to_bottom()
        self._mark_dirty()


class _StartupMemoryFetcher(QObject):
    """Fetch /api/memory in a background thread with retries.

    We cannot block the UI thread on startup. Also, the backend may be
    autostarted and take a moment to be ready.
    """

    fetched = Signal(dict)
    failed = Signal(str)

    def __init__(self, backend_url: str, autostart: bool):
        super().__init__()
        self._backend_url = str(backend_url).rstrip("/")
        self._autostart = bool(autostart)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        last_err = ""

        # If backend is not up and autostart is enabled, trigger it once.
        if self._autostart and not backend_is_up(timeout=0.25):
            try:
                start_backend_thread()
            except Exception as e:
                last_err = str(e)

        # Retry a few times until backend answers.
        for _ in range(40):
            if self._stop.is_set():
                return
            try:
                r = httpx.get(f"{self._backend_url}/api/memory", timeout=1.2)
                if r.status_code == 200:
                    self.fetched.emit(r.json())
                    return
                last_err = f"HTTP {r.status_code}"
            except Exception as e:
                last_err = str(e)
            time.sleep(0.25)

        self.failed.emit(last_err or "unknown")


class LauncherWidget(QWidget):
    """
    Small always-on-top bubble in bottom-right.
    - Left click toggles chat (unless dragged)
    - Drag to reposition (persisted)
    - Right click menu: Show/Hide, Exit
    """
    def __init__(self, icon_path: str, chat_window: ChatWindow, state: dict):
        super().__init__()
        self.chat_window = chat_window
        self.icon_path = icon_path
        self._state = state

        self._press_pos: Optional[QPoint] = None
        self._start_pos: Optional[QPoint] = None
        self._dragging = False

        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(56, 56)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.btn = QPushButton("B", self)
        self.btn.setFixedSize(56, 56)
        self.btn.setStyleSheet("""
            QPushButton {
              border-radius: 28px;
              background: #FFFFFF;
              color: #0b0f14;
              font-weight: 900;
              font-size: 18px;
            }
            QPushButton:hover { background: #FFFFFF; }
        """)
        if os.path.exists(self.icon_path):
            self.btn.setIcon(QIcon(self.icon_path))
            self.btn.setIconSize(self.btn.size() * 0.65)
            self.btn.setText("")

        self.btn.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout.addWidget(self.btn)

        pos = state.get("launcher_pos")
        if isinstance(pos, dict):
            self.move(int(pos.get("x", 0)), int(pos.get("y", 0)))
        else:
            self.move_to_bottom_right()

    def move_to_bottom_right(self):
        screen = QApplication.primaryScreen().availableGeometry()
        x = screen.x() + screen.width() - self.width() - 16
        y = screen.y() + screen.height() - self.height() - 16
        self.move(x, y)

    def _save_pos(self):
        p = self.pos()
        self._state["launcher_pos"] = {"x": int(p.x()), "y": int(p.y())}
        save_state(self._state)

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.open_menu()
            return
        if event.button() == Qt.LeftButton:
            self._press_pos = event.globalPosition().toPoint()
            self._start_pos = self.pos()
            self._dragging = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._press_pos is None or self._start_pos is None:
            return
        if not (event.buttons() & Qt.LeftButton):
            return
        cur = event.globalPosition().toPoint()
        delta = cur - self._press_pos
        if not self._dragging and (abs(delta.x()) + abs(delta.y()) > 6):
            self._dragging = True
        if self._dragging:
            self.move(self._start_pos + delta)
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self._dragging:
                self._save_pos()
            else:
                self.toggle_chat()
            self._press_pos = None
            self._start_pos = None
            self._dragging = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def open_menu(self):
        menu = QMenu(self)
        act_toggle = menu.addAction("Mostrar/Ocultar chat")
        act_exit = menu.addAction("Salir")
        chosen = menu.exec(QCursor.pos())
        if chosen == act_toggle:
            self.toggle_chat()
        elif chosen == act_exit:
            self.chat_window.stream.stop()
            self.chat_window._flush_state()
            self._save_pos()
            QApplication.quit()

    def toggle_chat(self):
        if self.chat_window.isVisible():
            self.chat_window.hide()
        else:
            self.chat_window.show()
            self.chat_window.raise_()
            self.chat_window.activateWindow()


def main():
    state = load_state()

    if AUTOSTART_BACKEND and not backend_is_up():
        start_backend_thread()
        for _ in range(30):
            if backend_is_up(timeout=0.4):
                break
            time.sleep(0.1)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    base = os.path.dirname(os.path.abspath(__file__))
    icon_path = os.path.join(base, "assets", "beeckario.png")

    chat = ChatWindow(icon_path=icon_path, state=state)
    launcher = LauncherWidget(icon_path=icon_path, chat_window=chat, state=state)
    launcher.show()

    def on_about_to_quit():
        try:
            chat._flush_state()
            launcher._save_pos()
        except Exception:
            pass

    app.aboutToQuit.connect(on_about_to_quit)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()