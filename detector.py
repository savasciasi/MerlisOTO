"""Roboflow detection utilities for the Merlis Metin2 bot."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional, Sequence

import cv2
import numpy as np

try:
    from roboflow import Roboflow
except Exception:  # pragma: no cover - roboflow optional during development
    Roboflow = None  # type: ignore


PLAYER_LABEL = "player"
PM_BOX_LABEL = "pm-box"


@dataclass
class Detection:
    label: str
    confidence: float
    x: int
    y: int
    width: int
    height: int

    def to_rect(self) -> tuple[int, int, int, int]:
        half_w = self.width // 2
        half_h = self.height // 2
        x1 = max(int(self.x - half_w), 0)
        y1 = max(int(self.y - half_h), 0)
        x2 = int(self.x + half_w)
        y2 = int(self.y + half_h)
        return x1, y1, x2, y2


class RoboflowDetector:
    """Lazy Roboflow model loader and predictor."""

    def __init__(
        self,
        api_key: str,
        workspace: str,
        project: str,
        version: int,
    ) -> None:
        self.api_key = api_key
        self.workspace = workspace
        self.project = project
        self.version = version
        self._model = None
        self._last_load_error: Optional[str] = None

    def ensure_model(self) -> None:
        if self._model is not None:
            return
        if Roboflow is None:
            raise RuntimeError("roboflow paketi yüklenemedi. Lütfen kurulu olduğundan emin olun.")
        api_key = (self.api_key or "").strip()
        workspace = (self.workspace or "").strip()
        project_name = (self.project or "").strip()
        if not workspace or not project_name:
            raise RuntimeError(
                "Roboflow workspace ve proje bilgileri boş olamaz. Lütfen ayarları güncelleyin."
            )
        try:
            rf = Roboflow(api_key=api_key)
            workspace_ref = rf.workspace(workspace)
            project = workspace_ref.project(project_name)
            self._model = project.version(self.version).model
            self._last_load_error = None
        except Exception as exc:  # pragma: no cover - depends on external API
            self._model = None
            message = str(exc)
            if "missing permissions" in message or "Unsupported request" in message:
                friendly = (
                    "Roboflow kimlik bilgileri doğrulanamadı. API anahtarını ve "
                    "workspace/proje ayarlarını kontrol edin."
                )
            else:
                friendly = "Roboflow modeli yüklenemedi."
            self._last_load_error = friendly
            raise RuntimeError(f"{friendly} (Detay: {message})") from exc

    def predict(
        self,
        frame: np.ndarray,
        confidence: float,
        overlap: float,
        labels: Optional[Sequence[str]] = None,
    ) -> List[Detection]:
        self.ensure_model()
        if self._model is None:
            return []
        prediction = self._model.predict(frame, confidence=confidence, overlap=overlap).json()
        predictions = prediction.get("predictions", [])
        detections: List[Detection] = []
        for item in predictions:
            label = item.get("class", "")
            if labels and label not in labels:
                continue
            detections.append(
                Detection(
                    label=label,
                    confidence=float(item.get("confidence", 0.0)),
                    x=int(item.get("x", 0)),
                    y=int(item.get("y", 0)),
                    width=int(item.get("width", 0)),
                    height=int(item.get("height", 0)),
                )
            )
        return detections


def draw_detections(frame: np.ndarray, detections: Sequence[Detection]) -> np.ndarray:
    """Draw bounding boxes with labels on frame."""
    output = frame.copy()
    for det in detections:
        x1, y1, x2, y2 = det.to_rect()
        color = (0, 200, 0) if det.label == PLAYER_LABEL else (255, 140, 0)
        cv2.rectangle(output, (x1, y1), (x2, y2), color, 2)
        label = f"{det.label} {det.confidence:.2f}"
        (tw, th), baseline = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.rectangle(output, (x1, y1 - th - baseline), (x1 + tw, y1), color, -1)
        cv2.putText(
            output,
            label,
            (x1, y1 - baseline),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (0, 0, 0),
            1,
            cv2.LINE_AA,
        )
    return output


def compute_fps(samples: Sequence[float]) -> float:
    if not samples:
        return 0.0
    return len(samples) / (samples[-1] - samples[0]) if len(samples) > 1 else 0.0


def crop_with_padding(frame: np.ndarray, detection: Detection, padding: int = 0) -> np.ndarray:
    x1, y1, x2, y2 = det_rect = detection.to_rect()
    height, width = frame.shape[:2]
    x1 = max(x1 - padding, 0)
    y1 = max(y1 - padding, 0)
    x2 = min(x2 + padding, width)
    y2 = min(y2 + padding, height)
    return frame[y1:y2, x1:x2].copy()


def convert_frame_to_qimage(frame: np.ndarray):
    """Convert BGR frame to QImage."""
    from PyQt5.QtGui import QImage

    rgb_image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    height, width, channel = rgb_image.shape
    bytes_per_line = channel * width
    return QImage(rgb_image.data, width, height, bytes_per_line, QImage.Format_RGB888).copy()


class SlidingWindowFPS:
    """Utility to keep FPS history with sliding window."""

    def __init__(self, max_samples: int = 30) -> None:
        self.max_samples = max_samples
        self.samples: List[float] = []

    def update(self) -> float:
        now = time.time()
        self.samples.append(now)
        if len(self.samples) > self.max_samples:
            self.samples.pop(0)
        return compute_fps(self.samples)
