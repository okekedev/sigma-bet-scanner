"""Sigma-bet scanner core — cloud port with Azure Blob state.

State blobs (container "state"):
  closes.csv             und,date,close  (rolling ~90 days)
  sigma_bets_daily.csv   und,date,call/put notional + top call strike (~90 days)
  microcap_signals.csv   latest build's microcap flow flags (current watchlist)
  microcap_log.csv       rolling microcap flag history (dedup source)
  alerts_sent.json       email dedup keys
Dashboard -> container "$web"/index.html (static website).
"""
import gzip
import io
import json
import logging
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

API = "https://api.massive.com"
KEY = os.environ["MASSIVE_API_KEY"]

# sigma-fire thresholds — used by the dashboard's fire detection (run_scan) and
# the nightly sigma-bet build (build_sigma_day).
SPIKE_NOTIONAL = 100_000
SPIKE_SHARE    = 0.20
FIRE_NOTIONAL  = 250_000
DRIFT_LO, DRIFT_HI = 0.05, 0.30
DTE_MAX   = 60
SIGMA_MIN = 3.0

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
DASH_URL = "https://stoptionsscan.z13.web.core.windows.net/"

def regime_of(r10_pct):
    """10-day trend regime. 'mild-down' (-3 to -8%) is the best dip-bounce regime
    in backtest (+1.5%/10d, 62% win); dips in uptrends are the weakest."""
    if r10_pct is None or r10_pct != r10_pct:  # None/NaN
        return "?"
    if r10_pct > 0.5:   return "up"
    if r10_pct > -3:    return "flat"
    if r10_pct > -8:    return "mild-down"      # sweet spot
    if r10_pct > -15:   return "steep-down"
    return "crash"

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
def get_json(url, retries=6):
    """GET JSON with backoff on 429 so bursty fetches (e.g. the 10-ETF board)
    don't silently drop tickers when the data plan rate-limits. Backoff totals
    ~45s worst-case, which comfortably clears a per-minute rate window."""
    for i in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < retries - 1:
                time.sleep(3.0 * (i + 1)); continue
            return {"_error": f"HTTP {e.code}"}
        except Exception as e:
            return {"_error": str(e)}
    return {"_error": "429 retries exhausted"}

def now_et():
    # DST-correct Eastern, returned naive (callers use .date()/.hour/.strftime)
    return datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)

def now_central():
    return datetime.now(ZoneInfo("America/Chicago"))

# Central-local slots the brief should land at (hour, minute); timers fire at both
# DST-candidate UTC times and send_brief gates on the real Central hour.
BRIEF_SLOTS = {"pm": (14, 30), "am": (3, 0)}

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

def render_daily_brief(rev_states, day):
    """Mobile-first actionable snapshot: BUY signals today + below-MA watch +
    link to the live chart. Alerts only — no positions/P&L."""
    def r10s(s):
        v = s.get("r10")
        return f"{v:+.0f}%" if v is not None and v == v else "—"
    buys = sorted([s for s in rev_states if s.get("buy")], key=lambda x: x["dev5"])
    below = sorted([s for s in rev_states if not s.get("buy") and s["dev5"] < 0],
                   key=lambda x: x["dev5"])[:3]
    todo = ""
    for s in buys:
        star = "⭐ " if s.get("prime") else ""
        todo += (f"<div style='color:#238636;font-weight:700;font-size:17px;margin:8px 0'>{star}🟢 BUY "
                 f"{s['tk']} ${s['close']:.2f}<br><span style='font-weight:400;font-size:13px;color:#777'>"
                 f"{s['dev5']:+.1f}% vs 5d · 10d {r10s(s)} ({s.get('regime','')})</span></div>")
    if not todo:
        todo = "<div style='color:#666;font-size:16px'>✓ No buy signal today</div>"
    prime_note = ("<div style='font-size:12px;color:#238636;margin:8px 0'>"
                  "⭐ = dip in a mild-down (−3 to −8%) 10-day trend — the best-bounce regime.</div>"
                  if any(s.get("prime") for s in buys) else "")
    below_html = ""
    if below:
        rows = "".join(f"<div style='font-size:13px;color:#888;margin:3px 0'>"
                       f"<b style='color:#444'>{s['tk']}</b> {s['dev5']:+.1f}% vs 5d "
                       f"<span style='color:#aaa'>· 10d {r10s(s)}</span></div>" for s in below)
        below_html = (f"<div style='margin-top:16px'><div style='font-size:11px;color:#999;"
                      f"text-transform:uppercase;letter-spacing:.04em'>Below 5-day MA (not yet a buy)</div>"
                      f"{rows}</div>")
    return (f"<div style='font-family:-apple-system,sans-serif;max-width:480px;margin:auto;"
            f"padding:8px;color:#222'>"
            f"<div style='font-size:13px;color:#999'>{day}</div>"
            f"<h2 style='margin:2px 0 12px;font-size:20px'>Today’s brief</h2>"
            f"{todo}{prime_note}{below_html}"
            f"<a href='{DASH_URL}' style='display:block;text-align:center;margin-top:18px;"
            f"background:#238636;color:#fff;text-decoration:none;font-weight:600;font-size:15px;"
            f"padding:12px;border-radius:8px'>📈 View live trends &amp; chart</a>"
            f"<div style='font-size:11px;color:#aaa;margin-top:14px'>Buy fresh dip below 5-day MA, "
            f"hold ~{REV_HOLD} trading days. Full 10-ETF chart at the link.</div></div>")

