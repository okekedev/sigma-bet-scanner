"""In-regime rolling test of the jump-bet signature: last 20 trading days only,
split into two 10-day slices. Base rates and lift computed WITHIN each slice,
so the signature is judged against its own regime, not the full-sample average.

Forward outcomes use whatever forward days exist (noted); late fires are truncated.
"""
import numpy as np
import pandas as pd
from pathlib import Path

BASE = Path("/Users/christian/dev/gold_forecast/opt_dayaggs")
STK  = Path("/Users/christian/dev/gold_forecast/grouped_stocks")

mny = pd.read_csv(BASE / "moneyness_daily.csv")
mny["date"] = pd.to_datetime(mny["date"])

closes_list = []
for f in sorted(STK.glob("2026-*.csv")):
    df = pd.read_csv(f)[["T","c","h","l"]].rename(columns={"T":"und","c":"close","h":"high","l":"low"})
    df["date"] = pd.to_datetime(f.stem)
    closes_list.append(df)
closes = pd.concat(closes_list, ignore_index=True).sort_values(["und","date"]).reset_index(drop=True)

feat = pd.read_csv(BASE / "trades_features.csv")
feat["date"] = pd.to_datetime(feat["date"])
feat = feat.sort_values(["und","date"]).reset_index(drop=True)
feat = feat.merge(mny, on=["und","date"], how="left")
feat = feat.merge(closes, on=["und","date"], how="left")

g = feat.groupby("und", group_keys=False)
feat["drift_10d"] = g["close"].apply(lambda s: s / s.shift(10) - 1)
feat["ch_days_10d"] = g["pc_ratio"].apply(lambda s: s.le(0.5).rolling(10, min_periods=5).sum())
feat["vol_med_10d"] = g["vol_total"].apply(lambda s: s.rolling(10, min_periods=5).median())
for c in ["call_vol","v_jump"]:
    feat[f"{c}_10d"] = g[c].apply(lambda s: s.rolling(10, min_periods=5).sum())
feat["sh_jump"] = feat["v_jump_10d"] / feat["call_vol_10d"].replace(0, np.nan)

# forward outcomes: max dod, peak, close over up to 15 fwd days + count available
def fwd_stats(gr, days=15):
    c = gr["close"].values; h = gr["high"].values; n = len(c)
    fmax = np.full(n, np.nan); fpeak = np.full(n, np.nan); fdays = np.zeros(n)
    for i in range(n - 1):
        end = min(i + days + 1, n)
        win_c = c[i+1:end]; prev = c[i:end-1]; win_h = h[i+1:end]
        if len(win_c) == 0: continue
        with np.errstate(invalid="ignore", divide="ignore"):
            dod = np.where(prev > 0, win_c/prev - 1, 0)
        fmax[i] = np.nanmax(dod)
        fpeak[i] = np.nanmax(win_h) / c[i] - 1 if c[i] > 0 else np.nan
        fdays[i] = len(win_c)
    return pd.DataFrame({"fwd_max": fmax, "fwd_peak": fpeak, "fwd_days": fdays}, index=gr.index)
fw = feat.groupby("und", group_keys=False).apply(fwd_stats)
feat = feat.join(fw)

elig = feat.dropna(subset=["sh_jump","fwd_max","close","drift_10d"]).copy()
elig = elig[(elig["close"] > 3) & (elig["vol_med_10d"] >= 100) & (elig["call_vol_10d"] >= 500)]

# last 20 trading days, split at 10
tdays = sorted(elig["date"].unique())
last20 = tdays[-20:]
slice_A = set(last20[:10])   # 06-02..06-15
slice_B = set(last20[10:])   # 06-16..06-30
print(f"Slice A: {pd.Timestamp(last20[0]).date()} .. {pd.Timestamp(last20[9]).date()}")
print(f"Slice B: {pd.Timestamp(last20[10]).date()} .. {pd.Timestamp(last20[19]).date()}\n")

SIG = lambda d: (d["sh_jump"] >= 0.30) & (d["drift_10d"].between(0.05, 0.30))

def dedup(fires):
    fires = fires.sort_values(["und","date"])
    keep = []; last = {}
    for idx, r in fires.iterrows():
        if r["und"] in last and (r["date"] - last[r["und"]]).days < 21: continue
        last[r["und"]] = r["date"]; keep.append(idx)
    return fires.loc[keep]

for name, sl in [("SLICE A (days 1-10)", slice_A), ("SLICE B (days 11-20, current regime)", slice_B)]:
    sub = elig[elig["date"].isin(sl)]
    base20 = (sub[sub["fwd_days"] >= 5]["fwd_max"] >= 0.20).mean()
    fires = dedup(sub[SIG(sub)])
    fires_eval = fires[fires["fwd_days"] >= 5]
    n, ne = len(fires), len(fires_eval)
    print(f"{'='*100}")
    print(f"  {name}   eligible rows={len(sub):,}   slice base p20={base20:.2%}")
    print(f"{'='*100}")
    if ne:
        p20 = (fires_eval["fwd_max"] >= 0.20).mean()
        p40 = (fires_eval["fwd_max"] >= 0.40).mean()
        mean_peak = fires_eval["fwd_peak"].mean()
        print(f"  JUMP>=30% + drift: fires={n} evaluable(>=5 fwd days)={ne}")
        print(f"  p20={p20:.1%} ({p20/base20:.1f}x slice base)   p40={p40:.1%}   mean_fwd_peak={mean_peak:+.1%}\n")
    else:
        print(f"  fires={n}, none evaluable yet\n")
    print(f"  {'date':<12} {'und':<7} {'close':>8} {'sh_jump':>8} {'drift':>7} {'fwd_days':>9} {'fwd_max':>8} {'fwd_peak':>9}")
    for _, r in fires.sort_values("date").iterrows():
        fm = f"{r['fwd_max']*100:+.1f}%" if pd.notna(r["fwd_max"]) and r["fwd_days"] > 0 else "—"
        fp = f"{r['fwd_peak']*100:+.1f}%" if pd.notna(r["fwd_peak"]) and r["fwd_days"] > 0 else "—"
        hit = " ★" if pd.notna(r["fwd_max"]) and r["fwd_max"] >= 0.20 else ""
        print(f"  {r['date'].strftime('%Y-%m-%d'):<12} {r['und']:<7} {r['close']:>8.2f} {r['sh_jump']:>8.1%} "
              f"{r['drift_10d']:>+6.1%} {int(r['fwd_days']):>9} {fm:>8} {fp:>9}{hit}")
    print()
