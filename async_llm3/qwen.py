"""
同步网关适配：基于同目录 `client.py` 中 `QWEN_3_5OMNIClient` / `AsyncLLMClient`（异步 + aiohttp），
为 `build_unified_sft_pipeline.py` 等脚本提供

  - QWENOMNIClient（含 video2text、get_gpt_response）
  - QWEN3_6_Client（纯文本合并 / QA，默认可用环境变量 QWEN_36_MODEL 覆盖模型名）
  - get_token_usage_summary / reset_token_usage_stats（与 SFT_QA_data/RL_pipeline/qwen 同口径的按 family 累计；依赖网关 `usage` 字段）

**说明**：`RL_pipeline/qwen.py` 为同网关逻辑的参考副本；本流水线**必须**用本目录的 `qwen`（`sys.path` 中 `async_llm` 须在 `RL_pipeline` 之前），勿让另一份 `qwen` 抢载。

`from qwen import ...` 由 `build_unified_sft_pipeline.py` 将 `SFT_QA_data/async_llm` 加入 sys.path 后使用。
"""
from __future__ import annotations

import asyncio
import base64
import os
import subprocess
import tempfile
import threading
import traceback
from typing import Any, Optional
from urllib.parse import unquote, urlparse

import sys
from pathlib import Path

# 供 `import qwen`（把 async_llm 加入 path）与 `from async_llm import qwen` 两种加载方式
_PKG = Path(__file__).resolve().parent
if str(_PKG) not in sys.path:
    sys.path.insert(0, str(_PKG))
from loguru import logger
from client import AsyncLLMClient, QWEN_3_5OMNIClient

# 同一线程内复用事件循环，避免多次 asyncio.run 导致 aiohttp ClientSession 失效
_tls = threading.local()

# 按 family 累计 token（与 RL_pipeline/qwen 一致）；线程安全
_token_stats_lock = threading.Lock()
_token_stats_by_family: dict[str, dict[str, int]] = {}


