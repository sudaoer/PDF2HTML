from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from .bundle import PaperBundle, bundle_shelf_record
from .bundle_verify import BundleVerificationError, verify_bundle
from .util import canonical_json_bytes, hash_file, read_json

PAPER_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?$")


class ServerError(RuntimeError):
    """The local bundle service cannot start safely."""


@dataclass(frozen=True)
class BundleEntry:
    bundle: PaperBundle
    path: Path
    signature: tuple[int, int]


@dataclass(frozen=True)
class BrokenBundle:
    paper_id: str
    message: str
    signature: tuple[int, int]
    digest: str


def _quoted_etag(value: str) -> str:
    return f'"{value.replace(chr(34), "")}"'


def _not_modified(request: Request, etag: str) -> bool:
    candidates = {item.strip() for item in request.headers.get("if-none-match", "").split(",")}
    return "*" in candidates or etag in candidates


def _cache_headers(etag: str) -> dict[str, str]:
    return {"Cache-Control": "no-cache", "ETag": etag, "X-Content-Type-Options": "nosniff"}


def _bytes_response(
    request: Request,
    content: bytes,
    *,
    media_type: str,
    digest: str,
) -> Response:
    etag = _quoted_etag(digest)
    headers = _cache_headers(etag)
    if _not_modified(request, etag):
        return Response(status_code=304, headers=headers)
    return Response(content=content, media_type=media_type, headers=headers)


class BundleRegistry:
    def __init__(self, library_root: Path) -> None:
        self.library_root = library_root.resolve()
        self.papers_root = self.library_root / "papers"
        self.entries: dict[str, BundleEntry] = {}
        self.broken: dict[str, BrokenBundle] = {}
        self.library_revision = "sha256:" + "0" * 64

    @staticmethod
    def _signature(path: Path) -> tuple[int, int]:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size

    def refresh(self) -> None:
        discovered: dict[str, tuple[Path, tuple[int, int]]] = {}
        if self.papers_root.is_dir():
            for paper_dir in self.papers_root.iterdir():
                if not paper_dir.is_dir() or not PAPER_ID_PATTERN.fullmatch(paper_dir.name):
                    continue
                manifest = paper_dir / "paper.json"
                if manifest.is_file() and not manifest.is_symlink():
                    discovered[paper_dir.name] = (manifest, self._signature(manifest))

        for paper_id in set(self.entries) | set(self.broken):
            if paper_id not in discovered:
                self.entries.pop(paper_id, None)
                self.broken.pop(paper_id, None)
        for paper_id, (path, signature) in discovered.items():
            current = self.entries.get(paper_id)
            broken = self.broken.get(paper_id)
            if (current and current.signature == signature) or (
                broken and broken.signature == signature
            ):
                continue
            try:
                verify_bundle(path)
                bundle = PaperBundle.model_validate(read_json(path))
                if bundle.paper_id != paper_id:
                    raise BundleVerificationError("Paper directory and paper.json ID differ.")
                self.entries[paper_id] = BundleEntry(bundle=bundle, path=path, signature=signature)
                self.broken.pop(paper_id, None)
            except (BundleVerificationError, OSError, ValueError) as exc:
                digest = hash_file(path) if path.is_file() else "missing"
                self.broken[paper_id] = BrokenBundle(
                    paper_id=paper_id,
                    message=str(exc),
                    signature=signature,
                    digest=digest,
                )
                self.entries.pop(paper_id, None)
        revision_payload = {
            "papers": sorted(
                (paper_id, entry.bundle.bundle_revision)
                for paper_id, entry in self.entries.items()
            ),
            "errors": sorted(
                (paper_id, item.digest, item.message) for paper_id, item in self.broken.items()
            ),
        }
        import hashlib

        digest = hashlib.sha256(canonical_json_bytes(revision_payload)).hexdigest()
        self.library_revision = f"sha256:{digest}"

    def shelf_payload(self) -> dict[str, Any]:
        records = [
            bundle_shelf_record(entry.bundle.model_dump(mode="json"))
            for entry in self.entries.values()
        ]
        records.sort(
            key=lambda item: (str(item.get("date", "")), str(item.get("title", ""))),
            reverse=True,
        )
        for record in records:
            paper_id = str(record["paper_id"])
            record["href"] = f"/papers/{paper_id}/"
            record["bundle_revision"] = self.entries[paper_id].bundle.bundle_revision
        return {
            "schema_version": 1,
            "library_revision": self.library_revision,
            "papers": records,
            "errors": [
                {"paper_id": item.paper_id, "code": "invalid_bundle", "message": item.message}
                for item in sorted(self.broken.values(), key=lambda value: value.paper_id)
            ],
        }

    def search_payload(self, query: str, limit: int) -> dict[str, Any]:
        folded = query.casefold()
        results: list[dict[str, Any]] = []
        for paper in self.shelf_payload()["papers"]:
            searchable = " ".join(
                [
                    str(paper.get("title", "")),
                    str(paper.get("title_zh", "")),
                    " ".join(str(item) for item in paper.get("creators", [])),
                    str(paper.get("date", "")),
                    " ".join(str(item) for item in paper.get("tags", [])),
                ]
            ).casefold()
            if folded in searchable:
                results.append(
                    {
                        "paper_id": paper["paper_id"],
                        "bundle_revision": paper["bundle_revision"],
                        "kind": "paper",
                        "block_id": None,
                        "segment_id": None,
                        "page": None,
                        "title": paper["title"],
                        "title_zh": paper["title_zh"],
                        "source_excerpt": paper["title"],
                        "target_excerpt": paper["title_zh"],
                        "score": 0.0,
                    }
                )
        return {
            "query": query,
            "library_revision": self.library_revision,
            "results": results[:limit],
        }


