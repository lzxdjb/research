"""
HotpotQA Memory Bank — persistent memory pool for RL with reflection.

Each memory entry stores a (problem, strategy) pair:
    {
        "id":           str,   # uuid
        "problem":      str,   # description of the situation/difficulty this strategy addresses
        "strategy":     str,   # the reusable strategy insight
        "score":        int,   # compatibility score: wins - losses
        "uses":         int,   # number of observed uses
        "wins":         int,   # count of positive paired deltas
        "losses":       int,   # count of negative paired deltas
        "mean_delta":   float, # EMA of reward delta when this memory was retrieved
        "embedding":    list,  # unit-norm embedding of `problem` (from embedding server)
        "created_step": int,
    }

Retrieval
---------
SearchMemory(query) → top-k memories selected from semantically similar candidates.
This replaces the old score-sorted get_top() injection: memories are now fetched
on-demand by the model via the SearchMemory tool, not blindly prepended.

Deduplication
-------------
Uses cosine similarity (dot product of unit-norm vectors) instead of Jaccard.
If sim(new_problem, existing_problem) >= dedup_threshold the entry is silently dropped.

Persistence
-----------
Mutations mark the pool dirty. Call flush() once per generation step to persist.
"""

import json
import logging
import os
import threading
from typing import Optional
from uuid import uuid4

import numpy as np
import requests

logger = logging.getLogger(__name__)


# ── Embedding client ──────────────────────────────────────────────────────────

