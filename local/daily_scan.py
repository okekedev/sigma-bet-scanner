#!/usr/bin/env python3
"""V5 SIGMA-BET DAILY SCAN
=========================
Run each evening after market close (once the day's options flat file is available).

    python3 daily_scan.py                 # scan latest day on disk
    python3 daily_scan.py 2026-07-06      # scan a specific date
    python3 daily_scan.py --fetch         # try to download today's files first

What it does:
  1. (--fetch) downloads the day's options day-aggs flat file + grouped stock bars
  2. computes sigma-distance jump-bet features for the day
  3. applies the v5 fire conditions over the trailing 10 trading days
  4. family-dedups against fires_log.csv (21-day window)
  5. reports: NEW FIRES / re-confirmations / flow status for open positions /
     dip-add signals / day-12 exits due / -15% close stops

State files (same directory):
  fires_log.csv      — every fire ever taken (source of dedup + open positions)
  sigma_bets_daily.csv — rolling per-(und,day) jump-bet aggregates (appended)

Config below: RCLONE_REMOTE for flat files, MASSIVE_API_KEY env var for stock bars.
"""
import gzip
import os
import subprocess
import sys
import json
import urllib.request
import numpy as np
import pandas as pd
from pathlib import Path

BASE = Path(__file__).parent
STK  = BASE.parent / "grouped_stocks"

# ---------------- config ----------------
RCLONE_REMOTE = "polyfiles"   # rclone remote name for flat files
FLATFILE_PATH = "flatfiles/us_options_opra/day_aggs_v1"
MASSIVE_GROUPED = "https://api.massive.com/v2/aggs/grouped/locale/us/market/stocks/{date}?adjusted=true&apiKey={key}"

# v5 fire conditions
SH_N3_MIN     = 0.20      # 3-sigma notional share of call notional (10d)
N3_NOTIONAL   = 250_000   # $ behind the 3-sigma bets (10d)
DRIFT_LO, DRIFT_HI = 0.05, 0.30
MIN_CLOSE     = 3.0
MIN_OPT_VOL   = 100       # median daily option volume (10d)
MIN_CALL_NOT  = 100_000   # call notional (10d)
DEDUP_DAYS    = 21
HOLD_DAYS     = 12        # sell at close of 12th trading day
STOP_CLOSE    = -0.15     # close-basis stop

FAMILY = {"NVDL":"NVDA","SNXX":"SNDK","SNDU":"SNDK","SNDG":"SNDK","MULL":"MU",
          "WDCX":"WDC","DLLL":"DELL","INTW":"INTC","MVLL":"MRVL","MRVU":"MRVL"}
fam = lambda t: FAMILY.get(t, t)

