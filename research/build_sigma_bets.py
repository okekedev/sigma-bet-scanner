"""Sigma-distance jump bets: per (und, day) aggregate of call flow at strikes
N standard deviations (stock's own realized vol) beyond spot, weighted by NOTIONAL.

Per contract:
  sigma_dist = ln(K/S) / (sig_daily * sqrt(dte_trading))   # d2-style distance
  breakeven_move = (K + premium) / S - 1
Aggregates per (und, day):
  call_notional          total call $ traded (vol * close * 100)
  n2_notional            $ in calls with sigma_dist >= 2 and DTE <= 60
  n3_notional            $ in calls with sigma_dist >= 3 and DTE <= 60
  be20_notional          $ in calls with breakeven >= +20% and DTE <= 60
"""
import numpy as np
import pandas as pd
from pathlib import Path

BASE = Path("/Users/christian/dev/gold_forecast/opt_dayaggs")
STK  = Path("/Users/christian/dev/gold_forecast/grouped_stocks")

# closes + trailing 20d realized daily vol
closes_list = []
for f in sorted(STK.glob("2026-*.csv")):
    df = pd.read_csv(f)[["T","c"]].rename(columns={"T":"und","c":"close"})
    df["date"] = pd.to_datetime(f.stem)
    closes_list.append(df)
closes = pd.concat(closes_list, ignore_index=True).sort_values(["und","date"]).reset_index(drop=True)
closes["logret"] = np.log(closes["close"] / closes.groupby("und")["close"].shift(1))
closes["sig20"] = closes.groupby("und", group_keys=False)["logret"].apply(
    lambda s: s.rolling(20, min_periods=10).std())

spot_by_day = {}
sig_by_day = {}
for d, g in closes.groupby("date"):
    key = d.strftime("%Y-%m-%d")
    spot_by_day[key] = g.set_index("und")["close"]
    sig_by_day[key] = g.set_index("und")["sig20"]

out = []
files = sorted(BASE.glob("2026-*.csv.gz"))
for i, f in enumerate(files):
    day = f.stem.replace(".csv","")
    if day not in spot_by_day: continue
    day_spot = spot_by_day[day]; day_sig = sig_by_day[day]
    df = pd.read_csv(f, usecols=["ticker","volume","close"])
    df = df.rename(columns={"close":"opt_close"})
    ext = df["ticker"].str.extract(r"^O:([A-Z]+[0-9]*?)(\d{6})([CP])(\d{8})$")
    ext.columns = ["root","exp","cp","strike"]
    df = pd.concat([df, ext], axis=1).dropna(subset=["root"])
    df = df[df["cp"] == "C"].copy()
    df["strike"] = df["strike"].astype(float) / 1000.0
    df["spot"] = df["root"].map(day_spot)
    df["sig"] = df["root"].map(day_sig)
    df = df.dropna(subset=["spot","sig"])
    df = df[(df["spot"] > 0) & (df["sig"] > 0)]
    exp_dt = pd.to_datetime("20" + df["exp"], format="%Y%m%d", errors="coerce")
    df["dte"] = (exp_dt - pd.Timestamp(day)).dt.days
    df = df[df["dte"] > 0]
    df["dte_trading"] = df["dte"] * (252/365)
    df["sigma_dist"] = np.log(df["strike"]/df["spot"]) / (df["sig"] * np.sqrt(df["dte_trading"]))
    df["notional"] = df["volume"] * df["opt_close"] * 100
    df["breakeven"] = (df["strike"] + df["opt_close"]) / df["spot"] - 1

    near = df["dte"] <= 60
    df["n2"] = df["notional"].where((df["sigma_dist"] >= 2) & near, 0)
    df["n3"] = df["notional"].where((df["sigma_dist"] >= 3) & near, 0)
    df["be20"] = df["notional"].where((df["breakeven"] >= 0.20) & near, 0)
    g = df.groupby("root").agg(
        call_notional=("notional","sum"),
        n2_notional=("n2","sum"), n3_notional=("n3","sum"),
        be20_notional=("be20","sum"),
    ).reset_index().rename(columns={"root":"und"})
    g["date"] = day
    out.append(g)
    if (i+1) % 15 == 0: print(f"  {i+1}/{len(files)}")

res = pd.concat(out, ignore_index=True)
res.to_csv(BASE / "sigma_bets_daily.csv", index=False)
print(f"Saved sigma_bets_daily.csv: {len(res):,} rows")
