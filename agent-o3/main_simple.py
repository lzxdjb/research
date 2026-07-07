import json
import os
import re
from tools import get_tools_results, uploaded_image_message
from api import GEMINIClient, LocalModelClient, WencaiClient
from prompt import SIMPLE_PLAN_PROMPT, SIMPLE_SUMMARY_PROMPT
import argparse
from utils import assign_plan_score, assign_summary_score, get_query, get_summary_query, parse_simple_tools, check_simple_planning_result, process_tools_results


def simple_messages_update(messages, tools_result, idx):
    i = 1
    tool_results_list = []
    if len(tools_result) == 0: # 工具调用失败的情况
        messages.append({'role': 'user', 'content':[{'type': 'text', 'text': 'Observation: 工具调用失败\n'}]})
        return messages, idx
    else:
        for tool_result in tools_result:
            if isinstance(tool_result, str):
                if '站点:\n溯源地址：' in tool_result: # 去掉"内容: "和"站点:\n溯源地址："之间的内容，普通模式不需要全文
                    tool_result_cleaned = re.sub(r'内容: .*?站点:\n溯源地址：', '内容:\n站点:\n溯源地址：', tool_result, flags=re.DOTALL)
                    updated_tool_result = f'编号：{idx+i}\n{tool_result_cleaned}\n'
                else:
                    updated_tool_result = f'编号：{idx+i}\n{tool_result}\n'
                tool_results_list.append(updated_tool_result)
                i += 1
            else: # 图片是用一个列表包起来
                updated_image_tools_results.append(tool_result)
    updated_list = []
    updated_list_info = []
    if len(updated_text_tools_results) > 0:
        info_text = ''
        for text_tools_result in updated_text_tools_results:
            info_text += text_tools_result
        updated_list_info.append({'type': 'text', 'text': f'<information>{info_text}</information>'})
        updated_list.append({'type': 'text', 'text': f'Observation: {info_text}'})
    if len(updated_image_tools_results) > 0: # 图片内容默认加载文本内容后面
        for image_tools_result in updated_image_tools_results:
            updated_list_info.extend(image_tools_result)
            updated_list.extend(image_tools_result)
    
    messages.append({"role": "user", "content": updated_list})
    simple_information.extend(updated_list_info)
    idx += (i-1)
    return messages, idx, simple_information


def score_results(messages, results, question, stage, online_client, fout):
    out_content = {
            'messages': messages,
            'choices': [],
            'query': question,
                    }
    for result in results:
        assistant_mesg = {
            'message':{
            "role": "assistant",
            "content":
                [
                {
                    "type": "text",
                    "text": result
                }
                ]
        }}
        out_content['choices'].append(assistant_mesg)
    if stage == 'summary':
        scored_out_content = assign_summary_score(out_content, online_client)
    else:
        scored_out_content = assign_plan_score(out_content, online_client)
    
    best_result = scored_out_content['choices'][0]
    fout.write(json.dumps(scored_out_content, ensure_ascii=False) + "\n")
    fout.flush()
    messages.append({"role": "assistant", "content": best_result['message']['content']})
    return messages, best_result


def prepare_messages(record, input_images_dir):
    question = record['query'] 
    type = record['type']
    time = record['time']
    idx = 0
    if type == 'pdf':
        file_name = record['file_name']
        context = record['context']
        messages= [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": simple_plan_prompt
                        },
                        {
                            "type": "text",
                            "text": "<document>"
                        },
                        {
                            "type": "text",
                            "text": f'文档名: {file_name}\n文档内容:'
                        }
                        ]
                        }
                ]
        messages[0]['content'].extend(context)
        messages[0]['content'].append(
            {
                "type": "text",
                "text": f'</document>'
            })
        messages[0]['content'].append(
            {
                "type": "text",
                "text": f'当前时间: {time}\n<question>\n{question}\n</question>'
            }
        )
    else: # 单/多图输入
        images = record['images']
        image_urls = record['image_url']
        messages= [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": simple_plan_prompt
                        }
                    ]
                }
            ]
        for image in images:
            image_path = os.path.join(input_images_dir, image)
            messages[0]['content'].extend([
                {
                    "type": "text",
                    "text": f'类型: 用户上传图片\n 用户上传图片编号: {idx+1}\n 用户上传图片url: {image_urls[idx]}'
                },
                {
                    "type": "image_url",
                    "image_url": {"url": image_path}
                }
            ])
            idx += 1
        messages[0]['content'].append(
            {
                "type": "text",
                "text": f'当前时间: {time}\n<question>\n{question}\n</question>'
            }
        )

    return messages, idx


