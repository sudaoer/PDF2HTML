from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from .layout import block_fingerprint
from .models import DocumentBlock, TextCorrections
from .util import read_json


class TextCorrectionError(RuntimeError):
    """A reviewed text correction is invalid or stale."""


def load_text_corrections(
    path: Path,
    *,
    job_id: str,
    source_sha256: str,
    content_sha256: str,
) -> TextCorrections | None:
    if not path.is_file():
        return None
    try:
        corrections = TextCorrections.model_validate(read_json(path))
    except (ValidationError, ValueError) as exc:
        raise TextCorrectionError(f"Invalid text correction file {path}: {exc}") from exc
    if corrections.job_id != job_id:
        raise TextCorrectionError(
            f"Text corrections belong to {corrections.job_id}, not {job_id}."
        )
    if corrections.source_sha256 != source_sha256:
        raise TextCorrectionError("Text correction source_sha256 no longer matches the PDF.")
    if corrections.content_sha256 != content_sha256:
        raise TextCorrectionError(
            "Text correction content_sha256 no longer matches MinerU output."
        )
    return corrections


def apply_text_corrections(
    blocks: list[DocumentBlock],
    corrections: TextCorrections | None,
    *,
    target_blocks: list[DocumentBlock] | None = None,
) -> tuple[list[DocumentBlock], int]:
    if corrections is None:
        return blocks, 0
    reviewed = target_blocks if target_blocks is not None else blocks
    original = {block.id: block for block in reviewed}
    if len(original) != len(reviewed):
        raise TextCorrectionError("Document block IDs are not unique before text corrections.")
    current = {block.id: block for block in blocks}
    seen: set[tuple[str, str]] = set()
    resolved_ids: list[str] = []
    for correction in corrections.corrections:
        target = correction.target
        block = original.get(target.id)
        if block is None:
            raise TextCorrectionError(f"Text correction target does not exist: {target.id}")
        if block.page != target.page or block.bbox != target.bbox:
            raise TextCorrectionError(f"Text correction target {target.id} moved.")
        if block_fingerprint(block) != target.block_hash:
            raise TextCorrectionError(f"Text correction target {target.id} content changed.")
        destination = current.get(target.id)
        if destination is None and correction.field == "caption_source":
            groups = [
                candidate
                for candidate in blocks
                if candidate.kind == "figure_group"
                and candidate.extra.get("caption_origin_id") == target.id
                and any(member.id == target.id for member in candidate.members)
            ]
            if len(groups) == 1:
                destination = groups[0]
        if destination is None:
            raise TextCorrectionError(
                f"Text correction target {target.id} was removed by a layout override."
            )
        key = (destination.id, correction.field)
        if key in seen:
            raise TextCorrectionError(
                f"Duplicate text correction for {target.id}.{correction.field}."
            )
        seen.add(key)
        if not correction.reason.strip() or not correction.value.strip():
            raise TextCorrectionError(
                f"Text correction for {target.id}.{correction.field} needs a reason and value."
            )
        if not getattr(destination, correction.field).strip():
            raise TextCorrectionError(
                f"Text correction for {target.id}.{correction.field} has no source text."
            )
        resolved_ids.append(destination.id)
    result = [block.model_copy(deep=True) for block in blocks]
    by_id = {block.id: block for block in result}
    for correction, destination_id in zip(corrections.corrections, resolved_ids, strict=True):
        block = by_id[destination_id]
        setattr(block, correction.field, correction.value.strip())
        block.extra["text_corrected"] = True
    return result, len(corrections.corrections)
