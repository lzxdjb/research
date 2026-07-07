python dataset_make.py \
  --local_dataset_path ./dataset1/paas_1036.jsonl \
  --images_dir ./dataset1/Images/ \
  --system_prompt_path ./system_prompt.md \
  --local_save_dir ./data/stock_candlestick



python dataset_make_2.py \
  --local_dataset_path ./data/dataset/processed_merge_30-45.jsonl \
  --images_dir ./data/dataset/ \
  --system_prompt_path ./system_prompt.md \
  --local_save_dir ./data/stock_candlestick_small


python dataset_make_2_parallel.py \
  --local_dataset_path ./data/dataset/processed_merge_30-45.jsonl \
  --images_dir ./data/dataset/ \
  --system_prompt_path ./system_prompt.md \
  --local_save_dir ./data/stock_candlestick_2_parallel

############

python dataset_make_2.py \
  --local_dataset_path ./data/new_train/search_stock_by_kline.jsonl \
  --images_dir ./data/new_train/ \
  --system_prompt_path ./system_prompt.md \
  --local_save_dir ./data/new_train_small


python dataset_make_2_parallel.py \
  --local_dataset_path ./data/new_train/search_stock_by_kline.jsonl \
  --images_dir ./data/new_train/ \
  --system_prompt_path ./system_prompt.md \
  --local_save_dir ./data/new_train




##########

python dataset_make_2_parallel.py \
  --local_dataset_path /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/data/k/0_RL.jsonl \
  --images_dir ./data/k/ \
  --system_prompt_path ./system_prompt.md \
  --local_save_dir ./data/k_testdata


python dataset_make_2_parallel_reflect.py \
  --local_dataset_path ./data/dataset/processed_merge_30-45.jsonl \
  --images_dir ./data/dataset/ \
  --system_prompt_path ./system_prompt.md \
  --local_save_dir ./data/dataset_make_2_parallel_reflect


python dataset_make_2_parallel_reflect_small.py \
  --local_dataset_path ./data/dataset/processed_merge_30-45.jsonl \
  --images_dir ./data/dataset/ \
  --system_prompt_path ./system_prompt.md \
  --local_save_dir ./data/dataset_make_2_parallel_reflect_small


#########

python dataset_make_2_parallel_reflect.py \
  --local_dataset_path data/new_train/search_stock_by_kline.jsonl \
  --images_dir ./data/new_train/ \
  --system_prompt_path ./system_prompt.md \
  --local_save_dir ./data/new_train_reflect


python dataset_make_2_parallel_reflect_small.py \
  --local_dataset_path data/new_train/search_stock_by_kline.jsonl \
  --images_dir ./data/new_train/ \
  --system_prompt_path ./system_prompt.md \
  --local_save_dir ./data/new_train_reflect


########



python vllm_quick_test.py \
    --parquet_path data/stock_candlestick/train.parquet \
    --system_prompt_path ./system_prompt.md \
    --model data/Qwen3-VL-8B-Instruct \
    --num_samples 20 \
    --temperature 0.0

git config --global user.email "zhengxinglei539@gmail.com"
git config --global user.name "lzxdjb"
  


export http_proxy="http://hexin:hx300033@10.244.57.246:30100"
export https_proxy="http://hexin:hx300033@10.244.57.246:30100"




git remote add gitlab https://git-cc.myhexin.com:6443/leizhengxing/stock-agent-rl.git
git push -u gitlab main


git add .
git commit -m "dsw-1234 add some speed up"
git push -u origin main


leizhengxing@myhexin.com
mSuGAFTucxAJgMz38WnT



huggingface-cli download Qwen/Qwen3-VL-30B-A3B-Instruct  --local-dir  /mnt/thscc


Qwen/Qwen3-Omni-30B-A3B-Instruct



export https_proxy="http://hexin:hx300033@10.217.180.65:30100"
export http_proxy="http://hexin:hx300033@10.217.180.65:30100"  
hf download Qwen/Qwen3.5-0.8B  --local-dir  ./data/Qwen3.5-0.8B


export https_proxy="http://hexin:hx300033@10.217.180.65:30100"
export http_proxy="http://hexin:hx300033@10.217.180.65:30100"  
hf download Qwen/Qwen3-8B  --local-dir  ./data/Qwen3-8B

export https_proxy="http://hexin:hx300033@10.217.180.65:30100"
export http_proxy="http://hexin:hx300033@10.217.180.65:30100"  
hf download Qwen/Qwen3.5-122B-A10B  --local-dir  ./data/Qwen3.5-122


export https_proxy="http://hexin:hx300033@10.217.180.65:30100"
export http_proxy="http://hexin:hx300033@10.217.180.65:30100"  
hf download Qwen/Qwen3-Omni-30B-A3B-Instruct --local-dir  ./data/Qwen3-Omini-30A3B

