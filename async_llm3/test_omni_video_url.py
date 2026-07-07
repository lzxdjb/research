#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 qwen-omni 能接受哪种 video_url 格式。
依次测试：本地绝对路径、file://、127.0.0.1 http、内网IP http

运行（在 multiple_speaker 上级或把 multiple_speaker 加入 PYTHONPATH）:
  cd .../multiple_speaker && python -m async_llm.test_omni_video_url
"""

import sys
from pathlib import Path

_pkg_root = Path(__file__).resolve().parent.parent
if str(_pkg_root) not in sys.path:
    sys.path.insert(0, str(_pkg_root))

from async_llm.qwen import QWENOMNIClient
from loguru import logger

logger.remove()
logger.add(sink=sys.stderr, level="INFO")

VIDEO_REL = "video/Drawing.Closer.2024.2160p.NF.WEB-DL.DDP5.1.HDR.H/p2_shot_0695_char12_char2.mp4"
LOCAL_ABS = "/mnt/dataset/multi-speaker/" + VIDEO_REL
INNER_IP = "10.244.209.170"
HTTP_PORT = 19000

PROMPT = "请描述这个视频里的人物和对话内容，输出格式为：<think>开始时间-结束时间, 人物描述, \"ASR文本\"</think>"

# 构建 4 种 URL 候选
candidates = [
    ("local_abs_path", LOCAL_ABS),
    ("file_protocol", f"file://{LOCAL_ABS}"),
    ("http_127.0.0.1", f"http://127.0.0.1:{HTTP_PORT}/{VIDEO_REL}"),
    ("http_inner_ip", f"http://{INNER_IP}:{HTTP_PORT}/{VIDEO_REL}"),
]

client = QWENOMNIClient()
try:
    for name, url in candidates:
        print(f"\n{'='*60}")
        print(f"【测试格式】{name}")
        print(f"【URL】{url}")
        print(f"{'='*60}")

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "video_url", "video_url": {"url": url}},
                    {"type": "text", "text": PROMPT},
                ],
            }
        ]
        try:
            resp = client.get_gpt_response(messages, client.chat_url, temperature=0.2, try_num=1)
            text = resp.get("content", "") if isinstance(resp, dict) else str(resp)
            if text.startswith("error:") or not text.strip():
                print(f"结果: 失败")
                print(f"原始返回: {text[:200]}")
            else:
                print(f"结果: 成功 ✓")
                print(f"返回长度: {len(text)} chars")
                print(f"前 500 字符:\n{text[:500]}")
        except Exception as e:
            print(f"结果: 异常")
            print(f"异常信息: {e}")
finally:
    client.close()
