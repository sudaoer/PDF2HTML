from __future__ import annotations

from typing import Any

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


class JobManifest(BaseModel):
    schema_version: int = 1
    job_id: str
    created_at: str
    updated_at: str
    status: str
    workspace: str
    metadata: PaperMetadata
    source: SourceReference
    mineru: MinerURecord = Field(default_factory=MinerURecord)
    document_path: str = ""
    translation_task_dir: str = ""
    translation_dir: str = ""
    output_path: str = ""
    warnings: list[str] = Field(default_factory=list)
    error: str = ""


class Segment(BaseModel):
    id: str
    role: str
    source: str
    source_hash: str
    protected: list[str] = Field(default_factory=list)


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
    extra: dict[str, Any] = Field(default_factory=dict)


class Document(BaseModel):
    schema_version: int = 1
    job_id: str
    metadata: PaperMetadata
    source: SourceReference
    blocks: list[DocumentBlock]
    segments: list[Segment]


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
