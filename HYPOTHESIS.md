# Options Signature Hypothesis — Sigma-Distance Jump Bets (v5)

**Status:** Active. Strongest signature found to date; survived in-regime slice validation.
**Last updated:** 2026-07-06
**Data:** 60 trading days (2026-04-01 → 2026-06-30), per-contract OPRA day aggregates, ~5,765 optionable US underlyings.

---

## The Hypothesis

> **Informed traders reveal themselves by paying real money for call options at strikes the stock's own volatility says are unreachable.**
>
> A call struck ≥2-3 standard deviations above spot (measured by the stock's own trailing 20-day realized vol, over the option's remaining life) with ≤60 days to expiry has no rational buyer among hedgers, income sellers, or momentum chasers. When a meaningful share of a stock's call **notional** (dollars, not contracts) concentrates in these "sigma-distance jump bets," someone is betting on a move that contradicts the stock's price history — and they are disproportionately right.

### Why this survived when everything else failed

Four prior hypothesis generations (volume spikes, book-shape, quiet-moderate flow, raw OTM share) all died the same death: the pattern was real among winners but common among non-winners (forward lift ceiling ~2×). The fix was using the **full option-pricing equation** instead of raw flow statistics:

1. **Sigma-distance, not raw moneyness** — 15% OTM is noise on a volatile stock, a 3σ event on a quiet one. Normalizing by realized vol makes the metric self-adjust across stocks AND across regimes. (This is the d2 term from Black-Scholes applied as a filter.)
2. **Notional dollars, not contract volume** — 1,000 contracts at $0.05 is $5K of lotto noise; weighting by premium paid measures conviction.
3. **Near expiry (≤60 DTE)** — a deep-OTM long-dated call is a cheap wish; a deep-OTM short-dated call is a bet on a violent move SOON.

---

## The Signature (formal)

Per contract per day, for calls only:
```
sigma_dist = ln(strike / spot) / (sig20_daily * sqrt(DTE * 252/365))
notional   = volume * option_close * 100
jump_bet   = (sigma_dist >= 2 or 3) AND (DTE <= 60)
```

Per (underlying, day), rolled over trailing 10 trading days:
```
sh_n2 = notional in 2σ+ jump bets / total call notional
sh_n3 = notional in 3σ+ jump bets / total call notional
```

**Fire conditions (tightest validated):**
```
FIRE = sh_n3 >= 0.20                       # 20%+ of call dollars at 3σ+ strikes
     AND n3_notional_10d >= $250k          # real money behind it
     AND drift_10d in [+5%, +30%]          # stock already quietly drifting up
Universe: close > $3, median option vol >= 100/day, call notional >= $100k/10d
Dedup: 21 days per ticker
```

---

## Validation Results (60-day window)

### Dose-response (the key evidence — monotonic, not threshold-lucky)

| 2σ notional share | n | p20 | lift | p40 | lift | mean fwd peak |
|---|---|---|---|---|---|---|
| ≥10% | 254 | 12.6% | 2.9× | 2.0% | 3.7× | +19.4% |
| ≥20% | 125 | 19.2% | 4.4× | 4.2% | 7.7× | +27.0% |
| ≥30% | 78 | 26.7% | 6.2× | 5.3% | 9.8× | +38.1% |
| ≥50% | 33 | 40.0% | 9.2× | 6.7% | 12.3× | +52.3% |

Base rates: p20 = 4.33%, p40 = 0.54% (108k eligible ticker-days).

### Tight combos

| Signature | n | p20 (lift) | p40 (lift) | mean peak |
|---|---|---|---|---|
| 3σ ≥20% + $250k + drift | 29 | **44.8% (10.3×)** | **13.8% (25.4×)** | **+64.6%** |
| 2σ ≥50% + $500k + drift | 15 | 60.0% (13.9×) | 13.3% (24.6×) | +90.8% |

Fire rate: ~1 per 2 trading days across the whole US optionable universe.

### In-regime slice test (last 20 trading days split at 10, in-slice base rates)

| Signature | Slice A (06-01→12) | Slice B (06-15→29) |
|---|---|---|
| 3σ ≥20% + $250k | p20 55.6% (**14.4×**) | p20 33.3% (**14.5×**) |
| + drift | p20 80.0% (20.8×) | p20 40.0% (17.4×) |