def _accumulate_token_usage(family: str, usage: dict) -> None:
    if not family or not isinstance(usage, dict):
        return
    try:
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        tt = int(usage.get("total_tokens") or 0)
    except (TypeError, ValueError):
        return
    with _token_stats_lock:
        if family not in _token_stats_by_family:
            _token_stats_by_family[family] = {
                "api_calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            }
        d = _token_stats_by_family[family]
        d["api_calls"] += 1
        d["prompt_tokens"] += pt
        d["completion_tokens"] += ct
        d["total_tokens"] += tt


def _thread_loop() -> asyncio.AbstractEventLoop:
    loop = getattr(_tls, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        _tls.loop = loop
    return loop


def _run_async(coro: Any) -> Any:
    loop = _thread_loop()
    asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def get_token_usage_summary() -> dict[str, Any]:
    with _token_stats_lock:
        return {k: dict(v) for k, v in _token_stats_by_family.items()}


def reset_token_usage_stats() -> None:
    with _token_stats_lock:
        _token_stats_by_family.clear()


def _merge_message_result(choices0: dict) -> Any:
    """与 client.AsyncLLMClient.get_gpt_response 返回习惯对齐。"""
    msg = choices0.get("message")
    if not isinstance(msg, dict):
        return msg
    if "reasoning_content" in msg:
        return msg
    return msg.get("content", msg)


# 进程内所有 QWENOMNIClient（含 QWEN3_6_Client），供进程退出前 close aiohttp 会话
_OMNI_CLIENT_INSTANCES: list[Any] = []


def close_all_omni_clients() -> None:
    for c in _OMNI_CLIENT_INSTANCES:
        try:
            c.close()
        except Exception:
            pass
    _OMNI_CLIENT_INSTANCES.clear()


def _local_path_from_video_arg(video_path: str) -> str:
    s = (video_path or "").strip()
    if s.startswith("file://"):
        return unquote(urlparse(s).path)
    return os.path.abspath(os.path.expanduser(s))


class QWENOMNIClient(QWEN_3_5OMNIClient):
    """qwen3.5-omni 系列；`model=...` 可覆盖为 qwen3.5-omni-plus / flash 等。"""

    @staticmethod
    def _compress_video_for_omni(
        input_path: str,
        output_path: str,
        fps: int = 2,
        resolution: str = "360",
        video_bitrate: str = "350k",
        audio_bitrate: str = "24k",
    ) -> bool:
        """降低 fps/分辨率/码率，与同仓库 RL_pipeline/qwen.QWENOMNIClient._compress_video 对齐。"""
        attempts = [
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-threads",
                "1",
                "-i",
                input_path,
                "-map_metadata",
                "-1",
                "-map_chapters",
                "-1",
                "-vf",
                f"fps={fps},scale=-2:{resolution}",
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "32",
                "-maxrate",
                video_bitrate,
                "-bufsize",
                "700k",
                "-c:a",
                "aac",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-b:a",
                audio_bitrate,
                "-movflags",
                "+faststart",
                output_path,
            ],
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-threads",
                "1",
                "-i",
                input_path,
                "-map_metadata",
                "-1",
                "-map_chapters",
                "-1",
                "-vf",
                f"fps={max(1, min(int(fps), 1))},scale=-2:256",
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "36",
                "-maxrate",
                "220k",
                "-bufsize",
                "440k",
                "-c:a",
                "aac",
                "-ac",
                "1",
                "-ar",
                "12000",
                "-b:a",
                "16k",
                "-movflags",
                "+faststart",
                output_path,
            ],
        ]
        for idx, cmd in enumerate(attempts, start=1):
            try:
                cp = subprocess.run(cmd, check=True, capture_output=True, timeout=180)
                stderr = (cp.stderr or b"").decode("utf-8", errors="ignore").strip()
                if stderr:
                    logger.info(
                        "ffmpeg compress attempt %s stderr for %s: %s",
                        idx,
                        input_path,
                        stderr[:800],
                    )
                return True
            except subprocess.CalledProcessError as e:
                stderr = (e.stderr or b"").decode("utf-8", errors="ignore").strip()
                logger.warning(
                    "ffmpeg compress attempt %s failed for %s: returncode=%s stderr=%s",
                    idx,
                    input_path,
                    e.returncode,
                    (stderr[:1200] or "<empty>"),
                )
            except subprocess.TimeoutExpired as e:
                stderr = (e.stderr or b"").decode("utf-8", errors="ignore").strip()
                logger.warning(
                    "ffmpeg compress attempt %s timed out for %s: timeout=%ss stderr=%s",
                    idx,
                    input_path,
                    e.timeout,
                    (stderr[:1200] or "<empty>"),
                )
            except Exception as e:
                logger.warning("ffmpeg compress attempt %s errored for %s: %s", idx, input_path, e)
        return False

    @staticmethod
    def _encode_local_video_data_uri(video_path: str) -> str:
        with open(video_path, "rb") as f:
            data = f.read()
        b64 = base64.b64encode(data).decode("utf-8")
        return f"data:video/mp4;base64,{b64}"

    def __init__(self, model: Optional[str] = None) -> None:
        if model is not None and str(model).strip():
            super().__init__(model=str(model).strip())
        else:
            super().__init__()
        _OMNI_CLIENT_INSTANCES.append(self)

    async def get_gpt_response_async(
        self,
        messages: list,
        chat_url: str,
        temperature: float = 0,
        try_num: int = 10,
        pbar: Any = None,
        extra_body: Optional[dict] = None,
        usage_family: Optional[str] = None,
    ) -> Any:
        if self.session is None:
            await self.initialize()
        error_messages = ""
        try:
            assert isinstance(messages, list) and not [
                x for x in messages if x.get("role") not in {"system", "user", "assistant"}
            ]
            chat_d: dict = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
            }
            if extra_body:
                chat_d.update(extra_body)
            if not self.authority:
                await self.refresh_authority()
            chat_h = {
                "Content-Type": "application/json",
                "userId": self.authority["data"]["user_id"],
                "token": self.authority["data"]["token"],
            }
        except Exception:
            error_messages = traceback.format_exc()
            return "error: " + error_messages

        last_bad_reply = ""
        for _try_id in range(try_num):
            try:
                async with self.session.post(
                    chat_url, json=chat_d, headers=chat_h, timeout=self.timeout
                ) as response:
                    try:
                        response_data = await response.json()
                    except Exception:
                        error_messages = traceback.format_exc()
                        last_bad_reply = f"response.json failed: {error_messages[:1200]}"
                        await self.refresh_authority()
                        continue
                    choices = self.find_key_in_nested_dict(response_data, "choices")
                    if "success" in response_data and not response_data.get("success"):
                        last_bad_reply = f"success=false: {str(response_data)[:1200]}"
                        continue
                    if choices is None:
                        last_bad_reply = f"no choices: {str(response_data)[:1200]}"
                        continue
                    if pbar:
                        pbar.update(1)
                    usage = response_data.get("usage")
                    if usage and isinstance(usage, dict):
                        pt = usage.get("prompt_tokens", "N/A")
                        ct = usage.get("completion_tokens", "N/A")
                        tt = usage.get("total_tokens", "N/A")
                        if usage_family:
                            logger.info(
                                f"[token] family={usage_family} | gateway_model={self.model} | "
                                f"prompt_tokens={pt} | completion_tokens={ct} | total_tokens={tt}"
                            )
                            _accumulate_token_usage(usage_family, usage)
                        else:
                            logger.info(
                                f"API usage: prompt_tokens={pt}, "
                                f"completion_tokens={ct}, total_tokens={tt}"
                            )
                    return _merge_message_result(choices[0])
            except Exception:
                error_messages = traceback.format_exc()
                last_bad_reply = error_messages[:1200] or last_bad_reply
                await self.refresh_authority()
        detail = (str(error_messages) if error_messages else "") or last_bad_reply or "all retries failed"
        return "error: " + detail

    def get_gpt_response(
        self,
        messages: list,
        chat_url: str,
        temperature: float = 0,
        try_num: int = 10,
        pbar: Any = None,
        extra_body: Optional[dict] = None,
        usage_family: Optional[str] = None,
    ) -> Any:
        return _run_async(
            self.get_gpt_response_async(
                messages,
                chat_url,
                temperature,
                try_num,
                pbar,
                extra_body,
                usage_family,
            )
        )

    def video2text(
        self,
        video_path: str,
        prompt: str,
        temperature: float = 0.2,
        try_num: int = 8,
        usage_family: Optional[str] = None,
    ) -> Any:
        """
        远程网关无法拉取本机 file:// URL，会报 InvalidParameter「URL does not appear to be valid」。
        对本地路径改为 data:video/mp4;base64,...（默认先 ffmpeg 压缩以控制体积），与 RL_pipeline/qwen 一致。

        环境变量：QWEN_OMNI_VIDEO_COMPRESS=0 关闭压缩；QWEN_OMNI_VIDEO_FPS=2；QWEN_MAX_DATA_URI_BYTES 默认 20MiB。
        """
        s = (video_path or "").strip()
        extra_body: Optional[dict] = None

        if s.startswith("http://") or s.startswith("https://"):
            url = s
        elif s.startswith("data:"):
            url = s
        else:
            ap = _local_path_from_video_arg(s)
            if not os.path.isfile(ap):
                return "error: local video not found: " + ap

            compress = os.getenv("QWEN_OMNI_VIDEO_COMPRESS", "1").strip().lower() not in (
                "0",
                "false",
                "no",
            )
            fps = int(os.getenv("QWEN_OMNI_VIDEO_FPS", "2"))
            size_limit = int(os.getenv("QWEN_MAX_DATA_URI_BYTES", str(20 * 1024 * 1024)))

            actual_path = ap
            tmp_path: Optional[str] = None
            try:
                if compress:
                    fd, tmp_path = tempfile.mkstemp(suffix=".mp4")
                    os.close(fd)
                    ok = self._compress_video_for_omni(ap, tmp_path, fps=fps)
                    if ok and os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                        actual_path = tmp_path
                    else:
                        if tmp_path and os.path.exists(tmp_path):
                            os.remove(tmp_path)
                        tmp_path = None
                        logger.warning(
                            "QWEN_OMNI: ffmpeg compress failed or empty, using original: %s",
                            ap,
                        )

                sz = os.path.getsize(actual_path)
                if sz > size_limit:
                    return (
                        "error: video too large for data URI "
                        f"(bytes={sz} limit={size_limit}); "
                        "install ffmpeg and keep QWEN_OMNI_VIDEO_COMPRESS=1, "
                        "or raise QWEN_MAX_DATA_URI_BYTES"
                    )

                url = self._encode_local_video_data_uri(actual_path)
                extra_body = {"fps": fps}
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "video_url", "video_url": {"url": url}},
                    {"type": "text", "text": prompt},
                ],
            }
        ]
        return self.get_gpt_response(
            messages,
            self.chat_url,
            temperature=temperature,
            try_num=try_num,
            extra_body=extra_body,
            usage_family=usage_family,
        )

    def close(self) -> None:
        try:
            _run_async(AsyncLLMClient.close(self))
        except Exception:
            pass


class QWEN3_6_Client(QWENOMNIClient):
    """Qwen3.6 文本；默认模型可用环境变量 QWEN_36_MODEL 覆盖。"""

    def __init__(self, model: Optional[str] = None) -> None:
        m = model or (os.environ.get("QWEN_36_MODEL") or "").strip()
        if not m:
            m = "qwen3-vl-235b-a22b-thinking"
        super().__init__(model=m)


# 与历史 __all__ 对齐
LLMClient = AsyncLLMClient
QWENClient = QWEN_3_5OMNIClient

__all__ = [
    "LLMClient",
    "QWENClient",
    "QWENOMNIClient",
    "QWEN3_6_Client",
    "get_token_usage_summary",
    "reset_token_usage_stats",
    "close_all_omni_clients",
]
