import json
from pathlib import Path

CONFIG_PATH = Path("config.json")

DEFAULTS = {
    "api_key": "TQD3fN53VSQE0ppFwLkM",
    "workspace": "kaan-cqltj",
    "project": "merlis-player-5m198",   # Roboflow URL’deki slug ile birebir
    "version": 3,

    "confidence": 30,
    "overlap": 60,

    # ekran/roi/pid
    "monitor_index": 0,
    "roi_top": 0,
    "roi_left": 0,
    "roi_width": 0,
    "roi_height": 0,
    "lock_pid": 0,                 # 0=kapalı, >0 ise bu PID’e kilitlen
    "process_hint": "merlis",      # PID bul butonu için ipucu

    # telegram
    "auto_send_pm": True,
    "crop_padding": 8,
    "telegram_token": "",
    "telegram_chat_id": ""
}

def load_config():
    if CONFIG_PATH.exists():
        try:
            return {**DEFAULTS, **json.loads(CONFIG_PATH.read_text(encoding="utf-8"))}
        except Exception:
            pass
    save_config(DEFAULTS)
    return DEFAULTS.copy()

def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
