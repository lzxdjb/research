import json
import traceback
import aiohttp
import asyncio
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

class AsyncLLMClient:
    def __init__(self, app_id, app_secret, url, chat_url, model, timeout=60,Semaphore=20):
        """
        初始化通用参数

        Args:
            app_id (str): 应用ID
            app_secret (str): 应用密钥
            url (str): 认证接口URL
            chat_url (str): 对话接口URL
            model (str): 使用的模型名称
            timeout (int): 请求超时时间，默认60秒
        """
        self.app_id = app_id
        self.app_secret = app_secret
        self.url = url
        self.chat_url = chat_url
        self.model = model
        self.timeout = timeout
        self.Semaphore = Semaphore
        self.authority = None
        self.session = None

    async def initialize(self):
        """初始化session"""
        if self.session is None:
            self.session = aiohttp.ClientSession()
        return self

    async def close(self):
        """关闭session"""
        if self.session:
            await self.session.close()
            self.session = None

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
    
    async def refresh_authority(self):
        """刷新身份信息"""
        if self.session is None:
            await self.initialize()
            
        d = {"app_id": self.app_id, "app_secret": self.app_secret}
        h = {"Content-Type": "application/json"}
        
        async with self.session.post(self.url, json=d, headers=h, timeout=self.timeout) as response:
            data = await response.json()
            # print("[Claude Auth Token] =>", data)
            if data.get("success"):
                self.authority = data
            else:
                raise Exception(f"Failed to authenticate: {data}")

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
    def save_file(path,instruction,predict_result):
        with open(path,'a',encoding='utf-8') as f: 
            tmp = instruction
            tmp['reasoning_content'] = predict_result['reasoning_content'] if isinstance(predict_result, dict) and 'reasoning_content' in predict_result else ""
            tmp['predict_result'] = predict_result['content'] if isinstance(predict_result, dict) and 'content' in predict_result else predict_result

            f.write(json.dumps(tmp,ensure_ascii=False)+"\n")
 
    async def get_gpt_response(self, messages, chat_url, temperature=0, try_num=10, pbar=None):
        """
        大模型调用和身份验证

        Args:
            messages (Obj): messages模式请求格式
            chat_url(str): 对话接口URL
            temperature(float): 调用接口的温度
            try_num(int): retry最大次数
            pbar (tqdm): 进度条对象，可选
        Returns:
            Dict: 接口返回的text或者错误
        """
        if self.session is None:
            await self.initialize()
        # logger.info(f"刚输入的message为{messages}")

        error_messages = ""
        try:
            assert isinstance(messages, list) and not [x for x in messages if x["role"] not in {"system", "user", "assistant"}]
            # logger.info(f"加工后的message为{messages}")
            # messages = [{'role': 'system', 'content': '早上好'},{'role': 'user', 'content': '今天是几号'}]
            chat_d = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature
            }
            # logger.info(f"构建完的chat_d为{chat_d}")
            if not self.authority:
                await self.refresh_authority()
            chat_h = {
                "Content-Type": "application/json",
                "userId": self.authority["data"]["user_id"],
                "token": self.authority["data"]["token"]
            }
        except Exception as e:
            error_messages = traceback.format_exc()
            # logger.error(error_messages)
            return "error: "+error_messages

        for try_id in range(try_num):
            try:
                async with self.session.post(chat_url, json=chat_d, headers=chat_h, timeout=self.timeout) as response:
                    response_data = await response.json()
                    choices = self.find_key_in_nested_dict(response_data, 'choices')
                    if 'success' in response_data and not response_data.get('success'):
                        # logger.error("Retry time:{}\n{}".format(try_id,response_data))
                        continue
                    if choices is None:
                        # logger.error("Retry time:{}\n{}".format(try_id,response_data))
                        continue
                    if pbar:
                        pbar.update(1)
                    logger.debug(response_data)
                    result = choices[0]["message"] if "reasoning_content" in choices[0]["message"] else choices[0]["message"]["content"]
                    return result
            except Exception as e:
                error_messages = traceback.format_exc()
                # logger.error("Retry time:{}\n{}".format(try_id,error_messages))
                await self.refresh_authority()
        return "error: "+str(error_messages)

    async def text2text(self, instruction, temperature=0,pbar=None,output_file=None):
        """
        大模型文本生成文本

        Args:
            instruction (str): input 文本
            temperature (float): 调用接口的温度
        Returns:
            Dict: 接口返回的text或者错误
        """
        if isinstance(instruction, str):
            messages = [
                {"role": "user", "content": instruction}
            ]
            instruction = {'messages':messages}
        else:
            messages = instruction['messages']
        # messages = [{'role': 'system', 'content': '你是None，想在券商开户投资。目前的场景是在网上办理开户业务。\n            - 当前情绪：学习，认真学习投资技巧,请对客服人员的反应按照你当前的情绪进行演绎，确保你的演绎符合你的情绪和在网上开户的场景\n            - 你的邮编是000000\n            - 你的转户id是111111\n            - 不要在客服询问当前问题的时候跳到后面的问题去，只有客服引导回答下一个问题了才能回答这个问题\n            你的完整信息：\n            - 收集用户手机号: 19614074628\n- 收集用户短信验证码: 8717\n- 姓名: 吕忠波\n- 性别: 女\n- 出生日期: 1990年10月19日\n- 民族: 仫佬族\n- 公民身份号码: 110101199202258498\n- 收集用户学历: 小学二年级\n- 收集用户职业和行业: 铸造工\n- 收集用户年收入: 64\n- 收集用户联系地址: 上海市长宁区人民路773号30单元154室\n- 收集用户邮政编码: 000000\n- 收集用户想要开立的账户类型: 深A\n- 请用户选择开立账户操作类型: 转户\n- 开户成功: 开户成功\n\n            - 请确保你的说话方式符合人物设定\n            - 请确保此时谈论的话题仅和开户成功相关，与流程在开户成功之后的客户信息无关\n            - 配合客服进行开户业务,不会主动提供任何信息，需要客服进行引导才会说出需要提供的信息\n            - 在网络上开户的时候会出现各种各样的意外，请充分发挥并演绎\n            性格特点：\n            - 对开户有兴趣，说话方式口语化\n            - 回答问题时要自然，要像真实用户一样回答\n            '}, {'role': 'user', 'content': '请分批次给出你的手机号,最多分五轮，按手机号顺序每轮对话给出一部分手机号，不要一次性把完整手机号给出去\n现在是第1轮对话.\nerror: '}]
        # logger.info(f"输入到接口前的内容为：{messages}")
        predict_result = await self.get_gpt_response(messages, self.chat_url, temperature=temperature,pbar=pbar)
        if output_file:
            self.save_file(output_file,instruction,predict_result)
        return predict_result
    
    async def image2text(self, instruction, image=None, temperature=0,pbar=None,output_file=None):
        """
        大模型图文生成文本

        Args:
            instruction(str): input 文本 或者message格式的list
            image (str): 图片路径，当传入message时可以是空
            temperature (float): 调用接口的温度
        Returns:
            Dict: 接口返回的text或者错误
        """
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
            for i,item in enumerate(messages):
                for j, item2 in enumerate(item["content"]):
                    if item2.get("type") == "image_url":
                        messages[i]["content"][j]["image_url"] = {
                            "url": f"data:image/jpeg;base64,{self._encode_image(item2['image_url']['url'])}"
                        }
        
        predict_result = await self.get_gpt_response(messages, self.chat_url, temperature,pbar=pbar)
        if output_file:
            self.save_file(output_file,instruction,predict_result)
        return predict_result

    async def images2texts(self, instructions: list, temperature=0, images: list=None,output_file=None):
        
        """
        大模型批量图文生成文本

        Args:
            instructions (list): input 一个列表，包含多个instruction,可以是messages格式
            images (list): 图片路径列表
            temperature (float): 调用接口的温度
        Returns:
            List: 接口返回一个结果列表，结果列表中元素顺序和instructions中元素顺序一致
        """
        await self.initialize()  # 确保session已初始化
        
        # 创建进度条
        pbar = tqdm(total=len(instructions), desc="Processing Image-Text Pairs")

        semaphore = asyncio.Semaphore(self.Semaphore)
        async def process_with_semaphore(instruction, image=None):
            async with semaphore:  # 使用信号量控制并发
                return await self.image2text(instruction, image, temperature, pbar=pbar, output_file=output_file)
    
        try:
            if images:
                tasks = [process_with_semaphore(instruction, image) 
                        for instruction, image in zip(instructions, images)]
            else:
                tasks = [process_with_semaphore(instruction) 
                        for instruction in instructions]
                
            # 执行所有任务
            results = await asyncio.gather(*tasks)
        finally:
            pbar.close()
            
        # 确保结果顺序与输入顺序一致
        return results

    async def texts2texts(self, instructions: list, temperature=0,output_file=None):
        """
        大模型批量文本生成文本

        Args:
            instructions (list): input 一个列表，包含多个instruction,可以是messages格式
            temperature (float): 调用接口的温度
        Returns:
            List: 接口返回一个结果列表，结果列表中元素顺序和instructions中元素顺序一致
        """
        await self.initialize()  # 确保session已初始化
        text_messages = []
        if not isinstance(instructions[0], str):
            for i,item in enumerate(instructions):
                if "messages" in item:
                    item = item['messages']
                tmp = []
                for text_item in item:
                    tmp.append({
                        "role": text_item["role"],
                        "content": "\n".join([x["text"] for x in text_item["content"]]) if isinstance(text_item["content"], list) else text_item["content"]
                    })
                instructions[i]['messages'] = tmp
        # print(text_messages)
        # 创建进度条
        pbar = tqdm(total=len(instructions), desc="Processing Text-to-Text")
        
        # 创建信号量来限制并发
        semaphore = asyncio.Semaphore(self.Semaphore)
        
        # 创建一个包装函数，在其中使用信号量
        async def process_with_semaphore(message):
            async with semaphore:  # 使用信号量控制并发
                return await self.text2text(message, temperature=temperature, pbar=pbar, output_file=output_file)
        
        try:
            # 使用包装函数创建任务
            tasks = [process_with_semaphore(message) for message in instructions]
            
            # 执行所有任务
            results = await asyncio.gather(*tasks)
        finally:
            pbar.close()

        return results

