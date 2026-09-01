from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Annotated, Any, cast

import httpx
import typer
from pydantic import ValidationError

from .document import build_document, write_document, write_translation_tasks
from .mineru_runner import (
    MinerUError,
    executable_path,
    mineru_version,
    run_mineru,
)
from .models import Document, Glossary, JobManifest, MinerURecord, SourceReference
from .render import build_paper
from .translations import TranslationError, load_translations
from .util import atomic_write_json, hash_file, read_json, relative_to, slugify, utc_now
from .verify import VerificationError, verify_output
from .zotero import Library, SelectedPaper, ZoteroClient, ZoteroError

app = typer.Typer(
    name="pdf2html",
    help="Build an offline bilingual reading library from Zotero PDFs.",
    no_args_is_help=True,
)


def _root() -> Path:
    return Path.cwd().resolve()


def _job_dir(job_id: str) -> Path:
    return _root() / ".work" / "jobs" / job_id


def _manifest_path(job_id: str) -> Path:
    return _job_dir(job_id) / "manifest.json"


def _load_manifest(job_id: str) -> JobManifest:
    path = _manifest_path(job_id)
    if not path.is_file():
        raise typer.BadParameter(f"Unknown job: {job_id}")
    return JobManifest.model_validate(read_json(path))


def _save_manifest(manifest: JobManifest) -> None:
    manifest.updated_at = utc_now()
    atomic_write_json(_manifest_path(manifest.job_id), manifest.model_dump(mode="json"))


def _selected_items(
    client: ZoteroClient,
    item: str | None,
    collection: str | None,
    tag: str | None,
) -> list[tuple[Library, dict[str, Any]]]:
    selectors = [value is not None for value in (item, collection, tag)]
    if sum(selectors) != 1:
        raise typer.BadParameter("Pass exactly one of --item, --collection, or --tag.")
    if item is not None:
        matches = client.select_item(item)
        if not matches:
            raise ZoteroError(f"No Zotero item matched: {item}")
        if len(matches) > 1:
            candidates = "; ".join(
                f"{library.name}: {entry.get('key')} "
                f"{entry.get('data', {}).get('title', '')}"
                for library, entry in matches[:10]
            )
            raise ZoteroError(f"The item selector is ambiguous: {candidates}")
        return matches
    if collection is not None:
        matches = client.select_collection(collection)
        if not matches:
            raise ZoteroError(f"No collection or papers matched: {collection}")
        return matches
    assert tag is not None
    matches = client.select_tag(tag)
    if not matches:
        raise ZoteroError(f"No papers matched tag: {tag}")
    return matches


@app.command()
def preflight() -> None:
    """Check Zotero, MinerU, Poppler, Python, and CUDA before a paper job."""
    failures: list[str] = []
    details: dict[str, Any] = {
        "python": sys.version.split()[0],
        "mineru": mineru_version(),
    }
    try:
        with ZoteroClient() as client:
            details["zotero"] = client.preflight()
    except ZoteroError as exc:
        failures.append(str(exc))
    mineru_exe = executable_path("mineru")
    poppler_exe = executable_path("pdftoppm")
    details["mineru_executable"] = str(mineru_exe or "")
    details["pdftoppm_executable"] = str(poppler_exe or "")
    if mineru_exe is None or not details["mineru"]:
        failures.append("MinerU is not installed in the active Pixi environment.")
    if poppler_exe is None:
        failures.append("Poppler pdftoppm is not installed in the active Pixi environment.")
    try:
        import torch

        torch_module = cast(Any, torch)

        details["torch"] = {
            "version": torch.__version__,
            "cuda_version": torch_module.version.cuda,
            "cuda_available": torch_module.cuda.is_available(),
            "device": torch_module.cuda.get_device_name(0)
            if torch_module.cuda.is_available()
            else "",
        }
        if not torch_module.cuda.is_available():
            failures.append("CUDA-enabled PyTorch is required for the configured MinerU backend.")
    except ImportError:
        failures.append("PyTorch is not installed.")
    typer.echo(json.dumps(details, ensure_ascii=False, indent=2))
    if failures:
        for failure in failures:
            typer.echo(f"ERROR: {failure}", err=True)
        raise typer.Exit(1)
    typer.echo("Preflight passed.")