modelscope download --model Qwen/Qwen3.5-4B --local_dir ./data/Qwen3.5-4B

modelscope download --model Qwen/Qwen3.5-0.8B --local_dir ./data/Qwen3.5-0.8B
hf download Qwen/Qwen3.5-0.8B --local-dir ./data/Qwen3.5-0.8B


modelscope download --model Qwen/Qwen3.5-4B README.md --local_dir ./dir

HF_MODEL_PATH=data/Qwen3.5-35-A3B
MCORE_MODEL_PATH=data/Qwen3.5-35-A3B-MCORE
python -m scripts.converter_hf_to_mcore --hf_model_path $HF_MODEL_PATH --output_path $MCORE_MODEL_PATH



HF_MODEL_PATH=data/Qwen3-VL-30B-A3B-Instruct
MCORE_MODEL_PATH=data/Qwen3-VL-30B-A3B-Instruct-MCORE
python -m scripts.converter_hf_to_mcore --hf_model_path $HF_MODEL_PATH --output_path $MCORE_MODEL_PATH

Qwen3-VL-30B-A3B-Instruct

pkill -9 -f VLLM

export env=fuji
python3 /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/finquery_2.py





cp -r /mnt/code/stock-rl/wandb/ /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/tmp_wandb
wandb login


wandb sync /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/wandb_logs/seeUPO_danjie/wandb/latest-run

huggingfacetoken 

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

wandb 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



source ./miniconda3/bin/activate 

########## hotspot

python dataset_make_2_parallel_hotspot.py \
    --input /cpfs01/nlp/leizhengxing/stock-rl/data/hotpot/hotpotqa_train.jsonl \
    --output ./data/hotpt_rl/hotpot_rl \
    --max_samples 5000


##### webshop

python dataset_make_2_parallel_webshop.py --webshop_path ./WebShop --train_size 3000 --test_size 200 --output_dir ./data/webshop_rl_data

https://git-cc.myhexin.com:6443/leizhengxing/stock-rl.git

pkill -f uvicorn


bash /cpfs01/nlp/leizhengxing/stock-rl/run_megatron_2b_my_dataset_test_EMPO_alfworld_2.sh | tee text_bash.txt &

##### install miniconda

wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh -b -p /cpfs01/nlp/leizhengxing/stock-rl/miniconda3
bash Miniconda3-latest-Linux-x86_64.sh -b -p ./miniconda3

######### alfworld
source ./miniconda3/bin/activate 
conda create -n alfworld python=3.9 
conda activate alfworld
pip install "spacy<3.6"
pip install alfworld
alfworld-download
pip install torch==1.13.1
pip install pyyaml
pip install uvicorn
pip install fastapi
cp -r ~/.cache/alfworld/ ./data/alfworld_env_data


######### alfworld_dev

git clone https://github.com/alfworld/alfworld.git alfworld
cd alfworld

source ./miniconda3/bin/activate 
conda create -n alfworld_dev python=3.9 
conda activate alfworld_dev
pip install "spacy<3.6"
cd alfworld
pip install -e .
alfworld-download
pip install torch==1.13.1
pip install pyyaml
pip install uvicorn
pip install fastapi
cp -r ~/.cache/alfworld/ ./data/alfworld_env_data




######## webshop
python -m spacy download en_core_web_sm

# source ./miniconda3/bin/activate 

git clone https://github.com/princeton-nlp/webshop.git webshop
source ./miniconda3/bin/activate 

conda create -n webshop python=3.8.13
conda activate webshop
# conda install -c conda-forge openjdk=11 "python=3.8.13"

# pip uninstall flask werkzeug -y
# pip install flask==2.0.3 werkzeug==2.0.3

# pip install \python -c "import sys; print(sys.version)"
# python -c "import spacy; print('spacy', spacy.__version__)"
# python -c "import pydantic; print('pydantic', pydantic.__version__)"
# python -c "import typing_extensions; print('typing_extensions', typing_extensions.__version__)"
#   "spacy<3.7" \
#   "numpy<1.25" \
#   "tqdm" \
#   "gdown"

# pip install clean-text
# pip install pyserini==0.20.0
# pip install rank_bm25
# pip install gym
# pip install selenium
# pip install rich
# pip install torch==2.0.1
# pip install thefuzz
# python -m spacy download en_core_web_sm
# ./setup.sh -d small


# pip uninstall -y numpy
# pip install numpy==1.23.5







######### make OOD dataset

# One-shot: stamp + build
python dataset_make_2_parallel_ood_test.py --stamp-xlsx --make-dataset \
  --input-xlsx ./data/识股-无答案.xlsx \
  --output-xlsx ./data/识股-无答案_uid.xlsx \
  --save-dir ./data/new_train/ \
  --system-prompt-path ./system_prompt.md \
  --output-jsonl ood_dataset.jsonl


