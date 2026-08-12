"""Why a document is asking for attention — and how much it deserves.

The review bucket holds documents with no tag, correspondent or type. That is
the normal state of anything just uploaded: it means "not filed yet", not
"something went wrong". Presented as a bare count next to the failure buckets
it reads as a fault list, and the two genuinely different things — a document
waiting to be filed, and a document whose OCR barely produced any text — look
identical.

So each reason carries its own severity. `info` is routine and needs no
action beyond filing; `problem` is something that actually did not work.
"""

from dataclasses import dataclass

# Under this many characters of recognised text per page, a scan is suspect.
# Same threshold as the /insights/weak-ocr worklist — one definition, so the
# review label and that page can never disagree about which scans are thin.
WEAK_OCR_CHARS_PER_PAGE = 150

INFO = "info"
PROBLEM = "problem"


@dataclass(frozen=True)
class ReviewReason:
    key: str
    label: str
    severity: str
    detail: str


def reasons_for(doc) -> list[ReviewReason]:
    """Every reason this document is worth a look, worst first.

    Reads only columns already loaded for the list view — no extra queries,
    and nothing that would pull text_content into memory per row.
    """
    reasons: list[ReviewReason] = []

    if doc.status == "flagged" or doc.error:
        reasons.append(
            ReviewReason(
                key="ocr_failed",
                label="OCR failed",
                severity=PROBLEM,
                detail=(
                    doc.error.strip()[:300]
                    if doc.error
                    else "Processing did not finish. The original is untouched."
                ),
            )
        )

    if _is_weak_ocr(doc):
        per_page = int((doc.text_length or 0) / doc.page_count)
        reasons.append(
            ReviewReason(
                key="weak_ocr",
                label="Little text recognised",
                severity=PROBLEM,
                detail=(
                    f"About {per_page} characters per page, under the "
                    f"{WEAK_OCR_CHARS_PER_PAGE} expected of a readable scan. "
                    "It may be a photograph, a very faint original, or the "
                    "wrong OCR language."
                ),
            )
        )

    if doc.archive_blob_id is not None and doc.archive_pdfa is False and doc.archive_pdfa_wanted:
        reasons.append(
            ReviewReason(
                key="not_pdfa",
                label="Not PDF/A",
                severity=PROBLEM,
                detail=(
                    "This document was meant to be archived as PDF/A and the "
                    "conversion fell back to plain PDF. The text layer is "
                    "intact; only the long-term-preservation guarantee is not."
                ),
            )
        )

    if _is_unfiled(doc):
        reasons.append(
            ReviewReason(
                key="unfiled",
                label="Not filed yet",
                severity=INFO,
                detail=(
                    "No tag, correspondent or document type. Normal for a new "
                    "upload — nothing is wrong with it. Give it any one of the "
                    "three and it leaves this list."
                ),
            )
        )

    return reasons


def _is_unfiled(doc) -> bool:
    # Matches the needs_review filter in routers/documents.py: any one of the
    # three counts as filed, so a books library is not asked to invent
    # correspondents for everything.
    return (
        doc.status == "ready"
        and doc.correspondent_id is None
        and doc.doc_type_id is None
        and not doc.tags
    )


def _is_weak_ocr(doc) -> bool:
    if doc.status != "ready" or doc.weak_ocr_dismissed:
        return False
    if not doc.page_count or doc.text_length is None:
        return False
    return doc.text_length < doc.page_count * WEAK_OCR_CHARS_PER_PAGE
