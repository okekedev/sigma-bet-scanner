"""Strike-relativity features per (underlying, day):
  - vw_moneyness: volume-weighted mean call moneyness (strike/spot - 1)
  - share_otm10/20/30: call volume share at strikes >10%/20%/30% above spot
  - jump_bet_vol: volume at strikes >=15% OTM with expiry <= 45 days ("jump bets")
"""
import numpy as np
import pandas as pd
from pathlib import Path

BASE = Path("/Users/christian/dev/gold_forecast/opt_dayaggs")
STK  = Path("/Users/christian/dev/gold_forecast/grouped_stocks")

closes_list = []
for f in sorted(STK.glob("2026-*.csv")):
    df = pd.read_csv(f)[["T","c"]].rename(columns={"T":"und","c":"close"})
    df["date"] = f.stem
    closes_list.append(df)
closes = pd.concat(closes_list, ignore_index=True)
spot_by_day = {d: g.set_index("und")["close"] for d, g in closes.groupby("date")}

out = []
files = sorted(BASE.glob("2026-*.csv.gz"))
for i, f in enumerate(files):
    day = f.stem.replace(".csv","")
    if day not in spot_by_day: continue
    day_spot = spot_by_day[day]
    df = pd.read_csv(f, usecols=["ticker","volume"])
    ext = df["ticker"].str.extract(r"^O:([A-Z]+[0-9]*?)(\d{6})([CP])(\d{8})$")
    ext.columns = ["root","exp","cp","strike"]
    df = pd.concat([df, ext], axis=1).dropna(subset=["root"])
    df = df[df["cp"] == "C"].copy()
    df["strike"] = df["strike"].astype(float) / 1000.0
    df["spot"] = df["root"].map(day_spot)
    df = df.dropna(subset=["spot"])
    df = df[df["spot"] > 0]
    df["mny"] = df["strike"] / df["spot"] - 1.0
    # expiry days out
    exp_dt = pd.to_datetime("20" + df["exp"], format="%Y%m%d", errors="coerce")
    df["dte"] = (exp_dt - pd.Timestamp(day)).dt.days
    df["vXm"] = df["volume"] * df["mny"]
    df["v_otm10"] = df["volume"].where(df["mny"] > 0.10, 0)
    df["v_otm20"] = df["volume"].where(df["mny"] > 0.20, 0)
    df["v_otm30"] = df["volume"].where(df["mny"] > 0.30, 0)
    df["v_jump"]  = df["volume"].where((df["mny"] >= 0.15) & (df["dte"] <= 45), 0)
    g = df.groupby("root").agg(
        call_vol=("volume","sum"), vXm=("vXm","sum"),
        v_otm10=("v_otm10","sum"), v_otm20=("v_otm20","sum"),
        v_otm30=("v_otm30","sum"), v_jump=("v_jump","sum"),
    ).reset_index().rename(columns={"root":"und"})
    g["date"] = day
    out.append(g)
    if (i+1) % 15 == 0: print(f"  {i+1}/{len(files)}")

res = pd.concat(out, ignore_index=True)
res.to_csv(BASE / "moneyness_daily.csv", index=False)
print(f"Saved moneyness_daily.csv: {len(res):,} rows")
