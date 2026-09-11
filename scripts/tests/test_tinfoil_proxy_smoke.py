#!/usr/bin/env python3
"""Operator-facing contract tests for the local Tinfoil proxy smoke path."""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from scripts.tinfoil_response_integrity_smoke import SmokeFailure, request_completion


REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "tinfoil_response_integrity_smoke.py"


@contextmanager
def completion_server(
    body: bytes,
    *,
    declared_length: int,
) -> Iterator[tuple[str, list[dict[str, object]]]]:
    requests: list[dict[str, object]] = []

    class CompletionHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
            request_length = int(self.headers["Content-Length"])
            requests.append(json.loads(self.rfile.read(request_length)))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(declared_length))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            self.close_connection = True

        def log_message(self, _format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), CompletionHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def run_integrity_smoke(api_base: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SMOKE_SCRIPT),
            "--api-base",
            api_base,
            "--model",
            "smoke-model",
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "LLM_API_KEY": "smoke-placeholder", "TINFOIL_REASONING_EFFORT": "low"},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


class TinfoilProxyComposeTests(unittest.TestCase):
    def test_chunked_incomplete_response_reports_unknown_length(self) -> None:
        class Headers:
            @staticmethod
            def get_content_type() -> str:
                return "application/json"

        class IncompleteResponse:
            headers = Headers()

            def __enter__(self) -> "IncompleteResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            @staticmethod
            def read() -> bytes:
                raise http.client.IncompleteRead(b"partial", expected=None)

        with patch(
            "scripts.tinfoil_response_integrity_smoke.urllib.request.urlopen",
            return_value=IncompleteResponse(),
        ):
            with self.assertRaisesRegex(SmokeFailure, "chunked transfer, length unknown"):
                request_completion(
                    api_base="http://proxy.test/v1",
                    api_key="test-key",
                    model="test-model",
                    timeout=1,
                )

    def test_default_compose_uses_supported_standalone_proxy(self) -> None:
        env = {
            **os.environ,
            "LLM_API_KEY": "compose-contract-placeholder",
            "SECRET_KEY": "compose-contract-placeholder",
        }

        result = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                "docker-compose.infra.yml",
                "config",
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "image: ghcr.io/tinfoilsh/tinfoil-proxy:0.1.6",
            result.stdout,
        )
        self.assertIn("- --allowed-host", result.stdout)
        self.assertIn("- tinfoil-proxy", result.stdout)
        self.assertNotIn("- proxy\n", result.stdout)


class TinfoilResponseIntegritySmokeTests(unittest.TestCase):
    def test_complete_non_streaming_response_passes(self) -> None:
        body = json.dumps(
            {
                "id": "completion-smoke",
                "model": "smoke-model",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
            }
        ).encode()

        with completion_server(body, declared_length=len(body)) as (
            api_base,
            requests,
        ):
            result = run_integrity_smoke(api_base)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("non-streaming response integrity verified", result.stdout)
        self.assertEqual(requests[0]["reasoning_effort"], "low")
        self.assertEqual(requests[0]["stream"], False)

    def test_wrong_model_or_unfinished_answer_cannot_pass(self) -> None:
        for model, finish, content in (("glm-5-2", "stop", "ok"), (None, "stop", "ok"),
                                       ("smoke-model", "length", "partial"), ("smoke-model", "stop", "")):
            with self.subTest(model=model, finish=finish, content=content):
                body = json.dumps({"model": model, "choices": [{"finish_reason": finish,
                    "message": {"role": "assistant", "content": content}}]}).encode()
                with completion_server(body, declared_length=len(body)) as (api_base, _):
                    result = run_integrity_smoke(api_base)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("[PASS]", result.stdout)

    def test_truncated_non_streaming_response_fails(self) -> None:
        body = json.dumps(
            {
                "id": "completion-smoke",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
            }
        ).encode()

        with completion_server(body, declared_length=len(body) + 20) as (
            api_base,
            requests,
        ):
            result = run_integrity_smoke(api_base)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("incomplete response body", result.stderr.lower())
        self.assertEqual(requests[0]["stream"], False)


class OperatorSmokeFlowTests(unittest.TestCase):
    def test_reset_runs_the_response_integrity_smoke(self) -> None:
        result = subprocess.run(
            [
                "bash",
                "scripts/reset_local_instance.sh",
                "--dry-run",
                "--no-build",
            ],
            cwd=REPO_ROOT,
            env={
                **os.environ,
                "LLM_API_KEY": "compose-contract-placeholder",
                "SECRET_KEY": "compose-contract-placeholder",
            },
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("scripts/smoke_test.sh", result.stdout)


if __name__ == "__main__":
    unittest.main()
