"""Window binding and DXCam capture helpers."""
from __future__ import annotations

import threading
from typing import List, Optional, Tuple

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


def _monitor_rects() -> List[Rect]:
    rects: List[Rect] = []
    if win32api is None:
        return rects

    def _cb(handle, hdc, rect, data):  # pragma: no cover - callback from Windows API
        try:
            rects.append((int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3])))
        except Exception:
            pass
        return True

    try:
        win32api.EnumDisplayMonitors(None, None, _cb, None)
    except Exception:
        return []
    return rects


def _resolve_output_region(region: Rect) -> Tuple[Optional[int], Rect, Optional[Rect], Optional[Rect]]:
    """Resolve dxcam region ensuring compatibility with multi-monitor layouts."""

    monitors = _monitor_rects()
    bounds = _virtual_bounds()
    monitor_idx: Optional[int] = None
    monitor_rect: Optional[Rect] = None
    if monitors:
        cx = int((region[0] + region[2]) / 2)
        cy = int((region[1] + region[3]) / 2)
        for idx, (ml, mt, mr, mb) in enumerate(monitors):
            if ml <= cx < mr and mt <= cy < mb:
                monitor_idx = idx
                monitor_rect = (ml, mt, mr, mb)
                break
    if monitor_rect is not None:
        ml, mt, mr, mb = monitor_rect
        rel = (
            int(region[0] - ml),
            int(region[1] - mt),
            int(region[2] - ml),
            int(region[3] - mt),
        )
        width = max(1, mr - ml)
        height = max(1, mb - mt)

        def _clamp(value: int, maximum: int) -> int:
            return max(0, min(maximum, value))

        left = _clamp(rel[0], width)
        top = _clamp(rel[1], height)
        right = _clamp(rel[2], width)
        bottom = _clamp(rel[3], height)
        if right <= left:
            right = min(width, left + max(1, region[2] - region[0]))
        if bottom <= top:
            bottom = min(height, top + max(1, region[3] - region[1]))

        # dxcam expects the region to stay strictly within the monitor bounds.
        right = min(width, max(left + 1, right))
        bottom = min(height, max(top + 1, bottom))

        capture_region = (left, top, right, bottom)
        if capture_region[2] > capture_region[0] and capture_region[3] > capture_region[1]:
            return monitor_idx, capture_region, None, monitor_rect

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
    needs_union = (region[0] < left or region[1] < top) or not inside
    if needs_union:
        capture_region = (0, 0, width, height)
        crop_rect = (
            max(0, min(width, x1)),
            max(0, min(height, y1)),
            max(0, min(width, x2)),
            max(0, min(height, y2)),
        )
    else:
        right = min(width, max(x1 + 1, x2))
        bottom = min(height, max(y1 + 1, y2))
        capture_region = (max(0, x1), max(0, y1), right, bottom)
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
                try:
                    self._cam.start(region=capture_region, target_fps=self.target_fps)
                except Exception as exc:
                    self._cam = None
                    raise RuntimeError(f"dxcam başlatılamadı: {exc}") from exc

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
