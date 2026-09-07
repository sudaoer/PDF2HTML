from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any


class ConfigurationError(ValueError):
    """The local PDF2HTML configuration is invalid."""


def _configured_path(value: Any, *, source: str, workspace: Path) -> Path:
    if not isinstance(value, str):
        raise ConfigurationError(f"{source} knowledge_root must be a string path.")
    if not value.strip():
        raise ConfigurationError(f"{source} knowledge_root must not be empty.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = workspace / path
    return path.resolve()


def _local_config_path(workspace: Path) -> Path:
    return workspace / "pdf2html.local.toml"


def _read_local_knowledge_root(path: Path, workspace: Path) -> Path:
    try:
        with path.open("rb") as stream:
            payload = tomllib.load(stream)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"Invalid TOML in {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigurationError(f"Cannot read local configuration {path}: {exc}") from exc

    unknown = sorted(set(payload) - {"knowledge_root"})
    if unknown:
        names = ", ".join(unknown)
        raise ConfigurationError(f"Unknown field(s) in {path}: {names}.")
    if "knowledge_root" not in payload:
        raise ConfigurationError(f"{path} must define top-level knowledge_root.")
    return _configured_path(payload["knowledge_root"], source=str(path), workspace=workspace)


def resolve_knowledge_root(workspace: Path) -> Path:
    """Resolve the external learning repository root for a workspace.

    ``PDF2HTML_KNOWLEDGE_ROOT`` takes precedence over the optional local TOML
    file.  With neither configured, the historical ``workspace/output`` path
    remains the default.
    """

    workspace = workspace.expanduser().resolve()
    if "PDF2HTML_KNOWLEDGE_ROOT" in os.environ:
        return _configured_path(
            os.environ["PDF2HTML_KNOWLEDGE_ROOT"],
            source="PDF2HTML_KNOWLEDGE_ROOT",
            workspace=workspace,
        )
    config_path = _local_config_path(workspace)
    if config_path.exists() or config_path.is_symlink():
        return _read_local_knowledge_root(config_path, workspace)
    return (workspace / "output").resolve()


def resolve_library_root(workspace: Path) -> Path:
    """Resolve the paper library below the configured knowledge repository."""

    return resolve_knowledge_root(workspace) / "library"
