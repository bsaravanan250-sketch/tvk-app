# Stock Analyzer: a daily, rules-based brief

Every weekday morning before the market opens, this module pulls free public data
from the web, scores each stock on your watchlist, checks whether the overall
market is worth being in, and publishes a ranked brief with a concrete action,
a stop-loss and a position size for each name. The dashboard (`index.html`)
renders it on your phone or laptop.

**What it is:** a disciplined screener plus a news digest. It tells you which
names on your list have trend, momentum, relative strength, volume and headlines
lined up, and how much to risk if you act.

**What it is not:** a prediction engine. Nothing here forecasts prices. Its job
is to stop you buying stretched stocks, averaging down into broken ones, or
sizing a trade so large that one loss hurts. Treat every "Accumulate" as a
candidate to research, not an order to place.

## Quick start

```bash
cd stock-analyzer
python3 analyze.py            # live run: writes data/latest.json and data/brief-YYYY-MM-DD.json
python3 -m http.server 8080   # then open http://localhost:8080/
```

No dependencies beyond Python 3.11+. The dashboard loads `data/latest.json`.
To preview the layout without network access, `python3 analyze.py --offline`
writes a synthetic `data/sample-brief.json` (clearly labelled, never committed)
that the dashboard falls back to when no live brief exists yet.

## Make it daily (3 steps)

1. Merge this folder to the repository's default branch. The cron in
   `.github/workflows/daily-stock-brief.yml` runs at 08:45 IST Monday to
   Friday and commits the brief back to the repo. GitHub only fires scheduled
   workflows on the default branch.
2. Turn on GitHub Pages for the repo (Settings, Pages, deploy from branch, root).
   The dashboard is then live at `https://<user>.github.io/<repo>/stock-analyzer/`.
3. Optional: add an `ANTHROPIC_API_KEY` repository secret. The workflow then
   asks Claude for a short pre-market narrative that cross-checks the scores
   against the headlines. Without the key the brief is still complete.

You can also trigger a run any time from the Actions tab (workflow_dispatch).

## How a stock is scored (0 to 100)

| Component | Max | What earns points |
|---|---|---|
| Trend | 30 | Price above the 50-day and 200-day averages, 50 above 200, 20-day average rising |
| Momentum | 25 | RSI(14) in the 45 to 65 band (full marks), MACD histogram positive and rising |
| Relative strength | 15 | 60-day return versus the benchmark (NIFTY 50 by default), +10% beats it for full marks |
| Volume | 10 | Above-average volume on an up day confirms; heavy volume on a down day scores zero |
| News | 20 | Recency-weighted sentiment of the last week's headlines from a finance lexicon |

Score bands map to actions: **Accumulate** (70+), **Hold / add on dips**
(55 to 69), **Watch** (40 to 54), **Avoid / reduce** (below 40). Two things
override the band:

- **Risk flags** such as RSI above 70 or price more than 2 ATR above the 20-day
  average downgrade Accumulate to Hold. Strong stocks are often the worst
  entries on the day they look strongest.
- **Market regime.** The benchmark's position against its 50 and 200-day
  averages, its 20-day return and the volatility index classify the tape as
  risk-on (full exposure), neutral (60%) or risk-off (25%). In risk-off, no
  stock gets an Accumulate; the best setups become starter-size holds.

## Position sizing

Each card shows a stop 2 ATR below the close and a quantity sized so that
hitting the stop loses `risk_per_trade_pct` of your capital (default 1%),
capped at `max_position_pct` of capital in one name (default 10%). Change
capital in the dashboard and the quantities recompute. Change the defaults in
`watchlist.json`.

## Customising the watchlist

Edit `watchlist.json`. Yahoo Finance symbols: NSE is `TICKER.NS`, BSE is
`TICKER.BO`, US is the plain ticker. For a US list set `"market": "US"`,
`"benchmark": "^GSPC"` and `"volatility_index": "^VIX"`. The `news` field is
the phrase searched on Google News.

## Data sources and their limits

- **Prices:** Yahoo Finance chart API, end-of-day, typically delayed. Free and
  keyless, but unofficial. If Yahoo changes or rate-limits, the run records the
  error per symbol and the dashboard shows it. `errors` in the JSON lists them.
- **Headlines:** Google News RSS, last 7 days per name. Sentiment is a word
  lexicon, which is blunt: it cannot tell "profit falls less than feared" from
  bad news. The optional Claude narrative exists to add that judgement.
- **No fundamentals yet.** Valuation, earnings dates, promoter pledges and
  institutional flows are not in the score. An earnings release can invalidate
  a technical setup overnight. Check the calendar before acting.
- **No backtest.** The weights are sensible defaults, not optimised. Expect
  the signal to be modest; the value is in the discipline and the sizing.

## Tests

```bash
cd stock-analyzer
python3 -m unittest discover -s tests -v
```

## Disclaimer

This tool is for information and education. It is not investment advice, and
nothing in it is a recommendation to buy or sell any security. Markets can
move against every signal here. You alone are responsible for your decisions.
