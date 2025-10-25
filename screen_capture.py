"""Window binding and capture helpers for DXCam and MSS backends."""
from __future__ import annotations

import threading
from typing import List, Optional, Protocol, Tuple

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

try:
    import mss
except Exception:  # pragma: no cover - optional dependency at runtime
    mss = None  # type: ignore


Rect = Tuple[int, int, int, int]


class CaptureBackend(Protocol):
    """Common interface for capture backends."""

    def start(self) -> None:
        ...

    def get_latest_frame(self) -> Optional[np.ndarray]:
        ...

    def stop(self) -> None:
        ...


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

    def _clamp_axis(start: int, end: int, maximum: int) -> Tuple[int, int]:
        if maximum <= 1:
            return 0, 1
        left = max(0, min(maximum - 1, start))
        right = max(left + 1, min(maximum, end))
        return left, right

    monitor_idx: Optional[int] = None
    monitor_rect: Optional[Rect] = None
    if monitors:
        best_overlap = 0
        for idx, (ml, mt, mr, mb) in enumerate(monitors):
            overlap_left = max(region[0], ml)
            overlap_top = max(region[1], mt)
            overlap_right = min(region[2], mr)
            overlap_bottom = min(region[3], mb)
            overlap_w = max(0, overlap_right - overlap_left)
            overlap_h = max(0, overlap_bottom - overlap_top)
            overlap = overlap_w * overlap_h
            if overlap > best_overlap:
                best_overlap = overlap
                monitor_idx = idx
                monitor_rect = (ml, mt, mr, mb)
    if monitor_rect is not None:
        ml, mt, mr, mb = monitor_rect
        width = max(1, mr - ml)
        height = max(1, mb - mt)
        rel_left = int(region[0] - ml)
        rel_top = int(region[1] - mt)
        rel_right = int(region[2] - ml)
        rel_bottom = int(region[3] - mt)
        left, right = _clamp_axis(rel_left, rel_right, width)
        top, bottom = _clamp_axis(rel_top, rel_bottom, height)
        capture_region = (left, top, right, bottom)
        if capture_region[2] > capture_region[0] and capture_region[3] > capture_region[1]:
            return monitor_idx, capture_region, None, monitor_rect

    if bounds is None:
        return None, region, None, None

    left, top, right, bottom = bounds
    width = max(1, right - left)
    height = max(1, bottom - top)
    rel_left = int(region[0] - left)
    rel_top = int(region[1] - top)
    rel_right = int(region[2] - left)
    rel_bottom = int(region[3] - top)
    cap_left, cap_right = _clamp_axis(rel_left, rel_right, width)
    cap_top, cap_bottom = _clamp_axis(rel_top, rel_bottom, height)
    capture_region = (cap_left, cap_top, cap_right, cap_bottom)
    return None, capture_region, None, bounds


def _clamp_region_to_rect(region: Rect, clamp: Optional[Rect]) -> Rect:
    if clamp is None:
        return region
    left = max(clamp[0], region[0])
    top = max(clamp[1], region[1])
    right = min(clamp[2], region[2])
    bottom = min(clamp[3], region[3])
    if right <= left or bottom <= top:
        raise ValueError("Geçersiz bölge: genişlik/yükseklik sıfırdan büyük olmalı.")
    return int(left), int(top), int(right), int(bottom)


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


class MSSCapture:
    """MSS-based capture backend for monitors where DXCam is unstable."""

    def __init__(self, region: Rect) -> None:
        self.region = region
        self._sct: Optional["mss.mss"] = None
        self._lock = threading.Lock()
        self._mon: Optional[dict] = None

    def start(self) -> None:
        if mss is None:
            raise RuntimeError("mss paketi bulunamadı. requirements.txt kurulumunu tamamlayın.")
        with self._lock:
            if self._sct is None:
                left, top, right, bottom = self.region
                width = max(1, int(right - left))
                height = max(1, int(bottom - top))
                self._mon = {"left": int(left), "top": int(top), "width": width, "height": height}
                self._sct = mss.mss()

    def get_latest_frame(self) -> Optional[np.ndarray]:
        with self._lock:
            if self._sct is None or self._mon is None:
                return None
            frame = np.array(self._sct.grab(self._mon))
            if frame.ndim == 3 and frame.shape[2] >= 3:
                frame = frame[:, :, :3]
            return frame

    def stop(self) -> None:
        with self._lock:
            if self._sct is not None:
                self._sct.close()
                self._sct = None
                self._mon = None


def select_capture_backend(
    region: Rect,
    prefer_dx: bool = True,
    target_fps: int = 60,
) -> Tuple[str, CaptureBackend, Optional[int]]:
    """Choose the optimal capture backend for the requested region."""

    monitor_idx, _, _, monitor_rect = _resolve_output_region(region)
    monitors = _monitor_rects()
    use_mss = False
    if monitor_rect is not None and monitors:
        ordered = sorted(range(len(monitors)), key=lambda idx: (monitors[idx][0], idx))
        if ordered:
            leftmost = ordered[0]
            if monitor_idx == leftmost:
                use_mss = True
    if monitor_rect is None and region[0] < 0:
        use_mss = True
    if use_mss:
        clamped_region = _clamp_region_to_rect(region, monitor_rect)
        if mss is None:
            raise RuntimeError(
                "Monitor için MSS gereklidir ancak mss paketi bulunamadı. 'pip install mss' komutunu çalıştırın."
            )
        return "MSS", MSSCapture(clamped_region), monitor_idx
    capture = DXCapture(region=region, prefer_dx=prefer_dx, target_fps=target_fps)
    return "DXCam", capture, monitor_idx
