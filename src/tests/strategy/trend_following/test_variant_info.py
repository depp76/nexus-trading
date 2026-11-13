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
            self.assertIn("Data", [h for h, _ in d.sections])

    def test_text_follows_the_variant_flags(self):
        self.assertNotIn("L1 market regime", _text(describe_variant("A0")))
        self.assertIn("L1 market regime", _text(describe_variant("A1")))
        self.assertNotIn("V: breakout-day volume", _text(describe_variant("A1")))
        self.assertIn("V: breakout-day volume", _text(describe_variant("A2")))
        self.assertIn("F: foreign + institution", _text(describe_variant("A3")))
        self.assertNotIn("FX:", _text(describe_variant("A4")))
        self.assertIn("FX:", _text(describe_variant("A5")))
        self.assertIn("Needs investor-flow data", _text(describe_variant("A5")))
        self.assertIn("Price data only", _text(describe_variant("C1")))

    def test_scoring_rows_reflect_their_overrides(self):
        self.assertIn("volatility normalised", _text(describe_variant("C1")))
        self.assertIn("no volatility normalisation", _text(describe_variant("C2")))
        self.assertIn("Range52", _text(describe_variant("C1")))
        self.assertIn("252-day high", _text(describe_variant("C3")))
        self.assertIn("No Donchian 10 exit", _text(describe_variant("C1")))

    def test_numbers_come_from_the_params(self):
        p = StrategyParams(max_positions=8, min_avg_trading_value=50e8, breakout_window=25)
        text = _text(describe_variant("A1", p))
        self.assertIn("up to 8 names", text)
        self.assertIn("50억 KRW", text)
        self.assertIn("Donchian 25", text)
        self.assertIn("top 8", _text(describe_variant("B", p)))


if __name__ == "__main__":
    unittest.main()
