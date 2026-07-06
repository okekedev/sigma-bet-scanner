"""Classify 50%+ pops into Flavor 1 (extrinsic-driven, stock flat, IV runs up)
vs Flavor 2 (intrinsic-driven, stock drifts up, controlled IV)."""
import pandas as pd
import numpy as np
from pathlib import Path

BASE = Path("/Users/christian/dev/gold_forecast/opt_dayaggs")
STK  = Path("/Users/christian/dev/gold_forecast/grouped_stocks")

feat = pd.read_csv(BASE / "trades_features.csv")
feat["date"] = pd.to_datetime(feat["date"])
feat = feat.sort_values(["und","date"]).reset_index(drop=True)

closes_list = []
for f in sorted(STK.glob("2026-*.csv")):
    df = pd.read_csv(f)[["T","c","v","vw"]].rename(columns={"T":"und","c":"close"})
    df["date"] = pd.to_datetime(f.stem)
    df["stock_dv"] = df["v"] * df["vw"]
    closes_list.append(df)
closes = pd.concat(closes_list, ignore_index=True).sort_values(["und","date"])
closes["prev_close"] = closes.groupby("und")["close"].shift(1)
closes["dod"] = closes["close"]/closes["prev_close"] - 1

optionable = set(feat["und"].unique())

# 50%+ pops on optionable tickers
pops = closes[
    (closes["dod"] >= 0.50) &
    (closes["close"] > 3) & (closes["prev_close"] > 3) &
    (closes["stock_dv"] > 10_000_000) &
    (closes["und"].isin(optionable))
].copy().sort_values("date")

print(f"Analyzing {len(pops)} pops of 50%+ on optionable mid-caps\n")

results = []
for _, pop in pops.iterrows():
    t = pop["und"]; d = pop["date"]

    # Pre-pop window: 10 trading days before
    prior_closes = closes[(closes["und"] == t) & (closes["date"] < d)].tail(10)
    prior_feat = feat[(feat["und"] == t) & (feat["date"] < d)].tail(10)
    if len(prior_closes) < 5 or len(prior_feat) < 5:
        continue

    # STOCK DRIFT: return from start to end of window
    stock_drift = prior_closes["close"].iloc[-1] / prior_closes["close"].iloc[0] - 1
    # Also: was there sustained upward drift?
    up_days = (prior_closes["close"].diff() > 0).sum()

    # IV PROXY: theta_paid_daily / vol_total (theta per contract traded)
    # A stable proxy for IV in the market's per-contract pricing
    prior_feat_v = prior_feat.copy()
    prior_feat_v["theta_per_contract"] = prior_feat_v["theta_paid_daily"] / prior_feat_v["vol_total"].replace(0, np.nan)
    iv_proxy_start = prior_feat_v["theta_per_contract"].iloc[:3].mean()
    iv_proxy_end = prior_feat_v["theta_per_contract"].iloc[-3:].mean()
    iv_proxy_growth = (iv_proxy_end / iv_proxy_start - 1) if pd.notna(iv_proxy_start) and iv_proxy_start > 0 else np.nan

    # Cumulative call delta buildup
    cum_delta = prior_feat["delta_call"].sum()
    # Average PC ratio
    avg_pc = prior_feat["pc_ratio"].mean()
    # Volume ratio (max/median) - detects loud spikes
    vol_median = prior_feat["vol_total"].median()
    vol_max = prior_feat["vol_total"].max()
    vol_ratio = vol_max / vol_median if vol_median > 0 else np.nan

    # Classification logic
    # Flavor 2: stock drifted up sustainably (>+5%), controlled IV growth (<+50%)
    # Flavor 1: stock roughly flat (-3% to +5%), IV growing more (+50%+)
    if stock_drift > 0.05 and (pd.isna(iv_proxy_growth) or iv_proxy_growth < 0.50):
        flavor = "F2 (intrinsic)"
    elif -0.03 <= stock_drift <= 0.05:
        flavor = "F1 (extrinsic)"
    elif stock_drift > 0.05 and iv_proxy_growth >= 0.50:
        flavor = "MIXED (both)"
    elif stock_drift < -0.03:
        flavor = "OVERSOLD"
    else:
        flavor = "?"

    results.append({
        "date": d, "ticker": t, "pop_%": pop["dod"]*100,
        "stock_drift_10d": stock_drift*100,
        "up_days_of_10": up_days,
        "iv_proxy_growth_%": iv_proxy_growth*100 if pd.notna(iv_proxy_growth) else np.nan,
        "avg_pc": avg_pc,
        "cum_delta_10d": cum_delta,
        "vol_max_over_med": vol_ratio,
        "flavor": flavor,
    })

df = pd.DataFrame(results)
print(f"{'date':<12} {'ticker':<7} {'pop%':>6} {'stk_drift':>10} {'up_d':>5} {'iv_grow':>8} {'avg_pc':>7} {'cum_delta':>11} {'vmax/med':>9} {'flavor':<16}")
for _, r in df.iterrows():
    iv_s = f"{r['iv_proxy_growth_%']:+.0f}%" if pd.notna(r['iv_proxy_growth_%']) else "—"
    print(f"{r['date'].strftime('%Y-%m-%d'):<12} {r['ticker']:<7} {r['pop_%']:>+5.0f}% {r['stock_drift_10d']:>+9.1f}% {int(r['up_days_of_10']):>5} {iv_s:>8} {r['avg_pc']:>7.2f} {int(r['cum_delta_10d']):>11,} {r['vol_max_over_med']:>9.1f} {r['flavor']:<16}")

# Summary
print(f"\n{'='*80}")
print(f"  Flavor breakdown")
print(f"{'='*80}")
print(df["flavor"].value_counts())

# Save for downstream
df.to_csv(BASE / "pop_flavors.csv", index=False)
print(f"\nSaved to pop_flavors.csv")
