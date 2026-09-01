from __future__ import annotations

import json
from pathlib import Path

import pytest

from pdf2html.document import build_document, write_document, write_translation_tasks
from pdf2html.layout import LayoutOverrideError, apply_layout_overrides, block_fingerprint
from pdf2html.models import (
    DocumentBlock,
    JobManifest,
    LayoutOperation,
    LayoutOverrides,
    LayoutTarget,
    MinerURecord,
    PaperMetadata,
    SourceReference,
    TranslationFile,
)
from pdf2html.render import build_paper, render_markdown
from pdf2html.translations import (
    TranslationError,
    load_translations,
    reconcile_translation_artifacts,
)
from pdf2html.util import atomic_write_json, hash_file, hash_text, read_json, slugify
from pdf2html.verify import VerificationError, structure_report, verify_output
from pdf2html.zotero import ZoteroClient


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


def write_content_list(root: Path) -> Path:
    images = root / "images"
    images.mkdir(parents=True)
    (images / "figure.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    payload = [
        {"type": "header", "text": "Journal header", "page_idx": 0},
        {"type": "text", "text": "1 Introduction", "text_level": 1, "page_idx": 0},
        {
            "type": "text",
            "text": "Accuracy is $x^2$ as shown in [1].",
            "page_idx": 0,
        },
        {
            "type": "image",
            "img_path": "images/figure.png",
            "image_caption": ["Figure 1: Routing overview."],
            "page_idx": 1,
        },
        {
            "type": "table",
            "table_body": "| Method | Dice |\n|---|---|\n| Ours | 0.95 |",
            "table_caption": ["Table 1: Results."],
            "page_idx": 1,
        },
        {"type": "equation", "text": "E = mc^2", "page_idx": 2},
        {
            "type": "list",
            "sub_type": "ref_text",
            "list_items": ["[1] A. Author. Paper."],
            "page_idx": 2,
        },
    ]
    path = root / "paper_content_list.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def prepared_document(tmp_path: Path):  # type: ignore[no-untyped-def]
    content_path = write_content_list(tmp_path)
    metadata = PaperMetadata(
        title="Routing Networks",
        creators=["Alice Example", "Bob Example"],
        date="2026",
        abstract="We route landmarks robustly.",
    )
    document = build_document(
        job_id="routing-abcd2345-aaaa",
        metadata=metadata,
        source=source_reference(),
        content_list_path=content_path,
    )
    return document, content_path


def fill_tasks(task_dir: Path, translation_dir: Path) -> None:
    translation_dir.mkdir(parents=True)
    for task in task_dir.glob("part-*.json"):
        payload = TranslationFile.model_validate(read_json(task))
        for entry in payload.translations:
            entry.target = f"译文：{entry.source}"
        (translation_dir / task.name).write_text(
            payload.model_dump_json(indent=2), encoding="utf-8"
        )


def test_document_tasks_render_and_verify(tmp_path: Path) -> None:
    document, content_path = prepared_document(tmp_path)
    assert len(document.blocks) == 6
    assert document.blocks[0].kind == "heading"
    assert document.blocks[-1].kind == "reference"
    task_dir = tmp_path / "translation_tasks"
    translation_dir = tmp_path / "translations"
    write_translation_tasks(document, task_dir, max_source_chars=80)
    fill_tasks(task_dir, translation_dir)
    translations = load_translations(document, translation_dir)
    html_path = build_paper(
        document,
        translations,
        content_list_path=content_path,
        library_root=tmp_path / "output" / "library",
    )
    report = verify_output(
        document,
        translation_dir,
        html_path,
        tmp_path / "verification.json",
    )
    assert report["status"] == "passed"
    assert report["images"] == 1
    assert (tmp_path / "output" / "library" / "index.html").is_file()
    html = html_path.read_text(encoding="utf-8")
    assert "https://cdn" not in html
    assert "译文：Routing Networks" in html
    html_path.write_text(
        html.replace("</main>", '<code class="math-fallback">broken</code></main>'),
        encoding="utf-8",
    )
    with pytest.raises(VerificationError, match="fell back"):
        verify_output(
            document,
            translation_dir,
            html_path,
            tmp_path / "verification-with-fallback.json",
        )


