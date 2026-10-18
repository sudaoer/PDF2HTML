from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

import pytest
from fastapi.testclient import TestClient

from pdf2html.bundle import BundleError, PaperBundle, build_bundle, render_bundle_markdown
from pdf2html.bundle_verify import BundleVerificationError, verify_bundle
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
    migrate_job_manifest,
)
from pdf2html.render import (
    _parse_equation,
    build_layout_preview,
    render_equation,
    render_markdown,
)
from pdf2html.search import SearchIndex, plain_text, search_tokens
from pdf2html.server import create_app, verify_http_app
from pdf2html.translations import (
    TranslationError,
    load_translations,
    reconcile_translation_artifacts,
)
from pdf2html.util import atomic_write_json, hash_file, hash_text, read_json, slugify
from pdf2html.verify import structure_report
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
        {"type": "equation", "text": r"E = mc^2\tag{1}", "page_idx": 2},
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
    result = build_bundle(
        document,
        translations,
        content_list_path=content_path,
        library_root=tmp_path / "output" / "library",
    )
    report = verify_bundle(result.bundle_path)
    assert report["status"] == "passed"
    assert report["media"] == 1
    assert not list((tmp_path / "output" / "library").rglob("*.html"))
    assert "译文：Routing Networks" in (
        result.paper_dir / "paper.zh-CN.md"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("abstract", "expected"),
    [
        ("Abstract \n           Optical scanning.", "Abstract\n           Optical scanning."),
        ("First line  \nSecond line.", "First line  \nSecond line."),
        ("```text\nvalue \n```", "```text\nvalue \n```"),
        ("A $x \n+y$ result.", "A $x \n+y$ result."),
    ],
)
def test_english_abstract_serialization_preserves_source_and_semantics(
    tmp_path: Path, abstract: str, expected: str
) -> None:
    original, content_path = prepared_document(tmp_path)
    document = build_document(
        job_id=original.job_id,
        metadata=original.metadata.model_copy(update={"abstract": abstract}),
        source=original.source,
        content_list_path=content_path,
    )
    before = document.model_dump()
    markdown = render_bundle_markdown(
        document,
        {},
        language="en",
        paper_id="paper",
        revision="sha256:test",
        document_sha256="a" * 64,
        media_mapping={"images/figure.png": "media/figure.png"},
    )
    assert f"## Abstract\n\n{expected}\n\n" in markdown
    assert document.model_dump() == before


