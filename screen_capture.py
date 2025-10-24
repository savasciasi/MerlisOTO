"""Window binding and DXCam capture helpers."""
from __future__ import annotations

import threading
from typing import Optional, Tuple

import numpy as np
import psutil
import win32gui
import win32process

try:
    import dxcam
except Exception:  # pragma: no cover - optional dependency at runtime
    dxcam = None  # type: ignore


Rect = Tuple[int, int, int, int]


def find_pid_by_name(substr: str) -> Optional[int]:
    substr = (substr or "").lower()
    if not substr:
        return None
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            cmd = " ".join(proc.info.get("cmdline") or []).lower()
            if substr in name or substr in cmd:
                return int(proc.info["pid"])
        except Exception:
            continue
    return None


def get_window_rect_by_pid(pid: int) -> Optional[Rect]:
    hwnds: list[int] = []

    def _callback(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and not win32gui.IsIconic(hwnd):
            _, proc_pid = win32process.GetWindowThreadProcessId(hwnd)
            if proc_pid == pid and win32gui.GetWindowText(hwnd):
                hwnds.append(hwnd)
        return True

    win32gui.EnumWindows(_callback, None)
    if not hwnds:
        return None
    return win32gui.GetWindowRect(hwnds[0])


class DXCapture:
    """dxcam wrapper with safe shutdown."""

    def __init__(self, region: Rect, prefer_dx: bool = True, target_fps: int = 60) -> None:
        self.region = region
        self.prefer_dx = prefer_dx
        self.target_fps = target_fps
        self._cam = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if dxcam is None:
            raise RuntimeError("dxcam bulunamadı. requirements.txt kurulumunu tamamlayın.")
        with self._lock:
            if self._cam is None:
                self._cam = dxcam.create(output_color="BGR", max_buffer_len=2)
                self._cam.start(region=self.region, target_fps=self.target_fps)

    def get_latest_frame(self) -> Optional[np.ndarray]:
        with self._lock:
            if self._cam is None:
                return None
            return self._cam.get_latest_frame()

    def stop(self) -> None:
        with self._lock:
            if self._cam is not None:
                self._cam.stop()
                self._cam = None
