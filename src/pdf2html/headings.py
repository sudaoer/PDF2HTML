from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .models import DocumentBlock, PaperMetadata
from .util import read_json

FRONT_MATTER_ROLES = {"document_title", "abstract_heading", "abstract_body"}
ALIGNMENT_TOLERANCE = 0.06
NUMBERED_HEADING = re.compile(r"^\s*(?P<number>\d+(?:\.\d+)*)(?:[.)])?\s+(?=\S)")


@dataclass(frozen=True)
class MiddleLine:
    source: str
    bbox: list[float]


@dataclass(frozen=True)
class MiddleHeading:
    page: int
    source: str
    bbox: list[float]
    lines: list[MiddleLine]


@dataclass(frozen=True)
class HeadingPiece:
    source: str
    bbox: list[float]
    line_indices: list[int]
    alignment: str


def is_front_matter_duplicate(block: DocumentBlock) -> bool:
    return str(block.extra.get("front_matter_role") or "") in FRONT_MATTER_ROLES


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _compact(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).casefold()


def _bbox(value: Any) -> list[float]:
    if not isinstance(value, list) or len(value) != 4:
        return []
    try:
        result = [float(item) for item in value]
    except (TypeError, ValueError):
        return []
    if result[0] >= result[2] or result[1] >= result[3]:
        return []
    return result


def _middle_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "".join(_middle_text(item) for item in value).strip()
    if not isinstance(value, dict):
        return ""
    content = value.get("content")
    if content is not None:
        return _middle_text(content)
    spans = value.get("spans")
    if spans is not None:
        return _middle_text(spans)
    return ""


def _load_middle_headings(path: Path | None) -> tuple[list[MiddleHeading], list[str]]:
    if path is None:
        return [], ["MinerU middle JSON is unavailable; line-level heading repair was skipped."]
    try:
        payload = read_json(path)
    except (OSError, ValueError) as exc:
        return [], [f"MinerU middle JSON could not be read: {exc}"]
    if not isinstance(payload, dict) or not isinstance(payload.get("pdf_info"), list):
        return [], ["MinerU middle JSON has an unsupported structure."]
    headings: list[MiddleHeading] = []
    for page_index, page_raw in enumerate(cast(list[Any], payload["pdf_info"])):
        if not isinstance(page_raw, dict):
            continue
        para_blocks = page_raw.get("para_blocks")
        if not isinstance(para_blocks, list):
            continue
        for raw in para_blocks:
            if not isinstance(raw, dict) or raw.get("type") != "title":
                continue
            lines_raw = raw.get("lines")
            if not isinstance(lines_raw, list):
                continue
            lines: list[MiddleLine] = []
            for line_raw in lines_raw:
                if not isinstance(line_raw, dict):
                    continue
                source = _middle_text(line_raw.get("spans"))
                line_bbox = _bbox(line_raw.get("bbox"))
                if source and line_bbox:
                    lines.append(MiddleLine(source=source, bbox=line_bbox))
            source = " ".join(line.source for line in lines).strip()
            heading_bbox = _bbox(raw.get("bbox"))
            if source and heading_bbox and lines:
                headings.append(
                    MiddleHeading(
                        page=page_index,
                        source=source,
                        bbox=heading_bbox,
                        lines=lines,
                    )
                )
    return headings, []


def _transform_bbox(
    line_bbox: list[float], middle_bbox: list[float], target_bbox: list[float]
) -> list[float]:
    if not line_bbox or not middle_bbox or not target_bbox:
        return []
    middle_width = middle_bbox[2] - middle_bbox[0]
    middle_height = middle_bbox[3] - middle_bbox[1]
    if middle_width <= 0 or middle_height <= 0:
        return []
    target_width = target_bbox[2] - target_bbox[0]
    target_height = target_bbox[3] - target_bbox[1]
    return [
        target_bbox[0] + (line_bbox[0] - middle_bbox[0]) / middle_width * target_width,
        target_bbox[1] + (line_bbox[1] - middle_bbox[1]) / middle_height * target_height,
        target_bbox[0] + (line_bbox[2] - middle_bbox[0]) / middle_width * target_width,
        target_bbox[1] + (line_bbox[3] - middle_bbox[1]) / middle_height * target_height,
    ]


def _vertical_distance(first: list[float], second: list[float]) -> float:
    if first[3] < second[1]:
        return second[1] - first[3]
    if second[3] < first[1]:
        return first[1] - second[3]
    return 0.0


def _union_bboxes(*values: list[float]) -> list[float]:
    valid = [bbox for bbox in values if len(bbox) == 4]
    if not valid:
        return []
    return [
        min(bbox[0] for bbox in valid),
        min(bbox[1] for bbox in valid),
        max(bbox[2] for bbox in valid),
        max(bbox[3] for bbox in valid),
    ]