#######
# One-shot: stamp + build
python dataset_make_2_parallel_ood_test_with_answer.py --stamp-xlsx --make-dataset \
  --input-xlsx ./data/识股-标准答案.xlsx \
  --output-xlsx ./data/识股-标准答案_uid.xlsx \
  --save-dir ./data/new_train/ \
  --system-prompt-path ./system_prompt.md \
  --output-jsonl ood_dataset.jsonl




python3 /cpfs01/nlp/leizhengxing/stock-rl/proprocess_val_log.py
tar -cvf empo.tar stock_empo_big_learn_rate_fix_reward_process/
tar -cvf grpo.tar stock_grpo_big_learn_rate_fix_reward_process/
tar -cvf 35B_new_coder_ood_test.tar 35B_new_coder_ood_test_process/
tar -cvf empo_dual_clip.tar stock_empo_big_learn_rate_fix_reward_dual_clip_process/



python -m pyserini.index -collection JsonCollection -generator DefaultLuceneDocumentGenerator -threads 1 -input resources_5k -index indexes_5k -storePositions -storeDocvectors -storeRaw

git clone https://git-cc.myhexin.com:6443/leizhengxing/stock-rl.git

###

git ls-files -z | xargs -0 du -h | sort -hr | head -20
git rm --cached alfworld
git fetch origin
git reset --hard origin/master



python dataset_make_multihop_mix.py \
    --hotpot_train    ./data/multihop_raw/hotpot_train.jsonl \
    --wiki2_train     ./data/multihop_raw/2wiki_train.jsonl \
    --musique_train   ./data/multihop_raw/musique_train.jsonl \
    --hotpot_test     ./data/multihop_raw/hotpot_test.jsonl \
    --wiki2_test      ./data/multihop_raw/2wiki_test.jsonl \
    --musique_test    ./data/multihop_raw/musique_test.jsonl \
    --bamboogle_test  ./data/multihop_raw/bamboogle.jsonl \
    --output_dir      ./data/multihop_mix \
    --val_ratio       0.003 \
    --max_per_source  5000 \
    --max_test_per_source 100 \
    --seed            42



python dataset_make_multihop_mix_reflect.py \
    --hotpot_train    ./data/multihop_raw/hotpot_train.jsonl \
    --wiki2_train     ./data/multihop_raw/2wiki_train.jsonl \
    --musique_train   ./data/multihop_raw/musique_train.jsonl \
    --hotpot_test     ./data/multihop_raw/hotpot_test.jsonl \
    --wiki2_test      ./data/multihop_raw/2wiki_test.jsonl \
    --musique_test    ./data/multihop_raw/musique_test.jsonl \
    --bamboogle_test  ./data/multihop_raw/bamboogle.jsonl \
    --output_dir      ./data/multihop_mix_reflect \
    --system_prompt_path ./system_prompt_hot_reflect.md \
    --val_ratio       0.02 \
    --max_per_source  5000 \
    --max_test_per_source 100 \
    --seed            42

python dataset_make_multimodal_vqa.py     --output_dir    ./data/multimodal_vqa     --val_ratio     0.05     --seed          42     --num_workers   16     --max_per_source 5000



python dataset_make_multimodal_vqa.py \
  --output_dir ./data/multimodal_vqa \
  --val_ratio 0.05 \
  --seed 42 \
  --num_workers 4 \
  --max_per_source 5000 \
  --datasets okvqa mmmu \
  --viquae_images_dir ./data/viquae_images


python dataset_make_multimodal_vqa.py \
  --output_dir ./data/multimodal_vqa \
  --val_ratio 0.05 \
  --seed 42 \
  --num_workers 16 \
  --max_per_source 5000 \
  --datasets  viquae \
  --viquae_download_delay 1.0 \
  --viquae_download_retries 3



python dataset_make_multimodal_vqa.py \
  --output_dir ./data/multimodal_vqa \
  --val_ratio 0.05 \
  --seed 42 \
  --num_workers 16 \
  --max_per_source 50 \
  --datasets okvqa mmmu \
  --mmmu_train_splits dev validation test \
  --mmmu_test_ratio 0.05 \
  --viquae_download_delay 1.0 \
  --viquae_download_retries 3




rm data/multimodal_vqa/mmmu_*.jsonl data/multimodal_vqa/mmmu_*.parquet

python dataset_make_multimodal_vqa.py \
  --output_dir ./data/multimodal_vqa \
  --val_ratio 0.05 \
  --seed 42 \
  --num_workers 16 \
  --max_per_source 5000 \
  --datasets mmmu okvqa \
  --mmmu_train_splits dev validation test \
  --mmmu_test_ratio 0.05


tar -xvf agent-03.tar
tar -xvf data.tar

mkdir evaluate_data
tar -xvf data.tar -C evaluate_data

tar -xvf model.tar
tar -xvf gold_train.tar
tar -xvf golden_test.tar


