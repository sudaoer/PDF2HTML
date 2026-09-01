from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from .models import Document
from .translations import load_translations
from .util import atomic_write_json


class VerificationError(RuntimeError):
    """A generated paper failed deterministic verification."""


SUBFIGURE_ONLY = re.compile(r"^\s*\([a-z]\)\s*$", re.IGNORECASE)
TEX_CONTROL_SEQUENCE = re.compile(r"\\[A-Za-z]+")
TEXT_KINDS = {"paragraph", "heading", "list", "aside", "page_footnote"}


def structure_report(
    document: Document, *, extra_errors: list[str] | None = None
) -> dict[str, Any]:
    errors = list(extra_errors or [])
    warnings: list[str] = []
    block_ids = [block.id for block in document.blocks]
    if len(block_ids) != len(set(block_ids)):
        errors.append("Document block IDs are not unique.")
    segment_ids = [segment.id for segment in document.segments]
    if len(segment_ids) != len(set(segment_ids)):
        errors.append("Document segment IDs are not unique.")
    member_ids: list[str] = []
    for block in document.blocks:
        if block.kind in TEXT_KINDS and not block.source.strip():
            errors.append(f"Empty body block remains: {block.id}")
        if block.kind in {"aside", "page_footnote", "front_note", "endnote"}:
            errors.append(f"Footnote-like block remains in the body: {block.id}")
        if block.kind == "figure":
            if not block.asset:
                errors.append(f"Figure {block.id} has no asset.")
            if SUBFIGURE_ONLY.fullmatch(block.caption_source):
                errors.append(f"Figure {block.id} has an orphaned subfigure label.")
        if block.kind == "figure_group":
            if len(block.members) < 2:
                errors.append(f"Figure group {block.id} has fewer than two members.")
            if not block.caption_source.strip():
                errors.append(f"Figure group {block.id} has no shared caption.")
            labels = [member.label.casefold() for member in block.members]
            expected_labels = [
                f"({chr(ord('a') + index)})" for index in range(len(block.members))
            ]
            if any(not member.asset or not member.label for member in block.members):
                errors.append(f"Figure group {block.id} has an incomplete member.")
            if len(labels) != len(set(labels)):
                errors.append(f"Figure group {block.id} has duplicate member labels.")
            if labels != expected_labels:
                errors.append(
                    f"Figure group {block.id} member labels are not consecutive from (a)."
                )
            if any(member.page != block.page for member in block.members):
                errors.append(f"Figure group {block.id} crosses PDF pages.")
            member_ids.extend(member.id for member in block.members)
    if len(member_ids) != len(set(member_ids)):
        errors.append("Figure member IDs are not unique.")
    note_ids = [note.id for note in [*document.front_matter_notes, *document.endnotes]]
    if len(note_ids) != len(set(note_ids)):
        errors.append("Document note IDs are not unique.")
    return {
        "job_id": document.job_id,
        "status": "failed" if errors else "passed",
        "blocks": len(document.blocks),
        "segments": len(document.segments),
        "figure_groups": sum(block.kind == "figure_group" for block in document.blocks),
        "front_matter_notes": len(document.front_matter_notes),
        "endnotes": len(document.endnotes),
        "normalization": {
            key: value
            for key, value in document.layout_info.items()
            if key != "override_targets"
        },
        "override_targets": document.layout_info.get("override_targets", []),
        "warnings": warnings,
        "errors": errors,
    }


def write_structure_report(
    document: Document,
    report_path: Path,
    *,
    extra_errors: list[str] | None = None,
) -> dict[str, Any]:
    report = structure_report(document, extra_errors=extra_errors)
    atomic_write_json(report_path, report)
    return report


