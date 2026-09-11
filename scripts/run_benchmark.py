#!/usr/bin/env python3
"""Run the versioned natural conversation benchmark.

The runner measures transport and conversation continuity. It deliberately
does not call an external judge: semantic quality stays ``unreviewed`` until
an approved reviewer records a review against the captured full turns.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx

# ``python scripts/run_benchmark.py`` sets ``sys.path`` to ``scripts/``.
# Bootstrap the repository root before importing the shared measurement module
# so the documented direct command works from any working directory.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.benches.quality_measurements import measure_artifact
from scripts.benches.synthetic_environment import (
    is_empty_synthetic_environment,
    validate_loopback_api_base,
    verify_http_target,
    verify_synthetic_environment,
)


SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
CONFIG_PATH = SCRIPT_DIR / "benchmark_config.json"
CORPUS_PATH = SCRIPT_DIR / "benchmark_sessions.json"
DEFAULT_TOOLS = ["knowledge-search", "curated-resources"]
SYNTHETIC_FIXTURE_SCHEMA = "synthetic-benchmark-fixture-manifest/v1"


class BenchmarkClient(Protocol):
    def chat(self, token: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]: ...

    def delete_session(self, token: str, session_id: str, timeout: float) -> None: ...


class HttpBenchmarkClient:
    """Small HTTP boundary kept injectable so the runner can be replay-tested."""

    def __init__(self, api_base: str):
        self.api_base = api_base.rstrip("/")

    def chat(self, token: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        response = httpx.post(
            f"{self.api_base}/llm/chat",
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )
        if response.status_code != 200:
            raise RuntimeError(f"chat failed with HTTP {response.status_code}")
        body = response.json()
        if not isinstance(body, dict):
            raise RuntimeError("chat returned a non-object JSON response")
        return body

    def delete_session(self, token: str, session_id: str, timeout: float) -> None:
        response = httpx.delete(
            f"{self.api_base}/query/session/{session_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )
        if response.status_code != 200:
            raise RuntimeError(f"session cleanup failed with HTTP {response.status_code}")
        body = response.json()
        if body.get("status") != "deleted":
            raise RuntimeError("session cleanup returned an unexpected status")


def load_dotenv() -> None:
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


def load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {
        "schema_version": "natural-conversation-benchmark-config/v2",
        "backend_url": "http://localhost:18000",
        "timeout_seconds": 120,
        "model": None,
    }


def load_corpus() -> dict[str, Any]:
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    if not isinstance(corpus.get("sessions"), dict) or not corpus["sessions"]:
        raise ValueError("benchmark corpus has no sessions")
    return corpus


def load_fixture_manifest(path: Path | None) -> dict[str, Any] | None:
    """Load and validate an explicitly supplied synthetic fixture manifest."""
    if path is None:
        return None
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SYNTHETIC_FIXTURE_SCHEMA:
        raise ValueError(f"fixture manifest must use schema {SYNTHETIC_FIXTURE_SCHEMA}")
    fixture_version = manifest.get("fixture_version")
    if not isinstance(fixture_version, str) or not fixture_version.strip():
        raise ValueError("fixture manifest must declare a fixture_version")
    knowledge_entries = manifest.get("knowledge")
    resource_entries = manifest.get("resources")
    if not isinstance(knowledge_entries, list) or not isinstance(resource_entries, list):
        raise ValueError("fixture manifest knowledge and resources must be lists")
    seen_ids: set[tuple[str, str]] = set()
    for collection, entries, prefixes, identifier in (("knowledge", knowledge_entries, ("conversation-bench-", "issue-539-"), "job_id"), ("resources", resource_entries, ("conversation-bench-", "issue-539-"), "resource_id")):
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"fixture manifest must contain a non-empty {collection} list")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get(identifier), str) or not entry[identifier].startswith(prefixes):
                raise ValueError(f"{collection} entries must use synthetic {identifier} markers")
            marker = (collection, entry[identifier])
            if marker in seen_ids:
                raise ValueError(f"fixture manifest contains duplicate {collection} {identifier}: {entry[identifier]}")
            seen_ids.add(marker)
            if collection == "knowledge" and (not isinstance(entry.get("chunk_id"), str) or not isinstance(entry.get("source_file"), str) or not isinstance(entry.get("source_text"), str)):
                raise ValueError("knowledge entries must include chunk_id, source_file, and source_text")
    return manifest


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _manifest_knowledge_fingerprint(entry: dict[str, Any]) -> str:
    return canonical_hash(
        {
            "job_id": entry["job_id"],
            "source_file": entry["source_file"],
            "chunk_id": entry["chunk_id"],
            "source_text": entry["source_text"],
        }
    )


def _manifest_resource_fingerprint(entry: dict[str, Any]) -> str:
    fields = ("resource_id", "name", "kind", "description", "languages", "pointers", "regions", "tags", "status", "missing_fields", "provenance")
    return canonical_hash({field: entry.get(field) for field in fields})


def verify_fixture_manifest(environment: Any, manifest: dict[str, Any]) -> dict[str, Any]:
    """Compare manifest hashes with the current local DB without returning rows."""
    knowledge = manifest["knowledge"]
    resources = manifest["resources"]
    expected_jobs = {
        entry["job_id"]: {
            "source_file": entry["source_file"],
            "chunk_id": entry["chunk_id"],
            "fingerprint": _manifest_knowledge_fingerprint(entry),
        }
        for entry in knowledge
    }
    expected_resources = {
        entry["resource_id"]: _manifest_resource_fingerprint(entry) for entry in resources
    }
    script = f"""
