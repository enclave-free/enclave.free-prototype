# Benchmark measurements and review

The September 2026 overhaul separates execution, deterministic contracts, semantic review, safety, and latency. A completed answer is not a quality pass. Missing review remains `unreviewed`; a supported safety failure blocks the reviewed gate even when other dimensions pass.

## What each suite measures

| Suite                        | Unit and scope                                                                             | Appropriate interpretation                                                                                |
| ---------------------------- | ------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------- |
| Conversation Model Bench     | 12 scenarios, 19 turns per model/repetition; natural and explicit contract prompts         | Runtime contracts plus reviewable whole conversations                                                     |
| Natural conversation corpus  | 5 independent journeys, 25 ordinary user questions                                         | Continuity, autonomy, relevance, grounding, and useful next steps; expert criteria review pending         |
| Curated contact matrix, full | 80 independent two-turn journeys, 8 inventory cases, 1 disabled-tool control: 169 requests | Exact current pointer, lookup behavior, stale reuse, inventory coverage, and harness integrity separately |
| Curated contact smoke        | 2 two-turn email journeys plus 2 inventory cases: 6 requests                               | Fast harness smoke, not full contact coverage                                                             |
| Offline evaluator tests      | Synthetic positive, negative, malformed, and saved failure evidence                        | Evaluator correctness, not model quality                                                                  |

Natural questions avoid prescribing an answer or a tool. Explicit contract probes remain useful for permissions, persistence, confirmation, and exact contacts, but they are not matched natural/contract quality experiments. The old three golden answers and automatic OpenAI grading are removed. Each natural turn instead names required and forbidden behavior. Review only the current and previous turns; future questions cannot supply missing context.

The resource matrix uses a neutral organization name and opaque randomized pointers. Each changed-source journey has its own session and mutation. Unchanged controls permit reuse without imposing an unnecessary lookup. Email, website, phone, address, and secure-contact cases run in English and Spanish for each configured persona. Inventory fixtures run apart from the contact fixture. A justified display subset is distinguished from backend pagination totals. Exact pointer checks reject reconstructed or misleadingly extended addresses and URLs; stale text is retained for semantic interpretation rather than silently treated as a recommendation.

## Run on a disposable synthetic stack

Use a dedicated local Docker Compose project with its own volumes and gateway port. The runners restrict HTTP targets to loopback, check for non-benchmark database rows, and bind the HTTP origin to the inspected backend with a disposable synthetic identity before real execution. This check is a guard, not anonymization: synthetic prefixes do not prove provenance. Never point these tools at an instance containing customer data or re-label customer records to pass preflight. Clean up only the disposable project you created. Do not use a production instance for admin-write scenarios.

From the repository root with its Python environment activated:

```bash
python -m unittest discover -s scripts/benches -p 'test_*.py'
python -m unittest discover -s scripts/tests -p 'test_*.py'

python scripts/benches/conversation_model_bench.py \
  --models glm-5-2,glm-5-3-flash --repeat 2 \
  --seed-knowledge --seed-resources \
  --output /tmp/conversation-comparison.json

python scripts/tests/TOOLS/test_5h_curated_resource_contact_model_eval.py \
  --profile full --evidence-file /tmp/contact-full.json

python scripts/run_benchmark.py --list
python scripts/benches/run_natural_corpus.py \
  --model glm-5-3-flash --output /tmp/natural-conversations.json
```

The natural corpus command creates an ephemeral user/token and the exact seeded knowledge/resource manifest, runs the corpus, then cleans up its fixtures. The manifest is retained and hashed for grounding review; ambient source fixtures are rejected. Every runner that owns setup requires an initially empty user/resource/document database, including rows left by earlier synthetic runs. For an existing controlled synthetic fixture setup, the lower-level `scripts/run_benchmark.py` accepts `--fixture-manifest PATH` and `--token-env NAME`. Keep tokens out of command arguments and artifacts. Sessions are fresh per journey and deleted afterward.

Main-bench user tokens and seeded knowledge/resources are also owned and cleaned up by the runner. The fictional Cedar follow-up requires `--seed-knowledge`; omitting its source fails before provider dispatch. Current-model runs and Apple Containers options are covered in the [runtime guide](conversation-model-bench.md).

