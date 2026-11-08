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


class DeepDive(unittest.TestCase):
    """deepdive.py helpers on synthetic data (no network)."""

    def setUp(self):
        import deepdive as D
        self.D = D

    def fundamentals(self):
        rows = lambda vals, dates: [{"date": d, "value": v, "currency": "INR"} for d, v in zip(dates, vals)]  # noqa: E731
        fy = ["2023-03-31", "2024-03-31", "2025-03-31", "2026-03-31"]
        q = ["2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30", "2026-09-30"]
        return {
            "annualTotalRevenue": rows([100e9, 110e9, 125e9, 150e9], fy),
            "annualNetIncome": rows([10e9, 12e9, 15e9, 18e9], fy),
            "annualOperatingIncome": rows([15e9, 17e9, 20e9, 25e9], fy),
            "annualStockholdersEquity": rows([80e9, 90e9, 100e9, 112e9], fy),
            "annualTotalDebt": rows([30e9, 28e9, 25e9, 20e9], fy),
            "annualCashAndCashEquivalents": rows([5e9, 6e9, 8e9, 10e9], fy),
            "annualDilutedAverageShares": rows([1e9, 1e9, 1.02e9, 1.05e9], fy),
            "annualInterestExpense": rows([3e9, 3e9, 2.5e9, 2e9], fy),
            "annualFreeCashFlow": rows([8e9, 9e9, 12e9, 14e9], fy),
            "quarterlyTotalRevenue": rows([35e9, 36e9, 38e9, 40e9, 42e9], q),
            "quarterlyNetIncome": rows([4e9, 4.2e9, 4.5e9, 4.8e9, 5e9], q),
            "quarterlyDilutedAverageShares": rows([1.05e9] * 5, q),
            "quarterlyTotalDebt": rows([20e9] * 5, q),
            "quarterlyCashAndCashEquivalents": rows([10e9] * 5, q),
            "quarterlyStockholdersEquity": rows([112e9] * 5, q),
            "trailingTotalRevenue": rows([156e9], ["2026-09-30"]),
            "trailingNetIncome": rows([18.5e9], ["2026-09-30"]),
            "trailingEBITDA": rows([30e9], ["2026-09-30"]),
            "trailingFreeCashFlow": rows([15e9], ["2026-09-30"]),
        }

    def test_period_table_and_growth(self):
        D = self.D
        ann = D.enrich_rows(D.period_table(self.fundamentals(), "annual", D.ANNUAL))
        self.assertEqual(len(ann), 4)
        self.assertAlmostEqual(ann[-1]["revenue_growth_pct"], 20.0)
        self.assertAlmostEqual(ann[-1]["net_margin_pct"], 12.0)
        self.assertAlmostEqual(ann[-1]["net_debt"], 10e9)
        self.assertAlmostEqual(ann[-1]["share_count_change_pct"], (1.05 / 1.02 - 1) * 100)
        self.assertAlmostEqual(ann[-1]["interest_coverage"], 12.5)

    def test_valuation_multiples(self):
        D = self.D
        hist = A.synthetic_history("X.NS", 1300, seed=5)
        hist["close"][-1] = 200.0
        v = D.valuation(self.fundamentals(), hist, financial=False)
        self.assertAlmostEqual(v["market_cap"], 200.0 * 1.05e9)
        self.assertAlmostEqual(v["enterprise_value"], v["market_cap"] + 10e9)
        self.assertAlmostEqual(v["pe_ttm"], v["market_cap"] / 18.5e9)
        self.assertAlmostEqual(v["ev_ebitda_ttm"], v["enterprise_value"] / 30e9)
        self.assertAlmostEqual(v["fcf_yield_pct"], 15e9 / v["market_cap"] * 100)
        self.assertEqual(len(v["history"]), 4)
        bank = D.valuation(self.fundamentals(), hist, financial=True)
        self.assertIsNone(bank["enterprise_value"]); self.assertIsNone(bank["fcf_yield_pct"])
        self.assertIsNotNone(bank["pb"])

    def test_levels_and_technicals(self):
        D = self.D
        hist = A.synthetic_history("Y.NS", 400, seed=9)
        lv = D.levels(hist)
        price = hist["close"][-1]
        self.assertTrue(all(s < price for s in lv["support"]))
        self.assertTrue(all(r > price for r in lv["resistance"]))
        self.assertLessEqual(lv["low_52w"], price); self.assertGreaterEqual(lv["high_52w"], price)
        t = D.technicals(hist)
        self.assertEqual(len(t["daily_last_20"]), 20)
        self.assertTrue(1 <= len(t["weekly"]) <= 26)

    def test_prompts_fill_ticker_and_drop_empty_columns(self):
        D = self.D
        hist = A.synthetic_history("Z.NS", 400, seed=2)
        f = self.fundamentals()
        pack = {"symbol": "Z.NS", "name": "Zed Ltd", "currency": "INR", "financial_company": False,
                "annual": D.enrich_rows(D.period_table(f, "annual", D.ANNUAL)),
                "quarterly": D.enrich_rows(D.period_table(f, "quarterly", D.QUARTERLY)),
                "valuation": D.valuation(f, hist, False), "technicals": D.technicals(hist),
                "peers": [], "news": {"Company news": [{"title": "Zed wins order", "source": "X", "published": "2026-11-01T00:00:00+00:00"}]}}
        prompts = D.build_prompts(pack)
        self.assertEqual([p["key"] for p in prompts], ["business", "financials", "valuation", "competitors", "risks", "chart"])
        self.assertIn("Zed Ltd (Z.NS)", prompts[0]["prompt"])
        self.assertNotIn("[TICKER]", prompts[0]["prompt"])
        self.assertNotIn("| FCF |", prompts[1]["data"].split("Quarterly")[1])  # quarterly FCF absent -> column dropped
        self.assertIn("₹", prompts[1]["data"])
