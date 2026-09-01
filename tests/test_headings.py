from __future__ import annotations

import json

import pytest

from pdf2html.bundle import render_bundle_markdown
from pdf2html.headings import normalize_heading_structure
from pdf2html.layout import LayoutOverrideError, apply_layout_overrides, block_fingerprint
from pdf2html.models import (
    Document,
    DocumentBlock,
    LayoutOperation,
    LayoutOverrides,
    LayoutTarget,
    PaperMetadata,
    SourceReference,
)
from pdf2html.verify import structure_report


def metadata() -> PaperMetadata:
    return PaperMetadata(title="Example Paper", abstract="An abstract.")


def source_reference() -> SourceReference:
    return SourceReference(
        library_prefix="users/0",
        item_key="ABCD2345",
        item_version=1,
        attachment_key="EFGH6789",
        attachment_version=2,
        original_path="C:/Zotero/paper.pdf",
        sha256="a" * 64,
    )


def heading(block_id: str, source: str, bbox: list[float]) -> DocumentBlock:
    return DocumentBlock(
        id=block_id,
        kind="heading",
        page=0,
        level=2,
        source=source,
        bbox=bbox,
    )


def column() -> DocumentBlock:
    return DocumentBlock(
        id="body",
        kind="paragraph",
        page=0,
        source="Body",
        bbox=[0, 0, 400, 500],
    )


def test_decimal_numbering_sets_semantic_body_levels() -> None:
    blocks = [
        heading("intro", "1 Introduction", [120, 10, 280, 30]),
        heading("scope", "1.1 Scope", [0, 40, 120, 60]),
    ]
    result, info = normalize_heading_structure(
        blocks,
        metadata=metadata(),
        middle_json_path=None,
    )
    assert [block.level for block in result] == [2, 3]
    assert [block.extra["heading_level_origin"] for block in result] == [
        "numbering",
        "numbering",
    ]
    assert info["inferred_heading_levels"] == 2


def test_metadata_abstract_duplicate_ignores_only_punctuation() -> None:
    abstract = DocumentBlock(
        id="abstract-body",
        kind="paragraph",
        page=0,
        source="An ab-stract.",
        bbox=[0, 0, 100, 20],
    )
    result, info = normalize_heading_structure(
        [abstract],
        metadata=PaperMetadata(title="Paper", abstract="An abstract."),
        middle_json_path=None,
    )
    assert result[0].extra["front_matter_role"] == "abstract_body"
    assert info["front_matter_duplicates"] == 1


def test_mixed_alignment_title_is_split_and_wrapped_title_is_not(tmp_path) -> None:  # type: ignore[no-untyped-def]
    blocks = [
        column(),
        heading("main-a", "Main A", [150, 10, 250, 30]),
        heading("sub-a", "Sub A", [0, 40, 100, 60]),
        heading("mixed", "Main B Sub B", [0, 70, 400, 110]),
        heading("wrapped", "A long wrapped heading", [100, 120, 300, 160]),
    ]
    payload = {
        "pdf_info": [
            {
                "para_blocks": [
                    {
                        "type": "title",
                        "bbox": [150, 10, 250, 30],
                        "lines": [
                            {
                                "bbox": [150, 10, 250, 30],
                                "spans": [{"type": "text", "content": "Main A"}],
                            }
                        ],
                    },
                    {
                        "type": "title",
                        "bbox": [0, 40, 100, 60],
                        "lines": [
                            {
                                "bbox": [0, 40, 100, 60],
                                "spans": [{"type": "text", "content": "Sub A"}],
                            }
                        ],
                    },
                    {
                        "type": "title",
                        "bbox": [0, 0, 300, 40],
                        "lines": [
                            {
                                "bbox": [100, 0, 200, 15],
                                "spans": [{"type": "text", "content": "Main B"}],
                            },
                            {
                                "bbox": [0, 20, 100, 35],
                                "spans": [{"type": "text", "content": "Sub B"}],
                            },
                        ],
                    },
                    {
                        "type": "title",
                        "bbox": [0, 0, 200, 40],
                        "lines": [
                            {
                                "bbox": [40, 0, 160, 15],
                                "spans": [
                                    {"type": "text", "content": "A long wrapped"}
                                ],
                            },
                            {
                                "bbox": [60, 20, 140, 35],
                                "spans": [{"type": "text", "content": "heading"}],
                            },
                        ],
                    },
                ]
            }
        ]
    }
    middle = tmp_path / "paper_middle.json"
    middle.write_text(json.dumps(payload), encoding="utf-8")
    result, info = normalize_heading_structure(
        blocks,
        metadata=metadata(),
        middle_json_path=middle,
    )
    headings = [block for block in result if block.kind == "heading"]
    assert [(block.id, block.level, block.source) for block in headings] == [
        ("main-a", 2, "Main A"),
        ("sub-a", 3, "Sub A"),
        ("mixed-part01", 2, "Main B"),
        ("mixed-part02", 3, "Sub B"),
        ("wrapped", 2, "A long wrapped heading"),
    ]
    assert info["automatic_heading_splits"] == 1
    assert info["heading_geometry_enabled"] is True


