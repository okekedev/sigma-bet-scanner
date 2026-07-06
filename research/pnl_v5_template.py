"""End-to-end P&L on the v5 sigma-bet fires (family-deduped, n=27).

Variants:
  A) Buy-hold 15d
  B) Trail 15% off peak-high, hard stop -10%, max 15d
  C) B + flow-fade exit (sh_n3 of fired ticker drops < 0.20 -> exit at close)
  D) C + dip-add (at 2nd day post-fire: if close < entry AND flow persists -> add 2nd lot)

$100 per lot. Stops checked on intraday low against max(peak*0.85, entry*0.90).
"""
import numpy as np
import pandas as pd
from pathlib import Path

BASE = Path("/Users/christian/dev/gold_forecast/opt_dayaggs")
STK  = Path("/Users/christian/dev/gold_forecast/grouped_stocks")

sig = pd.read_csv(BASE / "sigma_bets_daily.csv"); sig["date"] = pd.to_datetime(sig["date"])
closes = pd.concat([
    pd.read_csv(f)[["T","c","h","l"]].rename(columns={"T":"und","c":"close","h":"high","l":"low"}).assign(date=pd.to_datetime(f.stem))
    for f in sorted(STK.glob("2026-*.csv"))
]).sort_values(["und","date"]).reset_index(drop=True)

feat = pd.read_csv(BASE / "trades_features.csv"); feat["date"] = pd.to_datetime(feat["date"])
feat = feat.sort_values(["und","date"]).reset_index(drop=True)
feat = feat.merge(sig, on=["und","date"], how="left").merge(closes, on=["und","date"], how="left")
for c in ["call_notional","n3_notional"]: feat[c] = feat[c].fillna(0)

g = feat.groupby("und", group_keys=False)
feat["drift_10d"] = g["close"].apply(lambda s: s / s.shift(10) - 1)
feat["vol_med_10d"] = g["vol_total"].apply(lambda s: s.rolling(10, min_periods=5).median())
for c in ["call_notional","n3_notional"]:
    feat[f"{c}_10d"] = g[c].apply(lambda s: s.rolling(10, min_periods=5).sum())
feat["sh_n3"] = feat["n3_notional_10d"] / feat["call_notional_10d"].replace(0, np.nan)

elig = feat.dropna(subset=["sh_n3","close","drift_10d"])
elig = elig[(elig["close"] > 3) & (elig["vol_med_10d"] >= 100) & (elig["call_notional_10d"] >= 100_000)]
F = elig[(elig["sh_n3"] >= 0.2) & (elig["n3_notional_10d"] >= 250_000) & elig["drift_10d"].between(0.05, 0.30)].copy()
FAM = {"NVDL":"NVDA","SNXX":"SNDK","SNDU":"SNDK","SNDG":"SNDK","MULL":"MU","WDCX":"WDC","DLLL":"DELL","INTW":"INTC","MVLL":"MRVL","MRVU":"MRVL"}
F["family"] = F["und"].map(lambda t: FAM.get(t, t))
F = F.sort_values(["family","date"])
keep=[]; last={}
for idx, r in F.iterrows():
    if r["family"] in last and (r["date"] - last[r["family"]]).days < 21: continue
    last[r["family"]] = r["date"]; keep.append(idx)
F = F.loc[keep].sort_values("date")

def run_lot(post, entry, flow_exit=False, start_i=0, max_i=15):
    """Simulate one lot entered at `entry` using rows post[start_i:max_i].
    Returns (exit_return, exit_reason, days_held)."""
    peak = entry
    for j in range(start_i, min(max_i, len(post))):
        row = post.iloc[j]
        stop_level = max(peak * 0.85, entry * 0.90)
        if row["low"] <= stop_level:
            return stop_level / entry - 1, ("trail" if stop_level > entry * 0.90 else "SL"), j + 1
        peak = max(peak, row["high"])
        if flow_exit:
            shv = row["sh_n3"]
            if pd.isna(shv) or shv < 0.20:
                return row["close"] / entry - 1, "flow-fade", j + 1
    j_end = min(max_i, len(post)) - 1
    return post.iloc[j_end]["close"] / entry - 1, "time", j_end + 1

results = {"A": [], "B": [], "C": [], "D": []}
detail = []
for _, r in F.iterrows():
    t = r["und"]; d = r["date"]; entry = r["close"]
    post = feat[(feat["und"] == t) & (feat["date"] > d)].head(15).reset_index(drop=True)
    if len(post) < 3: continue

    # A: buy-hold
    ra = post.iloc[min(14, len(post)-1)]["close"] / entry - 1
    results["A"].append(("hold", ra, 100))

    # B: trail only
    rb, reasonb, daysb = run_lot(post, entry, flow_exit=False)
    results["B"].append((reasonb, rb, 100))

    # C: trail + flow-fade
    rc, reasonc, daysc = run_lot(post, entry, flow_exit=True)
    results["C"].append((reasonc, rc, 100))

    # D: C + dip-add at 2nd day close
    lots = [(rc, 100, reasonc)]
    add_note = ""
    if len(post) >= 3:
        d2 = post.iloc[1]
        if d2["close"] < entry and pd.notna(d2["sh_n3"]) and d2["sh_n3"] >= 0.20:
            r2, reason2, _ = run_lot(post, d2["close"], flow_exit=True, start_i=2)
            lots.append((r2, 100, reason2))
            add_note = f" +add@{d2['close']:.2f}->{r2:+.0%}"
    pl_d = sum(ret * amt for ret, amt, _ in lots)
    cap_d = sum(amt for _, amt, _ in lots)
    results["D"].append(("mix", pl_d / cap_d, cap_d))

    detail.append({"date": d, "und": t, "fam": r["family"], "entry": entry,
                   "A": ra, "B": rb, "C": rc, "D": pl_d / cap_d, "cap_D": cap_d,
                   "exitB": reasonb, "exitC": reasonc, "note": add_note})

print(f"n = {len(detail)} fires\n")
print(f"{'variant':<44} {'capital':>8} {'P&L':>9} {'return':>8} {'win%':>6} {'best':>8} {'worst':>8}")
print("-" * 100)
labels = {"A": "A) Buy-hold 15d", "B": "B) Trail 15% / SL -10%",
          "C": "C) B + flow-fade exit", "D": "D) C + dip-add 2nd lot"}
for k in ["A","B","C","D"]:
    rows = results[k]
    cap = sum(amt for _, _, amt in rows)
    pl = sum(ret * amt for _, ret, amt in rows)
    rets = [ret for _, ret, _ in rows]
    wins = sum(1 for x in rets if x > 0)
    print(f"{labels[k]:<44} ${cap:>7,.0f} ${pl:>+8.0f} {pl/cap:>+7.1%} {wins/len(rets):>6.0%} {max(rets):>+7.1%} {min(rets):>+7.1%}")

print(f"\nPer-fire detail (D = final template):")
print(f"{'date':<12} {'und':<7} {'fam':<6} {'entry':>8} {'A_hold':>8} {'B_trail':>8} {'C_+flow':>8} {'D_final':>8}  exits")
for r in sorted(detail, key=lambda x: x["date"]):
    print(f"{r['date'].strftime('%Y-%m-%d'):<12} {r['und']:<7} {r['fam']:<6} {r['entry']:>8.2f} "
          f"{r['A']:>+7.1%} {r['B']:>+7.1%} {r['C']:>+7.1%} {r['D']:>+7.1%}  B:{r['exitB']}/C:{r['exitC']}{r['note']}")
