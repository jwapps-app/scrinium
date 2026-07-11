from app.config import settings
from app.services.ocr.base import OCRProvider, OCRResult
from app.services.ocr.tesseract import TesseractProvider


def get_provider(engine: str | None = None) -> OCRProvider:
    """Resolve the OCR provider.

    `engine` (from the runtime Settings toggle) wins over the OCR_ENGINE env
    default. The Apple provider always falls back to Tesseract on connection
    failure, so a stopped sidecar never wedges ingestion.
    """
    effective = engine or settings.ocr_engine
    if effective == "apple" and settings.apple_ocr_url:
        from app.services.ocr.apple import AppleVisionProvider

        return AppleVisionProvider(fallback=TesseractProvider())
    return TesseractProvider()


__all__ = ["OCRProvider", "OCRResult", "TesseractProvider", "get_provider"]
