import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Optional


def _default_state_dir() -> Path:
    """
    Cross-platform-ish state directory.
    Windows: %APPDATA%/Beeckario
    macOS: ~/Library/Application Support/Beeckario
    Linux: ~/.config/beeckario
    """
    if os.name == "nt":
        base = os.getenv("APPDATA") or str(Path.home())
        return Path(base) / "Beeckario"
    if sys_platform() == "darwin":
        return Path.home() / "Library" / "Application Support" / "Beeckario"
    xdg = os.getenv("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "beeckario"


def sys_platform() -> str:
    import platform
    return platform.system().lower()


def state_path() -> Path:
    d = _default_state_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / "state.json"


def load_state() -> Dict[str, Any]:
    p = state_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: Dict[str, Any]) -> None:
    p = state_path()
    try:
        p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        # never crash app due to persistence
        return


def clamp_int(v: Any, lo: int, hi: int, default: int) -> int:
    try:
        i = int(v)
        return max(lo, min(hi, i))
    except Exception:
        return default
