"""Two things that were quietly wrong, and one that only became wrong today.

The review pile and the not-PDF/A counter both presented a fact without its
meaning: an untagged upload looked like a fault, and a deliberate plain-PDF
archive looked like a failed conversion. And a new tag came out with no colour
at all, because nothing assigned one at creation.
"""

import uuid

from sqlalchemy import select, update

from app.database import SessionLocal
from app.models import Document, DocumentStatus
from app.services import palette, review


class _Doc:
    """Just the columns reasons_for reads."""

    def __init__(self, **kw):
        self.status = "ready"
        self.error = None
        self.correspondent_id = None
        self.doc_type_id = None
        self.tags = []
        self.page_count = 10
        self.text_length = 10_000
        self.weak_ocr_dismissed = False
        self.archive_blob_id = "blob"
        self.archive_pdfa = True
        self.archive_pdfa_wanted = True
        self.__dict__.update(kw)


def _keys(doc):
    return [r.key for r in review.reasons_for(doc)]


def test_an_untagged_upload_is_routine_not_a_fault():
    """The whole complaint: three new documents landed in review and read as
    three problems."""
    reasons = review.reasons_for(_Doc())

    assert [r.key for r in reasons] == ["unfiled"]
    assert reasons[0].severity == review.INFO
    assert "nothing is wrong" in reasons[0].detail.lower()


def test_filing_by_any_one_of_the_three_clears_it():
    assert _keys(_Doc(doc_type_id="t")) == []
    assert _keys(_Doc(correspondent_id="c")) == []
    assert _keys(_Doc(tags=[object()])) == []


def test_a_real_problem_outranks_the_routine_one():
    """A thin scan that is also unfiled must not be filed away as routine."""
    reasons = review.reasons_for(_Doc(text_length=200))  # 20 chars/page

    assert [r.key for r in reasons] == ["weak_ocr", "unfiled"]
    assert reasons[0].severity == review.PROBLEM
    assert any(r.severity == review.INFO for r in reasons)


def test_the_weak_ocr_threshold_matches_the_worklist():
    """One definition, so the label and /insights/weak-ocr cannot disagree."""
    per_page = review.WEAK_OCR_CHARS_PER_PAGE
    assert "weak_ocr" in _keys(_Doc(page_count=10, text_length=per_page * 10 - 1))
    assert "weak_ocr" not in _keys(_Doc(page_count=10, text_length=per_page * 10))


def test_a_scan_dismissed_as_fine_stops_being_flagged():
    assert _keys(_Doc(text_length=1, weak_ocr_dismissed=True)) == ["unfiled"]


def test_plain_pdf_on_purpose_is_not_a_shortfall():
    """The regression ARCHIVE_FORMAT=auto would otherwise have introduced:
    every scan reported as a failed PDF/A conversion, for ever."""
    deliberate = _Doc(archive_pdfa=False, archive_pdfa_wanted=False, tags=[object()])
    assert _keys(deliberate) == []

    fell_back = _Doc(archive_pdfa=False, archive_pdfa_wanted=True, tags=[object()])
    assert _keys(fell_back) == ["not_pdfa"]


async def test_the_counter_only_counts_archives_that_fell_short(
    client, auth, pdf_factory
):
    """End to end, because the count and the filter are separate queries and
    both had the same bug.

    Measured as a delta: the test database is shared for the whole session, so
    an absolute count would depend on what every other test happened to leave
    behind.
    """
    async def counted():
        stats = (await client.get("/api/documents/stats", headers=auth)).json()
        listed = (
            await client.get("/api/documents?non_pdfa=true", headers=auth)
        ).json()
        return stats["non_pdfa"], listed["total"]

    before = await counted()
    doc = await _upload(client, auth, pdf_factory)

    async with SessionLocal() as session:
        # No worker runs in tests, so give it an archive by pointing at the
        # original blob — enough for a count that only reads the id.
        original = (
            await session.execute(
                select(Document.original_blob_id).where(Document.id == doc["id"])
            )
        ).scalar_one()
        await session.execute(
            update(Document)
            .where(Document.id == doc["id"])
            .values(
                archive_blob_id=original,
                archive_pdfa=False,
                archive_pdfa_wanted=False,
            )
        )
        await session.commit()

    assert await counted() == before, "a deliberate plain PDF is not a shortfall"

    async with SessionLocal() as session:
        await session.execute(
            update(Document)
            .where(Document.id == doc["id"])
            .values(archive_pdfa_wanted=True)
        )
        await session.commit()

    after = await counted()
    assert after == (before[0] + 1, before[1] + 1), (
        f"a conversion that fell back must still count: {before} -> {after}"
    )


