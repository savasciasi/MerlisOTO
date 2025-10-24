"""Tesseract OCR utilities for the Merlis Metin2 bot."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional, Sequence

import cv2
import numpy as np

try:
    import pytesseract
    from pytesseract import Output, TesseractNotFoundError
except Exception:  # pragma: no cover - pytesseract optional during development
    pytesseract = None  # type: ignore
    Output = None  # type: ignore
    TesseractNotFoundError = RuntimeError  # type: ignore


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
    text: str

    def to_rect(self) -> tuple[int, int, int, int]:
        half_w = self.width // 2
        half_h = self.height // 2
        x1 = max(int(self.x - half_w), 0)
        y1 = max(int(self.y - half_h), 0)
        x2 = int(self.x + half_w)
        y2 = int(self.y + half_h)
        return x1, y1, x2, y2


class TesseractDetector:
    """Wrapper around pytesseract OCR calls."""

    def __init__(
        self,
        language: str,
        oem: int,
        psm: int,
        custom_config: str = "",
    ) -> None:
        self.configure(language, oem, psm, custom_config)

    def configure(self, language: str, oem: int, psm: int, custom_config: str = "") -> None:
        self.language = (language or "eng").strip()
        self.oem = max(0, oem)
        self.psm = max(0, psm)
        self.custom_config = custom_config.strip()

    def predict(
        self,
        frame: np.ndarray,
        confidence: float,
        min_text_length: int,
        player_keywords: Optional[Sequence[str]] = None,
        pm_keywords: Optional[Sequence[str]] = None,
        include_pm: bool = True,
    ) -> List[Detection]:
        if pytesseract is None:
            raise RuntimeError("pytesseract paketi yüklenemedi. Lütfen kurulumunu tamamlayın.")
        try:
            config = f"--oem {self.oem} --psm {self.psm}"
            if self.custom_config:
                config = f"{config} {self.custom_config}"
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            data = pytesseract.image_to_data(
                rgb,
                lang=self.language or "eng",
                config=config,
                output_type=Output.DICT,
            )
        except TesseractNotFoundError as exc:  # pragma: no cover - depends on system setup
            raise RuntimeError(
                "Tesseract yürütülebilir dosyası bulunamadı. Lütfen sisteminize kurun ve PATH içerisine ekleyin."
            ) from exc
        except Exception as exc:  # pragma: no cover - passthrough external errors
            raise RuntimeError(f"OCR çalıştırılamadı: {exc}") from exc

        min_conf = max(0.0, min(confidence, 1.0)) * 100.0
        min_length = max(1, min_text_length)
        player_terms = _normalise_terms(player_keywords)
        pm_terms = _normalise_terms(pm_keywords)

        detections: List[Detection] = []
        texts = data.get("text", [])
        confs = data.get("conf", [])
        lefts = data.get("left", [])
        tops = data.get("top", [])
        widths = data.get("width", [])
        heights = data.get("height", [])
        for i in range(len(texts)):
            text = (texts[i] or "").strip()
            if not text:
                continue
            try:
                conf_raw = confs[i]
            except Exception:
                conf_raw = "0"
            try:
                conf_val = float(conf_raw)
            except Exception:
                conf_val = 0.0
            if conf_val < min_conf:
                continue
            if len(text) < min_length:
                continue
            try:
                left = int(lefts[i])
                top = int(tops[i])
                width = int(widths[i])
                height = int(heights[i])
            except Exception:
                continue
            if width <= 0 or height <= 0:
                continue
            label = _classify_text(text, player_terms, pm_terms, include_pm)
            if label is None:
                continue
            detections.append(
                Detection(
                    label=label,
                    confidence=conf_val / 100.0,
                    x=left + width // 2,
                    y=top + height // 2,
                    width=width,
                    height=height,
                    text=text,
                )
            )
        return detections


def _normalise_terms(terms: Optional[Sequence[str]]) -> List[str]:
    if not terms:
        return []
    return [term.lower().strip() for term in terms if term and term.strip()]


def _classify_text(
    text: str,
    player_terms: Sequence[str],
    pm_terms: Sequence[str],
    include_pm: bool,
) -> Optional[str]:
    lowered = text.lower()
    if include_pm and pm_terms:
        for term in pm_terms:
            if term and term in lowered:
                return PM_BOX_LABEL
    if player_terms:
        for term in player_terms:
            if term and term in lowered:
                return PLAYER_LABEL
        return None
    # if no player terms provided, treat every text as player (unless PM matched above)
    return PLAYER_LABEL


def draw_detections(frame: np.ndarray, detections: Sequence[Detection]) -> np.ndarray:
    """Draw bounding boxes with labels on frame."""
    output = frame.copy()
    for det in detections:
        x1, y1, x2, y2 = det.to_rect()
        color = (0, 200, 0) if det.label == PLAYER_LABEL else (255, 140, 0)
        cv2.rectangle(output, (x1, y1), (x2, y2), color, 2)
        label = f"{det.label} {det.confidence:.2f}: {det.text}"
        (tw, th), baseline = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
        y_text = max(y1 - th - baseline, 0)
        cv2.rectangle(output, (x1, y_text), (x1 + tw, y_text + th + baseline), color, -1)
        cv2.putText(
            output,
            label,
            (x1, y_text + th),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
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
    x1, y1, x2, y2 = detection.to_rect()
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