def test_translation_rejects_lost_protected_token(tmp_path: Path) -> None:
    document, _ = prepared_document(tmp_path)
    task_dir = tmp_path / "tasks"
    translation_dir = tmp_path / "translations"
    write_translation_tasks(document, task_dir)
    fill_tasks(task_dir, translation_dir)
    path = next(translation_dir.glob("part-*.json"))
    payload = TranslationFile.model_validate(read_json(path))
    protected_entry = next(entry for entry in payload.translations if "$x^2$" in entry.source)
    protected_entry.target = "译文中错误地删除了公式。"
    path.write_text(payload.model_dump_json(indent=2), encoding="utf-8")
    with pytest.raises(TranslationError, match="lost protected tokens"):
        load_translations(document, translation_dir)


def test_math_is_rendered_as_offline_mathml() -> None:
    rendered = render_markdown("The score is $x^2$ and $$y = 1$$.")
    assert "<math" in rendered
    assert "MATHPLACEHOLDER" not in rendered
    assert "<script" not in rendered
    assert "cdn" not in rendered


def test_legacy_math_font_commands_are_normalized_and_unparsed_tex_falls_back() -> None:
    rendered = render_markdown(r"The route uses $\textbf { \em F }$.")
    assert 'mathvariant="bold-italic"' in rendered
    assert r"\em" not in rendered
    fallback = render_markdown(r"The route uses $\textbf { \unknown F }$.")
    assert "math-fallback" in fallback


def test_helpers() -> None:
    assert slugify("RouterNet: AP X-ray") == "routernet-ap-x-ray"
    assert hash_text("stable") == hash_text("stable")
    assert ZoteroClient._file_url_to_path("file:///C:/Zotero/a%20paper.pdf") == Path(
        "C:/Zotero/a paper.pdf"
    )


def test_mineru_v2_pages_are_flattened(tmp_path: Path) -> None:
    payload = [
        [
            {
                "type": "title",
                "content": {
                    "title_content": [{"type": "text", "content": "Methods"}],
                    "level": 2,
                },
                "bbox": [1, 2, 3, 4],
            },
            {
                "type": "paragraph",
                "content": {
                    "paragraph_content": [
                        {"type": "text", "content": "The loss is "},
                        {"type": "equation_inline", "content": "L_1"},
                        {"type": "text", "content": "."},
                    ]
                },
            },
        ],
        [
            {
                "type": "list",
                "content": {
                    "list_type": "reference_list",
                    "list_items": [
                        {
                            "item_type": "text",
                            "item_content": [
                                {"type": "text", "content": "[1] Reference"}
                            ],
                        }
                    ],
                },
            }
        ],
    ]
    path = tmp_path / "paper_content_list_v2.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    document = build_document(
        job_id="v2-job",
        metadata=PaperMetadata(title="V2"),
        source=source_reference(),
        content_list_path=path,
    )
    assert [block.kind for block in document.blocks] == [
        "heading",
        "paragraph",
        "reference",
    ]
    assert document.blocks[1].page == 0
    assert "$L_1$" in document.blocks[1].source
    assert document.blocks[2].page == 1


def test_shifted_adjacent_table_and_figure_captions_are_repaired(tmp_path: Path) -> None:
    payload = [
        [
            {"type": "table", "content": {"html": "<table></table>"}},
            {
                "type": "table",
                "content": {
                    "html": "<table></table>",
                    "table_caption": [{"type": "text", "content": "Table 3: Ablation."}],
                },
            },
            {
                "type": "chart",
                "content": {
                    "chart_caption": [
                        {
                            "type": "text",
                            "content": "Table 4: Assessment.Figure 6: Root selection.",
                        }
                    ]
                },
            },
        ]
    ]
    path = tmp_path / "shifted_content_list_v2.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    document = build_document(
        job_id="captions-job",
        metadata=PaperMetadata(title="Captions"),
        source=source_reference(),
        content_list_path=path,
    )
    assert document.blocks[0].caption_source == "Table 3: Ablation."
    assert document.blocks[1].caption_source == "Table 4: Assessment."
    assert document.blocks[2].caption_source == "Figure 6: Root selection."


def test_float_interrupting_a_sentence_is_moved_after_its_continuation(
    tmp_path: Path,
) -> None:
    payload = [
        {"type": "text", "text": "We use the officially released", "page_idx": 0},
        {
            "type": "table",
            "table_body": "<table><tr><td>Result</td></tr></table>",
            "page_idx": 0,
        },
        {"type": "text", "text": "ground-truth angles.", "page_idx": 0},
    ]
    path = tmp_path / "interrupted_content_list.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    document = build_document(
        job_id="interrupted-job",
        metadata=PaperMetadata(title="Interrupted"),
        source=source_reference(),
        content_list_path=path,
    )
    assert [block.kind for block in document.blocks] == ["paragraph", "table"]
    assert document.blocks[0].source == "We use the officially released ground-truth angles."
    assert document.blocks[0].extra["origin_block_ids"] == ["b0001", "b0003"]
    assert document.layout_info["automatic_paragraph_merges"] == 1


