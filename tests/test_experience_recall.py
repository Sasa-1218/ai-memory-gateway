"""Experience recall selection remains opt-in and review-gated."""
import unittest
from experience_recall import card_cues, match_experience_cards, format_experience_recall


def make_card(card_id, title="烟花日的手链", status="approved", visible=True, details=None):
    return {
        "id": card_id, "title": title, "review_status": status,
        "ai_visible": visible, "key_details": details or ["烟花日", "星星手链"],
        "event_date_start": "2026-08-08", "event_date_end": "2026-08-09",
        "event_summary": "我们聊到那晚的烟花和手链",
        "interaction_trace": "我说想和你再去看一次。",
    }


class ExperienceRecallTests(unittest.TestCase):
    def test_approved_visible_card_can_match_key_detail(self):
        cards = [make_card(12)]
        selected = match_experience_cards("还记得烟花日吗？", cards, 1)
        self.assertEqual([item["id"] for item in selected], [12])
        self.assertIn("烟花日的手链", format_experience_recall(selected))

    def test_review_and_visibility_gate(self):
        cards = [
            make_card(1, status="pending"), make_card(2, visible=False),
            make_card(3, status="archived"), make_card(4, status="deleted"),
            make_card(5, status="superseded"),
        ]
        self.assertEqual(match_experience_cards("烟花日", cards), [])

    def test_specific_match_wins_over_shorter_and_limit_is_bounded(self):
        cards = [
            make_card(1, details=["烟花日"]),
            make_card(2, title="猫耳簪", details=["烟花日星星手链"]),
            make_card(3, title="其他回忆", details=["烟花日"]),
        ]
        selected = match_experience_cards("烟花日星星手链", cards, 1)
        self.assertEqual([item["id"] for item in selected], [2])
        self.assertEqual(len(match_experience_cards("烟花日星星手链", cards, 25)), 2)

    def test_one_or_two_character_generic_cue_does_not_fire(self):
        cards = [make_card(1, title="猫", details=["花", "心"])]
        self.assertEqual(card_cues(cards[0]), [])
        self.assertEqual(match_experience_cards("猫在花边", cards), [])

    def test_short_personal_cue_can_match_without_generic_one_character_terms(self):
        card = make_card(8, title="初初的小字", details=["初初"])
        self.assertEqual([c["id"] for c in match_experience_cards("你还记得初初吗？", [card])], [8])
        self.assertNotEqual(card_cues(card), [])

    def test_title_objects_are_individual_cues(self):
        card = make_card(9, title="樱花照、汉服与猫耳簪", details=[])
        self.assertEqual([c["id"] for c in match_experience_cards("那支猫耳簪呢？", [card])], [9])

    def test_nonmatching_query_does_not_inject(self):
        cards = [make_card(1)]
        self.assertEqual(match_experience_cards("明天吃什么？", cards), [])
        self.assertEqual(format_experience_recall([]), "")

    def test_recall_text_clearly_limits_temporal_interpretation(self):
        text = format_experience_recall([make_card(1)])
        self.assertIn("历史回忆", text)
        self.assertIn("无需", text) if "无需" in text else self.assertIn("不必", text)
        self.assertIn("2026-08-08 至 2026-08-09", text)


if __name__ == "__main__":
    unittest.main()
