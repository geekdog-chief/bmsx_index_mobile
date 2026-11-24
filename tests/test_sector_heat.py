import unittest

from sector_heat import calibrate_sector_heats


def sector_item(key, heat, posts=400, eligible=True, confidence=0.9, attention=50.0):
    return {
        "key": key,
        "available": True,
        "signals": {"heat": heat, "attention": attention},
        "metrics": {
            "samplePosts": posts,
            "sourceCount": 24,
            "platformCount": 4,
            "activityIndex": attention,
            "fairness": {"rankEligible": eligible, "confidence": confidence},
        },
    }


class SectorHeatTests(unittest.TestCase):
    def test_no_discrimination_no_rank(self):
        items = [sector_item(f"s{i}", 55.0) for i in range(10)]
        calibrate_sector_heats(items, 50.0)
        self.assertTrue(all(it["heatRank"] is None for it in items))
        self.assertTrue(all(it["heatModel"]["status"] == "no_distinguishable_spread" for it in items))

    def test_rank_only_when_eligible_and_distinguishable(self):
        # 情绪+关注度双维度拉开，区分度真实超过抽样噪声底
        items = [sector_item(f"s{i}", 40.0 + i * 5, attention=40.0 + i * 6) for i in range(10)]
        items[0]["metrics"]["fairness"]["rankEligible"] = False
        calibrate_sector_heats(items, 50.0)
        self.assertIsNone(items[0]["heatRank"])
        self.assertEqual(items[0]["heatTier"], "数据不足，暂不硬排名")
        self.assertEqual(items[-1]["heatRank"], 1)
        self.assertIsNotNone(items[1]["heatRank"])

    def test_relative_heat_capped(self):
        items = [sector_item(f"s{i}", 100.0) for i in range(5)] + [sector_item("low", 0.0)]
        calibrate_sector_heats(items, 50.0)
        for it in items:
            self.assertLessEqual(it["heat"], 95.0)
            self.assertGreaterEqual(it["heat"], 5.0)
            self.assertLessEqual(it["relativeHeat"], 100.0)
            self.assertGreaterEqual(it["relativeHeat"], 0.0)

    def test_low_confidence_not_hard_ranked(self):
        items = [sector_item(f"s{i}", 40.0 + i * 5, attention=40.0 + i * 6, eligible=(i != 0), confidence=(0.1 if i == 0 else 0.9)) for i in range(10)]
        calibrate_sector_heats(items, 50.0)
        self.assertIsNone(items[0]["heatRank"])


if __name__ == "__main__":
    unittest.main()
