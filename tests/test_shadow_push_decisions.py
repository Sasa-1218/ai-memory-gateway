import unittest

import main


class ShadowPushDecisionLoggingTest(unittest.TestCase):
    def test_model_skip_reason_is_preserved(self):
        decision = main.parse_shadow_decision(
            '{"action":"skip","reason":"need_more_space_after_conflict","message":""}'
        )
        self.assertTrue(decision["parse_success"])
        self.assertEqual(decision["action"], "skip")
        self.assertEqual(decision["reason"], "need_more_space_after_conflict")
        self.assertEqual(decision["message"], "")

    def test_model_skip_without_reason_uses_explicit_fallback(self):
        decision = main.parse_shadow_decision('{"action":"skip","message":""}')
        self.assertTrue(decision["parse_success"])
        self.assertEqual(decision["action"], "skip")
        self.assertEqual(decision["reason"], "model_skip_no_reason")
        self.assertEqual(decision["message"], "")

    def test_decision_payload_contains_only_desensitized_fields(self):
        payload = main._build_shadow_push_decision_payload(
            session_id="session-a",
            action="skip",
            reason="recent_topic_repeat",
            intent="avoid_pressure",
            state={
                "push_window": "early_window",
                "generation_window": "ok",
                "is_early_window": True,
                "silence_minutes": 123,
                "last_push_minutes": 45,
                "last_generated_push_minutes": 45,
                "minutes_until_normal_window": 90,
                "normal_window_minutes": 150,
                "last_effective_role": "assistant",
                "user_replied_after_last_push": False,
                "consecutive_unanswered_pushes": 2,
            },
            recent_excerpt_count=12,
            pushed=False,
            model="provider/model",
            parse_success=True,
        )
        self.assertEqual(payload["action"], "skip")
        self.assertEqual(payload["reason"], "recent_topic_repeat")
        self.assertEqual(payload["intent"], "avoid_pressure")
        self.assertEqual(payload["recent_excerpt_count"], 12)
        self.assertFalse(payload["pushed"])
        self.assertFalse(payload["user_replied_after_last_push"])

        forbidden_keys = {
            "content",
            "messages",
            "message",
            "prompt",
            "system_prompt",
            "push_text",
            "memory_text",
            "api_key",
            "secret",
            "authorization",
        }
        self.assertTrue(forbidden_keys.isdisjoint(payload.keys()))

    def test_shadow_decision_cooldown_ladder(self):
        self.assertEqual(main._shadow_decision_cooldown_minutes(0), 30)
        self.assertEqual(main._shadow_decision_cooldown_minutes(1), 60)
        self.assertEqual(main._shadow_decision_cooldown_minutes(2), 90)
        self.assertEqual(main._shadow_decision_cooldown_minutes(3), 120)
        self.assertEqual(main._shadow_decision_cooldown_minutes(9), 120)

    def test_shadow_decision_cooldown_bypass_reason_whitelist(self):
        self.assertTrue(main._is_push_decision_cooldown_bypass_reason("user_message_received"))
        self.assertTrue(main._is_push_decision_cooldown_bypass_reason("anniversary"))
        self.assertFalse(main._is_push_decision_cooldown_bypass_reason("seems_important"))
        self.assertFalse(main._is_push_decision_cooldown_bypass_reason("new_context"))

    def test_important_date_matching_supports_supported_forms(self):
        self.assertTrue(main._important_date_matches_today(today=main.date_cls(2026, 7, 27), date_text="07-27"))
        self.assertTrue(main._important_date_matches_today(today=main.date_cls(2026, 5, 13), date_text="05-13", event_year=2026))
        self.assertFalse(main._important_date_matches_today(today=main.date_cls(2025, 5, 13), date_text="05-13", event_year=2026))
        self.assertTrue(main._important_date_matches_today(today=main.date_cls(2026, 8, 8), date_start="08-07", date_end="08-08", event_year=2025))
        self.assertTrue(main._important_date_matches_today(today=main.date_cls(2026, 5, 13), date_text="2026-05-13"))
        self.assertFalse(main._important_date_matches_today(today=main.date_cls(2026, 12, 3), date_text="2024-12"))

    def test_shadow_decision_token_usage_helpers(self):
        usage = {"usage": {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200}}
        self.assertEqual(main._extract_usage_tokens(usage), (1000, 200, 1200))
        self.assertEqual(main._estimate_shadow_decision_cost_usd(1000, 200), 0.0045)

    def test_decision_payload_clamps_negative_minutes(self):
        payload = main._build_shadow_push_decision_payload(
            session_id="session-a",
            action="blocked",
            reason="hard_minimum_block",
            state={
                "silence_minutes": -5,
                "last_push_minutes": -1,
                "last_generated_push_minutes": "not_applicable",
                "minutes_until_normal_window": "not_reported",
                "normal_window_minutes": 150,
            },
        )
        self.assertEqual(payload["silence_minutes"], 0)
        self.assertEqual(payload["last_push_minutes"], 0)
        self.assertIsNone(payload["last_generated_push_minutes"])
        self.assertIsNone(payload["minutes_until_normal_window"])


if __name__ == "__main__":
    unittest.main()
