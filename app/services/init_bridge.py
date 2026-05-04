from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INIT_BRIDGE_PATH = PROJECT_ROOT / "data" / "staging" / "init_bridge.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_init_bridge() -> dict[str, Any]:
    if not INIT_BRIDGE_PATH.exists():
        return {}
    try:
        payload = json.loads(INIT_BRIDGE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def save_init_bridge(*, source_folder: str, note: str = "", show_drive_cta: bool = True) -> Path:
    payload = {
        "source_folder": source_folder,
        "note": note,
        "show_drive_cta": show_drive_cta,
        "updated_at": now_iso(),
    }
    INIT_BRIDGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    INIT_BRIDGE_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return INIT_BRIDGE_PATH
