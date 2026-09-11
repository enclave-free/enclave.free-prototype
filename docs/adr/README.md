# Architecture Decision Records

This directory records accepted product and architecture decisions for the Enclave Free Prototype. ADR-0023 defines Conversation Tool ownership and Tool Set boundaries, ADR-0029 owns the native Tool-call hard cut, ADR-0030 owns the bounded native Tool loop, ADR-0031 owns the explicit GLM reasoning-effort default, ADR-0032 owns shared User Conversation behavior for Test User Sessions, ADR-0033 owns model-led autonomy and concise User responses, ADR-0034 owns audience-aware Conversation Activity presentation, and ADR-0028 owns direct Admin Config write behavior.

## Review Ledger

Reviewed on 2026-06-15 for the unified model-driven Tool loop hard cut:

| ADR                                                                     | Impact                                                                                                            |
| ----------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| 0001 Sage owns Agent Runtime while Python retains Enclave Control Plane | Still valid; tool choice language is reinforced by ADR-0023.                                                      |
| 0002 Privacy means operator control, not offline-only operation         | No change required.                                                                                               |
| 0003 Model Providers must support encrypted verifiable inference        | Still valid; ADR-0029 additionally requires the configured Conversation models to support native Tool calls.      |
| 0004 Admin Conversations can apply confirmed control-plane changes      | Superseded for Admin Config writes by ADR-0028; it remains relevant to state-changing actions outside that boundary. |
| 0005 User Reachout is outside Conversations                             | No change required.                                                                                               |
| 0006 Retention and deletion are operator-controlled but incomplete      | No change required.                                                                                               |
| 0007 Audit Log is a product boundary but coverage is partial            | Still valid; `db-query` remains read-only and confirmed writes remain auditable.                                  |
| 0008 Replace Documents only after successful ingestion                  | No change required.                                                                                               |
| 0009 User Memory is low-sensitivity Sage context                        | No change required.                                                                                               |
| 0010 Session Memory Deletion uses retryable tombstones                  | No change required; route names remain API compatibility details.                                                 |
| 0011 Minimize Retrieval Index and encrypt chunk text                    | Still valid; ADR-0023 changes how Retrieval enters Conversations, not the storage boundary.                       |
| 0012 Conversations require current Verifiable Inference                 | No change required.                                                                                               |
| 0013 Conversation Traces are sanitized product metadata                 | Superseded by ADR-0024's transparent trace posture.                                                               |
| 0014 Sage owns tool-aware Conversation Streaming Transport              | Superseded for future tool-loop work by ADR-0023.                                                                 |
| 0015 External Retention Scheduler with product-owned run records        | No change required.                                                                                               |
| 0016 User Memory retention depends on retention class, not age alone    | No change required.                                                                                               |
| 0017 Remove Prototype Compatibility Debt after Sage hard cut            | Updated to name Scoped Config Context removal as part of the no-compatibility posture.                            |
| 0018 Shared rate limiting uses self-hosted Valkey                       | No change required.                                                                                               |
| 0019 Deployment Settings generate runtime env                           | No change required.                                                                                               |
| 0020 Use assistant-ui for the Conversation UI Surface                   | Updated so Knowledge is an explicit Tool control and Documents are Knowledge constraints.                         |
| 0021 Signal is a Conversation Channel                                   | No change required.                                                                                               |
| 0022 Bound Admin Database Streaming and Turn Timing                     | Superseded for future tool-loop work by ADR-0023.                                                                 |
| 0023 Unified Model-Driven Tool Loop                                     | Still owns Tool boundaries; ADR-0029 supersedes typed planning and ADR-0028 supersedes Admin Config proposals.     |
| 0024 Transparent Reasoning and Tool Trace Posture                       | Still owns live/persisted trace data, ephemeral Provider Continuity State privacy, and content-free Model Usage Observations; ADR-0034 supersedes identical Admin/User presentation. |
| 0027 Separate Tool decisions from final answer delivery                 | Superseded by ADR-0029; retained as final-answer safety history.                                                    |
| 0028 Sage owns direct Admin Config writes                               | Anchor decision for conversationally confirmed direct Admin Config writes.                                         |
| 0029 Native Tool calling with one Tool round                            | Native protocol and hard-cut decisions remain valid; its one-batch constraint is superseded by ADR-0030.           |
| 0030 Bounded native Tool loop                                           | Anchor decision for model-driven native Tool continuation, provider continuity, silent-stall recovery, and at most six executed Tool batches. |
| 0031 Default GLM reasoning effort to none                               | Explicit deployment-level reasoning setting; historical `none` default superseded by Flash / `low`.                         |
| 0032 Test User Sessions reuse the User Conversation module              | Anchor decision for one shared User Conversation execution and UI module with thin logged-in and Admin test harness adapters. |
| 0033 Model-led autonomy and concise User responses                      | Anchor decision for generic consent, personal-decision, brevity, Tool-stopping, and model-turn separator behavior without semantic answer rewriting. |
| 0034 Present Conversation Activity for its audience                     | Anchor decision for one retained trace contract with product-facing User Activity and diagnostic Admin Activity. |
