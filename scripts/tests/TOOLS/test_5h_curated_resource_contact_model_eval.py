#!/usr/bin/env python3
"""Executable, model-backed issue #539 evaluation (requires Compose + provider)."""
from __future__ import annotations
import argparse, hashlib, json, math, os, re, subprocess, time, unicodedata, uuid
from collections import namedtuple
from pathlib import Path
from typing import Any
import requests

from scripts.benches.synthetic_environment import (
    is_empty_synthetic_environment,
    validate_loopback_api_base,
    verify_http_target,
    verify_empty_synthetic_environment,
)

RUNNER_CODE_HASH = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
ROOT = Path(__file__).parents[3]
COMPOSE = ["docker", "compose"]
if os.environ.get("ENCLAVE_EVAL_COMPOSE_ENV_FILE"):
    COMPOSE.extend(["--env-file", os.environ["ENCLAVE_EVAL_COMPOSE_ENV_FILE"]])
COMPOSE.extend(["-f", "docker-compose.infra.yml", "-f", "docker-compose.app.yml"])
# Keep fixture names and pointers neutral.  In particular, the source state is
# deliberately not called "stale" or "fresh" in prompts or model-visible
# values; that would tell the model which answer the evaluator expects.
ORG_NAME = "Northbridge Legal Aid"
CONTACT_MODALITIES = ("email", "phone", "url", "address", "secure_channel")
SOURCE_STATES = ("baseline", "updated")


PersonaSpec = namedtuple(
    "PersonaSpec",
    ("key", "name", "user_type_id", "create_user_type"),
    defaults=(None, True),
)


PERSONAS = (
    PersonaSpec("generic_user", "Global / no User Type", create_user_type=False),
    PersonaSpec("family_member", "Family member"),
    PersonaSpec("former_political_prisoner", "Former Political Prisoner"),
    PersonaSpec(
        "solidarity_networks_for_political_prisoners",
        "Solidarity Networks for Political Prisoners",
    ),
)
REPLAY_LANGUAGES = ("en", "es")
DEMO_EFFECTIVE_DEFAULT_TOOL_IDS = ("curated-resources", "knowledge-search")
CONTACT_FOLLOWUPS = {
    "en": {
        "email": "Can you give me the email?",
        "phone": "Can you give me the phone number?",
        "url": "Can you give me the website?",
        "address": "Can you give me the address?",
        "secure_channel": "Can you give me the secure channel?",
    },
    "es": {
        "email": "¿Me puedes dar el email?",
        "phone": "¿Me das el número de teléfono?",
        "url": "¿Me das el sitio web?",
        "address": "¿Me das la dirección?",
        "secure_channel": "¿Me das el canal seguro?",
    },
}
INVENTORY_LIMIT = 10
INVENTORY_NAMES = tuple(
    f"Directory Sample {index:02d}"
    for index in range(1, INVENTORY_LIMIT + 2)
)
def fixture_contacts(suffix: str = "unit") -> tuple[dict[str, str], dict[str, str]]:
    """Return two distinct, neutral contact snapshots for one evaluation."""
    marker = hashlib.sha256(str(suffix).encode()).hexdigest()[:8]
    baseline = {
        "email": f"relay-{marker}-a@example.test",
        "phone": "+1-202-555-0147",
        "url": f"https://relay-{marker}-a.example.test",
        "address": "17 Meridian Avenue, Mexico City",
        "secure_channel": f"relay-{marker}-a-secure",
    }
    updated = {
        "email": f"relay-{marker}-b@example.test",
        "phone": "+1-202-555-0183",
        "url": f"https://relay-{marker}-b.example.test",
        "address": "29 Harbor Road, Mexico City",
        "secure_channel": f"relay-{marker}-b-secure",
    }
    return baseline, updated


