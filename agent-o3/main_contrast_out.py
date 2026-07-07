import json
import os
import re
import atexit
import shlex
import signal
import subprocess
import sys
import time
import socket
import urllib.error
import urllib.request
from out_tools import get_tools_results, uploaded_image_message
from api import GEMINIClient, GPT4OClient
from prompt import DEEP_PLAN_PROMPT, SIMPLE_PLAN_PROMPT, SIMPLE_SUMMARY_PROMPT, DEEP_SUMMARY_PROMPT
import argparse
from local_api import LocalModelClient
from wencai_api import WencaiClient
from utils import assign_plan_score, assign_summary_score, get_query, get_summary_query, parse_simple_tools, parse_deep_tools, check_simple_planning_result, check_deep_planning_result, process_tools_results


def build_vllm_command(args):
    command = [
        sys.executable,
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        args.model,
        "--host",
        args.vllm_host,
        "--port",
        str(args.vllm_port),
        "--trust-remote-code",
    ]
    if args.served_model_name:
        command.extend(["--served-model-name", args.served_model_name])
    if args.vllm_tensor_parallel_size:
        command.extend(["--tensor-parallel-size", str(args.vllm_tensor_parallel_size)])
    if args.vllm_pipeline_parallel_size:
        command.extend(["--pipeline-parallel-size", str(args.vllm_pipeline_parallel_size)])
    if args.vllm_data_parallel_size:
        command.extend(["--data-parallel-size", str(args.vllm_data_parallel_size)])
    if args.vllm_enable_expert_parallel:
        command.append("--enable-expert-parallel")
    if args.vllm_all2all_backend:
        command.extend(["--all2all-backend", args.vllm_all2all_backend])
    if args.vllm_expert_placement_strategy:
        command.extend(["--expert-placement-strategy", args.vllm_expert_placement_strategy])
    if args.vllm_enable_eplb:
        command.append("--enable-eplb")
    if args.vllm_gpu_memory_utilization is not None:
        command.extend(["--gpu-memory-utilization", str(args.vllm_gpu_memory_utilization)])
    if args.vllm_cpu_offload_gb is not None:
        command.extend(["--cpu-offload-gb", str(args.vllm_cpu_offload_gb)])
    if args.vllm_dtype:
        command.extend(["--dtype", args.vllm_dtype])
    if args.vllm_max_model_len:
        command.extend(["--max-model-len", str(args.vllm_max_model_len)])
    if args.vllm_extra_args:
        command.extend(shlex.split(args.vllm_extra_args))
    return command


def is_vllm_server_ready(api_base):
    try:
        with urllib.request.urlopen(f"{api_base.rstrip('/')}/models", timeout=5) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, socket.timeout):
        return False


