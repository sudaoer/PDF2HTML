from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Annotated, Any, cast

import httpx
import typer
from pydantic import ValidationError

from .bundle import BundleError, build_bundle
from .document import build_document, write_document
from .layout import LayoutOverrideError, load_layout_overrides
from .mineru_runner import (
    MinerUError,
    executable_path,
    mineru_version,
    run_mineru,
)
from .models import (
    Document,
    Glossary,
    JobManifest,
    MinerURecord,
    SourceReference,
    migrate_job_manifest,
)
from .render import build_layout_preview, build_paper
from .translations import (
    TranslationError,
    load_translations,
    reconcile_translation_artifacts,
)
from .util import (
    atomic_write_json,
    hash_file,
    hash_text,
    read_json,
    relative_to,
    slugify,
    utc_now,
)
from .verify import VerificationError, structure_report, verify_output, write_structure_report
from .zotero import Library, SelectedPaper, ZoteroClient, ZoteroError

app = typer.Typer(
    name="pdf2html",
    help="Build an offline bilingual reading library from Zotero PDFs.",
    no_args_is_help=True,
)

LAYOUT_PIPELINE_VERSION = "3"


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
    return JobManifest.model_validate(migrate_job_manifest(read_json(path)))


def _save_manifest(manifest: JobManifest) -> None:
    manifest.schema_version = 3
    manifest.updated_at = utc_now()
    atomic_write_json(_manifest_path(manifest.job_id), manifest.model_dump(mode="json"))


def _layout_inputs(manifest: JobManifest) -> tuple[Path, str, Path, str, str]:
    source_copy = _job_dir(manifest.job_id) / "source.pdf"
    if not source_copy.is_file() or hash_file(source_copy) != manifest.source.sha256:
        raise ValueError("The job source PDF is missing or its SHA-256 changed.")
    content_path = _root() / manifest.mineru.content_list_path
    if not content_path.is_file():
        raise ValueError(f"MinerU content list is missing: {content_path}")
    content_hash = hash_file(content_path)
    override_path = _job_dir(manifest.job_id) / "layout_overrides.json"
    override_hash = hash_file(override_path) if override_path.is_file() else ""
    input_hash = hash_text(
        ":".join(
            (
                LAYOUT_PIPELINE_VERSION,
                manifest.source.sha256,
                content_hash,
                override_hash,
            )
        )
    )
    return content_path, content_hash, override_path, override_hash, input_hash


def _write_layout(manifest: JobManifest) -> dict[str, Any]:
    content_path, content_hash, override_path, override_hash, input_hash = _layout_inputs(
        manifest
    )
    override_errors: list[str] = []
    overrides = None
    try:
        overrides = load_layout_overrides(
            override_path,
            job_id=manifest.job_id,
            source_sha256=manifest.source.sha256,
            content_sha256=content_hash,
        )
        document = build_document(
            job_id=manifest.job_id,
            metadata=manifest.metadata,
            source=manifest.source,
            content_list_path=content_path,
            content_sha256=content_hash,
            overrides=overrides,
        )
    except LayoutOverrideError as exc:
        override_errors.append(str(exc))
        document = build_document(
            job_id=manifest.job_id,
            metadata=manifest.metadata,
            source=manifest.source,
            content_list_path=content_path,
            content_sha256=content_hash,
        )
    document.layout_info.update(
        {
            "pipeline_version": LAYOUT_PIPELINE_VERSION,
            "layout_input_hash": input_hash,
            "overrides_sha256": override_hash,
        }
    )
    job_dir = _job_dir(manifest.job_id)
    layout_document_path = job_dir / "layout_document.json"
    layout_report_path = job_dir / "layout_report.json"
    preview_root = job_dir / "layout_preview"
    write_document(document, layout_document_path)
    preview_path = build_layout_preview(
        document,
        content_list_path=content_path,
        preview_root=preview_root,
    )
    report = write_structure_report(
        document,
        layout_report_path,
        extra_errors=override_errors,
    )
    manifest.layout_document_path = relative_to(layout_document_path, _root())
    manifest.layout_preview_path = relative_to(preview_path, _root())
    manifest.layout_report_path = relative_to(layout_report_path, _root())
    manifest.layout_overrides_path = relative_to(override_path, _root())
    manifest.layout_input_hash = input_hash
    manifest.layout_document_hash = hash_file(layout_document_path)
    manifest.translation_task_dir = ""
    manifest.status = (
        "awaiting_layout_review" if report["status"] == "passed" else "layout_review_required"
    )
    manifest.error = "; ".join(report["errors"])
    manifest.warnings = list(report["warnings"])
    _save_manifest(manifest)
    return report


def _assert_current_layout(manifest: JobManifest) -> tuple[Document, dict[str, Any]]:
    _, _, _, _, input_hash = _layout_inputs(manifest)
    if input_hash != manifest.layout_input_hash:
        raise ValueError(
            "Layout inputs changed; run the layout command and review the preview again."
        )
    layout_path = _root() / manifest.layout_document_path
    report_path = _root() / manifest.layout_report_path
    if not layout_path.is_file() or not report_path.is_file():
        raise ValueError("Layout artifacts are missing; run the layout command first.")
    if hash_file(layout_path) != manifest.layout_document_hash:
        raise ValueError("The reviewed layout document changed; run layout again.")
    report_raw = read_json(report_path)
    if not isinstance(report_raw, dict) or report_raw.get("status") != "passed":
        raise ValueError("Layout validation has not passed.")
    document = Document.model_validate(read_json(layout_path))
    current_report = structure_report(document)
    if current_report["status"] != "passed":
        raise ValueError("The reviewed layout document no longer passes structural validation.")
    return document, current_report