class GPT4OClient(AsyncLLMClient):
    def __init__(self, app_id="5cafd46a3b2342b5a903afafc38d4aef",
        app_secret="iv+JZxHTQKrpYmxx1U9HNyXEjJNZcUhlBCkmT/lSYIE=",
        url="https://arsenal-openai.10jqka.com.cn:8443/vtuber/auth/api/oauth/v1/login",
        chat_url="https://arsenal-openai.10jqka.com.cn:8443/vtuber/ai_access/chatgpt/v3/chat/completions",
        model="gpt-4o",timeout=60):
        super().__init__(app_id, app_secret, url, chat_url, model, timeout)

    async def text2image(self, prompt, output_path):
        """
        根据prompt生成图片并保存
        Args:
            prompt (str): 描述生成图片内容的文本
            output_path (str): 图片保存路径
        Returns:
            str: 图片保存路径
        """
        # 这里假设使用OpenAI DALL·E API（或其他支持的API），需要openai包和api_key
        try:
            import openai
            # 这里假设api_key已配置为环境变量，或可直接在此处指定
            client = openai.OpenAI()
            response = client.images.generate(
                model="dall-e-3",
                prompt=prompt,
                n=1,
                size="1024x1024"
            )
            image_url = response.data[0].url
            # 下载图片并保存到output_path
            import requests
            img_resp = requests.get(image_url)
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, "wb") as f:
                f.write(img_resp.content)
            return output_path
        except Exception as e:
            print(f"图片生成失败: {e}")
            raise

