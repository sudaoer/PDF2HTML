from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from pdf2html.document import build_document
from pdf2html.layout import (
    LayoutOverrideError,
    apply_layout_overrides,
    block_fingerprint,
    load_layout_overrides,
)
from pdf2html.models import (
    DocumentBlock,
    LayoutOperation,
    LayoutOverrides,
    LayoutTarget,
    PaperMetadata,
    SourceReference,
    TextCorrection,
    TextCorrections,
)
from pdf2html.text_corrections import TextCorrectionError, apply_text_corrections
from pdf2html.util import hash_file
from pdf2html.verify import structure_report


def group_overrides(
    figures: list[DocumentBlock], labels: dict[str, str] | None
) -> LayoutOverrides:
    return LayoutOverrides(
        job_id="reviewed-panels",
        source_sha256="a" * 64,
        content_sha256="b" * 64,
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
                    for block in reversed(figures)
                ],
                labels=labels,
                caption_from=figures[-1].id,
                layout="grid",
                reason="Panel labels and shared caption checked against the PDF.",
            )
        ],
    )


def caption_corrections(block: DocumentBlock, value: str) -> TextCorrections:
    return TextCorrections(
        job_id="reviewed-panels",
        source_sha256="a" * 64,
        content_sha256="b" * 64,
        corrections=[
            TextCorrection(
                target=LayoutTarget(
                    id=block.id,
                    page=block.page,
                    bbox=block.bbox,
                    block_hash=block_fingerprint(block),
                ),
                field="caption_source",
                value=value,
                reason="Shared caption checked against the source PDF.",
            )
        ],
    )


@pytest.fixture
def figures() -> list[DocumentBlock]:
    return [
        DocumentBlock(
            id="a",
            kind="figure",
            page=0,
            asset="images/a.png",
            bbox=[0, 0, 100, 100],
        ),
        DocumentBlock(
            id="b",
            kind="figure",
            page=0,
            asset="images/b.png",
            caption_source="Figure 1: Input and output.",
            bbox=[100, 0, 200, 100],
        ),
    ]