CUDA_VISIBLE_DEVICES=0,1,2,3 python agent-o3/main_contrast_out.py \
  --model /cpfs01/nlp/leizhengxing/stock-rl/data/model \
  --vllm_port 8100 \
  --served_model_name local-model \
  --vllm_tensor_parallel_size 4 \
  --vllm_gpu_memory_utilization 0.9


pip install json_repair


pkill -9 -f 'VLLM::'
pkill -9 -f 'vllm.entrypoints.openai.api_server'
rm  ./data/30/data/dpo_260224/vllm_server.log
python agent-o3/main_contrast_out.py \
  --model /cpfs01/nlp/leizhengxing/stock-rl/data/model \
  --vllm_port 8100 \
  --served_model_name local-model \
  --vllm_tensor_parallel_size 4 \
  --vllm_enable_expert_parallel \
  --vllm_expert_placement_strategy round_robin \
  --vllm_gpu_memory_utilization 0.80 \
  --vllm_max_model_len 16384 \
  --vllm_dtype bfloat16


pkill -9 -f 'VLLM::'
pkill -9 -f 'vllm.entrypoints.openai.api_server'
rm  ./data/30/data/dpo_260224/vllm_server.log

python agent-o3/main_contrast_out.py \
  --model /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/Qwen3.5-0.8B \
  --vllm_port 8100 \
  --served_model_name local-model \
  --vllm_tensor_parallel_size 4 \
  --vllm_gpu_memory_utilization 0.80 \
  --vllm_max_model_len  40960 \
  --vllm_dtype bfloat16 \
  2>&1 | tee eval.txt &






#######
tmux new -s wiki_reflect_with_mask


####

cd /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect
mkdir -p formal_run_log

nohup bash formal_run_script/wiki_reflect_search_with_mask.sh \
  > formal_run_log/wiki_reflect_search_with_mask.log 2>&1 < /dev/null &

echo $! > formal_run_log/wiki_reflect_search_with_mask.pid
disown



nohup bash run_script/run_watch_checkpoints_eval.sh \
  > eval_nohup.out 2>&1 &
echo $!





pkill -TERM -f 'agent-o3/watch_checkpoints_eval.py' || true
  pkill -TERM -f 'agent-o3/main_contrast_out.py' || true
  pkill -TERM -f 'vllm.entrypoints.openai.api_server' || true
  pkill -TERM -f 'vllm' || true
  pkill -TERM -f 'VLLM' || true
  sleep 5
  pkill -KILL -f 'agent-o3/watch_checkpoints_eval.py' || true
  pkill -KILL -f 'agent-o3/main_contrast_out.py' || true
  pkill -KILL -f 'vllm.entrypoints.openai.api_server' || true
  pkill -KILL -f 'vllm' || true
  pkill -KILL -f 'VLLM' || true

cd /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect
CLIENT_REWARD_ENDPOINT=http://10.244.200.11:8002/v1/chat/completions \
CLIENT_REWARD_MODEL_NAME=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/verl/data/Qwen3-4B \
SERVICE_MODEL_PATH=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/verl/data/Qwen3-4B \
bash run_script/run_digital_onboarding_service_quick_test.sh




cd /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect

nohup bash run_script/start_digital_onboarding_4b_multirole_server.sh \
  > /tmp/digital_onboarding_4b_server.log 2>&1 &

echo $! > /tmp/digital_onboarding_4b_server.pid
disown


python open-account/scripts/compact_history_log.py open-account/history.log -o open-account/history.compacted.txt
python open-account/scripts/compact_history_log.py open-account/history.log --include-logs -o open-account/history.compacted.txt


cd /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect
ray stop
  pkill -KILL -f 'vllm' || true
  pkill -KILL -f 'VLLM' || true
  rm -rf /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/rollout_log/formal_train
DIGITAL_ONBOARDING_TOOL_BACKEND=real_bank \
DIGITAL_ONBOARDING_REAL_BANK_UNIQUE_IDENTITIES=1 \
ENABLE_THINKING=True \
DIGITAL_ONBOARDING_BANK_REWARD_WEIGHT=0.5 \
DIGITAL_ONBOARDING_DEBUG_ESCAPE_NEWLINES=0 \
REWARD_MAX_TOKENS=4096 \
CUSTOMER_MAX_TOKENS=2048 \
MAX_RESPONSE_LENGTH=40000 \
PROJECT_NAME=digital_people \
JOB_NAME=formal_train \
CLIENT_REWARD_ENDPOINT=http://10.244.233.239:8002/v1/chat/completions \
CLIENT_REWARD_MODEL_NAME=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/data/Qwen3.5-9B \
CUSTOMER_MODEL_RETRY_ATTEMPTS=5 \
DIGITAL_ONBOARDING_RULE_GUARD_FIELDS=0 \
MAX_ASSISTANT_TURNS=80 \
MAX_USER_TURNS=80 \
SERVICE_MODEL_PATH=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/Qwen3-Omini-30A3B \
SCENARIO_PHASE=phase1 \
SKIP_SERVER_READY_CHECK=0 \
LR=3e-6 \
SAVE_FREQ=5 \
TEST_FREQ=5 \
TRAIN_SIZE=2048 \
bash /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/run_script/run_digital_onboarding_service_formal_train.sh


