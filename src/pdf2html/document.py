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
KEYWORD_MARKER = re.compile(
    r"^(?:keywords?|key\s+words?|index\s+terms)\s*[:：]\s*$",
    re.IGNORECASE,
)
MAX_KEYWORD_ITEMS = 32
MAX_KEYWORD_ITEM_CHARS = 160
UNORDERED_LIST_PREFIX = re.compile(r"^\s*(?:[-+*]|[•◦▪●○])\s+")


def _string(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(str(part).strip() for part in value if str(part).strip())
    return ""


def _normalize_list_item(value: Any) -> str:
    return UNORDERED_LIST_PREFIX.sub("", _string(value), count=1).strip()


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
SUBFIGURE_LABEL_RUN = re.compile(r"^\s*(?:\([a-z]\)\s*){2,}$", re.IGNORECASE)
TRAILING_SUBFIGURE_LABEL = re.compile(
    r"^(?P<body>.+\S)\s*(?P<label>\([a-z]\))\s*$",
    re.IGNORECASE | re.DOTALL,
)
FIGURE_NUMBER = re.compile(
    r"\b(?:Figure|Fig\.)\s+(\d+)(?=\s*(?:[.:]|\b))",
    re.IGNORECASE,
)
BARE_FIRST_SUBFIGURE = re.compile(
    r"^\s*(?:(?:predicted|ground[- ]?truth)\s+label\s*)?a\s*$",
    re.IGNORECASE,
)
BARE_SUBFIGURE_ONLY = re.compile(r"^\s*([a-z])\s*$", re.IGNORECASE)
BARE_SUBFIGURE_WITH_CAPTION = re.compile(
    r"^\s*([a-z])\s*((?:Figure|Fig\.)\s+\d+\b.*)$",
    re.IGNORECASE | re.DOTALL,
)
ARRAY_TRAILING_TAG = re.compile(
    r"(?P<prefix>\\begin\{array\}(?:\{[^{}]*\})?[\s\S]*?)"
    r"\\tag\s*\{(?P<label>[^{}]+)\}\s*\\\\\s*"
    r"(?P<end>\\end\{array\})\s*$"
)


def _valid_bbox(block: DocumentBlock) -> bool:
    return len(block.bbox) == 4 and block.bbox[0] < block.bbox[2] and block.bbox[1] < block.bbox[3]


def _is_keyword_item(
    anchor: DocumentBlock,
    previous: DocumentBlock,
    candidate: DocumentBlock,
) -> bool:
    if (
        candidate.kind != "paragraph"
        or candidate.page != anchor.page
        or not _valid_bbox(anchor)
        or not _valid_bbox(previous)
        or not _valid_bbox(candidate)
    ):
        return False
    text = candidate.source.strip()
    if (
        not text
        or "\n" in text
        or len(text) > MAX_KEYWORD_ITEM_CHARS
        or KEYWORD_MARKER.fullmatch(text)
    ):
        return False

    anchor_height = anchor.bbox[3] - anchor.bbox[1]
    previous_height = previous.bbox[3] - previous.bbox[1]
    candidate_height = candidate.bbox[3] - candidate.bbox[1]
    left_tolerance = max(12.0, anchor_height * 1.5)
    if abs(candidate.bbox[0] - anchor.bbox[0]) > left_tolerance:
        return False
    if candidate_height > max(36.0, anchor_height * 3.0):
        return False

    vertical_gap = candidate.bbox[1] - previous.bbox[3]
    gap_tolerance = max(12.0, previous_height * 1.5, candidate_height * 1.5)
    return -2.0 <= vertical_gap <= gap_tolerance


def _group_keyword_blocks(
    blocks: list[DocumentBlock],
) -> tuple[list[DocumentBlock], int]:
    grouped: list[DocumentBlock] = []
    group_count = 0
    index = 0
    while index < len(blocks):
        anchor = blocks[index]
        if (
            anchor.page != 0
            or anchor.kind not in {"heading", "paragraph"}
            or not KEYWORD_MARKER.fullmatch(anchor.source.strip())
            or not _valid_bbox(anchor)
        ):
            grouped.append(anchor)
            index += 1
            continue

        members = [anchor]
        previous = anchor
        cursor = index + 1
        while cursor < len(blocks) and len(members) <= MAX_KEYWORD_ITEMS:
            candidate = blocks[cursor]
            if not _is_keyword_item(anchor, previous, candidate):
                break
            members.append(candidate)
            previous = candidate
            cursor += 1
        if len(members) == 1:
            grouped.append(anchor)
            index += 1
            continue

        merged = anchor.model_copy(deep=True)
        merged.kind = "paragraph"
        merged.level = 0
        merged.subtype = "keywords"
        keyword_text = "; ".join(block.source.strip() for block in members[1:])
        merged.source = f"{anchor.source.strip()} {keyword_text}"
        merged.bbox = [
            min(block.bbox[0] for block in members),
            min(block.bbox[1] for block in members),
            max(block.bbox[2] for block in members),
            max(block.bbox[3] for block in members),
        ]
        merged.extra.update(
            {
                "front_matter_role": "keywords",
                "automatic_keyword_group": True,
                "origin_block_ids": [block.id for block in members],
                "origin_pages": [block.page for block in members],
                "origin_bboxes": [block.bbox for block in members],
            }
        )
        grouped.append(merged)
        group_count += 1
        index = cursor
    return grouped, group_count


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


def _reaches_page_bottom(
    block: DocumentBlock,
    *,
    page_bottoms: dict[int, float],
) -> bool:
    if not _valid_bbox(block):
        return False
    page_bottom = page_bottoms.get(block.page, block.bbox[3])
    page_height = max(page_bottom, 1.0)
    bottom_tolerance = max(12.0, page_height * 0.05)
    return page_bottom - block.bbox[3] <= bottom_tolerance


def _next_page_transition(
    before: DocumentBlock,
    after: DocumentBlock,
    *,
    page_bottoms: dict[int, float],
) -> bool:
    """Return whether a paragraph tail reaches the next physical PDF page."""
    if after.page != before.page + 1 or not _valid_bbox(after):
        return False
    return _reaches_page_bottom(before, page_bottoms=page_bottoms)


def _multi_page_float_transition(
    before: DocumentBlock,
    after: DocumentBlock,
    *,
    intervening_floats: list[DocumentBlock],
    page_bottoms: dict[int, float],
) -> bool:
    """Return whether floats occupy every page skipped by a paragraph continuation."""
    if (
        after.page <= before.page + 1
        or not intervening_floats
        or not _valid_bbox(after)
        or not _reaches_page_bottom(before, page_bottoms=page_bottoms)
    ):
        return False
    float_pages = [block.page for block in intervening_floats]
    if float_pages != sorted(float_pages):
        return False
    if any(page < before.page or page > after.page for page in float_pages):
        return False
    skipped_pages = set(range(before.page + 1, after.page))
    return skipped_pages.issubset(float_pages)


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
    if _next_page_transition(before, after, page_bottoms=page_bottoms):
        return True
    return _multi_page_float_transition(
        before,
        after,
        intervening_floats=intervening_floats,
        page_bottoms=page_bottoms,
    )


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


def _repair_displaced_figure_captions(
    blocks: list[DocumentBlock],
) -> tuple[list[DocumentBlock], int]:
    """Repair one conservative multi-figure caption displacement pattern.

    Some MinerU VLM outputs keep an upper two-panel figure as one image whose
    caption contains only ``(a)(b)``.  The real caption is then prepended to the
    lower row's ``(a)`` panel, while the lower ``(b)`` panel carries the next
    consecutively numbered figure caption.  Rebind the first caption only when
    labels, numbering, page, row geometry, and horizontal span all agree.
    """
    repaired = [block.model_copy(deep=True) for block in blocks]
    repair_count = 0
    index = 0
    while index + 2 < len(repaired):
        upper, lower_left, lower_right = repaired[index : index + 3]
        if any(block.kind != "figure" for block in (upper, lower_left, lower_right)):
            index += 1
            continue
        if len({block.page for block in (upper, lower_left, lower_right)}) != 1:
            index += 1
            continue
        if not all(_valid_bbox(block) for block in (upper, lower_left, lower_right)):
            index += 1
            continue
        if SUBFIGURE_LABEL_RUN.fullmatch(upper.caption_source) is None:
            index += 1
            continue
        upper_labels = re.findall(r"\([a-z]\)", upper.caption_source.casefold())
        if upper_labels != ["(a)", "(b)"]:
            index += 1
            continue
        trailing = TRAILING_SUBFIGURE_LABEL.fullmatch(lower_left.caption_source)
        if trailing is None or trailing.group("label").casefold() != "(a)":
            index += 1
            continue
        right_label, right_caption = _subfigure_parts(lower_right)
        if right_label != "(b)" or not right_caption:
            index += 1
            continue
        displaced_caption = trailing.group("body").strip()
        displaced_number = FIGURE_NUMBER.search(displaced_caption)
        following_number = FIGURE_NUMBER.search(right_caption)
        if (
            displaced_number is None
            or following_number is None
            or int(following_number.group(1)) != int(displaced_number.group(1)) + 1
        ):
            index += 1
            continue
        if not _horizontal_neighbors(lower_left, lower_right):
            index += 1
            continue
        if upper.bbox[3] > min(lower_left.bbox[1], lower_right.bbox[1]):
            index += 1
            continue
        lower_left_edge = min(lower_left.bbox[0], lower_right.bbox[0])
        lower_right_edge = max(lower_left.bbox[2], lower_right.bbox[2])
        overlap = max(
            0.0,
            min(upper.bbox[2], lower_right_edge)
            - max(upper.bbox[0], lower_left_edge),
        )
        narrower_span = min(
            upper.bbox[2] - upper.bbox[0],
            lower_right_edge - lower_left_edge,
        )
        if narrower_span <= 0 or overlap / narrower_span < 0.8:
            index += 1
            continue

        upper.caption_source = displaced_caption
        upper.extra["automatic_caption_repair"] = True
        upper.extra["caption_repaired_from_id"] = lower_left.id
        lower_left.caption_source = "(a)"
        lower_left.extra["automatic_caption_repair"] = True
        lower_left.extra["caption_reassigned_to"] = upper.id
        repair_count += 1
        index += 3
    return repaired, repair_count


def _repair_bare_subfigure_labels(
    blocks: list[DocumentBlock],
) -> tuple[list[DocumentBlock], int]:
    """Normalize a conservative bare-label figure run to ``(a)`` ... ``(n)``.

    Journal figures are sometimes emitted as consecutive panel crops whose
    captions are bare letters, with the final crop carrying ``fFig. 4 ...``.
    Accept only a same-page, geometrically ordered run beginning at ``a`` and
    ending in a numbered figure caption.  A common axis label accidentally
    attached to the first panel is tolerated only for predicted/ground-truth
    labels.
    """
    repaired = [block.model_copy(deep=True) for block in blocks]
    repair_count = 0
    index = 0
    while index < len(repaired):
        first = repaired[index]
        if (
            first.kind != "figure"
            or BARE_FIRST_SUBFIGURE.fullmatch(first.caption_source) is None
            or not _valid_bbox(first)
        ):
            index += 1
            continue

        run = [first]
        full_caption = ""
        cursor = index + 1
        expected_code = ord("b")
        while cursor < len(repaired):
            candidate = repaired[cursor]
            if (
                candidate.kind != "figure"
                or candidate.page != first.page
                or not _valid_bbox(candidate)
            ):
                break
            bare = BARE_SUBFIGURE_ONLY.fullmatch(candidate.caption_source)
            with_caption = BARE_SUBFIGURE_WITH_CAPTION.fullmatch(
                candidate.caption_source
            )
            match = with_caption or bare
            if match is None or match.group(1).casefold() != chr(expected_code):
                break
            previous = run[-1]
            same_row = _horizontal_neighbors(previous, candidate)
            new_row = (
                candidate.bbox[1] > previous.bbox[1]
                and candidate.bbox[0] <= previous.bbox[0]
            )
            if not same_row and not new_row:
                break
            run.append(candidate)
            cursor += 1
            expected_code += 1
            if with_caption is not None:
                full_caption = with_caption.group(2).strip()
                break

        if len(run) < 2 or not full_caption or FIGURE_NUMBER.search(full_caption) is None:
            index += 1
            continue

        for offset, block in enumerate(run):
            label = f"({chr(ord('a') + offset)})"
            block.caption_source = (
                f"{label}{full_caption}" if block is run[-1] else label
            )
            block.extra["automatic_bare_subfigure_label_repair"] = True
        repair_count += 1
        index = cursor
    return repaired, repair_count


def _repair_equation_tags(
    blocks: list[DocumentBlock],
) -> tuple[list[DocumentBlock], int]:
    """Move a trailing equation tag outside a MinerU array environment."""
    repaired = [block.model_copy(deep=True) for block in blocks]
    repair_count = 0
    for block in repaired:
        if block.kind != "equation":
            continue
        match = ARRAY_TRAILING_TAG.fullmatch(block.source.strip())
        if match is None:
            continue
        block.source = (
            f"{match.group('prefix').rstrip()} "
            f"{match.group('end')}\\tag{{{match.group('label').strip()}}}"
        )
        block.extra["automatic_equation_tag_repair"] = True
        repair_count += 1
    return repaired, repair_count


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
    primitive_blocks, keyword_group_count = _group_keyword_blocks(primitive_blocks)
    primitive_blocks, equation_tag_repair_count = _repair_equation_tags(primitive_blocks)
    heading_blocks, heading_info = normalize_heading_structure(
        primitive_blocks,
        metadata=metadata,
        middle_json_path=middle_json_path,
    )
    nonempty_blocks, empty_count = _drop_empty_blocks(heading_blocks)
    repaired_blocks, caption_repair_count = _repair_displaced_figure_captions(
        nonempty_blocks
    )
    label_repaired_blocks, bare_label_repair_count = _repair_bare_subfigure_labels(
        repaired_blocks
    )
    grouped_blocks, group_count = _group_adjacent_figures(label_repaired_blocks)
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
            "automatic_keyword_groups": keyword_group_count,
            "automatic_caption_repairs": caption_repair_count,
            "automatic_bare_subfigure_label_repairs": bare_label_repair_count,
            "automatic_equation_tag_repairs": equation_tag_repair_count,
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
                normalized_items = [_normalize_list_item(item) for item in items]
                source_text = "\n".join(f"- {item}" for item in normalized_items if item)
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


NOTE_MARKER = re.compile(
    r"^\s*(?:<sup>(?P<html>[^<]+)</sup>|"
    r"\$\^\{(?P<latex>[^{}]+)\}\$|(?P<plain>[*†‡]))\s*",
    re.IGNORECASE,
)
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
        marker = (
            next(group for group in match.groups() if group is not None).strip()
            if match
            else ""
        )
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
                or "equal contribution" in folded
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
