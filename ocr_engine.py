"""RapidOCR based OCR helper."""
from __future__ import annotations

import cv2
from rapidocr_onnxruntime import RapidOCR


class OCREngine:
    """Thin wrapper around RapidOCR with light preprocessing."""

    def __init__(self, use_gpu: bool = True, det_size_limit: int = 960) -> None:
        self._use_gpu = use_gpu
        self._det_size_limit = det_size_limit
        self._ocr: RapidOCR | None = None

    def _ensure_engine(self) -> RapidOCR:
        if self._ocr is None:
            self._ocr = RapidOCR(
                use_cuda=self._use_gpu,
                det_size_limit=self._det_size_limit,
            )
        return self._ocr

    def preprocess(self, bgr_image):
        gray = cv2.cvtColor(bgr_image, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)

    def read(self, bgr_image) -> str:
        engine = self._ensure_engine()
        processed = self.preprocess(bgr_image)
        result, _ = engine(processed)
        if not result:
            return ""
        return "\n".join(r[0] for r in result).strip()
