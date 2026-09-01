from __future__ import annotations

import html
import re
import shutil
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from latex2mathml.converter import convert as latex_to_mathml
from markdown_it import MarkdownIt

from .models import Document
from .util import atomic_write_json, atomic_write_text, read_json, slugify

MATH_PATTERN = re.compile(
    r"(\$\$(?P<dollar_display>.*?)\$\$|\\\[(?P<bracket_display>.*?)\\\]|"
    r"\\\((?P<paren_inline>.*?)\\\)|(?<!\\)\$(?P<dollar_inline>[^$\n]+?)\$)",
    re.DOTALL,
)


class RenderError(RuntimeError):
    """The parsed document cannot be rendered safely."""


def _markdown_renderer() -> MarkdownIt:
    renderer = MarkdownIt("commonmark", {"html": True, "linkify": False})
    renderer.enable("table")
    return renderer


def _mathml(latex: str, *, display: bool) -> str:
    value = latex.strip()
    try:
        result = latex_to_mathml(value)
    except Exception:  # noqa: BLE001 - malformed model output must remain readable
        klass = "math-display math-fallback" if display else "math-inline math-fallback"
        return f'<code class="{klass}">{html.escape(value)}</code>'
    if display and 'display="inline"' in result:
        result = result.replace('display="inline"', 'display="block"', 1)
    elif display:
        result = result.replace("<math", '<math display="block"', 1)
    klass = "math-display" if display else "math-inline"
    return f'<span class="{klass}">{result}</span>'


def render_markdown(value: str) -> str:
    if not value.strip():
        return ""
    replacements: list[str] = []

    def replace_math(match: re.Match[str]) -> str:
        display_text = match.group("dollar_display") or match.group("bracket_display")
        inline_text = match.group("paren_inline") or match.group("dollar_inline")
        replacement = _mathml(
            display_text if display_text is not None else inline_text or "",
            display=display_text is not None,
        )
        index = len(replacements)
        replacements.append(replacement)
        return f"MATHPLACEHOLDER{index}TOKEN"

    protected = MATH_PATTERN.sub(replace_math, value)
    rendered = _markdown_renderer().render(protected)
    for index, replacement in enumerate(replacements):
        rendered = rendered.replace(f"MATHPLACEHOLDER{index}TOKEN", replacement)
    return rendered


def render_inline(value: str) -> str:
    rendered = render_markdown(value).strip()
    if rendered.startswith("<p>") and rendered.endswith("</p>"):
        return rendered[3:-4]
    return rendered


def render_equation(value: str) -> str:
    stripped = value.strip()
    for opening, closing in (("$$", "$$"), ("\\[", "\\]"), ("\\(", "\\)")):
        if stripped.startswith(opening) and stripped.endswith(closing):
            stripped = stripped[len(opening) : -len(closing)]
            break
    return _mathml(stripped, display=True)


def _copy_asset(asset: str, mineru_base: Path, media_dir: Path) -> str:
    if not asset:
        return ""
    candidate = (mineru_base / asset).resolve()
    if not candidate.is_file():
        matches = list(mineru_base.parent.rglob(Path(asset).name))
        candidate = matches[0] if matches else candidate
    if not candidate.is_file():
        raise RenderError(f"MinerU asset is missing: {asset}")
    media_dir.mkdir(parents=True, exist_ok=True)
    destination = media_dir / candidate.name
    if destination.exists() and destination.read_bytes() != candidate.read_bytes():
        destination = media_dir / (
            f"{candidate.stem}-{abs(hash(str(candidate))) & 0xFFFF:x}{candidate.suffix}"
        )
    if not destination.exists():
        shutil.copy2(candidate, destination)
    return f"media/{destination.name}"


