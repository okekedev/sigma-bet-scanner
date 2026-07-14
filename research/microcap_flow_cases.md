# Microcap options-flow case file

**Status:** Observation log — deliberately pre-hypothesis. This documents individual
cases at the per-contract level so patterns can be *seen* before any rule is written.
Do not promote anything here to a signal without the forward evidence from the
`microcap_log.csv` shadow logger (cloud/scanner/core.py) and a survivorship-honest test.

**Method:** per-contract OPRA day-aggregate flat files (S3), spot/realized-vol from the
scanner's `closes.csv` blob, IV backed out from contract closes via Black-Scholes.
Day aggregates carry **no buy/sell side** — "accumulation" is inferred from
one-sidedness and persistence, never proven. All cases were found by starting from
stocks that already popped (+30% close-over-close), so this file has **survivorship
bias by construction**; the counter-cases below only partially offset it.

**Context that motivated this file:** of the 17 movers +28%+ on 2026-07-09/10, the
8 micro-float Asian listings had **zero** listed options — no chains, no prints, ever.
Options-flow reading is structurally blind to that group. The cases below are the
poppers that *did* have a tape.

---

## Case 1 — WRAP (Wrap Technologies, $137M) · +48% on 2026-07-09, +4% next day

Stock drifted $1.12 → $1.68 (+50%) over 8 sessions while call flow ramped; puts stayed
near zero the whole time (405 calls : 2 puts across the two quiet days before the pop).

| date | spot | call vol | put vol | call $ | key prints |
|---|---|---|---|---|---|
| 6/26 | 1.18 | 304 | 10 | $8.0k | $2 Jul-17 calls |
| 6/29 | 1.32 | 516 | 0 | $9.7k | |
| 7/01 | 1.48 | 1,574 | 2 | $19.7k | 1,324× $2 Jul-17 @ $0.08 |
| 7/02 | 1.41 | 1,056 | 0 | $17.6k | $2s across Aug/Dec, $4 Dec |
| 7/06 | 1.68 | 1,466 | 23 | $25.7k | 940× $2 Jul-17 @ $0.11, strikes to $7 |
| 7/07 | 1.66 | 231 | 2 | $4.1k | 133× $2 Jul-17 @ $0.06 + deep-ITM $1s |
| 7/08 | 1.59 | 174 | 0 | $3.1k | 133× $2 Jul-17 @ $0.03 + $1 Jan-27 |
| 7/09 | 2.36 | 22,441 | 1,281 | $1.39M | pop; $2 Jul-17 closed $0.50 |

Notables: same strike ($2 Jul-17, ~1.3× spot) rebought repeatedly, *including into the
7/07-08 dip at falling prices* (identical 133-lot clips both days). Relative to its own
baseline (median $962/day call notional Apr-Jun, options traded every day), 7/06 was
~27× median. Total pre-pop outlay ~$70k over two weeks. The $2 Jul-17s bought at
$0.03–0.06 printed $0.50 on pop day (8–16×). Stock closed pop-week at $2.46 — at the
whale's strike. Sigma-distance never exceeded ~1.2σ (realized vol 84–114% annualized),
so the v5 sigma signal correctly classified nothing here as an "impossible" strike.

## Case 2 — AARD (Aardvark Therapeutics, biotech, $166M) · +52% on 2026-07-10

Essentially one buyer, one contract, two clips, a week apart:

| date | spot | prints |
|---|---|---|
| 7/02 | 5.70 | **424× $7.5 Aug-21 calls @ $0.25–0.30** ($12.7k, 13 trades) — whole call side of the day |
| 7/03–7/08 | 5.99→5.28 | near-silence ($0–2.5k/day) |
| 7/09 | 5.00 | **773× $7.5 Aug-21 calls @ $0.30** ($23.2k, 19 trades); 775 calls vs 6 puts on the day |
| 7/10 | 7.60 | pop; same contract trades 1,500 in 16 blocks, opens $2.60 vs ~$0.30 basis |