python3 /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/recipe/digital_onboarding/scripts/split_rollout_by_bank_score.py \
  --input_dir /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/rollout_log/formal_train


  ########### new 27B service

  DIGITAL_ONBOARDING_TOOL_BACKEND=real_bank \
DIGITAL_ONBOARDING_REAL_BANK_UNIQUE_IDENTITIES=1 \
ENABLE_THINKING=True \
DIGITAL_ONBOARDING_BANK_REWARD_WEIGHT=0.5 \
DIGITAL_ONBOARDING_DEBUG_ESCAPE_NEWLINES=0 \
REWARD_MAX_TOKENS=4096 \
CUSTOMER_MAX_TOKENS=2048 \
MAX_RESPONSE_LENGTH=40000 \
PROJECT_NAME=digital_people \
JOB_NAME=formal_train \
SERVICE_MODEL_PATH=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/Qwen3-Omini-30A3B \
SCENARIO_PHASE=phase1 \
SKIP_SERVER_READY_CHECK=0 \
bash run_script/run_digital_onboarding_service_formal_train_qwen35_27b.sh

##########


cd /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect
  rm -rf /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/rollout_log/formal_train_no_think
ray stop
  pkill -KILL -f 'vllm' || true
  pkill -KILL -f 'VLLM' || true
DIGITAL_ONBOARDING_TOOL_BACKEND=real_bank \
DIGITAL_ONBOARDING_REAL_BANK_UNIQUE_IDENTITIES=1 \
ENABLE_THINKING=False \
DIGITAL_ONBOARDING_BANK_REWARD_WEIGHT=0.5 \
DIGITAL_ONBOARDING_DEBUG_ESCAPE_NEWLINES=0 \
REWARD_MAX_TOKENS=4096 \
CUSTOMER_MAX_TOKENS=2048 \
MAX_RESPONSE_LENGTH=40000 \
PROJECT_NAME=digital_people \
JOB_NAME=formal_train_no_think \
CLIENT_REWARD_ENDPOINT=http://10.244.71.253:8002/v1/chat/completions \
CLIENT_REWARD_MODEL_NAME=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/data/Qwen3.5-9B \
CUSTOMER_MODEL_RETRY_ATTEMPTS=5 \
DIGITAL_ONBOARDING_RULE_GUARD_FIELDS=0 \
MAX_ASSISTANT_TURNS=80 \
MAX_USER_TURNS=80 \
SERVICE_MODEL_PATH=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/Qwen3-Omini-30A3B \
SCENARIO_PHASE=phase1 \
SKIP_SERVER_READY_CHECK=0 \
bash /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/run_script/run_digital_onboarding_service_formal_train.sh



ray stop
  pkill -KILL -f 'vllm' || true
  pkill -KILL -f 'VLLM' || true
bash /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/run_script/reflect_demo.sh


git clone https://git-cc.myhexin.com:6443/leizhengxing/stock-rl-reflect.git

 nohup bash /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/run_script/reflect_demo.sh > nohup.txt 2>&1 &


 find . -mindepth 1 -maxdepth 1 -type d -exec du -sh {} + | sort -hr


cd /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect
nohup ./run_script/prune_old_checkpoints.sh --delete --watch --interval 60 > /tmp/prune_old_checkpoints_watch.log 2>&1 &


python dataset_make_multihop_mix_pag.py \
    --hotpot_train    ./data/multihop_raw/hotpot_train.jsonl \
    --wiki2_train     ./data/multihop_raw/2wiki_train.jsonl \
    --musique_train   ./data/multihop_raw/musique_train.jsonl \
    --hotpot_test     ./data/multihop_raw/hotpot_test.jsonl \
    --wiki2_test      ./data/multihop_raw/2wiki_test.jsonl \
    --musique_test    ./data/multihop_raw/musique_test.jsonl \
    --bamboogle_test  ./data/multihop_raw/bamboogle.jsonl \
    --output_dir      ./data/multihop_mix_pag \
    --system_prompt_path ./system_prompt_hot.md \
    --val_ratio       0.02 \
    --max_per_source  5000 \
    --max_test_per_source 100 \
    --seed            42


#### launch the server
  pkill -KILL -f 'vllm' || true
  pkill -KILL -f 'VLLM' || true
