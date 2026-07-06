"""Strike/expiry concentration: do real pre-pop windows show concentrated,
persistent call buying at specific strikes vs F2-lookalike controls that didn't pop?

Metrics per (underlying, 10-day pre-window):
  - top1_share: top contract's share of total call volume pooled over window
  - top3_share: top 3 contracts' share
  - hhi: Herfindahl index of call volume across contracts
  - persist_days: how many days the #1 pooled contract was traded
  - n_contracts: distinct call contracts traded
  - otm_share: share of call volume at strikes > 1.05x spot
  - near_expiry_share: share of call volume in expiries <= 45 days out
"""
import gzip
import re
import numpy as np
import pandas as pd
from pathlib import Path
from collections import defaultdict

BASE = Path("/Users/christian/dev/gold_forecast/opt_dayaggs")
STK  = Path("/Users/christian/dev/gold_forecast/grouped_stocks")

# ---------- load stock closes ----------
closes_list = []
for f in sorted(STK.glob("2026-*.csv")):
    df = pd.read_csv(f)[["T","c","v","vw"]].rename(columns={"T":"und","c":"close"})
    df["date"] = pd.to_datetime(f.stem)
    df["stock_dv"] = df["v"] * df["vw"]
    closes_list.append(df)
closes = pd.concat(closes_list, ignore_index=True).sort_values(["und","date"]).reset_index(drop=True)
closes["prev_close"] = closes.groupby("und")["close"].shift(1)
closes["dod"] = closes["close"]/closes["prev_close"] - 1

feat = pd.read_csv(BASE / "trades_features.csv")
feat["date"] = pd.to_datetime(feat["date"])
optionable = set(feat["und"].unique())

trading_days = sorted(closes["date"].unique())
td_index = {d: i for i, d in enumerate(trading_days)}

# ---------- CASES: 50%+ pops ----------
pops = closes[
    (closes["dod"] >= 0.50) & (closes["close"] > 3) & (closes["prev_close"] > 3) &
    (closes["stock_dv"] > 10_000_000) & (closes["und"].isin(optionable))
].copy().sort_values("date")
# need at least 10 trading days of history before the pop
pops = pops[pops["date"].map(lambda d: td_index.get(d, 0)) >= 12]
cases = [(r["und"], r["date"], r["dod"]) for _, r in pops.iterrows()]
print(f"Cases (50%+ pops with >=12d history): {len(cases)}")

# ---------- CONTROLS: F2-lookalike days that did NOT pop ----------
feat_s = feat.sort_values(["und","date"]).reset_index(drop=True)
feat_s = feat_s.merge(closes[["und","date","close"]], on=["und","date"], how="left")
g = feat_s.groupby("und", group_keys=False)
feat_s["drift_10d"] = g["close"].apply(lambda s: s / s.shift(10) - 1)
feat_s["ch_days_10d"] = g["pc_ratio"].apply(lambda s: s.le(0.5).rolling(10, min_periods=5).sum())
feat_s["cum_notional_10d"] = g["notional_M"].apply(lambda s: s.rolling(10, min_periods=5).sum())

def fwd_max_dod(gr, days=15):
    c = gr["close"].values; n = len(c); out = np.full(n, np.nan)
    for i in range(n - 1):
        end = min(i + days + 1, n)
        win = c[i+1:end]; prev = c[i:end-1]
        with np.errstate(invalid="ignore", divide="ignore"):
            dod = np.where(prev > 0, win/prev - 1, 0)
        if len(dod): out[i] = np.nanmax(dod)
    return pd.Series(out, index=gr.index)
feat_s["fwd_max"] = feat_s.groupby("und", group_keys=False).apply(fwd_max_dod)

ctrl_pool = feat_s[
    feat_s["drift_10d"].between(0.05, 0.30) &
    (feat_s["ch_days_10d"] >= 6) &
    (feat_s["cum_notional_10d"] >= 1) &
    (feat_s["fwd_max"] < 0.10) &          # did NOT pop
    (feat_s["close"] > 3) &
    (feat_s["date"].map(lambda d: td_index.get(d, 0)) >= 12)
].copy()
# one control day per ticker (the first qualifying), sample up to 60
ctrl_pool = ctrl_pool.sort_values("date").groupby("und").head(1)
rng = np.random.default_rng(42)
if len(ctrl_pool) > 60:
    ctrl_pool = ctrl_pool.sample(60, random_state=42)
controls = [(r["und"], r["date"], np.nan) for _, r in ctrl_pool.iterrows()]
print(f"Controls (F2-lookalike, no pop within 15d): {len(controls)}")

# ---------- gather needed (und, date) windows ----------
def pre_window(d, n=10):
    i = td_index[d]
    return trading_days[max(0, i-n):i]

need = {}  # date -> set of underlyings
all_samples = [("CASE",) + c for c in cases] + [("CTRL",) + c for c in controls]
for kind, und, d, dod in all_samples:
    for pd_ in pre_window(d):
        need.setdefault(pd_, set()).add(und)