class KIMIClient(AsyncLLMClient):
    def __init__(self, app_id="5cafd46a3b2342b5a903afafc38d4aef",
        app_secret="iv+JZxHTQKrpYmxx1U9HNyXEjJNZcUhlBCkmT/lSYIE=",
        url="https://arsenal-openai.10jqka.com.cn:8443/vtuber/auth/api/oauth/v1/login",
        chat_url="https://arsenal-openai.10jqka.com.cn:8443/vtuber/ai_access/kimi_moonshot/v1/chat/completions",
        model="moonshot-v1-8k",timeout=60):
        """
        KIMI初始化参数，注意KIMI不支持多模态数据

        Args:
            app_id (str): 应用ID
            app_secret (str): 应用密钥
            url (str): 认证接口URL
            chat_url (str): 对话接口URL
            model (str): 使用的模型名称
            timeout (int): 请求超时时间，默认800秒
        """
        super().__init__(app_id, app_secret, url, chat_url, model, timeout)

class CLAUDEClient(AsyncLLMClient):
    def __init__(self, app_id="5cafd46a3b2342b5a903afafc38d4aef",
        app_secret="iv+JZxHTQKrpYmxx1U9HNyXEjJNZcUhlBCkmT/lSYIE=",
        url="https://arsenal-openai.10jqka.com.cn:8443/vtuber/auth/api/oauth/v1/login",
        chat_url="https://arsenal-openai.10jqka.com.cn:8443/vtuber/ai_access/claude/v1/chat/completions",
        model="us.anthropic.claude-3-5-sonnet-20241022-v2:0",timeout=60):
        super().__init__(app_id, app_secret, url, chat_url, model, timeout)

