from pathlib import Path

import pytest

from pdf2html.models import Document, DocumentBlock, PaperMetadata, SourceReference
from pdf2html.render import build_layout_preview


@pytest.mark.parametrize("introduction", ["Introduction", "1. Introduction"])
def test_preview_preserves_independent_sections_before_introduction(
    tmp_path: Path, introduction: str
) -> None:
    def block(
        block_id: str, kind: str, source: str, level: int = 0, role: str = ""
    ) -> DocumentBlock:
        return DocumentBlock(
            id=block_id,
            kind=kind,
            source=source,
            level=level,
            page=0,
            extra={"front_matter_role": role} if role else {},
        )

    document = Document(
        job_id="context-job",
        metadata=PaperMetadata(title="Context paper", abstract="Summary text."),
        source=SourceReference(
            library_prefix="users/0",
            item_key="ABCD2345",
            item_version=1,
            attachment_key="EFGH6789",
            attachment_version=1,
            original_path="paper.pdf",
            sha256="a" * 64,
        ),
        blocks=[
            block("title", "heading", "Context paper", 1, "document_title"),
            block("article-info", "heading", "ARTICLE INFO", 2),
            block("summary", "heading", "Summary", 2),
            block("summary-text", "paragraph", "Summary text."),
            block("context", "heading", "Research in context", 2),
            block("evidence", "heading", "Evidence before this study", 3),
            block("evidence-text", "paragraph", "Prior evidence."),
            block("value", "heading", "Added value of this study", 3),
            block("value-text", "paragraph", "New contribution."),
            block("implications", "heading", "Implications of all the available evidence", 3),
            block("implications-text", "paragraph", "Clinical implications."),
            block("intro", "heading", introduction, 2),
        ],
        segments=[],
    )
    preview = build_layout_preview(
        document,
        content_list_path=tmp_path / "content_list.json",
        preview_root=tmp_path / "preview",
    ).read_text(encoding="utf-8")

    for block_id in [
        "context", "evidence", "evidence-text", "value", "value-text",
        "implications", "implications-text", "intro",
    ]:
        assert f'id="{block_id}"' in preview
    for block_id in ["title", "article-info", "summary", "summary-text"]:
        assert f'id="{block_id}"' not in preview
    assert preview.count("Summary text.") == 1
