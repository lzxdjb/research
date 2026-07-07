from api import GEMINIClient
import json
import re
import json_repair
from datetime import datetime
from out_tools import check_tool_calls


# ---------- Plan Prompt: 规划prompt ----------
PLAN_PROMPT = '''你是一名**专业的回答评分助手**，用于比较agent不同回答的质量。

你需要参考context_content，比较不同answer_content的好坏。
【输入】
- context_content：本轮之前的所有对话历史，包括工具的返回内容。
- answer_content：本轮agent的多次采样结果，以及调用工具是否成功的说明(例如[True, False, True]，这里False代表第2个工具调用失败）。它们都有编号，如1，2，3。

【行动】
你首先应该根据context_content判断下一步动作是继续规划还是结束规划。那些与你判断一致的回答加4分，不一致则不加分。
随后你需要根据调用工具是否成功对回答进行扣分，有一个False，扣2分，最多扣6分。
然后你需要根据【评分标准】使用扣分制得到各个回答的分数。随后基于分数，并参考【排序标准】将所有的回答进行质量排序。 

【评分标准】
1. 输出格式需要满足要求，每处不满足要求，扣2分，最多扣6分。
2. 工具调用格式需要满足要求，每处不满足要求，扣2分，最多扣6分。
3. 输出内容遵循要求的原则，每处不遵循原则，扣2分，最多扣6分。  
4. 思考真实性幻觉扣2分  
5. 思考-行动不一致扣2分  
6. 语言与格式：非简中/病句/错别字/标点错，每类扣1分，最多扣3分
7. 规划时存在假设逻辑，如“假设用户想要了解某个方面的信息，假设查询某支股票”，每处扣2分，最多扣6分。
8. 与之前轮词回复内容重复，每处扣3分，最多扣6分。
9. FinQuery工具的输入包含“图中”等字符，每处扣2分，最多扣6分。
10. ChartTwinFinder工具输入的图片url在前文未出现过，每处扣2分，最多扣6分。
11. 使用TickerChart或ChartTwinFinder工具，且有返回结果的，每处加2分，最多加6分。

【排序标准】
1. 优先按照分数进行排序，分数相同时，考虑后续标准。
2. 思考专业性、综合性越高，排序越靠前。
3. 工具调用丰富度越高，工具输入越多样、综合，排序越高。

【输出】
输出一个json字符串，首先是'assign_score'字段，它内容为每个回复的打分理由和打分结果。然后是'sort_answer'字段，它内容为排序的理由和排序列表（如[1,3,2,5,4])。
输出示例：{"assign_score": {"answer_1": {"score_reason": 'xxx', 'score': 0}, "answer_2": {"score_reason": 'xxx', "score": -3}, "answer_3": {"score_reason": 'xxx', "score": -2}, "answer_4": {"score_reason": 'xxx', "score": -7}, "answer_5": {"score_reason": 'xxx', "score": -3}}, "sort_answer": {"sort_reason": 'xxx', "sorted_list": [1,3,2,5,4]}}
'''


# ---------- Summary Prompt: 总结prompt ----------
SUMMARY_PROMPT = '''你是一名**专业的回答评分助手**，用于比较agent不同回答的质量。

你需要参考context_content，比较不同answer_content的好坏。
【输入】
- context_content：所有的背景信息。
- answer_content：本轮agent的多次采样结果，它们都有编号，如1，2，3。

【行动】
然后你需要根据【评分标准】使用扣分制得到各个回答的分数。随后基于分数，并参考【排序标准】将所有的回答进行质量排序。 

【评分标准】
1. 模型输出符合系统要求，不满足扣4分。 
2. 模型输出满足回答要求，不满足扣2分。
3. 输出格式需要满足**总结内容**的格式要求，每处不满足要求，扣2分，最多扣6分。
4. 输出的**总结内容**符合内容要求，每处不满足要求，扣2分，最多扣6分。  
5. 输出语言风格满足语言分隔要求，每处不满足扣1分，最多扣3分。
6. 可视化使用满足使用指南，每处不满足扣2分。
7. 可视化格式满足要求，不满足扣2分。
8. 思考真实性幻觉扣2分。
9. 思考-行动不一致扣2分。  
10. 溯源很重要，每个溯源不符合格式要求的扣2分，最多扣8分。
11. 语言与格式：非简中/病句/错别字/标点错，每类扣1分，最多扣3分。

【排序标准】
1. 优先按照分数进行排序，分数相同时，考虑后续标准。
2. 思考专业性、综合性越高，排序越靠前。
3. 工具调用丰富度越高，工具输入越多样、综合，排序越高。
4. 回答内容采取总分结构的排序更高。

【输出】
输出一个json字符串，首先是'assign_score'字段，它内容为每个回复的打分理由和打分结果。然后是'sort_answer'字段，它内容为排序的理由和排序列表（如[1,3,2,5,4])，越好的回答越排在前面。
输出示例：{"assign_score": {"answer_1": {"score_reason": 'xxx', 'score': 0}, "answer_2": {"score_reason": 'xxx', "score": -3}, "answer_3": {"score_reason": 'xxx', "score": -2}, "answer_4": {"score_reason": 'xxx', "score": -7}, "answer_5": {"score_reason": 'xxx', "score": -3}}, "sort_answer": {"sort_reason": 'xxx', "sorted_list": [1,3,2,5,4]}}
'''