import database, hashlib, ingest_db, json
database.init_schema()
ingest_db.init_ingest_schema()
expected_jobs = {json.dumps(expected_jobs, ensure_ascii=False)}
expected_resources = {json.dumps(expected_resources, ensure_ascii=False)}

def fingerprint(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

resource_rows = database.list_resources()
resource_ids = {{str(row.get("resource_id")) for row in resource_rows}}
with database.get_cursor() as cursor:
    job_total = int(cursor.execute("SELECT count(*) FROM ingest_jobs").fetchone()[0])
matched_jobs = 0
for job_id, expected in expected_jobs.items():
    job = ingest_db.get_job(job_id)
    chunk = ingest_db.get_retrieval_chunk(expected["chunk_id"])
    actual = {{
        "job_id": job.get("job_id") if job else None,
        "source_file": chunk.get("source_file") if chunk else (job.get("filename") if job else None),
        "chunk_id": chunk.get("chunk_id") if chunk else None,
        "source_text": chunk.get("text") if chunk else None,
    }}
    if fingerprint(actual) == expected["fingerprint"]:
        matched_jobs += 1
matched_resources = 0
fields = ("resource_id", "name", "kind", "description", "languages", "pointers", "regions", "tags", "status", "missing_fields", "provenance")
for resource_id, expected in expected_resources.items():
    resource = database.get_resource(resource_id)
    actual = {{field: resource.get(field) if resource else None for field in fields}}
    if fingerprint(actual) == expected:
        matched_resources += 1
print(json.dumps({{
    "eligible": matched_jobs == len(expected_jobs) and matched_resources == len(expected_resources) and job_total == len(expected_jobs) and resource_ids == set(expected_resources),
    "matched": {{"knowledge": matched_jobs, "resources": matched_resources}},
    "expected": {{"knowledge": len(expected_jobs), "resources": len(expected_resources)}},
    "ambient_counts": {{"documents": max(job_total - len(expected_jobs), 0), "resources": len(resource_ids - set(expected_resources))}},
}}))
"""
    try:
        lines = environment.run_backend_python(script, timeout=30).strip().splitlines()
        result = json.loads(lines[-1]) if lines else {}
        return result if isinstance(result, dict) else {"eligible": False}
    except Exception:
        return {"eligible": False, "matched": {}, "expected": {}, "ambient_counts": {}}


def get_git_info() -> dict[str, Any]:
    result: dict[str, Any] = {"commit": None, "branch": None, "dirty": None}
    try:
        result["commit"] = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
        result["branch"] = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
        result["dirty"] = bool(
            subprocess.run(["git", "status", "--porcelain"], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        pass
    return result


def backend_metadata(api_base: str, timeout: float) -> dict[str, Any]:
    try:
        response = httpx.get(
            f"{api_base.rstrip('/')}/llm/test", timeout=timeout, follow_redirects=False, trust_env=False
        )
        if response.status_code == 200 and isinstance(response.json(), dict):
            body = response.json()
            return {"provider": body.get("provider"), "model": body.get("model"), "health": body.get("health")}
    except (httpx.HTTPError, ValueError):
        pass
    return {"status": "unavailable"}


def _error(kind: str, message: str, *, turn: int | None = None) -> dict[str, Any]:
    result = {"kind": kind, "message": message}
    if turn is not None:
        result["turn"] = turn
    return result


def _tool_evidence(response: dict[str, Any]) -> list[dict[str, Any]]:
    def sanitize_tool(tool: Any) -> dict[str, Any]:
        if not isinstance(tool, dict):
            return {}
        allowed = ("tool_id", "tool_name", "id", "name", "query", "input_summary", "status", "execution", "warnings", "guarded")
        return {key: tool[key] for key in allowed if key in tool and key != "metadata"}

    tools = response.get("tools_used")
    if isinstance(tools, list) and tools:
        return [sanitized for tool in tools if (sanitized := sanitize_tool(tool))]
    trace = response.get("trace")
    if isinstance(trace, dict) and isinstance(trace.get("tools"), list):
        return [sanitized for tool in trace["tools"] if (sanitized := sanitize_tool(tool))]
    return []


def _sanitize_retrieval(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    allowed = ("source_type", "title", "score")
    return {key: item[key] for key in allowed if key in item}


def _response_evidence(response: dict[str, Any], *, answer: str, model: str, returned_session: Any) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "answer": answer,
        "model": model,
        "provider": response.get("provider"),
        "session_id": returned_session,
        "tools_used": _tool_evidence(response),
    }
    trace = response.get("trace")
    if isinstance(trace, dict):
        evidence["trace"] = {
            "visibility": trace.get("visibility"),
            "suppressed": trace.get("suppressed"),
            "tools": _tool_evidence({"trace": trace}),
            "retrieval": [sanitized for item in trace.get("retrieval", []) if (sanitized := _sanitize_retrieval(item))],
        }
    return evidence


def run_session(
    session_key: str,
    session_data: dict[str, Any],
    *,
    client: BenchmarkClient | None,
    token: str,
    timeout: float = 120.0,
    session_id: str | None = None,
    expected_model: str | None = None,
    tools: list[str] | None = None,
    fixture_version: str | None = None,
) -> dict[str, Any]:
    """Run one journey and return a complete, reviewable evidence record."""

    requested_session_id = session_id or str(uuid.uuid4())
    selected_tools = list(DEFAULT_TOOLS if tools is None else tools)
    turns: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    history: list[dict[str, str]] = []
    observed_model: str | None = None
    started = time.perf_counter()

    try:
        for turn_data in session_data.get("turns", []):
            turn_number = int(turn_data["turn"])
            message = str(turn_data["user_message"])
            payload = {"message": message, "session_id": requested_session_id, "tools": selected_tools}
            turn_record: dict[str, Any] = {
                "turn": turn_number,
                "request": payload,
                "rubric": turn_data.get("rubric", {}),
                "coverage": turn_data.get("coverage", {}),
                "history": [dict(item) for item in history],
            }
            turn_started = time.perf_counter()
            if client is None:
                turn_record.update({"status": "not_run", "answer": "", "tool_evidence": [], "elapsed_seconds": 0})
                turns.append(turn_record)
                continue
            turn_record["started_at"] = datetime.now(timezone.utc).isoformat()
            try:
                response = client.chat(token, payload, timeout)
                if not isinstance(response, dict):
                    raise RuntimeError("chat response was not an object")
                answer = str(response.get("message") or "")
                returned_session = response.get("session_id")
                model = str(response.get("model") or "")
                turn_record["response"] = _response_evidence(
                    response, answer=answer, model=model, returned_session=returned_session
                )
                turn_record["answer"] = answer
                turn_record["tool_evidence"] = _tool_evidence(response)
                turn_record["elapsed_seconds"] = round(time.perf_counter() - turn_started, 4)
                turn_errors_before = len(errors)
                if not returned_session or str(returned_session) != requested_session_id:
                    errors.append(_error("session_mismatch", f"expected session {requested_session_id}, got {returned_session!r}", turn=turn_number))
                if not model:
                    errors.append(_error("model_missing", "response did not identify a model", turn=turn_number))
                elif expected_model and model != expected_model:
                    errors.append(_error("model_mismatch", f"expected model {expected_model}, got {model}", turn=turn_number))
                elif observed_model and model != observed_model:
                    errors.append(_error("model_mismatch", f"model changed from {observed_model} to {model}", turn=turn_number))
                else:
                    observed_model = observed_model or model
                if not answer.strip():
                    errors.append(_error("empty_answer", "response contained no answer", turn=turn_number))
                history.append({"user": message, "assistant": answer})
                turn_record["status"] = "completed" if len(errors) == turn_errors_before else "invalid"
            except Exception as exc:
                turn_record.update({"status": "error", "error": str(exc), "elapsed_seconds": round(time.perf_counter() - turn_started, 4)})
                errors.append(_error("provider_error", str(exc), turn=turn_number))
            turns.append(turn_record)
    finally:
        if client is not None:
            try:
                client.delete_session(token, requested_session_id, timeout)
            except Exception as exc:
                errors.append(_error("cleanup_error", str(exc)))

    if client is None:
        errors.append(_error("not_run", "lifecycle setup failed"))

    expected_turns = len(session_data.get("turns", []))
    completed_turns = sum(turn.get("status") == "completed" for turn in turns)
    completed = bool(expected_turns and len(turns) == expected_turns and completed_turns == expected_turns and not errors)
    return {
        "session_key": session_key,
        "session_id": requested_session_id,
        "scenario": {
            "id": session_data.get("id", session_key),
            "name": session_data.get("name", session_key),
            "scenario_type": session_data.get("scenario_type", "natural"),
            "review_status": session_data.get("review_status", "expert_review_pending"),
            "scenario_hash": canonical_hash(session_data),
            "fixture_version": fixture_version,
        },
        "requested_tools": selected_tools,
        "model": observed_model,
        "turns": turns,
        "errors": errors,
        "counts": {"expected_turns": expected_turns, "completed_turns": completed_turns},
        "completed": completed,
        "semantic_outcome": {"status": "unreviewed", "reason": "No approved semantic reviewer was configured; transport completion is not a quality result."},
        "elapsed_seconds": round(time.perf_counter() - started, 4),
    }


def _measurement_artifact(
    runs: list[dict[str, Any]], *, metadata: dict[str, Any], fixture_manifest: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Adapt legacy journey evidence to the shared measurement schema."""
    scenarios: list[dict[str, Any]] = []
    for run in runs:
        scenario_id = str(run["scenario"].get("id", run["session_key"]))
        scenarios.append(
            {
                "id": scenario_id,
                "scenario_run_id": f"{scenario_id}#{run.get('repeat', 1)}",
                "repetition": run.get("repeat", 1),
                "expected_turn_count": run["counts"]["expected_turns"],
                "summary": {"status": "passed" if run["completed"] else "failed"},
                "rubric": {"review_status": run["scenario"].get("review_status"), "criteria_scope": "current_turn_only"},
                "fixtures": {"version": run["scenario"].get("fixture_version"), "manifest": fixture_manifest},
                "catalog_hash": metadata.get("catalog_hash"),
                "scenario_hash": run["scenario"].get("scenario_hash"),
                "fixture_version": run["scenario"].get("fixture_version"),
                "turns": [
                    {
                        **({"attempted": False} if turn.get("status") == "not_run" else {}),
                        "completed": turn.get("status") == "completed",
                        "rubric": turn.get("rubric", {}),
                        "request": {"message": turn.get("request", {}).get("message", "")},
                        "response": {
                            "answer": turn.get("answer", ""),
                            "model": (turn.get("response") or {}).get("model", run.get("model")),
                            "session_id": (turn.get("response") or {}).get("session_id", run.get("session_id")),
                        },
                        "timing": {"done_ms": round(float(turn.get("elapsed_seconds", 0)) * 1000, 3)},
                        "tool_evidence": turn.get("tool_evidence", []),
                        "full_evidence": turn,
                    }
                    for turn in run["turns"]
                ],
            }
        )
    return {
        "run": metadata,
        "candidates": [
            {
                "model": metadata.get("model_expected") or (runs[0].get("model") if runs else None),
                "runtime_config": {"tools": metadata.get("tools", [])},
                "scenarios": scenarios,
            }
        ],
    }


def build_report(
    runs: list[dict[str, Any]],
    *,
    corpus: dict[str, Any],
    metadata: dict[str, Any],
    fixture_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    catalog_hash = canonical_hash(corpus)
    run_metadata = {
        **metadata,
        "catalog_hash": catalog_hash,
        "fixture_version": (fixture_manifest or {}).get("fixture_version") or corpus.get("version"),
        "fixture_manifest_hash": canonical_hash(fixture_manifest) if fixture_manifest is not None else None,
        "config_hash": metadata.get("config_hash") or canonical_hash(load_config()),
        "runner_hash": canonical_hash(Path(__file__).read_text(encoding="utf-8")),
    }
    artifact = _measurement_artifact(runs, metadata=run_metadata, fixture_manifest=fixture_manifest)
    measurements = measure_artifact(artifact)
    return {
        "schema_version": "natural-conversation-benchmark-report/v3",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "metadata": run_metadata,
        "run": artifact["run"],
        "candidates": artifact["candidates"],
        "corpus": {"schema_version": corpus.get("schema_version"), "version": corpus.get("version"), "hash": catalog_hash, "catalog_hash": catalog_hash, "review": corpus.get("review", {"status": "expert_review_pending"})},
        "fixture_manifest": fixture_manifest,
        "measurements": measurements,
        "summary": {"journeys": len(runs), "complete_journeys": sum(run.get("completed", False) for run in runs), "transport_errors": sum(len(run.get("errors", [])) for run in runs), "semantic_quality": {"status": "unreviewed", "reviewed_journeys": 0}, "measurements": measurements},
    }


def configured_tools(corpus: dict[str, Any]) -> list[str]:
    measurement = corpus.get("measurement", {})
    tools = measurement["explicit_tools"] if "explicit_tools" in measurement else DEFAULT_TOOLS
    return [str(tool) for tool in tools] if isinstance(tools, list) else list(DEFAULT_TOOLS)


def selected_session_ids(requested: list[str], sessions: dict[str, Any]) -> list[str]:
    unknown = [key for key in requested if key not in sessions]
    if unknown:
        raise ValueError(f"Unknown session(s): {', '.join(unknown)}. Use --list.")
    if len(requested) != len(set(requested)):
        raise ValueError("Duplicate session IDs are not allowed; use --repeat to repeat a journey.")
    return requested or list(sessions)


def positive_finite_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a finite positive number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise argparse.ArgumentTypeError("timeout must be a finite positive number")
    return timeout


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    config = load_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_ids", nargs="*", help="session IDs to run; defaults to all")
    parser.add_argument("--list", action="store_true", help="list sessions and exit")
    parser.add_argument("--output", type=Path, help="write the JSON report to this path")
    parser.add_argument("--api-base", default=os.getenv("BACKEND_URL", config.get("backend_url", "http://localhost:18000")))
    parser.add_argument("--repeat", type=int, default=1, help="repeat every selected journey")
    parser.add_argument("--token-env", default="BENCHMARK_AUTH_TOKEN", help="environment variable containing the signed token")
    parser.add_argument("--timeout", type=positive_finite_timeout, default=positive_finite_timeout(str(config.get("timeout_seconds", 120))))
    parser.add_argument("--model", default=os.getenv("TINFOIL_MODEL") or config.get("model"), help="expected model identity")
    parser.add_argument("--fixture-manifest", type=Path, help="validated synthetic knowledge/resource manifest for grounding review")
    return parser.parse_args(argv)


def default_synthetic_preflight(
    api_base: str, token: str, fixture_manifest: dict[str, Any] | None
) -> dict[str, Any]:
    """Run the DB preflight through the existing local Compose adapter."""
    from scripts.benches.conversation_model_bench import LocalComposeEnvironment

    environment = LocalComposeEnvironment()
    result = verify_synthetic_environment(environment, token=token)
    if not result.get("eligible"):
        return result
    manifest_result = {"eligible": True, "skipped": True}
    if fixture_manifest is not None:
        manifest_result = verify_fixture_manifest(environment, fixture_manifest)
        if not manifest_result.get("eligible"):
            return {**result, "eligible": False, "fixture_manifest": manifest_result}
    target_verified = verify_http_target(environment, api_base)
    return {
        **result,
        "fixture_manifest": manifest_result,
        "http_target_verified": target_verified,
        "eligible": bool(target_verified),
    }


def main(
    argv: list[str] | None = None,
    *,
    client: BenchmarkClient | None = None,
    preflight: Callable[[str, str], dict[str, Any]] | None = None,
    token_override: str | None = None,
    fixture_manifest_override: dict[str, Any] | None = None,
) -> int:
    load_dotenv()
    args = parse_args(argv)
    try:
        args.api_base = validate_loopback_api_base(args.api_base)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    corpus = load_corpus()
    try:
        fixture_manifest = fixture_manifest_override if fixture_manifest_override is not None else load_fixture_manifest(args.fixture_manifest)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"invalid fixture manifest: {exc}", file=sys.stderr)
        return 2
    sessions = corpus["sessions"]
    if args.list:
        for key, session in sessions.items():
            print(f"{key}: {session.get('name', key)}")
        return 0
    if args.repeat < 1:
        print("--repeat must be at least 1", file=sys.stderr)
        return 2
    try:
        selected = selected_session_ids(args.session_ids, sessions)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    token = token_override or os.getenv(args.token_env)
    if not token:
        print(f"{args.token_env} must contain a signed user token", file=sys.stderr)
        return 2
    if client is None and configured_tools(corpus) and fixture_manifest is None:
        print("--fixture-manifest is required when knowledge/resource tools are enabled", file=sys.stderr)
        return 2
    if preflight is not None or client is None:
        if preflight is None:
            result = default_synthetic_preflight(args.api_base, token, fixture_manifest)
        else:
            result = preflight(args.api_base, token)
        if not result.get("eligible"):
            counts = result.get("unknown_counts") if isinstance(result, dict) else None
            print(f"synthetic benchmark preflight rejected this instance (unknown row counts: {counts})", file=sys.stderr)
            return 2
    http_client = client or HttpBenchmarkClient(args.api_base)
    runs: list[dict[str, Any]] = []
    for repeat_index in range(args.repeat):
        for key in selected:
            run = run_session(key, sessions[key], client=http_client, token=token, timeout=args.timeout, expected_model=args.model, tools=configured_tools(corpus), fixture_version=(fixture_manifest or {}).get("fixture_version") or corpus.get("version"))
            run["repeat"] = repeat_index + 1
            runs.append(run)
            state = "complete" if run["completed"] else "incomplete"
            print(f"{key} repeat {repeat_index + 1}: {state} ({run['counts']['completed_turns']}/{run['counts']['expected_turns']} turns)")
    metadata = {"api_base": args.api_base, "model_expected": args.model, "tools": configured_tools(corpus), "repeat": args.repeat, "timeout_seconds": args.timeout, "config_hash": canonical_hash(load_config()), "git": get_git_info(), "backend": backend_metadata(args.api_base, min(args.timeout, 10.0))}
    report = build_report(runs, corpus=corpus, metadata=metadata, fixture_manifest=fixture_manifest)
    output = args.output or SCRIPT_DIR / "evals" / f"natural_benchmark_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"saved: {output}")
    execution_status = report["measurements"].get("execution", {}).get("status")
    contract_status = report["measurements"].get("contracts", {}).get("status")
    return 0 if execution_status == "passed" and contract_status == "passed" else 1


def _seed_manifest(environment: Any, tools: tuple[str, ...]) -> dict[str, Any] | None:
    """Create the canonical manifest directly from the local fixture owner."""
    knowledge_fixture = environment.seed_knowledge() if "knowledge-search" in tools else None
    resource_fixture = environment.seed_resources() if "curated-resources" in tools else None
    if knowledge_fixture is None and resource_fixture is None:
        return None
    knowledge: list[dict[str, Any]] = []
    resources: list[dict[str, Any]] = []
    if knowledge_fixture is not None:
        job_ids = knowledge_fixture.get("job_ids", [])
        sources = knowledge_fixture.get("sources", [])
        if len(job_ids) != 1 or len(sources) != 1:
            raise ValueError("knowledge seed must create exactly one source")
        knowledge.append({
            "job_id": job_ids[0],
            "source_file": sources[0],
            "chunk_id": knowledge_fixture["chunk_id"],
            "source_text": knowledge_fixture["source_text"],
        })
    if resource_fixture is not None:
        resources = resource_fixture.get("resources", [])
        if not resources or not all(isinstance(row, dict) for row in resources):
            raise ValueError("resource seed did not return complete rows")
    return {
        "schema_version": SYNTHETIC_FIXTURE_SCHEMA,
        "fixture_version": (knowledge_fixture or {}).get("fixture_version", "post-release-v2"),
        "knowledge": knowledge,
        "resources": resources,
    }


def _not_run_report(
    args: argparse.Namespace,
    manifest: dict[str, Any] | None,
    corpus: dict[str, Any],
    selected: list[str],
) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    for repetition in range(1, args.repeat + 1):
        for key in selected:
            session = corpus["sessions"][key]
            run = run_session(key, session, client=None, token="", tools=configured_tools(corpus))
            run["session_id"] = None
            run["model"] = args.model
            run["repeat"] = repetition
            runs.append(run)
    return build_report(runs, corpus=corpus, metadata={
        "api_base": args.api_base, "model_expected": args.model,
        "repeat": args.repeat, "tools": configured_tools(corpus),
    }, fixture_manifest=manifest)


def run_isolated(
    argv: list[str] | None = None,
    *,
    environment_factory: Callable[[], Any] | None = None,
    runner: Callable[..., int] = main,
) -> int:
    """Own synthetic setup, exact-fixture execution, cleanup, and failure evidence."""
    args = parse_args(argv)
    if args.list:
        return main(argv)
    try:
        args.api_base = validate_loopback_api_base(args.api_base)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.repeat < 1 or args.output is None:
        print("isolated runs require --output and --repeat of at least 1", file=sys.stderr)
        return 2
    if args.fixture_manifest is not None:
        print("isolated runs create their own fixture manifest", file=sys.stderr)
        return 2
    if args.output.exists():
        print(f"refusing to overwrite existing output: {args.output}", file=sys.stderr)
        return 2

    if environment_factory is None:
        from scripts.benches.conversation_model_bench import LocalComposeEnvironment
        environment_factory = LocalComposeEnvironment
    environment = None
    manifest = None
    initial_preflight = None
    target_verified = None
    runner_exit_code = 1
    harness_errors: list[dict[str, str]] = []
    corpus = load_corpus()
    try:
        selected = selected_session_ids(args.session_ids, corpus["sessions"])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        environment = environment_factory()
        initial_preflight = verify_synthetic_environment(environment)
        if not is_empty_synthetic_environment(initial_preflight):
            raise RuntimeError("initial synthetic environment was not empty and eligible")
        target_verified = verify_http_target(environment, args.api_base)
        if not target_verified:
            raise RuntimeError("HTTP target did not bind to the verified local backend")
        tools = tuple(configured_tools(corpus))
        token = environment.user_token(tools)
        manifest = _seed_manifest(environment, tools)
        if tools and manifest is None:
            raise RuntimeError("configured source tools did not produce a fixture manifest")
        runner_exit_code = int(runner(argv, token_override=token, fixture_manifest_override=manifest))
    except Exception as exc:
        harness_errors.append({"kind": "lifecycle", "message": f"lifecycle failed ({type(exc).__name__})"})
    finally:
        if environment is not None:
            try:
                environment.cleanup_scenario()
            except Exception as exc:
                harness_errors.append({"kind": "cleanup", "message": f"cleanup failed ({type(exc).__name__})"})
        try:
            report = json.loads(args.output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report = _not_run_report(args, manifest, corpus, selected)
        report["lifecycle"] = {
            "status": "failed" if harness_errors or runner_exit_code else "passed",
            "initial_preflight": initial_preflight, "http_target_verified": target_verified,
            "runner_exit_code": runner_exit_code, "harness_errors": harness_errors,
        }
        if harness_errors:
            report["harness_errors"] = harness_errors
            report["run"]["harness_errors"] = harness_errors
        measurements = measure_artifact(report)
        if harness_errors:
            measurements["release_gate"] = "blocked"
        report["measurements"] = measurements
        report["summary"]["measurements"] = measurements
        report["summary"]["transport_errors"] += len(harness_errors)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 1 if harness_errors else runner_exit_code


if __name__ == "__main__":
    raise SystemExit(main())
