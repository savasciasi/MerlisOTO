"""Configuration utilities for Merlis PM OCR bot."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

CONFIG_PATH = Path("config.json")

DEFAULT_CONFIG: Dict[str, Any] = {
    "pid": 0,
    "process_hint": "merlis",
    "preview_scale": 0.75,
    "roi_top": 0,
    "roi_left": 0,
    "roi_width": 0,
    "roi_height": 0,
    "use_roi_override": False,
    "ocr_every": 6,
    "checksum_enabled": True,
    "icon_thr": 0.80,
    "btn_thr": 0.80,
    "pm_roi_offset_x": 0,
    "pm_roi_offset_y": 40,
    "pm_roi_width": 420,
    "pm_roi_height": 180,
    "auto_send": True,
    "dedupe_window": 8.0,
    "new_msg_min_len": 2,
    "dx_prefer": True,
    "ocr_use_gpu": True,
    "telegram_token": "",
    "telegram_chat_id": "",
}


def ensure_directories() -> None:
    """Create required directories for assets and captures."""
    Path("captures").mkdir(parents=True, exist_ok=True)
    Path("assets").mkdir(exist_ok=True)


def load_config() -> Dict[str, Any]:
    """Load configuration from disk, falling back to defaults."""
    ensure_directories()
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            merged = {**DEFAULT_CONFIG, **data}
            save_config(merged)
            return merged
        except Exception:
            # Fall back to defaults on malformed configuration files
            pass
    save_config(DEFAULT_CONFIG)
    return dict(DEFAULT_CONFIG)


def save_config(config: Dict[str, Any]) -> None:
    """Persist configuration to disk."""
    ensure_directories()
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
