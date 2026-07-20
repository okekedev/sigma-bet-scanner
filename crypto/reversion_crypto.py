#!/usr/bin/env python3
"""Crypto mean-reversion scanner — the ETF 5d-MA dip-buy model, ported to the top
liquid coins on HOURLY bars over the last ~30 days.

    python3 reversion_crypto.py                 # full sweep + live signal
    python3 reversion_crypto.py --refresh       # re-pull OHLC (ignore cache)
    python3 reversion_crypto.py --n 20 --cost 40

Data: Kraken PUBLIC REST (no API key). Kraken's OHLC endpoint returns at most 720
candles per call, so hourly => ~30 days of history. That is deliberate here: the
current regime is what we care about, not a 2yr backtest dominated by old regimes.

WHY THIS DIFFERS FROM THE ETF MODEL (research/reversion_alignment.py):
  * 24/7 market — calendar hours = trading hours, no session gating.
  * Far higher vol — a flat -2%/-3% ETF threshold is meaningless; each coin's
    entry threshold is auto-sized to ~1.3x its OWN avg |dev| from the MA.
  * Short horizons — 720 hourly bars can't support a 10-day hold (that'd be ~3
    trades). Holds are measured in HOURS and swept.
  * Fees dominate — Kraken round-trip is ~0.3-0.5% vs ~0.05% for liquid ETFs. At
    short holds this eats most edges, so we print GROSS and NET side by side.

Universe = top-N USD spot pairs by MEDIAN hourly dollar volume over the window
(robust to one-day pumps), requiring near-full 30-day history (drops new tokens),
stablecoins + fiat excluded.
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

import numpy as np
import pandas as pd

KRAKEN = "https://api.kraken.com/0/public"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# Not crypto reversion candidates: pegged (no reversion) or fiat crosses.
STABLE = {"USDT", "USDC", "DAI", "PYUSD", "USDG", "RLUSD", "TUSD", "USDS", "USDR",
          "EURT", "EURC", "USTC", "GUSD", "USDQ", "USD"}
FIAT = {"EUR", "GBP", "AUD", "CAD", "CHF", "JPY"}

INTERVAL = 60          # minutes per bar (hourly)
MIN_BARS = 600         # require ~25+ of 30 days present (drops brand-new listings)
THRESH_MULT = 1.3      # entry threshold = -1.3x each coin's avg |dev| (ETF-calibrated)

# LOCKED base config (chosen from the MA x hold sweep — see module docstring / git
# history): a 5-day MA anchor with a ~2-day hold was the clear sweet spot, +1.44%
# median/trade at 67% win, and it held up after dropping outlier names. Short MAs
# fire more but win ~coin-flip; short holds get eaten by fees.
ANCHOR_MA = 120        # 5-day MA anchor, in hours
HOLD      = 48         # 2-day hold, in hours

# Universe hygiene: a coin whose avg|dev| runs way above the pack (e.g. SYN at
# ~10%) is a thin/unpredictable name that inflates pooled stats. Drop anything
# more than this multiple of the universe's MEDIAN avg|dev| at the anchor MA.
MAX_ADEV_MULT = 2.5

MA_GRID   = [12, 24, 48, 72, 120]        # fast-MA windows tested in --sweep
HOLD_GRID = [4, 8, 12, 24, 48, 72]       # hold horizons tested in --sweep


# ---------------- Kraken public fetch ----------------
def kfetch(path, params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{KRAKEN}/{path}?{q}"
    for i in range(5):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                d = json.loads(r.read())
            if d.get("error"):
                # rate-limit / transient -> back off and retry
                time.sleep(2.0 * (i + 1)); continue
            return d["result"]
        except Exception:
            time.sleep(2.0 * (i + 1))
    return None


def usd_spot_pairs():
    """{display_coin: kraken_pair} for USD-quoted spot, minus stables/fiat.
    Dedups multiple pairs per coin, keeping the first."""
    res = kfetch("AssetPairs", {}) or {}
    out = {}
    for pk, meta in res.items():
        if meta.get("quote") != "ZUSD":
            continue
        base = meta.get("base", "")
        disp = base[1:] if (len(base) == 4 and base[0] in "XZ") else base
        disp = {"XBT": "BTC", "XDG": "DOGE"}.get(disp, disp)
        if disp in STABLE or disp in FIAT or "." in base:   # ".S"/".B" = staked/bonded
            continue
        out.setdefault(disp, pk)
    return out


def top_by_24h(pairs, pool):
    """Cheap pre-filter: rank the ~700 pairs by a single 24h $vol snapshot and keep
    the top `pool` as candidates (so we only pull hourly OHLC for a shortlist)."""
    tick = kfetch("Ticker", {}) or {}
    rows = []
    for disp, pk in pairs.items():
        t = tick.get(pk)
        if not t:
            continue
        dv = float(t["v"][1]) * float(t["p"][1])   # 24h volume * vwap
        rows.append((disp, pk, dv))
    rows.sort(key=lambda r: -r[2])
    return rows[:pool]


def fetch_ohlc(pk, refresh=False):
    """Hourly OHLC -> DataFrame[dt, close, high, low, vwap, volume]. Cached to
    crypto/data/<pair>.csv; Kraken returns the most recent ~720 bars."""
    cache = os.path.join(CACHE_DIR, f"{pk}.csv")
    if not refresh and os.path.exists(cache) and (time.time() - os.path.getmtime(cache) < 6 * 3600):
        return pd.read_csv(cache, parse_dates=["dt"])
    res = kfetch("OHLC", {"pair": pk, "interval": INTERVAL})
    if not res:
        return None
    key = next((k for k in res if k != "last"), None)
    rows = res.get(key) or []
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close",
                                     "vwap", "volume", "count"])
    for c in ["close", "high", "low", "vwap", "volume"]:
        df[c] = df[c].astype(float)
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    df = df[["dt", "close", "high", "low", "vwap", "volume"]].sort_values("dt").reset_index(drop=True)
    os.makedirs(CACHE_DIR, exist_ok=True)
    df.to_csv(cache, index=False)
    return df


# ---------------- universe ----------------
def build_universe(n, pool, refresh):
    pairs = usd_spot_pairs()
    cands = top_by_24h(pairs, pool)
    print(f"scanning {len(cands)} candidates (of {len(pairs)} USD spot pairs) for "
          f"the top {n} liquid coins by median hourly $vol ...")
    rows = []
    for i, (disp, pk, _) in enumerate(cands):
        df = fetch_ohlc(pk, refresh)
        if i and refresh:
            time.sleep(1.5)                 # be gentle on the public API on cold pulls
        if df is None or len(df) < MIN_BARS:
            continue
        rows.append({"coin": disp, "pair": pk, "df": df, "bars": len(df),
                     "med_dvol": float((df["vwap"] * df["volume"]).median()),
                     "adev": float(add_dev(df, ANCHOR_MA).abs().mean())})
    rows.sort(key=lambda u: -u["med_dvol"])
    # volatility ceiling: drop names whose avg|dev| dwarfs the liquid pack (thin,
    # unpredictable). Median taken over the top-2n by volume so the cap is set by
    # the coins we actually care about, not the long tail.
    ref = rows[: 2 * n] if len(rows) >= 2 * n else rows
    med_adev = float(np.median([u["adev"] for u in ref])) if ref else 0.0
    cap = MAX_ADEV_MULT * med_adev
    kept, dropped = [], []
    for u in rows:
        (dropped if med_adev and u["adev"] > cap else kept).append(u)
    if dropped:
        print(f"  vol-ceiling ({MAX_ADEV_MULT}x median avg|dev| = {cap:.1f}%) dropped: "
              + ", ".join(f"{u['coin']}({u['adev']:.1f}%)" for u in dropped[:2 * n]))
    return kept[:n]


# ---------------- reversion model ----------------
def add_dev(df, ma_hours):
    c = df["close"]
    ma = c.rolling(ma_hours, min_periods=ma_hours).mean()
    dev = (c - ma) / ma * 100.0
    return dev


def backtest(df, ma_hours, thresh, hold, cost_bps):
    """Fresh cross below `thresh` (a negative %), hold `hold` bars, sell at close.
    Sequential / non-overlapping. Returns (gross_list, net_list) of per-trade %."""
    dev = add_dev(df, ma_hours).values
    close = df["close"].values
    prev = np.concatenate([[np.nan], dev[:-1]])
    n = len(df)
    gross, net = [], []
    i = ma_hours
    while i < n:
        fresh = (dev[i] <= thresh) and (np.isnan(prev[i]) or prev[i] > thresh)
        if fresh:
            j = min(i + hold, n - 1)
            if j <= i:
                break
            g = (close[j] / close[i] - 1.0) * 100.0
            gross.append(g)
            net.append(g - cost_bps / 100.0)
            i = j + 1
        else:
            i += 1
    return gross, net


def pooled(net):
    if not net:
        return dict(n=0, avg=0.0, win=0.0)
    a = np.array(net)
    return dict(n=len(a), avg=float(a.mean()), win=float((a > 0).mean() * 100))


# ---------------- reports ----------------
def sweep(uni, cost_bps):
    """MA-window x hold-period grid: pooled NET avg % per trade, then win% and n.
    Threshold auto-sized per coin per MA."""
    for label, field in [("POOLED NET avg % per trade", "avg"),
                         ("win %", "win"), ("trade count n", "n")]:
        print(f"\n{label}   (cost {cost_bps:.0f} bps round-trip)")
        print("  hold->  " + "".join(f"{h:>8}h" for h in HOLD_GRID))
        for ma in MA_GRID:
            cells = []
            for hold in HOLD_GRID:
                allnet = []
                for u in uni:
                    dev = add_dev(u["df"], ma)
                    thr = -THRESH_MULT * dev.abs().mean()
                    _, net = backtest(u["df"], ma, thr, hold, cost_bps)
                    allnet += net
                s = pooled(allnet)
                if field == "avg":
                    cells.append(f"{s['avg']:>+8.2f}" + " ")
                elif field == "win":
                    cells.append(f"{s['win']:>8.0f}" + " ")
                else:
                    cells.append(f"{s['n']:>8d}" + " ")
            print(f"  {ma:>4}h MA " + "".join(cells))


def per_coin(uni, ma_hours, hold, cost_bps):
    print(f"\nPER-COIN  @ {ma_hours}h MA, hold {hold}h, cost {cost_bps:.0f}bps")
    print(f"  {'coin':<7}{'n':>4}{'gross%':>9}{'net%':>9}{'win%':>7}{'total%':>9}")
    print("  " + "-" * 45)
    allnet = []
    for u in uni:
        dev = add_dev(u["df"], ma_hours)
        thr = -THRESH_MULT * dev.abs().mean()
        gross, net = backtest(u["df"], ma_hours, thr, hold, cost_bps)
        allnet += net
        if not net:
            print(f"  {u['coin']:<7}{0:>4}")
            continue
        g = np.mean(gross); nt = np.mean(net); win = (np.array(net) > 0).mean() * 100
        total = (np.prod(1 + np.array(net) / 100) - 1) * 100
        print(f"  {u['coin']:<7}{len(net):>4}{g:>+9.2f}{nt:>+9.2f}{win:>6.0f}%{total:>+9.1f}")
    s = pooled(allnet)
    print("  " + "-" * 45)
    print(f"  {'POOLED':<7}{s['n']:>4}{'':>9}{s['avg']:>+9.2f}{s['win']:>6.0f}%")


def live_board(uni):
    """What to buy right now: current dev vs the 5-day MA and each coin's auto
    threshold, sorted most-oversold-relative-to-threshold first. 🟢 = triggered."""
    asof = uni[0]["df"]["dt"].iloc[-1]
    rows = []
    for u in uni:
        dev = add_dev(u["df"], ANCHOR_MA)
        cur = float(dev.iloc[-1]); thr = -THRESH_MULT * float(dev.abs().mean())
        rows.append((u["coin"], u["df"]["close"].iloc[-1], cur, thr, cur - thr))
    rows.sort(key=lambda r: r[4])
    buys = [r for r in rows if r[2] <= r[3]]
    print(f"\nLIVE BOARD  @ {ANCHOR_MA}h (5-day) MA · as of {asof:%Y-%m-%d %H:%M} UTC")
    print(f"  {'coin':<7}{'price':>12}{'dev%':>9}{'entry<%':>9}{'gap':>8}   signal")
    print("  " + "-" * 52)
    for coin, px, cur, thr, gap in rows:
        sig = "🟢 BUY" if cur <= thr else ("· below MA" if cur < 0 else "above MA")
        print(f"  {coin:<7}{px:>12.4f}{cur:>+9.2f}{thr:>9.2f}{gap:>+8.2f}   {sig}")
    print("  " + "-" * 52)
    print(f"  {len(buys)} BUY signal(s) now"
          + (": " + ", ".join(b[0] for b in buys) if buys else ""))


def main():
    ap = argparse.ArgumentParser(description="Crypto 5d-MA reversion — top liquid coins, hourly")
    ap.add_argument("--n", type=int, default=20, help="universe size (default 20)")
    ap.add_argument("--pool", type=int, default=50, help="candidate pool by 24h vol")
    ap.add_argument("--cost", type=float, default=40, help="round-trip cost, bps")
    ap.add_argument("--sweep", action="store_true", help="run the full MA x hold grid")
    ap.add_argument("--refresh", action="store_true", help="re-pull OHLC")
    a = ap.parse_args()

    uni = build_universe(a.n, a.pool, a.refresh)
    if not uni:
        sys.exit("no universe — fetch failed?")
    span = f"{uni[0]['df']['dt'].iloc[0]:%Y-%m-%d} -> {uni[0]['df']['dt'].iloc[-1]:%Y-%m-%d}"
    print(f"\nUNIVERSE: top {len(uni)} liquid USD coins on Kraken · hourly · {span}")
    print("  " + ", ".join(u["coin"] for u in uni))
    print(f"  base config: {ANCHOR_MA}h (5-day) MA · hold {HOLD}h · "
          f"entry -{THRESH_MULT}x avg|dev| · cost {a.cost:.0f}bps")

    live_board(uni)
    per_coin(uni, ANCHOR_MA, HOLD, a.cost)      # validation at the locked config
    if a.sweep:
        sweep(uni, a.cost)
    print("\nNOTE: 30-day hourly window, current regime only. Small n per coin — "
          "read the POOLED rows, not single coins. Fees dominate short holds.")


if __name__ == "__main__":
    main()