Notables: the 7/09 add came with the stock *down 12%* and a week of theta gone, at an
unchanged premium — i.e., IV was bid ~96%→136% while spot fell. ~$34k total premium →
~$260k at pop-day open (7–8×). Stock popped to $7.60; the accumulated strike was $7.50.
Spike days were 7/02 and 7/09 — **five trading days apart**, so a 2-spikes-in-3-days
persistence rule misses this case entirely.

## Case 3 — EVC (Entravision, ~$3.80 stock) · +93% on 2026-05-06

The same shape at 5–10× the dollar scale, in institutional-size blocks:

| date | spot | prints |
|---|---|---|
| 4/24 | 3.75 | **1,000× $5 Nov calls @ $0.60 in 2 trades** ($60k) — the entire day |
| 4/28 | 3.84 | **1,999× $5 Aug calls @ $0.50 in 5 trades** ($100k) |
| 4/29–5/05 | 3.74→3.98 | near-silence (≤$2.4k/day) |
| 5/06 | 7.69 | pop; $5 Aug prints $2.90 (~5.8×), $5 Nov $3.46 (~5.8×) |

Notables: ~$160k premium in two single-strike blocks (K/S ≈ 1.3), then a week of
silence, then the pop. Despite $100k+ in one day, the v5 sigma spike rule saw nothing:
n3 share was ~0.01 because a 1.3× strike on this stock's vol is nowhere near 3σ. The
$100k absolute floor was met; the "impossible strike" condition was not.

## Case 4 (counter) — BWEN (Broadwind) · +117% on 2026-05-12

Pre-pop tape: ambiguous dribbles, no concentration. Largest days: 406× $5 Oct calls
(1.9× spot, $10k) on 4/27 — two weeks early, wrong-ish strike; scattered ~ATM $2.5s on
5/04 ($5.4k); the *day before* the pop, just 137× $2.5 Jun @ $0.13 ($1.8k). A +117%
pop with no distinct footprint. **Big pops happen without pre-positioning.**

## Case 5 (counter) — VERU · +88% on 2026-06-04

Pre-pop tape: effectively dead — $0.1–3.1k/day, mostly 1–2-contract prints. The only
curiosity: 150× $7 Oct calls (2.9× spot!) in a single $1.5k print three days before.
Too small to distinguish from noise ex-ante. Day before the pop: $363 of calls.
**Second counter-case: no readable footprint.**

## Case 6 (artifact) — SVC · "+403%" on 2026-07-06 is a REVERSE SPLIT, not a pop

SVC executed a 5:1 reverse split (execution 2026-07-07, confirmed via the reference
splits API). The scanner's `closes.csv` is stitched from as-of-fetch grouped bars and
is **not retro-adjusted**, so the split appears as a fake +403% day. The giveaway in
the options tape: "pop day" $2 calls trading at $0.08 with spot "at $8.70" — impossible
for a standard contract.

**Data lesson that outlives this case:** any pop/backtest stat computed from
`closes.csv` (including the microcap relative-flow backtest and `fwd_max` numbers in
this research thread) can be contaminated by reverse splits masquerading as gains.
Check `/v3/reference/splits` before trusting any individual pop, and treat the
backtest hit-rates as upper-bound-noisy until split-filtered.
(Amusingly, SVC's *pre-split* tape was still WRAP-shaped: 1,981× / 1,455× / 960× /
760× of the $2 strike on 6/25–7/02 on a flat $1.70 stock. Whether that anticipated
the split-adjacent move is unknowable from day aggregates.)

---

## Cross-case observations (facts, not rules)

- **Three positives (WRAP, AARD, EVC) share:** a single OTM strike at ~1.3–1.5× spot,
  30–120 DTE, bought in repeated sized clips across multiple days *including down
  days*, with a near-empty put tape, followed by ≥+48% pops; in two of three the pop
  landed almost exactly at the accumulated strike ($7.50→$7.60; $2→$2.46).
