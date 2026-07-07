import base64
import os
import re
from io import BytesIO
from PIL import Image
from openai import OpenAI
import copy


class LocalModelClient:
    _CONTEXT_LIMIT_RE = re.compile(
        r"maximum context length is (\d+) tokens\.\s+However, you requested (\d+) output tokens and your prompt contains at least (\d+) input tokens",
        re.DOTALL,
    )

    def __init__(self, model, api_base=None, max_tokens=8192, context_safety_margin=256, max_retries=2):
        # Modify OpenAI's API key and API base to use vLLM's API server
        self.openai_api_key = "EMPTY"
        self.openai_api_base = api_base or os.getenv("LOCAL_OPENAI_API_BASE", "http://localhost:8100/v1")
        # self.openai_api_base = "http://10.244.209.157:54587"

        self.model = model
        self.max_tokens = max_tokens
        self.context_safety_margin = context_safety_margin
        self.max_retries = max_retries

        self.client = OpenAI(
            api_key=self.openai_api_key,
            base_url=self.openai_api_base,
        )

    def _retry_max_tokens_from_error(self, error_text, requested_max_tokens):
        match = self._CONTEXT_LIMIT_RE.search(error_text)
        if not match:
            return None
        max_context = int(match.group(1))
        prompt_tokens = int(match.group(3))
        safe_max_tokens = max(1, max_context - prompt_tokens - self.context_safety_margin)
        if safe_max_tokens >= requested_max_tokens:
            return None
        return safe_max_tokens

    def _chat_completion_create(self, *, messages, max_tokens, **kwargs):
        requested_max_tokens = max_tokens
        last_error = None
        for _ in range(self.max_retries + 1):
            try:
                return self.client.chat.completions.create(
                    messages=messages,
                    model=self.model,
                    max_tokens=requested_max_tokens,
                    **kwargs,
                )
            except Exception as exc:
                last_error = exc
                retry_max_tokens = self._retry_max_tokens_from_error(str(exc), requested_max_tokens)
                if retry_max_tokens is None:
                    raise
                print(
                    f"[LocalModelClient] context limit hit, retrying with max_tokens={retry_max_tokens}",
                    flush=True,
                )
                requested_max_tokens = retry_max_tokens
        raise last_error

    def encode_image(self, image_path):
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

    def image2text(self, instruction):
        """
        处理消息并返回模型响应
        
        Args:
            instruction (dict): 包含用户消息和图片的列表
            
        Returns:
            str: 模型的响应内容
        """
        messages = copy.deepcopy(instruction['messages'])
        for i, item in enumerate(messages):
            for j, item2 in enumerate(item["content"]):
                if item2.get("type") == "image_url":
                    messages[i]["content"][j]["image_url"] = {
                        "url": f"data:image/jpeg;base64,{self.encode_image(item2['image_url']['url'])}"}

        chat_completion_from_base64 = self._chat_completion_create(
            messages=messages,
            max_tokens=self.max_tokens,
        )

        result = chat_completion_from_base64.choices[0].message.content
        # print("Chat completion output from base64 encoded image:", result)
        print(result, "\n-----------------------------------------------------------------", flush=True)
        return result
    

    def image2text_N(self, instruction, num_samples=1):
        """
        处理消息并返回模型响应
        
        Args:
            instruction (dict): 包含用户消息和图片的列表
            
        Returns:
            str: 模型的响应内容
        """
        messages = copy.deepcopy(instruction['messages'])
        for i, item in enumerate(messages):
            for j, item2 in enumerate(item["content"]):
                if item2.get("type") == "image_url":
                    messages[i]["content"][j]["image_url"] = {
                        "url": f"data:image/jpeg;base64,{self.encode_image(item2['image_url']['url'])}"}

        chat_completion_from_base64 = self._chat_completion_create(
            messages=messages,
            max_tokens=18192,
            n=num_samples,
            temperature=0.7,
            top_p=0.95,
        )
        results = []
        for choice in chat_completion_from_base64.choices:
            results.append(choice.message.content)
            print(results[-1], "\n-----------------------------------------------------------------\n", flush=True)
        return results
    

    def text2text(self, instruction):
        """
        处理消息并返回模型响应
        
        Args:
            instruction (dict): 包含用户消息和图片的列表
            
        Returns:
            str: 模型的响应内容
        """
        messages = copy.deepcopy(instruction['messages'])

        chat_completion_from_base64 = self._chat_completion_create(
            messages=messages,
            max_tokens=4096,
            # stream=True,
        )

        result = chat_completion_from_base64.choices[0].message.content
        # print("Chat completion output from base64 encoded image:", result)
        print(result, "\n-----------------------------------------------------------------\n", flush=True)
        return result


