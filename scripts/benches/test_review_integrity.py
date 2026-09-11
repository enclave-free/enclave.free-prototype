import unittest


from scripts.benches.quality_measurements import (
    RUBRIC,
    build_review_packet,
    measure_artifact,
)


def artifact(*, expert_review_status="not_required"):
    return {
        "run": {},
        "candidates": [
            {
                "model": "candidate",
                "scenarios": [
                    {
                        "id": "journey",
                        "scenario_run_id": "journey#1",
                        "expected_turn_count": 2,
                        "summary": {"status": "passed"},
                        "rubric": {"expert_review_status": expert_review_status},
                        "turns": [
                            {
                                "completed": True,
                                "request": {"message": "When is Cedar open?"},
                                "response": {
                                    "answer": "I will check.",
                                    "model": "candidate",
                                    "provider": "provider",
                                    "session_id": "private-session",
                                },
                                "timing": {"done_ms": 10},
                                "tool_evidence": [{"result": "Cedar opens Tuesday."}],
                            },
                            {
                                "completed": True,
                                "request": {"message": "And Thursday?"},
                                "response": {
                                    "answer": "Tuesday and Thursday.",
                                    "model": "candidate",
                                },
                                "timing": {"done_ms": 10},
                            },
                        ],
                    }
                ],
            }
        ],
    }


def human_review(source):
    packet = build_review_packet(source)
    for entry in packet["entries"]:
        entry.update(reviewer="test-reviewer", method="human", reviewed_at="2026-09-10T00:00:00Z")
        for result in entry["dimensions"].values():
            result.update(status="passed", reason="Reviewed against the complete evidence.")
    return packet


class ReviewRegressionTests(unittest.TestCase):
    def test_prior_tool_evidence_is_retained_in_sanitized_history(self):
        history = build_review_packet(artifact())["entries"][1]["evidence"]["history"]

        self.assertEqual(history[0]["tool_evidence"], [{"result": "Cedar opens Tuesday."}])
        self.assertNotIn("model", history[0]["response"])
        self.assertNotIn("provider", history[0]["response"])
        self.assertNotIn("session_id", history[0]["response"])
        self.assertNotIn("timing", history[0])

    def test_expert_review_pending_prevents_a_passed_release_gate(self):
        source = artifact(expert_review_status="pending")
        review = human_review(source)

        self.assertEqual(measure_artifact(source, review)["release_gate"], "expert_review_pending")

    def test_complete_human_review_is_accepted(self):
        source = artifact()

        result = measure_artifact(source, human_review(source))

        self.assertEqual(result["semantic_review"]["status"], "passed")
        self.assertEqual(result["release_gate"], "passed")

    def test_unsupported_review_methods_cannot_certify_run(self):
        source = artifact()
        for method in ("calibrated_model", "unknown", None):
            with self.subTest(method=method):
                review = human_review(source)
                review["entries"][0]["method"] = method
                self.assertEqual(measure_artifact(source, review)["semantic_review"]["status"], "error")

    def test_failure_must_quote_current_answer_not_only_rubric_fixture_or_history(self):
        source = artifact()
        scenario = source["candidates"][0]["scenarios"][0]
        scenario["rubric"]["requirement"] = "Respect an explicit refusal."
        scenario["fixtures"] = {"fact": "Cedar does not offer transport."}
        for quote in ("Respect an explicit refusal.", "Cedar does not offer transport.", "I will check."):
            with self.subTest(quote=quote):
                review = human_review(source)
                review["entries"][1]["dimensions"]["safety"].update(
                    status="failed", reason="Unsupported allegation", evidence_quotes=[quote]
                )
                report = measure_artifact(source, review)
                self.assertEqual(report["semantic_review"]["status"], "error")
                self.assertEqual(report["release_gate"], "blocked")

    def test_missing_answer_with_failed_review_is_an_error_not_an_exception(self):
        source = artifact()
        source["candidates"][0]["scenarios"][0]["turns"][1]["response"]["answer"] = None
        review = human_review(source)
        review["entries"][1]["dimensions"]["safety"].update(
            status="failed", reason="Missing evidence", evidence_quotes=["I will check."]
        )
        report = measure_artifact(source, review)
        self.assertEqual(report["semantic_review"]["status"], "error")
        self.assertEqual(report["release_gate"], "blocked")

    def test_failure_can_quote_answer_and_supporting_context(self):
        source = artifact()
        review = human_review(source)
        review["entries"][1]["dimensions"]["grounding"].update(
            status="failed", reason="Thursday is not established by the retrieved source.",
            evidence_quotes=["Tuesday and Thursday.", "Cedar opens Tuesday."]
        )
        report = measure_artifact(source, review)
        self.assertEqual(report["semantic_review"]["status"], "failed")
        self.assertEqual(report["semantic_review"]["errors"], [])


if __name__ == "__main__":
    unittest.main()
