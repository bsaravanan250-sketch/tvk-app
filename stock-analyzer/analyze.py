#!/usr/bin/env python3
"""
Daily stock analyzer.

Pulls free public data (Yahoo Finance price history, Google News RSS headlines),
computes technical indicators, scores headline sentiment, checks the market
regime, and writes a ranked daily brief as JSON that index.html renders.

Standard library only. Optional: `pip install anthropic` + ANTHROPIC_API_KEY
for an AI-written narrative (--ai).

Usage:
  python3 analyze.py                 # live run, writes data/latest.json + dated archive
  python3 analyze.py --offline       # deterministic synthetic data, writes data/sample-brief.json
  python3 analyze.py --ai            # live run + Claude-written narrative
  python3 analyze.py --watchlist my.json --out data/

This is a rules-based screener, not a prediction engine. Nothing here is
financial advice. See README.md for the methodology and its limits.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
IST = ZoneInfo("Asia/Kolkata")


# --------------------------------------------------------------------------- #
# HTTP                                                                        #
# --------------------------------------------------------------------------- #
def http_get(url: str, timeout: int = 20, retries: int = 3) -> bytes:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (2 ** attempt))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
            time.sleep(1.5 * (2 ** attempt))
    raise RuntimeError(f"GET failed after {retries} tries: {url} ({last})")


# --------------------------------------------------------------------------- #
# Data sources                                                                #
# --------------------------------------------------------------------------- #
def fetch_yahoo_history(symbol: str, range_: str = "1y") -> dict:
    """Return {'dates': [...], 'open': [...], 'high': [...], 'low': [...],
    'close': [...], 'volume': [...], 'meta': {...}} from Yahoo's chart API."""
    q = urllib.parse.quote(symbol, safe="")
    err: Exception | None = None
    for host in ("query1", "query2"):
        url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/{q}"
               f"?range={range_}&interval=1d&includePrePost=false&events=div%2Csplit")
        try:
            raw = json.loads(http_get(url))
            result = raw["chart"]["result"][0]
            ts = result["timestamp"]
            quote = result["indicators"]["quote"][0]
            rows = [
                (dt.datetime.fromtimestamp(t, tz=dt.timezone.utc).date().isoformat(),
                 quote["open"][i], quote["high"][i], quote["low"][i], quote["close"][i],
                 quote["volume"][i])
                for i, t in enumerate(ts)
                if quote["close"][i] is not None
            ]
            return {
                "dates": [r[0] for r in rows],
                "open": [r[1] for r in rows],
                "high": [r[2] if r[2] is not None else r[4] for r in rows],
                "low": [r[3] if r[3] is not None else r[4] for r in rows],
                "close": [r[4] for r in rows],
                "volume": [r[5] or 0 for r in rows],
                "meta": result.get("meta", {}),
            }
        except Exception as e:  # noqa: BLE001 - try the other host
            err = e
    raise RuntimeError(f"Yahoo history failed for {symbol}: {err}")


def fetch_google_news(query: str, market: str, limit: int = 12) -> list[dict]:
    """Headlines from Google News RSS for a search phrase."""
    if market == "IN":
        loc = "hl=en-IN&gl=IN&ceid=IN:en"
    else:
        loc = "hl=en-US&gl=US&ceid=US:en"
    q = urllib.parse.quote(f"{query} stock when:7d")
    url = f"https://news.google.com/rss/search?q={q}&{loc}"
    root = ET.fromstring(http_get(url))
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = item.findtext("pubDate") or ""
        src_el = item.find("source")
        source = (src_el.text or "").strip() if src_el is not None else ""
        try:
            published = parsedate_to_datetime(pub).astimezone(dt.timezone.utc)
        except Exception:  # noqa: BLE001
            published = None
        # Google appends " - Source" to titles; strip it when we know the source.
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].strip()
        items.append({
            "title": title,
            "link": link,
            "source": source,
            "published": published.isoformat() if published else None,
        })
        if len(items) >= limit:
            break
    return items


