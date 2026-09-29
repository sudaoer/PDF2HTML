from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class PaperMetadata(BaseModel):
    title: str
    creators: list[str] = Field(default_factory=list)
    date: str = ""
    publication: str = ""
    doi: str = ""
    url: str = ""
    abstract: str = ""
    tags: list[str] = Field(default_factory=list)


class SourceReference(BaseModel):
    server_id: str = ""
    library_prefix: str
    item_key: str
    item_version: int
    attachment_key: str
    attachment_version: int
    original_path: str
    sha256: str


class MinerURecord(BaseModel):
    version: str = ""
    backend: str = "hybrid-engine"
    effort: str = "medium"
    command: list[str] = Field(default_factory=list)
    content_list_path: str = ""
    middle_json_path: str = ""


class JobManifest(BaseModel):
    schema_version: int = 5
    job_id: str
    created_at: str
    updated_at: str
    status: str
    workspace: str
    metadata: PaperMetadata
    source: SourceReference
    mineru: MinerURecord = Field(default_factory=MinerURecord)
    document_path: str = ""
    layout_document_path: str = ""
    layout_preview_path: str = ""
    layout_report_path: str = ""
    layout_overrides_path: str = ""
    layout_input_hash: str = ""
    layout_document_hash: str = ""
    translation_task_dir: str = ""
    translation_dir: str = ""
    translation_reuse_report: str = ""
    bundle_path: str = ""
    bundle_revision: str = ""
    verification_report_path: str = ""
    warnings: list[str] = Field(default_factory=list)
    error: str = ""


def migrate_job_manifest(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Job manifest must be a JSON object.")
    migrated = dict(payload)
    version = int(migrated.get("schema_version", 1))
    if version > 5:
        raise ValueError(f"Unsupported job manifest schema: {version}")
    if version < 3:
        migrated.setdefault("bundle_path", "")
        migrated.setdefault("bundle_revision", "")
    migrated.pop("output_path", None)
    migrated.pop("legacy_html_path", None)
    migrated.setdefault("verification_report_path", "")
    mineru = migrated.get("mineru")
    if isinstance(mineru, dict):
        mineru.setdefault("middle_json_path", "")
    migrated["schema_version"] = 5
    return migrated


class Segment(BaseModel):
    id: str
    role: str
    source: str
    source_hash: str
    protected: list[str] = Field(default_factory=list)


class FigureMember(BaseModel):
    id: str
    asset: str
    label: str = ""
    page: int
    bbox: list[float] = Field(default_factory=list)


class DocumentBlock(BaseModel):
    id: str
    kind: str
    page: int
    level: int = 0
    subtype: str = ""
    source: str = ""
    segment_id: str = ""
    asset: str = ""
    caption_source: str = ""
    caption_segment_id: str = ""
    footnote_source: str = ""
    footnote_segment_id: str = ""
    bbox: list[float] = Field(default_factory=list)
    members: list[FigureMember] = Field(default_factory=list)
    layout: Literal["auto", "row", "column", "grid"] = "auto"
    extra: dict[str, Any] = Field(default_factory=dict)


class DocumentNote(BaseModel):
    id: str
    kind: Literal["author_note", "copyright", "endnote"]
    marker: str = ""
    page: int
    source: str
    segment_id: str = ""
    url: str = ""
    bbox: list[float] = Field(default_factory=list)


class Document(BaseModel):
    schema_version: int = 2
    job_id: str
    metadata: PaperMetadata
    source: SourceReference
    blocks: list[DocumentBlock]
    segments: list[Segment]
    front_matter_notes: list[DocumentNote] = Field(default_factory=list)
    endnotes: list[DocumentNote] = Field(default_factory=list)
    layout_info: dict[str, Any] = Field(default_factory=dict)


class LayoutTarget(BaseModel):
    id: str
    page: int
    bbox: list[float]
    block_hash: str


class LayoutOperation(BaseModel):
    op: Literal["group", "move", "reclassify", "drop"]
    targets: list[LayoutTarget]
    reason: str
    before_id: str = ""
    after_id: str = ""
    caption_from: str = ""
    layout: Literal["auto", "row", "column", "grid"] = "auto"
    kind: str = ""
    level: int | None = None


class LayoutOverrides(BaseModel):
    schema_version: Literal[1] = 1
    job_id: str
    source_sha256: str
    content_sha256: str
    operations: list[LayoutOperation] = Field(default_factory=list)


class TextCorrection(BaseModel):
    target: LayoutTarget
    field: Literal["source", "caption_source"]
    value: str
    reason: str


class TextCorrections(BaseModel):
    schema_version: Literal[1] = 1
    job_id: str
    source_sha256: str
    content_sha256: str
    corrections: list[TextCorrection] = Field(default_factory=list)


class TranslationEntry(BaseModel):
    id: str
    role: str
    source_hash: str
    source: str
    target: str | None = None


class TranslationFile(BaseModel):
    schema_version: int = 1
    job_id: str
    source_language: str = "en"
    target_language: str = "zh-CN"
    instructions: list[str] = Field(default_factory=list)
    translations: list[TranslationEntry]


class Glossary(BaseModel):
    job_id: str
    terms: dict[str, str] = Field(default_factory=dict)
