from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from pdf2html.zotero import Library, ZoteroClient, ZoteroError


@pytest.mark.parametrize(
    ("is_wsl", "url", "expected"),
    [
        (True, "file:///C:/Zotero/a%20paper.pdf", Path("/mnt/c/Zotero/a paper.pdf")),
        (True, "file:///D:/Papers/%E6%B5%8B%E8%AF%95.pdf", Path("/mnt/d/Papers/测试.pdf")),
        (True, "file:///home/user/paper.pdf", Path("/home/user/paper.pdf")),
        (False, "file:///C:/Zotero/a%20paper.pdf", Path("C:/Zotero/a paper.pdf")),
    ],
)
def test_file_url_path_for_wsl_and_other_platforms(
    monkeypatch: pytest.MonkeyPatch, is_wsl: bool, url: str, expected: Path
) -> None:
    monkeypatch.setattr("pdf2html.zotero._is_wsl", lambda: is_wsl)

    assert ZoteroClient._file_url_to_path(url) == expected


def test_missing_wsl_attachment_reports_mount_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pdf2html.zotero._is_wsl", lambda: True)

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/children"):
            return httpx.Response(
                200,
                json=[
                    {
                        "key": "EFGH6789",
                        "data": {
                            "itemType": "attachment",
                            "contentType": "application/pdf",
                        },
                    }
                ],
            )
        return httpx.Response(200, text="file:///Z:/unmounted/paper.pdf")

    with ZoteroClient() as zotero:
        zotero.client.close()
        zotero.client = httpx.Client(
            base_url=zotero.base_url,
            transport=httpx.MockTransport(respond),
        )
        with pytest.raises(ZoteroError, match=r"/mnt/z/unmounted/paper\.pdf.*mounted under /mnt"):
            zotero.resolve_paper(
                Library("users/0", "My Library"),
                {"key": "ABCD2345", "data": {"title": "Paper"}},
            )