def stop_vllm_server(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        process.terminate()
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except Exception:
            process.kill()


def start_vllm_server(args, api_base):
    if not args.start_vllm_server:
        return None
    if is_vllm_server_ready(api_base):
        print(f"vLLM server is already available at {api_base}", flush=True)
        return None

    log_dir = os.path.dirname(args.vllm_log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    command = build_vllm_command(args)
    print(f"Starting vLLM server: {' '.join(shlex.quote(part) for part in command)}", flush=True)
    print(f"vLLM log file: {args.vllm_log_file}", flush=True)
    log_file = open(args.vllm_log_file, "a", encoding="utf-8")
    process = subprocess.Popen(
        command,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    process._codex_log_file = log_file

    def cleanup():
        stop_vllm_server(process)
        log_file.close()

    atexit.register(cleanup)
    deadline = time.time() + args.vllm_start_timeout
    while time.time() < deadline:
        if process.poll() is not None:
            log_file.flush()
            raise RuntimeError(
                f"vLLM server exited early with code {process.returncode}. "
                f"Check log: {args.vllm_log_file}"
            )
        if is_vllm_server_ready(api_base):
            print(f"vLLM server is ready at {api_base}", flush=True)
            return process
        time.sleep(5)

    log_file.flush()
    stop_vllm_server(process)
    raise TimeoutError(
        f"Timed out waiting for vLLM server at {api_base}. "
        f"Check log: {args.vllm_log_file}"
    )


def simple_messages_update(messages, tools_result, idx, simple_information):
    updated_text_tools_results = []
    updated_image_tools_results = []
    i = 1
    if len(tools_result) == 0: # 工具调用失败的情况
        messages.append({'role': 'user', 'content':[{'type': 'text', 'text': 'Observation: 工具调用失败\n'}]})
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
    
    messages.append({"role": "user", "content": updated_list})
    simple_information.extend(updated_list_info)
    idx += (i-1)
    return messages, idx, simple_information


def deep_messages_update(messages, tools_result, idx):
    updated_text_tools_results = []
    updated_image_tools_results = []
    i = 1
    if len(tools_result) == 0: # 工具调用失败的情况
        messages.append({'role': 'user', 'content':[{'type': 'text', 'text': '<information>工具调用失败</information>'}]})
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
    
    messages.append({"role": "user", "content": updated_list})
    idx += (i-1)
    return messages, idx


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
    messages.append({"role": "assistant", "content": best_result['message']['content']})
    return messages, best_result


def process_simple_record(record, simple_plan_prompt, simple_summary_prompt, fout_simple_plan, fout_simple_summary, online_client, local_client, wencai_client, input_images_dir, output_images_dir):
    stage = 'simple'
    question = record['query'] 
    type = record['type']
    time = record['time']
    image = record['image']
    image_url = record.get('image_url', None)
    query = get_query(question, time)
    if not os.path.exists(image):
        image_path = os.path.join(input_images_dir, image)
    img_message = uploaded_image_message(image_path, image_url)
    messages= [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": simple_plan_prompt + query
                    }]
                    }
            ]
    messages[0]['content'].extend(img_message[0])
    local_planning_results = local_client.image2text({'messages': messages}) # 初步规划
    if local_planning_results == 'error: ' or local_planning_results is None or 'Traceback' in local_planning_results:
        print("Error: local planning_results are None", flush=True)
        return
    wencai_planning_results = wencai_client.image2text({'messages': messages}) # 初步规划
    if not wencai_planning_results:
        print("Error: wencai planning_results are None", flush=True)
        return
    
    planning_results = [local_planning_results, wencai_planning_results]
    messages, best_result = score_results(messages, planning_results, question, type, stage, online_client, fout_simple_plan)
    turns = 1 # 规划轮次
    idx = 0 # 参考内容编号
    simple_information = []
    best_result_str = best_result['message']['content'][0]['text']
    full_tools_results = []
    while check_simple_planning_result(best_result_str) and turns <=10: # 继续规划
        tools = parse_simple_tools(best_result_str)
        tools_results = get_tools_results(tools, output_images_dir)
        full_tools_results.extend(tools_results)
        messages, idx, simple_information = simple_messages_update(messages, tools_results, idx, simple_information)
        local_planning_results = local_client.image2text({'messages': messages}) 
        if local_planning_results == 'error: ' or local_planning_results is None or 'Traceback' in local_planning_results:
            print("Error: planning_results are None", flush=True)
            return
        wencai_planning_results = wencai_client.image2text({'messages': messages}) 
        if not wencai_planning_results:
            print("Error: planning_results are None", flush=True)
            return
        planning_results = [local_planning_results, wencai_planning_results]
        messages, best_result = score_results(messages, planning_results, question, type, stage, online_client, fout_simple_plan)
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

    local_summary_results = local_client.image2text({'messages': summary_messages}) # 初步规划
    if local_summary_results == 'error: ' or local_summary_results is None or 'Traceback' in local_summary_results:
        print("Error: summary_results are None", flush=True)
        return
    wencai_summary_results = wencai_client.image2text({'messages': summary_messages}) # 初步规划
    if not wencai_summary_results:
        print("Error: summary_results are None", flush=True)
        return
    summary_results = [local_summary_results, wencai_summary_results]
    summary_messages, best_result = score_results(summary_messages, summary_results, question, type, stage, online_client, fout_simple_summary)
    return simple_information, idx, full_tools_results


