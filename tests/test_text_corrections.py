from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from playwright.sync_api import Error, sync_playwright
from pypdf import PdfWriter

from pdf2html.document import build_document
from pdf2html.layout import block_fingerprint
from pdf2html.models import (
    DocumentBlock,
    LayoutTarget,
    PaperMetadata,
    SourceReference,
    TextCorrection,
    TextCorrections,
)
from pdf2html.render import build_layout_preview
from pdf2html.screenshot import ScreenshotError, render_pdf_page, render_webpage
from pdf2html.text_corrections import (
    TextCorrectionError,
    apply_text_corrections,
    load_text_corrections,
)
from pdf2html.util import atomic_write_json


def _block() -> DocumentBlock:
    return DocumentBlock(
        id="b0001", kind="table", page=2, bbox=[1, 2, 3, 4],
        source="<table><tr><td>V</td></tr></table>", caption_source="Table I",
    )


def _corrections(block: DocumentBlock) -> TextCorrections:
    return TextCorrections(
        job_id="paper", source_sha256="pdf", content_sha256="mineru",
        corrections=[
            TextCorrection(
                target=LayoutTarget(
                    id=block.id, page=block.page, bbox=block.bbox,
                    block_hash=block_fingerprint(block),
                ),
                field="source", value="<table><tr><td>✓</td></tr></table>",
                reason="Checked against PDF page 3",
            )
        ],
    )


def test_text_corrections_are_transactional_and_source_bound(tmp_path: Path) -> None:
    block = _block()
    corrections = _corrections(block)
    updated, count = apply_text_corrections([block], corrections)
    assert count == 1
    assert updated[0].source == "<table><tr><td>✓</td></tr></table>"
    assert block.source == "<table><tr><td>V</td></tr></table>"

    reclassified = block.model_copy(update={"kind": "paragraph"})
    updated_reclassified, _ = apply_text_corrections(
        [reclassified], corrections, target_blocks=[block]
    )
    assert updated_reclassified[0].source == "<table><tr><td>✓</td></tr></table>"

    stale = corrections.model_copy(deep=True)
    stale.corrections[0].target.block_hash = "0" * 64
    with pytest.raises(TextCorrectionError, match="content changed"):
        apply_text_corrections([block], stale)

    duplicate = corrections.model_copy(deep=True)
    duplicate.corrections.append(duplicate.corrections[0].model_copy(deep=True))
    with pytest.raises(TextCorrectionError, match="Duplicate"):
        apply_text_corrections([block], duplicate)
    assert block.source == "<table><tr><td>V</td></tr></table>"

    path = tmp_path / "text_corrections.json"
    atomic_write_json(path, corrections.model_dump(mode="json"))
    assert load_text_corrections(
        path, job_id="paper", source_sha256="pdf", content_sha256="mineru"
    ) == corrections
    with pytest.raises(TextCorrectionError, match="source_sha256"):
        load_text_corrections(
            path, job_id="paper", source_sha256="changed", content_sha256="mineru"
        )


def test_corrected_table_is_the_translation_source(tmp_path: Path) -> None:
    content = tmp_path / "content.json"
    atomic_write_json(
        content,
        [{"type": "table", "page_idx": 2, "bbox": [1, 2, 3, 4],
          "table_body": "<table><tr><td>V</td></tr></table>",
          "table_caption": "Table I"}],
    )
    source = SourceReference(
        library_prefix="users/0", item_key="a", item_version=1,
        attachment_key="b", attachment_version=1, original_path="paper.pdf", sha256="pdf",
    )
    initial = build_document(
        job_id="paper", metadata=PaperMetadata(title="Paper"), source=source,
        content_list_path=content,
    )
    corrected = build_document(
        job_id="paper", metadata=PaperMetadata(title="Paper"), source=source,
        content_list_path=content, text_corrections=_corrections(initial.blocks[0]),
    )
    assert corrected.segments[1].source == "<table><tr><td>✓</td></tr></table>"
    assert corrected.segments[1].source_hash != initial.segments[1].source_hash


@pytest.mark.parametrize("kind", ["figure", "table", "figure_group"])
def test_missing_figure_or_table_caption_can_be_restored(kind: str) -> None:
    block = _block().model_copy(update={"kind": kind, "caption_source": ""})
    corrections = _corrections(block)
    corrections.corrections[0].field = "caption_source"
    corrections.corrections[0].value = "核对源 PDF 后补回的图表说明"

    updated, count = apply_text_corrections([block], corrections)
    assert count == 1
    assert updated[0].caption_source == "核对源 PDF 后补回的图表说明"
    assert block.caption_source == ""

    stale = corrections.model_copy(deep=True)
    stale.corrections[0].target.block_hash = "0" * 64
    with pytest.raises(TextCorrectionError, match="content changed"):
        apply_text_corrections([block], stale)


@pytest.mark.parametrize("kind", ["paragraph", "figure", "table", "figure_group"])
def test_missing_source_text_cannot_be_restored(kind: str) -> None:
    block = _block().model_copy(update={"kind": kind, "source": ""})
    with pytest.raises(TextCorrectionError, match="has no source text"):
        apply_text_corrections([block], _corrections(block))


@pytest.mark.parametrize("kind", ["paragraph", "code"])
def test_missing_caption_cannot_be_restored_on_other_kinds(kind: str) -> None:
    block = _block().model_copy(update={"kind": kind, "caption_source": ""})
    corrections = _corrections(block)
    corrections.corrections[0].field = "caption_source"
    with pytest.raises(TextCorrectionError, match="has no source text"):
        apply_text_corrections([block], corrections)


def test_code_caption_is_present_in_layout_preview(tmp_path: Path) -> None:
    content = tmp_path / "content.json"
    atomic_write_json(
        content,
        [{"type": "code", "page_idx": 0, "code_body": "alloc = vmalloc(size);",
          "code_caption": "Fig. 4: Allocation strategy."}],
    )
    source = SourceReference(
        library_prefix="users/0", item_key="a", item_version=1,
        attachment_key="b", attachment_version=1, original_path="paper.pdf", sha256="pdf",
    )
    document = build_document(
        job_id="paper", metadata=PaperMetadata(title="Paper"), source=source,
        content_list_path=content,
    )
    preview = build_layout_preview(
        document, content_list_path=content, preview_root=tmp_path / "preview"
    ).read_text(encoding="utf-8")
    assert 'class="code-caption pair"' in preview
    assert "Fig. 4: Allocation strategy." in preview


def test_render_pdf_page_checks_range_and_writes_png(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with pdf.open("wb") as stream:
        writer.write(stream)
    output = tmp_path / "page.png"
    assert render_pdf_page(pdf, output, page=1, dpi=72) == output
    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(ScreenshotError, match="between 1 and 1"):
        render_pdf_page(pdf, output, page=2)
    with pytest.raises(ScreenshotError, match="URL must use"):
        render_webpage("invalid", output)


def test_render_webpage_captures_distant_element(tmp_path: Path) -> None:
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Error as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("Playwright Chromium has not been installed")
            raise
        else:
            browser.close()
    html = tmp_path / "long.html"
    html.write_text(
        '<div style="height:12000px"></div>'
        '<div id="target" style="width:200px;height:100px;background:#ff0000"></div>',
        encoding="utf-8",
    )
    output = tmp_path / "target.png"
    render_webpage(html.as_uri(), output, selector="#target")
    with Image.open(output) as image:
        assert image.size == (200, 100)
        assert image.convert("RGB").getpixel((100, 50)) == (255, 0, 0)
