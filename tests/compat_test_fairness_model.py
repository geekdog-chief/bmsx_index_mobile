from __future__ import annotations

import copy
import unittest

from fairness_model import (
    MODEL_VERSION,
    aggregate_sector_company_first,
    companies_from_source_analyses,
)


DAYS = ("2026-08-06", "2026-08-05", "2026-08-04")


def signals(value: float, *, attention: float | None = None) -> dict:
    return {
        "optimism": value,
        "fomo": value,
        "panic": max(0.0, value - 50.0),
        "newcomer": value,
        "intensity": value,
        "attention": value if attention is None else attention,
    }


def platform(
    key: str,
    values: tuple[float, float, float] = (60.0, 55.0, 50.0),
    *,
    samples: tuple[int, int, int] = (60, 50, 40),
    volumes: tuple[float, float, float] = (18.0, 14.0, 10.0),
) -> dict:
    return {
        "key": key,
        "days": {
            day: {
                "status": "observed",
                "sampleCount": sample_count,
                "signals": signals(value),
                "volume": volume,
            }
            for day, value, sample_count, volume in zip(DAYS, values, samples, volumes)
        },
    }


def company(key: str, value: float = 60.0) -> dict:
    return {
        "key": key,
        "platforms": [
            platform("eastmoney", (value, value - 2, value - 4)),
            platform("sina", (value + 1, value - 1, value - 3), samples=(20, 18, 16)),
            platform("ths", (value + 8, value + 6, value + 4), samples=(12, 10, 8)),
        ],
    }


