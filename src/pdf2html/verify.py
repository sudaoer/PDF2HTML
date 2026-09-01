from __future__ import annotations

from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from .models import Document
from .translations import load_translations
from .util import atomic_write_json


class VerificationError(RuntimeError):
    """A generated paper failed deterministic verification."""


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
    errors: list[str] = []
    warnings: list[str] = []
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
        warnings.append(f"{fallback_math} formulas fell back to readable LaTeX text.")
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
        "warnings": warnings,
        "errors": errors,
    }
    atomic_write_json(report_path, report)
    if errors:
        raise VerificationError("; ".join(errors))
    return report