def render_microcap_section(rows):
    """HTML block for the microcap flow watchlist inside the digest. `rows` is a
    list of dicts from microcap_signals(). Experimental — labelled as such."""
    if not rows:
        return ("<div style='margin-top:18px'><div style='font-size:11px;color:#999;"
                "text-transform:uppercase;letter-spacing:.04em'>Microcap flow watch (experimental)</div>"
                "<div style='font-size:13px;color:#888;margin:4px 0'>· none today</div></div>")
    cards = ""
    for r in rows:
        dte = int(r.get("dte") or 0)
        horizon = "soon" if dte <= 45 else ("weeks out" if dte <= 90 else "months out")
        cards += (f"<div style='background:#12261a;border:1px solid #238636;border-radius:8px;"
                  f"padding:9px 11px;margin:6px 0'>"
                  f"<div style='font-size:16px;font-weight:700;color:#238636'>🔎 {r['und']} "
                  f"<span style='color:#222'>${r['close']:.2f}</span></div>"
                  f"<div style='font-size:12px;color:#555;margin-top:2px'>${r['strike']:.1f} calls "
                  f"exp {r.get('expiry','?')} ({dte}d — {horizon}) · {r['ks']:.2f}× spot</div>"
                  f"<div style='font-size:12px;color:#555'>${r['call_notional']/1e3:.0f}k = "
                  f"{r['x_base']:.0f}× baseline · {r['top_share']:.0%} one strike · puts {r['put_share']:.0%}</div>"
                  f"<div style='font-size:11px;color:#888;margin-top:1px'>"
                  f"{r['below_hi']:+.0%} vs 20d high · beaten-down + deep-OTM call buying</div></div>")
    return ("<div style='margin-top:18px'><div style='font-size:11px;color:#999;"
            "text-transform:uppercase;letter-spacing:.04em'>Microcap flow watch (experimental)</div>"
            f"{cards}"
            "<div style='font-size:10px;color:#aaa;margin-top:4px'>Unusual single-strike call buying "
            "on beaten-down microcaps. ~9-day median lead; ~2/3 fizzle. Not advice — a watchlist.</div></div>")

def load_micro_signals():
    """Microcap signals from the blob the EOD job wrote. run_eod overwrites this
    each morning with the latest build's flags, so it is always the current
    watchlist (dated the prior trading session) until the next overnight build."""
    df = read_csv_blob("microcap_signals.csv")
    if df is None or not len(df):
        return []
    return df.to_dict("records")