def process_deep_record(record, deep_plan_prompt, deep_summary_prompt, simple_information, fout_deep_plan, fout_deep_summary, online_client, local_client, wencai_client, input_images_dir, output_image_dir, idx, full_tools_results):
    stage = 'deep'
    question = record['query']
    type = record['type']
    time = record['time']
    image = record['image']
    image_url = record.get('image_url', None)
    query = get_query(question, time)
    if not os.path.exists(image):
        image_path = os.path.join(input_images_dir, image)
    img_message = uploaded_image_message(image_path, image_url)
    messages= [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": deep_plan_prompt + query
                    }]
                    }
            ]
    messages[0]['content'].extend(img_message[0])
    messages[0]['content'].extend(simple_information)
    local_planning_results = local_client.image2text({'messages': messages}) # 初步规划
    if local_planning_results == 'error: ' or local_planning_results is None or 'Traceback' in local_planning_results:
        print("Error: local planning_results are None", flush=True)
        return
    wencai_planning_results = wencai_client.image2text({'messages': messages}) # 初步规划
    if not wencai_planning_results:
        print("Error: wencai planning_results are None", flush=True)
        return
    
    planning_results = [local_planning_results, wencai_planning_results]
    messages, best_result = score_results(messages, planning_results, question, type, stage, online_client, fout_deep_plan)
    turns = 1 # 规划轮次
    best_result_str = best_result['message']['content'][0]['text']
    while check_deep_planning_result(best_result_str) and turns <=10: # 继续规划
        tools = parse_deep_tools(best_result_str)
        tools_results = get_tools_results(tools, output_image_dir)
        full_tools_results.extend(tools_results)
        messages, idx = deep_messages_update(messages, tools_results, idx)
        local_planning_results = local_client.image2text({'messages': messages}) 
        if local_planning_results == 'error: ' or local_planning_results is None or 'Traceback' in local_planning_results:
            print("Error: planning_results are None", flush=True)
            return
        wencai_planning_results = wencai_client.image2text({'messages': messages}) 
        if not wencai_planning_results:
            print("Error: planning_results are None", flush=True)
            return
        planning_results = [local_planning_results, wencai_planning_results]
        messages, best_result = score_results(messages, planning_results, question, type, stage, online_client, fout_deep_plan)
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
                        "text": deep_summary_prompt + f'### 背景及参考消息\n<背景>\n\n<参考>\n'
                    }]
                    }
            ]
    information_summary = process_tools_results(full_tools_results, best_result_str) # 对工具调用结果进行编号并组装，暂时定图片放最后？
    summary_messages[0]['content'].extend(information_summary)
    summary_query = get_summary_query(question, time)
    summary_messages[0]['content'].append({'type': 'text', 'text': summary_query})
    summary_messages[0]['content'].extend(img_message[0])
    local_summary_results = local_client.image2text({'messages': summary_messages}) # 初步规划
    if local_summary_results == 'error: ' or local_summary_results is None or 'Traceback' in local_summary_results:
        print("Error: summary_results are None", flush=True)
        return
    wencai_summary_results = wencai_client.image2text({'messages': summary_messages}) # 初步规划
    if not wencai_summary_results:
        print("Error: summary_results are None", flush=True)
        return
    summary_results = [local_summary_results, wencai_summary_results]
    summary_messages, best_result = score_results(summary_messages, summary_results, question, type, stage, online_client, fout_deep_summary)

    return 


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Process simulation data.')
    ROOT_PATH = './data/30'
    model_name = 'dpo_260224'
    os.makedirs(f"{ROOT_PATH}/data/{model_name}", exist_ok=True)
    parser.add_argument('--input', type=str, 
                        default=f"{ROOT_PATH}/data/eval.jsonl",
                        help='Input JSONL file path')
    parser.add_argument('--simple_plan_output', type=str, 
                        default=f"{ROOT_PATH}/data/{model_name}/simple_plan_output.jsonl",
                        help='Output JSONL file path')
    parser.add_argument('--simple_summary_output', type=str, 
                        default=f"{ROOT_PATH}/data/{model_name}/simple_summary_output.jsonl",
                        help='Output JSONL file path')
    parser.add_argument('--deep_plan_output', type=str, 
                        default=f"{ROOT_PATH}/data/{model_name}/deep_plan_output.jsonl",
                        help='Output JSONL file path')
    parser.add_argument('--deep_summary_output', type=str, 
                        default=f"{ROOT_PATH}/data/{model_name}/deep_summary_output.jsonl",
                        help='Output JSONL file path')
    parser.add_argument('--input_images_dir', type=str,
                        default=f"{ROOT_PATH}/images",
                        help='Input images directory path')
    parser.add_argument('--output_images_dir', type=str,
                        default=f"{ROOT_PATH}/data/{model_name}/images",
                        help='Output images directory path')
    parser.add_argument('--log_file', type=str,
                    default=f"{ROOT_PATH}/data/{model_name}/log.txt",
                    help='Log file')
    parser.add_argument('--model', type=str,
                # default="/mnt/thscc/workspace/dlc/952/48312/checkpoint/hf",
                default="/mnt/thscc/workspace/dlc/952/59651/checkpoint/hf",
                help='Local model')
    parser.add_argument('--start_vllm_server', dest='start_vllm_server', action='store_true',
                default=True,
                help='Start a local vLLM OpenAI-compatible server before processing')
    parser.add_argument('--no_start_vllm_server', dest='start_vllm_server', action='store_false',
                help='Do not start vLLM; connect to an already running server')
    parser.add_argument('--vllm_host', type=str, default='0.0.0.0',
                help='Host used by the vLLM server')
    parser.add_argument('--vllm_port', type=int, default=8100,
                help='Port used by the vLLM server')
    parser.add_argument('--local_api_base', type=str, default=None,
                help='OpenAI-compatible local API base URL. Defaults to http://127.0.0.1:<vllm_port>/v1')
    parser.add_argument('--served_model_name', type=str, default=None,
                help='Model name exposed by vLLM and used by LocalModelClient')
    parser.add_argument('--vllm_tensor_parallel_size', type=int, default=None,
                help='vLLM tensor parallel size')
    parser.add_argument('--vllm_pipeline_parallel_size', type=int, default=None,
                help='vLLM pipeline parallel size')
    parser.add_argument('--vllm_data_parallel_size', type=int, default=None,
                help='vLLM data parallel size')
    parser.add_argument('--vllm_enable_expert_parallel', action='store_true',
                help='Enable vLLM expert parallel for MoE models')
    parser.add_argument('--vllm_all2all_backend', type=str, default=None,
                help='vLLM all2all backend for expert parallel, e.g. naive, allgather_reducescatter')
    parser.add_argument('--vllm_expert_placement_strategy', type=str, default=None,
                choices=['linear', 'round_robin'],
                help='vLLM expert placement strategy')
    parser.add_argument('--vllm_enable_eplb', action='store_true',
                help='Enable vLLM expert parallel load balancing')
    parser.add_argument('--vllm_gpu_memory_utilization', type=float, default=None,
                help='vLLM GPU memory utilization')
    parser.add_argument('--vllm_cpu_offload_gb', type=float, default=None,
                help='vLLM CPU offload GB per GPU')
    parser.add_argument('--vllm_dtype', type=str, default=None,
                help='vLLM dtype, e.g. auto, float16, bfloat16')
    parser.add_argument('--vllm_max_model_len', type=int, default=None,
                help='vLLM max model length')
    parser.add_argument('--vllm_extra_args', type=str, default='',
                help='Extra arguments appended to the vLLM server command')
    parser.add_argument('--vllm_start_timeout', type=int, default=1200,
                help='Seconds to wait for vLLM server startup')
    parser.add_argument('--vllm_log_file', type=str,
                default=f"{ROOT_PATH}/data/{model_name}/vllm_server.log",
                help='vLLM server log file')
    args = parser.parse_args()

    os.makedirs(fr"{args.output_images_dir}", exist_ok=True)
    local_api_base = args.local_api_base or f"http://127.0.0.1:{args.vllm_port}/v1"
    if args.start_vllm_server and not args.served_model_name:
        args.served_model_name = "local-model"
    _vllm_process = start_vllm_server(args, local_api_base)
    simple_plan_prompt = SIMPLE_PLAN_PROMPT
    deep_plan_prompt = DEEP_PLAN_PROMPT
    simple_summary_prompt = SIMPLE_SUMMARY_PROMPT
    deep_summary_prompt = DEEP_SUMMARY_PROMPT
    # online_client = GEMINIClient()
    online_client = GPT4OClient()
    local_client = LocalModelClient(
        model=args.served_model_name or args.model,
        api_base=local_api_base,
        max_tokens=8192,
    )
    # wencai_client = WencaiClient("aime-multimodal-ucwlai")
    wencai_client = WencaiClient("aime-multimodal-ucwlai")

    with open(args.input, "r", encoding="utf-8") as fin, \
        open(args.simple_plan_output, "a", encoding="utf-8") as fout_simple_plan, \
        open(args.simple_summary_output, "a", encoding="utf-8") as fout_simple_summary, \
        open(args.deep_plan_output, "a", encoding="utf-8") as fout_deep_plan, \
        open(args.deep_summary_output, "a", encoding="utf-8") as fout_deep_summary, \
        open(args.log_file, "a", encoding="utf-8") as f_log:
        idx = 0
        for line in fin:
            if idx > -1:
                line = line.strip()
                try:
                    record = json.loads(line)
                    simple_information, info_id, full_tools_results = process_simple_record(record, simple_plan_prompt, simple_summary_prompt, fout_simple_plan, fout_simple_summary, online_client, local_client, wencai_client, args.input_images_dir, args.output_images_dir)
                    process_deep_record(record, deep_plan_prompt, deep_summary_prompt, simple_information, fout_deep_plan, fout_deep_summary, online_client, local_client, wencai_client, args.input_images_dir, args.output_images_dir, info_id, full_tools_results)
                    print(f"Successfully processed line: {idx}", flush=True)
                    f_log.write(f"Successfully processed line: {idx} \n")
                except Exception as e:
                    print(f"Error processing line: {idx}---{e}", flush=True)
                    f_log.write(f"Error processing line: {idx}-------------------------{e}\n")
                f_log.flush()
            idx += 1
    print(f"成功处理！", flush=True)
