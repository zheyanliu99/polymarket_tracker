# ColdMath Polymarket Weather Trading Analysis
Generated: 2026-06-12 22:19:42 EDT
Wallet: `0x594edb9112f526fa6a80b8f858a6379c8a2c1c11`
Window: 2025-06-12 22:19:42 EDT through 2026-06-12 22:19:42 EDT.
Data source: public Polymarket Data API endpoints `/trades`, `/activity`, and `/positions`. I used `/closed-positions` and `/positions` to discover weather condition IDs, then hydrated those markets with `/trades?market=...&takerOnly=false` and `/activity?market=...`. The API shows executed public fills and wallet-level events, not hidden orders, forecast inputs, or intent.
## Executive Takeaways
- In the one-year weather pull, I discovered **7,487 weather condition IDs** and hydrated **203,857 weather trades** across **7,487 markets**, with about **$7,107,881.79** of executed notional.
- ColdMath is overwhelmingly a **temperature-market specialist** in this data: the top weather categories are dominated by highest/lowest temperature exact, range, and threshold contracts.
- His style is barbell-like: **cheap tail tickets** plus **near-certain late fills**. Trades priced at <=5c account for **85,555 fills / $38,964.69**, while trades at >=95c account for **51,512 fills / $6,048,326.66**.
- He rarely behaves like a discretionary mid-price bettor: 20-80c weather trades were **5,999 fills / $108,341.57**.
- Timing is short-horizon. Trades on the market date or one day before account for **165,385 fills / $4,902,330.60**.
- I found **3,567 weather markets where he bought both Yes and No**, including **2,351** where the aggregate average complete-set cost was below $1 before fees. That is the clearest evidence of conversion/negative-risk/settlement-aware behavior rather than simple directional copying.
- The one-year closed-position snapshot also shows **9,607 weather outcome rows** with **$9,068,739.79 totalBought** and **$-116,849.77 realizedPnl** in the API fields. Treat that as directional accounting context, not audited net income.
- He is **not literally weather-only** in the one-year position data: I found **849 non-weather closed-position rows**. That said, weather was **95.5%** of closed-position `totalBought`, so the repeatable specialization is still clearly weather.

## Core Metrics
| Metric | Value |
|---|---:|
| Weather trades | 203,857 |
| Weather condition IDs discovered | 7,487 |
| Weather notional | $7,107,881.79 |
| Unique weather markets | 7,487 |
| Unique weather event groups | 2,666 |
| Active weather trading days | 189 |
| Active weather trading months | 7 |
| Buy notional | $6,496,932.34 |
| Sell notional | $610,949.45 |
| Median weather price | 0.0700 |
| Notional-weighted avg weather price | 0.9599 |
| Median weather trade notional | $0.58 |
| Average weather trade notional | $34.87 |

## Price Behavior
| Price bucket | Notional |
|---|---:|
| 95-99c | $3,648,114.43 |
| 99-100c | $2,400,212.23 |
| 80-95c | $883,071.45 |
| 20-80c | $108,341.57 |
| 1-5c | $30,964.73 |
| 5-20c | $30,193.98 |
| <1c | $6,983.40 |

| Price bucket | Fills |
|---|---:|
| 1-5c | 69,345 |
| 95-99c | 42,219 |
| 5-20c | 38,459 |
| 80-95c | 24,638 |
| <1c | 13,904 |
| 99-100c | 9,293 |
| 20-80c | 5,999 |

## Timing vs Weather Date
| Lead bucket | Notional |
|---|---:|
| same day | $3,371,613.04 |
| 1 day before | $1,530,717.56 |
| 2-3 days before | $1,126,565.75 |
| unknown | $756,015.26 |
| 4-7 days before | $245,219.54 |
| after market date | $77,750.63 |

| Lead bucket | Fills |
|---|---:|
| same day | 117,661 |
| 1 day before | 47,724 |
| 2-3 days before | 25,974 |
| 4-7 days before | 6,386 |
| unknown | 5,452 |
| after market date | 660 |

## Market Selection
| Category | Notional |
|---|---:|
| temperature_high_exact | $3,553,930.59 |
| temperature_high_range | $1,900,839.93 |
| temperature_high_threshold | $1,189,078.08 |
| temperature_low_exact | $233,944.63 |
| temperature_low_range | $211,004.38 |
| temperature_low_threshold | $19,084.18 |

