from __future__ import annotations

import copy
import unittest

from channel_fairness_model import (
    CHANNELS,
    MODEL_VERSION,
    aggregate_channel_fairness,
)


DAYS = ("2026-08-06", "2026-08-05", "2026-08-04")
HUGE_N = 10**12


def all_signals(value: float, *, attention: float | None = None) -> dict[str, float]:
    return {
        "optimism": value,
        "fomo": value,
        "panic": max(0.0, value - 50.0),
        "newcomer": value,
        "intensity": value,
        "attention": value if attention is None else attention,
    }


def source(
    channel: str,
    unit: str,
    values: tuple[float, float, float] = (60.0, 60.0, 60.0),
    *,
    samples: tuple[int, int, int] = (HUGE_N, HUGE_N, HUGE_N),
    attention: float | None = None,
) -> dict:
    return {
        "channel": channel,
        "unitKey": unit,
        "dailySignals": {
            day: {
                "status": "observed",
                "sampleCount": sample_count,
                "signals": all_signals(value, attention=attention),
            }
            for day, value, sample_count in zip(DAYS, values, samples)
        },
    }


def specs(
    *,
    units: dict[str, list[str]] | None = None,
    modes: dict[str, str] | None = None,
    ths_attention: bool = True,
) -> dict[str, dict]:
    units = units or {channel: [f"{channel}-unit"] for channel in CHANNELS}
    modes = modes or {}
    return {
        channel: {
            "mode": modes.get(channel, "company"),
            "expectedUnits": units[channel],
            "allowAttention": ths_attention if channel == "ths" else True,
        }
        for channel in CHANNELS
    }


