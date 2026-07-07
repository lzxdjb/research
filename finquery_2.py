# import gradio as gr
import os
import json
import sys
import re
import copy
import uuid
import queue
import threading
import time
import datetime
import asyncio
import numpy as np
import torch
import tempfile
# import ujson as json

import aiohttp
import subprocess

import atexit
import glob
import asyncio
import aiohttp

async def execute_action(session, action):
    """异步执行单个API动作"""
    import os

    env = os.getenv("env")  # read environment variable

    if env == "fuji":
        print("fuji")
        url = "http://190.92.231.77:880/iwencai/dialog/chain/execute"
    else:
        url = "http://122.224.107.233:880/iwencai/dialog/chain/execute"
        headers = {
            "Host": "aime-langchain-engine-server",
            "X-Arsenal-Auth": "aime-reinforcement-learning-environment-access",
            "Content-Type": "application/json"
    }
    
    chain_name = action[0]
    
    payload = {
        "chain_name": chain_name,
        "req_type": "nostream",
        "human_message": action[1],
        "debug": "false",
        "source": "ths_mobile_yuyinzhushou"
    }
    
    try:
        async with session.post(url, headers=headers, json=payload, timeout=60) as response:
            if response.status == 200:
                result_data = await response.json()
                return result_data.get('response', {}).get('result', [{}])[0].get('text', '')
            else:
                error_body = await response.text()
                print(f"API错误 {response.status}: {error_body}")
                return f"API返回错误: {response.status}, 原因: {error_body}"
    except aiohttp.ClientConnectorError as e:
        return f"连接错误: 检查IP是否通畅或端口是否开放. 原文: {repr(e)}"
    except aiohttp.ServerDisconnectedError as e:
        return f"服务器断开连接: 可能服务端崩溃了. 原文: {repr(e)}"
    except Exception as e:
        # 使用 repr 获取更多属性
        return f"未知异常类型: {type(e).__name__}, 内容: {repr(e)}"

async def main():
    """主异步函数"""
    async with aiohttp.ClientSession() as session:
        # action = ['CustomerServiceFAQ', '下周五是几号？']
        # action = ['FinQuery', '盛视科技 基本行情']
        # action = ['Search', '2025年 什么时候放假']
        # action = ['Search', '00和60开头的股票名称和代码']
        # action = ['Search', 'A股T+1的交易规则']
        # action = ['FinQuery', '近期出现强势技术形态（如突破平台、MACD金叉）且资金持续流入的股票池']
        # action = ['FinQueryOmni', '物流板块今天的整体涨跌幅']
        # action = ['FinQuery', '物流板块昨日的整体涨跌幅']
        # action = ['FinQueryOmni', '物流板块昨日的整体涨跌幅']

        # action = ['FinQuery', '彩电板块昨日的整体涨跌幅']
        # action = ['FinQueryOmni', '彩电板块昨日的整体涨跌幅']
        # action = ['FinQuery', '物流板块今天的整体涨跌幅']
        # action = ['SearchOmni', '2025年 什么时候放假']
        # action = ['Search', '2026年1月26日前5个交易日的日期']
        # action = ['Search', 'A股T+1的交易规则']
        # action = ['FinQueryOmni', '近期出现强势技术形态（如突破平台、MACD金叉）且资金持续流入的股票池']

        # action = ['FinQueryOmni', '最高价在19.54到19.56之间,最低价在11.80到11.90之间 2025年2月21日至2026年1月28日']
        # action = ['FinQueryOmni', '最高价在19.54到19.56之间,最低价在11.80到11.90之间 2025年2月21日至2026年1月28日']
        # action = ['FinQueryOmni', '最高价在19.54到19.56之间,最低价在11.80到11.90之间 2025年2月21日至2026年1月28日']
        # action = ['FinQueryOmni', '最高价在101。元左右 2025年12月20日至2025年12月30日']
        # action = ['FinQueryOmni', '2025年12月3日至2026年1月26日 最高价在101.45元左右,最低价在92.87元左右,2026年1月26日前5个交易日分别为阴阴阳阴阴']
        # action = ['FinQuery', '2025年12月3日至2026年1月26日最高价在100到102之间, 2025年12月3日至2026年1月26日最低价在90到95之间']
        # action = ['FinQuery', '2025年12月3日至2026年1月26日最高价在101左右, 2025年12月3日至2026年1月26日最低价在92左右']
        # action = ['FinQueryOmni', '2025年12月3日至2026年1月26日最高价在100到102之间,2025年12月3日至2026年1月26日最低价在90到95之间']
        # action = ['FinQueryOmni', '2025年12月3日至2026年1月26日最高价在100到102之间,2025年12月3日至2026年1月26日最低价在90到95之间']
        # action = ['FinQueryOmni', '最高价在100到102之间 2025年12月3日至2026年1月26日']
        # action = ['FinQueryOmni', '最高价在101左右 2025年12月3日至2026年1月26日']

        # action = ['FinQuery', '2025年12月3日至2026年1月26日最高价在100到102之间']
        # action = ['FinQueryOmni', '1月20日阴线，1月21阳线，1月22阴线，1月23阴线，1月26阴线，1月26日股价在92到93之间']
        # action = ['FinQueryOmni', '1月20日阴线，1月21阳线，1月22阴线，1月23阴线，1月26阴线']
        action = ['FinQueryOmni', '1月20日到1月26日为阴阳阴阴阴']
        # action = ['FinQueryOmni', '最高价在100到102之间,最低价在90到95之间 2025年12月3日至2026年1月26日']
        # action = ['FinQueryOmni', '2025年12月3日至2026年1月26日 最高价在100到102之间,最低价在90到95之间']

        # action = ['FinQueryOmni', '1月20日阴线，1月21阳线，1月22阴线，1月23阴线，1月26阴线，2025年12月3日到2026年1月26日最高价在100到102之间']

        ##之间比左右好
        ##具体时间的阴阳线 比近几天阴阳线是多少多少好
        ##最高价最低价 要是多少到多少之间 都需要给出多少到多少的时间范围

        # action = ['FinQuery', '最高价在101元左右 2025年12月至2026年1月']
        # action = ['FinQuery', '最高价是101.45 2025年12月20日至2025年12月30日']
        # action = ['FinQuery', '收盘价=92.87 换手率=0.17% 2026年1月26日']
        result = await execute_action(session, action)
        print("结果:", result)

if __name__ == "__main__":
    asyncio.run(main())