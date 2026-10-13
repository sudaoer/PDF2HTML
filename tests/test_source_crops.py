from __future__ import annotations

import json
from math import ceil, floor
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from typer.testing import CliRunner

from pdf2html import cli
from pdf2html.bundle import build_bundle
from pdf2html.document import build_document
from pdf2html.layout import (
    LayoutOverrideError,
    apply_layout_overrides,
    block_fingerprint,
    ensure_source_crop_media,
)
from pdf2html.models import (
    Document,
    DocumentBlock,
    JobManifest,
    LayoutOperation,
    LayoutOverrides,
    LayoutTarget,
    MinerURecord,
    PaperMetadata,
    SourceReference,
    TextCorrection,
    TextCorrections,
)
from pdf2html.render import build_layout_preview
from pdf2html.screenshot import render_pdf_page
from pdf2html.util import atomic_write_json, hash_file, relative_to, utc_now
from pdf2html.verify import verify_bundle


@pytest.fixture
def crop_case(tmp_path: Path) -> tuple[Path, Path, list[DocumentBlock], LayoutOverrides]:
    source = tmp_path / "source.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=1000)
    figures: list[DocumentBlock] = []
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    drawing: list[str] = []
    for row in range(6):
        drawing.append(
            f"0 0 0 rg BT /F1 12 Tf 60 {1000 - 200 - 120 * row} Td (scene {row + 1}) Tj ET"
        )
        for column in range(3):
            x, y = 100 + 220 * column, 100 + 120 * row
            bbox = [float(x), float(y), float(x + 180), float(y + 90)]
            red, green, blue = 30 + row * 25, 50 + column * 75, 100
            drawing.append(
                f"{red / 255} {green / 255} {blue / 255} rg "
                f"{x * 0.6} {1000 - y - 90} {180 * 0.6} 90 re f"
            )
            figures.append(
                DocumentBlock(
                    id=f"b{row * 3 + column:04}",
                    kind="figure",
                    page=0,
                    bbox=bbox,
                    asset=f"images/{row}-{column}.png",
                    caption_source="Figure 6: Six scenes, three columns."
                    if row == 5 and column == 2
                    else "",
                )
            )
    stream = DecodedStreamObject()
    stream.set_data("\n".join(drawing).encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(source)
    content = tmp_path / "content_list.json"
    entries = [
        dict(
            type="image",
            page_idx=block.page,
            bbox=block.bbox,
            img_path=block.asset,
            image_caption=[block.caption_source] if block.caption_source else [],
        )
        for block in figures
    ]
    atomic_write_json(content, entries)
    overrides = LayoutOverrides(
        job_id="source-crop",
        source_sha256=hash_file(source),
        content_sha256=hash_file(content),
        operations=[
            LayoutOperation(
                op="group",
                targets=[
                    LayoutTarget(
                        id=block.id,
                        page=block.page,
                        bbox=block.bbox,
                        block_hash=block_fingerprint(block),
                    )
                    for block in figures
                ],
                caption_from=figures[-1].id,
                source_crop=[90, 80, 750, 850],
                reason="核对源 PDF 的六个场景和三列结果。",
            )
        ],
    )
    return source, content, figures, overrides


def _group(case: tuple[Path, Path, list[DocumentBlock], LayoutOverrides]) -> DocumentBlock:
    source, content, figures, overrides = case
    grouped, count = apply_layout_overrides(
        figures, overrides, source_pdf=source, asset_root=content.parent
    )
    assert count == 1 and len(grouped) == 1
    return grouped[0]


def _select_single_figure(
    case: tuple[Path, Path, list[DocumentBlock], LayoutOverrides],
) -> None:
    _, content, figures, overrides = case
    figures[:] = figures[-1:]
    overrides.operations[0].targets = overrides.operations[0].targets[-1:]
    entries = json.loads(content.read_text())
    atomic_write_json(content, entries[-1:])
    overrides.content_sha256 = hash_file(content)


@pytest.mark.parametrize("single_target", [False, True])
def test_source_crop_preserves_the_full_six_scene_pdf_image(
    crop_case: tuple[Path, Path, list[DocumentBlock], LayoutOverrides],
    single_target: bool,
) -> None:
    if single_target:
        _select_single_figure(crop_case)
    source, content, figures, overrides = crop_case
    crop = _group(crop_case)
    assert crop.kind == "figure" and not crop.members
    assert crop.id == f"fg-{'-'.join(block.id for block in figures)}"
    assert crop.extra["origin_block_ids"] == [block.id for block in figures]
    assert crop.extra["caption_origin_id"] == figures[-1].id
    assert crop.extra["source_crop_sha256"] == hash_file(source)
    assert crop.caption_source == figures[-1].caption_source
    asset = content.parent / crop.asset
    assert asset.parent.name == "pdf2html-crops" and asset.stem == hash_file(asset)
    full = render_pdf_page(source, content.parent / "full.png", page=1)
    with Image.open(full) as image, Image.open(asset) as actual:
        bbox = overrides.operations[0].source_crop
        assert bbox is not None
        expected = image.crop(
            (
                floor(bbox[0] * image.width / 1000),
                floor(bbox[1] * image.height / 1000),
                ceil(bbox[2] * image.width / 1000),
                ceil(bbox[3] * image.height / 1000),
            )
        )
        assert actual.size == expected.size
        assert actual.tobytes() == expected.tobytes()
        for block in figures:
            x = (block.bbox[0] + block.bbox[2]) / 2
            y = (block.bbox[1] + block.bbox[3]) / 2
            pixel = actual.getpixel(
                (
                    int(x * image.width / 1000) - floor(bbox[0] * image.width / 1000),
                    int(y * image.height / 1000) - floor(bbox[1] * image.height / 1000),
                )
            )
            assert pixel != (255, 255, 255)
    assert all(block.kind == "figure" for block in figures)


@pytest.mark.parametrize(
    "bbox",
    [
        [],
        [0, 0, 1],
        [-1, 0, 750, 850],
        [0, 0, 1001, 850],
        [0, 0, 0, 850],
        [750, 80, 90, 850],
        [0, float("nan"), 750, 850],
        [0, 0, float("inf"), 850],
        [110, 80, 750, 850],
    ],
)
def test_source_crop_rejects_invalid_or_incomplete_bounds(
    crop_case: tuple[Path, Path, list[DocumentBlock], LayoutOverrides],
    bbox: list[float],
) -> None:
    crop_case[-1].operations[0].source_crop = bbox
    with pytest.raises(LayoutOverrideError, match="bbox"):
        _group(crop_case)


@pytest.mark.parametrize(
    "condition",
    [
        "cross-page",
        "labels",
        "caption",
        "caption-origin",
        "source-hash",
        "missing-pdf",
        "stale-target",
        "other-operation",
    ],
)
def test_source_crop_keeps_source_and_target_preconditions(
    crop_case: tuple[Path, Path, list[DocumentBlock], LayoutOverrides],
    condition: str,
) -> None:
    source, _, figures, overrides = crop_case
    op = overrides.operations[0]
    if condition == "cross-page":
        figures[0].page = 1
        op.targets[0].page = 1
        op.targets[0].block_hash = block_fingerprint(figures[0])
    elif condition == "labels":
        op.labels = {block.id: "(a)" for block in figures}
    elif condition == "caption":
        figures[-1].caption_source = "(a)"
        op.targets[-1].block_hash = block_fingerprint(figures[-1])
    elif condition == "caption-origin":
        op.caption_from = "missing"
    elif condition == "source-hash":
        overrides.source_sha256 = "0" * 64
    elif condition == "missing-pdf":
        source.unlink()
    elif condition == "stale-target":
        op.targets[-1].block_hash = "0" * 64
    else:
        op.op = "drop"
    with pytest.raises(LayoutOverrideError):
        _group(crop_case)


@pytest.mark.parametrize(
    "condition",
    [
        "empty-targets",
        "duplicate-targets",
        "ordinary-group",
        "labels",
        "caption-origin",
        "missing-asset",
        "non-figure",
        "stale-page",
        "stale-bbox",
        "stale-target",
    ],
)
def test_single_source_crop_keeps_group_and_target_preconditions(
    crop_case: tuple[Path, Path, list[DocumentBlock], LayoutOverrides],
    condition: str,
) -> None:
    _select_single_figure(crop_case)
    _, _, figures, overrides = crop_case
    op = overrides.operations[0]
    if condition == "empty-targets":
        op.targets = []
    elif condition == "duplicate-targets":
        op.targets.append(op.targets[0].model_copy(deep=True))
    elif condition == "ordinary-group":
        op.source_crop = None
    elif condition == "labels":
        op.labels = {figures[0].id: "(a)"}
    elif condition == "caption-origin":
        op.caption_from = "missing"
    elif condition == "missing-asset":
        figures[0].asset = ""
        op.targets[0].block_hash = block_fingerprint(figures[0])
    elif condition == "non-figure":
        figures[0].kind = "paragraph"
        op.targets[0].block_hash = block_fingerprint(figures[0])
    elif condition == "stale-page":
        op.targets[0].page = 1
    elif condition == "stale-bbox":
        op.targets[0].bbox = [0, 0, 1000, 1000]
    else:
        op.targets[0].block_hash = "0" * 64
    with pytest.raises(LayoutOverrideError):
        _group(crop_case)


@pytest.mark.parametrize("single_target", [False, True])
def test_source_crop_rebuilds_missing_media_and_rejects_changed_media(
    crop_case: tuple[Path, Path, list[DocumentBlock], LayoutOverrides],
    single_target: bool,
) -> None:
    if single_target:
        _select_single_figure(crop_case)
    source, content, _, overrides = crop_case
    crop = _group(crop_case)
    asset = content.parent / crop.asset
    expected = asset.read_bytes()
    asset.unlink()
    ensure_source_crop_media(
        [crop],
        source_pdf=source,
        asset_root=content.parent,
        source_sha256=overrides.source_sha256,
    )
    assert asset.read_bytes() == expected
    asset.write_bytes(b"changed")
    with pytest.raises(LayoutOverrideError, match="content changed"):
        ensure_source_crop_media(
            [crop],
            source_pdf=source,
            asset_root=content.parent,
            source_sha256=overrides.source_sha256,
        )
    assert asset.read_bytes() == b"changed"


def test_crop_temp_symlink_cannot_redirect_writes(
    crop_case: tuple[Path, Path, list[DocumentBlock], LayoutOverrides],
    tmp_path: Path,
) -> None:
    crop = _group(crop_case)
    asset = tmp_path / crop.asset
    asset.unlink()
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.write_bytes(b"untouched")
    staged = asset.with_name(f".{asset.name}.tmp")
    staged.symlink_to(outside)
    rebuilt = _group(crop_case)
    assert rebuilt.asset == crop.asset
    assert outside.read_bytes() == b"untouched"
    assert staged.is_symlink()


@pytest.mark.parametrize("field", ["source_sha256", "content_sha256"])
@pytest.mark.parametrize("single_target", [False, True])
def test_crop_document_rejects_mismatched_source_and_content_hashes(
    crop_case: tuple[Path, Path, list[DocumentBlock], LayoutOverrides],
    field: str,
    single_target: bool,
) -> None:
    if single_target:
        _select_single_figure(crop_case)
    source_pdf, content, _, overrides = crop_case
    source = SourceReference(
        library_prefix="users/0",
        item_key="ABCD2345",
        item_version=1,
        attachment_key="EFGH6789",
        attachment_version=1,
        original_path="/mnt/unread/source.pdf",
        sha256=hash_file(source_pdf),
    )
    setattr(overrides, field, "0" * 64)
    with pytest.raises(LayoutOverrideError, match=field):
        build_document(
            job_id="source-crop",
            metadata=PaperMetadata(title="Six scenes"),
            source=source,
            content_list_path=content,
            overrides=overrides,
            source_pdf=source_pdf,
        )
    assert not (content.parent / "pdf2html-crops").exists()


@pytest.mark.parametrize("single_target", [False, True])
def test_crop_document_caption_correction_preview_and_bundle(
    crop_case: tuple[Path, Path, list[DocumentBlock], LayoutOverrides],
    tmp_path: Path,
    single_target: bool,
) -> None:
    if single_target:
        _select_single_figure(crop_case)
    source_pdf, content, _, overrides = crop_case
    source = SourceReference(
        library_prefix="users/0",
        item_key="ABCD2345",
        item_version=1,
        attachment_key="EFGH6789",
        attachment_version=1,
        original_path="/mnt/unread/source.pdf",
        sha256=hash_file(source_pdf),
    )
    metadata = PaperMetadata(title="Six reviewed scenes")
    initial = build_document(
        job_id="source-crop",
        metadata=metadata,
        source=source,
        content_list_path=content,
    )
    overrides.operations[0].targets = [
        LayoutTarget(
            id=block.id, page=block.page, bbox=block.bbox, block_hash=block_fingerprint(block)
        )
        for block in initial.blocks
    ]
    overrides.operations[0].caption_from = initial.blocks[-1].id
    origin = initial.blocks[-1]
    corrections = TextCorrections(
        job_id="source-crop",
        source_sha256=source.sha256,
        content_sha256=hash_file(content),
        corrections=[
            TextCorrection(
                target=overrides.operations[0].targets[-1],
                field="caption_source",
                value="Figure 6: Corrected complete caption.",
                reason="源 PDF 图注逐字核对。",
            )
        ],
    )
    document = build_document(
        job_id="source-crop",
        metadata=metadata,
        source=source,
        content_list_path=content,
        overrides=overrides,
        text_corrections=corrections,
        source_pdf=source_pdf,
    )
    assert document.blocks[0].caption_source == corrections.corrections[0].value
    assert document.blocks[0].caption_segment_id == f"{origin.id}.caption"
    preview = build_layout_preview(
        document, content_list_path=content, preview_root=tmp_path / "preview"
    )
    assert preview.is_file() and list((preview.parent / "media").glob("*.png"))
    result = build_bundle(
        document,
        {s.id: s.source for s in document.segments},
        content_list_path=content,
        library_root=tmp_path / "library",
    )
    assert verify_bundle(result.bundle_path)["status"] == "passed"
    (content.parent / document.blocks[0].asset).unlink()
    rebuilt = build_document(
        job_id="source-crop",
        metadata=metadata,
        source=source,
        content_list_path=content,
        overrides=overrides,
        text_corrections=corrections,
        source_pdf=source_pdf,
    )
    assert rebuilt.model_dump() == document.model_dump()
    now = utc_now()
    job_dir = tmp_path / ".work/jobs/source-crop"
    job_dir.mkdir(parents=True)
    (job_dir / "source.pdf").write_bytes(source_pdf.read_bytes())
    atomic_write_json(job_dir / "layout_overrides.json", overrides.model_dump(mode="json"))
    atomic_write_json(job_dir / "text_corrections.json", corrections.model_dump(mode="json"))
    manifest = JobManifest(
        job_id="source-crop",
        created_at=now,
        updated_at=now,
        status="prepared",
        workspace=str(tmp_path),
        metadata=metadata,
        source=source,
        mineru=MinerURecord(content_list_path=relative_to(content, tmp_path)),
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(cli, "_root", lambda: tmp_path)
        assert cli._write_layout(manifest)["status"] == "passed"
        reviewed = Document.model_validate(
            json.loads((tmp_path / manifest.layout_document_path).read_text())
        )
        cli._ensure_source_crops(manifest, reviewed)
        altered = reviewed.model_copy(deep=True)
        altered.blocks[0].extra["source_crop"][0] -= 1
        with pytest.raises(LayoutOverrideError, match="reviewed layout"):
            cli._ensure_source_crops(manifest, altered)
        altered.blocks[0].extra.pop("source_crop")
        with pytest.raises(LayoutOverrideError, match="reviewed layout"):
            cli._ensure_source_crops(manifest, altered)
        layout_path = tmp_path / manifest.layout_document_path
        original_layout = layout_path.read_bytes()
        atomic_write_json(layout_path, altered.model_dump(mode="json"))
        with pytest.raises(ValueError, match="reviewed layout document changed"):
            cli._ensure_source_crops(manifest, altered)
        layout_path.write_bytes(original_layout)
        runner = CliRunner()
        assert runner.invoke(cli.app, ["finalize-layout", "--job", manifest.job_id]).exit_code == 0
        for path in (job_dir / "translations").glob("part-*.json"):
            payload = json.loads(path.read_text())
            for entry in payload["translations"]:
                entry["target"] = entry["source"]
            atomic_write_json(path, payload)
        asset = content.parent / reviewed.blocks[0].asset
        asset.unlink()
        assert runner.invoke(cli.app, ["build", "--job", manifest.job_id]).exit_code == 0
        assert asset.is_file()
        asset.unlink()
        assert runner.invoke(cli.app, ["verify", "--job", manifest.job_id]).exit_code == 0
        assert asset.is_file()
        asset.write_bytes(b"changed")
        rejected = runner.invoke(cli.app, ["verify", "--job", manifest.job_id])
        assert rejected.exit_code == 1 and "content changed" in rejected.output