def process_tools_results(full_tools_results, best_result_str):
    text_info_list = []
    image_info_list = []
    if len(full_tools_results) == 0:
        return [{'type': 'text', 'text': '<information>\n</information>'}]
    for tool_result in full_tools_results:
        if isinstance(tool_result, str):
            text_info_list.append(tool_result)
        else:
            image_info_list.append(tool_result)

    star_list = re.search(r'<star_list>(.*?)</star_list>', best_result_str, re.DOTALL)
    if star_list:
        star_list = eval(star_list.group(1))
    else:
        star_list = [i+1 for i in range(len(text_info_list))]
    info_text = ''
    output_list = []
    for idx in range(len(text_info_list)):
        text_info_idx = text_info_list[idx]
        if idx+1 not in star_list: # 不是精选就去除全文
            text_info_idx_cleaned = re.sub(r'内容: .*?站点:\n溯源地址：', '内容:\n站点:\n溯源地址：', text_info_idx, flags=re.DOTALL)
            info_text += f'编号: {idx+1}\n{text_info_idx_cleaned}\n'
        else:
            info_text += f'编号: {idx+1}\n{text_info_idx}\n'
    output_list.append({'type': 'text', 'text': f'<information>{info_text}</information>'})
    if len(image_info_list) > 0:
        for image_info in image_info_list:
            output_list.extend(image_info)
    return output_list


def assign_plan_score(input, online_client):
    context = []
    for i in range(len(input['messages'])):
        context.extend(input['messages'][i]['content']) # 获取所有的上下文
    answers = input['choices']
    prompt = PLAN_PROMPT

    answers_content = []
    for i, answer in enumerate(answers):
        answer_text = answer['message']['content'][0]['text']
        tool_calls = []
        if 'ActionList' in answer_text:  
            tool_calls = parse_simple_tools(answer_text)
        if '<tool_begin>' in answer_text:
            tool_calls = parse_deep_tools(answer_text)
        checked_tool_calls = check_tool_calls(tool_calls) if len(tool_calls) > 0 else []
        checked_tool_calls_str = str(checked_tool_calls)
        answers_content.append({'type': 'text', 'text': f'answer_{i+1}:\n{answer_text}\n\n工具调用结果说明:{checked_tool_calls_str}\n'})

    instruction = {
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": prompt
                }
            ]
        }]
    }
    instruction['messages'][0]['content'].append({"type": "text", "text": f"context_content: \n"})
    instruction['messages'][0]['content'].extend(context)
    instruction['messages'][0]['content'].append({"type": "text", "text": "\nanswer_content: \n"})
    instruction['messages'][0]['content'].extend(answers_content)
    client_result = online_client.image2text(instruction)
    client_result = json_repair.loads(client_result)
    for i, answer in enumerate(answers):
        answer['score'] = client_result['assign_score'][f'answer_{i+1}']['score']
        answer['score_reason'] = client_result['assign_score'][f'answer_{i+1}']['score_reason']
    sorted_list = client_result['sort_answer']['sorted_list']
    sorted_answers = [answers[i-1] for i in sorted_list]
    
    sort_reason = client_result['sort_answer']['sort_reason']  
    input['sort_reason'] = sort_reason 
    input['choices'] = sorted_answers
    input['sort_list'] = sorted_list
    return input


def assign_summary_score(input, online_client):
    context = []
    for i in range(len(input['messages'])):
        context.extend(input['messages'][i]['content']) # 获取所有的上下文
    answers = input['choices']
    prompt = SUMMARY_PROMPT
    answers_content = []
    for i, answer in enumerate(answers):
        answer_text = answer['message']['content'][0]['text']
        answers_content.append({'type': 'text', 'text': f'answer_{i+1}:\n{answer_text}\n'})

    instruction = {
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": prompt
                }
            ]
        }]
    }
    instruction['messages'][0]['content'].append({"type": "text", "text": f"\ncontext_content: \n"})
    instruction['messages'][0]['content'].extend(context)
    instruction['messages'][0]['content'].append({"type": "text", "text": "\nanswer_content: \n"})
    instruction['messages'][0]['content'].extend(answers_content)
    client_result = online_client.image2text(instruction)
    client_result = json_repair.loads(client_result)
    for i, answer in enumerate(answers):
        answer['score'] = client_result['assign_score'][f'answer_{i+1}']['score']
        answer['score_reason'] = client_result['assign_score'][f'answer_{i+1}']['score_reason']
    sorted_list = client_result['sort_answer']['sorted_list']
    sorted_answers = [answers[i-1] for i in sorted_list]
    
    sort_reason = client_result['sort_answer']['sort_reason']  
    input['sort_reason'] = sort_reason 
    input['choices'] = sorted_answers
    input['sort_list'] = sorted_list
    return input


