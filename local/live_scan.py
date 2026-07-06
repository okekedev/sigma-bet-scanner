#!/usr/bin/env python3
"""V5.1 LIVE SIGMA-BET SCANNER — mini-campaign rule, intraday polling + HTML dashboard.

    python3 live_scan.py --once          # single cycle (test)
    python3 live_scan.py                 # loop every 15 min during market hours
    python3 live_scan.py --interval 5    # custom minutes

Architecture (two tiers):
  EOD tier   : local flat files -> sigma_bets_daily.csv gives per-day spike history
               (run daily_scan.py --fetch each evening to append the new day)
  LIVE tier  : every cycle, poll option-chain snapshots ONLY for the watchlist =
               tickers with >=1 spike day in the last 3 trading days + open positions.
               Compute TODAY's running 3-sigma notional intraday. A second spike day
               within 3 days + drift in band = MINI-CAMPAIGN FIRE -> BUY alert.

Signal definitions:
  spike day      : n3_notional >= $100k AND n3_share >= 20% of call notional (DTE<=60)
  mini-campaign  : >=2 spike days within 3 trading days (incl. today intraday)
  fire           : mini-campaign AND 10d stock drift in [+5%, +30%]
Sell rules shown on dashboard: day-12 close exit, -15% close-basis stop.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.parse
import numpy as np
import pandas as pd
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
STK  = BASE.parent / "grouped_stocks"
API  = "https://api.massive.com"
KEY  = os.environ.get("MASSIVE_API_KEY", "")

SPIKE_NOTIONAL = 100_000
SPIKE_SHARE    = 0.20
FIRE_NOTIONAL  = 250_000     # cumulative over the campaign days
DRIFT_LO, DRIFT_HI = 0.05, 0.30
DTE_MAX        = 60
SIGMA_MIN      = 3.0
HOLD_DAYS      = 12
STOP_CLOSE     = -0.15
DEDUP_DAYS     = 21
LOT            = 100        # $ per paper trade — all sim P&L in $100 lots

FAMILY = {"NVDL":"NVDA","SNXX":"SNDK","SNDU":"SNDK","SNDG":"SNDK","MULL":"MU",
          "WDCX":"WDC","DLLL":"DELL","INTW":"INTC","MVLL":"MRVL","MRVU":"MRVL"}
fam = lambda t: FAMILY.get(t, t)

def get_json(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"_error": str(e)}

# ---------------- baselines from local EOD data ----------------
def load_baselines():
    closes = pd.concat([
        pd.read_csv(f)[["T","c"]].rename(columns={"T":"und","c":"close"}).assign(date=pd.to_datetime(f.stem))
        for f in sorted(STK.glob("2026-*.csv"))
    ]).sort_values(["und","date"]).reset_index(drop=True)
    closes["logret"] = np.log(closes["close"] / closes.groupby("und")["close"].shift(1))
    closes["sig20"] = closes.groupby("und", group_keys=False)["logret"].apply(
        lambda s: s.rolling(20, min_periods=10).std())
    latest = closes.sort_values("date").groupby("und").tail(1).set_index("und")
    drift = closes.sort_values(["und","date"]).groupby("und")["close"].apply(
        lambda s: s.iloc[-1]/s.iloc[-11]-1 if len(s) >= 11 else np.nan)

    sb = pd.read_csv(BASE / "sigma_bets_daily.csv")
    sb["date"] = pd.to_datetime(sb["date"])
    sb["sh_1d"] = sb["n3_notional"] / sb["call_notional"].replace(0, np.nan)
    sb["spike"] = (sb["n3_notional"] >= SPIKE_NOTIONAL) & (sb["sh_1d"] >= SPIKE_SHARE)
    tdays = sorted(sb["date"].unique())
    last3 = tdays[-3:] if len(tdays) >= 3 else tdays
    recent = sb[sb["date"].isin(last3)]
    spike_hist = recent[recent["spike"]].groupby("und").agg(
        spike_days=("date", lambda s: sorted(str(x)[:10] for x in s)),
        n3_recent=("n3_notional","sum")).reset_index()
    return latest, drift, spike_hist, str(pd.Timestamp(tdays[-1]).date()) if tdays else "?"

# ---------------- intraday polling ----------------
def poll_chain(und, spot_fallback, sig20):
    """Fetch today's option chain snapshot; return (n3_notional, call_notional, spot)."""
    url = f"{API}/v3/snapshot/options/{und}?limit=250&apiKey={KEY}"
    n3 = 0.0; total = 0.0; spot = None
    for _ in range(8):  # max pages
        data = get_json(url)
        if "_error" in data or not data.get("results"): break
        for c in data["results"]:
            det = c.get("details", {})
            if det.get("contract_type") != "call": continue
            day = c.get("day", {})
            vol = day.get("volume") or 0
            if vol <= 0: continue
            px = day.get("vwap") or day.get("close") or 0
            if px <= 0: continue
            if spot is None:
                ua = c.get("underlying_asset", {})
                spot = ua.get("price")
            K = det.get("strike_price"); exp = det.get("expiration_date")
            if not K or not exp: continue
            dte = (pd.Timestamp(exp) - pd.Timestamp.now().normalize()).days
            if dte <= 0 or dte > DTE_MAX:
                notional = vol * px * 100; total += notional; continue
            S = spot or spot_fallback
            if not S or S <= 0 or not sig20 or sig20 <= 0: continue
            sd = np.log(K / S) / (sig20 * np.sqrt(dte * 252/365))
            notional = vol * px * 100
            total += notional
            if sd >= SIGMA_MIN: n3 += notional
        nxt = data.get("next_url")
        if not nxt: break
        url = nxt + f"&apiKey={KEY}"
    return n3, total, (spot or spot_fallback)

