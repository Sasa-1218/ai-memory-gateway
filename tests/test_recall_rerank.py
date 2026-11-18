"""Staged Pawwake 4.2 recall-rerank tests; no network or database access."""
import math
import unittest

from recall_rerank import request_body, pick


class RerankRulesTests(unittest.TestCase):
    def test_candidate_payload_and_gate(self):
        payload = request_body(
            "test-model", "番茄小花园",
            [{"content": "我们一起做花环", "event_date": "2026-08-13"}],
            "memory",
        )
        self.assertEqual(payload["state"]["candidates"]["1"]["text"], "我们一起做花环")
        self.assertEqual(payload["questions"]["c1"]["type"], "noul")
        self.assertEqual(payload["model"], "test-model")

    def test_low_gate_rejects_irrelevant_candidates(self):
        answers = {
            "gate": {"type": "noul", "noul": 0.25},
            "c1": {"type": "noul", "noul": 0.99},
        }
        picked, gate = pick(answers, 1)
        self.assertEqual(picked, [])
        self.assertEqual(gate, 0.25)

    def test_high_gate_ranks_candidates(self):
        answers = {"gate": {"type": "noul", "noul": 0.95}}
        for i, score in enumerate([0.3, 0.9, 0.5, 0.8], start=1):
            answers[f"c{i}"] = {"type": "noul", "noul": score}
        picked, _ = pick(answers, 4)
        self.assertEqual(picked, [1, 3, 2])

    def test_individual_irrelevant_candidates_are_not_injected(self):
        answers = {"gate": {"type": "noul", "noul": 0.99}}
        for i, score in enumerate([0.9, 0.2, 0.49, 0.6], 1):
            answers[f"c{i}"] = {"type": "noul", "noul": score}
        picked, _ = pick(answers, 4)
        self.assertEqual(picked, [0, 3])

    def test_all_individual_candidates_low_returns_valid_empty(self):
        answers = {
            "gate": {"type": "noul", "noul": 0.99},
            "c1": {"type": "noul", "noul": 0.12},
        }
        self.assertEqual(pick(answers, 1)[0], [])

    def test_request_body_bounds_data_and_rejects_oversized_pool(self):
        with self.assertRaises(ValueError):
            request_body("m", "hello", [{"content": "x"}] * 21, "memory")
        with self.assertRaises(ValueError):
            request_body("m", "hello", [], "memory")
        with self.assertRaises(ValueError):
            request_body("m", "q" * 501, [{"content": "x"}], "memory")
        result = request_body("m", "hello", [{"content": "x" * 2000}], "memory")
        self.assertEqual(len(result["state"]["candidates"]["1"]["text"]), 950)

    def test_non_finite_scores_are_rejected(self):
        for bad in (math.nan, math.inf, -math.inf, True):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    pick({"gate": {"type": "noul", "noul": bad},
                          "c1": {"type": "noul", "noul": 0.9}}, 1)

    def test_malformed_response_rejected(self):
        answers = {
            "gate": {"type": "noul", "noul": 0.9},
            "c1": {"type": "noul", "noul": "high"},
        }
        with self.assertRaises(ValueError):
            pick(answers, 1)


if __name__ == "__main__":
    unittest.main()