| City / location | Notional |
|---|---:|
| New York City | $669,215.22 |
| Atlanta | $625,685.04 |
| Dallas | $610,700.00 |
| Wellington | $525,412.87 |
| Chicago | $427,776.15 |
| Miami | $344,720.84 |
| Lucknow | $312,018.00 |
| Buenos Aires | $300,845.37 |
| Tokyo | $261,578.56 |
| London | $230,921.89 |
| Seattle | $217,428.13 |
| Seoul | $182,542.09 |
| Paris | $157,908.69 |
| Toronto | $145,779.86 |
| Sao Paulo | $141,391.24 |

| Outcome | Notional |
|---|---:|
| No | $5,719,105.44 |
| Yes | $1,388,776.35 |

| Side | Notional |
|---|---:|
| BUY | $6,496,932.34 |
| SELL | $610,949.45 |

## Monthly Weather Notional
| Month | Notional |
|---|---:|
| 2025-12 | $193,401.09 |
| 2026-01 | $533,711.05 |
| 2026-02 | $1,259,658.99 |
| 2026-03 | $2,320,893.87 |
| 2026-04 | $1,770,760.78 |
| 2026-05 | $835,629.91 |
| 2026-06 | $193,826.09 |

## Multi-Bucket / Both-Side Behavior
Weather event groups with two or more market buckets traded: **1,883**. This matters because temperature markets often come as mutually related buckets. Trading multiple buckets is closer to pricing a distribution than making a single yes/no call.

Top multi-market event groups by notional:
| Event | Notional | Fills | Markets |
|---|---:|---:|---:|
| highest-temperature-in-cape-town-on-april-21-2026 | $77,937.75 | 3,877 | 6 |
| highest-temperature-in-wellington-on-march-19-2026 | $70,304.40 | 3,842 | 7 |
| highest-temperature-in-atlanta-on-march-14-2026 | $58,329.42 | 2,807 | 7 |
| highest-temperature-in-chicago-on-march-11-2026 | $49,954.86 | 152 | 5 |
| highest-temperature-in-tokyo-on-march-20-2026 | $44,977.76 | 4,416 | 6 |
| highest-temperature-in-chicago-on-march-26-2026 | $41,355.89 | 782 | 9 |
| highest-temperature-in-chicago-on-april-12-2026 | $39,140.80 | 891 | 9 |
| lowest-temperature-in-nyc-on-june-5-2026 | $37,929.75 | 396 | 8 |
| highest-temperature-in-tel-aviv-on-april-29-2026 | $35,875.47 | 708 | 2 |
| highest-temperature-in-lucknow-on-march-7-2026 | $34,798.70 | 1,017 | 8 |

Top markets where he bought both Yes and No:
| Market | Matched size | Yes avg | No avg | Complete-set cost | Edge before fees |
|---|---:|---:|---:|---:|---:|
| highest-temperature-in-wellington-on-march-28-2026-16c | 29,684.68 | 0.0121 | 0.9546 | 0.9667 | 0.0333 |
| highest-temperature-in-tokyo-on-march-20-2026-16c | 25,407.85 | 0.0109 | 0.9562 | 0.9671 | 0.0329 |
| highest-temperature-in-chicago-on-march-11-2026-54forhigher | 24,880.26 | 0.0027 | 0.9183 | 0.9209 | 0.0791 |
| highest-temperature-in-tokyo-on-march-20-2026-15c | 16,563.75 | 0.0116 | 0.9532 | 0.9648 | 0.0352 |
| highest-temperature-in-wellington-on-march-19-2026-20c | 15,694.02 | 0.0093 | 0.9765 | 0.9858 | 0.0142 |
| highest-temperature-in-wellington-on-march-19-2026-21c | 14,338.62 | 0.0057 | 0.9919 | 0.9976 | 0.0024 |
| highest-temperature-in-cape-town-on-april-21-2026-14c | 14,104.58 | 0.0286 | 0.9705 | 0.9992 | 0.0008 |
| highest-temperature-in-lucknow-on-march-7-2026-39c | 13,836.21 | 0.0059 | 0.9541 | 0.9600 | 0.0400 |
| highest-temperature-in-wellington-on-march-19-2026-19c | 13,220.41 | 0.0128 | 0.9581 | 0.9709 | 0.0291 |
| highest-temperature-in-atlanta-on-february-11-2026-58-59f | 12,271.22 | 0.0011 | 0.9909 | 0.9919 | 0.0081 |
| highest-temperature-in-atlanta-on-march-14-2026-70-71f | 12,145.11 | 0.0068 | 0.9648 | 0.9716 | 0.0284 |
| highest-temperature-in-atlanta-on-february-18-2026-53forbelow | 12,092.82 | 0.0063 | 0.9917 | 0.9979 | 0.0021 |

