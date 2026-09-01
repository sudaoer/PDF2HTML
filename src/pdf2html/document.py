from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

from .models import (
    Document,
    DocumentBlock,
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


def _repair_interrupted_paragraphs(blocks: list[DocumentBlock]) -> list[DocumentBlock]:
    """Move a same-page table/figure after a paragraph it visually interrupted.

    Two-column papers often place a floating table between two text fragments in
    MinerU's reading order.  A fragment without terminal punctuation followed by
    a float and another paragraph is a conservative signal that the sentence
    should be completed before the float is shown in the reflowed document.
    """
    repaired = list(blocks)
    index = 0
    while index + 2 < len(repaired):
        before, floating, after = repaired[index : index + 3]
        same_page = before.page == floating.page == after.page
        interrupted = (
            before.kind == "paragraph"
            and floating.kind in {"figure", "table"}
            and after.kind == "paragraph"
            and bool(before.source.strip())
            and not re.search(r"[.!?。！？:：;；]['\"”’)]*$", before.source.strip())
        )
        if same_page and interrupted:
            repaired[index + 1 : index + 3] = [after, floating]
            index += 3
            continue
        index += 1
    return repaired


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
) -> Document:
    entries = _content_entries(read_json(content_list_path))
    blocks: list[DocumentBlock] = []
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
        segment_id = ""
        caption_segment_id = ""
        footnote_segment_id = ""

        if kind == "text":
            rendered_kind = "heading" if level > 0 else "paragraph"
            segment_id = add_segment(f"{block_id}.text", rendered_kind, source_text)
        elif kind == "list":
            items = raw.get("list_items")
            if isinstance(items, list):
                source_text = "\n".join(f"- {_string(item)}" for item in items if _string(item))
            rendered_kind = "reference" if subtype == "ref_text" else "list"
            if rendered_kind != "reference":
                segment_id = add_segment(f"{block_id}.text", "list", source_text)
        elif kind in {"image", "chart"}:
            rendered_kind = "figure"
            caption_segment_id = add_segment(f"{block_id}.caption", "caption", caption)
            footnote_segment_id = add_segment(f"{block_id}.footnote", "caption", footnote)
        elif kind == "table":
            rendered_kind = "table"
            source_text = _string(raw.get("table_body") or raw.get("content") or source_text)
            segment_id = add_segment(f"{block_id}.table", "table", source_text)
            caption_segment_id = add_segment(f"{block_id}.caption", "caption", caption)
            footnote_segment_id = add_segment(f"{block_id}.footnote", "caption", footnote)
        elif kind == "equation":
            rendered_kind = "equation"
        elif kind == "code":
            rendered_kind = "code"
            caption_segment_id = add_segment(f"{block_id}.caption", "caption", caption)
        elif kind in {"aside_text", "page_footnote"}:
            rendered_kind = "aside"
            segment_id = add_segment(f"{block_id}.text", "footnote", source_text)
        else:
            rendered_kind = kind
            if source_text:
                segment_id = add_segment(f"{block_id}.text", "paragraph", source_text)

        blocks.append(
            DocumentBlock(
                id=block_id,
                kind=rendered_kind,
                page=page,
                level=level,
                subtype=subtype,
                source=source_text,
                segment_id=segment_id,
                asset=asset,
                caption_source=caption,
                caption_segment_id=caption_segment_id,
                footnote_source=footnote,
                footnote_segment_id=footnote_segment_id,
                bbox=bbox,
                extra={"mineru_type": kind},
            )
        )
    return Document(
        job_id=job_id,
        metadata=metadata,
        source=source,
        blocks=_repair_interrupted_paragraphs(blocks),
        segments=segments,
    )


def write_translation_tasks(
    document: Document,
    task_dir: Path,
    *,
    max_source_chars: int = 9000,
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
