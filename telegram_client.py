import io
import cv2
import requests
from PIL import Image

class TelegramClient:
    def __init__(self, token: str, chat_id: str):
        self.token = token or ""
        self.chat_id = chat_id or ""
        self.base = f"https://api.telegram.org/bot{self.token}" if self.token else ""

    def ready(self) -> bool:
        return bool(self.token) and bool(self.chat_id)

    def send_text(self, text: str):
        if not self.ready():
            return None
        url = f"{self.base}/sendMessage"
        data = {"chat_id": self.chat_id, "text": text}
        r = requests.post(url, data=data, timeout=15)
        r.raise_for_status()
        return r.json()

    def send_crop(self, bgr, bbox_xyxy, padding: int = 8, caption: str = "PM Box"):
        if not self.ready():
            return None
        x1, y1, x2, y2 = bbox_xyxy
        h, w = bgr.shape[:2]
        x1 = max(0, x1 - padding); y1 = max(0, y1 - padding)
        x2 = min(w - 1, x2 + padding); y2 = min(h - 1, y2 + padding)
        crop = bgr[y1:y2, x1:x2].copy()

        rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        bio = io.BytesIO()
        Image.fromarray(rgb).save(bio, format="PNG")
        bio.seek(0)

        url = f"{self.base}/sendPhoto"
        files = {"photo": ("pm.png", bio.getvalue(), "image/png")}
        data = {"chat_id": self.chat_id, "caption": caption}
        r = requests.post(url, data=data, files=files, timeout=30)
        r.raise_for_status()
        return r.json()