def test_column_and_page_continuations_merge_across_leading_floats(tmp_path: Path) -> None:
    payload = [
        {
            "type": "text",
            "text": "Classical approaches include Matthews and",
            "page_idx": 0,
            "bbox": [81, 845, 480, 890],
        },
        {
            "type": "text",
            "text": "Baker 2004 and we exhaustively train",
            "page_idx": 0,
            "bbox": [514, 68, 915, 890],
        },
        {
            "type": "table",
            "table_body": "<table><tr><td>Result</td></tr></table>",
            "page_idx": 1,
            "bbox": [84, 65, 477, 501],
        },
        {
            "type": "text",
            "text": "17 variants of the network.",
            "page_idx": 1,
            "bbox": [81, 758, 480, 832],
        },
    ]
    path = tmp_path / "boundary_content_list.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    document = build_document(
        job_id="boundary-job",
        metadata=PaperMetadata(title="Boundary"),
        source=source_reference(),
        content_list_path=path,
    )
    assert [block.kind for block in document.blocks] == ["paragraph", "table"]
    merged = document.blocks[0]
    assert merged.id == "b0001"
    assert merged.page == 0
    assert merged.bbox == [81.0, 845.0, 480.0, 890.0]
    assert merged.source == (
        "Classical approaches include Matthews and Baker 2004 and we exhaustively train "
        "17 variants of the network."
    )
    assert merged.extra["origin_block_ids"] == ["b0001", "b0002", "b0004"]
    assert merged.extra["origin_pages"] == [0, 0, 1]
    assert len(merged.extra["origin_bboxes"]) == 3
    assert document.layout_info["automatic_paragraph_merges"] == 1

    translations = {segment.id: segment.source for segment in document.segments}
    html_path = build_paper(
        document,
        translations,
        content_list_path=path,
        library_root=tmp_path / "output" / "library",
    )
    assert "PDF 1-2" in html_path.read_text(encoding="utf-8")


def test_paragraph_merging_stops_at_structure_and_terminal_punctuation(
    tmp_path: Path,
) -> None:
    payload = [
        {
            "type": "text",
            "text": "A fragment without punctuation",
            "page_idx": 0,
            "bbox": [81, 845, 480, 890],
        },
        {"type": "equation", "text": "x = 1", "page_idx": 0},
        {
            "type": "text",
            "text": "A new paragraph.",
            "page_idx": 0,
            "bbox": [514, 68, 915, 150],
        },
        {
            "type": "text",
            "text": "A completed paragraph.",
            "page_idx": 1,
            "bbox": [81, 845, 480, 890],
        },
        {
            "type": "text",
            "text": "Another paragraph.",
            "page_idx": 1,
            "bbox": [514, 68, 915, 150],
        },
        {
            "type": "text",
            "text": "Ordinary adjacent fragment",
            "page_idx": 2,
            "bbox": [81, 100, 480, 160],
        },
        {
            "type": "text",
            "text": "Ordinary adjacent paragraph.",
            "page_idx": 2,
            "bbox": [81, 170, 480, 230],
        },
    ]
    path = tmp_path / "non_merge_content_list.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    document = build_document(
        job_id="non-merge-job",
        metadata=PaperMetadata(title="Non merge"),
        source=source_reference(),
        content_list_path=path,
    )
    assert len(document.blocks) == len(payload)
    assert document.layout_info["automatic_paragraph_merges"] == 0


