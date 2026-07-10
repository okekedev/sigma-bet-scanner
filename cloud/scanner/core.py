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
import logging
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

# ---------------- ETF mean-reversion (see research/reversion_alignment.py) ----------------
# Buy a fresh cross below the 5-day MA, per-ETF threshold (calm names -2%, wild
# names -3% ~ 1.3x each ETF's own avg |dev|), hold REV_HOLD days, no early exit,
# no profit target (winners run). Macro uptrend is shown for context, NOT gated
# (regime filter unvalidated on <=2yr data). Validated pooled edge ~+1.7%/10d.
REV_UNIVERSE = {  # ticker: (theme, entry_dev_pct) — threshold ~ sized to each ETF's avg |dev|
    "GLD": ("Gold", -2.0), "SLV": ("Silver", -2.0), "URA": ("Uranium", -3.0),
    "USO": ("Oil", -3.0),  "XBI": ("Biotech", -3.0),
    # one per industry, thresholds measured from 2yr avg |dev|
    "URNM": ("Uranium miners", -3.0), "SMH": ("Semiconductors", -3.0),
    "XHB": ("Homebuilders", -2.0), "KRE": ("Regional banks", -2.0), "XLE": ("Energy", -2.0),
}
REV_FAST = 5      # reversion anchor MA
REV_HOLD = 10     # validated best simple exit (never sell in first 2 days)

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

# ---------------- ETF mean-reversion scan ----------------
def fetch_etf_daily(tk, days=400):
    frm = (now_et() - timedelta(days=days)).strftime("%Y-%m-%d")
    to  = now_et().strftime("%Y-%m-%d")
    js = get_json(f"{API}/v2/aggs/ticker/{tk}/range/1/day/{frm}/{to}"
                  f"?adjusted=true&sort=asc&limit=5000&apiKey={KEY}")
    if "_error" in js or not js.get("results"):
        return None
    df = pd.DataFrame(js["results"]).rename(columns={"c": "close", "t": "ts"})
    df["date"] = pd.to_datetime(df["ts"], unit="ms")
    return df[["date", "close"]].sort_values("date").reset_index(drop=True)

def scan_reversion(log):
    """Fresh-dip detection + 10-day exit management for the ETF universe.
    Mutates and returns (log, fires, states). Self-contained: uses its own
    ETF bars, so it never touches the sigma `closes` blob."""
    fires, states = [], []
    for tk, (theme, dev_thr) in REV_UNIVERSE.items():
        df = fetch_etf_daily(tk)
        if df is None or len(df) < 120:
            continue
        c = df["close"]
        ma5 = c.rolling(REV_FAST, min_periods=REV_FAST).mean()
        df["dev5"] = (c - ma5) / ma5 * 100.0
        ma100 = c.rolling(100, min_periods=100).mean()
        cur, prev = df.iloc[-1], df.iloc[-2]
        uptrend = bool(pd.notna(ma100.iloc[-1]) and cur["close"] > ma100.iloc[-1]
                       and ma100.iloc[-1] > ma100.iloc[-21])
        fresh = (cur["dev5"] <= dev_thr) and (prev["dev5"] > dev_thr)
        states.append(dict(tk=tk, theme=theme, dev5=cur["dev5"], thr=dev_thr,
                           close=cur["close"], uptrend=uptrend, buy=fresh))
        # manage this ETF's open reversion position (10-day time exit)
        openp = log[(log.get("mode") == "reversion") & (log["und"] == tk) & (log["status"] == "open")]
        for i, p in openp.iterrows():
            day_n = int(np.busday_count(np.datetime64(pd.Timestamp(p["date"]).date()),
                                        np.datetime64(pd.Timestamp(cur["date"]).date())))
            if day_n >= REV_HOLD:
                ret = cur["close"] / p["entry"] - 1
                log.loc[i, ["status", "exit_date", "exit_price", "exit_ret", "exit_reason"]] = \
                    ["closed", str(cur["date"])[:10], cur["close"], ret, f"day-{REV_HOLD} close"]
                alert_once(f"revclose:{tk}:{str(cur['date'])[:10]}",
                           f"⏰ CLOSED {tk} reversion ({ret:+.1%})",
                           f"<p>{tk} {theme}: entry ${p['entry']:.2f} → "
                           f"${cur['close']:.2f} ({ret:+.1%}) — day-{REV_HOLD} exit.</p>")
        # fresh dip and nothing open for this ETF -> new paper entry
        if fresh and not len(openp):
            log = pd.concat([log, pd.DataFrame([dict(
                date=cur["date"], und=tk, family=tk, entry=cur["close"],
                status="open", mode="reversion", rule=f"dip{dev_thr:.0f}")])], ignore_index=True)
            fires.append(dict(tk=tk, theme=theme, dev5=cur["dev5"],
                              close=cur["close"], uptrend=uptrend))
    return log, fires, states