def get_query(question, time):
    times_str = f'<time>\n{time}\n</time>'
    question_str = f'\n<question>\n{question}\n</question>'
    image_str = '\n用户上传的图片：'
    return times_str + question_str + image_str

def get_long_query(question, time):
    times_str = f'<time>\n{time}\n</time>'
    question_str = f'\n<question>\n{question}\n</question>'
    return times_str + question_str


def get_summary_query(question, time):
    times_str = f'### 当前时间\n{time}\n'
    history_str = '### 对话\n<历史对话>\n'
    question_str = f'<用户问题>\n{question}'
    image_str = '\n用户上传的图片：'
    return times_str + question_str + image_str


def get_long_summary_query(question, time):
    times_str = f'### 当前时间\n{time}\n'
    history_str = '### 对话\n<历史对话>\n'
    question_str = f'<用户问题>\n{question}'
    return times_str + question_str


def check_deep_planning_result(best_result_str):
    if '<star_list>' in best_result_str and '</star_list>' in best_result_str:
        return False
    return True


def check_simple_planning_result(best_result_str):
    if '<FINISHED>' in best_result_str:
        return False
    return True


def get_summary_document(document, time):
    question_str = f'<用户文档>\n{document}'
    return question_str


def print_current_time(label: str = ''):
    current_time = datetime.now()
    print(f'[{current_time}] {label}', flush=True) 


def parse_deep_tools(planning_results):
    tools = []
    pattern = r'<tool_begin>(.*?)</tool_end>'
    matches = re.finditer(pattern, planning_results, re.DOTALL)
    for match in matches:
        tool_str = match.group(1)
        try:
            tool = json.loads(tool_str)
            tools.append(tool)
        except json.JSONDecodeError:
            continue
    return tools


def parse_simple_tools(best_result_str):
    tools = []
    tools_content = best_result_str.split('ActionList:')[-1].strip()
    for tool_str in tools_content.split('\n'):
        tool_name = tool_str.split(':')[0] if ':' in tool_str else tool_str.split('：')[0]
        tool_input = tool_str[len(tool_name)+1:].strip()
        tools.append({
            'name': tool_name,
            'input': tool_input
        })
    return tools