def process_simple_record(record, simple_plan_prompt, simple_summary_prompt, fout_simple_plan, fout_simple_summary, online_client, local_client, input_images_dir, output_images_dir):
    stage = 'planning'
    question = record['query']
    messages, idx = prepare_messages(record, input_images_dir)
    
    planning_results = online_client.image2text_N({'messages': messages}, num_samples=5) # 初步规划
    if planning_results == 'error: ' or planning_results is None or 'Traceback' in planning_results:
        print("Error: planning_results are None")
        return
    messages, best_result = score_results(messages, planning_results, question, stage, online_client, fout_simple_plan)
    turns = 1 # 规划轮次
    simple_information = []
    best_result_str = best_result['message']['content'][0]['text']
    while check_simple_planning_result(best_result_str) and turns <=10: # 继续规划
        tools = parse_simple_tools(best_result_str)
        tools_results = get_tools_results(tools, output_images_dir)
        messages, idx, simple_information = simple_messages_update(messages, tools_results, idx, simple_information)
        # planning_results = local_client.image2text_N({'messages': messages}, num_samples=5)
        planning_results = online_client.image2text_N({'messages': messages}, num_samples=5)
        if planning_results == 'error: ' or planning_results is None or 'Traceback' in planning_results:
            print("Error: planning_results are None")
            return
        messages, best_result = score_results(messages, planning_results, question, stage, online_client, fout_simple_plan)
        best_result_str = best_result['message']['content'][0]['text']
        turns += 1
    
    ### 生成普通总结
    stage = 'summary'
    summary_messages= [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": simple_summary_prompt + f'### 背景及参考消息\n<背景>\n\n<参考>\n'
                    }]
                    }
            ]
    summary_messages[0]['content'].extend(simple_information)
    summary_query = get_summary_query(question, time)
    summary_messages[0]['content'].append({'type': 'text', 'text': summary_query})
    summary_messages[0]['content'].extend(img_message[0])
    summary_results = online_client.image2text_N({'messages': summary_messages}, num_samples=5, temperature=0.7) # 普通总结
    # summary_results = local_client.image2text_N({'messages': summary_messages}, num_samples=5) # 普通总结
    if summary_results == 'error: ' or summary_results is None or 'Traceback' in summary_results:
        print("Error: summary_results are None")
        return
    summary_messages, best_result = score_results(summary_messages, summary_results, question, type, stage, online_client, fout_simple_summary)
    return simple_information, idx, full_tools_results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Process simulation data.')
    ROOT_PATH = r'/mnt/HithinkOmniSSD/user_workspace/ganziliang/code/agent/synthesis/2509/02'
    model_name = 'gemini'
    os.makedirs(fr"{ROOT_PATH}\data\{model_name}", exist_ok=True)
    parser.add_argument('--input', type=str, 
                        default=fr"{ROOT_PATH}\data\{model_name}\input.jsonl",
                        help='Input JSONL file path')
    parser.add_argument('--simple_plan_output', type=str, 
                        default=fr"{ROOT_PATH}\data\{model_name}\simple_plan_output.jsonl",
                        help='Output JSONL file path')
    parser.add_argument('--simple_summary_output', type=str, 
                        default=fr"{ROOT_PATH}\data\{model_name}\simple_summary_output.jsonl",
                        help='Output JSONL file path')
    parser.add_argument('--input_images_dir', type=str,
                        default=fr"{ROOT_PATH}\data\{model_name}\images",
                        help='Input images directory path')
    parser.add_argument('--output_images_dir', type=str,
                        default=fr"{ROOT_PATH}\data\{model_name}\images",
                        help='Output images directory path')
    parser.add_argument('--log_file', type=str,
                    default=fr"{ROOT_PATH}\data\{model_name}\log.txt",
                    help='Log file')
    parser.add_argument('--model', type=str,
                default="/mnt/thscc/workspace/dlc/952/34085/checkpoint/checkpoint-1600/",
                help='Local model')
    args = parser.parse_args()

    simple_plan_prompt = SIMPLE_PLAN_PROMPT
    simple_summary_prompt = SIMPLE_SUMMARY_PROMPT
    online_client = GEMINIClient()
    local_client = LocalModelClient(model=args.model)
    wencai_client = WencaiClient()

    with open(args.input, "r", encoding="utf-8") as fin, \
        open(args.simple_plan_output, "a", encoding="utf-8") as fout_simple_plan, \
        open(args.simple_summary_output, "a", encoding="utf-8") as fout_simple_summary, \
        open(args.log_file, "a", encoding="utf-8") as f_log:
        idx = 0
        for line in fin:
            if idx > -1:
                line = line.strip()
                try:
                    record = json.loads(line)
                    simple_information, info_id, full_tools_results = process_simple_record(record, simple_plan_prompt, simple_summary_prompt, fout_simple_plan, fout_simple_summary, online_client, local_client, args.input_images_dir, args.output_images_dir)
                    print(f"Successfully processed line: {idx}")
                    f_log.write(f"Successfully processed line: {idx} \n")
                except Exception as e:
                    print(f"Error processing line: {idx}---{e}")
                    f_log.write(f"Error processing line: {idx}-------------------------{e}\n")
                f_log.flush()
            idx += 1
    print(f"成功处理！")