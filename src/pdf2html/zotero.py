from __future__ import annotations

import platform
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlparse

import httpx

from .models import PaperMetadata


class ZoteroError(RuntimeError):
    """A clear, actionable Zotero integration error."""


def _is_wsl() -> bool:
    return "microsoft" in platform.release().lower()


@dataclass(frozen=True)
class Library:
    prefix: str
    name: str


@dataclass(frozen=True)
class SelectedPaper:
    library: Library
    item: dict[str, Any]
    attachment: dict[str, Any]
    pdf_path: Path
    metadata: PaperMetadata


class ZoteroClient:
    def __init__(self, base_url: str = "http://localhost:23119/api") -> None:
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(
            base_url=self.base_url,
            headers={
                "Zotero-Allowed-Request": "1",
                "Zotero-API-Version": "3",
                "User-Agent": "PDF2HTML/0.1",
            },
            timeout=20.0,
        )
        self.server_id = ""

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> ZoteroClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def preflight(self) -> dict[str, str]:
        try:
            response = self.client.get("/")
        except httpx.HTTPError as exc:
            raise ZoteroError(
                "Cannot reach Zotero Local API. Start Zotero and enable local API access."
            ) from exc
        if response.status_code == 403:
            raise ZoteroError(
                "Zotero Local API is disabled. Enable 'Allow other applications on this "
                "computer to communicate with Zotero' in Settings > Advanced."
            )
        response.raise_for_status()
        self.server_id = response.headers.get("Zotero-Server-ID", "")
        return {
            "api_version": response.headers.get("Zotero-API-Version", "3"),
            "schema_version": response.headers.get("Zotero-Schema-Version", ""),
            "server_id": self.server_id,
        }

    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        response = self.client.get(path, params=params)
        response.raise_for_status()
        if not self.server_id:
            self.server_id = response.headers.get("Zotero-Server-ID", "")
        return response.json()

    def libraries(self) -> list[Library]:
        libraries = [Library("users/0", "My Library")]
        try:
            raw_groups = self._get("/users/0/groups")
        except httpx.HTTPStatusError:
            return libraries
        if isinstance(raw_groups, list):
            for group in raw_groups:
                if not isinstance(group, dict):
                    continue
                group_id = group.get("id") or group.get("data", {}).get("id")
                name = group.get("data", {}).get("name") or group.get("name")
                if group_id:
                    libraries.append(Library(f"groups/{group_id}", str(name or group_id)))
        return libraries

    def select_item(self, selector: str) -> list[tuple[Library, dict[str, Any]]]:
        selector = selector.strip()
        key = self._extract_item_key(selector)
        matches: list[tuple[Library, dict[str, Any]]] = []
        for library in self.libraries():
            if key:
                try:
                    raw = self._get(f"/{library.prefix}/items/{key}")
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 404:
                        continue
                    raise
                if isinstance(raw, dict):
                    matches.append((library, cast(dict[str, Any], raw)))
                continue
            raw_items = self._get(
                f"/{library.prefix}/items/top",
                {"q": selector, "qmode": "titleCreatorYear", "format": "json"},
            )
            if not isinstance(raw_items, list):
                continue
            candidates = [item for item in raw_items if isinstance(item, dict)]
            exact = [
                item
                for item in candidates
                if str(item.get("data", {}).get("title", "")).casefold()
                == selector.casefold()
            ]
            chosen = exact or candidates
            matches.extend((library, cast(dict[str, Any], item)) for item in chosen)
        return matches

    def select_collection(self, selector: str) -> list[tuple[Library, dict[str, Any]]]:
        results: list[tuple[Library, dict[str, Any]]] = []
        for library in self.libraries():
            raw = self._get(f"/{library.prefix}/collections")
            if not isinstance(raw, list):
                continue
            matched = [
                collection
                for collection in raw
                if isinstance(collection, dict)
                and (
                    str(collection.get("key", "")).casefold() == selector.casefold()
                    or str(collection.get("data", {}).get("name", "")).casefold()
                    == selector.casefold()
                )
            ]
            for collection in matched:
                key = str(collection.get("key", ""))
                items = self._get(f"/{library.prefix}/collections/{key}/items/top")
                if isinstance(items, list):
                    results.extend(
                        (library, cast(dict[str, Any], item))
                        for item in items
                        if isinstance(item, dict)
                    )
        return results

    def select_tag(self, tag: str) -> list[tuple[Library, dict[str, Any]]]:
        results: list[tuple[Library, dict[str, Any]]] = []
        for library in self.libraries():
            items = self._get(f"/{library.prefix}/items/top", {"tag": tag, "format": "json"})
            if isinstance(items, list):
                results.extend(
                    (library, cast(dict[str, Any], item))
                    for item in items
                    if isinstance(item, dict)
                )
        return results

    def resolve_paper(self, library: Library, selected: dict[str, Any]) -> SelectedPaper:
        data = self._data(selected)
        if data.get("itemType") == "attachment":
            parent_key = str(data.get("parentItem", ""))
            if not parent_key:
                raise ZoteroError(
                    f"Attachment {selected.get('key')} has no parent bibliographic item."
                )
            parent = self._get(f"/{library.prefix}/items/{parent_key}")
            if not isinstance(parent, dict):
                raise ZoteroError(f"Parent item {parent_key} could not be read.")
            attachment = selected
            item = cast(dict[str, Any], parent)
        else:
            item = selected
            item_key = str(item.get("key", ""))
            raw_children = self._get(f"/{library.prefix}/items/{item_key}/children")
            children = raw_children if isinstance(raw_children, list) else []
            pdfs = [
                cast(dict[str, Any], child)
                for child in children
                if isinstance(child, dict)
                and child.get("data", {}).get("itemType") == "attachment"
                and child.get("data", {}).get("contentType") == "application/pdf"
            ]
            if not pdfs:
                raise ZoteroError(f"Zotero item {item_key} has no PDF attachment.")
            if len(pdfs) > 1:
                preferred = [
                    attachment
                    for attachment in pdfs
                    if str(attachment.get("data", {}).get("title", "")).casefold()
                    in {"full text pdf", "pdf"}
                ]
                if len(preferred) != 1:
                    choices = ", ".join(
                        f"{entry.get('key')} ({entry.get('data', {}).get('title', '')})"
                        for entry in pdfs
                    )
                    raise ZoteroError(
                        f"Zotero item {item_key} has multiple PDF attachments: {choices}"
                    )
                attachment = preferred[0]
            else:
                attachment = pdfs[0]
        attachment_key = str(attachment.get("key", ""))
        url_response = self.client.get(
            f"/{library.prefix}/items/{attachment_key}/file/view/url"
        )
        if url_response.status_code == 404:
            raise ZoteroError(
                f"PDF attachment {attachment_key} is not available locally. "
                "Open or sync it in Zotero."
            )
        url_response.raise_for_status()
        pdf_path = self._file_url_to_path(url_response.text.strip())
        if not pdf_path.is_file():
            hint = (
                " Check that the Windows drive is mounted under /mnt."
                if _is_wsl() and pdf_path.is_relative_to("/mnt")
                else ""
            )
            raise ZoteroError(
                f"PDF attachment {attachment_key} points to a missing local file: {pdf_path}.{hint}"
            )
        return SelectedPaper(
            library=library,
            item=item,
            attachment=attachment,
            pdf_path=pdf_path,
            metadata=self._metadata(item),
        )

    @staticmethod
    def _extract_item_key(selector: str) -> str:
        if re.fullmatch(r"[23456789A-HJ-NP-Z]{8}", selector, re.IGNORECASE):
            return selector.upper()
        match = re.search(r"/items/([23456789A-HJ-NP-Z]{8})(?:[/?#]|$)", selector, re.I)
        return match.group(1).upper() if match else ""

    @staticmethod
    def _file_url_to_path(value: str) -> Path:
        parsed = urlparse(value)
        if parsed.scheme != "file":
            raise ZoteroError(f"Zotero returned an unexpected attachment URL: {value}")
        decoded = unquote(parsed.path)
        if re.match(r"^/[A-Za-z]:/", decoded):
            if _is_wsl():
                return Path("/mnt") / decoded[1].lower() / decoded[4:]
            decoded = decoded[1:]
        return Path(decoded)

    @staticmethod
    def _data(item: dict[str, Any]) -> dict[str, Any]:
        raw = item.get("data", {})
        return cast(dict[str, Any], raw) if isinstance(raw, dict) else {}

    @classmethod
    def _metadata(cls, item: dict[str, Any]) -> PaperMetadata:
        data = cls._data(item)
        creators: list[str] = []
        for creator in data.get("creators", []):
            if not isinstance(creator, dict):
                continue
            name = str(creator.get("name", "")).strip()
            if not name:
                name = " ".join(
                    part
                    for part in (
                        str(creator.get("firstName", "")).strip(),
                        str(creator.get("lastName", "")).strip(),
                    )
                    if part
                )
            if name:
                creators.append(name)
        tags = [
            str(tag.get("tag", ""))
            for tag in data.get("tags", [])
            if isinstance(tag, dict) and tag.get("tag")
        ]
        return PaperMetadata(
            title=str(data.get("title", "Untitled")),
            creators=creators,
            date=str(data.get("date", "")),
            publication=str(
                data.get("publicationTitle")
                or data.get("conferenceName")
                or data.get("bookTitle")
                or ""
            ),
            doi=str(data.get("DOI", "")),
            url=str(data.get("url", "")),
            abstract=str(data.get("abstractNote", "")),
            tags=tags,
        )