## Activity And Settlement Mechanics
| Activity type | Count | USDC size |
|---|---:|---:|
| MERGE | 6,102 | $3,037,805.37 |
| REDEEM | 4,535 | $2,977,606.91 |
| SPLIT | 1 | $300.00 |
| TRADE | 203,861 | $7,109,829.54 |

The presence of large MERGE and REDEEM activity is important: it means some of his edge may come from correctly handling complete sets, resolution, and negative-risk mechanics, not only from choosing the right weather outcome.

## Positions Snapshot
Closed/current positions are supporting context and can differ from a pure trade ledger because of merges, redeems, and open inventory.
| Metric | Value |
|---|---:|
| Closed weather outcome rows | 9,607 |
| Closed weather markets | 7,070 |
| Closed-position totalBought | $9,068,739.79 |
| Closed-position realizedPnl | $-116,849.77 |
| Closed-position negative-risk rows | not reported by endpoint |
| Weather positions returned | 1,589 |
| Weather positions ending after cutoff | 1,589 |
| Initial value | $32,321.63 |
| Current value | $719.04 |
| Cash PnL field | $-31,602.59 |
| Realized PnL field | $414,450.05 |
| Redeemable weather positions | 1,586 |

## Inferred Trading Logic
1. **He prices weather as a distribution.** The repeated use of adjacent exact/range/threshold temperature buckets suggests he is not just deciding whether one binary proposition is attractive. He is mapping forecast uncertainty into bucket probabilities.
2. **He likes short time-to-resolution markets.** Same-day and one-day-before activity dominates, which is when public forecast models, observed intraday temperatures, and exchange liquidity can diverge from stale market prices.
3. **He uses a barbell of tails and certainties.** Cheap <=5c buys look like mispriced tail/event-bucket optionality. >=95c buys look like late certainty harvesting or complete-set construction when the remaining uncertainty is tiny.
4. **He appears settlement-aware.** Buying both Yes and No in the same market, trading multiple related buckets in one event, then showing MERGE/REDEEM activity is consistent with negative-risk conversion or complete-set management. Blindly copying only the visible buy leg can miss the hedge.
5. **He usually does not need to sell to monetize.** Low sell notional relative to buy notional points toward holding through resolution, redeeming winners, merging complete sets, or treating contracts as settlement instruments.

## How To Follow Without Overpaying
- Separate trade types: copy-candidates are fresh `TRADE` events; `MERGE` and `REDEEM` are not new predictions.
- Do not chase after price movement. If he buys a 2c bucket and the market moves to 8c, your expected value may be completely different from his.
- Watch whether he is buying a single side or assembling both sides / multiple buckets. The latter may be an arb/conversion structure, not a directional bet.
- For weather markets, compare against the exact resolution source and time convention. A one-degree or one-hour rule detail can flip the edge.

## Files Produced
- raw hydrated weather trades: `data/raw/coldmath_weather_market_trades_365d.json`
- raw hydrated weather activity: `data/raw/coldmath_weather_market_activity_365d.json`
- raw current positions: `data/raw/coldmath_current_positions.json`
- raw closed positions: `data/raw/coldmath_closed_positions_365d.json`
- weather condition ids: `data/processed/coldmath_weather_condition_ids_365d.json`
- classified trades CSV: `data/processed/coldmath_classified_trades_365d.csv`
- weather trades CSV: `data/processed/coldmath_weather_trades_365d.csv`
- analysis report: `reports/coldmath_weather_analysis_365d.md`
