"""Contract tests for the versioned natural-conversation benchmark runner."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import run_benchmark
from scripts.benches.quality_measurements import build_review_packet, measure_artifact
from scripts.benches.synthetic_environment import validate_loopback_api_base, verify_synthetic_environment


class ScriptedClient:
    def __init__(self, responses=None, *, delete_error: Exception | None = None):
        self.responses = list(responses or [])
        self.delete_error = delete_error
        self.requests = []
        self.deleted = []

    def chat(self, token, payload, timeout):
        self.requests.append((token, payload, timeout))
        if not self.responses:
            raise RuntimeError("no scripted response")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if response.get("session_id") == "$requested":
            response = {**response, "session_id": payload["session_id"]}
        return response

    def delete_session(self, token, session_id, timeout):
        self.deleted.append((token, session_id, timeout))
        if self.delete_error:
            raise self.delete_error


def session_fixture(turns=2):
    return {
        "id": "fixture",
        "name": "Fixture journey",
        "scenario_type": "natural",
        "review_status": "expert_review_pending",
        "turns": [
            {
                "turn": index,
                "user_message": f"Question {index}",
                "coverage": {"natural": True, "contract": False},
                "rubric": {
                    "required": [{"id": "useful", "description": "Offers a useful next step."}],
                    "forbidden": [{"id": "fabrication", "description": "Does not invent facts."}],
                    "manual_review": ["grounding"],
                },
            }
            for index in range(1, turns + 1)
        ],
    }


class NaturalBenchmarkRunnerTests(unittest.TestCase):
    def test_complete_run_captures_full_context_tools_and_cleans_up(self):
        client = ScriptedClient(
            [
                {
                    "message": "A full first answer with details.",
                    "session_id": "requested",
                    "model": "glm-5-3-flash",
                    "provider": "sage",
                    "tools_used": [{"tool_id": "knowledge-search", "query": "first"}],
                    "trace": {"tools": ["knowledge-search"]},
                },
                {
                    "message": "A full second answer.",
                    "session_id": "requested",
                    "model": "glm-5-3-flash",
                    "provider": "sage",
                    "tools_used": [{"tool_id": "curated-resources", "query": "second"}],
                    "trace": {"tools": ["curated-resources"]},
                },
            ]
        )
        result = run_benchmark.run_session(
            "fixture",
            session_fixture(),
            client=client,
            token="token",
            timeout=4,
            session_id="requested",
        )

        self.assertTrue(result["completed"])
        self.assertEqual(result["semantic_outcome"]["status"], "unreviewed")
        self.assertEqual(len(result["turns"]), 2)
        self.assertEqual(result["turns"][0]["response"]["answer"], "A full first answer with details.")
        self.assertEqual(result["turns"][1]["history"][0]["assistant"], "A full first answer with details.")
        self.assertEqual(result["turns"][1]["tool_evidence"][0]["tool_id"], "curated-resources")
        self.assertEqual(client.requests[0][1]["tools"], ["knowledge-search", "curated-resources"])
        self.assertEqual(client.requests[1][1]["session_id"], "requested")
        self.assertEqual(client.deleted, [("token", "requested", 4)])

    def test_provider_trace_evidence_is_allowlisted(self):
        client = ScriptedClient(
            [
                {
                    "message": "answer",
                    "session_id": "requested",
                    "model": "glm-5-3-flash",
                    "trace": {
                        "visibility": "detailed",
                        "suppressed": False,
                        "reasoning": {"summary": "private reasoning"},
                        "tools": [{"name": "knowledge", "metadata": {"secret": "do-not-save"}, "status": "completed"}],
                        "retrieval": [{"source_type": "document", "title": "synthetic", "summary": "fixture", "metadata": {"secret": "do-not-save"}}],
                        "arbitrary": "do-not-save",
                    },
                }
            ]
        )
        result = run_benchmark.run_session("fixture", session_fixture(turns=1), client=client, token="token", session_id="requested")
        serialized = json.dumps(result)
        self.assertNotIn("do-not-save", serialized)
        self.assertEqual(result["turns"][0]["response"]["trace"]["retrieval"][0]["title"], "synthetic")

    def test_explicit_empty_tools_are_preserved(self):
        client = ScriptedClient([{"message": "answer", "session_id": "requested", "model": "glm-5-3-flash"}])
        run_benchmark.run_session(
            "fixture", session_fixture(turns=1), client=client, token="token", session_id="requested", tools=[]
        )
        self.assertEqual(client.requests[0][1]["tools"], [])

    def test_http_errors_do_not_copy_server_response_bodies(self):
        response = type("Response", (), {"status_code": 500, "text": "credential=SECRET"})()
        with patch.object(run_benchmark.httpx, "post", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "HTTP 500") as raised:
                run_benchmark.HttpBenchmarkClient("http://example.test").chat("token", {}, 1)
        self.assertNotIn("SECRET", str(raised.exception))

    def test_missing_session_id_is_an_incomplete_run(self):
        client = ScriptedClient([{"message": "answer", "model": "glm-5-3-flash"}])
        result = run_benchmark.run_session(
            "fixture", session_fixture(turns=1), client=client, token="token", session_id="requested"
        )
        self.assertFalse(result["completed"])
        self.assertTrue(any(error["kind"] == "session_mismatch" for error in result["errors"]))
        self.assertEqual(len(client.deleted), 1)

    def test_mismatched_session_and_model_are_recorded(self):
        client = ScriptedClient(
            [
                {"message": "one", "session_id": "other", "model": "glm-5-3-flash"},
                {"message": "two", "session_id": "requested", "model": "glm-5-2"},
            ]
        )
        result = run_benchmark.run_session(
            "fixture", session_fixture(), client=client, token="token", session_id="requested"
        )
        self.assertFalse(result["completed"])
        self.assertEqual({error["kind"] for error in result["errors"]}, {"session_mismatch", "model_mismatch"})

    def test_provider_error_and_cleanup_error_cannot_pass(self):
        client = ScriptedClient(
            [RuntimeError("provider unavailable")], delete_error=RuntimeError("cleanup unavailable")
        )
        result = run_benchmark.run_session(
            "fixture", session_fixture(turns=1), client=client, token="token", session_id="requested"
        )
        self.assertFalse(result["completed"])
        self.assertEqual(result["turns"][0]["status"], "error")
        self.assertTrue(any(error["kind"] == "provider_error" for error in result["errors"]))
        self.assertTrue(any(error["kind"] == "cleanup_error" for error in result["errors"]))

    def test_empty_answer_cannot_pass_as_a_completed_quality_run(self):
        client = ScriptedClient([{"message": "", "session_id": "requested", "model": "glm-5-3-flash"}])
        result = run_benchmark.run_session(
            "fixture", session_fixture(turns=1), client=client, token="token", session_id="requested"
        )
        self.assertFalse(result["completed"])
        self.assertTrue(any(error["kind"] == "empty_answer" for error in result["errors"]))

    def test_corpus_has_25_turns_and_no_prose_golden_answers(self):
        corpus = run_benchmark.load_corpus()
        self.assertEqual(sum(len(session["turns"]) for session in corpus["sessions"].values()), 25)
        self.assertEqual(corpus["review"]["status"], "expert_review_pending")
        for session in corpus["sessions"].values():
            for turn in session["turns"]:
                self.assertNotIn("expected_response", turn)
                self.assertIn("rubric", turn)
                self.assertIn("coverage", turn)

    def test_cli_list_is_read_only_and_positionals_remain_supported(self):
        self.assertEqual(run_benchmark.main(["--list"]), 0)

    def test_cli_runs_positional_journey_and_writes_report(self):
        fixture = session_fixture(turns=1)
        corpus = {
            "schema_version": "test",
            "version": "test-1",
            "review": {"status": "expert_review_pending"},
            "measurement": {"explicit_tools": ["knowledge-search", "curated-resources"]},
            "sessions": {"fixture": fixture},
        }
        client = ScriptedClient([{"message": "answer", "session_id": "$requested", "model": "glm-5-3-flash"}])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            with patch.object(run_benchmark, "load_corpus", return_value=corpus), patch.dict(os.environ, {"TEST_TOKEN": "token"}), patch.object(run_benchmark, "backend_metadata", return_value={}), patch.object(run_benchmark, "get_git_info", return_value={}):
                self.assertEqual(run_benchmark.main(["fixture", "--output", str(output), "--token-env", "TEST_TOKEN", "--api-base", "http://127.0.0.1:18000", "--timeout", "1"], client=client), 0)
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["summary"]["complete_journeys"], 1)
        self.assertEqual(client.requests[0][1]["message"], "Question 1")

    def test_cli_rejects_duplicate_sessions_and_nonfinite_timeout(self):
        corpus = {"sessions": {"fixture": session_fixture(turns=1)}, "measurement": {"explicit_tools": []}}
        with patch.object(run_benchmark, "load_corpus", return_value=corpus), patch.dict(os.environ, {"TEST_TOKEN": "token"}):
            self.assertEqual(run_benchmark.main(["fixture", "fixture", "--token-env", "TEST_TOKEN"]), 2)
            with self.assertRaises(SystemExit):
                run_benchmark.parse_args(["--timeout", "nan"])

    def test_cli_runs_preflight_before_injected_execution_and_rejects_remote_base(self):
        corpus = {"sessions": {"fixture": session_fixture(turns=1)}, "measurement": {"explicit_tools": []}}
        client = ScriptedClient([{"message": "answer", "session_id": "$requested", "model": "glm-5-3-flash"}])
        calls = []

        def rejected_preflight(api_base, token):
            calls.append((api_base, token))
            return {"eligible": False, "unknown_counts": {"users": 1}}

        with patch.object(run_benchmark, "load_corpus", return_value=corpus), patch.dict(os.environ, {"TEST_TOKEN": "token"}):
            self.assertEqual(run_benchmark.main(["fixture", "--token-env", "TEST_TOKEN"], client=client, preflight=rejected_preflight), 2)
            self.assertEqual(calls, [("http://localhost:18000", "token")])
            self.assertEqual(run_benchmark.main(["fixture", "--api-base", "https://example.test", "--token-env", "TEST_TOKEN"], client=client), 2)
        self.assertEqual(client.requests, [])

    def test_cli_exit_code_follows_canonical_contract_measurement(self):
        corpus = {
            "sessions": {"fixture": session_fixture(turns=2)},
            "measurement": {"explicit_tools": []},
        }
        client = ScriptedClient(
            [
                {"message": "one", "session_id": "$requested", "model": "glm-5-3-flash"},
                {"message": "two", "session_id": "$requested", "model": "glm-5-2"},
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(run_benchmark, "load_corpus", return_value=corpus), patch.dict(os.environ, {"TEST_TOKEN": "token"}), patch.object(run_benchmark, "backend_metadata", return_value={}), patch.object(run_benchmark, "get_git_info", return_value={}):
                code = run_benchmark.main(["fixture", "--output", str(Path(directory) / "drift.json"), "--token-env", "TEST_TOKEN", "--api-base", "http://127.0.0.1:18000"], client=client)
        self.assertEqual(code, 1)

    def test_synthetic_preflight_accepts_only_marker_rows_and_returns_counts(self):
        class Environment:
            def __init__(self):
                self.input_script = None
                self.input_data = None

            def run_backend_python(self, script, timeout=120):
                return '{"eligible": true, "token_synthetic": true, "counts": {"users": 1, "resources": 2, "documents": 1}, "unknown_counts": {"users": 0, "resources": 0, "documents": 0}}\n'

            def run_backend_python_input(self, script, *, input_data, timeout=120):
                self.input_script = script
                self.input_data = input_data
                return '{"eligible": true, "token_synthetic": true, "counts": {"users": 1, "resources": 2, "documents": 1}, "unknown_counts": {"users": 0, "resources": 0, "documents": 0}}\n'

        self.assertEqual(validate_loopback_api_base("http://localhost:18000/"), "http://localhost:18000")
        environment = Environment()
        result = verify_synthetic_environment(environment, token="signed-secret-token-123")
        self.assertTrue(result["eligible"])
        self.assertEqual(result["counts"]["resources"], 2)
        self.assertEqual(environment.input_data, "signed-secret-token-123")
        self.assertNotIn("signed-secret-token-123", environment.input_script)

    def test_standalone_script_imports_from_outside_repository_with_pythonpath_unset(self):
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        process = subprocess.run(
            [sys.executable, str(run_benchmark.PROJECT_ROOT / "scripts" / "run_benchmark.py"), "--list"],
            cwd=tempfile.gettempdir(),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("Natural consent regression", process.stdout)

    def test_fixture_manifest_requires_synthetic_ids_and_is_retained_for_review(self):
        manifest = {
            "schema_version": "synthetic-benchmark-fixture-manifest/v1",
            "fixture_version": "post-release-v2",
            "knowledge": [{"job_id": "conversation-bench-abc", "source_file": "synthetic.md", "chunk_id": "chunk-abc", "source_text": "Synthetic fact CEDAR-27."}],
            "resources": [{"resource_id": "conversation-bench-global-legal-abc", "name": "Bench Hotline", "pointers": [{"type": "email", "value": "bench@example.test"}]}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixtures.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            loaded = run_benchmark.load_fixture_manifest(path)
        self.assertEqual(loaded["fixture_version"], "post-release-v2")
        self.assertEqual(run_benchmark.canonical_hash(loaded), run_benchmark.canonical_hash(manifest))
        bad = {**manifest, "resources": [{"resource_id": "customer-data", "name": "unknown"}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ValueError):
                run_benchmark.load_fixture_manifest(path)
        duplicate = {**manifest, "knowledge": [manifest["knowledge"][0], manifest["knowledge"][0]]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.json"
            path.write_text(json.dumps(duplicate), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate knowledge"):
                run_benchmark.load_fixture_manifest(path)

    def test_manifest_is_retained_in_each_canonical_scenario(self):
        manifest = {
            "schema_version": "synthetic-benchmark-fixture-manifest/v1",
            "fixture_version": "fixture-1",
            "knowledge": [{"job_id": "conversation-bench-job", "source_file": "synthetic.md", "chunk_id": "chunk", "source_text": "Synthetic source."}],
            "resources": [{"resource_id": "conversation-bench-resource", "name": "Synthetic resource"}],
        }
        run = run_benchmark.run_session("fixture", session_fixture(turns=1), client=ScriptedClient([{"message": "answer", "session_id": "requested", "model": "glm-5-3-flash"}]), token="token", session_id="requested", fixture_version="fixture-1")
        report = run_benchmark.build_report([run], corpus={"schema_version": "test", "version": "catalog-1", "sessions": {"fixture": session_fixture(turns=1)}}, metadata={"model_expected": "glm-5-3-flash"}, fixture_manifest=manifest)
        scenario = report["candidates"][0]["scenarios"][0]
        self.assertEqual(scenario["fixtures"]["manifest"], manifest)
        self.assertEqual(report["run"]["fixture_manifest_hash"], run_benchmark.canonical_hash(manifest))

    def test_report_round_trip_keeps_catalog_hash_and_unreviewed_state(self):
        report = run_benchmark.build_report(
            [
                run_benchmark.run_session(
                    "fixture",
                    session_fixture(turns=1),
                    client=ScriptedClient(
                        [{"message": "answer", "session_id": "requested", "model": "glm-5-3-flash"}]
                    ),
                    token="token",
                    session_id="requested",
                )
            ],
            corpus={"schema_version": "test", "sessions": {"fixture": session_fixture(turns=1)}, "review": {"status": "expert_review_pending"}},
            metadata={"model": "glm-5-3-flash"},
        )
        self.assertTrue(report["corpus"]["hash"])
        self.assertEqual(report["schema_version"], "natural-conversation-benchmark-report/v3")
        self.assertNotIn("artifact", report)
        self.assertNotIn("runs", report)
        self.assertEqual(report["corpus"]["catalog_hash"], report["corpus"]["hash"])
        self.assertEqual(report["run"]["catalog_hash"], report["corpus"]["catalog_hash"])
        self.assertTrue(report["run"]["config_hash"])
        self.assertTrue(report["run"]["runner_hash"])
        self.assertEqual(report["candidates"][0]["scenarios"][0]["fixture_version"], None)
        self.assertEqual(report["summary"]["semantic_quality"]["status"], "unreviewed")
        self.assertEqual(report["measurements"]["semantic_review"]["status"], "unreviewed")
        self.assertEqual(report["candidates"][0]["scenarios"][0]["turns"][0]["response"]["answer"], "answer")
        packet = build_review_packet(report)
        self.assertEqual(packet["artifact_hash"], report["measurements"]["artifact_hash"])
        self.assertEqual(measure_artifact(report)["semantic_review"]["status"], "unreviewed")
        self.assertNotIn("turns", packet["entries"][0]["evidence"]["criteria"])
        self.assertEqual(packet["entries"][0]["evidence"]["turn"]["rubric"]["required"][0]["id"], "useful")
        json.dumps(report)


if __name__ == "__main__":
    unittest.main()
