import os
import sys
import threading
import time
import gc
import queue
import concurrent.futures  # <-- Add this import
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import alfworld.agents.modules.generic as generic
from alfworld.agents.environment import get_environment

app = FastAPI()

# POOL_SIZE = int(os.environ.get("ALFWORLD_POOL_SIZE", "30"))
POOL_SIZE=300

_all_game_files = []
_available_envs = queue.Queue(maxsize=POOL_SIZE)
_active_envs = {}
_active_envs_lock = threading.Lock()
_tw_lock = threading.Lock()

# Define config and env_class globally so the thread pool can access them
config = None
env_class = None

@app.on_event("startup")
def startup():
    global _all_game_files, config, env_class
    os.environ["ALFWORLD_DATA"] = os.path.expanduser(
        "/cpfs01/nlp/leizhengxing/stock-rl/data/alfworld_env_data/alfworld"
    )
    sys.argv = ["alfworld_server", os.path.abspath("alfworld/configs/base_config.yaml")]

    config = generic.load_config()
    env_class = get_environment("AlfredTWEnv")

    with _tw_lock:
        # 1. Discover all game files ONCE
        temp_env = env_class(config, train_eval="train")
        _all_game_files = list(temp_env.game_files)

        try:
            valid_seen_env = env_class(config, train_eval="eval_in_distribution")
            _all_game_files.extend(valid_seen_env.game_files)
        except Exception:
            pass

        try:
            valid_unseen_env = env_class(config, train_eval="eval_out_of_distribution")
            _all_game_files.extend(valid_unseen_env.game_files)
        except Exception:
            pass

        print(f"[startup] Cached {len(_all_game_files)} total valid game paths.")

    # 2. Pre-allocate the pool slots IN PARALLEL
    print(f"[startup] Initializing pool of {POOL_SIZE} AlfWorld slots in parallel...")
    
    def init_single_slot():
        # This function runs in a worker thread. 
        # It handles the heavy directory globbing done by env_class
        slot_base_env = env_class(config, train_eval="train")
        return {
            "base_env": slot_base_env,
            "specific_env": None,  # Will hold the actual compiled gym environment later
            "lock": threading.Lock(),
            "done": False,
            "last_active": time.time()  # <-- Add this
        }

    # Open a ThreadPool to initialize environments concurrently.
    # 16-32 workers usually saturates I/O nicely without overloading CPU.
    MAX_WORKERS = 5
    initialized_count = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(init_single_slot) for _ in range(POOL_SIZE)]
        
        for future in concurrent.futures.as_completed(futures):
            try:
                container = future.result()
                _available_envs.put(container)
                initialized_count += 1
                
                # Print progress every 50 envs so you know it's not hanging
                if initialized_count % 50 == 0 or initialized_count == POOL_SIZE:
                    print(f"   ... {initialized_count}/{POOL_SIZE} environments initialized.")
            except Exception as e:
                print(f"[startup] Error initializing slot: {e}")

    print(f"[startup] POOL SIZE: {_available_envs.qsize()}")
    print("[startup] Pool ready. Memory usage will remain stable.")
    reaper_thread = threading.Thread(target=zombie_reaper, daemon=True)
    reaper_thread.start()
    print("[startup] Zombie Reaper thread started.")


def zombie_reaper():
    """Background thread that reclaims environments abandoned by the client."""
    while True:
        time.sleep(60) # check every minute
        now = time.time()
        zombies = []
        
        with _active_envs_lock:
            for env_id, container in list(_active_envs.items()):
                # If an environment hasn't been touched in 5 minutes (300 seconds), it's a zombie
                if now - container.get("last_active", now) > 3600:
                    zombies.append(env_id)
            
            for env_id in zombies:
                container = _active_envs.pop(env_id)
                # Safely kill the environment
                with _tw_lock:
                    try:
                        if container["specific_env"] is not None:
                            container["specific_env"].close()
                    except Exception:
                        pass
                    container["specific_env"] = None
                
                # Put the slot back into the pool
                _available_envs.put(container)
                
        if zombies:
            print(f"[Reaper] Reclaimed {len(zombies)} zombie environments: {zombies}")
            gc.collect()
# ... (keep all your existing @app.post routes the exactly same as your original script)
@app.exception_handler(Exception)
async def handle_exc(request: Request, exc: Exception):
    import traceback; traceback.print_exc()
    return JSONResponse(status_code=500, content={"detail": str(exc)})