def _vite_assets(web_root: Path) -> dict[str, Path]:
    manifest_path = web_root / ".vite" / "manifest.json"
    if not manifest_path.is_file():
        raise ServerError(
            f"Vue assets are missing under {web_root}; run `pnpm build` before serving."
        )
    payload = read_json(manifest_path)
    if not isinstance(payload, dict):
        raise ServerError("The Vite asset manifest is malformed.")
    names: set[str] = set()
    for raw in payload.values():
        if not isinstance(raw, dict):
            continue
        for key in ("file",):
            value = raw.get(key)
            if isinstance(value, str):
                names.add(value)
        for key in ("css", "assets"):
            values = raw.get(key, [])
            if isinstance(values, list):
                names.update(value for value in values if isinstance(value, str))
    assets: dict[str, Path] = {}
    root_resolved = web_root.resolve()
    for name in names:
        if not name.startswith("assets/") or "\\" in name or ".." in Path(name).parts:
            raise ServerError(f"Unsafe Vite asset entry: {name}")
        candidate = (web_root / name).resolve()
        try:
            candidate.relative_to(root_resolved)
        except ValueError as exc:
            raise ServerError(f"Vite asset escapes its root: {name}") from exc
        if not candidate.is_file() or candidate.is_symlink():
            raise ServerError(f"Vite asset is missing: {name}")
        assets[name.removeprefix("assets/")] = candidate
    return assets