cd /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect
MAX_MODEL_LEN=65536 \
INTERACTIVE_MAX_TOKENS=4096 \
INTERACTIVE_MIN_RESPONSE_TOKENS=512 \
INTERACTIVE_CONTEXT_MARGIN_TOKENS=512 \
SERVER_TP=4 \
bash run_script/start_digital_onboarding_interactive.sh

##### real 
  pkill -KILL -f 'vllm' || true
  pkill -KILL -f 'VLLM' || true
  ray stop
cd /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect
INTERACTIVE_BRANCH_MODE=0 \
DIGITAL_ONBOARDING_BRANCH_MODE=us_market \
INTERACTIVE_DEBUGPY=0 \
MAX_MODEL_LEN=65536 \
DIGITAL_ONBOARDING_REAL_BANK_UNIQUE_IDENTITIES=1 \
DIGITAL_ONBOARDING_REAL_BANK_FAKE_VERIFICATION_WRAPPER=1 \
DIGITAL_ONBOARDING_REAL_BANK_BYPASS_SEND_RATE_LIMIT=0 \
DIGITAL_ONBOARDING_REAL_BANK_ALLOW_AUTH_BYPASS=0 \
DIGITAL_ONBOARDING_REAL_BANK_FAKE_UPLOAD_WRAPPER=0 \
INTERACTIVE_MAX_TOKENS=4096 \
INTERACTIVE_MIN_RESPONSE_TOKENS=512 \
INTERACTIVE_CONTEXT_MARGIN_TOKENS=512 \
SERVER_TP=4 \
MODEL_PATH=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/checkpoints/newest_bank_only_america/global_step_5/actor/huggingface \
bash run_script/start_digital_onboarding_interactive.sh
######
/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/checkpoints/store
/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/Qwen3-Omini-30A3B


ssh -N -L 7861:10.244.62.180:7860 OminiRobots
http://localhost:7861
ssh -N -L 7861:127.0.0.1:7860 OminiRobots

################# new part

cd /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect

DATA_DIR=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/digital_onboarding/service_formal \
rm -rf /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/digital_onboarding/service_formal
python3 -m recipe.digital_onboarding.scripts.build_data \
  --output-dir "/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/digital_onboarding/service_formal" \
  --train-size 2560 \
  --val-size 64 \
  --seed 17 \
  --behavior-mode phase1 \
  --branch-mode us_market \
  --tool-backend real_bank


####### newest code 

  pkill -KILL -f 'vllm' || true
  pkill -KILL -f 'VLLM' || true
  ray stop
cd /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect && nohup env \
rm -rf /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/rollout_log/fix_time_out
rm -rf /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/checkpoints/fix_time_out
DIGITAL_ONBOARDING_REAL_BANK_IDENTITY_NAMESPACE=us_market_bank_pool_v1 \
DIGITAL_ONBOARDING_REAL_BANK_IDENTITY_OFFSET=3000 \
DIGITAL_ONBOARDING_TOOL_BACKEND=real_bank \
DIGITAL_ONBOARDING_BRANCH_MODE=us_market \
DIGITAL_ONBOARDING_REAL_BANK_UNIQUE_IDENTITIES=1 \
DIGITAL_ONBOARDING_REAL_BANK_LEGACY_SSN_NAMESPACE=0 \
DIGITAL_ONBOARDING_REAL_BANK_FAKE_VERIFICATION_WRAPPER=1 \
DIGITAL_ONBOARDING_REAL_BANK_FAKE_UPLOAD_WRAPPER=0 \
DIGITAL_ONBOARDING_REAL_BANK_UPLOAD_NEED_MIN=0 \
DIGITAL_ONBOARDING_REQUIRE_UPLOADED_IMAGE=1 \
DIGITAL_ONBOARDING_FINISHABLE_BINARY_REWARD=1 \
DIGITAL_ONBOARDING_BANK_REWARD_ENABLED=1 \
ENABLE_THINKING=True \
DIGITAL_ONBOARDING_BANK_REWARD_WEIGHT=0.5 \
DIGITAL_ONBOARDING_DEBUG_ESCAPE_NEWLINES=0 \
REWARD_MAX_TOKENS=4096 \
CUSTOMER_MAX_TOKENS=2048 \
MAX_PROMPT_LENGTH=7000 \
MAX_RESPONSE_LENGTH=40000 \
MAX_TOOL_RESPONSE_LENGTH=4096 \
PROJECT_NAME=digital_people \
JOB_NAME=fix_time_out_and_some_other \
CLIENT_REWARD_ENDPOINT=http://10.244.233.239:8002/v1/chat/completions \
CLIENT_REWARD_MODEL_NAME=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/data/Qwen3.5-9B \
CUSTOMER_MODEL_RETRY_ATTEMPTS=5 \
DIGITAL_ONBOARDING_RULE_GUARD_FIELDS=0 \
MAX_ASSISTANT_TURNS=80 \
MAX_USER_TURNS=80 \
SERVICE_MODEL_PATH=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/Qwen3-Omini-30A3B \
SCENARIO_PHASE=phase1 \
SKIP_SERVER_READY_CHECK=0 \
LR=3e-6 \
SAVE_FREQ=5 \
TEST_FREQ=5 \
DATA_DIR=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/digital_onboarding/service_formal \
TRAIN_BATCH_SIZE=128 \
PPO_MINI_BATCH_SIZE=64 \
TOTAL_EPOCHS=1 \
TRAIN_SIZE=1280 \
DIGITAL_ONBOARDING_REAL_BANK_API_SCRIPTS_DIR=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/new-open-account/scripts \
DIGITAL_ONBOARDING_CUSTOMER_INTERRUPTION_ENABLED=0 \
DIGITAL_ONBOARDING_CUSTOMER_INTERRUPTION_MIN_TURNS=10 \
DIGITAL_ONBOARDING_CUSTOMER_INTERRUPTION_MAX_TURNS=12 \
bash /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/run_script/run_digital_onboarding_service_formal_train.sh \
> /tmp/fix_time_out.log 2>&1 & echo $!