def send_brief(tag, force=False, micro=None):
    """Build + send the consolidated digest: ETF reversion board + microcap flow
    watch, in ONE email. Runs pre-close (2:30p CT) and, folded into run_eod, in the
    morning after the flat file lands. tag: 'pm' | 'am'. `micro` (list of dicts) is
    passed by run_eod with fresh signals; otherwise read from the blob.

    DST: the timer fires at both candidate UTC times (CDT and CST); we only send
    when the real Central hour matches the slot, so it lands at the right CT slot
    year-round. `force=True` (manual endpoint) skips the gate for testing."""
    slot_h = BRIEF_SLOTS[tag][0]
    if not force and tag in BRIEF_SLOTS and now_central().hour != slot_h:
        return f"brief {tag}: skip (Central {now_central():%H:%M}, not the {slot_h}:00 slot)"
    board = reversion_board()
    states = [dict(tk=b["tk"], dev5=b["cur"], thr=b["thr"], close=b["price"],
                   buy=(b["state"] == "buy"), r10=b["r10"], regime=b["regime"], prime=b["prime"])
              for b in board]
    day = now_et().strftime("%Y-%m-%d")
    micro = micro if micro is not None else load_micro_signals()
    buys = [s["tk"] for s in states if s["buy"]]
    mtk = [m["und"] for m in micro]
    tags = (["BUY " + ", ".join(buys)] if buys else []) + (["flow " + ", ".join(mtk)] if mtk else [])
    subject = "ETFs — " + " · ".join(tags) if tags else f"ETFs · {day}"
    alert_once(f"brief:{tag}:{day}", subject, _digest_html(states, micro, day))
    return f"brief {tag}: {len(buys)} etf-buys / {len(micro)} microcap / {len(states)} etf"

def _digest_html(states, micro, day):
    """ETF brief + microcap section stitched into one mobile email."""
    base = render_daily_brief(states, day)
    inject = render_microcap_section(micro) + "<a href"
    return base.replace("<a href", inject, 1)   # insert microcap just above the chart button

# ---------------- intraday reversion ALIGNMENT (entry trigger) ----------------
def reversion_board():
    """Fetch each ETF's daily bars ONCE and compute its state + 30-day deviation
    trajectory. Single source for the intraday buy/align alerts AND the dashboard
    chart, so we don't re-fetch the same bars per feature. Sorted most-oversold first."""
    board = []
    for i, (tk, (theme, thr)) in enumerate(REV_UNIVERSE.items()):
        if i:
            time.sleep(13)      # data plan = 5 req/min; ~13s spacing keeps all 10 fetches under it
                                # deterministically (retry-backoff alone starves exactly one ticker)
        d = fetch_etf_daily(tk, days=120)
        if d is None or len(d) < REV_FAST + 2:
            continue
        c = d["close"]; ma5 = c.rolling(REV_FAST, min_periods=REV_FAST).mean()
        dev = (c - ma5) / ma5 * 100.0
        cur, prev = dev.iloc[-1], dev.iloc[-2]
        if pd.isna(cur):
            continue
        if cur <= thr:                          state = "buy"
        elif cur < 0 and cur > dev.iloc[-3]:    state = "turning"   # up over last 2 sessions
        elif cur < 0:                           state = "below"
        else:                                   state = "above"
        r10 = float(c.iloc[-1] / c.iloc[-11] - 1) * 100 if len(c) >= 11 else float("nan")
        regime = regime_of(r10)
        board.append(dict(tk=tk, theme=theme, thr=thr, price=float(c.iloc[-1]),
                          cur=float(cur), prev=float(prev), state=state, r10=r10, regime=regime,
                          prime=bool(state == "buy" and regime == "mild-down"),
                          dev30=[round(float(x), 2) for x in dev.dropna().iloc[-30:]]))
    board.sort(key=lambda b: b["cur"])
    return board

STATE_COLOR = {"buy": "#3fb950", "turning": "#e3b341", "below": "#58a6ff", "above": "#8b949e"}
STATE_LABEL = {"buy": "🟢 buy zone", "turning": "↑ turning up", "below": "· below MA", "above": "above MA"}

