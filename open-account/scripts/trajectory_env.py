#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Per-trajectory wrapper for the open-account mimic API.

Use one OpenAccountTrajectoryEnv per rollout/sample. Each environment writes
auth tokens to a trajectory-specific session file, which prevents local state
from leaking between concurrent trajectories.

Important: the remote mimic backend still stores account-opening progress by
user/account. For true independent training samples, use a unique phone/email
pair per trajectory or add a backend reset endpoint.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from api import TradeAPI


@dataclass(frozen=True)
class TrajectoryIdentity:
    trajectory_id: str
    contact: str
    verification_code: str
    contact_type: str = "MOBILE"
    area_code: str = "1"


class OpenAccountTrajectoryEnv:
    def __init__(
        self,
        identity: TrajectoryIdentity,
        environment: str = "test",
        session_root: str | None = None,
    ):
        self.identity = identity
        self.api = TradeAPI(
            environment=environment,
            session_id=identity.trajectory_id,
            session_dir=session_root or os.path.join(os.path.dirname(__file__), ".sessions"),
        )

    def reset_local_session(self) -> dict[str, Any]:
        """Clear only the local token cache for this trajectory."""
        self.api.clear_session()
        return {"ok": True, "session_file": self.api._session_path()}

    def bootstrap(self) -> dict[str, Any]:
        """Authenticate and return current account-opening progress."""
        identity = self.identity
        sent = self.api.send_verification_code(
            contact=identity.contact,
            contact_type=identity.contact_type,
            area_code=identity.area_code,
        )
        login = self.api.login(
            contact=identity.contact,
            verification_code=identity.verification_code,
            contact_type=identity.contact_type,
            area_code=identity.area_code,
        )
        token = self.api.get_trading_token()
        progress = self.api.query_progress()
        return {
            "send_verification_code": sent,
            "login": login,
            "get_trading_token": token,
            "query_progress": progress,
        }

    def query_progress(self) -> dict[str, Any]:
        return self.api.query_progress()

    def collect_information(self, data: dict[str, Any]) -> dict[str, Any]:
        return self.api.collect_information(data)

    def submit_application(self) -> dict[str, Any]:
        return self.api.submit_application()


def make_env(trajectory_id: str, contact: str, verification_code: str, area_code: str = "1") -> OpenAccountTrajectoryEnv:
    return OpenAccountTrajectoryEnv(
        TrajectoryIdentity(
            trajectory_id=trajectory_id,
            contact=contact,
            verification_code=verification_code,
            area_code=area_code,
        )
    )