- **Dollar scale varies 20×** across positives ($34k → $160k premium): a fixed
  notional floor either misses the small ones or admits noise. Relative-to-own-baseline
  is what all three have in common (10–200× median).
- **Two counters (BWEN, VERU):** comparable pops, no readable footprint. Whatever this
  pattern is, it is not *necessary* for a pop.
- **Timing between clips varied:** WRAP consecutive days; AARD 5 trading days apart;
  EVC 2 days apart then 6 quiet days before the pop.
- **The sigma (v5) framework is structurally blind here by design:** on 80–115%
  realized vol, 1.3–1.5× strikes are ~1σ. These cases and the v5 winners do not overlap.
- Broad backtests of the *generic* relative-flow version of this capped at ~2–3× lift
  (see PR #10 description); the per-contract features above (single-strike
  concentration, clips-into-weakness, put silence) are exactly what those aggregates
  can't see — and put data only exists in the blob from 2026-07-06 onward.

## Prospective test — the survivorship-honest answer (open question #1)

We built the pattern as a **forward-looking scanner** and ran it over every trading day
in the window, measuring what happened next regardless of whether a pop followed. This
is the test that matters, because every case above was found by starting from a pop.

**Method:** aggregate all 53 daily OPRA tapes into per-(ticker,day) strike concentration
(scripts `tape_scan.py` / `tape_assemble.py`). A **clip day** = call notional ≥ $5k, the
single top strike holds ≥60% of call dollars, K/S in [1.1, 2.2], DTE ≥ 7, puts ≤ 25% of
call dollars. An **event** = 2+ clip days on the *same strike* within 10 trading days with
< 25% price move between them (the WRAP/EVC fingerprint, stated ex-ante). Forward = max
close over the next 10/15 trading days. Control = all optionable $1–25 ticker-days.
Split-decontaminated on both sides (494 splits in window, 334 of them reverse — see SVC).

| Group | n | +20% in 10d | +40% in 10d | touched the strike (15d) |
|---|---|---|---|---|
| Control (optionable $1–25) | 92,999 | 11.7% | 3.1% | — |
| **Clip-accumulation events** | 505 | **14.9% (1.3×)** | **3.6% (1.1×)** | 15.8% |
| Events + relative-flow leg | 145 | 11.7% (1.0×) | 2.1% (0.7×) | 13.1% |

**Conclusion: the pattern does not predict pops.** Prospectively it is ~1.3× lift at
best, and *adding* the relative-flow leg that looked so clean on WRAP pushes it to 1.0×
(no edge). WRAP and EVC do surface as events (sanity check passed) — but they sit in a
crowd of 505 that collectively behaves like the control. **The three positives were
survivorship bias.** We found a shape that is common among poppers and equally common
among non-poppers — the exact failure mode HYPOTHESIS.md documents for v1–v4, now
reproduced a fifth time on a per-contract feature.

The "strike ≈ eventual price" regularity (open q#3) also dissolves: events touch their
accumulated strike within 15 days only 15.8% of the time. WRAP/AARD/EVC landing on their
strikes was 2-of-3 coincidence, not a tell.

This is a *clean negative result*, and a valuable one: it says stop building this
particular detector. The single genuinely-untested residual is **put silence as a
standalone discriminator** — but the blob only has put data from 2026-07-06, so the
whole window here is call-only and can't isolate it. That is exactly and only what the
PR #10 shadow logger is positioned to answer, forward, without survivorship bias.

## Open questions that remain

1. ~~How often does the tape occur without a pop?~~ **Answered: constantly. No edge.**
2. Do the clips print at ask (aggressor buys)? Needs intraday OPRA trades, not day aggs.
   (Only worth pursuing if the forward put-silence signal shows life first.)
3. ~~Is strike ≈ eventual price a real regularity?~~ **Answered: no, 15.8% touch rate.**
4. What were the catalysts on the *specific* pop days (WRAP/AARD/EVC)? Even as anecdotes
   these were real moves; understanding the trigger is separate from the failed detector.