# ---------- parse per-contract files ----------
pat = re.compile(r"^O:([A-Z]+[0-9]*)(\d{6})([CP])(\d{8})$")
# store: (und, date) -> list of (contract, expiry, cp, strike, volume)
rows = defaultdict(list)
for d, unds in sorted(need.items()):
    f = BASE / f"{pd.Timestamp(d).strftime('%Y-%m-%d')}.csv.gz"
    if not f.exists(): continue
    with gzip.open(f, "rt") as fh:
        header = fh.readline()
        for line in fh:
            parts = line.split(",")
            m = pat.match(parts[0])
            if not m: continue
            root = m.group(1)
            if root not in unds: continue
            expiry = "20" + m.group(2)
            cp = m.group(3)
            strike = int(m.group(4)) / 1000.0
            vol = int(parts[1])
            rows[(root, d)].append((parts[0], expiry, cp, strike, vol))
print(f"Parsed contract data for {len(rows)} (und, day) pairs")

# ---------- compute concentration metrics per sample ----------
def metrics(und, d):
    win = pre_window(d)
    # spot price at window end
    spot_rows = closes[(closes["und"]==und) & (closes["date"].isin(win))]
    if spot_rows.empty: return None
    spot = spot_rows["close"].iloc[-1]

    pooled = defaultdict(int)     # contract -> vol
    daily_present = defaultdict(set)  # contract -> set of days traded
    call_vol_total = 0
    otm_vol = 0; near_exp_vol = 0
    for day in win:
        for (c, expiry, cp, strike, vol) in rows.get((und, day), []):
            if cp != "C" or vol <= 0: continue
            pooled[c] += vol
            daily_present[c].add(day)
            call_vol_total += vol
            if strike > spot * 1.05: otm_vol += vol
            exp_dt = pd.Timestamp(expiry[:4] + "-" + expiry[4:6] + "-" + expiry[6:])
            if (exp_dt - d).days <= 45: near_exp_vol += vol
    if call_vol_total < 200 or len(pooled) < 3:
        return None
    vols = np.array(sorted(pooled.values(), reverse=True), dtype=float)
    shares = vols / vols.sum()
    top1 = shares[0]
    top3 = shares[:3].sum()
    hhi = (shares**2).sum()
    top_contract = max(pooled, key=pooled.get)
    persist = len(daily_present[top_contract])
    return {
        "top1_share": top1, "top3_share": top3, "hhi": hhi,
        "persist_days": persist, "n_contracts": len(pooled),
        "otm_share": otm_vol / call_vol_total,
        "near_exp_share": near_exp_vol / call_vol_total,
        "call_vol_10d": call_vol_total,
    }

out = []
for kind, und, d, dod in all_samples:
    m = metrics(und, d)
    if m is None: continue
    out.append({"kind": kind, "und": und, "date": d, "pop": dod, **m})
res = pd.DataFrame(out)
print(f"\nComputed metrics: {len(res[res['kind']=='CASE'])} cases, {len(res[res['kind']=='CTRL'])} controls\n")

# ---------- compare ----------
C = res[res["kind"]=="CASE"]; T = res[res["kind"]=="CTRL"]
print(f"{'metric':<18} {'case_med':>9} {'ctrl_med':>9} {'ratio':>7}   {'case_p25-p75':>17}   {'ctrl_p25-p75':>17}")
print("-"*90)
for m in ["top1_share","top3_share","hhi","persist_days","n_contracts","otm_share","near_exp_share","call_vol_10d"]:
    cm, tm = C[m].median(), T[m].median()
    ratio = cm/tm if tm else np.nan
    print(f"{m:<18} {cm:>9.3f} {tm:>9.3f} {ratio:>7.2f}   "
          f"{C[m].quantile(.25):>7.3f}-{C[m].quantile(.75):<8.3f}   {T[m].quantile(.25):>7.3f}-{T[m].quantile(.75):<8.3f}")

# per-case detail
print(f"\nCASE detail (sorted by top3_share):")
print(f"{'date':<12} {'und':<7} {'pop%':>6} {'top1':>6} {'top3':>6} {'hhi':>6} {'persist':>8} {'nctr':>5} {'otm%':>6} {'nearexp%':>9}")
for _, r in C.sort_values("top3_share", ascending=False).iterrows():
    print(f"{pd.Timestamp(r['date']).strftime('%Y-%m-%d'):<12} {r['und']:<7} {r['pop']*100:>+5.0f}% "
          f"{r['top1_share']:>6.2f} {r['top3_share']:>6.2f} {r['hhi']:>6.3f} {int(r['persist_days']):>8} "
          f"{int(r['n_contracts']):>5} {r['otm_share']*100:>5.0f}% {r['near_exp_share']*100:>8.0f}%")

res.to_csv(BASE / "strike_concentration_results.csv", index=False)
print("\nSaved strike_concentration_results.csv")
