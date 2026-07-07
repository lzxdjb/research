import json
import os

DATA_DIR = '/cpfs01/nlp/leizhengxing/stock-rl/webshop/data'
SEARCH_DIR = '/cpfs01/nlp/leizhengxing/stock-rl/webshop/search_engine'

print("1. Loading and slicing 10,000 products...")
with open(f'{DATA_DIR}/items_shuffle.json', 'r') as f:
    products = json.load(f)[:10000]
valid_asins = set(p['asin'] for p in products)

print("2. Filtering matching instructions...")
with open(f'{DATA_DIR}/items_ins_v2.json', 'r') as f:
    instructions = json.load(f)
    
if isinstance(instructions, dict):
    ins_10k = {k: v for k, v in instructions.items() if k in valid_asins}
else:
    ins_10k = [item for item in instructions if item.get('asin') in valid_asins]

with open(f'{DATA_DIR}/items_shuffle_10k.json', 'w') as f: 
    json.dump(products, f)
with open(f'{DATA_DIR}/items_ins_v2_10k.json', 'w') as f: 
    json.dump(ins_10k, f)

print("3. Creating Search Engine Documents...")
os.makedirs(f'{SEARCH_DIR}/resources_10k', exist_ok=True)
with open(f'{SEARCH_DIR}/resources_10k/docs.json', 'w') as f:
    for p in products:
        # Combine attributes for the text search engine
        categories = ' '.join(p.get('category', []))
        title = p.get('title', '')
        desc = p.get('description', '')
        bullets = p.get('bullet_point', '')
        if isinstance(bullets, list): 
            bullets = ' '.join(bullets)
        
        contents = f"{categories} {title} {desc} {bullets}"
        doc = {"id": p["asin"], "contents": contents}
        f.write(json.dumps(doc) + '\n')

print("Done! Files created successfully.")
