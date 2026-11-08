"""Run: python3 -m unittest discover -s stock-analyzer/tests  (from repo root)
   or: python3 -m unittest tests/test_analyzer.py (from stock-analyzer/)"""
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analyze as A  # noqa: E402


class Indicators(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(A.sma([1, 2, 3, 4, 5], 3)[-3:], [2.0, 3.0, 4.0])
        self.assertIsNone(A.sma([1, 2], 3)[-1])

    def test_ema_seed_and_length(self):
        xs = [float(i) for i in range(1, 40)]
        e = A.ema(xs, 10)
        self.assertEqual(len(e), 39)
        self.assertIsNone(e[8])
        self.assertAlmostEqual(e[9], 5.5)
        self.assertGreater(e[-1], e[-2])

    def test_rsi_extremes(self):
        up = [100 + i for i in range(40)]
        down = [100 - i for i in range(40)]
        self.assertAlmostEqual(A.rsi(up)[-1], 100.0)
        self.assertAlmostEqual(A.rsi(down)[-1], 0.0)
        flat_then_mixed = [100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100, 101]
        r = A.rsi(flat_then_mixed)[-1]
        self.assertTrue(40 <= r <= 60)

    def test_macd_shapes(self):
        xs = [100 + (i % 7) + i * 0.3 for i in range(80)]
        line, sig, hist = A.macd(xs)
        self.assertEqual(len(line), len(sig), len(hist))
        self.assertIsNone(line[24]); self.assertIsNotNone(line[25])
        self.assertIsNone(sig[32]); self.assertIsNotNone(sig[33])
        self.assertAlmostEqual(hist[-1], line[-1] - sig[-1])

    def test_atr_positive(self):
        h = A.synthetic_history("TEST.NS", 60, seed=1)
        a = A.atr(h["high"], h["low"], h["close"])
        self.assertIsNone(a[13]); self.assertGreater(a[-1], 0)

    def test_compute_indicators_fields(self):
        ind = A.compute_indicators(A.synthetic_history("X.NS", 300, seed=3))
        for k in ("rsi14", "sma200", "macd_hist", "atr14", "pos_52w_pct", "ext_atr"):
            self.assertIsNotNone(ind[k], k)
        self.assertTrue(0 <= ind["rsi14"] <= 100)
        self.assertTrue(0 <= ind["pos_52w_pct"] <= 100)


class Sentiment(unittest.TestCase):
    def test_signs(self):
        self.assertGreater(A.score_headline("Infosys surges after beating profit estimates"), 0)
        self.assertLess(A.score_headline("SEBI probe: Adani shares plunge 8%"), 0)
        self.assertEqual(A.score_headline("Company announces annual general meeting date"), 0.0)

    def test_negation(self):
        self.assertLess(A.score_headline("Bank fails to beat estimates, shares fall"), 0)
        self.assertGreater(A.score_headline("No loss this quarter as revenue rises"), 0)

    def test_summary_recency_weight(self):
        now = dt.datetime.now(dt.timezone.utc)
        old = (now - dt.timedelta(days=6)).isoformat()
        new = (now - dt.timedelta(hours=1)).isoformat()
        items = [{"title": "Shares plunge on fraud probe", "published": old},
                 {"title": "Shares surge on record profit", "published": new}]
        s = A.sentiment_summary(items, now)
        self.assertGreater(s["score"], 0)
        self.assertEqual((s["pos"], s["neg"]), (1, 1))


class Scoring(unittest.TestCase):
    def strong(self):
        return {"close": 110, "sma50": 100, "sma200": 90, "sma20_slope_pct": 1.0, "rsi14": 58,
                "macd_hist": 1.0, "macd_hist_prev": 0.5, "ret_60d_pct": 15, "vol_ratio": 1.5,
                "change_1d_pct": 1.2, "ext_atr": 1.0, "realized_vol_20d": 20, "atr14": 2.0}

    def weak(self):
        return {"close": 80, "sma50": 100, "sma200": 90, "sma20_slope_pct": -1.0, "rsi14": 25,
                "macd_hist": -1.0, "macd_hist_prev": -0.5, "ret_60d_pct": -15, "vol_ratio": 1.6,
                "change_1d_pct": -3.0, "ext_atr": -2.5, "realized_vol_20d": 60, "atr14": 2.0}

    def test_strong_vs_weak(self):
        bench = {"ret_60d_pct": 3.0}
        pos = {"score": 0.4, "n": 6, "pos": 4, "neg": 0, "neu": 2}
        neg = {"score": -0.4, "n": 6, "pos": 0, "neg": 4, "neu": 2}
        hi = A.score_stock(self.strong(), pos, bench)
        lo = A.score_stock(self.weak(), neg, bench)
        self.assertGreaterEqual(hi["score"], 85)
        self.assertLessEqual(lo["score"], 20)
        self.assertIn("Below 200DMA", lo["flags"])
        self.assertIn("Negative news cluster", lo["flags"])
        self.assertTrue(any(f.startswith("Oversold") for f in lo["flags"]))
        self.assertEqual(sum(c["max"] for c in hi["components"].values()), 100)

    def test_actions(self):
        self.assertEqual(A.action_for(80, [], "risk-on")[0], "Accumulate")
        self.assertEqual(A.action_for(80, ["Overbought (RSI 74)"], "risk-on")[0], "Hold / add on dips")
        self.assertEqual(A.action_for(80, [], "risk-off")[0], "Hold / add on dips")
        self.assertEqual(A.action_for(45, [], "risk-on")[0], "Watch")
        self.assertEqual(A.action_for(20, [], "risk-on")[0], "Avoid / reduce")

    def test_position_size(self):
        ind = {"close": 1000.0, "atr14": 20.0}
        p = A.position_size(ind, capital=1_000_000, risk_pct=1.0, max_pos_pct=10.0)
        self.assertEqual(p["stop"], 960.0)
        self.assertEqual(p["qty"], 100)        # 10,000 risk / 40 per share = 250, capped at 100 by 10%
        self.assertEqual(p["value"], 100000.0)
        self.assertEqual(p["target_1r"], 1080.0)
        p2 = A.position_size(ind, capital=100_000, risk_pct=1.0, max_pos_pct=50.0)
        self.assertEqual(p2["qty"], 25)        # 1,000 / 40

    def test_regime(self):
        bull = {"close": 110, "sma200": 100, "sma50": 105, "ret_20d_pct": 2.0}
        bear = {"close": 90, "sma200": 100, "sma50": 95, "ret_20d_pct": -4.0}
        self.assertEqual(A.market_regime(bull, 12.0)["state"], "risk-on")
        self.assertEqual(A.market_regime(bear, 22.0)["state"], "risk-off")
        self.assertEqual(A.market_regime(None, None)["state"], "unknown")


class EndToEnd(unittest.TestCase):
    def test_offline_run_writes_brief(self):
        with tempfile.TemporaryDirectory() as d:
            out = A.run(Path(A.HERE) / "watchlist.json", Path(d), offline=True, use_ai=False)
            b = json.loads(out.read_text())
            self.assertTrue(b["sample"])
            self.assertEqual(len(b["stocks"]), 20)
            self.assertEqual([s["rank"] for s in b["stocks"]], list(range(1, 21)))
            self.assertTrue(all(0 <= s["score"] <= 100 for s in b["stocks"]))
            self.assertIn(b["regime"]["state"], ("risk-on", "neutral", "risk-off"))
            self.assertTrue(5 <= b["benchmark"]["vix"] <= 40)


if __name__ == "__main__":
    unittest.main()
