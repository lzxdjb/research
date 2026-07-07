

cd /mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock-rl/data

tar -czvf new_data_2.tar.gz k_testdata/
## huggingface-cli login
## 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
## first create repo in huggingface


from huggingface_hub import upload_file
import os

# Set proxy environment variables
os.environ["https_proxy"] = "http://hexin:hx300033@10.244.57.231:30100"
os.environ["http_proxy"] = "http://hexin:hx300033@10.244.57.231:30100"
upload_file(
    path_or_fileobj="/mnt/data/HithinkOmniSSD/user_workspace/leizhengxing/stock_agent_loop/data/new_data_2.tar.gz",
    path_in_repo="new_data_2.tar.gz",
    repo_id="lzxdjb/new_data_2",
    repo_type="dataset",
)

##### then download

from huggingface_hub import snapshot_download

snapshot_download(
    repo_id="lzxdjb/new_data_2",
    repo_type="dataset",
    local_dir="/cpfs01/nlp/leizhengxing/stock-rl/data",
    allow_patterns=["new_data_2.tar.gz"],  # 🔑 ONLY download the tar
    max_workers=1,
    resume_download=True,
    local_dir_use_symlinks=False,
)

cd /cpfs01/nlp/leizhengxing/stock-rl/data
tar -xzvf new_data_2.tar.gz


#### if it is the model:
directly using:

hf download lzxdjb/checkpoint --local-dir ./data