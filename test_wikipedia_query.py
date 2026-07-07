#!/usr/bin/env python3
"""Small CLI smoke test for the `wikipedia` PyPI package."""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from urllib.parse import quote, urlparse

try:
    import wikipedia
    import wikipedia.wikipedia as wikipedia_core
except ImportError as exc:
    raise SystemExit(
        "Missing dependency: install it with `pip install wikipedia`."
    ) from exc

try:
    import requests
except ImportError as exc:
    raise SystemExit(
        "Missing dependency: install it with `pip install requests`."
    ) from exc


DEFAULT_USER_AGENT = "stock-rl-reflect-wikipedia-test/1.0"


def _one_line(text: str, max_chars: int) -> str:
    """Collapse whitespace and keep output readable in terminals/logs."""
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _print_disambiguation(title: str, exc: Exception, max_options: int) -> None:
    options = getattr(exc, "options", [])[:max_options]
    print(f"  ! '{title}' is ambiguous. First options:")
    for option in options:
        print(f"    - {option}")


def _force_https_api(lang: str) -> str:
    """The `wikipedia` package defaults to HTTP; HTTPS avoids proxy/redirect junk."""
    api_url = f"https://{lang.lower()}.wikipedia.org/w/api.php"
    wikipedia_core.API_URL = api_url
    wikipedia.API_URL = api_url
    return api_url


def _set_user_agent(user_agent: str) -> None:
    wikipedia_core.USER_AGENT = user_agent
    wikipedia.USER_AGENT = user_agent


def _install_wikipedia_timeout(timeout: float) -> None:
    """The package does not expose a timeout, so patch its requests module."""
    original_get = wikipedia_core.requests.get

    def get_with_timeout(*args, **kwargs):
        kwargs.setdefault("timeout", timeout)
        return original_get(*args, **kwargs)

    wikipedia_core.requests.get = get_with_timeout


def _debug_mediawiki_api(
    api_url: str,
    query: str,
    results: int,
    user_agent: str,
    timeout: float,
) -> None:
    params = {
        "action": "query",
        "format": "json",
        "list": "search",
        "srprop": "",
        "srlimit": results,
        "srsearch": query,
    }
    print("Debug HTTP request:")
    print(f"  url: {api_url}")
    try:
        response = requests.get(
            api_url,
            params=params,
            headers={"User-Agent": user_agent},
            timeout=timeout,
        )
    except Exception as exc:
        print(f"  request failed: {type(exc).__name__}: {exc}")
        return

    print(f"  final url: {response.url}")
    print(f"  status:    {response.status_code}")
    print(f"  type:      {response.headers.get('content-type', '<missing>')}")
    print(f"  bytes:     {len(response.content)}")
    print(f"  preview:   {_one_line(response.text[:500], 500)}")
    try:
        response.json()
    except json.JSONDecodeError as exc:
        print(f"  json:      decode failed: {exc}")
    else:
        print("  json:      ok")
    print()


