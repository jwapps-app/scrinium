"""Near-duplicate detection via simhash.

Byte-identical duplicates are caught at ingest by the content hash; this
catches the other kind — the same document scanned twice at different DPI,
or re-OCR'd by a different engine, where every byte differs but the text is
nearly the same.

Each document's text gets a 64-bit simhash (word-shingle features, weighted
bit voting). Similar texts differ in only a few bits, so candidate pairs are
found by banding (four 16-bit bands; near-identical hashes must collide in
at least one band) and confirmed by Hamming distance. 13k documents compare
in well under a second this way — no 169-million-pair scan.
"""

import hashlib
import re
from collections import defaultdict

_WORD = re.compile(r"[a-z0-9]{2,}")

MAX_HAMMING = 6  # ≤6 differing bits of 64 → effectively the same text


def simhash(text: str, max_chars: int = 40000) -> int | None:
    """64-bit simhash of the text (signed, to fit Postgres BIGINT).
    None for texts too short to fingerprint meaningfully."""
    words = _WORD.findall(text[:max_chars].lower())
    if len(words) < 20:
        return None
    weights = [0] * 64
    # Word bigrams: order-sensitive enough to distinguish real documents,
    # tolerant of small OCR differences.
    for i in range(len(words) - 1):
        feature = f"{words[i]} {words[i + 1]}"
        digest = int.from_bytes(
            hashlib.md5(feature.encode()).digest()[:8], "big"
        )
        for bit in range(64):
            weights[bit] += 1 if (digest >> bit) & 1 else -1
    value = 0
    for bit in range(64):
        if weights[bit] > 0:
            value |= 1 << bit
    if value >= 1 << 63:
        value -= 1 << 64
    return value


def hamming(a: int, b: int) -> int:
    return ((a ^ b) & ((1 << 64) - 1)).bit_count()


# Scoring reads three windows of this size — start, middle, end — rather than
# one long prefix. A prefix is actively misleading on this library's content:
# 40k characters is ~10 pages of a 700-page book, so two different volumes that
# share a publisher's front matter score as identical on their opening alone.
# Sampling across the document makes differing bodies visible.
SAMPLE_CHARS = 12000
COMPARE_CHARS = SAMPLE_CHARS * 3


def sample_windows(text: str, window: int = SAMPLE_CHARS) -> str:
    """Start + middle + end excerpt of the text — the Python mirror of the
    SQL-side sampling, so both sides describe the same span."""
    if len(text) <= window * 3:
        return text
    mid = max(0, len(text) // 2 - window // 2)
    return " ".join(
        (text[:window], text[mid : mid + window], text[-window:])
    )


def bigram_set(text: str, max_chars: int = COMPARE_CHARS) -> set[str]:
    """Word bigrams of the text as an explicit set, for measuring real overlap."""
    words = _WORD.findall(text[:max_chars].lower())
    return {f"{words[i]} {words[i + 1]}" for i in range(len(words) - 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    """Share of bigrams the two texts have in common, 0..1.

    This is a genuine content measure, unlike fingerprint distance: two
    unrelated documents score near zero, where their *fingerprints* can still
    land a couple of bits apart and look deceptively close. Too expensive to
    run across the whole library, which is what the fingerprint is for — but
    cheap on a shortlist, where it separates real rescans from hash collisions.
    """
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def find_near_duplicates(
    rows: list[tuple], max_hamming: int = MAX_HAMMING
) -> list[tuple]:
    """rows: (id, simhash) → [(id_a, id_b, distance)], closest first.

    Banding keeps this fast: hashes within `max_hamming` of each other must
    agree exactly on at least one of four 16-bit bands (pigeonhole: ≤6
    differing bits can't touch all four bands... with 7+ they could, hence
    the cap), so only band-colliding pairs are compared.
    """
    buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
    by_index: list[tuple] = []
    for idx, (doc_id, h) in enumerate(rows):
        unsigned = h & ((1 << 64) - 1)
        by_index.append((doc_id, unsigned))
        for band in range(4):
            key = (band, (unsigned >> (band * 16)) & 0xFFFF)
            buckets[key].append(idx)

    seen: set[tuple[int, int]] = set()
    pairs = []
    for members in buckets.values():
        if len(members) < 2 or len(members) > 500:
            continue  # oversized bucket = degenerate band, skip
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                if (a, b) in seen:
                    continue
                seen.add((a, b))
                dist = ((by_index[a][1] ^ by_index[b][1])).bit_count()
                if dist <= max_hamming:
                    pairs.append((by_index[a][0], by_index[b][0], dist))
    pairs.sort(key=lambda p: p[2])
    return pairs
