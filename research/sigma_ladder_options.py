"""Piggyback test: informed money buys 3sigma+ strikes. What if WE buy the same
expiry window but at lower sigma-distance strikes (higher probability)?

Ladder: target sigma-distance 0.5, 1.0, 1.5, 2.0, 3.0 — DTE 25-60, volume>=10.
Spot is inferred FROM THE CHAIN (min over calls of K + premium) so reverse-split
frame mismatches with the stock file don't distort strike selection.
Entry: fire-day close. Exit: last traded close within 15 trading days.
"""
import numpy as np
import pandas as pd
from pathlib import Path

BASE = Path("/Users/christian/dev/gold_forecast/opt_dayaggs")
STK  = Path("/Users/christian/dev/gold_forecast/grouped_stocks")

# ---- rebuild the 27 fires (same as before) ----
sig = pd.read_csv(BASE / "sigma_bets_daily.csv"); sig["date"] = pd.to_datetime(sig["date"])
closes = pd.concat([
    pd.read_csv(f)[["T","c"]].rename(columns={"T":"und","c":"close"}).assign(date=pd.to_datetime(f.stem))
    for f in sorted(STK.glob("2026-*.csv"))
]).sort_values(["und","date"]).reset_index(drop=True)
closes["logret"] = np.log(closes["close"] / closes.groupby("und")["close"].shift(1))
closes["sig20"] = closes.groupby("und", group_keys=False)["logret"].apply(lambda s: s.rolling(20, min_periods=10).std())

feat = pd.read_csv(BASE / "trades_features.csv"); feat["date"] = pd.to_datetime(feat["date"])
feat = feat.sort_values(["und","date"]).reset_index(drop=True)
feat = feat.merge(sig, on=["und","date"], how="left").merge(closes[["und","date","close","sig20"]], on=["und","date"], how="left")
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

trading_days = sorted(feat["date"].unique())
td_index = {d: i for i, d in enumerate(trading_days)}

need_days = {}
for _, r in F.iterrows():
    i = td_index[r["date"]]
    for d in trading_days[i:i+16]:
        need_days.setdefault(pd.Timestamp(d), set()).add(r["und"])

pat = r"^O:([A-Z]+[0-9]*?)(\d{6})([CP])(\d{8})$"
contract_px = {}
fire_chain = {}
for d, unds in sorted(need_days.items()):
    f = BASE / f"{d.strftime('%Y-%m-%d')}.csv.gz"
    if not f.exists(): continue
    df = pd.read_csv(f, usecols=["ticker","volume","close"])
    ext = df["ticker"].str.extract(pat)
    ext.columns = ["root","exp","cp","strike"]
    df = pd.concat([df, ext], axis=1).dropna(subset=["root"])
    df = df[df["root"].isin(unds) & (df["cp"] == "C")].copy()
    if df.empty: continue
    df["strike"] = df["strike"].astype(float) / 1000.0
    for _, row in df.iterrows():
        contract_px[(row["ticker"], d)] = row["close"]
    for und in df["root"].unique():
        fire_chain[(und, d)] = df[df["root"] == und].copy()

def exit_price(contract, fire_d):
    i = td_index[fire_d]
    last_px = None
    for d in trading_days[i+1:i+16]:
        px = contract_px.get((contract, pd.Timestamp(d)))
        if px is not None and px > 0: last_px = px
    return last_px

TARGETS = [0.5, 1.0, 1.5, 2.0, 3.0]
results = {t: [] for t in TARGETS}
rows_out = []
for _, r in F.iterrows():
    und = r["und"]; d = r["date"]; sig20 = r["sig20"]
    key = (und, d)
    if key not in fire_chain or pd.isna(sig20) or sig20 <= 0: continue
    ch = fire_chain[key].copy()
    exp_dt = pd.to_datetime("20" + ch["exp"], format="%Y%m%d", errors="coerce")
    ch["dte"] = (exp_dt - d).dt.days
    ch = ch[(ch["dte"] >= 25) & (ch["dte"] <= 60) & (ch["close"] > 0.01)]
    if ch.empty: continue
    # implied spot from the chain: min(K + premium) over all liquid calls
    liq = ch[ch["volume"] >= 1]
    if liq.empty: continue
    implied_spot = (liq["strike"] + liq["close"]).min()
    ch["sigma_dist"] = np.log(ch["strike"]/implied_spot) / (sig20 * np.sqrt(ch["dte"] * 252/365))
    ch = ch[ch["volume"] >= 10]
    if ch.empty: continue
    row_o = {"date": d, "und": und, "ispot": implied_spot}
    for t in TARGETS:
        ch["dist"] = (ch["sigma_dist"] - t).abs()
        best = ch.sort_values("dist").iloc[0]
        if best["dist"] > 0.75:   # no contract anywhere near this rung
            row_o[t] = None; continue
        xp = exit_price(best["ticker"], d)
        if xp is None:
            row_o[t] = None; continue
        ret = xp / best["close"] - 1
        results[t].append(ret)
        row_o[t] = (best["strike"], best["sigma_dist"], best["close"], xp, ret)
    rows_out.append(row_o)

print(f"{'sigma rung':>10} {'n':>3} {'mean':>8} {'median':>8} {'win%':>6} {'total/$100lots':>15} {'best':>8} {'worst':>8}")
print("-"*80)
for t in TARGETS:
    rets = np.array(results[t])
    if len(rets) == 0: continue
    print(f"{t:>10} {len(rets):>3} {rets.mean():>+8.1%} {np.median(rets):>+8.1%} {(rets>0).mean():>6.0%} "
          f"${rets.sum()*100:>+7.0f}/{len(rets)*100} {rets.max():>+8.1%} {rets.min():>+8.1%}")

print("\nPer-fire (sigma rung: strike @ sigma / entry->exit / ret):")
for ro in rows_out:
    parts = []
    for t in TARGETS:
        v = ro.get(t)
        if v is None: parts.append(f"{t}σ:—")
        else: parts.append(f"{t}σ:K{v[0]:g}@{v[1]:.1f} {v[2]:.2f}->{v[3]:.2f}={v[4]:+.0%}")
    print(f"  {ro['date'].strftime('%m-%d')} {ro['und']:<7} " + " | ".join(parts))
