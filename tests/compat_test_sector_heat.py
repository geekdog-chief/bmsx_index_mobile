from __future__ import annotations

import copy
import unittest

from sector_heat import (
    HEAT_MODEL_VERSION,
    TEMPORAL_ALPHA,
    TEMPORAL_FINAL_DAILY_CAP,
    TEMPORAL_RELATIVE_DAILY_CAP,
    TEMPORAL_SMOOTHING,
    calibrate_sector_heats,
    stabilize_sector_heats,
)


def sector(
    key: str,
    *,
    attention: float,
    emotion: float,
    activity: float,
    samples: int = 240,
    sources: int = 6,
    platforms: int = 3,
) -> dict:
    return {
        "key": key,
        "name": key,
        "available": True,
        "expectedSourceCount": 6,
        "scores": {"baoma": 51, "novice": 52, "retail": 53, "composite": 52},
        "signals": {
            "optimism": 51.0,
            "fomo": 52.0,
            "newcomer": 53.0,
            "intensity": 54.0,
            "attention": attention,
            "heat": emotion,
        },
        "metrics": {
            "activityIndex": activity,
            "samplePosts": samples,
            "sourceCount": sources,
            "platformCount": platforms,
        },
    }


class SectorHeatTests(unittest.TestCase):
    def test_real_cross_section_spreads_without_changing_formal_scores(self) -> None:
        rows = [
            sector(f"s{index}", attention=35 + index * 2, emotion=42 + index, activity=30 + index * 3)
            for index in range(16)
        ]
        original_scores = [copy.deepcopy(item["scores"]) for item in rows]
        original_signals = [copy.deepcopy(item["signals"]) for item in rows]

        audit = calibrate_sector_heats(rows, market_anchor=61.0)

        self.assertEqual(audit["version"], HEAT_MODEL_VERSION)
        self.assertEqual(audit["status"], "applied")
        self.assertGreater(audit["finalRange"], 20.0)
        self.assertGreater(audit["relativeRange"], 20.0)
        self.assertEqual([item["scores"] for item in rows], original_scores)
        self.assertEqual([item["signals"] for item in rows], original_signals)
        self.assertEqual(sorted(item["heatRank"] for item in rows), list(range(1, 17)))
        self.assertTrue(all(item["heatModel"]["formalPersonaScoresChanged"] is False for item in rows))

    def test_identical_cross_section_does_not_manufacture_rank_spread(self) -> None:
        rows = [sector(f"s{index}", attention=55, emotion=58, activity=60) for index in range(16)]
        audit = calibrate_sector_heats(rows, market_anchor=63.0)

        self.assertEqual(audit["status"], "no_distinguishable_spread")
        self.assertEqual(audit["dispersionGate"], 0.0)
        self.assertEqual({item["relativeHeat"] for item in rows}, {50.0})
        self.assertEqual({item["heat"] for item in rows}, {63.0})
        self.assertEqual({item["heatRank"] for item in rows}, {None})
        self.assertEqual({item["heatTier"] for item in rows}, {"差异不显著"})

    def test_low_coverage_shrinks_relative_deviation(self) -> None:
        full = sector("full", attention=75, emotion=72, activity=82)
        low = sector("low", attention=75, emotion=72, activity=82, samples=4, sources=1, platforms=1)
        anchors = [
            sector(f"base{index}", attention=42 + index, emotion=44 + index, activity=40 + index)
            for index in range(14)
        ]
        rows = [full, low, *anchors]
        calibrate_sector_heats(rows, market_anchor=55.0)

        self.assertLess(low["heatModel"]["reliability"], full["heatModel"]["reliability"])
        self.assertLess(abs(low["relativeHeat"] - 50), abs(full["relativeHeat"] - 50))

    def test_market_anchor_preserves_all_market_hot_or_cold_context(self) -> None:
        hot_rows = [sector(f"h{index}", attention=50 + index, emotion=50 + index / 2, activity=50 + index) for index in range(16)]
        cold_rows = copy.deepcopy(hot_rows)
        calibrate_sector_heats(hot_rows, market_anchor=72.0)
        calibrate_sector_heats(cold_rows, market_anchor=28.0)

        hot_median = sorted(item["heat"] for item in hot_rows)[7:9]
        cold_median = sorted(item["heat"] for item in cold_rows)[7:9]
        self.assertGreater(sum(hot_median) / 2, 65.0)
        self.assertLess(sum(cold_median) / 2, 35.0)

    def test_temporal_stabilizer_is_causal_capped_and_preserves_formal_scores(self) -> None:
        previous = [
            sector(f"s{index}", attention=50, emotion=50, activity=50)
            for index in range(16)
        ]
        calibrate_sector_heats(previous, market_anchor=58.0)
        current = [
            sector(
                f"s{index}",
                attention=25 + index * 4,
                emotion=35 + index * 2,
                activity=20 + index * 5,
            )
            for index in range(16)
        ]
        original_scores = [copy.deepcopy(item["scores"]) for item in current]
        calibrate_sector_heats(current, market_anchor=60.0)
        raw_relative = {item["key"]: item["relativeHeat"] for item in current}

        audit = stabilize_sector_heats(current, previous, market_anchor=60.0)

        self.assertEqual(audit["temporalSmoothing"], TEMPORAL_SMOOTHING)
        self.assertEqual(audit["temporalAlpha"], TEMPORAL_ALPHA)
        self.assertEqual(audit["stabilizedSectorCount"], 16)
        self.assertEqual([item["scores"] for item in current], original_scores)
        for item, prior in zip(current, previous):
            self.assertLessEqual(
                abs(item["relativeHeat"] - prior["relativeHeat"]),
                TEMPORAL_RELATIVE_DAILY_CAP + 0.1,
            )
            prior_centered = prior["heat"] + 2.0
            self.assertLessEqual(
                abs(item["heat"] - prior_centered),
                TEMPORAL_FINAL_DAILY_CAP + 0.1,
            )
            self.assertEqual(item["heatModel"]["rawRelativeHeat"], raw_relative[item["key"]])

    def test_temporal_stabilizer_does_not_rank_identical_sectors(self) -> None:
        previous = [sector(f"s{index}", attention=50, emotion=50, activity=50) for index in range(16)]
        current = copy.deepcopy(previous)
        calibrate_sector_heats(previous, market_anchor=55.0)
        calibrate_sector_heats(current, market_anchor=55.0)

        stabilize_sector_heats(current, previous, market_anchor=55.0)

        self.assertEqual({item["heatRank"] for item in current}, {None})
        self.assertEqual({item["heatTier"] for item in current}, {"差异不显著"})

    def test_temporal_gap_reduces_stale_inertia_without_lookahead(self) -> None:
        previous = [{
            "key": "only", "available": True, "relativeHeat": 50.0, "heat": 55.0,
            "heatModel": {"marketAnchor": 55.0},
        }]
        one_day = [{
            "key": "only", "available": True, "relativeHeat": 80.0, "heat": 75.0,
            "absoluteHeat": 70.0,
            "heatModel": {"marketAnchor": 55.0, "rawRelativeHeat": 80.0, "rawFinalHeat": 75.0},
        }]
        three_days = copy.deepcopy(one_day)

        stabilize_sector_heats(one_day, previous, market_anchor=55.0, gap_days=1)
        audit = stabilize_sector_heats(three_days, previous, market_anchor=55.0, gap_days=3)

        self.assertGreaterEqual(three_days[0]["relativeHeat"], one_day[0]["relativeHeat"])
        self.assertEqual(audit["temporalGapDays"], 3)
        self.assertAlmostEqual(
            audit["temporalEffectiveAlpha"],
            1 - (1 - TEMPORAL_ALPHA) ** 3,
            places=6,
        )

    def test_v4_gives_post_volume_more_weight_than_emotion(self) -> None:
        volume_hot = sector("volume", attention=75, emotion=45, activity=80)
        emotion_hot = sector("emotion", attention=45, emotion=80, activity=45)
        anchors = [
            sector(f"base{index}", attention=50 + index / 4, emotion=50, activity=50 + index / 4)
            for index in range(14)
        ]
        rows = [volume_hot, emotion_hot, *anchors]

        calibrate_sector_heats(rows, market_anchor=55.0)

        self.assertGreater(volume_hot["absoluteHeat"], emotion_hot["absoluteHeat"])
        self.assertGreater(
            volume_hot["heatModel"]["relativeLatent"],
            emotion_hot["heatModel"]["relativeLatent"],
        )
        self.assertEqual(
            volume_hot["heatModel"]["weights"]["relativeVolume"],
            0.85,
        )


if __name__ == "__main__":
    unittest.main()