def render_reversion_email(fires, states):
    def row(s):
        flag = "🟢 BUY" if s["buy"] else ("· below" if s["dev5"] < 0 else "above")
        trend = "↑ uptrend" if s["uptrend"] else "↓ / flat"
        return (f"<tr><td>{s['tk']}</td><td>{s['theme']}</td>"
                f"<td style='text-align:right'>{s['dev5']:+.2f}%</td>"
                f"<td style='text-align:right'>{s['thr']:.0f}%</td>"
                f"<td>{flag}</td><td>{trend}</td></tr>")
    body = "".join(row(s) for s in sorted(states, key=lambda x: x["dev5"]))
    names = ", ".join(f"{f['tk']} ({f['dev5']:+.1f}%)" for f in fires)
    return (f"<h3>ETF reversion buy zone: {names}</h3>"
            f"<p>Fresh cross below the 5-day MA. Rule: hold {REV_HOLD} days, "
            f"no early exit, no profit target. Trend column is context only.</p>"
            f"<table border='1' cellpadding='6' cellspacing='0' "
            f"style='border-collapse:collapse;font-family:sans-serif'>"
            f"<tr><th>ETF</th><th>theme</th><th>dev vs 5d MA</th><th>buy threshold</th>"
            f"<th>signal</th><th>100d trend</th></tr>{body}</table>")

def _sum_ret(df):
    if not len(df) or "exit_ret" not in df.columns: return 0.0
    return float(df["exit_ret"].fillna(0).sum()) * LOT

