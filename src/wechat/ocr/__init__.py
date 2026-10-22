"""OCR fallback for reading WeChat when its database cannot be read (``OCR_FALLBACK=on``)."""

from .fallback import OcrFallback, OcrMessage

__all__ = ["OcrFallback", "OcrMessage"]
