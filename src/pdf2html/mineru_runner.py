from __future__ import annotations

import importlib.metadata
import os
import shutil
import subprocess
import sys
from pathlib import Path


class MinerUError(RuntimeError):
    """MinerU could not parse a document."""


def executable_path(name: str) -> Path | None:
    suffix = ".exe" if sys.platform == "win32" else ""
    candidates = [
        Path(sys.prefix) / "Scripts" / f"{name}{suffix}",
        Path(sys.prefix) / "bin" / name,
        Path(sys.prefix) / "Library" / "bin" / f"{name}{suffix}",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    found = shutil.which(name)
    return Path(found) if found else None


def mineru_version() -> str:
    try:
        return importlib.metadata.version("mineru")
    except importlib.metadata.PackageNotFoundError:
        return ""


def _windows_cuda_runtime(work_root: Path) -> Path | None:
    if sys.platform != "win32":
        return None
    torch_lib = Path(sys.prefix) / "Lib" / "site-packages" / "torch" / "lib"
    if not torch_lib.is_dir():
        return None
    cuda_root = work_root / "cuda-runtime"
    bin_dir = cuda_root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for source in torch_lib.glob("*.dll"):
        destination = bin_dir / source.name
        if destination.exists():
            continue
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)
    return cuda_root


def find_content_list(output_dir: Path) -> Path | None:
    v2 = sorted(output_dir.rglob("*_content_list_v2.json"))
    if v2:
        return v2[0]
    v1 = sorted(output_dir.rglob("*_content_list.json"))
    return v1[0] if v1 else None


def find_middle_json(output_dir: Path) -> Path | None:
    candidates = sorted(output_dir.rglob("*_middle.json"))
    return candidates[0] if candidates else None


def run_mineru(
    source_pdf: Path,
    output_dir: Path,
    *,
    backend: str,
    effort: str,
    force: bool = False,
) -> tuple[Path, list[str]]:
    existing = find_content_list(output_dir)
    if existing and not force:
        return existing, []
    executable = executable_path("mineru")
    if executable is None:
        raise MinerUError("MinerU executable is missing from the Pixi environment.")
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir.parent / "logs" / "mineru.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(executable),
        "-p",
        str(source_pdf),
        "-o",
        str(output_dir),
        "-b",
        backend,
        "--effort",
        effort,
    ]
    work_root = output_dir.parents[2]
    cache_root = work_root / "cache"
    huggingface_root = cache_root / "huggingface"
    modelscope_root = cache_root / "modelscope"
    huggingface_root.mkdir(parents=True, exist_ok=True)
    modelscope_root.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    cuda_root = _windows_cuda_runtime(work_root)
    environment.update(
        {
            "HF_HOME": str(huggingface_root),
            "HF_HUB_CACHE": str(huggingface_root / "hub"),
            "MODELSCOPE_CACHE": str(modelscope_root),
            "MINERU_TOOLS_CONFIG_JSON": str(work_root / "mineru.json"),
            "MINERU_LMDEPLOY_BACKEND": "turbomind",
        }
    )
    if sys.platform == "linux" and backend in {"hybrid-engine", "vlm-engine"}:
        environment["VLLM_USE_FLASHINFER_SAMPLER"] = "0"
    if cuda_root is not None:
        # lmdeploy's Windows wheel only checks CUDA_PATH/bin. The locked PyTorch
        # wheel already carries the matching CUDA 12.8 runtime DLLs, exposed
        # above via space-efficient hard links.
        environment["CUDA_PATH"] = str(cuda_root)
    with log_path.open("w", encoding="utf-8", newline="\n") as log:
        process = subprocess.Popen(  # noqa: S603
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
        )
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="")
            log.write(line)
        return_code = process.wait()
    if return_code != 0:
        raise MinerUError(
            f"MinerU exited with status {return_code}. See {log_path} for details."
        )
    content_list = find_content_list(output_dir)
    if content_list is None:
        raise MinerUError(
            f"MinerU completed but did not produce a content list under {output_dir}."
        )
    return content_list, command
