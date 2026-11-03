# Strategy card: Deribit-led cross-exchange options latency

## Hypothesis
Deribit leads BTC/ETH options price discovery (it holds the large majority of
market volume/open interest). Binance Options and Bybit Options are thin
secondary venues whose quoted mark price / implied vol lags Deribit's moves by
some measurable interval. If that lag is (a) wide enough relative to round-trip
execution latency and (b) wide enough relative to spread+fees+slippage on the
lagging venue, there is a tradeable edge: detect the Deribit move, trade the
stale quote on Binance/Bybit before it updates.

## What this is NOT
- Not classical arbitrage between two independent markets (Binance and Bybit
  options are both followers of the same Deribit signal, not independent price
  sources — a direct Binance-vs-Bybit comparison with no Deribit anchor is
  expected to be noise, not edge).
- Not vol-surface relative value (that assumes two markets with genuinely
  different risk premia; weak assumption here given both are followers).

## Test plan, in order — stop at the first gate that fails

### Gate 0 — connectivity (hours, not days)
Confirm, from this machine, that all three of these are actually reachable
with real data, not assumed:
- Deribit public REST/WS (`https://www.deribit.com/api/v2/public/...`,
  `wss://www.deribit.com/ws/api/v2`) — index price, options chain, mark price/IV.
- Binance Options public REST/WS (`eapi.binance.com`) — options chain, mark price.
- Bybit Options public REST/WS (`api.bybit.com` v5 option endpoints) — options
  chain, mark price.
If any venue is unreachable or the options endpoints return near-empty/illiquid
chains (expect Binance/Bybit options volume to be much smaller than their
futures volume), say so plainly and reconsider before building further.

### Gate 1 — does a lag even exist? (one afternoon)
Pick BTC and ETH, the single nearest-expiry ~ATM strike on each venue (don't
require exact strike match — nearest available). Record synchronized
timestamped mark price / IV snapshots from all three venues for a few hours
(polling REST at the fastest safe rate, or WS ticker streams if available).
Cross-correlate Deribit's IV/mark changes against Binance's and Bybit's at
various lags (0ms, 100ms, 500ms, 1s, 5s, ...). Look for: does Binance/Bybit's
series show a statistically detectable delayed response to Deribit moves, and
if so, at what typical lag?
- If there's no detectable lag (correlation peaks at lag 0, or lag structure is
  noise) — stop here. There's no latency edge to chase.
- If a lag exists, note its typical size — this sets the latency budget you'd
  need to beat.

### Gate 2 — can you win the race? (compare to your own infra)
Measure your own realistic round-trip time: time from receiving a Deribit
update to being able to place/cancel an order on Binance/Bybit's options API
from this machine's actual network path. If your round-trip is longer than the
lag found in Gate 1, there is nothing to trade — the lagging venue's own market
makers (who likely also watch Deribit, probably with better infra) will have
already re-quoted before you can act.

### Gate 3 — does the edge survive real costs? (paper trade on recorded data)
Only if Gates 1-2 both pass: simulate the strategy against the recorded public
quotes (not an idealized backtest) — on every detected lag event, "trade" the
lagging venue's actual best bid/ask at that timestamp, then mark to the
lagging venue's own subsequent re-quote (not to Deribit's price — you can only
realize P&L on the venue you traded on). Subtract, explicitly:
- the quoted bid-ask spread at the time of the simulated fill (you cross it),
- the venue's real options taker fee schedule (Binance/Bybit publish these),
- a conservative slippage haircut (simulated fills should not be at the exact
  touch price for any real size — assume partial depth consumption).
Report the net edge per trade and annualized, with sample size and a simple
significance check (e.g. bootstrap or a shuffled-timestamp null), not just a
point estimate.

## Stop/go criteria
- GO only if: a measurable lag exists (Gate 1), your infra can plausibly beat
  it (Gate 2), and the simulated net-of-cost edge is positive with a sample
  large enough that it isn't noise (Gate 3).