########

python3 /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/recipe/digital_onboarding/scripts/split_rollout_by_bank_score.py \
  --input_dir /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/rollout_log/fix_time_out_and_some_other


#####
_handle_real_send_verification_code
_handle_real_login_and_get_token
_handle_real_upload_file

####

rm -rf ~/.vscode-server
rm -rf ~/.vscode-remote

##### 
berkeley 2540, california


# python ./scripts/test_api.py  send_code 2026041402 +1


rm -f .session
cd /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/new-open-account/scripts
python test_api.py send_code 2026060802 +1
python test_api.py login 2026060802 123456 +1
python test_api.py token
python test_api.py upload test.png false


college avenue 2530, berekely california 94100


export https_proxy="http://hexin:hx300033@10.217.180.65:30100"
export http_proxy="http://hexin:hx300033@10.217.180.65:30100"

wandb sync --include-online --include-offline wandb/latest-run

wandb sync \
  --include-online \
  --include-offline \
  --include-synced \
  --append \
  --entity zhengxinglei539-easynet \
  --project wiki \
  --id k1l29zmj \
  wandb/latent/run-20260609_185501-k1l29zmj/run-k1l29zmj.wandb


##########
python3 dataset_make_multidomain_mix.py \
  --multihop_train ./data/multihop_hotpot_only/train_mix.parquet \
  --multihop_val   ./data/multihop_hotpot_only/val_mix.parquet \
  --math_train     ./data/math/train.parquet \
  --math_val       ./data/math/test.parquet \
  --code_train     ./data/code_apps/train.parquet \
  --code_val       ./data/code_apps/test.parquet \
  --code_data_source apps \
  --max_multihop_train 5000 \
  --max_math_train 5000 \
  --max_code_train 5000 \
  --max_multihop_val 100 \
  --max_math_val 100 \
  --max_code_val 100 \
  --output_dir ./data/multidomain_hotpot_math_code \
  --seed 42


python3 burn_gpu_smart.py &
kill "$(cat formal_run_log/burn_gpu_smart.pid)"


##### new build data


python3 -m recipe.digital_onboarding.scripts.build_data \
  --output-dir /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/data/digital_onboarding/service_formal \
  --train-size 2560 \
  --val-size 64 \
  --seed 17 \
  --behavior-mode phase1 \
  --branch-mode us_market \
  --tool-backend real_bank

####### fake test

DIGITAL_ONBOARDING_REAL_BANK_API_SCRIPTS_DIR=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/new-open-account/scripts \
DIGITAL_ONBOARDING_REAL_BANK_CONNECT_TIMEOUT=5 \
DIGITAL_ONBOARDING_REAL_BANK_READ_TIMEOUT=30 \
python3 -m recipe.digital_onboarding.scripts.stress_real_bank_upload \
  --trajectories 16 \
  --uploads-per-trajectory 2 \
  --upload-workers 32 \
  --auth-workers 8 \
  --upload-image-path "/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/Weixin Image_2026-06-01_155311_574.png" \
  --resize-max-edge 768 \
  --query-progress-after-upload \
  --output-jsonl /tmp/real_bank_upload_stress_16x2.jsonl


DIGITAL_ONBOARDING_REAL_BANK_API_SCRIPTS_DIR=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/new-open-account/scripts \
DIGITAL_ONBOARDING_REAL_BANK_CONNECT_TIMEOUT=5 \
DIGITAL_ONBOARDING_REAL_BANK_READ_TIMEOUT=30 \
python3 -m recipe.digital_onboarding.scripts.stress_real_bank_upload \
  --trajectories 128 \
  --uploads-per-trajectory 8 \
  --upload-workers 1024 \
  --auth-workers 32 \
  --upload-image-path "/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/Weixin Image_2026-06-01_155311_574.png" \
  --resize-max-edge 768 \
  --query-progress-after-upload \
  --output-jsonl /tmp/real_bank_upload_stress_128x8.jsonl



