# ⚡ Sigma-Bet Scanner

**Detecting informed options positioning by measuring what the flow *believes*, not how loud it is.**

An options-tape scanner that looks for call buying at strikes the stock's own volatility says are
near-impossible — "sigma-distance jump bets." When real money concentrates in those strikes across
consecutive days on a quietly drifting stock, someone may know something. This repo contains the
research trail, the signal engine, and a fully automated Azure deployment with email alerts and a
live dashboard.

> **Disclaimer:** This is a research project and paper-trading experiment, not financial advice.
> The backtest covers one 60-day window dominated by a single sector theme, with small sample sizes.
> Nothing here is a proven edge. Do your own work.

## The core idea

Most options-flow analysis measures **how much** flow there is (volume spikes, put/call ratios,
block counts). Across four generations of hypotheses, every such feature capped out at ~2× lift
over base rates — real but untradeable.

The feature that worked measures **what the flow believes**:

```
sigma_dist = ln(strike / spot) / (σ_daily,20d × √(DTE × 252/365))
```

A call struck ≥3 standard deviations above spot (by the stock's *own* realized vol, over the
option's remaining life) with ≤60 DTE has no rational buyer among hedgers, income sellers, or
momentum chasers. When ≥20-30% of a name's call **notional** (dollars, not contracts) lands in
those strikes, day after day, the market maker's history-based pricing is being bet against by
someone whose information contradicts the price history.

## The signal (v5.1 "mini-campaign")

- **Spike day:** ≥$100k notional AND ≥20% of call dollars in 3σ+ strikes (DTE ≤ 60)
- **Fire:** 2 spike days within 3 trading days + ≥$250k cumulative + stock drifting +5–30% over 10d
- **Dedup:** 21 days per "family" (leveraged ETF wrappers map to their underlying)
- **Entry window:** fire day + 2 trading days — void immediately if the 3σ flow disappears
- **Exits:** sell at close of 12th day held; stop only on a daily *close* ≤ −15%
  (trailing stops and profit targets all tested worse — the winners run)

Key empirical findings from the research trail (see `HYPOTHESIS.md` and `research/`):

| Finding | Result |
|---|---|
| Dose-response on sigma-distance | Monotonic: deeper OTM share → higher forward pop rate |
| Regime-split stability | Lift multiplier held ~14× across two disjoint 10-day slices |
| Whale-strike copying | Buying the whales' exact 3σ strikes = **worst** variant (already repriced) |
| Follower's instrument | Near-ATM (0.5–1σ) calls or plain stock capture the move best |
| Flow-fade death signal | Entries after 3σ flow went to zero: 0-for-5 |
| Stale-window failure mode | 10d-rolling rule bought distribution tops after whales exited — replaced by the 2-in-3 rule |

## Architecture

```
                 ┌────────────────────────────────────────────┐
  nightly 5a ET  │ EOD job: pull OPRA day-agg flat file (S3), │
                 │ stock closes → build sigma-bet features →  │
                 │ manage positions → log shadow-rule fires   │
                 └──────────────────┬─────────────────────────┘
                                    │ blob state (CSVs)
                 ┌──────────────────▼─────────────────────────┐
 4×/day mkt hrs  │ Scan job: poll option-chain snapshots for  │
                 │ the watchlist → detect 2nd spike intraday  │
                 │ → FIRE → email alert + static dashboard    │
                 └────────────────────────────────────────────┘
```

- **`cloud/`** — Azure Functions app (Flex Consumption, Python 3.11): two timer triggers + manual HTTP kick
- **`local/`** — the same engine as local dev tools (`live_scan.py` 15-min loop + HTML dashboard, `daily_scan.py` EOD)
- **`research/`** — the validation trail: feature builders, base-rate tests, regime splits, P&L simulations, options-ladder tests
- **`notes/`** — plain-language primers on delta/gamma/theta/vega written during the research
- **`HYPOTHESIS.md`** — the full hypothesis document (v5), including the four rejected predecessors

An A/B experiment runs continuously: the live v5.1 rule vs. the older v5.0 10-day rule as a
silent "shadow" — both paper-traded at $100/lot, so forward data (not backtests) decides.

## Deploy your own

Requirements: an options-data plan with flat files + chain snapshots (Polygon/Massive-style API),
Azure subscription, Azure Communication Services for email.

```bash
cd cloud
az group create -n rg-options -l eastus
az storage account create -n <yourstorage> -g rg-options --sku Standard_LRS
az storage blob service-properties update --account-name <yourstorage> --static-website --index-document index.html
az functionapp create -g rg-options -n <yourapp> --storage-account <yourstorage> \
  --flexconsumption-location eastus --runtime python --runtime-version 3.11
func azure functionapp publish <yourapp> --python
```

App settings (secrets — never commit these):

| Setting | Purpose |
|---|---|
| `MASSIVE_API_KEY` | market data API key |
| `POLY_S3_KEY` / `POLY_S3_SECRET` | flat-file S3 credentials |
| `ACS_CONNECTION_STRING` | Azure Communication Services (email) |
| `EMAIL_SENDER_DOMAIN` / `EMAIL_SENDER_USERNAME` | verified sender domain + username |
| `EMAIL_TO` | alert recipient |

Seed the `state` blob container with `closes.csv` (und,date,close) and an empty
`fires_log.csv`, then let the nightly job build history forward.

CI/CD: `.github/workflows/deploy.yml` deploys `cloud/` on push using a publish-profile secret
(`AZURE_FUNCTIONAPP_PUBLISH_PROFILE`, `AZURE_FUNCTIONAPP_NAME`).

## License

MIT — see `LICENSE`.
