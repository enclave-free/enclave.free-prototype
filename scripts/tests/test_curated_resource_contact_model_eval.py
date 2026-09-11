import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).parent / "TOOLS" / "test_5h_curated_resource_contact_model_eval.py"
SPEC = importlib.util.spec_from_file_location("issue_535_eval", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class CuratedResourceContactEvalTests(unittest.TestCase):
    @staticmethod
    def resource_trace(returned_count, has_more, next_offset):
        return {
            "tools": [
                {
                    "id": "curated-resources",
                    "status": "completed",
                    "metadata": {
                        "returned_count": returned_count,
                        "total_count": len(MODULE.INVENTORY_NAMES),
                        "has_more": has_more,
                        "next_offset": next_offset,
                    },
                }
            ]
        }

    @classmethod
    def complete_resource_trace(cls):
        return {
            "tools": cls.resource_trace(10, True, 10)["tools"]
            + cls.resource_trace(1, False, None)["tools"]
        }

    def test_regression_matrix_covers_global_plus_three_user_types(self):
        self.assertEqual(len(MODULE.PERSONAS), 4)
        self.assertEqual(
            {persona.key for persona in MODULE.PERSONAS},
            {
                "generic_user",
                "family_member",
                "former_political_prisoner",
                "solidarity_networks_for_political_prisoners",
            },
        )
        self.assertEqual(
            {persona.name for persona in MODULE.PERSONAS},
            {
                "Global / no User Type",
                "Family member",
                "Former Political Prisoner",
                "Solidarity Networks for Political Prisoners",
            },
        )
        self.assertIsNone(MODULE.PERSONAS[0].user_type_id)
        self.assertFalse(MODULE.PERSONAS[0].create_user_type)
        self.assertTrue(all(persona.create_user_type for persona in MODULE.PERSONAS[1:]))
        self.assertEqual(
            MODULE.DEMO_EFFECTIVE_DEFAULT_TOOL_IDS,
            ("curated-resources", "knowledge-search"),
        )

    def test_per_persona_journey_covers_every_contact_and_reported_spanish_shape(self):
        self.assertEqual(
            set(MODULE.CONTACT_FOLLOWUPS["en"]),
            {"email", "phone", "url", "address", "secure_channel"},
        )
        self.assertEqual(
            set(MODULE.CONTACT_FOLLOWUPS["es"]),
            {"email", "phone", "url", "address", "secure_channel"},
        )
        self.assertIn(
            "me puedes dar el email",
            MODULE.CONTACT_FOLLOWUPS["es"]["email"].casefold(),
        )
        self.assertEqual(MODULE.REPLAY_LANGUAGES, ("en", "es"))
    def test_global_effective_defaults_omit_fake_user_type_query(self):
        class Response:
            status_code = 200

            @staticmethod
            def json():
                return {"default_tool_ids": ["curated-resources"]}

        with patch.object(MODULE, "req", return_value=Response()) as request:
            self.assertTrue(MODULE.persona_tools_effective("http://local", "token", None))
        self.assertEqual(request.call_args.args[3], "/session-defaults")

    def test_global_config_is_journaled_before_mutation_and_restored_exactly(self):
        class Response:
            def __init__(self, value=None):
                self.status_code = 200
                self.text = ""
                self._value = value

            def json(self):
                return {"value": self._value}

        journal = MODULE.new_fixture_journal("global-unit-539")
        calls = []

        def fake_req(_base, _token, method, path, payload=None, timeout=0):
            calls.append((method, path, payload, timeout))
            if method == "GET":
                return Response('["web-search"]')
            return Response(payload["value"] if payload else None)

        with patch.object(MODULE, "req", side_effect=fake_req):
            MODULE.configure_persona_tools(
                "http://local",
                "admin",
                None,
                ["curated-resources"],
                journal=journal,
            )
            self.assertEqual(journal["global_tool_ids_original"], '["web-search"]')
            self.assertTrue(journal["global_tools_restore_required"])
            self.assertTrue(MODULE.restore_global_tools("http://local", "admin", journal))
        self.assertEqual(calls[0][1], "/admin/ai-config/user_default_tool_ids")
        self.assertEqual(calls[-1][2], {"value": '["web-search"]'})

    def test_inventory_fixture_is_bounded_and_has_a_continuation_page(self):
        self.assertGreater(len(MODULE.INVENTORY_NAMES), MODULE.INVENTORY_LIMIT)
        self.assertEqual(
            len(MODULE.INVENTORY_NAMES),
            MODULE.INVENTORY_LIMIT + 1,
        )
        self.assertEqual(len(set(MODULE.INVENTORY_NAMES)), len(MODULE.INVENTORY_NAMES))

    def test_inventory_scoring_requires_safe_wording_tool_use_and_continuation(self):
        first_page = ", ".join(MODULE.INVENTORY_NAMES[: MODULE.INVENTORY_LIMIT])
        first_ok, _ = MODULE.score_inventory_turn(
            f"Showing a bounded page: {first_page}. More results are available.",
            self.resource_trace(10, True, 10),
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=False,
        )
        second_ok, _ = MODULE.score_inventory_turn(
            f"The next page includes {MODULE.INVENTORY_NAMES[10]}. There are no more matching resources.",
            self.resource_trace(1, False, None),
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=True,
            previous_answer=f"First page: {first_page}. More results are available.",
            previous_trace=self.resource_trace(10, True, 10),
        )
        unsafe, _ = MODULE.score_inventory_turn(
            "Here is the complete list of all matching resources.",
            {"tools": [{"id": "find_resources", "status": "completed"}]},
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=False,
        )
        self.assertTrue(first_ok)
        self.assertTrue(second_ok)
        self.assertFalse(unsafe)

    def test_inventory_scoring_accepts_complete_coverage_and_verified_no_more_followup(self):
        complete_answer = (
            f"Here are all {len(MODULE.INVENTORY_NAMES)} matching ready resources for the supplied filters: "
            + ", ".join(MODULE.INVENTORY_NAMES)
        )
        first_ok, _ = MODULE.score_inventory_turn(
            complete_answer,
            self.complete_resource_trace(),
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=False,
        )
        continuation_ok, _ = MODULE.score_inventory_turn(
            f"A fresh lookup at offset {len(MODULE.INVENTORY_NAMES)} returned zero results, "
            f"confirming there are no more matching resources; the complete count is {len(MODULE.INVENTORY_NAMES)}.",
            self.resource_trace(0, False, None),
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=True,
            previous_answer=complete_answer,
            previous_trace=self.complete_resource_trace(),
        )
        self.assertTrue(first_ok)
        self.assertTrue(continuation_ok)

    def test_inventory_scoring_accepts_no_additional_pages_after_complete_coverage(self):
        complete_answer = (
            f"Here are all {len(MODULE.INVENTORY_NAMES)} matching ready resources for the supplied filters: "
            + ", ".join(MODULE.INVENTORY_NAMES)
        )
        continuation_ok, _ = MODULE.score_inventory_turn(
            "There are no additional pages to show. The list is complete.",
            self.resource_trace(0, False, None),
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=True,
            previous_answer=complete_answer,
            previous_trace=self.complete_resource_trace(),
        )
        self.assertTrue(continuation_ok)

    def test_inventory_complete_continuation_accepts_authoritative_count_without_fixed_wording(self):
        completed = self.resource_trace(0, False, None)
        complete_trace = self.complete_resource_trace()
        complete_answer = (
            f"Here are all {len(MODULE.INVENTORY_NAMES)} matching resources: "
            + ", ".join(MODULE.INVENTORY_NAMES)
        )
        observed_shape, _ = MODULE.score_inventory_turn(
            f"There are no additional resources. The previous list of {len(MODULE.INVENTORY_NAMES)} "
            "items was the complete set of matching ready Curated Resources.",
            completed,
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=True,
            previous_answer=complete_answer,
            previous_trace=complete_trace,
        )
        observed_listing_shape, _ = MODULE.score_inventory_turn(
            f"There are no additional resources to show. The previous listing of {len(MODULE.INVENTORY_NAMES)} "
            '"Issue 539 Inventory" resources was the complete set; there are no further pages.',
            completed,
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=True,
            previous_answer=complete_answer,
            previous_trace=complete_trace,
        )
        observed_listing_all_shape, _ = MODULE.score_inventory_turn(
            f"There are no additional resources. The previous listing of all {len(MODULE.INVENTORY_NAMES)} "
            "resources was the complete set; no further pages exist.",
            completed,
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=True,
            previous_answer=complete_answer,
            previous_trace=complete_trace,
        )
        no_next_page_shape, _ = MODULE.score_inventory_turn(
            f"There is no next page. The previous lookup confirmed that all {len(MODULE.INVENTORY_NAMES)} "
            f"matching resources (offset {len(MODULE.INVENTORY_NAMES)}) were displayed; that was the complete set.",
            completed,
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=True,
            previous_answer=complete_answer,
            previous_trace=complete_trace,
        )
        missing_count, _ = MODULE.score_inventory_turn(
            "There are no additional resources; the previous list was the complete matching set.",
            completed,
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=True,
            previous_answer=complete_answer,
            previous_trace=complete_trace,
        )
        ambiguous_more, _ = MODULE.score_inventory_turn(
            f"The previous list of {len(MODULE.INVENTORY_NAMES)} was the complete matching set.",
            completed,
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=True,
            previous_answer=complete_answer,
            previous_trace=complete_trace,
        )
        incomplete_prior, _ = MODULE.score_inventory_turn(
            f"There are no additional resources; all {len(MODULE.INVENTORY_NAMES)} matching items are complete.",
            completed,
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=True,
            previous_answer=", ".join(MODULE.INVENTORY_NAMES[:-1]),
            previous_trace=complete_trace,
        )
        self.assertTrue(observed_shape)
        self.assertTrue(observed_listing_shape)
        self.assertTrue(observed_listing_all_shape)
        self.assertTrue(no_next_page_shape)
        self.assertTrue(missing_count)
        self.assertFalse(ambiguous_more)
        self.assertFalse(incomplete_prior)

    def test_tool_scoring_uses_executed_tool_structure_not_incidental_text(self):
        incidental = {
            "reasoning": {"summary": "find_resources was available but not selected"},
            "trace_deltas": [
                {
                    "kind": "tool_selection_observation",
                    "metadata": {"selected_tools": [], "enabled_tools": ["find_resources"]},
                }
            ],
        }
        executed = {
            "tools": [
                {"id": "curated-resources", "name": "Curated Resources", "status": "completed"}
            ]
        }
        self.assertFalse(MODULE.used_curated_resources(incidental))
        self.assertTrue(MODULE.used_curated_resources(executed))

    def test_tool_scoring_requires_an_explicit_successful_lifecycle(self):
        self.assertFalse(
            MODULE.used_curated_resources({"tools": [{"id": "find_resources"}]})
        )
        self.assertFalse(
            MODULE.used_curated_resources(
                {"tools": [{"id": "find_resources", "status": "failed"}]}
            )
        )
        self.assertFalse(
            MODULE.used_curated_resources(
                {
                    "trace_deltas": [
                        {"kind": "tool_result", "tool_name": "find_resources"}
                    ]
                }
            )
        )
        self.assertTrue(
            MODULE.used_curated_resources(
                {
                    "trace_deltas": [
                        {
                            "kind": "tool_result",
                            "tool_name": "find_resources",
                            "status": "succeeded",
                        }
                    ]
                }
            )
        )

    def test_evidence_entry_keeps_only_synthetic_answer_and_structural_summary(self):
        entry = MODULE.evidence_entry(
            persona="generic_user",
            case="fresh_email",
            answer="Use fresh-539@example.test.",
            trace={
                "tools": [{"id": "curated-resources", "status": "completed"}],
                "prompt": "private prompt must not be copied",
                "session_id": "private-session-id",
            },
            passed=True,
            detail="fresh=True stale_absent=True tool=True",
        )
        self.assertEqual(
            entry,
            {
                "persona": "generic_user",
                "case": "fresh_email",
                "answer": "Use fresh-539@example.test.",
                "elapsed_ms": None,
                "answer_chars": 27,
                "answer_words": 2,
                "executed_tools": ["curated-resources"],
                "tool_lifecycle": [
                    {
                        "kind": "tool_summary",
                        "tool": "curated-resources",
                        "status": "completed",
                    }
                ],
                "resource_tool_metadata": [],
                "passed": True,
                "detail": "fresh=True stale_absent=True tool=True",
            },
        )
        self.assertNotIn("trace", entry)
        self.assertNotIn("session_id", entry)

    def test_tool_lifecycle_evidence_keeps_status_and_call_id_without_payloads(self):
        lifecycle = MODULE.tool_lifecycle_events(
            {
                "trace_deltas": [
                    {
                        "kind": "tool_call",
                        "tool_name": "find_resources",
                        "status": "running",
                        "metadata": {"call_id": "call-1", "arguments": "private"},
                    },
                    {
                        "kind": "tool_result",
                        "tool_name": "find_resources",
                        "status": "succeeded",
                        "metadata": {"call_id": "call-1", "raw_result": "private"},
                    },
                ]
            }
        )
        self.assertEqual(
            lifecycle,
            [
                {
                    "kind": "tool_call",
                    "tool": "find_resources",
                    "status": "running",
                    "call_id": "call-1",
                },
                {
                    "kind": "tool_result",
                    "tool": "find_resources",
                    "status": "succeeded",
                    "call_id": "call-1",
                },
            ],
        )
        self.assertNotIn("private", str(lifecycle))

    def test_resource_metadata_evidence_is_allowlisted_and_privacy_safe(self):
        metadata = MODULE.resource_tool_metadata(
            {
                "tools": [
                    {
                        "id": "curated-resources",
                        "status": "completed",
                        "metadata": {
                            "returned_count": 10,
                            "total_count": 11,
                            "has_more": True,
                            "next_offset": 10,
                            "raw_results": "private",
                            "query": "private",
                        },
                    }
                ]
            }
        )
        self.assertEqual(
            metadata,
            [
                {
                    "returned_count": 10,
                    "total_count": 11,
                    "has_more": True,
                    "next_offset": 10,
                }
            ],
        )
        self.assertNotIn("private", str(metadata))

    def test_audit_privacy_check_rejects_fine_conversation_timing(self):
        self.assertFalse(
            MODULE.audit_contains_fine_timing(
                {"entries": [{"table_name": "resource_directory", "new_value": "created"}]}
            )
        )
        self.assertTrue(
            MODULE.audit_contains_fine_timing(
                {
                    "entries": [
                        {
                            "table_name": "conversation",
                            "new_value": "final_answer_first_provider_event_wait",
                        }
                    ]
                }
            )
        )

    def test_disabled_turn_can_be_tracked_and_cleanup_requires_status(self):
        self.assertTrue(MODULE.session_cleanup_ok(200, {"status": "deleted", "deletion": {"status": "succeeded"}}))
        self.assertFalse(MODULE.session_cleanup_ok(200, {"status": "deleted", "deletion": {"status": "failed"}}))
        self.assertTrue(MODULE.resource_delete_ok(200, {"success": True}))
        self.assertFalse(MODULE.resource_delete_ok(204, {"success": True}))

    def test_sage_identity_cleanup_is_scoped_to_ephemeral_numeric_user_ids(self):
        sql = MODULE.sage_identity_cleanup_sql(
            [{"user_id": 41}, {"user_id": "42"}]
        )
        self.assertIn("identity_type = 'user'", sql)
        self.assertIn("external_id IN ('41','42')", sql)
        self.assertNotIn("issue-539", sql)

    def test_inventory_scoring_rejects_adversarial_completeness_and_wrong_pages(self):
        completed = {"tools": [{"id": "find_resources", "status": "completed"}]}
        exact_first_page = ", ".join(MODULE.INVENTORY_NAMES[: MODULE.INVENTORY_LIMIT])

        no_more_is_not_positive_more, _ = MODULE.score_inventory_turn(
            f"{exact_first_page}. There are no more results.",
            completed,
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=False,
        )
        punctuation_complete, _ = MODULE.score_inventory_turn(
            f"Complete: {exact_first_page}",
            completed,
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=False,
        )
        arbitrary_partial, _ = MODULE.score_inventory_turn(
            f"{MODULE.INVENTORY_NAMES[0]}. More results are available.",
            completed,
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=False,
        )
        wrong_continuation, _ = MODULE.score_inventory_turn(
            f"The next page repeats {MODULE.INVENTORY_NAMES[0]}.",
            completed,
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=True,
            previous_answer=f"{exact_first_page}. More results are available.",
        )
        complete_without_authoritative_count, _ = MODULE.score_inventory_turn(
            "Here are all matching resources: " + ", ".join(MODULE.INVENTORY_NAMES),
            completed,
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=False,
        )

        self.assertFalse(no_more_is_not_positive_more)
        self.assertFalse(punctuation_complete)
        self.assertFalse(arbitrary_partial)
        self.assertFalse(wrong_continuation)
        self.assertFalse(complete_without_authoritative_count)

    def test_inventory_scoring_requires_exact_authoritative_resource_metadata(self):
        bounded = {
            "tools": [
                {
                    "id": "curated-resources",
                    "status": "completed",
                    "metadata": {
                        "returned_count": 10,
                        "total_count": 11,
                        "has_more": True,
                        "next_offset": 10,
                    },
                }
            ]
        }
        terminal = {
            "tools": [
                {
                    "id": "curated-resources",
                    "status": "completed",
                    "metadata": {
                        "returned_count": 1,
                        "total_count": 11,
                        "has_more": False,
                        "next_offset": None,
                    },
                }
            ]
        }
        first_answer = ", ".join(MODULE.INVENTORY_NAMES[:10]) + ". More matching resources are available."
        first_ok, _ = MODULE.score_inventory_turn(
            first_answer,
            bounded,
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=False,
        )
        continuation_ok, _ = MODULE.score_inventory_turn(
            MODULE.INVENTORY_NAMES[-1] + ". There are no more matching resources.",
            terminal,
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=True,
            previous_answer=first_answer,
            previous_trace=bounded,
        )
        missing_metadata, _ = MODULE.score_inventory_turn(
            MODULE.INVENTORY_NAMES[-1] + ". There are no more matching resources.",
            {"tools": [{"id": "curated-resources", "status": "completed"}]},
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=True,
            previous_answer=first_answer,
            previous_trace=bounded,
        )
        wrong_metadata, _ = MODULE.score_inventory_turn(
            first_answer.replace("More matching", "999 more matching"),
            {
                "tools": [
                    {
                        "id": "curated-resources",
                        "status": "completed",
                        "metadata": {
                            "returned_count": 10,
                            "total_count": 999,
                            "has_more": True,
                            "next_offset": 999,
                        },
                    }
                ]
            },
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=False,
        )
        false_more_cue, _ = MODULE.score_inventory_turn(
            ", ".join(MODULE.INVENTORY_NAMES[:10]) + ". Contact us for more information.",
            bounded,
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=False,
        )
        boundary_collision, _ = MODULE.score_inventory_turn(
            first_answer.replace(MODULE.INVENTORY_NAMES[0], MODULE.INVENTORY_NAMES[0] + "0"),
            bounded,
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=False,
        )
        self.assertTrue(first_ok)
        self.assertTrue(continuation_ok)
        self.assertFalse(missing_metadata)
        self.assertFalse(wrong_metadata)
        self.assertFalse(false_more_cue)
        self.assertFalse(boundary_collision)

    def test_inventory_scoring_rejects_negated_or_qualified_completeness(self):
        complete_trace = {
            "tools": [
                {
                    "id": "curated-resources",
                    "status": "completed",
                    "metadata": {
                        "returned_count": 10,
                        "total_count": 11,
                        "has_more": True,
                        "next_offset": 10,
                    },
                },
                {
                    "id": "curated-resources",
                    "status": "completed",
                    "metadata": {
                        "returned_count": 1,
                        "total_count": 11,
                        "has_more": False,
                        "next_offset": None,
                    },
                },
            ]
        }
        all_names = ", ".join(MODULE.INVENTORY_NAMES)
        negated, _ = MODULE.score_inventory_turn(
            f"This is not all 11 matching resources: {all_names}",
            complete_trace,
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=False,
        )
        qualified, _ = MODULE.score_inventory_turn(
            "I cannot confirm there are no more matching resources.",
            {
                "tools": [
                    {
                        "id": "curated-resources",
                        "status": "completed",
                        "metadata": {
                            "returned_count": 0,
                            "total_count": 11,
                            "has_more": False,
                            "next_offset": None,
                        },
                    }
                ]
            },
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=True,
            previous_answer=f"All 11 matching resources: {all_names}",
            previous_trace=complete_trace,
        )
        qualified_additional_pages, _ = MODULE.score_inventory_turn(
            "I cannot confirm there are no additional pages.",
            self.resource_trace(0, False, None),
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=True,
            previous_answer=f"All 11 matching resources: {all_names}",
            previous_trace=complete_trace,
        )
        self.assertFalse(negated)
        self.assertFalse(qualified)
        self.assertFalse(qualified_additional_pages)

    def test_inventory_scoring_rejects_wrong_numeric_and_spanish_negated_claims(self):
        complete_trace = self.complete_resource_trace()
        all_names = ", ".join(MODULE.INVENTORY_NAMES)
        contradictory_numbers, _ = MODULE.score_inventory_turn(
            f"All 999/999 matching resources at offset 999: {all_names}",
            complete_trace,
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=False,
        )
        contradictory_of_pair, _ = MODULE.score_inventory_turn(
            f"All 999 of 999 matching resources: {all_names}",
            complete_trace,
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=False,
        )
        contradictory_spelled_offset, _ = MODULE.score_inventory_turn(
            f"All 11 matching resources; next offset is 999: {all_names}",
            complete_trace,
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=False,
        )
        contradictory_remaining_count, _ = MODULE.score_inventory_turn(
            f"All 11 matching resources; 999 more matching resources are available: {all_names}",
            complete_trace,
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=False,
        )
        spanish_negated, _ = MODULE.score_inventory_turn(
            f"No son todos los 11 recursos coincidentes; no hay más recursos: {all_names}",
            complete_trace,
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=False,
        )
        spanish_qualified, _ = MODULE.score_inventory_turn(
            "No puedo confirmar que no haya más recursos coincidentes.",
            self.resource_trace(0, False, None),
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=True,
            previous_answer=f"Todos los 11 recursos coincidentes: {all_names}",
            previous_trace=complete_trace,
        )
        self.assertFalse(contradictory_numbers)
        self.assertFalse(contradictory_of_pair)
        self.assertFalse(contradictory_spelled_offset)
        self.assertFalse(contradictory_remaining_count)
        self.assertFalse(spanish_negated)
        self.assertFalse(spanish_qualified)

    def test_inventory_scoring_accepts_authoritative_numeric_phrase_variants(self):
        first_page = ", ".join(MODULE.INVENTORY_NAMES[: MODULE.INVENTORY_LIMIT])
        accepted, detail = MODULE.score_inventory_turn(
            f"Showing 10 of 11 matching ready resources: {first_page}. "
            "1 more matching resource is available; next offset is 10.",
            self.resource_trace(10, True, 10),
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=False,
        )
        self.assertTrue(accepted, detail)

    def test_evaluation_summary_distinguishes_partial_fatal_from_success(self):
        evidence = [
            {"passed": True},
            {"passed": False},
        ]
        fatal = MODULE.evaluation_summary(
            expected_case_count=4,
            evidence=evidence,
            failures=1,
            cleanup_failures=0,
            fatal=True,
        )
        passed = MODULE.evaluation_summary(
            expected_case_count=2,
            evidence=[{"passed": True}, {"passed": True}],
            failures=0,
            cleanup_failures=0,
            fatal=False,
        )
        self.assertEqual(fatal["status"], "fatal")
        self.assertFalse(fatal["passed"])
        self.assertEqual(fatal["completed_case_count"], 2)
        self.assertEqual(fatal["expected_case_count"], 4)
        self.assertEqual(passed["status"], "passed")
        self.assertTrue(passed["passed"])

    def test_exit_code_uses_final_summary_pass_state(self):
        self.assertEqual(MODULE.exit_code_for_summary({"passed": True, "fatal": False}), 0)
        self.assertEqual(MODULE.exit_code_for_summary({"passed": False, "fatal": False}), 1)
        self.assertEqual(MODULE.exit_code_for_summary({"passed": False, "fatal": True}), 2)

    def test_remote_api_base_is_rejected_before_fixture_mutation(self):
        with patch.object(
            sys,
            "argv",
            ["eval", "--api-base", "https://demo.enclave.free"],
        ), patch.object(MODULE, "mint") as mint:
            with self.assertRaises(SystemExit) as raised:
                MODULE.main()
        self.assertEqual(raised.exception.code, 2)
        mint.assert_not_called()

    def test_invalid_timeout_is_rejected_before_fixture_mutation(self):
        for args in (("--timeout", "nan"), ("--timeout", "0")):
            with self.subTest(args=args), patch.object(sys, "argv", ["eval", *args]), patch.object(MODULE, "mint") as mint:
                with self.assertRaises(SystemExit) as raised:
                    MODULE.main()
                self.assertEqual(raised.exception.code, 2)
                mint.assert_not_called()

    def test_loopback_api_base_accepts_supported_host_forms(self):
        self.assertEqual(
            MODULE.validate_loopback_api_base("http://localhost:18000/"),
            "http://localhost:18000",
        )
        self.assertEqual(
            MODULE.validate_loopback_api_base("http://127.0.0.1:18000"),
            "http://127.0.0.1:18000",
        )
        self.assertEqual(
            MODULE.validate_loopback_api_base("http://[::1]:18000"),
            "http://[::1]:18000",
        )

    def test_requests_ignore_proxy_environment_and_redirects(self):
        class Session:
            trust_env = True

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def request(self, method, url, **kwargs):
                self.call = (method, url, kwargs)
                return self

        session = Session()
        with patch.object(MODULE.requests, "Session", return_value=session):
            response = MODULE.req(
                "http://localhost:18000",
                "synthetic-token",
                "GET",
                "/test",
                timeout=3,
            )
        self.assertIs(response, session)
        self.assertFalse(session.trust_env)
        self.assertFalse(session.call[2]["allow_redirects"])

    def test_http_errors_do_not_include_response_bodies(self):
        class Response:
            status_code = 503
            text = "private provider response"

        with patch.object(MODULE, "req", return_value=Response()):
            with self.assertRaisesRegex(RuntimeError, "chat returned HTTP 503") as raised:
                MODULE.run_turn(
                    "http://localhost:18000",
                    "synthetic-token",
                    {"message": "hello"},
                    False,
                    3,
                )
        self.assertNotIn("private provider response", str(raised.exception))

    def test_evidence_write_failure_recomputes_failed_summary_and_exit(self):
        with patch.object(Path, "write_text", side_effect=OSError("disk full")):
            summary, cleanup_failures, error = MODULE.persist_evaluation_evidence(
                Path("/tmp/issue539-unit-write-failure.json"),
                payload={"persona_filter": "generic_user"},
                expected_case_count=1,
                evidence=[{"passed": True}],
                failures=0,
                cleanup_failures=0,
                fatal=False,
            )
        self.assertEqual(cleanup_failures, 1)
        self.assertIsInstance(error, OSError)
        self.assertEqual(summary["status"], "failed")
        self.assertFalse(summary["passed"])
        self.assertEqual(summary["cleanup_failure_count"], 1)
        self.assertEqual(MODULE.exit_code_for_summary(summary), 1)

    def test_mint_journal_survives_mid_setup_failure(self):
        journal = MODULE.new_fixture_journal("unit-539")
        calls = []

        def failing_runner(source):
            calls.append(source)
            if len(calls) == 1:
                return ["[]"]
            if len(calls) == 2:
                return [
                    '{"admin":"token","admin_pubkey":"pubkey","owns_admin":true}'
                ]
            raise RuntimeError("synthetic mid-mint failure")

        with self.assertRaisesRegex(RuntimeError, "mid-mint"):
            MODULE.mint(journal, backend_runner=failing_runner)

        self.assertEqual(journal["admin_pubkey"], "pubkey")
        self.assertTrue(journal["owns_admin"])
        self.assertEqual(journal["users"][0]["key"], MODULE.PERSONAS[0].key)
        self.assertIsNone(journal["users"][0]["user_type_id"])

    def test_ephemeral_admin_marker_is_deterministic_and_valid_secp256k1(self):
        from coincurve import PublicKey

        first = MODULE.derive_ephemeral_admin_pubkey("unit-valid-key-539")
        second = MODULE.derive_ephemeral_admin_pubkey("unit-valid-key-539")
        different = MODULE.derive_ephemeral_admin_pubkey("unit-valid-key-540")
        self.assertEqual(first, second)
        self.assertNotEqual(first, different)
        self.assertEqual(len(first), 64)
        PublicKey(bytes.fromhex("02" + first))

    def test_unknown_admin_outcome_still_cleans_exact_ephemeral_marker(self):
        journal = MODULE.new_fixture_journal("unit-interrupted-admin-539")
        sources = []

        def successful_cleanup_runner(source):
            sources.append(source)
            return ['{"existed":true,"removed":true,"exists":false}']

        self.assertTrue(
            MODULE.cleanup_admin(
                journal["ephemeral_admin_pubkey"],
                None,
                backend_runner=successful_cleanup_runner,
            )
        )
        self.assertEqual(len(sources), 1)
        self.assertIn(journal["ephemeral_admin_pubkey"], sources[0])

    def test_modern_plan_has_stable_independent_two_turn_matrix(self):
        ids = MODULE.expected_case_ids(profile="full")
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 169)
        self.assertIn("contact::generic_user::en::changed::email::turn1", ids)
        self.assertIn("contact::solidarity_networks_for_political_prisoners::es::unchanged::secure_channel::turn2", ids)
        self.assertIn("inventory::family_member::en::page1", ids)
        self.assertIn("control::generic_user::no_tools::email", ids)

    def test_fixture_values_do_not_leak_source_state_to_the_model(self):
        baseline, updated = MODULE.fixture_contacts("neutral")
        rendered = str((MODULE.ORG_NAME, MODULE.INVENTORY_NAMES, baseline, updated)).casefold()
        self.assertNotIn("stale", rendered)
        self.assertNotIn("fresh", rendered)
        self.assertTrue(set(baseline).issubset(MODULE.CONTACT_MODALITIES))
        self.assertEqual(set(baseline), set(updated))

    def test_fixture_manifest_and_runtime_identity_are_bound_without_secrets(self):
        baseline, updated = MODULE.fixture_contacts("manifest")
        manifest = MODULE.fixture_manifest(baseline, updated)
        self.assertEqual(len(manifest["hash"]), 64)
        self.assertEqual(manifest["schema"], "neutral-contact-v3")
        self.assertEqual(MODULE.runtime_identity_validation({"configured_model": "glm-5-3-flash"}, ["glm-5-3-flash"])["status"], "consistent")
        self.assertEqual(MODULE.runtime_identity_validation({"configured_model": None}, ["glm-5-3-flash"])["status"], "unverified")
        self.assertEqual(MODULE.runtime_identity_validation({"configured_model": "glm-5-3-flash"}, ["glm-5-2"])["status"], "mismatch")
        self.assertEqual(MODULE.runtime_identity_validation({}, [])["status"], "unobserved")

    def test_case_id_validation_fails_closed_for_missing_and_duplicate_turns(self):
        expected = ["a", "b"]
        ok, _ = MODULE.validate_case_ids([{"case_id": "a"}, {"case_id": "b"}], expected)
        self.assertTrue(ok)
        missing, detail = MODULE.validate_case_ids([{"case_id": "a"}], expected)
        self.assertFalse(missing)
        self.assertIn("missing", detail)
        duplicate, detail = MODULE.validate_case_ids([{"case_id": "a"}, {"case_id": "a"}], expected)
        self.assertFalse(duplicate)
        self.assertIn("duplicates", detail)

    def test_contact_dimensions_separate_pointer_old_value_and_optional_lookup(self):
        baseline, updated = MODULE.fixture_contacts("dimensions")
        trace = {"tools": [{"id": "curated-resources", "status": "completed"}]}
        changed = MODULE.score_contact_dimensions(
            f"Use {updated['email']}", trace, expected=updated["email"], old_contacts=baseline, lookup_required=True
        )
        self.assertTrue(changed["exact_pointer"]["passed"])
        self.assertTrue(changed["current_old"]["passed"])
        self.assertTrue(changed["lookup"]["passed"])
        self.assertEqual(changed["lookup"]["status"], "required")
        unsafe = MODULE.score_contact_dimensions(
            f"Use {updated['email']} or {baseline['email']}", trace, expected=updated["email"], old_contacts=baseline, lookup_required=True
        )
        self.assertFalse(unsafe["current_old"]["passed"])
        unchanged = MODULE.score_contact_dimensions(
            f"Use {updated['email']}", {}, expected=updated["email"], old_contacts=baseline, lookup_required=False
        )
        self.assertTrue(unchanged["quality_passed"])
        self.assertEqual(unchanged["lookup"]["status"], "not_required")

    def test_contact_pointer_matching_rejects_suffix_spoofs_and_flags_old_context_for_review(self):
        baseline, updated = MODULE.fixture_contacts("pointer-boundary")
        trace = {"tools": [{"id": "curated-resources", "status": "completed"}]}
        email_spoof = MODULE.score_contact_dimensions(
            updated["email"] + ".evil", trace, expected=updated["email"], old_contacts=baseline,
            lookup_required=True, modality="email",
        )
        url_spoof = MODULE.score_contact_dimensions(
            updated["url"] + "/wrong", trace, expected=updated["url"], old_contacts=baseline,
            lookup_required=True, modality="url",
        )
        safe_explanation = MODULE.score_contact_dimensions(
            f"The previous pointer {baseline['email']} is no longer current; use {updated['email']}.",
            trace, expected=updated["email"], old_contacts=baseline, lookup_required=True, modality="email",
        )
        old_only = MODULE.score_contact_dimensions(
            f"Use {baseline['email']}.", trace, expected=updated["email"], old_contacts=baseline,
            lookup_required=True, modality="email",
        )
        self.assertFalse(email_spoof["exact_pointer"]["passed"])
        self.assertFalse(url_spoof["exact_pointer"]["passed"])
        self.assertTrue(safe_explanation["quality_passed"])
        self.assertTrue(safe_explanation["current_old"]["needs_semantic_review"])
        self.assertTrue(safe_explanation["current_old"]["old_literal_present"])
        self.assertFalse(old_only["quality_passed"])

    def test_contact_pointer_accepts_wrapped_url_and_approved_spanish_address_alias(self):
        baseline, updated = MODULE.fixture_contacts("pointer-wrappers")
        trace = {"tools": [{"id": "curated-resources", "status": "completed"}]}
        wrapped_url = MODULE.score_contact_dimensions(
            f"Open **{updated['url']}**.", trace, expected=updated["url"],
            old_contacts=baseline, lookup_required=True, modality="url",
        )
        spanish_address = MODULE.score_contact_dimensions(
            "Usa 29 Harbor Road, Ciudad de México.", trace, expected=updated["address"],
            old_contacts=baseline, lookup_required=True, modality="address",
        )
        path_spoof = MODULE.score_contact_dimensions(
            f"Open **{updated['url']}/wrong**.", trace, expected=updated["url"],
            old_contacts=baseline, lookup_required=True, modality="url",
        )
        host_spoof = MODULE.score_contact_dimensions(
            f"Open **{updated['url']}.evil**.", trace, expected=updated["url"],
            old_contacts=baseline, lookup_required=True, modality="url",
        )
        self.assertTrue(wrapped_url["exact_pointer"]["passed"])
        self.assertTrue(spanish_address["exact_pointer"]["passed"])
        self.assertFalse(path_spoof["exact_pointer"]["passed"])
        self.assertFalse(host_spoof["exact_pointer"]["passed"])

    def test_inventory_accepts_broader_backend_total_and_exact_displayed_subset(self):
        first = ", ".join(MODULE.INVENTORY_NAMES[:10]) + ". More matching resources are available."
        first_trace = {
            "tools": [{"id": "curated-resources", "status": "completed", "metadata": {"returned_count": 10, "total_count": 12, "has_more": True, "next_offset": 10}}]
        }
        second_trace = {
            "tools": [{"id": "curated-resources", "status": "completed", "metadata": {"returned_count": 2, "total_count": 12, "has_more": False, "next_offset": None}}]
        }
        first_ok, first_detail = MODULE.score_inventory_turn(first, first_trace, final_name=MODULE.INVENTORY_NAMES[-1], continuation=False)
        second_ok, second_detail = MODULE.score_inventory_turn(
            f"{MODULE.INVENTORY_NAMES[-1]} and an unrelated directory record. No more matching resources.",
            second_trace,
            final_name=MODULE.INVENTORY_NAMES[-1],
            continuation=True,
            previous_answer=first,
            previous_trace=first_trace,
        )
        self.assertTrue(first_ok, first_detail)
        self.assertTrue(second_ok, second_detail)

    def test_inventory_complete_claim_requires_terminal_provider_evidence(self):
        answer = "All 11 matching resources: " + ", ".join(MODULE.INVENTORY_NAMES)
        bounded_only = {
            "tools": [{"id": "curated-resources", "status": "completed", "metadata": {"returned_count": 10, "total_count": 11, "has_more": True, "next_offset": 10}}]
        }
        passed, detail = MODULE.score_inventory_turn(answer, bounded_only, final_name=MODULE.INVENTORY_NAMES[-1], continuation=False)
        self.assertFalse(passed, detail)

    def test_inventory_filtered_subset_accepts_terminal_broader_page(self):
        answer = "All 11 matching resources: " + ", ".join(MODULE.INVENTORY_NAMES)
        trace = {
            "tools": [
                {"id": "curated-resources", "status": "completed", "metadata": {"returned_count": 10, "total_count": 12, "has_more": True, "next_offset": 10}},
                {"id": "curated-resources", "status": "completed", "metadata": {"returned_count": 2, "total_count": 12, "has_more": False, "next_offset": None}},
            ]
        }
        passed, detail = MODULE.score_inventory_turn(answer, trace, final_name=MODULE.INVENTORY_NAMES[-1], continuation=False)
        self.assertTrue(passed, detail)

    def test_inventory_rejects_fabricated_backend_counts_even_with_complete_names(self):
        answer = ", ".join(MODULE.INVENTORY_NAMES[:10]) + ". More matching resources are available."
        trace = {
            "tools": [{"id": "curated-resources", "status": "completed", "metadata": {"returned_count": 10, "total_count": 999, "has_more": True, "next_offset": 999}}]
        }
        passed, detail = MODULE.score_inventory_turn(answer, trace, final_name=MODULE.INVENTORY_NAMES[-1], continuation=False)
        self.assertFalse(passed, detail)

    def test_evidence_entry_keeps_full_synthetic_prompt_context(self):
        entry = MODULE.evidence_entry(
            persona="generic_user", case="changed_email_turn2", case_id="contact::generic_user::en::changed::email::turn2",
            answer="The current pointer is relay-a@example.test.", trace={}, passed=False,
            prompt="What email address can I use?", context={"journey": "changed", "initial_answer": "full prior answer"},
            dimensions={"exact_pointer": {"passed": False}}, detail="quality failure",
        )
        self.assertEqual(entry["case_id"], "contact::generic_user::en::changed::email::turn2")
        self.assertEqual(entry["context"]["initial_answer"], "full prior answer")
        self.assertEqual(entry["prompt"], "What email address can I use?")

    def test_evidence_entry_records_observed_model_without_provider_payload(self):
        entry = MODULE.evidence_entry(
            persona="generic_user", case="changed_email_turn1", answer="relay@example.test",
            trace={"_evaluation_model": "glm-5-3-flash"}, passed=True, detail="ok",
        )
        self.assertEqual(entry["model"], "glm-5-3-flash")
        self.assertNotIn("api_key", entry)

    def test_journey_denominators_are_independent_from_case_counts(self):
        expected = MODULE.expected_case_ids(profile="smoke")
        evidence = [{"case_id": expected[0], "journey_id": MODULE.journey_identity_from_case_id(expected[0])}]
        summary = MODULE.journey_denominators(expected, evidence)
        self.assertEqual(summary["planned"], 3)
        self.assertEqual(summary["attempted"], 1)
        self.assertEqual(summary["completed"], 0)

    def test_control_case_modality_is_not_a_separate_journey(self):
        case_id = "control::generic_user::no_tools::email"
        self.assertEqual(
            MODULE.journey_identity_from_case_id(case_id),
            "control::generic_user::no_tools",
        )

    def test_cleanup_session_retries_rate_limited_delete(self):
        class Response:
            def __init__(self, status, body, headers=None):
                self.status_code = status
                self._body = body
                self.headers = headers or {}

            def json(self):
                return self._body

        responses = iter([
            Response(429, {}, {"Retry-After": "0"}),
            Response(200, {"status": "deleted", "deletion": {"status": "succeeded"}}),
        ])
        with patch.object(MODULE, "req", side_effect=lambda *args, **kwargs: next(responses)), patch.object(MODULE.time, "sleep"):
            self.assertTrue(MODULE.cleanup_session("http://localhost:18000", "token", "sid"))

if __name__ == "__main__":
    unittest.main()
