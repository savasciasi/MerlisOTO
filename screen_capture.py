"""Window binding and DXCam capture helpers."""
from __future__ import annotations

import threading
from typing import Optional, Tuple

import numpy as np
import psutil
import win32gui
import win32process

try:
    import win32api
    import win32con
except Exception:  # pragma: no cover - optional at runtime
    win32api = None  # type: ignore
    win32con = None  # type: ignore

try:
    import dxcam
except Exception:  # pragma: no cover - optional dependency at runtime
    dxcam = None  # type: ignore


Rect = Tuple[int, int, int, int]


def _virtual_bounds() -> Optional[Rect]:
    if win32api is None or win32con is None:
        return None
    try:
        left = int(win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN))
        top = int(win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN))
        width = int(win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN))
        height = int(win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN))
    except Exception:
        return None
    return left, top, left + width, top + height


def _resolve_output_region(region: Rect) -> Tuple[Optional[int], Rect, Optional[Rect], Optional[Rect]]:
    """Resolve dxcam region ensuring compatibility with multi-monitor layouts."""

    bounds = _virtual_bounds()
    if bounds is None:
        return None, region, None, None
    left, top, right, bottom = bounds
    shift_x = -left
    shift_y = -top
    x1 = int(region[0] + shift_x)
    y1 = int(region[1] + shift_y)
    x2 = int(region[2] + shift_x)
    y2 = int(region[3] + shift_y)
    width = max(1, right - left)
    height = max(1, bottom - top)
    inside = 0 <= x1 < width and 0 <= y1 < height and 0 < x2 <= width and 0 < y2 <= height
    needs_union = (region[0] < 0 or region[1] < 0) or not inside
    if needs_union:
        capture_region: Rect = (0, 0, width, height)
        crop_rect = (
            max(0, min(width, x1)),
            max(0, min(height, y1)),
            max(0, min(width, x2)),
            max(0, min(height, y2)),
        )
    else:
        capture_region = (x1, y1, x2, y2)
        crop_rect = None
    return None, capture_region, crop_rect, bounds


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
    info = get_window_info_by_pid(pid)
    return info[1] if info else None


def get_window_info_by_pid(pid: int) -> Optional[Tuple[int, Rect]]:
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
    hwnd = hwnds[0]
    rect = win32gui.GetWindowRect(hwnd)
    return hwnd, rect


class DXCapture:
    """dxcam wrapper with safe shutdown."""

    def __init__(self, region: Rect, prefer_dx: bool = True, target_fps: int = 60) -> None:
        self.region = region
        self.prefer_dx = prefer_dx
        self.target_fps = target_fps
        self._cam = None
        self._lock = threading.Lock()
        self._output_idx: Optional[int] = None
        self._crop_rect: Optional[Rect] = None
        self._monitor_bounds: Optional[Rect] = None

    def start(self) -> None:
        if dxcam is None:
            raise RuntimeError("dxcam bulunamadı. requirements.txt kurulumunu tamamlayın.")
        with self._lock:
            if self._cam is None:
                capture_region = self.region
                if self.prefer_dx:
                    self._output_idx, capture_region, self._crop_rect, self._monitor_bounds = _resolve_output_region(
                        self.region
                    )
                create_kwargs = {"output_color": "BGR", "max_buffer_len": 2}
                if self._output_idx is not None:
                    create_kwargs["output_idx"] = self._output_idx
                self._cam = dxcam.create(**create_kwargs)
                self._cam.start(region=capture_region, target_fps=self.target_fps)

    def get_latest_frame(self) -> Optional[np.ndarray]:
        with self._lock:
            if self._cam is None:
                return None
            frame = self._cam.get_latest_frame()
            if frame is None:
                return None
            crop = self._crop_rect
            if crop is not None:
                x1, y1, x2, y2 = crop
                h, w = frame.shape[:2]
                x1c = max(0, min(w, x1))
                y1c = max(0, min(h, y1))
                x2c = max(0, min(w, x2))
                y2c = max(0, min(h, y2))
                if x2c <= x1c or y2c <= y1c:
                    return frame
                frame = frame[y1c:y2c, x1c:x2c]
            return frame

    def stop(self) -> None:
        with self._lock:
            if self._cam is not None:
                self._cam.stop()
                self._cam = None
