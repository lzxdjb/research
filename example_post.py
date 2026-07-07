# vllm_examples.py
# 统一调用函数，通过参数控制所有调用行为
# 依赖：pip install openai

from openai import OpenAI
from typing import Optional, Union
import json
import base64
import os

# ============================================================
# 客户端配置
# ============================================================
client = OpenAI(
    api_key="dummy",
    base_url="http://117.50.195.94:3430/v1" #wifi下访问这个url
    # base_url="http://interactive-h8uvtimmgmw3:18886/v1" #乌兰察布的dsw和任务式建模访问这个url
    # base_url="http://0.0.0.0:18886/v1"
)
MODEL = "/mnt/model/qwen_3_6_27B"


# ============================================================
# 统一调用函数
# ============================================================
def call_llm(
    # --- 必填 ---
    user_prompt: str,

    # --- 消息配置 ---
    system_prompt: str = "你是一个助手。",
    history: Optional[list] = None,        # 多轮历史，格式：[{"role": "user", "content": "..."}, ...]

    # --- 模型参数 ---
    model: str = MODEL,
    temperature: float = 0.7,
    max_tokens: int = 2048,
    top_p: float = 1.0,

    # --- 功能开关 ---
    stream: bool = False,                  # 是否流式输出
    enable_thinking: bool = True,          # 是否开启 think（Qwen3 等支持）

    # --- 工具配置 ---
    tools: Optional[list] = None,          # 工具列表
    tool_choice: str = "auto",             # "auto" | "none" | "required"
    tool_executor=None,                    # 工具执行函数 fn(name, args) -> str，为 None 时打印工具调用并返回 mock

    # --- 图片配置 ---
    images: Optional[Union[str, list]] = None,
    # 支持三种格式（可混合放进列表）：
    #   - 本地文件路径："/path/to/image.jpg"
    #   - 网络 URL：   "https://example.com/img.png"
    #   - base64 字符串："data:image/jpeg;base64,/9j/..."
) -> str:
    """
    统一 vLLM 调用函数，支持：
    - 流式 / 非流式
    - 开启 / 关闭 thinking
    - 带工具 / 不带工具（自动处理多轮 tool_call）
    - 多轮历史
    - 单张 / 多张图片（本地路径 / URL / base64）
    返回最终的 assistant 文本内容。
    """

    # 构建消息列表
    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)

    # 构建 user content（纯文本 or 图文混合）
    if images:
        image_list = [images] if isinstance(images, str) else images
        content = []
        for img in image_list:
            content.append({"type": "image_url", "image_url": {"url": _to_image_url(img)}})
        content.append({"type": "text", "text": user_prompt})
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": user_prompt})

    # 公共请求参数
    kwargs = dict(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        top_p=top_p,
        stream=stream,
        extra_body={"chat_template_kwargs": {"enable_thinking": enable_thinking}},
    )
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice

    # ---- 流式调用 ----
    if stream:
        return _call_stream(kwargs, tools, tool_executor)

    # ---- 非流式调用 ----
    return _call_sync(kwargs, tools, tool_executor)


# ============================================================
# 内部：图片格式转换
# ============================================================
def _to_image_url(img: str) -> str:
    """
    统一将图片转为 data URL 或直接返回网络 URL。
    - 本地路径 -> 读取文件并 base64 编码
    - http/https URL -> 直接返回
    - 已是 data:image/... -> 直接返回
    """
    if img.startswith("http://") or img.startswith("https://"):
        return img
    if img.startswith("data:image/"):
        return img
    # 本地文件路径
    ext = os.path.splitext(img)[-1].lower().lstrip(".")
    mime_map = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "gif": "gif", "webp": "webp"}
    mime = f"image/{mime_map.get(ext, 'jpeg')}"
    with open(img, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


# ============================================================
# 内部：非流式
# ============================================================
def _call_sync(kwargs: dict, tools, tool_executor) -> str:
    resp = client.chat.completions.create(**kwargs)
    msg = resp.choices[0].message

    # 无工具或模型直接回答
    if not tools or resp.choices[0].finish_reason != "tool_calls":
        return msg.content or ""

    # 处理工具调用（可能多轮）
    messages = kwargs["messages"]
    while resp.choices[0].finish_reason == "tool_calls":
        messages.append(msg)

        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments)
            print(f"[Tool Call] {tc.function.name}({args})")

            result = (
                tool_executor(tc.function.name, args)
                if tool_executor
                else f"[mock result for {tc.function.name}]"
            )
            print(f"[Tool Result] {result}")

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": str(result)
            })

        kwargs["messages"] = messages
        resp = client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message

    return msg.content or ""


