from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import ValidationError

from .models import (
    DocumentBlock,
    FigureMember,
    LayoutOperation,
    LayoutOverrides,
)
from .util import hash_text, read_json

SUBFIGURE_PREFIX = re.compile(r"^\s*(\([a-z]\))\s*", re.IGNORECASE)
ALLOWED_RECLASSIFICATIONS = {
    "paragraph",
    "heading",
    "figure",
    "table",
    "equation",
    "code",
    "reference",
    "front_note",
    "endnote",
}


class LayoutOverrideError(RuntimeError):
    """A layout override is invalid or no longer matches its parsed source."""


def block_fingerprint(block: DocumentBlock) -> str:
    payload = {
        "id": block.id,
        "kind": block.kind,
        "level": block.level,
        "page": block.page,
        "bbox": block.bbox,
        "source": block.source,
        "asset": block.asset,
        "caption_source": block.caption_source,
        "footnote_source": block.footnote_source,
        "members": [member.model_dump(mode="json") for member in block.members],
        "mineru_type": block.extra.get("mineru_type", ""),
        "heading_source_level": block.extra.get("heading_source_level"),
        "heading_level_origin": block.extra.get("heading_level_origin", ""),
        "heading_alignment": block.extra.get("heading_alignment", ""),
        "heading_ambiguous": block.extra.get("heading_ambiguous", False),
        "front_matter_role": block.extra.get("front_matter_role", ""),
        "origin_block_id": block.extra.get("origin_block_id", ""),
        "origin_line_indices": block.extra.get("origin_line_indices", []),
    }
    return hash_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def load_layout_overrides(
    path: Path,
    *,
    job_id: str,
    source_sha256: str,
    content_sha256: str,
) -> LayoutOverrides | None:
    if not path.is_file():
        return None
    try:
        overrides = LayoutOverrides.model_validate(read_json(path))
    except (ValidationError, ValueError) as exc:
        raise LayoutOverrideError(f"Invalid layout override file {path}: {exc}") from exc
    if overrides.job_id != job_id:
        raise LayoutOverrideError(
            f"Layout override belongs to {overrides.job_id}, not {job_id}."
        )
    if overrides.source_sha256 != source_sha256:
        raise LayoutOverrideError("Layout override source_sha256 no longer matches the PDF.")
    if overrides.content_sha256 != content_sha256:
        raise LayoutOverrideError("Layout override content_sha256 no longer matches MinerU output.")
    return overrides


def _validate_target(block: DocumentBlock, operation: LayoutOperation, index: int) -> None:
    target = operation.targets[index]
    if block.page != target.page:
        raise LayoutOverrideError(
            f"Override target {target.id} moved from page {target.page} to {block.page}."
        )
    if block.bbox != target.bbox:
        raise LayoutOverrideError(f"Override target {target.id} has a different bbox.")
    actual_hash = block_fingerprint(block)
    if actual_hash != target.block_hash:
        raise LayoutOverrideError(f"Override target {target.id} content hash changed.")


def _caption_parts(block: DocumentBlock) -> tuple[str, str]:
    match = SUBFIGURE_PREFIX.match(block.caption_source)
    if match is None:
        return "", block.caption_source.strip()
    return match.group(1).lower(), block.caption_source[match.end() :].strip()


def _union_bbox(blocks: list[DocumentBlock]) -> list[float]:
    valid = [block.bbox for block in blocks if len(block.bbox) == 4]
    if not valid:
        return []
    return [
        min(bbox[0] for bbox in valid),
        min(bbox[1] for bbox in valid),
        max(bbox[2] for bbox in valid),
        max(bbox[3] for bbox in valid),
    ]


def _row_major_figures(figures: list[DocumentBlock]) -> list[DocumentBlock]:
    """Sort reviewed panels left-to-right within geometrically detected rows."""
    rows: list[list[DocumentBlock]] = []
    for block in sorted(figures, key=lambda item: (item.bbox[1], item.bbox[0], item.id)):
        block_height = block.bbox[3] - block.bbox[1]
        matched_row: list[DocumentBlock] | None = None
        for row in rows:
            row_top = min(item.bbox[1] for item in row)
            row_bottom = max(item.bbox[3] for item in row)
            overlap = max(0.0, min(row_bottom, block.bbox[3]) - max(row_top, block.bbox[1]))
            overlap_base = min(row_bottom - row_top, block_height)
            if overlap_base > 0 and overlap / overlap_base >= 0.5:
                matched_row = row
                break
        if matched_row is None:
            rows.append([block])
        else:
            matched_row.append(block)
    rows.sort(key=lambda row: min(item.bbox[1] for item in row))
    return [
        block
        for row in rows
        for block in sorted(row, key=lambda item: (item.bbox[0], item.bbox[1], item.id))
    ]


