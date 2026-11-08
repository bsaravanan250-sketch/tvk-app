#!/usr/bin/env python3
"""
Deep dive: the six-part "Wall Street breakdown" for one ticker, grounded in data.

Gathers what the prompts ask you to paste by hand (multi-period financials,
valuation inputs, peer metrics, price and volume, recent headlines) from free
public sources, then either:

  * runs the six analyses through Claude and saves a structured report, or
  * with no ANTHROPIC_API_KEY, saves the six prompts pre-filled with the real
    data so you can paste them into any assistant.

Either way the data pack is saved so the numbers can be checked.

Usage:
  python3 deepdive.py RELIANCE.NS
  python3 deepdive.py AAPL --peers MSFT,GOOGL,AMZN
  python3 deepdive.py HDFCBANK.NS --no-ai

Outputs (under data/deepdive/):
  <SYMBOL>.json          data pack + report sections (AI) or filled prompts
  <SYMBOL>-prompts.md    the six prompts with the data inserted
  index.json             list of available deep dives (read by index.html)

Not investment advice. Free data is delayed and unofficial; verify key numbers
against the company's filings before acting.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

import analyze as A

HERE = Path(__file__).resolve().parent
OUT = HERE / "data" / "deepdive"

ANNUAL = ["TotalRevenue", "GrossProfit", "OperatingIncome", "EBITDA", "NetIncome", "DilutedEPS",
          "OperatingCashFlow", "CapitalExpenditure", "FreeCashFlow", "TotalDebt",
          "CashAndCashEquivalents", "StockholdersEquity", "DilutedAverageShares", "InterestExpense"]
QUARTERLY = ["TotalRevenue", "OperatingIncome", "NetIncome", "DilutedEPS", "FreeCashFlow",
             "TotalDebt", "CashAndCashEquivalents", "StockholdersEquity", "DilutedAverageShares",
             "ShareIssued"]
TRAILING = ["TotalRevenue", "NetIncome", "EBITDA", "FreeCashFlow", "OperatingIncome"]

# The user's six prompts, kept verbatim in spirit; [TICKER]/[PASTE DATA] are filled from the data pack.
PROMPTS = [
    ("business", "Run the full Wall Street breakdown",
     "Analyze {ticker} like a senior Wall Street equity analyst. Break down the business model, revenue "
     "drivers, growth, margins, cash flow, competitive position, recent earnings, catalysts, and biggest "
     "risks. End with the key numbers an investor should monitor. Ground every claim in the data below; "
     "where the data is silent, say so instead of guessing."),
    ("financials", "Dig through the financials",
     "Analyze {ticker}'s income statement, balance sheet and cash flow statement across the periods below. "
     "Evaluate revenue growth, margins, debt, cash, free cash flow and dilution. Compare the latest figures "
     "with prior periods and flag unusual changes or potential red flags."),
    ("valuation", "Break down the valuation",
     "Evaluate {ticker}'s current valuation using P/E, EV/EBITDA, price-to-sales, price-to-book and "
     "free-cash-flow yield where relevant (for banks and lenders use P/E, P/B and ROE; EV/EBITDA and FCF "
     "yield are not meaningful). Compare with the historical levels and peers below. Explain what growth "
     "and profitability assumptions appear to be reflected in the current price: what is the market already "
     "pricing in, not simply whether the stock looks cheap."),
    ("competitors", "Think like a Wall Street competitor",
     "Analyze {ticker} against the peers below using the same metrics and periods for every company: revenue "
     "growth, margins, profitability, valuation, scale, competitive advantages and major threats. Explain "
     "where {ticker} has an edge and where competitors may have the advantage."),
    ("risks", "Build the Wall Street risk map",
     "Identify the 5 biggest risks facing {ticker}. Cover company-specific, financial, industry, "
     "macroeconomic and regulatory risks. For each risk explain what could trigger it, the potential business "
     "impact, and which metrics or events to watch. Do not just build the bull case: find what could break it."),
    ("chart", "Read the stock chart",
     "Analyze the recent price and volume data for {ticker} below. Identify the primary trend, support and "
     "resistance levels, momentum, volume changes and notable technical patterns. Explain what the data "
     "suggests without treating any setup as a guaranteed prediction."),
]


# --------------------------------------------------------------------------- #
# Fetch                                                                       #
# --------------------------------------------------------------------------- #
def fetch_fundamentals(symbol: str) -> dict[str, list[dict]]:
    """Yahoo fundamentals timeseries -> {'annualTotalRevenue': [{'date', 'value'}, ...], ...}"""
    types = [f"annual{t}" for t in ANNUAL] + [f"quarterly{t}" for t in QUARTERLY] + [f"trailing{t}" for t in TRAILING]
    p2 = int(time.time())
    p1 = p2 - 6 * 365 * 86400
    q = urllib.parse.quote(symbol, safe="")
    url = (f"https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{q}"
           f"?symbol={q}&type={','.join(types)}&period1={p1}&period2={p2}")
    raw = json.loads(A.http_get(url))
    out: dict[str, list[dict]] = {}
    for item in raw.get("timeseries", {}).get("result", []) or []:
        key = item["meta"]["type"][0]
        rows = []
        for r in item.get(key, []) or []:
            if r and r.get("reportedValue") and r["reportedValue"].get("raw") is not None:
                rows.append({"date": r["asOfDate"], "value": float(r["reportedValue"]["raw"]),
                             "currency": r.get("currencyCode")})
        rows.sort(key=lambda x: x["date"])
        out[key] = rows
    return out


# --------------------------------------------------------------------------- #
# Shape                                                                       #
# --------------------------------------------------------------------------- #
def series(f: dict, key: str) -> list[dict]:
    return f.get(key, [])


def latest(f: dict, key: str) -> float | None:
    s = series(f, key)
    return s[-1]["value"] if s else None


def growth(cur: float | None, prev: float | None) -> float | None:
    if cur is None or prev in (None, 0):
        return None
    if prev < 0:
        return None
    return (cur / prev - 1.0) * 100.0


def ratio(a: float | None, b: float | None) -> float | None:
    if a is None or b in (None, 0):
        return None
    return a / b


def period_table(f: dict, prefix: str, fields: list[str]) -> list[dict]:
    """One row per period with every field aligned by date."""
    dates: set[str] = set()
    for fld in fields:
        for r in series(f, prefix + fld):
            dates.add(r["date"])
    rows = []
    for d in sorted(dates):
        row = {"period": d}
        for fld in fields:
            row[fld] = next((r["value"] for r in series(f, prefix + fld) if r["date"] == d), None)
        rows.append(row)
    return rows


def enrich_rows(rows: list[dict]) -> list[dict]:
    prev = None
    for r in rows:
        rev, ni, op, ebitda, gp = r.get("TotalRevenue"), r.get("NetIncome"), r.get("OperatingIncome"), r.get("EBITDA"), r.get("GrossProfit")
        fcf, ocf, capex = r.get("FreeCashFlow"), r.get("OperatingCashFlow"), r.get("CapitalExpenditure")
        debt, cash, eq, sh = r.get("TotalDebt"), r.get("CashAndCashEquivalents"), r.get("StockholdersEquity"), r.get("DilutedAverageShares") or r.get("ShareIssued")
        r["gross_margin_pct"] = ratio(gp, rev) * 100 if ratio(gp, rev) is not None else None
        r["operating_margin_pct"] = ratio(op, rev) * 100 if ratio(op, rev) is not None else None
        r["ebitda_margin_pct"] = ratio(ebitda, rev) * 100 if ratio(ebitda, rev) is not None else None
        r["net_margin_pct"] = ratio(ni, rev) * 100 if ratio(ni, rev) is not None else None
        r["fcf_margin_pct"] = ratio(fcf, rev) * 100 if ratio(fcf, rev) is not None else None
        r["net_debt"] = (debt - cash) if debt is not None and cash is not None else None
        r["net_debt_to_equity"] = ratio(r["net_debt"], eq)
        r["roe_pct"] = ratio(ni, eq) * 100 if ratio(ni, eq) is not None else None
        r["capex_to_ocf_pct"] = abs(ratio(capex, ocf)) * 100 if ratio(capex, ocf) is not None else None
        r["interest_coverage"] = ratio(op, r.get("InterestExpense")) if r.get("InterestExpense") else None
        if prev:
            r["revenue_growth_pct"] = growth(rev, prev.get("TotalRevenue"))
            r["net_income_growth_pct"] = growth(ni, prev.get("NetIncome"))
            r["eps_growth_pct"] = growth(r.get("DilutedEPS"), prev.get("DilutedEPS"))
            psh = prev.get("DilutedAverageShares") or prev.get("ShareIssued")
            r["share_count_change_pct"] = growth(sh, psh)
        prev = r
    return rows


def price_on(hist: dict, date: str) -> float | None:
    """Last close on or before an ISO date."""
    best = None
    for d, c in zip(hist["dates"], hist["close"]):
        if d <= date:
            best = c
        else:
            break
    return best


def valuation(f: dict, hist: dict, financial: bool) -> dict:
    price = hist["close"][-1]
    shares = latest(f, "quarterlyDilutedAverageShares") or latest(f, "quarterlyShareIssued") or latest(f, "annualDilutedAverageShares")
    mcap = price * shares if shares else None
    debt = latest(f, "quarterlyTotalDebt") or latest(f, "annualTotalDebt")
    cash = latest(f, "quarterlyCashAndCashEquivalents") or latest(f, "annualCashAndCashEquivalents")
    equity = latest(f, "quarterlyStockholdersEquity") or latest(f, "annualStockholdersEquity")
    ttm_rev = latest(f, "trailingTotalRevenue")
    ttm_ni = latest(f, "trailingNetIncome")
    ttm_ebitda = latest(f, "trailingEBITDA")
    ttm_fcf = latest(f, "trailingFreeCashFlow")
    # Fall back to the sum of the last 4 quarters when a trailing series is missing.
    def ttm_from_q(key):
        s = series(f, "quarterly" + key)
        return sum(r["value"] for r in s[-4:]) if len(s) >= 4 else None
    ttm_rev = ttm_rev or ttm_from_q("TotalRevenue")
    ttm_ni = ttm_ni or ttm_from_q("NetIncome")
    ttm_fcf = ttm_fcf or ttm_from_q("FreeCashFlow")
    ev = (mcap + (debt or 0) - (cash or 0)) if mcap is not None and not financial else None
    v = {
        "price": price,
        "shares": shares,
        "market_cap": mcap,
        "enterprise_value": ev,
        "ttm_revenue": ttm_rev, "ttm_net_income": ttm_ni, "ttm_ebitda": ttm_ebitda, "ttm_fcf": ttm_fcf,
        "pe_ttm": ratio(mcap, ttm_ni) if ttm_ni and ttm_ni > 0 else None,
        "ps_ttm": ratio(mcap, ttm_rev),
        "pb": ratio(mcap, equity),
        "ev_ebitda_ttm": ratio(ev, ttm_ebitda) if (ev is not None and ttm_ebitda and ttm_ebitda > 0) else None,
        "fcf_yield_pct": ratio(ttm_fcf, mcap) * 100 if (ratio(ttm_fcf, mcap) is not None and not financial) else None,
        "earnings_yield_pct": ratio(ttm_ni, mcap) * 100 if ratio(ttm_ni, mcap) is not None else None,
        "roe_ttm_pct": ratio(ttm_ni, equity) * 100 if ratio(ttm_ni, equity) is not None else None,
        "net_debt": (debt - cash) if debt is not None and cash is not None else None,
        "financial_company": financial,
        "history": [],
    }
    # Historical P/E and P/B at each fiscal year end: price then x shares then / that year's earnings.
    for r in period_table(f, "annual", ["NetIncome", "DilutedAverageShares", "StockholdersEquity", "TotalRevenue"]):
        p = price_on(hist, r["period"])
        sh = r.get("DilutedAverageShares")
        if p and sh:
            mc = p * sh
            v["history"].append({
                "period": r["period"], "price": p, "market_cap": mc,
                "pe": ratio(mc, r["NetIncome"]) if r.get("NetIncome") and r["NetIncome"] > 0 else None,
                "pb": ratio(mc, r.get("StockholdersEquity")),
                "ps": ratio(mc, r.get("TotalRevenue")),
            })
    return v


def levels(hist: dict, window: int = 5, lookback: int = 160) -> dict:
    """Swing-based support/resistance plus structural levels."""
    c = hist["close"][-lookback:]
    hi = hist["high"][-lookback:]
    lo = hist["low"][-lookback:]
    price = c[-1]
    swings_hi, swings_lo = [], []
    for i in range(window, len(c) - window):
        if hi[i] == max(hi[i - window:i + window + 1]):
            swings_hi.append(hi[i])
        if lo[i] == min(lo[i - window:i + window + 1]):
            swings_lo.append(lo[i])

    def cluster(vals):
        out: list[float] = []
        for v in sorted(vals):
            if out and abs(v - out[-1]) / out[-1] < 0.015:
                out[-1] = (out[-1] + v) / 2
            else:
                out.append(v)
        return out

    sup = [v for v in cluster(swings_lo) if v < price]
    res = [v for v in cluster(swings_hi) if v > price]
    return {
        "support": [round(v, 2) for v in sup[-3:]],
        "resistance": [round(v, 2) for v in res[:3]],
        "high_52w": round(max(hist["close"][-252:]), 2),
        "low_52w": round(min(hist["close"][-252:]), 2),
        "drawdown_from_52w_high_pct": round((price / max(hist["close"][-252:]) - 1) * 100, 2),
    }


def technicals(hist: dict) -> dict:
    ind = A.compute_indicators(hist)
    v = hist["volume"]
    v20 = sum(v[-20:]) / 20 if len(v) >= 20 else None
    v60 = sum(v[-60:]) / 60 if len(v) >= 60 else None
    c = hist["close"]
    weekly = []
    # 26 weekly closes + weekly volume for the chart prompt (compact, human-readable).
    for i in range(len(c) - 1, max(len(c) - 130, 0), -5):
        chunk = slice(max(0, i - 4), i + 1)
        weekly.append({"week_ending": hist["dates"][i], "close": round(c[i], 2),
                       "high": round(max(hist["high"][chunk]), 2), "low": round(min(hist["low"][chunk]), 2),
                       "volume": int(sum(v[chunk]))})
    weekly.reverse()
    return {
        "indicators": {k: (round(x, 2) if isinstance(x, float) else x) for k, x in ind.items()},
        "volume_20d_avg": int(v20) if v20 else None,
        "volume_60d_avg": int(v60) if v60 else None,
        "volume_trend_pct": round((v20 / v60 - 1) * 100, 1) if v20 and v60 else None,
        "ret_1y_pct": round(A.pct(c[-1], c[-252]) or 0, 2) if len(c) > 252 else None,
        "levels": levels(hist),
        "weekly": weekly[-26:],
        "daily_last_20": [{"date": d, "close": round(x, 2), "volume": int(vv)}
                          for d, x, vv in zip(hist["dates"][-20:], c[-20:], v[-20:])],
    }


def peer_snapshot(symbol: str, financial: bool) -> dict | None:
    try:
        hist = A.fetch_yahoo_history(symbol, "1y")
        f = fetch_fundamentals(symbol)
    except Exception as e:  # noqa: BLE001
        print(f"  ! peer {symbol}: {e}", file=sys.stderr)
        return None
    val = valuation(f, hist, financial)
    ann = enrich_rows(period_table(f, "annual", ANNUAL))
    last = ann[-1] if ann else {}
    return {
        "symbol": symbol,
        "name": hist.get("meta", {}).get("longName") or symbol,
        "price": hist["close"][-1],
        "market_cap": val["market_cap"],
        "ttm_revenue": val["ttm_revenue"], "ttm_net_income": val["ttm_net_income"],
        "revenue_growth_pct": last.get("revenue_growth_pct"),
        "net_income_growth_pct": last.get("net_income_growth_pct"),
        "operating_margin_pct": last.get("operating_margin_pct"),
        "net_margin_pct": last.get("net_margin_pct"),
        "roe_pct": val["roe_ttm_pct"],
        "pe_ttm": val["pe_ttm"], "ps_ttm": val["ps_ttm"], "pb": val["pb"],
        "ev_ebitda_ttm": val["ev_ebitda_ttm"], "fcf_yield_pct": val["fcf_yield_pct"],
        "net_debt_to_equity": last.get("net_debt_to_equity"),
        "ret_1y_pct": round(A.pct(hist["close"][-1], hist["close"][0]) or 0, 2),
    }


# --------------------------------------------------------------------------- #
# Formatting for prompts                                                      #
# --------------------------------------------------------------------------- #
def money(v: float | None, currency: str | None) -> str:
    if v is None:
        return "n/a"
    if currency == "INR":
        if abs(v) >= 1e12:
            return f"₹{v / 1e12:,.2f} lakh Cr"
        return f"₹{v / 1e7:,.0f} Cr"
    if abs(v) >= 1e9:
        return f"{v / 1e9:,.2f} B"
    if abs(v) >= 1e6:
        return f"{v / 1e6:,.1f} M"
    return f"{v:,.0f}"


def num(v: float | None, d: int = 1, suffix: str = "") -> str:
    return "n/a" if v is None else f"{v:,.{d}f}{suffix}"


def md_table(rows: list[dict], cols: list[tuple[str, str, callable]]) -> str:
    cols = [c for c in cols if any(r.get(c[0]) is not None for r in rows)] or cols[:1]
    head = "| " + " | ".join(c[1] for c in cols) + " |\n|" + "---|" * len(cols) + "\n"
    body = ""
    for r in rows:
        body += "| " + " | ".join(c[2](r.get(c[0])) for c in cols) + " |\n"
    return head + body


def financial_markdown(pack: dict) -> str:
    cur = pack["currency"]
    m = lambda v: money(v, cur)  # noqa: E731
    p = lambda v: num(v, 1, "%")  # noqa: E731
    x = lambda v: num(v, 2, "x")  # noqa: E731
    cols_a = [("period", "FY end", str), ("TotalRevenue", "Revenue", m), ("revenue_growth_pct", "Rev growth", p),
              ("operating_margin_pct", "Op margin", p), ("net_margin_pct", "Net margin", p), ("NetIncome", "Net income", m),
              ("DilutedEPS", "EPS", lambda v: num(v, 2)), ("OperatingCashFlow", "Op cash flow", m), ("FreeCashFlow", "FCF", m),
              ("net_debt", "Net debt", m), ("net_debt_to_equity", "Net debt/equity", x), ("roe_pct", "ROE", p),
              ("share_count_change_pct", "Share count chg", p)]
    cols_q = [("period", "Quarter", str), ("TotalRevenue", "Revenue", m), ("NetIncome", "Net income", m),
              ("net_margin_pct", "Net margin", p), ("DilutedEPS", "EPS", lambda v: num(v, 2)),
              ("FreeCashFlow", "FCF", m), ("net_debt", "Net debt", m)]
    return ("**Annual (fiscal years)**\n\n" + md_table(pack["annual"], cols_a) +
            "\n**Quarterly (latest five; growth is sequential)**\n\n" + md_table(pack["quarterly"], cols_q))


def valuation_markdown(pack: dict) -> str:
    v, cur = pack["valuation"], pack["currency"]
    lines = [
        f"Price {num(v['price'], 2)} {cur} · Market cap {money(v['market_cap'], cur)} · "
        f"Enterprise value {money(v['enterprise_value'], cur)}",
        f"P/E (TTM) {num(v['pe_ttm'], 1, 'x')} · P/S {num(v['ps_ttm'], 2, 'x')} · P/B {num(v['pb'], 2, 'x')} · "
        f"EV/EBITDA {num(v['ev_ebitda_ttm'], 1, 'x')} · FCF yield {num(v['fcf_yield_pct'], 2, '%')} · "
        f"Earnings yield {num(v['earnings_yield_pct'], 2, '%')} · ROE (TTM) {num(v['roe_ttm_pct'], 1, '%')}",
        f"TTM revenue {money(v['ttm_revenue'], cur)} · TTM net income {money(v['ttm_net_income'], cur)} · "
        f"TTM EBITDA {money(v['ttm_ebitda'], cur)} · TTM FCF {money(v['ttm_fcf'], cur)}",
        "", "**Multiples at past fiscal year-ends (price then x shares then / that year's figures)**", "",
        md_table(v["history"], [("period", "FY end", str), ("price", "Price", lambda q: num(q, 2)),
                                ("pe", "P/E", lambda q: num(q, 1, "x")), ("pb", "P/B", lambda q: num(q, 2, "x")),
                                ("ps", "P/S", lambda q: num(q, 2, "x"))]),
    ]
    if v["financial_company"]:
        lines.insert(1, "_Bank/NBFC: EV, EV/EBITDA and FCF yield are omitted as not meaningful. 'Revenue' and "
                        "'equity' follow Yahoo's consolidated definitions (equity may include minority interests), so "
                        "ROE here can sit below the bank's reported standalone ROE; treat NIM, NPA and CASA as 'not in data'._")
    return "\n".join(lines)


def peers_markdown(pack: dict) -> str:
    cur = pack["currency"]
    rows = [{"symbol": pack["symbol"] + " (subject)", **{k: pack["valuation"].get(k) for k in ("market_cap", "pe_ttm", "ps_ttm", "pb", "ev_ebitda_ttm", "fcf_yield_pct")},
             "revenue_growth_pct": (pack["annual"][-1].get("revenue_growth_pct") if pack["annual"] else None),
             "operating_margin_pct": (pack["annual"][-1].get("operating_margin_pct") if pack["annual"] else None),
             "net_margin_pct": (pack["annual"][-1].get("net_margin_pct") if pack["annual"] else None),
             "roe_pct": pack["valuation"].get("roe_ttm_pct"), "ret_1y_pct": pack["technicals"].get("ret_1y_pct")}] + pack["peers"]
    return md_table(rows, [("symbol", "Company", str), ("market_cap", "Mkt cap", lambda v: money(v, cur)),
                           ("revenue_growth_pct", "Rev growth (last FY)", lambda v: num(v, 1, "%")),
                           ("operating_margin_pct", "Op margin", lambda v: num(v, 1, "%")),
                           ("net_margin_pct", "Net margin", lambda v: num(v, 1, "%")), ("roe_pct", "ROE", lambda v: num(v, 1, "%")),
                           ("pe_ttm", "P/E", lambda v: num(v, 1, "x")), ("pb", "P/B", lambda v: num(v, 2, "x")),
                           ("ev_ebitda_ttm", "EV/EBITDA", lambda v: num(v, 1, "x")), ("fcf_yield_pct", "FCF yield", lambda v: num(v, 2, "%")),
                           ("ret_1y_pct", "1y return", lambda v: num(v, 1, "%"))])


def chart_markdown(pack: dict) -> str:
    t = pack["technicals"]; i = t["indicators"]; lv = t["levels"]
    lines = [
        f"Close {num(i['close'], 2)} · 1d {num(i['change_1d_pct'], 2, '%')} · 20d {num(i['ret_20d_pct'], 1, '%')} · "
        f"60d {num(i['ret_60d_pct'], 1, '%')} · 1y {num(t['ret_1y_pct'], 1, '%')}",
        f"SMA20 {num(i['sma20'], 2)} · SMA50 {num(i['sma50'], 2)} · SMA200 {num(i['sma200'], 2)} · RSI14 {num(i['rsi14'], 0)} · "
        f"MACD hist {num(i['macd_hist'], 2)} · ATR14 {num(i['atr14'], 2)} ({num(i['atr_pct'], 1, '%')}) · "
        f"realised vol 20d {num(i['realized_vol_20d'], 0, '%')}",
        f"52-week high {lv['high_52w']} · low {lv['low_52w']} · drawdown from high {lv['drawdown_from_52w_high_pct']}% · "
        f"position in range {num(i['pos_52w_pct'], 0, '%')}",
        f"Swing support {lv['support']} · swing resistance {lv['resistance']}",
        f"Volume: 20d avg {t['volume_20d_avg']:,} vs 60d avg {t['volume_60d_avg']:,} ({num(t['volume_trend_pct'], 1, '%')}); "
        f"last day {num(i['vol_ratio'], 2, 'x')} the 20d average",
        "", "**Weekly closes (last 26 weeks)**", "",
        md_table(t["weekly"], [("week_ending", "Week ending", str), ("close", "Close", lambda v: num(v, 2)),
                               ("high", "High", lambda v: num(v, 2)), ("low", "Low", lambda v: num(v, 2)),
                               ("volume", "Volume", lambda v: f"{v:,}")]),
        "**Daily closes (last 20 sessions)**", "",
        md_table(t["daily_last_20"], [("date", "Date", str), ("close", "Close", lambda v: num(v, 2)), ("volume", "Volume", lambda v: f"{v:,}")]),
    ]
    return "\n".join(lines)


def news_markdown(pack: dict) -> str:
    out = []
    for group, items in pack["news"].items():
        out.append(f"**{group}**")
        for n in items[:10]:
            out.append(f"- {n['title']} ({n.get('source') or 'unknown'}, {n.get('published', '')[:10]})")
        out.append("")
    return "\n".join(out) or "No headlines found."


def build_prompts(pack: dict) -> list[dict]:
    t = pack["symbol"]
    data_for = {
        "business": lambda: "\n\n".join(["### Financial summary", financial_markdown(pack), "### Valuation", valuation_markdown(pack),
                                         "### Recent headlines", news_markdown(pack)]),
        "financials": lambda: financial_markdown(pack),
        "valuation": lambda: valuation_markdown(pack) + "\n\n### Peers\n\n" + peers_markdown(pack),
        "competitors": lambda: peers_markdown(pack),
        "risks": lambda: "\n\n".join(["### Financial summary", financial_markdown(pack), "### Recent headlines", news_markdown(pack)]),
        "chart": lambda: chart_markdown(pack),
    }
    return [{"key": k, "title": title, "prompt": text.format(ticker=f"{pack['name']} ({t})"), "data": data_for[k]()}
            for k, title, text in PROMPTS]


# --------------------------------------------------------------------------- #
# Claude                                                                      #
# --------------------------------------------------------------------------- #
def run_claude(pack: dict, prompts: list[dict]) -> dict | None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("  AI skipped: ANTHROPIC_API_KEY not set (prompts file written instead)", file=sys.stderr)
        return None
    try:
        import anthropic  # type: ignore
    except ImportError:
        print("  AI skipped: pip install anthropic", file=sys.stderr)
        return None
    system = (
        "You are a senior sell-side equity analyst. You will receive six analysis tasks for one company, each with "
        "the real data it needs. Answer all six. Ground every number in the data given; if the data does not "
        "support a claim, say 'not in data' rather than inventing. Separate facts from judgement. Be specific, "
        "sceptical and concise; no filler. Currency amounts are as given (INR shown in crores). "
        "Respond with a single JSON object and nothing else, with these keys: "
        "business (markdown), financials (markdown), valuation (markdown), competitors (markdown), "
        "risks (array of {name, type, trigger, impact, watch}), chart (markdown), "
        "monitor (array of {metric, current, why, threshold}: the 6-10 numbers an investor should track), "
        "verdict (<=60 words: what the market is pricing in and what would change your mind)."
    )
    user = "\n\n".join(f"## Task {i + 1}: {p['title']}\n\n{p['prompt']}\n\n### Data\n\n{p['data']}" for i, p in enumerate(prompts))
    client = anthropic.Anthropic()
    kwargs = dict(model="claude-opus-5-5", max_tokens=16000, system=system,
                  messages=[{"role": "user", "content": user}])
    try:
        try:
            with client.beta.messages.stream(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs) as s:
                resp = s.get_final_message()
        except TypeError:  # older SDK without the fallbacks parameter
            with client.messages.stream(**kwargs) as s:
                resp = s.get_final_message()
    except Exception as e:  # noqa: BLE001
        print(f"  AI failed: {e}", file=sys.stderr)
        return None
    if resp.stop_reason == "refusal":
        print("  AI skipped: refused by safety classifier", file=sys.stderr)
        return None
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        out = json.loads(text)
    except json.JSONDecodeError:
        out = {"business": text}
    out["model"] = resp.model
    out["usage"] = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
    return out


# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #
def build_pack(symbol: str, peers: list[str], name: str | None, news_query: str | None,
               financial: bool, market: str) -> dict:
    print(f"[{symbol}] prices ...", file=sys.stderr)
    hist = A.fetch_yahoo_history(symbol, "5y")
    print(f"[{symbol}] fundamentals ...", file=sys.stderr)
    f = fetch_fundamentals(symbol)
    currency = next((r["currency"] for s in f.values() for r in s if r.get("currency")), hist.get("meta", {}).get("currency"))
    name = name or hist.get("meta", {}).get("longName") or symbol
    annual = enrich_rows(period_table(f, "annual", ANNUAL))
    quarterly = [r for r in period_table(f, "quarterly", QUARTERLY)
                 if r.get("TotalRevenue") is not None or r.get("NetIncome") is not None][-5:]
    quarterly = enrich_rows(quarterly)
    val = valuation(f, hist, financial)
    tech = technicals(hist)
    print(f"[{symbol}] peers {peers} ...", file=sys.stderr)
    peer_rows = [p for p in (peer_snapshot(s, financial) for s in peers) if p]
    print(f"[{symbol}] news ...", file=sys.stderr)
    q = news_query or name
    news = {}
    for label, query in (("Results and earnings", f"{q} quarterly results"),
                         ("Company news", q),
                         ("Outlook, guidance and analyst views", f"{q} outlook OR guidance OR target price")):
        try:
            news[label] = A.fetch_google_news(query, market, limit=10)
            time.sleep(0.4)
        except Exception as e:  # noqa: BLE001
            print(f"  ! news '{query}': {e}", file=sys.stderr)
            news[label] = []
    return {
        "symbol": symbol, "name": name, "currency": currency, "market": market,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "financial_company": financial,
        "annual": annual, "quarterly": quarterly, "valuation": val, "technicals": tech,
        "peers": peer_rows, "news": news,
        "spark_1y": [round(x, 2) for x in hist["close"][-252:]],
        "spark_1y_dates": hist["dates"][-252:],
        "sources": ["Yahoo Finance chart API (prices)", "Yahoo Finance fundamentals timeseries (statements)",
                    "Google News RSS (headlines)"],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbol")
    ap.add_argument("--peers", help="comma-separated symbols; defaults to the watchlist entry's peers")
    ap.add_argument("--watchlist", default=str(HERE / "watchlist.json"))
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--no-ai", action="store_true", help="skip Claude even if ANTHROPIC_API_KEY is set")
    a = ap.parse_args(argv)

    cfg = json.loads(Path(a.watchlist).read_text())
    entry = next((s for s in cfg["stocks"] if s["symbol"].upper() == a.symbol.upper()), {})
    peers = [p.strip() for p in a.peers.split(",")] if a.peers else entry.get("peers", [])
    pack = build_pack(a.symbol.upper(), peers, entry.get("name"), entry.get("news"),
                      bool(entry.get("financial")), cfg.get("market", "IN"))
    prompts = build_prompts(pack)
    report = None if a.no_ai else run_claude(pack, prompts)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9.^_-]", "_", pack["symbol"])
    doc = {"pack": pack, "prompts": prompts, "report": report,
           "disclaimer": "Free, delayed, unofficial data and an automated analysis. Not investment advice. "
                         "Verify every key number against the company's filings before acting."}
    (out / f"{safe}.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    md = [f"# {pack['name']} ({pack['symbol']}) — deep-dive prompts, data as of {pack['generated_at'][:10]}", ""]
    for i, p in enumerate(prompts, 1):
        md += [f"## {i}. {p['title']}", "", p["prompt"], "", p["data"], ""]
    (out / f"{safe}-prompts.md").write_text("\n".join(md))
    idx_path = out / "index.json"
    idx = json.loads(idx_path.read_text()) if idx_path.exists() else {}
    idx[pack["symbol"]] = {"file": f"{safe}.json", "name": pack["name"], "generated_at": pack["generated_at"],
                           "has_report": bool(report)}
    idx_path.write_text(json.dumps(idx, indent=1, ensure_ascii=False))
    print(f"wrote {out / (safe + '.json')} (report: {'yes' if report else 'prompts only'})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
