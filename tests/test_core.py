import itertools
import unittest

from home_media_follow.core import activation, choose_candidate, in_requested_range, logical_key, needs_upgrade, parse_episode


def item(episode=10, width=3840, height=2160, *, source="a", status="verified", season=1, part=None):
    return {"show_id": "test", "season": season, "episode": episode, "part": part, "source_id": source, "source_path": f"/{source}/{episode}.mp4", "size": 100, "probe": {"status": status, "width": width, "height": height}}


class CoreTests(unittest.TestCase):
    def test_activation_does_not_request_backfill(self):
        initial = activation([item(100), item(102)], "complete-scan")
        self.assertFalse(in_requested_range(item(51), initial))
        self.assertFalse(in_requested_range(item(101), initial))
        self.assertTrue(in_requested_range(item(102), initial))
        self.assertTrue(in_requested_range(item(103), initial))
        self.assertEqual(initial["initial_selected"], [logical_key(item(102))])

    def test_later_missing_episode_after_activation_is_requested(self):
        initial = activation([item(100)], "complete-scan")
        self.assertTrue(in_requested_range(item(101), initial))
        self.assertTrue(in_requested_range(item(102), initial))

    def test_variety_same_period_part_is_distinct_and_in_range(self):
        initial = activation([item(4, part="下"), item(5, part="上")], "complete")
        self.assertNotEqual(logical_key(item(5, part="上")), logical_key(item(5, part="下")))
        self.assertTrue(in_requested_range(item(5, part="下"), initial))
        self.assertFalse(in_requested_range(item(4, part="下"), initial))

    def test_watermark_survives_season_transition(self):
        initial = activation([item(40), item(1, season=2)], "complete")
        self.assertFalse(in_requested_range(item(50), initial))
        self.assertTrue(in_requested_range(item(2, season=2), initial))

    def test_verified_fallback_when_4k_unknown(self):
        lower = item(width=1920, height=1080)
        self.assertIs(choose_candidate([item(status="unknown"), lower]), lower)
        self.assertIsNone(choose_candidate([item(width=1280, height=720), item(status="unknown")]))

    def test_stable_best_candidate_independent_of_order(self):
        choices = [item(source="b"), item(source="a"), item(width=1920, height=1080, source="c")]
        for order in itertools.permutations(choices):
            self.assertEqual(choose_candidate(list(order))["source_id"], "a")

    def test_existing_same_tier_is_retained_and_not_upgraded(self):
        current = item(height=1600, source="old")
        higher_area = item(height=2160, source="new")
        self.assertIs(choose_candidate([higher_area], current), current)
        self.assertFalse(needs_upgrade(current, higher_area))
        self.assertTrue(needs_upgrade(item(width=1920, height=1080), higher_area))
        self.assertFalse(needs_upgrade(higher_area, item(width=1920, height=1080)))
        self.assertFalse(needs_upgrade(current, item(episode=11, width=7680, height=4320)))

    def test_malformed_and_cross_episode_candidates_fail_closed(self):
        self.assertIsNone(choose_candidate([item(width=3840, height=1)]))
        bad = item(); bad["probe"]["width"] = True
        self.assertIsNone(choose_candidate([bad]))
        with self.assertRaises(ValueError):
            choose_candidate([item(1), item(2)])
        with self.assertRaises(ValueError):
            activation([], "complete")
        with self.assertRaises(ValueError):
            activation([item()], "")

    def test_parse_episode_is_conservative(self):
        for name, kind, expected in [
            ("47.mkv", "anime", {"season": 1, "episode": 47, "part": None}),
            ("177_4K臻彩MAX.mp4", "anime", {"season": 1, "episode": 177, "part": None}),
            ("S02E03.2026.2160p.mp4", "tv", {"season": 2, "episode": 3, "part": None}),
            ("2026.10.10-第1期上-4K.mkv", "variety", {"season": 1, "episode": 1, "part": "上"}),
        ]:
            self.assertEqual(parse_episode(name, kind), expected, name)
        for name in ["预告01.mp4", "S01E01E02.mp4", "01-02.mp4", "2026.1080p.mp4", "unknown.mp4"]:
            self.assertIsNone(parse_episode(name, "tv"), name)

    def test_historical_chinese_hua_preserves_episode_and_download_boundary(self):
        active = activation([item(178)], "current-source")
        for episode in [1, 2, 83, 98, 99, 102]:
            parsed = parse_episode(f"第{episode}话.mp4", "anime")
            self.assertEqual(parsed, {"season": 1, "episode": episode, "part": None})
            self.assertFalse(in_requested_range(dict(parsed, show_id="test"), active))

    def test_bonus_episode_does_not_become_regular_variety_or_anime(self):
        for name in ["2026.10.30-第4期超前彩蛋.mp4", "第4期彩蛋.mkv", "第83话彩蛋.mp4"]:
            for kind in ["variety", "anime"]:
                self.assertIsNone(parse_episode(name, kind), name)


if __name__ == "__main__":
    unittest.main()
