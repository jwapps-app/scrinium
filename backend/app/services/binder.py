"""Print binder: one press-ready PDF from a set of documents.

If the grid is down, the NAS is too — a curated binder ("Water", "Medical",
"the go-bag set") printed and shelved is the tier below the offline app.
Output: generated cover page + table of contents (with each document's
starting sheet number) followed by every document in order. Cover and TOC
are drawn with Ghostscript from generated PostScript — no extra
dependencies. Page numbers on content pages are the printer's job (every
print dialog offers them); the TOC's sheet numbers account for cover+TOC
so they match the physical stack.
"""

import subprocess
import tempfile
from datetime import date
from pathlib import Path

import pikepdf

TOC_PER_PAGE = 34


class BinderError(Exception):
    pass


def _ps_escape(text: str) -> str:
    # Truncate *first*: slicing the escaped string could cut between a backslash
    # and the character it escaped, leaving a trailing lone backslash. That
    # escaped the closing paren, so the PostScript string literal ran past its
    # terminator and Ghostscript rejected the file — one title landing on the
    # 88-character boundary broke binder generation for any selection containing
    # it, and titles come from filenames, so that input is not ours to trust.
    return (
        text[:88].replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    )


def _front_matter_ps(title: str, entries: list[tuple[str, int]]) -> str:
    """PostScript for the cover + TOC pages. entries: (title, sheet_no)."""
    lines = [
        "%!PS-Adobe-3.0",
        "/Times-Bold findfont 34 scalefont setfont",
        f"72 640 moveto ({_ps_escape(title)}) show",
        "/Times-Roman findfont 14 scalefont setfont",
        f"72 600 moveto ({date.today().strftime('%B %d, %Y')} — "
        f"{len(entries)} documents) show",
        "0.5 setlinewidth 72 585 moveto 540 585 lineto stroke",
        "showpage",
    ]
    for start in range(0, len(entries), TOC_PER_PAGE):
        chunk = entries[start : start + TOC_PER_PAGE]
        lines += [
            "/Times-Bold findfont 18 scalefont setfont",
            "72 710 moveto (Contents) show",
            "/Times-Roman findfont 11 scalefont setfont",
        ]
        y = 680
        for doc_title, sheet in chunk:
            safe = _ps_escape(doc_title)
            lines += [
                f"72 {y} moveto ({safe}) show",
                f"505 {y} moveto ({sheet}) show",
            ]
            y -= 19
        lines.append("showpage")
    return "\n".join(lines) + "\n"


def build_binder(
    title: str, docs: list[tuple[str, Path]], out_path: Path
) -> int:
    """docs: (title, pdf_path) in binder order. Returns total pages."""
    if not docs:
        raise BinderError("Nothing to bind")

    # Pass 1: measure everything so TOC sheet numbers are exact.
    page_counts = []
    for doc_title, path in docs:
        try:
            with pikepdf.open(path) as pdf:
                page_counts.append(len(pdf.pages))
        except pikepdf.PdfError as exc:
            raise BinderError(f"“{doc_title}” isn't a readable PDF") from exc

    toc_pages = (len(docs) + TOC_PER_PAGE - 1) // TOC_PER_PAGE
    front_pages = 1 + toc_pages
    entries = []
    cursor = front_pages + 1  # first content sheet
    for (doc_title, _path), pages in zip(docs, page_counts):
        entries.append((doc_title, cursor))
        cursor += pages

    # Front matter via Ghostscript.
    with tempfile.TemporaryDirectory(prefix="binder-") as tmp:
        ps_path = Path(tmp) / "front.ps"
        front_pdf = Path(tmp) / "front.pdf"
        ps_path.write_text(_front_matter_ps(title, entries))
        result = subprocess.run(
            # -dSAFER to match every other Ghostscript call here: this is the one
            # whose input is built from user-controlled strings, so it's the last
            # place that should be missing the seatbelt.
            [
                "gs",
                "-q",
                "-dSAFER",
                "-dBATCH",
                "-dNOPAUSE",
                "-sDEVICE=pdfwrite",
                "-o",
                str(front_pdf),
                str(ps_path),
            ],
            capture_output=True,
            timeout=120,
        )
        if result.returncode != 0 or not front_pdf.exists():
            raise BinderError("Cover generation failed")

        merged = pikepdf.new()
        with pikepdf.open(front_pdf) as front:
            for page in front.pages:
                merged.pages.append(page)
        for _title, path in docs:
            with pikepdf.open(path) as pdf:
                for page in pdf.pages:
                    merged.pages.append(page)
        total = len(merged.pages)
        merged.save(out_path)
    return total