def _search_mediawiki_direct(
    api_url: str,
    query: str,
    results: int,
    user_agent: str,
    timeout: float,
) -> list[str]:
    response = requests.get(
        api_url,
        params={
            "action": "query",
            "format": "json",
            "list": "search",
            "srprop": "",
            "srlimit": results,
            "srsearch": query,
        },
        headers={"User-Agent": user_agent},
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    return [item["title"] for item in data.get("query", {}).get("search", [])]


def _summary_mediawiki_direct(
    api_url: str,
    title: str,
    max_chars: int,
    user_agent: str,
    timeout: float,
) -> str:
    lang = urlparse(api_url).hostname.split(".", 1)[0]
    slug = quote(title.replace(" ", "_"))
    url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{slug}"
    response = requests.get(url, headers={"User-Agent": user_agent}, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    return _one_line(data.get("extract") or "<no extract>", max_chars)


def _summary_for_title(title: str, sentences: int, max_chars: int, auto_suggest: bool) -> None:
    try:
        summary = wikipedia.summary(
            title,
            sentences=sentences,
            auto_suggest=auto_suggest,
            redirect=True,
        )
    except wikipedia.DisambiguationError as exc:
        _print_disambiguation(title, exc, max_options=8)
    except wikipedia.PageError as exc:
        print(f"  ! Page not found: {exc}")
    except Exception as exc:  # Network/proxy/API failures are easiest to see this way.
        print(f"  ! Summary failed: {type(exc).__name__}: {exc}")
    else:
        print(f"  { _one_line(summary, max_chars) }")


def _page_for_title(title: str, max_chars: int, auto_suggest: bool) -> None:
    try:
        page = wikipedia.page(title, auto_suggest=auto_suggest, redirect=True)
    except wikipedia.DisambiguationError as exc:
        _print_disambiguation(title, exc, max_options=8)
    except wikipedia.PageError as exc:
        print(f"  ! Page not found: {exc}")
    except Exception as exc:
        print(f"  ! Page fetch failed: {type(exc).__name__}: {exc}")
    else:
        print(f"  title: {page.title}")
        print(f"  url:   {page.url}")
        print(f"  text:  {_one_line(page.content, max_chars)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Test live Wikipedia search through the `wikipedia` package.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "query",
        nargs="*",
        default=["Qwen"],
        help="Search query. Multiple words are joined with spaces.",
    )
    parser.add_argument("--lang", default="en", help="Wikipedia language code.")
    parser.add_argument("--results", type=int, default=5, help="Number of search results.")
    parser.add_argument(
        "--sentences",
        type=int,
        default=2,
        help="Number of sentences to request for each summary.",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=700,
        help="Maximum characters to print per summary/page preview.",
    )
    parser.add_argument(
        "--auto-suggest",
        action="store_true",
        help="Allow the package to auto-correct page titles before fetching summaries.",
    )
    parser.add_argument(
        "--fetch-page",
        action="store_true",
        help="Fetch page content previews instead of short summaries.",
    )
    parser.add_argument(
        "--no-https",
        action="store_true",
        help="Keep the wikipedia package default HTTP API URL.",
    )
    parser.add_argument(
        "--debug-http",
        action="store_true",
        help="Print MediaWiki HTTP status/content preview if search fails.",
    )
    parser.add_argument(
        "--direct",
        action="store_true",
        help="Use requests against MediaWiki directly instead of the wikipedia package.",
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="User-Agent to send to Wikipedia.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="HTTP timeout in seconds.",
    )
    return parser.parse_args()


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass

    args = parse_args()
    query = " ".join(args.query).strip()
    if not query:
        print("Query is empty.", file=sys.stderr)
        return 2

    wikipedia.set_lang(args.lang)
    _set_user_agent(args.user_agent)
    _install_wikipedia_timeout(args.timeout)
    api_url = wikipedia_core.API_URL if args.no_https else _force_https_api(args.lang)

    print(f"language: {args.lang}")
    print(f"query:    {query}")
    print(f"api:      {api_url}")
    print()

    try:
        if args.direct:
            titles = _search_mediawiki_direct(
                api_url,
                query,
                args.results,
                args.user_agent,
                args.timeout,
            )
        else:
            titles = wikipedia.search(query, results=args.results)
    except Exception as exc:
        print(f"Search failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        if args.debug_http:
            _debug_mediawiki_api(
                api_url,
                query,
                args.results,
                args.user_agent,
                args.timeout,
            )
        return 1

    if not titles:
        print("No search results found.")
        return 1

    print("Search results:")
    for i, title in enumerate(titles, start=1):
        print(f"{i}. {title}")
    print()

    print("Fetched content:")
    for i, title in enumerate(titles, start=1):
        print(textwrap.fill(f"{i}. {title}", width=100))
        if args.direct:
            try:
                summary = _summary_mediawiki_direct(
                    api_url,
                    title,
                    args.max_chars,
                    args.user_agent,
                    args.timeout,
                )
            except Exception as exc:
                print(f"  ! Direct summary failed: {type(exc).__name__}: {exc}")
            else:
                print(f"  {summary}")
        elif args.fetch_page:
            _page_for_title(title, args.max_chars, args.auto_suggest)
        else:
            _summary_for_title(title, args.sentences, args.max_chars, args.auto_suggest)
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