def fixture_manifest(
    baseline: dict[str, str], updated: dict[str, str], *, inventory_names: tuple[str, ...] = INVENTORY_NAMES
) -> dict[str, Any]:
    """Describe every synthetic model-visible fixture and bind it to a hash."""
    manifest = {
        "schema": "neutral-contact-v3",
        "organization": ORG_NAME,
        "languages": list(REPLAY_LANGUAGES),
        "modalities": list(CONTACT_MODALITIES),
        "baseline_contact": dict(baseline),
        "updated_contact": dict(updated),
        "inventory_names": list(inventory_names),
        "inventory_limit": INVENTORY_LIMIT,
    }
    manifest["hash"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return manifest


def journey_case_id(persona: str, language: str, journey: str, modality: str, turn: int) -> str:
    """Stable ID for a planned, reviewable turn in the contact matrix."""
    return f"contact::{persona}::{language}::{journey}::{modality}::turn{turn}"


def inventory_case_id(persona: str, language: str, page: str) -> str:
    return f"inventory::{persona}::{language}::{page}"


def expected_case_ids(
    persona_filter: str | None = None,
    *,
    inventory_only: bool = False,
    contact_only: bool = False,
    language_filter: str | None = None,
    modality_filter: str | None = None,
    journey_filter: str | None = None,
    profile: str = "full",
) -> list[str]:
    """Return the exact plan; missing or duplicate evidence is a harness failure."""
    personas = [p for p in PERSONAS if persona_filter is None or p.key == persona_filter]
    languages = [l for l in REPLAY_LANGUAGES if language_filter is None or l == language_filter]
    modalities = [m for m in CONTACT_MODALITIES if modality_filter is None or m == modality_filter]
    journeys = [j for j in ("changed", "unchanged") if journey_filter is None or j == journey_filter]
    if profile == "smoke":
        personas = [p for p in personas if p.key == "generic_user"] or personas[:1]
        languages = [l for l in languages if l == "en"] or languages[:1]
        modalities = [m for m in modalities if m == "email"] or modalities[:1]
    ids: list[str] = []
    if not inventory_only:
        for persona in personas:
            for language in languages:
                for journey in journeys:
                    for modality in modalities:
                        for turn in (1, 2):
                            ids.append(journey_case_id(persona.key, language, journey, modality, turn))
        if persona_filter in (None, "generic_user") and not journey_filter and not modality_filter:
            if profile != "smoke":
                ids.append("control::generic_user::no_tools::email")
    if not contact_only:
        inventory_language = language_filter or "en"
        for persona in personas:
            ids.extend(
                [
                    inventory_case_id(persona.key, inventory_language, "page1"),
                    inventory_case_id(persona.key, inventory_language, "page2"),
                ]
            )
    return ids


def contact_prompt(language: str, modality: str, *, turn: int) -> str:
    prompts = {
        "en": {
            "email": "What email address can I use?",
            "phone": "What phone number can I call?",
            "url": "What website should I open?",
            "address": "What address should I use?",
            "secure_channel": "What secure channel is available?",
        },
        "es": {
            "email": "¿Qué correo electrónico puedo usar?",
            "phone": "¿Qué número de teléfono puedo llamar?",
            "url": "¿Qué sitio web debo abrir?",
            "address": "¿Qué dirección debo usar?",
            "secure_channel": "¿Qué canal seguro está disponible?",
        },
    }
    prefix = "Northbridge Legal Aid provides legal support. " if language == "en" else "Northbridge Legal Aid ofrece apoyo legal. "
    if turn == 1:
        return prefix + prompts[language][modality] + (" Responde en español." if language == "es" else " Answer in English.")
    followup = CONTACT_FOLLOWUPS[language][modality]
    return followup + (" Responde en español." if language == "es" else " Answer in English.")

def expect(label: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}{': ' + detail if detail and not ok else ''}")
    return ok

def backend_python(source: str) -> list[str]:
    p = subprocess.run([*COMPOSE, "exec", "-T", "core-backend", "python", "-c", source], cwd=ROOT, capture_output=True, text=True, timeout=45)
    if p.returncode: raise RuntimeError(p.stderr.strip() or "backend helper failed")
    return [x.strip() for x in p.stdout.splitlines() if x.strip()]


class _ComposeBackendRunner:
    def run_backend_python(self, source: str, timeout: int = 120) -> str:
        return "\n".join(backend_python(source))


def derive_ephemeral_admin_pubkey(suffix: str) -> str:
    """Derive a deterministic valid secp256k1 x-only public key marker."""
    field_prime = (1 << 256) - (1 << 32) - 977
    counter = 0
    while True:
        candidate = hashlib.sha256(
            f"issue-539-ephemeral-local-admin-{suffix}-{counter}".encode()
        ).digest()
        x_coordinate = int.from_bytes(candidate, "big")
        if x_coordinate < field_prime:
            y_squared = (pow(x_coordinate, 3, field_prime) + 7) % field_prime
            if y_squared == 0 or pow(y_squared, (field_prime - 1) // 2, field_prime) == 1:
                return candidate.hex()
        counter += 1


def new_fixture_journal(suffix: str) -> dict[str, Any]:
    """Create the cleanup journal before any fixture mutation can occur."""
    ephemeral_pubkey = derive_ephemeral_admin_pubkey(suffix)
    return {
        "suffix": suffix,
        "ephemeral_admin_pubkey": ephemeral_pubkey,
        "admin": None,
        "admin_pubkey": ephemeral_pubkey,
        "owns_admin": None,
        "replaced_admin_pubkeys": [],
        "users": [],
        "configured_type_ids": [],
        "global_tool_ids_original": None,
        "global_tools_restore_required": False,
        "resource_ids": [],
    }


def mint(
    fixtures: dict[str, Any] | None = None,
    *,
    backend_runner=backend_python,
) -> dict[str, Any]:
    """Mint fixtures stepwise, persisting every cleanup identity as soon as known."""
    if fixtures is None:
        fixtures = new_fixture_journal(str(int(time.time() * 1000)))
    suffix = str(fixtures["suffix"])
    ephemeral_pubkey = str(fixtures["ephemeral_admin_pubkey"])
    admin_rows = backend_runner(
        """import database, json
def usable(item):
    try:
        return len(bytes.fromhex(str(item.get('pubkey') or ''))) == 32
    except (TypeError, ValueError):
        return False
print(json.dumps([item.get('pubkey') for item in database.list_admins() if not usable(item)]))"""
    )
    invalid_admins = json.loads(admin_rows[-1]) if admin_rows else []
    if not isinstance(invalid_admins, list) or any(not isinstance(value, str) for value in invalid_admins):
        raise RuntimeError("could not journal invalid admin markers")
    fixtures["replaced_admin_pubkeys"] = list(invalid_admins)
    rows = backend_runner(
        f'''import auth, database, json
admins = database.list_admins()
def usable_admin(item):
    try:
        return len(bytes.fromhex(str(item.get("pubkey") or ""))) == 32
    except (TypeError, ValueError):
        return False
existing = next((item for item in admins if usable_admin(item)), None)
owns_admin = existing is None
ephemeral_pubkey = {ephemeral_pubkey!r}
if owns_admin:
    for invalid in {invalid_admins!r}:
        if database.get_admin_by_pubkey(invalid) is not None:
            database.remove_admin(invalid)
    database.add_admin(ephemeral_pubkey)
    a = database.get_admin_by_pubkey(ephemeral_pubkey)
else:
    a = existing
print(json.dumps({{"admin": auth.create_admin_session_token(a["id"], a["pubkey"], int(a.get("session_nonce", 0) or 0)), "admin_pubkey": a["pubkey"], "owns_admin": owns_admin}}))'''
    )
    admin = json.loads(rows[-1])
    fixtures.update(admin)

    for index, spec in enumerate(PERSONAS):
        entry: dict[str, Any] = {
            "key": spec.key,
            "name": spec.name,
            "user_type_id": None,
            "email": f"issue-539-{spec.key}-{suffix}@example.test",
        }
        fixtures["users"].append(entry)
        if spec.create_user_type:
            type_name = f"{spec.name} {suffix}"
            rows = backend_runner(
                f'''import database, json
type_id = database.create_user_type({type_name!r}, description="Temporary issue #539 local replay persona", display_order={index})
print(json.dumps({{"user_type_id": type_id}}))'''
            )
            entry["user_type_id"] = int(json.loads(rows[-1])["user_type_id"])
        rows = backend_runner(
            f'''import auth, database, json
email = {entry["email"]!r}
user_id = database.create_user(email=email, name={spec.name!r}, user_type_id={entry["user_type_id"]!r})
database.update_user_approval(user_id, True)
print(json.dumps({{"token": auth.create_session_token(user_id, email), "user_id": user_id}}))'''
        )
        entry.update(
            {
                **json.loads(rows[-1]),
            }
        )
    return fixtures

def req(base: str, token: str, method: str, path: str, payload: dict[str, Any] | None = None, timeout: float = 180) -> requests.Response:
    with requests.Session() as session:
        session.trust_env = False
        return session.request(
            method,
            base.rstrip("/") + path,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
            allow_redirects=False,
        )

def parse_sse(raw: str) -> list[dict[str, Any]]:
    out = []
    for block in raw.replace("\r\n", "\n").split("\n\n"):
        name, data = None, []
        for line in block.splitlines():
            if line.startswith("event:"): name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"): data.append(line.split(":", 1)[1].lstrip())
        if name:
            out.append({"event": name, "data": json.loads("\n".join(data) or "{}")})
    return out

def answer_json(value: Any) -> str:
    if not isinstance(value, dict): return ""
    return next((value[k] for k in ("answer", "content", "message") if isinstance(value.get(k), str)), "")

def executed_tool_ids(trace: Any) -> list[str]:
    if not isinstance(trace, dict):
        return []
    ids: list[str] = []
    for tool in trace.get("tools", []):
        if not isinstance(tool, dict):
            continue
        tool_id = tool.get("id")
        if isinstance(tool_id, str) and tool.get("status") in ("completed", "succeeded"):
            ids.append(tool_id)
    for delta in trace.get("trace_deltas", []):
        if not isinstance(delta, dict) or delta.get("kind") != "tool_result":
            continue
        tool_name = delta.get("tool_name")
        if isinstance(tool_name, str) and delta.get("status") in ("completed", "succeeded"):
            ids.append(tool_name)
    return sorted(set(ids))


def used_curated_resources(trace: Any) -> bool:
    return any(
        tool_id in {"curated-resources", "find_resources", "Curated Resources"}
        for tool_id in executed_tool_ids(trace)
    )


def tool_lifecycle_events(trace: Any) -> list[dict[str, str]]:
    """Keep lifecycle identity/status only; omit arguments, results, and prompts."""
    if not isinstance(trace, dict):
        return []
    events: list[dict[str, str]] = []
    for tool in trace.get("tools", []):
        if not isinstance(tool, dict):
            continue
        tool_name = tool.get("id") or tool.get("name")
        status = tool.get("status")
        if isinstance(tool_name, str) and isinstance(status, str):
            events.append(
                {"kind": "tool_summary", "tool": tool_name, "status": status}
            )
    lifecycle_kinds = {"tool_call", "tool_result", "tool_retry", "timeout"}
    for delta in trace.get("trace_deltas", []):
        if not isinstance(delta, dict) or delta.get("kind") not in lifecycle_kinds:
            continue
        tool_name = delta.get("tool_name")
        status = delta.get("status")
        if not isinstance(tool_name, str) or not isinstance(status, str):
            continue
        event = {"kind": str(delta["kind"]), "tool": tool_name, "status": status}
        metadata = delta.get("metadata")
        call_id = metadata.get("call_id") if isinstance(metadata, dict) else None
        if isinstance(call_id, str):
            event["call_id"] = call_id
        events.append(event)
    return events


def resource_tool_metadata(trace: Any) -> list[dict[str, Any]]:
    """Allowlist count/pagination metadata from successful Curated Resource Tools."""
    if not isinstance(trace, dict):
        return []
    records: list[dict[str, Any]] = []
    for tool in trace.get("tools", []):
        if not isinstance(tool, dict):
            continue
        tool_name = tool.get("id") or tool.get("name")
        if tool_name not in {"curated-resources", "find_resources", "Curated Resources"}:
            continue
        if tool.get("status") not in {"completed", "succeeded"}:
            continue
        metadata = tool.get("metadata")
        if not isinstance(metadata, dict):
            continue
        returned = metadata.get("returned_count")
        total = metadata.get("total_count")
        has_more = metadata.get("has_more")
        next_offset = metadata.get("next_offset")
        valid_integer = lambda value: isinstance(value, int) and not isinstance(value, bool) and value >= 0
        if not valid_integer(returned) or not valid_integer(total) or not isinstance(has_more, bool):
            continue
        if next_offset is not None and not valid_integer(next_offset):
            continue
        records.append(
            {
                "returned_count": returned,
                "total_count": total,
                "has_more": has_more,
                "next_offset": next_offset,
            }
        )
    return records


def evidence_entry(
    *,
    persona: str,
    case: str,
    answer: str,
    trace: Any,
    passed: bool,
    detail: str,
    case_id: str | None = None,
    journey_id: str | None = None,
    turn_index: int | None = None,
    prompt: str | None = None,
    context: dict[str, Any] | None = None,
    dimensions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return reviewable synthetic evidence without prompts or raw provider traces."""
    entry = {
        "persona": persona,
        "case": case,
        "answer": answer,
        "elapsed_ms": trace.get("_evaluation_elapsed_ms") if isinstance(trace, dict) else None,
        "answer_chars": len(answer),
        "answer_words": len(answer.split()),
        "executed_tools": executed_tool_ids(trace),
        "tool_lifecycle": tool_lifecycle_events(trace),
        "resource_tool_metadata": resource_tool_metadata(trace),
        "passed": passed,
        "detail": detail,
    }
    if case_id is not None:
        entry["case_id"] = case_id
    if journey_id is not None:
        entry["journey_id"] = journey_id
    if turn_index is not None:
        entry["turn_index"] = turn_index
    if prompt is not None:
        entry["prompt"] = prompt
    if context is not None:
        entry["context"] = context
    if dimensions is not None:
        entry["dimensions"] = dimensions
    observed_model = trace.get("_evaluation_model") if isinstance(trace, dict) else None
    if isinstance(observed_model, str) and observed_model:
        entry["model"] = observed_model
    return entry


def exact_pointer_match(answer: str, expected: str, modality: str | None = None) -> bool:
    """Match a complete pointer, rejecting suffix/prefix substring spoofs."""
    if not isinstance(expected, str) or not expected:
        return False
    if modality == "email":
        candidates = [
            candidate.rstrip(".,;:")
            for candidate in re.findall(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", answer)
        ]
        return expected in candidates
    if modality == "url":
        candidates = re.findall(r"https?://[^\s)\]>\"']+", answer)
        # Markdown emphasis/backticks are presentation wrappers, not URL
        # content. Strip only terminal punctuation/wrappers; path and host
        # suffixes remain part of the candidate and therefore fail closed.
        return any(candidate.rstrip(".,;:*_`") == expected for candidate in candidates)
    if modality == "phone":
        expected_digits = re.sub(r"\D", "", expected)
        candidates = re.findall(r"(?<!\d)\+?\d[\d\s().-]{7,}\d(?!\d)", answer)
        return any(re.sub(r"\D", "", candidate) == expected_digits for candidate in candidates)
    if modality == "address":
        # Keep the street and number exact. Only punctuation spacing and the
        # one fixture-approved Spanish city alias are normalized.
        variants = [expected]
        if expected.endswith(", Mexico City"):
            variants.append(expected[:-len("Mexico City")] + "Ciudad de México")

        def normalize_address(value: str) -> str:
            value = unicodedata.normalize("NFKC", value).casefold()
            value = re.sub(r"[,.;]", " ", value)
            return re.sub(r"\s+", " ", value).strip()

        normalized_answer = normalize_address(answer)
        return any(
            re.search(
                rf"(?<![\w-]){re.escape(normalize_address(variant))}(?![\w-])",
                normalized_answer,
            )
            for variant in variants
        )
    escaped = re.escape(expected)
    return bool(re.search(rf"(?<![\w-]){escaped}(?![\w-])", answer))


def score_contact_dimensions(
    answer: str,
    trace: Any,
    *,
    expected: str,
    old_contacts: dict[str, str],
    lookup_required: bool,
    tool_enabled: bool = True,
    modality: str | None = None,
) -> dict[str, Any]:
    """Score independent contact dimensions without conflating tool and answer quality."""
    exact = exact_pointer_match(answer, expected, modality)
    old_literal_present = [
        key for key, value in old_contacts.items() if isinstance(value, str) and value in answer
    ]
    old_absent = not old_literal_present
    used = used_curated_resources(trace)
    lookup_passed = (not used) if not tool_enabled else (used if lookup_required else True)
    return {
        "exact_pointer": {
            "passed": exact if tool_enabled else not exact,
            "expected": expected if tool_enabled else None,
            "observed": exact,
        },
        "current_old": {
            "passed": old_absent,
            "forbidden_values": len(old_contacts),
            "observed_absent": old_absent,
            "old_literal_present": old_literal_present,
            "needs_semantic_review": bool(old_literal_present),
        },
        "lookup": {
            "passed": lookup_passed,
            "required": lookup_required,
            "used": used,
            "status": "required" if lookup_required else "not_required",
        },
        "quality_passed": bool(
            (exact if tool_enabled else not exact)
            and lookup_passed
        ),
    }


def score_inventory_turn(
    answer: str,
    trace: Any,
    *,
    final_name: str,
    continuation: bool,
    previous_answer: str = "",
    previous_trace: Any = None,
) -> tuple[bool, str]:
    metadata = resource_tool_metadata(trace)
    previous_metadata = resource_tool_metadata(previous_trace)
    tool_ok = used_curated_resources(trace) and bool(metadata)
    normalized = answer.casefold()

    def has(*patterns: str) -> bool:
        return any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in patterns)

    no_more_claim = has(
        r"\bno\s+(?:more|additional)\s+(?:matching\s+)?(?:results?|resources?)\b",
        r"\bno\s+additional\s+pages?\b",
        r"\bno\s+remaining\s+(?:matching\s+)?(?:results?|resources?)\b",
        r"\bno\s+quedan\b",
        r"\bno\s+hay\s+m[aá]s\s+(?:resultados?|recursos?)\b",
        r"\bsin\s+m[aá]s\s+(?:resultados?|recursos?)\b",
        r"\bno\s+(?:next|further)\s+pages?\b",
        r"\bno\s+hay\s+(?:otra|siguiente)\s+p[aá]gina\b",
    )
    positive_more = not no_more_claim and has(
        r"\bmore\s+(?:matching\s+)?(?:results?|resources?|pages?)\b",
        r"\badditional\s+(?:matching\s+)?(?:results?|resources?)\b",
        r"\bnext\s+page\b",
        r"\bremaining\s+(?:matching\s+)?(?:results?|resources?)\b",
        r"\bm[aá]s\s+(?:resultados?|recursos?)\b",
        r"\bsiguiente\s+p[aá]gina\b",
        r"\brecursos?\s+restantes\b",
    )
    complete_claim = has(r"\ball\b", r"\bevery\b", r"\bcomplete\b", r"\btod[oa]s\b", r"\bcomplet[oa]s?\b", r"\bconjunto\s+completo\b")
    qualified_or_negated = has(
        r"\bnot\s+all\b", r"\bnot\s+(?:a\s+)?complete\b", r"\bmay\s+not\s+be\s+(?:all|complete)\b",
        r"\b(?:cannot|can\s+not|can['’]t|unable\s+to)\s+(?:confirm|verify)[^.]{0,80}\b(?:no\s+more|no\s+additional\s+pages?|all|complete)\b",
        r"\bno\s+puedo\s+(?:confirmar|verificar)[^.]{0,80}\b(?:no\s+hay\s+m[aá]s|tod[oa]s|complet[oa])\b",
        r"\bno\s+son\s+tod[oa]s\b",
    )

    def mentioned_names(value: str) -> set[str]:
        return {
            name for name in INVENTORY_NAMES
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", value, flags=re.IGNORECASE)
        }

    def name_counts(value: str) -> dict[str, int]:
        return {
            name: len(re.findall(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", value, flags=re.IGNORECASE))
            for name in INVENTORY_NAMES
        }

    answer_names = mentioned_names(answer)
    previous_names = mentioned_names(previous_answer)
    combined_names = answer_names | previous_names
    expected_names = set(INVENTORY_NAMES)
    expected_first_page = set(INVENTORY_NAMES[:INVENTORY_LIMIT])
    expected_last_page = {final_name}
    expected_count = len(INVENTORY_NAMES)
    scoped = has(r"\bmatching\b", r"\bsupplied\s+filters?\b", r"\bnames?\s+start", r"\bcoinciden\b", r"\bfiltros?\b", r"\bnombres?\s+empiezan\b", r"\bque\s+empiezan\b")

    all_metadata = [*previous_metadata, *metadata]
    actual_totals = {record["total_count"] for record in all_metadata}
    actual_counts = {record["returned_count"] for record in all_metadata}
    authoritative_counts = actual_totals | actual_counts | {expected_count}
    authoritative_pairs = {(record["returned_count"], record["total_count"]) for record in all_metadata}
    authoritative_offsets = {record["next_offset"] for record in all_metadata if record["next_offset"] is not None}
    authoritative_offsets.add(expected_count)
    authoritative_remaining_counts = {
        max(record["total_count"] - int(record["next_offset"]), 0)
        for record in all_metadata
        if record["has_more"] and record["next_offset"] is not None
    }
    claimed_pairs = {
        (int(returned), int(total))
        for pattern in (r"\b(\d+)\s*/\s*(\d+)\b", r"\b(\d+)\s+(?:of|de)\s+(\d+)\b")
        for returned, total in re.findall(pattern, normalized)
    }
    claimed_offsets = {
        int(offset) for offset in re.findall(r"\b(?:next\s+)?(?:offset|desplazamiento)(?:\s+(?:is|es|at|en))?\s*[:=]?\s*(\d+)\b", normalized)
    }
    claimed_counts = {
        int(value) for value in re.findall(r"\b(\d+)\s+(?:matching\s+|coincidentes?\s+)?(?:ready\s+)?(?:resources?|recursos?)\b", normalized)
    }
    claimed_remaining_counts = {
        int(value) for value in re.findall(r"\b(\d+)\s+(?:more\s+(?:matching\s+)?(?:resources?|results?)|(?:recursos?|resultados?)\s+m[aá]s)\b", normalized)
    }
    terminal_proof = (
        bool(all_metadata)
        and any(record["has_more"] is False for record in all_metadata)
        and sum(record["returned_count"] for record in all_metadata) >= expected_count
    )
    # A displayed-subset count is justified only when the answer names every
    # expected target and the provider trace proves it reached a terminal page.
    justified_subset_pairs = (
        {(expected_count, total) for total in actual_totals}
        if terminal_proof and combined_names == expected_names
        else set()
    )
    allowed_pairs = authoritative_pairs | justified_subset_pairs
    metadata_consistent = all(
        (record["has_more"] and record["next_offset"] is not None and record["next_offset"] < record["total_count"])
        or (not record["has_more"] and record["next_offset"] is None)
        for record in all_metadata
    )
    wrong_numeric_claim = (
        not claimed_pairs.issubset(allowed_pairs)
        or not claimed_offsets.issubset(authoritative_offsets)
        or not claimed_counts.issubset(authoritative_counts)
        or not claimed_remaining_counts.issubset(authoritative_remaining_counts | {0})
        or not metadata_consistent
    )
    duplicate_names = any(count > 1 for count in name_counts(answer).values())
    broad_total_ok = bool(actual_totals) and min(actual_totals) >= expected_count

    if continuation:
        if previous_names == expected_first_page:
            page_ok = (
                answer_names == expected_last_page
                and no_more_claim
                and bool(metadata)
                and metadata[-1]["has_more"] is False
                and broad_total_ok
            )
        elif previous_names == expected_names:
            page_ok = not answer_names and no_more_claim and bool(metadata) and metadata[-1]["has_more"] is False and broad_total_ok
        else:
            page_ok = False
    else:
        exact_bounded_page = (
            answer_names == expected_first_page
            and final_name not in answer_names
            and positive_more
            and bool(metadata)
            and metadata[0]["returned_count"] >= len(expected_first_page)
            and metadata[0]["has_more"] is True
            and broad_total_ok
        )
        scoped_complete = (
            answer_names == expected_names
            and final_name in answer_names
            and scoped
            and complete_claim
            and bool(metadata)
            and broad_total_ok
            and terminal_proof
        )
        page_ok = exact_bounded_page or scoped_complete
    unsupported_complete_claim = complete_claim and combined_names != expected_names
    passed = tool_ok and page_ok and not unsupported_complete_claim and not qualified_or_negated and not wrong_numeric_claim and not duplicate_names
    return passed, f"tool={tool_ok} metadata={len(metadata)} previous_metadata={len(previous_metadata)} page={page_ok} names={len(answer_names)} new_names={len(answer_names - previous_names)} combined_names={len(combined_names)} backend_totals={sorted(actual_totals)} unsupported_complete={unsupported_complete_claim} qualified={qualified_or_negated} wrong_numeric={wrong_numeric_claim} duplicate_names={duplicate_names}"


def validate_case_ids(evidence: list[dict[str, Any]], expected_ids: list[str]) -> tuple[bool, str]:
    """Fail closed when a run silently omits or repeats a planned turn."""
    expected = list(expected_ids)
    observed = [item.get("case_id") for item in evidence]
    if any(not isinstance(case_id, str) or not case_id for case_id in observed):
        return False, "missing case_id"
    duplicates = sorted({case_id for case_id in observed if observed.count(case_id) > 1})
    missing = sorted(set(expected) - set(observed))
    unexpected = sorted(set(observed) - set(expected))
    if duplicates or missing or unexpected or len(observed) != len(expected):
        return False, f"duplicates={duplicates} missing={missing} unexpected={unexpected} observed={len(observed)} expected={len(expected)}"
    return True, "case_ids_complete"


def journey_identity_from_case_id(case_id: str) -> str:
    parts = case_id.split("::")
    if not parts:
        return case_id
    if parts[0] == "contact":
        parts = [part for part in parts if not part.startswith("turn")]
    elif parts[0] == "inventory":
        parts = [part for part in parts if part not in {"page1", "page2"}]
    elif parts[0] == "control":
        # The control case has an explicit modality suffix, while its
        # ``no_tools`` journey marker is itself part of the identity.
        if len(parts) >= 2 and parts[-1] == "email":
            parts = parts[:-1]
    return "::".join(parts)


def journey_denominators(expected_ids: list[str], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    planned: dict[str, set[str]] = {}
    for case_id in expected_ids:
        planned.setdefault(journey_identity_from_case_id(case_id), set()).add(case_id)
    observed: dict[str, set[str]] = {}
    for item in evidence:
        case_id = item.get("case_id")
        if isinstance(case_id, str):
            observed_id = str(item.get("journey_id") or case_id)
            observed.setdefault(journey_identity_from_case_id(observed_id), set()).add(case_id)
    completed = sorted(identity for identity, case_ids in planned.items() if observed.get(identity, set()) >= case_ids)
    attempted = sorted(identity for identity in observed if identity in planned)
    incomplete = sorted(identity for identity in planned if identity not in completed)
    return {
        "planned": len(planned),
        "attempted": len(attempted),
        "completed": len(completed),
        "incomplete": incomplete,
        "case_denominator": len(expected_ids),
    }


def evaluation_summary(
    *,
    expected_case_count: int,
    evidence: list[dict[str, Any]],
    failures: int,
    cleanup_failures: int,
    fatal: bool,
    expected_case_ids: list[str] | None = None,
    harness_failures: int = 0,
) -> dict[str, Any]:
    completed = len(evidence)
    passed_cases = sum(item.get("passed") is True for item in evidence)
    failed_cases = completed - passed_cases
    case_ids_ok = True
    case_id_detail = "not_checked"
    if expected_case_ids is not None:
        case_ids_ok, case_id_detail = validate_case_ids(evidence, expected_case_ids)
    journeys = journey_denominators(expected_case_ids or [], evidence)
    quality_passed = failed_cases == 0 and failures == 0
    harness_passed = not fatal and harness_failures == 0 and case_ids_ok and completed == expected_case_count
    cleanup_passed = cleanup_failures == 0
    passed = (
        not fatal
        and quality_passed
        and cleanup_passed
        and harness_passed
    )
    status = "fatal" if fatal else ("passed" if passed else "failed")
    return {
        "status": status,
        "passed": passed,
        "fatal": fatal,
        "failure_count": failures,
        "quality_failure_count": failures,
        "harness_failure_count": harness_failures + (0 if case_ids_ok else 1),
        "cleanup_failure_count": cleanup_failures,
        "expected_case_count": expected_case_count,
        "completed_case_count": completed,
        "passed_case_count": passed_cases,
        "failed_case_count": failed_cases,
        "quality_status": "passed" if quality_passed else "failed",
        "harness_status": "passed" if harness_passed else "failed",
        "cleanup_status": "passed" if cleanup_passed else "failed",
        "case_ids_valid": case_ids_ok,
        "case_id_detail": case_id_detail,
        "journeys": journeys,
        "semantic_review_status": "unreviewed",
    }


def persist_evaluation_evidence(
    path: Path,
    *,
    payload: dict[str, Any],
    expected_case_count: int,
    evidence: list[dict[str, Any]],
    failures: int,
    cleanup_failures: int,
    fatal: bool,
    expected_case_ids: list[str] | None = None,
    harness_failures: int = 0,
) -> tuple[dict[str, Any], int, Exception | None]:
    summary = evaluation_summary(
        expected_case_count=expected_case_count,
        evidence=evidence,
        failures=failures,
        cleanup_failures=cleanup_failures,
        fatal=fatal,
        expected_case_ids=expected_case_ids,
        harness_failures=harness_failures,
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema": "curated-resource-contact-evidence-v3",
                    **payload,
                    **summary,
                    "cases": evidence,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return summary, cleanup_failures, None
    except Exception as exc:
        cleanup_failures += 1
        summary = evaluation_summary(
            expected_case_count=expected_case_count,
            evidence=evidence,
            failures=failures,
            cleanup_failures=cleanup_failures,
            fatal=fatal,
            expected_case_ids=expected_case_ids,
            harness_failures=harness_failures,
        )
        return summary, cleanup_failures, exc


def audit_contains_fine_timing(value: Any) -> bool:
    rendered = json.dumps(value, sort_keys=True, ensure_ascii=False).casefold()
    return any(
        phase in rendered
        for phase in (
            "tool_planning_model_duration",
            "final_answer_model_duration",
            "final_answer_response_header_wait",
            "final_answer_first_provider_event_wait",
            "tool_execution",
            "resource_directory_lookup",
            "retrieval",
            "retry_delay",
            "total_turn",
        )
    )

def run_turn(base: str, token: str, payload: dict[str, Any], stream: bool, timeout: float) -> tuple[str, Any, str | None]:
    started = time.perf_counter()
    response = req(base, token, "POST", "/llm/chat/stream" if stream else "/llm/chat", payload, timeout)
    if response.status_code != 200:
        raise RuntimeError(f"chat returned HTTP {response.status_code}")
    if not stream:
        body = response.json()
        trace = dict(body.get("trace") or body)
        observed_model = body.get("model") or trace.get("model")
        if isinstance(observed_model, str) and observed_model:
            trace["_evaluation_model"] = observed_model
        trace["_evaluation_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return answer_json(body), trace, body.get("session_id")
    response.encoding = "utf-8"
    events = parse_sse(response.text)
    answer = "".join(str(e["data"].get("delta") or "") for e in events if e["event"] == "answer_delta").strip()
    trace = next((e["data"].get("trace", {}) for e in events if e["event"] == "trace_final"), {})
    trace = dict(trace)
    observed_model = trace.get("model") or next(
        (e["data"].get("model") for e in events if isinstance(e.get("data"), dict) and isinstance(e["data"].get("model"), str)),
        None,
    )
    if isinstance(observed_model, str) and observed_model:
        trace["_evaluation_model"] = observed_model
    trace["_evaluation_elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
    sid = next((e["data"].get("session_id") for e in events if e["data"].get("session_id")), None)
    return answer, trace, sid

def resource(
    base: str,
    token: str,
    rid: str,
    contact: dict[str, str],
    method: str = "POST",
    *,
    name: str = ORG_NAME,
    display_order: int = 0,
) -> None:
    body = {
        "name": name,
        "kind": "organization",
        "description": "Synthetic issue #539 evaluation fixture; do not contact.",
        "pointers": [
            {"type": pointer_type, "value": value}
            for pointer_type, value in contact.items()
        ],
        "languages": ["en", "es"],
        "regions": [{"level": "country", "code": "MX"}],
        "tags": ["legal"],
        "provenance": {"vetted_by": "issue-539-eval"},
        "verified": True,
        "display_order": display_order,
    }
    if method == "POST":
        body["resource_id"] = rid
    path = "/admin/resources" if method == "POST" else f"/admin/resources/{rid}"
    r = req(base, token, method, path, body)
    if r.status_code not in ({200, 201} if method == "POST" else {200}):
        raise RuntimeError(f"resource {method} returned HTTP {r.status_code}")


def configure_persona_tools(
    base: str,
    admin_token: str,
    user_type_id: int | None,
    tool_ids: list[str] | None = None,
    *,
    journal: dict[str, Any] | None = None,
) -> None:
    if tool_ids is None:
        tool_ids = list(DEMO_EFFECTIVE_DEFAULT_TOOL_IDS)
    if user_type_id is None:
        if journal is None:
            raise ValueError("global Tool configuration requires a cleanup journal")
        if not journal.get("global_tools_restore_required"):
            original = req(
                base,
                admin_token,
                "GET",
                "/admin/ai-config/user_default_tool_ids",
                timeout=30,
            )
            if original.status_code != 200:
                raise RuntimeError(f"read global tools returned HTTP {original.status_code}")
            original_value = original.json().get("value")
            if not isinstance(original_value, str):
                raise RuntimeError("global Tool default did not return a string value")
            journal["global_tool_ids_original"] = original_value
            # Mark restoration required before the mutating request so a lost
            # response cannot strand the global configuration.
            journal["global_tools_restore_required"] = True
        path = "/admin/ai-config/user_default_tool_ids"
        label = "global tools"
    else:
        path = f"/admin/ai-config/user-type/{user_type_id}/user_default_tool_ids"
        label = f"User Type {user_type_id} tools"
    response = req(
        base,
        admin_token,
        "PUT",
        path,
        {"value": json.dumps(tool_ids, separators=(",", ":"))},
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f"configure {label} returned HTTP {response.status_code}")


def cleanup_persona_tools(base: str, admin_token: str, user_type_id: int) -> bool:
    response = req(
        base,
        admin_token,
        "DELETE",
        f"/admin/ai-config/user-type/{user_type_id}/user_default_tool_ids",
        timeout=30,
    )
    return response.status_code == 200


def restore_global_tools(
    base: str,
    admin_token: str,
    journal: dict[str, Any],
) -> bool:
    if not journal.get("global_tools_restore_required"):
        return True
    original = journal.get("global_tool_ids_original")
    if not isinstance(original, str):
        return False
    response = req(
        base,
        admin_token,
        "PUT",
        "/admin/ai-config/user_default_tool_ids",
        {"value": original},
        timeout=30,
    )
    if response.status_code == 200:
        journal["global_tools_restore_required"] = False
    return response.status_code == 200


def persona_tools_effective(base: str, user_token: str, user_type_id: int | None) -> bool:
    path = (
        "/session-defaults"
        if user_type_id is None
        else f"/session-defaults?user_type_id={user_type_id}"
    )
    response = req(
        base,
        user_token,
        "GET",
        path,
        timeout=30,
    )
    if response.status_code != 200:
        return False
    try:
        default_tool_ids = set(response.json().get("default_tool_ids", []))
        # The server correctly omits Knowledge Search when this synthetic replay
        # has no active documents. Curated Resources must still be effective.
        return "curated-resources" in default_tool_ids
    except (AttributeError, TypeError, ValueError):
        return False


def cleanup_session(base: str, token: str, session_id: str) -> bool:
    """Delete a session, tolerating the reverse proxy's short rate window."""
    for attempt in range(4):
        response = req(base, token, "DELETE", f"/query/session/{session_id}", timeout=30)
        try:
            body = response.json()
        except (AttributeError, ValueError):
            body = {}
        if session_cleanup_ok(response.status_code, body):
            return True
        if response.status_code != 429 or attempt == 3:
            return False
        retry_after = None
        headers = getattr(response, "headers", {})
        try:
            retry_after = float(headers.get("Retry-After"))
        except (AttributeError, TypeError, ValueError):
            pass
        time.sleep(min(max(retry_after if retry_after is not None else 0.5 * (attempt + 1), 0.25), 5.0))
    return False


def session_cleanup_ok(status_code: int, body: Any) -> bool:
    deletion = body.get("deletion", {}) if isinstance(body, dict) else {}
    return status_code == 200 and body.get("status") == "deleted" and deletion.get("status") == "succeeded"


def cleanup_resource(base: str, token: str, resource_id: str) -> bool:
    deleted = req(base, token, "DELETE", f"/admin/resources/{resource_id}", timeout=30)
    try:
        body = deleted.json()
    except ValueError:
        body = {}
    if deleted.status_code != 404 and not resource_delete_ok(deleted.status_code, body):
        return False
    listing = req(base, token, "GET", "/admin/resources", timeout=30)
    if listing.status_code != 200:
        return False
    try:
        resources = listing.json().get("resources", [])
    except (TypeError, ValueError):
        return False
    return not any(isinstance(item, dict) and item.get("resource_id") == resource_id for item in resources)


def resource_delete_ok(status_code: int, body: Any) -> bool:
    return status_code == 200 and isinstance(body, dict) and body.get("success") is True


def reconcile_fixture_journal(fixtures: dict[str, Any]) -> None:
    """Recover exact marker-owned IDs if a create succeeded before its call failed."""
    suffix = str(fixtures["suffix"])
    rows = backend_python(f'''import database, json
suffix = {suffix!r}
users = []
for user in database.list_users():
    email = str(user.get("email") or "")
    if email.startswith("issue-539-") and email.endswith("-" + suffix + "@example.test"):
        users.append({{"user_id": user["id"], "user_type_id": user.get("user_type_id"), "email": email}})
types = [{{"user_type_id": item["id"]}} for item in database.list_user_types() if str(item.get("name") or "").endswith(" " + suffix) and item.get("description") == "Temporary issue #539 local replay persona"]
print(json.dumps({{"users": users, "types": types}}))''')
    recovered = json.loads(rows[-1]) if rows else {"users": [], "types": []}
    by_type = {
        int(item["user_type_id"]): item
        for item in fixtures["users"]
        if item.get("user_type_id") is not None
    }
    for item in recovered.get("types", []):
        type_id = int(item["user_type_id"])
        if type_id not in by_type:
            entry = {"key": f"recovered_type_{type_id}", "user_type_id": type_id}
            fixtures["users"].append(entry)
            by_type[type_id] = entry
    for item in recovered.get("users", []):
        raw_type_id = item.get("user_type_id")
        if raw_type_id is None:
            entry = next(
                (
                    candidate
                    for candidate in fixtures["users"]
                    if candidate.get("email") == item.get("email")
                ),
                None,
            )
            if entry is None:
                entry = {
                    "key": "recovered_global_user",
                    "email": item.get("email"),
                    "user_type_id": None,
                }
        else:
            type_id = int(raw_type_id)
            entry = by_type.setdefault(
                type_id,
                {"key": f"recovered_type_{type_id}", "user_type_id": type_id},
            )
        if entry not in fixtures["users"]:
            fixtures["users"].append(entry)
        entry["user_id"] = int(item["user_id"])


def cleanup_personas(users: list[dict[str, Any]], suffix: str | None = None) -> bool:
    user_ids = sorted({int(user["user_id"]) for user in users if user.get("user_id") is not None})
    type_ids = sorted({int(user["user_type_id"]) for user in users if user.get("user_type_id") is not None})
    rows = backend_python(f"""import database, json
from pathlib import Path
user_ids = {user_ids!r}
type_ids = {type_ids!r}
suffix = {suffix!r}
if suffix:
    for user in database.list_users():
        email = str(user.get("email") or "")
        if email.startswith("issue-539-") and email.endswith("-" + suffix + "@example.test"):
            user_ids.append(int(user["id"]))
    for item in database.list_user_types():
        if str(item.get("name") or "").endswith(" " + suffix) and item.get("description") == "Temporary issue #539 local replay persona":
            type_ids.append(int(item["id"]))
user_ids = sorted(set(user_ids))
type_ids = sorted(set(type_ids))
with database.get_cursor() as cursor:
    placeholders = ",".join("?" for _ in user_ids) or "NULL"
    cursor.execute("SELECT transcript_path FROM session_logs WHERE subject_user_id IN (" + placeholders + ")", user_ids)
    transcript_paths = [row[0] for row in cursor.fetchall() if row[0]]
    cursor.execute("DELETE FROM session_logs WHERE subject_user_id IN (" + placeholders + ")", user_ids)
for value in transcript_paths:
    path = Path(value)
    if path.is_file():
        path.unlink()
deleted_users = [database.delete_user(user_id) for user_id in user_ids if database.get_user(user_id) is not None]
deleted_types = [database.delete_user_type(type_id) for type_id in type_ids if database.get_user_type(type_id) is not None]
print(json.dumps({{"deleted_users": deleted_users, "deleted_types": deleted_types, "deleted_transcripts": len(transcript_paths), "users_exist": [database.get_user(user_id) is not None for user_id in user_ids], "types_exist": [database.get_user_type(type_id) is not None for type_id in type_ids]}}))
""")
    result = json.loads(rows[-1]) if rows else {}
    return (
        all(result.get("deleted_users", []))
        and all(result.get("deleted_types", []))
        and not any(result.get("users_exist", [True]))
        and not any(result.get("types_exist", [True]))
    )


def sage_identity_cleanup_sql(users: list[dict[str, Any]]) -> str:
    external_ids = ",".join(
        f"'{int(user['user_id'])}'" for user in users if user.get("user_id") is not None
    )
    if not external_ids:
        return "SELECT 0;"
    return (
        "DELETE FROM external_identities "
        "WHERE identity_type = 'user' AND external_id IN ("
        + external_ids
        + "); SELECT count(*) FROM external_identities "
        "WHERE identity_type = 'user' AND external_id IN ("
        + external_ids
        + ");"
    )


def cleanup_sage_identities(users: list[dict[str, Any]]) -> bool:
    if not any(user.get("user_id") is not None for user in users):
        return True
    process = subprocess.run(
        [
            *COMPOSE,
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "sage",
            "-d",
            "sage",
            "-Atc",
            sage_identity_cleanup_sql(users),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=45,
    )
    if process.returncode:
        return False
    lines = [line.strip() for line in process.stdout.splitlines() if line.strip()]
    return bool(lines) and lines[-1] == "0"


def cleanup_admin(
    pubkey: str,
    owned: bool | None,
    *,
    backend_runner=backend_python,
) -> bool:
    if owned is False:
        return True
    rows = backend_runner(
        f"import database, json; existed = database.get_admin_by_pubkey({pubkey!r}) is not None; removed = database.remove_admin({pubkey!r}) if existed else False; print(json.dumps({{'existed': existed, 'removed': removed, 'exists': database.get_admin_by_pubkey({pubkey!r}) is not None}}))"
    )
    result = json.loads(rows[-1]) if rows else {}
    return result.get("exists") is False and (
        result.get("existed") is False or result.get("removed") is True
    )


def restore_replaced_admins(fixtures: dict[str, Any], *, backend_runner=backend_python) -> bool:
    markers = fixtures.get("replaced_admin_pubkeys", [])
    if not markers:
        return True
    rows = backend_runner(
        f"import database, json; markers = {markers!r}; restored = []; "
        "[restored.append(database.add_admin(marker)) for marker in markers if database.get_admin_by_pubkey(marker) is None]; "
        "print(json.dumps({'restored': len(restored), 'remaining': sum(database.get_admin_by_pubkey(marker) is None for marker in markers)}))"
    )
    result = json.loads(rows[-1]) if rows else {}
    return result.get("remaining") == 0


def exit_code_for_summary(summary: dict[str, Any]) -> int:
    if summary.get("fatal") is True:
        return 2
    return 0 if summary.get("passed") is True else 1


def runtime_identity_snapshot(
    observed_models: list[str] | None = None, *, probe_runtime: bool = False
) -> dict[str, Any]:
    """Capture non-secret model/config identity for auditability."""
    configured_model = next(
        (os.environ.get(name) for name in ("TINFOIL_MODEL", "LLM_MODEL") if os.environ.get(name)),
        None,
    )
    provider = next(
        (os.environ.get(name) for name in ("LLM_PROVIDER", "TINFOIL_PROVIDER") if os.environ.get(name)),
        None,
    )
    source = "environment" if configured_model or provider else "unavailable"
    if probe_runtime and configured_model is None:
        try:
            process = subprocess.run(
                [*COMPOSE, "exec", "-T", "sage", "sh", "-c", "printf '%s\\n' \"$TINFOIL_MODEL\" \"$TINFOIL_REASONING_EFFORT\""],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if process.returncode == 0:
                values = [line.strip() for line in process.stdout.splitlines()]
                if values and values[0]:
                    configured_model = values[0]
                    source = "compose_runtime"
                if len(values) > 1 and values[1]:
                    reasoning_effort = values[1]
                else:
                    reasoning_effort = None
            else:
                reasoning_effort = None
        except (OSError, subprocess.SubprocessError):
            reasoning_effort = None
    else:
        reasoning_effort = os.environ.get("TINFOIL_REASONING_EFFORT")
    models = sorted({model for model in (observed_models or []) if isinstance(model, str) and model})
    identity = {
        "configured_model": configured_model,
        "provider": provider,
        "reasoning_effort": reasoning_effort,
        "observed_models": models,
        "source": source,
    }
    identity["fingerprint"] = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return identity


def runtime_identity_validation(
    start: dict[str, Any], observed_models: list[str], end: dict[str, Any] | None = None
) -> dict[str, Any]:
    observed = sorted({model for model in observed_models if isinstance(model, str) and model})
    configured = start.get("configured_model") if isinstance(start, dict) else None
    configured_end = end.get("configured_model") if isinstance(end, dict) else configured
    if configured and configured_end and configured != configured_end:
        status = "changed"
    elif len(observed) == 0:
        status = "unobserved"
    elif len(observed) > 1:
        status = "mixed"
    elif configured and observed[0] != configured:
        status = "mismatch"
    elif not configured:
        status = "unverified"
    else:
        status = "consistent"
    return {
        "status": status,
        "harness_ok": status == "consistent",
        "configured_model": configured,
        "configured_model_end": configured_end,
        "observed_models": observed,
    }


def main(*, preflight=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-base", default="http://localhost:18000")
    ap.add_argument("--timeout", type=float, default=180)
    ap.add_argument("--persona", choices=[persona.key for persona in PERSONAS])
    ap.add_argument("--language", choices=REPLAY_LANGUAGES)
    ap.add_argument("--modality", choices=CONTACT_MODALITIES)
    ap.add_argument("--journey", choices=("changed", "unchanged"))
    ap.add_argument("--profile", choices=("smoke", "full"), default="smoke")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--inventory-only", action="store_true")
    mode.add_argument("--contact-only", action="store_true")
    ap.add_argument("--evidence-file", type=Path, default=Path("/tmp/issue539-model-eval-evidence.json"))
    args = ap.parse_args()
    try:
        args.api_base = validate_loopback_api_base(args.api_base)
    except ValueError as exc:
        ap.error(str(exc))
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        ap.error("--timeout must be a finite positive number")
    sessions: list[tuple[str, str]] = []
    suffix = str(int(time.time() * 1000))
    rid = f"issue-539-contact-{suffix}"
    inventory_ids = [f"issue-539-inventory-{index}-{suffix}" for index in range(len(INVENTORY_NAMES))]
    fixtures = new_fixture_journal(suffix)
    fixtures["resource_ids"] = []
    baseline, updated = fixture_contacts(suffix)
    manifest = fixture_manifest(baseline, updated)
    expected_ids = expected_case_ids(
        args.persona,
        inventory_only=args.inventory_only,
        contact_only=args.contact_only,
        language_filter=args.language,
        modality_filter=args.modality,
        journey_filter=args.journey,
        profile=args.profile,
    )
    expected_cases = len(expected_ids)
    quality_failures = 0
    harness_failures = 0
    cleanup_failures = 0
    fatal = False
    fatal_error_type: str | None = None
    evidence: list[dict[str, Any]] = []
    runtime_start = runtime_identity_snapshot(probe_runtime=True)
    synthetic_preflight: dict[str, Any] = {}
    try:
        synthetic_preflight = preflight() if preflight is not None else verify_empty_synthetic_environment(_ComposeBackendRunner())
        if not isinstance(synthetic_preflight, dict) or not is_empty_synthetic_environment(synthetic_preflight):
            raise RuntimeError(
                f"synthetic benchmark preflight rejected this instance: {synthetic_preflight.get('unknown_counts') if isinstance(synthetic_preflight, dict) else 'invalid result'}"
            )
        # Injected preflight hooks are used by unit tests. Live CLI runs also
        # bind the HTTP origin to the same backend before minting fixtures.
        if preflight is None and not verify_http_target(_ComposeBackendRunner(), args.api_base):
            raise RuntimeError("HTTP target does not match the preflight backend")
        mint(fixtures)
        for persona in fixtures["users"]:
            user_type_id = persona.get("user_type_id")
            if user_type_id is not None:
                # Journal the override target before mutation so a lost response
                # is still cleanup-safe.
                fixtures["configured_type_ids"].append(int(user_type_id))
            configure_persona_tools(
                args.api_base,
                fixtures["admin"],
                None if user_type_id is None else int(user_type_id),
                journal=fixtures,
            )
            effective = persona_tools_effective(
                args.api_base,
                str(persona["token"]),
                None if user_type_id is None else int(user_type_id),
            )
            harness_failures += 0 if expect(f"{persona['key']}: effective Curated Resources enabled", effective) else 1

        replay_users = [
            persona
            for persona in fixtures["users"]
            if args.persona is None or persona["key"] == args.persona
        ]
        selected_personas = [
            p for p in replay_users
            if args.profile != "smoke" or args.persona is not None or p["key"] == "generic_user"
        ]
        selected_languages = [l for l in REPLAY_LANGUAGES if args.language is None or l == args.language]
        selected_modalities = [m for m in CONTACT_MODALITIES if args.modality is None or m == args.modality]
        selected_journeys = [j for j in ("changed", "unchanged") if args.journey is None or j == args.journey]
        if args.profile == "smoke":
            selected_languages = [args.language or "en"]
            selected_modalities = [args.modality or "email"]
            selected_journeys = [args.journey] if args.journey else ["changed", "unchanged"]
        if not args.inventory_only and selected_personas:
            fixtures["resource_ids"].append(rid)
            resource(args.api_base, fixtures["admin"], rid, baseline)
        for persona_index, persona in enumerate(selected_personas):
            key = str(persona["key"]); token = str(persona["token"])
            if args.inventory_only:
                continue
            for language_index, language in enumerate(selected_languages):
                for journey in selected_journeys:
                    for contact_index, modality in enumerate(selected_modalities):
                        source_before, source_after = (baseline, updated) if journey == "changed" else (updated, updated)
                        resource(args.api_base, fixtures["admin"], rid, source_before, "PUT")
                        sid = str(uuid.uuid4()); sessions.append((token, sid))
                        first_prompt = contact_prompt(language, modality, turn=1)
                        first, first_trace, returned_sid = run_turn(args.api_base, token, {"message": first_prompt, "tools": ["curated-resources"], "session_id": sid}, bool((persona_index + language_index + contact_index) % 2), args.timeout)
                        if returned_sid != sid:
                            harness_failures += 1
                            raise RuntimeError(f"initial session id mismatch: expected {sid}, got {returned_sid}")
                        first_dimensions = score_contact_dimensions(first, first_trace, expected=source_before[modality], old_contacts=source_after if journey == "changed" else baseline, lookup_required=True, modality=modality)
                        first_ok = bool(first_dimensions["quality_passed"])
                        base_id = journey_case_id(key, language, journey, modality, 1)
                        case_id = base_id
                        journey_identity = f"contact::{key}::{language}::{journey}::{modality}"
                        quality_failures += 0 if first_ok else 1
                        evidence.append(evidence_entry(persona=key, case=f"{journey}_{modality}_turn1", case_id=case_id, journey_id=journey_identity, turn_index=1, answer=first, trace=first_trace, passed=first_ok, dimensions=first_dimensions, prompt=first_prompt, context={"journey": journey, "turn": 1, "modality": modality, "source_before": "baseline" if journey == "changed" else "updated", "source_after": "baseline" if journey == "changed" else "updated", "between_turn_mutation": {"applied": False}}, detail=json.dumps(first_dimensions, ensure_ascii=False, sort_keys=True)))
                        if journey == "changed":
                            resource(args.api_base, fixtures["admin"], rid, source_after, "PUT")
                        second_prompt = contact_prompt(language, modality, turn=2)
                        second, second_trace, followup_sid = run_turn(args.api_base, token, {"message": second_prompt, "tools": ["curated-resources"], "session_id": sid}, bool((persona_index + language_index + contact_index + 1) % 2), args.timeout)
                        if followup_sid != sid:
                            harness_failures += 1
                            raise RuntimeError(f"follow-up session id mismatch: expected {sid}, got {followup_sid}")
                        second_dimensions = score_contact_dimensions(second, second_trace, expected=source_after[modality], old_contacts=baseline, lookup_required=journey == "changed", modality=modality)
                        second_ok = bool(second_dimensions["quality_passed"])
                        base_id = journey_case_id(key, language, journey, modality, 2)
                        case_id = base_id
                        quality_failures += 0 if second_ok else 1
                        evidence.append(evidence_entry(persona=key, case=f"{journey}_{modality}_turn2", case_id=case_id, journey_id=journey_identity, turn_index=2, answer=second, trace=second_trace, passed=second_ok, dimensions=second_dimensions, prompt=second_prompt, context={"journey": journey, "turn": 2, "modality": modality, "initial_prompt": first_prompt, "initial_answer": first, "source_before": "baseline" if journey == "changed" else "updated", "source_after": "updated", "between_turn_mutation": {"applied": journey == "changed", "from": "baseline" if journey == "changed" else "updated", "to": "updated"}}, detail=json.dumps(second_dimensions, ensure_ascii=False, sort_keys=True)))

        if not args.contact_only and selected_personas:
            # Remove the contact record before inventory so broad backend totals
            # cannot accidentally include the contact fixture.
            if not args.inventory_only and not cleanup_resource(args.api_base, fixtures["admin"], rid):
                harness_failures += 1
                raise RuntimeError("contact fixture could not be isolated before inventory phase")
            if not args.inventory_only:
                fixtures["resource_ids"].remove(rid)
            for index, inventory_id in enumerate(inventory_ids):
                fixtures["resource_ids"].append(inventory_id)
                resource(args.api_base, fixtures["admin"], inventory_id, {"email": f"directory-{index}@example.test"}, name=INVENTORY_NAMES[index], display_order=index)
            inventory_language = args.language or "en"
            for persona_index, persona in enumerate(selected_personas):
                key = str(persona["key"]); token = str(persona["token"]); inventory_sid = str(uuid.uuid4()); sessions.append((token, inventory_sid))
                inventory_prompt = (
                    "Lista los recursos curados cuyos nombres empiezan con 'Directory Sample'. No supongas que la primera página está completa. Responde en español."
                    if inventory_language == "es"
                    else "List the Curated Resources whose names start with 'Directory Sample'. Do not assume the first bounded page is complete."
                )
                first_page, first_trace, inventory_returned_sid = run_turn(args.api_base, token, {"message": inventory_prompt, "tools": ["curated-resources"], "session_id": inventory_sid}, bool(persona_index % 2), args.timeout)
                if inventory_returned_sid != inventory_sid:
                    harness_failures += 1; raise RuntimeError(f"inventory session id mismatch: expected {inventory_sid}, got {inventory_returned_sid}")
                ok, detail = score_inventory_turn(first_page, first_trace, final_name=INVENTORY_NAMES[-1], continuation=False)
                quality_failures += 0 if ok else 1
                evidence.append(evidence_entry(persona=key, case="inventory_page1", case_id=inventory_case_id(key, inventory_language, "page1"), journey_id=f"inventory::{key}::{inventory_language}", turn_index=1, answer=first_page, trace=first_trace, passed=ok, prompt=inventory_prompt, context={"phase": "inventory_isolated", "contact_fixture_present": False, "page": 1}, detail=detail))
                continuation_prompt = "Muestra la siguiente página de esos recursos." if inventory_language == "es" else "Show the next page of those resources."
                next_page, next_trace, continuation_sid = run_turn(args.api_base, token, {"message": continuation_prompt, "tools": ["curated-resources"], "session_id": inventory_sid}, not bool(persona_index % 2), args.timeout)
                if continuation_sid != inventory_sid:
                    harness_failures += 1; raise RuntimeError(f"continuation session id mismatch: expected {inventory_sid}, got {continuation_sid}")
                ok, detail = score_inventory_turn(next_page, next_trace, final_name=INVENTORY_NAMES[-1], continuation=True, previous_answer=first_page, previous_trace=first_trace)
                quality_failures += 0 if ok else 1
                evidence.append(evidence_entry(persona=key, case="inventory_page2", case_id=inventory_case_id(key, inventory_language, "page2"), journey_id=f"inventory::{key}::{inventory_language}", turn_index=2, answer=next_page, trace=next_trace, passed=ok, prompt=continuation_prompt, context={"phase": "inventory_isolated", "contact_fixture_present": False, "page": 2, "initial_answer": first_page}, detail=detail))
            fixtures["resource_ids"].append(rid)
            resource(args.api_base, fixtures["admin"], rid, updated)

        if not args.inventory_only and args.profile == "full" and (args.persona is None or args.persona == "generic_user") and args.modality is None and args.journey is None:
            generic = next(p for p in selected_personas if p["key"] == "generic_user")
            configure_persona_tools(
                args.api_base,
                fixtures["admin"],
                None,
                [],
                journal=fixtures,
            )
            disabled_effective = not persona_tools_effective(
                args.api_base,
                str(generic["token"]),
                None,
            )
            harness_failures += 0 if expect("disabled tools: effective policy is disabled", disabled_effective) else 1
            disabled_sid = str(uuid.uuid4())
            sessions.append((str(generic["token"]), disabled_sid))
            prompt = "Do not use tools. For Northbridge Legal Aid, what email address is available?"
            answer, trace, returned_disabled_sid = run_turn(args.api_base, str(generic["token"]), {"message": prompt, "tools": [], "session_id": disabled_sid}, False, args.timeout)
            if returned_disabled_sid != disabled_sid:
                raise RuntimeError(f"disabled session id mismatch: expected {disabled_sid}, got {returned_disabled_sid}")
            dimensions = score_contact_dimensions(answer, trace, expected=updated["email"], old_contacts=baseline, lookup_required=False, tool_enabled=False, modality="email")
            ok = bool(dimensions["quality_passed"]); quality_failures += 0 if ok else 1
            evidence.append(evidence_entry(persona="generic_user", case="disabled_tools_no_invented_contact", case_id="control::generic_user::no_tools::email", journey_id="control::generic_user::no_tools", turn_index=1, answer=answer, trace=trace, passed=ok, dimensions=dimensions, prompt=prompt, context={"tools_enabled": False}, detail=json.dumps(dimensions, ensure_ascii=False, sort_keys=True)))
        audit = req(args.api_base, fixtures["admin"], "GET", "/admin/deployment/audit-log?limit=500", timeout=30)
        if audit.status_code != 200:
            raise RuntimeError(f"audit lookup returned HTTP {audit.status_code}")
        harness_failures += 0 if expect("Audit Log excludes fine Conversation timing", not audit_contains_fine_timing(audit.json())) else 1
    except Exception as exc:
        print(f"[ERROR] {exc}"); fatal = True; harness_failures += 1; fatal_error_type = type(exc).__name__
    finally:
        if fixtures.get("admin"):
            for index, (token, sid) in enumerate(sessions):
                try:
                    # Keep cleanup below the reverse proxy's request window;
                    # cleanup_session also handles an occasional 429.
                    if index:
                        time.sleep(0.15)
                    if not cleanup_session(args.api_base, token, sid):
                        cleanup_failures += 1; print(f"[FAIL] cleanup session {sid}")
                except Exception as exc:
                    cleanup_failures += 1; print(f"[FAIL] cleanup session {sid}: {exc}")
            for resource_id in fixtures["resource_ids"]:
                try:
                    if not cleanup_resource(args.api_base, fixtures["admin"], resource_id):
                        cleanup_failures += 1; print(f"[FAIL] cleanup resource {resource_id}")
                except Exception as exc:
                    cleanup_failures += 1; print(f"[FAIL] cleanup resource {resource_id}: {exc}")
            for user_type_id in fixtures["configured_type_ids"]:
                try:
                    if not cleanup_persona_tools(
                        args.api_base,
                        fixtures["admin"],
                        int(user_type_id),
                    ):
                        cleanup_failures += 1; print(f"[FAIL] cleanup User Type policy {user_type_id}")
                except Exception as exc:
                    cleanup_failures += 1; print(f"[FAIL] cleanup User Type policy {user_type_id}: {exc}")
            try:
                if not restore_global_tools(args.api_base, fixtures["admin"], fixtures):
                    cleanup_failures += 1; print("[FAIL] restore global Tool policy")
            except Exception as exc:
                cleanup_failures += 1; print(f"[FAIL] restore global Tool policy: {exc}")
        try:
            reconcile_fixture_journal(fixtures)
        except Exception as exc:
            cleanup_failures += 1; print(f"[FAIL] reconcile fixture journal: {exc}")
        try:
            if not cleanup_sage_identities(fixtures["users"]):
                cleanup_failures += 1; print("[FAIL] cleanup Sage identities verification")
        except Exception as exc:
            cleanup_failures += 1; print(f"[FAIL] cleanup Sage identities: {exc}")
        try:
            if not cleanup_personas(fixtures["users"], str(fixtures["suffix"])):
                cleanup_failures += 1; print("[FAIL] cleanup personas verification")
        except Exception as exc:
            cleanup_failures += 1; print(f"[FAIL] cleanup personas: {exc}")
        try:
            if not cleanup_admin(
                str(fixtures["ephemeral_admin_pubkey"]),
                fixtures.get("owns_admin"),
            ):
                cleanup_failures += 1; print("[FAIL] cleanup ephemeral admin verification")
        except Exception as exc:
            cleanup_failures += 1; print(f"[FAIL] cleanup ephemeral admin: {exc}")
        try:
            if not restore_replaced_admins(fixtures):
                cleanup_failures += 1; print("[FAIL] restore replaced admin markers")
        except Exception as exc:
            cleanup_failures += 1; print(f"[FAIL] restore replaced admin markers: {exc}")
        observed_models = [str(item["model"]) for item in evidence if isinstance(item.get("model"), str)]
        missing_model_count = sum(1 for item in evidence if not isinstance(item.get("model"), str) or not item.get("model"))
        runtime_end = runtime_identity_snapshot(observed_models, probe_runtime=True)
        runtime_validation = runtime_identity_validation(runtime_start, observed_models, runtime_end)
        runtime_validation["missing_model_count"] = missing_model_count
        runtime_validation["harness_ok"] = runtime_validation["harness_ok"] and missing_model_count == 0
        if not runtime_validation["harness_ok"]:
            harness_failures += 1
        manifest_end = fixture_manifest(baseline, updated)
        manifest_validation = {
            "start_hash": manifest.get("hash"),
            "end_hash": manifest_end.get("hash"),
            "consistent": manifest_end.get("hash") == manifest.get("hash"),
        }
        if not manifest_validation["consistent"]:
            harness_failures += 1
        summary, cleanup_failures, evidence_error = persist_evaluation_evidence(
            args.evidence_file,
            payload={
                "fatal_error_type": fatal_error_type,
                "persona_filter": args.persona,
                "inventory_only": args.inventory_only,
                "contact_only": args.contact_only,
                "language_filter": args.language,
                "modality_filter": args.modality,
                "journey_filter": args.journey,
                "profile": args.profile,
                "planned_case_ids": expected_ids,
                "runner_code_hash": RUNNER_CODE_HASH,
                "scenario_catalog_hash": hashlib.sha256(
                    json.dumps(expected_ids, separators=(",", ":")).encode("utf-8")
                ).hexdigest(),
                "fixture_schema": "neutral-contact-v3",
                "fixture_manifest": manifest,
                "fixture_manifest_validation": manifest_validation,
                "synthetic_preflight": synthetic_preflight,
                "runtime_identity_start": runtime_start,
                "runtime_identity_end": runtime_end,
                "runtime_identity_validation": runtime_validation,
                "inventory_fixture_count": len(INVENTORY_NAMES),
            },
            expected_case_count=expected_cases,
            evidence=evidence,
            failures=quality_failures,
            cleanup_failures=cleanup_failures,
            fatal=fatal,
            expected_case_ids=expected_ids,
            harness_failures=harness_failures,
        )
        if evidence_error is None:
            print(f"[EVIDENCE] {args.evidence_file}")
        else:
            print(f"[FAIL] write evidence: {evidence_error}")
    print(f"[SUMMARY] status={summary['status']} passed={summary['passed']} profile={args.profile} expected_cases={summary['expected_case_count']} completed_cases={summary['completed_case_count']} quality_failures={quality_failures} harness_failures={harness_failures} cleanup_failures={cleanup_failures}")
    return exit_code_for_summary(summary)

if __name__ == "__main__": raise SystemExit(main())
