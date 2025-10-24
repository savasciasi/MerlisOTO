"""RapidOCR based OCR helper."""
from __future__ import annotations

from typing import Any

import cv2


class OCREngineError(RuntimeError):
    """Raised when the OCR engine cannot be initialised or executed."""


try:  # pragma: no cover - import is environment-specific
    from rapidocr_onnxruntime import RapidOCR  # type: ignore
except Exception as exc:  # pragma: no cover - we want to surface the original error
    RapidOCR = None  # type: ignore[assignment]
    _IMPORT_EXCEPTION = exc
else:  # pragma: no cover - thin wrapper
    _IMPORT_EXCEPTION = None


class OCREngine:
    """Thin wrapper around RapidOCR with light preprocessing."""

    def __init__(self, use_gpu: bool = True, det_size_limit: int = 960) -> None:
        self._use_gpu = use_gpu
        self._det_size_limit = det_size_limit
        self._ocr: Any | None = None

        if RapidOCR is None:
            raise OCREngineError(
                "RapidOCR kütüphanesi yüklenemedi. ONNXRuntime bağımlılığı eksik olabilir: "
                f"{_IMPORT_EXCEPTION}"
            )

    def _ensure_engine(self):
        if self._ocr is None:
            try:
                self._ocr = RapidOCR(  # type: ignore[misc]
                    use_cuda=self._use_gpu,
                    det_size_limit=self._det_size_limit,
                )
            except Exception as exc:  # pragma: no cover - engine init happens once
                raise OCREngineError(
                    "RapidOCR motoru başlatılamadı. ONNXRuntime kurulumunu kontrol edin."
                ) from exc
        return self._ocr

    def preprocess(self, bgr_image):
        gray = cv2.cvtColor(bgr_image, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)

    def read(self, bgr_image) -> str:
        engine = self._ensure_engine()
        processed = self.preprocess(bgr_image)
        try:
            result, _ = engine(processed)
        except Exception as exc:  # pragma: no cover - delegated to RapidOCR
            raise OCREngineError(f"RapidOCR çalıştırılamadı: {exc}") from exc
        if not result:
            return ""
        return "\n".join(r[0] for r in result).strip()