class GEMINIClient(AsyncLLMClient):
    def __init__(self, app_id="e2e4903b01b549e1813317b7f8173465",
        app_secret="Gvdoa07eFcHrlRQJurJHh0eJK211ppzs9dKOJKuHS7o=",
        url="https://arsenal-openai.10jqka.com.cn:8443/vtuber/auth/api/oauth/v1/login",
        chat_url="https://arsenal-openai.10jqka.com.cn:8443/vtuber/ai_access/gemini/v1/chat/completions",
        model="gemini-2.0-flash",timeout=60):
        super().__init__(app_id, app_secret, url, chat_url, model, timeout,Semaphore=5)
    
class DEEPSEEKClient(AsyncLLMClient):
    def __init__(self, app_id="5cafd46a3b2342b5a903afafc38d4aef",
        app_secret="iv+JZxHTQKrpYmxx1U9HNyXEjJNZcUhlBCkmT/lSYIE=",
        url="https://arsenal-openai.10jqka.com.cn:8443/vtuber/auth/api/oauth/v1/login",
        chat_url="https://arsenal-openai.10jqka.com.cn:8443/vtuber/ai_access/doubao/v3/chat/completions",
        model="ep-20250204210426-gclbn",timeout=200):
        super().__init__(app_id, app_secret, url, chat_url, model, timeout,Semaphore=5)


class QWEN_3_5OMNIClient(AsyncLLMClient):
    """异步千问网关客户端（默认 qwen3-vl-235b-a22b-thinking，可改 model 为 omni-plus/flash 等）。"""

    def __init__(self, app_id="5cafd46a3b2342b5a903afafc38d4aef",
        app_secret="iv+JZxHTQKrpYmxx1U9HNyXEjJNZcUhlBCkmT/lSYIE=",
        url="https://arsenal-openai.10jqka.com.cn:8443/vtuber/auth/api/oauth/v1/login",
        chat_url="https://arsenal-openai.10jqka.com.cn:8443/vtuber/ai_access/qianwen/v1/chat/completions",
        model="qwen3-vl-235b-a22b-thinking", timeout=200):
        super().__init__(app_id, app_secret, url, chat_url, model, timeout, Semaphore=5)


