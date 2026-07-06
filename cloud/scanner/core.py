"""Sigma-bet scanner core — cloud port with Azure Blob state.

State blobs (container "state"):
  closes.csv            und,date,close       (rolling ~90 days)
  sigma_bets_daily.csv  und,date,call_notional,n3_notional
  fires_log.csv         positions + shadow log
  alerts_sent.json      email dedup keys
Dashboard -> container "$web"/index.html (static website).
"""
import gzip
import io
import json
import os
import urllib.request
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

API = "https://api.massive.com"
KEY = os.environ["MASSIVE_API_KEY"]

SPIKE_NOTIONAL = 100_000
SPIKE_SHARE    = 0.20
FIRE_NOTIONAL  = 250_000
DRIFT_LO, DRIFT_HI = 0.05, 0.30
DTE_MAX   = 60
SIGMA_MIN = 3.0
HOLD_DAYS = 12
STOP_CLOSE = -0.15
DEDUP_DAYS = 21
LOT = 100

FAMILY = {"NVDL":"NVDA","SNXX":"SNDK","SNDU":"SNDK","SNDG":"SNDK","MULL":"MU",
          "WDCX":"WDC","DLLL":"DELL","INTW":"INTC","MVLL":"MRVL","MRVU":"MRVL"}
fam = lambda t: FAMILY.get(t, t)

# ---------------- blob state ----------------
def _svc():
    from azure.storage.blob import BlobServiceClient
    return BlobServiceClient.from_connection_string(os.environ["AzureWebJobsStorage"])

def read_csv_blob(name):
    try:
        data = _svc().get_blob_client("state", name).download_blob().readall()
        return pd.read_csv(io.BytesIO(data))
    except Exception:
        return None

def write_csv_blob(name, df):
    _svc().get_blob_client("state", name).upload_blob(df.to_csv(index=False), overwrite=True)

def read_json_blob(name, default):
    try:
        return json.loads(_svc().get_blob_client("state", name).download_blob().readall())
    except Exception:
        return default

def write_json_blob(name, obj):
    _svc().get_blob_client("state", name).upload_blob(json.dumps(obj), overwrite=True)

def write_dashboard_blob(html):
    from azure.storage.blob import ContentSettings
    _svc().get_blob_client("$web", "index.html").upload_blob(
        html, overwrite=True, content_settings=ContentSettings(content_type="text/html"))

# ---------------- helpers ----------------
def get_json(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"_error": str(e)}

def now_et():
    return datetime.now(timezone.utc) - timedelta(hours=4)   # EDT approximation

def load_state():
    closes = read_csv_blob("closes.csv")
    sb = read_csv_blob("sigma_bets_daily.csv")
    log = read_csv_blob("fires_log.csv")
    if log is None:
        log = pd.DataFrame(columns=["date","und","family","entry","status","mode","rule",
                                    "exit_date","exit_price","exit_ret","exit_reason"])
    if closes is not None: closes["date"] = pd.to_datetime(closes["date"])
    if sb is not None: sb["date"] = pd.to_datetime(sb["date"])
    if len(log): log["date"] = pd.to_datetime(log["date"])
    return closes, sb, log

def baselines(closes, sb):
    closes = closes.sort_values(["und","date"])
    closes["logret"] = np.log(closes["close"] / closes.groupby("und")["close"].shift(1))
    closes["sig20"] = closes.groupby("und", group_keys=False)["logret"].apply(
        lambda s: s.rolling(20, min_periods=10).std())
    latest = closes.sort_values("date").groupby("und").tail(1).set_index("und")
    drift = closes.groupby("und")["close"].apply(
        lambda s: s.iloc[-1]/s.iloc[-11]-1 if len(s) >= 11 else np.nan)
    sb = sb.copy()
    sb["sh_1d"] = sb["n3_notional"] / sb["call_notional"].replace(0, np.nan)
    sb["spike"] = (sb["n3_notional"] >= SPIKE_NOTIONAL) & (sb["sh_1d"] >= SPIKE_SHARE)
    tdays = sorted(sb["date"].unique())
    last3 = tdays[-3:] if len(tdays) >= 3 else tdays
    recent = sb[sb["date"].isin(last3)]
    spikes = recent[recent["spike"]].groupby("und").agg(
        spike_days=("date", lambda s: sorted(str(x)[:10] for x in s)),
        n3_recent=("n3_notional","sum")).reset_index()
    eod = str(pd.Timestamp(tdays[-1]).date()) if len(tdays) else "?"
    return latest, drift, spikes, eod

