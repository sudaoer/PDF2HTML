from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any

from markdown_it import MarkdownIt
from pydantic import ValidationError

from .bundle import (
    BUNDLE_SCHEMA_VERSION,
    BundleArtifact,
    BundleMedia,
    PaperBundle,
    render_bundle_markdown,
)
from .render import _parse_equation
from .util import atomic_write_json, canonical_json_bytes, hash_file, read_json

REVISION_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
MEDIA_PATH_PATTERN = re.compile(r"^media/[0-9a-f]{64}\.(?:gif|jpg|png|webp)$")
BLOCK_PATTERN = re.compile(
    r'<!-- pdf2html:block id="([A-Za-z0-9_.:-]+)" '
    r'kind="([A-Za-z0-9_.:-]+)" page="([1-9][0-9]*)" -->'
)
END_BLOCK_PATTERN = re.compile(
    r'<!-- pdf2html:end-block id="([A-Za-z0-9_.:-]+)" -->'
)
SEGMENT_PATTERN = re.compile(
    r'<!-- pdf2html:segment id="([A-Za-z0-9_.:-]+)" '
    r'role="([A-Za-z0-9_.:-]+)" source_hash="([0-9a-f]{64})" -->'
)
ANCHOR_PATTERN = re.compile(r'<a id="block-([A-Za-z0-9_.:-]+)"></a>')
IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
SAFE_HTML_PATTERN = re.compile(
    r"^(?:<!-- pdf2html:[\s\S]*?-->|<a id=\"(?:abstract|block-[A-Za-z0-9_.:-]+)\"></a>|"
    r"</?(?:sup|sub)>|<br\s*/?>)\s*$"
)
TABLE_TAGS = {"table", "thead", "tbody", "tfoot", "tr", "th", "td"}


class _SafeTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.safe = True
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in TABLE_TAGS:
            self.safe = False
            return
        for name, value in attrs:
            if name not in {"colspan", "rowspan"} or not (value or "").isdigit():
                self.safe = False
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack.pop() != tag:
            self.safe = False

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.safe = False

    def handle_comment(self, data: str) -> None:
        self.safe = False


def _safe_raw_html(value: str) -> bool:
    if SAFE_HTML_PATTERN.fullmatch(value):
        return True
    if not value.lstrip().lower().startswith("<table"):
        return False
    parser = _SafeTableParser()
    parser.feed(value)
    parser.close()
    return parser.safe and not parser.stack


class BundleVerificationError(RuntimeError):
    """A generated Markdown bundle failed deterministic verification."""


def _sha256(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()


def _safe_member_path(paper_dir: Path, value: str) -> Path | None:
    if not value or "\\" in value or ":" in value:
        return None
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        return None
    candidate = paper_dir.joinpath(*relative.parts)
    resolved = candidate.resolve()
    try:
        resolved.relative_to(paper_dir.resolve())
    except ValueError:
        return None
    return candidate


def _validate_frontmatter(
    text: str,
    *,
    artifact: BundleArtifact,
    bundle: PaperBundle,
    errors: list[str],
) -> None:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        errors.append(f"{artifact.path} has no frontmatter opening delimiter.")
        return
    try:
        closing = lines.index("---", 1)
    except ValueError:
        errors.append(f"{artifact.path} has no frontmatter closing delimiter.")
        return
    parsed: dict[str, Any] = {}
    for line in lines[1:closing]:
        key, separator, raw = line.partition(": ")
        if not separator or key in parsed:
            errors.append(f"{artifact.path} has malformed frontmatter: {line!r}")
            continue
        try:
            parsed[key] = json.loads(raw)
        except json.JSONDecodeError:
            errors.append(f"{artifact.path} has invalid frontmatter JSON for {key}.")
    expected_language = "en" if artifact.path == "paper.en.md" else "zh-CN"
    expected = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "bundle_revision": bundle.bundle_revision,
        "paper_id": bundle.paper_id,
        "job_id": bundle.job_id,
        "language": expected_language,
        "source_sha256": bundle.document.source.sha256,
        "document_sha256": bundle.document_sha256,
    }
    for key, value in expected.items():
        if parsed.get(key) != value:
            errors.append(f"{artifact.path} frontmatter {key} does not match the bundle.")


