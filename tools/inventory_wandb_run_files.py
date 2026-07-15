#!/usr/bin/env python3
"""Inventory W&B run files and artifacts without downloading large payloads."""

import argparse
import json
from pathlib import Path

import wandb


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_path")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--download-dir", type=Path)
    args = parser.parse_args()

    run = wandb.Api(timeout=60).run(args.run_path)
    files = [
        {"name": item.name, "size": item.size, "url": item.url}
        for item in run.files()
    ]
    artifacts = []
    for artifact in run.logged_artifacts():
        download_path = None
        if args.download_dir:
            download_path = artifact.download(root=args.download_dir / artifact.name.replace(":", "-"))
        artifacts.append({
            "name": artifact.name,
            "type": artifact.type,
            "version": artifact.version,
            "size": artifact.size,
            "download_path": download_path,
        })
    result = {"run": run.path, "files": files, "logged_artifacts": artifacts}
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
