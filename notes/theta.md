# Theta — the daily rent

## What it is

**Theta = the dollars an option loses each day just from time passing, holding everything else constant.**

For anyone who buys options, theta is negative — the option shrinks in value every day the stock does nothing.

## The one-sentence intuition

**Theta is the daily rent you pay to keep your gamma exposure alive.**

The market charges you every day for the "optionality" of maybe having a big move happen. If no big move comes, you paid rent for nothing.

## Formula (approximation for ATM)

$$\Theta_{ATM} \approx -\frac{S \cdot \sigma \cdot \phi(d_1)}{2\sqrt{t}}$$

Note the **1/√t** factor — theta accelerates as expiration approaches.

## Where theta lives

Theta is **worst on ATM options** and **worst on short-dated options**:

| Moneyness | Theta magnitude | Why |
|---|---|---|
| Deep ITM | Low | Almost pure intrinsic; extrinsic tiny, so decay is tiny |
| ITM | Moderate | Some extrinsic to bleed |
| **ATM** | **Peak** | Maximum extrinsic value, all bleeds |
| OTM | Moderate | Extrinsic exists but is smaller |
| Deep OTM | Low in $ terms | Extrinsic is tiny, but percent-wise brutal |

| DTE | Daily theta as % of price | Notes |
|---|---|---|
| 90 days | ~0.5% / day | Slow bleed |
| 60 days | ~0.8% / day | Manageable |
| 30 days | ~1.5% / day | Meaningful bleed |
| 14 days | ~3% / day | Fast decay |
| 7 days | ~5% / day | Vicious — avoid |
| 1 day | ~15% / day | Only intraday plays |

## Concrete example — QURE $27 call, 30 DTE

At entry: S = $26.46, K = $27, IV = 100%, cost = $2.84

**Daily theta ≈ −$0.05 per contract** (annualized theta ≈ −$18.35)

**That's −1.77% of the option's value bleeding every day.**

## What theta does to a 10-day hold with no stock movement

Starting call value: $2.84

| Day | Theta drag (approx) | Remaining value |
|---|---|---|
| 0 | — | $2.84 |
| 3 | -$0.15 | $2.69 |
| 5 | -$0.28 | $2.55 |
| 7 | -$0.42 | $2.42 |
| 10 | -$0.65 | $2.19 |

**Over 10 days of the stock doing nothing, the call loses ~23% just to theta.**

And theta accelerates in the second half — the last 3 days lose more per day than the first 3.

## Why theta accelerates near expiration

Because of the **1/√t** term. Options with less time have less "hope" to burn — but they burn it faster.

Day-over-day theta as expiration approaches:

| Days to expiration | Daily theta ($) | Daily theta (%) |
|---|---|---|
| 30 | -$0.050 | -1.8% |
| 20 | -$0.061 | -2.2% |
| 14 | -$0.073 | -2.6% |
| 7 | -$0.104 | -3.7% |
| 3 | -$0.158 | -5.6% |
| 1 | -$0.274 | -9.7% |

**Never hold ATM options into the last week unless you're 100% expecting a catalyst that day.** Theta is a runaway train in the final stretch.

## Theta on our real strategy

For our 10-day hold on Branch A fires:

**Scenario A — winner (QURE-style +85% pop by day 7):**
- Theta bill over 7 days: -$0.35 (12% of premium)
- Delta+gamma gain: +$17
- Net: still massive win, theta barely mattered

**Scenario B — loser (CPRI-style, stock drifts flat to -8% over 10 days):**
- Theta bill: -$0.65 (23% of premium)
- Delta loss (stock -$1.75 × 0.5): -$0.88
- IV crush (no catalyst → -20% IV): -$0.60
- **Total: -$2.13 = -75% on the option**

**Note:** the option was already OTM by exit. Theta and IV crush did more damage than the actual stock move.

## Why theta is worst around the strike

An ATM option is 100% extrinsic — the theta bleeds ALL of it if you hold to expiration. A deep ITM option is mostly intrinsic — theta only bleeds the small extrinsic sliver.

Same 30-DTE QURE call, S = $26.46:

| Strike | Cost | Extrinsic | Daily theta % of cost |
|---|---|---|---|
| $20 (deep ITM) | $7.09 | ~$0.63 | -0.4% |
| $23 (ITM) | $4.94 | ~$1.48 | -0.8% |
| **$27 (ATM)** | **$2.84** | **$2.84** | **-1.8%** |
| $30 (OTM) | $1.80 | $1.80 | -1.9% |
| $35 (far OTM) | $0.80 | $0.80 | -2.5% |

**Deep ITM options have the lowest theta as a percentage of premium.** That's why they're the "safer leverage" structure — you're not paying much for extrinsic time value that will bleed away.

## Why theta accelerates the closer you get to the money too

The strike is where "hope" concentrates. If you're deep ITM, most of your value is already realized (intrinsic). If you're deep OTM, there's not much hope to burn. **The middle is where the market prices the most uncertainty**, so that's where the daily bleed is worst.

## Practical rules

1. **Buy deeper expiration (60-90 DTE) whenever possible.** Slower bleed lets your signal develop.
2. **Never buy short-dated (< 14 DTE) unless you're sure of the timing.** Theta is a killer.
3. **Prefer deeper ITM if you're not confident about a fast catalyst.** Less extrinsic = less theta.
4. **Exit as soon as your thesis plays out.** Every extra day of holding is rent paid.

## Key rule

**Theta is the price of admission for gamma.** You want gamma acceleration on the winners, but you pay theta on every day-of-nothing. The math only works if the winners are big enough to cover the theta bill on all the losers.

**For our signal (30% precision, 10-day hold, ATM calls):** theta chews about 15-20% off every fire's premium during the hold. Winners at +200-600% easily absorb that. Losers get pushed from -60% to -80% by theta on top of the stock loss. This is why deeper ITM (delta 0.75+) is the better structure for our precision profile — cuts theta impact in half while keeping most of the delta capture.
