import unittest

import main


class ShadowMindPhaseATest(unittest.TestCase):
    def test_compute_state_returns_only_expected_drives(self):
        result = main.compute_shadow_mind_state(
            {
                "silence_minutes": 360,
                "last_push_minutes": 200,
                "last_effective_role": "user",
                "user_replied_after_last_push": True,
                "consecutive_unanswered_pushes": 0,
            },
            {"push_window": "normal_window", "is_early_window": False},
        )
        self.assertEqual(set(result["state"].keys()), set(main.SHADOW_MIND_DRIVES))
        for value in result["state"].values():
            self.assertIsInstance(value, int)
            self.assertGreaterEqual(value, 0)
            self.assertLessEqual(value, 100)
        self.assertEqual(result["inputs"]["silence_minutes"], 360)

    def test_concern_is_cautious_and_time_only(self):
        result = main.compute_shadow_mind_state(
            {
                "silence_minutes": 24 * 60,
                "last_effective_role": "assistant",
                "user_replied_after_last_push": False,
                "consecutive_unanswered_pushes": 4,
            },
            {"push_window": "normal_window"},
        )
        self.assertLessEqual(result["state"]["concern"], 45)
        reason_codes = {item["reason_code"] for item in result["reasons"] if item["drive"] == "concern"}
        self.assertIn("concern_time_only", reason_codes)
        self.assertNotIn("health_inferred", reason_codes)
        self.assertNotIn("danger_inferred", reason_codes)

    def test_unanswered_pushes_reduce_pressure_without_blocking(self):
        base = main.compute_shadow_mind_state(
            {"silence_minutes": 360, "last_effective_role": "user", "consecutive_unanswered_pushes": 0},
            {"push_window": "normal_window"},
        )
        unanswered = main.compute_shadow_mind_state(
            {"silence_minutes": 360, "last_effective_role": "user", "consecutive_unanswered_pushes": 3},
            {"push_window": "normal_window"},
        )
        self.assertLess(unanswered["state"]["longing"], base["state"]["longing"])
        reason_codes = {item["reason_code"] for item in unanswered["reasons"]}
        self.assertIn("unanswered_pushes_reduce_pressure", reason_codes)

    def test_thought_pool_is_desensitized(self):
        result = main.compute_shadow_mind_state(
            {
                "silence_minutes": 900,
                "last_effective_role": "user",
                "user_replied_after_last_push": True,
                "consecutive_unanswered_pushes": 0,
            },
            {"push_window": "normal_window"},
        )
        forbidden = {"content", "message", "prompt", "system_prompt", "memory", "secret", "api_key"}
        self.assertTrue(result["thought_pool"])
        for item in result["thought_pool"]:
            self.assertTrue(forbidden.isdisjoint(item.keys()))
            self.assertIn(item["drive"], main.SHADOW_MIND_DRIVES)

    def test_public_state_parses_jsonb_strings(self):
        state = main._shadow_mind_public_state({
            "session_id": "session-a",
            "longing": 1,
            "curiosity": 2,
            "share": 3,
            "warmth": 4,
            "concern": 5,
            "reasons": '[{"drive":"warmth","reason_code":"soft_reconnect_after_time","weight":10}]',
            "inputs": '{"silence_minutes":360}',
            "computed_at": None,
            "updated_at": None,
        })
        self.assertIsInstance(state["reasons"], list)
        self.assertIsInstance(state["inputs"], dict)
        self.assertEqual(state["inputs"]["silence_minutes"], 360)

    def test_public_events_do_not_expose_text_fields(self):
        events = main._shadow_mind_public_events([
            {
                "drive": "warmth",
                "previous_value": 50,
                "new_value": 60,
                "delta": 10,
                "reason_code": "user_replied_after_last_push",
                "created_at": None,
            }
        ])
        self.assertEqual(events[0]["drive"], "warmth")
        self.assertNotIn("content", events[0])
        self.assertNotIn("metadata", events[0])


if __name__ == "__main__":
    unittest.main()
