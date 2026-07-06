# Gamma — how fast delta changes

## What it is

**Gamma = the rate of change of delta per $1 move in the stock.**

If delta is the option's "gas pedal position," gamma is how fast that pedal shifts as the stock moves.

## The one-sentence intuition

**Gamma is the acceleration of your option's directional exposure.**

- High gamma → delta shifts fast when the stock moves → your gains/losses accelerate
- Low gamma → delta is stable → the option behaves like a fixed fraction of a share

## Formula (for reference)

$$\Gamma = \frac{\phi(d_1)}{S \cdot \sigma \cdot \sqrt{t}}$$

Where φ is the normal probability density. Gamma is always positive for long calls and long puts.

## Where gamma lives

Gamma is **highest at the money** and decays rapidly as you move ITM or OTM:

| Moneyness | Gamma | Interpretation |
|---|---|---|
| Deep ITM (delta 0.95) | Very low | Delta already near 1.0, can't rise much more |
| ITM (delta 0.75) | Moderate | Delta still has room to grow toward 1.0 |
| ATM (delta 0.50) | **Peak** | Delta most sensitive to stock moves |
| OTM (delta 0.25) | Moderate | Delta grows fastest as stock approaches strike |
| Deep OTM (delta 0.05) | Very low | Stock too far away to matter |

## Concrete example — QURE $27 call, 30 DTE

At S = $26.46: **gamma = 0.052**

This means for every $1 the stock moves, delta shifts by 0.052.

Delta at S = $26.46: **0.535**

**What delta does as stock climbs $1 at a time:**

| Stock | Approximate delta | Δdelta per $1 move |
|---|---|---|
| $26.46 | 0.535 | — |
| $27.46 | 0.585 (+0.05) | 0.050 |
| $28.46 | 0.640 (+0.055) | 0.055 |
| $29.46 | 0.700 (+0.060) | 0.060 |
| $30.46 | 0.755 (+0.055) | 0.055 |
| $32.46 | 0.855 (+0.10 over $2) | 0.050 avg |
| $35.46 | 0.925 | slower now |
| $40+ | 0.97+ | gamma near zero |

**Gamma is highest around the strike ($27), then fades as delta approaches 1.0.**

## Why gamma is the "friend of long calls"

Gamma is positive for anyone who buys options (long calls or long puts). This means:

**When the stock moves in your favor**, delta rises, so your NEXT dollar of gain is bigger than your last one. **Gains compound.**

**When the stock moves against you**, delta falls, so your NEXT dollar of loss is smaller than your last one. **Losses decelerate.**

This is the mathematical source of the classic "long option asymmetry" — you profit accelerating, you lose decelerating.

## The gamma payoff on QURE ($26.46 → $47)

Without gamma (fixed delta of 0.535 the whole way):
- Move: +$20.54 × 0.535 = **+$11.00 expected gain**

With gamma (delta ratchets 0.535 → 0.98):
- Actual gain: **+$17.16 (intrinsic $20 minus $2.84 cost)**

**The extra $6.16 is the gamma payoff.** It comes from delta rising as the stock climbed — each successive dollar was captured more aggressively than the last.

## Practical implication

**Gamma is what makes options magical when you're right, and it's why traders pay theta.** You're renting gamma exposure day by day.

- **Buy ATM if you want maximum gamma** — most acceleration per dollar move
- **Buy ITM if you want stable delta** — you sacrifice gamma for reliable "leveraged stock" behavior
- **Buy OTM if you want peak gamma AT the strike** — maximum acceleration when the pop crosses your strike

## The theta trade-off

**High gamma always comes with high theta.** They are two sides of the same coin.

An ATM option has the highest gamma AND the highest theta relative to price. You get maximum directional acceleration, but you pay maximum daily bleed. The market prices this trade fairly.

You can't have gamma without paying theta. The question is: does the stock move fast enough to make the acceleration worth the daily rent?

## Key rule

**Gamma is why you buy ATM for catalyst plays and ITM for slow-development plays.** If you expect a fast pop, you want the acceleration. If you expect a slow drift, you want stable delta and low theta.

For our signature (10-day hold, potential 40%+ pop): **ATM has the highest gamma payoff on the winners but the worst theta bleed on the losers.** ITM (delta 0.75) is a compromise that keeps most of the gamma while cutting theta impact.
