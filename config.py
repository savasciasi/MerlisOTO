"""Configuration utilities for the dual-client Merlis PM bot."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List

ROOT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = ROOT_DIR / "config.json"

DEFAULT_CLIENT: Dict[str, Any] = {
    "name": "Client 1",
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
    "workflow_enabled": True,
    "reply_offset_x": 271,
    "reply_offset_y": 178,
    "dedupe_window": 8.0,
    "new_msg_min_len": 2,
    "dx_prefer": True,
    "yellow_detect_enabled": True,
    "yellow_min_area": 1200,
    "yellow_padding": 60,
    "yellow_dedupe_window": 12.0,
}

DEFAULT_GLOBAL: Dict[str, Any] = {
    "auto_send": True,
    "tesseract_path": r"C:\\Program Files\\Tesseract-OCR\\tesseract.exe",
    "ocr_lang": "tur+eng",
    "telegram_token": "",
    "telegram_chat_id": "",
}

DEFAULT_AUTOMATION: Dict[str, Any] = {
    "reply_prefix": "#",
    "message_template": (
        "📨 {client_name} · PM #{client_index}\n"
        "💬 Mesaj:\n{text}\n\n"
        "Yanıtlamak için {reply_prefix}{client_index} <mesajınız> yazın."
    ),
    "photo_caption": "PM #{client_index} — {client_name}",
    "awaiting_caption": "✉️ Yanıt bekleniyor",
    "delivered_template": "✅ PM #{client_index} gönderildi",
    "reply_timeout": 120.0,
    "space_delay": 0.5,
    "space_hold": 0.5,
    "yellow_message_template": (
        "🟡 {client_name} · Yakındaki sarı oyuncu\n"
        "📷 Ekran görüntüsü gönderildi.\n"
        "{text_block}"
        "Yanıtlamak için {reply_prefix}{client_index} <mesajınız> yazın."
    ),
    "yellow_photo_caption": "Sarı oyuncu #{client_index} — {client_name}",
    "yellow_delivered_template": "✅ Sarı oyuncu yanıtlandı · {client_name}",
}

DEFAULT_CONFIG: Dict[str, Any] = {
    "global": deepcopy(DEFAULT_GLOBAL),
    "automation": deepcopy(DEFAULT_AUTOMATION),
    "clients": [
        deepcopy(DEFAULT_CLIENT),
        {**deepcopy(DEFAULT_CLIENT), "name": "Client 2"},
    ],
}


def ensure_directories() -> None:
    """Create required directories for assets and captures."""
    (ROOT_DIR / "captures").mkdir(parents=True, exist_ok=True)
    (ROOT_DIR / "assets").mkdir(exist_ok=True)


def _merge_dict(template: Dict[str, Any], values: Dict[str, Any] | None) -> Dict[str, Any]:
    merged = deepcopy(template)
    if not values:
        return merged
    for key in template.keys():
        if key in values:
            merged[key] = values[key]
    for key, value in values.items():
        if key not in merged:
            merged[key] = value
    return merged


def _migrate_flat_config(data: Dict[str, Any]) -> Dict[str, Any]:
    """Upgrade legacy single-client configuration into the new schema."""
    migrated = deepcopy(DEFAULT_CONFIG)
    legacy_client = {key: data[key] for key in DEFAULT_CLIENT.keys() if key in data}
    migrated["clients"][0] = _merge_dict(DEFAULT_CLIENT, legacy_client)
    migrated["clients"][1] = deepcopy(DEFAULT_CONFIG["clients"][1])
    global_keys = {"auto_send", "tesseract_path", "ocr_lang", "telegram_token", "telegram_chat_id"}
    migrated["global"] = _merge_dict(
        DEFAULT_GLOBAL,
        {key: data.get(key) for key in global_keys if key in data},
    )
    migrated["automation"] = deepcopy(DEFAULT_AUTOMATION)
    return migrated


def load_config() -> Dict[str, Any]:
    """Load configuration from disk, normalising to the latest schema."""
    ensure_directories()
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            raw = deepcopy(DEFAULT_CONFIG)
        if "clients" not in raw:
            config = _migrate_flat_config(raw)
        else:
            clients: List[Dict[str, Any]] = raw.get("clients", [])
            merged_clients = []
            for index in range(2):
                base = deepcopy(DEFAULT_CLIENT)
                base["name"] = f"Client {index + 1}" if index else base.get("name", "Client 1")
                merged_clients.append(_merge_dict(base, clients[index] if index < len(clients) else None))
            config = {
                "global": _merge_dict(DEFAULT_GLOBAL, raw.get("global")),
                "automation": _merge_dict(DEFAULT_AUTOMATION, raw.get("automation")),
                "clients": merged_clients,
            }
    else:
        config = deepcopy(DEFAULT_CONFIG)
    save_config(config)
    return config


def save_config(config: Dict[str, Any]) -> None:
    """Persist configuration to disk."""
    ensure_directories()
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
