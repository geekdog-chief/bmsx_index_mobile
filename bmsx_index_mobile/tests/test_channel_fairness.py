import unittest

from channel_fairness_model import aggregate_channel_fairness


def row(channel, unit, day, signals, samples=24, status="observed"):
    return {
        "channel": channel,
        "unitKey": unit,
        "dailySignals": {
            day: {"status": status, "signals": dict(signals), "sampleCount": samples}
        },
    }


SIG = {"optimism": 70.0, "fomo": 60.0, "panic": 20.0, "newcomer": 55.0, "intensity": 50.0, "attention": 50.0}
DAYS = ("2026-11-20", "2026-11-21", "2026-11-24")
PRECISION_24 = 24.0 / (24.0 + 12.0)  # n/(n+12) = 2/3


def full_rows(sig=SIG, samples=24):
    rows = []
    for channel, units in (("eastmoney", ["A", "B", "C"]), ("sina", ["A", "B", "C"]),
                             ("ths", ["SEC"]), ("taoguba", ["A", "B", "C"])):
        for day in DAYS:
            for unit in units:
                rows.append(row(channel, unit, day, sig, samples=samples))
    return rows


class FourSeatFairnessTests(unittest.TestCase):
    def specs(self):
        return {
            "eastmoney": {"mode": "company", "expectedUnits": ["A", "B", "C"]},
            "sina": {"mode": "company", "expectedUnits": ["A", "B", "C"]},
            "ths": {"mode": "sector", "expectedUnits": ["SEC"]},
            "taoguba": {"mode": "company", "expectedUnits": ["A", "B", "C"]},
        }

    def test_missing_channel_neutralised_not_redistributed(self):
        rows = [r for r in full_rows() if r["channel"] != "ths"]
        result = aggregate_channel_fairness(rows, DAYS, self.specs())
        self.assertIn("ths_missing", result["diagnostics"]["flags"])
        # 三渠道在场：50 + 0.25*3*(70-50)*(2/3) = 60；缺失的ths按中性50计入，不重分配
        self.assertAlmostEqual(result["signals"]["optimism"], 60.0, places=1)
        for w in result["diagnostics"]["channelWeights"]:
            self.assertAlmostEqual(w["targetWeight"], 0.25)
        self.assertFalse(result["diagnostics"]["concentration"]["capBreached"])

    def test_day_weights_fixed_50_30_20(self):
        rows = []
        opt = {DAYS[0]: 70.0, DAYS[1]: 60.0, DAYS[2]: 40.0}
        for channel, units in (("eastmoney", ["A", "B", "C"]), ("sina", ["A", "B", "C"]),
                               ("ths", ["SEC"]), ("taoguba", ["A", "B", "C"])):
            for day in DAYS:
                for unit in units:
                    rows.append(row(channel, unit, day, dict(SIG, optimism=opt[day])))
        result = aggregate_channel_fairness(rows, DAYS, self.specs())
        # 先逐日收缩再日加权：0.5*13.33+0.3*6.67-0.2*6.67 = 7.33；50+0.25*4*7.33 = 57.3
        self.assertAlmostEqual(result["signals"]["optimism"], 57.3, places=1)

    def test_shrinkage_does_not_borrow_precision(self):
        rows = []
        for channel, units in (("eastmoney", ["A", "B", "C"]), ("sina", ["A", "B", "C"]),
                               ("ths", ["SEC"]), ("taoguba", ["A", "B", "C"])):
            for day in DAYS:
                for unit in units:
                    if unit == "A":
                        rows.append(row(channel, unit, day, dict(SIG, optimism=100.0), samples=1))
                    else:
                        rows.append(row(channel, unit, day, dict(SIG, optimism=50.0), samples=500))
        result = aggregate_channel_fairness(rows, DAYS, self.specs())
        # n=1 → 精度1/13≈0.077：(100-50)*0.077/3≈1.28/渠道 → 50+0.25*4*1.28 ≈ 51.3
        self.assertGreater(result["signals"]["optimism"], 50.0)
        self.assertLess(result["signals"]["optimism"], 55.0)

    def test_duplicate_same_source_idempotent(self):
        rows = full_rows() + full_rows()  # 同源复制
        result = aggregate_channel_fairness(rows, DAYS, self.specs())
        self.assertIn("duplicate_unit_sources_collapsed", result["diagnostics"]["flags"])
        # 复制不产生额外票：50 + 0.25*4*20*(2/3) = 63.3
        self.assertAlmostEqual(result["signals"]["optimism"], 63.3, places=1)

    def test_verified_zero_differs_from_unobserved(self):
        rows = [r for r in full_rows() if r["channel"] != "ths"]
        for day in DAYS:
            rows.append(row("ths", "SEC", day, {}, samples=0, status="verified_zero"))
        result = aggregate_channel_fairness(rows, DAYS, self.specs())
        self.assertNotIn("ths_missing", result["diagnostics"]["flags"])


if __name__ == "__main__":
    unittest.main()
