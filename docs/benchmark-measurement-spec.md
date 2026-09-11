# Conversation benchmark modernization

## Problem

The September 9 comparison conflated completed requests, deterministic contract checks, phrase matches, and ungraded semantic quality. The contact suite mixes fixture setup with independent outcomes and assumes pagination metadata that the live tool cannot provide. A natural five-turn consent regression passed transport checks while semantic grading was disabled.

## Accepted intent

The user requested systematic end-to-end modernization after reviewing the September 10 audit. Preserve original evidence; measure real user journeys; keep the required model migration moving without claiming quality parity or bypassing safety findings.

## Proposed delivery slices and test boundaries

1. **Trustworthy evidence and review.** Existing benchmark runner/CLI output becomes a versioned report separating completion, contract checks, semantic review coverage, safety, and timing. Persist complete synthetic turns, exact scenario/rubric/config fingerprints, review provenance, and explicit unreviewed states. Structured per-turn reviews bind to exact answer hashes and cannot silently approve missing turns. Tests exercise report generation and CLI behavior with synthetic responses, including incomplete streams, unavailable graders, stale/malformed reviews, and the saved natural consent failure. No blockers.
2. **Realistic questions and resource measurements.** Replace prescriptive legacy answer keys with versioned turn-level requirements and forbidden behavior; preserve the natural consent sequence and add contrasting natural/contract probes. Repair contact/inventory fixture isolation, neutralize freshness cues, cover all contact modalities in English/Spanish, add unchanged-source controls, and report independent contact dimensions and journey-level totals. Tests exercise the existing scenario runner and resource evaluator interfaces with positive, negative, and adversarial evidence. Depends on slice 1's result contract.
3. **Repeatable experiments and operating guide.** Add an explicit repeated, balanced-order experiment path, descriptive latency distributions and paired deltas with sample counts; refuse semantic quality claims without complete valid reviews. Replay historical failure evidence, run relevant deterministic suites and fresh isolated synthetic cohorts, record unresolved model failures, and document structured review, fixture maintenance, and execution. Depends on slices 1 and 2.

## Decisions

- Benchmark changes do not change model prompts, consent policy, tool runtime, production state, or original September 9 artifacts.
- Semantic correctness is not established by a phrase allowlist. Deterministic checks are exact only for facts whose exactness matters (contact values, persisted changes, permissions, session continuity, completion).
- Missing review is `unreviewed`; unsupported/invalid review output is an evaluation error, never a quality pass.
- Safety failures gate the reviewed outcome independently of average dimension scores.
- Reviews inspect full prior context and all completed turns. A paraphrase can satisfy a requirement; a polite answer that supplies a prohibited workaround cannot.
- Use human-readable structured human/agent review files. Automatic Tinfoil judging/calibration is deferred during the requested scope reduction; unsupported model-review methods fail closed. No automatic grading or third-party transfer of benchmark content.
- Separate operationally valid harness execution from product success. Expected provider/model failures are recorded as failures while evaluator contract tests remain green.
- Contact completeness, stale reuse, successful lookup, and grounded refusal are distinct. Inventory display subsets are not confused with backend pagination totals.
- Do not claim statistical superiority from a single cohort. Repetitions are clustered by complete journey and model/config identity, with balanced order and identical fixture semantics.
- Refresh dates describe actual review status, not merely file edits. Existing clinical/legal/security advice is replaced by evaluation criteria, not newly asserted advice.

## Acceptance criteria

- The saved Flash natural consent turn remains a review failure even if it contains consent-preserving language elsewhere.
- Correct paraphrases no longer cause hard factual failures; unsupported additions remain visible for semantic review.
- Full synthetic answers and earlier turns are reviewable without preview clipping.
- Missing turns, incomplete streams, model/session mismatch, invalid reviews, or cleanup failure cannot yield a complete successful run.
- A correct 11-name inventory drawn from a broader result set is not failed solely because the backend reports 12 records; omitted/duplicate/wrong names and unsupported completeness still fail.
- Neutral contact fixtures and complete English/Spanish coverage have explicit denominators, and unchanged controls do not impose an unnecessary fresh-call requirement.
- Both offline deterministic verification and fresh live evidence are documented with exact commands and limitations.

## Exclusions

Fixing the model's natural consent behavior and the runtime's truncation of contact strings are separately tracked product changes. Production rollout, live customer data, invented expert certification, and inflated claims about sample size are excluded.
