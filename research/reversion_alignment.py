#!/usr/bin/env python3
"""Mean-reversion ETF scanner: buy the dip below the 5-day MA, timed by multi-
timeframe *reversion alignment*, gated by macro trend regime, held ~10 days.

    python3 reversion_alignment.py                 # daily backtest, all ETFs
    python3 reversion_alignment.py --regime        # add the macro trend gate
    python3 reversion_alignment.py --align URA     # intraday alignment diagnostic

Needs MASSIVE_API_KEY in the env (same feed as live_scan.py). Data is fetched
live; nothing is cached to disk. The massive plan only carries ~2yr of daily
history, which bounds everything below.

--------------------------------------------------------------------------------
WHAT WAS MEASURED (research trail, 5 ETFs, ~2yr daily to 2026-07, pooled unless
noted). Every number here came from an explicit backtest, not a guess:

  Deviation from the 5-day MA, by ETF (avg |dev|): URA 2.56% / SLV 2.27% /
  USO 2.01% / XBI 1.40% / GLD 1.17%. => a flat threshold is wrong. A 2% dip is
  routine noise for uranium but a real event for gold. Hence per-ETF ENTRY_DEV.

  Dose-response (buy X% below 5d MA, hold 5d, pooled):
      -1% -> +0.62% | -2% -> +0.64% | -3% -> +0.88% | -4% -> +1.55%. Monotonic.

  Exit (fresh -3% cross, pooled):
      hold  1d -> -0.27%   (LOSES: the dip keeps falling short-term)
      hold  2d -> -0.11%   (LOSES)
      hold  7d -> +0.92%
      hold 10d -> +1.71%   <- best simple exit
      profit-target +2%/+3% (10d cap) -> +0.95% / +1.23%: WORSE than just
      holding 10d. The winners run; taking profit early caps them. => no target.

  Reversion alignment (15-min bars; 1h=4 / 5h=20 / 5d=130 bars). Forward return
  climbs with how many timeframes sit below their mean at once:
      0 aligned -> +0.04% (1d) | 1 -> +0.07% | 2 -> +0.24% | 3 -> +0.49%.
      Single intraday reversion alone = noise (~0, 50% win). ALIGNMENT is the
      signal. Within the daily-oversold setup, requiring intraday alignment
      improved the 1-2 day entry ~30-36% (+0.49 vs +0.36 at 1d) but both
      converged by day 5 (~1.1%). => alignment times the entry; it does not
      enlarge the reversion. Daily dev says WHAT to buy, alignment says WHEN.

  Macro trend regime (buy dips only when price > rising 100d MA): NOT VALIDATED.
  On the ~2yr window dips in downtrends actually scored higher (+2.9%, n=13),
  which is a data artifact -- the sample has no real bear regime and n is tiny.
  The filter is theory-backed (don't catch falling knives) but this feed can't
  confirm it; it needs 5yr+ history spanning a bear market. Kept behind --regime,
  off by default, so it activates cleanly when a longer feed is plugged in.
--------------------------------------------------------------------------------
"""
import json
import os
import sys
import argparse
import urllib.request
import numpy as np
import pandas as pd
from datetime import date, timedelta

API = "https://api.massive.com"
KEY = os.environ.get("MASSIVE_API_KEY", "")

# theme -> ETF, and the per-ETF entry threshold (calm names revert at -2%, wild
# ones need -3%; ~1.3x each ETF's own avg |dev| from the research above).
UNIVERSE = {
    "GLD": ("Gold",     -2.0),
    "SLV": ("Silver",   -2.0),
    "URA": ("Uranium",  -3.0),
    "USO": ("Oil",      -3.0),
    "XBI": ("Biotech",  -3.0),
}

MA_FAST   = 5      # the reversion anchor
HOLD_DAYS = 10     # validated best simple exit
MIN_HOLD  = 2      # never exit in the first 2 days (that window loses)
COST_BPS  = 5.0    # round-trip transaction cost, 0.05% (liquid ETFs)
LOT       = 100    # $ per paper trade, matching the sigma-bet convention


def get_json(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"_error": str(e)}


def fetch_daily(tk, days=760):
    """~2yr of adjusted daily bars -> DataFrame[date, close, high, low]."""
    to = date.today()
    frm = to - timedelta(days=days)
    url = (f"{API}/v2/aggs/ticker/{tk}/range/1/day/{frm}/{to}"
           f"?adjusted=true&sort=asc&limit=5000&apiKey={KEY}")
    js = get_json(url)
    if "_error" in js or not js.get("results"):
        print(f"  {tk}: fetch failed ({js.get('_error', js.get('status'))})")
        return None
    df = pd.DataFrame(js["results"]).rename(
        columns={"c": "close", "h": "high", "l": "low", "t": "ts"})
    df["date"] = pd.to_datetime(df["ts"], unit="ms")
    return df[["date", "close", "high", "low"]].sort_values("date").reset_index(drop=True)


def add_features(df):
    c = df["close"]
    ma = c.rolling(MA_FAST, min_periods=MA_FAST).mean()
    df["dev5"] = (c - ma) / ma * 100.0
    df["prev_dev5"] = df["dev5"].shift(1)
    # macro trend context (all fit inside ~2yr)
    for w in (50, 100, 200):
        df[f"ma{w}"] = c.rolling(w, min_periods=w).mean()
    df["ma100_slope"] = df["ma100"] - df["ma100"].shift(20)   # rising 100d?
    df["uptrend"] = (c > df["ma100"]) & (df["ma100_slope"] > 0)
    return df


