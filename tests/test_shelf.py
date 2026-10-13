from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_pipeline import prepared_document, write_test_web_root

from pdf2html import shelf
from pdf2html.bundle import build_bundle
from pdf2html.server import create_app
from pdf2html.shelf import read_shelf_metadata, record_added_at
from pdf2html.util import atomic_write_json, read_json


def test_first_successful_build_records_time_and_rebuild_preserves_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    document, content_path = prepared_document(tmp_path)
    translations = {segment.id: f"译文：{segment.source}" for segment in document.segments}
    library = tmp_path / "library"
    monkeypatch.setattr(shelf, "utc_now", lambda: "2026-10-01T12:00:00+00:00")
    first = build_bundle(
        document, translations, content_list_path=content_path, library_root=library
    )
    metadata_path = library / "shelf" / f"{first.paper_id}.json"
    first_metadata = metadata_path.read_bytes()
    assert read_json(metadata_path) == {
        "paper_id": first.paper_id,
        "added_at": "2026-10-01T12:00:00+00:00",
        "source": "built",
    }
    monkeypatch.setattr(shelf, "utc_now", lambda: "2026-10-12T12:00:00+00:00")
    translations["meta.title"] = "路由网络"
    second = build_bundle(
        document, translations, content_list_path=content_path, library_root=library
    )
    assert first.revision != second.revision
    assert metadata_path.read_bytes() == first_metadata


def test_failed_build_does_not_record_added_at(tmp_path: Path) -> None:
    document, content_path = prepared_document(tmp_path)
    library = tmp_path / "library"
    with pytest.raises(KeyError):
        build_bundle(document, {}, content_list_path=content_path, library_root=library)
    assert not (library / "shelf").exists()


def test_archive_time_is_utc_and_is_not_overwritten(tmp_path: Path) -> None:
    first = record_added_at(
        tmp_path, "paper-abcd2345", added_at="2026-09-01T10:00:00Z", source="git_archive"
    )
    second = record_added_at(tmp_path, "paper-abcd2345", added_at="2026-10-01T10:00:00Z")
    assert first == second
    assert first.added_at == "2026-09-01T10:00:00+00:00"
    assert first.source == "git_archive"


def test_shelf_time_updates_etag_without_changing_bundle_and_service_stays_read_only(
    tmp_path: Path,
) -> None:
    document, content_path = prepared_document(tmp_path)
    translations = {segment.id: f"译文：{segment.source}" for segment in document.segments}
    library = tmp_path / "library"
    bundle = build_bundle(
        document, translations, content_list_path=content_path, library_root=library
    )
    metadata_path = library / "shelf" / f"{bundle.paper_id}.json"
    metadata_path.unlink()
    (library / "shelf").rmdir()
    bundle_bytes = bundle.bundle_path.read_bytes()
    client = TestClient(create_app(library_root=library, web_root=write_test_web_root(tmp_path)))
    first = client.get("/api/shelf")
    assert first.status_code == 200
    assert first.json()["papers"][0]["added_at"] is None
    assert not (library / "shelf").exists()

    record_added_at(library, bundle.paper_id, added_at="2026-09-01T10:00:00Z")
    before = {
        path.relative_to(library): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in library.rglob("*") if path.is_file()
    }
    changed = client.get("/api/shelf", headers={"If-None-Match": first.headers["etag"]})
    assert changed.status_code == 200
    assert changed.headers["etag"] != first.headers["etag"]
    assert changed.json()["papers"][0]["added_at"] == "2026-09-01T10:00:00+00:00"
    assert client.get(
        "/api/shelf", headers={"If-None-Match": changed.headers["etag"]}
    ).status_code == 304
    assert bundle.bundle_path.read_bytes() == bundle_bytes
    after = {
        path.relative_to(library): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in library.rglob("*") if path.is_file()
    }
    assert before == after
    metadata_path.unlink()
    removed = client.get("/api/shelf", headers={"If-None-Match": changed.headers["etag"]})
    assert removed.status_code == 200
    assert removed.json()["papers"][0]["added_at"] is None


def test_corrupt_shelf_metadata_is_reported_and_preserved(tmp_path: Path) -> None:
    document, content_path = prepared_document(tmp_path)
    translations = {segment.id: f"译文：{segment.source}" for segment in document.segments}
    library = tmp_path / "library"
    bundle = build_bundle(
        document, translations, content_list_path=content_path, library_root=library
    )
    metadata_path = library / "shelf" / f"{bundle.paper_id}.json"
    metadata_path.write_text("{", encoding="utf-8")
    client = TestClient(create_app(library_root=library, web_root=write_test_web_root(tmp_path)))
    response = client.get("/api/shelf")
    assert response.status_code == 500
    assert response.json()["code"] == "invalid_shelf_metadata"
    assert client.get(f"/papers/{bundle.paper_id}/paper.json").status_code == 200
    with pytest.raises(ValueError):
        build_bundle(
            document, translations, content_list_path=content_path, library_root=library
        )
    assert metadata_path.read_text(encoding="utf-8") == "{"


@pytest.mark.parametrize("paper_id", ["../outside", "UPPERCASE", "paper/name"])
def test_shelf_metadata_rejects_unsafe_ids(tmp_path: Path, paper_id: str) -> None:
    with pytest.raises(ValueError, match="论文 ID 无效"):
        record_added_at(tmp_path, paper_id)
    assert not (tmp_path / "shelf").exists()


@pytest.mark.parametrize("added_at", ["2026-09-01", "2026-09-01T10:00:00+08:00", "invalid"])
def test_shelf_metadata_rejects_non_utc_timestamps(tmp_path: Path, added_at: str) -> None:
    with pytest.raises(ValueError):
        record_added_at(tmp_path, "paper-abcd2345", added_at=added_at)
    assert not (tmp_path / "shelf").exists()


def test_shelf_metadata_rejects_mismatched_id_and_symlinks(tmp_path: Path) -> None:
    path = tmp_path / "shelf" / "paper-abcd2345.json"
    atomic_write_json(path, {
        "paper_id": "other-abcd2345", "added_at": "2026-09-01T10:00:00Z", "source": "built",
    })
    with pytest.raises(ValueError, match="文件名与论文 ID 不一致"):
        read_shelf_metadata(tmp_path, "paper-abcd2345")
    path.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    path.symlink_to(outside)
    with pytest.raises(ValueError, match="不能使用符号链接"):
        record_added_at(tmp_path, "paper-abcd2345")
    assert outside.read_text(encoding="utf-8") == "{}"
