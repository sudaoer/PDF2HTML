from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .headings import is_front_matter_duplicate
from .models import Document, Segment
from .shelf import read_shelf_metadata, record_added_at
from .util import atomic_write_bytes, canonical_json_bytes, slugify

BUNDLE_SCHEMA_VERSION = 1
MEDIA_TYPES = {
    ".gif": "image/gif",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
SAFE_ATTRIBUTE = re.compile(r"^[A-Za-z0-9_.:-]+$")


class BundleError(RuntimeError):
    """The locked document cannot be emitted as a safe Markdown bundle."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BundleLanguages(StrictModel):
    source: Literal["en"] = "en"
    target: Literal["zh-CN"] = "zh-CN"


class BundleTranslation(StrictModel):
    id: str
    source_hash: str
    target: str


class BundleArtifact(StrictModel):
    path: Literal["paper.en.md", "paper.zh-CN.md"]
    sha256: str
    size: int
    media_type: str = "text/markdown; charset=utf-8"


class BundleMedia(StrictModel):
    asset: str
    path: str
    sha256: str
    size: int
    media_type: str


class PaperBundle(StrictModel):
    schema_version: Literal[1] = BUNDLE_SCHEMA_VERSION
    bundle_revision: str
    paper_id: str
    job_id: str
    languages: BundleLanguages = Field(default_factory=BundleLanguages)
    document_sha256: str
    translations_sha256: str
    document: Document
    translations: list[BundleTranslation]
    artifacts: list[BundleArtifact]
    media: list[BundleMedia]


@dataclass(frozen=True)
class BundleBuildResult:
    paper_dir: Path
    bundle_path: Path
    paper_id: str
    revision: str


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _attribute(value: str, *, label: str) -> str:
    if not SAFE_ATTRIBUTE.fullmatch(value):
        raise BundleError(f"Unsafe {label}: {value!r}")
    return html.escape(value, quote=True)


def _asset_candidate(asset: str, base: Path) -> tuple[Path, str, str]:
    if not asset or "\\" in asset or ":" in asset:
        raise BundleError(f"Unsafe MinerU media path: {asset!r}")
    relative = PurePosixPath(asset)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise BundleError(f"Unsafe MinerU media path: {asset!r}")
    base_resolved = base.resolve()
    candidate = base.joinpath(*relative.parts)
    resolved = candidate.resolve()
    try:
        resolved.relative_to(base_resolved)
    except ValueError as exc:
        raise BundleError(f"MinerU media escapes its content root: {asset!r}") from exc
    if not resolved.is_file():
        raise BundleError(f"MinerU media is missing: {asset}")
    extension = resolved.suffix.lower()
    if extension == ".jpeg":
        extension = ".jpg"
    media_type = MEDIA_TYPES.get(extension)
    if media_type is None:
        raise BundleError(f"Unsupported MinerU media type: {asset}")
    return resolved, extension, media_type


def _document_media(
    document: Document,
    content_list_path: Path,
) -> tuple[list[BundleMedia], dict[str, str], dict[str, bytes]]:
    assets: list[str] = []
    for block in document.blocks:
        if block.asset:
            assets.append(block.asset)
        assets.extend(member.asset for member in block.members if member.asset)

    media: list[BundleMedia] = []
    mapping: dict[str, str] = {}
    files: dict[str, bytes] = {}
    for asset in sorted(set(assets)):
        candidate, extension, media_type = _asset_candidate(asset, content_list_path.parent)
        content = candidate.read_bytes()
        digest = _sha256(content)
        relative_path = f"media/{digest}{extension}"
        mapping[asset] = relative_path
        files.setdefault(relative_path, content)
        media.append(
            BundleMedia(
                asset=asset,
                path=relative_path,
                sha256=digest,
                size=len(content),
                media_type=media_type,
            )
        )
    media.sort(key=lambda item: (item.path, item.asset))
    return media, mapping, files


def _segment_marker(segment: Segment | None) -> str:
    if segment is None:
        return ""
    return (
        f'<!-- pdf2html:segment id="{_attribute(segment.id, label="segment ID")}"'
        f' role="{_attribute(segment.role, label="segment role")}"'
        f' source_hash="{_attribute(segment.source_hash, label="source hash")}" -->\n'
    )


def _frontmatter(
    document: Document,
    *,
    language: str,
    title: str,
    paper_id: str,
    revision: str,
    document_sha256: str,
) -> str:
    values: list[tuple[str, Any]] = [
        ("schema_version", BUNDLE_SCHEMA_VERSION),
        ("bundle_revision", revision),
        ("paper_id", paper_id),
        ("job_id", document.job_id),
        ("language", language),
        ("title", title),
        ("creators", document.metadata.creators),
        ("date", document.metadata.date),
        ("publication", document.metadata.publication),
        ("doi", document.metadata.doi),
        ("source_sha256", document.source.sha256),
        ("document_sha256", document_sha256),
    ]
    lines = ["---"]
    for key, value in values:
        rendered = canonical_json_bytes(value).decode("utf-8").rstrip("\n")
        lines.append(f"{key}: {rendered}")
    lines.extend(("---", ""))
    return "\n".join(lines)


def _translated(
    source: str,
    segment_id: str,
    *,
    language: str,
    translations: dict[str, str],
) -> str:
    if language == "zh-CN" and segment_id:
        return translations[segment_id]
    return source


def render_bundle_markdown(
    document: Document,
    translations: dict[str, str],
    *,
    language: Literal["en", "zh-CN"],
    paper_id: str,
    revision: str,
    document_sha256: str,
    media_mapping: dict[str, str],
) -> str:
    segments = {segment.id: segment for segment in document.segments}
    title = _translated(
        document.metadata.title,
        "meta.title",
        language=language,
        translations=translations,
    )
    chunks = [
        _frontmatter(
            document,
            language=language,
            title=title,
            paper_id=paper_id,
            revision=revision,
            document_sha256=document_sha256,
        ),
        _segment_marker(segments.get("meta.title")),
        f"# {title}\n\n",
    ]
    if document.metadata.abstract:
        abstract = _translated(
            document.metadata.abstract,
            "meta.abstract",
            language=language,
            translations=translations,
        )
        chunks.extend(
            (
                '<a id="abstract"></a>\n',
                _segment_marker(segments.get("meta.abstract")),
                f"## {'摘要' if language == 'zh-CN' else 'Abstract'}\n\n{abstract}\n\n",
            )
        )

    for note in document.front_matter_notes:
        value = _translated(
            note.source,
            note.segment_id,
            language=language,
            translations=translations,
        )
        chunks.extend(
            (
                '<!-- pdf2html:note '
                f'id="{_attribute(note.id, label="note ID")}" '
                f'kind="{note.kind}" page="{note.page + 1}" -->\n',
                _segment_marker(segments.get(note.segment_id)),
                f"{value}\n\n",
            )
        )

    for block in document.blocks:
        if is_front_matter_duplicate(block):
            continue
        block_id = _attribute(block.id, label="block ID")
        kind = _attribute(block.kind, label="block kind")
        chunks.extend(
            (
                f'<!-- pdf2html:block id="{block_id}" kind="{kind}" page="{block.page + 1}" -->\n',
                f'<a id="block-{block_id}"></a>\n',
                _segment_marker(segments.get(block.segment_id)),
            )
        )
        value = _translated(
            block.source,
            block.segment_id,
            language=language,
            translations=translations,
        )
        if block.kind == "heading":
            chunks.append(f"{'#' * min(max(block.level, 1), 6)} {value}\n\n")
        elif block.kind == "equation":
            chunks.append(f"$$\n{block.source}\n$$\n\n")
        elif block.kind == "code":
            chunks.append(f"````text\n{block.source}\n````\n\n")
        elif block.kind == "figure_group":
            for member in block.members:
                chunks.append(
                    f"![{member.label or member.id}]({media_mapping[member.asset]})\n\n"
                )
        elif block.kind in {"figure", "table"} and block.asset:
            chunks.append(f"![{block.id}]({media_mapping[block.asset]})\n\n")
            if block.kind == "table" and value:
                chunks.append(f"{value}\n\n")
        elif value:
            chunks.append(f"{value}\n\n")

        if block.caption_source:
            chunks.append(_segment_marker(segments.get(block.caption_segment_id)))
            caption = _translated(
                block.caption_source,
                block.caption_segment_id,
                language=language,
                translations=translations,
            )
            chunks.append(f"{caption}\n\n")
        if block.footnote_source:
            chunks.append(_segment_marker(segments.get(block.footnote_segment_id)))
            footnote = _translated(
                block.footnote_source,
                block.footnote_segment_id,
                language=language,
                translations=translations,
            )
            chunks.append(f"{footnote}\n\n")
        chunks.append(f'<!-- pdf2html:end-block id="{block_id}" -->\n\n')

    if document.endnotes:
        chunks.append("## Notes\n\n")
    for note in document.endnotes:
        value = note.url or _translated(
            note.source,
            note.segment_id,
            language=language,
            translations=translations,
        )
        chunks.extend(
            (
                '<!-- pdf2html:note '
                f'id="{_attribute(note.id, label="note ID")}" '
                f'kind="{note.kind}" page="{note.page + 1}" -->\n',
                _segment_marker(segments.get(note.segment_id)),
                f"{note.marker}. {value}\n\n",
            )
        )
    return "".join(chunks).rstrip() + "\n"


def build_bundle(
    document: Document,
    translations: dict[str, str],
    *,
    content_list_path: Path,
    library_root: Path,
) -> BundleBuildResult:
    paper_id = f"{slugify(document.metadata.title)}-{document.source.item_key.lower()}"
    read_shelf_metadata(library_root, paper_id)
    paper_dir = library_root / "papers" / paper_id
    first_build = not (paper_dir / "paper.json").exists()
    media, mapping, media_files = _document_media(document, content_list_path)

    document_payload = document.model_dump(mode="json")
    document_bytes = canonical_json_bytes(document_payload)
    translation_items = [
        BundleTranslation(
            id=segment.id,
            source_hash=segment.source_hash,
            target=translations[segment.id],
        )
        for segment in document.segments
    ]
    translations_payload = [item.model_dump(mode="json") for item in translation_items]
    translations_bytes = canonical_json_bytes(translations_payload)
    document_sha256 = _sha256(document_bytes)
    translations_sha256 = _sha256(translations_bytes)
    revision_seed = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "document": document_payload,
        "translations": translations_payload,
        "media": [item.model_dump(mode="json") for item in media],
    }
    revision = f"sha256:{_sha256(canonical_json_bytes(revision_seed))}"

    english = render_bundle_markdown(
        document,
        translations,
        language="en",
        paper_id=paper_id,
        revision=revision,
        document_sha256=document_sha256,
        media_mapping=mapping,
    )
    chinese = render_bundle_markdown(
        document,
        translations,
        language="zh-CN",
        paper_id=paper_id,
        revision=revision,
        document_sha256=document_sha256,
        media_mapping=mapping,
    )
    markdown: dict[Literal["paper.en.md", "paper.zh-CN.md"], bytes] = {
        "paper.en.md": english.encode("utf-8"),
        "paper.zh-CN.md": chinese.encode("utf-8"),
    }

    artifacts = [
        BundleArtifact(
            path=name,
            sha256=_sha256(content),
            size=len(content),
        )
        for name, content in sorted(markdown.items())
    ]
    bundle = PaperBundle(
        bundle_revision=revision,
        paper_id=paper_id,
        job_id=document.job_id,
        document_sha256=document_sha256,
        translations_sha256=translations_sha256,
        document=document,
        translations=translation_items,
        artifacts=artifacts,
        media=media,
    )

    paper_dir.mkdir(parents=True, exist_ok=True)
    media_dir = paper_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    desired_names = {PurePosixPath(path).name for path in media_files}
    for existing in media_dir.iterdir():
        if existing.is_file() and existing.name not in desired_names:
            existing.unlink()
    for relative_path, content in media_files.items():
        atomic_write_bytes(paper_dir / PurePosixPath(relative_path), content)
    for name, content in markdown.items():
        atomic_write_bytes(paper_dir / name, content)
    bundle_path = paper_dir / "paper.json"
    atomic_write_bytes(
        bundle_path,
        canonical_json_bytes(bundle.model_dump(mode="json"), indent=2),
    )
    cleanup_legacy_library(library_root)
    if first_build:
        record_added_at(library_root, paper_id)
    return BundleBuildResult(
        paper_dir=paper_dir,
        bundle_path=bundle_path,
        paper_id=paper_id,
        revision=revision,
    )


def cleanup_legacy_library(library_root: Path) -> list[Path]:
    root = library_root.resolve()
    candidates = [
        root / "index.html",
        root / "assets" / "app.css",
        root / "assets" / "app.js",
    ]
    papers_root = root / "papers"
    if papers_root.is_dir() and not papers_root.is_symlink():
        candidates.extend(
            paper_dir / "index.html"
            for paper_dir in papers_root.iterdir()
            if paper_dir.is_dir() and not paper_dir.is_symlink()
        )
    removed: list[Path] = []
    for candidate in candidates:
        try:
            candidate.resolve().relative_to(root)
        except ValueError as exc:
            raise BundleError(f"Refusing to clean a legacy path outside {root}.") from exc
        if candidate.is_file() or candidate.is_symlink():
            candidate.unlink()
            removed.append(candidate)
    assets = root / "assets"
    if assets.is_dir() and not any(assets.iterdir()):
        assets.rmdir()
    return removed


def bundle_shelf_record(payload: dict[str, Any]) -> dict[str, Any]:
    if "document" not in payload:
        return payload
    bundle = PaperBundle.model_validate(payload)
    translations = {item.id: item.target for item in bundle.translations}
    metadata = bundle.document.metadata
    return {
        "paper_id": bundle.paper_id,
        "job_id": bundle.job_id,
        "item_key": bundle.document.source.item_key,
        "title": metadata.title,
        "title_zh": translations.get("meta.title", metadata.title),
        "creators": metadata.creators,
        "date": metadata.date,
        "publication": metadata.publication,
        "doi": metadata.doi,
        "tags": metadata.tags,
        "href": f"/papers/{bundle.paper_id}/",
    }