def poll_stock(und, fallback):
    """Stock plan is options-only (403 on stock trades) — the chain snapshot's
    underlying_asset.price is our live spot; caller passes it as `fallback`."""
    return fallback

# ---------------- dashboard ----------------
def write_dashboard(ts, fires, watch, positions, eod_date, closed, signals=None, shadow_closed=None, shadow_stats=None):
    signals = signals or []; shadow_closed = shadow_closed or []
    shadow_stats = shadow_stats or {"n":0,"pl":0,"wins":0,"open":0}
    n_closed = len(closed)
    wins = sum(1 for c in closed if c["ret"] > 0)
    total_pl = sum(c["ret"] for c in closed) * LOT
    open_pl = sum(p["ret"] for p in positions) * LOT
    winrate = f"{wins}/{n_closed}" if n_closed else "—"

    def kpi(value, label, tone=""):
        return f"<div class='kpi {tone}'><div class='v'>{value}</div><div class='l'>{label}</div></div>"
    kpis = (
        kpi(len(fires), "fires now", "good" if fires else "") +
        kpi(len(watch), "watchlist") +
        kpi(len(positions), "open") +
        kpi(f"{open_pl:+.0f}" if positions else "—", "open P&L $", "good" if open_pl > 0 else ("bad" if open_pl < 0 else "")) +
        kpi(winrate, "paper wins") +
        kpi(f"{total_pl:+.0f}", "closed P&L $", "good" if total_pl > 0 else ("bad" if total_pl < 0 else ""))
    )

    fire_cards = "".join(
        f"<div class='card fire'><div class='t'>🔥 {r['und']}</div><div class='p'>${r['spot']:.2f}</div>"
        f"<div class='m'>{r['sh']:.0%} at 3σ · ${r['n3']/1e3:,.0f}k · drift {r['drift']:+.0%}</div>"
        f"<div class='a'>BUY AT CLOSE</div></div>"
        for r in fires)

    pos_rows = "".join(
        f"<tr class='{r['cls']}'><td><b>{r['und']}</b></td><td>{r['day']}/{HOLD_DAYS}d</td>"
        f"<td class='{'pos' if r['ret']>0 else 'neg'}'>{r['ret']*LOT:+,.0f}</td>"
        f"<td class='{'pos' if r['ret']>0 else 'neg'}'>{r['ret']:+.1%}</td><td>{r['flag']}</td></tr>"
        for r in positions) or "<tr><td colspan=5 class='dim'>none</td></tr>"

    watch_rows = "".join(
        f"<tr><td><b>{r['und']}</b></td><td>{r['spikes']} spike{'s' if r['spikes']!=1 else ''}</td>"
        f"<td>{r['note']}</td></tr>"
        for r in watch) or "<tr><td colspan=3 class='dim'>empty</td></tr>"

    hist_rows = "".join(
        f"<tr><td><b>{c['und']}</b></td><td>{c['fired']}</td><td>{c['exited']}</td>"
        f"<td class='{'pos' if c['ret']>0 else 'neg'}'>{c['ret']*LOT:+,.0f}</td>"
        f"<td class='{'pos' if c['ret']>0 else 'neg'}'>{c['ret']:+.1%}</td>"
        f"<td class='dim'>{c['reason']} · {c['rule']}</td></tr>"
        for c in closed) or "<tr><td colspan=6 class='dim'>none yet</td></tr>"

    html_head = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="60">
