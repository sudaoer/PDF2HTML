from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass
from typing import Any

from .bundle import PaperBundle

ENGLISH_WORD = re.compile(r"[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)*")
HAN_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
HTML_TAG = re.compile(r"<[^>]+>")
MARKDOWN_MARK = re.compile(r"[*_`#>|]+")
WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class SearchRecord:
    paper_id: str
    revision: str
    kind: str
    block_id: str | None
    segment_id: str | None
    page: int | None
    title: str
    heading: str
    metadata: str
    body: str
    source_text: str
    target_text: str


def search_tokens(value: str) -> list[str]:
    tokens = [
        f"e{word.casefold().replace('-', '').replace(chr(39), '')}"
        for word in ENGLISH_WORD.findall(value)
    ]
    for run in HAN_RUN.findall(value):
        tokens.extend(f"u{character}" for character in run)
        tokens.extend(f"b{run[index:index + 2]}" for index in range(len(run) - 1))
    return tokens


def token_stream(value: str) -> str:
    return " ".join(search_tokens(value))


def plain_text(value: str) -> str:
    text = HTML_TAG.sub(" ", value)
    text = MARKDOWN_MARK.sub(" ", text)
    return WHITESPACE.sub(" ", text).strip()


def _excerpt(value: str, query: str, maximum: int = 160) -> str:
    text = plain_text(value)
    if len(text) <= maximum:
        return text
    candidates = [query, *ENGLISH_WORD.findall(query), *HAN_RUN.findall(query)]
    positions = [text.casefold().find(item.casefold()) for item in candidates if item]
    matches = [position for position in positions if position >= 0]
    center = min(matches) if matches else 0
    start = max(0, center - maximum // 3)
    end = min(len(text), start + maximum)
    start = max(0, end - maximum)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def _bundle_records(bundle: PaperBundle) -> list[SearchRecord]:
    document = bundle.document
    translations = {entry.id: entry.target for entry in bundle.translations}
    metadata = document.metadata
    title_source = metadata.title
    title_target = translations.get("meta.title", title_source)
    metadata_source = " ".join(
        [
            *metadata.creators,
            metadata.date,
            metadata.publication,
            metadata.doi,
            *metadata.tags,
        ]
    )
    abstract_target = translations.get("meta.abstract", "")
    records = [
        SearchRecord(
            paper_id=bundle.paper_id,
            revision=bundle.bundle_revision,
            kind="paper",
            block_id=None,
            segment_id="meta.title",
            page=None,
            title=f"{title_source} {title_target}",
            heading="",
            metadata=metadata_source,
            body=f"{metadata.abstract} {abstract_target}",
            source_text=f"{title_source}. {metadata.abstract}".strip(),
            target_text=f"{title_target}. {abstract_target}".strip(),
        )
    ]
    for block in document.blocks:
        source_parts = [block.source, block.caption_source, block.footnote_source]
        target_parts = [
            translations.get(block.segment_id, ""),
            translations.get(block.caption_segment_id, ""),
            translations.get(block.footnote_segment_id, ""),
        ]
        source = plain_text(" ".join(part for part in source_parts if part))
        target = plain_text(" ".join(part for part in target_parts if part))
        if not source and not target:
            continue
        is_heading = block.kind == "heading"
        records.append(
            SearchRecord(
                paper_id=bundle.paper_id,
                revision=bundle.bundle_revision,
                kind=block.kind,
                block_id=block.id,
                segment_id=block.segment_id or block.caption_segment_id or None,
                page=block.page + 1,
                title="",
                heading=f"{source} {target}" if is_heading else "",
                metadata="",
                body="" if is_heading else f"{source} {target}",
                source_text=source,
                target_text=target,
            )
        )
    for note in [*document.front_matter_notes, *document.endnotes]:
        source = plain_text(note.url or note.source)
        target = plain_text(translations.get(note.segment_id, note.url))
        records.append(
            SearchRecord(
                paper_id=bundle.paper_id,
                revision=bundle.bundle_revision,
                kind="note",
                block_id=note.id,
                segment_id=note.segment_id or None,
                page=note.page + 1,
                title="",
                heading="",
                metadata="",
                body=f"{source} {target}",
                source_text=source,
                target_text=target,
            )
        )
    return records


class SearchIndex:
    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.revisions: dict[str, str] = {}
        self.titles: dict[str, tuple[str, str]] = {}
        self.connection.execute(
            """
            CREATE VIRTUAL TABLE search_fts USING fts5(
                paper_id UNINDEXED,
                revision UNINDEXED,
                kind UNINDEXED,
                block_id UNINDEXED,
                segment_id UNINDEXED,
                page UNINDEXED,
                title,
                heading,
                metadata,
                body,
                source_text UNINDEXED,
                target_text UNINDEXED,
                tokenize = 'unicode61'
            )
            """
        )

    def sync(self, bundles: dict[str, PaperBundle]) -> None:
        with self.lock:
            self._sync(bundles)

    def _sync(self, bundles: dict[str, PaperBundle]) -> None:
        for paper_id in set(self.revisions) - set(bundles):
            self.connection.execute("DELETE FROM search_fts WHERE paper_id = ?", (paper_id,))
            self.revisions.pop(paper_id, None)
            self.titles.pop(paper_id, None)
        for paper_id, bundle in bundles.items():
            if self.revisions.get(paper_id) == bundle.bundle_revision:
                continue
            self.connection.execute("DELETE FROM search_fts WHERE paper_id = ?", (paper_id,))
            translations = {entry.id: entry.target for entry in bundle.translations}
            self.titles[paper_id] = (
                bundle.document.metadata.title,
                translations.get("meta.title", bundle.document.metadata.title),
            )
            for record in _bundle_records(bundle):
                self.connection.execute(
                    """
                    INSERT INTO search_fts(
                        paper_id, revision, kind, block_id, segment_id, page,
                        title, heading, metadata, body, source_text, target_text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.paper_id,
                        record.revision,
                        record.kind,
                        record.block_id,
                        record.segment_id,
                        record.page,
                        token_stream(record.title),
                        token_stream(record.heading),
                        token_stream(record.metadata),
                        token_stream(record.body),
                        record.source_text,
                        record.target_text,
                    ),
                )
            self.revisions[paper_id] = bundle.bundle_revision
        self.connection.commit()

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        with self.lock:
            return self._search(query, limit)

    def _search(self, query: str, limit: int) -> list[dict[str, Any]]:
        tokens = search_tokens(query)
        if not tokens:
            return []
        match = " AND ".join(f'"{token}"' for token in dict.fromkeys(tokens))
        rows = self.connection.execute(
            """
            SELECT paper_id, revision, kind, block_id, segment_id, page,
                   source_text, target_text,
                   bm25(search_fts, 0, 0, 0, 0, 0, 0, 12, 8, 5, 1, 0, 0) AS rank
            FROM search_fts
            WHERE search_fts MATCH ?
            ORDER BY rank, paper_id, COALESCE(page, 0), COALESCE(block_id, '')
            LIMIT ?
            """,
            (match, limit),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            title, title_zh = self.titles[str(row["paper_id"])]
            results.append(
                {
                    "paper_id": row["paper_id"],
                    "bundle_revision": row["revision"],
                    "kind": row["kind"],
                    "block_id": row["block_id"],
                    "segment_id": row["segment_id"],
                    "page": row["page"],
                    "title": title,
                    "title_zh": title_zh,
                    "source_excerpt": _excerpt(str(row["source_text"]), query),
                    "target_excerpt": _excerpt(str(row["target_text"]), query),
                    "score": -float(row["rank"]),
                }
            )
        return results
