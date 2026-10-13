from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from .util import atomic_write_json, read_json, utc_now

PAPER_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?$")


class ShelfMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    paper_id: str
    added_at: str
    source: Literal["built", "git_archive"]

    @field_validator("paper_id")
    @classmethod
    def validate_paper_id(cls, value: str) -> str:
        if not PAPER_ID_PATTERN.fullmatch(value):
            raise ValueError(f"论文 ID 无效：{value}")
        return value

    @field_validator("added_at")
    @classmethod
    def validate_added_at(cls, value: str) -> str:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
            raise ValueError("书架 added_at 必须带有 UTC 时区。")
        return timestamp.astimezone(UTC).isoformat(timespec="seconds")


def shelf_metadata_path(library_root: Path, paper_id: str) -> Path:
    if not PAPER_ID_PATTERN.fullmatch(paper_id):
        raise ValueError(f"论文 ID 无效：{paper_id}")
    root = library_root.resolve()
    shelf_root = root / "shelf"
    path = shelf_root / f"{paper_id}.json"
    if shelf_root.is_symlink() or path.is_symlink():
        raise ValueError(f"书架元数据不能使用符号链接：{path}")
    path.resolve().relative_to(root)
    return path


def read_shelf_metadata(library_root: Path, paper_id: str) -> ShelfMetadata | None:
    path = shelf_metadata_path(library_root, paper_id)
    if not path.exists():
        return None
    metadata = ShelfMetadata.model_validate(read_json(path))
    if metadata.paper_id != paper_id:
        raise ValueError("书架元数据文件名与论文 ID 不一致。")
    return metadata


def record_added_at(
    library_root: Path,
    paper_id: str,
    *,
    added_at: str | None = None,
    source: Literal["built", "git_archive"] = "built",
) -> ShelfMetadata:
    existing = read_shelf_metadata(library_root, paper_id)
    if existing is not None:
        return existing
    metadata = ShelfMetadata(
        paper_id=paper_id,
        added_at=added_at if added_at is not None else utc_now(),
        source=source,
    )
    atomic_write_json(
        shelf_metadata_path(library_root, paper_id), metadata.model_dump(mode="json")
    )
    return metadata