def test_markdown_bundle_is_deterministic_and_self_contained(tmp_path: Path) -> None:
    document, content_path = prepared_document(tmp_path)
    task_dir = tmp_path / "tasks"
    translation_dir = tmp_path / "translations"
    write_translation_tasks(document, task_dir)
    fill_tasks(task_dir, translation_dir)
    translations = load_translations(document, translation_dir)
    library_root = tmp_path / "output" / "library"

    first = build_bundle(
        document,
        translations,
        content_list_path=content_path,
        library_root=library_root,
    )
    first_bytes = {
        path.relative_to(first.paper_dir).as_posix(): path.read_bytes()
        for path in sorted(first.paper_dir.rglob("*"))
        if path.is_file()
    }
    second = build_bundle(
        document,
        translations,
        content_list_path=content_path,
        library_root=library_root,
    )
    second_bytes = {
        path.relative_to(second.paper_dir).as_posix(): path.read_bytes()
        for path in sorted(second.paper_dir.rglob("*"))
        if path.is_file()
    }

    assert first.revision == second.revision
    assert first_bytes == second_bytes
    bundle = PaperBundle.model_validate(read_json(first.bundle_path))
    assert bundle.document == document
    assert {entry.id: entry.target for entry in bundle.translations} == translations
    assert bundle.document_sha256 == hash_text(
        json.dumps(
            document.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    english = (first.paper_dir / "paper.en.md").read_text(encoding="utf-8")
    chinese = (first.paper_dir / "paper.zh-CN.md").read_text(encoding="utf-8")
    paragraph = next(block for block in document.blocks if block.kind == "paragraph")
    assert f'bundle_revision: "{first.revision}"' in english
    assert (
        f'<!-- pdf2html:block id="{paragraph.id}" kind="paragraph" page="1" -->'
        in english
    )
    assert f'<a id="block-{paragraph.id}"></a>' in english
    assert "译文：Accuracy" in chinese
    assert len(bundle.media) == 1
    assert bundle.media[0].path.startswith("media/")
    assert (first.paper_dir / bundle.media[0].path).is_file()


def test_bundle_rejects_media_path_traversal(tmp_path: Path) -> None:
    document, content_path = prepared_document(tmp_path)
    translations = {segment.id: f"译文：{segment.source}" for segment in document.segments}
    figure = next(block for block in document.blocks if block.kind == "figure")
    figure.asset = "../outside.png"
    with pytest.raises(BundleError, match="Unsafe MinerU media path"):
        build_bundle(
            document,
            translations,
            content_list_path=content_path,
            library_root=tmp_path / "output" / "library",
        )


def test_bundle_build_cleans_only_known_legacy_html(tmp_path: Path) -> None:
    document, content_path = prepared_document(tmp_path)
    translations = {segment.id: f"译文：{segment.source}" for segment in document.segments}
    library_root = tmp_path / "library"
    legacy_paper = library_root / "papers" / "old-paper"
    legacy_assets = library_root / "assets"
    legacy_paper.mkdir(parents=True)
    legacy_assets.mkdir(parents=True)
    (library_root / "index.html").write_text("legacy shelf", encoding="utf-8")
    (legacy_paper / "index.html").write_text("legacy paper", encoding="utf-8")
    (legacy_paper / "custom.html").write_text("user file", encoding="utf-8")
    (legacy_assets / "app.css").write_text("legacy css", encoding="utf-8")
    (legacy_assets / "app.js").write_text("legacy js", encoding="utf-8")
    (legacy_assets / "keep.txt").write_text("keep", encoding="utf-8")

    result = build_bundle(
        document,
        translations,
        content_list_path=content_path,
        library_root=library_root,
    )

    assert not (library_root / "index.html").exists()
    assert not (legacy_paper / "index.html").exists()
    assert not (legacy_assets / "app.css").exists()
    assert not (legacy_assets / "app.js").exists()
    assert (legacy_paper / "custom.html").read_text(encoding="utf-8") == "user file"
    assert (legacy_assets / "keep.txt").is_file()
    assert not list(result.paper_dir.glob("*.html"))


def test_job_manifest_v1_migrates_to_bundle_fields() -> None:
    payload = {
        "schema_version": 1,
        "output_path": "output/library/papers/example/index.html",
        "mineru": {"content_list_path": "paper_content_list_v2.json"},
    }
    migrated = migrate_job_manifest(payload)
    assert migrated["schema_version"] == 5
    assert "output_path" not in migrated
    assert "legacy_html_path" not in migrated
    assert migrated["bundle_path"] == ""
    assert migrated["bundle_revision"] == ""
    assert migrated["mineru"]["middle_json_path"] == ""


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("markdown", "hash or size changed"),
        ("unsafe_html", "unsafe raw HTML"),
        ("extra_media", "does not exactly match"),
        ("traversal", "Invalid bundle media path"),
        ("extra_field", "Invalid paper.json"),
    ],
)
def test_bundle_verifier_rejects_corruption(
    tmp_path: Path,
    mutation: str,
    error: str,
) -> None:
    document, content_path = prepared_document(tmp_path)
    translations = {segment.id: f"译文：{segment.source}" for segment in document.segments}
    result = build_bundle(
        document,
        translations,
        content_list_path=content_path,
        library_root=tmp_path / "output" / "library",
    )
    if mutation in {"markdown", "unsafe_html"}:
        markdown = result.paper_dir / "paper.en.md"
        suffix = "\n" if mutation == "markdown" else "\n<script>alert(1)</script>\n"
        markdown.write_text(markdown.read_text(encoding="utf-8") + suffix, encoding="utf-8")
    elif mutation == "extra_media":
        (result.paper_dir / "media" / "extra.png").write_bytes(b"extra")
    else:
        payload = read_json(result.bundle_path)
        assert isinstance(payload, dict)
        if mutation == "traversal":
            payload["media"][0]["path"] = "media/../outside.png"
        else:
            payload["unexpected"] = True
        atomic_write_json(result.bundle_path, payload)
    with pytest.raises(BundleVerificationError, match=error):
        verify_bundle(result.bundle_path)


def test_bundle_verifier_rejects_invalid_equation_tag(tmp_path: Path) -> None:
    document, content_path = prepared_document(tmp_path)
    equation = next(block for block in document.blocks if block.kind == "equation")
    equation.source = r"E = mc^2\tag{1} trailing"
    translations = {segment.id: f"译文：{segment.source}" for segment in document.segments}
    result = build_bundle(
        document,
        translations,
        content_list_path=content_path,
        library_root=tmp_path / "output" / "library",
    )
    with pytest.raises(BundleVerificationError, match="invalid tag command"):
        verify_bundle(result.bundle_path)


def write_test_web_root(root: Path) -> Path:
    web_root = root / "web"
    assets = web_root / "assets"
    vite = web_root / ".vite"
    assets.mkdir(parents=True)
    vite.mkdir()
    (web_root / "index.html").write_text(
        '<!doctype html><div id="app"></div><script src="/assets/app.js"></script>',
        encoding="utf-8",
    )
    (assets / "app.js").write_text("console.log('fixture')", encoding="utf-8")
    atomic_write_json(vite / "manifest.json", {"index.html": {"file": "assets/app.js"}})
    return web_root