- Any gate failing is a legitimate, reportable result — "no edge, here's why"
  is a valid outcome of this research, not a failure to find the prompt's answer.

## Results log

### Gate 0 — connectivity: PASS (2026-10-23, ~12:50 UTC)
Scripts: `scripts/00_connectivity.py`, `00b_binance_ws_probe.py`, `00c_binance_opt_streams.py`,
`01_chain_snapshot.py`. Raw: `data_cache/*/gate0_*`. Results: `results/gate0_*.json|csv`.
- REST reachable on all three (HTTP 200, 200-550 ms cold): Deribit `get_book_summary_by_currency`,
  Binance `eapi/v1/{exchangeInfo,ticker,mark,depth}`, Bybit `v5/market/{tickers,instruments-info,orderbook}`.
- WS: Deribit `ticker.<inst>.100ms` OK; Bybit `v5/public/option` tickers + orderbook.25 OK.
  Binance: old `nbstream.binance.com/eoptions` host returns **404 (retired)**. On `fstream.binance.com`
  (which DID deliver here this time, incl. USD-M futures control streams — unlike the prior project)
  only two option streams deliver: `/market/ws/<und>@optionMarkPrice` (whole chain, **1 s cadence**) and
  `/public/ws/<lowercase-symbol>@bookTicker` (event-driven BBO, ~290 ms median gap). Upper-case symbols,
  `@depth*`, `@optionTicker`, `@trade` all connect but deliver nothing.
- Chain size (live): Deribit BTC 930 / ETH 784 instruments (12 expiries); Binance 638 / 528 (11);
  Bybit 686 / 568 (11). Not near-empty. 24h volume (contracts): Deribit BTC 18.2k / ETH 120k;
  Binance 6.2k / 125k; Bybit 7.0k / 158k.
- **Premise check:** nearest-expiry ATM spreads are *tighter* on Binance/Bybit than on Deribit —
  BTC $5 (1 tick, ~1% of mid) vs Deribit $33-82 (8-15% of mid; Deribit's 0.0001 BTC tick ≈ $8 is
  coarse for short-dated options); ETH $0.1-0.2 vs Deribit $2.4. Touch depth: Binance/Bybit 4-18 BTC,
  hundreds of ETH. "Thin secondary venue" does not describe Binance/Bybit short-dated ATM options.
- Local Windows clock is ~730-780 ms behind all three exchanges (which agree with each other to
  ~20 ms); exchange timestamps are used as the common clock, local times corrected by measured offset.
  (Later in the day it drifted to ~330 ms — always use the live-measured offset.)
- **ISP DNS (Jio) intermittently sinkholes all `*.binance.com` to 49.44.79.236**; public resolvers
  return real AWS IPs. Caused a ~52 min Binance outage mid-recording. User decision: no bypass,
  use Binance only while system DNS resolves it, mask outages.
- **USD vs USDT settlement:** Deribit = USD index; Binance/Bybit = USDT index. USDT ≈ 0.9987 USD
  (Coinbase, Deribit `usdt_usd`, index ratios agree) → a USDT K-strike ≈ USD p·K-strike (~$100 gap on
  BTC). Any Deribit↔USDT-venue comparison must convert, or it shows a fake $20-35 "cross".

### Gate 1 — lag: STATISTICALLY REAL, ECONOMICALLY NEGLIGIBLE → FAIL (final, 2026-10-23)
Full 4 h recording `data_cache/rec_20261023_125702` (outage-masked, 71k samples): Deribit coef t = 4.1-6.7,
shift-null p < 0.005; follower drift after pure Deribit moves +$0.33-1.05/BTC contract (1-5 s). xcorr/event
study files are from the clean pre-outage 44 min.
Scripts: `02_record.py` (4 h, 12 ATM options, 3 venues + perps), `03_leadlag.py`, `03b_incremental.py`.
Results: `results/gate1_*.csv|json`, `results/INTERIM_REPORT_2026-10-23.md`.
- The underlying perp leads every option venue. Bybit/Binance option mids respond at +50-100 ms;
  Deribit's at +100 ms (exchange clock) / +450-500 ms as received here.
