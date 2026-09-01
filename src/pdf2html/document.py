from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

from .headings import is_front_matter_duplicate, normalize_heading_structure
from .layout import SUBFIGURE_PREFIX, apply_layout_overrides, block_fingerprint
from .models import (
    Document,
    DocumentBlock,
    DocumentNote,
    FigureMember,
    LayoutOverrides,
    PaperMetadata,
    Segment,
    SourceReference,
    TranslationEntry,
    TranslationFile,
)
from .util import atomic_write_json, hash_text, read_json

PROTECTED_PATTERNS = [
    re.compile(r"\$\$.*?\$\$", re.DOTALL),
    re.compile(r"(?<!\\)\$(?!\s).*?(?<!\s)(?<!\\)\$", re.DOTALL),
    re.compile(r"\\\[.*?\\\]", re.DOTALL),
    re.compile(r"\\\(.*?\\\)", re.DOTALL),
    re.compile(r"\[(?:\d+[a-z]?\s*[,;\-]?\s*)+\]"),
    re.compile(r"https?://[^\s)>]+"),
    re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE),
    re.compile(r"<sup>[^<]+</sup>", re.IGNORECASE),
]

SKIPPED_TYPES = {"header", "footer", "page_number"}


def _string(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(str(part).strip() for part in value if str(part).strip())
    return ""


def _protected_tokens(text: str) -> list[str]:
    matches: list[tuple[int, str]] = []
    for pattern in PROTECTED_PATTERNS:
        matches.extend((match.start(), match.group(0)) for match in pattern.finditer(text))
    seen: set[str] = set()
    ordered: list[str] = []
    for _, token in sorted(matches, key=lambda pair: pair[0]):
        if token not in seen:
            ordered.append(token)
            seen.add(token)
    return ordered


def _rich_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_rich_text(part) for part in value)
    if not isinstance(value, dict):
        return ""
    kind = str(value.get("type") or value.get("item_type") or "")
    if kind.startswith("equation"):
        latex = _string(value.get("content") or value.get("math_content"))
        return f"${latex}$" if latex else ""
    for key in ("content", "item_content", "paragraph_content", "title_content"):
        if key in value:
            return _rich_text(value[key])
    return ""


def _image_path(content: dict[str, Any]) -> str:
    source = content.get("image_source")
    if isinstance(source, dict):
        return _string(source.get("path"))
    return ""


def _normalize_v2_entry(raw: dict[str, Any], page_index: int) -> dict[str, Any]:
    kind = str(raw.get("type") or "")
    content_raw = raw.get("content")
    content = cast(dict[str, Any], content_raw) if isinstance(content_raw, dict) else {}
    normalized: dict[str, Any] = {
        "type": kind,
        "page_idx": page_index,
        "bbox": raw.get("bbox", []),
    }
    if kind == "title":
        normalized.update(
            type="text",
            text=_rich_text(content.get("title_content")),
            text_level=int(content.get("level") or 1),
        )
    elif kind == "paragraph":
        normalized.update(type="text", text=_rich_text(content.get("paragraph_content")))
    elif kind == "equation_interline":
        normalized.update(type="equation", text=_string(content.get("math_content")))
    elif kind in {"image", "chart"}:
        caption_key = "chart_caption" if kind == "chart" else "image_caption"
        footnote_key = "chart_footnote" if kind == "chart" else "image_footnote"
        normalized.update(
            img_path=_image_path(content),
            caption=_rich_text(content.get(caption_key)),
            footnote=_rich_text(content.get(footnote_key)),
        )
    elif kind == "table":
        normalized.update(
            table_body=_string(content.get("html") or content.get("content")),
            table_caption=_rich_text(content.get("table_caption")),
            table_footnote=_rich_text(content.get("table_footnote")),
            img_path=_image_path(content),
        )
    elif kind == "list":
        items: list[str] = []
        raw_items = content.get("list_items")
        if isinstance(raw_items, list):
            items = [_rich_text(item) for item in raw_items if _rich_text(item).strip()]
        normalized.update(
            list_items=items,
            sub_type="ref_text"
            if content.get("list_type") == "reference_list"
            else "text",
        )
    elif kind == "page_header":
        normalized.update(type="header", text=_rich_text(content.get("page_header_content")))
    elif kind == "page_footer":
        normalized.update(type="footer", text=_rich_text(content.get("page_footer_content")))
    elif kind == "page_number":
        normalized.update(text=_rich_text(content.get("page_number_content")))
    elif kind == "page_footnote":
        normalized.update(text=_rich_text(content.get("page_footnote_content")))
    else:
        normalized["text"] = _rich_text(content)
    return normalized