def test_read_only_http_routes_and_incremental_corruption(tmp_path: Path) -> None:
    document, content_path = prepared_document(tmp_path)
    translations = {segment.id: f"译文：{segment.source}" for segment in document.segments}
    library_root = tmp_path / "library"
    result = build_bundle(
        document,
        translations,
        content_list_path=content_path,
        library_root=library_root,
    )
    app = create_app(library_root=library_root, web_root=write_test_web_root(tmp_path))
    client = TestClient(app)

    shell = client.get("/")
    assert shell.status_code == 200
    assert shell.headers["content-type"].startswith("text/html")
    shelf = client.get("/api/shelf")
    assert shelf.status_code == 200
    assert shelf.json()["papers"][0]["paper_id"] == result.paper_id
    assert client.get(
        "/api/shelf",
        headers={"If-None-Match": shelf.headers["etag"]},
    ).status_code == 304
    assert client.get(f"/papers/{result.paper_id}", follow_redirects=False).status_code == 307
    assert client.get(f"/papers/{result.paper_id}/paper.json").status_code == 200
    markdown = client.get(f"/papers/{result.paper_id}/paper.zh-CN.md")
    assert markdown.status_code == 200
    assert "译文：Routing Networks" in markdown.text
    bundle = PaperBundle.model_validate(read_json(result.bundle_path))
    media_name = PurePosixPath(bundle.media[0].path).name
    assert client.get(f"/papers/{result.paper_id}/media/{media_name}").content.startswith(
        b"\x89PNG"
    )
    assert client.get(f"/papers/{result.paper_id}/media/unlisted.png").status_code == 404
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/assets/../index.html").status_code == 404
    search = client.get("/api/search", params={"q": "Routing", "limit": 10})
    assert search.status_code == 200
    assert search.json()["results"][0]["paper_id"] == result.paper_id
    first_search_etag = search.headers["etag"]

    translations["meta.title"] = "新版路由网络"
    build_bundle(
        document,
        translations,
        content_list_path=content_path,
        library_root=library_root,
    )
    changed_search = client.get("/api/search", params={"q": "Routing", "limit": 10})
    assert changed_search.status_code == 200
    assert changed_search.headers["etag"] != first_search_etag
    assert changed_search.json()["results"][0]["paper_id"] == result.paper_id
    assert client.get("/api/search", params={"q": "路由"}).json()["results"]

    damaged = read_json(result.bundle_path)
    assert isinstance(damaged, dict)
    damaged["unexpected"] = True
    atomic_write_json(result.bundle_path, damaged)
    broken = client.get(f"/papers/{result.paper_id}/paper.json")
    assert broken.status_code == 422
    assert broken.json()["code"] == "invalid_bundle"
    shelf_after = client.get("/api/shelf").json()
    assert shelf_after["papers"] == []
    assert shelf_after["errors"][0]["paper_id"] == result.paper_id


@pytest.mark.parametrize(
    "title",
    [
        "A Multimodal Spinal Screening Framework",
        "A Multimodal " + "anatomical " * 30 + "screening",
    ],
)
def test_http_verifier_searches_distinctive_title_in_crowded_library(
    tmp_path: Path, title: str,
) -> None:
    content_path = write_content_list(tmp_path)
    library_root = tmp_path / "library"
    paper_id = ""
    for index, paper_title in enumerate(["A"] * 11 + [title]):
        document = build_document(
            job_id=f"screening-{index}",
            metadata=PaperMetadata(title=paper_title),
            source=source_reference().model_copy(update={"item_key": f"ABCD{index:04d}"}),
            content_list_path=content_path,
        )
        paper_id = build_bundle(
            document,
            {segment.id: f"译文：{segment.source}" for segment in document.segments},
            content_list_path=content_path,
            library_root=library_root,
        ).paper_id
    web_root = write_test_web_root(tmp_path)
    client = TestClient(create_app(library_root=library_root, web_root=web_root))
    broad_results = client.get("/api/search", params={"q": "A", "limit": 10}).json()
    assert len(broad_results["results"]) == 10
    assert paper_id not in {item["paper_id"] for item in broad_results["results"]}

    report = verify_http_app(
        library_root=library_root, paper_id=paper_id, web_root=web_root,
    )
    assert report["status"] == "passed"
    assert "search" in report["checks"]


def test_bilingual_fts_tokens_weights_pages_and_limits(tmp_path: Path) -> None:
    assert search_tokens("RouterNet 路由") == ["erouternet", "u路", "u由", "b路由"]
    document, content_path = prepared_document(tmp_path)
    translations = {segment.id: f"译文：{segment.source}" for segment in document.segments}
    translations["meta.title"] = "路由网络"
    paragraph = next(block for block in document.blocks if block.kind == "paragraph")
    translations[paragraph.segment_id] = "路由精度为 $x^2$，如 [1] 所示。"
    result = build_bundle(
        document,
        translations,
        content_list_path=content_path,
        library_root=tmp_path / "library",
    )
    bundle = PaperBundle.model_validate(read_json(result.bundle_path))
    index = SearchIndex()
    index.sync({bundle.paper_id: bundle})

    title_results = index.search("Routing", 10)
    assert title_results[0]["kind"] == "paper"
    chinese_results = index.search("精度", 10)
    assert chinese_results[0]["block_id"] == paragraph.id
    assert chinese_results[0]["page"] == paragraph.page + 1
    assert "路由精度" in chinese_results[0]["target_excerpt"]
    assert len(index.search("路由", 1)) == 1


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


