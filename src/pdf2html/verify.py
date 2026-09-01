from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .bundle_verify import BundleVerificationError, verify_bundle
from .headings import is_front_matter_duplicate
from .models import Document
from .util import atomic_write_json

SUBFIGURE_ONLY = re.compile(r"^\s*\([a-z]\)\s*$", re.IGNORECASE)
TEXT_KINDS = {"paragraph", "heading", "list", "aside", "page_footnote"}


def structure_report(
    document: Document,
    *,
    extra_errors: list[str] | None = None,
) -> dict[str, Any]:
    errors = list(extra_errors or [])
    heading_warnings = document.layout_info.get("heading_warnings", [])
    warnings = (
        [str(item) for item in heading_warnings]
        if isinstance(heading_warnings, list)
        else []
    )
    block_ids = [block.id for block in document.blocks]
    if len(block_ids) != len(set(block_ids)):
        errors.append("Document block IDs are not unique.")
    segment_ids = [segment.id for segment in document.segments]
    if len(segment_ids) != len(set(segment_ids)):
        errors.append("Document segment IDs are not unique.")
    member_ids: list[str] = []
    for block in document.blocks:
        if block.kind in TEXT_KINDS and not block.source.strip():
            errors.append(f"Empty body block remains: {block.id}")
        if block.kind in {"aside", "page_footnote", "front_note", "endnote"}:
            errors.append(f"Footnote-like block remains in the body: {block.id}")
        if block.kind == "heading":
            if not 1 <= block.level <= 6:
                errors.append(f"Heading {block.id} has invalid level {block.level}.")
            if block.extra.get("heading_ambiguous"):
                errors.append(f"Heading {block.id} has unresolved line-level hierarchy.")
        elif block.level != 0:
            errors.append(f"Non-heading block {block.id} retains heading level {block.level}.")
        if block.kind == "figure":
            if not block.asset:
                errors.append(f"Figure {block.id} has no asset.")
            if SUBFIGURE_ONLY.fullmatch(block.caption_source):
                errors.append(f"Figure {block.id} has an orphaned subfigure label.")
        if block.kind == "figure_group":
            if len(block.members) < 2:
                errors.append(f"Figure group {block.id} has fewer than two members.")
            if not block.caption_source.strip():
                errors.append(f"Figure group {block.id} has no shared caption.")
            labels = [member.label.casefold() for member in block.members]
            expected_labels = [
                f"({chr(ord('a') + index)})" for index in range(len(block.members))
            ]
            if any(not member.asset or not member.label for member in block.members):
                errors.append(f"Figure group {block.id} has an incomplete member.")
            if len(labels) != len(set(labels)):
                errors.append(f"Figure group {block.id} has duplicate member labels.")
            if labels != expected_labels:
                errors.append(
                    f"Figure group {block.id} member labels are not consecutive from (a)."
                )
            if any(member.page != block.page for member in block.members):
                errors.append(f"Figure group {block.id} crosses PDF pages.")
            member_ids.extend(member.id for member in block.members)
    if len(member_ids) != len(set(member_ids)):
        errors.append("Figure member IDs are not unique.")
    note_ids = [note.id for note in [*document.front_matter_notes, *document.endnotes]]
    if len(note_ids) != len(set(note_ids)):
        errors.append("Document note IDs are not unique.")
    outline_blocks = [
        block
        for block in document.blocks
        if block.kind == "heading" and not is_front_matter_duplicate(block)
    ]
    previous_level = 1
    for block in outline_blocks:
        if block.level == 1:
            errors.append(f"Body heading {block.id} improperly uses level 1.")
        if block.level > previous_level + 1:
            errors.append(
                f"Heading {block.id} jumps from level {previous_level} to {block.level}."
            )
        previous_level = block.level
    heading_outline = [
        {
            "id": block.id,
            "page": block.page,
            "level": block.level,
            "source": block.source,
            "source_level": block.extra.get("heading_source_level", block.level),
            "origin": block.extra.get("heading_level_origin", ""),
            "alignment": block.extra.get("heading_alignment", ""),
        }
        for block in outline_blocks
    ]
    return {
        "job_id": document.job_id,
        "status": "failed" if errors else "passed",
        "blocks": len(document.blocks),
        "segments": len(document.segments),
        "figure_groups": sum(block.kind == "figure_group" for block in document.blocks),
        "front_matter_notes": len(document.front_matter_notes),
        "endnotes": len(document.endnotes),
        "normalization": {
            key: value
            for key, value in document.layout_info.items()
            if key not in {"override_targets", "heading_warnings"}
        },
        "heading_outline": heading_outline,
        "override_targets": document.layout_info.get("override_targets", []),
        "warnings": warnings,
        "errors": errors,
    }


def write_structure_report(
    document: Document,
    report_path: Path,
    *,
    extra_errors: list[str] | None = None,
) -> dict[str, Any]:
    report = structure_report(document, extra_errors=extra_errors)
    atomic_write_json(report_path, report)
    return report


__all__ = [
    "BundleVerificationError",
    "structure_report",
    "verify_bundle",
    "write_structure_report",
]