def test_heading_override_requires_level_and_changes_fingerprint() -> None:
    block = heading("section", "Section", [0, 0, 100, 20])
    target = LayoutTarget(
        id=block.id,
        page=block.page,
        bbox=block.bbox,
        block_hash=block_fingerprint(block),
    )
    overrides = LayoutOverrides(
        job_id="job",
        source_sha256="a" * 64,
        content_sha256="b" * 64,
        operations=[
            LayoutOperation(
                op="reclassify",
                targets=[target],
                kind="heading",
                level=3,
                reason="Confirmed subsection.",
            )
        ],
    )
    result, _ = apply_layout_overrides([block], overrides)
    assert result[0].level == 3
    assert result[0].extra["heading_level_origin"] == "override"
    assert block_fingerprint(result[0]) != target.block_hash

    missing_level = overrides.model_copy(deep=True)
    missing_level.operations[0].level = None
    with pytest.raises(LayoutOverrideError, match="requires level"):
        apply_layout_overrides([block], missing_level)

    invalid_nonheading = overrides.model_copy(deep=True)
    invalid_nonheading.operations[0].kind = "paragraph"
    with pytest.raises(LayoutOverrideError, match="Only heading"):
        apply_layout_overrides([block], invalid_nonheading)


def test_structure_report_rejects_body_h1_and_level_jump() -> None:
    title = heading("title", "Example Paper", [0, 0, 200, 30])
    title.level = 1
    title.extra["front_matter_role"] = "document_title"
    section = heading("section", "Section", [0, 40, 200, 60])
    subsection = heading("deep", "Too deep", [0, 70, 200, 90])
    subsection.level = 4
    rogue = heading("rogue", "Rogue", [0, 100, 200, 120])
    rogue.level = 1
    document = Document(
        job_id="job",
        metadata=metadata(),
        source=source_reference(),
        blocks=[title, section, subsection, rogue],
        segments=[],
    )
    report = structure_report(document)
    assert report["status"] == "failed"
    assert any("jumps from level 2 to 4" in error for error in report["errors"])
    assert any("improperly uses level 1" in error for error in report["errors"])


def test_markdown_emits_only_visible_body_headings() -> None:
    title = heading("title", "Example Paper", [0, 0, 200, 30])
    title.level = 1
    title.extra["front_matter_role"] = "document_title"
    section = heading("section", "Section", [0, 40, 200, 60])
    document = Document(
        job_id="job",
        metadata=metadata(),
        source=source_reference(),
        blocks=[title, section],
        segments=[],
    )
    markdown = render_bundle_markdown(
        document,
        {},
        language="en",
        paper_id="paper",
        revision="sha256:test",
        document_sha256="a" * 64,
        media_mapping={},
    )
    assert 'id="title"' not in markdown
    assert 'id="section"' in markdown
    assert markdown.count("# Example Paper") == 1
    assert "## Section" in markdown