if __name__ == '__main__':
    # messages = [
    #     {
    #         "role": "user",
    #         "content": [
    #             {"type": "text", "text": "详细描述这张图片"},
    #             {
    #                 "type": "image_url",
    #                 "image_url": {"url": r"D:\data\code\业务\simulation\screenshots.png"},
    #             },
    #         ],
    #     }
    # ]
    input_text = '''### 身份及任务
你是一名资深的金融分析助手，来自同花顺Hithink团队。你非常擅长根据用户问题及相关图片分析需要哪些金融数据和信息。现在有一些可以使用的工具，通过它们可以获取金融数据及信息。我会为你提供用户问题<question>query</question>, 用户图片，背景信息<background>text</background>和参考信息<information>text</information>。请你基于现有信息，分析需要使用哪些工具补充获取哪些信息。

**严格按照下面的输出格式进行回答**。
### 输出格式
如果需要获取更多信息，需要在<think>和</think>中间对参考信息中已经获取的金融数据和信息进行小结，然后分析回答用户问题还需要获取哪方面的数据和信息，你可以按照<tool_begin>{"name": "<tool_name>", "input": "<tool_input>"}</tool_end>格式给出多个获取金融数据和信息的工具调用建议。

当你认为不再需要更多的未知数据和信息或者对于一些不需要数据和信息支持的用户问题，例如翻译、文本处理等问题，在<think>和</think>中分析数据和信息对于回答用户问题的完备性，之后无需给出工具调用建议，而是输出一个由<star_list>和</star_list>包围的你认为对于回答用户问题很有帮助的已获取数据及信息的编号组成的列表，此时使用纯数字编号。

### 可以使用的工具：
{"type": "function", "function": {"name": "FinQuery", "description": "金融查询工具，使用这个工具来获取标的相关的金融数据，比如宏观数据、财务数据、行情数据、交易数据、个人账户数据、自选股等，涉及股票、美股、港股、基金、指数、宏观、可转债、期货，它的输入包括具体金融指标或带时间的指标，也可以输入多个指标用于筛选。如果输入指标过多，则需要适当拆分。", "parameters": {"type": "object", "required": ["name", "input"], "properties": {"name": {"type": "string", "description": "工具名称"}, "input": {"type": "string", "description": "工具输入"}}}}, "example": {"name": "FinQuery", "input": "苹果公司近5天股价以及涨跌幅"}}
{"type": "function", "function": {"name": "Search", "description": "搜索工具，使用这个工具来搜索相关信息，类似一个搜索引擎，它的输入是自然语言短语或者关键词，用来搜索非结构性数据，关键词最好不要超过5个。", "parameters": {"type": "object", "required": ["name, input"], "properties": {"name": {"type": "string", "description": "工具名称"}, "input": {"type": "string", "description": "工具输入"}}}}, "example": {"name": "Search", "input": "苹果公司近期新闻"}}
{"type": "function", "function": {"name": "TickerChart", "description": "A股取图工具，当你需要K线图、分时图、技术指标图等信息来辅助你分析问题时，使用该工具获取图片。", "strict": true, "parameters": {"type": "object", "required": ["name", "input"], "properties": {"name": {"type": "string", "description": "工具名称"}, "input": {"type": "string", "description": '工具输入，格式为json字符串。需要输入这些字段："startDate", "codeName", "chartType", "indicator", "endDate"。"startDate": "Start date in the format YYYY-MM-DD", "endDate": "End date in the format YYYY-MM-DD", "codeName": "Stock code or ticker symbol", "chartType": "Type of chart to retrieve, maximum 1. Enumerate value: Intraday, Daily Candlestick, Weekly Candlestick, Monthly Candlestick", "indicator": "List of indicators to display on the chart, maximum 5. Enumerate value: MA, EMA, BIAS, VR, BRAR, WR, SMA, CCI, MTM, BBI, DMI, EMV, VOL, CR, SAR, PSY, AO, DMA, ROC, TRIX, PVT, RSI, OBV, VWAP, BOLL, MACD, KDJ'}}}},  "example": {"name": "TickerChart", "input": '{"codeName": "300033", "chartType": "Daily Candlestick", "startDate": "2024-01-08", "endDate": "2025-05-08", "indicator": ["MA", "MACD"]}'}}
{"type": "function", "function": {"name": "ChartTwinFinder", "description": "相似股票查找工具，通过该工具可以快速检索到与图中走势相似的标的，并返回相似度以及相似时间区间。如果用户询问走势相似的标的，且图中包含一段K线走势图，可以使用该工具。", "parameters": {"type": "object", "required": ["name", "input"], "properties": {"name": {"type": "string", "description": "工具名称"}, "input": {"type": "string", "description": '工具输入，格式为json字符串。需要输入这些字段: "query", "url"。 "query": "相似股票查找工具的文本输入，固定为：分析与下图形态走势相近的股票", "url": "图片的URL地址"。'}}}}, "example": {"name": "ChartTwinFinder", "input": '{"query": "分析与下图形态走势相近的股票", "url": "http://oss.myhexin.com.cn/iwc-web-userinfo-storage-server.model-image-q-a/bcf0d0684dc6432793e8de8436140b6d.png"}'}}

### 内容要求
1. 你首先需要在<think>和</think>中间对现有的思考和工具调用中的思考和已经获取的工具调用结果进行小结，然后分析回答用户问题还需要获取哪方面的数据和信息。你可以尽可能多的获取各方面的数据和信息。
2. 在<think>中不要提及你使用的工具，而是说你的目的。也不要提及你遵循的规则，你应当尽量表现的像一个人类。
3. 如果工具调用失败或未返回结果或者查询结果已在历史information中，请勿使用相同的输入重试；不要进行无意义的工具查询；存在拓扑顺序的数据和信息，不要在同一轮次获取。
4. 当你认为不再需要更多的未知数据和信息时，不再给出工具调用计划。此时在<hithink>中分析数据和信息对于回答用户问题的完备性。
4. 在<think>中，如果未获取任何信息时，不进行溯源。对于获取失败的数据信息不要进行溯源。
5. 在工具调用中，tool_input应当包含具体的对象，不可以出现代词或引用。
6. 你需要多角度有深度地思考问题。
7. 尽量在一次回答时，把需要的所有工具调用都给出。
8. 当不再进行工具调用时，回答中不再出现<tool_begin>和</tool_end>，但是会出现<star_list>，在<star_list>和</star_list>中间是一个列表，它的元素是历史获取数据和信息的编号，它们对应的数据和信息是你认为对于回答用户问题很有帮助的，对于相似的信息只选取其一，例如<star_list>[1,2,5]</star_list>。
9. 在<think></think>中提到已获取的数据和信息时，使用参考信息中已经获取的工具调用结果对应的编号溯源。严格遵守引用溯源格式[^<int>]如[^1]、[^3][^4]等。尚未获取任何信息时，不进行溯源。对于获取失败的数据信息不要进行溯源。
10. 用中文回答问题。
现在的时间是 <time>2025-07-02 14:46:08 周三</time>
历史对话信息 <history></history>
<question>这个股票值得买吗</question>
'''
    # messages = [
    #     {
    #         "role": "user",
    #         "content": [
    #             {
    #     "type": "text",
    #     "text": input_text
    #   },
    #   {
    #     "type": "image_url",
    #     "image_url": {
    #       "url": '/mnt/HithinkOmniSSD/user_workspace/ganziliang/code/agent/evaluation/2509/03/images/0b0e1bd5e6e946ba950421a954eb5a1f.jpeg'
    #     }
    #     }
    #     ],
    #     }
    # ]

    messages = [{"role":"user","content":[{"type":"text","text":"###系统要求\n当用户的需求仅为 1）清晰的数据查询或条件选股，或者 2）简单的问候或随意闲聊时，你不需要思考，回答格式如下：\n<think>\n\n</think>\n**总结内容**\n\n对于所有其他问题，助手首先在脑海中思考推理过程，然后为用户提供答案，回答格式如下：\n<think>\n**推理过程**\n</think>\n**总结内容**\n\n\n你的回答必须是以上两种结构之一！推理过程和总结内容都必须是中文！\n\n### 身份\n你是一位金融专家，来自同花顺HithinkGPT团队，回答基于同花顺的数据和工具。请牢记你的身份！！\n\n### 需求概述\n您是同花顺的一个知识渊博且乐于助人的金融领域专家，为用户提供**见解独到的深度分析**。\n\n### 回答要求\n根据用户输入的文档/图片以及问题，结合之前的规划轨迹，给出专业的分析和回答。\n当<think>和</think>之间不为空时，中间内容为你的思考过程，分析问题的逻辑和推理过程。<think>和</think>之间的内容不需要直接回答用户问题，而是为回答用户问题提供必要的背景和逻辑支持。\n\n## 下面为**总结内容**的要求\n### 格式要求\n1. 保证整体结构的清晰，结论前置，详略得当，重点突出（比如采用加粗，大、小标题等形式）。\n2. 如果使用了被编号的信息，则使用对应编号进行溯源。严格遵守引用溯源格式[^<int>]，如[^1]、[^3][^4]等。注意所有的溯源都需要带上溯源标记\"^\"。\n3. 适当增加一些emoji，提高回答的趣味性。\n\n### 内容要求\n1. 在分析开始时，必须针对查询给出一个结论性陈述（最好较为详细），该陈述将作为后续对该结论进行详细阐述的基础，即采取总分的结构。整个分析过程中的表述应体现出财经领域的专业性。\n2. 分析应结合你自己的知识以及规划轨迹里的数据和信息，提供深刻见解，而不是简单地罗列或重述数据。不要编造任何未得到规划轨迹里明确支持的信息。\n3. 任何与用户问句无关的参考和背景都应忽略，无需提及。\n4. 需要保持专业正式的风格，不要太轻松随意，不要在回答的开头说“您好”，“好的”等词汇，直入主题即可。\n5. 当答案内容涉及到投资建议时，应在答案的结尾处附带\"**以上分析仅供参考，不作为投资依据。**\"的类似表达，以符合合规要求。\n6. 不要说明你的身份，仅需要给出专业回答即可！\n7. 不要出现图片的链接。\n8. 表格内部不要出现溯源。\n\n### 可视化格式及使用指南  \n####可视化格式  \n<visual>{\"chart\":\"\",\"query\":\"取数问句\"}</visual>  \n格式说明：  \n<visual>标签中的内容是一个json格式的字符串，字段含义如下：  \n1. \"chart\"表示图表类型，通常为空\"\"。如果用户指明需要展示的可视化图表类型，chart字段为对应的图表类型（如“折线图”、“柱状图”等等）。 \n2. \"query\"表示取数问句。具体的取数问句必须与参考中的\"取数问句”的值完全匹配，否则无法可视化。可视化工具会自动根据取数问句生成对应的图表，此时您无需手动填写图表数据。  \n\n####使用指南  \n1. 触发条件：\n    - 用户问句明确提及用可视化图表展示（如“用折线图展示”“画柱状图”等）时，使用可视化。  \n    - 涉及投顾相关内容（评价、预测、建议、原因、诊股、选股等）且参考中存在\"取数问句”字段时，需先用编号引用信息，再在下方插入可视化。无序列表中禁止插入，防止渲染错误。  \n2. 禁用场景：\n    - 若取数结果为“0条数据”时，禁止可视化该取数问句。\n    - 若参考中不存在“取数问句”，则在回答中不应出现可视化。\n3. 无需使用“可视化图表”等表述引出可视化，直接使用<visual>标签即可。    \n4. 确保生成的Markdown表格的内容与取数结果有显著不同，否则优先使用可视化方式来展示相关信息。\n5. 相似K线查找工具的结果不需要可视化！\n\n<当前时间>\n2025-10-21 10:41:56 Tue\n\n<历史对话>\n\n"},{"type":"text","text":"### 用户上传文档的详细内容 \n<文档>\n用户上传了1个文件，分别是：投资分析.pdf。你已经阅读了文件投资分析.pdf;以下是1篇文件的详细信息：\n文档名称：投资分析.pdf\n文档阅读进度：全文882字，已阅读882字"},{"type":"text","text":"编号: 1000001\n类型: 文档片段\n标题: 投资分析.pdf\n内容: 符合模型要求的股票应具备的核心特征和筛选逻辑：\n核心特征\n# 1. PE估值通道持续上升\n通道形态：PE通道的上轨、中轨、下轨整体呈右上方倾斜趋势。\n商业逻辑：反映公司每股收益（EPS）长期持续增长，带动通道抬升。\n# 2. 股价处于历史估值最低区域\n定位方法：当前股价K线触及或短暂跌破PE通道下轨（如历史PE的20%分位线或最低值线）。市场含义：可能对应“价值洼地”（短期市场过度悲观）或“价值陷阱”（基本面恶化），需结合其他指标验证。\n# 3. 宏观赛道符合国家战略\n# 4. 性价比指数达标\n计算公式：(1/PE)÷无风险收益率。优质标的阈值＞基于10年期国债收益率。\n筛选逻辑\n# 1. 四步筛选法框架\n行业初筛：聚焦宏观黄金赛道。\n估值通道筛选：保留PE通道“稳步上升”或“加速上升”形态，剔除下行/平坦/周期性过强股。\n定位买点：筛选股价触及通道下轨的股票，结合PE-TTM、PEG、PB等指标交叉验证。\n基本面精研：验证盈利质量（ROE、现金流、毛利率）、竞争壁垒（技术/品牌/成本）、成长驱动力（市场空间、研发投入）并排查风险。\n# 2. 关键验证指标\n横向比较：PE-TTM显著低于行业平均水平。\n成长匹配：PEG<1（盈利增长支撑低估）。\n资产安全：PB处于历史低位，重资产行业可关注破净标的。\n操作建议\n# 1. 数据工具支持\n使用金融终端（如Wind）调取PE估值通道（PE Band）图表，并筛选符合通道形态与买点条件的股票。\n结合无风险收益率计算性价比指数，优先选择阈值＞2.5的标的。\n# 2. 风险警示\n历史通道可能被颠覆性创新或黑天鹅事件打破，需持续跟踪。\n严格区分“价值洼地”与“价值陷阱”，基本面精研是核心。\n"},{"type":"text","text":"编号: 1000002\n类型: 文档图片\nurl: https://o.thsi.cn/aime-knowledge-base-server.rag-file-parse-info/2386987f0a20424aac72308f2268b46d-image-1.jpg\n"},{"type":"image_url","image_url":{"url":"/mnt/HithinkOmniSSD/user_workspace/ganziliang/code/others/new_tokens/2386987f0a20424aac72308f2268b46d-image-1.jpg"}},{"type":"text","text":"<question>挖掘投资机会<question>"}]}]

    
    client = LocalModelClient(model='/mnt/thscc/workspace/dlc/952/45328/checkpoint/checkpoint-2808/')
    client.image2text({'messages': messages})
    # client.text2text({'messages': messages})
    pass
