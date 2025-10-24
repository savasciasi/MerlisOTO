"""Configuration helpers for Merlis Metin2 Bot GUI."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict

CONFIG_FILE = Path("config.json")


def _ensure_parent(path: Path) -> None:
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)


@dataclass
class TelegramSettings:
    bot_token: str = "PASTE_BOT_TOKEN"
    chat_id: str = "PASTE_CHAT_ID"
    auto_send: bool = False
    padding: int = 20


@dataclass
class RoboflowSettings:
    api_key: str = "TQD3fN53VSQE0ppFwLkM"
    workspace: str = "kaan-cqltj"
    project: str = "merlis-player-5m198"
    version: int = 3
    confidence: float = 0.5
    overlap: float = 0.5


@dataclass
class DetectionSettings:
    monitor_index: int = 0
    enable_player: bool = True
    enable_pm_box: bool = True
    enable_pm_send: bool = False
    save_overlay_dir: str = "captures"
    show_only_player: bool = False


@dataclass
class AppState:
    telegram: TelegramSettings = field(default_factory=TelegramSettings)
    roboflow: RoboflowSettings = field(default_factory=RoboflowSettings)
    detection: DetectionSettings = field(default_factory=DetectionSettings)
    theme: str = "dark"


class ConfigManager:
    """Thread-safe configuration loader/saver."""

    def __init__(self, path: Path = CONFIG_FILE) -> None:
        self._path = path
        self._lock = threading.Lock()
        self.state = AppState()
        self.load()

    def load(self) -> None:
        """Load configuration from disk if present."""
        if not self._path.exists():
            return
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                raw: Dict[str, Any] = json.load(fh)
            self._update_state(raw)
        except Exception:
            # keep defaults; caller will log
            return

    def _update_state(self, raw: Dict[str, Any]) -> None:
        telegram = raw.get("telegram", {})
        roboflow = raw.get("roboflow", {})
        detection = raw.get("detection", {})
        self.state.telegram = TelegramSettings(**{**asdict(TelegramSettings()), **telegram})
        self.state.roboflow = RoboflowSettings(**{**asdict(RoboflowSettings()), **roboflow})
        self.state.detection = DetectionSettings(**{**asdict(DetectionSettings()), **detection})
        self.state.theme = raw.get("theme", "dark")

    def save(self) -> None:
        """Persist configuration to disk."""
        data = asdict(self.state)
        with self._lock:
            _ensure_parent(self._path)
            with self._path.open("w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)

    def update(self, **kwargs: Any) -> None:
        """Update top-level values and persist."""
        for key, value in kwargs.items():
            if hasattr(self.state, key):
                setattr(self.state, key, value)
        self.save()


def ensure_directories(config: AppState) -> None:
    """Create directories needed by the application."""
    capture_dir = Path(config.detection.save_overlay_dir)
    capture_dir.mkdir(parents=True, exist_ok=True)
    docs_dir = Path("docs")
    docs_dir.mkdir(exist_ok=True)