def _column_bounds(blocks: list[DocumentBlock], page: int, bbox: list[float]) -> list[float]:
    if not bbox:
        return []
    center = (bbox[0] + bbox[2]) / 2
    width = bbox[2] - bbox[0]
    candidates: list[tuple[float, float, list[float]]] = []
    for block in blocks:
        if block.page != page or block.kind not in {"paragraph", "list", "reference", "aside"}:
            continue
        candidate = block.bbox
        if len(candidate) != 4 or candidate[0] >= candidate[2] or candidate[1] >= candidate[3]:
            continue
        candidate_width = candidate[2] - candidate[0]
        if not candidate[0] <= center <= candidate[2] or candidate_width < max(80.0, width):
            continue
        candidates.append(
            (
                _vertical_distance(bbox, candidate),
                -candidate_width,
                [candidate[0], candidate[2]],
            )
        )
    return min(candidates, default=(0.0, 0.0, []))[2]


def _alignment(blocks: list[DocumentBlock], page: int, bbox: list[float]) -> str:
    column = _column_bounds(blocks, page, bbox)
    if len(column) != 2:
        return "unknown"
    width = column[1] - column[0]
    if width <= 0:
        return "unknown"
    left_delta = abs(bbox[0] - column[0]) / width
    center_delta = abs((bbox[0] + bbox[2] - column[0] - column[1]) / 2) / width
    if left_delta <= ALIGNMENT_TOLERANCE:
        return "left"
    if center_delta <= ALIGNMENT_TOLERANCE:
        return "center"
    return "unknown"


def _front_matter_heading_role(block: DocumentBlock, metadata: PaperMetadata) -> str:
    if block.page != 0:
        return ""
    if _normalized(block.source) == _normalized(metadata.title):
        return "document_title"
    if metadata.abstract and _normalized(block.source) == "abstract":
        return "abstract_heading"
    return ""


def _mark_front_matter(blocks: list[DocumentBlock], metadata: PaperMetadata) -> int:
    marked = 0
    abstract = _normalized(metadata.abstract)
    for block in blocks:
        role = ""
        if block.kind == "heading":
            role = _front_matter_heading_role(block, metadata)
        elif block.page == 0 and abstract and _compact(block.source) == _compact(metadata.abstract):
            role = "abstract_body"
        if role:
            block.extra["front_matter_role"] = role
            marked += 1
    return marked


def _match_middle_heading(
    block: DocumentBlock,
    candidates: list[MiddleHeading],
    used: set[int],
) -> MiddleHeading | None:
    expected = _normalized(block.source)
    for index, candidate in enumerate(candidates):
        if index in used or candidate.page != block.page:
            continue
        if _normalized(candidate.source) == expected:
            used.add(index)
            return candidate
    return None


def _line_pieces(
    block: DocumentBlock,
    middle: MiddleHeading | None,
    blocks: list[DocumentBlock],
) -> list[HeadingPiece]:
    if middle is None:
        return [
            HeadingPiece(
                source=block.source,
                bbox=block.bbox,
                line_indices=[0],
                alignment=_alignment(blocks, block.page, block.bbox),
            )
        ]
    pieces: list[HeadingPiece] = []
    for index, line in enumerate(middle.lines):
        bbox = _transform_bbox(line.bbox, middle.bbox, block.bbox)
        pieces.append(
            HeadingPiece(
                source=line.source,
                bbox=bbox,
                line_indices=[index],
                alignment=_alignment(blocks, block.page, bbox),
            )
        )
    return pieces


def _merge_same_alignment(pieces: list[HeadingPiece]) -> list[HeadingPiece]:
    grouped: list[HeadingPiece] = []
    for piece in pieces:
        if grouped and grouped[-1].alignment == piece.alignment:
            previous = grouped[-1]
            grouped[-1] = HeadingPiece(
                source=f"{previous.source} {piece.source}".strip(),
                bbox=_union_bboxes(previous.bbox, piece.bbox),
                line_indices=[*previous.line_indices, *piece.line_indices],
                alignment=piece.alignment,
            )
        else:
            grouped.append(piece)
    return grouped


def _numbered_level(source: str) -> int | None:
    match = NUMBERED_HEADING.match(source)
    if match is None:
        return None
    depth = match.group("number").count(".") + 1
    return min(6, depth + 1)


def _mineru_hierarchy_is_coherent(headings: list[DocumentBlock]) -> tuple[bool, int]:
    raw_levels = [int(block.extra.get("heading_source_level") or block.level) for block in headings]
    if len(set(raw_levels)) < 2 or any(level < 1 or level > 6 for level in raw_levels):
        return False, 0
    offset = max(0, 2 - min(raw_levels))
    adjusted = [min(6, level + offset) for level in raw_levels]
    previous = 1
    for level in adjusted:
        if level > previous + 1:
            return False, 0
        previous = level
    return True, offset


