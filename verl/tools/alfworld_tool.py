import asyncio
import json
import os
import random
from uuid import uuid4

import aiohttp

from verl.tools.base_tool import BaseTool, ToolResponse
from verl.tools.schemas import OpenAIFunctionToolSchema
from verl.utils.rollout_trace import rollout_trace_op

# ── Multi-cluster config ──────────────────────────────────────────────────────

def _load_clusters():
    raw = os.environ.get("ALFWORLD_CLUSTERS")
    if raw:
        clusters = json.loads(raw)
        return [
            {
                "host": c["host"],
                "base_port": int(c["base_port"]),
                "num_servers": int(c["num_servers"]),
            }
            for c in clusters
        ]
    return [
        {
            "host": os.environ.get("ALFWORLD_SERVER_HOST", "localhost"),
            "base_port": int(os.environ.get("ALFWORLD_BASE_PORT", 8800)),
            "num_servers": int(os.environ.get("ALFWORLD_NUM_SERVERS", 16)),
        }
    ]


CLUSTERS = _load_clusters()

_CREATE_TIMEOUT = aiohttp.ClientTimeout(total=600)
_STEP_TIMEOUT = aiohttp.ClientTimeout(total=120)
_CLOSE_TIMEOUT = aiohttp.ClientTimeout(total=100)
_MAX_RETRIES = 30

import logging

logger = logging.getLogger(__name__)


class EnvStepTool(BaseTool):
    """
    Manages a shared pool of (host, port) slots across ALL clusters.
    Each RL rollout acquires one slot on create(), reuses it across
    execute() calls, then releases it back to the pool on release().
    """

    def __init__(self, config, tool_schema):
        super().__init__(config, tool_schema)
        # instance_id → (host, port)
        self._instances: dict[str, tuple[str, int]] = {}
        self._slot_pool: asyncio.Queue | None = None
        self._session: aiohttp.ClientSession | None = None

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(limit=0)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def _get_slot_pool(self) -> asyncio.Queue:
        """Build a flat pool of (host, port) tuples from all clusters."""
        if self._slot_pool is None:
            self._slot_pool = asyncio.Queue()
            for cluster in CLUSTERS:
                host = cluster["host"]
                base_port = cluster["base_port"]
                n = cluster["num_servers"]
                for i in range(n):
                    await self._slot_pool.put((host, base_port + i))
            total = self._slot_pool.qsize()
            print(
                f"[EnvStep] pool ready: {total} slots across "
                f"{len(CLUSTERS)} cluster(s)"
            )
        return self._slot_pool

    @staticmethod
    def _server_url(host: str, port: int) -> str:
        return f"http://{host}:{port}"

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def create(self, instance_id=None, create_kwargs=None, **kwargs):
        if instance_id is None:
            instance_id = str(uuid4())

        create_kwargs = create_kwargs or {}
        game_file = create_kwargs.get("game_file")

        pool = await self._get_slot_pool()
        session = self._get_session()

        for attempt in range(_MAX_RETRIES):
            host, port = await pool.get()
            try:
                async with session.post(
                    f"{self._server_url(host, port)}/reset",
                    json={"env_id": instance_id, "game_file": game_file},
                    timeout=_CREATE_TIMEOUT,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self._instances[instance_id] = (host, port)
                        obs = data.get("observation", "")
                        cmds = data.get("admissible_commands", [])
                        return instance_id, ToolResponse(text=obs), cmds
                    else:
                        text = await resp.text()
                        logger.warning(
                            f"reset attempt {attempt+1} failed "
                            f"({resp.status}) @ {host}:{port}: {text}"
                        )
                        await pool.put((host, port))

            except Exception as e:
                import traceback

                print("in the alfworld tool create")
                traceback.print_exc()
                print(
                    f"reset attempt {attempt+1} exception " f"@ {host}:{port}: {e}"
                )
                await pool.put((host, port))

            await asyncio.sleep(0.5 * (2 ** min(attempt, 4)))

        raise RuntimeError(f"[EnvStep] reset failed after {_MAX_RETRIES} retries")

    @rollout_trace_op
    async def execute(self, instance_id: str, parameters: dict, **kwargs):
        slot = self._instances.get(instance_id)
        if slot is None:
            return ToolResponse(text="Instance not found."), 0.0, {}

        host, port = slot
        action = parameters.get("action", "").strip()
        if not action:
            return ToolResponse(text="No action provided."), 0.0, {}

        session = self._get_session()
        step_payload = {"env_id": instance_id, "action": action}
        for attempt in range(_MAX_RETRIES):
            try:
                async with session.post(
                    f"{self._server_url(host, port)}/step",
                    json=step_payload,
                    timeout=_STEP_TIMEOUT,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        done = data["done"]
                        reward = data["reward"]
                        step_reward = (
                            1.0
                            if (done and reward > 0)
                            else (0.1 if reward > 0 else 0.0)
                        )
                        obs = data.get("observation", "")
                        cmds = data.get("admissible_commands", [])

                        if done and reward > 0:
                            obs += "\n\nTask completed."
                        elif done and reward <= 0:
                            obs += "\n\nTask failed."
                        if cmds:
                            obs += "\n\n[Available Actions]:\n" + ", ".join(cmds)

                        return (
                            ToolResponse(text=obs),
                            step_reward,
                            {"done": done},
                        )
                    else:
                        text = await resp.text()
                        logger.warning(
                            f"step attempt {attempt+1} failed "
                            f"({resp.status}) @ {host}:{port}: {text}"
                        )

            except Exception as e:
                import traceback

                print("in the alfworld tool step")
                traceback.print_exc()

            await asyncio.sleep(0.2 * (attempt + 1))

        return ToolResponse(text="step failed after max retries"), 0.0, {}

    async def release(self, instance_id: str, **kwargs):
        """
        Tell the server to destroy the environment, then return the slot
        back to the pool so another rollout can use it.
        """
        slot = self._instances.pop(instance_id, None)
        if slot is None:
            return

        host, port = slot
        session = self._get_session()

        # Tell the server to close and free memory for this env_id
        try:
            async with session.post(
                f"{self._server_url(host, port)}/close",
                json={"env_id": instance_id},
                timeout=_CLOSE_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    print(
                        f"[EnvStep] close warning @ {host}:{port}: "
                        f"status {resp.status}"
                    )
        except Exception as e:
            import traceback

            traceback.print_exc()
            print(f"[EnvStep] close exception @ {host}:{port}: {e}")

        # Return the slot to the pool for reuse
        pool = await self._get_slot_pool()
        await pool.put(slot)