class ChannelFairnessModelTests(unittest.TestCase):
    def test_four_complete_channels_have_exactly_twenty_five_percent_seats(self) -> None:
        values = {"eastmoney": 80.0, "sina": 60.0, "ths": 40.0, "taoguba": 20.0}
        rows = [source(channel, f"{channel}-unit", (value, value, value)) for channel, value in values.items()]

        result = aggregate_channel_fairness(rows, DAYS, specs())
        weights = result["diagnostics"]["channelWeights"]

        self.assertEqual(result["modelVersion"], MODEL_VERSION)
        self.assertEqual([row["targetWeight"] for row in weights], [0.25] * 4)
        self.assertTrue(all(row["formalInfluence"] <= 0.25 for row in weights))
        self.assertEqual(result["diagnostics"]["concentration"]["maxTargetChannelWeight"], 0.25)
        self.assertFalse(result["diagnostics"]["concentration"]["capBreached"])
        # Four raw channel seats average to 50.  No source can dominate the vote.
        self.assertEqual(result["signals"]["optimism"], 50.0)

    def test_copying_a_source_or_adding_samples_never_creates_an_extra_vote(self) -> None:
        base_row = source("eastmoney", "eastmoney-unit", (100.0, 100.0, 100.0), samples=(12, 12, 12))
        base = aggregate_channel_fairness([base_row], DAYS, specs())
        duplicated = aggregate_channel_fairness([base_row, copy.deepcopy(base_row)], DAYS, specs())
        more_samples_row = source(
            "eastmoney", "eastmoney-unit", (100.0, 100.0, 100.0), samples=(120, 120, 120)
        )
        more_samples = aggregate_channel_fairness([more_samples_row], DAYS, specs())

        self.assertEqual(base["signals"], duplicated["signals"])
        self.assertEqual(base["scores"], duplicated["scores"])
        self.assertIn("duplicate_unit_sources_collapsed", duplicated["diagnostics"]["flags"])
        # More observations reduce neutral shrinkage, but Eastmoney still owns one 25% seat.
        self.assertGreater(more_samples["signals"]["optimism"], base["signals"]["optimism"])
        for result in (base, duplicated, more_samples):
            eastmoney = result["diagnostics"]["channelWeights"][0]
            self.assertEqual(eastmoney["targetWeight"], 0.25)
            self.assertLessEqual(eastmoney["formalInfluence"], 0.25)

    def test_missing_channels_keep_neutral_seats_without_reallocation(self) -> None:
        result = aggregate_channel_fairness(
            [source("eastmoney", "eastmoney-unit", (100.0, 100.0, 100.0))],
            DAYS,
            specs(),
        )
        weights = {row["channel"]: row for row in result["diagnostics"]["channelWeights"]}

        # 50 + Eastmoney's one quarter of a +50 deviation = 62.5.
        self.assertEqual(result["signals"]["optimism"], 62.5)
        self.assertEqual(weights["sina"]["targetWeight"], 0.25)
        self.assertEqual(weights["sina"]["formalInfluence"], 0.0)
        self.assertEqual(weights["sina"]["neutralisedWeight"], 0.25)
        self.assertIn("missing_channels_neutralised", result["diagnostics"]["flags"])

    def test_three_company_units_keep_equal_targets_but_shrink_by_own_precision(self) -> None:
        expected = ["a", "b", "c"]
        configured_units = {channel: ([*expected] if channel == "eastmoney" else [f"{channel}-unit"]) for channel in CHANNELS}
        configured = specs(units=configured_units)
        rows_a = [
            source("eastmoney", "a", (100.0,) * 3, samples=(1, 1, 1)),
            source("eastmoney", "b", (0.0,) * 3, samples=(HUGE_N,) * 3),
            source("eastmoney", "c", (50.0,) * 3, samples=(50, 50, 50)),
        ]
        rows_b = [
            source("eastmoney", "a", (100.0,) * 3, samples=(HUGE_N,) * 3),
            source("eastmoney", "b", (0.0,) * 3, samples=(1, 1, 1)),
            source("eastmoney", "c", (50.0,) * 3, samples=(50, 50, 50)),
        ]

        first = aggregate_channel_fairness(rows_a, DAYS, configured)
        second = aggregate_channel_fairness(rows_b, DAYS, configured)
        first_units = first["diagnostics"]["unitWeights"]["eastmoney"]
        second_units = second["diagnostics"]["unitWeights"]["eastmoney"]

        # Raw evidence and immutable target seats stay equal.  Only each seat's
        # usable influence changes with that same unit's own n/(n+12) precision.
        self.assertEqual(first["evidenceSignals"]["optimism"], 50.0)
        self.assertEqual(second["evidenceSignals"]["optimism"], 50.0)
        self.assertEqual([row["targetWithinChannel"] for row in first_units], [0.333333] * 3)
        self.assertEqual([row["targetFinalWeight"] for row in first_units], [0.083333] * 3)
        self.assertEqual(
            [row["targetFinalWeight"] for row in first_units],
            [row["targetFinalWeight"] for row in second_units],
        )
        self.assertLess(first["signals"]["optimism"], 50.0)
        self.assertGreater(second["signals"]["optimism"], 50.0)
        self.assertAlmostEqual(
            first["signals"]["optimism"] + second["signals"]["optimism"],
            100.0,
            delta=0.1,
        )
        self.assertLess(first_units[0]["formalInfluence"], first_units[1]["formalInfluence"])
        self.assertGreater(second_units[0]["formalInfluence"], second_units[1]["formalInfluence"])

    def test_large_neutral_units_cannot_lend_precision_to_a_small_extreme_unit(self) -> None:
        configured_units = {
            channel: (["a", "b", "c"] if channel == "eastmoney" else [f"{channel}-unit"])
            for channel in CHANNELS
        }
        configured = specs(units=configured_units)
        rows = [
            source("eastmoney", "a", (100.0,) * 3, samples=(1, 1, 1)),
            source("eastmoney", "b", (50.0,) * 3, samples=(HUGE_N,) * 3),
            source("eastmoney", "c", (50.0,) * 3, samples=(HUGE_N,) * 3),
        ]

        result = aggregate_channel_fairness(rows, DAYS, configured)

        # Only unit a departs from neutral, so only its own 1/(1+12)
        # precision may act on its fixed one-third of the channel seat.
        expected = 50.0 + 0.25 * ((100.0 - 50.0) / 3.0) * (1.0 / 13.0)
        self.assertAlmostEqual(result["signals"]["optimism"], expected, delta=0.051)
        self.assertLess(result["signals"]["optimism"], 51.0)

    def test_sector_mode_uses_explicit_community_units_directly(self) -> None:
        configured_units = {
            channel: (["pcb-concept", "pcb-community", "pcb-etf"] if channel == "ths" else [f"{channel}-unit"])
            for channel in CHANNELS
        }
        configured = specs(units=configured_units, modes={"ths": "sector"})
        rows = [
            source("ths", "pcb-concept", (80.0,) * 3),
            source("ths", "pcb-community", (50.0,) * 3),
            source("ths", "pcb-etf", (20.0,) * 3),
        ]

        result = aggregate_channel_fairness(rows, DAYS, configured)
        ths_summary = next(row for row in result["diagnostics"]["platformSummary"] if row["channel"] == "ths")

        self.assertEqual(result["signals"]["optimism"], 50.0)
        self.assertEqual(ths_summary["mode"], "sector")
        self.assertEqual(ths_summary["expectedUnitCount"], 3)
        self.assertEqual(ths_summary["observedUnitCount"], 3)

    def test_ths_attention_is_neutral_when_channel_spec_disables_it(self) -> None:
        configured = specs(ths_attention=False)
        rows = [source("ths", "ths-unit", (90.0,) * 3, attention=100.0)]

        result = aggregate_channel_fairness(rows, DAYS, configured)
        ths_summary = next(row for row in result["diagnostics"]["platformSummary"] if row["channel"] == "ths")

        self.assertEqual(result["signals"]["attention"], 50.0)
        self.assertEqual(result["diagnostics"]["channelSignals"]["ths"]["attention"], 50.0)
        self.assertEqual(ths_summary["attentionWeight"], 0.0)
        self.assertIn("ths_attention_disabled", result["diagnostics"]["flags"])

    def test_three_trading_days_keep_fixed_fifty_thirty_twenty_weights(self) -> None:
        rows = [source("eastmoney", "eastmoney-unit", (100.0, 50.0, 0.0))]

        result = aggregate_channel_fairness(rows, DAYS, specs())
        day_rows = result["diagnostics"]["effectiveDayWeights"]

        self.assertEqual([row["weight"] for row in day_rows], [0.5, 0.3, 0.2])
        # Eastmoney's unshrunk 3-day signal is 65; its fixed quarter seat moves
        # 50 to 53.75.  The finite-n precision curve remains infinitesimally
        # below one, so assert within the public one-decimal display tolerance.
        self.assertAlmostEqual(result["signals"]["optimism"], 53.75, delta=0.051)
        self.assertEqual(result["evidenceSignals"]["optimism"], 53.8)
        self.assertEqual(
            [row["channelTargetWeights"]["eastmoney"] for row in day_rows],
            [0.25, 0.25, 0.25],
        )

    def test_source_and_spec_order_do_not_change_results(self) -> None:
        configured = specs()
        rows = [
            source("eastmoney", "eastmoney-unit", (70.0, 60.0, 50.0), samples=(30, 20, 10)),
            source("sina", "sina-unit", (40.0, 50.0, 60.0), samples=(10, 20, 30)),
            source("ths", "ths-unit", (80.0, 70.0, 60.0), samples=(8, 9, 10)),
            source("taoguba", "taoguba-unit", (20.0, 30.0, 40.0), samples=(11, 12, 13)),
        ]
        reversed_specs = {key: configured[key] for key in reversed(tuple(configured))}

        first = aggregate_channel_fairness(rows, DAYS, configured)
        second = aggregate_channel_fairness(list(reversed(rows)), tuple(reversed(DAYS)), reversed_specs, day_weights=(0.2, 0.3, 0.5))

        self.assertEqual(first["signals"], second["signals"])
        self.assertEqual(first["evidenceSignals"], second["evidenceSignals"])
        self.assertEqual(first["scores"], second["scores"])
        self.assertEqual(first["breadth"], second["breadth"])
        self.assertEqual(first["diagnostics"]["channelWeights"], second["diagnostics"]["channelWeights"])
        self.assertEqual(first["diagnostics"]["concentration"], second["diagnostics"]["concentration"])


if __name__ == "__main__":
    unittest.main()
