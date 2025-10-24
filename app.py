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

from config import ensure_directories, load_config, save_config
from ocr_engine import OCREngine, OCREngineError
from screen_capture import DXCapture, find_pid_by_name, get_window_rect_by_pid
from templates import match_template
from telegram_client import TelegramClient

ASSET_ICON = Path("assets/pm_icon.png")
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
        try:
            self._ocr_engine = OCREngine(use_gpu=config.get("ocr_use_gpu", True))
        except OCREngineError as exc:
            self.initialization_error = str(exc)
            self._ocr_engine = None
        self._telegram = TelegramClient(
            token=config.get("telegram_token", ""),
            chat_id=config.get("telegram_chat_id", ""),
        )
        self._pm_icon = cv2.imread(str(ASSET_ICON), cv2.IMREAD_GRAYSCALE)
        self._last_checksum: Optional[str] = None
        self._last_text: Optional[str] = None
        self._last_text_time: Optional[datetime] = None
        self._frame_counter = 0
        self._fps_window: deque[float] = deque(maxlen=120)

    def stop(self) -> None:
        self._stop_event.set()

    def _resolve_region(self) -> Optional[Tuple[int, int, int, int]]:
        if self.config.get("use_roi_override"):
            left = int(self.config.get("roi_left", 0))
            top = int(self.config.get("roi_top", 0))
            width = int(self.config.get("roi_width", 0))
            height = int(self.config.get("roi_height", 0))
            if width > 0 and height > 0:
                return (left, top, left + width, top + height)
        pid = int(self.config.get("pid") or 0)
        if pid <= 0:
            return None
        rect = get_window_rect_by_pid(pid)
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
                    detected_text = self._maybe_run_ocr(frame, (top, left, bottom, right))
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
        self, frame: np.ndarray, roi_slice: Tuple[int, int, int, int] | np.ndarray
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
        self._handle_telegram(text, roi)
        self.statusUpdated.emit("Yeni PM tespit edildi")
        return text

    def _handle_telegram(self, text: str, roi: np.ndarray) -> None:
        if not self.config.get("auto_send", True):
            return
        if not self._telegram.ready():
            self.logMessage.emit("WARN", "Telegram bilgileri eksik, mesaj gönderilemiyor.")
            return
        try:
            self._telegram.send_text(text)
            self._telegram.send_photo(roi, caption=text[:120])
            self.logMessage.emit("INFO", "Telegram'a PM iletildi.")
        except Exception as exc:  # pragma: no cover
            self.logMessage.emit("ERROR", f"Telegram gönderimi başarısız: {exc}")


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
        layout.addWidget(QLabel("Önizleme ölçeği"), 5, 0)
        layout.addWidget(scale_combo, 5, 1)

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
                "RapidOCR/ONNXRuntime doğru yüklenmediği için OCR devre dışı bırakıldı.\n"
                "Kurulumu kontrol edip uygulamayı yeniden başlatın.",
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