class FairnessModelTests(unittest.TestCase):
    def test_company_and_platform_order_permutation_does_not_change_result(self) -> None:
        rows = [company("c", 65), company("a", 55), company("b", 45)]
        permuted = [copy.deepcopy(rows[1]), copy.deepcopy(rows[2]), copy.deepcopy(rows[0])]
        for row in permuted:
            row["platforms"].reverse()

        first = aggregate_sector_company_first(rows, DAYS)
        second = aggregate_sector_company_first(permuted, DAYS)

        self.assertEqual(first["modelVersion"], MODEL_VERSION)
        self.assertEqual(first["signals"], second["signals"])
        self.assertEqual(first["evidenceSignals"], second["evidenceSignals"])
        self.assertEqual(first["scores"], second["scores"])
        self.assertEqual(first["metrics"], second["metrics"])
        self.assertEqual(first["diagnostics"]["reliability"], second["diagnostics"]["reliability"])
        self.assertEqual(first["diagnostics"]["concentration"], second["diagnostics"]["concentration"])

    def test_one_post_platform_does_not_receive_an_artificial_twenty_percent(self) -> None:
        rows = []
        for key in ("a", "b", "c"):
            rows.append({
                "key": key,
                "platforms": [
                    platform("eastmoney", (65, 65, 65), samples=(100, 100, 100)),
                    platform("sina", (0, 0, 0), samples=(1, 1, 1)),
                ],
            })

        result = aggregate_sector_company_first(rows, DAYS)
        day_platforms = result["diagnostics"]["platformWeights"]["a"][DAYS[0]]
        weights = {row["platform"]: row["emotionWeight"] for row in day_platforms}

        self.assertLess(weights["sina"], 0.10)
        self.assertGreater(weights["eastmoney"], 0.90)
        self.assertIn("tiny_platform_downweighted", result["diagnostics"]["flags"])
        self.assertGreater(result["signals"]["optimism"], 58.0)

    def test_sample_size_changes_reliability_but_not_complete_point_estimate(self) -> None:
        def rows(sample_count: int) -> list[dict]:
            return [
                {
                    "key": key,
                    "platforms": [platform(
                        "eastmoney",
                        (80, 80, 80),
                        samples=(sample_count, sample_count, sample_count),
                    )],
                }
                for key in ("a", "b", "c")
            ]

        tiny = aggregate_sector_company_first(rows(1), DAYS)
        large = aggregate_sector_company_first(rows(100), DAYS)

        self.assertEqual(tiny["signals"], large["signals"])
        self.assertEqual(tiny["signals"]["optimism"], 80.0)
        self.assertLess(tiny["diagnostics"]["reliability"], large["diagnostics"]["reliability"])
        self.assertEqual(
            [row["effectiveWeight"] for row in large["diagnostics"]["companyWeights"]],
            [0.333333, 0.333333, 0.333333],
        )

    def test_ths_only_evidence_retains_its_fifteen_percent_quality_cap(self) -> None:
        rows = [
            {
                "key": key,
                "platforms": [platform("ths", (100, 100, 100), samples=(100, 100, 100))],
            }
            for key in ("a", "b", "c")
        ]

        result = aggregate_sector_company_first(rows, DAYS)

        self.assertEqual(result["signals"]["optimism"], 57.5)
        self.assertEqual(result["signals"]["attention"], 50.0)
        self.assertAlmostEqual(
            result["diagnostics"]["concentration"]["maxFinalCompanyWeight"],
            0.05,
            places=6,
        )

    def test_missing_day_never_renormalises_fixed_50_30_20_weights(self) -> None:
        rows = []
        for key in ("a", "b", "c"):
            rows.append({
                "key": key,
                "platforms": [{
                    "key": "eastmoney",
                    "days": {
                        DAYS[0]: {
                            "status": "observed", "sampleCount": 100000,
                            "signals": signals(100), "volume": 100,
                        },
                        DAYS[1]: {"status": "verified_zero", "volume": 0},
                        DAYS[2]: {"status": "unobserved"},
                    },
                }],
            })

        result = aggregate_sector_company_first(rows, DAYS)
        day_rows = result["diagnostics"]["effectiveDayWeights"]

        self.assertEqual([row["weight"] for row in day_rows], [0.5, 0.3, 0.2])
        self.assertEqual([row["status"] for row in day_rows], ["observed", "verified_zero", "unobserved"])
        # If the one observed day were silently renormalised, this would be 100.
        self.assertAlmostEqual(result["evidenceSignals"]["optimism"], 75.0, places=1)
        self.assertLess(result["signals"]["optimism"], 76.0)
        self.assertIn("verified_zero_day", result["diagnostics"]["flags"])
        self.assertIn("unobserved_day", result["diagnostics"]["flags"])

    def test_ths_is_capped_for_emotion_and_excluded_from_attention_and_volume(self) -> None:
        def make_rows(ths_attention: float, ths_volume: float) -> list[dict]:
            result = []
            for key in ("a", "b", "c"):
                eastmoney = platform("eastmoney", (55, 55, 55), samples=(100, 100, 100), volumes=(10, 10, 10))
                for row in eastmoney["days"].values():
                    row["signals"]["attention"] = 40
                ths = platform("ths", (100, 100, 100), samples=(100, 100, 100), volumes=(ths_volume,) * 3)
                for row in ths["days"].values():
                    row["signals"]["attention"] = ths_attention
                result.append({"key": key, "platforms": [eastmoney, ths]})
            return result

        first = aggregate_sector_company_first(make_rows(100, 10000), DAYS)
        second = aggregate_sector_company_first(make_rows(0, 0), DAYS)
        ths_row = next(
            row for row in first["diagnostics"]["platformWeights"]["a"][DAYS[0]]
            if row["platform"] == "ths"
        )

        self.assertLessEqual(ths_row["emotionWeight"], 0.15)
        self.assertEqual(ths_row["attentionWeight"], 0.0)
        self.assertFalse(ths_row["volumeEligible"])
        self.assertEqual(first["signals"]["attention"], second["signals"]["attention"])
        self.assertEqual(first["metrics"]["coverageAdjustedVolume"], 10.0)
        self.assertEqual(first["metrics"], second["metrics"])
        self.assertGreater(first["signals"]["optimism"], 55.0)

    def test_missing_company_lowers_reliability_without_breaching_forty_percent(self) -> None:
        full = aggregate_sector_company_first(
            [company("a", 80), company("b", 80), company("c", 80)], DAYS
        )
        partial = aggregate_sector_company_first(
            [company("a", 80), company("b", 80)], DAYS
        )

        self.assertLess(partial["diagnostics"]["reliability"], full["diagnostics"]["reliability"])
        self.assertLess(partial["signals"]["optimism"], full["signals"]["optimism"])
        self.assertLessEqual(
            partial["diagnostics"]["concentration"]["maxFinalCompanyWeight"],
            0.40,
        )
        self.assertFalse(partial["diagnostics"]["concentration"]["capBreached"])
        self.assertIn("missing_companies", partial["diagnostics"]["flags"])
        self.assertIn("conditional_company_concentration_high", partial["diagnostics"]["flags"])
        missing = next(
            row for row in partial["diagnostics"]["companyWeights"]
            if row["key"] == "__missing__"
        )
        self.assertAlmostEqual(missing["neutralisedWeight"], 1 / 3, places=6)

    def test_adapter_understands_current_daily_raw_and_keeps_absence_unobserved(self) -> None:
        source_rows = [{
            "companyKey": "a",
            "platform": "eastmoney",
            "verifiedZeroDays": [DAYS[1]],
            "analysis": {
                "dailyRaw": [{
                    "date": DAYS[0],
                    "available": True,
                    "sampleCount": 20,
                    "metrics": {"net": 0.2, "fomo": 0.1, "panic": 0.02, "newcomer": 0.04, "intense": 0.2},
                }],
                "activity": {
                    "dailyPostCounts": [{
                        "date": DAYS[0], "postCount": 20, "equivalentPosts": 18,
                    }],
                },
            },
        }]

        companies = companies_from_source_analyses(source_rows, DAYS)
        statuses = [
            companies[0]["platforms"][0]["days"][day]["status"] for day in DAYS
        ]

        self.assertEqual(statuses, ["observed", "verified_zero", "unobserved"])
        result = aggregate_sector_company_first(companies, DAYS)
        self.assertTrue(result["available"])
        self.assertEqual(
            [row["weight"] for row in result["diagnostics"]["effectiveDayWeights"]],
            [0.5, 0.3, 0.2],
        )

    def test_adapter_prefers_platform_calibrated_daily_signals(self) -> None:
        source_rows = [{
            "companyKey": "a",
            "platform": "eastmoney",
            "dailySignals": {
                DAYS[0]: {
                    "status": "observed",
                    "sampleCount": 25,
                    "signals": signals(82, attention=73),
                    "volume": 12,
                },
            },
            "analysis": {
                "dailyRaw": [{
                    "date": DAYS[0], "available": True, "sampleCount": 25,
                    "metrics": {"net": -1, "fomo": 0, "panic": 1, "newcomer": 0, "intense": 0},
                }],
                "activity": {"dailyPostCounts": [{"date": DAYS[0], "equivalentPosts": 1}]},
            },
        }]

        companies = companies_from_source_analyses(source_rows, DAYS)
        calibrated = companies[0]["platforms"][0]["days"][DAYS[0]]

        self.assertEqual(calibrated["signals"]["optimism"], 82.0)
        self.assertEqual(calibrated["signals"]["attention"], 73.0)
        self.assertEqual(calibrated["volume"], 12.0)


if __name__ == "__main__":
    unittest.main()
