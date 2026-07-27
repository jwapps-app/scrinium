import uuid

from app.services.similarity import find_near_duplicates, hamming, simhash

# A realistic document: long and varied. (A short repetitive text would
# amplify every changed word into many changed features — real documents
# don't behave that way, and neither should the fixture.)
LOREM = " ".join(
    f"section{i} paragraph about topic{i % 97} covering point{i % 31}"
    for i in range(300)
)


def test_similar_texts_have_close_hashes():
    a = simhash(LOREM)
    # simulate OCR noise: a few misreads across the document
    noisy = (
        LOREM.replace("topic13", "t0pic13")
        .replace("section250", "secti0n250")
        .replace("point7 ", "poinl7 ")
    )
    b = simhash(noisy)
    assert a is not None and b is not None
    assert hamming(a, b) <= 6


def test_different_texts_have_distant_hashes():
    other = (
        "Chapter seven discusses crop rotation strategies for smallholdings "
        "including nitrogen fixing legumes cover crops and the four field "
        "system as practiced in temperate climates across many seasons. "
    ) * 5
    a = simhash(LOREM)
    b = simhash(other)
    assert hamming(a, b) > 6


def test_too_short_returns_none():
    assert simhash("just a few words") is None
    assert simhash("") is None


def test_signed_range_fits_bigint():
    value = simhash(LOREM)
    assert -(1 << 63) <= value < (1 << 63)


def test_find_near_duplicates_banding():
    a = simhash(LOREM)
    b = simhash(LOREM.replace("water", "wailer"))  # near-identical
    c = simhash(
        "Completely unrelated content about barcode separator sheets and "
        "worker concurrency in a self hosted document management system "
        "with many additional words to pass the minimum threshold easily. "
        * 5
    )
    ids = [uuid.uuid4() for _ in range(3)]
    pairs = find_near_duplicates([(ids[0], a), (ids[1], b), (ids[2], c)])
    assert len(pairs) == 1
    pair_ids = {pairs[0][0], pairs[0][1]}
    assert pair_ids == {ids[0], ids[1]}


async def test_duplicates_endpoint(client, auth, pdf_factory):
    # two docs whose *text* is near-identical (files differ)
    import sqlalchemy as sa

    from app.database import SessionLocal
    from app.models import Document
    from app.services.similarity import simhash as sh

    created = []
    for i in range(2):
        resp = await client.post(
            "/api/documents", headers=auth,
            files={"file": (f"near{i}.pdf", pdf_factory(text=f"near-{i}-{uuid.uuid4().hex}"), "application/pdf")},
        )
        created.append(resp.json()["id"])

    text_a = LOREM
    text_b = LOREM.replace("topic42", "topic43")
    async with SessionLocal() as session:
        for doc_id, text_value in zip(created, (text_a, text_b)):
            await session.execute(
                sa.update(Document)
                .where(Document.id == uuid.UUID(doc_id))
                .values(text_content=text_value, simhash=sh(text_value))
            )
        await session.commit()

    data = (await client.get("/api/insights/duplicates", headers=auth)).json()
    pair_ids = {
        frozenset((p["a"]["id"], p["b"]["id"])) for p in data["pairs"]
    }
    assert frozenset(created) in pair_ids


def test_jaccard_measures_real_overlap():
    from app.services.similarity import bigram_set, jaccard

    a = "alpha beta gamma delta epsilon zeta eta theta"
    assert jaccard(bigram_set(a), bigram_set(a)) == 1.0
    # No shared bigrams at all → zero, unlike fingerprint distance which is
    # floored well above zero by the shortlist cutoff.
    assert jaccard(bigram_set(a), bigram_set("one two three four")) == 0.0
    assert jaccard(bigram_set(a), set()) == 0.0


def test_sampling_separates_a_fingerprint_collision():
    """Two documents sharing front matter but differing in the body must not
    score as duplicates. Reading only a prefix rates them identical; sampling
    across the document is what tells them apart."""
    import random

    from app.services.similarity import (
        bigram_set, hamming, jaccard, sample_windows, simhash,
    )

    vocab = [f"w{i}" for i in range(600)]

    def words(n, seed):
        r = random.Random(seed)
        return " ".join(r.choice(vocab) for _ in range(n))

    shared = words(12000, 1)
    a = shared + " " + words(20000, 11)
    b = shared + " " + words(20000, 22)

    # Identical fingerprints: the shortlist cannot tell these apart at all.
    assert hamming(simhash(a), simhash(b)) <= 6
    prefix_score = jaccard(bigram_set(a), bigram_set(b))
    sampled_score = jaccard(
        bigram_set(sample_windows(a)), bigram_set(sample_windows(b))
    )
    assert prefix_score > 0.9        # a prefix calls them the same document
    assert sampled_score < 0.5       # sampling exposes the different bodies

    # A genuine rescan (same text, OCR noise) still scores high when sampled.
    base = words(30000, 5)
    noisy = base.replace("w1 ", "w1O ", 300)
    assert jaccard(
        bigram_set(sample_windows(base)), bigram_set(sample_windows(noisy))
    ) > 0.8


async def test_duplicates_reports_true_backlog(client, auth):
    """The response separates the scored window from the real candidate count,
    so the review counter can't sit frozen at the window size."""
    data = (await client.get("/api/insights/duplicates", headers=auth)).json()
    assert "total" in data and "shown" in data
    assert data["shown"] == len(data["pairs"])
    assert data["total"] >= data["shown"]
    for pair in data["pairs"]:
        assert 0 <= pair["similarity"] <= 100
        assert pair["fingerprint_distance"] <= 6
    # Highest-confidence pairs first.
    scores = [p["similarity"] for p in data["pairs"]]
    assert scores == sorted(scores, reverse=True)