**The multiplier is stable across slices even as the regime cooled** (slice base rate fell 3.8% → 2.3%). The signal is regime-RELATIVE: it promises a multiple of the current market's base rate, not an absolute hit rate. Vol-normalization is likely why — the metric re-calibrates itself as volatility regimes shift.

---

## Supporting Findings (from prior rounds, retained as evidence)

1. **Flavor census of 50%+ pops (n=35):** 43% intrinsic-driven quiet accumulation (stock drifting up, controlled IV), 31% oversold bounces, 17% mixed, only 9% "flat stock + IV blowout." The drift filter comes from this: winners usually drift up before popping.
2. **Strike concentration is NOT a tell.** Winners' pre-pop flow was LESS concentrated across strikes than lookalike controls (top-3 share 0.44 vs 0.49). Informed flow spreads across strikes.
3. **OTM tilt was the breadcrumb.** Case-control showed 66% vs 30% OTM share; raw version only lifted 1.4× forward. Vol-normalizing and dollar-weighting it produced the current signature.
4. **Daily-aggregate features without the pricing equation cap at ~2× lift** (tested exhaustively: volume ratios, call-heavy day counts, IV proxies, cumulative delta, blocks, notional).

## Known Failure Modes / Caveats

1. **Small n.** The tight combo has 29 fires; p40 = 4 hits. The dose-response gradient and slice-stable multiplier argue against luck, but this is not yet proof.
2. **Residual tuning risk.** σ-thresholds (2/3) are principled; the $250k floor and drift band [+5%,+30%] are data-informed on this window.
3. **Single 60-day window** (Apr-Jun 2026). No out-of-window validation yet.
4. **fwd_peak uses intraday highs; execution would capture less.** Trailing-stop P&L not yet run on v5 fires.
5. **Options P&L math still binding:** naked OTM calls need >28-40% win rates to overcome IV crush + theta. At p20 = 45%, options structures become viable for the first time — but must be tested with realistic spreads.

## Next Steps

1. **Trailing-stop P&L** on the 29 v5 fires (stock; then deep-ITM call / spread variants).
2. **Daily live scan** — compute sh_n2/sh_n3 each evening, log fires forward. The only clean validation is forward observation.
3. **Payoff-matched instruments** — the fires literally state the expected move (the strike being bought); trade the instrument that matches it.
4. **Optional deepening:** intraday OPRA trades for aggressor-side/sweep confirmation on fires (raise precision further); event-calendar conditioning.

## Data Artifacts

`/Users/christian/dev/gold_forecast/opt_dayaggs/`:
- `sigma_bets_daily.csv` — per (und, day) sigma-distance jump-bet notionals (THE dataset)
- `moneyness_daily.csv` — per (und, day) moneyness-bucketed call volumes
- `otm_share_daily.csv` — per (und, day) OTM call share
- `build_sigma_bets.py` / `validate_sigma.py` — signature build + validation
- `regime_rolling_jump.py` — in-regime slice testing framework
- `trades_features.csv` — original 4-greek daily aggregates (276k rows)
- `2026-*.csv.gz` — per-contract OPRA day aggregates (62 days, source of truth)
- `pop_flavors.csv`, `strike_concentration_results.csv` — supporting studies

**Data source:** api.massive.com flat files (options day aggregates per contract) + grouped stock bars.

---

## Rejected Prior Hypotheses (v1-v4, for the record)

- **v1 volume-spike** (≥5× baseline volume → pop): 0.6% precision at scale. Dead.
- **v2 book-shape** (cum delta + notional + theta burn + momentum): 5.9% @ 40%+, in-sample only. Dead.
- **v3 quiet-moderate** (sustained 1.5-4× vol, call-heavy, flat stock): p10 46.9% in-sample; lift collapsed to 1.8× at full-universe base-rate test. Dead.
- **v4 branch signatures / F2 accumulation** (drift + call-heavy persistence + controlled IV): case-control separations real; forward lift ≤2.3×. Dead as standalone; drift component survives inside v5.

The lesson across all five generations: **features describing HOW MUCH flow there is cap at ~2× lift; the feature describing WHAT THE FLOW BELIEVES (strike choice vs the stock's own vol, weighted by dollars) reached 10-25×.**
