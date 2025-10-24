
"""Modern dual-client Merlis PM control panel."""
from __future__ import annotations

import hashlib
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import cv2
import numpy as np
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QColor, QImage, QPalette, QPixmap, QTextCursor
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from automation import bring_window_to_front, click_left, paste_text_via_clipboard, send_space
from config import ensure_directories, load_config, save_config
from ocr_engine import OCREngine, OCREngineError
from screen_capture import DXCapture, find_pid_by_name, get_window_info_by_pid
from templates import match_template
from telegram_client import TelegramClient

BASE_DIR = Path(__file__).resolve().parent
ASSET_ICON = BASE_DIR / "assets" / "pm_icon.png"
ASSET_SEND = BASE_DIR / "assets" / "pm_send_btn.png"
ASSET_CLOSE = BASE_DIR / "assets" / "pm_close_x.png"
CAPTURE_DIR = BASE_DIR / "captures"


def numpy_to_qimage(frame: np.ndarray) -> QImage:
    height, width, channel = frame.shape
    bytes_per_line = channel * width
    return QImage(frame.data, width, height, bytes_per_line, QImage.Format_BGR888).copy()


class TelegramPoller(QThread):
    """Background long-polling worker for Telegram replies."""

    messageReceived = pyqtSignal(dict)
    logMessage = pyqtSignal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self._client = TelegramClient()
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._offset: Optional[int] = None

    def configure(self, token: str, chat_id: str) -> None:
        with self._lock:
            self._client.configure(token, chat_id)
            self._offset = None

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:  # noqa: D401
        while not self._stop_event.is_set():
            with self._lock:
                client = self._client
                offset = self._offset
                ready = client.ready()
            if not ready:
                self._stop_event.wait(1.0)
                continue
            try:
                updates = list(client.get_updates(offset=offset, timeout=20))
            except Exception as exc:  # pragma: no cover - external service
                self.logMessage.emit("ERROR", f"Telegram dinleme hatası: {exc}")
                self._stop_event.wait(3.0)
                continue
            if not updates:
                continue
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    with self._lock:
                        self._offset = update_id + 1
                message = update.get("message") or update.get("edited_message")
                if not message:
                    continue
                chat = message.get("chat", {})
                if str(chat.get("id")) != str(client.chat_id):
                    continue
                text = message.get("text")
                if not text:
                    continue
                payload = {
                    "text": text.strip(),
                    "from": message.get("from", {}),
                    "message_id": message.get("message_id"),
                    "raw": update,
                }
                self.messageReceived.emit(payload)


@dataclass
class ClientUIState:
    config: Dict[str, Any]
    worker: Optional["CaptureWorker"] = None
    preview_label: Optional[QLabel] = None
    fps_label: Optional[QLabel] = None
    status_label: Optional[QLabel] = None
    awaiting_label: Optional[QLabel] = None
    name_label: Optional[QLabel] = None
    pm_preview_label: Optional[QLabel] = None
    pm_text_label: Optional[QLabel] = None
    pm_list: Optional[QListWidget] = None
    save_button: Optional[QPushButton] = None
    start_button: Optional[QPushButton] = None
    stop_button: Optional[QPushButton] = None
    last_frame: Optional[np.ndarray] = None
    awaiting_reply: bool = False