@pytest.mark.parametrize(
    ("source", "label", "tag_kind", "visible_label"),
    [
        (r"$$x = 1\tag{1}$$", "1", "tag", "(1)"),
        (r"x = 1\tag{10}", "10", "tag", "(10)"),
        (r"x = 1\tag{2a}", "2a", "tag", "(2a)"),
        (r"x = 1\tag{S1}", "S1", "tag", "(S1)"),
        (r"x = 1\tag*{S1}", "S1", "tag-star", "S1"),
        (r"\begin{array}{c}x=1\\y=2\end{array}\tag{3}", "3", "tag", "(3)"),
        (r"x = 1\tag{A_{1}}", "A_{1}", "tag", "(A1)"),
    ],
)
def test_display_equation_number_is_rendered_outside_mathml(
    source: str,
    label: str,
    tag_kind: str,
    visible_label: str,
) -> None:
    rendered = render_equation(source)
    assert f'data-equation-label="{label}"' in rendered
    assert f'data-equation-tag="{tag_kind}"' in rendered
    assert '<div class="equation-scroll"><span class="math-display"><math' in rendered
    formula, number = rendered.split('<span class="equation-number"', maxsplit=1)
    assert r"\tag" not in formula
    number_html = '<span class="equation-number"' + number
    assert plain_text(number_html).replace(" ", "") == visible_label
    assert "math-fallback" not in rendered


def test_unnumbered_equation_keeps_the_existing_mathml_shape() -> None:
    rendered = render_equation(r"\[x = 1\]")
    assert '<span class="math-display"><math' in rendered
    assert "equation-layout" not in rendered
    assert "equation-number" not in rendered


@pytest.mark.parametrize(
    "source",
    [
        r"x = 1\tag",
        r"x = 1\tag{",
        r"x = 1\tag{1} trailing",
        r"x = \text{\tag{1}}",
        r"x = 1\tag{1}\tag{2}",
        r"\tag{1}",
        r"x = 1\tag{}",
    ],
)
def test_malformed_or_non_terminal_equation_tags_are_not_detached(source: str) -> None:
    parsed = _parse_equation(source)
    assert parsed.invalid_tag
    assert parsed.label is None
    assert render_equation(source).find('class="equation-number"') == -1


def test_latex_tag_text_inside_an_operator_name_is_not_an_equation_number() -> None:
    parsed = _parse_equation(r"x = \operatorname{tag}(y)")
    assert not parsed.invalid_tag
    assert parsed.label is None


def test_legacy_math_font_commands_are_normalized_and_unparsed_tex_falls_back() -> None:
    rendered = render_markdown(r"The route uses $\textbf { \em F }$.")
    assert 'mathvariant="bold-italic"' in rendered
    assert r"\em" not in rendered
    fallback = render_markdown(r"The route uses $\textbf { \unknown F }$.")
    assert "math-fallback" in fallback


def test_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    assert slugify("RouterNet: AP X-ray") == "routernet-ap-x-ray"
    assert hash_text("stable") == hash_text("stable")
    monkeypatch.setattr("pdf2html.zotero._is_wsl", lambda: False)
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
    assert document.blocks[2].source == "- [1] Reference"


@pytest.mark.parametrize("image_path", ["", "images/", "images\\"])
def test_mineru_v2_tables_without_image_assets_keep_html(
    tmp_path: Path, image_path: str
) -> None:
    table_html = "<table><tr><td>Continued result</td><td>0.95</td></tr></table>"
    payload = [
        [{
            "type": "table",
            "content": {
                "html": table_html,
                "image_source": {"path": image_path},
            },
        }],
        [{
            "type": "table",
            "content": {
                "html": "",
                "image_source": {"path": image_path},
            },
        }],
    ]
    path = tmp_path / "paper_content_list_v2.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "images").mkdir()
    document = build_document(
        job_id="v2-table-job",
        metadata=PaperMetadata(title="V2 tables"),
        source=source_reference(),
        content_list_path=path,
    )

    assert [block.kind for block in document.blocks] == ["table", "table"]
    assert [block.asset for block in document.blocks] == ["", ""]
    assert [block.source for block in document.blocks] == [table_html, ""]
    assert document.blocks[1].page == 1
    preview = build_layout_preview(
        document,
        content_list_path=path,
        preview_root=tmp_path / "preview",
    )
    assert table_html in preview.read_text(encoding="utf-8")


