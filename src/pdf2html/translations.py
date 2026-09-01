from __future__ import annotations

from pathlib import Path

from .models import Document, TranslationFile
from .util import read_json


class TranslationError(RuntimeError):
    """Translation files are incomplete or inconsistent."""


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
            target = (entry.target or "").strip()
            if not target or target.casefold() in {"todo", "tbd", "null"}:
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