def verify_output(
    document: Document,
    translation_dir: Path,
    html_path: Path,
    report_path: Path,
) -> dict[str, Any]:
    translations = load_translations(document, translation_dir)
    if not html_path.is_file():
        raise VerificationError(f"Generated HTML is missing: {html_path}")
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    structural = structure_report(document)
    errors: list[str] = list(structural["errors"])
    warnings: list[str] = list(structural["warnings"])
    title = soup.find("title")
    if title is None or not title.get_text(strip=True):
        errors.append("The document has no HTML title.")
    if soup.find("main") is None:
        errors.append("The document has no main landmark.")
    for marker in ("MATHPLACEHOLDER", "target: null", "TODO", "{{", "}}"):
        if marker in str(soup):
            errors.append(f"Unresolved marker remains in HTML: {marker}")
    missing_images: list[str] = []
    material_images = [image for image in soup.find_all("img") if image.get("src")]
    for image in material_images:
        source = str(image.get("src", ""))
        if not source:
            continue
        if source.startswith(("http://", "https://", "//")):
            errors.append(f"Remote image dependency is not allowed: {source}")
            continue
        candidate = (html_path.parent / source).resolve()
        if not candidate.is_file():
            missing_images.append(source)
    if missing_images:
        errors.append(f"Missing images: {missing_images[:5]}")
    for script in soup.find_all("script", src=True):
        source = str(script.get("src", ""))
        if source.startswith(("http://", "https://", "//")):
            errors.append(f"Remote script dependency is not allowed: {source}")
        elif not (html_path.parent / source).resolve().is_file():
            errors.append(f"Missing script: {source}")
    for link in soup.find_all("link", rel="stylesheet"):
        href = str(link.get("href", ""))
        if href.startswith(("http://", "https://", "//")):
            errors.append(f"Remote stylesheet dependency is not allowed: {href}")
        elif not (html_path.parent / href).resolve().is_file():
            errors.append(f"Missing stylesheet: {href}")
    pair_count = len(soup.select(".pair"))
    if pair_count == 0:
        errors.append("The document contains no bilingual content pairs.")
    fallback_math = len(soup.select(".math-fallback"))
    if fallback_math:
        errors.append(f"{fallback_math} formulas fell back to readable LaTeX text.")
    raw_mathml_commands = [
        node.get_text(" ", strip=True)
        for node in soup.select("math mtext, math mi, math mo")
        if TEX_CONTROL_SEQUENCE.search(node.get_text(" ", strip=True))
    ]
    if raw_mathml_commands:
        errors.append(
            "MathML contains unparsed TeX control sequences: "
            f"{raw_mathml_commands[:3]}"
        )
    expected_groups = sum(block.kind == "figure_group" for block in document.blocks)
    rendered_groups = soup.select("figure.figure-group")
    if len(rendered_groups) != expected_groups:
        errors.append(
            f"Expected {expected_groups} figure groups, rendered {len(rendered_groups)}."
        )
    for group in rendered_groups:
        expected_members = int(str(group.get("data-members") or "0"))
        if len(group.select(".figure-member img")) != expected_members:
            errors.append(f"Figure group {group.get('id')} rendered the wrong image count.")
    if soup.select(".paper-body .paper-front-notes"):
        errors.append("Front-matter notes were rendered inside the paper body.")
    if soup.select(".paper-body .paper-endnotes"):
        errors.append("Endnotes were rendered inside the paper body.")
    if soup.select(".paper-body .aside, .paper-body .page_footnote"):
        errors.append("Footnote-like content was rendered through the body branch.")
    for body_pair in soup.select(".paper-body > .text-pair"):
        if not body_pair.get_text(strip=True):
            errors.append(f"Empty body pair was rendered: {body_pair.get('id')}")
    report: dict[str, Any] = {
        "job_id": document.job_id,
        "status": "failed" if errors else "passed",
        "html": str(html_path.resolve()),
        "blocks": len(document.blocks),
        "segments": len(document.segments),
        "translations": len(translations),
        "bilingual_pairs": pair_count,
        "images": len(material_images),
        "math_fallbacks": fallback_math,
        "figure_groups": expected_groups,
        "front_matter_notes": len(document.front_matter_notes),
        "endnotes": len(document.endnotes),
        "warnings": warnings,
        "errors": errors,
    }
    atomic_write_json(report_path, report)
    if errors:
        raise VerificationError("; ".join(errors))
    return report