def _group(blocks: list[DocumentBlock], operation: LayoutOperation) -> list[DocumentBlock]:
    target_ids = [target.id for target in operation.targets]
    if len(target_ids) < 2 or len(set(target_ids)) != len(target_ids):
        raise LayoutOverrideError("A group operation requires at least two unique targets.")
    selected = [
        next((block for block in blocks if block.id == block_id), None)
        for block_id in target_ids
    ]
    if any(block is None for block in selected):
        raise LayoutOverrideError(
            "A group operation refers to a block changed by an earlier operation."
        )
    figures = [block for block in selected if block is not None]
    if any(block.kind != "figure" for block in figures):
        raise LayoutOverrideError("Only standalone figure blocks can be grouped.")
    if len({block.page for block in figures}) != 1:
        raise LayoutOverrideError("Grouped figures must be on the same PDF page.")
    if any(len(block.bbox) != 4 for block in figures):
        raise LayoutOverrideError("Every grouped figure needs a four-value bbox.")
    figures = _row_major_figures(figures)
    ids = [block.id for block in figures]
    if operation.caption_from not in target_ids:
        raise LayoutOverrideError("group.caption_from must name one of the grouped targets.")
    caption_block = next(block for block in figures if block.id == operation.caption_from)
    _, caption = _caption_parts(caption_block)
    if not caption:
        raise LayoutOverrideError("The selected caption source has no group caption.")
    members: list[FigureMember] = []
    for block in figures:
        label, _ = _caption_parts(block)
        if not label or not block.asset:
            raise LayoutOverrideError("Every grouped figure needs an asset and subfigure label.")
        members.append(
            FigureMember(
                id=block.id,
                asset=block.asset,
                label=label,
                page=block.page,
                bbox=block.bbox,
            )
        )
    expected_labels = [f"({chr(ord('a') + index)})" for index in range(len(members))]
    if [member.label for member in members] != expected_labels:
        raise LayoutOverrideError(
            "Grouped figure labels must be consecutive from (a) in bbox order."
        )
    first_index = min(index for index, block in enumerate(blocks) if block.id in target_ids)
    grouped = DocumentBlock(
        id=f"fg-{'-'.join(ids)}",
        kind="figure_group",
        page=figures[0].page,
        caption_source=caption,
        footnote_source=caption_block.footnote_source,
        bbox=_union_bbox(figures),
        members=members,
        layout=operation.layout,
        extra={
            "mineru_type": "figure_group",
            "caption_origin_id": caption_block.id,
            "origin_block_ids": ids,
            "override": True,
        },
    )
    remaining = [block for block in blocks if block.id not in target_ids]
    remaining.insert(first_index, grouped)
    return remaining


def _move(blocks: list[DocumentBlock], operation: LayoutOperation) -> list[DocumentBlock]:
    if len(operation.targets) != 2:
        raise LayoutOverrideError("A move operation requires the moved block and anchor targets.")
    if bool(operation.before_id) == bool(operation.after_id):
        raise LayoutOverrideError("A move operation requires exactly one of before_id or after_id.")
    moved_id = operation.targets[0].id
    anchor_id = operation.before_id or operation.after_id
    if operation.targets[1].id != anchor_id or moved_id == anchor_id:
        raise LayoutOverrideError("The move anchor must be the operation's second target.")
    by_id = {block.id: block for block in blocks}
    if moved_id not in by_id or anchor_id not in by_id:
        raise LayoutOverrideError("A move operation refers to a block changed earlier.")
    result = [block for block in blocks if block.id != moved_id]
    anchor_index = next(index for index, block in enumerate(result) if block.id == anchor_id)
    if operation.after_id:
        anchor_index += 1
    result.insert(anchor_index, by_id[moved_id])
    return result


def _reclassify(blocks: list[DocumentBlock], operation: LayoutOperation) -> list[DocumentBlock]:
    if len(operation.targets) != 1 or operation.kind not in ALLOWED_RECLASSIFICATIONS:
        raise LayoutOverrideError("reclassify requires one target and a supported kind.")
    if operation.kind == "heading" and not (
        operation.level is not None and 1 <= operation.level <= 6
    ):
        raise LayoutOverrideError("reclassify to heading requires level from 1 through 6.")
    if operation.kind != "heading" and operation.level is not None:
        raise LayoutOverrideError("Only heading reclassifications can specify level.")
    target_id = operation.targets[0].id
    result = [block.model_copy(deep=True) for block in blocks]
    target = next((block for block in result if block.id == target_id), None)
    if target is None:
        raise LayoutOverrideError("A reclassify target was changed by an earlier operation.")
    target.kind = operation.kind
    if operation.kind == "heading":
        assert operation.level is not None
        target.level = operation.level
    else:
        target.level = 0
    target.extra["override"] = True
    if operation.kind == "heading":
        target.extra["heading_level_origin"] = "override"
        target.extra.pop("heading_ambiguous", None)
    return result


def _drop(blocks: list[DocumentBlock], operation: LayoutOperation) -> list[DocumentBlock]:
    if len(operation.targets) != 1 or not operation.reason.strip():
        raise LayoutOverrideError("drop requires one target and a non-empty reason.")
    target_id = operation.targets[0].id
    if not any(block.id == target_id for block in blocks):
        raise LayoutOverrideError("A drop target was changed by an earlier operation.")
    return [block for block in blocks if block.id != target_id]


def apply_layout_overrides(
    blocks: list[DocumentBlock], overrides: LayoutOverrides | None
) -> tuple[list[DocumentBlock], int]:
    if overrides is None:
        return blocks, 0
    original = [block.model_copy(deep=True) for block in blocks]
    by_id = {block.id: block for block in original}
    if len(by_id) != len(original):
        raise LayoutOverrideError("Document block IDs are not unique before overrides.")
    for operation in overrides.operations:
        if not operation.reason.strip() or not operation.targets:
            raise LayoutOverrideError(f"{operation.op} requires targets and a reason.")
        for index, target in enumerate(operation.targets):
            block = by_id.get(target.id)
            if block is None:
                raise LayoutOverrideError(f"Override target does not exist: {target.id}")
            _validate_target(block, operation, index)

    result = original
    for operation in overrides.operations:
        if operation.op == "group":
            result = _group(result, operation)
        elif operation.op == "move":
            result = _move(result, operation)
        elif operation.op == "reclassify":
            result = _reclassify(result, operation)
        else:
            result = _drop(result, operation)
    return result, len(overrides.operations)
