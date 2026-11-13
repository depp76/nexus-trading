"""variant_info.describe_variant: one description per 6-2 matrix row, built
from the Variant flags and StrategyParams so it follows config changes."""
import unittest

from strategy.trend_following import VARIANTS, StrategyParams, describe_variant


def _text(d) -> str:
    return " ".join([d.title, d.summary] + [line for _, lines in d.sections for line in lines])


class TestDescribeVariant(unittest.TestCase):
    def test_every_variant_has_a_description(self):
        for vid, v in VARIANTS.items():
            d = describe_variant(vid)
            self.assertEqual(d.id, vid)
            self.assertTrue(d.title and d.summary)
            self.assertEqual(d.needs_flows, v.needs_flows)
            self.assertIn("데이터", [h for h, _ in d.sections])

    def test_text_follows_the_variant_flags(self):
        self.assertNotIn("L1 시장 국면", _text(describe_variant("A0")))
        self.assertIn("L1 시장 국면", _text(describe_variant("A1")))
        self.assertNotIn("V: 돌파일 거래량", _text(describe_variant("A1")))
        self.assertIn("V: 돌파일 거래량", _text(describe_variant("A2")))
        self.assertIn("F: 최근", _text(describe_variant("A3")))
        self.assertNotIn("FX:", _text(describe_variant("A4")))
        self.assertIn("FX:", _text(describe_variant("A5")))
        self.assertIn("투자자 수급 데이터 필요", _text(describe_variant("A5")))
        self.assertIn("가격 데이터만 사용", _text(describe_variant("C1")))

    def test_scoring_rows_reflect_their_overrides(self):
        self.assertIn("ATR20 / 종가로 나눔", _text(describe_variant("C1")))
        self.assertIn("변동성 정규화 없음", _text(describe_variant("C2")))
        self.assertIn("Range52", _text(describe_variant("C1")))
        self.assertIn("252일 최고가", _text(describe_variant("C3")))
        self.assertIn("Donchian 10 청산은 쓰지 않으며", _text(describe_variant("C1")))

    def test_numbers_come_from_the_params(self):
        p = StrategyParams(max_positions=8, min_avg_trading_value=50e8, breakout_window=25)
        text = _text(describe_variant("A1", p))
        self.assertIn("최대 8종목", text)
        self.assertIn("50억 원", text)
        self.assertIn("Donchian 25", text)
        self.assertIn("상위 8종목", _text(describe_variant("B", p)))


if __name__ == "__main__":
    unittest.main()