def _embed(texts: list[str], server_url: str) -> list[list[float]] | None:
    """Call the embedding server and return unit-norm vectors, or None on failure."""
    try:
        resp = requests.post(
            f"{server_url}/embed",
            json={"texts": texts},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["embeddings"]
    except Exception as e:
        logger.warning(f"Embedding server error: {e}. Dedup/search disabled for this call.")
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    """Dot product of two unit-norm vectors (= cosine similarity)."""
    va = np.array(a, dtype=np.float32)
    vb = np.array(b, dtype=np.float32)
    return float(np.dot(va, vb))


# ── MemoryBank ────────────────────────────────────────────────────────────────

class MemoryBank:
    """Thread-safe in-process memory pool with (problem, strategy) pairs."""

    def __init__(
        self,
        save_dir: str = "/tmp/hotpot_memory",
        max_memories: int = 100,
        prune_threshold: int = -2,
        dedup_threshold: float = 0.85,
        embedding_server: str = "http://localhost:8765",
        prune_min_uses: int = 4,
        prune_mean_delta_threshold: float = 0.0,
        delta_ema_alpha: float = 0.2,
        search_min_similarity: float = 0.0,
        search_candidate_multiplier: int = 4,
    ):
        self.save_dir          = save_dir
        self.max_memories      = max_memories
        self.prune_threshold   = prune_threshold
        self.dedup_threshold   = dedup_threshold
        self.embedding_server  = embedding_server
        self.prune_min_uses    = max(1, int(prune_min_uses))
        self.prune_mean_delta_threshold = float(prune_mean_delta_threshold)
        self.delta_ema_alpha   = min(max(float(delta_ema_alpha), 0.0), 1.0)
        self.search_min_similarity = float(search_min_similarity)
        self.search_candidate_multiplier = max(1, int(search_candidate_multiplier))

        self._lock   = threading.Lock()
        self._pool: dict[str, dict] = {}
        self._step   = 0
        self._dirty  = False

        os.makedirs(save_dir, exist_ok=True)

        latest = _find_latest_checkpoint(save_dir)
        if latest:
            self._load_from_file_locked(latest)
            logger.info(f"MemoryBank auto-resumed from {latest}")

    # ── public API ────────────────────────────────────────────────────────────

    def add(self, problem: str, strategy: str) -> Optional[str]:
        """
        Add a new (problem, strategy) memory.
        Deduplicates by embedding cosine similarity on the problem field.
        Returns the new memory id, or None if deduplicated / empty.
        If the embedding server is unreachable, dedup is skipped and the entry is added.
        """
        problem  = problem.strip()
        strategy = strategy.strip()
        if not problem or not strategy:
            return None

        embeddings = _embed([problem], self.embedding_server)
        embedding  = embeddings[0] if embeddings is not None else None

        with self._lock:
            # Cosine dedup — skipped if embedding server is down
            if embedding is not None:
                for entry in self._pool.values():
                    existing_emb = entry.get("embedding")
                    if existing_emb and _cosine(embedding, existing_emb) >= self.dedup_threshold:
                        logger.debug("MemoryBank: skipped duplicate memory (cosine dedup)")
                        return None

            mid = uuid4().hex[:12]
            self._pool[mid] = {
                "id":           mid,
                "problem":      problem,
                "strategy":     strategy,
                "score":        0,
                "uses":         0,
                "wins":         0,
                "losses":       0,
                "mean_delta":   0.0,
                "embedding":    embedding,
                "created_step": self._step,
            }
            self._prune_and_cap()
            self._dirty = True
        return mid

    def vote(self, memory_id: str, delta: float) -> None:
        """
        Update a memory from an observed reward delta.

        Positive deltas count as wins, negative deltas count as losses, and zero
        means "used but inconclusive". New memories are only pruned after enough
        uses show a negative average effect, so score 0 remains unknown instead
        of bad.
        """
        with self._lock:
            if memory_id in self._pool:
                entry = self._pool[memory_id]
                self._ensure_stats(entry)
                delta = float(delta)
                entry["uses"] += 1
                if delta > 0:
                    entry["wins"] += 1
                elif delta < 0:
                    entry["losses"] += 1

                old_mean = float(entry.get("mean_delta", 0.0))
                alpha = self.delta_ema_alpha
                if entry["uses"] <= 1:
                    entry["mean_delta"] = delta
                else:
                    entry["mean_delta"] = (1.0 - alpha) * old_mean + alpha * delta
                entry["score"] = int(entry.get("wins", 0)) - int(entry.get("losses", 0))
                self._prune_and_cap()
                self._dirty = True

    def search(self, query: str, k: int = 3) -> list[dict]:
        """
        Return up to k memories from the most semantically similar candidates.

        Similarity is still the gate: candidates below search_min_similarity are
        dropped, then only the top similarity pool is considered. Within that
        pool, proven memories are preferred over unknown/negative ones.
        Falls back to score-ordered top-k if the embedding server is unreachable.
        """
        if not self._pool:
            return []

        with self._lock:
            entries = [self._with_stats(e) for e in self._pool.values()]

        embeddings = _embed([query], self.embedding_server)
        if embeddings is None:
            return self._rank_by_score(entries, k)

        query_emb = embeddings[0]
        scored = []
        for entry in entries:
            emb = entry.get("embedding")
            if not emb:
                continue
            sim = _cosine(query_emb, emb)
            if sim < self.search_min_similarity:
                continue
            scored.append((sim, entry))
        scored.sort(key=lambda x: x[0], reverse=True)
        candidate_count = max(k, k * self.search_candidate_multiplier)
        candidates = scored[:candidate_count]
        candidates.sort(
            key=lambda x: (
                self._entry_value(x[1]),
                x[1].get("score", 0),
                x[0],
                x[1].get("created_step", 0),
            ),
            reverse=True,
        )
        return [
            {**entry, "similarity": sim}
            for sim, entry in candidates[:k]
        ]

    def get_top(self, k: int = 3) -> list[dict]:
        """Return up to k highest-value memories by score, independent of query."""
        if not self._pool:
            return []
        with self._lock:
            entries = [self._with_stats(e) for e in self._pool.values()]
        return self._rank_by_score(entries, k)

    @staticmethod
    def _rank_by_score(entries: list[dict], k: int) -> list[dict]:
        return sorted(
            entries,
            key=lambda e: (
                MemoryBank._entry_value(e),
                e.get("score", 0),
                e.get("created_step", 0),
            ),
            reverse=True,
        )[:k]

    @staticmethod
    def _ensure_stats(entry: dict) -> None:
        entry["score"] = int(entry.get("score", 0))
        entry["uses"] = int(entry.get("uses", 0))
        entry["wins"] = int(entry.get("wins", 0))
        entry["losses"] = int(entry.get("losses", 0))
        entry["mean_delta"] = float(entry.get("mean_delta", 0.0))

    @classmethod
    def _with_stats(cls, entry: dict) -> dict:
        copied = dict(entry)
        cls._ensure_stats(copied)
        return copied

    @staticmethod
    def _entry_value(entry: dict) -> float:
        uses = int(entry.get("uses", 0))
        if uses > 0:
            return float(entry.get("mean_delta", 0.0))
        return float(entry.get("score", 0))

    def flush(self) -> bool:
        """Write pool to disk if dirty. Safe for concurrent calls."""
        with self._lock:
            if not self._dirty:
                return False
            self._dirty = False
            path = os.path.join(self.save_dir, f"memory_step_{self._step}.json")
            # Exclude embedding vectors from the saved JSON for readability;
            # they are recomputed on load.
            payload = {
                "step":     self._step,
                "memories": [
                    {k: v for k, v in e.items() if k != "embedding"}
                    for e in self._pool.values()
                ],
            }
            self._step += 1

        os.makedirs(self.save_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        logger.info(f"MemoryBank flushed {len(payload['memories'])} entries → {path}")
        return True

    def get(self, memory_id: str) -> Optional[dict]:
        with self._lock:
            return self._pool.get(memory_id)

    def load_from_file(self, path: str) -> None:
        with self._lock:
            self._load_from_file_locked(path)

    # ── internals ─────────────────────────────────────────────────────────────

    def _load_from_file_locked(self, path: str) -> None:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        entries = data.get("memories", [])
        self._pool = {}
        # Recompute embeddings for loaded entries
        problems = [e.get("problem", e.get("content", "")) for e in entries]
        if problems:
            embeddings = _embed(problems, self.embedding_server)
            if embeddings is None:
                embeddings = [None] * len(entries)
        else:
            embeddings = []
        for entry, emb in zip(entries, embeddings):
            # Support old format (content-only) gracefully
            if "problem" not in entry:
                entry["problem"]  = entry.get("content", "")
                entry["strategy"] = entry.get("content", "")
            self._ensure_stats(entry)
            entry["embedding"] = emb
            self._pool[entry["id"]] = entry
        self._step  = data.get("step", 0)
        self._dirty = False
        logger.info(f"MemoryBank loaded {len(self._pool)} memories from {path}")

    def _prune_and_cap(self) -> None:
        """Must be called while self._lock is held."""
        for entry in self._pool.values():
            self._ensure_stats(entry)

        bad = [
            mid for mid, e in self._pool.items()
            if (
                e["uses"] >= self.prune_min_uses
                and e["mean_delta"] < self.prune_mean_delta_threshold
                and e["losses"] > e["wins"]
            )
        ]
        for mid in bad:
            del self._pool[mid]

        if len(self._pool) > self.max_memories:
            ranked = sorted(
                self._pool.values(),
                key=lambda e: (
                    self._entry_value(e),
                    e.get("score", 0),
                    e.get("created_step", 0),
                ),
                reverse=True,
            )
            keep   = {e["id"] for e in ranked[: self.max_memories]}
            self._pool = {mid: e for mid, e in self._pool.items() if mid in keep}


# ── Checkpoint helper ─────────────────────────────────────────────────────────

def _find_latest_checkpoint(save_dir: str) -> Optional[str]:
    best_step, best_path = -1, None
    try:
        for fname in os.listdir(save_dir):
            if fname.startswith("memory_step_") and fname.endswith(".json"):
                try:
                    step = int(fname[len("memory_step_"):-len(".json")])
                    if step > best_step:
                        best_step = step
                        best_path = os.path.join(save_dir, fname)
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    return best_path


# ── Process-level singleton registry ─────────────────────────────────────────

_REGISTRY_LOCK = threading.Lock()
_REGISTRY: dict[str, "MemoryBank"] = {}


def get_or_create_memory_bank(
    save_dir: str = "/tmp/hotpot_memory",
    max_memories: int = 100,
    prune_threshold: int = -2,
    dedup_threshold: float = 0.85,
    embedding_server: str = "http://localhost:8765",
    prune_min_uses: int = 4,
    prune_mean_delta_threshold: float = 0.0,
    delta_ema_alpha: float = 0.2,
    search_min_similarity: float = 0.0,
    search_candidate_multiplier: int = 4,
) -> MemoryBank:
    """Return (creating if necessary) the singleton MemoryBank for `save_dir`."""
    with _REGISTRY_LOCK:
        if save_dir not in _REGISTRY:
            _REGISTRY[save_dir] = MemoryBank(
                save_dir=save_dir,
                max_memories=max_memories,
                prune_threshold=prune_threshold,
                dedup_threshold=dedup_threshold,
                embedding_server=embedding_server,
                prune_min_uses=prune_min_uses,
                prune_mean_delta_threshold=prune_mean_delta_threshold,
                delta_ema_alpha=delta_ema_alpha,
                search_min_similarity=search_min_similarity,
                search_candidate_multiplier=search_candidate_multiplier,
            )
        return _REGISTRY[save_dir]
