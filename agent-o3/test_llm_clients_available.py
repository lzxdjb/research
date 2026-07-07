import argparse
import json
import time

from api import GEMINIClient, GPT4OClient


CLIENTS = {
    "gemini": GEMINIClient,
    "gpt4o": GPT4OClient,
}


def response_preview(response, max_chars=300):
    if isinstance(response, dict):
        response = response.get("content", response)
    if isinstance(response, (dict, list)):
        preview = json.dumps(response, ensure_ascii=False)
    else:
        preview = str(response)
    return preview.replace("\n", "\\n")[:max_chars]


def test_client(name, prompt, temperature=0, try_num=1, timeout=None):
    client_cls = CLIENTS[name]
    client = client_cls()
    if timeout is not None:
        client.timeout = timeout

    result = {
        "name": name,
        "client": client_cls.__name__,
        "model": client.model,
        "available": False,
        "auth": "not_started",
        "elapsed_seconds": None,
    }
    start_time = time.time()

    try:
        client.refresh_authority()
        result["auth"] = "ok"

        messages = [{"role": "user", "content": prompt}]
        response = client.get_gpt_response(
            messages,
            client.chat_url,
            temperature=temperature,
            try_num=try_num,
        )
        if isinstance(response, str) and response.startswith("error:"):
            raise RuntimeError(response)

        result["available"] = True
        result["response_preview"] = response_preview(response)
    except Exception as exc:
        if result["auth"] != "ok":
            result["auth"] = "failed"
        result["error"] = str(exc)
    finally:
        result["elapsed_seconds"] = round(time.time() - start_time, 2)
        client.close()

    return result


def print_result(result):
    status = "OK" if result["available"] else "FAIL"
    message = (
        f"[{status}] {result['client']} "
        f"model={result['model']} auth={result['auth']} "
        f"elapsed={result['elapsed_seconds']}s"
    )
    if result["available"]:
        message += f" response={result.get('response_preview', '')}"
    else:
        message += f" error={result.get('error', '')}"
    print(message)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Test whether GEMINIClient and GPT4OClient are available."
    )
    parser.add_argument(
        "--client",
        choices=["all", *CLIENTS.keys()],
        default="all",
        help="Client to test. Default: all.",
    )
    parser.add_argument(
        "--prompt",
        default="Please reply with OK only.",
        help="Small prompt used for the chat request.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0,
        help="Temperature for the test request.",
    )
    parser.add_argument(
        "--try-num",
        type=int,
        default=1,
        help="Retry count for the chat request.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Override client request timeout in seconds.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print full results as JSON.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    names = list(CLIENTS) if args.client == "all" else [args.client]
    results = [
        test_client(
            name,
            prompt=args.prompt,
            temperature=args.temperature,
            try_num=args.try_num,
            timeout=args.timeout,
        )
        for name in names
    ]

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for result in results:
            print_result(result)

    return 0 if all(result["available"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
