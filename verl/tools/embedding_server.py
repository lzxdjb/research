"""
Lightweight embedding server backed by sentence-transformers.

Start with:
    python -m verl.tools.embedding_server --model all-MiniLM-L6-v2 --port 8765

POST /embed   {"texts": ["...", "..."]}  → {"embeddings": [[...], ...]}
GET  /health  → {"status": "ok"}
"""

import argparse
import logging
from typing import List

import numpy as np
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

logger = logging.getLogger(__name__)

app = FastAPI()
_model = None


class EmbedRequest(BaseModel):
    texts: List[str]


class EmbedResponse(BaseModel):
    embeddings: List[List[float]]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/embed", response_model=EmbedResponse)
def embed(req: EmbedRequest):
    vecs = _model.encode(req.texts, normalize_embeddings=True)
    return EmbedResponse(embeddings=vecs.tolist())


def main():
    global _model
    import socket
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="BAAI/bge-large-en-v1.5")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    from sentence_transformers import SentenceTransformer
    _model = SentenceTransformer(args.model)

    # Resolve the actual IP so remote callers know where to connect.
    try:
        hostname = socket.getfqdn()
        ip = socket.gethostbyname(hostname)
    except Exception:
        ip = "127.0.0.1"

    url = f"http://{ip}:{args.port}"
    print(f"[embedding_server] model={args.model}")
    print(f"[embedding_server] listening on {args.host}:{args.port}")
    print(f"[embedding_server] reachable at {url}")
    print(f"[embedding_server] set embedding_server: {url}  in your memory config")

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
