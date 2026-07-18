import unittest
from datetime import datetime, timedelta, timezone

import main


def dt(minutes=0):
    return datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)


class PushDeliverySemanticsTest(unittest.TestCase):
    def base_meta(self):
        return {
            "is_push": True,
            "push_source": "shadow_cron",
            "delivery": "bark",
        }

    def test_first_bark_success_writes_delivered_at(self):
        meta = main._apply_bark_delivery_result(
            self.base_meta(),
            {"attempted": True, "delivered": True, "error_type": "none", "http_status": 200},
        )
        self.assertTrue(meta["bark_delivered"])
        self.assertEqual(meta["bark_attempts"], 1)
        self.assertIn("bark_last_attempt_at", meta)
        self.assertIn("bark_delivered_at", meta)
        self.assertEqual(meta["bark_delivered_at"], meta["bark_last_attempt_at"])

    def test_existing_delivered_at_is_not_overwritten(self):
        meta = self.base_meta()
        meta["bark_attempts"] = 1
        meta["bark_delivered_at"] = "2026-07-19T01:00:00+00:00"
        updated = main._apply_bark_delivery_result(
            meta,
            {"attempted": True, "delivered": True, "error_type": "none", "http_status": 200},
        )
        self.assertEqual(updated["bark_delivered_at"], "2026-07-19T01:00:00+00:00")
        self.assertEqual(updated["bark_attempts"], 2)

    def test_first_failure_then_retry_success(self):
        meta = main._apply_bark_delivery_result(
            self.base_meta(),
            {"attempted": True, "delivered": False, "error_type": "ConnectError", "http_status": "not_reported"},
        )
        self.assertFalse(meta["bark_delivered"])
        self.assertNotIn("bark_delivered_at", meta)
        self.assertTrue(main._is_retryable_undelivered_push(meta))

        meta = main._apply_bark_delivery_result(
            meta,
            {"attempted": True, "delivered": True, "error_type": "none", "http_status": 200},
        )
        self.assertTrue(meta["bark_delivered"])
        self.assertEqual(meta["bark_attempts"], 2)
        self.assertIn("bark_delivered_at", meta)
        self.assertFalse(main._is_retryable_undelivered_push(meta))

    def test_three_failures_exhaust_retry_without_delivery(self):
        meta = self.base_meta()
        for _ in range(3):
            meta = main._apply_bark_delivery_result(
                meta,
                {"attempted": True, "delivered": False, "error_type": "ConnectError", "http_status": "not_reported"},
            )
        self.assertFalse(meta["bark_delivered"])
        self.assertTrue(meta["bark_retry_exhausted"])
        self.assertNotIn("bark_delivered_at", meta)
        self.assertFalse(main._is_retryable_undelivered_push(meta))

    def test_bark_failed_but_kelivo_replied_stops_retry(self):
        meta = main._apply_bark_delivery_result(
            self.base_meta(),
            {"attempted": True, "delivered": False, "error_type": "ConnectError", "http_status": "not_reported"},
        )
        meta["bark_retry_stopped"] = True
        meta["bark_retry_stop_reason"] = "user_replied_after_generated_push"
        self.assertFalse(main._is_retryable_undelivered_push(meta))

    def test_old_delivered_record_fallbacks(self):
        created_at = dt()
        meta = {**self.base_meta(), "bark_delivered": True, "bark_last_attempt_at": "2026-07-19T13:00:00+00:00"}
        self.assertEqual(main._get_bark_delivered_at(meta, created_at), dt(60))

        meta = {**self.base_meta(), "bark_delivered": True}
        self.assertEqual(main._get_bark_delivered_at(meta, created_at), created_at)

    def test_latest_delivered_uses_delivery_time_not_generation_order(self):
        early_generated_late_delivered = {
            "created_at": dt(0),
            "metadata": {
                **self.base_meta(),
                "bark_delivered": True,
                "bark_delivered_at": "2026-07-19T16:00:00+00:00",
            },
        }
        later_generated_early_delivered = {
            "created_at": dt(120),
            "metadata": {
                **self.base_meta(),
                "bark_delivered": True,
                "bark_delivered_at": "2026-07-19T15:00:00+00:00",
            },
        }
        bad_delivered_at_falls_back_to_created_at = {
            "created_at": dt(30),
            "metadata": {
                **self.base_meta(),
                "bark_delivered": True,
                "bark_delivered_at": "not-a-time",
                "bark_last_attempt_at": "",
            },
        }
        rows = [
            later_generated_early_delivered,
            bad_delivered_at_falls_back_to_created_at,
            early_generated_late_delivered,
        ]
        self.assertEqual(main._latest_delivered_shadow_push_at_from_rows(rows), dt(240))

    def test_timing_uses_delivered_time_for_hard_minimum(self):
        state = main._build_push_timing_state(
            dt(20).astimezone(main.SHANGHAI_TZ),
            last_generated_at=dt(5),
            last_delivered_at=dt(),
            target_minutes=150,
        )
        self.assertEqual(state["push_window"], "hard_minimum_block")
        self.assertEqual(state["generation_window"], "generated_recent_block")

    def test_failed_generation_protects_briefly_but_does_not_refresh_delivery_window(self):
        state = main._build_push_timing_state(
            dt(20).astimezone(main.SHANGHAI_TZ),
            last_generated_at=dt(),
            last_delivered_at=None,
            target_minutes=150,
        )
        self.assertEqual(state["push_window"], "normal_window")
        self.assertEqual(state["generation_window"], "generated_recent_block")
        self.assertEqual(state["last_push_minutes"], "not_applicable")

        later = main._build_push_timing_state(
            dt(40).astimezone(main.SHANGHAI_TZ),
            last_generated_at=dt(),
            last_delivered_at=None,
            target_minutes=150,
        )
        self.assertEqual(later["push_window"], "normal_window")
        self.assertEqual(later["generation_window"], "ok")

    def test_early_and_normal_windows_use_stable_target(self):
        target = main._stable_push_target_minutes("session-a", dt())
        self.assertEqual(target, main._stable_push_target_minutes("session-a", dt()))

        early = main._build_push_timing_state(
            dt(target - 1).astimezone(main.SHANGHAI_TZ),
            last_generated_at=dt(-300),
            last_delivered_at=dt(),
            target_minutes=target,
        )
        self.assertEqual(early["push_window"], "early_window")

        normal = main._build_push_timing_state(
            dt(target).astimezone(main.SHANGHAI_TZ),
            last_generated_at=dt(-300),
            last_delivered_at=dt(),
            target_minutes=target,
        )
        self.assertEqual(normal["push_window"], "normal_window")

    def test_model_skip_does_not_refresh_target_and_new_delivery_can_refresh_it(self):
        original = dt()
        target = main._stable_push_target_minutes("session-b", original)
        self.assertEqual(target, main._stable_push_target_minutes("session-b", original))

        changed_target = None
        for minutes in range(1, 360):
            candidate = main._stable_push_target_minutes("session-b", dt(minutes))
            if candidate != target:
                changed_target = candidate
                break
        self.assertIsNotNone(changed_target)


if __name__ == "__main__":
    unittest.main()
