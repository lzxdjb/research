import json
import traceback
import requests
from tqdm import tqdm
import pandas as pd
import base64
from PIL import Image
from io import BytesIO
import yaml
from openai import OpenAI
from loguru import logger
import copy
import sys,os

sys.path.append(os.path.abspath(os.path.dirname(__file__)))

logger.remove()
logger.add(sink=sys.stderr, level="INFO")

class LLMClient:
    def __init__(self, app_id, app_secret, url, chat_url, model, timeout=60, max_workers=20):
        """
        初始化通用参数

        Args:
            app_id (str): 应用ID
            app_secret (str): 应用密钥
            url (str): 认证接口URL
            chat_url (str): 对话接口URL
            model (str): 使用的模型名称
            timeout (int): 请求超时时间，默认60秒
            max_workers (int): 最大并发数
        """
        self.app_id = app_id
        self.app_secret = app_secret
        self.url = url
        self.chat_url = chat_url
        self.model = model
        self.timeout = timeout
        self.max_workers = max_workers
        self.authority = None
        self.session = requests.Session()

    def close(self):
        """关闭session"""
        if self.session:
            self.session.close()

    @staticmethod
    def _encode_image(image_path):
        """
        将图片编码为base64格式
        
        Args:
            image_path (str): 图片的路径
            
        Returns:
            str: Base64编码的图片
        """
        pil_image = Image.open(image_path)
        img_byte_arr = BytesIO()
        pil_image.save(img_byte_arr, format=pil_image.format)
        return base64.b64encode(img_byte_arr.getvalue()).decode('utf-8')

    @staticmethod
    def find_key_in_nested_dict(nested_dict, target_key):
        """
        嵌套字典中寻找目标Key

        Args:
            nested_dict (dict):嵌套字典
            target_key(str):要寻找的目标Key
        Returns:
            Dict: 如果找到返回键所在的值，否则返回 None
        """
        if not isinstance(nested_dict, dict):
            return None

        stack = [nested_dict]
        while stack:
            current_dict = stack.pop()
            if target_key in current_dict:
                return current_dict[target_key]
            for value in current_dict.values():
                if isinstance(value, dict):
                    stack.append(value)
        return None

    @staticmethod
    def save_file(path, instruction, predict_result):
        with open(path, 'a', encoding='utf-8') as f: 
            tmp = instruction
            tmp['reasoning_content'] = predict_result['reasoning_content'] if isinstance(predict_result, dict) and 'reasoning_content' in predict_result else ""
            tmp['predict_result'] = predict_result['content'] if isinstance(predict_result, dict) and 'content' in predict_result else predict_result

            f.write(json.dumps(tmp, ensure_ascii=False) + "\n")

    def refresh_authority(self):
        """刷新身份信息"""
        d = {"app_id": self.app_id, "app_secret": self.app_secret}
        h = {"Content-Type": "application/json"}
        
        response = self.session.post(self.url, json=d, headers=h, timeout=self.timeout)
        data = response.json()
        if data.get("success"):
            self.authority = data
        else:
            raise Exception(f"Failed to authenticate: {data}")

    def get_gpt_response(self, messages, chat_url, temperature=0, try_num=3, pbar=None):
        """
        大模型调用和身份验证
        """
        error_messages = ""
        try:
            assert isinstance(messages, list) and not [x for x in messages if x["role"] not in {"system", "user", "assistant"}]
            chat_d = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature
            }
            if not self.authority:
                self.refresh_authority()
            chat_h = {
                "Content-Type": "application/json",
                "userId": self.authority["data"]["user_id"],
                "token": self.authority["data"]["token"]
            }
        except Exception as e:
            error_messages = traceback.format_exc()
            logger.error(error_messages)
            return "error: "+error_messages

        for try_id in range(try_num):
            try:
                response = self.session.post(chat_url, json=chat_d, headers=chat_h, timeout=self.timeout)
                response_data = response.json()
                choices = self.find_key_in_nested_dict(response_data, 'choices')
                if 'success' in response_data and not response_data.get('success'):
                    logger.error("Retry time:{}\n{}".format(try_id,response_data))
                    continue
                if choices is None:
                    logger.error("Retry time:{}\n{}".format(try_id,response_data))
                    continue
                if pbar:
                    pbar.update(1)
                logger.debug(response_data)
                result = choices[0]["message"] if "reasoning_content" in choices[0]["message"] else choices[0]["message"]["content"]
                return result
            except Exception as e:
                error_messages = traceback.format_exc()
                logger.error("Retry time:{}\n{}".format(try_id,error_messages))
                self.refresh_authority()
        return "error: "+str(error_messages)
    

    def get_gpt_response_N(self, messages, chat_url, temperature=0, try_num=3, pbar=None, num_samples=1):
        """
        大模型调用和身份验证
        """
        error_messages = ""
        try:
            assert isinstance(messages, list) and not [x for x in messages if x["role"] not in {"system", "user", "assistant"}]
            chat_d = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "n": num_samples
            }
            if not self.authority:
                self.refresh_authority()
            chat_h = {
                "Content-Type": "application/json",
                "userId": self.authority["data"]["user_id"],
                "token": self.authority["data"]["token"]
            }
        except Exception as e:
            error_messages = traceback.format_exc()
            logger.error(error_messages)
            return "error: "+error_messages

        for try_id in range(try_num):
            try:
                response = self.session.post(chat_url, json=chat_d, headers=chat_h, timeout=self.timeout)
                response_data = response.json()
                choices = self.find_key_in_nested_dict(response_data, 'choices')
                if 'success' in response_data and not response_data.get('success'):
                    logger.error("Retry time:{}\n{}".format(try_id,response_data))
                    continue
                if choices is None:
                    logger.error("Retry time:{}\n{}".format(try_id,response_data))
                    continue
                if pbar:
                    pbar.update(1)
                logger.debug(response_data)
                results = []
                for choice in choices:
                    result = choice["message"] if "reasoning_content" in choice["message"] else choice["message"]["content"]
                    results.append(result)
                return results
            except Exception as e:
                error_messages = traceback.format_exc()
                logger.error("Retry time:{}\n{}".format(try_id,error_messages))
                self.refresh_authority()
        return "error: "+str(error_messages)


    def text2text(self, instruction, temperature=0, pbar=None, output_file=None):
        """同步版本的文本生成文本"""
        if isinstance(instruction, str):
            messages = [
                {"role": "user", "content": instruction}
            ]
            instruction = {'messages': messages}
        else:
            messages = instruction['messages']
        
        predict_result = self.get_gpt_response(messages, self.chat_url, temperature=temperature, pbar=pbar)
        if output_file:
            self.save_file(output_file, instruction, predict_result)
        print(predict_result, '\n-----------------------------------------------------------------')
        return predict_result

    def image2text(self, instruction, image=None, temperature=0, pbar=None, output_file=None):
        """同步版本的图文生成文本"""
        if isinstance(instruction, str):
            messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": instruction
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{self._encode_image(image)}"
                            }
                        }
                    ]
                }
            ]
            instruction = {
                "messages":[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": instruction
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": image
                                }
                            }
                        ]
                    }
                ]
            }
        else:
            messages = copy.deepcopy(instruction['messages'])
            for i, item in enumerate(messages):
                for j, item2 in enumerate(item["content"]):
                    if item2.get("type") == "image_url":
                        messages[i]["content"][j]["image_url"] = {
                            "url": f"data:image/jpeg;base64,{self._encode_image(item2['image_url']['url'])}"
                        }
        
        predict_result = self.get_gpt_response(messages, self.chat_url, temperature, pbar=pbar)
        if output_file:
            self.save_file(output_file, instruction, predict_result)
        print(predict_result, '\n-----------------------------------------------------------------\n')
        return predict_result
    

    def image2text_N(self, instruction, image=None, temperature=0, pbar=None, output_file=None, num_samples=1):
        """同步版本的图文生成文本"""
        if isinstance(instruction, str):
            messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": instruction
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{self._encode_image(image)}"
                            }
                        }
                    ]
                }
            ]
            instruction = {
                "messages":[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": instruction
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": image
                                }
                            }
                        ]
                    }
                ]
            }
        else:
            messages = copy.deepcopy(instruction['messages'])
            for i, item in enumerate(messages):
                for j, item2 in enumerate(item["content"]):
                    if item2.get("type") == "image_url":
                        messages[i]["content"][j]["image_url"] = {
                            "url": f"data:image/jpeg;base64,{self._encode_image(item2['image_url']['url'])}"
                        }
        
        predict_result = self.get_gpt_response_N(messages, self.chat_url, temperature, pbar=pbar, num_samples=num_samples)
        if output_file:
            self.save_file(output_file, instruction, predict_result)
        print(predict_result, '\n-----------------------------------------------------------------\n')
        return predict_result
    

    def images2texts(self, instructions: list, temperature=0, images: list=None, output_file=None):
        """同步版本的批量图文生成文本"""
        pbar = tqdm(total=len(instructions), desc="Processing Image-Text Pairs")
        results = []
        
        try:
            if images:
                for instruction, image in zip(instructions, images):
                    result = self.image2text(instruction, image, temperature, pbar=pbar, output_file=output_file)
                    results.append(result)
            else:
                for instruction in instructions:
                    result = self.image2text(instruction, None, temperature, pbar=pbar, output_file=output_file)
                    results.append(result)
        finally:
            pbar.close()
            
        return results

    def texts2texts(self, instructions: list, temperature=0, output_file=None):
        """同步版本的批量文本生成文本"""
        text_messages = []
        if not isinstance(instructions[0], str):
            for i, item in enumerate(instructions):
                if "messages" in item:
                    item = item['messages']
                tmp = []
                for text_item in item:
                    tmp.append({
                        "role": text_item["role"],
                        "content": "\n".join([x["text"] for x in text_item["content"]]) if isinstance(text_item["content"], list) else text_item["content"]
                    })
                instructions[i]['messages'] = tmp

        pbar = tqdm(total=len(instructions), desc="Processing Text-to-Text")
        results = []
        
        try:
            for message in instructions:
                result = self.text2text(message, temperature=temperature, pbar=pbar, output_file=output_file)
                results.append(result)
        finally:
            pbar.close()

        return results
    
