"""Telegram client wrapper using python-telegram-bot 13.15."""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np
from PIL import Image

try:
    from telegram import Bot
    from telegram.error import TelegramError
except Exception:  # pragma: no cover - during tests
    Bot = None  # type: ignore
    TelegramError = Exception  # type: ignore


@dataclass
class TelegramCredentials:
    bot_token: str
    chat_id: str


class TelegramClient:
    def __init__(self, credentials: TelegramCredentials) -> None:
        self.credentials = credentials
        self._bot: Optional[Bot] = None

    def ensure_bot(self) -> None:
        if self._bot is not None:
            return
        if not self.credentials.bot_token:
            raise RuntimeError("Telegram bot token boş olamaz.")
        if Bot is None:
            raise RuntimeError("python-telegram-bot paketi bulunamadı.")
        self._bot = Bot(self.credentials.bot_token)

    def send_message(self, text: str) -> None:
        self.ensure_bot()
        if not self.credentials.chat_id:
            raise RuntimeError("Telegram chat ID boş olamaz.")
        try:
            assert self._bot is not None
            self._bot.send_message(chat_id=self.credentials.chat_id, text=text)
        except TelegramError as exc:
            raise RuntimeError(str(exc)) from exc

    def send_image(self, image, caption: str = "") -> None:
        """Send an OpenCV image or PIL image to Telegram."""
        self.ensure_bot()
        if not self.credentials.chat_id:
            raise RuntimeError("Telegram chat ID boş olamaz.")
        if isinstance(image, Image.Image):
            pil_image = image
        elif isinstance(image, np.ndarray):
            if image.ndim == 3 and image.shape[2] == 3:
                rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            else:
                rgb = image
            pil_image = Image.fromarray(rgb)
        else:
            raise RuntimeError("Desteklenmeyen görüntü formatı")
        bio = io.BytesIO()
        pil_image.save(bio, format="PNG")
        bio.seek(0)
        try:
            assert self._bot is not None
            self._bot.send_photo(chat_id=self.credentials.chat_id, photo=bio, caption=caption)
        except TelegramError as exc:
            raise RuntimeError(str(exc)) from exc
