import json
import os

DATA_DIR = '/cpfs01/nlp/leizhengxing/stock-rl/webshop/data'

print("1. Loading full products to slice 5000...")
with open(f'{DATA_DIR}/items_shuffle.json', 'r') as f:
    products = json.load(f)[:5000]
valid_asins = set(p['asin'] for p in products)

print("2. Filtering matching instructions...")
with open(f'{DATA_DIR}/items_ins_v2.json', 'r') as f:
    instructions = json.load(f)

# Filter out only the 5000 ASINs
if isinstance(instructions, dict):
    ins_5k = {k: v for k, v in instructions.items() if k in valid_asins}
else:
    ins_5k = [item for item in instructions if item.get('asin') in valid_asins]

print("3. Saving lightweight JSON files...")
with open(f'{DATA_DIR}/items_shuffle_5k.json', 'w') as f: 
    json.dump(products, f)
with open(f'{DATA_DIR}/items_ins_v2_5k.json', 'w') as f: 
    json.dump(ins_5k, f)

print("Done! Fast loading files created.")