def _load_finalized_document(manifest: JobManifest) -> Document:
    if not manifest.document_path or not manifest.layout_document_hash:
        raise ValueError("The job has no finalized layout document.")
    document_path = _root() / manifest.document_path
    if not document_path.is_file():
        raise ValueError(f"The finalized layout document is missing: {document_path}")
    if hash_file(document_path) != manifest.layout_document_hash:
        raise ValueError(
            "The finalized document no longer matches the reviewed layout; "
            "run finalize-layout again."
        )
    document = Document.model_validate(read_json(document_path))
    if document.job_id != manifest.job_id:
        raise ValueError(
            f"The finalized document belongs to {document.job_id}, not {manifest.job_id}."
        )
    return document


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
        manifest = JobManifest.model_validate(migrate_job_manifest(read_json(manifest_path)))
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
        manifest.document_path = ""
        report = _write_layout(manifest)
        typer.echo(
            f"Prepared {job_id}: layout {report['status']}, "
            f"{report['blocks']} blocks and {report['figure_groups']} figure groups."
        )
        typer.echo(f"Layout preview: {_root() / manifest.layout_preview_path}")
        typer.echo(f"Layout report: {_root() / manifest.layout_report_path}")
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
    """Copy selected PDFs, run MinerU, and create a layout review preview."""
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


@app.command(name="layout")
def layout_command(
    job: Annotated[str, typer.Option("--job", help="Prepared job ID")],
) -> None:
    """Reapply deterministic repairs and overrides, then refresh the source preview."""
    try:
        manifest = _load_manifest(job)
        report = _write_layout(manifest)
        display_report = {key: value for key, value in report.items() if key != "override_targets"}
        typer.echo(json.dumps(display_report, ensure_ascii=False, indent=2))
        typer.echo(f"Layout preview: {_root() / manifest.layout_preview_path}")
        if report["status"] != "passed":
            raise typer.Exit(1)
    except (ValidationError, OSError, ValueError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command(name="finalize-layout")
def finalize_layout_command(
    job: Annotated[str, typer.Option("--job", help="Reviewed job ID")],
) -> None:
    """Lock an unchanged reviewed layout and create translation artifacts."""
    try:
        manifest = _load_manifest(job)
        document, _ = _assert_current_layout(manifest)
        job_dir = _job_dir(job)
        document_path = job_dir / "document.json"
        task_dir = job_dir / "translation_tasks"
        translation_dir = job_dir / "translations"
        reuse_report_path = job_dir / "translation_reuse.json"
        old_document_path = job_dir / "document.json"
        archive_key = (
            hash_file(old_document_path)[:16]
            if old_document_path.is_file()
            else "initial"
        )
        reuse_report = reconcile_translation_artifacts(
            document,
            task_dir=task_dir,
            translation_dir=translation_dir,
            history_dir=job_dir / "translation_history",
            archive_key=archive_key,
            report_path=reuse_report_path,
        )
        write_document(document, document_path)
        manifest.document_path = relative_to(document_path, _root())
        manifest.translation_task_dir = relative_to(task_dir, _root())
        manifest.translation_dir = relative_to(translation_dir, _root())
        manifest.translation_reuse_report = relative_to(reuse_report_path, _root())
        manifest.status = "awaiting_translation"
        manifest.error = ""
        _save_manifest(manifest)
        typer.echo(f"Finalized layout: {document_path}")
        typer.echo(json.dumps(reuse_report, ensure_ascii=False, indent=2))
        typer.echo(f"Translation tasks: {task_dir}")
    except (ValidationError, OSError, ValueError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command(name="build")
def build_command(
    job: Annotated[str, typer.Option("--job", help="Prepared job ID")],
) -> None:
    """Validate Codex translations and build the paper plus shelf."""
    try:
        manifest = _load_manifest(job)
        if manifest.status not in {"awaiting_translation", "built", "verified"}:
            raise ValueError("The layout must be finalized before build.")
        translation_dir = _root() / manifest.translation_dir
        content_list_path = _root() / manifest.mineru.content_list_path
        document = _load_finalized_document(manifest)
        translations = load_translations(document, translation_dir)
        bundle = build_bundle(
            document,
            translations,
            content_list_path=content_list_path,
            library_root=_root() / "output" / "library",
        )
        html_path = build_paper(
            document,
            translations,
            content_list_path=content_list_path,
            library_root=_root() / "output" / "library",
        )
        manifest.output_path = relative_to(html_path, _root())
        manifest.legacy_html_path = manifest.output_path
        manifest.bundle_path = relative_to(bundle.bundle_path, _root())
        manifest.bundle_revision = bundle.revision
        manifest.status = "built"
        manifest.error = ""
        _save_manifest(manifest)
        typer.echo(f"Built bundle {bundle.bundle_path} ({bundle.revision})")
        typer.echo(f"Built legacy HTML {html_path}")
    except (BundleError, ValidationError, TranslationError, OSError, ValueError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(1) from exc


@app.command()
def verify(
    job: Annotated[str, typer.Option("--job", help="Built job ID")],
) -> None:
    """Run deterministic offline and translation checks for a built paper."""
    try:
        manifest = _load_manifest(job)
        if manifest.status not in {"built", "verified"}:
            raise ValueError("Build the finalized paper before verification.")
        document = _load_finalized_document(manifest)
        report = verify_output(
            document,
            _root() / manifest.translation_dir,
            _root() / manifest.output_path,
            _job_dir(job) / "verification.json",
        )
        manifest.status = "verified"
        _save_manifest(manifest)
        typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    except (ValidationError, TranslationError, VerificationError, OSError, ValueError) as exc:
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
        manifest = JobManifest.model_validate(migrate_job_manifest(read_json(path)))
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
