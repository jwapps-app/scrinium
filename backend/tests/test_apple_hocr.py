"""hOCR emitted by the Vision plugin has to be parseable XML.

Regression test for the fault that pushed this whole library onto the
--force-ocr path: Vision returns a control character, html.escape() leaves it
alone because it only handles & < >, XML 1.0 has no way to represent it, and
ocrmypdf's ElementTree.parse dies with "not well-formed". Both cheap attempts
fail identically, so every affected document re-rasterized every page.
"""

from xml.etree import ElementTree

import pytest

from app.services.ocr.apple_engine_plugin import _to_hocr, _xml_safe


def _result(text: str) -> dict:
    return {
        "width": 1000,
        "height": 1400,
        "blocks": [
            {"text": text, "bbox": [0.1, 0.1, 0.9, 0.2], "confidence": 0.98}
        ],
    }


@pytest.mark.parametrize(
    "bad",
    [
        "form\x0cfeed",
        "vertical\x0btab",
        "null\x00byte",
        "bell\x07here",
        "esc\x1bsequence",
        "\ud800lone surrogate",
    ],
)
def test_control_characters_do_not_break_the_hocr(bad):
    hocr = _to_hocr(_result(bad))
    ElementTree.fromstring(hocr)  # must not raise ParseError


def test_the_unsanitized_form_really_would_have_broken(bad="page\x0cbreak"):
    """Guard against the fix being a no-op: prove the input is genuinely bad."""
    import html

    hostile = _to_hocr(_result("placeholder")).replace(
        "placeholder", html.escape(bad)
    )
    with pytest.raises(ElementTree.ParseError):
        ElementTree.fromstring(hostile)


def test_legitimate_text_survives_intact():
    """Sanitizing must not eat real content — accents, CJK, emoji, quotes."""
    keep = "Café — naïve “quotes” 日本語 🙂 tab\there"
    assert _xml_safe(keep) == keep

    hocr = _to_hocr(_result(keep))
    root = ElementTree.fromstring(hocr)
    words = [e.text for e in root.iter() if e.get("class") == "ocrx_word"]
    assert words == [keep]


def test_markup_in_ocr_text_is_still_escaped():
    """Sanitizing must not replace escaping — <b> is text, not an element."""
    hocr = _to_hocr(_result("a <b>tag</b> & an ampersand"))
    root = ElementTree.fromstring(hocr)
    words = [e.text for e in root.iter() if e.get("class") == "ocrx_word"]
    assert words == ["a <b>tag</b> & an ampersand"]
