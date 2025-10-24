"""Minimal Telegram Bot API helper with polling support."""
from __future__ import annotations

import io
from typing import Any, Dict, Iterable, Optional

import cv2
import requests
from PIL import Image


class TelegramClient:
    """HTTP based Telegram sender and update fetcher for PM notifications."""

    def __init__(self, token: str = "", chat_id: str = "") -> None:
        self.token = token or ""
        self.chat_id = chat_id or ""
        self._session = requests.Session()

    @property
    def base_url(self) -> str:
        return f"https://api.telegram.org/bot{self.token}" if self.token else ""

    def configure(self, token: str, chat_id: str) -> None:
        self.token = token or ""
        self.chat_id = chat_id or ""

    def ready(self) -> bool:
        return bool(self.token and self.chat_id)

    def send_text(self, text: str, **kwargs: Any) -> Optional[Any]:
        if not self.ready():
            return None
        payload = {"chat_id": self.chat_id, "text": text}
        payload.update(kwargs)
        resp = self._session.post(
            f"{self.base_url}/sendMessage",
            data=payload,
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()

    def send_photo(self, bgr_image, caption: str = "PM", **kwargs: Any) -> Optional[Any]:
        if not self.ready():
            return None
        rgb = cv2.cvtColor(bgr_image, cv2.COLOR_BGR2RGB)
        buffer = io.BytesIO()
        Image.fromarray(rgb).save(buffer, format="PNG")
        buffer.seek(0)
        files = {"photo": ("pm.png", buffer.getvalue(), "image/png")}
        data = {"chat_id": self.chat_id, "caption": caption}
        data.update(kwargs)
        resp = self._session.post(
            f"{self.base_url}/sendPhoto",
            data=data,
            files=files,
            timeout=40,
        )
        resp.raise_for_status()
        return resp.json()

    def get_updates(self, offset: Optional[int] = None, timeout: int = 20) -> Iterable[Dict[str, Any]]:
        """Yield updates from the configured bot chat."""
        if not self.token:
            return []
        params: Dict[str, Any] = {"timeout": max(0, timeout)}
        if offset is not None:
            params["offset"] = offset
        resp = self._session.get(
            f"{self.base_url}/getUpdates",
            params=params,
            timeout=timeout + 5,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            return []
        return data.get("result", [])
