"""PyQt5 GUI entry point for the Merlis Metin2 Bot Control Panel."""
from __future__ import annotations

import sys
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np
from mss import mss
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QKeySequence, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QShortcut,
)

from config import AppState, ConfigManager, ensure_directories
from detector import (
    PLAYER_LABEL,
    PM_BOX_LABEL,
    Detection,
    RoboflowDetector,
    SlidingWindowFPS,
    convert_frame_to_qimage,
    crop_with_padding,
    draw_detections,
)
from telegram_client import TelegramClient, TelegramCredentials


class DetectionWorker(QThread):
    frameReady = pyqtSignal(object, list)  # QImage, detections
    statsUpdated = pyqtSignal(float, str)
    logMessage = pyqtSignal(str, str)
    detectionsUpdated = pyqtSignal(list)
    pmPreviewReady = pyqtSignal(object)  # QImage

    def __init__(
        self,
        app_state: AppState,
        video_source: Optional[str] = None,
    ) -> None:
        super().__init__()
        self._state = app_state
        self._video_source = video_source
        self._stop_event = threading.Event()
        self._detector = RoboflowDetector(
            api_key=app_state.roboflow.api_key,
            workspace=app_state.roboflow.workspace,
            project=app_state.roboflow.project,
            version=app_state.roboflow.version,
        )
        self._telegram_client: Optional[TelegramClient] = None
        self._last_event = "Hazır"
        self._fps_counter = SlidingWindowFPS()
        self._last_detection_error: Optional[str] = None
        self._last_telegram_error: Optional[str] = None

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:  # noqa: D401
        if self._video_source:
            self._run_video()
        else:
            self._run_live()

    def _run_video(self) -> None:
        cap = cv2.VideoCapture(self._video_source)
        if not cap.isOpened():
            self.logMessage.emit("ERROR", "Video dosyası açılamadı.")
            return
        while not self._stop_event.is_set():
            ret, frame = cap.read()
            if not ret:
                break
            self._process_frame(frame)
            time.sleep(0.03)
        cap.release()

    def _run_live(self) -> None:
        with mss() as screen:
            monitors = screen.monitors
            index = min(max(self._state.detection.monitor_index, 0), len(monitors) - 1)
            monitor = monitors[index]
            while not self._stop_event.is_set():
                sct_img = screen.grab(monitor)
                frame = np.array(sct_img)
                frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                self._process_frame(frame)

    def _process_frame(self, frame: np.ndarray) -> None:
        try:
            labels = [PLAYER_LABEL]
            if self._state.detection.enable_pm_box:
                labels.append(PM_BOX_LABEL)
            detections = self._detector.predict(
                frame,
                confidence=self._state.roboflow.confidence,
                overlap=self._state.roboflow.overlap,
                labels=labels,
            )
        except Exception as exc:
            self._handle_prediction_failure(frame, str(exc))
            return

        self._clear_prediction_error()

        filtered = [d for d in detections if d.label == PLAYER_LABEL or self._state.detection.enable_pm_box]
        if self._state.detection.show_only_player:
            filtered = [d for d in filtered if d.label == PLAYER_LABEL]

        annotated = draw_detections(frame, filtered)
        if filtered:
            self._last_event = f"{len(filtered)} tespit bulundu"
            self.detectionsUpdated.emit(filtered)

        fps = self._fps_counter.update()
        qimage = convert_frame_to_qimage(annotated)
        self.frameReady.emit(qimage, filtered)
        self.statsUpdated.emit(fps, self._last_event)

        if self._state.detection.enable_pm_box:
            padding = max(self._state.telegram.padding, 0)
            for det in filtered:
                if det.label != PM_BOX_LABEL:
                    continue
                cropped = crop_with_padding(frame, det, padding)
                preview = convert_frame_to_qimage(cropped)
                self.pmPreviewReady.emit(preview)
                if self._state.telegram.auto_send:
                    if not self._state.telegram.bot_token or not self._state.telegram.chat_id:
                        self._log_telegram_error("Telegram bot token veya chat ID ayarlanmadı.")
                    else:
                        if self._telegram_client is None:
                            credentials = TelegramCredentials(
                                bot_token=self._state.telegram.bot_token,
                                chat_id=self._state.telegram.chat_id,
                            )
                            self._telegram_client = TelegramClient(credentials)
                        if self._telegram_client:
                            try:
                                self._telegram_client.send_image(cropped, caption="Yeni PM tespiti")
                                self._last_event = "PM kutusu gönderildi"
                                self.logMessage.emit("INFO", "PM kutusu Telegram'a gönderildi.")
                                self._clear_telegram_error()
                            except Exception as exc:
                                self._log_telegram_error(str(exc))
                                self._telegram_client = None
                break

    def _handle_prediction_failure(self, frame: np.ndarray, message: str) -> None:
        formatted = message if message.startswith("Tahmin") else f"Tahmin yapılamadı: {message}"
        if self._last_detection_error != formatted:
            self.logMessage.emit("ERROR", formatted)
            self._last_detection_error = formatted
        self._last_event = "Tahmin hatası"
        fps = self._fps_counter.update()
        qimage = convert_frame_to_qimage(frame)
        self.frameReady.emit(qimage, [])
        self.statsUpdated.emit(fps, self._last_event)
        time.sleep(0.5)

    def _clear_prediction_error(self) -> None:
        self._last_detection_error = None

    def _log_telegram_error(self, message: str) -> None:
        formatted = f"Telegram mesajı gönderilemedi: {message}"
        if self._last_telegram_error != formatted:
            self.logMessage.emit("ERROR", formatted)
            self._last_telegram_error = formatted

    def _clear_telegram_error(self) -> None:
        self._last_telegram_error = None