<title>Sigma-Bet Scanner</title>
<style>
 :root{{--bg:#0d1117;--card:#161b22;--line:#21262d;--ink:#e6edf3;--ink2:#8b949e;--ink3:#484f58;
       --good:#3fb950;--bad:#f85149;--warn:#e3b341}}
 body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:var(--bg);color:var(--ink);
      margin:0;padding:28px;max-width:760px;margin-inline:auto}}
 header{{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:20px}}
 h1{{font-size:17px;margin:0}} .ts{{color:var(--ink3);font-size:12px}}
 .kpis{{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:22px}}
 .kpi{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 8px;text-align:center}}
 .kpi .v{{font-size:22px;font-weight:700}} .kpi .l{{font-size:10.5px;color:var(--ink2);margin-top:4px;
      text-transform:uppercase;letter-spacing:.5px}}
 .kpi.good .v{{color:var(--good)}} .kpi.bad .v{{color:var(--bad)}}
 .card.fire{{background:#12261a;border:1px solid #238636;border-radius:12px;padding:18px;margin-bottom:14px}}
 .card.fire .t{{font-size:20px;font-weight:700;color:var(--good)}}
 .card.fire .p{{font-size:28px;font-weight:700;margin:4px 0}}
 .card.fire .m{{color:var(--ink2);font-size:13px}}
 .card.fire .a{{margin-top:10px;display:inline-block;background:#238636;color:#fff;font-weight:700;
      font-size:12px;padding:5px 12px;border-radius:6px;letter-spacing:.5px}}
 details{{background:var(--card);border:1px solid var(--line);border-radius:10px;margin-bottom:12px}}
 summary{{cursor:pointer;padding:13px 16px;font-size:13px;font-weight:600;color:var(--ink2);
      text-transform:uppercase;letter-spacing:.6px;list-style:none;display:flex;justify-content:space-between}}
 summary::after{{content:"▾";color:var(--ink3)}} details[open] summary::after{{content:"▴"}}
 table{{border-collapse:collapse;width:100%;font-size:14px}}
 td{{padding:9px 16px;border-top:1px solid var(--line)}}
 .pos{{color:var(--good);font-weight:600}} .neg{{color:var(--bad);font-weight:600}}
 .dim{{color:var(--ink3)}} .stop td{{background:#1f1416}} .exit td{{background:#1f1c12}}
 .count{{color:var(--ink3);font-weight:400}}
</style></head><body>"""

    sig_rows = "".join(
        f"<tr><td><b>{s['und']}</b></td><td class='dim'>fired {s['fired']}</td><td>{s['window']}</td></tr>"
        for s in signals) or "<tr><td colspan=3 class='dim'>no live signals — nothing to enter</td></tr>"

    shadow_rows = "".join(
        f"<tr><td><b>{c['und']}</b></td><td>{c['fired']}</td><td>{c['exited']}</td>"
        f"<td class='{'pos' if c['ret']>0 else 'neg'}'>{c['ret']*LOT:+,.0f}</td>"
        f"<td class='{'pos' if c['ret']>0 else 'neg'}'>{c['ret']:+.1%}</td><td class='dim'>{c['reason']}</td></tr>"
        for c in shadow_closed) or "<tr><td colspan=6 class='dim'>no closed shadow trades yet</td></tr>"
    sh = shadow_stats
    shadow_summary = (f"old rule A/B · {sh['open']} open · {sh['wins']}/{sh['n']} wins · ${sh['pl']:+,.0f}"
                      if (sh['n'] or sh['open']) else "old rule A/B — logging only")

    html_body = f"""
<header><h1>⚡ Sigma-Bet Scanner</h1><span class="ts">{ts} · data thru {eod_date} · refresh 60s</span></header>
<div class="kpis">{kpis}</div>
{fire_cards}
<details open><summary>Live signals — entry window <span class="count">{len(signals)}</span></summary>
<table>{sig_rows}</table></details>
<details open><summary>Open positions <span class="count">{len(positions)}</span></summary>
<table>{pos_rows}</table></details>
<details {"open" if watch else ""}><summary>Watchlist <span class="count">{len(watch)}</span></summary>
<table>{watch_rows}</table></details>
<details><summary>History <span class="count">{n_closed}</span></summary>
<table>{hist_rows}</table></details>
<details><summary>Shadow — {shadow_summary} <span class="count">{sh['n']}</span></summary>
<table>{shadow_rows}</table></details>
</body></html>"""
    (BASE / "live_dashboard.html").write_text(html_head + html_body)

# ---------------- email alerts ----------------
VENV_PY = str((BASE.parent / ".venv" / "bin" / "python"))

def send_alerts(fires, positions):
    """Email on: new fire (BUY), stop hit, day-12 exit due. One email per event,
    deduped across cycles via alerts_sent.json."""
    import subprocess
    state_path = BASE / "alerts_sent.json"
    sent = set(json.loads(state_path.read_text())) if state_path.exists() else set()
    today = datetime.now().strftime("%Y-%m-%d")
    events = []
    for f_ in fires:
        key = f"fire:{f_['und']}:{today}"
        if key in sent: continue
        events.append((key, f"🔥 BUY {f_['und']} @ ~${f_['spot']:.2f}",
            f"<h2 style='color:#238636'>🔥 FIRE: {f_['und']}</h2>"
            f"<p><b>${f_['spot']:.2f}</b> · {f_['sh']:.0%} of call $ at 3σ strikes (${f_['n3']/1e3:,.0f}k today) "
            f"· 10d drift {f_['drift']:+.0%}</p>"
            f"<p>Enter at today's close. Window: today + 2 trading days (void if 3σ flow fades).</p>"
            f"<p style='color:#888'>Sell: close of 12th day held · stop −15% on close · dip-add day 2 if red + flow alive.</p>"))
    for p_ in positions:
        if "STOP" in p_["flag"]:
            key = f"stop:{p_['und']}:{today}"
            if key in sent: continue
            events.append((key, f"🛑 SELL {p_['und']} — stop ({p_['ret']:+.1%})",
                f"<h2 style='color:#f85149'>🛑 STOP: {p_['und']}</h2>"
                f"<p>Entry ${p_['entry']:.2f} → ${p_['now']:.2f} ({p_['ret']:+.1%}). Close-basis stop hit — sell.</p>"))
        elif "DAY-12" in p_["flag"]:
            key = f"exit:{p_['und']}:{today}"
            if key in sent: continue
            events.append((key, f"⏰ SELL {p_['und']} — day-12 exit ({p_['ret']:+.1%})",
                f"<h2 style='color:#e3b341'>⏰ DAY-12 EXIT: {p_['und']}</h2>"
                f"<p>Entry ${p_['entry']:.2f} → ${p_['now']:.2f} ({p_['ret']:+.1%}). Sell at today's close.</p>"))
    for key, subject, html in events:
        try:
            r = subprocess.run([VENV_PY, str(BASE / "alert_email.py"), subject, html],
                               capture_output=True, text=True, timeout=90)
            if r.returncode == 0:
                sent.add(key)
                print(f"  📧 emailed: {subject}")
            else:
                print(f"  📧 email FAILED: {r.stderr.strip()[:200]}")
        except Exception as e:
            print(f"  📧 email error: {e}")
    state_path.write_text(json.dumps(sorted(sent)))

# ---------------- cycle ----------------
def cycle():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    latest, drift, spike_hist, eod_date = load_baselines()

    log_path = BASE / "fires_log.csv"
    log = pd.read_csv(log_path) if log_path.exists() else pd.DataFrame(columns=["date","und","family","entry","status","mode","rule"])
    if "mode" not in log.columns: log["mode"] = "paper"
    if len(log): log["date"] = pd.to_datetime(log["date"])
    open_all = log[log["status"] == "open"] if len(log) else log
    open_pos = open_all[open_all["mode"] != "shadow"] if len(open_all) else open_all
    shadow_open = open_all[open_all["mode"] == "shadow"] if len(open_all) else open_all

    watch_tickers = set(spike_hist["und"]) | set(open_pos["und"] if len(open_pos) else [])
    fires, watch, positions = [], [], []

    for und in sorted(watch_tickers):
        if und not in latest.index: continue
        prev_close = latest.loc[und, "close"]; sig20 = latest.loc[und, "sig20"]
        n3, total, spot = poll_chain(und, prev_close, sig20)
        sh = n3 / total if total > 0 else 0.0
        dr = drift.get(und, np.nan)
        hist = spike_hist[spike_hist["und"] == und]
        prior_days = hist.iloc[0]["spike_days"] if len(hist) else []
        spike_today = (n3 >= SPIKE_NOTIONAL) and (sh >= SPIKE_SHARE)
        n_spikes = len(prior_days) + (1 if spike_today else 0)

        # open position management (paper = v5.1 auto-entries)
        if len(open_pos) and und in set(open_pos["und"]):
            p = open_pos[open_pos["und"] == und].iloc[0]
            now_px = poll_stock(und, spot)
            ret = now_px / p["entry"] - 1
            day_n = int(np.busday_count(np.datetime64(pd.Timestamp(p['date']).date()), np.datetime64(datetime.now().date())))
            flow_ok = sh >= SPIKE_SHARE
            flag, cls = "hold", ""
            if ret <= STOP_CLOSE: flag, cls = "🛑 STOP — SELL", "stop"
            elif day_n >= HOLD_DAYS: flag, cls = "⏰ DAY-12 — SELL AT CLOSE", "exit"
            elif day_n == 2 and ret < 0 and flow_ok: flag = "➕ DIP-ADD"
            positions.append({"und": und, "fired": pd.Timestamp(p["date"]).strftime("%Y-%m-%d"), "day": day_n,
                              "entry": p["entry"], "now": now_px, "ret": ret, "flag": flag, "cls": cls,
                              "flow_ok": flow_ok})
            continue

        # fire / watch logic
        family = fam(und)
        deduped = len(log) and len(log[(log["family"] == family) &
                       (log["date"] > pd.Timestamp.now() - pd.Timedelta(days=DEDUP_DAYS))])
        cum_n3 = (hist.iloc[0]["n3_recent"] if len(hist) else 0) + n3
        if (n_spikes >= 2 and cum_n3 >= FIRE_NOTIONAL and pd.notna(dr)
                and DRIFT_LO <= dr <= DRIFT_HI and not deduped):
            fires.append({"und": und, "spot": spot, "sh": sh, "n3": n3, "drift": dr,
                          "days": f"{len(prior_days)}+{'today' if spike_today else '0'}"})
            log = pd.concat([log, pd.DataFrame([{"date": pd.Timestamp.now().normalize(), "und": und,
                    "family": family, "entry": spot, "status": "open"}])], ignore_index=True)
        else:
            note = "deduped (family active)" if deduped else ("spike TODAY — needs drift" if spike_today else "awaiting 2nd spike")
            watch.append({"und": und, "spikes": n_spikes, "sh": sh, "n3": n3,
                          "drift": dr if pd.notna(dr) else 0, "note": note})

    closed, shadow_closed = [], []
    if len(log):
        for _, c in log[log["status"] == "closed"].iterrows():
            ret = c.get("exit_ret")
            if pd.isna(ret) and pd.notna(c.get("exit_price")):
                ret = c["exit_price"] / c["entry"] - 1
            rec = {"und": c["und"], "fired": str(c["date"])[:10],
                   "exited": str(c.get("exit_date",""))[:10],
                   "ret": float(ret) if pd.notna(ret) else 0.0,
                   "reason": c.get("exit_reason",""),
                   "rule": c.get("rule","v5.1")}
            (shadow_closed if c.get("mode") == "shadow" else closed).append(rec)

    # live signals: v5.1 fires still inside the entry window (T+0..T+2, flow alive)
    signals = []
    for p_ in positions:
        if p_["day"] > 2: continue
        days_left = 2 - p_["day"]
        if not p_["flow_ok"]:
            window = "❌ EXPIRED — flow faded, do not enter"
        elif days_left == 2: window = "🟢 T+0 — enter at today's close (2 more days ok)"
        elif days_left == 1: window = "🟡 T+1 — 1 day left to enter"
        else: window = "🟠 T+2 — LAST day to enter"
        signals.append({"und": p_["und"], "fired": p_["fired"], "day": p_["day"],
                        "window": window, "flow_ok": p_["flow_ok"]})
    for f_ in fires:
        signals.insert(0, {"und": f_["und"], "fired": "today", "day": 0,
                           "window": "🟢 T+0 — enter at today's close (2 more days ok)", "flow_ok": True})

    n_shadow = len(shadow_closed)
    shadow_pl = sum(c["ret"] for c in shadow_closed) * LOT
    shadow_wins = sum(1 for c in shadow_closed if c["ret"] > 0)
    shadow_stats = {"n": n_shadow, "pl": shadow_pl, "wins": shadow_wins, "open": len(shadow_open)}

    log.to_csv(log_path, index=False)
    write_dashboard(ts, fires, watch, positions, eod_date, closed, signals, shadow_closed, shadow_stats)
    send_alerts(fires, positions)

    print(f"[{ts}] fires={len(fires)} watch={len(watch)} positions={len(positions)}  -> live_dashboard.html")
    for f_ in fires: print(f"  🔥 BUY {f_['und']} @ ~${f_['spot']:.2f}  (3σ share {f_['sh']:.0%}, ${f_['n3']/1e3:.0f}k today)")
    for p_ in positions:
        if p_["flag"] != "hold": print(f"  {p_['flag']}: {p_['und']} ({p_['ret']:+.1%})")

def main():
    once = "--once" in sys.argv
    interval = 15
    if "--interval" in sys.argv:
        interval = int(sys.argv[sys.argv.index("--interval")+1])
    while True:
        cycle()
        if once: break
        time.sleep(interval * 60)

if __name__ == "__main__":
    main()