def _prepare_one(
    client: ZoteroClient,
    selected: SelectedPaper,
    *,
    backend: str,
    effort: str,
    force_parse: bool,
) -> JobManifest:
    source_hash = hash_file(selected.pdf_path)
    item_key = str(selected.item.get("key", ""))
    attachment_key = str(selected.attachment.get("key", ""))
    job_id = (
        f"{slugify(selected.metadata.title, max_length=40)}-"
        f"{item_key.lower()}-{source_hash[:12]}"
    )
    job_dir = _job_dir(job_id)
    source_copy = job_dir / "source.pdf"
    job_dir.mkdir(parents=True, exist_ok=True)
    if not source_copy.is_file() or hash_file(source_copy) != source_hash:
        shutil.copy2(selected.pdf_path, source_copy)
    now = utc_now()
    source = SourceReference(
        server_id=client.server_id,
        library_prefix=selected.library.prefix,
        item_key=item_key,
        item_version=int(selected.item.get("version") or 0),
        attachment_key=attachment_key,
        attachment_version=int(selected.attachment.get("version") or 0),
        original_path=str(selected.pdf_path.resolve()),
        sha256=source_hash,
    )
    manifest_path = _manifest_path(job_id)
    if manifest_path.is_file():
        manifest = JobManifest.model_validate(read_json(manifest_path))
        manifest.status = "parsing"
        manifest.error = ""
        manifest.source = source
        manifest.metadata = selected.metadata
    else:
        manifest = JobManifest(
            job_id=job_id,
            created_at=now,
            updated_at=now,
            status="parsing",
            workspace=str(_root()),
            metadata=selected.metadata,
            source=source,
        )
    _save_manifest(manifest)
    mineru_dir = job_dir / "mineru"
    try:
        content_list, command = run_mineru(
            source_copy,
            mineru_dir,
            backend=backend,
            effort=effort,
            force=force_parse,
        )
        document = build_document(
            job_id=job_id,
            metadata=selected.metadata,
            source=source,
            content_list_path=content_list,
        )
        document_path = job_dir / "document.json"
        task_dir = job_dir / "translation_tasks"
        translation_dir = job_dir / "translations"
        translation_dir.mkdir(parents=True, exist_ok=True)
        write_document(document, document_path)
        task_paths = write_translation_tasks(document, task_dir)
        glossary_path = job_dir / "glossary.json"
        if not glossary_path.exists():
            atomic_write_json(
                glossary_path,
                Glossary(job_id=job_id).model_dump(mode="json"),
            )
        manifest.mineru = MinerURecord(
            version=mineru_version(),
            backend=backend,
            effort=effort,
            command=command or manifest.mineru.command,
            content_list_path=relative_to(content_list, _root()),
        )
        manifest.document_path = relative_to(document_path, _root())
        manifest.translation_task_dir = relative_to(task_dir, _root())
        manifest.translation_dir = relative_to(translation_dir, _root())
        manifest.status = "awaiting_translation"
        _save_manifest(manifest)
        typer.echo(
            f"Prepared {job_id}: {len(document.blocks)} blocks, "
            f"{len(document.segments)} translation segments in {len(task_paths)} files."
        )
        typer.echo(f"Translation tasks: {task_dir}")
        return manifest
    except Exception as exc:
        manifest.status = "failed"
        manifest.error = str(exc)
        _save_manifest(manifest)
        raise


