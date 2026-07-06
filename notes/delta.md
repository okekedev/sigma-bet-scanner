# Delta — the "share equivalence" of an option

## What it is

**Delta = how much your option's price moves for every $1 the underlying stock moves.**

It's a number between 0 and 1 for calls, and between −1 and 0 for puts.

## The one-sentence intuition

**Delta is the fraction of a share you effectively own right now.**

- Delta 0.15 → your call moves like you own 15% of a share
- Delta 0.50 → you effectively own half a share
- Delta 1.00 → you effectively own a whole share

## How delta relates to moneyness

For a call option:

| State | Stock vs. Strike | Delta range | Meaning |
|---|---|---|---|
| Deep ITM | Stock way above strike | 0.85 – 1.00 | Behaves like leveraged stock |
| ITM | Stock above strike | 0.55 – 0.85 | Moves mostly with the stock |
| ATM | Stock at strike | ≈ 0.50 | Coin flip on where it ends up |
| OTM | Stock below strike | 0.20 – 0.50 | Cheap, needs move to matter |
| Deep OTM | Stock way below strike | 0.00 – 0.20 | Nearly worthless |

## Delta is NOT fixed at purchase

**When you buy an option, you choose your strike, which sets your STARTING delta.** But delta changes constantly as:

1. **Stock price moves** (dominant effect — this is gamma)
2. **Time passes** (small drift — charm)
3. **IV changes** (small — vanna)

The stock price effect is what matters most for trading.

## Concrete example — QURE $27 call, 30 DTE

Starting position: stock at $26.46, ATM $27 call, delta = 0.535.

As QURE moves, delta shifts:

| QURE stock price | Delta | Dollars gained per $1 stock move |
|---|---|---|
| $20 | 0.15 | $0.15 |
| $23 | 0.30 | $0.30 |
| $26.46 (entry) | 0.535 | $0.535 |
| $30 | 0.72 | $0.72 |
| $35 | 0.87 | $0.87 |
| $47 (post-pop) | 0.98 | $0.98 |

## Why this matters — the gamma acceleration

As the stock moves in your favor, delta rises. As delta rises, the next dollar of stock movement gains you MORE. This is why calls are asymmetric:

**Fixed-delta math on QURE $26.46 → $47:**
- Move: +$20.54
- Delta 0.535 (constant) × $20.54 = **+$11.00 gain**

**Actual delta-ratcheting math:**
- Each $1 stock move captures a progressively higher fraction
- End state: option is deep ITM, worth ~$20 intrinsic
- Cost was $2.84 → actual gain **≈ +$17.16 (+604%)**

**The extra $6+ came from delta rising as the stock climbed.**

## Losses decelerate too (the good side)

As the stock moves against you, delta falls. Each additional dollar of stock loss hurts LESS.

- QURE drops from $26.46 → $22: option value falls from $2.84 → ~$0.30
- Stock lost 17%, option lost 89% — but the LAST dollar of loss barely mattered because delta was already low

**This asymmetry (accelerating gains, decelerating losses) is the built-in feature of long calls.**

## Choosing your delta at purchase

Different strikes = different starting deltas = different tradeoffs:

**High delta (0.75-0.95, deep ITM):**
- Behaves like leveraged stock
- Small theta and IV crush impact
- Expensive per contract
- "Cash out anytime with most gains banked"

**Medium delta (0.40-0.60, near ATM):**
- Highest gamma (delta accelerates most rapidly)
- Highest theta bleed
- Balanced cost and payoff
- Sweet spot for directional bets

**Low delta (0.15-0.35, OTM):**
- Lottery-ticket profile
- Cheap per contract
- Massive returns IF stock moves to/through strike
- Worthless most of the time (rapid theta decay)

## Key rule

**Delta is your "gas pedal" — but it only helps if the stock moves in your favor within the option's time window.** Everything else in options math (theta, vega, IV crush) is about how much you PAY to keep that gas pedal engaged while you wait.
