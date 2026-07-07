"""Native VERL tools for interactive HDL compilation, simulation, and synthesis."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional
from uuid import uuid4

from recipe.hdl_agent.hdl_judge import compute_hdl_score_for_phase, task_from_tool_parameters
from verl.tools.base_tool import BaseTool
from verl.tools.schemas import OpenAIFunctionToolSchema, ToolResponse

logger = logging.getLogger(__name__)
logger.setLevel(os.getenv("VERL_LOGGING_LEVEL", "WARN"))


def _default_task_from_agent(agent_data: Any) -> dict[str, Any]:
    task = getattr(agent_data, "task", None)
    return dict(task) if isinstance(task, dict) else {}


def _format_tool_result(phase: str, result: dict[str, Any]) -> str:
    feedback = str(result.get("hdl_feedback") or "").strip()
    summary = {
        "phase": phase,
        "score": result.get("score"),
        "extract_ok": result.get("hdl_extract_ok"),
        "chisel_elab_ok": result.get("hdl_chisel_elab_ok"),
        "slang_ok": result.get("hdl_slang_ok"),
        "lint_ok": result.get("hdl_lint_ok"),
        "sim_ok": result.get("hdl_sim_ok"),
        "sim_mode": result.get("hdl_sim_mode"),
        "functional_sim_ok": result.get("hdl_functional_sim_ok"),
        "auto_smoke_sim_ok": result.get("hdl_auto_smoke_sim_ok"),
        "synth_ok": result.get("hdl_synth_ok"),
    }
    return "HDL_TOOL_RESULT " + json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n" + feedback


class _HDLBaseTool(BaseTool):
    phase = "full"

    def __init__(self, config: dict, tool_schema: OpenAIFunctionToolSchema):
        super().__init__(config, tool_schema)
        self._instance_dict: dict[str, dict[str, Any]] = {}
        self.timeout = int(config.get("timeout") or os.environ.get("HDL_AGENT_TIMEOUT", 30))
        self.feedback_max_chars = int(config.get("feedback_max_chars") or os.environ.get("HDL_AGENT_FEEDBACK_MAX_CHARS", 5000))
        self.keep_work = bool(config.get("keep_work", False))
        self.env_sh = config.get("env_sh") or os.environ.get("HDL_ENV_SH")

    async def create(self, instance_id: Optional[str] = None, **kwargs) -> tuple[str, ToolResponse]:
        if instance_id is None:
            instance_id = str(uuid4())
        self._instance_dict[instance_id] = {}
        return instance_id, ToolResponse()

    async def execute(
        self,
        instance_id: str,
        parameters: dict[str, Any],
        **kwargs,
    ) -> tuple[ToolResponse, float, dict]:
        del instance_id
        agent_data = kwargs.get("agent_data")
        default_task = _default_task_from_agent(agent_data)
        default_task.pop("testbench_cpp", None)
        task = task_from_tool_parameters(parameters or {}, default_task)
        code = str((parameters or {}).get("code") or "")
        if not code.strip():
            return ToolResponse(text=f"HDL_TOOL_RESULT {{\"phase\": \"{self.phase}\", \"score\": -0.1}}\nNo code was provided."), 0.0, {}

        result = compute_hdl_score_for_phase(
            solution_str=code,
            ground_truth=task,
            extra_info=getattr(agent_data, "extra_info", {}) if agent_data is not None else {},
            phase=self.phase,
            env_sh=task.get("env_sh") or self.env_sh,
            timeout=int(task.get("timeout") or self.timeout),
            feedback_max_chars=int(task.get("feedback_max_chars") or self.feedback_max_chars),
            keep_work=self.keep_work,
        )
        score = float(result.get("score", 0.0))
        return ToolResponse(text=_format_tool_result(self.phase, result)), score, result

    async def release(self, instance_id: str, **kwargs) -> None:
        self._instance_dict.pop(instance_id, None)


class CompileHDLTool(_HDLBaseTool):
    phase = "compile"


class SimulateHDLTool(_HDLBaseTool):
    phase = "simulate"


class SynthesizeHDLTool(_HDLBaseTool):
    phase = "synthesize"