def _validate_markdown_ast(
    text: str,
    *,
    artifact: BundleArtifact,
    bundle: PaperBundle,
    errors: list[str],
) -> None:
    block_ids = [block.id for block in bundle.document.blocks]
    marked_blocks = [match[0] for match in BLOCK_PATTERN.findall(text)]
    ended_blocks = END_BLOCK_PATTERN.findall(text)
    anchors = ANCHOR_PATTERN.findall(text)
    if marked_blocks != block_ids:
        errors.append(f"{artifact.path} block markers do not match Document order.")
    if ended_blocks != block_ids:
        errors.append(f"{artifact.path} end-block markers do not match Document order.")
    if anchors != block_ids:
        errors.append(f"{artifact.path} block anchors do not match Document order.")

    expected_segments = {segment.id: segment for segment in bundle.document.segments}
    marked_segments = SEGMENT_PATTERN.findall(text)
    if len(marked_segments) != len(expected_segments):
        errors.append(f"{artifact.path} does not contain exactly one marker per segment.")
    for segment_id, role, source_hash in marked_segments:
        segment = expected_segments.get(segment_id)
        if segment is None:
            errors.append(f"{artifact.path} contains unknown segment {segment_id}.")
        elif (segment.role, segment.source_hash) != (role, source_hash):
            errors.append(f"{artifact.path} segment marker changed for {segment_id}.")
    if len({item[0] for item in marked_segments}) != len(marked_segments):
        errors.append(f"{artifact.path} contains duplicate segment markers.")

    allowed_media = {item.path for item in bundle.media}
    referenced_media = set(IMAGE_PATTERN.findall(text))
    if not referenced_media.issubset(allowed_media):
        errors.append(f"{artifact.path} references media outside the bundle manifest.")
    expected_references = {
        item.path
        for item in bundle.media
        if any(
            item.asset == asset
            for block in bundle.document.blocks
            for asset in [block.asset, *(member.asset for member in block.members)]
        )
    }
    if referenced_media != expected_references:
        errors.append(f"{artifact.path} does not reference every document media artifact.")

    parser = MarkdownIt("commonmark", {"html": True, "linkify": False})
    parser.enable("table")
    for token in parser.parse(text):
        if token.type in {"html_block", "html_inline"} and not _safe_raw_html(token.content):
            errors.append(f"{artifact.path} contains unsafe raw HTML in its Markdown AST.")
            break


def _validate_media(
    paper_dir: Path,
    media: list[BundleMedia],
    errors: list[str],
) -> None:
    expected_files: set[str] = set()
    records_by_path: dict[str, tuple[str, int, str]] = {}
    for item in media:
        if not MEDIA_PATH_PATTERN.fullmatch(item.path):
            errors.append(f"Invalid bundle media path: {item.path}")
            continue
        filename_digest = PurePosixPath(item.path).stem
        if filename_digest != item.sha256:
            errors.append(f"Media filename hash does not match its record: {item.path}")
        candidate = _safe_member_path(paper_dir, item.path)
        if candidate is None:
            errors.append(f"Unsafe bundle media path: {item.path}")
            continue
        expected_files.add(item.path)
        record = (item.sha256, item.size, item.media_type)
        previous = records_by_path.setdefault(item.path, record)
        if previous != record:
            errors.append(f"Conflicting bundle media records: {item.path}")
        if not candidate.is_file() or candidate.is_symlink():
            errors.append(f"Bundle media is missing or a symlink: {item.path}")
            continue
        if candidate.stat().st_size != item.size or hash_file(candidate) != item.sha256:
            errors.append(f"Bundle media hash or size changed: {item.path}")
    media_dir = paper_dir / "media"
    actual_files = (
        {path.relative_to(paper_dir).as_posix() for path in media_dir.iterdir() if path.is_file()}
        if media_dir.is_dir()
        else set()
    )
    if actual_files != expected_files:
        errors.append("Bundle media directory does not exactly match paper.json.")


def _validate_document(bundle: PaperBundle, errors: list[str]) -> None:
    document = bundle.document
    block_ids = [block.id for block in document.blocks]
    segment_ids = [segment.id for segment in document.segments]
    note_ids = [note.id for note in [*document.front_matter_notes, *document.endnotes]]
    if len(block_ids) != len(set(block_ids)):
        errors.append("Document block IDs are not unique.")
    if len(segment_ids) != len(set(segment_ids)):
        errors.append("Document segment IDs are not unique.")
    if len(note_ids) != len(set(note_ids)):
        errors.append("Document note IDs are not unique.")
    referenced_segments: list[str] = []
    member_ids: list[str] = []
    for block in document.blocks:
        referenced_segments.extend(
            value
            for value in (
                block.segment_id,
                block.caption_segment_id,
                block.footnote_segment_id,
            )
            if value
        )
        if block.kind == "figure" and not block.asset:
            errors.append(f"Figure {block.id} has no media asset.")
        if block.kind == "figure_group":
            labels = [member.label.casefold() for member in block.members]
            expected = [f"({chr(ord('a') + index)})" for index in range(len(labels))]
            if len(block.members) < 2 or not block.caption_source.strip():
                errors.append(f"Figure group {block.id} is incomplete.")
            if labels != expected or len(labels) != len(set(labels)):
                errors.append(f"Figure group {block.id} has invalid member labels.")
            if any(not member.asset or member.page != block.page for member in block.members):
                errors.append(f"Figure group {block.id} has invalid media members.")
            member_ids.extend(member.id for member in block.members)
    referenced_segments.extend(
        note.segment_id
        for note in [*document.front_matter_notes, *document.endnotes]
        if note.segment_id
    )
    referenced_segments.extend(
        segment_id
        for segment_id in ("meta.title", "meta.abstract")
        if segment_id in set(segment_ids)
    )
    if len(member_ids) != len(set(member_ids)):
        errors.append("Figure member IDs are not unique.")
    if sorted(referenced_segments) != sorted(segment_ids):
        errors.append("Document segment references are incomplete or duplicated.")