@pytest.mark.parametrize("kind", ["code", "algorithm"])
def test_mineru_v2_code_preserves_body_and_caption(tmp_path: Path, kind: str) -> None:
    code = "if (ready) {\n    return value;\n}"
    caption = "Listing 2: Security checks."
    payload = [[{
        "type": kind,
        "content": {
            f"{kind}_content": [{"type": "text", "content": code}],
            f"{kind}_caption": [{"type": "text", "content": caption}],
        },
        "bbox": [1, 2, 3, 4],
    }]]
    path = tmp_path / "paper_content_list_v2.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    document = build_document(
        job_id="v2-code-job",
        metadata=PaperMetadata(title="Code figure"),
        source=source_reference(),
        content_list_path=path,
    )
    block = document.blocks[0]
    assert block.kind == "code"
    assert block.source == code
    assert block.caption_source == caption
    assert block.bbox == [1, 2, 3, 4]
    assert not block.segment_id
    assert block.caption_segment_id == "b0001.caption"

    task_dir = tmp_path / "tasks"
    translation_dir = tmp_path / "translations"
    task_paths = write_translation_tasks(document, task_dir)
    task = TranslationFile.model_validate(read_json(task_paths[0]))
    assert {entry.id for entry in task.translations} == {"meta.title", "b0001.caption"}
    caption_entry = next(entry for entry in task.translations if entry.id == "b0001.caption")
    assert caption_entry.source == caption
    assert caption_entry.source_hash == hash_text(caption)
    fill_tasks(task_dir, translation_dir)
    result = build_bundle(
        document,
        load_translations(document, translation_dir),
        content_list_path=path,
        library_root=tmp_path / "library",
    )
    assert verify_bundle(result.bundle_path)["status"] == "passed"
    for language in ("en", "zh-CN"):
        markdown = (result.paper_dir / f"paper.{language}.md").read_text(encoding="utf-8")
        assert f"```text\n{code}\n```" in markdown
        assert (caption if language == "en" else f"译文：{caption}") in markdown


def test_list_item_markers_are_canonicalized_without_changing_content(
    tmp_path: Path,
) -> None:
    def item(source: str) -> dict[str, object]:
        return {
            "item_type": "text",
            "item_content": [{"type": "text", "content": source}],
        }

    payload = [
        [
            {
                "type": "list",
                "content": {
                    "list_type": "text_list",
                    "list_items": [
                        item("- First"),
                        item("• Second"),
                        item("Third"),
                        item("--flag stays"),
                        item("1. Numbered text"),
                    ],
                },
            }
        ]
    ]
    path = tmp_path / "prefixed-list.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    document = build_document(
        job_id="prefixed-list",
        metadata=PaperMetadata(title="List paper"),
        source=source_reference(),
        content_list_path=path,
    )

    block = document.blocks[0]
    assert block.kind == "list"
    assert block.source == (
        "- First\n"
        "- Second\n"
        "- Third\n"
        "- --flag stays\n"
        "- 1. Numbered text"
    )
    rendered = render_markdown(block.source)
    assert rendered.count("<ul>") == 1
    assert rendered.count("<ol>") == 1
    assert rendered.count("<li>") == 6


def test_structure_report_rejects_repeated_list_markers(tmp_path: Path) -> None:
    document, _ = prepared_document(tmp_path)
    document.blocks.append(
        DocumentBlock(
            id="bad-list",
            kind="list",
            page=0,
            source="- - Nested by mistake",
        )
    )

    report = structure_report(document)
    assert "List bad-list contains repeated item markers." in report["errors"]