The contact evaluator runs one complete matrix per invocation; its partial `--repeat` option was removed. Use separate output files for independent contact cohorts. The balanced conversation and natural-corpus runners retain their repetition options. Run `--help` for selectors and timeout options. Save each attempt to a distinct output path, including failures. Do not replace a failed cohort with a successful retry without reporting both. Live calls consume the configured provider budget; CI uses no provider credentials or live calls.

## Review the evidence

The canonical report retains full synthetic requests and answers, prior tool evidence, observed model/session identity, fixture definitions, per-turn criteria, timing, planned denominators, and cleanup outcomes. Raw provider bodies, credentials, and arbitrary retrieval payloads are not review artifacts. Natural report v3 uses top-level `run` and `candidates`; duplicate `runs` and nested `artifact` copies were removed. Resource v3 reports are adapted by the same review CLI; historical preview-only reports cannot be certified.

```bash
python scripts/benches/review_bench.py template /tmp/conversation-comparison.json \
  --output /tmp/conversation-review.json
# Fill each entry's reviewer, method, reviewed_at, and dimension judgments.
python scripts/benches/review_bench.py apply /tmp/conversation-comparison.json \
  --review /tmp/conversation-review.json --output /tmp/conversation-reviewed.json
```

Every turn has safety, grounding, relevance, and usefulness dimensions. Explain each verdict; failures require at least one exact quotation from the current model answer; additional context quotations may support it. Faithful paraphrases may pass. Indirect consent workarounds can fail even after a polite refusal. Review hashes bind the complete artifact, rubric, and per-turn context. Modified answers, duplicate or unknown entries, missing evidence, fabricated quotations, and invalid review metadata cannot pass. An agent may document failures but cannot certify passes.

Reviewer names are declared provenance, not authenticated credentials. Ordinary content review does not clear a corpus's pending expert review. Qualified maintainers must review and version advice criteria separately before changing that metadata; do not infer expert approval from a model grade or editing date.

Automatic model judging and calibration are deferred. The supported review methods are `human` and `agent`; agents may record supported failures, but only human review can certify a pass. Old `calibrated_model` packets are rejected. This keeps one structured review workflow and avoids treating engineering calibration as expert approval.

## Interpret results

| Field             | Meaning                                                                                                                            |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `execution`       | Completed / attempted / planned turns; missing journeys remain in the denominator                                                  |
| `contracts`       | Objective invariants, returned identity, fixture/session cleanup, and harness failures                                             |
| `semantic_review` | Fully reviewed turns / planned turns, failures, and invalid-review errors                                                          |
| `safety`          | Separate supported safety failures; never averaged away                                                                            |
| `release_gate`    | Blocked, unreviewed, expert review pending, or passed; not a deployment action                                                     |
| `timing`          | Completed-turn sample counts and descriptive p50/p90/p95, min/max by model                                                         |
| `paired_timing`   | Whole-journey deltas only with balanced repeated order, matching known runtime settings, and matching scenario/fixture definitions |

Two-model runs alternate A/B then B/A. An even repetition count is required for the paired comparison. Pairing excludes incomplete journeys and reports them separately. Changed reasoning budgets, unknown fixture semantics, or missing ordering evidence withhold the automated comparison. Small cohorts do not establish reliability or quality parity. Turns within a conversation are correlated; do not count them as independent quality samples. Provider caching and warm-up remain potential confounders. Percentile tails with small sample counts are descriptive only; the observed delta range is not a confidence interval.

## Keep measurements current

Repository maintainers own the engineering corpus; the current version is `2026-09-10.2`, with expert advice review explicitly pending. Review the catalog monthly and whenever the main model, system prompt, tools, policies, source fixtures, or observed failures change. Record actual reviewer, date, change rationale, and source authority; a touched file is not a reviewed corpus.

Keep stable case IDs for comparable questions. Change scenario, fixture, or rubric versions when semantics change, preserve old artifacts, and start a new comparison cohort. Add regressions from confirmed failures after converting them into synthetic cases. Keep a held-out review set separate from prompt tuning. Report exclusions, incomplete reviews, and unresolved product failures alongside successful measurements.

The [modernization report](agents/runs/2026-09-10-benchmark-modernization.md) records this rollout's exact verification and limitations. The [quality audit](glm-5-3-flash-quality-audit.md) preserves why the old aggregate results were misleading.
