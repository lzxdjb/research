import os
import sys
import io
import re
import json
from tools import get_tools_results, image_message
from api import GEMINIClient, GPT4OClient
from prompt import LONG_PLAN_PROMPT, LONG_SUMMARY_PROMPT
import argparse
from local_api import LocalModelClient
from utils import assign_plan_score, assign_summary_score, get_long_query, get_long_summary_query, get_summary_document, parse_deep_tools, \
    check_deep_planning_result, process_tools_results, print_current_time
from typing import Optional
import shutil

# DEBUG_MODE=True
DEBUG_MODE=False


def simple_messages_update(messages, tools_result, idx, simple_information):
    updated_text_tools_results = []
    updated_image_tools_results = []
    i = 1
    if len(tools_result) == 0: # 工具调用失败的情况
        messages[0]['content'].append({'type': 'text', 'text': 'Observation: 工具调用失败\n'})
        simple_information.append({'type': 'text', 'text': f'<information>工具调用失败</information>'})
        return messages, idx, simple_information
    else:    
        for tool_result in tools_result:
            if isinstance(tool_result, str):
                if '站点:\n溯源地址：' in tool_result: # 去掉"内容: "和"站点:\n溯源地址："之间的内容，普通模式不需要全文
                    tool_result_cleaned = re.sub(r'内容: .*?站点:\n溯源地址：', '内容:\n站点:\n溯源地址：', tool_result, flags=re.DOTALL)
                    updated_tool_result = f'编号：{idx+i}\n{tool_result_cleaned}\n'
                else:
                    updated_tool_result = f'编号：{idx+i}\n{tool_result}\n'
                updated_text_tools_results.append(updated_tool_result)
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
    
    # messages[0]['content'].extend(updated_list)
    messages.append({"role": "user", "content": updated_list})  # For multi-turn form
    simple_information.extend(updated_list_info)
    idx += (i-1)
    return messages, idx, simple_information

def deep_messages_update(messages, tools_result, idx):
    updated_text_tools_results = []
    updated_image_tools_results = []
    i = 1
    if len(tools_result) == 0: # 工具调用失败的情况
        messages[0]['content'].append({'type': 'text', 'text': '<information>工具调用失败</information>'})
        return messages, idx
    else:    
        for tool_result in tools_result:
            if isinstance(tool_result, str):
                if '站点:\n溯源地址：' in tool_result: # 去掉"内容: "和"站点:\n溯源地址："之间的内容，普通模式不需要全文
                    tool_result_cleaned = re.sub(r'内容: .*?站点:\n溯源地址：', '内容:\n站点:\n溯源地址：', tool_result, flags=re.DOTALL)
                    updated_tool_result = f'编号：{idx+i}\n{tool_result_cleaned}\n'
                else:
                    updated_tool_result = f'编号：{idx+i}\n{tool_result}\n'
                updated_text_tools_results.append(updated_tool_result)
                i += 1
            else: # 图片是用一个列表包起来
                updated_image_tools_results.append(tool_result)

    updated_list = []
    if len(updated_text_tools_results) > 0:
        info_text = ''
        for text_tools_result in updated_text_tools_results:
            info_text += text_tools_result
        updated_list.append({'type': 'text', 'text': f'<information>{info_text}</information>'})
    if len(updated_image_tools_results) > 0: # 图片内容默认加载文本内容后面
        for image_tools_result in updated_image_tools_results:
            updated_list.extend(image_tools_result)
    
    # messages[0]['content'].extend(updated_list)
    messages.append({"role": "user", "content": updated_list})  # For multi-turn form
    idx += (i-1)
    return messages, idx