- Deribit→Binance/Bybit xcorr peaks at lag 0 or negative; LLR < 1 in most cells; when a Deribit move
  arrives here, followers have already done 65-85 % of their net move over [-5 s, +10 s] (event-study ratio 0.40-0.48 at arrival vs 0.57-0.61 at +10 s; corrected 2026-11-01).
- Incremental regression (3 h, 60k samples, perp + own-lag controlled): Deribit coefficient t = 4-6.5,
  circular-shift null p < 0.005. Real, but after a "pure" Deribit move the followers drift only
  $0.33-1.08 per BTC contract over 1-5 s vs ≈ $54 round-trip cost (fees $24.5/side + spread).
- Deribit option ticker arrives ~640 ms (p50) / 1.3 s (p95) after its own timestamp here.

### Gate 2 — latency: FAIL
Warm REST RTT p50: Bybit 138 ms, Binance 211 ms, Deribit 255 ms (`scripts/04_latency.py`,
`results/gate2_latency.json`). Earliest reaction to a Deribit move ≈ 0.7-0.9 s; followers have mostly
re-quoted by then.

### Gate 3 — live paper trade (user-requested despite gate fails): no edge so far (1.5 of 3 h)
`scripts/05_paper_trade.py` + `src/paper/engine.py`, report `scripts/06_paper_report.py` →
`results/paper/<run>/report.md`. 56 instruments, latency-aware IOC fills against the exchange-clock book.
- S1 latency-follow: Bybit −$23.6/trade [−23.9, −23.2] (40.8k trades), Binance −$39.5/trade (13.7k);
  gross before costs already −$4 to −$6; signal vs random-direction null differs by ≤ $0.05 (p ≥ 0.08).
  Zero-latency oracle variants no better.
- S2 crossed-market arb: Deribit↔Bybit/Binance never crossed after USDT basis. Binance↔Bybit crossed
  77k quote updates, all killed by costs: max cross $40 BTC / $1.10 ETH vs hurdle ≈ $82 / $2.4. 0 trades.

