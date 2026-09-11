#!/usr/bin/env python3
"""Verify a complete, non-streaming chat completion through Tinfoil."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any


class SmokeFailure(RuntimeError):
    """A concise operator-facing smoke failure."""


def completion_url(api_base: str) -> str:
    return f"{api_base.rstrip('/')}/chat/completions"


def request_completion(
    *,
    api_base: str,
    api_key: str,
    model: str,
    timeout: float,
    reasoning_effort: str = "low",
) -> dict[str, Any]:
    request_body = json.dumps(
        {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": "Reply briefly to confirm response integrity.",
                }
            ],
            "stream": False,
            "max_tokens": 1024,
            "reasoning_effort": reasoning_effort,
            "temperature": 0,
        }
    ).encode()
    request = urllib.request.Request(
        completion_url(api_base),
        data=request_body,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get_content_type()
            response_body = response.read()
    except http.client.IncompleteRead as exc:
        if exc.expected is None:
            raise SmokeFailure(
                f"incomplete response body: received {len(exc.partial)} bytes "
                "before the connection closed (chunked transfer, length unknown)"
            ) from exc
        expected_total = len(exc.partial) + exc.expected
        raise SmokeFailure(
            "incomplete response body: "
            f"received {len(exc.partial)} of {expected_total} declared bytes"
        ) from exc
    except urllib.error.HTTPError as exc:
        raise SmokeFailure(f"proxy returned HTTP {exc.code}") from exc
    except (TimeoutError, urllib.error.URLError) as exc:
        raise SmokeFailure(f"proxy request failed: {exc.reason if hasattr(exc, 'reason') else exc}") from exc

    if content_type != "application/json":
        raise SmokeFailure(f"expected application/json, received {content_type}")

    try:
        payload = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise SmokeFailure(f"response body is not complete JSON: {exc.msg}") from exc

    if not isinstance(payload, dict):
        raise SmokeFailure("completion response must be a JSON object")
    if payload.get("model") != model:
        raise SmokeFailure("returned model does not match the requested model")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise SmokeFailure("completion response is missing choices")
    first_choice = choices[0]
    if not isinstance(first_choice, dict) or not isinstance(
        first_choice.get("message"), dict
    ):
        raise SmokeFailure("completion response is missing the assistant message")

    content = first_choice["message"].get("content")
    if first_choice.get("finish_reason") != "stop" or not isinstance(content, str) or not content.strip():
        raise SmokeFailure("model did not finish a non-empty assistant answer")
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify strict non-streaming response integrity through Tinfoil."
    )
    parser.add_argument(
        "--api-base",
        default=os.environ.get("LLM_API_URL", "http://tinfoil-proxy:8089/v1"),
        help="OpenAI-compatible API base URL",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("LLM_MODEL")
        or os.environ.get("TINFOIL_MODEL")
        or "glm-5-3-flash",
        help="Model used for the completion smoke",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120,
        help="Request timeout in seconds (default: 120)",
    )
    parser.add_argument("--reasoning-effort", default=os.environ.get("TINFOIL_REASONING_EFFORT") or "low")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("TINFOIL_API_KEY")
    if not api_key:
        print("[FAIL] LLM_API_KEY is required", file=sys.stderr)
        return 2

    try:
        payload = request_completion(
            api_base=args.api_base,
            api_key=api_key,
            model=args.model,
            timeout=args.timeout,
            reasoning_effort=args.reasoning_effort,
        )
    except SmokeFailure as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1

    response_model = payload["model"]
    print(f"[PASS] non-streaming response integrity verified ({response_model})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