class GEMINIClient(LLMClient):
    def __init__(self, app_id="5cafd46a3b2342b5a903afafc38d4aef",
        app_secret="iv+JZxHTQKrpYmxx1U9HNyXEjJNZcUhlBCkmT/lSYIE=",
        url="https://arsenal-openai.10jqka.com.cn:8443/vtuber/auth/api/oauth/v1/login",
        chat_url="https://arsenal-openai.10jqka.com.cn:8443/vtuber/ai_access/gemini/v1/chat/completions",
        # model="gemini-2.5-pro-preview-03-25",timeout=120):
        model="gemini-3.1-pro-preview",timeout=300):
        super().__init__(app_id, app_secret, url, chat_url, model, timeout)



class GPT4OClient(LLMClient):
    # def __init__(self, app_id="5cafd46a3b2342b5a903afafc38d4aef",
    def __init__(self, app_id="e2e4903b01b549e1813317b7f8173465",
        app_secret="Gvdoa07eFcHrlRQJurJHh0eJK211ppzs9dKOJKuHS7o=",
        url="https://arsenal-openai.10jqka.com.cn:8443/vtuber/auth/api/oauth/v1/login",
        chat_url="https://arsenal-openai.10jqka.com.cn:8443/vtuber/ai_access/chatgpt/v3/chat/completions",
        model="gpt-5.2",timeout=120):
        super().__init__(app_id, app_secret, url, chat_url, model, timeout)


class CLAUDEClient(LLMClient):
    def __init__(self, app_id="5cafd46a3b2342b5a903afafc38d4aef",
        app_secret="iv+JZxHTQKrpYmxx1U9HNyXEjJNZcUhlBCkmT/lSYIE=",
        url="https://arsenal-openai.10jqka.com.cn:8443/vtuber/auth/api/oauth/v1/login",
        chat_url="https://arsenal-openai.10jqka.com.cn:8443/vtuber/ai_access/claude/v1/chat/completions",
        model="us.anthropic.claude-3-5-sonnet-20241022-v2:0",timeout=60):
        super().__init__(app_id, app_secret, url, chat_url, model, timeout)


