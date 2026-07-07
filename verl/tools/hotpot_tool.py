"""
SearchTool — wraps a Wikipedia / BM25 retriever for HotpotQA rollouts.

Two backends are supported (set via config.backend):
  • "wikipedia_api"  — live Wikipedia search via the `wikipedia` package
  • "bm25_local"     — offline BM25 index over a pre-built corpus (HotpotQA abstracts)

For RL training you almost always want "bm25_local" to avoid network latency
and rate-limit issues during large-scale rollouts.
"""

import asyncio
import logging
import os
from typing import Any, Optional
from uuid import uuid4

from verl.tools.base_tool import BaseTool
from verl.tools.schemas import OpenAIFunctionToolSchema, ToolResponse
from verl.utils.rollout_trace import rollout_trace_op

logger = logging.getLogger(__file__)
logger.setLevel(os.getenv("VERL_LOGGING_LEVEL", "WARN"))

# ── Optional heavy imports (lazy) ────────────────────────────────────────────

def _import_wikipedia():
    try:
        import wikipedia  # pip install wikipedia
        wikipedia.set_lang("en")
        return wikipedia
    except ImportError:
        raise ImportError("Install the `wikipedia` package: pip install wikipedia")


def _build_bm25_index(corpus_path: str):
    """
    Build a BM25 index from a JSONL file where each line has:
        {"title": "...", "text": "..."}
    Returns (index, docs) where docs[i] is the raw text snippet.
    """
    from rank_bm25 import BM25Okapi  # pip install rank-bm25
    import json

    docs   = []
    titles = []
    with open(corpus_path, "r", encoding="utf-8") as f:
        for line in f:
            obj = json.loads(line)
            title = obj.get("title", "")
            text  = obj.get("text", "")
            docs.append(f"{title}\n{text}")
            titles.append(title)

    tokenized = [d.lower().split() for d in docs]
    index     = BM25Okapi(tokenized)
    return index, docs


# ── Tool class ────────────────────────────────────────────────────────────────

class SearchTool(BaseTool):
    """
    Retrieves Wikipedia-style passages given a natural-language query.

    Config keys
    -----------
    backend        : "wikipedia_api" | "bm25_local"   (default: wikipedia_api)
    corpus_path    : path to JSONL corpus              (required for bm25_local)
    top_k          : number of passages to return      (default: 3)
    max_chars      : max chars per passage             (default: 1000)
    """

    def __init__(self, config: dict, tool_schema: OpenAIFunctionToolSchema):
        super().__init__(config, tool_schema)
        self._instance_dict: dict[str, dict] = {}

        self.backend    = config.get("backend",     "wikipedia_api")
        self.top_k      = int(config.get("top_k",   3))
        self.max_chars  = int(config.get("max_chars", 1000))

        # Lazy BM25 state
        self._bm25_index = None
        self._bm25_docs  = None

        if self.backend == "bm25_local":
            corpus_path = config.get("corpus_path", "")
            if not corpus_path:
                raise ValueError("SearchTool: 'corpus_path' is required for bm25_local backend")
            logger.info(f"Building BM25 index from {corpus_path} …")
            self._bm25_index, self._bm25_docs = _build_bm25_index(corpus_path)
            logger.info("BM25 index ready.")

    # ── BaseTool interface ────────────────────────────────────────────────────

    def get_openai_tool_schema(self) -> OpenAIFunctionToolSchema:
        return self.tool_schema

    async def create(
        self,
        instance_id: Optional[str] = None,
        **kwargs,
    ) -> tuple[str, ToolResponse]:
        if instance_id is None:
            instance_id = str(uuid4())
        self._instance_dict[instance_id] = {}
        return instance_id, ToolResponse()

    @rollout_trace_op
    async def execute(
        self,
        instance_id: str,
        parameters: dict[str, Any],
        **kwargs,
    ) -> tuple[ToolResponse, float, dict]:
        query = (parameters.get("query") or "").strip()
        if not query:
            return ToolResponse(text="Search tool: query is empty."), 0.0, {}

        try:
            if self.backend == "wikipedia_api":
                text = await asyncio.get_event_loop().run_in_executor(
                    None, self._search_wikipedia, query
                )
            else:
                text = self._search_bm25(query)
        except Exception as e:
            logger.warning(f"SearchTool error for query '{query}': {e}")
            text = f"Search tool call failed: {e}"

        # Reward only useful retrieval, not empty or failed lookups.
        lowered = text.lower() if text else ""
        tool_reward = 0.1 if text and "failed" not in lowered and "no relevant results found" not in lowered else 0.0
        result_text = f"Search query: {query}\nSearch results:\n{text}"
        return ToolResponse(text=result_text), tool_reward, {}

    async def calc_reward(self, instance_id: str, **kwargs) -> float:
        return 0.0

    async def release(self, instance_id: str, **kwargs) -> None:
        self._instance_dict.pop(instance_id, None)

    # ── Backend implementations ───────────────────────────────────────────────

    def _search_wikipedia(self, query: str) -> str:
        """Synchronous Wikipedia API search (run in executor)."""
        wikipedia = _import_wikipedia()
        snippets = []

        try:
            search_results = wikipedia.search(query, results=self.top_k)
        except Exception as e:
            return f"Search tool call failed: {type(e).__name__}: {e}"

        for title in search_results[: self.top_k]:
            try:
                page    = wikipedia.page(title, auto_suggest=False)
                content = page.content[: self.max_chars]
                snippets.append(f"[{page.title}]\n{content}")
            except Exception:
                continue

        return "\n\n".join(snippets) if snippets else "No relevant results found."

    def _search_bm25(self, query: str) -> str:
        """Local BM25 retrieval (synchronous, no I/O)."""
        tokenized_query = query.lower().split()
        scores          = self._bm25_index.get_scores(tokenized_query)

        # Top-k indices
        top_indices = sorted(
            range(len(scores)), key=lambda i: scores[i], reverse=True
        )[: self.top_k]

        snippets = []
        for idx in top_indices:
            doc = self._bm25_docs[idx]
            snippets.append(doc[: self.max_chars])

        return "\n\n".join(snippets) if snippets else "No relevant results found."