def normalize_heading_structure(
    blocks: list[DocumentBlock],
    *,
    metadata: PaperMetadata,
    middle_json_path: Path | None,
) -> tuple[list[DocumentBlock], dict[str, Any]]:
    result = [block.model_copy(deep=True) for block in blocks]
    warnings: list[str] = []
    middle_headings, middle_warnings = _load_middle_headings(middle_json_path)
    warnings.extend(middle_warnings)
    used_middle: set[int] = set()
    line_map: dict[str, list[HeadingPiece]] = {}
    matched_middle: dict[str, MiddleHeading] = {}

    for block in result:
        if block.kind != "heading":
            continue
        block.extra["heading_source_level"] = block.level
        middle = _match_middle_heading(block, middle_headings, used_middle)
        if middle is not None:
            matched_middle[block.id] = middle
        line_map[block.id] = _line_pieces(block, middle, result)

    excluded_ids = {
        block.id
        for block in result
        if block.kind == "heading" and _front_matter_heading_role(block, metadata)
    }
    center_count = sum(
        any(piece.alignment == "center" for piece in pieces)
        for block_id, pieces in line_map.items()
        if block_id not in excluded_ids
    )
    left_count = sum(
        any(piece.alignment == "left" for piece in pieces)
        for block_id, pieces in line_map.items()
        if block_id not in excluded_ids
    )
    geometry_enabled = center_count >= 2 and left_count >= 2

    rebuilt: list[DocumentBlock] = []
    split_count = 0
    for block in result:
        if block.kind != "heading":
            rebuilt.append(block)
            continue
        pieces = line_map[block.id]
        grouped = _merge_same_alignment(pieces)
        alignments = {piece.alignment for piece in grouped}
        can_split = (
            geometry_enabled
            and len(grouped) > 1
            and alignments <= {"center", "left"}
            and len(alignments) > 1
        )
        if not can_split:
            block.extra["heading_alignment"] = (
                grouped[0].alignment if len(grouped) == 1 else "unknown"
            )
            if len(grouped) > 1 and len(alignments) > 1:
                block.extra["heading_ambiguous"] = True
            rebuilt.append(block)
            continue
        split_count += 1
        for index, piece in enumerate(grouped, start=1):
            part = block.model_copy(deep=True)
            part.id = f"{block.id}-part{index:02d}"
            part.source = piece.source
            part.bbox = piece.bbox
            part.extra.update(
                {
                    "origin_block_id": block.id,
                    "origin_line_indices": piece.line_indices,
                    "heading_alignment": piece.alignment,
                    "heading_source_level": block.level,
                }
            )
            rebuilt.append(part)

    front_matter_count = _mark_front_matter(rebuilt, metadata)
    body_headings = [
        block
        for block in rebuilt
        if block.kind == "heading" and not is_front_matter_duplicate(block)
    ]
    coherent_mineru, mineru_offset = _mineru_hierarchy_is_coherent(body_headings)
    inferred_count = 0
    for block in rebuilt:
        if block.kind != "heading":
            continue
        role = str(block.extra.get("front_matter_role") or "")
        if role == "document_title":
            block.level = 1
            block.extra["heading_level_origin"] = "metadata"
            continue
        if role == "abstract_heading":
            block.level = 2
            block.extra["heading_level_origin"] = "metadata"
            continue
        numbered_level = _numbered_level(block.source)
        if numbered_level is not None:
            block.level = numbered_level
            block.extra["heading_level_origin"] = "numbering"
            inferred_count += 1
            continue
        if coherent_mineru:
            source_level = int(block.extra.get("heading_source_level") or block.level)
            block.level = min(6, source_level + mineru_offset)
            block.extra["heading_level_origin"] = "mineru"
            continue
        alignment = str(block.extra.get("heading_alignment") or "")
        if geometry_enabled and alignment in {"center", "left"}:
            block.level = 2 if alignment == "center" else 3
            block.extra["heading_level_origin"] = "layout"
            inferred_count += 1
            continue
        block.level = min(6, max(2, block.level or 2))
        block.extra["heading_level_origin"] = "fallback"

    if body_headings and not coherent_mineru and not geometry_enabled:
        warnings.append(
            "Body heading levels were flat and lacked enough layout evidence; "
            "MinerU levels were retained."
        )
    matched_count = len(matched_middle)
    if middle_headings and matched_count < len(line_map):
        warnings.append(
            f"Matched {matched_count} of {len(line_map)} headings to MinerU line structure."
        )
    return rebuilt, {
        "automatic_heading_splits": split_count,
        "inferred_heading_levels": inferred_count,
        "front_matter_duplicates": front_matter_count,
        "heading_geometry_enabled": geometry_enabled,
        "heading_alignment_counts": {"center": center_count, "left": left_count},
        "heading_warnings": warnings,
    }