class OtherGPTClient:
    def __init__(self, api_key,base_url,model):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.client = OpenAI(api_key=api_key, base_url=base_url)

    def text2text(self, instruction,temperature=0.0):
        """
        大模型文本生成文本

        Args:
            instruction (str):input 文本
            temperature (float):调用接口的温度
        Returns:
            Dict: 接口返回的text或者错误
        """
        try:
            client = self.client
            response = client.chat.completions.create(
                model=self.model,  # 默认模型，可替换为其他模型
                messages=[
                    {"role": "system", "content": instruction},
                ],
                stream=False,
                temperature=temperature
            )
            return response.choices[0].message.content
        except Exception as e:
            # logger.error(f"{e}")
            return None

async def debug():
    logger.remove()
    logger.add(sink=sys.stderr, level="DEBUG")

    gpt4o = GPT4OClient()
    ds = DEEPSEEKClient()
    service_gpt = CLAUDEClient()
    gemini = GEMINIClient()
    '''
    # with open('/mnt/data/users/xiongkai/datasets/training_data/jinglianwen_example_pass.jsonl','r') as f:
    #     data = [json.loads(l) for l in f]
    
    instruction = {"messages":[
        {
            "role": "user",
            "content": "你好你是谁nenene"
        },
        {
            "role": "assistant",
            "content": "你好！我是一个人工智能助手，可以帮助回答问题、提供信息或协助解决问题。如果你有任何问题或需要帮助，请随时告诉我！"
        },
        {
            "role": "user",
            "content": "明天天气如何"
        }
    ]}
    '''
    # gpt4o_result = await gpt4o.text2text("是否可以用世界上最大的湖填满世界上最大的沙漠？")
    # ds_result = await ds.texts2texts(data,output_file="reasoning.jsonl")
    # image_result =await gpt4o.images2texts(instructions = ["描述一下这张图片","描述一下这张图片"], images=["/mnt/data/damien/LCK作业/images/客服.png","/mnt/data/damien/LCK作业/images/客服.png"],output_file="reasoning.jsonl")
    
    claude_result = await service_gpt.text2text("[{'role': 'system', 'content': '你是同花顺客服小顺，说话口语化，感性思维主导。\n            你需要让客户说出所需要的开户信息来帮助客户进行开户。\n            请不要使用任何书面用语，要尽量模仿人能说出来的话，保持感情充沛，积极和热情\n\n            需要收集的信息清单：\n            - 收集用户手机号\n- 收集用户短信验证码\n- 姓名\n- 性别\n- 出生日期\n- 民族\n- 公民身份号码\n- 收集用户学历\n- 收集用户职业和行业\n- 收集用户年收入\n- 收集用户联系地址\n- 收集用户邮政编码\n- 收集用户想要开立的账户类型\n- 请用户选择开立账户操作类型\n- 开户成功\n\n\n            \n            当你第一次开始对话时，请先自我介绍并欢迎客户来同花顺办理开设股票账户的业务\n            - 如果用户是新开户，即使你的目标是收集用户的转户id，你也不要去收集用户的转户id，因为用户没有转户id，请告知用户他没有转户id\n            要求：\n            - 不要推测客户的性别，不要重复客户名字\n            - 热情专业但简洁，通过自然对话引导客户提供信息\n            - 保持对话连贯性，不要重复自我介绍\n            - 当收集到一个信息后，自然地过渡到下一个信息的询问\n            - 根据已有的对话历史调整话术\n            '}]")
    # gemini_result = await gemini.text2text(instruction)
    
    print(claude_result)

# 示例用法
if __name__ == "__main__":
    asyncio.run(debug())
    # instructions = ['你是小明，想在券商开户投资。目前的场景是在网上办理开户业务。- 当前情绪：开心,请对客服人员的反应按照你当前的情绪进行演绎，确保你的演绎符合你的情绪和在网上开户的场景- 你的邮编是000000- 你的转户id是111111- 不要在客服询问当前问题的时候跳到后面的问题去，只有客服引导回答下一个问题了才能回答这个问题.你的完整信息：']
    # results = claude.texts2texts(instructions)
    # print(results)
    # image_result = gpt4o.images2texts(["描述一下这张图片","描述一下这张图片"],["/mnt/data/damien/LCK作业/images/客服.png","/mnt/data/damien/LCK作业/images/客服.png"])
    # print(image_result)