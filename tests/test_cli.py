from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_pipeline import fill_tasks, prepared_document, write_translation_tasks
from typer.testing import CliRunner

from pdf2html import cli
from pdf2html.cli import app
from pdf2html.models import JobManifest, MinerURecord
from pdf2html.util import atomic_write_json, hash_file, relative_to, utc_now


@pytest.fixture(autouse=True)
def isolate_knowledge_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PDF2HTML_KNOWLEDGE_ROOT", raising=False)


def _make_job(workspace: Path, job_id: str = "routing-job") -> JobManifest:
    document, content_path = prepared_document(workspace)
    document.job_id = job_id
    job_dir = workspace / ".work" / "jobs" / job_id
    job_dir.mkdir(parents=True)
    document_path = job_dir / "document.json"
    from pdf2html.document import write_document

    write_document(document, document_path)
    task_dir = job_dir / "translation_tasks"
    translation_dir = job_dir / "translations"
    write_translation_tasks(document, task_dir)
    fill_tasks(task_dir, translation_dir)
    now = utc_now()
    manifest = JobManifest(
        job_id=job_id,
        created_at=now,
        updated_at=now,
        status="awaiting_translation",
        workspace=str(workspace),
        metadata=document.metadata,
        source=document.source,
        mineru=MinerURecord(content_list_path=relative_to(content_path, workspace)),
        document_path=relative_to(document_path, workspace),
        layout_document_hash=hash_file(document_path),
        translation_dir=relative_to(translation_dir, workspace),
    )
    atomic_write_json(job_dir / "manifest.json", manifest.model_dump(mode="json"))
    return manifest


def test_cli_build_verify_and_serve_share_external_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    knowledge_root = tmp_path.parent / "Research" / "knowledge"
    (tmp_path / "pdf2html.local.toml").write_text(
        f"knowledge_root = {str(knowledge_root)!r}\n", encoding="utf-8"
    )
    manifest = _make_job(tmp_path)
    monkeypatch.setattr(cli, "_root", lambda: tmp_path.resolve())
    runner = CliRunner()

    built = runner.invoke(app, ["build", "--job", manifest.job_id])
    assert built.exit_code == 0, built.output
    manifest_path = tmp_path / ".work" / "jobs" / manifest.job_id / "manifest.json"
    saved = json.loads(manifest_path.read_text())
    bundle_path = Path(saved["bundle_path"])
    assert bundle_path.is_absolute()
    expected = knowledge_root / "library" / "papers" / "routing-networks-abcd2345" / "paper.json"
    assert bundle_path == expected.resolve()
    assert not (tmp_path / "output").exists()

    verified = runner.invoke(app, ["verify", "--job", manifest.job_id])
    assert verified.exit_code == 0, verified.output
    assert json.loads(verified.output)["status"] == "passed"

    served: list[tuple[Path, str, int]] = []

    def capture_run(application: object, *, host: str, port: int) -> None:
        library = application.state.registry.library_root  # type: ignore[attr-defined]
        served.append((library, host, port))

    monkeypatch.setattr(cli.uvicorn, "run", capture_run)
    default_server = runner.invoke(app, ["serve"])
    assert default_server.exit_code == 0, default_server.output
    custom_server = runner.invoke(app, ["serve", "--host", "127.0.0.1", "--port", "8876"])
    assert custom_server.exit_code == 0, custom_server.output
    assert served == [
        ((knowledge_root / "library").resolve(), "0.0.0.0", 8765),
        ((knowledge_root / "library").resolve(), "127.0.0.1", 8876),
    ]


def test_cli_verify_rejects_bundle_from_another_configured_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_root = tmp_path.parent / "first-knowledge"
    (tmp_path / "pdf2html.local.toml").write_text(
        f"knowledge_root = {str(first_root)!r}\n", encoding="utf-8"
    )
    manifest = _make_job(tmp_path, "wrong-library-job")
    monkeypatch.setattr(cli, "_root", lambda: tmp_path.resolve())
    runner = CliRunner()
    built = runner.invoke(app, ["build", "--job", manifest.job_id])
    assert built.exit_code == 0, built.output

    second_root = tmp_path.parent / "second-knowledge"
    (tmp_path / "pdf2html.local.toml").write_text(
        f"knowledge_root = {str(second_root)!r}\n", encoding="utf-8"
    )
    verified = runner.invoke(app, ["verify", "--job", manifest.job_id])
    assert verified.exit_code == 1
    assert "outside the configured library" in verified.output
    assert "Rebuild the job or migrate" in verified.output


def test_cli_verify_rejects_empty_bundle_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _make_job(tmp_path, "empty-bundle-job")
    manifest.status = "built"
    manifest_path = tmp_path / ".work" / "jobs" / manifest.job_id / "manifest.json"
    atomic_write_json(manifest_path, manifest.model_dump(mode="json"))
    monkeypatch.setattr(cli, "_root", lambda: tmp_path.resolve())

    result = CliRunner().invoke(app, ["verify", "--job", manifest.job_id])
    assert result.exit_code == 1
    assert "no bundle_path" in result.output


def test_serve_reports_invalid_local_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pdf2html.local.toml").write_text("knowledge_root = [", encoding="utf-8")
    monkeypatch.setattr(cli, "_root", lambda: tmp_path.resolve())

    result = CliRunner().invoke(app, ["serve"])
    assert result.exit_code == 1
    assert "Invalid TOML" in result.output
