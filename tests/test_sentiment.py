import unittest

from sentiment import (
    classify_persona_evidence,
    dual_directional_condition,
    grassroots_style_weight,
    has_negating_prefix,
    sentiment_score,
    trade_switch_adjustment,
    valid_text,
)


class NegationTests(unittest.TestCase):
    def test_negation_reverses_phrase(self):
        self.assertLess(sentiment_score("不会上涨"), 0)
        self.assertLess(sentiment_score("这票没有利好"), 0)

    def test_no_cross_clause_reversal(self):
        # 逗号后的分句不被前面的否定反转
        self.assertGreater(sentiment_score("不看好大盘，龙头继续上涨"), 0)

    def test_trade_switch_negative_when_selling_current(self):
        self.assertLess(trade_switch_adjustment("卖出当前标的，换入别的强势股"), 0)
        self.assertGreater(trade_switch_adjustment("卖出别的票，加仓这只股票", topic="这只股票"), 0)

    def test_dual_directional_condition_is_neutral(self):
        self.assertTrue(dual_directional_condition("如果突破就买入加仓，如果跌破就卖出止损"))
        score = sentiment_score("如果突破就买入加仓，如果跌破就卖出止损")
        self.assertLessEqual(abs(score), 1)


class PersonaTests(unittest.TestCase):
    def test_persona_mutually_exclusive_priority(self):
        # 家庭词+交易词并存：家庭优先
        label, reason, _ = classify_persona_evidence("孩子学费要交了，把这只票清仓")
        self.assertEqual(label, "baoma")
        # 新手词+交易词：新手
        label, _, _ = classify_persona_evidence("小白求问，现在加仓还来得及吗")
        self.assertEqual(label, "novice")
        # 只有交易词：散户
        label, _, _ = classify_persona_evidence("突破均线满仓干")
        self.assertEqual(label, "retail")
        # 无证据：None
        label, _, _ = classify_persona_evidence("今天天气不错")
        self.assertIsNone(label)

    def test_masked_text_removes_evidence_words(self):
        # 遮蔽只针对本类证据正则：新手分支遮蔽新手词，保留交易词
        _, _, masked = classify_persona_evidence("我是新手小白，第一次开户，请问怎么加仓")
        for word in ("新手", "小白", "第一次", "请问", "怎么"):
            self.assertNotIn(word, masked)
        self.assertIn("加仓", masked)


class GrassrootsAndFilterTests(unittest.TestCase):
    def test_certified_zero_weight(self):
        self.assertEqual(grassroots_style_weight({"certified": True, "title": "复盘"}), 0.0)

    def test_long_professional_text_downweighted(self):
        long_research = {"title": "复盘" * 300, "originalLength": 1500, "stockTagCount": 0, "kind": "post"}
        short_voice = {"title": "我满仓了怎么办", "originalLength": 20, "stockTagCount": 0, "kind": "post"}
        self.assertLess(grassroots_style_weight(long_research), grassroots_style_weight(short_voice))

    def test_spam_and_marketing_rejected(self):
        self.assertFalse(valid_text("加微信进群领取资料"))
        self.assertFalse(valid_text("签到"))
        self.assertTrue(valid_text("今天这票跌得我看不懂了，求安慰"))


if __name__ == "__main__":
    unittest.main()