# ---------------- fetch (optional) ----------------
def fetch(date_str):
    y, m, _ = date_str.split("-")
    dst = BASE / f"{date_str}.csv.gz"
    if not dst.exists():
        src = f"{RCLONE_REMOTE}:{FLATFILE_PATH}/{y}/{m}/{date_str}.csv.gz"
        print(f"rclone copy {src} ...")
        r = subprocess.run(["rclone","copy",src,str(BASE)], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  rclone failed: {r.stderr.strip()[:300]}"); return False
    stk_dst = STK / f"{date_str}.csv"
    if not stk_dst.exists():
        key = os.environ.get("MASSIVE_API_KEY","")
        if not key:
            print("  MASSIVE_API_KEY not set; skipping stock bars fetch"); return dst.exists()
        url = MASSIVE_GROUPED.format(date=date_str, key=key)
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                data = json.loads(resp.read())
            rows = data.get("results") or []
            if rows:
                pd.DataFrame(rows).to_csv(stk_dst, index=False)
                print(f"  saved {stk_dst.name} ({len(rows)} tickers)")
        except Exception as e:
            print(f"  stock bars fetch failed: {e}")
    return dst.exists()

# ---------------- incremental sigma-bet build ----------------
def build_sigma_day(date_str, closes):
    """Compute per-(und) jump-bet aggregates for one day. Returns DataFrame."""
    f = BASE / f"{date_str}.csv.gz"
    if not f.exists(): return None
    day = pd.Timestamp(date_str)
    day_rows = closes[closes["date"] == day]
    if day_rows.empty: return None
    day_spot = day_rows.set_index("und")["close"]
    day_sig  = day_rows.set_index("und")["sig20"]

    df = pd.read_csv(f, usecols=["ticker","volume","close"]).rename(columns={"close":"opt_close"})
    ext = df["ticker"].str.extract(r"^O:([A-Z]+[0-9]*?)(\d{6})([CP])(\d{8})$")
    ext.columns = ["root","exp","cp","strike"]
    df = pd.concat([df, ext], axis=1).dropna(subset=["root"])
    df = df[df["cp"] == "C"].copy()
    df["strike"] = df["strike"].astype(float) / 1000.0
    df["spot"] = df["root"].map(day_spot); df["sig"] = df["root"].map(day_sig)
    df = df.dropna(subset=["spot","sig"])
    df = df[(df["spot"] > 0) & (df["sig"] > 0)]
    exp_dt = pd.to_datetime("20" + df["exp"], format="%Y%m%d", errors="coerce")
    df["dte"] = (exp_dt - day).dt.days
    df = df[df["dte"] > 0]
    df["sigma_dist"] = np.log(df["strike"]/df["spot"]) / (df["sig"] * np.sqrt(df["dte"] * 252/365))
    df["notional"] = df["volume"] * df["opt_close"] * 100
    near = df["dte"] <= 60
    df["n3"] = df["notional"].where((df["sigma_dist"] >= 3) & near, 0)
    g = df.groupby("root").agg(call_notional=("notional","sum"), n3_notional=("n3","sum")).reset_index()
    g = g.rename(columns={"root":"und"}); g["date"] = date_str
    return g

# ---------------- main ----------------
def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    do_fetch = "--fetch" in sys.argv

    # stock closes + realized vol
    closes = pd.concat([
        pd.read_csv(f)[["T","c"]].rename(columns={"T":"und","c":"close"}).assign(date=pd.to_datetime(f.stem))
        for f in sorted(STK.glob("2026-*.csv"))
    ]).sort_values(["und","date"]).reset_index(drop=True)
    closes["logret"] = np.log(closes["close"] / closes.groupby("und")["close"].shift(1))
    closes["sig20"] = closes.groupby("und", group_keys=False)["logret"].apply(
        lambda s: s.rolling(20, min_periods=10).std())

    target = args[0] if args else max(f.stem.replace(".csv","") for f in BASE.glob("2026-*.csv.gz"))
    if do_fetch: fetch(target)

    # sigma-bet table: load, append missing days
    sb_path = BASE / "sigma_bets_daily.csv"
    sb = pd.read_csv(sb_path) if sb_path.exists() else pd.DataFrame(columns=["und","date","call_notional","n3_notional"])
    built_marker = BASE / "built_days.txt"
    built = set(built_marker.read_text().split()) if built_marker.exists() else set()
    have = set(sb["date"].astype(str)) | built
    disk_days = sorted(f.stem.replace(".csv","") for f in BASE.glob("2026-*.csv.gz"))
    for d in disk_days:
        if d in have or d > target: continue
        day_df = build_sigma_day(d, closes)
        if day_df is not None and len(day_df):
            sb = pd.concat([sb, day_df[["und","date","call_notional","n3_notional"]]], ignore_index=True)
            print(f"built sigma-bets for {d}")
            built.add(d)   # only mark done on success so missing inputs retry next run
    built_marker.write_text("\n".join(sorted(built)))
    sb.to_csv(sb_path, index=False)
    sb["date"] = pd.to_datetime(sb["date"])

    # need vol_total 10d median: from trades_features if available, else approximate with call notional filter only
    tf_path = BASE / "trades_features.csv"
    feat = sb.merge(closes[["und","date","close"]], on=["und","date"], how="left")
    if tf_path.exists():
        tf = pd.read_csv(tf_path)[["und","date","vol_total"]]
        tf["date"] = pd.to_datetime(tf["date"])
        feat = feat.merge(tf, on=["und","date"], how="left")
    else:
        feat["vol_total"] = np.nan

    feat = feat.sort_values(["und","date"]).reset_index(drop=True)
    g = feat.groupby("und", group_keys=False)
    feat["drift_10d"] = g["close"].apply(lambda s: s / s.shift(10) - 1)
    feat["vol_med_10d"] = g["vol_total"].apply(lambda s: s.rolling(10, min_periods=5).median())
    feat["call_notional_10d"] = g["call_notional"].apply(lambda s: s.rolling(10, min_periods=5).sum())
    feat["n3_notional_10d"] = g["n3_notional"].apply(lambda s: s.rolling(10, min_periods=5).sum())
    feat["sh_n3"] = feat["n3_notional_10d"] / feat["call_notional_10d"].replace(0, np.nan)

    day = pd.Timestamp(target)
    today = feat[feat["date"] == day].dropna(subset=["sh_n3","close","drift_10d"])
    today = today[(today["close"] > MIN_CLOSE) & (today["call_notional_10d"] >= MIN_CALL_NOT)]
    if today["vol_med_10d"].notna().any():
        today = today[today["vol_med_10d"].fillna(MIN_OPT_VOL) >= MIN_OPT_VOL]
    fires_today = today[(today["sh_n3"] >= SH_N3_MIN) & (today["n3_notional_10d"] >= N3_NOTIONAL) &
                        today["drift_10d"].between(DRIFT_LO, DRIFT_HI)].copy()

    # fires log / dedup
    log_path = BASE / "fires_log.csv"
    log = pd.read_csv(log_path) if log_path.exists() else pd.DataFrame(columns=["date","und","family","entry","status"])
    if len(log): log["date"] = pd.to_datetime(log["date"])

    trading_days = sorted(feat["date"].unique())
    td_index = {d: i for i, d in enumerate(trading_days)}

    print(f"\n{'='*90}\n  V5 SIGMA-BET SCAN — {target}\n{'='*90}")

    # 1) new fires vs re-confirmations
    new_fires, reconfirms = [], []
    for _, r in fires_today.iterrows():
        family = fam(r["und"])
        prior = log[(log["family"] == family) & (log["date"] > day - pd.Timedelta(days=DEDUP_DAYS))]
        (reconfirms if len(prior) else new_fires).append(r)

    print(f"\nSHADOW v5.0 FIRES ({len(new_fires)}):  [logged only — no action; A/B vs live v5.1 rule]")
    for r in new_fires:
        print(f"  👻 {r['und']:<7} (family {fam(r['und'])})  close=${r['close']:.2f}  "
              f"sh_n3={r['sh_n3']:.0%}  n3_10d=${r['n3_notional_10d']/1e3:,.0f}k  drift={r['drift_10d']:+.1%}")
        log = pd.concat([log, pd.DataFrame([{"date": day, "und": r["und"], "family": fam(r["und"]),
                                             "entry": r["close"], "status": "open",
                                             "mode": "shadow", "rule": "v5.0 (10d window)"}])], ignore_index=True)
    if not new_fires: print("  none")

    print(f"\nRE-CONFIRMATIONS ({len(reconfirms)}):  [flow still loaded on active families]")
    for r in reconfirms:
        print(f"  ↻  {r['und']:<7} sh_n3={r['sh_n3']:.0%}  n3_10d=${r['n3_notional_10d']/1e3:,.0f}k")
    if not reconfirms: print("  none")

    # 2) open positions: flow status, dip-add, exit due, stop
    open_pos = log[log["status"] == "open"] if len(log) else log
    if len(open_pos):
        print(f"\nOPEN POSITIONS ({len(open_pos)}):")
        for i, p in open_pos.iterrows():
            t = p["und"]; e = p["entry"]
            row = feat[(feat["und"] == t) & (feat["date"] == day)]
            if row.empty:
                print(f"  {t:<7} no data today"); continue
            row = row.iloc[0]
            ret = row["close"]/e - 1
            di = td_index.get(day, 0) - td_index.get(pd.Timestamp(p["date"]), 0)
            flow_ok = pd.notna(row["sh_n3"]) and row["sh_n3"] >= SH_N3_MIN
            flags = []
            if di >= HOLD_DAYS: flags.append("⏰ DAY-12 EXIT DUE — SELL AT CLOSE")
            if ret <= STOP_CLOSE: flags.append("🛑 CLOSE STOP -15% — SELL")
            if di == 2 and ret < 0 and flow_ok: flags.append("➕ DIP-ADD signal (down + flow persists)")
            if not flow_ok: flags.append("⚠️ flow faded")
            mode = p.get("mode", "paper")
            print(f"  [{mode}] {t:<7} day {di:>2}/{HOLD_DAYS}  entry=${e:.2f}  now=${row['close']:.2f} ({ret:+.1%})  "
                  f"flow={'✓' if flow_ok else '✗'}  {' '.join(flags)}")
            if di >= HOLD_DAYS or ret <= STOP_CLOSE:
                log.loc[i, "status"] = "closed"
                log.loc[i, "exit_date"] = day.strftime("%Y-%m-%d")
                log.loc[i, "exit_price"] = row["close"]
                log.loc[i, "exit_ret"] = ret
                log.loc[i, "exit_reason"] = "stop -15%" if ret <= STOP_CLOSE else "day-12 close"
    log.to_csv(log_path, index=False)
    print(f"\nlog: {log_path}")

if __name__ == "__main__":
    main()