class LogPanel:
    def __init__(self, widget: QTextEdit) -> None:
        self.widget = widget
        self.widget.setReadOnly(True)

    def append(self, level: str, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted = f"[{timestamp}] {level}: {message}"
        self.widget.append(formatted)


class MainWindow(QMainWindow):
    def __init__(self, config: ConfigManager) -> None:
        super().__init__()
        self.setWindowTitle("Merlis Metin2 Bot Kontrol Paneli")
        self.resize(1280, 800)
        self.config = config
        self.worker: Optional[DetectionWorker] = None
        self.player_detections: List[str] = []
        self.pm_previews: List[QPixmap] = []
        ensure_directories(config.state)

        self._build_ui()
        self._apply_theme()
        self._connect_signals()

    # region UI setup
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)

        self.nav_list = QListWidget()
        self.nav_list.setFixedWidth(220)
        self.nav_list.addItems(
            [
                "Dashboard",
                "Oyuncu Tanıma",
                "PM Mesaj Algılama",
                "Model / API",
                "Telegram",
                "Loglar",
            ]
        )
        self.nav_list.setCurrentRow(0)
        layout.addWidget(self.nav_list)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)

        self._init_dashboard()
        self._init_player_page()
        self._init_pm_page()
        self._init_model_page()
        self._init_telegram_page()
        self._init_logs_page()

    def _init_dashboard(self) -> None:
        page = QWidget()
        vbox = QVBoxLayout(page)

        self.preview_label = QLabel("Önizleme bekleniyor...")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumHeight(480)
        self.preview_label.setStyleSheet("background-color: #202020; border: 1px solid #444;")
        vbox.addWidget(self.preview_label)

        button_layout = QHBoxLayout()
        self.start_button = QPushButton("Başlat")
        self.start_button.setStyleSheet("background-color: #2ecc71; color: white; padding: 8px 16px;")
        self.stop_button = QPushButton("Durdur")
        self.stop_button.setStyleSheet("background-color: #e74c3c; color: white; padding: 8px 16px;")
        self.stop_button.setEnabled(False)
        button_layout.addWidget(self.start_button)
        button_layout.addWidget(self.stop_button)
        vbox.addLayout(button_layout)

        info_layout = QHBoxLayout()
        self.fps_label = QLabel("FPS: 0.0")
        self.last_event_label = QLabel("Son Olay: Yok")
        info_layout.addWidget(self.fps_label)
        info_layout.addWidget(self.last_event_label)
        info_layout.addStretch(1)
        vbox.addLayout(info_layout)

        self.stack.addWidget(page)

    def _init_player_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)

        slider_group = QGroupBox("Tespit Ayarları")
        grid = QGridLayout(slider_group)

        self.confidence_slider = QSlider(Qt.Horizontal)
        self.confidence_slider.setRange(0, 100)
        self.confidence_slider.setValue(int(self.config.state.roboflow.confidence * 100))
        grid.addWidget(QLabel("Güven: %"), 0, 0)
        grid.addWidget(self.confidence_slider, 0, 1)

        self.overlap_slider = QSlider(Qt.Horizontal)
        self.overlap_slider.setRange(0, 100)
        self.overlap_slider.setValue(int(self.config.state.roboflow.overlap * 100))
        grid.addWidget(QLabel("Örtüşme: %"), 1, 0)
        grid.addWidget(self.overlap_slider, 1, 1)

        self.only_player_checkbox = QCheckBox("Sadece oyuncu tespitlerini göster")
        self.only_player_checkbox.setChecked(self.config.state.detection.show_only_player)
        grid.addWidget(self.only_player_checkbox, 2, 0, 1, 2)

        layout.addWidget(slider_group)

        self.capture_button = QPushButton("Ekran Görüntüsü Kaydet")
        layout.addWidget(self.capture_button)

        self.last_detections_list = QListWidget()
        layout.addWidget(QLabel("Son 10 Tespit"))
        layout.addWidget(self.last_detections_list)

        self.stack.addWidget(page)

    def _init_pm_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)

        self.pm_detection_checkbox = QCheckBox("PM kutusu tespitini etkinleştir")
        self.pm_detection_checkbox.setChecked(self.config.state.detection.enable_pm_box)
        layout.addWidget(self.pm_detection_checkbox)

        self.pm_auto_send_checkbox = QCheckBox("Tespit edilince otomatik Telegram gönder")
        self.pm_auto_send_checkbox.setChecked(self.config.state.telegram.auto_send)
        layout.addWidget(self.pm_auto_send_checkbox)

        padding_layout = QHBoxLayout()
        padding_layout.addWidget(QLabel("Kırpma padding:"))
        self.padding_spin = QSpinBox()
        self.padding_spin.setRange(0, 200)
        self.padding_spin.setValue(self.config.state.telegram.padding)
        padding_layout.addWidget(self.padding_spin)
        padding_layout.addStretch(1)
        layout.addLayout(padding_layout)

        layout.addWidget(QLabel("Son gönderilen 3 PM önizlemesi"))
        self.pm_preview_layout = QHBoxLayout()
        for _ in range(3):
            lbl = QLabel()
            lbl.setFixedSize(160, 120)
            lbl.setStyleSheet("background-color: #1f1f1f; border: 1px solid #555;")
            lbl.setScaledContents(True)
            self.pm_preview_layout.addWidget(lbl)
        layout.addLayout(self.pm_preview_layout)

        self.stack.addWidget(page)

    def _init_model_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)

        self.api_key_edit = QLineEdit(self.config.state.roboflow.api_key)
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.workspace_edit = QLineEdit(self.config.state.roboflow.workspace)
        self.project_edit = QLineEdit(self.config.state.roboflow.project)
        self.version_spin = QSpinBox()
        self.version_spin.setRange(1, 99)
        self.version_spin.setValue(self.config.state.roboflow.version)

        form = QGridLayout()
        form.addWidget(QLabel("API Anahtarı"), 0, 0)
        form.addWidget(self.api_key_edit, 0, 1)
        form.addWidget(QLabel("Çalışma Alanı"), 1, 0)
        form.addWidget(self.workspace_edit, 1, 1)
        form.addWidget(QLabel("Proje"), 2, 0)
        form.addWidget(self.project_edit, 2, 1)
        form.addWidget(QLabel("Versiyon"), 3, 0)
        form.addWidget(self.version_spin, 3, 1)
        layout.addLayout(form)

        self.test_model_button = QPushButton("Tek Kare Test Et")
        layout.addWidget(self.test_model_button)

        self.stack.addWidget(page)

    def _init_telegram_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)

        self.bot_token_edit = QLineEdit(self.config.state.telegram.bot_token)
        self.bot_token_edit.setEchoMode(QLineEdit.Password)
        self.chat_id_edit = QLineEdit(self.config.state.telegram.chat_id)

        layout.addWidget(QLabel("Bot Token"))
        layout.addWidget(self.bot_token_edit)
        layout.addWidget(QLabel("Chat ID"))
        layout.addWidget(self.chat_id_edit)

        self.test_message_button = QPushButton("Test Mesajı Gönder")
        layout.addWidget(self.test_message_button)

        self.stack.addWidget(page)

    def _init_logs_page(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)

        self.log_text = QTextEdit()
        layout.addWidget(self.log_text)
        self.log_panel = LogPanel(self.log_text)

        self.clear_logs_button = QPushButton("Log'u temizle")
        layout.addWidget(self.clear_logs_button)

        self.stack.addWidget(page)

    # endregion

    def _apply_theme(self) -> None:
        self.setStyleSheet(
            """
            QWidget {
                background-color: #121212;
                color: #f0f0f0;
                font-family: 'Segoe UI';
                font-size: 12pt;
            }
            QPushButton {
                background-color: #2c3e50;
                border-radius: 4px;
                padding: 6px 12px;
            }
            QPushButton:hover {
                background-color: #34495e;
            }
            QSlider::groove:horizontal {
                height: 6px;
                background: #2c3e50;
            }
            QSlider::handle:horizontal {
                width: 14px;
                background: #3498db;
                margin: -4px 0;
                border-radius: 6px;
            }
            QListWidget {
                background-color: #1c1c1c;
                border: none;
            }
            QTextEdit {
                background-color: #1a1a1a;
                border: 1px solid #333;
            }
            QGroupBox {
                border: 1px solid #333;
                margin-top: 12px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 3px;
            }
            """
        )

    def _connect_signals(self) -> None:
        self.nav_list.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.start_button.clicked.connect(self.start_detection)
        self.stop_button.clicked.connect(self.stop_detection)
        self.capture_button.clicked.connect(self.save_capture)
        self.confidence_slider.valueChanged.connect(self._update_confidence)
        self.overlap_slider.valueChanged.connect(self._update_overlap)
        self.only_player_checkbox.toggled.connect(self._update_show_only_player)
        self.pm_detection_checkbox.toggled.connect(self._update_pm_detection)
        self.pm_auto_send_checkbox.toggled.connect(self._update_pm_auto_send)
        self.padding_spin.valueChanged.connect(self._update_padding)
        self.api_key_edit.textChanged.connect(self._update_model_settings)
        self.workspace_edit.textChanged.connect(self._update_model_settings)
        self.project_edit.textChanged.connect(self._update_model_settings)
        self.version_spin.valueChanged.connect(self._update_model_settings)
        self.bot_token_edit.textChanged.connect(self._update_telegram_settings)
        self.chat_id_edit.textChanged.connect(self._update_telegram_settings)
        self.test_message_button.clicked.connect(self._send_test_message)
        self.test_model_button.clicked.connect(self._run_single_frame_test)
        self.clear_logs_button.clicked.connect(lambda: self.log_text.clear())

        # Shortcuts
        self.start_stop_shortcut = QShortcut(QKeySequence(Qt.Key_Space), self)
        self.start_stop_shortcut.activated.connect(self.toggle_detection)

        self.confidence_up = QShortcut(QKeySequence("C"), self)
        self.confidence_up.activated.connect(lambda: self._nudge_slider(self.confidence_slider, 5))
        self.confidence_down = QShortcut(QKeySequence("Shift+C"), self)
        self.confidence_down.activated.connect(lambda: self._nudge_slider(self.confidence_slider, -5))

        self.overlap_up = QShortcut(QKeySequence("O"), self)
        self.overlap_up.activated.connect(lambda: self._nudge_slider(self.overlap_slider, 5))
        self.overlap_down = QShortcut(QKeySequence("Shift+O"), self)
        self.overlap_down.activated.connect(lambda: self._nudge_slider(self.overlap_slider, -5))

    def _nudge_slider(self, slider: QSlider, delta: int) -> None:
        slider.setValue(max(slider.minimum(), min(slider.maximum(), slider.value() + delta)))

    def start_detection(self) -> None:
        if self.worker and self.worker.isRunning():
            self.log_panel.append("WARN", "Tespit zaten çalışıyor.")
            return
        self.worker = DetectionWorker(self.config.state)
        self.worker.frameReady.connect(self._update_preview)
        self.worker.statsUpdated.connect(self._update_stats)
        self.worker.logMessage.connect(self.log_panel.append)
        self.worker.detectionsUpdated.connect(self._handle_detections)
        self.worker.pmPreviewReady.connect(self._handle_pm_preview)
        self.worker.start()
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.log_panel.append("INFO", "Canlı tespit başlatıldı.")

    def stop_detection(self) -> None:
        if self.worker:
            self.worker.stop()
            self.worker.wait(2000)
            self.worker = None
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.log_panel.append("INFO", "Tespit durduruldu.")

    def toggle_detection(self) -> None:
        if self.worker and self.worker.isRunning():
            self.stop_detection()
        else:
            self.start_detection()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.stop_detection()
        self.config.save()
        super().closeEvent(event)

    def _update_preview(self, image, detections: list) -> None:
        pixmap = QPixmap.fromImage(image)
        self.preview_label.setPixmap(pixmap.scaled(self.preview_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _update_stats(self, fps: float, last_event: str) -> None:
        self.fps_label.setText(f"FPS: {fps:.1f}")
        self.last_event_label.setText(f"Son Olay: {last_event}")

    def _handle_detections(self, detections: List[Detection]) -> None:
        for det in detections:
            if det.label != PLAYER_LABEL:
                continue
            entry = f"{datetime.now().strftime('%H:%M:%S')} - Güven {det.confidence:.2f}"
            self.player_detections.append(entry)
        self.player_detections = self.player_detections[-10:]
        self.last_detections_list.clear()
        for item in reversed(self.player_detections):
            self.last_detections_list.addItem(QListWidgetItem(item))

    def _handle_pm_preview(self, image) -> None:
        pixmap = QPixmap.fromImage(image)
        self.pm_previews.append(pixmap)
        self.pm_previews = self.pm_previews[-3:]
        for index in range(3):
            label: QLabel = self.pm_preview_layout.itemAt(index).widget()  # type: ignore[assignment]
            if index < len(self.pm_previews):
                label.setPixmap(self.pm_previews[-(index + 1)].scaled(label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
            else:
                label.clear()

    def _update_confidence(self, value: int) -> None:
        self.config.state.roboflow.confidence = value / 100
        self.config.save()

    def _update_overlap(self, value: int) -> None:
        self.config.state.roboflow.overlap = value / 100
        self.config.save()

    def _update_show_only_player(self, checked: bool) -> None:
        self.config.state.detection.show_only_player = checked
        self.config.save()

    def _update_pm_detection(self, checked: bool) -> None:
        self.config.state.detection.enable_pm_box = checked
        self.config.save()

    def _update_pm_auto_send(self, checked: bool) -> None:
        self.config.state.telegram.auto_send = checked
        self.config.save()

    def _update_padding(self, value: int) -> None:
        self.config.state.telegram.padding = value
        self.config.save()

    def _update_model_settings(self) -> None:
        self.config.state.roboflow.api_key = self.api_key_edit.text()
        self.config.state.roboflow.workspace = self.workspace_edit.text()
        self.config.state.roboflow.project = self.project_edit.text()
        self.config.state.roboflow.version = self.version_spin.value()
        self.config.save()

    def _update_telegram_settings(self) -> None:
        self.config.state.telegram.bot_token = self.bot_token_edit.text()
        self.config.state.telegram.chat_id = self.chat_id_edit.text()
        self.config.save()

    def save_capture(self) -> None:
        pixmap = self.preview_label.pixmap()
        if pixmap is None:
            QMessageBox.warning(self, "Uyarı", "Kaydedilecek görüntü yok.")
            return
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        capture_dir = Path(self.config.state.detection.save_overlay_dir)
        capture_dir.mkdir(parents=True, exist_ok=True)
        filename = capture_dir / f"capture-{timestamp}.png"
        pixmap.save(str(filename), "PNG")
        self.log_panel.append("INFO", f"Ekran görüntüsü kaydedildi: {filename}")

    def _send_test_message(self) -> None:
        credentials = TelegramCredentials(
            bot_token=self.config.state.telegram.bot_token,
            chat_id=self.config.state.telegram.chat_id,
        )
        client = TelegramClient(credentials)
        try:
            client.send_message("Merlis Metin2 Bot test mesajı")
            self.log_panel.append("INFO", "Test mesajı gönderildi.")
        except Exception as exc:
            self.log_panel.append("ERROR", f"Telegram mesajı gönderilemedi: {exc}")

    def _run_single_frame_test(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Test için görüntü veya video seçiniz",
            "",
            "Video Dosyaları (*.mp4 *.avi *.mkv);;Görüntüler (*.png *.jpg *.jpeg);;Tümü (*.*)",
        )
        if not file_path:
            return
        suffix = Path(file_path).suffix.lower()
        if suffix in {".png", ".jpg", ".jpeg", ".bmp"}:
            image = cv2.imread(file_path)
            if image is None:
                self.log_panel.append("ERROR", "Görüntü okunamadı.")
                return
            self._predict_single_frame(image)
        else:
            self._run_test_video(file_path)

    def _predict_single_frame(self, frame: np.ndarray) -> None:
        detector = RoboflowDetector(
            self.config.state.roboflow.api_key,
            self.config.state.roboflow.workspace,
            self.config.state.roboflow.project,
            self.config.state.roboflow.version,
        )
        try:
            detections = detector.predict(
                frame,
                confidence=self.config.state.roboflow.confidence,
                overlap=self.config.state.roboflow.overlap,
                labels=[PLAYER_LABEL, PM_BOX_LABEL],
            )
        except Exception as exc:
            self.log_panel.append("ERROR", f"Test tahmini başarısız: {exc}")
            return
        annotated = draw_detections(frame, detections)
        qimage = convert_frame_to_qimage(annotated)
        self.preview_label.setPixmap(QPixmap.fromImage(qimage).scaled(self.preview_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.log_panel.append("INFO", f"Test tahmini tamamlandı. {len(detections)} tespit bulundu.")

    def _run_test_video(self, file_path: str) -> None:
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, "Uyarı", "Önce canlı tespiti durdurun.")
            return
        self.worker = DetectionWorker(self.config.state, video_source=file_path)
        self.worker.frameReady.connect(self._update_preview)
        self.worker.statsUpdated.connect(self._update_stats)
        self.worker.logMessage.connect(self.log_panel.append)
        self.worker.detectionsUpdated.connect(self._handle_detections)
        self.worker.pmPreviewReady.connect(self._handle_pm_preview)
        self.worker.start()
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.log_panel.append("INFO", "Test modu video oynatılıyor.")


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Merlis Metin2 Bot")

    config = ConfigManager()
    window = MainWindow(config)
    window.show()
    result = app.exec_()
    return result


if __name__ == "__main__":
    sys.exit(main())
