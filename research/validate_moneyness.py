"""Forward validation of strike-relativity (moneyness) features at universe scale."""
import numpy as np
import pandas as pd
from pathlib import Path

BASE = Path("/Users/christian/dev/gold_forecast/opt_dayaggs")
STK  = Path("/Users/christian/dev/gold_forecast/grouped_stocks")

mny = pd.read_csv(BASE / "moneyness_daily.csv")
mny["date"] = pd.to_datetime(mny["date"])

closes_list = []
for f in sorted(STK.glob("2026-*.csv")):
    df = pd.read_csv(f)[["T","c"]].rename(columns={"T":"und","c":"close"})
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

for c in ["call_vol","vXm","v_otm10","v_otm20","v_otm30","v_jump"]:
    feat[f"{c}_10d"] = g[c].apply(lambda s: s.rolling(10, min_periods=5).sum())

feat["vw_mny_10d"] = feat["vXm_10d"] / feat["call_vol_10d"].replace(0, np.nan)
feat["sh_otm10"] = feat["v_otm10_10d"] / feat["call_vol_10d"].replace(0, np.nan)
feat["sh_otm20"] = feat["v_otm20_10d"] / feat["call_vol_10d"].replace(0, np.nan)
feat["sh_otm30"] = feat["v_otm30_10d"] / feat["call_vol_10d"].replace(0, np.nan)
feat["sh_jump"]  = feat["v_jump_10d"]  / feat["call_vol_10d"].replace(0, np.nan)

def fwd_max_dod(gr, days=15):
    c = gr["close"].values; n = len(c); out = np.full(n, np.nan)
    for i in range(n - 1):
        end = min(i + days + 1, n)
        win = c[i+1:end]; prev = c[i:end-1]
        with np.errstate(invalid="ignore", divide="ignore"):
            dod = np.where(prev > 0, win/prev - 1, 0)
        if len(dod): out[i] = np.nanmax(dod)
    return pd.Series(out, index=gr.index)
feat["fwd_max"] = feat.groupby("und", group_keys=False).apply(fwd_max_dod)

elig = feat.dropna(subset=["vw_mny_10d","fwd_max","close","drift_10d"]).copy()
elig = elig[(elig["close"] > 3) & (elig["vol_med_10d"] >= 100) & (elig["call_vol_10d"] >= 500)]
print(f"Eligible rows: {len(elig):,}")

base20 = (elig["fwd_max"] >= 0.20).mean()
base40 = (elig["fwd_max"] >= 0.40).mean()
print(f"BASE: p20={base20:.2%}  p40={base40:.2%}\n")

def dedup(fires):
    fires = fires.sort_values(["und","date"])
    keep = []; last = {}
    for idx, r in fires.iterrows():
        if r["und"] in last and (r["date"] - last[r["und"]]).days < 21: continue
        last[r["und"]] = r["date"]; keep.append(idx)
    return fires.loc[keep]

def test(mask, label):
    fires = dedup(elig[mask])
    n = len(fires)
    if n == 0:
        print(f"{label:<74} n=0"); return
    p20 = (fires["fwd_max"] >= 0.20).mean()
    p40 = (fires["fwd_max"] >= 0.40).mean()
    print(f"{label:<74} n={n:>5}  p20={p20:>6.1%} ({p20/base20:>4.1f}x)  p40={p40:>6.1%} ({p40/base40:>4.1f}x)")

print(f"{'signature':<74} {'n':>7}  {'p20 (lift)':>16}  {'p40 (lift)':>16}")
print("-"*124)

# volume-weighted moneyness thresholds — "how far OTM is the average call traded"
for thr in [0.05, 0.10, 0.15, 0.20]:
    test(elig["vw_mny_10d"] >= thr, f"vw moneyness >= +{thr:.0%}")
print()
# deep OTM shares
for col, lbl in [("sh_otm10",">10% OTM share"),("sh_otm20",">20% OTM share"),("sh_otm30",">30% OTM share")]:
    for thr in [0.3, 0.5]:
        test(elig[col] >= thr, f"{lbl} >= {thr:.0%}")
print()
# jump bets (deep OTM + near expiry)
for thr in [0.2, 0.3, 0.5]:
    test(elig["sh_jump"] >= thr, f"jump-bet share (>=15% OTM, <=45DTE) >= {thr:.0%}")
print()
# combos with drift/call-heavy
test((elig["sh_jump"] >= 0.3) & (elig["drift_10d"].between(0.05, 0.30)), "jump>=30% + drift +5..30%")
test((elig["sh_jump"] >= 0.3) & (elig["ch_days_10d"] >= 6), "jump>=30% + 6+ call-heavy days")
test((elig["sh_jump"] >= 0.3) & (elig["drift_10d"].between(0.05, 0.30)) & (elig["ch_days_10d"] >= 6),
     "jump>=30% + drift + call-heavy")
test((elig["vw_mny_10d"] >= 0.15) & (elig["drift_10d"].between(0.05, 0.30)) & (elig["ch_days_10d"] >= 6),
     "vw_mny>=15% + drift + call-heavy")
test((elig["sh_otm30"] >= 0.3) & (elig["drift_10d"].between(0.05, 0.30)) & (elig["ch_days_10d"] >= 6),
     "otm30 share>=30% + drift + call-heavy")
test((elig["sh_jump"] >= 0.3) & (elig["drift_10d"].between(0.05, 0.30)) & (elig["ch_days_10d"] >= 6) &
     (elig["call_vol_10d"] <= 15000),
     "jump>=30% + drift + call-heavy + quiet book")
