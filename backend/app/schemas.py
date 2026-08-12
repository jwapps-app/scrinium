import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class SetupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    totp: str | None = None


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class AuthStatus(BaseModel):
    needs_setup: bool


class TagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    parent_id: uuid.UUID | None = None
    color: str | None = None
    count: int = 0


class TagCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    parent_id: uuid.UUID | None = None
    # Hex only: the value is interpolated into a CSS property client-side.
    color: str | None = Field(default=None, max_length=16, pattern="^#[0-9a-fA-F]{3,8}$")


class TagUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    parent_id: uuid.UUID | None = None
    clear_parent: bool = False  # set true to move a tag to the root
    # Hex only: the value is interpolated into a CSS property client-side.
    color: str | None = Field(default=None, max_length=16, pattern="^#[0-9a-fA-F]{3,8}$")
    clear_color: bool = False


class ReviewReasonOut(BaseModel):
    key: str
    label: str
    severity: str  # "info" (routine, just needs filing) | "problem"
    detail: str


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    original_filename: str
    status: str
    error: str | None
    ocr_engine: str | None
    page_count: int | None
    archive_dpi: int | None = None
    size_bytes: int | None = None  # total on-disk footprint (original + archive)
    has_archive: bool = False
    has_thumbnail: bool = False
    progress: float | None = None  # 0..1 while OCR is running
    phase: str | None = None  # preparing | ocr | finishing, while running
    doc_date: date | None = None
    expires_on: date | None = None
    correspondent_id: uuid.UUID | None = None
    correspondent_name: str | None = None
    doc_type_id: uuid.UUID | None = None
    doc_type_name: str | None = None
    deleted_at: datetime | None = None
    notes: str | None = None
    custom_values: dict[str, str] = {}
    tags: list[TagOut] = []
    # Why this document wants attention, worst first. Empty for most of the
    # library; "not filed yet" is severity info, not a fault.
    review_reasons: list[ReviewReasonOut] = []
    created_at: datetime
    updated_at: datetime


class DocumentList(BaseModel):
    items: list[DocumentOut]
    total: int


class DocumentUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=1024)
    tag_ids: list[uuid.UUID] | None = None
    doc_date: date | None = None
    clear_doc_date: bool = False
    expires_on: date | None = None
    clear_expires: bool = False
    correspondent_id: uuid.UUID | None = None
    clear_correspondent: bool = False
    doc_type_id: uuid.UUID | None = None
    clear_doc_type: bool = False
    # field_id -> value; empty string removes the value
    custom_values: dict[uuid.UUID, str] | None = None
    notes: str | None = None  # empty string clears


class ReprocessRequest(BaseModel):
    mode: str = Field(default="redo", pattern="^(skip|redo|force)$")


class CopyTagsRequest(BaseModel):
    source_id: uuid.UUID  # document whose tags are mirrored onto this one


class ShareLinkCreate(BaseModel):
    days: int = Field(default=7, ge=0, le=365)  # 0 = no expiry


class ShareLinkOut(BaseModel):
    id: uuid.UUID
    token: str
    url_path: str
    expires_at: datetime | None
    created_at: datetime


class PageOpRequest(BaseModel):
    action: str = Field(pattern="^(rotate|delete|extract)$")
    pages: list[int] = Field(min_length=1, max_length=2000)
    degrees: int = 90
    title: str | None = None


class BulkActionRequest(BaseModel):
    ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)
    correspondent_id: uuid.UUID | None = None
    doc_type_id: uuid.UUID | None = None
    # Alternative to ids: act on every document carrying this tag.
    # Processed in chunks of 500; call again while `remaining` > 0.
    filter_tag_id: uuid.UUID | None = None
    # Alternative to ids: act on everything in the trash.
    filter_trash: bool = False
    action: str = Field(
        pattern="^(reprocess|delete|restore|purge|add_tags|remove_tags|set_correspondent|set_doc_type)$"
    )
    mode: str = Field(default="skip", pattern="^(skip|redo|force)$")
    tag_ids: list[uuid.UUID] = []


class BulkActionResult(BaseModel):
    processed: int
    skipped: int
    remaining: int = 0


class DeviceRegister(BaseModel):
    token: str = Field(min_length=8, max_length=255)
    platform: str = Field(default="ios", max_length=16)
    environment: str = Field(default="production", pattern="^(sandbox|production)$")


class RuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    match_type: str
    pattern: str
    tag_id: uuid.UUID | None
    set_title: str | None
    correspondent_id: uuid.UUID | None = None
    doc_type_id: uuid.UUID | None = None
    priority: int
    enabled: bool
    error: str | None = None  # set when the rule was auto-disabled


class RuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    match_type: str = Field(default="contains", pattern="^(contains|regex)$")
    pattern: str = Field(min_length=1, max_length=1024)
    tag_id: uuid.UUID | None = None
    set_title: str | None = Field(default=None, max_length=1024)
    correspondent_id: uuid.UUID | None = None
    doc_type_id: uuid.UUID | None = None
    priority: int = 100
    enabled: bool = True


class RuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    match_type: str | None = Field(default=None, pattern="^(contains|regex)$")
    pattern: str | None = Field(default=None, min_length=1, max_length=1024)
    tag_id: uuid.UUID | None = None
    set_title: str | None = Field(default=None, max_length=1024)
    correspondent_id: uuid.UUID | None = None
    doc_type_id: uuid.UUID | None = None
    priority: int | None = None
    enabled: bool | None = None


class ClassifyResult(BaseModel):
    matched_rules: list[str]
    added_tags: list[str]
    new_title: str | None
    document: DocumentOut


class BulkClassifyResult(BaseModel):
    documents_examined: int
    documents_changed: int


class FileFacet(BaseModel):
    """One stored copy of a document."""

    exists: bool = False
    size_bytes: int | None = None
    dpi: int | None = None  # None = unmeasured, 0 = no raster images
    label: str | None = None  # filename or format, whichever is meaningful


class FileDetails(BaseModel):
    """Original versus archive, and whether the archive can be improved."""

    original: FileFacet
    archive: FileFacet
    page_count: int | None = None
    ocr_engine: str | None = None
    archive_pdfa: bool | None = None
    dpi_cap: int = 0
    # True when the archive is limited by the cap rather than by the source, so
    # a rebuild would recover detail that exists. False when the source is the
    # limit and no setting can help — the distinction the panel exists for.
    can_improve: bool = False
    # The most a rebuild could usefully ask for: the original's own resolution.
    max_useful_dpi: int | None = None
    # Why a downsample could not shrink it, when that is why it sits over cap.
    downsample_note: str | None = None


class SearchResult(BaseModel):
    id: uuid.UUID
    title: str
    status: str
    snippet: str
    rank: float
    # How many pages of this document mention the query. Falls out of the
    # per-page index and says more than a score does: "28 pages" is why a
    # thousand-page encyclopedia is worth opening and a passing mention isn't.
    pages_hit: int = 0


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult]
    suggestions: list[str] = []
    # How many documents matched in total, not how many are in `results`.
    # A common word matches hundreds; without this the first page looks like
    # the whole answer, and a large book ranked past it looks absent.
    total: int = 0
    offset: int = 0


class NamedEntityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    count: int = 0


class NamedEntityCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class SavedViewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    params: str


class SavedViewCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    params: str = Field(max_length=2048)


class CustomFieldOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    kind: str


class CustomFieldCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    kind: str = Field(pattern="^(text|number|date|money|url|bool)$")
