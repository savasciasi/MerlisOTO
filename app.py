import sys, time, traceback
from dataclasses import dataclass
import cv2
import numpy as np
import mss
from PyQt5 import QtCore, QtGui, QtWidgets

from config import load_config, save_config
from detector import YoloDetector
from telegram_client import TelegramClient
from windows_capture import get_window_rect_by_pid, find_pid_by_name


def bgr_to_qimage(bgr: np.ndarray) -> QtGui.QImage:
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    h, w, ch = rgb.shape
    return QtGui.QImage(rgb.data, w, h, ch * w, QtGui.QImage.Format_RGB888)

@dataclass
class AppState:
    running: bool = False
    fps: float = 0.0
    last_event: str = ""


class CaptureWorker(QtCore.QThread):
    frame_ready = QtCore.pyqtSignal(np.ndarray)
    event_info = QtCore.pyqtSignal(str)
    pm_found = QtCore.pyqtSignal(np.ndarray, tuple)  # (frame, bbox)

    def __init__(self, detector: YoloDetector, conf: int, ovl: int, monitor_index: int, roi=None, lock_pid: int = 0):
        super().__init__()
        self.detector = detector
        self.conf = conf
        self.ovl = ovl
        self.monitor_index = monitor_index
        self.roi = roi                 # (top,left,width,height) veya None
        self.lock_pid = int(lock_pid)
        self._stop = False

    def stop(self):
        self._stop = True

    def _region_from_pid_or_roi(self, mon):
        # PID varsa pencere rect’ini kullan
        if self.lock_pid > 0:
            rect = get_window_rect_by_pid(self.lock_pid)
            if rect:
                L, T, R, B = rect
                w = max(0, R - L); h = max(0, B - T)
                if w > 0 and h > 0:
                    return {"left": L, "top": T, "width": w, "height": h}
        # manuel ROI varsa onu kullan
        if self.roi and all(int(v) > 0 for v in self.roi):
            t, l, w_, h_ = self.roi
            return {"left": l, "top": t, "width": w_, "height": h_}
        # aksi halde tüm monitör
        return {"left": mon["left"], "top": mon["top"], "width": mon["width"], "height": mon["height"]}

    def run(self):
        try:
            with mss.mss() as sct:
                monitors = sct.monitors
                idx = min(max(1, self.monitor_index + 1), len(monitors) - 1)
                mon = monitors[idx]
                t0 = time.time()
                frames = 0
                while not self._stop:
                    region = self._region_from_pid_or_roi(mon)
                    raw = np.array(sct.grab(region))
                    bgr = cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)

                    preds = self.detector.predict_scaled(bgr, self.conf, self.ovl, max_w=1024)
                    drawn = self.detector.draw_predictions(bgr.copy(), preds)

                    for p in preds.get("predictions", []):
                        if self.detector.is_pm(p.get("class","")):
                            bbox = self.detector.bbox_xyxy_from_pred(p)
                            self.pm_found.emit(bgr, bbox)
                            break

                    self.frame_ready.emit(drawn)
                    frames += 1
                    if frames % 15 == 0:
                        dt = time.time() - t0
                        fps = frames / max(1e-6, dt)
                        self.event_info.emit(f"FPS: {fps:.1f}")
        except Exception as e:
            self.event_info.emit(f"Hata: {e}")
            traceback.print_exc()


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Merlis Metin2 Bot Kontrol Paneli")
        self.resize(1200, 760)
        self.state = AppState()
        self.cfg = load_config()
        self.detector = None
        self.tg = TelegramClient(self.cfg.get("telegram_token",""), self.cfg.get("telegram_chat_id",""))
        self.worker: CaptureWorker = None
        self._build_ui()

    def _build_ui(self):
        central = QtWidgets.QWidget(); self.setCentralWidget(central)
        layout = QtWidgets.QHBoxLayout(central)

        # Sol menü
        left = QtWidgets.QFrame(); left.setFixedWidth(220)
        left.setStyleSheet("QFrame{background:#111;color:#ddd;} QPushButton{color:#ddd;padding:10px;text-align:left;border:none;} QPushButton:hover{background:#1c1c1c;} QLabel{color:#9aa;}")
        v = QtWidgets.QVBoxLayout(left)
        self.btn_dashboard = QtWidgets.QPushButton("Dashboard")
        self.btn_player = QtWidgets.QPushButton("Oyuncu Tanıma")
        self.btn_pm = QtWidgets.QPushButton("PM Algılama")
        self.btn_model = QtWidgets.QPushButton("Model / API")
        self.btn_tg = QtWidgets.QPushButton("Telegram")
        self.btn_logs = QtWidgets.QPushButton("Loglar")
        for b in (self.btn_dashboard,self.btn_player,self.btn_pm,self.btn_model,self.btn_tg,self.btn_logs):
            v.addWidget(b)
        v.addStretch(); layout.addWidget(left)

        # Sayfalar
        self.stack = QtWidgets.QStackedWidget(); layout.addWidget(self.stack, 1)

        # Dashboard
        page_d = QtWidgets.QWidget(); dlay = QtWidgets.QVBoxLayout(page_d)
        self.preview = QtWidgets.QLabel(alignment=QtCore.Qt.AlignCenter)
        self.preview.setMinimumHeight(450); self.preview.setStyleSheet("background:#000;border-radius:12px;")
        ctrl = QtWidgets.QHBoxLayout()
        self.btn_start = QtWidgets.QPushButton("Başlat"); self.btn_start.setStyleSheet("background:#5c8;padding:10px;")
        self.btn_stop  = QtWidgets.QPushButton("Durdur"); self.btn_stop.setStyleSheet("background:#c66;padding:10px;")
        ctrl.addWidget(self.btn_start); ctrl.addWidget(self.btn_stop)
        self.status = QtWidgets.QLabel("Hazır.")
        dlay.addWidget(self.preview); dlay.addLayout(ctrl); dlay.addWidget(self.status)
        self.stack.addWidget(page_d)

        # Oyuncu Tanıma
        page_p = QtWidgets.QWidget(); play = QtWidgets.QFormLayout(page_p)
        self.s_conf = QtWidgets.QSlider(QtCore.Qt.Horizontal); self.s_conf.setRange(0,100); self.s_conf.setValue(int(self.cfg["confidence"]))
        self.s_ovl  = QtWidgets.QSlider(QtCore.Qt.Horizontal); self.s_ovl.setRange(0,100); self.s_ovl.setValue(int(self.cfg["overlap"]))
        self.lb_conf = QtWidgets.QLabel(f"%{self.s_conf.value()}"); self.lb_ovl = QtWidgets.QLabel(f"%{self.s_ovl.value()}")
        def upd(): self.lb_conf.setText(f"%{self.s_conf.value()}"); self.lb_ovl.setText(f"%{self.s_ovl.value()}")
        self.s_conf.valueChanged.connect(upd); self.s_ovl.valueChanged.connect(upd)
        h1=QtWidgets.QHBoxLayout(); h1.addWidget(self.s_conf); h1.addWidget(self.lb_conf)
        h2=QtWidgets.QHBoxLayout(); h2.addWidget(self.s_ovl);  h2.addWidget(self.lb_ovl)
        play.addRow("Confidence", h1); play.addRow("Overlap", h2)
        self.stack.addWidget(page_p)

        # PM Algılama
        page_pm = QtWidgets.QWidget(); pmlay = QtWidgets.QFormLayout(page_pm)
        self.cb_auto_send = QtWidgets.QCheckBox("PM tespit edilince Telegram’a otomatik gönder"); self.cb_auto_send.setChecked(bool(self.cfg["auto_send_pm"]))
        self.spin_pad = QtWidgets.QSpinBox(); self.spin_pad.setRange(0,64); self.spin_pad.setValue(int(self.cfg["crop_padding"]))
        pmlay.addRow(self.cb_auto_send); pmlay.addRow("Kırpma Padding (px):", self.spin_pad)
        self.stack.addWidget(page_pm)

        # Model / API
        page_m = QtWidgets.QWidget(); mlay = QtWidgets.QFormLayout(page_m)
        self.ed_api  = QtWidgets.QLineEdit(self.cfg["api_key"]); self.ed_api.setEchoMode(QtWidgets.QLineEdit.Password)
        self.ed_ws   = QtWidgets.QLineEdit(self.cfg["workspace"])
        self.ed_proj = QtWidgets.QLineEdit(self.cfg["project"])
        self.spin_ver = QtWidgets.QSpinBox(); self.spin_ver.setRange(1,99); self.spin_ver.setValue(int(self.cfg["version"]))
        self.spin_mon = QtWidgets.QSpinBox(); self.spin_mon.setRange(0,8); self.spin_mon.setValue(int(self.cfg["monitor_index"]))
        # ROI
        self.spin_top = QtWidgets.QSpinBox();  self.spin_top.setRange(0,4000); self.spin_top.setValue(int(self.cfg["roi_top"]))
        self.spin_left= QtWidgets.QSpinBox();  self.spin_left.setRange(0,4000); self.spin_left.setValue(int(self.cfg["roi_left"]))
        self.spin_w   = QtWidgets.QSpinBox();  self.spin_w.setRange(0,10000);  self.spin_w.setValue(int(self.cfg["roi_width"]))
        self.spin_h   = QtWidgets.QSpinBox();  self.spin_h.setRange(0,10000);  self.spin_h.setValue(int(self.cfg["roi_height"]))
        # PID
        self.spin_pid = QtWidgets.QSpinBox(); self.spin_pid.setRange(0, 10_000_000); self.spin_pid.setValue(int(self.cfg["lock_pid"]))
        self.ed_hint  = QtWidgets.QLineEdit(self.cfg.get("process_hint","merlis"))
        self.btn_find_pid = QtWidgets.QPushButton("Merlis’i Bul (PID)")

        self.btn_init = QtWidgets.QPushButton("Modeli Başlat/Test Et")
        mlay.addRow("API Key:", self.ed_api)
        mlay.addRow("Workspace:", self.ed_ws)
        mlay.addRow("Project:", self.ed_proj)
        mlay.addRow("Version:", self.spin_ver)
        mlay.addRow("Monitor Index:", self.spin_mon)
        mlay.addRow("ROI Top:", self.spin_top)
        mlay.addRow("ROI Left:", self.spin_left)
        mlay.addRow("ROI Width:", self.spin_w)
        mlay.addRow("ROI Height:", self.spin_h)
        mlay.addRow("PID’e kilitle (0=Kapalı):", self.spin_pid)
        mlay.addRow("Süreç adı ipucu:", self.ed_hint)
        mlay.addRow(self.btn_find_pid)
        mlay.addRow(self.btn_init)
        self.stack.addWidget(page_m)

        # Telegram
        page_tg = QtWidgets.QWidget(); tglay = QtWidgets.QFormLayout(page_tg)
        self.ed_tg_token = QtWidgets.QLineEdit(self.cfg["telegram_token"]); self.ed_tg_token.setEchoMode(QtWidgets.QLineEdit.Password)
        self.ed_tg_chat  = QtWidgets.QLineEdit(self.cfg["telegram_chat_id"])
        self.btn_tg_test = QtWidgets.QPushButton("Test Mesajı Gönder")
        tglay.addRow("Bot Token:", self.ed_tg_token)
        tglay.addRow("Chat ID:", self.ed_tg_chat)
        tglay.addRow(self.btn_tg_test)
        self.stack.addWidget(page_tg)

        # Loglar
        page_l = QtWidgets.QWidget(); ll = QtWidgets.QVBoxLayout(page_l)
        self.logs = QtWidgets.QTextEdit(); self.logs.setReadOnly(True)
        self.btn_clear_log = QtWidgets.QPushButton("Log’u Temizle")
        ll.addWidget(self.logs); ll.addWidget(self.btn_clear_log)
        self.stack.addWidget(page_l)

        # gezinme
        self.btn_dashboard.clicked.connect(lambda: self.stack.setCurrentIndex(0))
        self.btn_player.clicked.connect(lambda: self.stack.setCurrentIndex(1))
        self.btn_pm.clicked.connect(lambda: self.stack.setCurrentIndex(2))
        self.btn_model.clicked.connect(lambda: self.stack.setCurrentIndex(3))
        self.btn_tg.clicked.connect(lambda: self.stack.setCurrentIndex(4))
        self.btn_logs.clicked.connect(lambda: self.stack.setCurrentIndex(5))

        # aksiyonlar
        self.btn_init.clicked.connect(self.init_model)
        self.btn_start.clicked.connect(self.start_capture)
        self.btn_stop.clicked.connect(self.stop_capture)
        self.btn_tg_test.clicked.connect(self.test_telegram)
        self.btn_clear_log.clicked.connect(lambda: self.logs.clear())
        self.btn_find_pid.clicked.connect(self.find_merlis_pid)

        # kısayollar
        QtWidgets.QShortcut(QtGui.QKeySequence("Space"), self, activated=self.toggle_run)
        QtWidgets.QShortcut(QtGui.QKeySequence("C"), self, activated=lambda: self.bump_slider(self.s_conf))
        QtWidgets.QShortcut(QtGui.QKeySequence("O"), self, activated=lambda: self.bump_slider(self.s_ovl))

    def bump_slider(self, sld: QtWidgets.QSlider, step=5):
        sld.setValue(int(np.clip(sld.value()+step, 0, 100)))

    # ---- helpers ----
    def log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.logs.append(f"[{ts}] {msg}")
        self.status.setText(msg)

    def find_merlis_pid(self):
        hint = self.ed_hint.text().strip() or "merlis"
        pid = find_pid_by_name(hint)
        if pid:
            self.spin_pid.setValue(int(pid))
            self.log(f"Bulundu: PID={pid}")
        else:
            self.log("Merlis süreci bulunamadı. Oyun açık mı?")

    # ---- model & run ----
    def init_model(self):
        self.cfg.update({
            "api_key": self.ed_api.text().strip(),
            "workspace": self.ed_ws.text().strip(),
            "project": self.ed_proj.text().strip(),
            "version": int(self.spin_ver.value()),
            "monitor_index": int(self.spin_mon.value()),
        })
        save_config(self.cfg)
        try:
            self.detector = YoloDetector(self.cfg["api_key"], self.cfg["workspace"], self.cfg["project"], self.cfg["version"])
            self.log("Model hazır.")
            _ = self.detector.predict_scaled(np.zeros((320,320,3), np.uint8), self.cfg["confidence"], self.cfg["overlap"])
            self.log("Model bağlantısı OK.")
        except Exception as e:
            self.log(f"Model başlatılamadı: {e}")

    def ensure_detector(self) -> bool:
        if self.detector is None:
            self.init_model()
        return self.detector is not None

    def start_capture(self):
        if self.state.running:
            return
        if not self.ensure_detector():
            return
        # cfg güncelle
        self.cfg["confidence"] = int(self.s_conf.value())
        self.cfg["overlap"] = int(self.s_ovl.value())
        self.cfg["auto_send_pm"] = bool(self.cb_auto_send.isChecked())
        self.cfg["crop_padding"] = int(self.spin_pad.value())
        self.cfg["telegram_token"] = self.ed_tg_token.text().strip()
        self.cfg["telegram_chat_id"] = self.ed_tg_chat.text().strip()
        self.cfg["roi_top"] = int(self.spin_top.value())
        self.cfg["roi_left"] = int(self.spin_left.value())
        self.cfg["roi_width"] = int(self.spin_w.value())
        self.cfg["roi_height"] = int(self.spin_h.value())
        self.cfg["lock_pid"] = int(self.spin_pid.value())
        self.cfg["process_hint"] = self.ed_hint.text().strip() or "merlis"
        save_config(self.cfg)
        self.tg = TelegramClient(self.cfg["telegram_token"], self.cfg["telegram_chat_id"])

        roi = (self.cfg["roi_top"], self.cfg["roi_left"], self.cfg["roi_width"], self.cfg["roi_height"])
        self.worker = CaptureWorker(
            self.detector, self.cfg["confidence"], self.cfg["overlap"],
            self.cfg["monitor_index"], roi=roi, lock_pid=self.cfg["lock_pid"]
        )
        self.worker.frame_ready.connect(self.on_frame)
        self.worker.event_info.connect(self.log)
        self.worker.pm_found.connect(self.on_pm_found)
        self.worker.start()
        self.state.running = True
        self.log("Yakalama başlatıldı.")

    def stop_capture(self):
        if self.worker:
            self.worker.stop()
            self.worker.wait(2000)
        self.state.running = False
        self.log("Durduruldu.")

    def toggle_run(self):
        if self.state.running:
            self.stop_capture()
        else:
            self.start_capture()

    @QtCore.pyqtSlot(np.ndarray)
    def on_frame(self, bgr):
        qimg = bgr_to_qimage(bgr)
        self.preview.setPixmap(QtGui.QPixmap.fromImage(qimg))

    @QtCore.pyqtSlot(np.ndarray, tuple)
    def on_pm_found(self, bgr, bbox):
        if self.cfg.get("auto_send_pm") and self.tg.ready():
            try:
                self.tg.send_crop(bgr, bbox, padding=int(self.cfg["crop_padding"]), caption="PM Box tespit edildi")
                self.log("PM crop Telegram’a gönderildi.")
            except Exception as e:
                self.log(f"Telegram gönderim hatası: {e}")

    def test_telegram(self):
        self.cfg["telegram_token"] = self.ed_tg_token.text().strip()
        self.cfg["telegram_chat_id"] = self.ed_tg_chat.text().strip()
        save_config(self.cfg)
        self.tg = TelegramClient(self.cfg["telegram_token"], self.cfg["telegram_chat_id"])
        if not self.tg.ready():
            self.log("Telegram bilgileri eksik.")
            return
        try:
            self.tg.send_text("Merlis Bot testi: bağlandı ✅")
            self.log("Telegram test mesajı gönderildi.")
        except Exception as e:
            self.log(f"Telegram testi hatası: {e}")


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    pal = app.palette()
    pal.setColor(QtGui.QPalette.Window, QtGui.QColor(20,20,20))
    pal.setColor(QtGui.QPalette.WindowText, QtCore.Qt.white)
    pal.setColor(QtGui.QPalette.Base, QtGui.QColor(25,25,25))
    pal.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor(35,35,35))
    pal.setColor(QtGui.QPalette.Text, QtCore.Qt.white)
    pal.setColor(QtGui.QPalette.Button, QtGui.QColor(45,45,45))
    pal.setColor(QtGui.QPalette.ButtonText, QtCore.Qt.white)
    app.setPalette(pal)

    w = MainWindow()
    w.show()
    sys.exit(app.exec_())