def render_daily_brief(rev_states, log, latest_close, day):
    """One combined morning email covering BOTH strategies: sigma-options status
    + the full ETF reversion state table. Sent every nightly run (deduped/day)."""
    TD = "padding:5px 8px;border-top:1px solid #eee"
    TDR = TD + ";text-align:right"
    TH = "text-align:left;color:#888;font-weight:600;font-size:12px;padding:4px 8px"
    open_all = log[log["status"] == "open"] if len(log) else log
    def held(d0):
        return int(np.busday_count(np.datetime64(pd.Timestamp(d0).date()),
                                   np.datetime64(pd.Timestamp(day).date())))
    def pos_rows(rows, hold_d, price_of):
        out = ""
        for _, p in (rows.iterrows() if len(rows) else []):
            cur = price_of(p["und"]); ret = (cur / p["entry"] - 1) if pd.notna(cur) else np.nan
            out += (f"<tr><td style='{TD}'><b>{p['und']}</b></td><td style='{TD}'>{held(p['date'])}/{hold_d}d</td>"
                    f"<td style='{TD}'>${p['entry']:.2f}</td>"
                    f"<td style='{TDR}'>{('%+.1f%%' % (ret*100)) if pd.notna(ret) else '—'}</td></tr>")
        return out or f"<tr><td colspan=4 style='{TD};color:#888'>no open positions</td></tr>"

    # ---- ETF reversion ----
    def flag(s): return "🟢 BUY" if s["buy"] else ("· below" if s["dev5"] < 0 else "above")
    erows = "".join(
        f"<tr><td style='{TD}'><b>{s['tk']}</b></td><td style='{TD}'>{s['theme']}</td>"
        f"<td style='{TDR}'>{s['dev5']:+.2f}%</td><td style='{TDR}'>{s['thr']:.0f}%</td>"
        f"<td style='{TD}'>{flag(s)}</td><td style='{TD}'>{'↑ up' if s['uptrend'] else '↓/flat'}</td></tr>"
        for s in sorted(rev_states, key=lambda x: x["dev5"]))
    revmap = {s["tk"]: s["close"] for s in rev_states}
    rev_open = open_all[open_all["mode"] == "reversion"] if len(open_all) else open_all
    rrows = pos_rows(rev_open, REV_HOLD, lambda t: revmap.get(t, np.nan))
    rev_pl = _sum_ret(log[(log["status"] == "closed") & (log["mode"] == "reversion")]) if len(log) else 0

    # ---- sigma options ----
    sig_open = open_all[~open_all["mode"].isin(["shadow", "reversion"])] if len(open_all) else open_all
    srows = pos_rows(sig_open, HOLD_DAYS, lambda t: latest_close.get(t, np.nan))
    sig_pl = _sum_ret(log[(log["status"] == "closed") & (~log["mode"].isin(["shadow", "reversion"]))]) if len(log) else 0
    shadow_open = len(open_all[open_all["mode"] == "shadow"]) if len(open_all) else 0

    buys = [s["tk"] for s in rev_states if s["buy"]]
    headline = ("🟢 ETF buy: " + ", ".join(buys)) if buys else "No new ETF buys today"
    tbl = "border-collapse:collapse;width:100%;font-size:14px"
    return (f"<div style='font-family:-apple-system,sans-serif;max-width:560px;margin:auto;color:#222'>"
            f"<h2 style='margin:0 0 2px'>📊 Daily brief · {day}</h2>"
            f"<div style='color:#238636;font-weight:700;margin-bottom:16px'>{headline}</div>"
            f"<h3 style='margin:14px 0 4px'>ETF reversion</h3>"
            f"<table style='{tbl}'><tr><th style='{TH}'>ETF</th><th style='{TH}'>theme</th>"
            f"<th style='{TH};text-align:right'>dev vs 5d</th><th style='{TH};text-align:right'>buy&lt;</th>"
            f"<th style='{TH}'>signal</th><th style='{TH}'>trend</th></tr>{erows}</table>"
            f"<div style='font-size:12px;color:#888;margin:8px 0 2px'>Open reversion positions · "
            f"closed P&amp;L ${rev_pl:+,.0f}</div>"
            f"<table style='{tbl}'>{rrows}</table>"
            f"<h3 style='margin:20px 0 4px'>Sigma options</h3>"
            f"<div style='font-size:12px;color:#888;margin-bottom:2px'>Open positions · shadow open {shadow_open} · "
            f"closed P&amp;L ${sig_pl:+,.0f}</div>"
            f"<table style='{tbl}'>{srows}</table>"
            f"<div style='font-size:11px;color:#aaa;margin-top:18px'>Reversion: buy fresh dip below 5d MA "
            f"(GLD/SLV −2%, URA/USO/XBI −3%), hold {REV_HOLD}d. Event alerts (buy-at-close, 🎯 aligned, exits) "
            f"arrive intraday as they happen.</div></div>")