@pytest.mark.parametrize(
    ("marker_type", "items", "expected"),
    [
        (
            "paragraph",
            [
                "Medical imaging",
                "Landmark detection",
                "Foundation models",
                "Echocardiography measurement estimation",
            ],
            "Keywords: Medical imaging; Landmark detection; Foundation models; "
            "Echocardiography measurement estimation",
        ),
        (
            "title",
            [
                "Scoliosis early screening",
                "Scoliosis severity diagnosis",
                "Back area detection",
                "Class imbalance",
                "Deep learning",
            ],
            "Keywords: Scoliosis early screening; Scoliosis severity diagnosis; "
            "Back area detection; Class imbalance; Deep learning",
        ),
    ],
)
def test_split_keyword_lines_are_grouped_without_crossing_columns(
    tmp_path: Path,
    marker_type: str,
    items: list[str],
    expected: str,
) -> None:
    marker_content = (
        {
            "title_content": [{"type": "text", "content": "Keywords:"}],
            "level": 1,
        }
        if marker_type == "title"
        else {"paragraph_content": [{"type": "text", "content": "Keywords:"}]}
    )
    page = [
        {
            "type": "title",
            "content": {
                "title_content": [{"type": "text", "content": "ARTICLE INFO"}],
                "level": 2,
            },
            "bbox": [58, 370, 268, 390],
        },
        {"type": marker_type, "content": marker_content, "bbox": [58, 400, 109, 410]},
    ]
    for offset, item in enumerate(items):
        top = 410 + offset * 11
        page.append(
            {
                "type": "paragraph",
                "content": {
                    "paragraph_content": [{"type": "text", "content": item}]
                },
                "bbox": [58, top, 260, top + 10],
            }
        )
    abstract = "A long abstract in the adjacent right column must remain separate."
    page.extend(
        [
            {
                "type": "paragraph",
                "content": {
                    "paragraph_content": [{"type": "text", "content": abstract}]
                },
                "bbox": [332, 400, 937, 520],
            },
            {
                "type": "title",
                "content": {
                    "title_content": [{"type": "text", "content": "1. Introduction"}],
                    "level": 2,
                },
                "bbox": [58, 560, 200, 580],
            },
        ]
    )
    path = tmp_path / f"keywords-{marker_type}.json"
    path.write_text(json.dumps([page]), encoding="utf-8")

    document = build_document(
        job_id=f"keywords-{marker_type}",
        metadata=PaperMetadata(title="Keyword paper", abstract=abstract),
        source=source_reference(),
        content_list_path=path,
    )

    keyword = next(
        block
        for block in document.blocks
        if block.extra.get("front_matter_role") == "keywords"
    )
    assert keyword.id == "b0002"
    assert keyword.kind == "paragraph"
    assert keyword.level == 0
    assert keyword.subtype == "keywords"
    assert keyword.source == expected
    assert keyword.bbox == [58.0, 400.0, 260.0, float(420 + (len(items) - 1) * 11)]
    assert keyword.extra["origin_block_ids"] == [
        f"b{index:04d}" for index in range(2, len(items) + 3)
    ]
    assert document.layout_info["automatic_keyword_groups"] == 1
    assert any(block.source == abstract for block in document.blocks)
    assert not any(
        block.kind == "heading" and block.source == "Keywords:"
        for block in document.blocks
    )


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

    html_path = build_layout_preview(
        document,
        content_list_path=path,
        preview_root=tmp_path / "preview",
    )
    assert "PDF 1-2" in html_path.read_text(encoding="utf-8")


def test_multi_page_figure_run_merges_sentence_continuation(tmp_path: Path) -> None:
    images = tmp_path / "images"
    images.mkdir()
    for name in ("full-page.png", "leading.png"):
        (images / name).write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    payload = [
        {
            "type": "text",
            "text": "Our model uses only unclothed",
            "page_idx": 0,
            "bbox": [502, 900, 927, 945],
        },
        {
            "type": "image",
            "img_path": "images/full-page.png",
            "caption": "Fig. 1 Full-page methods.",
            "page_idx": 1,
            "bbox": [70, 60, 930, 930],
        },
        {
            "type": "image",
            "img_path": "images/leading.png",
            "caption": "Fig. 2 Leading results.",
            "page_idx": 2,
            "bbox": [70, 60, 930, 500],
        },
        {
            "type": "text",
            "text": "back images for screening.",
            "page_idx": 2,
            "bbox": [70, 590, 490, 700],
        },
    ]
    path = tmp_path / "multi_page_float_content_list.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    document = build_document(
        job_id="multi-page-float-job",
        metadata=PaperMetadata(title="Multi-page floats"),
        source=source_reference(),
        content_list_path=path,
    )

    assert [block.kind for block in document.blocks] == ["paragraph", "figure", "figure"]
    merged = document.blocks[0]
    assert merged.source == "Our model uses only unclothed back images for screening."
    assert merged.extra["origin_block_ids"] == ["b0001", "b0004"]
    assert merged.extra["origin_pages"] == [0, 2]
    assert document.layout_info["automatic_paragraph_merges"] == 1


