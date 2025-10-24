"""Tesseract OCR helper for Merlis PM bot."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2


class OCREngineError(RuntimeError):
    """Raised when the OCR engine cannot be initialised or executed."""


try:  # pragma: no cover - import depends on optional dependency
    import pytesseract  # type: ignore
except Exception as exc:  # pragma: no cover - bubble up original reason
    pytesseract = None  # type: ignore[assignment]
    _IMPORT_EXCEPTION = exc
else:  # pragma: no cover - executed when import is successful
    _IMPORT_EXCEPTION = None


DEFAULT_WINDOWS_PATH = Path(r"C:\\Program Files\\Tesseract-OCR\\tesseract.exe")


class OCREngine:
    """Thin wrapper around pytesseract with light preprocessing."""

    def __init__(self, tesseract_path: str | None = None, language: str = "tur+eng") -> None:
        if pytesseract is None:
            raise OCREngineError(
                "pytesseract kütüphanesi yüklenemedi. Lütfen 'pytesseract' paketinin kurulu olduğundan emin olun: "
                f"{_IMPORT_EXCEPTION}"
            )

        self._language = language or "eng"
        self._tesseract_cmd: str | None = None

        # Resolve Tesseract executable path
        configured = Path(tesseract_path) if tesseract_path else None
        candidate: Path | None = None
        if configured and configured.exists():
            candidate = configured
        elif configured and not configured.exists():
            raise OCREngineError(f"Tesseract yolu bulunamadı: {configured}")
        elif DEFAULT_WINDOWS_PATH.exists():
            candidate = DEFAULT_WINDOWS_PATH

        if candidate is not None:
            self._tesseract_cmd = str(candidate)
            pytesseract.pytesseract.tesseract_cmd = self._tesseract_cmd

        # Validate that Tesseract is reachable
        try:
            pytesseract.get_tesseract_version()
        except pytesseract.TesseractNotFoundError as exc:  # type: ignore[attr-defined]
            raise OCREngineError(
                "Tesseract çalıştırılabilir dosyası bulunamadı. 'C\\Program Files\\Tesseract-OCR' kurulumu ve yol ayarlarını kontrol edin."
            ) from exc

    def preprocess(self, bgr_image: Any) -> Any:
        gray = cv2.cvtColor(bgr_image, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)

    def read(self, bgr_image: Any) -> str:
        processed = self.preprocess(bgr_image)
        try:
            rgb = cv2.cvtColor(processed, cv2.COLOR_BGR2RGB)
            text = pytesseract.image_to_string(rgb, lang=self._language)
        except pytesseract.TesseractNotFoundError as exc:  # type: ignore[attr-defined]
            raise OCREngineError(
                "Tesseract bulunamadı. Kurulu olduğundan ve yolunun doğru ayarlandığından emin olun."
            ) from exc
        except pytesseract.TesseractError as exc:  # type: ignore[attr-defined]
            raise OCREngineError(f"Tesseract OCR çalıştırılırken hata oluştu: {exc}") from exc
        except Exception as exc:  # pragma: no cover - defensive catch-all
            raise OCREngineError(f"OCR beklenmeyen bir hata verdi: {exc}") from exc
        return text.strip()

