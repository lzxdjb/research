"""
verl/tools/stock_external_tools.py

Thin verl BaseTool wrappers around the three external Hithink API tools:
  - FinQueryTool   (FinQuery)
  - SearchTool     (Search)
  - TickerChartTool (TickerChart)

Each tool:
  * create()   — lightweight; just allocates an instance slot (no server state needed)
  * execute()  — calls the Hithink chain API and returns a ToolResponse
  * release()  — frees the slot
  * calc_reward() — always 0.0 (these are information tools, not reward tools)

Image handling for TickerChart
-------------------------------
The Hithink API returns a URL pointing to a PNG chart.  We download it,
convert to a PIL Image, and return it inside ToolResponse.image so the
agent loop can inject it as a multi-modal prompt token for the VLM.
The image is also written to a local cache directory for debugging.
"""

from __future__ import annotations
import asyncio  # Ensure this is imported at the top of your file
import json
import logging
import os
import re
import time
from io import BytesIO
from typing import Any, Optional
from uuid import uuid4

import requests
from PIL import Image

from verl.tools.base_tool import BaseTool, ToolResponse
from verl.tools.schemas import OpenAIFunctionToolSchema
from verl.utils.rollout_trace import rollout_trace_op

logger = logging.getLogger(__name__)
import uuid

# ---------------------------------------------------------------------------
# Shared HTTP helper (mirrors the original request_chain function)
# ---------------------------------------------------------------------------

import os

env = os.getenv("env")  # read environment variable

if env == "fuji":
    print("fujifjifjifjifji!!!")
    _LANGCHAIN_SERVER_URL = "http://190.92.231.77:880/iwencai/dialog/chain/execute"
else:
    _LANGCHAIN_SERVER_URL = "http://122.224.107.233:880/iwencai/dialog/chain/execute"
trace_id = str(uuid.uuid4())
user_id = f"user_{uuid.uuid4().hex[:8]}"
session_id = f"session_{uuid.uuid4().hex}"
_LANGCHAIN_HEADERS = {
    "Host": "aime-langchain-engine-server",
    "X-Arsenal-Auth": "aime-reinforcement-learning-environment-access",
    "Content-Type": "application/json",
    "X-Trace-Id": trace_id,
    "X-User-Id": user_id,
    "X-Session-Id": session_id,
}
_DEFAULT_TIMEOUT = 20
MAX_RETRIES = 5

