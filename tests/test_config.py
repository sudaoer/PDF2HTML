from __future__ import annotations

from pathlib import Path

import pytest

from pdf2html.config import ConfigurationError, resolve_knowledge_root, resolve_library_root


def test_knowledge_root_defaults_to_workspace_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PDF2HTML_KNOWLEDGE_ROOT", raising=False)

    assert resolve_knowledge_root(tmp_path) == (tmp_path / "output").resolve()
    assert resolve_library_root(tmp_path) == (tmp_path / "output" / "library").resolve()


def test_local_config_resolves_relative_and_absolute_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PDF2HTML_KNOWLEDGE_ROOT", raising=False)
    relative = tmp_path / "pdf2html.local.toml"
    relative.write_text('knowledge_root = "research/knowledge"\n', encoding="utf-8")
    assert resolve_knowledge_root(tmp_path) == (tmp_path / "research/knowledge").resolve()

    absolute = tmp_path / "absolute-knowledge"
    relative.write_text(f"knowledge_root = {absolute.as_posix()!r}\n", encoding="utf-8")
    assert resolve_knowledge_root(tmp_path) == absolute.resolve()


def test_environment_takes_precedence_over_invalid_local_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pdf2html.local.toml").write_text("knowledge_root = [", encoding="utf-8")
    environment_root = tmp_path / "from-environment"
    monkeypatch.setenv("PDF2HTML_KNOWLEDGE_ROOT", str(environment_root))

    assert resolve_knowledge_root(tmp_path) == environment_root.resolve()


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("knowledge_root = [", "Invalid TOML"),
        ('knowledge_root = ""', "must not be empty"),
        ("knowledge_root = 42", "must be a string"),
        ('knowledge_root = "ok"\nextra = true', "Unknown field"),
        ("other = true", "Unknown field"),
    ],
)
def test_invalid_local_config_fails_clearly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contents: str,
    message: str,
) -> None:
    monkeypatch.delenv("PDF2HTML_KNOWLEDGE_ROOT", raising=False)
    config = tmp_path / "pdf2html.local.toml"
    config.write_text(contents, encoding="utf-8")

    with pytest.raises(ConfigurationError, match=message):
        resolve_knowledge_root(tmp_path)


def test_empty_environment_path_fails_instead_of_using_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PDF2HTML_KNOWLEDGE_ROOT", " ")

    with pytest.raises(ConfigurationError, match="must not be empty"):
        resolve_knowledge_root(tmp_path)


def test_local_config_directory_is_not_treated_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PDF2HTML_KNOWLEDGE_ROOT", raising=False)
    (tmp_path / "pdf2html.local.toml").mkdir()

    with pytest.raises(ConfigurationError, match="Cannot read local configuration"):
        resolve_knowledge_root(tmp_path)
