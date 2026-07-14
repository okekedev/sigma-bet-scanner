#!/usr/bin/env python3
"""Dose-response: does the EXTREME tail lift, even though the mild population
(505 events, ~1.3x) does not? Two axes, matching what the cases actually were:
  SIZE  = clip-day call notional / that ticker's own trailing-20d median
  DEPTH = sigma_dist of the top strike = ln(K/S)/(sig20*sqrt(dte*252/365))
          AND raw K/S (how far OTM in plain terms)
Split-decontaminated. Reports n at every stratum (tail n gets small -- shown)."""
import glob
import os
import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
tape = pd.concat([pd.read_csv(f) for f in sorted(glob.glob(os.path.join(BASE,"tape_scan_days","*.csv")))],
                 ignore_index=True)
tape["date"] = pd.to_datetime(tape["date"])
closes = pd.read_csv(os.path.join(BASE,"closes.csv")); closes["date"] = pd.to_datetime(closes["date"])
splits = pd.read_csv(os.path.join(BASE,"splits.csv")); splits["execution_date"] = pd.to_datetime(splits["execution_date"])

# realized vol (sig20) per (und,date) -- same definition as the scanner
c = closes.sort_values(["und","date"]).copy()
c["lr"] = np.log(c["close"]/c.groupby("und")["close"].shift(1))
c["sig20"] = c.groupby("und",group_keys=False)["lr"].apply(lambda s: s.rolling(20,min_periods=10).std())
g = c.groupby("und")["close"]
c["fwd_max10"] = g.transform(lambda s: s.iloc[::-1].rolling(10,min_periods=3).max().iloc[::-1].shift(-1))/c["close"]-1
c["fwd_max15"] = g.transform(lambda s: s.iloc[::-1].rolling(15,min_periods=3).max().iloc[::-1].shift(-1))/c["close"]-1

# per-ticker trailing median call notional (own baseline) from the tape itself
t = tape.sort_values(["und","date"]).copy()
t["base_med"] = t.groupby("und")["call_n"].transform(lambda s: s.rolling(20,min_periods=8).median().shift(1))
t = t.merge(c[["und","date","sig20","fwd_max10","fwd_max15"]], on=["und","date"], how="left")
t["size_x"] = t["call_n"]/t["base_med"].clip(lower=200)
t["ks"] = t["top_strike"]/t["spot"]
t["sig_dist"] = np.log(t["ks"])/(t["sig20"]*np.sqrt(t["dte_min"].clip(lower=1)*252/365))

# universe: an actual clip day (concentrated single OTM strike, real money, quiet puts)
clip = ((t["call_n"]>=5000) & (t["top_n"]/t["call_n"]>=0.60) & t["ks"].between(1.05,6.0)
        & (t["dte_min"]>=7) & (t["put_n"]<=0.25*t["call_n"]) & t["base_med"].notna())
u = t[clip].dropna(subset=["fwd_max10","size_x","sig_dist"]).copy()
# drop split-contaminated (und-month with a split)
splmo = set(zip(splits["ticker"], splits["execution_date"].dt.strftime("%Y-%m")))
u = u[~u.apply(lambda r:(r["und"], r["date"].strftime("%Y-%m")) in splmo, axis=1)]

# control = all optionable $1-25 ticker-days with any call flow, split-filtered
ctrl = t.dropna(subset=["fwd_max10"]).copy()
ctrl = ctrl[~ctrl.apply(lambda r:(r["und"], r["date"].strftime("%Y-%m")) in splmo, axis=1)]
b20 = (ctrl["fwd_max10"]>=0.20).mean(); b40 = (ctrl["fwd_max10"]>=0.40).mean()
print(f"CONTROL n={len(ctrl):,}  base +20%/10d {b20:.1%}  +40% {b40:.1%}\n")

def line(tag, s):
    if len(s)==0: print(f"  {tag:<26} n=0"); return
    h20=(s["fwd_max10"]>=0.20).mean(); h40=(s["fwd_max10"]>=0.40).mean()
    print(f"  {tag:<26} n={len(s):<4} +20%: {h20:5.1%} ({h20/b20:4.1f}x)  +40%: {h40:5.1%} ({h40/b40:4.1f}x)  mean {s['fwd_max10'].mean():+6.1%}  med {s['fwd_max10'].median():+.1%}")

print(f"ALL clip days (single OTM strike, >=$5k, quiet puts): n={len(u)}")
line("all clip days", u)

print("\nSIZE axis  (call$ / own 20d median):")
for lo,hi in [(2,10),(10,50),(50,200),(200,1000),(1000,1e9)]:
    line(f"{lo}-{hi:g}x baseline", u[(u['size_x']>=lo)&(u['size_x']<hi)])

print("\nDEPTH axis  (sigma-distance of the top strike):")
for lo,hi in [(-1,1),(1,2),(2,3),(3,5),(5,1e9)]:
    line(f"sig_dist {lo}..{hi:g}", u[(u['sig_dist']>=lo)&(u['sig_dist']<hi)])

print("\nRAW moneyness axis  (K/S of the top strike):")
for lo,hi in [(1.05,1.3),(1.3,1.6),(1.6,2.2),(2.2,3.0),(3.0,6.0)]:
    line(f"K/S {lo}-{hi}", u[(u['ks']>=lo)&(u['ks']<hi)])

print("\nCORNER: extreme size AND far OTM together:")
line("size>=100x & sig_dist>=2", u[(u['size_x']>=100)&(u['sig_dist']>=2)])
line("size>=100x & sig_dist>=3", u[(u['size_x']>=100)&(u['sig_dist']>=3)])
line("size>=50x & K/S>=1.5", u[(u['size_x']>=50)&(u['ks']>=1.5)])
line("size>=200x & K/S>=1.5", u[(u['size_x']>=200)&(u['ks']>=1.5)])

# where do the known cases land on these axes?
print("\nwhere the case tickers sit (their clip days in u):")
for tk in ["WRAP","AARD","EVC","BWEN","VERU"]:
    s = u[u["und"]==tk]
    for _,r in s.iterrows():
        print(f"  {tk:<5} {r['date'].strftime('%m-%d')}  size {r['size_x']:6.0f}x  K/S {r['ks']:.2f}  sig_dist {r['sig_dist']:+.2f}  fwd10 {r['fwd_max10']:+.0%}")