def create_app(
    *,
    library_root: Path,
    web_root: Path | None = None,
) -> FastAPI:
    frontend = (web_root or (Path(__file__).parent / "web" / "dist")).resolve()
    shell = frontend / "index.html"
    if not shell.is_file():
        raise ServerError(f"Vue application shell is missing: {shell}")
    assets = _vite_assets(frontend)
    registry = BundleRegistry(library_root)
    app = FastAPI(title="PDF2HTML", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.registry = registry

    def refresh() -> BundleRegistry:
        registry.refresh()
        return registry

    def broken_response(paper_id: str) -> JSONResponse | None:
        broken = registry.broken.get(paper_id)
        if broken is None:
            return None
        return JSONResponse(
            status_code=422,
            content={"code": "invalid_bundle", "paper_id": paper_id, "message": broken.message},
        )

    @app.get("/")
    def shelf_shell(request: Request) -> Response:
        return _bytes_response(
            request,
            shell.read_bytes(),
            media_type="text/html; charset=utf-8",
            digest=hash_file(shell),
        )

    @app.get("/papers/{paper_id}", status_code=307)
    def paper_redirect(paper_id: str) -> RedirectResponse:
        return RedirectResponse(f"/papers/{paper_id}/", status_code=307)

    @app.get("/papers/{paper_id}/")
    def paper_shell(paper_id: str, request: Request) -> Response:
        state = refresh()
        if paper_id not in state.entries and paper_id not in state.broken:
            return JSONResponse(status_code=404, content={"code": "paper_not_found"})
        return _bytes_response(
            request,
            shell.read_bytes(),
            media_type="text/html; charset=utf-8",
            digest=hash_file(shell),
        )

    @app.get("/api/shelf")
    def shelf(request: Request) -> Response:
        state = refresh()
        content = canonical_json_bytes(state.shelf_payload())
        return _bytes_response(
            request,
            content,
            media_type="application/json",
            digest=state.library_revision,
        )

    @app.get("/api/search")
    def search(request: Request, q: str, limit: int = 20) -> Response:
        if not q.strip() or len(q) > 200 or not 1 <= limit <= 50:
            return JSONResponse(status_code=422, content={"code": "invalid_search"})
        state = refresh()
        payload = state.search_payload(q.strip(), limit)
        seed = canonical_json_bytes((state.library_revision, q.strip().casefold(), limit))
        import hashlib

        digest = "sha256:" + hashlib.sha256(seed).hexdigest()
        return _bytes_response(
            request,
            canonical_json_bytes(payload),
            media_type="application/json",
            digest=digest,
        )

    @app.get("/papers/{paper_id}/paper.json")
    def paper_json(paper_id: str, request: Request) -> Response:
        state = refresh()
        if response := broken_response(paper_id):
            return response
        entry = state.entries.get(paper_id)
        if entry is None:
            return JSONResponse(status_code=404, content={"code": "paper_not_found"})
        content = entry.path.read_bytes()
        return _bytes_response(
            request,
            content,
            media_type="application/json",
            digest=hash_file(entry.path),
        )

    @app.get("/papers/{paper_id}/{artifact_name}")
    def markdown_artifact(paper_id: str, artifact_name: str, request: Request) -> Response:
        state = refresh()
        if response := broken_response(paper_id):
            return response
        entry = state.entries.get(paper_id)
        if entry is None:
            return JSONResponse(status_code=404, content={"code": "paper_not_found"})
        artifact = next(
            (item for item in entry.bundle.artifacts if item.path == artifact_name),
            None,
        )
        if artifact is None:
            return JSONResponse(status_code=404, content={"code": "artifact_not_found"})
        return _bytes_response(
            request,
            (entry.path.parent / artifact.path).read_bytes(),
            media_type=artifact.media_type,
            digest=artifact.sha256,
        )

    @app.get("/papers/{paper_id}/media/{name}")
    def media(paper_id: str, name: str, request: Request) -> Response:
        state = refresh()
        if response := broken_response(paper_id):
            return response
        entry = state.entries.get(paper_id)
        if entry is None:
            return JSONResponse(status_code=404, content={"code": "paper_not_found"})
        path = f"media/{name}"
        item = next((candidate for candidate in entry.bundle.media if candidate.path == path), None)
        if item is None:
            return JSONResponse(status_code=404, content={"code": "media_not_found"})
        candidate = entry.path.parent / "media" / name
        return _bytes_response(
            request,
            candidate.read_bytes(),
            media_type=item.media_type,
            digest=item.sha256,
        )

    @app.get("/assets/{name:path}")
    def asset(name: str, request: Request) -> Response:
        candidate = assets.get(name)
        if candidate is None:
            return JSONResponse(status_code=404, content={"code": "asset_not_found"})
        etag = _quoted_etag(hash_file(candidate))
        headers = _cache_headers(etag)
        if _not_modified(request, etag):
            return Response(status_code=304, headers=headers)
        return FileResponse(candidate, headers=headers)

    return app