def _repair_adjacent_captions(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for index in range(len(entries) - 1):
        current = entries[index]
        following = entries[index + 1]
        if current.get("type") != "table" or current.get("table_caption"):
            continue
        if following.get("type") == "table" and following.get("table_caption"):
            current["table_caption"] = following["table_caption"]
            following["table_caption"] = ""
            continue
        if following.get("type") not in {"image", "chart"}:
            continue
        caption = _string(following.get("caption"))
        figure_match = re.search(r"(?=Figure\s+\d+\s*:)", caption)
        if caption.startswith("Table ") and figure_match:
            current["table_caption"] = caption[: figure_match.start()].strip()
            following["caption"] = caption[figure_match.start() :].strip()
    return entries


FLOAT_KINDS = {"figure", "figure_group", "table"}
TERMINAL_PUNCTUATION = re.compile(r"[.!?。！？:：;；]['\"”’)]*$")


def _valid_bbox(block: DocumentBlock) -> bool:
    return len(block.bbox) == 4 and block.bbox[0] < block.bbox[2] and block.bbox[1] < block.bbox[3]


def _page_bottoms(blocks: list[DocumentBlock]) -> dict[int, float]:
    bottoms: dict[int, float] = {}
    for block in blocks:
        if _valid_bbox(block):
            bottoms[block.page] = max(bottoms.get(block.page, block.bbox[3]), block.bbox[3])
    return bottoms


def _same_page_column_transition(before: DocumentBlock, after: DocumentBlock) -> bool:
    """Return whether reading order reset from a left column to a right column."""
    if before.page != after.page or not _valid_bbox(before) or not _valid_bbox(after):
        return False
    return before.bbox[2] <= after.bbox[0] and before.bbox[1] > after.bbox[1]


def _next_page_transition(
    before: DocumentBlock,
    after: DocumentBlock,
    *,
    page_bottoms: dict[int, float],
) -> bool:
    """Return whether a paragraph tail reaches the next physical PDF page."""
    if after.page != before.page + 1 or not _valid_bbox(before) or not _valid_bbox(after):
        return False
    page_bottom = page_bottoms.get(before.page, before.bbox[3])
    page_height = max(page_bottom, 1.0)
    bottom_tolerance = max(12.0, page_height * 0.05)
    return page_bottom - before.bbox[3] <= bottom_tolerance


def _is_paragraph_continuation(
    before: DocumentBlock,
    after: DocumentBlock,
    *,
    intervening_floats: list[DocumentBlock],
    page_bottoms: dict[int, float],
) -> bool:
    if before.kind != "paragraph" or after.kind != "paragraph":
        return False
    if not before.source.strip() or TERMINAL_PUNCTUATION.search(before.source.strip()):
        return False
    if before.page == after.page:
        return bool(intervening_floats) or _same_page_column_transition(before, after)
    return _next_page_transition(before, after, page_bottoms=page_bottoms)


def _merge_boundary_paragraphs(
    blocks: list[DocumentBlock],
) -> tuple[list[DocumentBlock], int]:
    """Merge sentence fragments split by a float, column, or page boundary.

    MinerU already provides reading order.  We only join a non-terminated paragraph
    to the next paragraph when geometry proves a column/page transition, or when
    only floating figures/tables interrupt the two fragments.  Structural blocks
    such as headings and equations therefore remain hard boundaries.
    """
    page_bottoms = _page_bottoms(blocks)
    merged_blocks: list[DocumentBlock] = []
    merge_groups = 0
    index = 0
    while index < len(blocks):
        first = blocks[index]
        if first.kind != "paragraph":
            merged_blocks.append(first)
            index += 1
            continue

        merged = first.model_copy(deep=True)
        tail = first
        origin_blocks = [first]
        deferred_floats: list[DocumentBlock] = []
        cursor = index + 1
        while cursor < len(blocks):
            float_end = cursor
            while float_end < len(blocks) and blocks[float_end].kind in FLOAT_KINDS:
                float_end += 1
            if float_end >= len(blocks):
                break
            continuation = blocks[float_end]
            floats = blocks[cursor:float_end]
            if not _is_paragraph_continuation(
                tail,
                continuation,
                intervening_floats=floats,
                page_bottoms=page_bottoms,
            ):
                break
            merged.source = f"{merged.source.rstrip()} {continuation.source.lstrip()}"
            origin_blocks.append(continuation)
            deferred_floats.extend(floats)
            tail = continuation
            cursor = float_end + 1

        if len(origin_blocks) == 1:
            merged_blocks.append(first)
            index += 1
            continue

        merged.extra.update(
            {
                "automatic_paragraph_merge": True,
                "origin_block_ids": [block.id for block in origin_blocks],
                "origin_pages": [block.page for block in origin_blocks],
                "origin_bboxes": [block.bbox for block in origin_blocks],
            }
        )
        merged_blocks.append(merged)
        merged_blocks.extend(deferred_floats)
        merge_groups += 1
        index = cursor
    return merged_blocks, merge_groups


def _content_entries(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        if payload and all(isinstance(page, list) for page in payload):
            entries = [
                _normalize_v2_entry(cast(dict[str, Any], entry), page_index)
                for page_index, page in enumerate(payload)
                for entry in cast(list[Any], page)
                if isinstance(entry, dict)
            ]
            return _repair_adjacent_captions(entries)
        return [cast(dict[str, Any], entry) for entry in payload if isinstance(entry, dict)]
    if not isinstance(payload, dict):
        raise ValueError("MinerU content list must be a list or object.")
    for key in ("content_list", "blocks", "items"):
        entries = payload.get(key)
        if isinstance(entries, list):
            return [cast(dict[str, Any], entry) for entry in entries if isinstance(entry, dict)]
    pages = payload.get("pages")
    if isinstance(pages, list):
        flattened: list[dict[str, Any]] = []
        for page_index, page in enumerate(pages):
            if not isinstance(page, dict):
                continue
            blocks = page.get("blocks") or page.get("content_list") or []
            if isinstance(blocks, list):
                for raw in blocks:
                    if isinstance(raw, dict):
                        entry = cast(dict[str, Any], dict(raw))
                        entry.setdefault("page_idx", page_index)
                        flattened.append(entry)
        if flattened:
            return flattened
    raise ValueError("Unsupported MinerU content-list structure.")


def build_document(
    *,
    job_id: str,
    metadata: PaperMetadata,
    source: SourceReference,
    content_list_path: Path,
    middle_json_path: Path | None = None,
    content_sha256: str = "",
    overrides: LayoutOverrides | None = None,
) -> Document:
    entries = _content_entries(read_json(content_list_path))
    primitive_blocks = _primitive_blocks(entries)
    heading_blocks, heading_info = normalize_heading_structure(
        primitive_blocks,
        metadata=metadata,
        middle_json_path=middle_json_path,
    )
    nonempty_blocks, empty_count = _drop_empty_blocks(heading_blocks)
    grouped_blocks, group_count = _group_adjacent_figures(nonempty_blocks)
    override_targets = [
        {
            "id": block.id,
            "kind": block.kind,
            "page": block.page,
            "level": block.level,
            "bbox": block.bbox,
            "block_hash": block_fingerprint(block),
        }
        for block in grouped_blocks
    ]
    overridden_blocks, override_count = apply_layout_overrides(grouped_blocks, overrides)
    body_blocks, front_notes, endnotes = _extract_notes(overridden_blocks)
    body_blocks, paragraph_merge_count = _merge_boundary_paragraphs(body_blocks)

    blocks, front_notes, endnotes, segments = _assign_segments(
        body_blocks,
        front_notes,
        endnotes,
        metadata=metadata,
    )
    return Document(
        job_id=job_id,
        metadata=metadata,
        source=source,
        blocks=blocks,
        segments=segments,
        front_matter_notes=front_notes,
        endnotes=endnotes,
        layout_info={
            "content_sha256": content_sha256,
            "automatic_figure_groups": group_count,
            "automatic_paragraph_merges": paragraph_merge_count,
            "dropped_empty_blocks": empty_count,
            "overrides_applied": override_count,
            "override_targets": override_targets,
            **heading_info,
        },
    )


def _primitive_blocks(entries: list[dict[str, Any]]) -> list[DocumentBlock]:
    blocks: list[DocumentBlock] = []
    for index, raw in enumerate(entries, start=1):
        block_id = f"b{index:04d}"
        kind = str(raw.get("type") or raw.get("block_type") or "text").lower()
        subtype = str(raw.get("sub_type") or raw.get("subtype") or "")
        if kind in SKIPPED_TYPES:
            continue
        page = int(raw.get("page_idx") or raw.get("page") or 0)
        bbox_raw = raw.get("bbox")
        bbox = [float(value) for value in bbox_raw] if isinstance(bbox_raw, list) else []
        level = int(raw.get("text_level") or raw.get("level") or 0)
        source_text = _string(
            raw.get("text")
            or raw.get("content")
            or raw.get("code_body")
            or raw.get("latex")
            or raw.get("formula")
        )
        caption = _string(
            raw.get("image_caption")
            or raw.get("chart_caption")
            or raw.get("table_caption")
            or raw.get("code_caption")
            or raw.get("caption")
        )
        footnote = _string(
            raw.get("image_footnote")
            or raw.get("chart_footnote")
            or raw.get("table_footnote")
            or raw.get("footnote")
        )
        asset = _string(raw.get("img_path") or raw.get("image_path") or raw.get("asset"))
        if kind == "text":
            rendered_kind = "heading" if level > 0 else "paragraph"
        elif kind == "list":
            items = raw.get("list_items")
            if isinstance(items, list):
                source_text = "\n".join(f"- {_string(item)}" for item in items if _string(item))
            rendered_kind = "reference" if subtype == "ref_text" else "list"
        elif kind in {"image", "chart"}:
            rendered_kind = "figure"
        elif kind == "table":
            rendered_kind = "table"
            source_text = _string(raw.get("table_body") or raw.get("content") or source_text)
        elif kind == "equation":
            rendered_kind = "equation"
        elif kind == "code":
            rendered_kind = "code"
        elif kind == "page_footnote":
            rendered_kind = "page_footnote"
        elif kind == "aside_text":
            rendered_kind = "aside"
        else:
            rendered_kind = kind

        blocks.append(
            DocumentBlock(
                id=block_id,
                kind=rendered_kind,
                page=page,
                level=level,
                subtype=subtype,
                source=source_text,
                asset=asset,
                caption_source=caption,
                footnote_source=footnote,
                bbox=bbox,
                extra={"mineru_type": kind},
            )
        )
    return blocks


def _drop_empty_blocks(blocks: list[DocumentBlock]) -> tuple[list[DocumentBlock], int]:
    text_kinds = {"paragraph", "heading", "list", "reference", "aside", "page_footnote"}
    retained = [
        block
        for block in blocks
        if block.kind not in text_kinds or bool(block.source.strip())
    ]
    return retained, len(blocks) - len(retained)


FIGURE_CAPTION = re.compile(r"\b(?:Figure|Fig\.)\s+\d+\s*:", re.IGNORECASE)


def _subfigure_parts(block: DocumentBlock) -> tuple[str, str]:
    match = SUBFIGURE_PREFIX.match(block.caption_source)
    if match is None:
        return "", block.caption_source.strip()
    return match.group(1).lower(), block.caption_source[match.end() :].strip()


def _horizontal_neighbors(left: DocumentBlock, right: DocumentBlock) -> bool:
    if left.page != right.page or len(left.bbox) != 4 or len(right.bbox) != 4:
        return False
    left_height = max(left.bbox[3] - left.bbox[1], 0.0)
    right_height = max(right.bbox[3] - right.bbox[1], 0.0)
    shorter_height = min(left_height, right_height)
    if shorter_height <= 0:
        return False
    overlap = max(0.0, min(left.bbox[3], right.bbox[3]) - max(left.bbox[1], right.bbox[1]))
    if overlap / shorter_height < 0.85:
        return False
    left_width = max(left.bbox[2] - left.bbox[0], 0.0)
    right_width = max(right.bbox[2] - right.bbox[0], 0.0)
    smaller_width = min(left_width, right_width)
    if smaller_width <= 0 or right.bbox[0] < left.bbox[0]:
        return False
    gap = right.bbox[0] - left.bbox[2]
    return -0.1 * smaller_width <= gap <= 0.25 * smaller_width


def _automatic_group(run: list[DocumentBlock]) -> DocumentBlock | None:
    if len(run) < 2 or any(block.footnote_source for block in run):
        return None
    parts = [_subfigure_parts(block) for block in run]
    expected = [f"({chr(ord('a') + index)})" for index in range(len(run))]
    labels = [label for label, _ in parts]
    if labels != expected:
        return None
    full = [(block, body) for block, (_, body) in zip(run, parts, strict=True) if body]
    if len(full) != 1 or FIGURE_CAPTION.search(full[0][1]) is None:
        return None
    caption_block, caption = full[0]
    caption_folded = caption.casefold()
    if any(label not in caption_folded for label in labels):
        return None
    members = [
        FigureMember(
            id=block.id,
            asset=block.asset,
            label=label,
            page=block.page,
            bbox=block.bbox,
        )
        for block, label in zip(run, labels, strict=True)
    ]
    if any(not member.asset for member in members):
        return None
    return DocumentBlock(
        id=f"fg-{'-'.join(block.id for block in run)}",
        kind="figure_group",
        page=run[0].page,
        caption_source=caption,
        bbox=[
            min(block.bbox[0] for block in run),
            min(block.bbox[1] for block in run),
            max(block.bbox[2] for block in run),
            max(block.bbox[3] for block in run),
        ],
        members=members,
        layout="row",
        extra={
            "mineru_type": "figure_group",
            "caption_origin_id": caption_block.id,
            "origin_block_ids": [block.id for block in run],
            "automatic": True,
        },
    )


def _group_adjacent_figures(
    blocks: list[DocumentBlock],
) -> tuple[list[DocumentBlock], int]:
    grouped: list[DocumentBlock] = []
    group_count = 0
    index = 0
    while index < len(blocks):
        current = blocks[index]
        label, _ = _subfigure_parts(current)
        if current.kind != "figure" or label != "(a)":
            grouped.append(current)
            index += 1
            continue
        run = [current]
        cursor = index + 1
        while cursor < len(blocks):
            following = blocks[cursor]
            expected = f"({chr(ord('a') + len(run))})"
            following_label, _ = _subfigure_parts(following)
            if (
                following.kind != "figure"
                or following_label != expected
                or not _horizontal_neighbors(run[-1], following)
            ):
                break
            run.append(following)
            cursor += 1
        candidate = _automatic_group(run)
        if candidate is None:
            grouped.extend(run)
        else:
            grouped.append(candidate)
            group_count += 1
        index += len(run)
    return grouped, group_count


NOTE_MARKER = re.compile(r"^\s*<sup>([^<]+)</sup>\s*", re.IGNORECASE)
PURE_URL = re.compile(r"^(?:https?://|www\.)\S+$", re.IGNORECASE)


def _extract_notes(
    blocks: list[DocumentBlock],
) -> tuple[list[DocumentBlock], list[DocumentNote], list[DocumentNote]]:
    body: list[DocumentBlock] = []
    front_notes: list[DocumentNote] = []
    endnotes: list[DocumentNote] = []
    for block in blocks:
        if block.kind not in {"page_footnote", "front_note", "endnote"}:
            body.append(block)
            continue
        match = NOTE_MARKER.match(block.source)
        marker = match.group(1).strip() if match else ""
        note_source = block.source[match.end() :].strip() if match else block.source.strip()
        folded = note_source.casefold()
        forced_front = block.kind == "front_note"
        forced_end = block.kind == "endnote"
        copyright_note = bool(
            re.search(r"copyright|©|all rights reserved", note_source, re.IGNORECASE)
        )
        author_note = (
            block.page == 0
            and (
                marker in {"*", "†", "‡"}
                or "contributed equally" in folded
                or "corresponding author" in folded
            )
        )
        is_front = forced_front or (not forced_end and (copyright_note or author_note))
        url = note_source.replace("\\#", "#") if PURE_URL.fullmatch(note_source) else ""
        if url.startswith("www."):
            url = f"https://{url}"
        note_kind = (
            "copyright"
            if is_front and copyright_note
            else "author_note"
            if is_front
            else "endnote"
        )
        note = DocumentNote(
            id=block.id,
            kind=note_kind,
            marker=marker,
            page=block.page,
            source=note_source,
            url=url,
            bbox=block.bbox,
        )
        if is_front:
            front_notes.append(note)
        else:
            endnotes.append(note)
    return body, front_notes, endnotes


def _assign_segments(
    blocks: list[DocumentBlock],
    front_notes: list[DocumentNote],
    endnotes: list[DocumentNote],
    *,
    metadata: PaperMetadata,
) -> tuple[list[DocumentBlock], list[DocumentNote], list[DocumentNote], list[Segment]]:
    rendered_blocks = [block.model_copy(deep=True) for block in blocks]
    rendered_front = [note.model_copy(deep=True) for note in front_notes]
    rendered_end = [note.model_copy(deep=True) for note in endnotes]
    segments: list[Segment] = []

    def add_segment(segment_id: str, role: str, text: str) -> str:
        clean = text.strip()
        if not clean:
            return ""
        segments.append(
            Segment(
                id=segment_id,
                role=role,
                source=clean,
                source_hash=hash_text(clean),
                protected=_protected_tokens(clean),
            )
        )
        return segment_id

    add_segment("meta.title", "title", metadata.title)
    if metadata.abstract:
        add_segment("meta.abstract", "abstract", metadata.abstract)

    for block in rendered_blocks:
        if is_front_matter_duplicate(block):
            continue
        if block.kind in {"heading", "paragraph", "list"}:
            block.segment_id = add_segment(f"{block.id}.text", block.kind, block.source)
        elif block.kind == "table":
            block.segment_id = add_segment(f"{block.id}.table", "table", block.source)
        elif block.source and block.kind not in {"equation", "code", "reference"}:
            block.segment_id = add_segment(f"{block.id}.text", "paragraph", block.source)
        if block.caption_source:
            caption_origin = str(block.extra.get("caption_origin_id") or block.id)
            block.caption_segment_id = add_segment(
                f"{caption_origin}.caption", "caption", block.caption_source
            )
        if block.footnote_source:
            block.footnote_segment_id = add_segment(
                f"{block.id}.footnote", "caption", block.footnote_source
            )
    for note in [*rendered_front, *rendered_end]:
        if note.source and not note.url:
            note.segment_id = add_segment(f"{note.id}.note", "footnote", note.source)
    return rendered_blocks, rendered_front, rendered_end, segments


def write_translation_tasks(
    document: Document,
    task_dir: Path,
    *,
    max_source_chars: int = 9000,
    targets: dict[str, str] | None = None,
) -> list[Path]:
    task_dir.mkdir(parents=True, exist_ok=True)
    for old in task_dir.glob("part-*.json"):
        old.unlink()
    chunks: list[list[TranslationEntry]] = []
    current: list[TranslationEntry] = []
    current_chars = 0
    for segment in document.segments:
        entry = TranslationEntry(
            id=segment.id,
            role=segment.role,
            source_hash=segment.source_hash,
            source=segment.source,
            target=(targets or {}).get(segment.id),
        )
        if current and current_chars + len(segment.source) > max_source_chars:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(entry)
        current_chars += len(segment.source)
    if current:
        chunks.append(current)
    instructions = [
        "Translate faithfully into Simplified Chinese without summarizing or omitting content.",
        "Keep formulas, code, URLs, DOI strings, and citation markers exactly unchanged.",
        "Keep Markdown/HTML table structure intact and translate only human-readable cell text.",
        "Use consistent academic terminology; include the English term at its first material use.",
    ]
    paths: list[Path] = []
    for index, translations in enumerate(chunks, start=1):
        payload = TranslationFile(
            job_id=document.job_id,
            instructions=instructions,
            translations=translations,
        )
        path = task_dir / f"part-{index:04d}.json"
        atomic_write_json(path, payload.model_dump(mode="json"))
        paths.append(path)
    return paths


def write_document(document: Document, path: Path) -> None:
    atomic_write_json(path, document.model_dump(mode="json"))