@app.command()
def prepare(
    item: Annotated[str | None, typer.Option("--item", help="Item key, link, or title")] = None,
    collection: Annotated[
        str | None, typer.Option("--collection", help="Collection key or exact name")
    ] = None,
    tag: Annotated[str | None, typer.Option("--tag", help="Exact Zotero tag")] = None,
    backend: Annotated[str, typer.Option("--backend")] = "hybrid-engine",
    effort: Annotated[str, typer.Option("--effort")] = "medium",
    force_parse: Annotated[bool, typer.Option("--force-parse")] = False,
) -> None:
    """Copy selected Zotero PDFs, run MinerU, and create Codex translation tasks."""
    if backend not in {"hybrid-engine", "vlm-engine", "pipeline"}:
        raise typer.BadParameter("Unsupported local backend.")
    if effort not in {"medium", "high"}:
        raise typer.BadParameter("--effort must be medium or high.")
    failures: list[str] = []
    prepared = 0
    try:
        with ZoteroClient() as client:
            client.preflight()
            matches = _selected_items(client, item, collection, tag)
            for library, raw in matches:
                try:
                    selected = client.resolve_paper(library, raw)
                    _prepare_one(
                        client,
                        selected,
                        backend=backend,
                        effort=effort,
                        force_parse=force_parse,
                    )
                    prepared += 1
                except (ZoteroError, MinerUError, ValueError) as exc:
                    label = raw.get("data", {}).get("title") or raw.get("key")
                    failures.append(f"{label}: {exc}")
                    if item is not None:
                        raise
    except (ZoteroError, MinerUError, httpx.HTTPError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(1) from exc
    for failure in failures:
        typer.echo(f"ERROR: {failure}", err=True)
    if prepared == 0 or failures:
        raise typer.Exit(1)
@app.command(name="build")
def build_command(
    job: Annotated[str, typer.Option("--job", help="Prepared job ID")],
) -> None:
    """Validate Codex translations and build the paper plus shelf."""
    try:
        manifest = _load_manifest(job)
        document_path = _root() / manifest.document_path
        translation_dir = _root() / manifest.translation_dir
        content_list_path = _root() / manifest.mineru.content_list_path
        document = Document.model_validate(read_json(document_path))
        translations = load_translations(document, translation_dir)
        html_path = build_paper(
            document,
            translations,
            content_list_path=content_list_path,
            library_root=_root() / "output" / "library",
        )
        manifest.output_path = relative_to(html_path, _root())
        manifest.status = "built"
        manifest.error = ""
        _save_manifest(manifest)
        typer.echo(f"Built {html_path}")
    except (ValidationError, TranslationError, OSError, ValueError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command()
def verify(
    job: Annotated[str, typer.Option("--job", help="Built job ID")],
) -> None:
    """Run deterministic offline and translation checks for a built paper."""
    try:
        manifest = _load_manifest(job)
        document = Document.model_validate(read_json(_root() / manifest.document_path))
        report = verify_output(
            document,
            _root() / manifest.translation_dir,
            _root() / manifest.output_path,
            _job_dir(job) / "verification.json",
        )
        manifest.status = "verified"
        _save_manifest(manifest)
        typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    except (ValidationError, TranslationError, VerificationError, OSError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command()
def status(
    job: Annotated[str | None, typer.Option("--job", help="Optional job ID")] = None,
) -> None:
    """Show durable job state."""
    paths = (
        [_manifest_path(job)]
        if job
        else sorted((_root() / ".work" / "jobs").glob("*/manifest.json"))
    )
    if not paths or not any(path.is_file() for path in paths):
        typer.echo("No jobs found.")
        return
    records: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            continue
        manifest = JobManifest.model_validate(read_json(path))
        records.append(
            {
                "job_id": manifest.job_id,
                "status": manifest.status,
                "title": manifest.metadata.title,
                "updated_at": manifest.updated_at,
                "output": manifest.output_path,
                "error": manifest.error,
            }
        )
    typer.echo(json.dumps(records, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    app()