def verify_bundle(
    bundle_path: Path,
    report_path: Path | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    try:
        bundle = PaperBundle.model_validate(read_json(bundle_path))
    except (OSError, ValueError, ValidationError) as exc:
        raise BundleVerificationError(f"Invalid paper.json: {exc}") from exc
    paper_dir = bundle_path.parent
    if bundle_path.name != "paper.json" or paper_dir.name != bundle.paper_id:
        errors.append("paper.json is not stored under its declared paper ID.")
    if not REVISION_PATTERN.fullmatch(bundle.bundle_revision):
        errors.append("Bundle revision is not a SHA-256 revision.")

    document_payload = bundle.document.model_dump(mode="json")
    translation_payload = [item.model_dump(mode="json") for item in bundle.translations]
    if _sha256(canonical_json_bytes(document_payload)) != bundle.document_sha256:
        errors.append("Document hash does not match paper.json.")
    if _sha256(canonical_json_bytes(translation_payload)) != bundle.translations_sha256:
        errors.append("Translation hash does not match paper.json.")
    revision_seed = {
        "schema_version": bundle.schema_version,
        "document": document_payload,
        "translations": translation_payload,
        "media": [item.model_dump(mode="json") for item in bundle.media],
    }
    expected_revision = f"sha256:{_sha256(canonical_json_bytes(revision_seed))}"
    if bundle.bundle_revision != expected_revision:
        errors.append("Bundle revision does not match its canonical content.")

    _validate_document(bundle, errors)

    segments = bundle.document.segments
    if [entry.id for entry in bundle.translations] != [segment.id for segment in segments]:
        errors.append("Bundle translations do not match Document segment order.")
    for segment, translation in zip(segments, bundle.translations, strict=False):
        if translation.source_hash != segment.source_hash:
            errors.append(f"Translation source hash changed for {segment.id}.")
        if not translation.target.strip():
            errors.append(f"Translation target is empty for {segment.id}.")
        for token in segment.protected:
            if translation.target.count(token) != segment.source.count(token):
                errors.append(f"Translation changed protected token counts for {segment.id}.")
    for block in bundle.document.blocks:
        if block.kind == "equation" and _parse_equation(block.source).invalid_tag:
            errors.append(f"Equation {block.id} has an invalid tag command.")

    _validate_media(paper_dir, bundle.media, errors)
    translations = {entry.id: entry.target for entry in bundle.translations}
    mapping = {item.asset: item.path for item in bundle.media}
    artifacts_by_path = {artifact.path: artifact for artifact in bundle.artifacts}
    if len(bundle.artifacts) != 2 or set(artifacts_by_path) != {
        "paper.en.md",
        "paper.zh-CN.md",
    }:
        errors.append("Bundle must declare exactly the two Markdown artifacts.")
    for name, language in (("paper.en.md", "en"), ("paper.zh-CN.md", "zh-CN")):
        artifact = artifacts_by_path.get(name)
        if artifact is None:
            continue
        candidate = _safe_member_path(paper_dir, name)
        if candidate is None or not candidate.is_file() or candidate.is_symlink():
            errors.append(f"Bundle artifact is missing or unsafe: {name}")
            continue
        content = candidate.read_bytes()
        if len(content) != artifact.size or _sha256(content) != artifact.sha256:
            errors.append(f"Bundle artifact hash or size changed: {name}")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            errors.append(f"Bundle artifact is not UTF-8: {name}")
            continue
        _validate_frontmatter(text, artifact=artifact, bundle=bundle, errors=errors)
        _validate_markdown_ast(text, artifact=artifact, bundle=bundle, errors=errors)
        expected = render_bundle_markdown(
            bundle.document,
            translations,
            language=language,  # type: ignore[arg-type]
            paper_id=bundle.paper_id,
            revision=bundle.bundle_revision,
            document_sha256=bundle.document_sha256,
            media_mapping=mapping,
        )
        if text != expected:
            errors.append(f"{name} is not the canonical rendering of paper.json.")

    report: dict[str, Any] = {
        "job_id": bundle.job_id,
        "paper_id": bundle.paper_id,
        "bundle_revision": bundle.bundle_revision,
        "status": "failed" if errors else "passed",
        "blocks": len(bundle.document.blocks),
        "segments": len(bundle.document.segments),
        "translations": len(bundle.translations),
        "artifacts": len(bundle.artifacts),
        "media": len({item.path for item in bundle.media}),
        "errors": errors,
    }
    if report_path is not None:
        atomic_write_json(report_path, report)
    if errors:
        raise BundleVerificationError("; ".join(errors))
    return report
