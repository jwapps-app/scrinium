from app.config import settings
from app.services.ocr.base import OCRProvider, OCRResult
from app.services.ocr.tesseract import TesseractProvider


def get_provider() -> OCRProvider:
    """Resolve the configured OCR provider.

    "apple" (Mac sidecar / Option B) plugs in here later; it must fall back to
    Tesseract on connection failure so a stopped sidecar never wedges ingestion.
    """
    if settings.ocr_engine == "apple" and settings.apple_ocr_url:
        from app.services.ocr.apple import AppleVisionProvider

        return AppleVisionProvider(fallback=TesseractProvider())
    return TesseractProvider()


__all__ = ["OCRProvider", "OCRResult", "TesseractProvider", "get_provider"]
