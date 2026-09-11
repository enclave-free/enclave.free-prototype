"""Safety preflight helpers for local synthetic benchmark runs.

The preflight deliberately returns counts and booleans only. It never emits
user, resource, document, or token values from the local database.
"""

from __future__ import annotations

import json
from typing import Any, Protocol
from urllib.parse import urlsplit


SYNTHETIC_EMAIL_PREFIXES = ("conversation-bench-", "issue-539-contact-")
SYNTHETIC_NAME_PREFIXES = ("Conversation Bench User",)
SYNTHETIC_RESOURCE_PREFIXES = ("conversation-bench-", "issue-539-")
SYNTHETIC_JOB_PREFIXES = ("conversation-bench-", "issue-539-")


class BackendScriptRunner(Protocol):
    def run_backend_python(self, script: str, timeout: int = 120) -> str: ...
    def run_backend_python_input(self, script: str, *, input_data: str, timeout: int = 120) -> str: ...


def validate_loopback_api_base(value: str) -> str:
    """Accept only an HTTP(S) loopback origin without credentials or paths."""

    parsed = urlsplit(value)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("--api-base must be an HTTP(S) loopback origin")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("--api-base must not contain credentials")
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("--api-base is restricted to localhost, 127.0.0.1, or [::1]")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid --api-base port: {exc}") from exc
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("--api-base must be an origin without a path, query, or fragment")
    return f"{parsed.scheme.casefold()}://{parsed.netloc}".rstrip("/")


def _preflight_script(has_token: bool) -> str:
    return f"""
import auth
import database
import json
import sys

database.init_schema()

def prefixed(value, prefixes):
    return isinstance(value, str) and value.startswith(prefixes)

users = database.list_users()
resources = database.list_resources()
with database.get_cursor() as cursor:
    jobs = [dict(row) for row in cursor.execute("SELECT job_id, filename FROM ingest_jobs").fetchall()]

synthetic_users = 0
for user in users:
    if prefixed(user.get("email"), {SYNTHETIC_EMAIL_PREFIXES!r}) or prefixed(user.get("name"), {SYNTHETIC_NAME_PREFIXES!r}):
        synthetic_users += 1
synthetic_resources = sum(prefixed(item.get("resource_id"), {SYNTHETIC_RESOURCE_PREFIXES!r}) for item in resources)
synthetic_jobs = sum(prefixed(item.get("job_id"), {SYNTHETIC_JOB_PREFIXES!r}) or prefixed(item.get("filename"), {SYNTHETIC_JOB_PREFIXES!r}) for item in jobs)

token_synthetic = None
if {has_token!r}:
    token_data = auth.verify_session_token(sys.stdin.read())
    token_user = database.get_user(int(token_data["user_id"])) if token_data and token_data.get("user_id") is not None else None
    token_synthetic = bool(token_user and (prefixed(token_user.get("email"), {SYNTHETIC_EMAIL_PREFIXES!r}) or prefixed(token_user.get("name"), {SYNTHETIC_NAME_PREFIXES!r})))

unknown_users = len(users) - synthetic_users
unknown_resources = len(resources) - synthetic_resources
unknown_jobs = len(jobs) - synthetic_jobs
print(json.dumps({{
    "eligible": unknown_users == 0 and unknown_resources == 0 and unknown_jobs == 0 and (token_synthetic is not False),
    "token_synthetic": token_synthetic,
    "counts": {{"users": len(users), "resources": len(resources), "documents": len(jobs)}},
    "unknown_counts": {{"users": unknown_users, "resources": unknown_resources, "documents": unknown_jobs}},
}}))
"""


def verify_synthetic_environment(environment: BackendScriptRunner, *, token: str | None = None) -> dict[str, Any]:
    """Verify that the local DB is benchmark-only, without returning row data."""

    try:
        script = _preflight_script(token is not None)
        output = (environment.run_backend_python_input(script, input_data=token, timeout=30) if token is not None
                  else environment.run_backend_python(script, timeout=30))
        lines = output.strip().splitlines()
        payload = json.loads(lines[-1]) if lines else {}
        if not isinstance(payload, dict):
            raise ValueError("preflight did not return an object")
        return payload
    except Exception:
        return {
            "eligible": False,
            "token_synthetic": None,
            "counts": {"users": None, "resources": None, "documents": None},
            "unknown_counts": {"users": None, "resources": None, "documents": None},
        }


def is_empty_synthetic_environment(result: dict[str, Any]) -> bool:
    """A fresh cohort must start without rows left by earlier synthetic runs."""
    counts = result.get("counts")
    return (
        result.get("eligible") is True
        and isinstance(counts, dict)
        and all(type(counts.get(key)) is int and counts[key] == 0
                for key in ("users", "resources", "documents"))
    )


def verify_empty_synthetic_environment(environment: BackendScriptRunner) -> dict[str, Any]:
    result = verify_synthetic_environment(environment)
    return {**result, "eligible": is_empty_synthetic_environment(result)}


def verify_http_target(environment: BackendScriptRunner, api_base: str, *, get=None) -> bool:
    """Bind the HTTP origin to this backend using a disposable random identity.

    No model call is made. A different stack with the same session-signing key
    still cannot pass unless its database contains the exact fresh canary.
    """
    import uuid
    import httpx

    base = validate_loopback_api_base(api_base)
    email = f"conversation-bench-probe-{uuid.uuid4().hex}@example.test"
    canary = None
    matched = False
    cleanup_ok = False
    try:
        source = f'''
import auth, database, json
database.init_schema()
with database.get_write_cursor() as cursor:
    cursor.execute("INSERT INTO users (email, name, approved, created_at) VALUES (?, ?, 1, CURRENT_TIMESTAMP)", ({email!r}, "Conversation Bench User HTTP probe"))
    user_id = cursor.lastrowid
print(json.dumps({{"user_id": user_id, "token": auth.create_session_token(user_id, {email!r})}}))
'''
        canary = json.loads(environment.run_backend_python(source, timeout=30).strip().splitlines()[-1])
        request = get or httpx.get
        response = request(base + "/users/" + str(canary["user_id"]), headers={"Authorization": "Bearer " + canary["token"]},
                           timeout=10, follow_redirects=False, trust_env=False)
        if response.status_code == 200:
            payload = response.json()
            matched = payload.get("email") == email and payload.get("id") == canary["user_id"]
    except Exception:
        matched = False
    finally:
        # Cleanup by the random email also covers a lost/invalid creation response.
        cleanup = f'''
import database, json
with database.get_write_cursor() as cursor:
    cursor.execute("DELETE FROM users WHERE email = ?", ({email!r},))
    remaining = cursor.execute("SELECT count(*) FROM users WHERE email = ?", ({email!r},)).fetchone()[0]
print(json.dumps({{"removed": remaining == 0}}))
'''
        try:
            result = json.loads(environment.run_backend_python(cleanup, timeout=30).strip().splitlines()[-1])
            cleanup_ok = result.get("removed") is True
        except Exception:
            cleanup_ok = False
    return matched and cleanup_ok
