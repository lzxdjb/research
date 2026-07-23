git config --global user.email "zhengxinglei539@gmail.com"
git config --global user.name "lzxdjb"
  


export http_proxy="http://hexin:hx300033@10.244.57.246:30100"
export https_proxy="http://hexin:hx300033@10.244.57.246:30100"




git remote add gitlab https://git-cc.myhexin.com:6443/leizhengxing/stock-agent-rl.git
git push -u gitlab main


git add .
git commit -m "dsw-1234 add multiple validate"
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
##########


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


########## running algorithm

MIX_MULTIHOP_SAMPLES=8000 \
MIX_ALFWORLD_SAMPLES=2000 \
MIX_WEBSHOP_SAMPLES=2000 \
REBUILD_MIXED_DATASET=1 \
TRAIN_BATCH_SIZE=32 \
PPO_MINI_BATCH_SIZE=32 \
VAL_BATCH_SIZE=32 \
ALFWORLD_SERVER_HOST=10.244.168.177 \
WEBSHOP_SERVER_HOST=10.244.13.156 \
ROLLOUT_N=8 \
bash run_script/demo_mixed_webshop_alfworld_multihop_grpo_avg_pass_val.sh

##### in nohup

nohup bash -lc '
MIX_MULTIHOP_SAMPLES=8000 \
MIX_ALFWORLD_SAMPLES=2000 \
MIX_WEBSHOP_SAMPLES=2000 \
REBUILD_MIXED_DATASET=1 \
TRAIN_BATCH_SIZE=32 \
PPO_MINI_BATCH_SIZE=32 \
VAL_BATCH_SIZE=32 \
ALFWORLD_SERVER_HOST=10.244.168.177 \
WEBSHOP_SERVER_HOST=10.244.13.156 \
ROLLOUT_N=8 \
bash run_script/demo_mixed_webshop_alfworld_multihop_grpo_avg_pass_val.sh
' > /tmp/demo_mixed_webshop_alfworld_multihop_grpo_avg_pass_val.log 2>&1 &

########

python3 burn_gpu_smart.py &
nohup bash run_script/start_digital_onboarding_4b_multirole_server.sh \
  > /tmp/digital_onboarding_4b_server.log 2>&1 &

echo $! > /tmp/digital_onboarding_4b_server.pid
disown


  pkill -KILL -f 'vllm' || true
  pkill -KILL -f 'VLLM' || true
  ray stop
pgrep -f 'multiprocessing\.spawn.*spawn_main' | xargs -r kill -KILL


################# single data build


set -euo pipefail

ROOT=/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl-reflect
cd "$ROOT"

export https_proxy="${https_proxy:-http://hexin:hx300033@10.217.180.65:30100}"
export http_proxy="${http_proxy:-http://hexin:hx300033@10.217.180.65:30100}"
export HTTPS_PROXY="$https_proxy"
export HTTP_PROXY="$http_proxy"
export HF_HUB_ETAG_TIMEOUT=60
export HF_HUB_DOWNLOAD_TIMEOUT=120
export HF_HUB_DISABLE_XET=1

# Versions currently installed when this pipeline was inspected.
python3 -m pip install \
  datasets==4.7.0 pandas==3.0.1 pyarrow==23.0.1 \
  huggingface_hub==1.18.0

mkdir -p \
  data/multihop_raw \
  data/math \
  data/code_apps \
  data/challenging_multidomain_math \
  data/challenging_multidomain_benchmark

# 1. Download HotpotQA, 2Wiki, MuSiQue, and Bamboogle from Hugging Face.
python3 download_multihop_datasets.py

# 2. Download and convert DigitalLearningGmbH/MATH-lighteval.
python3 -m examples.data_preprocess.math_dataset \
  --local_save_dir ./data/math

# 3. Download APPS raw JSONL and convert valid test-based problems to VERL format.
APPS_OUTPUT_DIR=./data/code_apps \
APPS_CACHE_DIR=./data/code_apps/.hf_cache \
python3 build_code_dataset.py

# 4. Download/convert DAPO, NuminaMath train, and AIMO AMC.
python3 preprocess_challenging_math_benchmarks.py \
  --output_dir ./data/challenging_multidomain_math \
  --skip_aime2024 \
  --skip_aime2025 \
  --numina_split train \
  --max_numina_rows 1000 \
  --seed 42

# 5. Build the exact train/validation mixture used by the launcher.
python3 make_dataset/dataset_make_challenging_multidomain.py \
  --wiki2_train ./data/multihop_raw/2wiki_train.jsonl \
  --hotpot_train ./data/multihop_raw/hotpot_train.jsonl \
  --musique_train ./data/multihop_raw/musique_train.jsonl \
  --math_train ./data/challenging_multidomain_math/dapo_math_17k.parquet \
  --code_train ./data/code_apps/train.jsonl \
  --wiki2_val ./data/multihop_raw/2wiki_test.jsonl \
  --bamboogle_val ./data/multihop_raw/bamboogle.jsonl \
  --hotpot_val ./data/multihop_raw/hotpot_test.jsonl \
  --musique_val ./data/multihop_raw/musique_test.jsonl \
  --math_val ./data/math/test.parquet \
  --math_val_level_filter "Level 5" \
  --numina_val ./data/challenging_multidomain_math/numina_math_cot.parquet \
  --aimo_amc_val ./data/challenging_multidomain_math/aimo_validation_amc.parquet \
  --code_val ./data/code_apps/test.jsonl \
  --max_2wiki_train 1000 --max_hotpot_train 1000 \
  --max_musique_train 1000 --max_math_train 3000 \
  --max_code_train 3000 \
  --max_2wiki_val 100 --max_bamboogle_val 100 \
  --max_hotpot_val 100 --max_musique_val 100 \
  --max_math_val 100 --max_numina_val 100 \
  --max_aimo_amc_val 100 --max_code_val 100 \
  --max_val_mix 50 --on_insufficient all \
  --output_dir ./data/challenging_multidomain_benchmark \
  --seed 42