if __name__ == "__main__":
    gemini = GEMINIClient()
    gpt4o = GPT4OClient()
    sys_prompt = '''### 身份及任务
你是一名资深的金融分析助手，来自同花顺Hithink团队。你非常擅长根据用户问题确定分析需要哪些金融数据和信息。现在有一些可以使用的工具，通过它们可以获取金融数据及信息。我会为你提供用户问题<question>query</question>,背景信息<background>text</background>和参考信息<information>text</information>。请你基于现有信息，分析需要使用哪些工具补充获取哪些信息。

### 输出格式
当你认为需要获取信息时，回答格式如下：
Thought: 你对问题的思考和分析，基于现有的背景信息和参考信息，分析回答用户问题还需要获取哪些方面的数据和信息。你可以尽可能多的获取各方面的数据和信息。
ActionList: 你需要执行的动作列表，每一个动作由工具名称和工具输入组成。动作列表有多行，每一行的表示为：工具名称: 工具输入。

当你认为规划完成时，回答格式如下：
Thought: 信息完整，我知道如何回答了。
<FINISHED>

### 可以使用的工具：
FinQuery: 金融查询工具，使用这个工具来获取标的相关的金融数据，比如宏观数据、财务数据、行情数据、交易数据、个人账户数据、自选股等，涉及股票、美股、港股、基金、指数、宏观、可转债、期货，它的输入包括具体金融指标或带时间的指标，也可以输入多个指标用于筛选。如果输入指标过多，则需要适当拆分。例子: "FinQuery: 苹果公司近5天股价以及涨跌幅"
Search: 搜索工具，使用这个工具来搜索相关信息，类似一个搜索引擎，它的输入是自然语言短语或者关键词，用来搜索非结构性数据，关键词最好不要超过5个。例子: "Search: 苹果公司近期新闻"
TickerChart: A股取图工具，当你需要K线图、分时图、技术指标图等信息来辅助你分析问题时，使用该工具获取图片。需要输入这些字段："startDate", "codeName", "chartType", "indicator", "endDate"。"startDate": "Start date in the format YYYY-MM-DD", "endDate": "End date in the format YYYY-MM-DD", "codeName": "Stock code or ticker symbol", "chartType": "Type of chart to retrieve, maximum 1. Enumerate value: Intraday, Daily Candlestick, Weekly Candlestick, Monthly Candlestick", "indicator": "List of indicators to display on the chart, maximum 5. Enumerate value: MA, EMA, BIAS, VR, BRAR, WR, SMA, CCI, MTM, BBI, DMI, EMV, VOL, CR, SAR, PSY, AO, DMA, ROC, TRIX, PVT, RSI, OBV, VWAP, BOLL, MACD, KDJ"。例子: "TickerChart: {"codeName": "300033", "chartType": "Daily Candlestick", "startDate": "2024-01-08", "endDate": "2025-05-08", "indicator": ["MA", "MACD"]}"
ChartTwinFinder: 相似股票查找工具，通过该工具可以快速检索到与图中走势相似的标的，并返回相似度以及相似时间区间。如果用户询问走势相似的标的，且图中包含一段K线走势图，可以使用该工具。需要输入这些字段: "query", "url"。 "query": "相似股票查找工具的文本输入，固定为：分析与下图形态走势相近的股票", "url": "图片的URL地址"。例子: "ChartTwinFinder: {"query": "分析与下图形态走势相近的股票", "url": "http://oss.myhexin.com.cn/iwc-web-userinfo-storage-server.model-image-q-a/bcf0d0684dc6432793e8de8436140b6d.png"}"
VisitWeb: 网页解析工具，这个工具用于实时抓取与解析网页内容的工具，其主要功能是通过输入一个网页的URL，从该网页中提取结构化的信息。输入必须为url，且每次只有一个url。例子: "VisitWeb: https://paas.myhexin.com/hithinkflow/dataview/list?tenantId=14"

### 内容要求
你务必遵守以下原则：
1. 你的思考和动作一定要深入全面，保持联想能力和创新能力，这很大程度上决定了我最后能否有足够的信息回答这个问题。
2. 在Thought中不要提及你使用的工具，而是说你的目的。也不要提及你遵循的规则，你应当尽量表现的像一个人类。
3. Thought内容不要过长，简单几句话概括即可。
4. 若你多次利用工具后仍查询不到结果，为了防止无意义的查询，你可以选择终止计划，进入回答阶段。
5. 在ActionList中，工具输入应当包含具体的对象，不可以出现代词或引用。
6. 尽量在一次回答时，把需要的所有工具调用都给出。
7. 用中文回答问题。
    '''
    # query = '查找相似形态的股票'
    # response = gemini.text2text(instruction)

    instruction = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": sys_prompt 
                    },
                    {
                        "type": "image_url",
                        "image_url": {'url':'/mnt/HithinkOmniSSD/user_workspace/ganziliang/code/agent/synthesis/2511/18/data/gemini/images/2b1f63c26e362010630a20910514d184_750_942.png'} }
                ]
            }]
        }

    # messages = [{"role":"user","content":[{"type":"text","text":"### 身份及任务\n你是一名资深的金融分析助手，来自同花顺Hithink团队。你非常擅长根据用户问题及相关图片分析需要哪些金融数据和信息。现在有一些可以使用的工具，通过它们可以获取金融数据及信息。我会为你提供用户问题<question>query</question>, 用户图片，背景信息<background>text</background>和参考信息<information>text</information>。请你基于现有信息，分析需要使用哪些工具补充获取哪些信息。\n\n**严格按照下面的输出格式进行回答**。\n### 输出格式\n如果需要获取更多信息，需要在<think>和</think>中间对参考信息中已经获取的金融数据和信息进行小结，然后分析回答用户问题还需要获取哪方面的数据和信息，你可以按照<tool_begin>{\"name\": \"<tool_name>\", \"input\": \"<tool_input>\"}</tool_end>格式给出多个获取金融数据和信息的工具调用建议。\n\n当你认为不再需要更多的未知数据和信息或者对于一些不需要数据和信息支持的用户问题，例如翻译、文本处理等问题，在<think>和</think>中分析数据和信息对于回答用户问题的完备性，之后无需给出工具调用建议，而是输出一个由<star_list>和</star_list>包围的你认为对于回答用户问题很有帮助的已获取数据及信息的编号组成的列表，此时使用纯数字编号。\n\n### 可以使用的工具：\n{\"type\": \"function\", \"function\": {\"name\": \"FinQuery\", \"description\": \"金融查询工具，使用这个工具来获取标的相关的金融数据，比如宏观数据、财务数据、行情数据、交易数据、个人账户数据、自选股等，涉及股票、美股、港股、基金、指数、宏观、可转债、期货，它的输入包括具体金融指标或带时间的指标，也可以输入多个指标用于筛选。如果输入指标过多，则需要适当拆分。\", \"parameters\": {\"type\": \"object\", \"required\": [\"name\", \"input\"], \"properties\": {\"name\": {\"type\": \"string\", \"description\": \"工具名称\"}, \"input\": {\"type\": \"string\", \"description\": \"工具输入\"}}}}, \"example\": {\"name\": \"FinQuery\", \"input\": \"苹果公司近5天股价以及涨跌幅\"}}\n{\"type\": \"function\", \"function\": {\"name\": \"Search\", \"description\": \"搜索工具，使用这个工具来搜索相关信息，类似一个搜索引擎，它的输入是自然语言短语或者关键词，用来搜索非结构性数据，关键词最好不要超过5个。\", \"parameters\": {\"type\": \"object\", \"required\": [\"name, input\"], \"properties\": {\"name\": {\"type\": \"string\", \"description\": \"工具名称\"}, \"input\": {\"type\": \"string\", \"description\": \"工具输入\"}}}}, \"example\": {\"name\": \"Search\", \"input\": \"苹果公司近期新闻\"}}\n{\"type\": \"function\", \"function\": {\"name\": \"TickerChart\", \"description\": \"A股取图工具，当你需要K线图、分时图、技术指标图等信息来辅助你分析问题时，使用该工具获取图片。\", \"strict\": true, \"parameters\": {\"type\": \"object\", \"required\": [\"name\", \"input\"], \"properties\": {\"name\": {\"type\": \"string\", \"description\": \"工具名称\"}, \"input\": {\"type\": \"string\", \"description\": '工具输入，格式为json字符串。需要输入这些字段：\"startDate\", \"codeName\", \"chartType\", \"indicator\", \"endDate\"。\"startDate\": \"Start date in the format YYYY-MM-DD\", \"endDate\": \"End date in the format YYYY-MM-DD\", \"codeName\": \"Stock code or ticker symbol\", \"chartType\": \"Type of chart to retrieve, maximum 1. Enumerate value: Intraday, Daily Candlestick, Weekly Candlestick, Monthly Candlestick\", \"indicator\": \"List of indicators to display on the chart, maximum 5. Enumerate value: MA, EMA, BIAS, VR, BRAR, WR, SMA, CCI, MTM, BBI, DMI, EMV, VOL, CR, SAR, PSY, AO, DMA, ROC, TRIX, PVT, RSI, OBV, VWAP, BOLL, MACD, KDJ'}}}},  \"example\": {\"name\": \"TickerChart\", \"input\": '{\"codeName\": \"300033\", \"chartType\": \"Daily Candlestick\", \"startDate\": \"2024-01-08\", \"endDate\": \"2025-05-08\", \"indicator\": [\"MA\", \"MACD\"]}'}}\n{\"type\": \"function\", \"function\": {\"name\": \"ChartTwinFinder\", \"description\": \"相似股票查找工具，通过该工具可以快速检索到日K走势与图中走势相似的标的，并返回相似度以及相似时间区间。如果用户询问走势相似的标的，且图中包含一段K线走势图，可以使用该工具。如果图片是分时走势图，不需要使用该工具。\", \"parameters\": {\"type\": \"object\", \"required\": [\"name\", \"input\"], \"properties\": {\"name\": {\"type\": \"string\", \"description\": \"工具名称\"}, \"input\": {\"type\": \"string\", \"description\": '工具输入，格式为json字符串。需要输入这些字段: \"query\", \"url\"。 \"query\": \"相似股票查找工具的文本输入，固定为：分析与下图形态走势相近的股票\", \"url\": \"图片的URL地址\"。'}}}}, \"example\": {\"name\": \"ChartTwinFinder\", \"input\": '{\"query\": \"分析与下图形态走势相近的股票\", \"url\": \"http://oss.myhexin.com.cn/iwc-web-userinfo-storage-server.model-image-q-a/bcf0d0684dc6432793e8de8436140b6d.png\"}'}}\n{\"type\": \"function\", \"function\": {\"name\": \"VisitWeb\", \"description\": \"网页解析工具，这个工具用于实时抓取与解析网页内容的工具，其主要功能是通过输入一个网页的URL，从该网页中提取结构化的信息。输入必须为url，且每次只有一个url。\", \"parameters\": {\"type\": \"object\", \"required\": [\"name\", \"input\"], \"properties\": {\"name\": {\"type\": \"string\", \"description\": \"工具名称\"}, \"input\": {\"type\": \"string\", \"description\": \"工具输入\"}}}}, \"example\": {\"name\": \"VisitWeb\", \"input\": \"https://news.10jqka.com.cn/realtimenews.html\"}}\n\n### 内容要求\n1. 你首先需要在<think>和</think>中间对现有的思考和工具调用中的思考和已经获取的工具调用结果进行小结，然后分析回答用户问题还需要获取哪方面的数据和信息。你可以尽可能多的获取各方面的数据和信息。\n2. 在<think>中不要提及你使用的工具，而是说你的目的。也不要提及你遵循的规则，你应当尽量表现的像一个人类。\n3. 如果工具调用失败或未返回结果或者查询结果已在历史information中，请勿使用相同的输入重试；不要进行无意义的工具查询；存在拓扑顺序的数据和信息，不要在同一轮次获取。\n4. 当你认为不再需要更多的未知数据和信息时，不再给出工具调用计划。此时在<hithink>中分析数据和信息对于回答用户问题的完备性。\n4. 在<think>中，如果未获取任何信息时，不进行溯源。对于获取失败的数据信息不要进行溯源。\n5. 在工具调用中，tool_input应当包含具体的对象，不可以出现代词或引用。\n6. 你需要多角度有深度地思考问题。\n7. 尽量在一次回答时，把需要的所有工具调用都给出。\n8. 当不再进行工具调用时，回答中不再出现<tool_begin>和</tool_end>，但是会出现<star_list>，在<star_list>和</star_list>中间是一个列表，它的元素是历史获取数据和信息的编号，它们对应的数据和信息是你认为对于回答用户问题很有帮助的，对于相似的信息只选取其一，例如<star_list>[1,2,5]</star_list>。\n9. 在<think></think>中提到已获取的数据和信息时，使用参考信息中已经获取的工具调用结果对应的编号溯源。严格遵守引用溯源格式[^<int>]如[^1]、[^3][^4]等。尚未获取任何信息时，不进行溯源。对于获取失败的数据信息不要进行溯源。\n10. 用中文回答问题。\n现在的时间是 <time>2025-08-20 19:48:42 周三</time>\n历史对话信息 <history></history>\n<question>这是什么品牌的键盘</question>\n用户上传的图片："},{"type":"text","text":"<img_url>https://ai.iwencai.com/userinfo-model-image-q-a/cc14b1477303443aa1c51fac8c5f4b90.jpg</img_url>"},{"type":"image_url","image_url":{"url":"https://ai.iwencai.com/userinfo-model-image-q-a/cc14b1477303443aa1c51fac8c5f4b90.jpg"}},{"type":"text","text":"<information>\n编号: 1\n标题: 荷兰品牌Wooting键盘官网，港澳台选项前已加中国前缀\n时间: 2025-08-18\n摘要: 近日，有网友称在荷兰品牌Wooting键盘的官方网站上填写收货地址时，“国家”选项栏里中国与港澳台选项并列其中。 8月18日，记者登录Wooting官网发现，原来的“国家”选项栏已调整为“国家/地区”，台湾、香港、澳门的选项前面也已加上了“中国”前缀。 \n站点: \n溯源地址: https://www.thepaper.cn/newsDetail_forward_31422056\n\n编号: 2\n标题: RAZER发布黑寡妇蜘蛛V4 矮轴超薄游戏键盘系列\n时间: 2025-08-15\n摘要: Razer（雷蛇），全球领先的玩家生活方式潮流品牌（以下简称“雷蛇”）发布了雷蛇黑寡妇蜘蛛 V4 矮轴极速版 ( Razer BlackWidow V4 Low-profile HyperSpeed ) 和雷蛇黑寡妇蜘蛛 V4 矮轴竞技极速版 (Razer BlackWidow V4 Low-profile Tenkeyless HyperSpeed)，这是先进的超薄游戏键盘。 两款键盘均专为沉浸式游戏体验和精准操控而设计，搭载了 Razer HyperSpeed 无线技术、专用宏按键及控制功能，并且首次采用了雷蛇矮轴机械轴体。 这使得键盘拥有快速精准的触发响应、舒适的按键反馈，以及简约流畅的外观，能够完美融入各类使用场景。“问世 15 年以来，雷蛇黑寡妇蜘蛛系列键盘一直是游戏装备的经典之选。 其按键寿命最高达约 8000 万次，足以承受竞技游戏的高强度使用，是行业标准的 1.6 倍。这两...\n站点: \n溯源地址: https://www.gamersky.com/hardware/202508/1979551.shtml\n\n编号: 3\n标题: 矮轴键盘\n时间: 2025-08-15\n摘要: 摘要：Hexgears X-1无线超矮轴键盘，国外型号为HEYKU GK950，已于 2018 年 7 月 24 日在美国kono商店众筹上线。轴体更薄、机身更轻的的机械键盘，越来越受消费者青睐。 在Kono商店上，新增了一款来自HEXGEARS外设品牌的机械键盘。这款机械键盘的亮眼之处，在于融合了德国的精密设计与搭载了凯华（Kailh）自主研发的超矮轴。 键盘的设计灵感来自世界上第一架超音速飞机——传奇的Bell X-1。HEXGEARS X- 1 是一款亮点满满的全新超薄机? \n站点: \n溯源地址: https://www.chinaz.com/tags/987664.shtml\n\n编号: 4\n标题: RAZER发布黑寡妇蜘蛛 V4 矮轴超薄游戏键盘系列 - 游戏茶馆\n时间: 2025-08-15\n摘要: 加州尔湾 —— Razer(雷蛇)，全球领先的玩家生活方式潮流品牌(以下简称“雷蛇”)发布了雷蛇黑寡妇蜘蛛 V4 矮轴极速版 ( Razer BlackWidow V4 Low-profile HyperSpeed ) 和雷蛇黑寡妇蜘蛛 V4 矮轴竞技极速版 (Razer BlackWidow V4 Low-profile Tenkeyless HyperSpeed)，这是先进的超薄游戏键盘。 两款键盘均专为沉浸式游戏体验和精准操控而设计，搭载了 Razer HyperSpeed 无线技术、专用宏按键及控制功能，并且首次采用了雷蛇矮轴机械轴体。 这使得键盘拥有快速精准的触发响应、舒适的按键反馈，以及简约流畅的外观，能够完美融入各类使用场景。“问世 15 年以来，雷蛇黑寡妇蜘蛛系列键盘一直是游戏装备的经典之选。 这两款键盘专为耐用性与精准性而设计，配备高品质 5052 铝合金上盖、双层消音泡...\n站点: \n溯源地址: http://youxichaguan.com/news/35923.html;jsessionid=709B5C8B9681FBF88E4EED52F38829BE\n\n编号: 5\n标题: RAZER发布黑寡妇蜘蛛 V4 矮轴超薄游戏键盘系列\n时间: 2025-08-15\n摘要: RAZER发布黑寡妇蜘蛛 V4 矮轴超薄游戏键盘系列加州尔湾 —— Razer（雷蛇）， 全球领先的玩家生活方式潮流品牌（以下简称“雷蛇”）发布了雷蛇黑寡妇蜘蛛 V4 矮轴极速版 ( Razer BlackWidow V4 Low-profile HyperSpeed ) 和雷蛇黑寡妇蜘蛛 V4 矮轴竞技极速版 (Razer BlackWidow V4 Low-profile Tenkeyless HyperSpeed)，这是先进的超薄游戏键盘。 两款键盘均专为沉浸式游戏体验和精准操控而设计，搭载了 Razer HyperSpeed 无线技术、专用宏按键及控制功能，并且首次采用了雷蛇矮轴机械轴体。 其按键寿命最高达约 8000 万次，足以承受竞技游戏的高强度使用，是行业标准的 1.6 倍。这两款键盘专为耐用性与精准性而设计，配备高品质 5052 铝合金上盖、双层消音泡绵、厂润卫星轴以及不锈...\n站点: \n溯源地址: https://www.chinaz.com/2025/0815/1704219.shtml\n\n编号: 6\n标题: Keychron推黑神话联名机械键盘\n时间: 2025-07-25\n摘要: 键盘品牌Keychron近期发布了一款全新联名无线机械键盘，设计灵感源自热门作品黑神话：悟空。键盘整体采用深黑色调，辅以鎏金纹饰点缀，外观沉稳且富有东方韵味。 布局方面选用75%配列，相较传统全尺寸键盘更为紧凑，省去独立数字小键盘区域，提升桌面空间利用率。 该键盘延续品牌一贯的多模连接设计，支持USB-C有线、2.4GHz无线及蓝牙5.2三种连接方式，其中蓝牙可同时配对三台设备，便于在不同终端间快速切换。 \n站点: \n溯源地址: https://finance.sina.com.cn/tech/roll/2025-07-25/doc-infhsfmv1907418.shtml\n\n编号: 7\n标题: 华硕 ROG 影魔 75% 分离式键盘上市：HFX V2 磁轴、无线 8K 回报率，2599 元\n时间: 2025-08-14\n摘要: IT之家 8 月 13 日消息，华硕 ROG 影魔 75% 分离式键盘今日晚正式上市开售，新品搭载 HFX V2 磁轴、支持无线 8K 回报率，售价 2599 元。 京东 华硕 ROG 影魔 75% 分离式 磁轴键盘 HFX V2 磁轴   无线 8K 回报率 2599 元直达链接 据介绍，华硕 ROG 影魔键盘采用分离式设计，可在游戏时只使用左边键盘， 新品键盘搭载全新 ROG HFX V2 第二代磁轴，轴芯与底壳选用 POM 材质，按键寿命达 1 亿次。HFX V2 磁轴还支持自定义触发行程，可实现 0.1mm 精度调节。 华硕 ROG 影魔 75% 分离式键盘采用新一代 Nordic 54H 主控、支持 SpeedNova 8K 无线技术 + 三模连接，续航至高 200+ 小时（Zone 模式续航时间约 127 小时）。 键盘还采用 4 层消音结构、配备双色注塑 PBT 键帽。 IT...\n站点: \n溯源地址: http://news.10jqka.com.cn/19700121/c670355992.shtml\n\n编号: 8\n标题: 设计师键盘再添新选择，绘王蓝牙单手键盘K40上市\n时间: 2025-07-15\n摘要: 7月15日，数字绘画品牌绘王(HUION)发布新款蓝牙单手键盘Keydial Remote K40。Keydial Remote K40键盘的尺寸为152x56x13.5mm，重量约为109g。 K40键盘前端通过外圈和内圈镶嵌，将双Dial编码器结合在一起，外圈滚轮点缀巴黎钉纹。机身共有8个自定义按键、2个Dial旋钮、2个切组按键。按键与键帽为一体式硅胶材质，表面有微弧+凹凸面符号，按键克重为100g。 支持按键间防冲，按键与Dial可任意组合使用。连接方面，该键盘搭载蓝牙5.0技术，可连Windows、macOS 、Android、iOS、iPadOS等系统的电脑、手机、平板设备使用。 \n站点: \n溯源地址: https://t.cj.sina.com.cn/articles/view/1747383115/6826f34b020026imu?finpagefr=p_103\n\n编号: 9\n标题: 赛睿（SteelSeries）ApexPro Gen3 三代电竞磁轴键盘 游戏有线键盘 RT急停机械键盘 FPS 无畏契约 CSGO 打瓦键盘 104键_哔哩哔哩_bilibili\n时间: 2025-08-16\n摘要: 赛睿（SteelSeries）ApexPro Gen3 三代电竞磁轴键盘 游戏有线键盘 RT急停机械键盘 FPS 无畏契约 CSGO 打瓦键盘 104键_哔哩哔哩_bilibili。 赛睿（SteelSeries）ApexPro Gen3 三代电竞磁轴键盘 游戏有线键盘 RT急停机械键盘 FPS 无畏契约 CSGO 打瓦键盘 104键。 赛睿 Apex Pro TKL三代：真的猛如外挂吗？。【灵感源自死亡搁浅？】怒喵新品Battleye Key上手：1.7 英寸触控屏 + 半度脚撑，磁轴还能这么玩？。国产键盘全是垃圾？2025年5月最新最全键盘推荐，国产品牌已经卷冒烟了！ \n站点: \n溯源地址: https://www.bilibili.com/video/BV1gab4z7Eiz/\n\n编号: 10\n标题: Pulsar派世发布eS HE 70机械键盘：8KHz高回报率+磁轴可调触发\n时间: 2025-08-19\n摘要: Pulsar派世发布eS HE 70机械键盘：8KHz高回报率+磁轴可调触发....................................... \n站点: \n溯源地址: http://news.10jqka.com.cn/19700121/c670474181.shtml\n\n编号: 11\n标题: Keychron 渴创推出猫爪巨轴单键键盘，39.99 美元\n时间: 2025-08-20\n摘要: IT之家 8 月 20 日消息，键盘中的轴体在将按键动作转换为电信号的过程中起到关键作用，而将常规轴体放大数倍的“巨轴”不仅仍然能执行输入操作，也是一款别具风味的桌搭摆件。 Keychron 渴创就在北京时间今日宣布推出 Big Kitty Paw Key 猫爪巨轴单键键盘。 Keychron Big Kitty Paw Key 猫爪巨轴官网售价 39.99 美元（IT之家注：现汇率约合 287.3 元人民币），渴创官方还提供了用户通过 3D 打印自定义更换键帽所需的多视图参数文件。 \n站点: \n溯源地址: http://news.10jqka.com.cn/19700121/c670504043.shtml\n\n编号: 12\n标题: 黑鲨外设震撼发布：全球首款搭载“特斯拉”电感轴的无线电竞键盘引领行业新风尚！\n时间: 2024-09-23\n摘要: 黑鲨外设震撼发布：全球首款搭载“特斯拉”电感轴的无线电竞键盘引领行业新风尚！近日，国内知名游戏外设品牌——黑鲨外设，举办了一场以“创无止竞”为主题的2024秋季新品发布会。 会上，黑鲨外设品牌运营总经理项可兴为广大媒体与玩家展示了多款令人瞩目的新品，黑鲨无线电竞键盘电竞版与“我的三体版”限量版键盘， 以及黑鲨凤鸣头戴式游戏耳机、黑鲨凤鸣真无线蓝牙耳机、黑鲨55W自带线移动电源以及黑鲨67W氮化镓充电器悉数亮相。 本次发布会的重磅产品——黑鲨无线电竞键盘，搭载了全球首发的“特斯拉”电感轴技术，为电竞行业树立了新的技术标杆。 \n站点: \n溯源地址: https://www.gamersky.com/hardware/202409/1820059.shtml\n\n编号: 13\n标题: 七彩虹发布青羽98系列机械键盘，8月22日开售\n时间: 2025-08-16\n摘要: 七彩虹发布青羽98系列机械键盘，8月22日开售....................................... \n站点: \n溯源地址: http://news.10jqka.com.cn/19700121/c670422274.shtml\n\n编号: 14\n标题: 华硕ROG影魔75%磁轴键盘上市：无线8000Hz，五向调节\n时间: 2025-08-14\n摘要: 华硕ROG影魔75%磁轴键盘上市：无线8000Hz，五向调节....................................... \n站点: \n溯源地址: http://news.10jqka.com.cn/19700121/c670365883.shtml\n\n编号: 15\n标题: 罗技 G PRO X 60 机械键盘发布：60% 配列，179.99 美元\n时间: 2024-04-09\n摘要: 其他方面，这款键盘还采用了罗技 GX Optical 机械轴以及透光 PBT 材质键帽，支持全局 RGB 灯效背光，配备了音量滚轮、游戏模式开关、蓝牙模式以及 LIGHTSPEED 模式开关等， 支持蓝牙、Ligtspeed 双无线连接，续航可达 65 小时，采用 USB-C 接口。 \n站点: \n溯源地址: http://stock.10jqka.com.cn/usstock/20240409/c656709944.shtml\n\n编号: 16\n标题: 机械键盘有哪些分类_机械键盘分几种类型-CSDN博客\n时间: 2025-08-17\n摘要: 版权声明：本文为博主原创文章，遵循 CC 4.0 BY-SA 版权协议，转载请附上原文出处链接和本声明。机械键盘是一种比传统的薄膜键盘更耐用、更快捷、更具有手感的键盘。 它的键帽和按键是独立的，能够提供更好的反应速度和操作感。机械键盘在现代化生活中得到了广泛的应用。根据其特性和使用场景，机械键盘可以分为以下几类：。1.轴体分类。 机械键盘根据轴体的不同，可以分为青轴、茶轴、红轴、黑轴等多种类型。其中，青轴是声音最响亮的，操作的力度也最大，通常适用于打字等需要力度的操作场合。茶轴是声音最小的，手感较为轻盈，适合游戏玩家使用。 红轴则是适合需要频繁操作的场合，手感极佳。黑轴手感与红轴相近，但操作力度更强，适用于计算机程序员等。2.按键结构分类。机械键盘按键结构也有所不同，可以分为带有T型支架和不带T型支架两种类型。 机械键盘也可以按照颜色分类，包括黑色、白色、银色和灰色等。不同颜色的键盘在设计...\n站点: \n溯源地址: https://blog.csdn.net/qq_38665677/article/details/140248947\n\n编号: 17\n标题: 大电池+彩屏＋旋钮+FR4单键开槽+5层填充！黑爵AK980三模机械键盘体验\n时间: 2024-09-08\n摘要: 这款键盘尺寸为：416.7x148x19.4mm共有97枚按键，比传统的104配列键盘更节省桌面空间。键盘右上方分布了一块彩色LCD屏幕以及一枚金属旋钮，屏幕右侧还有山川图案的装饰，细节满满。 键帽&轴体 黑爵 AK980采用一套黑色字体、原厂高度、PBT材质的二色键帽，深灰、浅灰和黄色三拼配色，美观大方。键帽做工很好，键帽壁很厚，触感细腻，长时间使用不易打油。AK980支持全键热插拔。 大键位采用厂家精润的黑色卫星轴，到手即用。中高配版采用FR4单键开槽的沉金定位板，使拔插轴体更加顺畅的同时，增加打字软弹度。下灯位设计还可以搭配侧透键帽使用。 \n站点: \n溯源地址: https://t.cj.sina.com.cn/articles/view/1662256934/63140726001017zku?finpagefr=p_103\n\n编号: 18\n标题: 京东京造《黑神话：悟空》首款联名机械键盘开售：699元起 首批限量版仅1000套\n时间: 2024-12-25\n摘要: 京东京造《黑神话：悟空》首款联名机械键盘开售：699元起 首批限量版仅1000套快科技12月25日消息，今日，京东京造推出的《黑神话：悟空》联名键盘《墨染乾坤》开售，这是“黑神话”IP在键盘品类的独家合作项目， 也是唯一获得IP方游戏科学主创团队全面参与外观设计的键盘产品。《墨染乾坤》键盘提供纪念版、收藏版和限量收藏版三种版本，价格分别为699元、999元和1499元。其中,《墨染乾坤》限量收藏版首批限量发售仅有1000套。 ID设计上，键盘融入很多传统文化元素和《黑神话：悟空》游戏元素。比如，收藏版和限量收藏版ESC键为“乌金兽吞”黄铜键帽，同时采用“葫芦”旋钮，纪念版为“金晴”旋钮。 键盘采用一体成型铝合金机身，并配备金属涂层五面热升华键帽（纪念版为高含量PBT材质），均为“龙宫夺宝”金属立体背板。 据了解，键盘采用75配列81键设计，支持有线、2.4G、蓝牙三模连接，除纪念版为400...\n站点: \n溯源地址: https://finance.sina.com.cn/tech/roll/2024-12-25/doc-inearqpv0943007.shtml\n\n编号: 19\n标题: 达尔优推出 A75HE 电竞磁轴键盘：8000Hz 轮询率、Gasket 结构，首发 599 元起 达尔优 电竞 IT之家_新浪科技_新浪网\n时间: 2024-11-21\n摘要: 键盘使用 Gasket 结构，配备音量调节旋钮、拥有 3 颗独立指示灯。IT之家附产品信息如下： \n站点: \n溯源地址: https://finance.sina.com.cn/tech/digi/2024-11-21/doc-incwvuih1241136.shtml\n\n编号: 20\n标题: Keychron联名《黑神话》推无线机械键盘\n时间: 2025-07-26\n摘要: Keychron联名《黑神话》推无线机械键盘键盘品牌Keychron近日推出与国产动作游戏黑神话：悟空联名的全新无线机械键盘——Black Myth:Wukong无线机械键盘。 该产品在延续Keychron一贯精湛工艺的同时，深度融入黑神话：悟空的视觉元素，将机械键盘的设计语言与东方神话美学巧妙融合，呈现出兼具文化质感与现代科技感的全新作品。 键盘整体采用全黑色外壳，搭配金色细节装饰，营造出深邃而富有力量的视觉效果，充分呼应游戏所呈现的暗黑幻想氛围。 采用75%紧凑布局，在精简体积、去除数字小键盘的同时，完整保留功能键区域，兼顾桌面空间利用与操作便捷性，适合追求高效与美感的用户。连接方面，键盘支持三种模式：USB-C有线连接、2.4GHz无线以及蓝牙5.2。 \n站点: \n溯源地址: http://news.10jqka.com.cn/19700121/c669923866.shtml\n</information>"},{"type":"text","text":"<think>\n\n用户想知道图片中的键盘是什么品牌。我仔细观察了图片，发现键盘左上角有一串模糊的文字，似乎是“Kazz”。我推测这可能是键盘的品牌名称。为了确认这一点，我需要搜索关于“Kazz”品牌的键盘信息。参考信息中没有直接相关的品牌信息，因此需要通过搜索引擎来查找。\n\n</think>"},{"type":"text","text":"<information>\n编号: 21\n标题: 珂芝K98胶坨坨键盘——电竞玩家的轻奢之选\n时间: \n摘要: 珂芝（KZZI），作为一家致力于将轻奢复古与时尚潮玩融合为一体的外设品牌，其新品K98胶坨坨键盘一经推出便引起了广泛关注。作为一个热衷于机械键盘的玩家，我对市面上的各种键盘产品都保持着高度的关注。珂芝K98胶坨坨键盘作为珂芝品牌的新款旗舰产品，自上市以来就备受瞩目。#图文动态同步大赛# 今天，我有幸为大家带来这款键盘的详细测评\n站点: \n溯源地址: https://baijiahao.baidu.com/s?for=pc&id=1804294171154974408&wfr=spider\n\n编号: 22\n标题: 从薄膜到机械的“中年觉醒”:珂芝K75 Lite让我找回码字的仪式感...\n时间: \n摘要: 一、关于珂芝:用理科生的浪漫做键盘 在机械键盘领域,珂芝(Kzzi)犹如一位将代码逻辑与工业设计完美融合的跨界工程师。这个诞生于2021年的国产品牌,以“客制化”为核心理念(中文名谐音“客制”),将轻奢复古设计与精密制造工艺结合。他们不做花里胡哨的营销噱头,而是执着于“结构决定手感”的底层逻辑。就像我手上的K...\n站点: \n溯源地址: https://zhuanlan.zhihu.com/p/32740210506\n\n编号: 23\n标题: 重新定义厚胶位，KZZI K98厚胶位机械键盘引领键圈新潮流\n时间: 2024-06-21\n摘要: 作为日常办公的用品,厚胶位键盘没有铝制键盘的笨重感,同时由于自身的重量,在使用过程中不容易滑动。这些特质让整个键盘在日常办公和游戏过程中都能带来更稳定和舒适的使用体验。 #FormatImgID_2# 引领新潮流 随着KZZI量产厚胶位机械键盘的推出,重新定义了塑胶机械键盘的品质标准,为追求质感与手感兼备的用户提供造型出众、配色丰富、外观更具个性化的机械键盘产品,引领键圈新潮流。 作为厚胶位机械键盘的领跑者,KZZI将持续深耕厚胶位注塑工艺,通过新工艺、新技术的应用,让更多消费者体验到高品质机械键盘的独特魅力,为消费者带来更多技术革新的外设产品。 \n站点: \n溯源地址: https://finance.sina.com.cn/tech/roll/2024-06-21/doc-inaznhnk1368543.shtml\n\n编号: 24\n标题: 珂芝K75机械键盘究竟怎么样?高效舒适的打字体验等你发现\n时间: \n摘要: 珂芝（KZZI）作为一家致力于电子外设产品研发和创新的品牌，一直以来以其卓越的设计和优质的产品在行业内享有盛誉。其推出的K75炫彩版机械键盘，凭借多种先进功能和独特设计，为消费者提供了高效且舒适的使用体验。珂芝K75机械键盘评测：珂芝K75机械键盘是专为不同用户需求而设计的客制化机械键盘。无论是办公、游戏...\n站点: \n溯源地址: https://baijiahao.baidu.com/s?for=pc&id=1815132499144841448&wfr=spider\n\n编号: 25\n标题: 黑爵首款电竞磁轴键盘 AK820 MAX 发布：8KHz 低延迟，199 元起\n时间: 2024-07-13\n摘要: 黑爵首款电竞磁轴键盘 AK820 MAX 发布：8KHz 低延迟，199 元起IT之家 7 月 13 日消息，黑爵 Ajazz 推出该品牌首款电竞磁轴键盘AK820 MAX，该键盘将于7 月 16 日 10 点首发， 199 元起。黑爵 AK820 MAX 键盘采用 75% 配列，带音量旋钮；搭载厂润磁轴，全键支持热插拔，可适配凯华 / 佳达隆等磁轴。 内部填充方面，这款键盘还采用 GASKET 垫片结构设计，搭配铝定位板及三层填充，结构稳固，可充分发挥磁轴性能，还标配下灯位 RGB 背光。 \n站点: \n溯源地址: https://finance.sina.com.cn/roll/2024-07-13/doc-inccytwt3684111.shtml\n\n编号: 26\n标题: 黑爵推出 AK680 MAX 磁轴键盘：磁稻轴、TOP 结构，首发 79 元起 - IT之家\n时间: 2024-11-22\n摘要: IT之家附黑爵 AK680 MAX 磁轴键盘亮点、产品信息如下： 京东黑爵（AJAZZ）AK680 MAX 有线磁轴键盘磁稻轴   TOP 结构 79 元起直达链接 广告声明：文内含有的对外跳转链接（包括不限于超链接、二维码、口令等形式），用于传递更多信息，节省甄选时间，结果仅供参考，IT之家所有文章均包含本声明。 \n站点: \n溯源地址: https://www.ithome.com/0/812/334.htm\n\n编号: 27\n标题: 金属滚轮、仿碳纤维上盖，GAS+TOP双结构！珂芝K87胶坨坨三模机械键盘\n时间: 2024-11-09\n摘要: K87采用厚胶位注塑工艺，外壳非常厚实，用双手很难掰弯，实测重量达两斤； 实测键盘前高不到2cm，不用碗托即可轻松打字； 键盘右上方设有一枚手感很棒的铝合金音量滚轮，圆形的Kzzi Logo键用于开关电源。 方向键上方还设有键盘指示灯以及RGB灯饰条，非常有设计感； 键盘左上侧设有接收器磁吸收纳仓和Type-C充电接口，如果是MAX版这里还会多一个USB HUB接口，更方便些； 值得一提的是，这款键盘还内置了NTC电池温度保护功能，如果温度超过45度会自动断开充电，可以很好的保护电池，延长使用寿命。 键盘背面设有4枚椭圆形防滑脚垫以及1对脚撑； 通过二段式脚撑可以调节三种输入高度，，有效减轻用户长时间使用带来的疲劳。键帽和轴体 标配的PBT二色侧透键帽做工不错，闭口字符，表面磨砂质感，可以有效防止打油。 \n站点: \n溯源地址: https://t.cj.sina.com.cn/articles/view/1662256934/63140726001018gaq?finpagefr=p_103\n\n编号: 28\n标题: 珂芝双·十一AI体验月福利来袭，更有众多新品上新！_新浪科技_新浪网\n时间: 2024-11-04\n摘要: K87三模机械键盘:采用87配列布局,非公模ID设计传承珂芝K系列复古设计语言;支持全键热插拔;采用CNC 阳极/电泳工艺的金属旋钮,可进行音量调节以及一键静音;支持FN组合键实现功能切换;支持全键无冲突; 其上盖采用前卫而富有科技感的碳纤纹理,可带来更加统一协调的视觉体验与触摸手感。现已开启预售,共有六款配色可供选择,售价279元起。 I75超薄矮轴机械键盘:采用75配列布局,上盖边框采用铝合金材质,其键盘最薄处仅8mm,轻薄时尚;共有伯爵黑/皓月白/桃夭粉三种配色可供选择; 其中伯爵黑上盖采用阳极氧化工艺,皓月白/桃夭粉上盖采用陶瓷喷漆工艺,颜值与质感兼备;采用TOP/GAS双结构,F区针对MAC操作系统优化,同时拥有“摸鱼键”可一键复位桌面,种种贴心功能,致力于改善广大用户的移动办公体验。 双·十一优惠持续升级,用户还可关注抖音官方直播间(珂芝KZZI官方旗舰店),我们将在11月11...\n站点: \n溯源地址: https://finance.sina.com.cn/tech/roll/2024-11-04/doc-incuwsqf2220478.shtml\n\n编号: 29\n标题: 黑爵 AK680 MAX 磁轴键盘首销：磁稻轴、TOP 结构，79 元起 - IT之家\n时间: 2024-11-29\n摘要: 黑爵 AK680 MAX 磁轴键盘首销：磁稻轴、TOP 结构，79 元起 - IT之家IT之家 11 月 29 日消息，黑爵 AK680 MAX 磁轴键盘将于今日 20:00 正式开售。 新款键盘搭载磁稻轴、采用 TOP 结构设计，首发 79 元起。 标准版（ABS 双色注塑键帽）：79 元 旗舰版（ABS 双色注塑键帽）：139 元 旗舰版（三拼键帽）：169 元 旗舰版（侧刻渐变）：189 元 旗舰版（等高线）：199 元 京东黑爵（AJAZZ）AK680 MAX 有线磁轴键盘磁稻轴   TOP 结构首发 79 元起直达链接 据介绍，黑爵 AK680 MAX 磁轴键盘拥有标准版和旗舰版两款，均采用有线连接方式，延迟约 0.125ms。 该款键盘支持动态键程自由调节，玩家可根据需求设置 DKS、MT、TGL、按键宏等功能，按键可按照最低 0.01mm 进行个性化设置（标准版为 0.1mm...\n站点: \n溯源地址: https://www.ithome.com/0/814/293.htm\n\n编号: 30\n标题: AJAZZ黑爵K690T无线机械键盘天猫促销价169元\n时间: 2025-03-06\n摘要: AJAZZ黑爵K690T无线机械键盘天猫促销价169元AJAZZ黑爵K690T 69键2.4G蓝牙多模无线机械键盘正在天猫商城促销。 其轴体为BOX白轴（办公推荐），颜色分类是大碳版 - BOX轴 - 68键，套餐类型为官方标配。该键盘参加售价立减30的活动，原价299元，当前到手价只要169元，降价幅度达到43%。 这款K690T是一款非常出色的无线蓝牙机械键盘。它采用68键紧凑型设计，精简机身尺寸，优化后的61键位布局保留了常用按键区域，窄边设计很精巧简约。 它有多种优点，全键无冲设计能适配多种场合，热插拔设计可换轴，玩法多样；采用凯华BOX轴和PBT键帽，支持RGB背光与全键宏编程。连接模式上，支持2.4G无线、蓝牙5.0、有线三种，信号传输稳定，能适配多种操作系统。 \n站点: \n溯源地址: https://finance.sina.com.cn/tech/roll/2025-03-06/doc-inenswii7948946.shtml\n\n编号: 31\n标题: 潮玩前线！珂芝携多款新品参展ZFX 2024 机械键盘_新浪科技_新浪网\n时间: 2024-09-25\n摘要: 潮玩前线！珂芝携多款新品参展ZFX 2024 机械键盘_新浪科技_新浪网装备前线国际数字娱乐设备博览会(简称ZFX2024)，将于2024年10月1日至10月4日携手深圳动玩嘉年华于深圳市福田会展中心联合呈现。 在本届ZFX2024上我们将会带来包括但不限于以下产品，供广大游戏玩家以及键盘爱好者们体验—— 珂芝G68SE电竞磁轴键盘：珂芝磁轴键盘有了新的选择，G68 SE电竞磁轴键盘为你带来更稳的手感，更好的品质！ 首发搭载佳达隆磁白轴，支持全系列N极朝下电竞磁轴，可自由DIY；从0.1mm到4.0mm为范围，支持自定义RT触发，拥有40个可调节段位；支持有线8KHz回报率，8倍速于常规键盘。 K98 PRO低延迟三模键盘：作为我们第一款厚胶位键盘——珂芝K98一经上市便受到玩家们的喜爱，如今针对游戏玩家我们做出全新调整。 \n站点: \n溯源地址: https://finance.sina.com.cn/tech/roll/2024-09-25/doc-incqivrt4050974.shtml\n\n编号: 32\n标题: 珂芝G68SE键盘：性能稳定与兼容性的完美融合\n时间: 2024-08-24\n摘要: 珂芝G68SE键盘：性能稳定与兼容性的完美融合作为一名热爱游戏的玩家，我一直在寻找一款性能卓越、能够满足我各种游戏需求的键盘。 今天，我要向大家推荐的，正是这样一款集性能稳定、兼容性强于一身的电竞神器——珂芝G68SE电竞磁轴键盘。自从它进入我的桌面，无论是深夜的激战还是日常的码字，都为我带来了前所未有的体验。 初见倾心，设计不凡 初见珂芝G68SE，其简约而不失科技感的设计便深深吸引了我。外包装依旧采用的是整个黑色系的盒子搭配上“KZZI”的大logo，加上手绘风格的键盘手稿，让人眼前一亮。 键盘本次一共有紫微星和海王星两种配色，前者是白色键帽的基础上几个紫色大键做点缀，后者则是黑色做底。同时，紧凑的68键布局，既节省了桌面空间，又不失功能性，对于追求效率与整洁的我来说，简直是量身定做。 键盘外壳采用高品质材料，手感细腻，耐用性极佳，即便是长时间使用，也能保持舒适的触感。加上键盘本次是单...\n站点: \n溯源地址: https://t.cj.sina.com.cn/articles/view/3939212872/eacb9e4805500zjps?finpagefr=p_103\n\n编号: 33\n标题: 潮玩前线！珂芝携多款新品参展 ZFX 2024\n时间: 2024-09-25\n摘要: 潮玩前线！珂芝携多款新品参展 ZFX 2024装备前线国际数字娱乐设备博览会 (简称 ZFX2024)，将于 2024 年 10 月 1 日至 10 月 4 日携手深圳动玩嘉年华于深圳市福田会展中心联合呈现。 潮流外设品牌 KZZI 珂芝确认参展 ZFX2024，展台位于 8 号馆 B16。我们期待与广大外设爱好者深入交流，分享体验我们目前最新的产品，探索无限次元，解锁潮玩装备前线。 在本届 ZFX2024 上我们将会带来包括但不限于以下产品，供广大游戏玩家以及键盘爱好者们体验 —— 珂芝 G68SE 电竞磁轴键盘：珂芝磁轴键盘有了新的选择，G68 SE 电竞磁轴键盘为你带来更稳的手感，更好的品质！ 首发搭载佳达隆磁白轴，支持全系列 N 极朝下电竞磁轴，可自由 DIY；从 0.1mm 到 4.0mm 为范围，支持自定义 RT 触发，拥有 40 个可调节段位；支持有线 8KHz 回报率，8 ...\n站点: \n溯源地址: https://www.ithome.com/0/798/240.htm\n\n编号: 34\n标题: 珂芝G68SE电竞磁轴键盘：性能稳定与兼容性的完美融合\n时间: 2024-08-24\n摘要: 珂芝G68SE电竞磁轴键盘：性能稳定与兼容性的完美融合作为一名热爱游戏的玩家，我一直在寻找一款性能卓越、能够满足我各种游戏需求的键盘。 今天，我要向大家推荐的，正是这样一款集性能稳定、兼容性强于一身的电竞神器——珂芝G68SE电竞磁轴键盘。自从它进入我的桌面，无论是深夜的激战还是日常的码字，都为我带来了前所未有的体验。 初见倾心，设计不凡 初见珂芝G68SE，其简约而不失科技感的设计便深深吸引了我。外包装依旧采用的是整个黑色系的盒子搭配上“KZZI”的大logo，加上手绘风格的键盘手稿，让人眼前一亮。 键盘本次一共有紫微星和海王星两种配色，前者是白色键帽的基础上几个紫色大键做点缀，后者则是黑色做底。同时，紧凑的68键布局，既节省了桌面空间，又不失功能性，对于追求效率与整洁的我来说，简直是量身定做。 键盘外壳采用高品质材料，手感细腻，耐用性极佳，即便是长时间使用，也能保持舒适的触感。加上键盘...\n站点: \n溯源地址: https://t.cj.sina.com.cn/articles/view/3939212872/eacb9e4800100zjlg?finpagefr=p_103\n\n编号: 35\n标题: 珂芝双十一AI体验月福利来袭，更有众多新品上新！\n时间: 2024-11-04\n摘要: K87 三模机械键盘：采用87配列布局，非公模ID设计传承珂芝K系列复古设计语言；支持全键热插拔；采用CNC 阳极/电泳工艺的金属旋钮，可进行音量调节以及一键静音；支持FN组合键实现功能切换；支持全键无冲突； 其上盖采用前卫而富有科技感的碳纤纹理，可带来更加统一协调的视觉体验与触摸手感。现已开启预售，共有六款配色可供选择，售价279元起。 I75超薄矮轴机械键盘：采用75配列布局，上盖边框采用铝合金材质，其键盘最薄处仅8mm，轻薄时尚；共有伯爵黑/皓月白/桃夭粉三种配色可供选择； 其中伯爵黑上盖采用阳极氧化工艺，皓月白/桃夭粉上盖采用陶瓷喷漆工艺，颜值与质感兼备；采用TOP/GAS双结构，F区针对MAC操作系统优化，同时拥有“摸鱼键”可一键复位桌面，种种贴心功能，致力于改善广大用户的移动办公体验。 双十一优惠持续升级，用户还可关注抖音官方直播间（珂芝KZZI官方旗舰店），我们将在11月11...\n站点: \n溯源地址: https://finance.sina.com.cn/tech/roll/2024-11-04/doc-incuwfyi5626345.shtml\n\n编号: 36\n标题: 小而全,好看又有料——珂芝K68Pro+K20三模机械键盘上手体验\n时间: \n摘要: 产品包装很复古，简约的白色纸盒上印有产品外形、型号、Logo以及大号书法字体的品牌名称；包装侧面为产品的基本信息：RGB背光、相聚轴、布局、颜色。清单包括：K68Pro机械键盘、K20数字机械键盘、2.4G无线接收器*2、键盘防尘罩、TYPE-C键盘线*2，钢丝拔键器&拔轴器、纸质说明书、产品合格证等 值得一提的是，K68...\n站点: \n溯源地址: https://baijiahao.baidu.com/s?for=pc&id=1785972075105821384&wfr=spider\n\n编号: 37\n标题: 珂芝Z98三模机械键盘AI版,指间灵感,智能化创作风潮引领者-聚超值\n时间: 2024-05-01\n摘要: 一、写在最前： ▲不知道各位小伙伴有没有设想过，机械键盘除了卷轴体卷键帽还会朝那个方向继续进化？ ▲充满创新精神的珂芝设计师这次把AI功效赋能到键盘上，带来珂芝Z98AI版，既有全键功能输入的便利性和缩减配列的简洁性，又有智慧听写AI创作能力的三模机械键盘。 ▲至于基础功能则是覆盖键线分离、蓝牙/有线/2.4G无线三模连接、全键标配RGB灯效、全键轴体支持热插拔和三段式角度调节，可以说是把兼顾颜值和实用性的键盘。 ▲再加上经典的98键布局，适配使用场景广阔，不论是日常办公码字输入，还是玩游戏都非常完美。 珂芝（KZZI）Z98AI版三模机械键盘AI写作问答智能PPT全键热插拔RGB背光全键无冲PBT键帽弥豆紫风雨轴 30天销量500+ ￥599 去看看 二、键盘AI创作、智能听写、智能翻译体验： ▲在使用珂芝Z98AI版进行智能化创作前，PC端必须先安装KZZI AI hub和KZZI-A...\n站点: \n溯源地址: https://best.pconline.com.cn/yuanchuang/31035743.html\n\n编号: 38\n标题: 机械键盘通常伴随着硬朗、酷炫等...@加勒比考斯的动态\n时间: \n摘要: 机械键盘通常伴随着硬朗、酷炫等 关键词一起出现,外形大同小异,看多了着实觉得缺乏新意,有点审美疲劳了。直到看见珂芝 Z98IP定制款三模无线键盘,简直让猛男狂喜,二次元结合机械键盘,碰撞出不一样的火花。 首先简单说一下珂芝这个品牌,珂芝(KZZI)专注于机械键盘领域,注重产品的设计和用户体验,它以提供客制化服务和...\n站点: \n溯源地址: https://mbd.baidu.com/newspage/data/dtlandingsuper?nid=dt_3701295571224616247\n\n编号: 39\n标题: 二次元桌面比上海房价贵？ChinaJoy四大消费趋势-36氪\n时间: 2025-08-07\n摘要: 依托于自有工厂，AKKO 围绕 IP 设计研发相对应的键盘、键帽等。另外，以键帽为例，AKKO 还打破了固有形象，做了许多创新设计。把键帽设计成馒头、猫爪等形状。甚至还根据消费者使用键盘的不同消费场景进行“情绪向研发”。 打工人专用的“牛马键帽”、女生宿舍专用的“暹罗猫键盘”。与其说 AKKO 做得是一把高颜值高品质的外设，不如说，消费者更喜欢用 AKKO 的产品来代表“我是谁”。以及在按下键盘的那一刻，消费者的心情立马变好。 同样的，国内专业做键盘轴体的行业前三「凯华电子」也意识到了年轻人喜欢在键盘上进行客制化（键盘 DIY）的这股消费趋势。品牌考虑到用户不只是为了性能买单，还有轴体触感、颜值等维度，内部会根据热门消费趋势进行研发轴体。 比如，从时节入手，研发端午轴、知夏轴；以食品饮料为灵感，设计出了冰淇淋轴、圣代轴；还会根据时尚潮流趋势设计国风轴体，青黛轴、沐雪轴等，满足消费者对键盘的...\n站点: \n溯源地址: https://www.36kr.com/p/3412610343931272\n\n编号: 40\n标题: 黑爵 AJ179 NL 星闪版系列鼠标开售：三模连接、轻量化设计，89 元起 - IT之家\n时间: 2025-05-29\n摘要: 黑爵 AJ179 NL 星闪版系列鼠标开售：三模连接、轻量化设计，89 元起 - IT之家IT之家 5 月 29 日消息，黑爵 AJ179 NL 星闪版系列鼠标今日官宣开售， 新品支持星闪 / 蓝牙 / 有线三模连接、采用轻量化设计，售价 89 元起。 PAW3311（400mAh）：89 元 PAW3311（800mAh）：109 元 PAW3395（800mAh）：139 元 京东黑爵（AJAZZ）AJ179NL 星闪鼠标三模连接   轻量化设计 89 元起直达链接 据介绍，黑爵 AJ179 NL 星闪版鼠标共有黑、白、玫红 3 款配色可选，采用非对称右手型人体工学设计，适配抓握，趴握、指握。 鼠标不同版本分别搭载原相 PAW3311 / 3395 旗舰传感器，至高支持 26000DPI、650IPS 以及 50G 加速度。 \n站点: \n溯源地址: https://www.ithome.com/0/856/806.htm\n</information>"}]}]
    # instruction = {'messages': messages}
    # response = gemini.image2text_N(instruction, num_samples=2, temperature=0.7)
    response = gpt4o.image2text(instruction, temperature=1.0)
    # response = gemini.image2text(instruction, temperature=1.0)
    print(response[0], '\n\n', response[1])