@pytest.mark.parametrize("panel_count", [2, 4])
@pytest.mark.parametrize("correct_caption", [False, True])
def test_explicit_labels_group_chart_assets_with_empty_captions(
    tmp_path: Path, panel_count: int, correct_caption: bool
) -> None:
    image_dir = tmp_path / "images"
    image_dir.mkdir()
    caption = "Figure 1: Results for (a) input and (b) output."
    entries = []
    for index in range(panel_count):
        asset = f"images/{index}.png"
        Image.new("RGB", (20, 20), "white").save(tmp_path / asset)
        x, y = 100 * (index % 2), 100 * (index // 2)
        entries.append(
            {
                "type": "chart",
                "img_path": asset,
                "page_idx": 0,
                "bbox": [x, y, x + 90, y + 90],
                "chart_caption": [caption] if index == panel_count - 1 else [],
                "chart_footnote": ["Shared note."] if index == panel_count - 1 else [],
            }
        )
    content_path = tmp_path / "content_list.json"
    content_path.write_text(json.dumps(entries), encoding="utf-8")
    source = SourceReference(
        library_prefix="users/0",
        item_key="ABCD2345",
        item_version=1,
        attachment_key="EFGH6789",
        attachment_version=1,
        original_path="paper.pdf",
        sha256="a" * 64,
    )
    metadata = PaperMetadata(title="Reviewed panels")
    content_hash = hash_file(content_path)
    initial = build_document(
        job_id="reviewed-panels",
        metadata=metadata,
        source=source,
        content_list_path=content_path,
        content_sha256=content_hash,
    )
    assert [block.caption_source for block in initial.blocks] == [
        *([""] * (panel_count - 1)),
        caption,
    ]
    labels = {
        block.id: f"({chr(ord('a') + index)})"
        for index, block in enumerate(initial.blocks)
    }
    overrides = group_overrides(initial.blocks, labels)
    overrides.content_sha256 = content_hash
    corrections = None
    expected_caption = caption
    if correct_caption:
        expected_caption = caption.replace("Results", "Corrected results")
        corrections = caption_corrections(initial.blocks[-1], expected_caption)
        corrections.content_sha256 = content_hash
    document = build_document(
        job_id="reviewed-panels",
        metadata=metadata,
        source=source,
        content_list_path=content_path,
        content_sha256=content_hash,
        overrides=overrides,
        text_corrections=corrections,
    )
    assert len(document.blocks) == 1
    group = document.blocks[0]
    assert group.kind == "figure_group"
    assert [member.id for member in group.members] == [block.id for block in initial.blocks]
    assert [member.label for member in group.members] == list(labels.values())
    assert all((tmp_path / member.asset).is_file() for member in group.members)
    assert group.caption_source == expected_caption
    assert group.caption_segment_id == f"{initial.blocks[-1].id}.caption"
    caption_segment = next(
        segment for segment in document.segments if segment.id == group.caption_segment_id
    )
    assert caption_segment.source == expected_caption
    assert group.footnote_source == "Shared note."
    assert initial.blocks[0].caption_source == ""
    assert initial.blocks[-1].caption_source == caption
    assert structure_report(document)["status"] == "passed"


@pytest.mark.parametrize(
    ("labels", "message"),
    [
        ({}, "every grouped target"),
        ({"a": "(a)"}, "every grouped target"),
        ({"a": "(a)", "b": "(b)", "other": "(c)"}, "every grouped target"),
        ({"a": "(b)", "b": "(a)"}, "consecutive from \\(a\\) in bbox order"),
        ({"a": "(a)", "b": "(c)"}, "consecutive from \\(a\\) in bbox order"),
        ({"a": "(a)", "b": ""}, "asset and subfigure label"),
    ],
)
def test_explicit_labels_require_complete_consecutive_mapping(
    figures: list[DocumentBlock], labels: dict[str, str], message: str
) -> None:
    with pytest.raises(LayoutOverrideError, match=message):
        apply_layout_overrides(figures, group_overrides(figures, labels))


def test_explicit_labels_reject_caption_conflicts(figures: list[DocumentBlock]) -> None:
    figures[0].caption_source = "(b)"
    with pytest.raises(LayoutOverrideError, match="conflicts with its caption label"):
        apply_layout_overrides(figures, group_overrides(figures, {"a": "(a)", "b": "(b)"}))


@pytest.mark.parametrize("labels", [None, {"a": "(a)", "b": "(b)"}])
def test_source_caption_labels_remain_supported(
    figures: list[DocumentBlock], labels: dict[str, str] | None
) -> None:
    figures[0].caption_source = "(a)"
    figures[1].caption_source = "(b) Figure 1: Input and output."
    result, count = apply_layout_overrides(figures, group_overrides(figures, labels))
    assert count == 1
    assert [member.label for member in result[0].members] == ["(a)", "(b)"]
    assert result[0].caption_source == "Figure 1: Input and output."
    assert figures[0].caption_source == "(a)"
    assert figures[1].caption_source == "(b) Figure 1: Input and output."


def test_unlabelled_panels_still_require_explicit_mapping(figures: list[DocumentBlock]) -> None:
    with pytest.raises(LayoutOverrideError, match="asset and subfigure label"):
        apply_layout_overrides(figures, group_overrides(figures, None))


@pytest.mark.parametrize("field", ["page", "bbox", "block_hash"])
def test_explicit_labels_preserve_target_preconditions(
    figures: list[DocumentBlock], field: str
) -> None:
    overrides = group_overrides(figures, {"a": "(a)", "b": "(b)"})
    target = overrides.operations[0].targets[0]
    if field == "page":
        target.page += 1
    elif field == "bbox":
        target.bbox = [1, 1, 2, 2]
    else:
        target.block_hash = "c" * 64
    with pytest.raises(LayoutOverrideError, match="Override target b"):
        apply_layout_overrides(figures, overrides)


@pytest.mark.parametrize("field", ["source_sha256", "content_sha256"])
def test_explicit_labels_preserve_source_hash_preconditions(
    tmp_path: Path, figures: list[DocumentBlock], field: str
) -> None:
    overrides = group_overrides(figures, {"a": "(a)", "b": "(b)"})
    payload = overrides.model_dump(mode="json")
    payload[field] = "c" * 64
    path = tmp_path / "layout_overrides.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(LayoutOverrideError, match=f"{field} no longer matches"):
        load_layout_overrides(
            path,
            job_id=overrides.job_id,
            source_sha256=overrides.source_sha256,
            content_sha256=overrides.content_sha256,
        )


@pytest.mark.parametrize("field", ["page", "bbox", "block_hash"])
def test_group_caption_corrections_require_original_target_preconditions(
    figures: list[DocumentBlock], field: str
) -> None:
    grouped, _ = apply_layout_overrides(
        figures, group_overrides(figures, {"a": "(a)", "b": "(b)"})
    )
    corrections = caption_corrections(figures[-1], "Figure 1: Corrected caption.")
    target = corrections.corrections[0].target
    if field == "page":
        target.page += 1
    elif field == "bbox":
        target.bbox = [1, 1, 2, 2]
    else:
        target.block_hash = "c" * 64
    with pytest.raises(TextCorrectionError, match="Text correction target b"):
        apply_text_corrections(grouped, corrections, target_blocks=figures)
    assert grouped[0].caption_source == figures[-1].caption_source


@pytest.mark.parametrize("target_id", ["a", "b"])
def test_group_members_only_accept_caption_origin_corrections(
    figures: list[DocumentBlock], target_id: str
) -> None:
    figures[-1].source = "Original chart text."
    grouped, _ = apply_layout_overrides(
        figures, group_overrides(figures, {"a": "(a)", "b": "(b)"})
    )
    target = next(block for block in figures if block.id == target_id)
    corrections = caption_corrections(target, "Corrected text.")
    if target_id == "b":
        corrections.corrections[0].field = "source"
    with pytest.raises(TextCorrectionError, match="was removed by a layout override"):
        apply_text_corrections(grouped, corrections, target_blocks=figures)


@pytest.mark.parametrize("condition", ["removed", "duplicate", "member", "origin", "kind"])
def test_group_caption_correction_requires_unique_group_with_origin_member(
    figures: list[DocumentBlock], condition: str
) -> None:
    grouped, _ = apply_layout_overrides(
        figures, group_overrides(figures, {"a": "(a)", "b": "(b)"})
    )
    corrections = caption_corrections(figures[-1], "Figure 1: Corrected caption.")
    if condition == "removed":
        grouped.clear()
    elif condition == "duplicate":
        grouped.append(grouped[0].model_copy(update={"id": "duplicate"}, deep=True))
    elif condition == "member":
        grouped[0].members = [grouped[0].members[0]]
    elif condition == "origin":
        grouped[0].extra["caption_origin_id"] = "a"
    else:
        grouped[0].kind = "figure"
    with pytest.raises(TextCorrectionError, match="was removed by a layout override"):
        apply_text_corrections(grouped, corrections, target_blocks=figures)


def test_group_caption_corrections_reject_duplicates_transactionally(
    figures: list[DocumentBlock],
) -> None:
    grouped, _ = apply_layout_overrides(
        figures, group_overrides(figures, {"a": "(a)", "b": "(b)"})
    )
    corrections = caption_corrections(figures[-1], "Figure 1: Corrected caption.")
    corrections.corrections.append(corrections.corrections[0].model_copy(deep=True))
    with pytest.raises(TextCorrectionError, match="Duplicate text correction"):
        apply_text_corrections(grouped, corrections, target_blocks=figures)
    assert grouped[0].caption_source == figures[-1].caption_source