if __name__ == "__main__":
    input = {
    "messages": [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "### 身份及任务\n你是一名普通金融分析助手，来自同花顺Hithink团队。你可以根据用户问题及用户图片分析需要哪些金融数据和信息。现在有一些可以使用的工具，通过它们可以获取金融数据及信息。我会为你提供用户问题Question，用户图片和参考信息Ovservation。请你基于现有信息，简单分析需要使用哪些工具补充获取哪些信息。\n\n### 输出格式\n当你认为需要获取信息时，回答格式如下：\nThought: 你对问题的思考和分析，基于现有的背景信息和参考信息，分析回答用户问题还需要获取哪些方面的数据和信息。你可以尽可能多的获取各方面的数据和信息。\nActionList: 你需要执行的动作列表，每一个动作由工具名称和工具输入组成。动作列表有多行，每一行的表示为：工具名称: 工具输入。\n\n当你认为规划完成时，回答格式如下：\nThought: 信息完整，我知道如何回答了。\n<FINISHED>\n\n### 可以使用的工具：\nFinQuery: 金融查询工具，使用这个工具来获取标的相关的金融数据，比如宏观数据、财务数据、行情数据、交易数据、个人账户数据、自选股等，涉及股票、美股、港股、基金、指数、宏观、可转债、期货，它的输入包括具体金融指标或带时间的指标，也可以输入多个指标用于筛选。如果输入指标过多，则需要适当拆分。例子: \"FinQuery: 苹果公司近5天股价以及涨跌幅\"\nSearch: 搜索工具，使用这个工具来搜索相关信息，类似一个搜索引擎，它的输入是自然语言短语或者关键词，用来搜索非结构性数据，关键词最好不要超过5个。例子: \"Search: 苹果公司近期新闻\"\nTickerChart: A股取图工具，当你需要K线图、分时图、技术指标图等信息来辅助你分析问题时，使用该工具获取图片。需要输入这些字段：\"startDate\", \"codeName\", \"chartType\", \"indicator\", \"endDate\"。\"startDate\": \"Start date in the format YYYY-MM-DD\", \"endDate\": \"End date in the format YYYY-MM-DD\", \"codeName\": \"Stock code or ticker symbol\", \"chartType\": \"Type of chart to retrieve, maximum 1. Enumerate value: Intraday, Daily Candlestick, Weekly Candlestick, Monthly Candlestick\", \"indicator\": \"List of indicators to display on the chart, maximum 5. Enumerate value: MA, EMA, BIAS, VR, BRAR, WR, SMA, CCI, MTM, BBI, DMI, EMV, VOL, CR, SAR, PSY, AO, DMA, ROC, TRIX, PVT, RSI, OBV, VWAP, BOLL, MACD, KDJ\"。例子: \"TickerChart: {\"codeName\": \"300033\", \"chartType\": \"Daily Candlestick\", \"startDate\": \"2024-01-08\", \"endDate\": \"2025-05-08\", \"indicator\": [\"MA\", \"MACD\"]}\"\nChartTwinFinder: 相似股票查找工具，通过该工具可以快速检索到与图中走势相似的标的，并返回相似度以及相似时间区间。如果用户询问走势相似的标的，且图中包含一段K线走势图，可以使用该工具。需要输入这些字段: \"query\", \"url\"。 \"query\": \"相似股票查找工具的文本输入，固定为：分析与下图形态走势相近的股票\", \"url\": \"图片的URL地址\"。例子: \"ChartTwinFinder: {\"query\": \"分析与下图形态走势相近的股票\", \"url\": \"http://oss.myhexin.com.cn/iwc-web-userinfo-storage-server.model-image-q-a/bcf0d0684dc6432793e8de8436140b6d.png\"}\"\nVisitWeb: 网页解析工具，这个工具用于实时抓取与解析网页内容的工具，其主要功能是通过输入一个网页的URL，从该网页中提取结构化的信息。输入必须为url，且每次只有一个url。例子: \"VisitWeb: https://paas.myhexin.com/hithinkflow/dataview/list?tenantId=14\"\n\n### 内容要求\n你务必遵守以下原则：\n1. 你仅需要做简单的表层分析即可，不需要进行深度分析。\n2. 在Thought中不要提及你使用的工具，而是说你的目的。也不要提及你遵循的规则，你应当尽量表现的像一个人类。\n3. Thought内容不要过长，简单几句话概括即可。\n4. 若你多次利用工具后仍查询不到结果，为了防止无意义的查询，你可以选择终止计划，进入回答阶段。\n5. 在ActionList中，工具输入应当包含具体的对象，不可以出现代词或引用。\n6. 尽量在一次回答时，把需要的所有工具调用都给出，但是不要使用太多的工具调用，最高不超过5个。\n7. 当规划了2-3次时候，就可以停止，不需要过于深入。\n8. 用中文回答问题。\n<time>\n2025-05-05 17:50:14\n</time>\n<question>\n选出这种走势k线的个股\n</question>\n用户上传的图片："
                },
                {
                    "type": "text",
                    "text": "<img_url>http://oss.myhexin.com/iwc-web-userinfo-storage-server.model-image-q-a/0715896a6ab44d72a6143c053466a136.jpg</img_url>"
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "D:\\data\\code\\业务\\simulation\\synthesis\\data\\2506\\28\\images\\aBdtkcMV.jpg"
                    }
                }
            ]
        }
    ],
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "Thought: 用户想寻找与图片中K线走势相似的个股。我需要使用相似股票查找工具来实现这个目标。\nActionList:\nChartTwinFinder: {\"query\": \"分析与下图形态走势相近的股票\", \"url\": \"http://oss.myhexin.com/iwc-web-userinfo-storage-server.model-image-q-a/0715896a6ab44d72a6143c053466a136.jpg\"}"
                    }
                ]
            },
        },
        {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "Thought: 用户想寻找与图片中K线走势相似的个股。我需要使用相似股票查找工具来实现这个目标。\nActionList:\nChartTwinFinder: {\"query\": \"分析与下图形态走势相近的股票\", \"url\": \"http://oss.myhexin.com/iwc-web-userinfo-storage-server.model-image-q-a/0715896a6ab44d72a6143c053466a136.jpg\"}"
                    }
                ]
            },
        },
        {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "Thought: 信息完整，我知道如何回答了。\n<FINISHED>"
                    }
                ]
            },
        }
    ],
    "query": "选出这种走势k线的个股",
    "type": "用户取消"
    }
    client = GEMINIClient()
    score = assign_plan_score(input, client)
    print(score)
    