def score_results_bypass(messages, results, question, type, stage, online_client, fout):   # [TODO]
    out_content = {
            'messages': messages,
            'choices': [],
            'query': question,
            'type': type
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
    # if stage == 'summary':
    #     scored_out_content = assign_summary_score(out_content, online_client)
    # else:
    #     scored_out_content = assign_plan_score(out_content, online_client)
    
    # best_result = scored_out_content['choices'][0]
    best_result = out_content['choices'][0]
    # fout.write(json.dumps(scored_out_content, ensure_ascii=False) + "\n")
    fout.write(json.dumps(out_content, ensure_ascii=False) + "\n")
    fout.flush()
    # messages[0]['content'].append(best_result['message']['content'][0])
    messages.append({"role": "assistant", "content": best_result['message']['content']})    # For multi-turn form
    return messages, best_result

def score_results(messages, results, question, type, stage, online_client, fout):
    out_content = {
            'messages': messages,
            'choices': [],
            'query': question,
            'type': type
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
    # messages[0]['content'].append(best_result['message']['content'][0])
    messages.append({"role": "assistant", "content": best_result['message']['content']})    # For multi-turn form
    return messages, best_result

def process_simple_record(record, summary_prompt, fout_simple_summary, online_client, local_client):
    if DEBUG_MODE == True:
        selected_client = local_client  # For debug
        # score_results_func = score_results_bypass
        score_results_func = score_results
    else:
        selected_client = online_client # For data synthesis
        score_results_func = score_results

    print_current_time(f'In process_simple_record')
    stage = 'simple'
    question = record['query'] 
    type = record['type'] if hasattr(record, 'type') else None
    time = record['time'] if hasattr(record, 'time') else None
    pdf_text = record['text'] 
    
    idx = 0 # 参考内容编号
    simple_information = []
    full_tools_results = [] # 全部工具调用结果
    ### 生成普通总结
    stage = 'summary'
    summary_messages= [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": summary_prompt + f'### 背景及参考消息\n<背景>\n\n<参考>\n'
                    }]
                    }
            ]
    summary_messages[0]['content'].extend(simple_information)
    summary_document = get_summary_document(pdf_text, time)
    summary_messages[0]['content'].append({'type': 'text', 'text': summary_document})
    summary_query = get_long_summary_query(question, time)
    summary_messages[0]['content'].append({'type': 'text', 'text': summary_query})
    print_current_time(f'Before summary')
    summary_results = selected_client.image2text_N({'messages': summary_messages}, num_samples=5, temperature=0.7)
    print_current_time(f'After summary')
    if summary_results == 'error: ' or summary_results is None or 'Traceback' in summary_results:
        print("Error: summary_results are None")
        return
    summary_messages, best_result = score_results_func(summary_messages, summary_results, question, type, stage, selected_client, fout_simple_summary)
    print_current_time(f'Out process_simple_record')
    return simple_information, idx, full_tools_results