class ResetRequest(BaseModel):
    env_id: str
    game_file: Optional[str] = None

class StepRequest(BaseModel):
    env_id: str
    action: str

class CloseRequest(BaseModel):
    env_id: str


@app.post("/reset")
def reset(req: ResetRequest):
    global _all_game_files
    
    try:
        # Backpressure: If 100 requests arrive, 30 will process immediately.
        # The other 70 will timeout and safely retry via the client script's retry loop!
        container = _available_envs.get(timeout=15)

    except queue.Empty:
        raise HTTPException(
            status_code=503, 
            detail="Server pool is full. Client should retry."
        )

    max_retries = 5
    last_err = None

    for attempt in range(max_retries):
        try:
            with _tw_lock:
                base_env = container["base_env"]

                # --- FIX 1: Safely KILL the old underlying C-process to prevent leaks ---
                if container["specific_env"] is not None:
                    try:
                        container["specific_env"].close()
                    except Exception:
                        pass
                    container["specific_env"] = None

                # Resolve the requested game path
                target_dir = req.game_file
                trial_name = os.path.basename(target_dir.rstrip("/"))
                if trial_name == "game.tw-pddl":
                    trial_name = os.path.basename(os.path.dirname(target_dir))

                actual_game_path = next(
                    (p for p in _all_game_files if trial_name in p), None
                )

                if actual_game_path:
                    base_env.game_files = [actual_game_path]
                else:
                    target_game_file = (
                        target_dir
                        if target_dir.endswith("game.tw-pddl")
                        else os.path.join(target_dir, "game.tw-pddl")
                    )
                    base_env.game_files = [target_game_file]

                base_env.num_games = 1

                # Compile and start the specific environment!
                specific_env = base_env.init_env(batch_size=1)
                obs, info = specific_env.reset()

                container["specific_env"] = specific_env
                container["done"] = False

            # Assign ownership
            container["last_active"] = time.time()
            with _active_envs_lock:
                _active_envs[req.env_id] = container

            return {
                "observation": obs[0],
                "admissible_commands": list(info.get("admissible_commands", [[]])[0]),
            }
            
        except Exception as e:
            import traceback; traceback.print_exc()
            last_err = e
            time.sleep(0.2 * (attempt + 1))

    # If all 5 retries fail, return the slot to the pool so it isn't permanently lost
    _available_envs.put(container)
    raise HTTPException(status_code=500, detail=f"reset failed: {last_err}")


@app.post("/step")
def step(req: StepRequest):
    with _active_envs_lock:
        container = _active_envs.get(req.env_id)

    if not container:
        raise HTTPException(status_code=404, detail="Environment not found. Reset first.")

    with _tw_lock:
        specific_env = container["specific_env"]
        obs, scores, dones, infos = specific_env.step([req.action])
        container["done"] = bool(dones[0])

    container["last_active"] = time.time()
    return {
        "observation": obs[0],
        "reward": float(scores[0]),
        "done": bool(dones[0]),
        "admissible_commands": list(infos.get("admissible_commands", [[]])[0]),
    }


@app.post("/close")
def close_env(req: CloseRequest):
    with _active_envs_lock:
        container = _active_envs.pop(req.env_id, None)

    if container:
        # --- FIX 2: Explicitly terminate the environment immediately ---
        with container["lock"]:
            try:
                if container["specific_env"] is not None:
                    container["specific_env"].close()
            except Exception as e:
                print(f"[close] Error closing env: {e}")
                
            # Clear the reference so GC can purge it
            container["specific_env"] = None
        
        # Return slot back to the available queue
        _available_envs.put(container)
        
        # Periodic passive GC sweep
        # gc.collect()

    return {"status": "returned to pool and memory freed"}


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
                try:
                    if container["specific_env"] is not None:
                        # Close the underlying C-process to prevent memory leaks
                        container["specific_env"].close()
                except Exception as e:
                    print(f"[recollect] Error closing stuck env {env_id}: {e}")
                
                # Clear reference
                container["specific_env"] = None
            
            # Put back in the queue
            _available_envs.put(container)
            reclaimed_count += 1
            
    # Force python to clean up the orphaned memory
    gc.collect()

    return {
        "status": "recollected",
        "reclaimed_count": reclaimed_count,
        "available_pool_size": _available_envs.qsize()
    }
