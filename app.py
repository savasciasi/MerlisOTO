"""PyQt5 GUI for the Merlis PM OCR bot."""
from __future__ import annotations

import hashlib
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import cv2
import numpy as np
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QImage, QPalette, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
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

ASSET_ICON = Path("assets/pm_icon.png")
ASSET_SEND = Path("assets/pm_send_btn.png")
ASSET_CLOSE = Path("assets/pm_close_x.png")
CAPTURE_DIR = Path("captures")


def numpy_to_qimage(frame: np.ndarray) -> QImage:
    height, width, channel = frame.shape
    bytes_per_line = channel * width
    return QImage(frame.data, width, height, bytes_per_line, QImage.Format_BGR888).copy()


class CaptureWorker(QThread):
    frameReady = pyqtSignal(object, float)  # QImage, FPS
    logMessage = pyqtSignal(str, str)
    statusUpdated = pyqtSignal(str)
    pmPreviewReady = pyqtSignal(object, str)  # QImage, text
    frameForSave = pyqtSignal(object)  # np.ndarray overlay frame

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__()
        self.config = config
        self._stop_event = threading.Event()
        self._capture: Optional[DXCapture] = None
        self._ocr_engine: Optional[OCREngine] = None
        self._ocr_error_reported = False
        self.initialization_error: Optional[str] = None
        self._region: Optional[Tuple[int, int, int, int]] = None
        self._window_hwnd: Optional[int] = None
        try:
            self._ocr_engine = OCREngine(
                tesseract_path=config.get("tesseract_path"),
                language=config.get("ocr_lang", "tur+eng"),
            )
        except OCREngineError as exc:
            self.initialization_error = str(exc)
            self._ocr_engine = None
        self._telegram = TelegramClient(
            token=config.get("telegram_token", ""),
            chat_id=config.get("telegram_chat_id", ""),
        )
        self._pm_icon = cv2.imread(str(ASSET_ICON), cv2.IMREAD_GRAYSCALE)
        self._pm_send = cv2.imread(str(ASSET_SEND), cv2.IMREAD_GRAYSCALE)
        self._pm_close = cv2.imread(str(ASSET_CLOSE), cv2.IMREAD_GRAYSCALE)
        self._last_checksum: Optional[str] = None
        self._last_text: Optional[str] = None
        self._last_text_time: Optional[datetime] = None
        self._frame_counter = 0
        self._fps_window: deque[float] = deque(maxlen=120)
        self._workflow_enabled = bool(self.config.get("workflow_enabled", True))
        self._reply_offset_x = int(self.config.get("reply_offset_x", 271))
        self._reply_offset_y = int(self.config.get("reply_offset_y", 178))
        self._auto_reply_text = str(self.config.get("auto_reply_text", "")).strip()
        self._btn_thr = float(self.config.get("btn_thr", 0.8))

    def stop(self) -> None:
        self._stop_event.set()

    def update_setting(self, key: str, value: Any) -> None:
        self.config[key] = value
        if key == "workflow_enabled":
            self._workflow_enabled = bool(value)
        elif key == "reply_offset_x":
            self._reply_offset_x = int(value)
        elif key == "reply_offset_y":
            self._reply_offset_y = int(value)
        elif key == "auto_reply_text":
            self._auto_reply_text = str(value).strip()
        elif key == "btn_thr":
            self._btn_thr = float(value)
        elif key == "auto_send":
            self.config["auto_send"] = bool(value)
        elif key == "telegram_token":
            self._telegram.configure(
                token=self.config.get("telegram_token", ""),
                chat_id=self.config.get("telegram_chat_id", ""),
            )
        elif key == "telegram_chat_id":
            self._telegram.configure(
                token=self.config.get("telegram_token", ""),
                chat_id=self.config.get("telegram_chat_id", ""),
            )

    def _resolve_region(self) -> Optional[Tuple[int, int, int, int]]:
        self._window_hwnd = None
        self._region = None
        if self.config.get("use_roi_override"):
            left = int(self.config.get("roi_left", 0))
            top = int(self.config.get("roi_top", 0))
            width = int(self.config.get("roi_width", 0))
            height = int(self.config.get("roi_height", 0))
            if width > 0 and height > 0:
                region = (left, top, left + width, top + height)
                self._region = region
                return region
        pid = int(self.config.get("pid") or 0)
        if pid <= 0:
            return None
        info = get_window_info_by_pid(pid)
        if info is None:
            return None
        hwnd, rect = info
        self._window_hwnd = hwnd
        self._region = rect
        return rect

    def run(self) -> None:  # noqa: D401
        region = self._resolve_region()
        if region is None:
            self.logMessage.emit("ERROR", "Geçerli pencere bölgesi bulunamadı. PID/ROI ayarlarını kontrol edin.")
            return
        try:
            self._capture = DXCapture(region=region, prefer_dx=self.config.get("dx_prefer", True))
            self._capture.start()
        except Exception as exc:  # pragma: no cover - hardware interaction
            self.logMessage.emit("ERROR", f"dxcam başlatılamadı: {exc}")
            return
        if self._pm_icon is None:
            self.logMessage.emit("WARN", "assets/pm_icon.png okunamadı. PM tespiti yapılamaz.")
        if self._pm_send is None:
            self.logMessage.emit(
                "WARN", "assets/pm_send_btn.png okunamadı. PM otomasyonu kısıtlı olacaktır."
            )
        if self._pm_close is None:
            self.logMessage.emit(
                "WARN", "assets/pm_close_x.png okunamadı. PM penceresi otomatik kapatılamayabilir."
            )
        if self._window_hwnd:
            bring_window_to_front(self._window_hwnd)
        self.logMessage.emit("INFO", "Yakalama başlatıldı.")
        self.statusUpdated.emit("Çalışıyor")
        last_time = time.perf_counter()
        while not self._stop_event.is_set():
            frame = None
            try:
                frame = self._capture.get_latest_frame() if self._capture else None
            except Exception as exc:  # pragma: no cover
                self.logMessage.emit("ERROR", f"Frame alınamadı: {exc}")
                break
            if frame is None:
                time.sleep(0.005)
                continue
            if frame.ndim == 2:
                frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
            self._frame_counter += 1
            overlay_frame, pm_roi, pm_text = self._process_frame(frame)

            now = time.perf_counter()
            dt = max(now - last_time, 1e-6)
            last_time = now
            fps = 1.0 / dt
            self._fps_window.append(fps)
            avg_fps = sum(self._fps_window) / len(self._fps_window)

            scaled_frame = overlay_frame
            scale = float(self.config.get("preview_scale", 0.75) or 1.0)
            if scale and 0 < scale < 1.5:
                scaled_frame = cv2.resize(overlay_frame, (0, 0), fx=scale, fy=scale)
            qimage = numpy_to_qimage(scaled_frame)
            self.frameReady.emit(qimage, avg_fps)
            self.frameForSave.emit(overlay_frame.copy())

            if pm_roi is not None and pm_text is not None:
                self.pmPreviewReady.emit(numpy_to_qimage(pm_roi), pm_text)
            else:
                self.statusUpdated.emit("Çalışıyor")
        self.statusUpdated.emit("Durduruldu")
        if self._capture:
            self._capture.stop()
        self.logMessage.emit("INFO", "Yakalama sonlandırıldı.")

    def _process_frame(self, frame: np.ndarray) -> Tuple[np.ndarray, Optional[np.ndarray], Optional[str]]:
        display = frame.copy()
        pm_roi_img = None
        detected_text = None
        if self._pm_icon is not None:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            matches = match_template(gray, self._pm_icon, float(self.config.get("icon_thr", 0.8)))
            if matches:
                x1, y1, x2, y2, score = matches[0]
                cv2.rectangle(display, (x1, y1), (x2, y2), (0, 200, 255), 2)
                cv2.putText(
                    display,
                    f"icon {score:.2f}",
                    (x1, max(0, y1 - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    (0, 200, 255),
                    1,
                    cv2.LINE_AA,
                )
                icon_box = (x1, y1, x2, y2, score)
                roi_info = self._extract_pm_roi(frame, (x1, y1, x2, y2))
                if roi_info is not None:
                    top, left, bottom, right, roi_img = roi_info
                    pm_roi_img = roi_img
                    cv2.rectangle(
                        display,
                        (left, top),
                        (right, bottom),
                        (120, 255, 120),
                        2,
                    )
                    detected_text = self._maybe_run_ocr(
                        frame,
                        (top, left, bottom, right),
                        roi_img,
                        icon_box,
                    )
        return display, pm_roi_img, detected_text

    def _extract_pm_roi(
        self, frame: np.ndarray, icon_box: Tuple[int, int, int, int]
    ) -> Optional[Tuple[int, int, int, int, np.ndarray]]:
        x1, y1, x2, y2 = icon_box
        offset_x = int(self.config.get("pm_roi_offset_x", 0))
        offset_y = int(self.config.get("pm_roi_offset_y", 40))
        width = int(self.config.get("pm_roi_width", 420))
        height = int(self.config.get("pm_roi_height", 180))
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
        roi_slice: Tuple[int, int, int, int] | np.ndarray,
        roi_image: np.ndarray,
        icon_box: Tuple[int, int, int, int, float],
    ) -> Optional[str]:
        if isinstance(roi_slice, np.ndarray):
            roi = roi_slice
        else:
            top, left, bottom, right = roi_slice
            roi = frame[top:bottom, left:right]
        every = int(self.config.get("ocr_every", 6))
        if every <= 0:
            every = 1
        if self._frame_counter % every != 0:
            return None
        checksum = None
        if self.config.get("checksum_enabled", True):
            checksum = hashlib.md5(roi.tobytes()).hexdigest()
            if checksum == self._last_checksum:
                return None
        if self._ocr_engine is None:
            if self.initialization_error and not self._ocr_error_reported:
                self.logMessage.emit("ERROR", self.initialization_error)
                self._ocr_error_reported = True
            return None
        try:
            text = self._ocr_engine.read(roi)
        except OCREngineError as exc:
            if not self._ocr_error_reported:
                self.logMessage.emit("ERROR", str(exc))
                self._ocr_error_reported = True
            return None
        if not text or len(text.strip()) < int(self.config.get("new_msg_min_len", 2)):
            return None
        now = datetime.utcnow()
        dedupe_window = float(self.config.get("dedupe_window", 8.0))
        if self._last_text == text and self._last_text_time:
            if now - self._last_text_time < timedelta(seconds=dedupe_window):
                return None
        self._last_text = text
        self._last_text_time = now
        if checksum is not None:
            self._last_checksum = checksum
        self.logMessage.emit("INFO", f"Yeni PM: {text}")
        self._handle_new_pm(text, roi.copy(), frame.copy(), roi_image.copy(), icon_box)
        self.statusUpdated.emit("Yeni PM tespit edildi")
        return text

    def _handle_telegram(self, text: str, screenshot: np.ndarray, roi: Optional[np.ndarray] = None) -> None:
        if not self.config.get("auto_send", True):
            return
        if not self._telegram.ready():
            self.logMessage.emit("WARN", "Telegram bilgileri eksik, mesaj gönderilemiyor.")
            return
        try:
            caption = text[:120]
            self._telegram.send_text(text)
            self._telegram.send_photo(screenshot, caption=caption)
            if roi is not None:
                self._telegram.send_photo(roi, caption="PM ROI")
            self.logMessage.emit("INFO", "Telegram'a PM iletildi.")
        except Exception as exc:  # pragma: no cover
            self.logMessage.emit("ERROR", f"Telegram gönderimi başarısız: {exc}")

    def _handle_new_pm(
        self,
        text: str,
        roi: np.ndarray,
        frame: np.ndarray,
        roi_image: np.ndarray,
        icon_box: Tuple[int, int, int, int, float],
    ) -> None:
        if self._workflow_enabled and self._execute_pm_workflow(text, roi_image, frame, icon_box):
            return
        self._handle_telegram(text, frame, roi)

    def _execute_pm_workflow(
        self,
        text: str,
        roi: np.ndarray,
        fallback_frame: np.ndarray,
        icon_box: Tuple[int, int, int, int, float],
    ) -> bool:
        if self._capture is None or self._region is None:
            return False
        try:
            bring_window_to_front(self._window_hwnd)
            icon_center = self._box_center(icon_box)
            screen_point = self._to_screen(*icon_center)
            if screen_point is None:
                return False
            click_left(*screen_point)
            time.sleep(0.25)

            send_box, latest_frame = self._find_template(self._pm_send, self._btn_thr)
            if send_box is None or latest_frame is None:
                self.logMessage.emit("WARN", "pm_send_btn.png tespit edilemedi.")
                return False

            send_center = self._box_center(send_box)
            send_screen = self._to_screen(*send_center)
            if send_screen is None:
                return False

            text_point = (
                int(send_center[0] - self._reply_offset_x),
                int(send_center[1] - self._reply_offset_y),
            )
            text_screen = self._to_screen(*text_point)
            if text_screen is None:
                self.logMessage.emit("WARN", "Metin alanı koordinatları pencere dışında kaldı.")
                return False

            click_left(*text_screen)
            time.sleep(0.15)

            screenshot = latest_frame.copy() if latest_frame is not None else fallback_frame
            self._handle_telegram(text, screenshot, roi)

            if self._auto_reply_text:
                paste_text_via_clipboard(self._auto_reply_text)
                time.sleep(0.15)
            else:
                self.logMessage.emit("WARN", "Otomatik cevap metni boş, mesaj gönderilmedi.")

            click_left(*send_screen)
            time.sleep(0.25)

            self._close_pm_window()
            send_space(delay=0.5)
            return True
        except Exception as exc:  # pragma: no cover - defensive automation guard
            self.logMessage.emit("ERROR", f"PM otomasyonu sırasında hata: {exc}")
            return False

    def _close_pm_window(self) -> None:
        if self._pm_close is None:
            self.logMessage.emit("WARN", "pm_close_x.png bulunamadı, pencere kapatılamadı.")
            return
        match, _ = self._find_template(self._pm_close, self._btn_thr, attempts=6)
        if match is None:
            self.logMessage.emit("WARN", "PM pencere kapatma simgesi bulunamadı.")
            return
        center = self._box_center(match)
        screen = self._to_screen(*center)
        if screen is None:
            return
        click_left(*screen)

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

    def _box_center(self, box: Tuple[int, int, int, int, float]) -> Tuple[int, int]:
        x1, y1, x2, y2, _ = box
        return int((x1 + x2) / 2), int((y1 + y2) / 2)

    def _to_screen(self, x: int, y: int) -> Optional[Tuple[int, int]]:
        if self._region is None:
            return None
        left, top, _, _ = self._region
        return int(left + x), int(top + y)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        ensure_directories()
        self.config = load_config()
        self.capture_thread: Optional[CaptureWorker] = None
        self.latest_overlay: Optional[np.ndarray] = None
        self.preview_label = QLabel(alignment=Qt.AlignCenter)
        self.fps_label = QLabel("FPS: 0")
        self.status_label = QLabel("Durum: Hazır")
        self.last_messages = QListWidget()
        self.log_panel = QTextEdit()
        self.log_panel.setReadOnly(True)
        self.pm_preview_label = QLabel("PM önizleme yok", alignment=Qt.AlignCenter)
        self.pm_preview_label.setMinimumHeight(150)
        self.telegram_client = TelegramClient(
            token=self.config.get("telegram_token", ""),
            chat_id=self.config.get("telegram_chat_id", ""),
        )
        self._build_ui()
        self.update_status("Hazır")

    # UI construction -------------------------------------------------
    def _build_ui(self) -> None:
        self.setWindowTitle("Merlis Metin2 PM OCR Kontrol Paneli")
        central = QWidget()
        layout = QVBoxLayout(central)
        tabs = QTabWidget()
        tabs.addTab(self._build_dashboard_tab(), "Dashboard")
        tabs.addTab(self._build_pm_tab(), "PM Algılama")
        tabs.addTab(self._build_window_tab(), "Pencere / PID")
        tabs.addTab(self._build_telegram_tab(), "Telegram")
        tabs.addTab(self._build_logs_tab(), "Loglar")
        layout.addWidget(tabs)
        self.setCentralWidget(central)

    def _build_dashboard_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(self.preview_label, stretch=1)

        info_row = QHBoxLayout()
        info_row.addWidget(self.fps_label)
        info_row.addWidget(self.status_label)
        info_row.addStretch(1)
        layout.addLayout(info_row)

        btn_row = QHBoxLayout()
        start_btn = QPushButton("Başlat")
        stop_btn = QPushButton("Durdur")
        shot_btn = QPushButton("Ekran Görüntüsü Kaydet")
        start_btn.clicked.connect(self.start_capture)
        stop_btn.clicked.connect(self.stop_capture)
        shot_btn.clicked.connect(self.save_screenshot)
        btn_row.addWidget(start_btn)
        btn_row.addWidget(stop_btn)
        btn_row.addWidget(shot_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        pm_group = QGroupBox("Son PM Mesajları")
        pm_layout = QVBoxLayout(pm_group)
        pm_layout.addWidget(self.pm_preview_label)
        pm_layout.addWidget(self.last_messages)
        layout.addWidget(pm_group)
        return tab

    def _build_pm_tab(self) -> QWidget:
        tab = QWidget()
        layout = QGridLayout(tab)

        icon_thr_spin = QDoubleSpinBox()
        icon_thr_spin.setRange(0.1, 1.0)
        icon_thr_spin.setSingleStep(0.01)
        icon_thr_spin.setValue(float(self.config.get("icon_thr", 0.8)))
        icon_thr_spin.valueChanged.connect(lambda v: self._update_config("icon_thr", float(v)))

        ocr_every_spin = QSpinBox()
        ocr_every_spin.setRange(1, 30)
        ocr_every_spin.setValue(int(self.config.get("ocr_every", 6)))
        ocr_every_spin.valueChanged.connect(lambda v: self._update_config("ocr_every", int(v)))

        min_len_spin = QSpinBox()
        min_len_spin.setRange(1, 200)
        min_len_spin.setValue(int(self.config.get("new_msg_min_len", 2)))
        min_len_spin.valueChanged.connect(lambda v: self._update_config("new_msg_min_len", int(v)))

        dedupe_spin = QDoubleSpinBox()
        dedupe_spin.setRange(0.0, 60.0)
        dedupe_spin.setDecimals(1)
        dedupe_spin.setValue(float(self.config.get("dedupe_window", 8.0)))
        dedupe_spin.valueChanged.connect(lambda v: self._update_config("dedupe_window", float(v)))

        checksum_box = QCheckBox("Checksum ile değişim algıla")
        checksum_box.setChecked(bool(self.config.get("checksum_enabled", True)))
        checksum_box.stateChanged.connect(
            lambda state: self._update_config("checksum_enabled", state == Qt.Checked)
        )

        workflow_box = QCheckBox("PM otomasyonunu etkinleştir")
        workflow_box.setChecked(bool(self.config.get("workflow_enabled", True)))
        workflow_box.stateChanged.connect(
            lambda state: self._update_config("workflow_enabled", state == Qt.Checked)
        )

        reply_text_edit = QLineEdit(self.config.get("auto_reply_text", ""))
        reply_text_edit.setPlaceholderText("Otomatik cevap metni")
        reply_text_edit.textChanged.connect(lambda text: self._update_config("auto_reply_text", text))

        offset_x_spin = QSpinBox()
        offset_x_spin.setRange(0, 1000)
        offset_x_spin.setValue(int(self.config.get("reply_offset_x", 271)))
        offset_x_spin.valueChanged.connect(lambda v: self._update_config("reply_offset_x", int(v)))

        offset_y_spin = QSpinBox()
        offset_y_spin.setRange(0, 1000)
        offset_y_spin.setValue(int(self.config.get("reply_offset_y", 178)))
        offset_y_spin.valueChanged.connect(lambda v: self._update_config("reply_offset_y", int(v)))

        tesseract_edit = QLineEdit(self.config.get("tesseract_path", ""))
        tesseract_edit.setPlaceholderText(r"C:\\Program Files\\Tesseract-OCR\\tesseract.exe")
        tesseract_edit.textChanged.connect(
            lambda text: self._update_config("tesseract_path", text.strip())
        )

        ocr_lang_edit = QLineEdit(self.config.get("ocr_lang", "tur+eng"))
        ocr_lang_edit.setPlaceholderText("tur+eng")
        ocr_lang_edit.setToolTip("Tesseract dil kodlarını '+' ile birleştirerek giriniz. Örn: tur+eng")
        ocr_lang_edit.textChanged.connect(
            lambda text: self._update_config("ocr_lang", text.strip() or "eng")
        )

        scale_combo = QComboBox()
        for label, value in [("0.5", 0.5), ("0.75", 0.75), ("1.0", 1.0)]:
            scale_combo.addItem(label, value)
        current_scale = float(self.config.get("preview_scale", 0.75))
        idx = max(0, scale_combo.findData(current_scale))
        scale_combo.setCurrentIndex(idx)
        scale_combo.currentIndexChanged.connect(
            lambda i: self._update_config("preview_scale", float(scale_combo.itemData(i)))
        )

        layout.addWidget(QLabel("Icon Eşik"), 0, 0)
        layout.addWidget(icon_thr_spin, 0, 1)
        layout.addWidget(QLabel("OCR her N kare"), 1, 0)
        layout.addWidget(ocr_every_spin, 1, 1)
        layout.addWidget(QLabel("Minimum metin uzunluğu"), 2, 0)
        layout.addWidget(min_len_spin, 2, 1)
        layout.addWidget(QLabel("Dedupe saniye"), 3, 0)
        layout.addWidget(dedupe_spin, 3, 1)
        layout.addWidget(checksum_box, 4, 0, 1, 2)
        layout.addWidget(workflow_box, 5, 0, 1, 2)
        layout.addWidget(QLabel("Otomatik cevap metni"), 6, 0)
        layout.addWidget(reply_text_edit, 6, 1)
        layout.addWidget(QLabel("Metin alanı X offset"), 7, 0)
        layout.addWidget(offset_x_spin, 7, 1)
        layout.addWidget(QLabel("Metin alanı Y offset"), 8, 0)
        layout.addWidget(offset_y_spin, 8, 1)
        layout.addWidget(QLabel("Önizleme ölçeği"), 9, 0)
        layout.addWidget(scale_combo, 9, 1)
        layout.addWidget(QLabel("Tesseract yolu"), 10, 0)
        layout.addWidget(tesseract_edit, 10, 1)
        layout.addWidget(QLabel("OCR dil (ör. tur+eng)"), 11, 0)
        layout.addWidget(ocr_lang_edit, 11, 1)

        return tab

    def _build_window_tab(self) -> QWidget:
        tab = QWidget()
        layout = QGridLayout(tab)

        process_hint_edit = QLineEdit(self.config.get("process_hint", "merlis"))
        process_hint_edit.textChanged.connect(lambda text: self._update_config("process_hint", text))

        pid_spin = QSpinBox()
        pid_spin.setRange(0, 1_000_000)
        pid_spin.setValue(int(self.config.get("pid", 0)))
        pid_spin.valueChanged.connect(lambda v: self._update_config("pid", int(v)))

        find_btn = QPushButton("Merlis'i Bul (PID)")

        def _find_pid() -> None:
            hint = process_hint_edit.text().strip()
            pid = find_pid_by_name(hint)
            if pid is None:
                QMessageBox.warning(self, "PID bulunamadı", f"'{hint}' için süreç bulunamadı.")
                return
            pid_spin.setValue(pid)
            self._update_config("pid", pid)
            self.log("INFO", f"PID bulundu: {pid}")

        find_btn.clicked.connect(_find_pid)

        use_override = QCheckBox("ROI Override kullan")
        use_override.setChecked(bool(self.config.get("use_roi_override", False)))
        use_override.stateChanged.connect(
            lambda state: self._update_config("use_roi_override", state == Qt.Checked)
        )

        roi_top = QSpinBox()
        roi_top.setRange(0, 4000)
        roi_top.setValue(int(self.config.get("roi_top", 0)))
        roi_top.valueChanged.connect(lambda v: self._update_config("roi_top", int(v)))

        roi_left = QSpinBox()
        roi_left.setRange(0, 4000)
        roi_left.setValue(int(self.config.get("roi_left", 0)))
        roi_left.valueChanged.connect(lambda v: self._update_config("roi_left", int(v)))

        roi_width = QSpinBox()
        roi_width.setRange(0, 4000)
        roi_width.setValue(int(self.config.get("roi_width", 0)))
        roi_width.valueChanged.connect(lambda v: self._update_config("roi_width", int(v)))

        roi_height = QSpinBox()
        roi_height.setRange(0, 4000)
        roi_height.setValue(int(self.config.get("roi_height", 0)))
        roi_height.valueChanged.connect(lambda v: self._update_config("roi_height", int(v)))

        layout.addWidget(QLabel("Process ipucu"), 0, 0)
        layout.addWidget(process_hint_edit, 0, 1)
        layout.addWidget(find_btn, 0, 2)
        layout.addWidget(QLabel("PID"), 1, 0)
        layout.addWidget(pid_spin, 1, 1)
        layout.addWidget(use_override, 2, 0, 1, 3)
        layout.addWidget(QLabel("Top"), 3, 0)
        layout.addWidget(roi_top, 3, 1)
        layout.addWidget(QLabel("Left"), 3, 2)
        layout.addWidget(roi_left, 3, 3)
        layout.addWidget(QLabel("Width"), 4, 0)
        layout.addWidget(roi_width, 4, 1)
        layout.addWidget(QLabel("Height"), 4, 2)
        layout.addWidget(roi_height, 4, 3)
        return tab

    def _build_telegram_tab(self) -> QWidget:
        tab = QWidget()
        layout = QGridLayout(tab)

        token_edit = QLineEdit(self.config.get("telegram_token", ""))
        token_edit.setEchoMode(QLineEdit.Password)
        chat_id_edit = QLineEdit(self.config.get("telegram_chat_id", ""))
        auto_send_box = QCheckBox("Yeni PM gelince otomatik gönder")
        auto_send_box.setChecked(bool(self.config.get("auto_send", True)))

        token_edit.textChanged.connect(self._on_token_changed)
        chat_id_edit.textChanged.connect(self._on_chat_id_changed)
        auto_send_box.stateChanged.connect(
            lambda state: self._update_config("auto_send", state == Qt.Checked)
        )

        test_button = QPushButton("Test Mesajı Gönder")

        def _send_test() -> None:
            if not self.telegram_client.ready():
                QMessageBox.warning(self, "Telegram", "Token ve Chat ID giriniz.")
                return
            try:
                self.telegram_client.send_text("Merlis PM OCR bot test mesajı")
                QMessageBox.information(self, "Telegram", "Mesaj gönderildi.")
            except Exception as exc:
                QMessageBox.critical(self, "Telegram", f"Gönderim başarısız: {exc}")

        test_button.clicked.connect(_send_test)

        layout.addWidget(QLabel("Bot Token"), 0, 0)
        layout.addWidget(token_edit, 0, 1)
        layout.addWidget(QLabel("Chat ID"), 1, 0)
        layout.addWidget(chat_id_edit, 1, 1)
        layout.addWidget(auto_send_box, 2, 0, 1, 2)
        layout.addWidget(test_button, 3, 0, 1, 2)
        return tab

    def _build_logs_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(self.log_panel)
        clear_btn = QPushButton("Log'u temizle")
        clear_btn.clicked.connect(self.log_panel.clear)
        layout.addWidget(clear_btn)
        return tab

    # Config helpers --------------------------------------------------
    def _update_config(self, key: str, value: Any) -> None:
        self.config[key] = value
        save_config(self.config)
        if key in {"telegram_token", "telegram_chat_id"}:
            self.telegram_client.configure(
                self.config.get("telegram_token", ""),
                self.config.get("telegram_chat_id", ""),
            )
        if self.capture_thread and self.capture_thread.isRunning():
            self.capture_thread.update_setting(key, value)

    def _on_token_changed(self, text: str) -> None:
        self._update_config("telegram_token", text)

    def _on_chat_id_changed(self, text: str) -> None:
        self._update_config("telegram_chat_id", text)

    # Capture lifecycle -----------------------------------------------
    def start_capture(self) -> None:
        if self.capture_thread and self.capture_thread.isRunning():
            QMessageBox.information(self, "Bilgi", "Yakalama zaten çalışıyor.")
            return
        worker = CaptureWorker(dict(self.config))
        self.capture_thread = worker
        self.capture_thread.frameReady.connect(self.on_frame_ready)
        self.capture_thread.logMessage.connect(self.log)
        self.capture_thread.statusUpdated.connect(self.update_status)
        self.capture_thread.pmPreviewReady.connect(self.on_pm_preview)
        self.capture_thread.frameForSave.connect(self._on_frame_for_save)
        if worker.initialization_error:
            self.log("ERROR", worker.initialization_error)
            QMessageBox.warning(
                self,
                "OCR Başlatılamadı",
                "Tesseract veya PyTesseract doğru yapılandırılamadığı için OCR devre dışı bırakıldı.\n"
                "Tesseract kurulumunu ve yol ayarını kontrol edip uygulamayı yeniden başlatın.",
            )
            worker._ocr_error_reported = True
        self.capture_thread.start()
        self.update_status("Başlatılıyor")

    def stop_capture(self) -> None:
        if self.capture_thread:
            self.capture_thread.stop()
            self.capture_thread.wait(2000)
            self.capture_thread = None
            self.update_status("Durduruldu")

    def closeEvent(self, event) -> None:  # noqa: D401
        self.stop_capture()
        save_config(self.config)
        super().closeEvent(event)

    # Slots -----------------------------------------------------------
    def on_frame_ready(self, qimage: QImage, fps: float) -> None:
        self.fps_label.setText(f"FPS: {fps:.1f}")
        self.preview_label.setPixmap(QPixmap.fromImage(qimage))

    def on_pm_preview(self, qimage: QImage, text: str) -> None:
        self.pm_preview_label.setPixmap(QPixmap.fromImage(qimage))
        timestamp = datetime.now().strftime("%H:%M:%S")
        item_text = f"[{timestamp}] {text}" if text else f"[{timestamp}] (boş)"
        self.last_messages.insertItem(0, item_text)
        while self.last_messages.count() > 10:
            self.last_messages.takeItem(self.last_messages.count() - 1)

    def _on_frame_for_save(self, frame: np.ndarray) -> None:
        self.latest_overlay = frame

    def update_status(self, message: str) -> None:
        self.status_label.setText(f"Durum: {message}")

    def log(self, level: str, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        entry = f"[{timestamp}] {level}: {message}"
        self.log_panel.append(entry)

    # Actions ---------------------------------------------------------
    def save_screenshot(self) -> None:
        if self.latest_overlay is None:
            QMessageBox.information(self, "Ekran görüntüsü", "Henüz görüntü yok.")
            return
        ensure_directories()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = CAPTURE_DIR / f"capture_{timestamp}.png"
        try:
            cv2.imwrite(str(filename), self.latest_overlay)
            self.log("INFO", f"Ekran görüntüsü kaydedildi: {filename}")
        except Exception as exc:
            QMessageBox.critical(self, "Kaydetme hatası", str(exc))


def apply_dark_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.Window, Qt.black)
    palette.setColor(QPalette.WindowText, Qt.white)
    palette.setColor(QPalette.Base, Qt.black)
    palette.setColor(QPalette.AlternateBase, Qt.gray)
    palette.setColor(QPalette.ToolTipBase, Qt.white)
    palette.setColor(QPalette.ToolTipText, Qt.white)
    palette.setColor(QPalette.Text, Qt.white)
    palette.setColor(QPalette.Button, Qt.gray)
    palette.setColor(QPalette.ButtonText, Qt.white)
    palette.setColor(QPalette.Highlight, Qt.darkYellow)
    palette.setColor(QPalette.HighlightedText, Qt.black)
    app.setPalette(palette)


def main() -> None:
    ensure_directories()
    app = QApplication(sys.argv)
    apply_dark_theme(app)
    window = MainWindow()
    window.resize(960, 720)
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