def _spark_svg(dev30, thr, color, W=200, H=44):
    if not dev30:
        return ""
    ymin, ymax = -10.0, 7.0
    xf = lambda i: 4 + i * (W - 8) / max(1, len(dev30) - 1)
    yf = lambda v: round(H - (max(ymin, min(ymax, v)) - ymin) / (ymax - ymin) * H, 1)
    path = " ".join(("L" if i else "M") + f"{xf(i):.1f} {yf(v):.1f}" for i, v in enumerate(dev30))
    return (f"<svg viewBox='0 0 {W} {H}' style='width:100%;height:{H}px;margin-top:4px'>"
            f"<line x1='0' y1='{yf(0)}' x2='{W}' y2='{yf(0)}' stroke='#484f58' stroke-width='1'/>"
            f"<text x='{W}' y='{yf(0)-2}' text-anchor='end' font-size='7' fill='#484f58'>5d MA</text>"
            f"<line x1='0' y1='{yf(thr)}' x2='{W}' y2='{yf(thr)}' stroke='#f85149' stroke-width='1' stroke-dasharray='3 3' opacity='.5'/>"
            f"<text x='{W}' y='{yf(thr)-2}' text-anchor='end' font-size='7' fill='#f85149' opacity='.7'>buy {thr:.0f}%</text>"
            f"<path d='{path}' fill='none' stroke='{color}' stroke-width='2' stroke-linejoin='round'/>"
            f"<circle cx='{xf(len(dev30)-1):.1f}' cy='{yf(dev30[-1])}' r='3.5' fill='{color}'/></svg>")

def rev_chart_html(board):
    """Server-rendered inline-SVG sparkline grid for the dashboard (no JS needed)."""
    if not board:
        return ""
    cells = ""
    for b in board:
        col = STATE_COLOR[b["state"]]
        r10 = b.get("r10", float("nan"))
        r10s = f"{r10:+.1f}%" if r10 == r10 else "—"
        prime = b.get("prime")
        star = "⭐ " if prime else ""
        border = "#3fb950" if prime else "#21262d"
        cells += (f"<div style='background:#0d1117;border:1px solid {border};border-radius:8px;padding:9px 10px'>"
                  f"<div style='display:flex;justify-content:space-between;align-items:baseline'>"
                  f"<b>{star}{b['tk']}</b><span style='color:{col};font-weight:700;font-variant-numeric:tabular-nums'>"
                  f"{b['cur']:+.2f}%</span></div>"
                  f"<div style='font-size:10px;color:{col}'>{STATE_LABEL[b['state']]}</div>"
                  f"<div style='font-size:10px;color:#8b949e'>10d {r10s} · {b.get('regime','?')}</div>"
                  f"{_spark_svg(b['dev30'], b['thr'], col)}</div>")
    return ("<details open><summary>ETF reversion — dev vs 5-day MA (30d) · ⭐ = dip in mild-down regime "
            f"<span class='count'>{len(board)}</span></summary>"
            "<div style='display:grid;grid-template-columns:repeat(auto-fill,minmax(148px,1fr));"
            f"gap:8px;padding:12px'>{cells}</div></details>")