class CaptureWorker(QThread):
    """Capture, OCR and automation worker bound to a single client."""

    frameReady = pyqtSignal(int, object, float)
    logMessage = pyqtSignal(int, str, str)
    statusUpdated = pyqtSignal(int, str)
    pmPreviewReady = pyqtSignal(int, object, str)
    frameForSave = pyqtSignal(int, object)
    awaitingReply = pyqtSignal(int, bool)
    pmTextCaptured = pyqtSignal(int, str)

    def __init__(
        self,
        client_index: int,
        client_config: Dict[str, Any],
        global_config: Dict[str, Any],
        automation_config: Dict[str, Any],
    ) -> None:
        super().__init__()
        self.client_index = client_index
        self.client_config = dict(client_config)
        self.global_config = dict(global_config)
        self.automation_config = dict(automation_config)
        self.client_name = self.client_config.get("name", f"Client {client_index + 1}")
        self._stop_event = threading.Event()
        self._capture: Optional[DXCapture] = None
        self._region: Optional[Tuple[int, int, int, int]] = None
        self._window_hwnd: Optional[int] = None
        self._reply_queue: deque[str] = deque()
        self._pending_reply: Optional[Dict[str, Any]] = None
        self._state_lock = threading.Lock()
        self._awaiting_reply_flag = False
        self._auto_send = bool(self.global_config.get("auto_send", True))
        self._telegram = TelegramClient(
            token=self.global_config.get("telegram_token", ""),
            chat_id=self.global_config.get("telegram_chat_id", ""),
        )
        self._template_warnings: list[tuple[str, str]] = []
        self._pm_icon = self._load_template(ASSET_ICON, "PM ikon (pm_icon.png)")
        self._pm_send = self._load_template(ASSET_SEND, "Gönder butonu (pm_send_btn.png)")
        self._pm_close = self._load_template(ASSET_CLOSE, "Kapat simgesi (pm_close_x.png)")
        self._reported_missing_icon = False
        self._reported_missing_send = False
        self._reported_missing_close = False
        self._frame_counter = 0
        self._fps_window: deque[float] = deque(maxlen=120)
        self._last_checksum: Optional[str] = None
        self._last_text: Optional[str] = None
        self._last_text_time: Optional[datetime] = None
        self.initialization_error: Optional[str] = None
        self._ocr_engine: Optional[OCREngine] = None
        self._ocr_error_reported = False
        self._initialise_ocr()

    def _initialise_ocr(self) -> None:
        try:
            self._ocr_engine = OCREngine(
                tesseract_path=self.global_config.get("tesseract_path"),
                language=self.global_config.get("ocr_lang", "tur+eng"),
            )
            self.initialization_error = None
            self._ocr_error_reported = False
        except OCREngineError as exc:
            self._ocr_engine = None
            self.initialization_error = str(exc)

    def _load_template(self, path: Path, label: str) -> Optional[np.ndarray]:
        if not path.exists():
            self._template_warnings.append(
                (
                    "WARN",
                    f"{label} bulunamadı: {path}. Lütfen assets klasörüne doğru şablon dosyasını ekleyin.",
                )
            )
            return None
        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            self._template_warnings.append(
                (
                    "WARN",
                    f"{label} okunamadı: {path}. Dosyanın PNG formatında olduğundan emin olun.",
                )
            )
            return None
        return image

    # Additional methods will be appended later

    def stop(self) -> None:
        self._stop_event.set()

    def is_waiting_reply(self) -> bool:
        with self._state_lock:
            return bool(self._pending_reply)

    def enqueue_reply(self, text: str) -> None:
        clean = (text or "").strip()
        if not clean:
            return
        with self._state_lock:
            self._reply_queue.append(clean)

    def apply_client_update(self, key: str, value: Any) -> None:
        self.client_config[key] = value
        if key == "name":
            self.client_name = str(value).strip() or f"Client {self.client_index + 1}"
        elif key == "workflow_enabled":
            pass
        elif key == "reply_offset_x" or key == "reply_offset_y":
            self.client_config[key] = int(value)
        elif key in {"icon_thr", "btn_thr", "preview_scale", "dedupe_window"}:
            self.client_config[key] = float(value)
        elif key in {"ocr_every", "new_msg_min_len", "pm_roi_offset_x", "pm_roi_offset_y", "pm_roi_width", "pm_roi_height"}:
            self.client_config[key] = int(value)

    def apply_global_update(self, key: str, value: Any) -> None:
        self.global_config[key] = value
        if key in {"telegram_token", "telegram_chat_id"}:
            self._telegram.configure(
                token=self.global_config.get("telegram_token", ""),
                chat_id=self.global_config.get("telegram_chat_id", ""),
            )
        elif key == "auto_send":
            self._auto_send = bool(value)
        elif key in {"tesseract_path", "ocr_lang"}:
            self.global_config[key] = value
            self._initialise_ocr()

    def apply_automation_update(self, key: str, value: Any) -> None:
        self.automation_config[key] = value

    def run(self) -> None:  # noqa: D401
        region = self._resolve_region()
        if region is None:
            self.logMessage.emit(self.client_index, "ERROR", "Geçerli pencere bölgesi bulunamadı. PID/ROI ayarlarını kontrol edin.")
            return
        for level, message in self._template_warnings:
            self.logMessage.emit(self.client_index, level, message)
        try:
            self._capture = DXCapture(region=region, prefer_dx=bool(self.client_config.get("dx_prefer", True)))
            self._capture.start()
        except Exception as exc:  # pragma: no cover - hardware interaction
            self.logMessage.emit(self.client_index, "ERROR", f"dxcam başlatılamadı: {exc}")
            return
        if self._window_hwnd:
            bring_window_to_front(self._window_hwnd)
        self.statusUpdated.emit(self.client_index, "Çalışıyor")
        self.logMessage.emit(self.client_index, "INFO", "Yakalama başlatıldı.")
        last_time = time.perf_counter()
        while not self._stop_event.is_set():
            self._process_reply_queue()
            self._check_reply_timeout()
            frame = None
            try:
                frame = self._capture.get_latest_frame() if self._capture else None
            except Exception as exc:  # pragma: no cover
                self.logMessage.emit(self.client_index, "ERROR", f"Frame alınamadı: {exc}")
                break
            if frame is None:
                time.sleep(0.005)
                continue
            if frame.ndim == 2:
                frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
            self._frame_counter += 1
            overlay_frame, pm_roi, pm_text = self._process_frame(frame)
            now = time.perf_counter()
            fps = 1.0 / max(now - last_time, 1e-6)
            last_time = now
            self._fps_window.append(fps)
            avg_fps = sum(self._fps_window) / len(self._fps_window)
            scale = float(self.client_config.get("preview_scale", 0.75) or 1.0)
            scaled_frame = overlay_frame
            if 0 < scale <= 1.5:
                scaled_frame = cv2.resize(overlay_frame, (0, 0), fx=scale, fy=scale)
            qimage = numpy_to_qimage(scaled_frame)
            self.frameReady.emit(self.client_index, qimage, avg_fps)
            self.frameForSave.emit(self.client_index, overlay_frame.copy())
            if pm_roi is not None and pm_text is not None:
                self.pmPreviewReady.emit(self.client_index, numpy_to_qimage(pm_roi), pm_text)
        self.statusUpdated.emit(self.client_index, "Durduruldu")
        if self._capture:
            self._capture.stop()
        self.awaitingReply.emit(self.client_index, False)
        self.logMessage.emit(self.client_index, "INFO", "Yakalama sonlandırıldı.")

    def _resolve_region(self) -> Optional[Tuple[int, int, int, int]]:
        self._region = None
        self._window_hwnd = None
        if self.client_config.get("use_roi_override"):
            left = int(self.client_config.get("roi_left", 0))
            top = int(self.client_config.get("roi_top", 0))
            width = int(self.client_config.get("roi_width", 0))
            height = int(self.client_config.get("roi_height", 0))
            if width > 0 and height > 0:
                region = (left, top, left + width, top + height)
                self._region = region
                return region
        pid = int(self.client_config.get("pid") or 0)
        if pid <= 0:
            return None
        info = get_window_info_by_pid(pid)
        if info is None:
            return None
        hwnd, rect = info
        self._window_hwnd = hwnd
        self._region = rect
        return rect

    def _box_center(self, box: Tuple[int, int, int, int, float]) -> Tuple[int, int]:
        x1, y1, x2, y2, _ = box
        return int((x1 + x2) / 2), int((y1 + y2) / 2)

    def _to_screen(self, x: int, y: int) -> Optional[Tuple[int, int]]:
        if not self._region:
            return None
        left, top, _, _ = self._region
        return left + int(x), top + int(y)

    def _process_frame(self, frame: np.ndarray) -> Tuple[np.ndarray, Optional[np.ndarray], Optional[str]]:
        display = frame.copy()
        pm_roi_img: Optional[np.ndarray] = None
        detected_text: Optional[str] = None
        if self._pm_icon is None:
            if not self._reported_missing_icon:
                self.logMessage.emit(
                    self.client_index,
                    "WARN",
                    "PM ikon şablonu yüklenemediği için tespit devre dışı. assets/pm_icon.png dosyasını ekleyin.",
                )
                self._reported_missing_icon = True
            return display, None, None
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        matches = match_template(gray, self._pm_icon, float(self.client_config.get("icon_thr", 0.8)))
        if matches:
            x1, y1, x2, y2, score = matches[0]
            cv2.rectangle(display, (x1, y1), (x2, y2), (0, 200, 255), 2)
            cv2.putText(
                display,
                f"icon {score:.2f}",
                (x1, max(12, y1 - 8)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (0, 200, 255),
                1,
                cv2.LINE_AA,
            )
            roi_info = self._extract_pm_roi(frame, (x1, y1, x2, y2))
            if roi_info is not None:
                top, left, bottom, right, roi_img = roi_info
                pm_roi_img = roi_img
                cv2.rectangle(display, (left, top), (right, bottom), (120, 255, 120), 2)
                detected_text = self._maybe_run_ocr(frame, (top, left, bottom, right), roi_img, matches[0])
        return display, pm_roi_img, detected_text

    def _extract_pm_roi(
        self,
        frame: np.ndarray,
        icon_box: Tuple[int, int, int, int],
    ) -> Optional[Tuple[int, int, int, int, np.ndarray]]:
        x1, y1, x2, y2 = icon_box
        offset_x = int(self.client_config.get("pm_roi_offset_x", 0))
        offset_y = int(self.client_config.get("pm_roi_offset_y", 40))
        width = int(self.client_config.get("pm_roi_width", 420))
        height = int(self.client_config.get("pm_roi_height", 180))
        top = max(0, y2 + offset_y)
        left = max(0, x1 + offset_x)
        bottom = min(frame.shape[0], top + height)
        right = min(frame.shape[1], left + width)
        if bottom <= top or right <= left:
            return None
        return top, left, bottom, right, frame[top:bottom, left:right].copy()

    def _maybe_run_ocr(
        self,
        frame: np.ndarray,
        roi_bounds: Tuple[int, int, int, int],
        roi_image: np.ndarray,
        icon_box: Tuple[int, int, int, int, float],
    ) -> Optional[str]:
        every = max(1, int(self.client_config.get("ocr_every", 6)))
        if self._frame_counter % every != 0:
            return None
        checksum = None
        if self.client_config.get("checksum_enabled", True):
            checksum = hashlib.md5(roi_image.tobytes()).hexdigest()
            if checksum == self._last_checksum:
                return None
        if self._ocr_engine is None:
            if self.initialization_error and not self._ocr_error_reported:
                self.logMessage.emit(self.client_index, "ERROR", self.initialization_error)
                self._ocr_error_reported = True
            return None
        try:
            text = self._ocr_engine.read(roi_image)
        except OCREngineError as exc:
            if not self._ocr_error_reported:
                self.logMessage.emit(self.client_index, "ERROR", str(exc))
                self._ocr_error_reported = True
            return None
        text = text.strip()
        if not text or len(text) < int(self.client_config.get("new_msg_min_len", 2)):
            return None
        now = datetime.utcnow()
        window = float(self.client_config.get("dedupe_window", 8.0))
        if self._last_text == text and self._last_text_time and now - self._last_text_time < timedelta(seconds=window):
            return None
        if checksum is not None:
            self._last_checksum = checksum
        self._last_text = text
        self._last_text_time = now
        self.pmTextCaptured.emit(self.client_index, text)
        self.statusUpdated.emit(self.client_index, "PM analiz ediliyor")
        self._handle_new_pm(text, roi_image, frame, icon_box)
        return text

    def _handle_new_pm(
        self,
        text: str,
        roi_image: np.ndarray,
        frame: np.ndarray,
        icon_box: Tuple[int, int, int, int, float],
    ) -> None:
        if self._start_pm_session(text, roi_image, frame, icon_box):
            return
        self._notify_telegram(text, frame, roi_image, awaiting=False)
        self.statusUpdated.emit(self.client_index, "Telegram'a aktarıldı")

    def _start_pm_session(
        self,
        text: str,
        roi_image: np.ndarray,
        frame: np.ndarray,
        icon_box: Tuple[int, int, int, int, float],
    ) -> bool:
        if not self.client_config.get("workflow_enabled", True):
            return False
        if self._capture is None or self._region is None:
            return False
        if self._pm_send is None:
            if not self._reported_missing_send:
                self.logMessage.emit(
                    self.client_index,
                    "WARN",
                    "pm_send_btn.png şablonu bulunamadığı için otomatik yanıt devre dışı. assets klasörünü kontrol edin.",
                )
                self._reported_missing_send = True
            return False
        try:
            if self._window_hwnd:
                bring_window_to_front(self._window_hwnd)
            icon_center = self._box_center(icon_box)
            screen_point = self._to_screen(*icon_center)
            if screen_point is None:
                return False
            click_left(*screen_point)
            time.sleep(0.25)
            send_box, latest_frame = self._find_template(self._pm_send, float(self.client_config.get("btn_thr", 0.8)))
            if send_box is None or latest_frame is None:
                self.logMessage.emit(self.client_index, "WARN", "pm_send_btn.png tespit edilemedi.")
                return False
            send_center = self._box_center(send_box)
            send_screen = self._to_screen(*send_center)
            if send_screen is None:
                return False
            text_point = (
                int(send_center[0] - int(self.client_config.get("reply_offset_x", 271))),
                int(send_center[1] - int(self.client_config.get("reply_offset_y", 178))),
            )
            text_screen = self._to_screen(*text_point)
            if text_screen is None:
                self.logMessage.emit(self.client_index, "WARN", "Metin alanı koordinatları pencere dışında kaldı.")
                return False
            screenshot = latest_frame.copy() if latest_frame is not None else frame
            with self._state_lock:
                self._pending_reply = {
                    "text_screen": text_screen,
                    "send_screen": send_screen,
                    "started": time.time(),
                    "screenshot": screenshot,
                    "roi": roi_image.copy(),
                    "text": text,
                }
                self._awaiting_reply_flag = True
            self.awaitingReply.emit(self.client_index, True)
            self.statusUpdated.emit(self.client_index, "Yanıt bekleniyor")
            self._notify_telegram(text, screenshot, roi_image, awaiting=True)
            return True
        except Exception as exc:  # pragma: no cover - automation safety
            self.logMessage.emit(self.client_index, "ERROR", f"PM otomasyonu sırasında hata: {exc}")
            return False

    def _find_template(
        self,
        template: Optional[np.ndarray],
        threshold: float,
        attempts: int = 8,
        delay: float = 0.1,
    ) -> Tuple[Optional[Tuple[int, int, int, int, float]], Optional[np.ndarray]]:
        if template is None or self._capture is None:
            return None, None
        last_frame: Optional[np.ndarray] = None
        for _ in range(max(1, attempts)):
            frame = self._capture.get_latest_frame()
            if frame is None:
                time.sleep(delay)
                continue
            last_frame = frame
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            matches = match_template(gray, template, threshold)
            if matches:
                return matches[0], frame
            time.sleep(delay)
        return None, last_frame

    def _process_reply_queue(self) -> None:
        with self._state_lock:
            if not self._pending_reply or not self._reply_queue:
                return
            text = self._reply_queue.popleft()
            pending = dict(self._pending_reply)
        success = self._deliver_reply(text, pending)
        if success:
            self._clear_pending_reply()
            self.awaitingReply.emit(self.client_index, False)
            self.statusUpdated.emit(self.client_index, "Mesaj gönderildi")
            self._notify_ack(text)
        else:
            with self._state_lock:
                self._reply_queue.appendleft(text)

    def _clear_pending_reply(self) -> None:
        with self._state_lock:
            self._pending_reply = None
            self._awaiting_reply_flag = False
            self._reply_queue.clear()

    def _check_reply_timeout(self) -> None:
        with self._state_lock:
            pending = self._pending_reply
        if not pending:
            return
        timeout = float(self.automation_config.get("reply_timeout", 120.0))
        if timeout <= 0:
            return
        if time.time() - float(pending.get("started", 0.0)) > timeout:
            self.logMessage.emit(self.client_index, "WARN", "Telegram yanıtı zaman aşımına uğradı.")
            self._notify_timeout()
            self._clear_pending_reply()
            self.awaitingReply.emit(self.client_index, False)
            self.statusUpdated.emit(self.client_index, "Yanıt zaman aşımı")

    def _notify_timeout(self) -> None:
        if not self._auto_send or not self._telegram.ready():
            return
        try:
            self._telegram.send_text(
                f"⚠️ PM #{self.client_index + 1} ({self.client_name}) yanıt beklerken zaman aşımına uğradı."
            )
        except Exception as exc:  # pragma: no cover
            self.logMessage.emit(self.client_index, "ERROR", f"Telegram uyarısı gönderilemedi: {exc}")

    def _deliver_reply(self, text: str, pending: Dict[str, Any]) -> bool:
        try:
            if self._window_hwnd:
                bring_window_to_front(self._window_hwnd)
            target = pending.get("text_screen")
            send_target = pending.get("send_screen")
            if target is None or send_target is None:
                return False
            click_left(*target)
            time.sleep(0.15)
            paste_text_via_clipboard(text)
            time.sleep(0.15)
            click_left(*send_target)
            time.sleep(0.25)
            self._close_pm_window()
            send_space(delay=float(self.automation_config.get("space_delay", 0.5)))
            self.logMessage.emit(self.client_index, "INFO", f"Oyuna mesaj gönderildi: {text}")
            return True
        except Exception as exc:  # pragma: no cover
            self.logMessage.emit(self.client_index, "ERROR", f"Yanıt gönderilemedi: {exc}")
            return False

    def _close_pm_window(self) -> None:
        if self._pm_close is None:
            if not self._reported_missing_close:
                self.logMessage.emit(
                    self.client_index,
                    "WARN",
                    "pm_close_x.png şablonu eksik. PM penceresi otomatik kapatılamıyor.",
                )
                self._reported_missing_close = True
            return
        match, _ = self._find_template(self._pm_close, float(self.client_config.get("btn_thr", 0.8)), attempts=6)
        if match is None:
            self.logMessage.emit(self.client_index, "WARN", "PM pencere kapatma simgesi bulunamadı.")
            return
        center = self._box_center(match)
        screen = self._to_screen(*center)
        if screen is None:
            return
        click_left(*screen)

    def _notify_ack(self, reply_text: str) -> None:
        if not self._auto_send or not self._telegram.ready():
            return
        template = self.automation_config.get("delivered_template", "✅ PM gönderildi")
        message = self._format_template(template, reply_text)
        try:
            self._telegram.send_text(f"{message}\nGönderilen mesaj: {reply_text}")
        except Exception as exc:  # pragma: no cover
            self.logMessage.emit(self.client_index, "ERROR", f"Telegram bildiriminde hata: {exc}")

    def _notify_telegram(
        self,
        text: str,
        screenshot: np.ndarray,
        roi: Optional[np.ndarray],
        awaiting: bool,
    ) -> None:
        if not self._auto_send or not self._telegram.ready():
            return
        try:
            template = self.automation_config.get("message_template", "{text}")
            message = self._format_template(template, text)
            if awaiting:
                awaiting_caption = self.automation_config.get("awaiting_caption", "")
                if awaiting_caption:
                    message = f"{message}\n\n{awaiting_caption}"
            self._telegram.send_text(message)
            caption_template = self.automation_config.get("photo_caption", "PM #{client_index}")
            caption = self._format_template(caption_template, text)
            self._telegram.send_photo(screenshot, caption=caption)
            if roi is not None:
                self._telegram.send_photo(roi, caption="PM metin alanı")
            self.logMessage.emit(self.client_index, "INFO", "Telegram'a paket gönderildi.")
        except Exception as exc:  # pragma: no cover
            self.logMessage.emit(self.client_index, "ERROR", f"Telegram gönderimi başarısız: {exc}")

    def _format_template(self, template: str, text: str) -> str:
        values = {
            "client_index": self.client_index + 1,
            "client_name": self.client_name,
            "text": text,
            "reply_prefix": self.automation_config.get("reply_prefix", "#"),
        }
        try:
            return template.format(**values)
        except Exception:
            return template



class MainWindow(QMainWindow):
    """Modern dark-themed control panel for dual Merlis clients."""

    def __init__(self) -> None:
        super().__init__()
        ensure_directories()
        self.config = load_config()
        self.client_states = [ClientUIState(config=c) for c in self.config.get("clients", [])]
        self.log_view: Optional[QTextEdit] = None
        self.stack: Optional[QStackedWidget] = None
        self.nav_buttons: list[QPushButton] = []
        self.telegram_sender = TelegramClient(
            self.config.get("global", {}).get("telegram_token", ""),
            self.config.get("global", {}).get("telegram_chat_id", ""),
        )
        self.telegram_poller = TelegramPoller()
        self.telegram_poller.messageReceived.connect(self._on_telegram_message)
        self.telegram_poller.logMessage.connect(self._on_poller_log)
        self._apply_theme()
        self._build_ui()
        self._update_telegram_credentials()
        self.telegram_poller.start()
        self.setWindowTitle("Merlis PM Kontrol Merkezi")
        self.resize(1500, 880)

    def _apply_theme(self) -> None:
        app = QApplication.instance()
        if app:
            palette = QPalette()
            palette.setColor(QPalette.Window, QColor("#11151c"))
            palette.setColor(QPalette.WindowText, QColor("#f1f4fb"))
            palette.setColor(QPalette.Base, QColor("#151b24"))
            palette.setColor(QPalette.AlternateBase, QColor("#1c2330"))
            palette.setColor(QPalette.Text, QColor("#f1f4fb"))
            palette.setColor(QPalette.Button, QColor("#1e2532"))
            palette.setColor(QPalette.ButtonText, QColor("#f1f4fb"))
            palette.setColor(QPalette.Highlight, QColor("#3d7dff"))
            palette.setColor(QPalette.HighlightedText, QColor("#ffffff"))
            app.setStyle("Fusion")
            app.setPalette(palette)
        self.setStyleSheet(
            """
            QMainWindow { background-color: #0f172a; }
            QFrame#NavPanel {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #111c2e, stop:1 #0b1220);
                border-radius: 24px;
                border: 1px solid #1e2b44;
            }
            QLabel#BrandTitle {
                color: #f8fafc;
                font-size: 22px;
                font-weight: 700;
            }
            QLabel#BrandSubtitle {
                color: #9fb3d1;
                font-size: 12px;
            }
            QPushButton#NavButton {
                background-color: transparent;
                color: #c7d5f5;
                border: none;
                text-align: left;
                padding: 12px 18px;
                border-radius: 12px;
                font-size: 14px;
                font-weight: 600;
            }
            QPushButton#NavButton:hover {
                background-color: rgba(61, 125, 255, 0.14);
                color: #f1f5ff;
            }
            QPushButton#NavButton:checked {
                background-color: #3d7dff;
                color: #ffffff;
            }
            QFrame#ContentFrame { background: transparent; }
            QFrame#HeroBanner {
                background: rgba(33, 47, 75, 0.85);
                border: 1px solid #24324d;
                border-radius: 20px;
                padding: 20px;
            }
            QLabel#HeroTitle {
                font-size: 24px;
                font-weight: 700;
                color: #f8fbff;
            }
            QLabel#HeroSubtitle {
                color: #a9b8d9;
                font-size: 13px;
            }
            QFrame#Card {
                background-color: #141c2f;
                border: 1px solid #1f2b46;
                border-radius: 20px;
            }
            QLabel { color: #e8eef9; }
            QLabel#TitleLabel { font-size: 20px; font-weight: 600; }
            QLabel#AwaitBadge {
                background-color: #22d3ee;
                color: #04121f;
                padding: 4px 12px;
                border-radius: 12px;
                font-weight: 600;
            }
            QLabel#StatusLabel { color: #7dd3fc; font-weight: 600; }
            QLabel#FpsLabel { color: #a5b4fc; font-weight: 600; }
            QPushButton {
                background-color: #1c2539;
                color: #f0f4ff;
                border-radius: 10px;
                padding: 9px 20px;
                border: 1px solid #27334a;
            }
            QPushButton:hover { background-color: #26304a; }
            QPushButton#PrimaryButton {
                background-color: #3d7dff;
                border-color: #3d7dff;
                color: #ffffff;
            }
            QPushButton#PrimaryButton:hover { background-color: #356ceb; }
            QPushButton#DangerButton {
                background-color: #ef4444;
                border-color: #ef4444;
                color: #ffffff;
            }
            QPushButton#DangerButton:hover { background-color: #dc2626; }
            QListWidget {
                background-color: #121a2d;
                border: none;
                padding: 12px;
                color: #d6deeb;
                border-radius: 16px;
            }
            QListWidget::item { border-radius: 12px; padding: 12px; margin: 2px 0; }
            QListWidget::item:selected { background-color: rgba(61, 125, 255, 0.25); color: #ffffff; }
            QGroupBox {
                border: 1px solid #25324b;
                border-radius: 18px;
                margin-top: 20px;
                color: #9fa9c1;
                font-weight: 600;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 18px; padding: 0 6px; }
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTextEdit {
                background-color: #16223a;
                color: #f0f4ff;
                border: 1px solid #273142;
                border-radius: 10px;
                padding: 8px 10px;
            }
            QTextEdit { min-height: 140px; }
            QCheckBox { color: #d6deeb; }
            QScrollArea { border: none; }
            QListWidget#LastMessages { background-color: #131c30; border: 1px solid #23304b; border-radius: 14px; }
            QTextEdit#LogView { background-color: #131c30; border: 1px solid #23304b; border-radius: 14px; }
            """
        )

    def _build_ui(self) -> None:
        central = QWidget()
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(24, 24, 24, 24)
        root_layout.setSpacing(24)

        nav_panel = QFrame()
        nav_panel.setObjectName("NavPanel")
        nav_layout = QVBoxLayout(nav_panel)
        nav_layout.setContentsMargins(20, 28, 20, 28)
        nav_layout.setSpacing(18)

        brand = QLabel("Merlis PM")
        brand.setObjectName("BrandTitle")
        brand_sub = QLabel("Çift Merlis istemcisi için gerçek zamanlı PM yakalama ve otomasyon")
        brand_sub.setObjectName("BrandSubtitle")
        brand_sub.setWordWrap(True)
        nav_layout.addWidget(brand)
        nav_layout.addWidget(brand_sub)
        nav_layout.addSpacing(12)

        nav_titles = [
            "Kontrol Merkezi",
            self.config["clients"][0].get("name", "Client 1"),
            self.config["clients"][1].get("name", "Client 2"),
            "Telegram & Otomasyon",
            "Loglar",
        ]
        self.nav_buttons = []
        for index, title in enumerate(nav_titles):
            button = QPushButton(title)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.clicked.connect(lambda checked, i=index: self._navigate(i))
            nav_layout.addWidget(button)
            self.nav_buttons.append(button)

        nav_layout.addStretch()
        root_layout.addWidget(nav_panel)

        content = QFrame()
        content.setObjectName("ContentFrame")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(24)

        hero = QFrame()
        hero.setObjectName("HeroBanner")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(18, 18, 18, 18)
        hero_layout.setSpacing(6)
        hero_title = QLabel("Merlis PM Kontrol Merkezi")
        hero_title.setObjectName("HeroTitle")
        hero_desc = QLabel(
            "dxcam yakalama, Tesseract OCR ve Telegram otomasyonu ile iki Merlis istemcisini tek panelden yönetin."
        )
        hero_desc.setObjectName("HeroSubtitle")
        hero_desc.setWordWrap(True)
        hero_layout.addWidget(hero_title)
        hero_layout.addWidget(hero_desc)
        content_layout.addWidget(hero)

        self.stack = QStackedWidget()
        content_layout.addWidget(self.stack, 1)

        root_layout.addWidget(content, 1)

        overview = self._build_overview_page()
        client_pages = [self._build_client_page(0), self._build_client_page(1)]
        telegram_page = self._build_telegram_page()
        logs_page = self._build_logs_page()

        self.stack.addWidget(overview)
        self.stack.addWidget(client_pages[0])
        self.stack.addWidget(client_pages[1])
        self.stack.addWidget(telegram_page)
        self.stack.addWidget(logs_page)

        self.setCentralWidget(central)
        self._navigate(0)
        self._refresh_navigation_titles()

    def _navigate(self, index: int) -> None:
        if not self.stack:
            return
        index = max(0, min(index, self.stack.count() - 1))
        self.stack.setCurrentIndex(index)
        for idx, button in enumerate(self.nav_buttons):
            if button:
                button.setChecked(idx == index)

    def _refresh_navigation_titles(self) -> None:
        for idx, state in enumerate(self.client_states[:2]):
            name = state.config.get("name", f"Client {idx + 1}")
            button_index = 1 + idx
            if button_index < len(self.nav_buttons):
                self.nav_buttons[button_index].setText(name)

    def _build_overview_page(self) -> QWidget:
        page = QWidget()
        layout = QGridLayout(page)
        layout.setSpacing(20)
        layout.setContentsMargins(0, 0, 0, 0)
        for idx in range(2):
            card = self._create_client_card(idx)
            layout.addWidget(card, 0, idx)
        return page

    def _create_client_card(self, idx: int) -> QFrame:
        state = self.client_states[idx]
        card = QFrame()
        card.setObjectName("Card")
        card_layout = QVBoxLayout(card)
        card_layout.setSpacing(16)
        header = QHBoxLayout()
        name = state.config.get("name", f"Client {idx + 1}")
        title = QLabel(name)
        title.setObjectName("TitleLabel")
        header.addWidget(title)
        header.addStretch()
        awaiting = QLabel(self.config["automation"].get("awaiting_caption", "✉️ Yanıt bekleniyor"))
        awaiting.setObjectName("AwaitBadge")
        awaiting.setVisible(False)
        header.addWidget(awaiting)
        card_layout.addLayout(header)

        preview = QLabel("Önizleme yok")
        preview.setAlignment(Qt.AlignCenter)
        preview.setMinimumSize(520, 300)
        preview.setStyleSheet("background-color: #151b23; border-radius: 12px; border: 1px solid #1f2733;")
        preview.setScaledContents(True)
        card_layout.addWidget(preview)

        info_row = QHBoxLayout()
        fps_label = QLabel("0.0 FPS")
        fps_label.setObjectName("FpsLabel")
        status_label = QLabel("Hazır")
        status_label.setObjectName("StatusLabel")
        info_row.addWidget(fps_label)
        info_row.addStretch()
        info_row.addWidget(status_label)
        card_layout.addLayout(info_row)

        button_row = QHBoxLayout()
        start_btn = QPushButton("Başlat")
        start_btn.setObjectName("PrimaryButton")
        start_btn.clicked.connect(lambda _, i=idx: self.start_client(i))
        stop_btn = QPushButton("Durdur")
        stop_btn.setObjectName("DangerButton")
        stop_btn.clicked.connect(lambda _, i=idx: self.stop_client(i))
        save_btn = QPushButton("Ekran Görüntüsü Kaydet")
        save_btn.clicked.connect(lambda _, i=idx: self.save_screenshot(i))
        button_row.addWidget(start_btn)
        button_row.addWidget(stop_btn)
        button_row.addStretch()
        button_row.addWidget(save_btn)
        card_layout.addLayout(button_row)

        pm_group = QGroupBox("Son PM")
        pm_layout = QVBoxLayout(pm_group)
        pm_preview = QLabel("Önizleme yok")
        pm_preview.setAlignment(Qt.AlignCenter)
        pm_preview.setMinimumHeight(140)
        pm_preview.setStyleSheet("background-color: #141b25; border-radius: 10px; border: 1px solid #202a36;")
        pm_preview.setScaledContents(True)
        pm_text = QLabel("")
        pm_text.setWordWrap(True)
        pm_text.setStyleSheet("color: #cbd3e7; font-size: 13px;")
        pm_list = QListWidget()
        pm_list.setObjectName("LastMessages")
        pm_list.setMaximumHeight(140)
        pm_layout.addWidget(pm_preview)
        pm_layout.addWidget(pm_text)
        pm_layout.addWidget(pm_list)
        card_layout.addWidget(pm_group)
        card_layout.addStretch()

        state.preview_label = preview
        state.fps_label = fps_label
        state.status_label = status_label
        state.awaiting_label = awaiting
        state.name_label = title
        state.pm_preview_label = pm_preview
        state.pm_text_label = pm_text
        state.pm_list = pm_list
        state.start_button = start_btn
        state.stop_button = stop_btn
        state.save_button = save_btn
        return card

    def _build_client_page(self, idx: int) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        form_layout = QVBoxLayout(container)

        client_cfg = self.config["clients"][idx]

        general_box = QGroupBox("Genel")
        general_form = QFormLayout(general_box)
        name_edit = QLineEdit(client_cfg.get("name", f"Client {idx + 1}"))
        name_edit.textChanged.connect(lambda text, i=idx: self._update_client_config(i, "name", text.strip()))
        general_form.addRow("Takma Ad", name_edit)

        process_hint_edit = QLineEdit(client_cfg.get("process_hint", "merlis"))
        process_hint_edit.textChanged.connect(lambda text, i=idx: self._update_client_config(i, "process_hint", text))
        general_form.addRow("Süreç ipucu", process_hint_edit)

        pid_spin = QSpinBox()
        pid_spin.setRange(0, 1_000_000)
        pid_spin.setValue(int(client_cfg.get("pid", 0)))
        pid_spin.valueChanged.connect(lambda value, i=idx: self._update_client_config(i, "pid", int(value)))
        general_form.addRow("PID", pid_spin)

        find_button = QPushButton("Merlis'i Bul (PID)")

        def _find_pid() -> None:
            hint = process_hint_edit.text().strip()
            pid = find_pid_by_name(hint)
            if pid:
                pid_spin.setValue(pid)
                self._update_client_config(idx, "pid", pid)
                self.append_log("INFO", f"{client_cfg.get('name', f'Client {idx + 1}')} için PID bulundu: {pid}")
            else:
                QMessageBox.warning(self, "PID bulunamadı", f"'{hint}' için uygun bir Merlis penceresi bulunamadı.")

        find_button.clicked.connect(_find_pid)
        general_form.addRow("", find_button)
        form_layout.addWidget(general_box)

        capture_box = QGroupBox("Yakalama & ROI")
        capture_form = QFormLayout(capture_box)
        use_roi = QCheckBox("Manuel ROI kullan")
        use_roi.setChecked(bool(client_cfg.get("use_roi_override", False)))
        capture_form.addRow(use_roi)

        roi_left = QSpinBox()
        roi_left.setRange(0, 10000)
        roi_left.setValue(int(client_cfg.get("roi_left", 0)))
        roi_top = QSpinBox()
        roi_top.setRange(0, 10000)
        roi_top.setValue(int(client_cfg.get("roi_top", 0)))
        roi_width = QSpinBox()
        roi_width.setRange(0, 10000)
        roi_width.setValue(int(client_cfg.get("roi_width", 0)))
        roi_height = QSpinBox()
        roi_height.setRange(0, 10000)
        roi_height.setValue(int(client_cfg.get("roi_height", 0)))

        roi_spins = [roi_left, roi_top, roi_width, roi_height]
        for spin in roi_spins:
            spin.setEnabled(use_roi.isChecked())

        roi_left.valueChanged.connect(lambda value, i=idx: self._update_client_config(i, "roi_left", int(value)))
        roi_top.valueChanged.connect(lambda value, i=idx: self._update_client_config(i, "roi_top", int(value)))
        roi_width.valueChanged.connect(lambda value, i=idx: self._update_client_config(i, "roi_width", int(value)))
        roi_height.valueChanged.connect(lambda value, i=idx: self._update_client_config(i, "roi_height", int(value)))

        def _toggle_roi(state: int) -> None:
            enabled = state == Qt.Checked
            for spin in roi_spins:
                spin.setEnabled(enabled)
            self._update_client_config(idx, "use_roi_override", enabled)

        use_roi.stateChanged.connect(_toggle_roi)
        capture_form.addRow("Sol", roi_left)
        capture_form.addRow("Üst", roi_top)
        capture_form.addRow("Genişlik", roi_width)
        capture_form.addRow("Yükseklik", roi_height)

        preview_combo = QComboBox()
        for label, value in [("0.5", 0.5), ("0.75", 0.75), ("1.0", 1.0)]:
            preview_combo.addItem(label, value)
        current_scale = float(client_cfg.get("preview_scale", 0.75))
        idx_scale = max(0, preview_combo.findData(current_scale))
        preview_combo.setCurrentIndex(idx_scale)
        preview_combo.currentIndexChanged.connect(
            lambda i, c=preview_combo, client_idx=idx: self._update_client_config(client_idx, "preview_scale", float(c.itemData(i)))
        )
        capture_form.addRow("Önizleme ölçeği", preview_combo)

        dx_check = QCheckBox("DirectX hızlandırmayı tercih et")
        dx_check.setChecked(bool(client_cfg.get("dx_prefer", True)))
        dx_check.stateChanged.connect(lambda state, i=idx: self._update_client_config(i, "dx_prefer", state == Qt.Checked))
        capture_form.addRow(dx_check)
        form_layout.addWidget(capture_box)

        pm_box = QGroupBox("PM Algılama ve OCR")
        pm_form = QFormLayout(pm_box)

        icon_thr = QDoubleSpinBox()
        icon_thr.setRange(0.1, 1.0)
        icon_thr.setSingleStep(0.01)
        icon_thr.setValue(float(client_cfg.get("icon_thr", 0.8)))
        icon_thr.valueChanged.connect(lambda value, i=idx: self._update_client_config(i, "icon_thr", float(value)))
        pm_form.addRow("İkon eşiği", icon_thr)

        btn_thr = QDoubleSpinBox()
        btn_thr.setRange(0.1, 1.0)
        btn_thr.setSingleStep(0.01)
        btn_thr.setValue(float(client_cfg.get("btn_thr", 0.8)))
        btn_thr.valueChanged.connect(lambda value, i=idx: self._update_client_config(i, "btn_thr", float(value)))
        pm_form.addRow("Buton eşiği", btn_thr)

        pm_roi_x = QSpinBox(); pm_roi_x.setRange(-500, 500); pm_roi_x.setValue(int(client_cfg.get("pm_roi_offset_x", 0)))
        pm_roi_y = QSpinBox(); pm_roi_y.setRange(-500, 500); pm_roi_y.setValue(int(client_cfg.get("pm_roi_offset_y", 40)))
        pm_roi_w = QSpinBox(); pm_roi_w.setRange(10, 2000); pm_roi_w.setValue(int(client_cfg.get("pm_roi_width", 420)))
        pm_roi_h = QSpinBox(); pm_roi_h.setRange(10, 2000); pm_roi_h.setValue(int(client_cfg.get("pm_roi_height", 180)))
        pm_roi_x.valueChanged.connect(lambda value, i=idx: self._update_client_config(i, "pm_roi_offset_x", int(value)))
        pm_roi_y.valueChanged.connect(lambda value, i=idx: self._update_client_config(i, "pm_roi_offset_y", int(value)))
        pm_roi_w.valueChanged.connect(lambda value, i=idx: self._update_client_config(i, "pm_roi_width", int(value)))
        pm_roi_h.valueChanged.connect(lambda value, i=idx: self._update_client_config(i, "pm_roi_height", int(value)))
        pm_form.addRow("ROI X", pm_roi_x)
        pm_form.addRow("ROI Y", pm_roi_y)
        pm_form.addRow("ROI genişlik", pm_roi_w)
        pm_form.addRow("ROI yükseklik", pm_roi_h)

        ocr_every = QSpinBox(); ocr_every.setRange(1, 60); ocr_every.setValue(int(client_cfg.get("ocr_every", 6)))
        ocr_every.valueChanged.connect(lambda value, i=idx: self._update_client_config(i, "ocr_every", int(value)))
        pm_form.addRow("OCR her N kare", ocr_every)

        min_len = QSpinBox(); min_len.setRange(1, 400); min_len.setValue(int(client_cfg.get("new_msg_min_len", 2)))
        min_len.valueChanged.connect(lambda value, i=idx: self._update_client_config(i, "new_msg_min_len", int(value)))
        pm_form.addRow("Minimum metin uzunluğu", min_len)

        dedupe = QDoubleSpinBox(); dedupe.setRange(0.0, 120.0); dedupe.setDecimals(1); dedupe.setValue(float(client_cfg.get("dedupe_window", 8.0)))
        dedupe.valueChanged.connect(lambda value, i=idx: self._update_client_config(i, "dedupe_window", float(value)))
        pm_form.addRow("Dedupe (sn)", dedupe)

        checksum_check = QCheckBox("Checksum ile değişim algıla")
        checksum_check.setChecked(bool(client_cfg.get("checksum_enabled", True)))
        checksum_check.stateChanged.connect(lambda state, i=idx: self._update_client_config(i, "checksum_enabled", state == Qt.Checked))
        pm_form.addRow(checksum_check)
        form_layout.addWidget(pm_box)

        automation_box = QGroupBox("Otomasyon")
        automation_form = QFormLayout(automation_box)
        workflow_check = QCheckBox("PM otomasyonunu etkinleştir")
        workflow_check.setChecked(bool(client_cfg.get("workflow_enabled", True)))
        workflow_check.stateChanged.connect(lambda state, i=idx: self._update_client_config(i, "workflow_enabled", state == Qt.Checked))
        automation_form.addRow(workflow_check)

        reply_x = QSpinBox(); reply_x.setRange(0, 1000); reply_x.setValue(int(client_cfg.get("reply_offset_x", 271)))
        reply_y = QSpinBox(); reply_y.setRange(0, 1000); reply_y.setValue(int(client_cfg.get("reply_offset_y", 178)))
        reply_x.valueChanged.connect(lambda value, i=idx: self._update_client_config(i, "reply_offset_x", int(value)))
        reply_y.valueChanged.connect(lambda value, i=idx: self._update_client_config(i, "reply_offset_y", int(value)))
        automation_form.addRow("Metin alanı X offset", reply_x)
        automation_form.addRow("Metin alanı Y offset", reply_y)

        form_layout.addWidget(automation_box)
        form_layout.addStretch()

        scroll.setWidget(container)
        layout.addWidget(scroll)
        return page

    def _build_telegram_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        form = QFormLayout(container)

        global_cfg = self.config["global"]
        auto_send_check = QCheckBox("Yeni PM'leri Telegram'a otomatik aktar")
        auto_send_check.setChecked(bool(global_cfg.get("auto_send", True)))
        auto_send_check.stateChanged.connect(lambda state: self._update_global_config("auto_send", state == Qt.Checked))
        form.addRow(auto_send_check)

        token_edit = QLineEdit(global_cfg.get("telegram_token", ""))
        token_edit.setEchoMode(QLineEdit.Password)
        token_edit.textChanged.connect(lambda text: self._update_global_config("telegram_token", text.strip()))
        form.addRow("Bot Token", token_edit)

        chat_edit = QLineEdit(global_cfg.get("telegram_chat_id", ""))
        chat_edit.textChanged.connect(lambda text: self._update_global_config("telegram_chat_id", text.strip()))
        form.addRow("Chat ID", chat_edit)

        automation_cfg = self.config["automation"]
        reply_prefix_edit = QLineEdit(automation_cfg.get("reply_prefix", "#"))
        reply_prefix_edit.setMaxLength(4)
        reply_prefix_edit.textChanged.connect(lambda text: self._update_automation_config("reply_prefix", text.strip()))
        form.addRow("Telegram yanıt ön eki", reply_prefix_edit)

        message_template = QTextEdit()
        message_template.setPlainText(automation_cfg.get("message_template", "{text}"))
        message_template.textChanged.connect(lambda: self._update_automation_config("message_template", message_template.toPlainText()))
        form.addRow("Mesaj şablonu", message_template)

        photo_caption = QLineEdit(automation_cfg.get("photo_caption", "PM #{client_index}"))
        photo_caption.textChanged.connect(lambda text: self._update_automation_config("photo_caption", text))
        form.addRow("Fotoğraf başlığı", photo_caption)

        delivered_template = QLineEdit(automation_cfg.get("delivered_template", "✅ PM gönderildi"))
        delivered_template.textChanged.connect(lambda text: self._update_automation_config("delivered_template", text))
        form.addRow("Gönderim bildirimi", delivered_template)

        awaiting_edit = QLineEdit(automation_cfg.get("awaiting_caption", "✉️ Yanıt bekleniyor"))
        awaiting_edit.textChanged.connect(lambda text: self._update_automation_config("awaiting_caption", text))
        form.addRow("Yanıt bekleniyor etiketi", awaiting_edit)

        reply_timeout_spin = QDoubleSpinBox()
        reply_timeout_spin.setRange(5.0, 600.0)
        reply_timeout_spin.setDecimals(1)
        reply_timeout_spin.setValue(float(automation_cfg.get("reply_timeout", 120.0)))
        reply_timeout_spin.valueChanged.connect(lambda value: self._update_automation_config("reply_timeout", float(value)))
        form.addRow("Yanıt zaman aşımı (sn)", reply_timeout_spin)

        space_delay_spin = QDoubleSpinBox()
        space_delay_spin.setRange(0.0, 2.0)
        space_delay_spin.setDecimals(2)
        space_delay_spin.setSingleStep(0.05)
        space_delay_spin.setValue(float(automation_cfg.get("space_delay", 0.5)))
        space_delay_spin.valueChanged.connect(lambda value: self._update_automation_config("space_delay", float(value)))
        form.addRow("Space gecikmesi", space_delay_spin)

        test_button = QPushButton("Telegram Test Mesajı Gönder")
        test_button.setObjectName("PrimaryButton")
        test_button.clicked.connect(self._send_test_message)
        form.addRow("", test_button)

        info_label = QLabel("Yanıt için Telegram'da '#1 Merhaba' gibi ön ekli mesajlar kullanabilirsiniz.")
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color: #b4bfd6; font-size: 12px;")
        form.addRow("", info_label)

        scroll.setWidget(container)
        layout.addWidget(scroll)
        return page

    def _build_logs_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setObjectName("LogView")
        layout.addWidget(self.log_view)
        return page

    def start_client(self, idx: int) -> None:
        state = self.client_states[idx]
        if state.worker and state.worker.isRunning():
            self.append_log("WARN", f"{state.config.get('name', f'Client {idx + 1}')} zaten çalışıyor.")
            return
        worker = CaptureWorker(idx, self.config["clients"][idx], self.config["global"], self.config["automation"])
        state.worker = worker
        worker.frameReady.connect(self._on_frame_ready)
        worker.frameForSave.connect(self._on_frame_for_save)
        worker.pmPreviewReady.connect(self._on_pm_preview)
        worker.statusUpdated.connect(self._on_status_update)
        worker.logMessage.connect(self._on_worker_log)
        worker.pmTextCaptured.connect(self._on_pm_text)
        worker.awaitingReply.connect(self._on_awaiting_reply)
        worker.finished.connect(lambda i=idx: self._on_worker_finished(i))
        worker.start()
        self.append_log("INFO", f"{state.config.get('name', f'Client {idx + 1}')} başlatıldı.")

    def stop_client(self, idx: int) -> None:
        state = self.client_states[idx]
        worker = state.worker
        if not worker:
            return
        worker.stop()
        worker.wait(3000)
        state.worker = None
        state.awaiting_reply = False
        if state.status_label:
            state.status_label.setText("Durduruldu")
        self.append_log("INFO", f"{state.config.get('name', f'Client {idx + 1}')} durduruldu.")

    def save_screenshot(self, idx: int) -> None:
        state = self.client_states[idx]
        if state.last_frame is None:
            self.append_log("WARN", f"Client {idx + 1} için kaydedilecek kare bulunamadı.")
            return
        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        filename = CAPTURE_DIR / f"client{idx + 1}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        if cv2.imwrite(str(filename), state.last_frame):
            self.append_log("INFO", f"Ekran görüntüsü kaydedildi: {filename}")
        else:
            self.append_log("ERROR", "Ekran görüntüsü kaydedilemedi.")

    def _on_frame_ready(self, idx: int, image: QImage, fps: float) -> None:
        state = self.client_states[idx]
        if state.preview_label:
            state.preview_label.setPixmap(QPixmap.fromImage(image))
        if state.fps_label:
            state.fps_label.setText(f"{fps:0.1f} FPS")

    def _on_frame_for_save(self, idx: int, frame: Any) -> None:
        state = self.client_states[idx]
        state.last_frame = frame

    def _on_pm_preview(self, idx: int, image: QImage, text: str) -> None:
        state = self.client_states[idx]
        if state.pm_preview_label:
            state.pm_preview_label.setPixmap(QPixmap.fromImage(image))
        if state.pm_text_label:
            state.pm_text_label.setText(text)

    def _on_pm_text(self, idx: int, text: str) -> None:
        state = self.client_states[idx]
        if state.pm_list:
            state.pm_list.insertItem(0, text)
            while state.pm_list.count() > 10:
                state.pm_list.takeItem(state.pm_list.count() - 1)

    def _on_status_update(self, idx: int, status: str) -> None:
        state = self.client_states[idx]
        if state.status_label:
            state.status_label.setText(status)

    def _on_worker_log(self, idx: int, level: str, message: str) -> None:
        name = self.config["clients"][idx].get("name", f"Client {idx + 1}")
        self.append_log(level, f"[{name}] {message}")

    def _on_awaiting_reply(self, idx: int, waiting: bool) -> None:
        state = self.client_states[idx]
        state.awaiting_reply = waiting
        if state.awaiting_label:
            state.awaiting_label.setVisible(waiting)

    def _on_worker_finished(self, idx: int) -> None:
        state = self.client_states[idx]
        state.worker = None
        if state.status_label:
            state.status_label.setText("Durduruldu")

    def _on_telegram_message(self, payload: Dict[str, Any]) -> None:
        text = (payload.get("text") or "").strip()
        if not text:
            return
        target, content = self._resolve_reply_target(text)
        if target is None:
            self.append_log("WARN", f"Telegram mesajı yönlendirilemedi: {text}")
            return
        state = self.client_states[target]
        if not state.worker:
            self.append_log("WARN", f"{state.config.get('name', f'Client {target + 1}')} çalışmıyor, mesaj atlandı.")
            return
        if not content:
            self.append_log("WARN", "Gönderilecek mesaj boş.")
            return
        state.worker.enqueue_reply(content)
        self.append_log("INFO", f"Telegram yanıtı {state.config.get('name', f'Client {target + 1}')}: {content}")

    def _on_poller_log(self, level: str, message: str) -> None:
        self.append_log(level, f"[Telegram] {message}")

    def _resolve_reply_target(self, text: str) -> Tuple[Optional[int], str]:
        prefix = self.config["automation"].get("reply_prefix", "#") or ""
        clean = text.strip()
        if prefix and clean.startswith(prefix):
            rest = clean[len(prefix):].lstrip()
            digits = ""
            for ch in rest:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            content = rest[len(digits):].strip()
            if digits:
                idx = int(digits) - 1
                if 0 <= idx < len(self.client_states):
                    return idx, content
        waiting = [i for i, state in enumerate(self.client_states) if state.worker and state.worker.is_waiting_reply()]
        if len(waiting) == 1:
            return waiting[0], clean
        return None, clean

    def _send_test_message(self) -> None:
        if not self.telegram_sender.ready():
            QMessageBox.warning(self, "Telegram", "Bot token ve chat ID girilmeden test mesajı gönderilemez.")
            return
        try:
            self.telegram_sender.send_text("🔔 Test: Merlis PM botu bağlantı doğrulaması.")
            QMessageBox.information(self, "Telegram", "Test mesajı gönderildi.")
        except Exception as exc:
            QMessageBox.warning(self, "Telegram", f"Mesaj gönderilemedi: {exc}")

    def _update_client_config(self, idx: int, key: str, value: Any) -> None:
        self.config["clients"][idx][key] = value
        state = self.client_states[idx]
        if state.worker:
            state.worker.apply_client_update(key, value)
        if key == "name":
            self._update_client_name(idx)
        self._persist_config()

    def _update_client_name(self, idx: int) -> None:
        name = self.config["clients"][idx].get("name", f"Client {idx + 1}")
        if 1 + idx < len(self.nav_buttons):
            self.nav_buttons[1 + idx].setText(name)
        state = self.client_states[idx]
        if state.name_label:
            state.name_label.setText(name)

    def _update_global_config(self, key: str, value: Any) -> None:
        self.config["global"][key] = value
        for state in self.client_states:
            if state.worker:
                state.worker.apply_global_update(key, value)
        if key in {"telegram_token", "telegram_chat_id"}:
            self._update_telegram_credentials()
        self._persist_config()

    def _update_automation_config(self, key: str, value: Any) -> None:
        self.config["automation"][key] = value
        for state in self.client_states:
            if state.worker:
                state.worker.apply_automation_update(key, value)
        if key == "awaiting_caption":
            for state in self.client_states:
                if state.awaiting_label and state.awaiting_reply:
                    state.awaiting_label.setText(value)
        self._persist_config()

    def _update_telegram_credentials(self) -> None:
        token = self.config["global"].get("telegram_token", "")
        chat_id = self.config["global"].get("telegram_chat_id", "")
        self.telegram_sender.configure(token, chat_id)
        self.telegram_poller.configure(token, chat_id)

    def _persist_config(self) -> None:
        save_config(self.config)

    def append_log(self, level: str, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        entry = f"[{timestamp}] {level}: {message}"
        if self.log_view:
            self.log_view.append(entry)
            self.log_view.moveCursor(QTextCursor.End)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        for state in self.client_states:
            if state.worker:
                state.worker.stop()
                state.worker.wait(3000)
                state.worker = None
        if self.telegram_poller.isRunning():
            self.telegram_poller.stop()
            self.telegram_poller.wait(3000)
        save_config(self.config)
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
