# GLM-5.3 Flash migration

The Enclave conversation model is `glm-5-3-flash`, served through the existing
verified Tinfoil proxy. The default reasoning effort is `low`.

This replaces the GLM-5.2 configuration at the operator's request because that
model is being deprecated. Tests and benchmarks were run before changing source
defaults, then repeated with the new configuration. The work started from
Enclave staging `37082ef` and Sage `f41321e` on September 9, 2026.

## Provider compatibility

The [Tinfoil model catalog](https://inference.tinfoil.sh/v1/models), retrieved
through the verified proxy, lists `glm-5-3-flash` with tool calling and reasoning
support. GLM-5.2 was absent from that catalog but still accepted completion
requests during the baseline run.

Keep the model and reasoning settings together. A direct preflight with
`glm-5-3-flash` and the former `reasoning_effort=none` setting returned reasoning
text in the answer's `content` field. The same request with `low` returned the
requested answer in `content` and reasoning separately. Sage already handles
the separate provider reasoning fields without publishing them as answer text.
The [model publisher's instructions](https://github.com/zai-org/GLM-5/blob/main/README.md)
describe `low`, `high`, and `max` thinking budgets for GLM-5.3 Flash.

The migration updates Compose, Python's diagnostic provider, frontend model
examples in every locale, benchmark/smoke defaults, and the pinned Sage runtime's
own defaults and launch instructions. Explicit operator model overrides remain
supported. The Tinfoil endpoint and proxy configuration are unchanged.

## Apply to an existing deployment

In Admin Deployment Settings, save the Model Provider model (`LLM_MODEL`) as
`glm-5-3-flash`. This saved setting takes precedence over the container's
`LLM_MODEL` environment for Python diagnostics and is the source of generated
runtime env exports. Restarting with a new environment does not overwrite an
existing saved value. The local migration used the normal audited admin update
endpoint, `PUT /admin/deployment/config/LLM_MODEL`.

Update both values in the deployment's `.env`:

```dotenv
TINFOIL_MODEL=glm-5-3-flash
TINFOIL_REASONING_EFFORT=low
```

Existing `.env` values override the new repository defaults. If the deployment
also loads exported files from `runtime/generated`, update or regenerate their
model values: `TINFOIL_MODEL` in `sage.env` and `LLM_MODEL` in
`core-backend.env`. A later env file can otherwise restore the old model. Carry
`TINFOIL_REASONING_EFFORT=low` into any operator override that sets reasoning.

After fetching the updated Sage submodule, rebuild and recreate the app services
using the deployment's normal Compose arguments. For the base local profile:

```bash
git submodule update --init runtime/sage
docker compose -f docker-compose.infra.yml -f docker-compose.app.yml up -d --build core-backend sage frontend
docker compose -f docker-compose.infra.yml -f docker-compose.app.yml exec -T sage printenv TINFOIL_MODEL TINFOIL_REASONING_EFFORT
docker compose -f docker-compose.infra.yml -f docker-compose.app.yml exec -T core-backend printenv LLM_MODEL
```

Expected outputs are `glm-5-3-flash`, `low`, and `glm-5-3-flash`, respectively.
Reapply the appropriate `--env-file` arguments when using generated deployment
files. Verify a fresh conversation as well as the runtime configuration; a
successful model-list health check alone does not exercise inference. Check that
`GET /llm/test` also reports `model: "glm-5-3-flash"`; this catches an old saved
diagnostic-model override that environment inspection alone misses.

## Verification results

The original measured candidate pinned Sage `0f24926` ([Sage PR #55](https://github.com/enclave-free/sage/pull/55)).
The local stack was rebuilt and its Sage environment, backend environment, and
`GET /llm/test` all verified as `glm-5-3-flash`; Sage uses `low` reasoning.
Raw local command logs are in `/tmp/enclave-glm53-results`.

| Check | Before | After |
| --- | --- | --- |
| Backend, fresh image | 435 passed, 1 error, 1 skipped | 435 passed, same error, 1 skipped |
| Frontend | 487 passed, 1 timeout; affected file's 6 tests passed on rerun | 488 passed |
| Frontend production build | Passed | Passed |
| Sage host workspace | 256 passed | 256 passed |
| Sage host Clippy and formatting | Passed | Passed |
| Benchmark unit tests | 67 passed | 67 passed |
| Script unit tests | 53 passed | 53 passed |
| Compose contracts | 3 passed | 3 passed |
| Frontend HTTP contracts | 5 passed | 5 passed |
| Integration runner with local harness adjustments | 14/18 scripts passed | 14/18 scripts passed; same failure categories |
| Direct chunk retrieval | Functional checks passed; cleanup failed | Functional checks passed; same cleanup failure |
| Standalone Sage Linux smoke | Endpoint-retry unit test failed | Same test failed |

### Live model comparison

| Measurement | GLM-5.2 / `none` | GLM-5.3 Flash / `low` |
| --- | ---: | ---: |
| Conversation benchmark completed turns | 12/12 | 12/12 |
| Automated conversation scenarios passed | 9/10 | 8/10 |
| Median first visible answer | 2.767 s | 2.126 s |
| Median completed turn | 3.567 s | 3.307 s |
| Conversation warning checks | 13 | 8 |
| Contact-refresh cases completed | 41/41 | 41/41 |
| Contact-refresh cases passed | 7/41 | 4/41 |
| Contact-refresh cleanup failures | 0 | 0 |
| Legacy benchmark completed turns | 25/25 | 25/25 |
| Legacy summed response time | 254.5 s | 162.4 s |
| Legacy median response time | 6.8 s | 5.4 s |

The GLM-5.2 conversation failure was failure to surface the seeded vetted
resource. GLM-5.3 Flash passed that scenario, but failed the exact-wording checks
in the knowledge-only and combined knowledge/resource scenarios. Inspection of
those two answers shows safe-place and trusted-people guidance expressed with
different wording. Their automated failure scores are retained, without changing
the benchmark checks. Both consent-boundary answers explicitly refused covert
documentation, although the benchmark's lexical warning still fired for both.

The contact-refresh evaluation is a substantive unresolved issue: both models
often repeated stale contact data instead of refreshing it through the resource
tool. The new model's 4/41 score is worse than the baseline's 7/41 in this run.
This migration therefore demonstrates lower observed latency, not an overall
quality improvement or a clean release gate. The requested deprecation migration
proceeds with these results disclosed.

All five separate admin timing scenarios completed:

| Admin scenario | Before: first visible / done | After: first visible / done |
| --- | ---: | ---: |
| No tools | 0.919 / 1.160 s | 0.485 / 0.638 s |
| Setup summary | 3.603 / 3.628 s | 2.429 / 2.455 s |
| Config only | 2.262 / 2.285 s | 2.059 / 2.087 s |
| Natural-language database query | 6.069 / 6.148 s | 4.285 / 4.362 s |
| Direct database select | 1.848 / 1.875 s | 1.004 / 1.026 s |

### Evidence and commands

- [Machine-readable comparison](https://github.com/enclave-free/enclave.free/blob/b37cbe1f0cace0669ae8c4b9522df7dc9b920474/docs/agents/runs/artifacts/glm-5-3-flash/comparison.json)
- Conversation artifacts: [before](https://github.com/enclave-free/enclave.free/blob/b37cbe1f0cace0669ae8c4b9522df7dc9b920474/docs/agents/runs/artifacts/glm-5-3-flash/baseline-conversation.json), [after](https://github.com/enclave-free/enclave.free/blob/b37cbe1f0cace0669ae8c4b9522df7dc9b920474/docs/agents/runs/artifacts/glm-5-3-flash/candidate-conversation.json)
- Contact artifacts: [before](https://github.com/enclave-free/enclave.free/blob/b37cbe1f0cace0669ae8c4b9522df7dc9b920474/docs/agents/runs/artifacts/glm-5-3-flash/baseline-contact-eval.json), [after](https://github.com/enclave-free/enclave.free/blob/b37cbe1f0cace0669ae8c4b9522df7dc9b920474/docs/agents/runs/artifacts/glm-5-3-flash/candidate-contact-eval.json)
- Admin timing: [before](https://github.com/enclave-free/enclave.free/blob/b37cbe1f0cace0669ae8c4b9522df7dc9b920474/docs/agents/runs/artifacts/glm-5-3-flash/baseline-admin-timing.json), [after](https://github.com/enclave-free/enclave.free/blob/b37cbe1f0cace0669ae8c4b9522df7dc9b920474/docs/agents/runs/artifacts/glm-5-3-flash/candidate-admin-timing.json)
- [Verified provider preflight](https://github.com/enclave-free/enclave.free/blob/b37cbe1f0cace0669ae8c4b9522df7dc9b920474/docs/agents/runs/artifacts/glm-5-3-flash/provider-preflight.json) and [model catalog snapshot](https://github.com/enclave-free/enclave.free/blob/b37cbe1f0cace0669ae8c4b9522df7dc9b920474/docs/agents/runs/artifacts/glm-5-3-flash/tinfoil-models.json)

Principal commands, run before and after in the isolated worktree:

```bash
python -m unittest discover -s backend/tests
python -m unittest discover -s scripts/benches
python -m unittest discover -s scripts/tests
python scripts/tests/DEPLOYMENT/test_frontend_compose_contract.py
FRONTEND_RUNTIME_URL=http://127.0.0.1:5173 python scripts/tests/DEPLOYMENT/test_frontend_http.py
npm --prefix frontend test
npm --prefix frontend run build
cargo test --workspace --manifest-path runtime/sage/Cargo.toml
cargo clippy --workspace --all-targets --all-features --manifest-path runtime/sage/Cargo.toml -- -D warnings
cargo fmt --all --manifest-path runtime/sage/Cargo.toml -- --check
bash runtime/sage/scripts/smoke_tinfoil.sh
python scripts/tests/run_all_be_tests.py --api-base http://localhost:18000 --no-restore
python scripts/tests/TOOLS/test_5d_chunk_retrieval_gateway_smoke.py --api-base http://localhost:18000
python scripts/benches/conversation_model_bench.py --seed-knowledge --seed-resources --output /tmp/conversation-result.json
python scripts/run_benchmark.py
python scripts/tests/TOOLS/measure_admin_conversation_timing.py --output /tmp/admin-timing.json
```

Backend tests also ran in the freshly built image with this worktree mounted
read-only. Host Rust commands used Homebrew's libpq linker path. Live commands
used a temporary Docker wrapper that added a Compose override for uniquely named
`enclaveglm53-*` containers and corrected only the obsolete restart health URL.
The integration harness retained disposable test state with `--no-restore`.
The legacy benchmark received an ephemeral signed user token and cleaned up its
user fixture; no auth tokens are included in these evidence files.

Baseline limitations recorded before edits:

- The host backend suite ran 437 tests: 435 passed, one skipped, and one failed
  because the pre-existing virtual environment contains `accelerate`.
- A fresh backend image also ran 437 tests: 435 passed, one skipped, and one
  errored because a test accesses `.path` on FastAPI 0.141.1's `_IncludedRouter`.
  The host virtual environment uses FastAPI 0.136.3.
- The frontend suite had 487 passes and one five-second timeout. All six tests
  in the affected file passed on a focused rerun. Its production build passed.
- All 256 host Sage tests, formatting, and Clippy checks passed. The standalone
  Linux smoke script failed the existing endpoint-retry contract test, including
  when run serially. It stopped before its provider/memory smoke stages.
- All 67 conversation-benchmark unit tests, 53 script unit tests, three Compose
  checks, and five frontend HTTP checks passed.
- The integration runner needs port 18000 in its restart probe. A temporary
  wrapper corrected the old port 8000 probe for this isolated test stack. The
  rerun had 14 passing scripts and four failures: contact-quality evaluation,
  missing user-type ID for the chunk smoke, and unsupported `--api-base`
  arguments for the two deployment test scripts. The deployment scripts passed
  when run directly. Direct chunk retrieval passed its functional checks but
  failed fixture cleanup on a foreign-key constraint.
- Database restoration by the old integration harness changed the copied
  database's ownership. Ownership was repaired in the disposable test stack;
  subsequent runs retained test state there instead of invoking that restore.

Live benchmark results are single-run observations with possible provider cache
effects, not a statistically powered latency or reliability claim. The legacy
multi-session benchmark ran with grading disabled because no grading credential
was configured; completing those conversations is not a quality-pass score.

## Review packaging refinement

The original provider catalog, preflight, and before/after evidence are preserved at the immutable migration commit linked above. Generated run dumps are excluded from the current PR diff. This packaging change does not rerun or rescore the migration cohort.

## Second migration review

The revised candidate pins Sage `07fcd7850a6e68a7a880373ffad5ab2f292e4e2c`. Flash now rejects unsupported legacy reasoning settings (`none`, `minimal`, `medium`, `xhigh`) at startup while accepting `low`, `high`, and `max`. Custom models retain their existing explicit effort overrides. Sage's configuration owns the `low` deployment default; the generic native-client enum default stays unchanged at `none`. ADR-0031 now labels its GLM 5.2 / `none` decision as historical.

The application response-integrity smoke sends the configured reasoning effort and requires the requested model, a finished response, and nonempty content. Tests reject missing/wrong identity, length-limited/empty answers, and incomplete bodies. A Compose contract verifies operator model/effort overrides reach both runtimes. These two app suites pass **10 tests**.

Sage's standalone smoke additionally forwards stdin (`docker run -i`) into the embedded Python checks. Previously those checks could execute no Python; their apparent success must not be used as provider evidence. Provider checks now run before the unrelated Linux workspace suite. Host verification passed **256 tests**, Clippy with all targets/features and warnings denied, and formatting.

The corrected isolated live smoke **failed** after bounded retries on truncated HTTP bodies (`IncompleteRead: 1312 bytes received, 20 more expected`). It did not certify live model identity or reach the memory/workspace stages. This is a current verification blocker, not a green smoke or a newly measured model-quality regression. All owned smoke containers, networks, volumes, and image were removed. Earlier before/after cohorts above remain historical evidence; no production rollout was performed.

## Truncated-response blocker resolved

The Sage pin is now `22d342dacc0b4c180b847c5526af9a8068c724b0`. The verification failure came from harness drift: Sage standalone Compose and smoke still used `tinfoil-cli:latest proxy`, while the application already used `tinfoil-proxy:0.1.6`. The same minimal GLM request repeatedly failed through the CLI proxy with exactly 20 body bytes missing; requesting identity encoding did not help. Through the pinned standalone proxy, three requests completed with exact Content-Length, the requested model, and finish_reason=stop. The strict application response-integrity smoke also passed live.

The fix changes two Sage files (6 additions, 4 deletions) to use the application's proxy image/arguments, including the internal host allowlist. No truncation tolerance or weaker completion check was added. Rendered Sage Compose and Bash syntax checks passed. The actual standalone smoke now passes chat and embeddings; it subsequently fails on its separate default vision model returning HTTP 404. That vision configuration is unchanged, so the full multi-model smoke is not claimed green. All diagnostic and smoke containers were removed. The [standalone proxy](https://github.com/tinfoilsh/tinfoil-proxy/) retains verified enclave transport.

## Explicit Enclave verification mode

The Sage pin is now `73b2a792708122bbeee47f89460b2722ab0e9be2`. Run
`just smoke-tinfoil enclave` in Sage for this Deployment. Full Sage verification
remains the default (`just smoke-tinfoil`); Enclave mode excludes only the
messenger vision provider check and prints `NOT TESTED vision`. All workspace
and database checks remain mandatory in both modes. This is provider and shared
memory verification, not a full authenticated web Conversation/session test.

The first Enclave run reproduced the existing Linux endpoint-retry test failure.
The test constructed a fresh HTTP client inside each short request deadline.
Moving client construction into test setup fixed the failure without changing
production code, timeout values, or assertions. The retry contract then passed
10 consecutive Linux runs.

Verification of this revision:

- Enclave smoke exited 0: chat completion, 768-dimensional embeddings, invalid-model
  rejection, fresh migrations, recall before/after embedding, and tagged archival
  retrieval passed.
- All 256 containerized workspace tests, workspace compilation, and all-target/
  all-feature Clippy with warnings denied passed. Rust formatting and Bash syntax
  checks passed.
- Four offline smoke-mode tests passed, covering exclusion reporting, full-mode
  vision failures, shared embedding failures, and argument validation. Final
  review caught that routine gates did not invoke them; a dedicated workflow
  now covers all PR target branches, and `just ci-check` runs the same command.
- Full Sage smoke was rerun through the default command and exited 1 on the
  unchanged vision-model HTTP 404, after passing chat and embeddings. Vision is
  untested in Enclave mode; its availability has not been repaired.
- All containers, networks, volumes, and images created by these smoke runs were
  removed. The six application response-integrity unit tests also passed.

This removes the Enclave provider/storage verification blocker. The earlier
consent and contact-refresh quality findings remain unresolved; this result does
not certify Deployment Readiness. No merge or deployment was performed.
