"""Minimal RAG over a directory of text files — no vector DB, no local
embedding model. Chunks are embedded via the Gemini Embedding API and kept
in memory; search is plain cosine similarity over that list. Proportionate
to "a handful of dropped-in documents", not a production knowledge base —
see ../CLAUDE.md for what a real one (Structured Facts Store + reranking)
would need on top of this.

Embeddings are cached to disk (`knowledge/.embeddings_cache.json`, keyed by
content hash) so restarting the server doesn't re-embed unchanged files —
only new or edited ones cost an API call.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

_LOG = logging.getLogger("knowledge")

_SUPPORTED_SUFFIXES = (".txt", ".md")
_CACHE_FILENAME = ".embeddings_cache.json"
# Paragraph-ish chunking: split on blank lines, then pack paragraphs back
# together up to this size so a chunk is neither one sentence nor a whole
# file. No tokenizer — character count is a good enough proxy here.
_CHUNK_CHARS = 1200


@dataclass(frozen=True, slots=True)
class KnowledgeChunk:
    source: str  # filename, for citing in the tool result
    text: str
    embedding: list[float]


@dataclass(frozen=True, slots=True)
class SearchResult:
    source: str
    text: str
    score: float


def _chunk_text(text: str) -> list[str]:
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) > _CHUNK_CHARS and current:
            chunks.append(current)
            current = para
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


@dataclass(slots=True)
class KnowledgeBase:
    """Embeds and searches every .txt/.md file in `directory`.

    Call `ensure_fresh()` before `search()` — it re-scans the directory and
    only pays for embedding calls on chunks whose content actually changed
    (or is new), using the on-disk cache. Safe to call on every tool
    invocation; a no-op re-scan when nothing changed is cheap.
    """

    directory: Path
    api_key: str
    embedding_model: str
    timeout_s: float = 20.0
    _chunks: list[KnowledgeChunk] = field(default_factory=list, init=False, repr=False)
    _cache: dict[str, list[float]] = field(default_factory=dict, init=False, repr=False)
    _cache_loaded: bool = field(default=False, init=False, repr=False)
    _indexed_signature: tuple[tuple[str, float], ...] = field(
        default=(), init=False, repr=False
    )

    def _cache_path(self) -> Path:
        return self.directory / _CACHE_FILENAME

    def _load_cache(self) -> None:
        if self._cache_loaded:
            return
        self._cache_loaded = True
        path = self._cache_path()
        if not path.exists():
            return
        try:
            self._cache = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _LOG.warning("knowledge.cache_load_failed: %s", exc)

    def _save_cache(self) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._cache_path().write_text(json.dumps(self._cache), encoding="utf-8")
        except OSError as exc:
            _LOG.warning("knowledge.cache_save_failed: %s", exc)

    def _current_files(self) -> list[Path]:
        if not self.directory.is_dir():
            return []
        return sorted(
            p
            for p in self.directory.iterdir()
            if p.is_file()
            and p.suffix.lower() in _SUPPORTED_SUFFIXES
            and p.name.lower() != "readme.md"  # this dir's own usage note, not content
        )

    async def ensure_fresh(self) -> None:
        self._load_cache()
        files = self._current_files()
        signature = tuple((str(p), p.stat().st_mtime) for p in files)
        if signature == self._indexed_signature:
            return

        all_chunk_texts: list[tuple[str, str]] = []  # (source, chunk_text)
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                _LOG.warning("knowledge.read_failed: %s: %s", path, exc)
                continue
            for chunk in _chunk_text(text):
                all_chunk_texts.append((path.name, chunk))

        to_embed = [
            (source, text) for source, text in all_chunk_texts if _hash(text) not in self._cache
        ]
        if to_embed:
            _LOG.info("knowledge.embedding %d new/changed chunk(s)", len(to_embed))
            embeddings = await self._embed_batch([text for _, text in to_embed])
            for (_, text), vector in zip(to_embed, embeddings, strict=True):
                self._cache[_hash(text)] = vector
            self._save_cache()

        self._chunks = [
            KnowledgeChunk(source=source, text=text, embedding=self._cache[_hash(text)])
            for source, text in all_chunk_texts
            if _hash(text) in self._cache
        ]
        self._indexed_signature = signature
        _LOG.info(
            "knowledge.indexed %d chunk(s) from %d file(s)", len(self._chunks), len(files)
        )

    async def search(self, query: str, *, top_k: int) -> list[SearchResult]:
        if not self._chunks:
            return []
        query_vec = (await self._embed_batch([query]))[0]
        scored = [
            SearchResult(source=c.source, text=c.text, score=_cosine(query_vec, c.embedding))
            for c in self._chunks
        ]
        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:top_k]

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/"
            f"{self.embedding_model}:batchEmbedContents"
        )
        requests = [
            {"model": self.embedding_model, "content": {"parts": [{"text": t}]}} for t in texts
        ]
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            response = await client.post(
                url, params={"key": self.api_key}, json={"requests": requests}
            )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return [item["values"] for item in payload["embeddings"]]


def _build_default() -> KnowledgeBase:
    # Deferred import: app.config already imports nothing from here, but
    # importing it at module top would be a needless coupling for a module
    # that's otherwise self-contained and easy to reuse/test standalone.
    from app.config import settings

    return KnowledgeBase(
        directory=Path(settings.knowledge_dir),
        api_key=settings.resolved_api_key(),
        embedding_model=settings.knowledge_embedding_model,
    )


# Module-level singleton (mirrors app/config.py's `settings`) so the
# in-memory chunk index and embedding cache survive across WS sessions in
# app/main.py instead of being rebuilt — and re-embedded — per connection.
knowledge_base = _build_default()