def test_composite_figure_and_page_notes_are_normalized(tmp_path: Path) -> None:
    images = tmp_path / "images"
    images.mkdir()
    for name in ("a.png", "b.png"):
        (images / name).write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    payload = [
        {
            "type": "image",
            "img_path": "images/a.png",
            "caption": "(a)",
            "page_idx": 0,
            "bbox": [100, 200, 250, 350],
        },
        {
            "type": "image",
            "img_path": "images/b.png",
            "caption": "(b)Figure 1: (a) Input. (b) Prediction.",
            "page_idx": 0,
            "bbox": [250, 201, 400, 351],
        },
        {"type": "text", "text": "", "page_idx": 0, "bbox": [1, 1, 2, 2]},
        {
            "type": "page_footnote",
            "text": "<sup>*</sup>These authors contributed equally.",
            "page_idx": 0,
        },
        {
            "type": "page_footnote",
            "text": "Copyright © 2026. All rights reserved.",
            "page_idx": 0,
        },
        {
            "type": "page_footnote",
            "text": "<sup>1</sup>https://example.test/data\\#anchor",
            "page_idx": 2,
        },
    ]
    path = tmp_path / "composite.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    document = build_document(
        job_id="composite-job",
        metadata=PaperMetadata(title="Composite"),
        source=source_reference(),
        content_list_path=path,
        content_sha256=hash_file(path),
    )
    assert [block.kind for block in document.blocks] == ["figure_group"]
    group = document.blocks[0]
    assert [member.id for member in group.members] == ["b0001", "b0002"]
    assert [member.label for member in group.members] == ["(a)", "(b)"]
    assert group.caption_source == "Figure 1: (a) Input. (b) Prediction."
    assert group.caption_segment_id == "b0002.caption"
    assert [note.kind for note in document.front_matter_notes] == [
        "author_note",
        "copyright",
    ]
    assert document.endnotes[0].marker == "1"
    assert document.endnotes[0].url == "https://example.test/data#anchor"
    assert document.endnotes[0].segment_id == ""
    assert document.layout_info["dropped_empty_blocks"] == 1
    report = structure_report(document)
    assert report["status"] == "passed"
    assert len(report["override_targets"]) == 4
    task_dir = tmp_path / "translation_tasks"
    translation_dir = tmp_path / "translations"
    write_translation_tasks(document, task_dir)
    fill_tasks(task_dir, translation_dir)
    translations = load_translations(document, translation_dir)
    html_path = build_paper(
        document,
        translations,
        content_list_path=path,
        library_root=tmp_path / "output" / "library",
    )
    report = verify_output(
        document,
        translation_dir,
        html_path,
        tmp_path / "verification.json",
    )
    html = html_path.read_text(encoding="utf-8")
    assert report["figure_groups"] == 1
    assert 'class="figure-group layout-row"' in html
    assert html.count('class="figure-member"') == 2
    assert html.index('class="paper-front-notes"') < html.index('class="paper-body"')
    assert html.index('class="paper-endnotes"') > html.index('class="paper-body"')
    assert "https://example.test/data\\#anchor" not in html
    css = (tmp_path / "output" / "library" / "assets" / "app.css").read_text(
        encoding="utf-8"
    )
    assert ".figure-members { grid-template-columns: minmax(0, 1fr) !important; }" in css


def test_conservative_grouping_leaves_distant_subfigures_for_review(tmp_path: Path) -> None:
    payload = [
        {
            "type": "image",
            "img_path": "images/a.png",
            "caption": "(a)",
            "page_idx": 0,
            "bbox": [100, 100, 200, 200],
        },
        {
            "type": "image",
            "img_path": "images/b.png",
            "caption": "(b)Figure 1: (a) Input. (b) Output.",
            "page_idx": 0,
            "bbox": [800, 500, 900, 600],
        },
    ]
    path = tmp_path / "distant.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    document = build_document(
        job_id="distant-job",
        metadata=PaperMetadata(title="Distant"),
        source=source_reference(),
        content_list_path=path,
    )
    report = structure_report(document)
    assert [block.kind for block in document.blocks] == ["figure", "figure"]
    assert any("orphaned subfigure" in error for error in report["errors"])


def target_for(block: DocumentBlock) -> LayoutTarget:
    return LayoutTarget(
        id=block.id,
        page=block.page,
        bbox=block.bbox,
        block_hash=block_fingerprint(block),
    )


