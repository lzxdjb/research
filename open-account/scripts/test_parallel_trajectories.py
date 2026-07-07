#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Parallel trajectory smoke test for the open-account environment.

Default mode is offline: it starts several workers at the same time and proves
that each trajectory writes/loads a different local session file.

Live mode calls the mimic open-account backend in parallel. For true isolated
live trajectories, provide unique phone/email accounts. If multiple live
workers reuse the same contact, the remote backend will share application
progress for that account.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import time
from typing import Any

from api import TradeAPI
from trajectory_env import OpenAccountTrajectoryEnv, TrajectoryIdentity


def _default_session_root() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".parallel_test_sessions")


def _offline_worker(index: int, session_root: str, sleep_seconds: float) -> dict[str, Any]:
    trajectory_id = f"offline_traj_{index:06d}"
    api = TradeAPI(environment="test", session_id=trajectory_id, session_dir=session_root)
    api.clear_session()
    if sleep_seconds:
        time.sleep(sleep_seconds)

    api.userId = f"user_{index:06d}"
    api.token = f"session_token_{index:06d}"
    api.access_token = f"access_token_{index:06d}"
    api._save_session()

    reloaded = TradeAPI(environment="test", session_id=trajectory_id, session_dir=session_root)
    ok = (
        reloaded.userId == api.userId
        and reloaded.token == api.token
        and reloaded.access_token == api.access_token
    )
    return {
        "trajectory_id": trajectory_id,
        "session_file": api._session_path(),
        "userId": reloaded.userId,
        "token": reloaded.token,
        "access_token": reloaded.access_token,
        "ok": ok,
    }


def _load_accounts(path: str) -> list[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("accounts", [])
    if not isinstance(data, list):
        raise ValueError("accounts JSON must be a list or an object with an 'accounts' list")

    accounts = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"account entry {index} must be an object")
        contact = str(item.get("contact", "")).strip()
        verification_code = str(item.get("verification_code", "")).strip()
        if not contact or not verification_code:
            raise ValueError(f"account entry {index} requires contact and verification_code")
        contact_type = str(item.get("contact_type") or ("EMAIL" if "@" in contact else "MOBILE")).upper()
        accounts.append(
            {
                "trajectory_id": str(item.get("trajectory_id") or f"live_traj_{index:06d}"),
                "contact": contact,
                "verification_code": verification_code,
                "contact_type": contact_type,
                "area_code": str(item.get("area_code", "1")).lstrip("+"),
            }
        )
    return accounts


def _live_worker(
    index: int,
    account: dict[str, Any],
    session_root: str,
    do_collect: bool,
) -> dict[str, Any]:
    identity = TrajectoryIdentity(
        trajectory_id=account["trajectory_id"],
        contact=account["contact"],
        verification_code=account["verification_code"],
        contact_type=account["contact_type"],
        area_code=account["area_code"],
    )
    env = OpenAccountTrajectoryEnv(identity=identity, environment="test", session_root=session_root)
    env.reset_local_session()

    started_at = time.time()
    bootstrap = env.bootstrap()
    collect = None
    if do_collect:
        collect = env.collect_information(
            {
                "given_name": f"Parallel{index}",
                "family_name": f"Trajectory{index}",
            }
        )
    progress = env.query_progress()

    progress_data = progress.get("d") if isinstance(progress, dict) else None
    if not isinstance(progress_data, dict):
        progress_data = {}
    return {
        "trajectory_id": identity.trajectory_id,
        "contact": identity.contact,
        "session_file": env.api._session_path(),
        "elapsed_seconds": round(time.time() - started_at, 3),
        "login_success": bool(bootstrap.get("login", {}).get("data")),
        "token_success": bootstrap.get("get_trading_token", {}).get("s") == "ok",
        "progress_status": progress_data.get("status"),
        "missing_count": len(progress_data.get("missing_fields", [])),
        "collected_fields": progress_data.get("collected_fields", []),
        "collect_result": collect,
        "ok": bool(bootstrap.get("login", {}).get("data"))
        and bootstrap.get("get_trading_token", {}).get("s") == "ok"
        and progress.get("s") == "ok",
    }


def _run_parallel(fn, items: list[Any], max_workers: int) -> list[dict[str, Any]]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(fn, index, item) for index, item in enumerate(items)]
        results = []
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    return sorted(results, key=lambda item: item["trajectory_id"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--num-trajectories", type=int, default=4, help="Offline trajectory count.")
    parser.add_argument("--max-workers", type=int, default=4, help="Parallel worker count.")
    parser.add_argument("--session-root", default=_default_session_root(), help="Directory for per-trajectory sessions.")
    parser.add_argument("--sleep-seconds", type=float, default=0.0, help="Offline worker delay to create overlap.")
    parser.add_argument("--live", action="store_true", help="Call the real mimic backend in parallel.")
    parser.add_argument("--accounts-json", help="JSON list of live test accounts.")
    parser.add_argument(
        "--collect",
        action="store_true",
        help="In live mode, also collect a small name payload. This mutates remote backend progress.",
    )
    args = parser.parse_args()

    os.makedirs(args.session_root, exist_ok=True)

    if args.live:
        if not args.accounts_json:
            parser.error("--live requires --accounts-json")
        accounts = _load_accounts(args.accounts_json)
        if not accounts:
            parser.error("--accounts-json did not contain any accounts")
        worker = lambda index, account: _live_worker(index, account, args.session_root, args.collect)
        results = _run_parallel(worker, accounts, args.max_workers)
    else:
        count = max(args.num_trajectories, 1)
        worker = lambda index, _item: _offline_worker(index, args.session_root, args.sleep_seconds)
        results = _run_parallel(worker, list(range(count)), args.max_workers)

    session_files = [item["session_file"] for item in results]
    all_ok = all(item["ok"] for item in results) and len(session_files) == len(set(session_files))

    print(json.dumps({"ok": all_ok, "session_root": args.session_root, "results": results}, indent=2))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
