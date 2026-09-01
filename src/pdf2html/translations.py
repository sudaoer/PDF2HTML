from __future__ import annotations

import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

from .document import write_translation_tasks
from .models import Document, TranslationFile
from .util import atomic_write_json, read_json


class TranslationError(RuntimeError):
    """Translation files are incomplete or inconsistent."""


def _completed_target(value: str | None) -> str:
    target = (value or "").strip()
    if not target or target.casefold() in {"todo", "tbd", "null"}:
        return ""
    return target


def load_translations(document: Document, translation_dir: Path) -> dict[str, str]:
    expected = {segment.id: segment for segment in document.segments}
    translations: dict[str, str] = {}
    paths = sorted(translation_dir.glob("part-*.json"))
    if not paths:
        raise TranslationError(f"No translation files found under {translation_dir}.")
    for path in paths:
        payload = TranslationFile.model_validate(read_json(path))
        if payload.job_id != document.job_id:
            raise TranslationError(
                f"{path} belongs to job {payload.job_id}, not {document.job_id}."
            )
        for entry in payload.translations:
            source = expected.get(entry.id)
            if source is None:
                raise TranslationError(f"{path} contains unknown segment {entry.id}.")
            if entry.id in translations:
                raise TranslationError(f"Segment {entry.id} appears in more than one file.")
            if entry.source_hash != source.source_hash or entry.source != source.source:
                raise TranslationError(
                    f"Source text changed for segment {entry.id}; retranslate it."
                )
            target = _completed_target(entry.target)
            if not target:
                raise TranslationError(f"Segment {entry.id} has no completed translation.")
            missing = [token for token in source.protected if token not in target]
            if missing:
                raise TranslationError(
                    f"Segment {entry.id} lost protected tokens: {missing[:3]}"
                )
            translations[entry.id] = target
    missing_ids = sorted(set(expected) - set(translations))
    if missing_ids:
        raise TranslationError(
            f"Missing {len(missing_ids)} translated segments, beginning with {missing_ids[:5]}."
        )
    return translations


def reconcile_translation_artifacts(
    document: Document,
    *,
    task_dir: Path,
    translation_dir: Path,
    history_dir: Path,
    archive_key: str,
    report_path: Path,
) -> dict[str, Any]:
    existing: list[tuple[str, str, str, str, str]] = []
    old_ids: set[str] = set()
    old_by_id: dict[str, tuple[str, str]] = {}
    existing_paths = sorted(translation_dir.glob("part-*.json"))
    for path in existing_paths:
        payload = TranslationFile.model_validate(read_json(path))
        if payload.job_id != document.job_id:
            continue
        for entry in payload.translations:
            old_ids.add(entry.id)
            old_by_id[entry.id] = (entry.source_hash, entry.source)
            target = _completed_target(entry.target)
            if target:
                existing.append(
                    (entry.id, entry.role, entry.source_hash, entry.source, target)
                )

    if existing_paths:
        archive = history_dir / archive_key / "translations"
        archive.mkdir(parents=True, exist_ok=True)
        for path in existing_paths:
            destination = archive / path.name
            if not destination.exists():
                shutil.copy2(path, destination)

    candidates: dict[tuple[str, str, str], list[tuple[str, str]]] = defaultdict(list)
    for entry_id, role, source_hash, source, target in existing:
        candidates[(role, source_hash, source)].append((entry_id, target))

    reused_targets: dict[str, str] = {}
    reused_old_ids: set[str] = set()
    invalidated = 0
    for segment in document.segments:
        matches = candidates.get((segment.role, segment.source_hash, segment.source), [])
        unique_targets = {target for _, target in matches}
        if len(matches) == 1 and len(unique_targets) == 1:
            old_id, target = matches[0]
            if all(token in target for token in segment.protected):
                reused_targets[segment.id] = target
                reused_old_ids.add(old_id)
                continue
        old_source = old_by_id.get(segment.id)
        if old_source is not None and old_source != (segment.source_hash, segment.source):
            invalidated += 1

    write_translation_tasks(document, task_dir, targets=reused_targets)
    write_translation_tasks(document, translation_dir, targets=reused_targets)
    report: dict[str, Any] = {
        "job_id": document.job_id,
        "segments": len(document.segments),
        "reused": len(reused_targets),
        "pending": len(document.segments) - len(reused_targets),
        "invalidated": invalidated,
        "obsolete": len(old_ids - reused_old_ids),
        "archive": str((history_dir / archive_key / "translations").resolve())
        if existing_paths
        else "",
    }
    atomic_write_json(report_path, report)
    return report
