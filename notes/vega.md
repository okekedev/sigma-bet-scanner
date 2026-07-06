# Vega — the IV sensitivity

## What it is

**Vega = the dollars an option gains for every 1 percentage point rise in implied volatility (IV).**

For long options (calls or puts), vega is positive — higher IV = more valuable option, lower IV = less valuable.

## The one-sentence intuition

**Vega is the market's price for "how much movement is expected."**

When IV rises, the market is saying "wider range of outcomes is priced in." That makes options more valuable because there's more optionality. When IV falls, options lose value even if the stock does nothing.

## What is IV, really

**Implied volatility (IV)** is the market's forecast of how volatile the stock will be over the option's life. It's expressed as an annualized standard deviation of returns.

- IV = 30% → market expects the stock to trade in a range of roughly ±30% over the next year
- IV = 100% → market expects ±100% range (typical for pre-catalyst biotech)
- IV = 200% → market expects massive movement (rare — usually right before FDA readouts)

**IV is priced by the market, not calculated from the stock.** It's the number that makes Black-Scholes match the actual option's market price.

## Formula

$$V = S \cdot \phi(d_1) \cdot \sqrt{t}$$

Vega grows with **√t** — long-dated options have MUCH more vega than short-dated ones.

## Where vega lives

Vega is highest at ATM and for long-dated options:

| Moneyness | Vega magnitude |
|---|---|
| Deep ITM | Low |
| ATM | **Peak** |
| Deep OTM | Low |

| DTE | Relative vega |
|---|---|
| 7 days | 0.3× baseline |
| 30 days | 1.0× baseline |
| 90 days | 1.7× baseline |
| 180 days | 2.5× baseline |
| 365 days | 3.5× baseline |

**LEAPS have massive vega. Weekly options barely notice IV changes.**

## Concrete example — QURE $27 call, 30 DTE, IV = 100%

Vega = 3.015 (per 1.0 change in σ)

**Per 1% change in IV: option value changes by $0.030.**

If QURE's IV suddenly rises from 100% → 120%:
- +20 IV points × $0.030 = **+$0.60 gain** (call was $2.84, now ~$3.44 = +21%)

If QURE's IV crashes from 100% → 50% (catalyst resolved):
- −50 IV points × $0.030 = **−$1.50 loss** (call drops from $2.84 → $1.34 = **−53%**)

**The stock didn't move. IV alone did that damage.**

## IV crush — the silent killer

The classic scenario:

1. Pre-catalyst (earnings, FDA, M&A): IV is elevated because uncertainty is priced in
2. You buy the call at high IV, paying a premium
3. Catalyst resolves — outcome is known
4. IV collapses because uncertainty is gone
5. **Your call loses vega value even if the stock moves in your favor**

**Real example on QURE (stylized):**

- Day 0: QURE = $26.46, IV = 100%, $27 call = $2.84
- Day 5: Phase 2 data drops. QURE spikes to $47, but IV crushes 100% → 40%.
  - Intrinsic (delta + gamma): now $20 — **massive win**
  - But vega loss: -60 × $0.030 = -$1.80
  - Total value: ~$18.50 instead of ~$20.30 → **still +550% on a $2.84 cost**
  - Vega loss barely mattered because intrinsic dominated

**Now the scenario where vega DOES kill you:**

- Day 0: QURE = $26.46, IV = 100%, $30 OTM call = $1.80
- Day 5: Phase 2 data is mediocre. QURE moves modestly to $28. IV crashes to 40%.
  - Intrinsic: still $0 (option is still OTM)
  - Delta gain: 0.35 × $1.54 = $0.54
  - Vega loss: -60 × $0.021 = -$1.26
  - Total value: ~$1.10 → **-39% despite the stock going up**

**OTM calls on catalyst events are the classic "right direction, wrong instrument" trap.**

## The three regimes of IV

1. **IV rising (before a catalyst)**: Vega is your friend. You're gaining value as the market prices in more uncertainty. This is why some traders buy calls 2-4 weeks before earnings.

2. **IV stable**: Vega does nothing. Only delta/gamma/theta matter.

3. **IV falling (after a catalyst OR general market calming)**: Vega bleeds. Even if your direction is right, you can lose money.

## Why LEAPS reduce vega risk

**LEAPS (6-12 month expiries) have HIGHER absolute vega**, but the impact of a single catalyst IV crush is dampened because:
- Only part of the option's life is affected by the specific event
- IV mean-reverts over longer time frames
- More time for delta/gamma to compensate

**Short-dated (weekly, 30-DTE) options on catalyst events get hit hardest by IV crush**, because the crush is a one-time event that takes out a big chunk of the option's life.

## Vega on our strategy

For our Branch A signal (mid-cap catalyst candidates):

**Buy timing matters a lot for vega.**
- Buy 2-3 weeks BEFORE an expected catalyst → IV usually rising → vega helps you
- Buy the day of a catalyst → IV at peak → you'll eat the crush if you hold through

Our signal fires DURING the accumulation phase (before the catalyst) — that's the good side of vega.

**But if the catalyst hits and IV crushes:**
- If we're deep ITM by then: vega loss is small relative to intrinsic gain
- If we're still OTM: vega loss can wipe out delta gains

**This is another reason to prefer ITM (delta 0.75+) over OTM for our precision profile:** ITM options have proportionally less vega exposure. When the pop happens and IV crushes, the intrinsic gains dwarf the vega loss.

## Practical rules

1. **Never buy calls the day BEFORE an announced catalyst.** IV is at peak, you'll pay maximum vega premium and then eat the crush.
2. **Buy calls 1-3 weeks before expected catalysts when IV is still building.** You get vega tailwind AND delta capture.
3. **Prefer ITM strikes when IV is high** — less exposure to the crush.
4. **Prefer LEAPS for long-hold plays where catalyst timing is uncertain** — vega absolute is higher but crush impact is diluted.

## Key rule

**Vega is the greek most people ignore — until IV crush wipes out their winning trade.** You can be right on direction, right on timing, and still lose money because you paid too much for uncertainty premium that then evaporated.

**The tell for a bad vega setup: high IV + long time to catalyst + short-dated option. That's how retail traders pay the most vega premium for the least vega utility.**

## Summary of the 4 greeks together

| Greek | What it does to a long call |
|---|---|
| **Delta** | Gain on stock rises (positive, 0-1) |
| **Gamma** | Delta accelerates as stock moves (positive, friend) |
| **Theta** | Bleeds daily from time passing (negative, enemy) |
| **Vega** | Gains on IV rises, loses on IV drops (positive, ambivalent) |

**Delta and Gamma are what you WANT.**
**Theta is the rent you PAY to have them.**
**Vega is the wild card — can help or hurt depending on regime.**

**The whole art of options trading is choosing structures where you get the delta/gamma you want without paying too much theta and without being blindsided by vega.**
