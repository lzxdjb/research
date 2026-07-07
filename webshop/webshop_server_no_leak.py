import os
import threading
import time
import gym
import re
import queue
import gc

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from web_agent_site.envs import WebAgentTextEnv

app = FastAPI()

OBS_MODE = os.environ.get("WEBSHOP_OBS_MODE", "text")
PORT     = int(os.environ.get("PORT", 8900))

# Set how many concurrent environments this server can handle at once.
# e.g., if you have 100 samples and 4 servers, set this to >= 25.
# POOL_SIZE = int(os.environ.get("WEBSHOP_POOL_SIZE", "30")) 
POOL_SIZE=300
raw_num = os.environ.get("WEBSHOP_NUM_PRODUCTS", "all")
NUM_PRODUCTS = None if raw_num == "all" else int(raw_num)

# --- PRE-ALLOCATED POOL ---
_available_envs = queue.Queue(maxsize=POOL_SIZE)
_active_envs = {}
_active_envs_lock = threading.Lock()
_asin_to_session: dict = {}  # ASIN -> integer session index

def override_instruction(obs: str, target_goal: str) -> str:
    """Forcefully replaces the WebShop instruction with our target goal."""
    if not target_goal:
        return obs
    return re.sub(
        r"(Instruction:.*?\[SEP\]\s*)(.*?)(\s*\[SEP\])", 
        rf"\g<1>{target_goal}\g<3>", 
        obs, 
        count=1,
        flags=re.IGNORECASE | re.DOTALL
    )

@app.on_event("startup")
def startup():
    global _asin_to_session
    print(f"[startup] Initializing pool of {POOL_SIZE} WebShop envs on port {PORT}...")

    # Build ASIN -> goal index map using a single temp env.
    # self.goals is a list; index i is what env.reset(session=i) uses.
    _tmp_env = gym.make("WebAgentTextEnv-v0", observation_mode=OBS_MODE, num_products=5000)
    _tmp_env.reset()
    unwrapped = getattr(_tmp_env, "unwrapped", _tmp_env)
    server = getattr(unwrapped, "server", unwrapped)
    if hasattr(server, "goals"):
        for idx, goal in enumerate(server.goals):
            asin = goal.get("asin")
            if asin:
                _asin_to_session[str(asin).upper()] = idx
        print(f"[startup] Built ASIN→goal-index map: {len(_asin_to_session)} entries")
    else:
        print("[startup] WARNING: env has no .goals attribute — session lookup will fail!")
    _tmp_env.close()

    for i in range(POOL_SIZE):
        env = gym.make(
            "WebAgentTextEnv-v0",
            observation_mode=OBS_MODE,
            num_products=5000
        )
        env.reset() # Warm up

        container = {
            "env": env,
            "lock": threading.Lock(),
            "goal": None
        }
        _available_envs.put(container)

    print(f"[startup] {POOL_SIZE} envs ready. Memory usage will now be stable.")

@app.exception_handler(Exception)
async def handle_exc(request: Request, exc: Exception):
    import traceback; traceback.print_exc()
    return JSONResponse(status_code=500, content={"detail": str(exc)})

class ResetRequest(BaseModel):
    env_id: str
    session_id: Optional[str] = None
    goal: Optional[str] = None       

class StepRequest(BaseModel):
    env_id: str
    action: str                        

class CloseRequest(BaseModel):
    env_id: str

@app.post("/reset")
def reset(req: ResetRequest):
    try:
        # Wait up to 10 seconds for an environment to free up.
        # If the pool is empty, it raises queue.Empty, and we return 503.
        # Your client's _MAX_RETRIES loop will automatically back off and try again!
        container = _available_envs.get(timeout=10)
    except queue.Empty:
        raise HTTPException(
            status_code=503, 
            detail="Server pool is full. Client should retry."
        )

    _env = container["env"]
    container["goal"] = req.goal 

    try:
        with container["lock"]:
            kwargs = {}
            target_session = None

            # Primary lookup: ASIN → integer session index via the pre-built map
            if req.session_id:
                target_session = _asin_to_session.get(str(req.session_id).upper())
                if target_session is None:
                    print(f"[reset] WARNING: ASIN '{req.session_id}' not found in session map")

            # Fallback: scan sessions by goal text (slower, but catches edge cases)
            if target_session is None and req.goal:
                unwrapped_env = getattr(_env, "unwrapped", _env)
                if hasattr(unwrapped_env, "sessions"):
                    for sid, s_data in unwrapped_env.sessions.items():
                        inst = s_data.get("instruction", "") if isinstance(s_data, dict) else str(s_data)
                        if req.goal.strip().lower() in inst.lower():
                            target_session = sid
                            break

            if target_session is not None:
                kwargs["session"] = target_session
            else:
                print(f"[reset] ERROR: could not resolve session for asin={req.session_id} goal={req.goal!r:.80}")

            obs_result = _env.reset(**kwargs)
            obs = str(obs_result[0] if isinstance(obs_result, tuple) else obs_result)
            
            obs = override_instruction(obs, container["goal"])
            info = {"goal": req.goal, "session_id": getattr(_env, "session", target_session)}

        # Mark this environment as actively owned by this specific env_id
        with _active_envs_lock:
            _active_envs[req.env_id] = container

        return {"observation": obs, "info": info}

    except Exception as e:
        # If reset fails, put the env back in the queue so it's not permanently lost
        _available_envs.put(container)
        raise HTTPException(status_code=500, detail=f"reset failed: {e}")


@app.post("/step")
def step(req: StepRequest):
    with _active_envs_lock:
        container = _active_envs.get(req.env_id)
        
    if not container:
        raise HTTPException(status_code=404, detail="env_id not found or already closed")

    _env = container["env"]

    with container["lock"]:
        step_result = _env.step(req.action)
        
        if len(step_result) == 5:
            obs, reward, terminated, truncated, info = step_result
            done = terminated or truncated
        else:
            obs, reward, done, info = step_result
            
        obs = str(obs)
        obs = override_instruction(obs, container["goal"])

    return {
        "observation": obs,
        "reward": float(reward),
        "done": bool(done),
        "info": info,
    }


@app.post("/close")
def close_env(req: CloseRequest):
    with _active_envs_lock:
        container = _active_envs.pop(req.env_id, None)
        
    if container:
        # DO NOT DESTROY the gym environment. 
        # Just clean its temporary state and return it to the queue for the next trajectory!
        container["goal"] = None
        _available_envs.put(container)
            
    return {"status": "returned to pool"}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "available_pool_size": _available_envs.qsize(),
        "active_trajectories": len(_active_envs)
    }


@app.post("/recollect")
def recollect_envs():
    """
    Forcefully reclaims any 'lost' or stuck environments from the active list
    and returns them to the available pool. Run this between training runs.
    """
    reclaimed_count = 0

    with _active_envs_lock:
        stuck_env_ids = list(_active_envs.keys())

        for env_id in stuck_env_ids:
            container = _active_envs.pop(env_id)

            with container["lock"]:
                container["goal"] = None

            _available_envs.put(container)
            reclaimed_count += 1

    gc.collect()

    return {
        "status": "recollected",
        "reclaimed_count": reclaimed_count,
        "available_pool_size": _available_envs.qsize()
    }