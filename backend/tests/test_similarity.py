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
