# verl/trainer/ppo/erl_memory.py
"""
Cross-episode reflection memory for Experiential Reinforcement Learning (ERL).

Stores successful reflections (those that led to r > threshold) so they can
be injected into subsequent rollouts as contextual priors.
"""

import threading
import time
from collections import deque
from typing import Optional


class ERLReflectionMemory:
    """
    Thread-safe ring-buffer of successful reflections.

    Each entry is a plain string (the reflection text). During rollout,
    the agent samples up to `max_inject` reflections and prepends them to
    the reflection-generation prompt so the model can reuse effective patterns.
    """

    def __init__(self, max_size: int = 64, max_inject: int = 3):
        self._buffer: deque[str] = deque(maxlen=max_size)
        self._lock = threading.Lock()
        self.max_inject = max_inject

    # ------------------------------------------------------------------
    def store(self, reflection: str) -> None:
        with self._lock:
            self._buffer.append(reflection)

    def sample(self) -> list[str]:
        """Return up to max_inject recent reflections (newest first)."""
        with self._lock:
            items = list(self._buffer)
        # Return the most-recent ones
        return items[-self.max_inject :][::-1]

    def __len__(self) -> int:
        with self._lock:
            return len(self._buffer)


# Module-level singleton — shared across all agent-loop workers in the same
# Python process. For multi-process setups (Ray remote workers) each worker
# has its own copy; cross-worker sharing can be added via a Ray actor if needed.
_GLOBAL_MEMORY: Optional[ERLReflectionMemory] = None
_MEMORY_LOCK = threading.Lock()


def get_global_memory(max_size: int = 64, max_inject: int = 3) -> ERLReflectionMemory:
    global _GLOBAL_MEMORY
    with _MEMORY_LOCK:
        if _GLOBAL_MEMORY is None:
            _GLOBAL_MEMORY = ERLReflectionMemory(max_size=max_size, max_inject=max_inject)
    return _GLOBAL_MEMORY