"""
webshop_tool.py
───────────────
Async tool classes for the WebShop multi-turn RL agent.

Key fixes vs. original:
  1. create() returns (instance_id, ToolResponse, clickables) — 3-tuple like ALFWorld.
  2. execute() appends [Available Actions] to every observation so the model
     always knows exactly what it can click next.
  3. "Task completed.<reward=X>" is appended on done so the reward function
     can parse the true env reward without trusting the model's output.
"""

import asyncio
import json
import os
from uuid import uuid4

import aiohttp

from verl.tools.base_tool import BaseTool, ToolResponse
from verl.tools.schemas import OpenAIFunctionToolSchema
from verl.utils.rollout_trace import rollout_trace_op


# ── Multi-cluster config ──────────────────────────────────────────────────────

def _load_clusters():
    raw = os.environ.get("WEBSHOP_CLUSTERS")
    if raw:
        clusters = json.loads(raw)
        return [
            {
                "host":        c["host"],
                "base_port":   int(c["base_port"]),
                "num_servers": int(c["num_servers"]),
            }
            for c in clusters
        ]
    return [
        {
            "host":        os.environ.get("WEBSHOP_SERVER_HOST", "localhost"),
            "base_port":   int(os.environ.get("WEBSHOP_BASE_PORT", 8900)),
            "num_servers": int(os.environ.get("WEBSHOP_NUM_SERVERS", 64)),
        }
    ]

CLUSTERS = _load_clusters()

_CREATE_TIMEOUT = aiohttp.ClientTimeout(total=120)
_STEP_TIMEOUT   = aiohttp.ClientTimeout(total=60)
_MAX_RETRIES    = 30


def _parse_available_actions(observation: str) -> list[str]:
    """
    Extract clickable items and search bar presence from a WebShop text observation.

    The text observation format uses [SEP] as a delimiter.  Segments that look
    like product ASINs (B0…) or navigation links are treated as clickables.
    This mirrors what WebAgentTextEnv.get_available_actions() returns.
    """
    parts = [p.strip() for p in observation.split("[SEP]") if p.strip()]
    actions = []

    has_search_bar = any("search" in p.lower() and len(p) < 20 for p in parts)
    if has_search_bar:
        actions.append("search[<your query here>]")

    # Navigation / product links appear as short segments
    nav_keywords = {
        "back to search", "next >", "< prev", "description",
        "features", "reviews", "buy now",
    }
    for part in parts:
        low = part.lower()
        # Navigation buttons
        if low in nav_keywords:
            actions.append(f"click[{part}]")
            continue
        # Product ASINs (e.g. B09QCVCYVY)
        if len(part) <= 12 and part.upper() == part and part.isalnum() and len(part) >= 6:
            actions.append(f"click[{part.lower()}]")
            continue
        # Attribute values (color / size options) — short, no digits run
        if len(part) <= 20 and part.count(" ") <= 3 and part[0].isalpha():
            # Avoid including long product titles
            if part.lower() not in {"webshop", "instruction"} and "[" not in part:
                actions.append(f"click[{part.lower()}]")

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for a in actions:
        if a not in seen:
            seen.add(a)
            unique.append(a)
    return unique


