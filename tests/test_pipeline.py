from __future__ import annotations

import json
from pathlib import Path

import pytest

from pdf2html.document import build_document, write_translation_tasks
from pdf2html.models import PaperMetadata, SourceReference, TranslationFile
from pdf2html.render import build_paper, render_markdown
from pdf2html.translations import TranslationError, load_translations
from pdf2html.util import hash_text, read_json, slugify
from pdf2html.verify import verify_output
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
    assert [block.kind for block in document.blocks] == [
        "paragraph",
        "paragraph",
        "table",
    ]