def process_deep_record(record, plan_prompt, summary_prompt, simple_information, fout_deep_plan, fout_deep_summary, online_client, local_client, \
    input_images_dir, output_image_dir, idx, full_tools_results):
    if DEBUG_MODE == True:
        selected_client = local_client  # For debug
        # score_results_func = score_results_bypass
        score_results_func = score_results
    else:
        selected_client = online_client # For data synthesis
        score_results_func = score_results

    stage = 'deep'
    print_current_time(f'In process_deep_record')

    question = record['query'] 
    type = record['type'] if hasattr(record, 'type') else None
    time = record['time'] if hasattr(record, 'time') else None
    query = get_long_query(question, time)
    pdf_text = record['text']
    
    messages= [{
        "role": "user",
        "content": [{
            "type": "text",
            "text": plan_prompt  + '<document>' + pdf_text + '</document>' + query
        }]
    }]       
    
    messages[0]['content'].extend(simple_information)
    print_current_time(f'Before simple plan')
    planning_results = selected_client.image2text_N({'messages': messages}, num_samples=5)
    print_current_time(f'After simple plan')
    
    if planning_results == 'error: ' or planning_results is None or 'Traceback' in planning_results:
        print("Error: planning_results are None")
        return
    print_current_time(f'Before score_results')
    messages, best_result = score_results_func(messages, planning_results, question, type, stage, selected_client, fout_deep_plan)
    print_current_time(f'After score_results')
    turns = 1 # 规划轮次
    best_result_str = best_result['message']['content'][0]['text']
    while check_deep_planning_result(best_result_str) and turns <=10: # 继续规划
        tools = parse_deep_tools(best_result_str)
        tools_results = get_tools_results(tools, output_image_dir)
        full_tools_results.extend(tools_results)
        messages, idx = deep_messages_update(messages, tools_results, idx)
        print_current_time(f'Before simple plan')
        planning_results = selected_client.image2text_N({'messages': messages}, num_samples=5)
        print_current_time(f'After simple plan')
        if planning_results == 'error: ' or planning_results is None or 'Traceback' in planning_results:
            print("Error: planning_results are None")
            return
        print_current_time(f'Before score_results')
        messages, best_result = score_results_func(messages, planning_results, question, type, stage, selected_client, fout_deep_plan)
        print_current_time(f'After score_results')
        best_result_str = best_result['message']['content'][0]['text']
        turns += 1
    
    ### 生成深度总结
    stage = 'summary'
    summary_messages= [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": summary_prompt + f'### 背景及参考消息\n<背景>\n\n<参考>\n'
                    }]
                    }
            ]
    information_summary = process_tools_results(full_tools_results, best_result_str) # 对工具调用结果进行编号并组装，暂时定图片放最后？
    summary_messages[0]['content'].extend(information_summary)
    summary_document = get_summary_document(pdf_text, time)
    summary_messages[0]['content'].append({'type': 'text', 'text': summary_document})
    summary_query = get_long_summary_query(question, time)
    summary_messages[0]['content'].append({'type': 'text', 'text': summary_query})

    print_current_time(f'Before summary')
    summary_results = selected_client.image2text_N({'messages': summary_messages}, num_samples=5, temperature=0.7)
    print_current_time(f'After summary')
    if summary_results == 'error: ' or summary_results is None or 'Traceback' in summary_results:
        print("Error: summary_results are None")
        return
    summary_messages, best_result = score_results_func(summary_messages, summary_results, question, type, stage, selected_client, fout_deep_summary)
    print_current_time(f'Out process_deep_record')
    return 


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Process simulation data.')
    ROOT_PATH = r'D:\data\code\业务\simulation\synthesis\long_context'
    parser.add_argument('--model-name', type=str, 
                        default=None,
                        help='Model identifier')
    parser.add_argument('--model_path', type=str, 
                        default='/mnt/thscc/workspace/dlc/952/34085/checkpoint/checkpoint-1600/',
                        help='Model path')
    args = parser.parse_args()
    # OUTPUT_DIR = fr"{ROOT_PATH}\outputs{'_' + args.model_name if args.model_name else ''}"
    OUTPUT_DIR = fr"{ROOT_PATH}\2508\15\data"

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    parser.add_argument('--input', type=str, 
                        default=fr"{OUTPUT_DIR}\input.jsonl",
                        help='Input JSONL file path')
    parser.add_argument('--simple_plan_output', type=str, 
                        default=fr"{OUTPUT_DIR}\simple_plan_output.jsonl",
                        help='Output JSONL file path')
    parser.add_argument('--simple_summary_output', type=str, 
                        default=fr"{OUTPUT_DIR}\simple_summary_output.jsonl",
                        help='Output JSONL file path')
    parser.add_argument('--deep_plan_output', type=str, 
                        default=fr"{OUTPUT_DIR}\deep_plan_output.jsonl",
                        help='Output JSONL file path')
    parser.add_argument('--deep_summary_output', type=str, 
                        default=fr"{OUTPUT_DIR}\deep_summary_output.jsonl",
                        help='Output JSONL file path')
    parser.add_argument('--log_file', type=str, 
                        default=fr"{OUTPUT_DIR}\log.txt",
                        help='Debug Output JSONL file path')
    parser.add_argument('--input_images_dir', type=str,
                        default=fr"{ROOT_PATH}\datasets\images",
                        help='Input images directory path')
    parser.add_argument('--output_images_dir', type=str,
                        default=fr"{OUTPUT_DIR}\images",
                        help='Output images directory path')
    args = parser.parse_args()

    plan_prompt = LONG_PLAN_PROMPT
    summary_prompt = LONG_SUMMARY_PROMPT
    online_client = GEMINIClient()
    local_client = LocalModelClient(args.model_path)

    with open(args.input, "r", encoding="utf-8") as fin, \
        open(args.simple_plan_output, "a", encoding="utf-8") as fout_simple_plan, \
        open(args.simple_summary_output, "a", encoding="utf-8") as fout_simple_summary, \
        open(args.deep_plan_output, "a", encoding="utf-8") as fout_deep_plan, \
        open(args.deep_summary_output, "a", encoding="utf-8") as fout_deep_summary, \
        open(args.log_file, "a", encoding="utf-8") as fout_log:
        idx = 0
        for line in fin:
            if idx > 0:
                line = line.strip()
                try:
                # if True:
                    record = json.loads(line)
                    simple_information, info_id, full_tools_results = process_simple_record(record, summary_prompt, fout_simple_summary, online_client, local_client)
                    process_deep_record(record, plan_prompt, summary_prompt, simple_information, fout_deep_plan, fout_deep_summary, online_client, local_client, args.input_images_dir, args.output_images_dir, info_id, full_tools_results)
                    print(f"Successfully processed line: {idx}")
                    fout_log.write(f"Successfully processed line: {idx} \n")
                except Exception as e:
                    print(f"Error processing line: {idx} {e}")
                    fout_log.write(f"Error processing line: {idx} {e}\n")
            idx += 1
    print(f"成功处理！")