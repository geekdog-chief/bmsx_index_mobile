from __future__ import annotations

import unittest

import update_index
from sector_catalog import (
    PARENT_SECTORS,
    SECTOR_CATALOG_ID,
    SECTOR_CATALOG_VERSION,
    SECTORS,
)


def leaf(key: str, parent: str, value: float) -> dict[str, object]:
    signals = {
        signal: value
        for signal in (*update_index.SIGNAL_KEYS, "panic", "heat")
    }
    return {
        "key": key,
        "name": key,
        "parentKey": parent,
        "parentName": PARENT_SECTORS[parent],
        "available": True,
        "expectedSourceCount": 4,
        "metrics": {"sourceCount": 4, "platformCount": 3},
        "signals": signals,
    }


class SectorCatalogTests(unittest.TestCase):
    def test_catalog_has_thirty_leaves_eight_parents_and_unique_companies(self) -> None:
        self.assertEqual(SECTOR_CATALOG_VERSION, 3)
        self.assertEqual(SECTOR_CATALOG_ID, "thirty_leaf_sectors_v3_ths_communities")
        self.assertEqual(len(SECTORS), 30)
        self.assertEqual(len(PARENT_SECTORS), 8)
        codes: list[str] = []
        for definition in SECTORS.values():
            representatives = definition["representativeCompanies"]
            self.assertEqual(len(representatives), 3)
            self.assertIn(definition["parentKey"], PARENT_SECTORS)
            self.assertTrue(definition["description"])
            self.assertIsNone(definition["industry"])
            codes.extend(str(company["code"]) for company in representatives)
        self.assertEqual(len(codes), 90)
        self.assertEqual(len(set(codes)), 90)

    def test_every_sector_has_explicit_ths_board_communities(self) -> None:
        community_count = 0
        for definition in SECTORS.values():
            self.assertNotIn("thsStock", definition)
            communities = definition["thsCommunities"]
            self.assertGreaterEqual(len(communities), 1)
            for community in communities:
                self.assertEqual(set(community), {"code", "name", "marketId"})
                self.assertEqual(len(str(community["code"])), 6)
                self.assertTrue(str(community["code"]).isdigit())
                self.assertTrue(str(community["name"]).strip())
                self.assertEqual(community["marketId"], 48)
            community_count += len(communities)
        # 券商保险、工业金属与稀土、基础化工、种业粮食各有两个社区。
        self.assertEqual(community_count, 34)

    def test_verified_ths_board_mappings_are_preserved(self) -> None:
        self.assertEqual(
            SECTORS["pcb"]["thsCommunities"],
            [{"code": "885959", "name": "PCB概念", "marketId": 48}],
        )
        self.assertEqual(
            SECTORS["nonbank_finance"]["thsCommunities"],
            [
                {"code": "881157", "name": "证券", "marketId": 48},
                {"code": "881156", "name": "保险", "marketId": 48},
            ],
        )
        self.assertEqual(
            SECTORS["seeds"]["thsCommunities"],
            [
                {"code": "885812", "name": "农业种植", "marketId": 48},
                {"code": "881101", "name": "种植业与林业", "marketId": 48},
            ],
        )

    def test_source_plan_is_bounded_and_explicit(self) -> None:
        specs, market_keys, sector_keys = update_index.build_source_specs()
        self.assertEqual(len(specs), 308)
        self.assertEqual(len(market_keys), 4)
        platform_totals = {
            platform: sum(str(spec["platform"]) == platform for spec in specs.values())
            for platform in ("eastmoney", "sina", "ths", "taoguba")
        }
        self.assertEqual(
            platform_totals,
            {"eastmoney": 93, "sina": 90, "ths": 35, "taoguba": 90},
        )
        for sector_key, keys in sector_keys.items():
            platforms = [str(specs[key]["platform"]) for key in keys]
            self.assertEqual(platforms.count("eastmoney"), 3)
            self.assertEqual(platforms.count("sina"), 3)
            self.assertEqual(platforms.count("taoguba"), 3)
            self.assertEqual(
                platforms.count("ths"),
                len(SECTORS[sector_key]["thsCommunities"]),
            )

    def test_splitting_one_parent_does_not_increase_its_market_vote(self) -> None:
        unsplit = [
            leaf("tech", "technology", 80.0),
            leaf("farm", "agriculture", 20.0),
        ]
        split = [
            leaf("tech_a", "technology", 80.0),
            leaf("tech_b", "technology", 80.0),
            leaf("tech_c", "technology", 80.0),
            leaf("farm", "agriculture", 20.0),
        ]
        unsplit_parents = update_index.aggregate_sector_parents(unsplit)
        split_parents = update_index.aggregate_sector_parents(split)
        self.assertEqual(len(unsplit_parents), len(update_index.PARENT_SECTORS))
        self.assertEqual(len(split_parents), len(update_index.PARENT_SECTORS))
        for signal in (*update_index.SIGNAL_KEYS, "panic", "heat"):
            unsplit_market = sum(float(row["signals"][signal]) for row in unsplit_parents) / len(unsplit_parents)
            split_market = sum(float(row["signals"][signal]) for row in split_parents) / len(split_parents)
            self.assertEqual(unsplit_market, split_market)
            self.assertEqual(split_market, 12.5 if signal == "panic" else 50.0)


if __name__ == "__main__":
    unittest.main()