def test_layout_override_operations_are_transactional() -> None:
    figures = [
        DocumentBlock(
            id="a",
            kind="figure",
            page=0,
            asset="a.png",
            caption_source="(a)",
            bbox=[0, 0, 10, 10],
        ),
        DocumentBlock(
            id="b",
            kind="figure",
            page=0,
            asset="b.png",
            caption_source="(b)Figure 1: (a) A. (b) B.",
            bbox=[10, 0, 20, 10],
        ),
    ]
    paragraph_a = DocumentBlock(id="p1", kind="paragraph", page=0, source="First")
    paragraph_b = DocumentBlock(id="p2", kind="paragraph", page=0, source="Second")
    note = DocumentBlock(id="n", kind="paragraph", page=0, source="A note")
    garbage = DocumentBlock(id="g", kind="paragraph", page=0, source="Duplicate")
    blocks = [*figures, paragraph_a, paragraph_b, note, garbage]
    overrides = LayoutOverrides(
        job_id="override-job",
        source_sha256="a" * 64,
        content_sha256="b" * 64,
        operations=[
            LayoutOperation(
                op="group",
                targets=[target_for(block) for block in figures],
                caption_from="b",
                layout="row",
                reason="Confirmed composite figure.",
            ),
            LayoutOperation(
                op="move",
                targets=[target_for(paragraph_b), target_for(paragraph_a)],
                before_id="p1",
                reason="Correct reading order.",
            ),
            LayoutOperation(
                op="reclassify",
                targets=[target_for(note)],
                kind="endnote",
                reason="Footer note.",
            ),
            LayoutOperation(
                op="drop",
                targets=[target_for(garbage)],
                reason="Duplicate OCR block.",
            ),
        ],
    )
    result, count = apply_layout_overrides(blocks, overrides)
    assert count == 4
    assert [block.id for block in result] == ["fg-a-b", "p2", "p1", "n"]
    assert [member.id for member in result[0].members] == ["a", "b"]
    assert result[-1].kind == "endnote"

    stale = overrides.model_copy(deep=True)
    stale.operations[0].targets[0].bbox = [1, 1, 2, 2]
    with pytest.raises(LayoutOverrideError, match="different bbox"):
        apply_layout_overrides(blocks, stale)
    assert [block.id for block in blocks] == ["a", "b", "p1", "p2", "n", "g"]


def test_override_group_is_bbox_ordered_and_renders_its_footnote(tmp_path: Path) -> None:
    images = tmp_path / "images"
    images.mkdir()
    for name in ("a.png", "b.png"):
        (images / name).write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    payload = [
        {
            "type": "image",
            "img_path": "images/a.png",
            "caption": "(a)",
            "page_idx": 0,
            "bbox": [0, 0, 100, 100],
        },
        {
            "type": "image",
            "img_path": "images/b.png",
            "caption": "(b)Figure 1: (a) Input. (b) Output.",
            "footnote": "Shared figure note.",
            "page_idx": 0,
            "bbox": [100, 0, 200, 100],
        },
    ]
    content_path = tmp_path / "manual_group.json"
    content_path.write_text(json.dumps(payload), encoding="utf-8")
    content_hash = hash_file(content_path)
    initial = build_document(
        job_id="manual-group",
        metadata=PaperMetadata(title="Manual Group"),
        source=source_reference(),
        content_list_path=content_path,
        content_sha256=content_hash,
    )
    left, right = initial.blocks
    overrides = LayoutOverrides(
        job_id="manual-group",
        source_sha256=source_reference().sha256,
        content_sha256=content_hash,
        operations=[
            LayoutOperation(
                op="group",
                targets=[target_for(right), target_for(left)],
                caption_from=right.id,
                layout="row",
                reason="Confirmed against the PDF.",
            )
        ],
    )
    document = build_document(
        job_id="manual-group",
        metadata=PaperMetadata(title="Manual Group"),
        source=source_reference(),
        content_list_path=content_path,
        content_sha256=content_hash,
        overrides=overrides,
    )
    group = document.blocks[0]
    assert group.id == "fg-b0001-b0002"
    assert [member.id for member in group.members] == ["b0001", "b0002"]
    assert group.footnote_source == "Shared figure note."
    assert structure_report(document)["status"] == "passed"

    broken = document.model_copy(deep=True)
    broken.blocks[0].members.reverse()
    assert any(
        "not consecutive" in error for error in structure_report(broken)["errors"]
    )

    task_dir = tmp_path / "tasks"
    translation_dir = tmp_path / "translations"
    write_translation_tasks(document, task_dir)
    fill_tasks(task_dir, translation_dir)
    html_path = build_paper(
        document,
        load_translations(document, translation_dir),
        content_list_path=content_path,
        library_root=tmp_path / "output" / "library",
    )
    html = html_path.read_text(encoding="utf-8")
    assert '<div class="figure-note pair">' in html
    assert "Shared figure note." in html
    assert "译文：Shared figure note." in html