### Follow-up idea 1 — full-chain crossed-market scan incl. cheap wings: NO EDGE (2026-10-23)
`scripts/07_wing_scan.py` → `results/wing_scan/`, `results/wing_scan_10min.log`. ~4,100 instruments per
snapshot (Deribit 1716, Bybit 1256, Binance 1166), 20 s snapshots, candidates re-checked on simultaneously
fetched books. Premium-capped fees, delta-weighted exercise fee + settlement haircut, USDT strike gap.
- ~220 gross crosses per snapshot, ~140 net-positive — ALL long-dated (4-11 months) deep-ITM, one direction
  (buy Deribit, sell Bybit), $900-2,200/contract. That is interest-rate carry (Deribit inverse options priced
  off Deribit's futures curve vs Bybit USDT discounting): **1.9-3.1 %/yr on locked capital, below T-bills**,
  plus a year of two-exchange counterparty risk. Not an arb.
- ≤30-day expiries: 2 real crosses in 10 min, both min-tick artifacts on near-worthless options
  (e.g. 102k call: buy Binance $5, sell Deribit $8.17 = Deribit's 0.0001 BTC floor): $3.21 + $1.64 total. Dust.

### Follow-up idea 2 — expiry TWAP-settlement mispricing: NOT BACKTESTABLE FROM FREE DATA
Deribit dailies settle on 07:30-08:00 UTC index TWAP (`get_delivery_prices` works). But the expiring
daily barely trades at the end (10-29 BTC trades in the final hour on 20-23 Oct; last trade 10-32 min
before expiry). Needs live 3-venue quote recording 07:00-08:00 UTC for weeks (1 sample/morning).

### Idea 3 — maker-side quoting: not viable at retail fees
Maker ~0.02 % of index ≈ $16/BTC contract/side vs a $5 touch spread. Only inside an exchange MM program
(rebates) — not testable with public data.

### Gate 3 — FINAL 3 h live paper trade (2026-10-23 15:09-18:09 UTC): NO EDGE
Run `results/paper/20261023_150900/report.md` (raw trades `data_cache/paper/20261023_150900/`).
56 instruments; Binance live 78 % of minutes (DNS outages masked); USDT/USD 0.9988.
- S1 latency-follow, real latency: Bybit 1 s −$20.76/trade [−21.19, −20.34] (92,348 trades), 5 s −$20.93,
  30 s −$21.30; Binance 1 s −$34.94 [−35.59, −34.31] (41,443), 5 s −$35.24, 30 s −$35.99. Win rate 0 %.
  Gross before costs −$3.8 to −$6.3/trade. Signal vs random direction: +$0.02-0.06/trade (Bybit 1 s
  p = 0.002 — the real-but-tiny Deribit information from Gate 1, ~0.2 % of fees).
- S2 crossed-market arb: 0 trades. Binance↔Bybit crossed on 171k quote updates, all killed by costs
  (BTC max $40 vs ≈ $82 hurdle; ETH max $1.10 vs ≈ $2.4). Deribit↔USDT venues: 9 crossings, all killed.
**Overall verdict: NO GO.**

### Follow-up idea 4 — Polymarket "Bitcoin above $K" digitals vs Deribit-implied probability: NO EDGE (2026-10-27)
`scripts/08_polymarket_vs_deribit.py` → `results/pm_vs_deribit/`. Polymarket daily markets (11 strikes,
$70k-670k liquidity, feesEnabled=False) resolve on ONE Binance BTC/USDT 1m close at 12:00 ET (16:00 UTC;
17:00 UTC after 1 Nov). Deribit settles 08:00 UTC USD TWAP → 8 h gap + USDT/USD + strike spacing ⇒ no exact
hedge exists; any trade is hedged relative value with large residual risk on ATM strikes.
- Fair P from Deribit smile (variance interpolated between bracketing expiries, USDT strike conversion,
  smile-slope digital correction). 6 events × ~10 strikes, 30 s snapshots: mean |mid − fair| 0.0-0.5 ¢;
  max 2-4.4 ¢, only at the 1-2 strikes nearest spot (PM implies slightly higher short-tenor vol — inside
  model error: intraday vol seasonality, single-candle vs TWAP). Touch depth at those strikes 17-1,416 shares.
- Intra-Polymarket monotonicity arbs (riskless): 0.
Verdict: Polymarket BTC dailies are already market-made off Deribit; deviations are model-error-sized and
capacity ~$10-50/day. Also: polymarket.com website is DNS-sinkholed by the ISP (APIs gamma/clob reachable).

### Gate 3 — repeat live paper trade, different day (2026-11-02 18:02-18:20 UTC, stopped early): NO EDGE
Run `results/paper/20261102_180156/report.md` (planned 11 h; stopped after 18 min, result unambiguous). Same 56-option
setup, BTC $77.1k / ETH $2,256, Binance live 100 %. Added `--dash` live terminal view (`src/paper/dash.py`).
- S1 real latency: Bybit 1 s −$32.08/trade [−32.38, −31.77] (10,886 trades), 5 s −$32.10, 30 s −$32.18;
  Binance 1 s −$44.43 [−45.01, −43.91] (6,759), 5 s −$45.01, 30 s −$46.41. 0 wins in 27,458 trades.
  Signal vs random direction: no variant significant (p ≥ 0.37).
- Worse than 23 Oct because options were ~3× pricier (Bybit BTC avg premium $504 vs $172), so the 7 % premium
  fee cap no longer bound: fees $34.9 vs $20.4 per BTC trade. Gross before costs similar (−$7.4 vs −$6.1).
- S2: Binance↔Bybit crossed on 26,742 updates, all killed by costs (max BTC $19.97 vs ≈ $73 + slippage hurdle). 0 trades.
- Both runs combined: 236,146 S1 paper trades, 4 net-positive; 197,866 S2 crosses, 0 trades.