# ---------------- intraday chain poll ----------------
def poll_chain(und, spot_fallback, sig20):
    url = f"{API}/v3/snapshot/options/{und}?limit=250&apiKey={KEY}"
    n3 = 0.0; total = 0.0; spot = None
    for _ in range(8):
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
                spot = (c.get("underlying_asset") or {}).get("price")
            K = det.get("strike_price"); exp = det.get("expiration_date")
            if not K or not exp: continue
            dte = (pd.Timestamp(exp) - pd.Timestamp(now_et().date())).days
            notional = vol * px * 100
            total += notional
            if dte <= 0 or dte > DTE_MAX: continue
            S = spot or spot_fallback
            if not S or S <= 0 or not sig20 or sig20 <= 0: continue
            sd = np.log(K / S) / (sig20 * np.sqrt(dte * 252/365))
            if sd >= SIGMA_MIN: n3 += notional
        nxt = data.get("next_url")
        if not nxt: break
        url = nxt + f"&apiKey={KEY}"
    return n3, total, (spot or spot_fallback)

# ---------------- email ----------------
def send_email(subject, html):
    from azure.communication.email import EmailClient
    client = EmailClient.from_connection_string(os.environ["ACS_CONNECTION_STRING"])
    sender_user = os.environ.get("EMAIL_SENDER_USERNAME", "DoNotReply")
    poller = client.begin_send({
        "senderAddress": f"{sender_user}@{os.environ['EMAIL_SENDER_DOMAIN']}",
        "recipients": {"to": [{"address": os.environ["EMAIL_TO"]}]},
        "content": {"subject": subject, "plainText": subject, "html": html},
    })
    poller.result(timeout=60)

def alert_once(key, subject, html):
    sent = set(read_json_blob("alerts_sent.json", []))
    if key in sent: return False
    send_email(subject, html)
    sent.add(key)
    write_json_blob("alerts_sent.json", sorted(sent))
    return True