def test_translation_reconciliation_reuses_content_across_id_changes(
    tmp_path: Path,
) -> None:
    document, _ = prepared_document(tmp_path)
    old_tasks = tmp_path / "old_tasks"
    translation_dir = tmp_path / "translations"
    write_translation_tasks(document, old_tasks)
    fill_tasks(old_tasks, translation_dir)

    changed = document.model_copy(deep=True)
    segment = next(item for item in changed.segments if item.id.endswith(".caption"))
    old_id = segment.id
    segment.id = "renamed.caption"
    block = next(item for item in changed.blocks if item.caption_segment_id == old_id)
    block.caption_segment_id = segment.id
    report = reconcile_translation_artifacts(
        changed,
        task_dir=tmp_path / "new_tasks",
        translation_dir=translation_dir,
        history_dir=tmp_path / "history",
        archive_key="old-layout",
        report_path=tmp_path / "reuse.json",
    )
    assert report["reused"] == len(changed.segments)
    translations = load_translations(changed, translation_dir)
    assert translations["renamed.caption"].startswith("译文：")
    assert (tmp_path / "history" / "old-layout" / "translations").is_dir()


@pytest.mark.parametrize("placeholder", ["TODO", "TBD", "null"])
def test_translation_reconciliation_does_not_reuse_placeholders(
    tmp_path: Path, placeholder: str
) -> None:
    document, _ = prepared_document(tmp_path)
    old_tasks = tmp_path / "old_tasks"
    translation_dir = tmp_path / "translations"
    write_translation_tasks(document, old_tasks)
    fill_tasks(old_tasks, translation_dir)
    old_path = next(translation_dir.glob("part-*.json"))
    old_payload = TranslationFile.model_validate(read_json(old_path))
    pending_id = old_payload.translations[0].id
    old_payload.translations[0].target = placeholder
    atomic_write_json(old_path, old_payload.model_dump(mode="json"))

    report = reconcile_translation_artifacts(
        document,
        task_dir=tmp_path / "new_tasks",
        translation_dir=translation_dir,
        history_dir=tmp_path / "history",
        archive_key="old-layout",
        report_path=tmp_path / "reuse.json",
    )
    assert report["reused"] == len(document.segments) - 1
    assert report["pending"] == 1
    new_payload = TranslationFile.model_validate(read_json(old_path))
    pending = next(entry for entry in new_payload.translations if entry.id == pending_id)
    assert pending.target is None


def test_finalized_document_hash_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pdf2html.cli import _load_finalized_document

    document, _ = prepared_document(tmp_path)
    job_dir = tmp_path / ".work" / "jobs" / document.job_id
    job_dir.mkdir(parents=True)
    document_path = job_dir / "document.json"
    write_document(document, document_path)
    manifest = JobManifest(
        job_id=document.job_id,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        status="awaiting_translation",
        workspace=str(tmp_path),
        metadata=document.metadata,
        source=document.source,
        document_path=document_path.relative_to(tmp_path).as_posix(),
        layout_document_hash=hash_file(document_path),
    )
    monkeypatch.chdir(tmp_path)
    assert _load_finalized_document(manifest).job_id == document.job_id

    changed = read_json(document_path)
    assert isinstance(changed, dict)
    changed["metadata"]["title"] = "Unreviewed title"
    atomic_write_json(document_path, changed)
    with pytest.raises(ValueError, match="no longer matches the reviewed layout"):
        _load_finalized_document(manifest)


def test_staged_layout_can_be_finalized_without_zotero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pdf2html.cli import _assert_current_layout, _write_layout

    monkeypatch.chdir(tmp_path)
    job_id = "staged-job"
    job_dir = tmp_path / ".work" / "jobs" / job_id
    job_dir.mkdir(parents=True)
    source_pdf = job_dir / "source.pdf"
    source_pdf.write_bytes(b"PDF fixture")
    content_path = job_dir / "content.json"
    content_path.write_text(
        json.dumps([{"type": "text", "text": "Body", "page_idx": 0}]),
        encoding="utf-8",
    )
    source = source_reference().model_copy(update={"sha256": hash_file(source_pdf)})
    manifest = JobManifest(
        job_id=job_id,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        status="parsing",
        workspace=str(tmp_path),
        metadata=PaperMetadata(title="Staged"),
        source=source,
        mineru=MinerURecord(content_list_path=content_path.relative_to(tmp_path).as_posix()),
    )
    atomic_write_json(job_dir / "manifest.json", manifest.model_dump(mode="json"))
    report = _write_layout(manifest)
    assert report["status"] == "passed"
    assert manifest.status == "awaiting_layout_review"
    document, _ = _assert_current_layout(manifest)
    assert document.blocks[0].source == "Body"
