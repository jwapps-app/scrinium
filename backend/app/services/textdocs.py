"""Native text extraction for non-scanned formats.

Office documents (.docx/.xlsx/.pptx/.odt) are zipped XML; epubs are zipped
XHTML — all readable with the standard library, no LibreOffice needed. The
original file is stored untouched as always; the extracted text feeds
search and the in-app reader view. There is deliberately no PDF conversion:
formatting-faithful viewing means downloading the original.
"""

import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
# defusedxml hardens against entity-expansion / external-entity attacks in
# untrusted office XML, regardless of the platform libexpat version.
try:
    from defusedxml.ElementTree import iterparse as _safe_iterparse
except Exception:  # pragma: no cover — fallback if the dep is missing
    from xml.etree.ElementTree import iterparse as _safe_iterparse
from xml.etree import ElementTree

class _BombError(Exception):
    """A zip member decompresses beyond the allowed budget."""


TEXT_SUFFIXES = {
    ".txt", ".md", ".epub", ".docx", ".xlsx", ".pptx", ".odt",
}

_MAX_TEXT = 20_000_000  # 20 MB of extracted text is plenty
_MAX_MEMBER = 200 * 1024 * 1024  # refuse to decompress a >200 MB member
_MAX_UNCOMPRESSED = 1024 * 1024 * 1024  # 1 GB total across a document


class _HTMLText(HTMLParser):
    _BLOCKS = {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _html_to_text(html: str) -> str:
    parser = _HTMLText()
    try:
        parser.feed(html)
    except Exception:
        return html
    return "".join(parser.parts)


def _xml_text(payload: bytes, block_tags: tuple[str, ...]) -> str:
    """All text nodes, with newlines at the given (local-name) block tags."""
    parts: list[str] = []
    try:
        for event, element in _safe_iterparse(
            __import__("io").BytesIO(payload), events=("end",)
        ):
            local = element.tag.rsplit("}", 1)[-1]
            if local in block_tags:
                parts.append("\n")
            if element.text:
                parts.append(element.text)
            if element.tail:
                parts.append(element.tail)
            element.clear()
    except Exception:
        # Malformed or hostile XML (entity attacks raise here via
        # defusedxml) — return whatever parsed cleanly; never crash intake.
        pass
    return "".join(parts)


def _clean(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:_MAX_TEXT]


def extract_text(path: Path) -> str | None:
    """Text for a supported non-scanned format; None if unsupported."""
    suffix = path.suffix.lower()
    if suffix not in TEXT_SUFFIXES:
        return None

    if suffix in (".txt", ".md"):
        return _clean(path.read_text(errors="replace"))

    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()

            budget = {"left": _MAX_UNCOMPRESSED}

            def read(member: str) -> bytes:
                info = zf.getinfo(member)
                if info.file_size > _MAX_MEMBER or info.file_size > budget["left"]:
                    raise _BombError()
                budget["left"] -= info.file_size
                return zf.read(member)

            if suffix == ".docx":
                return _clean(
                    _xml_text(read("word/document.xml"), ("p",))
                )

            if suffix == ".odt":
                return _clean(_xml_text(read("content.xml"), ("p", "h")))

            if suffix == ".xlsx":
                parts = []
                if "xl/sharedStrings.xml" in names:
                    parts.append(_xml_text(read("xl/sharedStrings.xml"), ("si",)))
                for name in sorted(n for n in names if n.startswith("xl/worksheets/")):
                    parts.append(_xml_text(read(name), ("row",)))
                return _clean("\n".join(parts))

            if suffix == ".pptx":
                slides = sorted(
                    (n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                    key=lambda n: int(re.search(r"(\d+)", n).group(1)),
                )
                return _clean(
                    "\n\n".join(_xml_text(read(n), ("p",)) for n in slides)
                )

            if suffix == ".epub":
                docs = [
                    n for n in names
                    if n.lower().endswith((".xhtml", ".html", ".htm"))
                ]
                # Spine order when the OPF is readable; natural order otherwise.
                try:
                    opf_name = next(n for n in names if n.endswith(".opf"))
                    opf = read(opf_name).decode(errors="replace")
                    order = re.findall(r'href="([^"]+\.x?html?)"', opf)
                    base = opf_name.rsplit("/", 1)[0] + "/" if "/" in opf_name else ""
                    ordered = [base + h for h in order if base + h in names]
                    docs = ordered or sorted(docs)
                except StopIteration:
                    docs = sorted(docs)
                chapters = [
                    _html_to_text(read(n).decode(errors="replace")) for n in docs
                ]
                return _clean("\n\n".join(chapters))
    except _BombError:
        # Refuse quietly — the document is stored as-is; only text is skipped.
        return None
    except (zipfile.BadZipFile, KeyError, OSError):
        return None
    return None