def test_multi_page_continuation_requires_float_on_every_skipped_page(
    tmp_path: Path,
) -> None:
    image = tmp_path / "leading.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    payload = [
        {
            "type": "text",
            "text": "A fragment at the page boundary",
            "page_idx": 0,
            "bbox": [502, 900, 927, 945],
        },
        {
            "type": "image",
            "img_path": "leading.png",
            "caption": "Fig. 1 Appears only on the continuation page.",
            "page_idx": 2,
            "bbox": [70, 60, 930, 500],
        },
        {
            "type": "text",
            "text": "An unrelated paragraph.",
            "page_idx": 2,
            "bbox": [70, 590, 490, 700],
        },
    ]
    path = tmp_path / "missing_float_page_content_list.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    document = build_document(
        job_id="missing-float-page-job",
        metadata=PaperMetadata(title="Missing float page"),
        source=source_reference(),
        content_list_path=path,
    )

    assert [block.kind for block in document.blocks] == ["paragraph", "figure", "paragraph"]
    assert document.layout_info["automatic_paragraph_merges"] == 0


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
    html_path = build_layout_preview(
        document,
        content_list_path=path,
        preview_root=tmp_path / "preview",
    )
    html = html_path.read_text(encoding="utf-8")
    assert 'class="figure-group layout-row"' in html
    assert html.count('class="figure-member"') == 2
    assert html.index('class="paper-front-notes"') < html.index('class="paper-body"')
    assert html.index('class="paper-endnotes"') > html.index('class="paper-body"')
    assert "https://example.test/data\\#anchor" not in html
    css = (tmp_path / "preview" / "assets" / "app.css").read_text(
        encoding="utf-8"
    )
    assert ".figure-members { grid-template-columns: minmax(0, 1fr) !important; }" not in css
    assert "@media (min-width: 641px)" not in css
    assert "@media (max-width: 640px)" not in css
    assert "@media (max-width: 980px)" in css
    assert ".reader-controls button[data-font]" not in css
    assert "font-size: clamp(2.1rem, 4.1vw, 3.2rem);" in css
    assert "padding: clamp(1.5rem, 3.2vw, 2.5rem);" in css
    assert ".source-only.reference {" in css
    assert "background: transparent;" in css
    assert "text-indent: -1.4em;" in css
    divider_rule = css.split(".paper-body > * + *::before {", 1)[1].split("}", 1)[0]
    assert "border-top: 1px solid var(--line);" in divider_rule
    source_rule = css.split(".pair > .source {", 1)[1].split("}", 1)[0]
    assert "border-top:" not in source_rule


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


def test_nearby_split_subfigure_label_is_resolved_by_full_caption(
    tmp_path: Path,
) -> None:
    payload = [
        {
            "type": "image",
            "img_path": "images/a.png",
            "caption": "(a)",
            "page_idx": 0,
            "bbox": [100, 100, 220, 220],
        },
        {
            "type": "image",
            "img_path": "images/detail.png",
            "caption": "IoU=0.81",
            "page_idx": 0,
            "bbox": [240, 100, 360, 220],
        },
        {
            "type": "image",
            "img_path": "images/b.png",
            "caption": "(b)Fig. 2. Split export with (a) overview and (b) examples.",
            "page_idx": 0,
            "bbox": [380, 100, 500, 220],
        },
    ]
    path = tmp_path / "nearby-split.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    document = build_document(
        job_id="nearby-split-job",
        metadata=PaperMetadata(title="Nearby split"),
        source=source_reference(),
        content_list_path=path,
    )
    report = structure_report(document)
    assert report["status"] == "passed"
    assert not any("orphaned subfigure" in error for error in report["errors"])


def test_repairs_displaced_caption_before_manual_subfigure_grouping(
    tmp_path: Path,
) -> None:
    images = tmp_path / "images"
    images.mkdir()
    for name in ("figure-7.png", "figure-8a.png", "figure-8b.png"):
        (images / name).write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    payload = [
        {
            "type": "image",
            "img_path": "images/figure-7.png",
            "caption": "(a)(b)",
            "page_idx": 0,
            "bbox": [100, 100, 500, 220],
        },
        {
            "type": "image",
            "img_path": "images/figure-8a.png",
            "caption": "Fig. 7. Model-size analysis.(a)",
            "page_idx": 0,
            "bbox": [100, 260, 300, 380],
        },
        {
            "type": "image",
            "img_path": "images/figure-8b.png",
            "caption": "(b)Fig. 8. Rank sensitivity analysis.",
            "page_idx": 0,
            "bbox": [300, 260, 500, 380],
        },
    ]
    path = tmp_path / "displaced-caption.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    first_pass = build_document(
        job_id="caption-repair-job",
        metadata=PaperMetadata(title="Caption repair"),
        source=source_reference(),
        content_list_path=path,
        content_sha256=hash_file(path),
    )
    assert first_pass.layout_info["automatic_caption_repairs"] == 1
    assert first_pass.blocks[0].caption_source == "Fig. 7. Model-size analysis."
    assert first_pass.blocks[1].caption_source == "(a)"
    assert first_pass.blocks[2].caption_source == "(b)Fig. 8. Rank sensitivity analysis."

    overrides = LayoutOverrides(
        job_id="caption-repair-job",
        source_sha256="a" * 64,
        content_sha256=hash_file(path),
        operations=[
            LayoutOperation(
                op="group",
                targets=[target_for(block) for block in first_pass.blocks[1:]],
                caption_from=first_pass.blocks[2].id,
                layout="row",
                reason="Confirmed against the source PDF.",
            )
        ],
    )
    document = build_document(
        job_id="caption-repair-job",
        metadata=PaperMetadata(title="Caption repair"),
        source=source_reference(),
        content_list_path=path,
        content_sha256=hash_file(path),
        overrides=overrides,
    )
    assert [block.kind for block in document.blocks] == ["figure", "figure_group"]
    assert document.blocks[0].caption_source == "Fig. 7. Model-size analysis."
    assert document.blocks[1].caption_source == "Fig. 8. Rank sensitivity analysis."