async def test_the_api_says_why_a_document_is_in_review(
    client, auth, pdf_factory
):
    doc = await _upload(client, auth, pdf_factory)

    # Still pending: no worker runs here, and a document that has not finished
    # processing is not asking to be filed yet.
    fresh = (await client.get(f"/api/documents/{doc['id']}", headers=auth)).json()
    assert fresh["review_reasons"] == [], "a pending document is not in review"

    async with SessionLocal() as session:
        await session.execute(
            update(Document)
            .where(Document.id == doc["id"])
            .values(status=DocumentStatus.READY, page_count=2, text_length=9000)
        )
        await session.commit()

    body = (await client.get(f"/api/documents/{doc['id']}", headers=auth)).json()

    unfiled = [r for r in body["review_reasons"] if r["key"] == "unfiled"]
    assert unfiled, f"finished and unfiled, but got {body['review_reasons']}"
    assert unfiled[0]["severity"] == "info"
    assert unfiled[0]["label"] and unfiled[0]["detail"]


# --- tag colour ------------------------------------------------------------


async def test_a_new_child_tag_gets_a_shade_of_its_parent(client, auth):
    """It was coming back with no colour at all — the only thing that ever
    assigned one was the whole-tree recolour, run by hand."""
    parent = (
        await client.post("/api/tags", json={"name": f"Vehicles-{uuid.uuid4().hex[:6]}"}, headers=auth)
    ).json()
    assert parent["color"], "even a root tag should get a colour"

    child = (
        await client.post(
            "/api/tags",
            json={"name": f"Insurance-{uuid.uuid4().hex[:6]}", "parent_id": parent["id"]},
            headers=auth,
        )
    ).json()

    assert child["color"], "a nested tag came back with no colour"
    parent_hsl = palette.hex_to_hsl(parent["color"])
    child_hsl = palette.hex_to_hsl(child["color"])
    assert child_hsl[2] > parent_hsl[2], "a child should be the lighter shade"


async def test_siblings_stay_distinct_and_stay_in_the_family(client, auth):
    """The reported bug: the twelfth child of a pink parent came out olive.

    Fanning by a fixed step per child had no bound, so a big family walked
    right off its own hue — and counting siblings could not see the colours
    they actually held.
    """
    from app.services.palette import SIBLING_HUE_BAND

    parent = (
        await client.post(
            "/api/tags", json={"name": f"Home-{uuid.uuid4().hex[:6]}"}, headers=auth
        )
    ).json()
    parent_hue = palette.hex_to_hsl(parent["color"])[0]

    colors = []
    for _ in range(12):
        made = (
            await client.post(
                "/api/tags",
                json={
                    "name": f"leaf-{uuid.uuid4().hex[:8]}",
                    "parent_id": parent["id"],
                },
                headers=auth,
            )
        ).json()
        colors.append(made["color"])

    assert len(set(colors)) == 12, f"siblings collided: {colors}"
    for color in colors:
        hue = palette.hex_to_hsl(color)[0]
        drift = abs(((hue - parent_hue + 180) % 360) - 180)
        # +2 for the hue quantisation of an 8-bit-per-channel round trip.
        assert drift <= SIBLING_HUE_BAND + 2, (
            f"{color} is {drift:.0f} deg from its parent — out of the family"
        )


async def test_a_new_child_avoids_the_hues_its_siblings_hold(client, auth):
    """Colours assigned by the whole-tree pass, or by hand, or left behind by
    a deleted sibling — a count cannot see any of them."""
    parent = (
        await client.post(
            "/api/tags", json={"name": f"Fleet-{uuid.uuid4().hex[:6]}"}, headers=auth
        )
    ).json()
    parent_hue = palette.hex_to_hsl(parent["color"])[0]
    taken = palette.hsl_to_hex(parent_hue, 50, 55)

    await client.post(
        "/api/tags",
        json={
            "name": f"taken-{uuid.uuid4().hex[:8]}",
            "parent_id": parent["id"],
            "color": taken,
        },
        headers=auth,
    )
    made = (
        await client.post(
            "/api/tags",
            json={"name": f"next-{uuid.uuid4().hex[:8]}", "parent_id": parent["id"]},
            headers=auth,
        )
    ).json()

    assert made["color"] != taken
    gap = abs(
        ((palette.hex_to_hsl(made["color"])[0] - parent_hue + 180) % 360) - 180
    )
    assert gap > 5, f"landed on the hand-picked hue: {made['color']} vs {taken}"


async def test_an_explicit_colour_still_wins(client, auth):
    made = (
        await client.post(
            "/api/tags", json={"name": f"Chosen-{uuid.uuid4().hex[:6]}", "color": "#123456"}, headers=auth
        )
    ).json()
    assert made["color"] == "#123456"


async def _upload(client, auth, pdf_factory):
    resp = await client.post(
        "/api/documents",
        headers=auth,
        files={
            "file": (f"rv-{uuid.uuid4().hex[:6]}.pdf", pdf_factory(), "application/pdf")
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()