class EnvStepTool(BaseTool):
    """
    Manages a shared pool of (host, port) slots across ALL clusters.
    Each RL rollout acquires one slot on create(), reuses it across
    execute() calls, then releases it back to the pool on release().
    """

    def __init__(self, config, tool_schema):
        super().__init__(config, tool_schema)
        self._instances: dict[str, tuple[str, int]] = {}
        self._slot_pool: asyncio.Queue | None = None
        self._session:   aiohttp.ClientSession | None = None

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(limit=0)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def _get_slot_pool(self) -> asyncio.Queue:
        if self._slot_pool is None:
            self._slot_pool = asyncio.Queue()
            for cluster in CLUSTERS:
                host      = cluster["host"]
                base_port = cluster["base_port"]
                n         = cluster["num_servers"]
                for i in range(n):
                    await self._slot_pool.put((host, base_port + i))
            total = self._slot_pool.qsize()
            print(f"[EnvStep] pool ready: {total} slots across "
                  f"{len(CLUSTERS)} cluster(s)")
        return self._slot_pool

    @staticmethod
    def _server_url(host: str, port: int) -> str:
        return f"http://{host}:{port}"

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def create(self, instance_id=None, create_kwargs=None, **kwargs):
        """
        Reset a WebShop env and return (instance_id, ToolResponse, available_actions).

        The 3-tuple return mirrors ALFWorld's EnvStepTool so the agent loop
        can inject the initial observation + actions into the first user message.
        """
        if instance_id is None:
            instance_id = str(uuid4())

        create_kwargs = create_kwargs or {}

        pool    = await self._get_slot_pool()
        session = self._get_session()

        reset_payload = {
            "env_id":     instance_id,
            "session_id": create_kwargs.get("session_id") or create_kwargs.get("asin"),
            "goal":       create_kwargs.get("goal"),
        }
        print("reset_payload: ", reset_payload)

        for attempt in range(_MAX_RETRIES):
            host, port = await pool.get()
            try:
                async with session.post(
                    f"{self._server_url(host, port)}/reset",
                    json=reset_payload,
                    timeout=_CREATE_TIMEOUT,
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        self._instances[instance_id] = (host, port)
                        obs  = data.get("observation", "")
                        # Parse available actions from the initial observation
                        cmds = _parse_available_actions(obs)
                        return instance_id, ToolResponse(text=obs), cmds
                    else:
                        text = await resp.text()
                        print(f"[EnvStep] reset attempt {attempt+1} failed "
                              f"({resp.status}) @ {host}:{port}: {text[:200]}")
                        await pool.put((host, port))

            except Exception as e:
                import traceback; traceback.print_exc()
                print(f"[EnvStep] reset attempt {attempt+1} exception "
                      f"@ {host}:{port}: {e}")
                await pool.put((host, port))

            await asyncio.sleep(0.5 * (2 ** min(attempt, 4)))

        raise RuntimeError(
            f"[EnvStep] reset failed after {_MAX_RETRIES} retries"
        )

    @rollout_trace_op
    async def execute(self, instance_id: str, parameters: dict, **kwargs):
        """
        Execute one step and return (ToolResponse, step_reward, info).

        The observation always ends with:
          [Available Actions]: click[...], click[...], ...

        On episode end the observation also contains:
          Task completed.<reward=0.44>   (or Task failed.)

        This lets the reward function read the true env reward from the
        conversation history rather than trusting the model's <Finish> block.
        """
        slot = self._instances.get(instance_id)
        if slot is None:
            return ToolResponse(text="Instance not found."), 0.0, {}

        host, port = slot
        action = parameters.get("action", "").strip()
        if not action:
            return ToolResponse(text="No action provided."), 0.0, {}

        session = self._get_session()
        step_payload = {
            "env_id": instance_id,
            "action": action,
        }

        for attempt in range(_MAX_RETRIES):
            try:
                async with session.post(
                    f"{self._server_url(host, port)}/step",
                    json=step_payload,
                    timeout=_STEP_TIMEOUT,
                ) as resp:
                    if resp.status == 200:
                        data   = await resp.json()
                        done   = data["done"]
                        reward = data["reward"]
                        obs    = data.get("observation", "")

                        # ── Shape step reward ────────────────────────────
                        if done and reward >= 1.0:
                            step_reward = 1.0
                        elif done and reward > 0:
                            step_reward = reward
                        else:
                            step_reward = 0.0

                        # ── Append terminal signal (parsed by reward fn) ──
                        if done:
                            if reward > 0:
                                # Encode the true env reward so the reward function
                                # can extract it without trusting the model output.
                                obs += f"\n\nTask completed.<reward={reward:.6f}>"
                            else:
                                obs += "\n\nTask failed."
                        else:
                            # Always show what the agent can do next
                            cmds = _parse_available_actions(obs)
                            if cmds:
                                obs += "\n\n[Available Actions]:\n" + ", ".join(cmds)

                        return (
                            ToolResponse(text=obs),
                            step_reward,
                            {"done": done, "raw_reward": reward},
                        )
                    else:
                        text = await resp.text()
                        print(f"[EnvStep] step attempt {attempt+1} failed "
                              f"({resp.status}) @ {host}:{port}: {text[:200]}")

            except Exception as e:
                import traceback; traceback.print_exc()
                print(f"[EnvStep] step attempt {attempt+1} exception "
                      f"@ {host}:{port}: {e}")

            await asyncio.sleep(0.2 * (attempt + 1))

        return ToolResponse(text="step failed after max retries"), 0.0, {}

    async def release(self, instance_id: str, **kwargs):
        slot = self._instances.pop(instance_id, None)
        if slot is not None:
            host, port = slot
            session = self._get_session()
            
            # --- FIX: Tell the server to explicitly delete and close this environment ---
            try:
                async with session.post(
                    f"{self._server_url(host, port)}/close",
                    json={"env_id": instance_id},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        print(f"[EnvStep] close warning @ {host}:{port}: status {resp.status}")
            except Exception as e:
                import traceback; traceback.print_exc()
                print(f"[EnvStep] close exception @ {host}:{port}: {e}")
            # -------------------------------------------------------------------------

            # Put the slot back in the pool for the next rollout
            pool = await self._get_slot_pool()
            await pool.put(slot)