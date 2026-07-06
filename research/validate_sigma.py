"""Forward + regime-slice validation of sigma-distance jump bets (notional-weighted)."""
import numpy as np
import pandas as pd
from pathlib import Path

BASE = Path("/Users/christian/dev/gold_forecast/opt_dayaggs")
STK  = Path("/Users/christian/dev/gold_forecast/grouped_stocks")

sig = pd.read_csv(BASE / "sigma_bets_daily.csv")
sig["date"] = pd.to_datetime(sig["date"])

closes_list = []
for f in sorted(STK.glob("2026-*.csv")):
    df = pd.read_csv(f)[["T","c","h"]].rename(columns={"T":"und","c":"close","h":"high"})
    df["date"] = pd.to_datetime(f.stem)
    closes_list.append(df)
closes = pd.concat(closes_list, ignore_index=True).sort_values(["und","date"]).reset_index(drop=True)

feat = pd.read_csv(BASE / "trades_features.csv")
feat["date"] = pd.to_datetime(feat["date"])
feat = feat.sort_values(["und","date"]).reset_index(drop=True)
feat = feat.merge(sig, on=["und","date"], how="left")
feat = feat.merge(closes, on=["und","date"], how="left")
for c in ["call_notional","n2_notional","n3_notional","be20_notional"]:
    feat[c] = feat[c].fillna(0)

g = feat.groupby("und", group_keys=False)
feat["drift_10d"] = g["close"].apply(lambda s: s / s.shift(10) - 1)
feat["vol_med_10d"] = g["vol_total"].apply(lambda s: s.rolling(10, min_periods=5).median())
for c in ["call_notional","n2_notional","n3_notional","be20_notional"]:
    feat[f"{c}_10d"] = g[c].apply(lambda s: s.rolling(10, min_periods=5).sum())

feat["sh_n2"] = feat["n2_notional_10d"] / feat["call_notional_10d"].replace(0, np.nan)
feat["sh_n3"] = feat["n3_notional_10d"] / feat["call_notional_10d"].replace(0, np.nan)
feat["sh_be20"] = feat["be20_notional_10d"] / feat["call_notional_10d"].replace(0, np.nan)

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

elig = feat.dropna(subset=["sh_n2","fwd_max","close","drift_10d"]).copy()
elig = elig[(elig["close"] > 3) & (elig["vol_med_10d"] >= 100) & (elig["call_notional_10d"] >= 100_000)]
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
    ev = fires[fires["fwd_days"] >= 5]
    if len(ev) == 0:
        print(f"{label:<74} n={n} (none evaluable)"); return
    p20 = (ev["fwd_max"] >= 0.20).mean()
    p40 = (ev["fwd_max"] >= 0.40).mean()
    mp = ev["fwd_peak"].mean()
    print(f"{label:<74} n={n:>5}  p20={p20:>6.1%} ({p20/base20:>4.1f}x)  p40={p40:>5.1%} ({p40/base40:>4.1f}x)  peak={mp:+.1%}")

print(f"{'signature':<74} {'n':>7}  {'p20 (lift)':>16}  {'p40 (lift)':>14}  {'peak':>7}")
print("-"*130)

# dose-response on sigma-distance share (notional weighted)
for thr in [0.1, 0.2, 0.3, 0.5]:
    test(elig["sh_n2"] >= thr, f"2-sigma notional share >= {thr:.0%}")
print()
for thr in [0.05, 0.1, 0.2, 0.3]:
    test(elig["sh_n3"] >= thr, f"3-sigma notional share >= {thr:.0%}")
print()
for thr in [0.1, 0.2, 0.3, 0.5]:
    test(elig["sh_be20"] >= thr, f"breakeven>=20% notional share >= {thr:.0%}")
print()
# absolute dollars behind the extreme bets
test((elig["n3_notional_10d"] >= 250_000), "3-sigma notional >= $250k/10d (absolute)")
test((elig["n3_notional_10d"] >= 1_000_000), "3-sigma notional >= $1M/10d (absolute)")
print()
# combos
test((elig["sh_n3"] >= 0.2) & elig["drift_10d"].between(0.05, 0.30), "3-sig share>=20% + drift +5..30%")
test((elig["sh_n3"] >= 0.2) & (elig["n3_notional_10d"] >= 250_000), "3-sig share>=20% + >= $250k behind it")
test((elig["sh_n3"] >= 0.2) & (elig["n3_notional_10d"] >= 250_000) & elig["drift_10d"].between(0.05, 0.30),
     "3-sig share>=20% + $250k + drift")
test((elig["sh_n2"] >= 0.5) & (elig["n2_notional_10d"] >= 500_000) & elig["drift_10d"].between(0.05, 0.30),
     "2-sig share>=50% + $500k + drift")

# ---- regime slices for the leading candidates ----
tdays = sorted(elig["date"].unique())
last20 = tdays[-20:]
slices = [("SLICE A", set(last20[:10])), ("SLICE B", set(last20[10:]))]
CANDS = {
    "3-sig share>=20% + $250k": (elig["sh_n3"] >= 0.2) & (elig["n3_notional_10d"] >= 250_000),
    "3-sig share>=20% + $250k + drift": (elig["sh_n3"] >= 0.2) & (elig["n3_notional_10d"] >= 250_000) & elig["drift_10d"].between(0.05, 0.30),
}
print(f"\n{'='*110}\n  REGIME SLICES (in-slice base rates)\n{'='*110}")
for label, mask in CANDS.items():
    for sname, sl in slices:
        sub = elig[elig["date"].isin(sl)]
        sb = (sub[sub["fwd_days"] >= 5]["fwd_max"] >= 0.20).mean()
        fires = dedup(elig[mask & elig["date"].isin(sl)])
        ev = fires[fires["fwd_days"] >= 5]
        if len(ev) == 0:
            print(f"  {label:<42} {sname}: fires={len(fires)}, none evaluable"); continue
        p20 = (ev["fwd_max"] >= 0.20).mean()
        print(f"  {label:<42} {sname}: fires={len(fires):>3} eval={len(ev):>3}  base={sb:.1%}  p20={p20:.1%} ({p20/sb:.1f}x)  peak={ev['fwd_peak'].mean():+.1%}")
