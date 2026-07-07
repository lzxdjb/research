"""VERL reward-function entry point for HDL generation tasks."""

from __future__ import annotations

from typing import Any

from recipe.hdl_agent.hdl_judge import compute_hdl_score


def compute_score(
    data_source: str | None = None,
    solution_str: str | None = None,
    ground_truth: Any | None = None,
    extra_info: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    return compute_hdl_score(
        data_source=data_source,
        solution_str=solution_str,
        ground_truth=ground_truth,
        extra_info=extra_info,
        **kwargs,
    )

