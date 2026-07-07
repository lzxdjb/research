from huggingface_hub import HfApi

api = HfApi()



api.upload_large_folder(
    folder_path="/cpfs01/nlp/leizhengxing/stock-rl/checkpoint/wiki_reflect_5000_inject_search",
    repo_id="lzxdjb/checkpoint",
    repo_type="dataset",
    num_workers=8,
)
