import sys
import json
import os
from tqdm import tqdm
sys.path.insert(0, '../')

from web_agent_site.utils import DEFAULT_FILE_PATH
from web_agent_site.engine.engine import load_products

# This naturally loads ALL products
all_products, *_ = load_products(filepath=DEFAULT_FILE_PATH)

docs = []
for p in tqdm(all_products, total=len(all_products)):
    option_texts = []
    options = p.get('options', {})
    for option_name, option_contents in options.items():
        option_contents_text = ', '.join(option_contents)
        option_texts.append(f'{option_name}: {option_contents_text}')
    option_text = ', and '.join(option_texts)

    doc = dict()
    doc['id'] = p['asin']
    doc['contents'] = ' '.join([
        p.get('Title', ''),
        p.get('Description', ''),
        p.get('BulletPoints', [''])[0] if p.get('BulletPoints') else '',
        option_text,
    ]).lower()
    doc['product'] = p
    docs.append(doc)

# Create directories
for d in ['100', '1k', '5k', '10k', '100k', '']:
    os.makedirs(f'./resources_{d}' if d else './resources', exist_ok=True)

# Write out the splits
print("Writing 100...")
with open('./resources_100/documents.jsonl', 'w+') as f:
    for doc in docs[:100]: f.write(json.dumps(doc) + '\n')

print("Writing 1k...")
with open('./resources_1k/documents.jsonl', 'w+') as f:
    for doc in docs[:1000]: f.write(json.dumps(doc) + '\n')

print("Writing 5k...")
with open('./resources_5k/documents.jsonl', 'w+') as f:
    for doc in docs[:5000]: f.write(json.dumps(doc) + '\n')

print("Writing 10k...")
with open('./resources_10k/documents.jsonl', 'w+') as f:
    for doc in docs[:10000]: f.write(json.dumps(doc) + '\n')

print("Writing 100k...")
with open('./resources_100k/documents.jsonl', 'w+') as f:
    for doc in docs[:100000]: f.write(json.dumps(doc) + '\n')

print("Writing Full...")
with open('./resources/documents.jsonl', 'w+') as f:
    for doc in docs: f.write(json.dumps(doc) + '\n')

print("Done converting!")