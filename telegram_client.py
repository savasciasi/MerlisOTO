"""Minimal Telegram client using the Bot API."""
from __future__ import annotations

import cv2
import io
from typing import Any, Optional

import requests
from PIL import Image


class TelegramClient:
    """HTTP based Telegram sender for PM notifications."""

    def __init__(self, token: str = "", chat_id: str = "") -> None:
        self.token = token or ""
        self.chat_id = chat_id or ""

    @property
    def base_url(self) -> str:
        return f"https://api.telegram.org/bot{self.token}" if self.token else ""

    def configure(self, token: str, chat_id: str) -> None:
        self.token = token or ""
        self.chat_id = chat_id or ""

    def ready(self) -> bool:
        return bool(self.token and self.chat_id)

    def send_text(self, text: str) -> Optional[Any]:
        if not self.ready():
            return None
        resp = requests.post(
            f"{self.base_url}/sendMessage",
            data={"chat_id": self.chat_id, "text": text},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def send_photo(self, bgr_image, caption: str = "PM") -> Optional[Any]:
        if not self.ready():
            return None
        rgb = cv2.cvtColor(bgr_image, cv2.COLOR_BGR2RGB)
        buffer = io.BytesIO()
        Image.fromarray(rgb).save(buffer, format="PNG")
        buffer.seek(0)
        files = {"photo": ("pm.png", buffer.getvalue(), "image/png")}
        resp = requests.post(
            f"{self.base_url}/sendPhoto",
            data={"chat_id": self.chat_id, "caption": caption},
            files=files,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