# --------------------------------------------------------------------------- #
# Indicators                                                                  #
# --------------------------------------------------------------------------- #
def sma(xs: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    s = 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(xs: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(xs)
    if len(xs) < n:
        return out
    k = 2.0 / (n + 1)
    seed = sum(xs[:n]) / n
    out[n - 1] = seed
    prev = seed
    for i in range(n, len(xs)):
        prev = xs[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(closes: list[float], n: int = 14) -> list[float | None]:
    """Wilder's RSI."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def macd(closes: list[float]) -> tuple[list[float | None], list[float | None], list[float | None]]:
    e12, e26 = ema(closes, 12), ema(closes, 26)
    line: list[float | None] = [
        (a - b) if a is not None and b is not None else None for a, b in zip(e12, e26)
    ]
    valid = [v for v in line if v is not None]
    sig_valid = ema(valid, 9)
    signal: list[float | None] = [None] * len(closes)
    offset = len(closes) - len(valid)
    for i, v in enumerate(sig_valid):
        signal[offset + i] = v
    hist: list[float | None] = [
        (a - b) if a is not None and b is not None else None for a, b in zip(line, signal)
    ]
    return line, signal, hist


def atr(high: list[float], low: list[float], close: list[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(close)
    if len(close) <= n:
        return out
    trs = [high[0] - low[0]]
    for i in range(1, len(close)):
        trs.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
    prev = sum(trs[1:n + 1]) / n
    out[n] = prev
    for i in range(n + 1, len(close)):
        prev = (prev * (n - 1) + trs[i]) / n
        out[i] = prev
    return out


def pct(a: float | None, b: float | None) -> float | None:
    if a is None or b in (None, 0):
        return None
    return (a / b - 1.0) * 100.0


def realized_vol(closes: list[float], n: int = 20) -> float | None:
    if len(closes) <= n:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - n, len(closes))]
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    return math.sqrt(var) * math.sqrt(252) * 100.0


def compute_indicators(h: dict) -> dict:
    c, hi, lo, v = h["close"], h["high"], h["low"], h["volume"]
    n = len(c)
    s20, s50, s200 = sma(c, 20), sma(c, 50), sma(c, 200)
    r = rsi(c)
    _, _, hist = macd(c)
    a = atr(hi, lo, c)
    v20 = sma([float(x) for x in v], 20)
    last = n - 1

    def at(xs, i):
        return xs[i] if 0 <= i < len(xs) else None

    hi52 = max(c[-252:]) if n else None
    lo52 = min(c[-252:]) if n else None
    pos52 = None
    if hi52 and lo52 and hi52 != lo52:
        pos52 = (c[last] - lo52) / (hi52 - lo52) * 100.0

    ind = {
        "close": c[last],
        "prev_close": at(c, last - 1),
        "change_1d_pct": pct(c[last], at(c, last - 1)),
        "ret_5d_pct": pct(c[last], at(c, last - 5)),
        "ret_20d_pct": pct(c[last], at(c, last - 20)),
        "ret_60d_pct": pct(c[last], at(c, last - 60)),
        "sma20": at(s20, last),
        "sma50": at(s50, last),
        "sma200": at(s200, last),
        "sma20_slope_pct": pct(at(s20, last), at(s20, last - 5)),
        "rsi14": at(r, last),
        "macd_hist": at(hist, last),
        "macd_hist_prev": at(hist, last - 1),
        "atr14": at(a, last),
        "atr_pct": (at(a, last) / c[last] * 100.0) if at(a, last) else None,
        "vol_ratio": (v[last] / at(v20, last)) if at(v20, last) else None,
        "realized_vol_20d": realized_vol(c),
        "high_52w": hi52,
        "low_52w": lo52,
        "pos_52w_pct": pos52,
        "bars": n,
    }
    if ind["atr14"] and ind["sma20"]:
        ind["ext_atr"] = (c[last] - ind["sma20"]) / ind["atr14"]
    else:
        ind["ext_atr"] = None
    return ind


# --------------------------------------------------------------------------- #
# Headline sentiment (finance lexicon)                                        #
# --------------------------------------------------------------------------- #
POS = {
    "beat": 2, "beats": 2, "surge": 2, "surges": 2, "soar": 2, "soars": 2, "record": 1,
    "upgrade": 2, "upgrades": 2, "upgraded": 2, "outperform": 2, "rally": 1, "rallies": 1,
    "jump": 1, "jumps": 1, "gain": 1, "gains": 1, "rises": 1, "rise": 1, "climb": 1,
    "climbs": 1, "strong": 1, "growth": 1, "profit": 1, "buyback": 2, "dividend": 1,
    "order": 1, "orders": 1, "wins": 2, "win": 1, "bags": 2, "approval": 2, "approved": 2,
    "expansion": 1, "launch": 1, "launches": 1, "partnership": 1, "buy": 1, "accumulate": 1,
    "bullish": 2, "upside": 1, "target": 0, "raises": 1, "raised": 1, "hike": 0,
    "robust": 1, "momentum": 1, "high": 1, "all-time": 1, "recovery": 1, "deal": 1,
    "stake": 0, "overweight": 2, "add": 1, "positive": 1, "boost": 1, "boosts": 1,
}
NEG = {
    "plunge": 2, "plunges": 2, "crash": 2, "crashes": 2, "slump": 2, "slumps": 2,
    "fall": 1, "falls": 1, "drop": 1, "drops": 1, "slide": 1, "slides": 1, "tumble": 2,
    "tumbles": 2, "sink": 1, "sinks": 1, "downgrade": 2, "downgrades": 2, "downgraded": 2,
    "underperform": 2, "sell": 1, "miss": 2, "misses": 2, "loss": 2, "losses": 2,
    "weak": 1, "weakness": 1, "probe": 2, "investigation": 2, "fraud": 3, "scam": 3,
    "penalty": 2, "fine": 1, "fined": 2, "lawsuit": 2, "ban": 2, "banned": 2, "default": 2,
    "debt": 1, "layoff": 1, "layoffs": 1, "cuts": 1, "cut": 1, "resign": 1, "resigns": 2,
    "quits": 1, "exit": 1, "bearish": 2, "downside": 1, "warning": 1, "warns": 1,
    "concern": 1, "concerns": 1, "risk": 1, "pressure": 1, "low": 1, "52-week low": 2,
    "sebi": 1, "notice": 1, "show-cause": 2, "delay": 1, "delayed": 1, "recall": 2,
    "outage": 1, "strike": 1, "underweight": 2, "reduce": 1, "negative": 1, "dips": 1,
    "dip": 1, "crackdown": 2, "allegation": 2, "allegations": 2, "fail": 2, "fails": 2,
    "failed": 2,
}
NEGATORS = {"not", "no", "never", "despite", "without", "fails", "fail", "failed"}


def score_headline(title: str) -> float:
    """Return a score in [-1, 1] from the finance lexicon with light negation handling."""
    t = title.lower()
    if "52-week low" in t:
        return -0.6
    words = re.findall(r"[a-z][a-z\-']*", t)
    total = 0.0
    hits = 0
    for i, w in enumerate(words):
        s = POS.get(w, 0) - NEG.get(w, 0)
        if s == 0:
            continue
        if any(words[j] in NEGATORS for j in range(max(0, i - 3), i)):
            s = -s
        total += s
        hits += 1
    if hits == 0:
        return 0.0
    return max(-1.0, min(1.0, total / (2.0 * max(hits, 2))))


def sentiment_summary(items: list[dict], now: dt.datetime) -> dict:
    if not items:
        return {"score": 0.0, "n": 0, "pos": 0, "neg": 0, "neu": 0}
    wsum = 0.0
    wtot = 0.0
    pos = neg = neu = 0
    for it in items:
        s = score_headline(it["title"])
        it["sentiment"] = round(s, 2)
        age_days = 3.0
        if it.get("published"):
            try:
                age_days = max(0.0, (now - dt.datetime.fromisoformat(it["published"])).total_seconds() / 86400)
            except Exception:  # noqa: BLE001
                pass
        w = 1.0 / (1.0 + age_days / 2.0)  # today ~1.0, 2 days old 0.5, a week 0.22
        wsum += s * w
        wtot += w
        if s > 0.1:
            pos += 1
        elif s < -0.1:
            neg += 1
        else:
            neu += 1
    return {"score": round(wsum / wtot, 3) if wtot else 0.0, "n": len(items), "pos": pos, "neg": neg, "neu": neu}


# --------------------------------------------------------------------------- #
# Scoring                                                                     #
# --------------------------------------------------------------------------- #
def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def score_stock(ind: dict, sent: dict, bench: dict | None) -> dict:
    """Composite 0-100 from five components. Each component returns (points, max, note)."""
    parts = []
    flags = []
    reasons_up = []
    reasons_down = []

    # 1. Trend (max 30)
    t = 0.0
    c = ind["close"]
    if ind["sma50"] is not None:
        if c > ind["sma50"]:
            t += 10; reasons_up.append("Above 50-day average")
        else:
            reasons_down.append("Below 50-day average")
    if ind["sma200"] is not None:
        if c > ind["sma200"]:
            t += 10; reasons_up.append("Above 200-day average (long-term uptrend)")
        else:
            reasons_down.append("Below 200-day average"); flags.append("Below 200DMA")
    else:
        t += 5  # not enough history: neutral credit
    if ind["sma50"] is not None and ind["sma200"] is not None and ind["sma50"] > ind["sma200"]:
        t += 5
    if ind["sma20_slope_pct"] is not None and ind["sma20_slope_pct"] > 0:
        t += 5
    parts.append(("trend", round(t, 1), 30))

    # 2. Momentum (max 25)
    m = 0.0
    r = ind["rsi14"]
    if r is not None:
        if 45 <= r <= 65:
            m += 12; reasons_up.append(f"RSI {r:.0f}: healthy momentum, not stretched")
        elif 65 < r <= 70:
            m += 8
        elif 30 <= r < 45:
            m += 6
        elif r > 70:
            m += 2; flags.append(f"Overbought (RSI {r:.0f})"); reasons_down.append(f"RSI {r:.0f}: overbought, chase risk")
        else:
            m += 4; flags.append(f"Oversold (RSI {r:.0f})"); reasons_down.append(f"RSI {r:.0f}: oversold, wait for a base")
    h, hp = ind["macd_hist"], ind["macd_hist_prev"]
    if h is not None:
        if h > 0:
            m += 8; reasons_up.append("MACD positive")
        else:
            reasons_down.append("MACD negative")
        if hp is not None and h > hp:
            m += 5
    parts.append(("momentum", round(m, 1), 25))

    # 3. Relative strength vs benchmark over 60 days (max 15)
    rs = 7.5
    rel = None
    if bench and ind["ret_60d_pct"] is not None and bench.get("ret_60d_pct") is not None:
        rel = ind["ret_60d_pct"] - bench["ret_60d_pct"]
        rs = clamp(7.5 + rel * 0.75, 0, 15)  # +10% vs index -> full marks
        if rel >= 5:
            reasons_up.append(f"Outperformed the index by {rel:.1f}% over 60 days")
        elif rel <= -5:
            reasons_down.append(f"Lagged the index by {abs(rel):.1f}% over 60 days")
    parts.append(("relative_strength", round(rs, 1), 15))

    # 4. Volume confirmation (max 10)
    vol = 5.0
    vr, chg = ind["vol_ratio"], ind["change_1d_pct"]
    if vr is not None and chg is not None:
        if vr >= 1.3 and chg > 0:
            vol = 10; reasons_up.append(f"Volume {vr:.1f}x average on an up day")
        elif vr >= 1.3 and chg < 0:
            vol = 0; reasons_down.append(f"Heavy selling volume ({vr:.1f}x average)")
        elif vr < 0.5:
            vol = 3; flags.append("Thin volume")
    parts.append(("volume", round(vol, 1), 10))

    # 5. News sentiment (max 20)
    ns = 10.0
    if sent["n"]:
        ns = clamp(10 + sent["score"] * 10 * min(1.0, sent["n"] / 5.0), 0, 20)
        if sent["score"] >= 0.2:
            reasons_up.append("Headlines skew positive")
        elif sent["score"] <= -0.2:
            reasons_down.append("Headlines skew negative")
        if sent["neg"] >= 3 and sent["neg"] > sent["pos"]:
            flags.append("Negative news cluster")
    parts.append(("news", round(ns, 1), 20))

    # Risk flags that don't score but change the action
    if ind["ext_atr"] is not None and ind["ext_atr"] > 2.0:
        flags.append(f"Extended {ind['ext_atr']:.1f} ATR above 20DMA")
    if ind["realized_vol_20d"] is not None and ind["realized_vol_20d"] > 45:
        flags.append(f"High volatility ({ind['realized_vol_20d']:.0f}% annualised)")
    if ind["change_1d_pct"] is not None and abs(ind["change_1d_pct"]) >= 5:
        flags.append(f"Moved {ind['change_1d_pct']:+.1f}% yesterday")

    total = round(sum(p[1] for p in parts), 1)
    return {
        "score": total,
        "components": {p[0]: {"points": p[1], "max": p[2]} for p in parts},
        "reasons_up": reasons_up[:4],
        "reasons_down": reasons_down[:4],
        "flags": flags,
        "rel_60d_pct": round(rel, 2) if rel is not None else None,
    }


def action_for(score: float, flags: list[str], regime: str) -> tuple[str, str]:
    """Map score + flags + regime to an action label and a one-line instruction."""
    overbought = any(f.startswith("Overbought") or f.startswith("Extended") for f in flags)
    if score >= 70:
        if overbought:
            return "Hold / add on dips", "Strong setup but stretched. Do not chase; buy a pullback to the 20-day average."
        if regime == "risk-off":
            return "Hold / add on dips", "Strong stock in a weak market. Starter size only until the index reclaims its 200-day average."
        return "Accumulate", "Trend, momentum and news agree. Buy in 2-3 tranches with the stop below."
    if score >= 55:
        return "Hold / add on dips", "Constructive. Hold existing positions; add only on a dip with the stop intact."
    if score >= 40:
        return "Watch", "Mixed signals. No new money. Re-check when price reclaims the 50-day average or news turns."
    return "Avoid / reduce", "Trend and momentum are against you. Avoid fresh buys; tighten or exit existing positions."


def position_size(ind: dict, capital: float, risk_pct: float, max_pos_pct: float) -> dict | None:
    if not ind.get("atr14") or not ind.get("close"):
        return None
    price = ind["close"]
    stop = round(price - 2.0 * ind["atr14"], 2)
    risk_per_share = price - stop
    if risk_per_share <= 0:
        return None
    risk_amt = capital * risk_pct / 100.0
    qty = int(risk_amt // risk_per_share)
    cap_qty = int((capital * max_pos_pct / 100.0) // price)
    qty = max(0, min(qty, cap_qty))
    return {
        "stop": stop,
        "stop_pct": round(-risk_per_share / price * 100.0, 2),
        "qty": qty,
        "value": round(qty * price, 2),
        "risk_amount": round(qty * risk_per_share, 2),
        "target_1r": round(price + 2.0 * risk_per_share, 2),
    }


def market_regime(bench_ind: dict | None, vix_level: float | None) -> dict:
    """Classify the tape so the per-stock actions can be dampened."""
    if not bench_ind:
        return {"state": "unknown", "exposure_pct": 50, "notes": ["Benchmark data unavailable."]}
    notes = []
    score = 0
    c = bench_ind["close"]
    if bench_ind["sma200"] is not None:
        if c > bench_ind["sma200"]:
            score += 1; notes.append("Index above its 200-day average")
        else:
            score -= 1; notes.append("Index below its 200-day average")
    if bench_ind["sma50"] is not None:
        if c > bench_ind["sma50"]:
            score += 1; notes.append("Index above its 50-day average")
        else:
            score -= 1; notes.append("Index below its 50-day average")
    if bench_ind["ret_20d_pct"] is not None:
        notes.append(f"Index {bench_ind['ret_20d_pct']:+.1f}% over 20 days")
        score += 1 if bench_ind["ret_20d_pct"] > 0 else -1
    if vix_level is not None:
        notes.append(f"Volatility index at {vix_level:.1f}")
        if vix_level >= 20:
            score -= 1
        elif vix_level <= 13:
            score += 1
    if score >= 2:
        state, exposure = "risk-on", 100
    elif score <= -2:
        state, exposure = "risk-off", 25
    else:
        state, exposure = "neutral", 60
    return {"state": state, "score": score, "exposure_pct": exposure, "notes": notes}


# --------------------------------------------------------------------------- #
# Offline synthetic data (for tests and a demo brief; clearly labelled)       #
# --------------------------------------------------------------------------- #
def synthetic_history(symbol: str, days: int = 300, seed: int | None = None) -> dict:
    rnd = random.Random(seed if seed is not None else sum(map(ord, symbol)))
    is_vix = "VIX" in symbol.upper()
    price = rnd.uniform(10, 25) if is_vix else rnd.uniform(200, 3000)
    drift = 0.0 if is_vix else rnd.uniform(-0.0008, 0.0012)
    vol = 0.03 if is_vix else rnd.uniform(0.01, 0.025)
    today = dt.date.today()
    dates, o, h, l, c, v = [], [], [], [], [], []
    d = today - dt.timedelta(days=int(days * 1.45))
    while len(c) < days:
        d += dt.timedelta(days=1)
        if d.weekday() >= 5:
            continue
        r = rnd.gauss(drift, vol)
        op = price
        price = max(1.0, price * math.exp(r))
        if is_vix:
            price = min(max(price, 9.0), 35.0)
        hi = max(op, price) * (1 + abs(rnd.gauss(0, vol / 2)))
        lo = min(op, price) * (1 - abs(rnd.gauss(0, vol / 2)))
        dates.append(d.isoformat()); o.append(op); h.append(hi); l.append(lo); c.append(price)
        v.append(int(rnd.lognormvariate(13, 0.5)))
    return {"dates": dates, "open": o, "high": h, "low": l, "close": c, "volume": v,
            "meta": {"longName": symbol, "currency": "INR"}}


SYN_HEADLINES = [
    "{n} shares rise after strong quarterly results", "{n} bags large order from overseas client",
    "Brokerages upgrade {n}, see further upside", "{n} falls as margins miss estimates",
    "{n} board approves dividend and expansion plan", "Regulator issues notice to {n} over disclosures",
    "{n} stock slips amid sector-wide selling pressure", "{n} launches new product line, analysts positive",
]


def synthetic_news(name: str, seed: int) -> list[dict]:
    rnd = random.Random(seed)
    now = dt.datetime.now(dt.timezone.utc)
    picks = rnd.sample(SYN_HEADLINES, k=5)
    return [{"title": t.format(n=name), "link": "#", "source": "Sample",
             "published": (now - dt.timedelta(hours=rnd.randint(2, 150))).isoformat()}
            for t in picks]


# --------------------------------------------------------------------------- #
# Optional AI narrative                                                       #
# --------------------------------------------------------------------------- #
def ai_narrative(brief: dict) -> dict | None:
    """Ask Claude for a short daily note. Returns None if unavailable."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("  AI narrative skipped: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return None
    try:
        import anthropic  # type: ignore
    except ImportError:
        print("  AI narrative skipped: pip install anthropic", file=sys.stderr)
        return None

    slim = {
        "date": brief["date"], "regime": brief["regime"],
        "market_headlines": [h["title"] for h in brief.get("market_news", [])][:10],
        "stocks": [{
            "symbol": s["symbol"], "name": s["name"], "score": s["score"], "action": s["action"],
            "flags": s["flags"], "reasons_up": s["reasons_up"], "reasons_down": s["reasons_down"],
            "headlines": [h["title"] for h in s["news"]][:6],
        } for s in brief["stocks"]],
    }
    system = (
        "You are a sell-side-quality equity analyst writing a pre-market note for one retail "
        "investor. You are given rules-based scores, indicator reasons and raw headlines. "
        "Be concrete and sceptical: call out where headlines contradict the technical score, "
        "where a score is driven by one component only, and what would change your mind. "
        "Never invent facts not present in the data. Never promise returns. "
        "Respond with JSON only: {\"market_note\": string (<=120 words), "
        "\"top_ideas\": [{\"symbol\": string, \"note\": string (<=40 words)}] (max 3), "
        "\"avoid\": [{\"symbol\": string, \"note\": string (<=30 words)}] (max 3), "
        "\"caution\": string (<=40 words)}"
    )
    client = anthropic.Anthropic()
    try:
        resp = client.messages.create(
            model="claude-opus-5-5",
            max_tokens=4000,
            system=system,
            messages=[{"role": "user", "content": json.dumps(slim)}],
        )
    except Exception as e:  # noqa: BLE001
        print(f"  AI narrative failed: {e}", file=sys.stderr)
        return None
    if resp.stop_reason == "refusal":
        print("  AI narrative skipped: request refused by safety classifier", file=sys.stderr)
        return None
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        out = json.loads(text)
        out["model"] = resp.model
        return out
    except json.JSONDecodeError:
        return {"market_note": text[:800], "model": resp.model}


# --------------------------------------------------------------------------- #
# Main pipeline                                                               #
# --------------------------------------------------------------------------- #
def analyze_symbol(entry: dict, hist: dict, news: list[dict], bench_ind: dict | None,
                   regime: str, cfg: dict, now: dt.datetime) -> dict:
    ind = compute_indicators(hist)
    sent = sentiment_summary(news, now)
    sc = score_stock(ind, sent, bench_ind)
    action, instruction = action_for(sc["score"], sc["flags"], regime)
    size = position_size(ind, cfg.get("capital", 500000), cfg.get("risk_per_trade_pct", 1.0),
                         cfg.get("max_position_pct", 10.0))
    closes = hist["close"][-60:]
    return {
        "symbol": entry["symbol"],
        "name": entry.get("name") or hist.get("meta", {}).get("longName") or entry["symbol"],
        "currency": hist.get("meta", {}).get("currency"),
        "score": sc["score"],
        "action": action,
        "instruction": instruction,
        "components": sc["components"],
        "reasons_up": sc["reasons_up"],
        "reasons_down": sc["reasons_down"],
        "flags": sc["flags"],
        "indicators": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in ind.items()},
        "rel_60d_pct": sc["rel_60d_pct"],
        "sentiment": sent,
        "news": news,
        "position": size,
        "spark": [round(x, 2) for x in closes],
        "spark_dates": hist["dates"][-60:],
    }


def run(watchlist_path: Path, out_dir: Path, offline: bool, use_ai: bool) -> Path:
    cfg = json.loads(watchlist_path.read_text())
    market = cfg.get("market", "IN")
    now = dt.datetime.now(dt.timezone.utc)
    today = now.astimezone(IST).date().isoformat()
    errors: list[str] = []

    def get_hist(sym: str) -> dict | None:
        if offline:
            return synthetic_history(sym)
        try:
            h = fetch_yahoo_history(sym)
            time.sleep(0.4)
            return h
        except Exception as e:  # noqa: BLE001
            errors.append(f"{sym}: {e}")
            print(f"  ! {sym}: {e}", file=sys.stderr)
            return None

    def get_news(query: str, seed: int) -> list[dict]:
        if offline:
            return synthetic_news(query, seed)
        try:
            items = fetch_google_news(query, market)
            time.sleep(0.4)
            return items
        except Exception as e:  # noqa: BLE001
            errors.append(f"news '{query}': {e}")
            print(f"  ! news '{query}': {e}", file=sys.stderr)
            return []

    print(f"[{today}] benchmark {cfg.get('benchmark')} ...", file=sys.stderr)
    bench_hist = get_hist(cfg["benchmark"]) if cfg.get("benchmark") else None
    bench_ind = compute_indicators(bench_hist) if bench_hist else None
    vix_level = None
    if cfg.get("volatility_index"):
        vh = get_hist(cfg["volatility_index"])
        if vh:
            vix_level = vh["close"][-1]
    regime = market_regime(bench_ind, vix_level)
    market_query = "Nifty Sensex stock market today" if market == "IN" else "stock market today Wall Street"
    market_news = get_news(market_query, 7)
    market_sent = sentiment_summary(market_news, now)

    stocks = []
    for i, entry in enumerate(cfg["stocks"]):
        print(f"  {entry['symbol']} ...", file=sys.stderr)
        hist = get_hist(entry["symbol"])
        if not hist or len(hist["close"]) < 30:
            errors.append(f"{entry['symbol']}: insufficient history")
            continue
        news = get_news(entry.get("news") or entry.get("name") or entry["symbol"], i + 11)
        stocks.append(analyze_symbol(entry, hist, news, bench_ind, regime["state"], cfg, now))

    stocks.sort(key=lambda s: s["score"], reverse=True)
    for rank, s in enumerate(stocks, 1):
        s["rank"] = rank

    brief = {
        "generated_at": now.isoformat(),
        "date": today,
        "sample": offline,
        "market": market,
        "config": {k: cfg.get(k) for k in ("benchmark", "volatility_index", "capital",
                                           "risk_per_trade_pct", "max_position_pct")},
        "regime": regime,
        "benchmark": {
            "symbol": cfg.get("benchmark"),
            "indicators": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in bench_ind.items()} if bench_ind else None,
            "spark": [round(x, 2) for x in bench_hist["close"][-60:]] if bench_hist else [],
            "vix": round(vix_level, 2) if vix_level is not None else None,
        },
        "market_news": market_news,
        "market_sentiment": market_sent,
        "stocks": stocks,
        "summary": {
            "accumulate": [s["symbol"] for s in stocks if s["action"] == "Accumulate"],
            "hold": [s["symbol"] for s in stocks if s["action"].startswith("Hold")],
            "watch": [s["symbol"] for s in stocks if s["action"] == "Watch"],
            "avoid": [s["symbol"] for s in stocks if s["action"].startswith("Avoid")],
        },
        "errors": errors,
        "disclaimer": ("Rules-based screener on delayed public data. Not investment advice. "
                       "Past patterns do not predict future returns. You are responsible for your trades."),
    }
    if use_ai and not offline:
        brief["ai"] = ai_narrative(brief)

    out_dir.mkdir(parents=True, exist_ok=True)
    if offline:
        target = out_dir / "sample-brief.json"
        target.write_text(json.dumps(brief, indent=1))
    else:
        target = out_dir / "latest.json"
        target.write_text(json.dumps(brief, indent=1))
        (out_dir / f"brief-{today}.json").write_text(json.dumps(brief, indent=1))
    print(f"wrote {target} ({len(stocks)} stocks, regime {regime['state']}, {len(errors)} errors)", file=sys.stderr)
    return target


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--watchlist", default=str(HERE / "watchlist.json"))
    p.add_argument("--out", default=str(HERE / "data"))
    p.add_argument("--offline", action="store_true", help="synthetic data, no network (demo/tests)")
    p.add_argument("--ai", action="store_true", help="add a Claude-written narrative (needs ANTHROPIC_API_KEY)")
    a = p.parse_args(argv)
    run(Path(a.watchlist), Path(a.out), a.offline, a.ai)
    return 0


if __name__ == "__main__":
    sys.exit(main())