DIGITAL_ONBOARDING_REAL_BANK_API_SCRIPTS_DIR=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/new-open-account/scripts \
DIGITAL_ONBOARDING_REAL_BANK_CONNECT_TIMEOUT=5 \
DIGITAL_ONBOARDING_REAL_BANK_READ_TIMEOUT=30 \
python3 -m recipe.digital_onboarding.scripts.stress_real_bank_upload \
  --trajectories 128 \
  --uploads-per-trajectory 8 \
  --upload-workers 1024 \
  --auth-workers 32 \
  --upload-image-path "/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/Weixin Image_2026-06-01_155311_574.png" \
  --resize-max-edge 1 \
  --query-progress-after-upload \
  --output-jsonl /tmp/real_bank_upload_stress_128x8.jsonl


########## test

# Try upload-workers: 16, 32, 64, 128, 256
DIGITAL_ONBOARDING_REAL_BANK_API_SCRIPTS_DIR=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/new-open-account/scripts \
DIGITAL_ONBOARDING_REAL_BANK_CONNECT_TIMEOUT=5 \
DIGITAL_ONBOARDING_REAL_BANK_READ_TIMEOUT=30 \
python3 -m recipe.digital_onboarding.scripts.stress_real_bank_upload \
  --trajectories 128 \
  --uploads-per-trajectory 8 \
  --upload-workers 32 \
  --auth-workers 8 \
  --upload-mode query-progress \
  --output-jsonl /tmp/real_bank_query_progress_workers64.jsonl


####
bash run_script/sweep_real_bank_upload_workers.sh

##### real picture test
UPLOAD_MODE=normal bash run_script/sweep_real_bank_upload_workers.sh \
  --upload-image-path "/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/Weixin Image_2026-06-01_155311_574.png"


#### smallest test

UPLOAD_MODE=normal bash run_script/sweep_real_bank_upload_workers.sh \
  --upload-image-path "/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/Weixin Image_2026-06-01_155311_574.png" \
  --resize-max-edge 1


nohup env UPLOAD_MODE=normal bash run_script/sweep_real_bank_upload_workers.sh \
  --upload-image-path "/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect/Weixin Image_2026-06-01_155311_574.png" \
  --resize-max-edge 1 \
  > /tmp/real_bank_upload_workers.log 2>&1 & echo $!

nohup env UPLOAD_MODE=normal bash run_script/sweep_real_bank_upload_workers.sh \
  --upload-image-path /tmp/test_1px.png \
  > /tmp/real_bank_upload_workers_1px_png.log 2>&1 & echo $!


nohup env UPLOAD_MODE=empty-file bash run_script/sweep_real_bank_upload_workers.sh \
  > /tmp/real_bank_empty_file_workers_meta.log 2>&1 & echo $!

########## test original api

nohup bash run_script/sweep_test_api_upload_workers.sh \
  > /tmp/test_api_upload_workers_sweep.log 2>&1 & echo $!


###### compare with the original api with fair test

nohup env \
  UPLOAD_MODE=normal \
  UPLOADS_PER_TRAJECTORY=1 \
  bash run_script/sweep_real_bank_upload_workers.sh \
    --upload-image-path new-open-account/scripts/test.png \
  > upload_workers.log 2>&1 &


######## control variable

# B: prove thumbnail effect: thumbnail OFF + burst
nohup env UPLOAD_MODE=normal UPLOADS_PER_TRAJECTORY=1 WORKER_COUNTS="16 32 48 64" \
bash run_script/sweep_real_bank_upload_workers.sh \
  --upload-image-path new-open-account/scripts/test.png \
  --no-min-file \
  --raw-upload-response \
  > /tmp/stress_B_min_off_burst.log 2>&1 & echo $!


# C: prove burst effect: thumbnail OFF + immediate release
nohup env UPLOAD_MODE=normal UPLOADS_PER_TRAJECTORY=1 WORKER_COUNTS="16 32 48 64" \
bash run_script/sweep_real_bank_upload_workers.sh \
  --upload-image-path new-open-account/scripts/test.png \
  --no-min-file \
  --upload-release-mode immediate \
  --raw-upload-response \
  > /tmp/stress_C_min_off_immediate.log 2>&1 & echo $!


########## runnign algorithm
ray stop
  pkill -KILL -f 'vllm' || true
  pkill -KILL -f 'VLLM' || true
SKIP_PIP_INSTALL=1 \
STATE_PREDICTIVE_SEGMENT_BACKEND=torch \
STATE_PREDICTIVE_PRECOMPUTE_STATE_INDEX=True \
bash /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/research/run_script/demo_state_predictive_grpo.sh