"""Public lifecycle tests for the supported natural corpus entry point."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import run_benchmark
from scripts.benches import run_natural_corpus


class FakeEnvironment:
    def __init__(self, *, cleanup_error: Exception | None = None):
        self.cleanup_error = cleanup_error
        self.cleanup_calls = 0
        self.tools = None

    def run_backend_python(self, script, timeout=120):
        return "{}\n"

    def user_token(self, tools=()):
        self.tools = tools
        return "temporary-benchmark-token"

    def seed_knowledge(self):
        return {
            "job_ids": ["conversation-bench-job"],
            "sources": ["synthetic.md"],
            "chunk_id": "conversation-bench-job_chunk_0000",
            "source_text": "Synthetic fact CEDAR-27.",
            "fixture_version": "post-release-v2",
        }

    def seed_resources(self):
        return {
            "resources": [
                {
                    "resource_id": "conversation-bench-resource",
                    "name": "Synthetic resource",
                    "kind": "organization",
                    "description": "Synthetic benchmark resource.",
                    "languages": ["en"],
                    "pointers": [],
                    "regions": [],
                    "tags": ["synthetic"],
                    "status": "active",
                    "missing_fields": [],
                    "provenance": {"source_note": "Synthetic benchmark fixture."},
                }
            ]
        }

    def cleanup_scenario(self):
        self.cleanup_calls += 1
        if self.cleanup_error:
            raise self.cleanup_error


class NaturalCorpusLifecycleTests(unittest.TestCase):
    def test_setup_failure_writes_planned_turns_and_still_cleans_up(self):
        environment = FakeEnvironment()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "setup-failed.json"
            with patch.object(run_benchmark, "verify_synthetic_environment", return_value={
                "eligible": False,
                "counts": {"users": 1, "resources": 0, "documents": 0},
                "unknown_counts": {"users": 1, "resources": 0, "documents": 0},
            }), patch.object(run_benchmark, "verify_http_target") as target:
                runner = Mock()
                code = run_benchmark.run_isolated(
                    ["4", "--api-base", "http://127.0.0.1:18000", "--output", str(output)],
                    environment_factory=lambda: environment,
                    runner=runner,
                )
            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(code, 1)
        self.assertEqual(environment.cleanup_calls, 1)
        target.assert_not_called()
        runner.assert_not_called()
        self.assertEqual(report["measurements"]["execution"]["expected_turns"], 5)
        self.assertEqual(report["measurements"]["execution"]["completed_turns"], 0)
        self.assertEqual(report["measurements"]["execution"]["attempted_turns"], 0)
        self.assertEqual(report["summary"]["journeys"], 1)
        turns = report["candidates"][0]["scenarios"][0]["turns"]
        self.assertTrue(all(not turn["completed"] for turn in turns))
        self.assertTrue(all(turn["timing"]["done_ms"] == 0 for turn in turns))
        self.assertEqual(report["lifecycle"]["status"], "failed")

    def test_runner_gets_seed_manifest_and_cleanup_failure_is_canonical(self):
        environment = FakeEnvironment(cleanup_error=RuntimeError("cleanup SECRET"))
        captured = {}

        def fake_runner(runner_args, *, token_override, fixture_manifest_override):
            captured["args"] = list(runner_args)
            captured["token"] = token_override
            captured["manifest"] = fixture_manifest_override
            output = Path(runner_args[runner_args.index("--output") + 1])
            report = run_benchmark.build_report(
                [],
                corpus={"schema_version": "test", "version": "test", "sessions": {}},
                metadata={"model_expected": "glm-5-3-flash"},
                fixture_manifest=captured["manifest"],
            )
            output.write_text(json.dumps(report), encoding="utf-8")
            return 0

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BENCHMARK_AUTH_TOKEN", None)
            output = Path(directory) / "natural-final.json"
            with patch.object(run_benchmark, "verify_synthetic_environment", return_value={
                "eligible": True,
                "counts": {"users": 0, "resources": 0, "documents": 0},
                "unknown_counts": {"users": 0, "resources": 0, "documents": 0},
            }), patch.object(run_benchmark, "verify_http_target", return_value=True):
                code = run_benchmark.run_isolated(
                    ["--api-base", "http://127.0.0.1:18000", "--output", str(output)],
                    environment_factory=lambda: environment,
                    runner=fake_runner,
                )
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(code, 1)
            self.assertEqual(environment.cleanup_calls, 1)
            self.assertNotIn("BENCHMARK_AUTH_TOKEN", os.environ)
            self.assertEqual(captured["token"], "temporary-benchmark-token")
            self.assertEqual(captured["manifest"]["knowledge"][0]["source_text"], "Synthetic fact CEDAR-27.")
            self.assertEqual(captured["manifest"]["resources"][0]["resource_id"], "conversation-bench-resource")
            serialized = json.dumps(report)
            self.assertEqual(report["lifecycle"]["status"], "failed")
            self.assertEqual(report["harness_errors"][0]["kind"], "cleanup")
            self.assertEqual(report["measurements"]["release_gate"], "blocked")
            self.assertNotIn("SECRET", serialized)
            self.assertNotIn("temporary-benchmark-token", serialized)

    def test_compatibility_module_exports_the_consolidated_runner(self):
        self.assertIs(run_natural_corpus.run_isolated, run_benchmark.run_isolated)


if __name__ == "__main__":
    unittest.main()
