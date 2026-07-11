"""Extract a document's own date (letter date, invoice date…) from its text.

Heuristic, transparent, and editable afterwards: scan the first stretch of
text in reading order (letterhead dates come first), accept the first match
that parses to a plausible date. Ambiguous numeric dates follow DATE_ORDER
(default MDY, the US convention).
"""

import re
from datetime import date, timedelta

from app.config import settings

MONTHS = {
    m: i + 1
    for i, group in enumerate(
        [
            ("jan", "january"), ("feb", "february"), ("mar", "march"),
            ("apr", "april"), ("may",), ("jun", "june"), ("jul", "july"),
            ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"),
            ("nov", "november"), ("dec", "december"),
        ]
    )
    for m in group
}

_MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))

PATTERNS = [
    # 2024-03-05 / 2024/03/05
    re.compile(r"\b(?P<y>19\d{2}|20\d{2})[-/](?P<m>0?[1-9]|1[0-2])[-/](?P<d>0?[1-9]|[12]\d|3[01])\b"),
    # March 5, 2024 · Mar 5 2024 · March 5th, 2024
    re.compile(
        rf"\b(?P<mon>{_MONTH_RE})\.?\s+(?P<d>0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?,?\s+(?P<y>19\d{{2}}|20\d{{2}})\b",
        re.IGNORECASE,
    ),
    # 5 March 2024
    re.compile(
        rf"\b(?P<d>0?[1-9]|[12]\d|3[01])\s+(?P<mon>{_MONTH_RE})\.?,?\s+(?P<y>19\d{{2}}|20\d{{2}})\b",
        re.IGNORECASE,
    ),
    # 03/05/2024 · 3-5-24 (order per DATE_ORDER)
    re.compile(r"\b(?P<a>0?[1-9]|[12]\d|3[01])[/-](?P<b>0?[1-9]|[12]\d|3[01])[/-](?P<y>19\d{2}|20\d{2}|\d{2})\b"),
]

SCAN_CHARS = 8000  # roughly the first few pages


def _plausible(candidate: date) -> bool:
    return date(1950, 1, 1) <= candidate <= date.today() + timedelta(days=400)


def _build(y: int, m: int, d: int) -> date | None:
    if y < 100:
        y += 2000 if y <= (date.today().year % 100) + 1 else 1900
    try:
        candidate = date(y, m, d)
    except ValueError:
        return None
    return candidate if _plausible(candidate) else None


def extract_document_date(text: str | None) -> date | None:
    if not text:
        return None
    window = text[:SCAN_CHARS]
    best: tuple[int, date] | None = None
    for pattern in PATTERNS:
        for match in pattern.finditer(window):
            groups = match.groupdict()
            if "mon" in groups and groups.get("mon"):
                candidate = _build(
                    int(groups["y"]), MONTHS[groups["mon"].lower()], int(groups["d"])
                )
            elif "a" in groups and groups.get("a"):
                first, second = int(groups["a"]), int(groups["b"])
                month, day = (
                    (first, second)
                    if settings.date_order == "MDY"
                    else (second, first)
                )
                if month > 12 and day <= 12:
                    month, day = day, month
                if month > 12:
                    continue
                candidate = _build(int(groups["y"]), month, day)
            else:
                candidate = _build(
                    int(groups["y"]), int(groups["m"]), int(groups["d"])
                )
            if candidate and (best is None or match.start() < best[0]):
                best = (match.start(), candidate)
    return best[1] if best else None