def _request_chain(payload: dict, timeout: int = _DEFAULT_TIMEOUT) -> dict:
    """POST to the Hithink chain API and return the parsed JSON response."""
    resp = requests.post(
        _LANGCHAIN_SERVER_URL,
        headers=_LANGCHAIN_HEADERS,
        json=payload,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Shared parse helper for Search raw_data
# ---------------------------------------------------------------------------

def _parse_search_raw(query: str, raw_data: list[dict]) -> str:
    """
    Format the raw_data list returned by the Search API into a readable string.
    Each item is expected to have at least a 'title' and optionally 'abstract'/'url'.
    """
    lines = [f"搜索问句: {query}"]
    for item in raw_data[:10]:   # cap at 10 results to stay token-budget friendly
        title    = item.get("title", "")
        abstract = item.get("abstract", item.get("summary", ""))
        url      = item.get("url", item.get("link", ""))
        entry = f"标题: {title}"
        if abstract:
            entry += f"\n摘要: {abstract}"
        if url:
            entry += f"\n链接: {url}"
        lines.append(entry)
    return "\n\n".join(lines)


# ===========================================================================
# FinQueryTool
# ===========================================================================

class FinQueryTool(BaseTool):
    """Wraps the FinQuery chain — returns tabular financial data as text."""

    def __init__(self, config: dict, tool_schema: OpenAIFunctionToolSchema):
        super().__init__(config, tool_schema)
        self._instance_dict: dict[str, dict] = {}

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
        query = parameters.get("query", "")
        if not query:
            return ToolResponse(text="FinQuery工具：查询内容为空。"), 0.0, {}

        max_retries = MAX_RETRIES
        last_exception = None

        for attempt in range(max_retries):
            try:
                payload = {"chain_name": "FinQuery", "req_type": "nostream", "human_message": query}
                
                # If _request_chain is a blocking/synchronous call, consider 
                # wrapping it in a thread pool, but for now, we'll keep your structure:
                data = _request_chain(payload)
                
                response = data.get("response", {})
                results = response.get("result", []) if response else []

                if not results:
                    return ToolResponse(text=f"FinQuery工具，输入'{query}'，未返回结果。"), 0.0, {}

                text = results[0].get("text", "")
                result_text = f"取数问句: {query}\n取数结果:\n{text}"
                
                found_data = "找到0条数据" not in text and text.strip()
                tool_reward = 0.1 if found_data else 0.0
                
                # If successful, return immediately
                return ToolResponse(text=result_text), tool_reward, {}

            except Exception as e:
                last_exception = e
                logger.warning(f"FinQueryTool attempt {attempt + 1} failed: {e}")
                
                # Wait a bit before retrying (optional but recommended)
                if attempt < max_retries - 1:
                    await asyncio.sleep(10) 

        # If all retries fail
        result_text = f"FinQuery工具调用失败 (已重试{max_retries}次): {last_exception}"
        return ToolResponse(text=result_text), 0.0, {}


    async def calc_reward(self, instance_id: str, **kwargs) -> float:
        return 0.0

    async def release(self, instance_id: str, **kwargs) -> None:
        self._instance_dict.pop(instance_id, None)


# ===========================================================================
# SearchTool
# ===========================================================================

class SearchTool(BaseTool):
    """Wraps the Search chain — returns news / knowledge snippets as text."""

    # Brief sleep between back-to-back Search calls to avoid rate limits
    _INTER_CALL_SLEEP_S = 1.0

    def __init__(self, config: dict, tool_schema: OpenAIFunctionToolSchema):
        super().__init__(config, tool_schema)
        self._instance_dict: dict[str, dict] = {}
        self._last_call_ts: float = 0.0

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
        query = parameters.get("query", "")
        if not query:
            return ToolResponse(text="Search工具：查询内容为空。"), 0.0, {}

        # Light rate-limit guard (synchronous sleep acceptable here because
        # the event loop dispatches tool calls concurrently anyway)
        now = time.monotonic()
        elapsed = now - self._last_call_ts
        if elapsed < self._INTER_CALL_SLEEP_S:
            time.sleep(self._INTER_CALL_SLEEP_S - elapsed)
        self._last_call_ts = time.monotonic()

        try:
            payload = {
                "chain_name": "Search",
                "req_type": "nostream",
                "events": [{"event_name": "deep_research", "event_type": "user_input"}],
                "human_message": query,
            }
            data     = _request_chain(payload)
            response = data.get("response", {})
            results  = response.get("result", []) if response else []

            if not results:
                return ToolResponse(text=f"Search工具，输入'{query}'，未返回结果。"), 0.0, {}

            raw_data = results[0].get("raw_data", [])
            if raw_data:
                result_text = _parse_search_raw(query, raw_data)
            else:
                # Fallback: use the 'text' field directly
                result_text = f"搜索问句: {query}\n{results[0].get('text', '无结果')}"
        except Exception as e:
            logger.warning(f"SearchTool error: {e}")
            result_text = f"Search工具调用失败: {e}"

        return ToolResponse(text=result_text), 0.0, {}

    async def calc_reward(self, instance_id: str, **kwargs) -> float:
        return 0.0

    async def release(self, instance_id: str, **kwargs) -> None:
        self._instance_dict.pop(instance_id, None)


# ===========================================================================
# TickerChartTool
# ===========================================================================

class TickerChartTool(BaseTool):
    """
    Wraps the TickerChart chain.

    Fetches a K-line chart PNG from the Hithink API, caches it locally,
    and returns it as a PIL Image inside ToolResponse.image so the VLM
    can directly compare it with the user's uploaded screenshot.
    """

    def __init__(self, config: dict, tool_schema: OpenAIFunctionToolSchema):
        super().__init__(config, tool_schema)
        self._instance_dict: dict[str, dict] = {}
        # Local directory for caching fetched chart images
        self._images_dir: str = config.get("images_dir", "/tmp/verl_stock_charts")
        os.makedirs(self._images_dir, exist_ok=True)

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
        code_name  = parameters.get("codeName", "")
        chart_type = parameters.get("chartType", "Daily Candlestick")
        start_date = parameters.get("startDate", "")
        end_date   = parameters.get("endDate", "")
        indicator  = parameters.get("indicator", ["MA"])

        if not code_name or not start_date or not end_date:
            return (
                ToolResponse(text="TickerChart工具：缺少必填参数 codeName / startDate / endDate。"),
                0.0,
                {},
            )

        query_dict = {
            "startDate": start_date,
            "endDate":   end_date,
            "codeName":  code_name,
            "chartType": chart_type,
            "indicator": indicator,
        }
        query_str = json.dumps(query_dict, ensure_ascii=False, separators=(",", ":"))

        payload = {
            "chain_name":  "TickerChart",
            "req_type":    "nostream",
            "user_id":     "125",
            "session_id":  "143",
            "question_id": "143",
            "trace_id":    "1746001144320",
            "debug":       False,
            "source":      "aicubes_agent_77",
            "human_message": query_str,
            "question":      query_str,
            "stream":        False,
        }

        max_retries = MAX_RETRIES
        for attempt in range(max_retries):
            try:
                # 1. Request the chain for the URL
                data = _request_chain(payload)
                response = data.get("response")
                if not response:
                    raise Exception("调用失败（无响应）")

                results = response.get("result", [])
                if not results:
                    raise Exception("调用失败（无结果）")

                result_data = results[0]
                media_info  = result_data.get("media_info", {})
                url         = media_info.get("url") if media_info else None

                if not url:
                    raise Exception(f"未返回图片URL（股票：{code_name}）")

                # 2. Download the chart image
                img_resp = requests.get(url, timeout=30)
                img_resp.raise_for_status()
                pil_image = Image.open(BytesIO(img_resp.content)).convert("RGB")

                # 3. Cache to disk
                filename = url.split("/")[-1]
                cache_path = os.path.join(self._images_dir, filename)
                pil_image.save(cache_path)
                
                logger.info(f"TickerChartTool: cached chart to {cache_path}")
                feedback_text = (
                    f"已获取 {code_name} 的{chart_type}图表（{start_date} ~ {end_date}），"
                    f"指标：{indicator}。请与用户截图进行对比分析。\n"
                    f"[DEBUG] 图表URL: {url}"
                )
                
                # Success: return results immediately
                return ToolResponse(text=feedback_text, image=[pil_image]), 0.0, {}

            except Exception as e:
                last_exception = e
                logger.warning(f"TickerChartTool attempt {attempt + 1} failed: {e}")
                
                # If we have more attempts, wait 1 second before trying again
                if attempt < max_retries - 1:
                    await asyncio.sleep(10)

        # If we reach here, all retries failed
        error_msg = f"TickerChart工具调用失败 (已重试{max_retries}次): {last_exception}"
        return ToolResponse(text=error_msg), 0.0, {}

    async def calc_reward(self, instance_id: str, **kwargs) -> float:
        return 0.0

    async def release(self, instance_id: str, **kwargs) -> None:
        self._instance_dict.pop(instance_id, None)