# ---------------- intraday reversion ALIGNMENT (entry trigger) ----------------
def fetch_etf_15m(tk, days=40):
    frm = (now_et() - timedelta(days=days)).strftime("%Y-%m-%d")
    to  = now_et().strftime("%Y-%m-%d")
    js = get_json(f"{API}/v2/aggs/ticker/{tk}/range/15/minute/{frm}/{to}"
                  f"?adjusted=true&sort=asc&limit=50000&apiKey={KEY}")
    if "_error" in js or not js.get("results"):
        return None
    df = pd.DataFrame(js["results"]).rename(columns={"c": "close", "t": "ts"})
    df["hr"] = (df["ts"] // 1000 % 86400) // 3600
    df = df[(df["hr"] >= 14) & (df["hr"] <= 20)]          # core RTH, both DST regimes
    return df[["ts", "close"]].sort_values("ts").reset_index(drop=True)

def scan_alignment():
    """Intraday entry trigger. For each ETF that is ALSO in its daily buy zone
    (daily dev <= threshold), alert when the 1h/5h/5d timeframes all sit below
    their means at once (validated: this times the entry, ~30% better 1-2d than
    an unaligned entry). Deduped once per ETF per day. Independent of sigma flow."""
    hits = []
    for tk, (theme, dev_thr) in REV_UNIVERSE.items():
        d = fetch_etf_daily(tk, days=60)
        if d is None or len(d) < REV_FAST + 1:
            continue
        c = d["close"]; ma5 = c.rolling(REV_FAST, min_periods=REV_FAST).mean()
        daily_dev = (c.iloc[-1] - ma5.iloc[-1]) / ma5.iloc[-1] * 100.0
        if pd.isna(daily_dev) or daily_dev > dev_thr:     # not oversold on the daily -> skip
            continue
        m = fetch_etf_15m(tk)
        if m is None or len(m) < 130:
            continue
        cc = m["close"]
        def dv(w):
            ma = cc.rolling(w, min_periods=w).mean().iloc[-1]
            return (cc.iloc[-1] - ma) / ma * 100.0 if pd.notna(ma) else np.nan
        d1h, d5h, d5d = dv(4), dv(20), dv(130)
        if pd.notna(d5d) and d1h < 0 and d5h < 0 and d5d < 0:   # 3/3 aligned below
            hits.append(dict(tk=tk, theme=theme, daily_dev=daily_dev, d1h=d1h,
                             d5h=d5h, d5d=d5d, price=cc.iloc[-1]))
    for h in hits:
        alert_once(f"revalign:{h['tk']}:{now_et().strftime('%Y-%m-%d')}",
            f"🎯 {h['tk']} aligned — reversion entry ({h['daily_dev']:+.1f}% vs 5d)",
            f"<div style='font-family:-apple-system,sans-serif;padding:20px;text-align:center'>"
            f"<div style='font-size:38px;font-weight:800;color:#238636'>🎯 {h['tk']}</div>"
            f"<div style='font-size:26px;font-weight:700;margin:6px 0'>${h['price']:.2f}</div>"
            f"<div style='font-size:15px;color:#555'>all timeframes aligned below mean — entry window</div>"
            f"<div style='font-size:13px;color:#888;margin-top:8px'>daily {h['daily_dev']:+.1f}% · "
            f"1h {h['d1h']:+.2f}% · 5h {h['d5h']:+.2f}% · 5d {h['d5d']:+.2f}%</div></div>")
    return hits

def scan_reversion_buyzone():
    """Late-day (>=3pm ET) buy-at-close alert. The backtest enters at the CLOSE of
    the dip day, so this fires the daily BUY on the last intraday cycle -- in time
    to act -- rather than waiting for the 5a nightly job (a day late). Read-only:
    it does NOT open positions; the nightly job logs the entry on the settled
    close. Deduped per ETF per day."""
    if now_et().hour < 15:            # only near the close (~3:45p ET cycle)
        return []
    hits = []
    for tk, (theme, thr) in REV_UNIVERSE.items():
        d = fetch_etf_daily(tk, days=30)
        if d is None or len(d) < REV_FAST + 1:
            continue
        c = d["close"]; ma5 = c.rolling(REV_FAST, min_periods=REV_FAST).mean()
        dev = (c - ma5) / ma5 * 100.0
        cur, prev = dev.iloc[-1], dev.iloc[-2]
        if pd.notna(cur) and pd.notna(prev) and cur <= thr and prev > thr:
            hits.append(dict(tk=tk, theme=theme, dev=cur, price=c.iloc[-1], thr=thr))
    for h in hits:
        alert_once(f"revbuyclose:{h['tk']}:{now_et().strftime('%Y-%m-%d')}",
            f"🟢 {h['tk']} buy at close ({h['dev']:+.1f}% vs 5d MA)",
            f"<div style='font-family:-apple-system,sans-serif;padding:20px;text-align:center'>"
            f"<div style='font-size:38px;font-weight:800;color:#238636'>🟢 {h['tk']}</div>"
            f"<div style='font-size:26px;font-weight:700;margin:6px 0'>${h['price']:.2f}</div>"
            f"<div style='font-size:15px;color:#555'>{h['theme']} — fresh dip below 5-day MA · "
            f"buy at close</div>"
            f"<div style='font-size:13px;color:#888;margin-top:8px'>{h['dev']:+.1f}% vs 5d "
            f"(threshold {h['thr']:.0f}%) · hold {REV_HOLD}d</div></div>")
    return hits

# ---------------- dashboard (same layout as local) ----------------
def render_dashboard(ts, fires, watch, positions, eod_date, closed, signals, shadow_closed,
                     shadow_open_n, rev_open=None, rev_closed=None):
    rev_open = rev_open or []; rev_closed = rev_closed or []
    n_closed = len(closed)
    wins = sum(1 for c in closed if c["ret"] > 0)
    total_pl = sum(c["ret"] for c in closed) * LOT
    open_pl = sum(p["ret"] for p in positions) * LOT
    sh_pl = sum(c["ret"] for c in shadow_closed) * LOT
    sh_wins = sum(1 for c in shadow_closed if c["ret"] > 0)
    rev_pl = sum(c["ret"] for c in rev_closed) * LOT
    rev_wins = sum(1 for c in rev_closed if c["ret"] > 0)
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
    rev_open_rows = "".join(
        f"<tr><td><b>{r['und']}</b></td><td>{r['day']}/{REV_HOLD}d</td>"
        f"<td>entry ${r['entry']:.2f}</td><td class='dim'>{r['rule']} · {r['fired']}</td></tr>"
        for r in rev_open) or "<tr><td colspan=4 class='dim'>none</td></tr>"
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
<details {"open" if rev_open else ""}><summary>ETF reversion · open {len(rev_open)} · {rev_wins}/{len(rev_closed)} wins · ${rev_pl:+,.0f} <span class="count">{len(rev_closed)}</span></summary><table>{rev_open_rows}</table><table>{hist(rev_closed)}</table></details>
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
    # sigma live positions only — exclude shadow AND reversion (managed elsewhere)
    open_pos = open_all[~open_all["mode"].isin(["shadow", "reversion"])] if len(open_all) else open_all
    watch_tickers = set(spikes["und"]) | set(open_pos["und"] if len(open_pos) else [])

    fires, watch, positions = [], [], []
    for und in sorted(watch_tickers):
        if und not in latest.index: continue
        prev_close = latest.loc[und, "close"]; sig20 = latest.loc[und, "sig20"]
        if prev_close <= 3 and und not in set(open_pos["und"] if len(open_pos) else []):
            continue  # universe gate: price > $3 (open positions still managed)
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

    closed, shadow_closed, rev_closed = [], [], []
    if len(log):
        for _, c in log[log["status"] == "closed"].iterrows():
            ret = c.get("exit_ret")
            if pd.isna(ret) and pd.notna(c.get("exit_price")): ret = c["exit_price"]/c["entry"]-1
            m = c.get("mode")
            rec = {"und": c["und"], "fired": str(c["date"])[:10], "exited": str(c.get("exit_date",""))[:10],
                   "ret": float(ret) if pd.notna(ret) else 0.0,
                   "reason": str(c.get("exit_reason","")) + (" · " + str(c.get("rule","")) if m in ("shadow","reversion") else "")}
            (rev_closed if m == "reversion" else shadow_closed if m == "shadow" else closed).append(rec)
    shadow_open_n = len(open_all[open_all["mode"] == "shadow"]) if len(open_all) else 0
    rev_open = []
    if len(open_all):
        for _, p in open_all[open_all["mode"] == "reversion"].iterrows():
            day_n = int(np.busday_count(np.datetime64(pd.Timestamp(p["date"]).date()),
                                        np.datetime64(now_et().date())))
            rev_open.append({"und": p["und"], "fired": str(p["date"])[:10], "day": max(day_n, 0),
                             "entry": float(p["entry"]), "rule": str(p.get("rule", ""))})

    # intraday reversion alerts (self-alerting, deduped per day): buy-at-close
    # near the close + alignment entry trigger. Independent of the sigma flow.
    try:
        buy_hits = scan_reversion_buyzone()
        align_hits = scan_alignment()
    except Exception as e:
        buy_hits = align_hits = []; logging.warning("reversion intraday scan: %s", e)

    write_csv_blob("fires_log.csv", log)
    write_dashboard_blob(render_dashboard(ts, fires, watch, positions, eod_date, closed,
                                          signals, shadow_closed, shadow_open_n, rev_open, rev_closed))
    return (f"scan ok: fires={len(fires)} watch={len(watch)} pos={len(positions)} "
            f"rev_open={len(rev_open)} buyzone={len(buy_hits)} aligned={len(align_hits)}")

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
    is_call = df["cp"] == "C"
    df["cn"] = df["notional"].where(is_call, 0)
    df["n3"] = df["notional"].where(is_call & (df["sigma_dist"] >= SIGMA_MIN) & near, 0)
    df["pn"] = df["notional"].where(~is_call, 0)
    df["p3"] = df["notional"].where(~is_call & (df["sigma_dist"] <= -SIGMA_MIN) & near, 0)
    g = df.groupby("root").agg(call_notional=("cn","sum"), n3_notional=("n3","sum"),
                               put_notional=("pn","sum"), p3_notional=("p3","sum")).reset_index()
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
            for _c in ["put_notional","p3_notional"]:
                if _c in sb.columns: sb[_c] = sb[_c].fillna(0)
            msgs.append(f"{d}: built ({len(srow)} und)")
    # trim closes to last 90 days
    cutoff = pd.Timestamp(now_et().date()) - pd.Timedelta(days=130)
    closes = closes[closes["date"] >= cutoff]

    # position management on latest EOD closes (paper + shadow)
    if len(log):
        latest_close = closes.sort_values("date").groupby("und").tail(1).set_index("und")["close"]
        last_day = str(closes["date"].max())[:10]
        for i, p in log[log["status"] == "open"].iterrows():
            if p.get("mode") == "reversion": continue  # managed by scan_reversion
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
        for _c in ["p3_notional","put_notional"]:
            if _c not in f.columns: f[_c] = 0.0
            f[_c] = f[_c].fillna(0)
        f["p3_3d"] = g["p3_notional"].apply(lambda s: s.rolling(3, min_periods=2).sum())
        f["p3_p7"] = g["p3_notional"].apply(lambda s: s.shift(3).rolling(7, min_periods=4).sum())
        f["put_absent"] = (f["p3_3d"].fillna(0) < 25_000) & (f["p3_p7"].fillna(0) < 25_000)

        def log_shadow(rows, rule_name):
            nonlocal log
            for _, r in rows.iterrows():
                family = fam(r["und"])
                prior = log[(log["mode"] == "shadow") & (log["rule"] == rule_name) &
                            (log["family"] == family) &
                            (log["date"] > last_day_ts - pd.Timedelta(days=DEDUP_DAYS))]
                if len(prior): continue
                log = pd.concat([log, pd.DataFrame([{"date": last_day_ts, "und": r["und"], "family": family,
                        "entry": r["close"], "status": "open", "mode": "shadow",
                        "rule": rule_name}])], ignore_index=True)
                msgs.append(f"shadow[{rule_name}] fire: {r['und']}")

        base_gate = (f["date"] == last_day_ts) & (f["close"] > 3) & (f["cn10"] >= 100_000) & \
                    f["drift_10d"].between(DRIFT_LO, DRIFT_HI)
        log_shadow(f[base_gate & (f["sh10"] >= 0.2) & (f["n310"] >= FIRE_NOTIONAL)],
                   "v5.0 (10d window)")
        log_shadow(f[base_gate & (f["sh10"] >= 0.10) & (f["n310"] >= FIRE_NOTIONAL) & f["put_absent"]],
                   "v5.2 (sh10 + $250k + put-ABSENT)")

    # ETF mean-reversion scan + combined daily brief (independent of sigma flow)
    try:
        log, rev_fires, rev_states = scan_reversion(log)
        last_day = str(closes["date"].max())[:10]
        latest_close = closes.sort_values("date").groupby("und").tail(1).set_index("und")["close"]
        # one combined morning email every night, deduped per day
        alert_once(f"dailybrief:{last_day}", f"📊 Daily brief · {last_day}",
                   render_daily_brief(rev_states, log, latest_close, last_day))
        if rev_fires:
            msgs.append("reversion buy: " + ",".join(f["tk"] for f in rev_fires))
    except Exception as e:
        msgs.append(f"reversion/brief error: {e}")

    write_csv_blob("closes.csv", closes)
    if sb is not None: write_csv_blob("sigma_bets_daily.csv", sb)
    write_csv_blob("fires_log.csv", log)
    return "eod ok: " + ("; ".join(msgs) if msgs else "nothing new")