# ---------------- dashboard (same layout as local) ----------------
def render_dashboard(ts, fires, watch, eod_date, board=None):
    """Alerts-only dashboard: current signals + the ETF reversion chart. No paper
    positions / P&L (removed). Mobile-responsive."""
    board = board or []
    rev_chart = rev_chart_html(board)
    n_buys = sum(1 for b in board if b["state"] == "buy")
    def kpi(v, l, tone=""):
        return f"<div class='kpi {tone}'><div class='v'>{v}</div><div class='l'>{l}</div></div>"
    kpis = (kpi(len(fires), "options fires", "good" if fires else "") +
            kpi(n_buys, "ETF buys", "good" if n_buys else "") +
            kpi(len(watch), "options watch") +
            kpi(len(board), "ETFs tracked"))
    fire_cards = "".join(
        f"<div class='card fire'><div class='t'>🔥 {r['und']}</div><div class='p'>${r['spot']:.2f}</div>"
        f"<div class='m'>{r['sh']:.0%} at 3σ · ${r['n3']/1e3:,.0f}k · drift {r['drift']:+.0%}</div>"
        f"<div class='a'>BUY AT CLOSE</div></div>" for r in fires)
    watch_rows = "".join(f"<tr><td><b>{r['und']}</b></td><td>{r['spikes']} spike(s)</td><td>{r['note']}</td></tr>"
        for r in watch) or "<tr><td colspan=3 class='dim'>empty</td></tr>"
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="600"><title>Reversion Scanner</title><style>
 :root{{--bg:#0d1117;--card:#161b22;--line:#21262d;--ink:#e6edf3;--ink2:#8b949e;--ink3:#484f58;--good:#3fb950;--bad:#f85149}}
 *{{box-sizing:border-box}}
 body{{font-family:-apple-system,sans-serif;background:var(--bg);color:var(--ink);margin:0;padding:16px;max-width:820px;margin-inline:auto}}
 header{{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:6px;margin-bottom:16px}}
 h1{{font-size:16px;margin:0}} .ts{{color:var(--ink3);font-size:12px}}
 .kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(76px,1fr));gap:8px;margin-bottom:18px}}
 .kpi{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 6px;text-align:center}}
 .kpi .v{{font-size:22px;font-weight:700}} .kpi .l{{font-size:10px;color:var(--ink2);margin-top:4px;text-transform:uppercase}}
 .kpi.good .v{{color:var(--good)}}
 .card.fire{{background:#12261a;border:1px solid #238636;border-radius:12px;padding:16px;margin-bottom:12px}}
 .card.fire .t{{font-size:20px;font-weight:700;color:var(--good)}} .card.fire .p{{font-size:26px;font-weight:700;margin:4px 0}}
 .card.fire .m{{color:var(--ink2);font-size:13px}}
 .card.fire .a{{margin-top:10px;display:inline-block;background:#238636;color:#fff;font-weight:700;font-size:12px;padding:5px 12px;border-radius:6px}}
 details{{background:var(--card);border:1px solid var(--line);border-radius:10px;margin-bottom:12px}}
 summary{{cursor:pointer;padding:13px 16px;font-size:13px;font-weight:600;color:var(--ink2);text-transform:uppercase;list-style:none;display:flex;justify-content:space-between}}
 summary::after{{content:"▾"}} details[open] summary::after{{content:"▴"}}
 table{{border-collapse:collapse;width:100%;font-size:14px}} td{{padding:9px 14px;border-top:1px solid var(--line)}}
 .dim{{color:var(--ink3)}} .count{{color:var(--ink3);font-weight:400}} b{{color:var(--ink)}}
</style></head><body>
<header><h1>Reversion Scanner <span style="color:#484f58;font-size:11px">azure · alerts-only</span></h1>
<span class="ts">{ts} ET · data thru {eod_date}</span></header>
<div class="kpis">{kpis}</div>
{fire_cards}
{rev_chart}
<details {"open" if watch else ""}><summary>Options watchlist <span class="count">{len(watch)}</span></summary><table>{watch_rows}</table></details>
</body></html>"""

# ---------------- SCAN CYCLE (every 2h intraday) ----------------
def run_scan():
    """Intraday, DASHBOARD-ONLY (no emails): detect sigma option fires + refresh
    the ETF reversion board, and rewrite the static dashboard. All actionable
    output goes out in the single 4:30a ET morning digest (see send_brief)."""
    closes, sb, _ = load_state()
    if closes is None or sb is None:
        return "state not seeded"
    latest, drift, spikes, eod_date = baselines(closes, sb)
    ts = now_et().strftime("%Y-%m-%d %H:%M")
    today = now_et().strftime("%Y-%m-%d")

    # sigma fire detection from spike history — alerts only, no paper positions
    fires, watch = [], []
    for und in sorted(set(spikes["und"])):
        if und not in latest.index:
            continue
        prev_close = latest.loc[und, "close"]; sig20 = latest.loc[und, "sig20"]
        if prev_close <= 3:                              # universe gate: price > $3
            continue
        n3, total, spot = poll_chain(und, prev_close, sig20)
        sh = n3 / total if total > 0 else 0.0
        dr = drift.get(und, np.nan)
        hist_r = spikes[spikes["und"] == und]
        prior_days = hist_r.iloc[0]["spike_days"] if len(hist_r) else []
        spike_today = (n3 >= SPIKE_NOTIONAL) and (sh >= SPIKE_SHARE)
        n_spikes = len(prior_days) + (1 if spike_today else 0)
        cum_n3 = (hist_r.iloc[0]["n3_recent"] if len(hist_r) else 0) + n3
        if (n_spikes >= 2 and cum_n3 >= FIRE_NOTIONAL and pd.notna(dr)
                and DRIFT_LO <= dr <= DRIFT_HI):
            fires.append({"und": und, "spot": spot, "sh": sh, "n3": n3, "drift": dr})
        else:
            watch.append({"und": und, "spikes": n_spikes,
                          "note": "spike TODAY" if spike_today else "awaiting 2nd spike"})

    # Intraday scan is DASHBOARD-ONLY now: no emails. Everything the user acts on
    # goes out in the single 4:30a ET morning digest (ETF reversion + microcap
    # flow). Sigma fires and the ETF board still render on the live dashboard.
    try:
        board = reversion_board()
    except Exception as e:
        board = []; logging.warning("reversion intraday scan: %s", e)

    write_dashboard_blob(render_dashboard(ts, fires, watch, eod_date, board))
    return f"scan ok (dashboard only): fires={len(fires)} watch={len(watch)}"

# ---------------- microcap flow signal ----------------
# Detector for the "WRAP/AARD/EVC signature" (research/microcap_flow_cases.md):
# a beaten-down small stock where one day's CALL notional runs >=15x its own
# 30-day baseline, concentrated in a single OTM strike, with a near-silent put
# tape. Case-control on this window (dose_response + the past-30d run) put the
# tight variant at ~27% +30%/10d vs a ~4% base (6.6x) with a ~9-day median lead;
# the differentiators were, in order: stock BEATEN DOWN (below its 20d high),
# strike DEEP OTM (K/S>=1.4), and a THIN name -- NOT the raw spike multiple.
# Caveat: validated in-sample on one 60-day window; ship as an experimental
# watchlist and let forward data confirm. Needs the top-strike columns that
# build_sigma_day now writes (top_strike/top_notional), so it fires only on days
# built after this deploy.
MICRO_PX_LO,  MICRO_PX_HI = 1.0, 10.0
MICRO_SPIKE_X   = 15        # day call notional >= 15x own 30d median
MICRO_CALL_MIN  = 8_000     # $ call notional floor
MICRO_TOPSHARE  = 0.55      # single strike holds >=55% of call $
MICRO_KS_LO, MICRO_KS_HI = 1.4, 3.0   # strike 1.4-3.0x spot (deep OTM)
MICRO_PUT_MAX   = 0.20      # put notional <= 20% of call notional
MICRO_BELOW_HI  = -0.15     # stock >=15% below its 20d high (beaten down)
MICRO_THIN_BASE = 1_500     # 30d median call notional < $1.5k (thin/readable name)
MICRO_DEDUP_DAYS = 14

def microcap_signals(closes, sb, prior):
    """Flag microcap flow signals for the latest day in `sb`. Pure function of the
    state frames (offline-testable). Returns a DataFrame; empty if none fire."""
    cols = ["date","und","close","strike","expiry","dte","ks","call_notional","x_base",
            "top_share","put_share","below_hi"]
    if sb is None or not {"top_strike","top_notional","put_notional"}.issubset(sb.columns):
        return pd.DataFrame(columns=cols)
    sbx = sb.sort_values(["und","date"]).copy()
    sbx["base30"] = sbx.groupby("und")["call_notional"].transform(
        lambda s: s.rolling(30, min_periods=8).median().shift(1))
    day = sbx["date"].max()
    t = sbx[sbx["date"] == day].copy()
    t = t[t["base30"].notna() & (t["base30"] < MICRO_THIN_BASE)]
    t["x_base"] = t["call_notional"] / t["base30"].clip(lower=500)
    t = t[(t["call_notional"] >= MICRO_CALL_MIN) & (t["x_base"] >= MICRO_SPIKE_X)]
    if t.empty:
        return pd.DataFrame(columns=cols)
    t["top_share"] = t["top_notional"] / t["call_notional"].replace(0, np.nan)
    t["put_share"] = t["put_notional"].fillna(0) / t["call_notional"].replace(0, np.nan)
    t = t[(t["top_share"] >= MICRO_TOPSHARE) & (t["put_share"] <= MICRO_PUT_MAX)]
    if t.empty:
        return pd.DataFrame(columns=cols)
    # spot + 20-day-high distance (beaten-down filter) from closes
    c = closes.sort_values(["und","date"])
    spot = c.groupby("und")["close"].last()
    hi20 = c.groupby("und")["close"].apply(lambda s: s.tail(20).max())
    t["close"] = t["und"].map(spot)
    t["below_hi"] = t["und"].map(spot / hi20 - 1)
    t["strike"] = t["top_strike"]
    t["ks"] = t["top_strike"] / t["close"]
    t["dte"] = t["top_dte"] if "top_dte" in t.columns else np.nan
    t = t[t["close"].between(MICRO_PX_LO, MICRO_PX_HI)
          & t["ks"].between(MICRO_KS_LO, MICRO_KS_HI)
          & (t["below_hi"] <= MICRO_BELOW_HI)]
    if prior is not None and len(prior):
        recent = prior[pd.to_datetime(prior["date"]) > day - pd.Timedelta(days=MICRO_DEDUP_DAYS)]
        t = t[~t["und"].isin(set(recent["und"]))]
    if t.empty:
        return pd.DataFrame(columns=cols)
    # expiration of the accumulated strike (dte is exact calendar days from the
    # trade date) — shows the buyer's horizon: near-dated = a bet on a move SOON.
    t["expiry"] = (t["date"] + pd.to_timedelta(t["dte"].fillna(0), unit="D")).dt.strftime("%Y-%m-%d")
    t["date"] = t["date"].dt.strftime("%Y-%m-%d")
    t["x_base"] = t["x_base"].round(0)
    out = t.reindex(columns=cols)
    for col, nd in [("close",2),("ks",2),("call_notional",0),("top_share",2),
                    ("put_share",2),("below_hi",2)]:
        out[col] = out[col].astype(float).round(nd)
    return out.sort_values("x_base", ascending=False)

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
    # top single CALL strike per root (for the microcap flow detector): the strike
    # holding the most call notional among <=90 DTE calls, plus its notional and DTE.
    # closes.csv carries spot; K/S and the concentration share are derived downstream.
    calls = df[is_call & (df["dte"] <= 90)]
    if len(calls):
        ks = calls.groupby(["root","strike"]).agg(sn=("notional","sum"), dte=("dte","min")).reset_index()
        top = ks.sort_values("sn", ascending=False).drop_duplicates("root").set_index("root")
        g["top_strike"]   = g["und"].map(top["strike"])
        g["top_notional"] = g["und"].map(top["sn"])
        g["top_dte"]      = g["und"].map(top["dte"])
    else:
        g["top_strike"] = np.nan; g["top_notional"] = 0.0; g["top_dte"] = np.nan
    return g

def run_eod():
    closes, sb, _ = load_state()
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

    # alerts-only: no paper positions, shadow experiments, or reversion logging.
    # run_eod just keeps closes.csv + sigma_bets_daily.csv fresh for the intraday
    # fire detection and the reversion board.
    write_csv_blob("closes.csv", closes)
    if sb is not None: write_csv_blob("sigma_bets_daily.csv", sb)

    # microcap flow signal — validated detector. Write today's flags (for the
    # digest) + append to the rolling log (history + 14d dedup source).
    micro_rows = []
    try:
        log = read_csv_blob("microcap_log.csv")
        new = microcap_signals(closes, sb, log)
        micro_rows = new.to_dict("records")
        write_csv_blob("microcap_signals.csv", new)          # current watchlist for the digest
        if len(new):
            log = pd.concat([log, new], ignore_index=True) if log is not None else new
            write_csv_blob("microcap_log.csv", log)
            msgs.append(f"microcap: {', '.join(new['und'])}")
    except Exception as e:
        logging.warning("microcap signal: %s", e)

    # send the consolidated morning digest now that fresh data + signals are in hand
    # (replaces the old standalone brief_morning timer). run_eod fires at both DST
    # candidate UTC times; send_brief's Central-hour gate lets exactly one land at
    # ~4:30a ET, and alert_once is a second dedup backstop.
    try:
        msgs.append(send_brief("am", micro=micro_rows))
    except Exception as e:
        logging.warning("morning digest: %s", e)
    return "eod ok: " + ("; ".join(msgs) if msgs else "nothing new")