def _templates() -> Environment:
    root = Path(__file__).parent / "web"
    return Environment(
        loader=FileSystemLoader(root),
        autoescape=select_autoescape(("html", "xml")),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def build_paper(
    document: Document,
    translations: dict[str, str],
    *,
    content_list_path: Path,
    library_root: Path,
) -> Path:
    title_slug = slugify(document.metadata.title)
    paper_id = f"{title_slug}-{document.source.item_key.lower()}"
    paper_dir = library_root / "papers" / paper_id
    media_dir = paper_dir / "media"
    paper_dir.mkdir(parents=True, exist_ok=True)
    blocks: list[dict[str, Any]] = []
    toc: list[dict[str, Any]] = []
    stream_blocks = document.blocks
    if document.metadata.abstract:
        introduction_index = next(
            (
                index
                for index, block in enumerate(document.blocks)
                if block.kind == "heading"
                and re.sub(r"^\d+(?:\.\d+)*\s*", "", block.source).strip().casefold()
                == "introduction"
            ),
            None,
        )
        if introduction_index is not None:
            # Zotero metadata already supplies the title, authors, and abstract
            # used by the reader masthead. Keep useful pre-introduction links,
            # then begin the reflowed body at the Introduction heading.
            useful_front_matter = [
                block
                for block in document.blocks[:introduction_index]
                if block.kind == "paragraph" and "http" in block.source.casefold()
            ]
            stream_blocks = useful_front_matter + document.blocks[introduction_index:]
    for block in stream_blocks:
        source_html = ""
        target_html = ""
        if block.kind == "equation":
            source_html = render_equation(block.source)
        elif block.kind == "code":
            source_html = f"<pre><code>{html.escape(block.source)}</code></pre>"
        elif block.source:
            source_html = render_markdown(block.source)
        if block.segment_id:
            target_html = render_markdown(translations[block.segment_id])
        if block.kind == "heading":
            source_html = render_inline(block.source)
            target_html = render_inline(translations[block.segment_id])
        caption_target = (
            render_markdown(translations[block.caption_segment_id])
            if block.caption_segment_id
            else ""
        )
        footnote_target = (
            render_markdown(translations[block.footnote_segment_id])
            if block.footnote_segment_id
            else ""
        )
        rendered = {
            "id": block.id,
            "kind": block.kind,
            "level": min(max(block.level, 1), 6),
            "page": block.page + 1,
            "source_html": source_html,
            "target_html": target_html,
            "asset": _copy_asset(block.asset, content_list_path.parent, media_dir),
            "caption_source_html": render_markdown(block.caption_source),
            "caption_target_html": caption_target,
            "footnote_source_html": render_markdown(block.footnote_source),
            "footnote_target_html": footnote_target,
        }
        blocks.append(rendered)
        if block.kind == "heading" and block.source:
            toc.append(
                {
                    "id": block.id,
                    "level": rendered["level"],
                    "source": block.source,
                    "target": translations.get(block.segment_id, ""),
                }
            )
    metadata_title_zh = translations["meta.title"]
    abstract_zh = translations.get("meta.abstract", "")
    template = _templates().get_template("paper.html.j2")
    output = template.render(
        document=document,
        title_zh=metadata_title_zh,
        abstract_zh_html=render_markdown(abstract_zh),
        abstract_source_html=render_markdown(document.metadata.abstract),
        blocks=blocks,
        toc=toc,
    )
    atomic_write_text(paper_dir / "index.html", output)
    paper_manifest = {
        "paper_id": paper_id,
        "job_id": document.job_id,
        "item_key": document.source.item_key,
        "title": document.metadata.title,
        "title_zh": metadata_title_zh,
        "creators": document.metadata.creators,
        "date": document.metadata.date,
        "publication": document.metadata.publication,
        "doi": document.metadata.doi,
        "tags": document.metadata.tags,
        "href": f"papers/{paper_id}/index.html",
    }
    atomic_write_json(paper_dir / "paper.json", paper_manifest)
    _write_shared_assets(library_root)
    build_shelf(library_root)
    return paper_dir / "index.html"


def _write_shared_assets(library_root: Path) -> None:
    web_root = Path(__file__).parent / "web"
    assets = library_root / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    for name in ("app.css", "app.js"):
        atomic_write_text(assets / name, (web_root / name).read_text(encoding="utf-8"))


def build_shelf(library_root: Path) -> Path:
    papers: list[dict[str, Any]] = []
    papers_root = library_root / "papers"
    if papers_root.exists():
        for manifest_path in sorted(papers_root.glob("*/paper.json")):
            raw = read_json(manifest_path)
            if isinstance(raw, dict):
                papers.append(raw)
    papers.sort(
        key=lambda item: (str(item.get("date", "")), str(item.get("title", ""))),
        reverse=True,
    )
    output = _templates().get_template("shelf.html.j2").render(papers=papers)
    path = library_root / "index.html"
    atomic_write_text(path, output)
    return path