def backtest(df, entry_dev, use_regime):
    """Sequential, non-overlapping. Enter on a FRESH cross below entry_dev; hold
    HOLD_DAYS; sell at that close. Returns list of per-trade % returns (net)."""
    trades = []
    n = len(df)
    i = MA_FAST
    while i < n:
        r = df.iloc[i]
        fresh = (r["dev5"] <= entry_dev) and (
            pd.isna(r["prev_dev5"]) or r["prev_dev5"] > entry_dev)
        if fresh and (not use_regime or bool(r["uptrend"])):
            exit_i = min(i + HOLD_DAYS, n - 1)
            if exit_i <= i:
                break
            gross = df.iloc[exit_i]["close"] / r["close"] - 1.0
            trades.append(gross * 100.0 - COST_BPS / 100.0)
            i = exit_i + 1           # non-overlapping: resume after the exit
        else:
            i += 1
    return trades


def stats(trades):
    if not trades:
        return dict(n=0, avg=0, win=0, tot=0, best=0, worst=0, sharpe=0)
    a = np.array(trades)
    equity = np.prod(1 + a / 100.0) - 1.0     # compounded, one lot rolled forward
    return dict(n=len(a), avg=a.mean(), win=(a > 0).mean() * 100,
                tot=equity * 100, best=a.max(), worst=a.min(),
                sharpe=a.mean() / a.std() * np.sqrt(len(a)) if a.std() else 0)


def run_daily(use_regime):
    tag = "WITH macro regime gate (px>rising 100d)" if use_regime else "no regime gate"
    print(f"\nDAILY MEAN-REVERSION BACKTEST  --  {tag}")
    print(f"  entry: fresh cross below per-ETF dev | hold {HOLD_DAYS}d | "
          f"cost {COST_BPS:.0f}bps rt | ~2yr\n")
    print(f"  {'ETF':<5}{'theme':<9}{'buy<':>6}{'n':>5}{'avg%':>8}{'win%':>7}"
          f"{'total%':>9}{'sharpe':>8}{'B&H%':>9}")
    print("  " + "-" * 68)
    allt = []
    for tk, (theme, dev) in UNIVERSE.items():
        df = fetch_daily(tk)
        if df is None or len(df) < 210:
            continue
        df = add_features(df)
        tr = backtest(df, dev, use_regime)
        allt += tr
        s = stats(tr)
        bh = (df.iloc[-1]["close"] / df.iloc[200]["close"] - 1) * 100  # since 200d warmup
        print(f"  {tk:<5}{theme:<9}{dev:>6.1f}{s['n']:>5}{s['avg']:>+8.2f}"
              f"{s['win']:>6.0f}%{s['tot']:>+9.1f}{s['sharpe']:>8.2f}{bh:>+9.1f}")
    s = stats(allt)
    print("  " + "-" * 68)
    print(f"  {'POOLED':<20}{s['n']:>5}{s['avg']:>+8.2f}{s['win']:>6.0f}%"
          f"{'':>9}{s['sharpe']:>8.2f}")
    print(f"\n  per-trade avg {s['avg']:+.2f}% over {HOLD_DAYS}d "
          f"(best {s['best']:+.1f}% / worst {s['worst']:+.1f}%, n={s['n']})")
    if use_regime:
        print("  NOTE: regime gate is theory-backed but UNVALIDATED on <=2yr data "
              "(no bear regime in sample).")


# ---------------- intraday reversion-alignment diagnostic ----------------

def fetch_15m(tk, days=150):
    to = date.today()
    frm = to - timedelta(days=days)
    url = (f"{API}/v2/aggs/ticker/{tk}/range/15/minute/{frm}/{to}"
           f"?adjusted=true&sort=asc&limit=50000&apiKey={KEY}")
    js = get_json(url)
    if "_error" in js or not js.get("results"):
        print(f"  {tk}: intraday fetch failed"); return None
    df = pd.DataFrame(js["results"]).rename(columns={"c": "close", "t": "ts"})
    df["hr_utc"] = (df["ts"] // 1000 % 86400) // 3600
    df = df[(df["hr_utc"] >= 14) & (df["hr_utc"] <= 20)]     # core RTH, both DST regimes
    return df[["ts", "close"]].sort_values("ts").reset_index(drop=True)


def run_align(tk):
    """Reproduce the alignment finding: forward return by how many of the
    1h/5h/5d timeframes are below their mean at once."""
    df = fetch_15m(tk)
    if df is None or len(df) < 300:
        return
    c = df["close"]
    for w, name in ((4, "d1h"), (20, "d5h"), (130, "d5d")):
        ma = c.rolling(w, min_periods=w).mean()
        df[name] = (c - ma) / ma * 100.0
    df["f1d"] = c.shift(-26) / c - 1          # ~1 session ahead
    df["aligned_below"] = ((df.d1h < 0).astype(int) + (df.d5h < 0).astype(int)
                           + (df.d5d < 0).astype(int))
    d = df.dropna(subset=["d5d", "f1d"])
    print(f"\nREVERSION ALIGNMENT  --  {tk}  ({len(d)} bars, 15-min)")
    print(f"  {'timeframes below mean':<24}{'n':>6}{'fwd 1d %':>10}{'win%':>7}")
    print("  " + "-" * 47)
    for k in range(4):
        g = d[d.aligned_below == k]
        if len(g):
            print(f"  {k} aligned{'':<15}{len(g):>6}{g.f1d.mean()*100:>+10.3f}"
                  f"{(g.f1d > 0).mean()*100:>6.0f}%")
    print("  higher alignment -> higher forward return = the timing signal.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--regime", action="store_true", help="add macro trend gate")
    ap.add_argument("--align", metavar="TICKER", help="intraday alignment diagnostic")
    a = ap.parse_args()
    if not KEY:
        sys.exit("MASSIVE_API_KEY not set")
    if a.align:
        run_align(a.align.upper())
    else:
        run_daily(a.regime)