def test_latex_and_plain_author_note_markers_are_extracted(tmp_path: Path) -> None:
    payload = [
        {
            "type": "page_footnote",
            "text": "$^{1}$  Equal contribution.",
            "page_idx": 0,
        },
        {
            "type": "page_footnote",
            "text": "* Corresponding authors.",
            "page_idx": 0,
        },
    ]
    path = tmp_path / "author-notes.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    document = build_document(
        job_id="author-notes-job",
        metadata=PaperMetadata(title="Author notes"),
        source=source_reference(),
        content_list_path=path,
    )
    assert not document.blocks
    assert not document.endnotes
    assert [note.marker for note in document.front_matter_notes] == ["1", "*"]
    assert [note.source for note in document.front_matter_notes] == [
        "Equal contribution.",
        "Corresponding authors.",
    ]


def test_repairs_and_groups_bare_subfigure_grid_in_reviewed_order(
    tmp_path: Path,
) -> None:
    images = tmp_path / "images"
    images.mkdir()
    names = [f"panel-{label}.png" for label in "abcdef"]
    for name in names:
        (images / name).write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    captions = [
        "Predicted labela",
        "b",
        "C",
        "d",
        "e",
        "fFig. 4 Grid comparison. a Model. b-e Experts. f Accuracy.",
    ]
    bboxes = [
        [100, 100, 250, 220],
        [260, 100, 410, 220],
        [100, 240, 250, 360],
        [260, 240, 410, 360],
        [100, 380, 250, 500],
        [260, 380, 410, 500],
    ]
    payload = [
        {
            "type": "image",
            "img_path": f"images/{name}",
            "caption": caption,
            "page_idx": 0,
            "bbox": bbox,
        }
        for name, caption, bbox in zip(names, captions, bboxes, strict=True)
    ]
    path = tmp_path / "bare-grid.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    first_pass = build_document(
        job_id="bare-grid-job",
        metadata=PaperMetadata(title="Bare grid"),
        source=source_reference(),
        content_list_path=path,
        content_sha256=hash_file(path),
    )
    assert first_pass.layout_info["automatic_bare_subfigure_label_repairs"] == 1
    assert [block.caption_source for block in first_pass.blocks[:-1]] == [
        "(a)",
        "(b)",
        "(c)",
        "(d)",
        "(e)",
    ]
    assert first_pass.blocks[-1].caption_source.startswith("(f)Fig. 4")

    overrides = LayoutOverrides(
        job_id="bare-grid-job",
        source_sha256="a" * 64,
        content_sha256=hash_file(path),
        operations=[
            LayoutOperation(
                op="group",
                targets=[target_for(block) for block in first_pass.blocks],
                caption_from=first_pass.blocks[-1].id,
                layout="grid",
                reason="Confirmed row-major panel order against the source PDF.",
            )
        ],
    )
    document = build_document(
        job_id="bare-grid-job",
        metadata=PaperMetadata(title="Bare grid"),
        source=source_reference(),
        content_list_path=path,
        content_sha256=hash_file(path),
        overrides=overrides,
    )
    assert [block.kind for block in document.blocks] == ["figure_group"]
    group = document.blocks[0]
    assert group.layout == "grid"
    assert [member.id for member in group.members] == [
        "b0001",
        "b0002",
        "b0003",
        "b0004",
        "b0005",
        "b0006",
    ]
    assert [member.label for member in group.members] == [
        "(a)",
        "(b)",
        "(c)",
        "(d)",
        "(e)",
        "(f)",
    ]
    assert group.caption_source.startswith("Fig. 4 Grid comparison.")


def test_moves_trailing_equation_tag_outside_array(tmp_path: Path) -> None:
    payload = [
        {
            "type": "equation",
            "text": r"\begin{array}{l} x = 1 \tag {12} \\ \end{array}",
            "page_idx": 0,
            "bbox": [10, 10, 100, 40],
        }
    ]
    path = tmp_path / "array-tag.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    document = build_document(
        job_id="array-tag-job",
        metadata=PaperMetadata(title="Array tag"),
        source=source_reference(),
        content_list_path=path,
    )
    equation = document.blocks[0]
    assert equation.kind == "equation"
    assert equation.source == r"\begin{array}{l} x = 1 \end{array}\tag{12}"
    assert document.layout_info["automatic_equation_tag_repairs"] == 1


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
    result = build_bundle(
        document,
        load_translations(document, translation_dir),
        content_list_path=content_path,
        library_root=tmp_path / "output" / "library",
    )
    chinese = (result.paper_dir / "paper.zh-CN.md").read_text(encoding="utf-8")
    assert "Shared figure note." in chinese
    assert "译文：Shared figure note." in chinese


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