# ============================================================
# 内部：流式
# ============================================================
def _call_stream(kwargs: dict, tools, tool_executor) -> str:
    stream = client.chat.completions.create(**kwargs)

    tool_calls_buf = {}
    finish_reason = None
    full_content = []

    for chunk in stream:
        choice = chunk.choices[0]
        finish_reason = choice.finish_reason or finish_reason
        delta = choice.delta

        if delta.content:
            print(delta.content, end="", flush=True)
            full_content.append(delta.content)

        if delta.tool_calls:
            for tc in delta.tool_calls:
                idx = tc.index
                if idx not in tool_calls_buf:
                    tool_calls_buf[idx] = {"id": "", "name": "", "arguments": ""}
                if tc.id:
                    tool_calls_buf[idx]["id"] = tc.id
                if tc.function.name:
                    tool_calls_buf[idx]["name"] += tc.function.name
                if tc.function.arguments:
                    tool_calls_buf[idx]["arguments"] += tc.function.arguments

    if full_content:
        print()  # 换行

    # 无工具调用，直接返回
    if finish_reason != "tool_calls" or not tool_calls_buf:
        return "".join(full_content)

    # 处理工具调用后，第二轮用非流式返回最终答案
    messages = kwargs["messages"]
    messages.append({
        "role": "assistant",
        "tool_calls": [
            {
                "id": tc["id"],
                "type": "function",
                "function": {"name": tc["name"], "arguments": tc["arguments"]}
            }
            for tc in tool_calls_buf.values()
        ]
    })

    for tc in tool_calls_buf.values():
        args = json.loads(tc["arguments"])
        print(f"[Tool Call] {tc['name']}({args})")

        result = (
            tool_executor(tc["name"], args)
            if tool_executor
            else f"[mock result for {tc['name']}]"
        )
        print(f"[Tool Result] {result}")

        messages.append({
            "role": "tool",
            "tool_call_id": tc["id"],
            "content": str(result)
        })

    # 最终回答流式输出
    kwargs["messages"] = messages
    kwargs.pop("tools", None)
    final_stream = client.chat.completions.create(**kwargs)
    final_content = []
    for chunk in final_stream:
        delta = chunk.choices[0].delta.content
        if delta:
            print(delta, end="", flush=True)
            final_content.append(delta)
    print()

    return "".join(final_content)


# ============================================================
# 示例调用
# ============================================================
if __name__ == "__main__":

    # 1. 普通非流式
    print("\n=== 1. 普通非流式 ===")
    result = call_llm(
        user_prompt="用一句话介绍Python。",
        stream=False,
        max_tokens=4096,
    )
    print(result)

    # 2. 流式
    print("\n=== 2. 流式 ===")
    call_llm(
        user_prompt="给我讲个冷笑话。",
        stream=True,
        temperature=0.8,
        max_tokens=4096,
    )

    # 3. 关闭 think，非流式
    print("\n=== 3. 关闭 think（非流式）===")
    result = call_llm(
        user_prompt="1+1等于几？",
        enable_thinking=False,
        stream=False,
        max_tokens=4096,
    )
    print(result)

    # 4. 关闭 think，流式
    print("\n=== 4. 关闭 think（流式）===")
    call_llm(
        user_prompt="简单介绍一下机器学习。",
        enable_thinking=False,
        stream=True,
        max_tokens=4096,
    )

    # 5. 带工具，非流式（mock 执行器）
    print("\n=== 5. 工具调用（非流式）===")

    weather_tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "获取指定城市的天气信息",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string", "description": "城市名称"},
                        "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]}
                    },
                    "required": ["city"]
                }
            }
        }
    ]

    def my_tool_executor(name, args):
        if name == "get_weather":
            return json.dumps({"city": args["city"], "temperature": "25°C", "condition": "晴天"}, ensure_ascii=False)
        return "unknown tool"

    result = call_llm(
        user_prompt="北京今天天气怎么样？",
        tools=weather_tools,
        tool_executor=my_tool_executor,
        stream=False,
        max_tokens=4096,
    )
    print(result)

    # 6. 带工具，流式
    print("\n=== 6. 工具调用（流式）===")
    call_llm(
        user_prompt="上海今天天气怎么样？",
        tools=weather_tools,
        tool_executor=my_tool_executor,
        stream=True,
        max_tokens=4096,
    )

    # 7. 多轮对话
    print("\n=== 7. 多轮对话 ===")
    history = [
        {"role": "user",      "content": "我叫小明。"},
        {"role": "assistant", "content": "你好，小明！有什么可以帮你的？"},
    ]
    result = call_llm(
        user_prompt="你还记得我叫什么吗？",
        history=history,
        stream=False,
        max_tokens=4096,
    )
    print(result)

    # 8. 传入图片（URL），非流式
    print("\n=== 8. 图片理解（URL，非流式）===")
    result = call_llm(
        user_prompt="请描述这张图片的内容。",
        images="https://ai.iwencai.com/userinfo-model-image-q-a/684cd2ef1dbe4d478c9e4a09073d2cf8.png",
        stream=False,
        enable_thinking=False,
        max_tokens=4096,
    )
    print(result)

    # 9. 传入图片（本地路径），流式
    print("\n=== 9. 图片理解（本地路径，流式）===")
    # 将 "/your/local/image.jpg" 替换为实际路径
    call_llm(
        user_prompt="图片里有什么文字？请帮我识别出来。",
        images="/your/local/image.jpg",
        stream=True,
        enable_thinking=False,
        max_tokens=4096,
    )

    # 10. 传入多张图片（混合 URL + 本地路径），非流式
    print("\n=== 10. 多图理解（混合来源，非流式）===")
    result = call_llm(
        user_prompt="请比较这两张图片的异同。",
        images=[
            "https://ai.iwencai.com/userinfo-model-image-q-a/684cd2ef1dbe4d478c9e4a09073d2cf8.png",
            "/mnt/HithinkOmniSSD/user_workspace/zhangrongjunchen/omni_robot_test/images/1.jpg",   # 替换为本地图片路径
        ],
        stream=False,
        enable_thinking=False,
        max_tokens=4096,
    )
    print(result)

    # 11. 传入 base64 图片，非流式
    print("\n=== 11. 图片理解（base64，非流式）===")
    # 手动读取文件转 base64
    with open("/your/local/image.jpg", "rb") as f:
        b64_str = "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()
    result = call_llm(
        user_prompt="这张图片里有几个人？",
        images=b64_str,
        stream=False,
        enable_thinking=False,
        max_tokens=4096,
    )
    print(result)