# ---------------- dashboard (same layout as local) ----------------
def render_dashboard(ts, fires, watch, positions, eod_date, closed, signals, shadow_closed, shadow_open_n):
    n_closed = len(closed)
    wins = sum(1 for c in closed if c["ret"] > 0)
    total_pl = sum(c["ret"] for c in closed) * LOT
    open_pl = sum(p["ret"] for p in positions) * LOT
    sh_pl = sum(c["ret"] for c in shadow_closed) * LOT
    sh_wins = sum(1 for c in shadow_closed if c["ret"] > 0)
    def kpi(v, l, tone=""):
        return f"<div class='kpi {tone}'><div class='v'>{v}</div><div class='l'>{l}</div></div>"
    kpis = (kpi(len(fires), "fires now", "good" if fires else "") + kpi(len(watch), "watchlist") +
            kpi(len(positions), "open") +
            kpi(f"{open_pl:+.0f}" if positions else "—", "open P&L $", "good" if open_pl>0 else ("bad" if open_pl<0 else "")) +
            kpi(f"{wins}/{n_closed}" if n_closed else "—", "paper wins") +
            kpi(f"{total_pl:+.0f}", "closed P&L $", "good" if total_pl>0 else ("bad" if total_pl<0 else "")))
    fire_cards = "".join(
        f"<div class='card fire'><div class='t'>🔥 {r['und']}</div><div class='p'>${r['spot']:.2f}</div>"
        f"<div class='m'>{r['sh']:.0%} at 3σ · ${r['n3']/1e3:,.0f}k · drift {r['drift']:+.0%}</div>"
        f"<div class='a'>BUY AT CLOSE</div></div>" for r in fires)
    sig_rows = "".join(f"<tr><td><b>{s['und']}</b></td><td class='dim'>fired {s['fired']}</td><td>{s['window']}</td></tr>"
        for s in signals) or "<tr><td colspan=3 class='dim'>no live signals</td></tr>"
    pos_rows = "".join(
        f"<tr class='{r['cls']}'><td><b>{r['und']}</b></td><td>{r['day']}/{HOLD_DAYS}d</td>"
        f"<td class='{'pos' if r['ret']>0 else 'neg'}'>{r['ret']*LOT:+,.0f}</td>"
        f"<td class='{'pos' if r['ret']>0 else 'neg'}'>{r['ret']:+.1%}</td><td>{r['flag']}</td></tr>"
        for r in positions) or "<tr><td colspan=5 class='dim'>none</td></tr>"
    watch_rows = "".join(f"<tr><td><b>{r['und']}</b></td><td>{r['spikes']} spike(s)</td><td>{r['note']}</td></tr>"
        for r in watch) or "<tr><td colspan=3 class='dim'>empty</td></tr>"
    def hist(rows):
        return "".join(
            f"<tr><td><b>{c['und']}</b></td><td>{c['fired']}</td><td>{c['exited']}</td>"
            f"<td class='{'pos' if c['ret']>0 else 'neg'}'>{c['ret']*LOT:+,.0f}</td>"
            f"<td class='{'pos' if c['ret']>0 else 'neg'}'>{c['ret']:+.1%}</td><td class='dim'>{c['reason']}</td></tr>"
            for c in rows) or "<tr><td colspan=6 class='dim'>none yet</td></tr>"
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><meta http-equiv="refresh" content="600">
<title>Sigma-Bet Scanner</title><style>
 :root{{--bg:#0d1117;--card:#161b22;--line:#21262d;--ink:#e6edf3;--ink2:#8b949e;--ink3:#484f58;--good:#3fb950;--bad:#f85149}}
 body{{font-family:-apple-system,sans-serif;background:var(--bg);color:var(--ink);margin:0;padding:28px;max-width:760px;margin-inline:auto}}
 header{{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:20px}}
 h1{{font-size:17px;margin:0}} .ts{{color:var(--ink3);font-size:12px}}
 .kpis{{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:22px}}
 .kpi{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 8px;text-align:center}}
 .kpi .v{{font-size:22px;font-weight:700}} .kpi .l{{font-size:10.5px;color:var(--ink2);margin-top:4px;text-transform:uppercase}}
 .kpi.good .v{{color:var(--good)}} .kpi.bad .v{{color:var(--bad)}}
 .card.fire{{background:#12261a;border:1px solid #238636;border-radius:12px;padding:18px;margin-bottom:14px}}
 .card.fire .t{{font-size:20px;font-weight:700;color:var(--good)}} .card.fire .p{{font-size:28px;font-weight:700;margin:4px 0}}
 .card.fire .m{{color:var(--ink2);font-size:13px}}
 .card.fire .a{{margin-top:10px;display:inline-block;background:#238636;color:#fff;font-weight:700;font-size:12px;padding:5px 12px;border-radius:6px}}
 details{{background:var(--card);border:1px solid var(--line);border-radius:10px;margin-bottom:12px}}
 summary{{cursor:pointer;padding:13px 16px;font-size:13px;font-weight:600;color:var(--ink2);text-transform:uppercase;list-style:none;display:flex;justify-content:space-between}}
 summary::after{{content:"▾"}} details[open] summary::after{{content:"▴"}}
 table{{border-collapse:collapse;width:100%;font-size:14px}} td{{padding:9px 16px;border-top:1px solid var(--line)}}
 .pos{{color:var(--good);font-weight:600}} .neg{{color:var(--bad);font-weight:600}} .dim{{color:var(--ink3)}}
 .stop td{{background:#1f1416}} .exit td{{background:#1f1c12}} .count{{color:var(--ink3);font-weight:400}}
</style></head><body>
<header><h1>⚡ Sigma-Bet Scanner <span style="color:#484f58;font-size:11px">azure</span></h1>
<span class="ts">{ts} ET · data thru {eod_date}</span></header>
<div class="kpis">{kpis}</div>
{fire_cards}
<details open><summary>Live signals — entry window <span class="count">{len(signals)}</span></summary><table>{sig_rows}</table></details>
<details open><summary>Open positions <span class="count">{len(positions)}</span></summary><table>{pos_rows}</table></details>
<details {"open" if watch else ""}><summary>Watchlist <span class="count">{len(watch)}</span></summary><table>{watch_rows}</table></details>
<details><summary>History <span class="count">{n_closed}</span></summary><table>{hist(closed)}</table></details>
<details><summary>Shadow v5.0 · {sh_wins}/{len(shadow_closed)} wins · ${sh_pl:+,.0f} <span class="count">{len(shadow_closed)}</span></summary><table>{hist(shadow_closed)}</table></details>
</body></html>"""

# ---------------- SCAN CYCLE (every 2h intraday) ----------------
def run_scan():
    closes, sb, log = load_state()
    if closes is None or sb is None:
        return "state not seeded"
    latest, drift, spikes, eod_date = baselines(closes, sb)
    ts = now_et().strftime("%Y-%m-%d %H:%M")
    today = now_et().strftime("%Y-%m-%d")

    open_all = log[log["status"] == "open"] if len(log) else log
    open_pos = open_all[open_all["mode"] != "shadow"] if len(open_all) else open_all
    watch_tickers = set(spikes["und"]) | set(open_pos["und"] if len(open_pos) else [])

    fires, watch, positions = [], [], []
    for und in sorted(watch_tickers):
        if und not in latest.index: continue
        prev_close = latest.loc[und, "close"]; sig20 = latest.loc[und, "sig20"]
        n3, total, spot = poll_chain(und, prev_close, sig20)
        sh = n3 / total if total > 0 else 0.0
        dr = drift.get(und, np.nan)
        hist_r = spikes[spikes["und"] == und]
        prior_days = hist_r.iloc[0]["spike_days"] if len(hist_r) else []
        spike_today = (n3 >= SPIKE_NOTIONAL) and (sh >= SPIKE_SHARE)
        n_spikes = len(prior_days) + (1 if spike_today else 0)

        if len(open_pos) and und in set(open_pos["und"]):
            p = open_pos[open_pos["und"] == und].iloc[0]
            ret = spot / p["entry"] - 1
            day_n = int(np.busday_count(np.datetime64(pd.Timestamp(p["date"]).date()),
                                        np.datetime64(now_et().date())))
            flow_ok = sh >= SPIKE_SHARE
            flag, cls = "hold", ""
            if ret <= STOP_CLOSE: flag, cls = "🛑 STOP — SELL", "stop"
            elif day_n >= HOLD_DAYS: flag, cls = "⏰ DAY-12 — SELL AT CLOSE", "exit"
            elif day_n == 2 and ret < 0 and flow_ok: flag = "➕ DIP-ADD"
            positions.append({"und": und, "fired": pd.Timestamp(p["date"]).strftime("%Y-%m-%d"),
                              "day": day_n, "entry": p["entry"], "now": spot, "ret": ret,
                              "flag": flag, "cls": cls, "flow_ok": flow_ok})
            continue

        family = fam(und)
        deduped = len(log) and len(log[(log["family"] == family) &
                       (log["date"] > pd.Timestamp(now_et().date()) - pd.Timedelta(days=DEDUP_DAYS))])
        cum_n3 = (hist_r.iloc[0]["n3_recent"] if len(hist_r) else 0) + n3
        if (n_spikes >= 2 and cum_n3 >= FIRE_NOTIONAL and pd.notna(dr)
                and DRIFT_LO <= dr <= DRIFT_HI and not deduped):
            fires.append({"und": und, "spot": spot, "sh": sh, "n3": n3, "drift": dr})
            log = pd.concat([log, pd.DataFrame([{"date": pd.Timestamp(now_et().date()), "und": und,
                    "family": family, "entry": spot, "status": "open", "mode": "paper", "rule": "v5.1"}])],
                    ignore_index=True)
        else:
            note = "deduped" if deduped else ("spike TODAY" if spike_today else "awaiting 2nd spike")
            watch.append({"und": und, "spikes": n_spikes, "note": note})

    # alerts — minimal: what / price / window
    window_end = np.busday_offset(np.datetime64(now_et().date()), 2, roll="forward")
    window_end_s = pd.Timestamp(window_end).strftime("%a %b %-d")
    for f_ in fires:
        alert_once(f"fire:{f_['und']}:{today}", f"🔥 {f_['und']} — ${f_['spot']:.2f} — buy by {window_end_s}",
            f"<div style='font-family:-apple-system,sans-serif;text-align:center;padding:24px'>"
            f"<div style='font-size:40px;font-weight:800;color:#238636'>🔥 {f_['und']}</div>"
            f"<div style='font-size:32px;font-weight:700;margin:8px 0'>${f_['spot']:.2f}</div>"
            f"<div style='font-size:16px;color:#555'>buy by close {window_end_s}</div></div>")
    for p_ in positions:
        if "STOP" in p_["flag"]:
            alert_once(f"stop:{p_['und']}:{today}", f"🛑 {p_['und']} — sell ({p_['ret']:+.1%})",
                f"<div style='font-family:-apple-system,sans-serif;text-align:center;padding:24px'>"
                f"<div style='font-size:40px;font-weight:800;color:#f85149'>🛑 {p_['und']}</div>"
                f"<div style='font-size:32px;font-weight:700;margin:8px 0'>{p_['ret']:+.1%}</div>"
                f"<div style='font-size:16px;color:#555'>sell — stop hit</div></div>")
        elif "DAY-12" in p_["flag"]:
            alert_once(f"exit:{p_['und']}:{today}", f"⏰ {p_['und']} — sell at close ({p_['ret']:+.1%})",
                f"<div style='font-family:-apple-system,sans-serif;text-align:center;padding:24px'>"
                f"<div style='font-size:40px;font-weight:800;color:#e3b341'>⏰ {p_['und']}</div>"
                f"<div style='font-size:32px;font-weight:700;margin:8px 0'>{p_['ret']:+.1%}</div>"
                f"<div style='font-size:16px;color:#555'>sell at close — day 12</div></div>")

    # live signals + closed
    signals = []
    for p_ in positions:
        if p_["day"] > 2: continue
        left = 2 - p_["day"]
        w = ("❌ EXPIRED — flow faded" if not p_["flow_ok"] else
             "🟢 T+0 — enter at close (2 more days)" if left == 2 else
             "🟡 T+1 — 1 day left" if left == 1 else "🟠 T+2 — LAST day")
        signals.append({"und": p_["und"], "fired": p_["fired"], "window": w})
    for f_ in fires:
        signals.insert(0, {"und": f_["und"], "fired": "today", "window": "🟢 T+0 — enter at close"})

    closed, shadow_closed = [], []
    if len(log):
        for _, c in log[log["status"] == "closed"].iterrows():
            ret = c.get("exit_ret")
            if pd.isna(ret) and pd.notna(c.get("exit_price")): ret = c["exit_price"]/c["entry"]-1
            rec = {"und": c["und"], "fired": str(c["date"])[:10], "exited": str(c.get("exit_date",""))[:10],
                   "ret": float(ret) if pd.notna(ret) else 0.0, "reason": str(c.get("exit_reason",""))}
            (shadow_closed if c.get("mode") == "shadow" else closed).append(rec)
    shadow_open_n = len(open_all[open_all["mode"] == "shadow"]) if len(open_all) else 0

    write_csv_blob("fires_log.csv", log)
    write_dashboard_blob(render_dashboard(ts, fires, watch, positions, eod_date, closed,
                                          signals, shadow_closed, shadow_open_n))
    return f"scan ok: fires={len(fires)} watch={len(watch)} pos={len(positions)}"

# ---------------- EOD UPDATE (nightly) ----------------
def _s3():
    import boto3
    return boto3.client("s3", endpoint_url="https://files.polygon.io",
                        aws_access_key_id=os.environ["POLY_S3_KEY"],
                        aws_secret_access_key=os.environ["POLY_S3_SECRET"])

def fetch_day_aggs(date_str):
    y, m, _ = date_str.split("-")
    key = f"us_options_opra/day_aggs_v1/{y}/{m}/{date_str}.csv.gz"
    try:
        obj = _s3().get_object(Bucket="flatfiles", Key=key)
        return pd.read_csv(io.BytesIO(gzip.decompress(obj["Body"].read())),
                           usecols=["ticker","volume","close"])
    except Exception:
        return None

def fetch_grouped(date_str):
    d = get_json(f"{API}/v2/aggs/grouped/locale/us/market/stocks/{date_str}?adjusted=true&apiKey={KEY}")
    rows = d.get("results") or []
    if not rows: return None
    df = pd.DataFrame(rows)[["T","c"]].rename(columns={"T":"und","c":"close"})
    df["date"] = pd.Timestamp(date_str)
    return df

def build_sigma_day(df, date_str, closes):
    day = pd.Timestamp(date_str)
    day_rows = closes[closes["date"] == day]
    if day_rows.empty: return None
    closes_s = closes.sort_values(["und","date"])
    closes_s["logret"] = np.log(closes_s["close"] / closes_s.groupby("und")["close"].shift(1))
    sig20 = closes_s.groupby("und", group_keys=False)["logret"].apply(
        lambda s: s.rolling(20, min_periods=10).std())
    closes_s["sig20"] = sig20
    day_full = closes_s[closes_s["date"] == day].set_index("und")
    df = df.rename(columns={"close":"opt_close"})
    ext = df["ticker"].str.extract(r"^O:([A-Z]+[0-9]*?)(\d{6})([CP])(\d{8})$")
    ext.columns = ["root","exp","cp","strike"]
    df = pd.concat([df, ext], axis=1).dropna(subset=["root"])
    df = df[df["cp"] == "C"].copy()
    df["strike"] = df["strike"].astype(float) / 1000.0
    df["spot"] = df["root"].map(day_full["close"])
    df["sig"] = df["root"].map(day_full["sig20"])
    df = df.dropna(subset=["spot","sig"])
    df = df[(df["spot"] > 0) & (df["sig"] > 0)]
    exp_dt = pd.to_datetime("20" + df["exp"], format="%Y%m%d", errors="coerce")
    df["dte"] = (exp_dt - day).dt.days
    df = df[df["dte"] > 0]
    df["sigma_dist"] = np.log(df["strike"]/df["spot"]) / (df["sig"] * np.sqrt(df["dte"] * 252/365))
    df["notional"] = df["volume"] * df["opt_close"] * 100
    near = df["dte"] <= DTE_MAX
    df["n3"] = df["notional"].where((df["sigma_dist"] >= SIGMA_MIN) & near, 0)
    g = df.groupby("root").agg(call_notional=("notional","sum"), n3_notional=("n3","sum")).reset_index()
    g = g.rename(columns={"root":"und"}); g["date"] = day
    return g

def run_eod():
    closes, sb, log = load_state()
    if closes is None:
        return "state not seeded"
    have = set(str(d)[:10] for d in (sb["date"].unique() if sb is not None else []))
    msgs = []
    # try the last 5 calendar days for anything missing
    for i in range(5, 0, -1):
        d = (now_et() - timedelta(days=i)).strftime("%Y-%m-%d")
        if d in have: continue
        if pd.Timestamp(d).weekday() >= 5: continue
        gr = fetch_grouped(d)
        if gr is None: continue
        closes = pd.concat([closes, gr], ignore_index=True).drop_duplicates(["und","date"], keep="last")
        da = fetch_day_aggs(d)
        if da is None:
            msgs.append(f"{d}: no flat file yet"); continue
        srow = build_sigma_day(da, d, closes)
        if srow is not None:
            sb = pd.concat([sb, srow], ignore_index=True)
            msgs.append(f"{d}: built ({len(srow)} und)")
    # trim closes to last 90 days
    cutoff = pd.Timestamp(now_et().date()) - pd.Timedelta(days=130)
    closes = closes[closes["date"] >= cutoff]

    # position management on latest EOD closes (paper + shadow)
    if len(log):
        latest_close = closes.sort_values("date").groupby("und").tail(1).set_index("und")["close"]
        last_day = str(closes["date"].max())[:10]
        for i, p in log[log["status"] == "open"].iterrows():
            t = p["und"]
            if t not in latest_close.index: continue
            ret = latest_close[t] / p["entry"] - 1
            day_n = int(np.busday_count(np.datetime64(pd.Timestamp(p["date"]).date()),
                                        np.datetime64(pd.Timestamp(last_day).date())))
            if ret <= STOP_CLOSE or day_n >= HOLD_DAYS:
                log.loc[i, ["status","exit_date","exit_price","exit_ret","exit_reason"]] = \
                    ["closed", last_day, latest_close[t], ret,
                     "stop -15%" if ret <= STOP_CLOSE else "day-12 close"]
                if p.get("mode") != "shadow":
                    alert_once(f"eodclose:{t}:{last_day}",
                        f"{'🛑' if ret <= STOP_CLOSE else '⏰'} CLOSED {t} ({ret:+.1%})",
                        f"<p>{t}: entry ${p['entry']:.2f} → ${latest_close[t]:.2f} ({ret:+.1%}) — "
                        f"{'stop' if ret <= STOP_CLOSE else 'day-12 exit'}.</p>")

    # shadow v5.0 fires on latest day (10d rolling rule)
    if sb is not None and len(sb):
        f = sb.merge(closes, on=["und","date"], how="left").sort_values(["und","date"])
        g = f.groupby("und", group_keys=False)
        f["drift_10d"] = g["close"].apply(lambda s: s/s.shift(10)-1)
        f["cn10"] = g["call_notional"].apply(lambda s: s.rolling(10, min_periods=5).sum())
        f["n310"] = g["n3_notional"].apply(lambda s: s.rolling(10, min_periods=5).sum())
        f["sh10"] = f["n310"] / f["cn10"].replace(0, np.nan)
        last_day_ts = f["date"].max()
        tod = f[(f["date"] == last_day_ts) & (f["sh10"] >= 0.2) & (f["n310"] >= FIRE_NOTIONAL) &
                f["drift_10d"].between(DRIFT_LO, DRIFT_HI) & (f["close"] > 3) & (f["cn10"] >= 100_000)]
        for _, r in tod.iterrows():
            family = fam(r["und"])
            shadow_prior = log[(log["mode"] == "shadow") & (log["family"] == family) &
                               (log["date"] > last_day_ts - pd.Timedelta(days=DEDUP_DAYS))]
            if len(shadow_prior): continue
            log = pd.concat([log, pd.DataFrame([{"date": last_day_ts, "und": r["und"], "family": family,
                    "entry": r["close"], "status": "open", "mode": "shadow",
                    "rule": "v5.0 (10d window)"}])], ignore_index=True)
            msgs.append(f"shadow fire: {r['und']}")

    write_csv_blob("closes.csv", closes)
    if sb is not None: write_csv_blob("sigma_bets_daily.csv", sb)
    write_csv_blob("fires_log.csv", log)
    return "eod ok: " + ("; ".join(msgs) if msgs else "nothing new")
